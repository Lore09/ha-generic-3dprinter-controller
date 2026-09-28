"""The timelapse: a frame per layer while printing, one video when the job ends."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from homeassistant.components.camera import Image
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.generic_3dprinter import timelapse as timelapse_module
from custom_components.generic_3dprinter.const import LightChannel, PrintState, ProtocolId
from custom_components.generic_3dprinter.models import PrinterSnapshot
from custom_components.generic_3dprinter.timelapse import EVENT_TIMELAPSE, Timelapse

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def _snapshot(state: PrintState, layer: int | None = None, *, lit: bool = False) -> PrinterSnapshot:
    return PrinterSnapshot(
        protocol=ProtocolId.ANYCUBIC_KOBRA,
        connected=True,
        print_state=state,
        current_layer=layer,
        lights=frozenset({LightChannel.CHAMBER}) if lit else frozenset(),
    )


@pytest.fixture(name="jpeg", scope="module")
def jpeg_fixture(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    if shutil.which("ffmpeg") is None:
        return b""
    path = tmp_path_factory.mktemp("frame") / "frame.jpg"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=teal:s=63x35", "-frames:v", "1", str(path)],
        check=True,
    )
    return path.read_bytes()


@pytest.fixture(name="timelapse")
def timelapse_fixture(hass: HomeAssistant, tmp_path: Path, jpeg: bytes, monkeypatch: pytest.MonkeyPatch) -> Timelapse:
    hass.config.media_dirs = {"local": str(tmp_path)}
    taken: list[str] = []

    async def fake_get_image(hass: HomeAssistant, entity_id: str) -> Image:
        taken.append(entity_id)
        return Image("image/jpeg", jpeg)

    monkeypatch.setattr(timelapse_module, "async_get_image", fake_get_image)
    recorder = Timelapse(hass, "Kobra X", lambda: "camera.kobra_x")
    recorder.taken = taken  # type: ignore[attr-defined]
    return recorder


async def _feed(hass: HomeAssistant, recorder: Timelapse, *snapshots: PrinterSnapshot) -> None:
    for snapshot in snapshots:
        recorder.observe(snapshot)
        await hass.async_block_till_done(wait_background_tasks=True)


@needs_ffmpeg
async def test_a_print_becomes_one_video_of_a_frame_per_layer(hass: HomeAssistant, timelapse: Timelapse, tmp_path: Path) -> None:
    events = async_capture_events(hass, EVENT_TIMELAPSE)
    await _feed(
        hass,
        timelapse,
        _snapshot(PrintState.PREPARING),
        _snapshot(PrintState.PRINTING, 1),
        _snapshot(PrintState.PRINTING, 1),
        _snapshot(PrintState.PRINTING, 2),
        _snapshot(PrintState.PAUSED, 2),
        _snapshot(PrintState.UNKNOWN),
        _snapshot(PrintState.PRINTING, 3),
    )
    assert (timelapse.recording, timelapse.frames) == (True, 3)

    await _feed(hass, timelapse, _snapshot(PrintState.FINISHED))

    assert timelapse.taken == ["camera.kobra_x"] * 3
    (event,) = events
    video = Path(event.data["path"])
    assert video.parent == tmp_path / "generic_3dprinter" / "kobra_x"
    assert video.suffix == ".mp4" and video.stat().st_size > 0
    assert not video.with_suffix("").exists(), "the frames are removed once the video is made"
    assert event.data["frames"] == 3
    assert event.data["media_content_id"] == f"media-source://media_source/local/generic_3dprinter/kobra_x/{video.name}"
    assert (timelapse.recording, timelapse.last_video) == (False, video)


async def test_a_job_that_never_got_going_leaves_nothing(hass: HomeAssistant, timelapse: Timelapse, tmp_path: Path) -> None:
    events = async_capture_events(hass, EVENT_TIMELAPSE)
    await _feed(hass, timelapse, _snapshot(PrintState.PRINTING, 0), _snapshot(PrintState.IDLE))
    assert events == []
    assert list((tmp_path / "generic_3dprinter" / "kobra_x").iterdir()) == []


async def test_a_printer_without_layers_gets_a_frame_per_interval(
    hass: HomeAssistant, timelapse: Timelapse, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(timelapse_module, "monotonic", lambda: now[0])
    await _feed(hass, timelapse, _snapshot(PrintState.PRINTING), _snapshot(PrintState.PRINTING))
    now[0] += timelapse_module.FALLBACK_INTERVAL
    await _feed(hass, timelapse, _snapshot(PrintState.PRINTING))
    assert timelapse.frames == 2


async def test_a_camera_that_fails_skips_the_frame(hass: HomeAssistant, timelapse: Timelapse, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeassistant.exceptions import HomeAssistantError

    async def broken(hass: HomeAssistant, entity_id: str) -> Image:
        raise HomeAssistantError("no frame")

    monkeypatch.setattr(timelapse_module, "async_get_image", broken)
    await _feed(hass, timelapse, _snapshot(PrintState.PRINTING, 1))
    assert (timelapse.recording, timelapse.frames) == (True, 0)


@pytest.fixture(name="light")
def light_fixture(timelapse: Timelapse, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record the light commands, and the frame count each one was sent at."""
    monkeypatch.setattr(timelapse_module, "LIGHT_SETTLE", 0)
    sent: list[object] = []

    async def set_light(on: bool) -> None:
        sent.append((on, timelapse.frames))

    timelapse.set_light = set_light
    timelapse.with_light = True
    return sent


async def test_a_job_started_in_the_dark_is_lit_for_its_frames(hass: HomeAssistant, timelapse: Timelapse, light: list) -> None:
    await _feed(hass, timelapse, _snapshot(PrintState.PRINTING, 1), _snapshot(PrintState.PRINTING, 2, lit=True))
    assert light == [(True, 0)], "the light goes on before the first frame"
    await timelapse.async_finish()
    assert light == [(True, 0), (False, 0)]


async def test_a_light_already_on_is_left_alone(hass: HomeAssistant, timelapse: Timelapse, light: list) -> None:
    await _feed(hass, timelapse, _snapshot(PrintState.PRINTING, 1, lit=True), _snapshot(PrintState.PRINTING, 2, lit=True))
    await timelapse.async_finish()
    assert light == []


async def test_a_refused_light_still_records(hass: HomeAssistant, timelapse: Timelapse, light: list) -> None:
    from homeassistant.exceptions import HomeAssistantError

    async def refused(on: bool) -> None:
        light.append(on)
        raise HomeAssistantError("refused")

    timelapse.set_light = refused
    await _feed(hass, timelapse, _snapshot(PrintState.PRINTING, 1))
    assert timelapse.frames == 1
    await timelapse.async_finish()
    assert light == [True], "a light that never went on is not switched off"

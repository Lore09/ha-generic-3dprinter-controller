"""A printer whose camera is a native video stream, played by Home Assistant.

The MJPEG relay stays for printers that serve JPEG frames. A printer with
CAMERA_STREAM instead hands Home Assistant a stream URL: its camera entity plays
it through the stream component, its stills come from ffmpeg, and the card is told
to embed that entity rather than an MJPEG URL.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.components.camera import CameraEntityFeature
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_3dprinter.camera import Generic3DPrinterStreamCamera
from custom_components.generic_3dprinter.const import DOMAIN, Capability, Command, ProtocolId
from custom_components.generic_3dprinter.models import PrinterSnapshot
from custom_components.generic_3dprinter.protocols import Protocol, UnreachableError, parse_config
from custom_components.generic_3dprinter.runtime import PrinterRuntime

STREAM_URL = "http://192.0.2.5:18088/live/token"


class StreamProtocol(Protocol):
    """A printer whose camera starts on request and answers with a URL."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        config = parse_config({"name": "Kobra", "protocol": "web_only", "host": "192.0.2.5"})
        super().__init__(config, session, granted=frozenset({Capability.CAMERA_STREAM}))
        self.starts = 0

    async def async_setup(self) -> None:
        return None

    async def async_teardown(self) -> None:
        return None

    async def _async_read(self) -> PrinterSnapshot:
        return PrinterSnapshot(protocol=ProtocolId.WEB_ONLY, connected=True, camera=True)

    async def _async_dispatch(self, command: Command, params: Mapping[str, Any]) -> None:
        return None

    async def async_list_files(self):
        return []

    async def async_upload_file(self, name, stream, *, size=None):
        raise NotImplementedError

    async def async_stream_source(self) -> str:
        self.starts += 1
        return STREAM_URL


@pytest.fixture(name="adapter")
async def adapter_fixture():
    async with aiohttp.ClientSession() as session:
        yield StreamProtocol(session)


def _runtime(hass: HomeAssistant, adapter: Protocol) -> PrinterRuntime:
    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="stream1")
    entry.add_to_hass(hass)
    tokens = MagicMock()
    tokens.async_url.side_effect = lambda **kwargs: f"/signed/{kwargs['scope']}"
    return PrinterRuntime(
        hass=hass,
        entry=entry,
        config=adapter.config,
        adapter=adapter,
        session=adapter.session,
        tokens=tokens,
        web_proxy=MagicMock(),
    )


def _camera(runtime: PrinterRuntime) -> Generic3DPrinterStreamCamera:
    coordinator = SimpleNamespace(
        runtime=runtime,
        config_entry=runtime.entry,
        last_update_success=True,
    )
    return Generic3DPrinterStreamCamera(coordinator)  # type: ignore[arg-type]


async def test_the_default_adapter_has_no_stream(hass: HomeAssistant) -> None:
    async with aiohttp.ClientSession() as session:
        from custom_components.generic_3dprinter.adapters.web_only import WebOnlyProtocol

        config = parse_config({"name": "w", "protocol": "web_only", "host": "127.0.0.1"})
        with pytest.raises(UnreachableError):
            await WebOnlyProtocol(config, session, granted=frozenset()).async_stream_source()


async def test_a_stream_printer_is_described_without_an_mjpeg_url(
    hass: HomeAssistant, adapter: StreamProtocol
) -> None:
    runtime = _runtime(hass, adapter)
    description = runtime.describe()
    assert description["camera_kind"] == "stream"
    assert description["camera"] is True
    assert description["camera_url"] is None
    assert description["snapshot_url"] is None
    assert runtime.has_camera is False
    assert runtime.has_stream is True


async def test_the_stream_camera_plays_the_adapters_source(
    hass: HomeAssistant, adapter: StreamProtocol
) -> None:
    camera = _camera(_runtime(hass, adapter))
    assert CameraEntityFeature.STREAM in camera.supported_features
    assert await camera.stream_source() == STREAM_URL


async def test_the_webrtc_probe_does_not_start_the_camera(
    hass: HomeAssistant, adapter: StreamProtocol
) -> None:
    """Starting the capture switches a Kobra's light on, so only playback may start it."""
    camera = _camera(_runtime(hass, adapter))
    await camera.async_refresh_providers(write_state=False)
    assert adapter.starts == 0


async def test_a_still_comes_from_ffmpeg_and_a_failure_is_no_still(
    hass: HomeAssistant, adapter: StreamProtocol
) -> None:
    camera = _camera(_runtime(hass, adapter))
    with patch(
        "custom_components.generic_3dprinter.camera.async_still_from_stream",
        AsyncMock(return_value=b"\xff\xd8jpeg"),
    ) as grab:
        assert await camera.async_camera_image() == b"\xff\xd8jpeg"
    assert grab.await_args.args[1] == STREAM_URL
    with patch(
        "custom_components.generic_3dprinter.camera.async_still_from_stream",
        AsyncMock(side_effect=KeyError("ffmpeg")),
    ):
        assert await camera.async_camera_image() is None


async def test_a_still_reuses_the_running_stream_and_restarts_a_dead_one(
    hass: HomeAssistant, adapter: StreamProtocol
) -> None:
    """Restarting the capture cuts off a viewer, so a timelapse must not do it per frame."""
    camera = _camera(_runtime(hass, adapter))
    target = "custom_components.generic_3dprinter.camera.async_still_from_stream"
    with patch(target, AsyncMock(return_value=b"\xff\xd8jpeg")):
        assert await camera.async_camera_image() == b"\xff\xd8jpeg"
        assert await camera.async_camera_image() == b"\xff\xd8jpeg"
    assert adapter.starts == 1
    with patch(target, AsyncMock(side_effect=[None, b"\xff\xd8again"])):
        assert await camera.async_camera_image() == b"\xff\xd8again"
    assert adapter.starts == 2

"""The resin camera behind its opt-in: when it may open, the ffmpeg it runs over UDP, and the
graceful stop (q, then SIGTERM, never SIGKILL while either works) before 386 Enable 0."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest

from custom_components.generic_3dprinter.adapters import rtsp_frames, sdcp, sdcp_resin
from custom_components.generic_3dprinter.adapters.rtsp_frames import RtspFrames, ffmpeg_args
from custom_components.generic_3dprinter.adapters.sdcp_resin import SdcpResinProtocol
from custom_components.generic_3dprinter.const import Capability
from custom_components.generic_3dprinter.protocols import CommandRejectedError, UnreachableError, parse_config
from custom_components.generic_3dprinter.registry import build_adapter
from custom_components.generic_3dprinter.runtime import CameraHub
from tests.fake_resin_printer import FakeResinPrinter, load_fixture

SATURN = load_fixture()["attributes"]["Attributes"]["MainboardID"]
JPEG = b"\xff\xd8frame\xff\xd9"
STREAMED = b"\xff\xd8streamed\xff\xd9"
#: The printer names itself in VideoUrl; the adapter must reach it at the entry's address.
REACHED_URL = "rtsp://127.0.0.1:554/video"


class FakeStdin:
    """What ffmpeg reads its keys from."""

    def __init__(self, process: FakeFfmpeg) -> None:
        self._process = process

    def write(self, data: bytes) -> None:
        self._process.log.append(data.decode())
        if "q" in self._process.obeys:
            self._process.exit(0)

    async def drain(self) -> None:
        return None


class FakeFfmpeg:
    """A fake ffmpeg process: it writes the frames it is given, and quits on the signals it obeys."""

    def __init__(self, args: tuple[str, ...], printer: FakeResinPrinter | None, obeys: set[str]) -> None:
        self.args = args
        self.printer = printer
        self.obeys = obeys
        self.log: list[str] = []
        self.stdout = asyncio.StreamReader()
        self.stdin = FakeStdin(self)
        self.returncode: int | None = None
        #: The 386 ``Enable`` values the printer had seen when this process ended.
        self.enables_at_exit: list[Any] | None = None
        self._exited = asyncio.Event()

    def feed(self, *frames: bytes) -> None:
        for frame in frames:
            self.stdout.feed_data(frame)

    def exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self.enables_at_exit = list(self.printer.video_enables) if self.printer else None
            self.stdout.feed_eof()
            self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.log.append("TERM")
        if "TERM" in self.obeys:
            self.exit(-15)

    def kill(self) -> None:
        self.log.append("KILL")
        self.exit(-9)


class Spawner:
    """Starts fake ffmpeg processes that each write ``frames`` and obey ``obeys``."""

    def __init__(self, printer: FakeResinPrinter | None = None, *, frames: tuple[bytes, ...] = (JPEG,),
                 obeys: tuple[str, ...] = ("q",)) -> None:
        self.printer = printer
        self.frames = frames
        self.obeys = set(obeys)
        self.processes: list[FakeFfmpeg] = []

    async def __call__(self, *args: str) -> FakeFfmpeg:
        process = FakeFfmpeg(args, self.printer, self.obeys)
        process.feed(*self.frames)
        self.processes.append(process)
        return process


@pytest.fixture(name="session")
async def session_fixture() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture(name="printing")
async def printing_fixture() -> AsyncIterator[FakeResinPrinter]:
    server = FakeResinPrinter(printing=True)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture(autouse=True)
def quick_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rtsp_frames, "STOP_WAIT", 0.05)


def _adapter(printer: FakeResinPrinter, session: aiohttp.ClientSession, spawner: Spawner | None = None,
             *, opted_in: bool = True) -> SdcpResinProtocol:
    config = parse_config(
        {"name": "Saturn", "protocol": "sdcp_resin", "host": "127.0.0.1", "port": printer.port, "serial": SATURN,
         "unsafe_enabled": ["sdcp_resin_camera"] if opted_in else []}
    )
    adapter = build_adapter(config, session)
    assert isinstance(adapter, SdcpResinProtocol)
    if spawner is not None:
        adapter.spawn_ffmpeg = spawner
    return adapter


def _video(printer: FakeResinPrinter) -> list[Any]:
    """Return every 386 ``Data`` the printer got, in order."""
    return [item["Data"] for item in printer.received if item["Cmd"] == 386]


# -------------------------------------------------------------- the ffmpeg


def test_ffmpeg_reads_rtsp_over_udp_with_wall_clock_timestamps() -> None:
    still = ffmpeg_args("ffmpeg", REACHED_URL, still=True)
    stream = ffmpeg_args("ffmpeg", REACHED_URL, still=False)
    for args in (still, stream):
        joined = " ".join(args)
        assert "-rtsp_transport udp" in joined
        assert "-use_wallclock_as_timestamps 1 -fflags nobuffer -err_detect ignore_err" in joined
        assert "-an -c:v mjpeg" in joined
        assert args[args.index("-i") + 1] == REACHED_URL
        assert "-probesize" not in args and "tcp" not in args and "-nostdin" not in args
        assert args[-3:] == ["-f", "image2pipe", "pipe:1"]
    assert "-frames:v 1" in " ".join(still) and "-r" not in still
    assert "-r 2" in " ".join(stream) and "-frames:v" not in stream


async def test_frames_are_split_across_reads() -> None:
    spawner = Spawner(frames=(b"junk\xff\xd8one", b"\xff\xd9\xff\xd8two\xff\xd9"))
    reader = RtspFrames("ffmpeg", REACHED_URL, still=False, spawn=spawner)
    await reader.async_start()
    frames = reader.async_frames()
    assert [await anext(frames), await anext(frames)] == [b"\xff\xd8one\xff\xd9", b"\xff\xd8two\xff\xd9"]
    await frames.aclose()
    await reader.async_stop()
    assert spawner.processes[0].log == ["q"]


@pytest.mark.parametrize(
    ("obeys", "log"),
    [(("q",), ["q"]), (("TERM",), ["q", "TERM"]), ((), ["q", "TERM", "KILL"])],
    ids=["q", "term", "kill"],
)
async def test_ffmpeg_is_stopped_with_q_then_term_and_killed_last(
    obeys: tuple[str, ...], log: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    spawner = Spawner(obeys=obeys)
    reader = RtspFrames("ffmpeg", REACHED_URL, still=False, spawn=spawner)
    await reader.async_start()
    with caplog.at_level(logging.WARNING):
        await reader.async_stop()
    assert spawner.processes[0].log == log
    assert ("switched off and on" in caplog.text) is ("KILL" in log)


async def test_the_stop_drains_ffmpeg_once_a_viewer_stops_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """ffmpeg quits slowly; while it does, its output is drained even after the viewer left."""
    monkeypatch.setattr(rtsp_frames, "STOP_WAIT", 5.0)
    monkeypatch.setattr(rtsp_frames, "DRAIN_RETRY", 0.001)
    spawner = Spawner(obeys=())
    reader = RtspFrames("ffmpeg", REACHED_URL, still=False, spawn=spawner)
    await reader.async_start()
    frames = reader.async_frames()
    assert await anext(frames) == JPEG
    viewer = asyncio.create_task(anext(frames))
    await asyncio.sleep(0)
    (process,) = spawner.processes
    assert process.stdout._waiter is not None  # noqa: SLF001 - the viewer waits in read()
    stop = asyncio.create_task(reader.async_stop())
    await asyncio.sleep(0.01)
    viewer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await viewer
    process.feed(b"x" * 1000)
    for _ in range(100):
        if not process.stdout._buffer:  # noqa: SLF001
            break
        await asyncio.sleep(0.01)
    assert not process.stdout._buffer  # noqa: SLF001
    process.exit(0)
    await asyncio.wait_for(stop, 5)
    assert process.log == ["q"]


async def test_no_frame_in_time_is_an_unreachable_camera(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rtsp_frames, "FRAME_TIMEOUT", 0.05)
    reader = RtspFrames("ffmpeg", REACHED_URL, still=True, spawn=Spawner(frames=()))
    await reader.async_start()
    with pytest.raises(UnreachableError, match="no frame"):
        await anext(reader.async_frames())
    await reader.async_stop()


async def test_a_missing_ffmpeg_is_an_unreachable_camera() -> None:
    async def spawn(*args: str) -> Any:
        raise FileNotFoundError(args[0])

    with pytest.raises(UnreachableError, match="cannot run ffmpeg"):
        await RtspFrames("/nowhere/ffmpeg", REACHED_URL, still=True, spawn=spawn).async_start()


# ------------------------------------------------------------- the adapter


async def test_without_the_opt_in_there_is_no_camera(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    spawner = Spawner(resin_printer)
    adapter = _adapter(resin_printer, session, spawner, opted_in=False)
    snapshot = await adapter.async_read()
    assert Capability.CAMERA not in snapshot.capabilities
    assert snapshot.camera is False
    assert [feature.id for feature in adapter.unsafe_features] == ["sdcp_resin_start_print", "sdcp_resin_camera"]
    before = list(resin_printer.sent_commands)
    with pytest.raises(UnreachableError, match="allowed"):
        await adapter.async_camera_frame()
    assert resin_printer.sent_commands == before
    assert spawner.processes == []
    await adapter.async_teardown()


async def test_a_still_opens_the_camera_for_one_frame_and_switches_it_off(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    spawner = Spawner(resin_printer)
    adapter = _adapter(resin_printer, session, spawner)
    snapshot = await adapter.async_read()
    assert Capability.CAMERA in snapshot.capabilities
    assert snapshot.camera is True
    before = len(resin_printer.sent_commands)

    assert await adapter.async_camera_frame() == JPEG

    assert resin_printer.sent_commands[before:] == [1, 0, 386, 386]
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}]
    (process,) = spawner.processes
    assert process.args[process.args.index("-i") + 1] == REACHED_URL
    assert "-frames:v" in process.args
    # ffmpeg had ended, and sent its TEARDOWN, before the video was switched off.
    assert process.enables_at_exit == [1]
    assert process.log == ["q"]
    assert resin_printer.forbidden == []
    await adapter.async_teardown()


async def test_a_stream_stops_with_q_then_term_and_then_switches_the_video_off(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    spawner = Spawner(resin_printer, frames=(JPEG, JPEG), obeys=("TERM",))
    adapter = _adapter(resin_printer, session, spawner)
    stream = adapter.async_camera_stream()
    assert [await anext(stream), await anext(stream)] == [JPEG, JPEG]
    assert _video(resin_printer) == [{"Enable": 1}]
    await stream.aclose()

    (process,) = spawner.processes
    assert "-r" in process.args
    assert process.log == ["q", "TERM"]
    assert process.enables_at_exit == [1]
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}]
    await adapter.async_teardown()


async def test_a_cancelled_stream_still_stops_ffmpeg_and_switches_the_video_off(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    """A viewer leaving cancels the reader; the release must finish all the same."""
    spawner = Spawner(resin_printer, obeys=("TERM",))
    adapter = _adapter(resin_printer, session, spawner)
    first = asyncio.Event()

    async def watch() -> None:
        async for _frame in adapter.async_camera_stream():
            first.set()

    task = asyncio.create_task(watch())
    await asyncio.wait_for(first.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spawner.processes[0].log == ["q", "TERM"]
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}]
    await adapter.async_teardown()


@pytest.mark.parametrize("case", ["one_slot_used", "no_camera", "stale_attributes"])
async def test_the_camera_is_not_opened_unless_the_printer_is_free_for_it(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdcp, "PUSH_TIMEOUT", 0.2)
    spawner = Spawner(resin_printer)
    adapter = _adapter(resin_printer, session, spawner)
    await adapter.async_read()
    if case == "one_slot_used":
        resin_printer.video_streams = 1
    elif case == "no_camera":
        resin_printer.attributes["CameraStatus"] = 0
    else:
        resin_printer.withhold_attributes = True
    with pytest.raises(UnreachableError):
        await adapter.async_camera_frame()
    assert 386 not in resin_printer.sent_commands
    assert spawner.processes == []
    assert resin_printer.forbidden == []
    await adapter.async_teardown()


@pytest.mark.parametrize(("layer", "opens"), [(0, False), (2, False), (3, True)])
async def test_the_camera_waits_for_layer_3_of_a_print(
    printing: FakeResinPrinter, session: aiohttp.ClientSession, layer: int, opens: bool
) -> None:
    printing.status["PrintInfo"]["CurrentLayer"] = layer
    spawner = Spawner(printing)
    adapter = _adapter(printing, session, spawner)
    if opens:
        assert await adapter.async_camera_frame() == JPEG
        assert _video(printing) == [{"Enable": 1}, {"Enable": 0}]
    else:
        with pytest.raises(UnreachableError, match="layer 3"):
            await adapter.async_camera_frame()
        assert 386 not in printing.sent_commands
    assert printing.forbidden == []
    await adapter.async_teardown()


async def test_a_second_open_within_10_s_is_refused_before_the_wire(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdcp_resin, "STILL_MAX_AGE", 0.0)
    spawner = Spawner(resin_printer)
    adapter = _adapter(resin_printer, session, spawner)
    await adapter.async_camera_frame()
    before = list(resin_printer.sent_commands)
    with pytest.raises(UnreachableError, match="less than 10 s"):
        await adapter.async_camera_frame()
    assert resin_printer.sent_commands == before
    assert len(spawner.processes) == 1

    adapter._video_opened_at -= 10.5  # noqa: SLF001
    assert await adapter.async_camera_frame() == JPEG
    assert len(spawner.processes) == 2
    await adapter.async_teardown()


async def test_no_video_url_is_refused_and_the_video_switched_off(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    resin_printer.video_url = None
    spawner = Spawner(resin_printer)
    adapter = _adapter(resin_printer, session, spawner)
    with pytest.raises(UnreachableError, match="no RTSP address"):
        await adapter.async_camera_frame()
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}]
    assert spawner.processes == []
    await adapter.async_teardown()


async def test_a_refused_enable_says_why_and_is_switched_off(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    resin_printer.acks[386] = 2
    spawner = Spawner(resin_printer)
    adapter = _adapter(resin_printer, session, spawner)
    with pytest.raises(CommandRejectedError, match="the printer has no camera"):
        await adapter.async_camera_frame()
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}]
    assert spawner.processes == []
    await adapter.async_teardown()


async def test_two_stills_within_a_minute_open_the_camera_once(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home Assistant asks for a still every 10 s while a dashboard shows the camera."""
    assert sdcp_resin.STILL_MAX_AGE == 60.0
    spawner = Spawner(resin_printer)
    adapter = _adapter(resin_printer, session, spawner)
    assert await adapter.async_camera_frame() == JPEG
    sent = list(resin_printer.sent_commands)
    for _ in range(3):
        assert await adapter.async_camera_frame() == JPEG
    assert resin_printer.sent_commands == sent
    assert len(spawner.processes) == 1

    # Once the still is older than STILL_MAX_AGE, the next one opens the camera again.
    monkeypatch.setattr(sdcp_resin, "STILL_MAX_AGE", 0.2)
    monkeypatch.setattr(sdcp_resin, "VIDEO_SPACING", 0.1)
    await asyncio.sleep(0.25)
    spawner.frames = (STREAMED,)
    assert await adapter.async_camera_frame() == STREAMED
    assert len(spawner.processes) == 2
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}] * 2
    await adapter.async_teardown()


async def test_a_still_during_a_stream_without_a_frame_yet_is_refused(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdcp_resin, "STILL_MAX_AGE", 0.0)
    spawner = Spawner(resin_printer, frames=())
    adapter = _adapter(resin_printer, session, spawner)
    stream = adapter.async_camera_stream()
    waiting = asyncio.create_task(anext(stream))
    for _ in range(100):
        if spawner.processes:
            break
        await asyncio.sleep(0.01)
    with pytest.raises(UnreachableError, match="already open"):
        await adapter.async_camera_frame()
    spawner.processes[0].feed(STREAMED)
    assert await asyncio.wait_for(waiting, 5) == STREAMED
    assert await adapter.async_camera_frame() == STREAMED
    await stream.aclose()
    assert len(spawner.processes) == 1
    await adapter.async_teardown()


async def test_a_still_during_a_live_stream_is_its_latest_frame_and_opens_nothing(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdcp_resin, "STILL_MAX_AGE", 0.0)
    spawner = Spawner(resin_printer, frames=(STREAMED,))
    adapter = _adapter(resin_printer, session, spawner)
    await adapter.async_read()
    hub = CameraHub(adapter)
    viewer = hub.async_subscribe()
    assert await asyncio.wait_for(anext(viewer), 5) == STREAMED
    sent = list(resin_printer.sent_commands)

    assert await adapter.async_camera_frame() == STREAMED
    hub._last_frame_at = 0.0  # noqa: SLF001 - the cached frame is stale, so the hub asks for a still
    assert await hub.async_refresh_frame() == STREAMED
    assert resin_printer.sent_commands == sent
    assert len(spawner.processes) == 1

    await viewer.aclose()
    await hub.async_stop()
    assert spawner.processes[0].log == ["q"]
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}]
    await adapter.async_teardown()


async def test_teardown_stops_a_running_stream_before_it_closes_the_socket(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    spawner = Spawner(resin_printer, obeys=("TERM",))
    adapter = _adapter(resin_printer, session, spawner)
    stream = adapter.async_camera_stream()
    assert await anext(stream) == JPEG
    await adapter.async_teardown()
    assert spawner.processes[0].log == ["q", "TERM"]
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}]
    await stream.aclose()
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}]
    assert resin_printer.forbidden == []


async def test_teardown_while_a_viewer_waits_for_a_frame_still_switches_off_and_closes(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    """A real ffmpeg takes a while to quit after ``q``, so the viewer is still reading
    its output when the stop starts draining it."""
    spawner = Spawner(resin_printer, obeys=("TERM",))
    adapter = _adapter(resin_printer, session, spawner)
    first = asyncio.Event()

    async def watch() -> list[bytes]:
        frames = []
        async for frame in adapter.async_camera_stream():
            frames.append(frame)
            first.set()
        return frames

    task = asyncio.create_task(watch())
    await asyncio.wait_for(first.wait(), 5)
    (process,) = spawner.processes
    assert process.stdout._waiter is not None  # noqa: SLF001 - the viewer waits in read()

    await asyncio.wait_for(adapter.async_teardown(), 5)

    assert process.log == ["q", "TERM"]
    assert process.enables_at_exit == [1]
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}]
    for _ in range(50):
        if not resin_printer.connections:
            break
        await asyncio.sleep(0.02)
    assert resin_printer.connections == 0
    assert await asyncio.wait_for(task, 5) == [JPEG]
    assert resin_printer.forbidden == []


# ------------------------------------------------------ teardown during an open


class GatedSpawner(Spawner):
    """Holds each ffmpeg start until ``gate`` is set, as a slow process start would."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.gate = asyncio.Event()
        self.starting = asyncio.Event()

    async def __call__(self, *args: str) -> FakeFfmpeg:
        self.starting.set()
        await self.gate.wait()
        return await super().__call__(*args)


async def _until(check: Any) -> None:
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


async def _closed_for_good(printer: FakeResinPrinter) -> None:
    """Wait for the socket to close, then check that nothing opens another."""
    await _until(lambda: printer.connections == 0)
    await asyncio.sleep(0.3)
    assert printer.connections == 0


async def test_teardown_after_the_enable_starts_no_ffmpeg_and_switches_the_video_off(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    """386 Enable 1 is on the wire when the entry unloads: ffmpeg never starts."""
    spawner = Spawner(resin_printer)
    adapter = _adapter(resin_printer, session, spawner)
    await adapter.async_read()
    resin_printer.answer_delay[386] = 0.2
    still = asyncio.create_task(adapter.async_camera_frame())
    # The printer has the request and holds its answer back.
    await _until(lambda: 386 in resin_printer.sent_commands)
    assert resin_printer.video_enables == []

    await asyncio.wait_for(adapter.async_teardown(), 5)

    with pytest.raises(UnreachableError, match="closing"):
        await still
    assert spawner.processes == []
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}]
    await _closed_for_good(resin_printer)
    assert resin_printer.forbidden == []


async def test_teardown_while_ffmpeg_starts_stops_it_before_the_video_goes_off(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    spawner = GatedSpawner(resin_printer, frames=())
    adapter = _adapter(resin_printer, session, spawner)
    still = asyncio.create_task(adapter.async_camera_frame())
    await asyncio.wait_for(spawner.starting.wait(), 5)

    teardown = asyncio.create_task(adapter.async_teardown())
    await asyncio.sleep(0.05)
    assert not teardown.done()  # it waits for the start in progress
    spawner.gate.set()
    await asyncio.wait_for(teardown, 5)

    (process,) = spawner.processes
    assert process.log == ["q"]
    assert process.enables_at_exit == [1]
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}]
    with pytest.raises(UnreachableError):
        await asyncio.wait_for(still, 5)
    await _closed_for_good(resin_printer)
    assert _video(resin_printer) == [{"Enable": 1}, {"Enable": 0}]


@pytest.mark.parametrize("gives_up", [False, True], ids=["in_time", "after_the_wait"])
async def test_an_open_a_teardown_overtook_never_reconnects(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch,
    gives_up: bool,
) -> None:
    """The open waits for the attributes when the entry unloads; it must not reopen the socket
    to go on, even when it outlasts the teardown's wait."""
    monkeypatch.setattr(sdcp, "PUSH_TIMEOUT", 0.3)
    if gives_up:
        monkeypatch.setattr(sdcp_resin, "VIDEO_CLOSE_WAIT", 0.05)
    spawner = Spawner(resin_printer)
    adapter = _adapter(resin_printer, session, spawner)
    await adapter.async_read()
    resin_printer.withhold_attributes = True
    sent = len(resin_printer.sent_commands)
    still = asyncio.create_task(adapter.async_camera_frame())
    await _until(lambda: resin_printer.sent_commands[sent:] == [1])

    await asyncio.wait_for(adapter.async_teardown(), 5)
    assert still.done() is not gives_up
    with pytest.raises(UnreachableError, match="closing"):
        await asyncio.wait_for(still, 5)

    await _closed_for_good(resin_printer)
    assert resin_printer.sent_commands[sent:] == [1]
    assert spawner.processes == []
    assert 386 not in resin_printer.sent_commands


async def test_a_still_asked_for_during_teardown_is_refused(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    spawner = Spawner(resin_printer)
    adapter = _adapter(resin_printer, session, spawner)
    adapter._closing = True  # noqa: SLF001
    with pytest.raises(UnreachableError, match="closing"):
        await adapter.async_camera_frame()
    assert spawner.processes == []
    adapter._closing = False  # noqa: SLF001
    await adapter.async_teardown()

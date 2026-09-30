"""JPEG frames from an RTSP camera through one ffmpeg process, stopped gracefully so it
sends its TEARDOWN: a killed client leaks the session on a Saturn until a power cycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any, Final

from ..mjpeg import jpeg_frames
from ..protocols import UnreachableError

_LOGGER = logging.getLogger(__name__)

#: Seconds ffmpeg is given to stop after ``q``, and again after SIGTERM, before it is killed.
STOP_WAIT: Final = 5.0
#: Seconds without a whole frame before the camera counts as gone.
FRAME_TIMEOUT: Final = 15.0
#: Frames per second of a stream: enough for a print, and light on the printer.
STREAM_RATE: Final = 2
READ_CHUNK: Final = 65536
#: Seconds the drain waits while another reader holds ffmpeg's output.
DRAIN_RETRY: Final = 0.05
MAX_FRAME_BYTES: Final = 8 * 1024 * 1024

#: Starts ffmpeg with these arguments; a test hands in a fake process.
Spawn = Callable[..., Awaitable[Any]]


async def async_spawn(*args: str) -> asyncio.subprocess.Process:
    """Start ffmpeg with a stdin for ``q``, its frames on stdout, and its log discarded."""
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


def ffmpeg_args(binary: str, url: str, *, still: bool) -> list[str]:
    """Return the command line: RTSP over UDP only, with the wall clock as timestamps,
    since the Saturn's server refuses TCP and its timestamps do not always increase."""
    output = ["-frames:v", "1"] if still else ["-r", str(STREAM_RATE)]
    return [
        binary, "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "udp", "-use_wallclock_as_timestamps", "1",
        "-fflags", "nobuffer", "-err_detect", "ignore_err",
        "-i", url,
        "-an", "-c:v", "mjpeg", *output, "-f", "image2pipe", "pipe:1",
    ]  # fmt: skip


class RtspFrames:
    """One ffmpeg reading one RTSP URL, for a single still or for a stream of frames."""

    def __init__(self, binary: str, url: str, *, still: bool, spawn: Spawn = async_spawn) -> None:
        """Prepare the process; nothing runs until :meth:`async_start`."""
        self.args = ffmpeg_args(binary, url, still=still)
        self._spawn = spawn
        self._process: Any = None

    async def async_start(self) -> None:
        """Start ffmpeg, which opens the RTSP session."""
        try:
            self._process = await self._spawn(*self.args)
        except OSError as err:
            raise UnreachableError(f"cannot run ffmpeg ({self.args[0]}): {err}") from err

    async def async_frames(self) -> AsyncIterator[bytes]:
        """Yield each whole JPEG until ffmpeg ends; raise when none comes in time."""
        stdout = self._process.stdout
        loop = asyncio.get_running_loop()
        buffer = bytearray()
        deadline = loop.time() + FRAME_TIMEOUT
        while True:
            for frame in jpeg_frames(buffer):
                yield frame
                deadline = loop.time() + FRAME_TIMEOUT
            try:
                chunk = await asyncio.wait_for(stdout.read(READ_CHUNK), max(deadline - loop.time(), 0))
            except TimeoutError:
                raise UnreachableError(f"the camera sent no frame for {FRAME_TIMEOUT:g} s") from None
            if not chunk:
                return
            buffer.extend(chunk)
            if len(buffer) > MAX_FRAME_BYTES:
                raise UnreachableError("the camera frame exceeded the size cap")

    async def async_stop(self) -> None:
        """Ask ffmpeg to quit with ``q``, then SIGTERM, each given :data:`STOP_WAIT`; either
        lets it send TEARDOWN. SIGKILL, which does not, comes last and with a warning."""
        process, self._process = self._process, None
        if process is None:
            return
        # ffmpeg blocked on a full pipe would never read the ``q``, so its output is drained.
        drain = asyncio.create_task(self._async_drain(process))
        try:
            if process.returncode is None:
                with suppress(OSError, AttributeError):
                    process.stdin.write(b"q")
                    await process.stdin.drain()
            if await self._async_exited(process):
                return
            with suppress(ProcessLookupError):
                process.terminate()
            if await self._async_exited(process):
                return
            _LOGGER.warning(
                "ffmpeg ignored q and SIGTERM and was killed, so the printer may hold its camera "
                "session until it is switched off and on"
            )
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        finally:
            drain.cancel()
            with suppress(asyncio.CancelledError):
                await drain

    @staticmethod
    async def _async_exited(process: Any) -> bool:
        try:
            await asyncio.wait_for(process.wait(), STOP_WAIT)
        except TimeoutError:
            return False
        return True

    @staticmethod
    async def _async_drain(process: Any) -> None:
        with suppress(OSError, ValueError):
            while True:
                try:
                    if not await process.stdout.read(READ_CHUNK):
                        return
                except RuntimeError:
                    # A viewer still waits in its own read, and drains until it stops reading.
                    await asyncio.sleep(DRAIN_RETRY)

"""The shared camera connection and the per-printer runtime.

Every camera frame reaches a browser through Home Assistant, and every viewer of
one printer shares a single upstream connection. That is not an optimisation on
some of this hardware, it is a requirement: the Centauri Carbon's camera server
keeps only a handful of connection slots and leaks them when a client disconnects,
so per-viewer connections starve the stream within a few page refreshes.

:class:`CameraHub` is the single owner of that connection, exactly as
``StreamHub`` is in the video proxy integration it is modelled on.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import Capability
from .models import FileEntry, PrinterSnapshot
from .protocols import PrinterConfig, Protocol, ProtocolError

if TYPE_CHECKING:
    from .proxy import WebProxyRuntime
    from .security import TokenManager

_LOGGER = logging.getLogger(__name__)

#: How long the upstream camera connection is held after the last viewer leaves.
IDLE_STOP_DELAY = 20.0

#: Backoff ceiling between camera reconnect attempts.
MAX_BACKOFF = 30.0


def create_session(verify_ssl: bool = True) -> aiohttp.ClientSession:
    """Create the HTTP session one printer's adapters share.

    A dedicated session keeps long-lived camera and WebSocket connections out of
    Home Assistant's shared pool, lets one printer be torn down without disturbing
    another, and lets each printer decide whether its HTTPS certificate is checked.
    """
    connector = aiohttp.TCPConnector(
        limit=0, limit_per_host=0, ttl_dns_cache=300, ssl=bool(verify_ssl)
    )
    return aiohttp.ClientSession(
        connector=connector, timeout=aiohttp.ClientTimeout(total=None)
    )


@dataclass(slots=True)
class CameraStats:
    """A serialisable view of the shared camera connection."""

    live: bool
    viewers: int
    frames: int
    last_frame_bytes: int
    last_error: str | None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe mapping."""
        return {
            "live": self.live,
            "viewers": self.viewers,
            "frames": self.frames,
            "last_frame_bytes": self.last_frame_bytes,
            "last_error": self.last_error,
        }


class CameraHub:
    """Own one upstream camera stream and fan its frames out to subscribers."""

    def __init__(self, adapter: Protocol) -> None:
        """Create a hub bound to one adapter."""
        self._adapter = adapter
        self._subscribers: set[asyncio.Queue[bytes]] = set()
        self._last_frame: bytes | None = None
        self._last_frame_at: float | None = None
        self._frames = 0
        self._last_error: str | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._backoff = 1.0
        #: Set while the runtime is being torn down, so a departing viewer does not
        #: re-arm the idle timer on the way out.
        self._stopping = False

    @property
    def last_frame(self) -> bytes | None:
        """Return the most recent frame, if one has been read."""
        return self._last_frame

    @property
    def is_running(self) -> bool:
        """Return ``True`` while the upstream connection is open."""
        return self._supervisor is not None and not self._supervisor.done()

    @property
    def viewers(self) -> int:
        """Return the number of attached subscribers."""
        return len(self._subscribers)

    def stats(self) -> CameraStats:
        """Return a serialisable view of this hub."""
        return CameraStats(
            live=self.is_running,
            viewers=len(self._subscribers),
            frames=self._frames,
            last_frame_bytes=len(self._last_frame) if self._last_frame else 0,
            last_error=self._last_error,
        )

    def is_frame_fresh(self, max_age: float = 5.0) -> bool:
        """Return ``True`` when the cached frame is recent enough to reuse."""
        return (
            self._last_frame is not None
            and self._last_frame_at is not None
            and (time.monotonic() - self._last_frame_at) <= max_age
        )

    def async_subscribe(self) -> AsyncIterator[bytes]:
        """Return an iterator of frames for one viewer."""
        return self._async_iter_frames()

    async def _async_iter_frames(self) -> AsyncIterator[bytes]:
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        self._subscribers.add(queue)
        self._stopping = False
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        self._ensure_supervisor()
        try:
            while True:
                frame = await queue.get()
                yield frame
        finally:
            self._subscribers.discard(queue)
            # Only arm the idle timer when this hub is still in service. Arming it
            # while the runtime is shutting down leaves a sleep pending on the loop
            # after the printer has gone, which is a task leak rather than a feature.
            if not self._subscribers and not self._stopping:
                self._idle_task = asyncio.create_task(self._async_stop_when_idle())

    def _ensure_supervisor(self) -> None:
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(self._async_supervise())

    async def _async_stop_when_idle(self) -> None:
        """Close the upstream connection once nobody has been watching for a while.

        A viewer that comes back within the delay reuses the open connection instead
        of paying for a new one, which matters on a camera server with few slots.
        """
        try:
            await asyncio.sleep(IDLE_STOP_DELAY)
        except asyncio.CancelledError:
            raise
        if not self._subscribers:
            await self.async_stop()

    async def _async_supervise(self) -> None:
        while self._subscribers or self._idle_task is not None:
            try:
                await self._async_pump()
                self._backoff = 1.0
            except asyncio.CancelledError:
                raise
            except (ProtocolError, aiohttp.ClientError, OSError, TimeoutError) as err:
                self._last_error = str(err)
                _LOGGER.debug("%s: camera stream ended: %s", self._adapter.config.name, err)
            if not self._subscribers:
                return
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    async def _async_pump(self) -> None:
        """Read frames from the adapter and hand each one to every viewer.

        A full queue means a viewer is slower than the camera, so the stale frame is
        dropped rather than buffered: a late frame of a live print is worthless.
        """
        stream = self._adapter.async_camera_stream()
        async with aclosing(stream) as frames:
            async for frame in frames:
                self._last_frame = frame
                self._last_frame_at = time.monotonic()
                self._frames += 1
                for queue in list(self._subscribers):
                    if queue.full():
                        with suppress(asyncio.QueueEmpty):
                            queue.get_nowait()
                    with suppress(asyncio.QueueFull):
                        queue.put_nowait(frame)

    async def async_refresh_frame(self) -> bytes | None:
        """Fetch a single frame, reusing the cached one when it is fresh."""
        if self.is_frame_fresh():
            return self._last_frame
        if not self._adapter.capabilities or Capability.CAMERA not in self._adapter.capabilities:
            return self._last_frame
        try:
            frame = await self._adapter.async_camera_frame()
        except (ProtocolError, aiohttp.ClientError, OSError, TimeoutError) as err:
            self._last_error = str(err)
            _LOGGER.debug("%s: snapshot failed: %s", self._adapter.config.name, err)
            return self._last_frame
        self._last_frame = frame
        self._last_frame_at = time.monotonic()
        return frame

    async def async_stop(self) -> None:
        """Stop the supervisor, the idle timer, and drop every subscriber.

        The idle timer is cancelled here as well as the supervisor, because it is a
        sleep that outlives the viewer that scheduled it: leaving it pending means a
        task still waiting on the loop after the printer has been unloaded.
        """
        self._stopping = True
        idle, self._idle_task = self._idle_task, None
        if idle is not None and not idle.done():
            idle.cancel()
            with suppress(asyncio.CancelledError):
                await idle

        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
            with suppress(asyncio.CancelledError):
                await supervisor

        for queue in list(self._subscribers):
            with suppress(asyncio.QueueFull):
                queue.put_nowait(b"")
        self._subscribers.clear()


class PrinterRuntime:
    """Everything the entities, the views and diagnostics need for one printer."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        config: PrinterConfig,
        adapter: Protocol,
        session: aiohttp.ClientSession,
        tokens: "TokenManager",
        web_proxy: "WebProxyRuntime",
    ) -> None:
        """Assemble the runtime for one config entry."""
        self.hass = hass
        self.entry = entry
        self.config = config
        self.adapter = adapter
        self.session = session
        self.tokens = tokens
        self.web_proxy = web_proxy
        self.camera = CameraHub(adapter)
        self.snapshot: PrinterSnapshot = PrinterSnapshot(
            protocol=config.protocol, connected=False
        )
        self.files: list[FileEntry] = []
        self.last_error: str | None = None
        self.last_seen: float | None = None

    @property
    def entry_id(self) -> str:
        """Return the config entry id."""
        return self.entry.entry_id

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Return the capabilities this printer grants right now."""
        return self.adapter.capabilities

    @property
    def has_camera(self) -> bool:
        """Return ``True`` when this printer exposes a camera this integration can read."""
        return Capability.CAMERA in self.adapter.capabilities

    @property
    def has_stream(self) -> bool:
        """Return ``True`` when this printer's camera is a stream Home Assistant plays."""
        return Capability.CAMERA_STREAM in self.adapter.capabilities

    @property
    def camera_kind(self) -> str | None:
        """Return ``"mjpeg"``, ``"stream"`` or ``None``, for the card."""
        if self.has_stream:
            return "stream"
        if self.has_camera:
            return "mjpeg"
        return None

    def camera_entity_id(self) -> str | None:
        """Return the entity id of this printer's camera, once it is registered."""
        from homeassistant.helpers import entity_registry as er

        from .const import DOMAIN

        return er.async_get(self.hass).async_get_entity_id(
            "camera", DOMAIN, f"{self.entry_id}_camera"
        )

    @property
    def has_web_ui(self) -> bool:
        """Return ``True`` when this printer serves a web page worth proxying."""
        return Capability.WEB_UI in self.adapter.capabilities

    @property
    def upload_suffixes(self) -> tuple[str, ...]:
        """Return the file suffixes this printer stores as a job, such as ``.gcode``."""
        return self.adapter.upload_suffixes

    def model_profile(self) -> dict[str, object] | None:
        """Return which model profile applies, and whether it was measured."""
        profile = self.adapter.model_profile
        if profile is None:
            return None
        return {"id": profile.id, "name": profile.name, "verified": profile.verified}

    async def async_stop(self) -> None:
        """Stop background work owned by this runtime."""
        await self.camera.async_stop()
        with suppress(ProtocolError):
            await self.adapter.async_teardown()
        if not self.session.closed:
            await self.session.close()

    def describe(self) -> dict[str, object]:
        """Return everything the card needs, with freshly signed URLs.

        The card never guesses a URL: it asks for this document over the
        authenticated WebSocket API or the signed status endpoint and receives
        relative URLs that already carry their own authorisation.
        """
        entry_id = self.entry_id
        tokens = self.tokens
        camera_url = (
            tokens.async_url(entry_id=entry_id, scope="camera") if self.has_camera else None
        )
        snapshot_url = (
            tokens.async_url(entry_id=entry_id, scope="snapshot") if self.has_camera else None
        )
        web_port = self.config.port or 80
        web_proxy_url = (
            tokens.async_url(entry_id=entry_id, scope="web", port=web_port)
            if self.has_web_ui
            else None
        )
        return {
            "entry_id": entry_id,
            "name": self.config.name,
            "protocol": self.config.protocol.value,
            "model": self.snapshot.model,
            "firmware": self.snapshot.firmware,
            "serial": self.snapshot.serial,
            "connected": self.snapshot.connected,
            "last_error": self.last_error,
            "camera": self.camera_kind is not None,
            "camera_kind": self.camera_kind,
            "camera_entity_id": self.camera_entity_id() if self.camera_kind else None,
            "web_ui": self.has_web_ui,
            "camera_url": camera_url,
            "snapshot_url": snapshot_url,
            "status_url": tokens.async_url(entry_id=entry_id, scope="status"),
            "web_proxy_url": web_proxy_url,
            "web_ui_url": self.config.web_url,
            "upload_suffixes": list(self.upload_suffixes),
            "camera_stats": self.camera.stats().as_dict(),
            "unsafe_features": [
                {"id": feature.id, "label": feature.label, "reason": feature.reason}
                for feature in self.adapter.unsafe_features
            ],
            "filament_presets": [dict(item) for item in self.adapter.filament_presets],
            "model_profile": self.model_profile(),
            "printer": self.snapshot.as_dict(),
        }


def set_runtime(hass: HomeAssistant, runtime: PrinterRuntime) -> None:
    """Register a runtime so the HTTP views can find it."""
    from .const import DATA_RUNTIMES

    hass.data.setdefault(DATA_RUNTIMES, {})[runtime.entry_id] = runtime


def get_runtime(hass: HomeAssistant, entry_id: str) -> PrinterRuntime | None:
    """Return the runtime for a config entry, if it is loaded."""
    from .const import DATA_RUNTIMES

    return hass.data.get(DATA_RUNTIMES, {}).get(entry_id)


def remove_runtime(hass: HomeAssistant, entry_id: str) -> None:
    """Forget a runtime."""
    from .const import DATA_RUNTIMES

    hass.data.get(DATA_RUNTIMES, {}).pop(entry_id, None)


def iter_runtimes(hass: HomeAssistant) -> list[PrinterRuntime]:
    """Return every loaded runtime."""
    from .const import DATA_RUNTIMES

    return list(hass.data.get(DATA_RUNTIMES, {}).values())

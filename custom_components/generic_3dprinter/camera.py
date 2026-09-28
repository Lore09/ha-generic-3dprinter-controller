"""Camera entities: MJPEG frames relayed from the printer, or a native stream HA plays.
Either way viewers share one upstream connection instead of reaching the printer."""

from __future__ import annotations

import logging
import shlex

from aiohttp import web
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.setup import async_setup_component

from .coordinator import PrinterCoordinator
from .entity import async_device_info, async_require_coordinator
from .protocols import ProtocolError
from .runtime import PrinterRuntime

_LOGGER = logging.getLogger(__name__)

#: Seconds between MJPEG frames built from stills: smooth enough, and light on a one-slot camera.
FRAME_INTERVAL = 0.2
#: ffmpeg's default probe on a Kobra X's live FLV outlasts the still timeout; 32 KiB is enough.
STILL_PROBESIZE = 32768


async def async_still_from_stream(
    hass: HomeAssistant, source: str, width: int | None, height: int | None
) -> bytes | None:
    """Return one JPEG from a video stream; ffmpeg is imported lazily, for stream cameras only."""
    from homeassistant.components import ffmpeg

    # ffmpeg's helper takes a whole input string in place of a bare source.
    command = f"-probesize {STILL_PROBESIZE} -i {shlex.quote(source)}"
    return await ffmpeg.async_get_image(hass, command, width=width, height=height)


async def async_require_ffmpeg(hass: HomeAssistant, name: str) -> None:
    """Set up Home Assistant's ffmpeg, which a stream camera's stills come from.

    ``after_dependencies`` only orders this integration after an ffmpeg the user
    configured. Neither ``default_config`` nor the camera and stream components set
    it up, so on an install where no other integration needs it every still, and
    every timelapse frame, would fail. Playback does not need it, so a failure is
    logged and the camera is still added.
    """
    if not await async_setup_component(hass, "ffmpeg", {}):
        _LOGGER.warning(
            "%s: Home Assistant's ffmpeg integration could not be set up, so this "
            "camera gives no stills and no timelapse frames",
            name,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the camera entity when the printer has a camera."""
    runtime: PrinterRuntime = entry.runtime_data
    if runtime.has_stream:
        await async_require_ffmpeg(hass, runtime.config.name)
        coordinator = async_require_coordinator(hass, entry.entry_id)
        async_add_entities([Generic3DPrinterStreamCamera(coordinator)])
    elif runtime.has_camera:
        coordinator = async_require_coordinator(hass, entry.entry_id)
        async_add_entities([Generic3DPrinterCamera(coordinator)])


class _PrinterCamera(Camera):
    """A printer's camera, named after the printer and available while it answers."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, coordinator: PrinterCoordinator) -> None:
        """Bind the camera to its coordinator and its printer's device."""
        super().__init__()
        self._coordinator = coordinator
        runtime = coordinator.runtime
        self._attr_name = runtime.config.name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_camera"
        self._attr_device_info = async_device_info(runtime)

    @property
    def runtime(self) -> PrinterRuntime:
        """Return everything this integration knows about the printer."""
        return self._coordinator.runtime

    @property
    def available(self) -> bool:
        """Return ``False`` while the coordinator's last poll failed."""
        return self._coordinator.last_update_success


class Generic3DPrinterCamera(_PrinterCamera):
    """A camera serving the frames this integration proxies."""

    _attr_frame_interval = FRAME_INTERVAL
    _attr_is_streaming = True

    @property
    def is_streaming(self) -> bool:
        """Return ``True``: this entity serves an MJPEG stream, not a still only."""
        return True

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return one JPEG frame, reusing a cached frame while it is fresh."""
        frame = await self.runtime.camera.async_refresh_frame()
        if frame is None:
            _LOGGER.debug("%s: the printer returned no camera frame", self.runtime.config.name)
        return frame

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse | None:
        """Serve Home Assistant's MJPEG proxy from the shared camera stream.

        Home Assistant's default handler would build the stream from repeated stills,
        which opens a fresh upstream request per frame. The import is deferred
        because the views module imports the router this platform is loaded from.
        """
        from .views import async_proxy_mjpeg_stream

        return await async_proxy_mjpeg_stream(request, self.runtime)


class Generic3DPrinterStreamCamera(_PrinterCamera):
    """A printer camera that is a native video stream, played by Home Assistant."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: PrinterCoordinator) -> None:
        """Bind the camera, with no stream started yet."""
        super().__init__(coordinator)
        #: The URL the last start handed out, which a still reuses while it plays.
        self._source: str | None = None

    async def stream_source(self) -> str | None:
        """Start the printer's stream and return its URL, or ``None`` when it fails."""
        try:
            self._source = await self.runtime.adapter.async_stream_source()
        except ProtocolError as err:
            _LOGGER.debug("%s: the stream did not start: %s", self.runtime.config.name, err)
            self._source = None
        return self._source

    async def async_refresh_providers(self, *, write_state: bool = True) -> None:
        """Skip HA's WebRTC probe, which would start the printer's capture with nobody watching."""

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return one still, from the stream already running when it gives one.
        Restarting the capture cuts off whoever is watching."""
        if self._source is not None and (still := await self._async_still(self._source, width, height)):
            return still
        source = await self.stream_source()
        return await self._async_still(source, width, height) if source is not None else None

    async def _async_still(self, source: str, width: int | None, height: int | None) -> bytes | None:
        try:
            return await async_still_from_stream(self.hass, source, width, height)
        except Exception as err:  # noqa: BLE001 - no ffmpeg, or a stream that refused it
            _LOGGER.debug("%s: no still from the stream: %s", self.runtime.config.name, err)
            return None

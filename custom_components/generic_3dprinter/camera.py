"""A camera entity serving frames the integration fetched from the printer.

A dashboard behind HTTPS cannot load a printer's own plain-HTTP camera URL, and
several of these cameras keep only a few connection slots, so a browser cannot be
pointed at the printer directly. This entity therefore serves frames the
integration already holds: Home Assistant's own ``/api/camera_proxy`` and
``/api/camera_proxy_stream`` then work for dashboards, snapshots, notifications and
third-party camera cards, with no client needing to reach the printer at all.

It shares the runtime's :class:`~.runtime.CameraHub`, so a camera tile, a card and
a notification all read one upstream connection rather than one each.

A printer whose camera is a native video stream gets a different entity: it hands
Home Assistant the stream's URL, so the stream component plays it and shares one
upstream connection between viewers the same way, and its stills come from ffmpeg.
"""

from __future__ import annotations

import logging

from aiohttp import web
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PrinterCoordinator
from .entity import async_device_info, async_require_coordinator
from .protocols import ProtocolError
from .runtime import PrinterRuntime

_LOGGER = logging.getLogger(__name__)

#: Seconds between frames of the MJPEG stream Home Assistant's own camera proxy
#: builds from stills. Five frames a second is smooth enough to watch a print and
#: cheap enough not to saturate a single-slot camera server.
FRAME_INTERVAL = 0.2


async def async_still_from_stream(
    hass: HomeAssistant, source: str, width: int | None, height: int | None
) -> bytes | None:
    """Return one JPEG taken from a video stream by Home Assistant's ffmpeg.

    Imported here rather than at the top: the ffmpeg component needs a library that
    is installed only where that component is used, and a printer without a stream
    camera must not depend on it.
    """
    from homeassistant.components import ffmpeg

    return await ffmpeg.async_get_image(hass, source, width=width, height=height)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the camera entity when the printer has a camera."""
    runtime: PrinterRuntime = entry.runtime_data
    if runtime.has_stream:
        coordinator = async_require_coordinator(hass, entry.entry_id)
        async_add_entities([Generic3DPrinterStreamCamera(coordinator)])
    elif runtime.has_camera:
        coordinator = async_require_coordinator(hass, entry.entry_id)
        async_add_entities([Generic3DPrinterCamera(coordinator)])


class Generic3DPrinterCamera(Camera):
    """A camera serving the frames this integration proxies for one printer.

    The entity is named after the printer rather than after itself, which is the
    shape the fleet's dashboards already expect from a printer's camera: one device
    called "Centauri Carbon" whose camera is that printer.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_frame_interval = FRAME_INTERVAL
    _attr_is_streaming = True

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


class Generic3DPrinterStreamCamera(Camera):
    """A printer camera that is a native video stream, played by Home Assistant."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: PrinterCoordinator) -> None:
        """Bind the camera to its coordinator and its printer's device."""
        super().__init__()
        self._coordinator = coordinator
        runtime = coordinator.runtime
        self._attr_name = runtime.config.name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_camera"
        self._attr_device_info = async_device_info(runtime)
        #: The URL the last start handed out, which a still reuses while it plays.
        self._source: str | None = None

    @property
    def runtime(self) -> PrinterRuntime:
        """Return everything this integration knows about the printer."""
        return self._coordinator.runtime

    @property
    def available(self) -> bool:
        """Return ``False`` while the coordinator's last poll failed."""
        return self._coordinator.last_update_success

    async def stream_source(self) -> str | None:
        """Start the printer's stream and return its URL, or ``None`` when it fails."""
        try:
            self._source = await self.runtime.adapter.async_stream_source()
        except ProtocolError as err:
            _LOGGER.debug("%s: the stream did not start: %s", self.runtime.config.name, err)
            self._source = None
        return self._source

    async def async_refresh_providers(self, *, write_state: bool = True) -> None:
        """Skip Home Assistant's WebRTC probe, which would start the stream.

        The probe calls :meth:`stream_source` whenever the entity is added or a
        provider registers, and starting a printer's camera is not free: on an
        Anycubic Kobra it switches the light on. Playback falls back to HLS, which
        asks for the source only when someone actually watches.
        """

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return one still from the stream, or ``None`` when none can be taken.

        The stream already playing is tried first. Starting it again restarts the
        printer's capture, which cuts off whoever is watching, so a still starts it
        only when the running one gives no frame. A timelapse takes a still per layer.
        """
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

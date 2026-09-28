"""A timelapse of each print, taken from the printer's own camera.

While the timelapse switch is on, a frame is saved each time the printer reports a
new layer, or every :data:`FALLBACK_INTERVAL` seconds from a printer that reports no
layer. When the job ends the frames become one MP4 in Home Assistant's media folder,
under ``generic_3dprinter/<printer>/``, and a ``generic_3dprinter_timelapse`` event
says where, so an automation can send it on.

Frames are read through the printer's camera entity, so an MJPEG camera and a
stream camera are read the same way, and a timelapse shares the printer's one
upstream camera connection with whoever is watching.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from time import monotonic
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from homeassistant.components.camera import async_get_image
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import slugify

from .const import DOMAIN, Command, LightChannel, PrintState
from .coordinator import PrinterCoordinator
from .entity import Generic3DPrinterEntity
from .models import PrinterSnapshot

_LOGGER = logging.getLogger(__name__)

EVENT_TIMELAPSE: Final = f"{DOMAIN}_timelapse"
#: Seconds between frames from a printer that reports no layer.
FALLBACK_INTERVAL: Final = 30.0
#: Frames per second of the finished video: 300 layers make ten seconds.
FRAME_RATE: Final = 30
#: A job with fewer frames than this, such as one cancelled while heating, leaves
#: no video behind.
MIN_FRAMES: Final = 2
#: Seconds the camera is given to settle after the light is switched on for a job.
LIGHT_SETTLE: Final = 2.0
#: States that end a job. Paused, preparing and unknown do not: a print that pauses
#: for a filament change, or a printer that drops off the network for a poll, goes
#: on in the same timelapse.
_ENDED: Final = frozenset({PrintState.FINISHED, PrintState.CANCELLED, PrintState.IDLE, PrintState.ERROR})


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    """Return the ffmpeg Home Assistant is configured with, or the one on the path."""
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(hass).binary
    except (ImportError, ValueError):
        return "ffmpeg"


def media_dir(hass: HomeAssistant) -> Path:
    """Return the ``local`` media folder, which the media browser shows."""
    return Path(hass.config.media_dirs.get("local") or hass.config.path("media"))


class Timelapse:
    """The frames of one printer's current job, and the video they become."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        camera: Callable[[], str | None],
        set_light: Callable[[bool], Awaitable[None]] | None = None,
    ) -> None:
        """Record from the camera entity ``camera`` names, into a folder named after the printer.

        The entity id is asked for at each frame: the camera platform may register
        its entity after this one is made. ``set_light`` switches the printer's light,
        for :attr:`with_light`.
        """
        self.hass = hass
        self.camera = camera
        self.set_light = set_light
        #: Switch the light on for a job that starts with it off, and off again after.
        self.with_light = False
        self._needs_light = False
        self._lit = False
        self.slug = slugify(name)
        self.frames = 0
        self.last_video: Path | None = None
        self._job: Path | None = None
        self._last_layer: int | None = None
        self._last_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def recording(self) -> bool:
        """Return ``True`` while a job's frames are being collected."""
        return self._job is not None

    @callback
    def observe(self, snapshot: PrinterSnapshot) -> None:
        """Take a frame when one is due, and finish the video when the job ends."""
        state = snapshot.print_state
        if state is PrintState.PRINTING:
            layer = snapshot.current_layer
            if layer is not None:
                due = layer != self._last_layer
            else:
                due = monotonic() - self._last_at >= FALLBACK_INTERVAL
            if self._job is None:
                folder = media_dir(self.hass) / DOMAIN / self.slug
                self._job = folder / datetime.now().strftime("%Y%m%d-%H%M%S")
                self.frames = 0
                self._needs_light = (
                    self.with_light and self.set_light is not None and LightChannel.CHAMBER not in snapshot.lights
                )
                due = True
            if due and not self._lock.locked():
                self._last_layer, self._last_at = layer, monotonic()
                self.hass.async_create_background_task(self.async_capture(), f"{DOMAIN} timelapse frame")
        elif state in _ENDED and self._job is not None:
            self.hass.async_create_background_task(self.async_finish(), f"{DOMAIN} timelapse video")

    async def async_capture(self) -> None:
        """Save one frame of the current job. A camera that fails skips a frame."""
        async with self._lock:
            job, camera = self._job, self.camera()
            if job is None or camera is None:
                return
            if self._needs_light:
                self._needs_light = False
                self._lit = await self._async_light(True)
                if self._lit:
                    await asyncio.sleep(LIGHT_SETTLE)
            try:
                image = await async_get_image(self.hass, camera)
            except HomeAssistantError as err:
                _LOGGER.debug("%s: no timelapse frame: %s", camera, err)
                return
            path = job / f"{self.frames:05d}.jpg"
            await self.hass.async_add_executor_job(_write, path, image.content)
            self.frames += 1

    async def async_finish(self) -> Path | None:
        """Turn the job's frames into a video, and return it."""
        async with self._lock:
            job, frames = self._job, self.frames
            self._job, self._last_layer, self.frames = None, None, 0
            if self._lit:
                self._lit = False
                await self._async_light(False)
            if job is None:
                return None
            if frames < MIN_FRAMES:
                await self.hass.async_add_executor_job(shutil.rmtree, job, True)
                return None
            video = job.with_suffix(".mp4")
            if not await self._async_encode(job, video):
                # The frames stay, so the video can still be made by hand.
                return None
            await self.hass.async_add_executor_job(shutil.rmtree, job, True)
            self.last_video = video
        self.hass.bus.async_fire(
            EVENT_TIMELAPSE,
            {
                "camera_entity_id": self.camera(),
                "path": str(video),
                "media_content_id": self.media_content_id(video),
                "frames": frames,
            },
        )
        return video

    async def _async_light(self, on: bool) -> bool:
        """Switch the light, and return whether the printer took it."""
        if self.set_light is None:
            return False
        try:
            await self.set_light(on)
        except HomeAssistantError as err:
            _LOGGER.warning("the timelapse could not switch the light %s: %s", "on" if on else "off", err)
            return False
        return True

    def media_content_id(self, video: Path) -> str:
        """Return the media-source id the media browser and a notification use."""
        relative = video.relative_to(media_dir(self.hass))
        return f"media-source://media_source/local/{relative.as_posix()}"

    async def _async_encode(self, job: Path, video: Path) -> bool:
        command = (
            _ffmpeg_binary(self.hass), "-y", "-loglevel", "error",
            "-framerate", str(FRAME_RATE), "-i", str(job / "%05d.jpg"),
            # H.264 in yuv420p needs even sides, and plays on every phone.
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(video),
        )  # fmt: skip
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await process.communicate()
        except OSError as err:
            _LOGGER.error("cannot run ffmpeg for the timelapse in %s: %s", job, err)
            return False
        if process.returncode != 0:
            _LOGGER.error("ffmpeg could not make the timelapse in %s: %s", job, stderr.decode(errors="replace").strip())
            return False
        return True


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class TimelapseSwitch(Generic3DPrinterEntity, SwitchEntity, RestoreEntity):
    """Whether each print is recorded as a timelapse. Off by default, and remembered."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PrinterCoordinator) -> None:
        """Record from the printer's camera entity."""
        super().__init__(coordinator, "timelapse")
        self.entity_description = SwitchEntityDescription(key="timelapse", icon="mdi:timelapse")
        runtime = coordinator.runtime

        async def set_light(on: bool) -> None:
            await coordinator.async_send_command(Command.SET_LIGHT, on=on, channel=LightChannel.CHAMBER.value)

        self.timelapse = Timelapse(coordinator.hass, runtime.config.name, runtime.camera_entity_id, set_light)
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        """Restore the switch as it was left."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._attr_is_on and self.coordinator.data is not None:
            self.timelapse.observe(self.coordinator.data)
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """A setting stays available while the printer is off."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Report the frames taken so far and the last video made."""
        last = self.timelapse.last_video
        return {
            "recording": self.timelapse.recording,
            "frames": self.timelapse.frames,
            "last_video": str(last) if last else None,
            "last_media_content_id": self.timelapse.media_content_id(last) if last else None,
        }

    async def async_turn_on(self, **kwargs: object) -> None:
        """Record from the next frame due."""
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        """Stop recording, and make a video of what the current job has so far."""
        self._attr_is_on = False
        self.async_write_ha_state()
        await self.timelapse.async_finish()
        self.async_write_ha_state()


class TimelapseLightSwitch(Generic3DPrinterEntity, SwitchEntity, RestoreEntity):
    """Whether a timelapse switches the light on for a job that starts in the dark.

    A job that starts with the light off gets it switched on before its first frame
    and off again when its video is made. A light already on is left alone.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PrinterCoordinator, timelapse: Timelapse) -> None:
        """Set the option on the printer's timelapse."""
        super().__init__(coordinator, "timelapse_light")
        self.entity_description = SwitchEntityDescription(key="timelapse_light", icon="mdi:lightbulb-auto")
        self.timelapse = timelapse

    async def async_added_to_hass(self) -> None:
        """Restore the option as it was left."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self.timelapse.with_light = last.state == "on"

    @property
    def available(self) -> bool:
        """A setting stays available while the printer is off."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether the option is set."""
        return self.timelapse.with_light

    async def async_turn_on(self, **kwargs: object) -> None:
        """Light the next job that starts in the dark."""
        self.timelapse.with_light = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        """Leave the light alone."""
        self.timelapse.with_light = False
        self.async_write_ha_state()

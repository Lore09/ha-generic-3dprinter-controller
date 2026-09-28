"""Switches for the printer's own lights, and for recording a timelapse.

A printer's chamber light is the one thing a user reaches for constantly and the
one thing most protocols expose as a plain on/off. The switch exists only when the
printer grants ``SET_LIGHT``, and its state comes from the snapshot's light set
rather than from a local flag, so a light turned on at the printer's own panel
shows as on here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import Capability, Command, LightChannel
from .coordinator import PrinterCoordinator
from .entity import (
    FilamentEntityFactories,
    Generic3DPrinterEntity,
    async_follow_filament,
    async_require_coordinator,
    granted_capabilities,
)
from .models import FilamentSystem
from .timelapse import TimelapseLightSwitch, TimelapseSwitch


@dataclass(frozen=True, kw_only=True)
class Generic3DPrinterSwitchDescription(SwitchEntityDescription):
    """One switchable light and the channel it addresses."""

    channel: LightChannel


SWITCH_DESCRIPTIONS: Final[tuple[Generic3DPrinterSwitchDescription, ...]] = (
    Generic3DPrinterSwitchDescription(
        key="chamber_light",
        icon="mdi:lightbulb",
        channel=LightChannel.CHAMBER,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the lights and settings this printer's capabilities allow."""
    granted = granted_capabilities(entry.runtime_data)
    coordinator = async_require_coordinator(hass, entry.entry_id)
    if granted[Capability.SET_LIGHT]:
        async_add_entities(
            Generic3DPrinterSwitch(coordinator, description)
            for description in SWITCH_DESCRIPTIONS
        )
    if entry.runtime_data.camera_kind is not None:
        timelapse = TimelapseSwitch(coordinator)
        async_add_entities([timelapse])
        if granted[Capability.SET_LIGHT]:
            async_add_entities([TimelapseLightSwitch(coordinator, timelapse.timelapse)])
    if granted[Capability.SET_AUTO_REFILL]:

        def build(filament: FilamentSystem) -> FilamentEntityFactories:
            if filament.auto_refill is None:
                return {}
            return {"auto_refill": lambda: AutoRefillSwitch(coordinator)}

        async_follow_filament(entry, coordinator, async_add_entities, build)


class Generic3DPrinterSwitch(Generic3DPrinterEntity, SwitchEntity):
    """One switchable light on the printer."""

    _command = Command.SET_LIGHT

    entity_description: Generic3DPrinterSwitchDescription

    def __init__(
        self,
        coordinator: PrinterCoordinator,
        description: Generic3DPrinterSwitchDescription,
    ) -> None:
        """Bind the entity to its coordinator and its description."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Return whether the printer reports this light as on."""
        return self.entity_description.channel in self.coordinator.data.lights

    async def async_turn_on(self, **kwargs: object) -> None:
        """Turn the light on."""
        await self._async_set_light(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Turn the light off."""
        await self._async_set_light(False)

    async def _async_set_light(self, on: bool) -> None:
        await self.coordinator.async_send_command(
            Command.SET_LIGHT, on=on, channel=self.entity_description.channel.value
        )


class AutoRefillSwitch(Generic3DPrinterEntity, SwitchEntity):
    """Whether the multi-material unit switches to a matching slot when one runs out."""

    _command = Command.SET_AUTO_REFILL

    def __init__(self, coordinator: PrinterCoordinator) -> None:
        """Bind the switch to the printer's multi-material system."""
        super().__init__(coordinator, "auto_refill")
        self.entity_description = SwitchEntityDescription(key="auto_refill", icon="mdi:autorenew")

    @property
    def is_on(self) -> bool | None:
        """Return the setting the printer reports."""
        filament = self.coordinator.data.filament
        return filament.auto_refill if filament is not None else None

    async def async_turn_on(self, **kwargs: object) -> None:
        """Turn auto-refill on."""
        await self.coordinator.async_send_command(Command.SET_AUTO_REFILL, on=True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Turn auto-refill off."""
        await self.coordinator.async_send_command(Command.SET_AUTO_REFILL, on=False)

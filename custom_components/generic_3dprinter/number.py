"""Targets the user can set: temperatures, speed and flow factors, fan duty.

Each number maps one snapshot reading to one command, and the mapping is data
rather than a subclass per control. A number exists only when its command's
capability is granted, so a printer that cannot be told a chamber temperature is
not given a chamber slider.

``native_value`` reads the printer's current setting and ``async_set_native_value``
sends the new one, which is what makes a number entity round trip: the value shown
is the value the printer reported, not the value the user last typed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import Capability, Command
from .coordinator import PrinterCoordinator
from .entity import (
    Generic3DPrinterEntity,
    async_require_coordinator,
    granted_capabilities,
)
from .models import PrinterSnapshot
from .validation import MAX_PERCENT, MAX_TEMPERATURE, MIN_PERCENT, MIN_TEMPERATURE

#: Step size of a temperature target, in degrees.
TEMPERATURE_STEP: Final = 1.0

#: Step size of a percentage, in percent.
PERCENT_STEP: Final = 1.0


@dataclass(frozen=True, kw_only=True)
class Generic3DPrinterNumberDescription(NumberEntityDescription):
    """One writable setting, the command that sets it, and where its value lives."""

    command: Command
    capability: Capability
    value_fn: Callable[[PrinterSnapshot], float | None]
    #: Selects which channel a multi-channel command addresses, such as a fan.
    channel: str | None = None


def _temperature(
    key: str,
    command: Command,
    capability: Capability,
    value_fn: Callable[[PrinterSnapshot], float | None],
) -> Generic3DPrinterNumberDescription:
    """Return the shared shape of a temperature target."""
    return Generic3DPrinterNumberDescription(
        key=key,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_min_value=MIN_TEMPERATURE,
        native_max_value=MAX_TEMPERATURE,
        native_step=TEMPERATURE_STEP,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        mode=NumberMode.BOX,
        command=command,
        capability=capability,
        value_fn=value_fn,
    )


def _fan(
    key: str, channel: str
) -> Generic3DPrinterNumberDescription:
    """Return one fan channel's duty control."""
    return Generic3DPrinterNumberDescription(
        key=key,
        native_min_value=MIN_PERCENT,
        native_max_value=MAX_PERCENT,
        native_step=PERCENT_STEP,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.BOX,
        command=Command.SET_FAN_SPEED,
        capability=Capability.SET_FAN_SPEED,
        channel=channel,
        value_fn=lambda snapshot: getattr(snapshot.fans, channel),
    )


NUMBER_DESCRIPTIONS: Final[tuple[Generic3DPrinterNumberDescription, ...]] = (
    _temperature(
        "nozzle_target",
        Command.SET_HOTEND_TEMP,
        Capability.SET_HOTEND_TEMP,
        lambda snapshot: snapshot.hotend.target,
    ),
    _temperature(
        "bed_target",
        Command.SET_BED_TEMP,
        Capability.SET_BED_TEMP,
        lambda snapshot: snapshot.bed.target,
    ),
    _temperature(
        "chamber_target",
        Command.SET_CHAMBER_TEMP,
        Capability.SET_CHAMBER_TEMP,
        lambda snapshot: snapshot.chamber.target,
    ),
    Generic3DPrinterNumberDescription(
        key="speed_factor",
        native_min_value=MIN_PERCENT,
        native_max_value=MAX_PERCENT,
        native_step=PERCENT_STEP,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.BOX,
        command=Command.SET_SPEED,
        capability=Capability.SET_SPEED,
        value_fn=lambda snapshot: snapshot.speed_factor,
    ),
    Generic3DPrinterNumberDescription(
        key="flow_factor",
        native_min_value=MIN_PERCENT,
        native_max_value=MAX_PERCENT,
        native_step=PERCENT_STEP,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.BOX,
        command=Command.SET_FLOW,
        capability=Capability.SET_FLOW,
        value_fn=lambda snapshot: snapshot.flow_factor,
    ),
    _fan("fan_model_speed", "model"),
    _fan("fan_auxiliary_speed", "auxiliary"),
    _fan("fan_chamber_speed", "chamber"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the writable settings this printer's capabilities allow."""
    coordinator = async_require_coordinator(hass, entry.entry_id)
    granted = granted_capabilities(entry.runtime_data)
    async_add_entities(
        Generic3DPrinterNumber(coordinator, description)
        for description in NUMBER_DESCRIPTIONS
        if granted[description.capability]
    )


class Generic3DPrinterNumber(Generic3DPrinterEntity, NumberEntity):
    """One writable setting on the printer."""

    entity_description: Generic3DPrinterNumberDescription

    def __init__(
        self,
        coordinator: PrinterCoordinator,
        description: Generic3DPrinterNumberDescription,
    ) -> None:
        """Bind the entity to its coordinator and its description."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._command = description.command

    @property
    def native_value(self) -> float | None:
        """Return the value the printer last reported."""
        return self.entity_description.value_fn(self.coordinator.data)

    async def async_set_native_value(self, value: float) -> None:
        """Send the value to the printer through the coordinator."""
        description = self.entity_description
        params: dict[str, object] = {"value": value}
        if description.channel is not None:
            params["channel"] = description.channel
        await self.coordinator.async_send_command(description.command, **params)

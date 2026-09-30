"""Read-only sensors for one printer.

One entity per reading, so a dashboard, an automation and the recorder all see the
same named value instead of one opaque attribute blob. A sensor exists only when
the printer can actually produce the reading, which is what `_gate` on each
description states: a printer that cannot report a chamber temperature and cannot
be told one gets no chamber sensor at all, rather than a permanently empty one.

Layers are the one exception to "gate on the capability that names the value",
because no protocol declares a layer capability. See :data:`SENSOR_DESCRIPTIONS`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import Capability, PrintState, ResinPhase
from .coordinator import PrinterCoordinator
from .entity import (
    FilamentEntityFactories,
    Generic3DPrinterEntity,
    async_follow_filament,
    async_require_coordinator,
    granted_capabilities,
)
from .models import FilamentSlot, FilamentSystem, PrinterSnapshot, ResinState
from .runtime import PrinterRuntime

type CapabilityGate = Callable[[Mapping[Capability, bool]], bool]


def _always(_: Mapping[Capability, bool]) -> bool:
    """Return ``True`` for a reading every protocol can produce."""
    return True


def _gate_for(*capabilities: Capability, any_of: bool = False) -> CapabilityGate:
    """Return a predicate over the granted capability set.

    ``any_of`` asks for one of the capabilities rather than all of them, which is
    how the position sensors accept either a jog control or a home control.
    """
    if any_of:
        return lambda granted: any(granted[item] for item in capabilities)
    return lambda granted: all(granted[item] for item in capabilities)


def _gate_unless(capability: Capability) -> CapabilityGate:
    """Return a predicate that holds while ``capability`` is not granted."""
    return lambda granted: not granted[capability]


def _resin(
    read: Callable[[ResinState], StateType | None],
) -> Callable[[PrinterSnapshot], StateType | None]:
    """Return a reader of the resin state, ``None`` while the printer reports none."""
    return lambda snapshot: read(snapshot.resin) if snapshot.resin is not None else None


def _release_film_attributes(snapshot: PrinterSnapshot) -> dict[str, Any] | None:
    resin = snapshot.resin
    if resin is None:
        return None
    return {"max": resin.release_film_max, "used_percent": resin.release_film_used}


def _axis(axis: str) -> Callable[[PrinterSnapshot], StateType | None]:
    def read(snapshot: PrinterSnapshot) -> StateType | None:
        position = snapshot.position
        return getattr(position, axis) if position is not None else None

    return read


def _configured_address(_: PrinterSnapshot) -> StateType | None:
    """Mark a reading that comes from the configuration rather than the printer.

    The address is the one diagnostic the snapshot cannot carry: an adapter knows
    where it was pointed, and the printer never reports it.
    """
    return None


@dataclass(frozen=True, kw_only=True)
class Generic3DPrinterSensorDescription(SensorEntityDescription):
    """One reading, its Home Assistant metadata, and when it exists."""

    value_fn: Callable[[PrinterSnapshot], StateType | None]
    gate: CapabilityGate = _always
    #: Extra state attributes read from the same snapshot, if the reading has any.
    attributes_fn: Callable[[PrinterSnapshot], dict[str, Any] | None] | None = None


#: Every sensor this platform can offer.
#:
#: The layer counters are gated on ``FILE_UPLOAD`` rather than on a layer
#: capability, because no protocol in this fleet declares one: OctoPrint and
#: PrusaLink upload files and report no layer count at all, while Moonraker, SDCP
#: and Duet all report layers. ``FILE_UPLOAD`` is the honest proxy for "this
#: protocol has a job it tracks layer by layer", and it is deterministic at entry
#: setup, unlike "the first snapshot happened to be taken mid-print". A resin
#: printer reports layers whether or not it can upload, so ``RESIN_STATUS`` counts.
SENSOR_DESCRIPTIONS: tuple[Generic3DPrinterSensorDescription, ...] = (
    Generic3DPrinterSensorDescription(
        key="printer_state",
        device_class=SensorDeviceClass.ENUM,
        options=[item.value for item in PrintState],
        value_fn=lambda snapshot: snapshot.print_state.value,
    ),
    Generic3DPrinterSensorDescription(
        key="progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.progress,
        gate=_gate_for(
            Capability.START_PRINT, Capability.PAUSE, Capability.RESIN_STATUS, any_of=True
        ),
    ),
    Generic3DPrinterSensorDescription(
        key="current_layer",
        value_fn=lambda snapshot: snapshot.current_layer,
        gate=_gate_for(Capability.FILE_UPLOAD, Capability.RESIN_STATUS, any_of=True),
    ),
    Generic3DPrinterSensorDescription(
        key="total_layers",
        value_fn=lambda snapshot: snapshot.total_layers,
        gate=_gate_for(Capability.FILE_UPLOAD, Capability.RESIN_STATUS, any_of=True),
    ),
    Generic3DPrinterSensorDescription(
        key="remaining_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=lambda snapshot: snapshot.remaining,
    ),
    Generic3DPrinterSensorDescription(
        key="elapsed_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=lambda snapshot: snapshot.elapsed,
    ),
    Generic3DPrinterSensorDescription(
        key="filename",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.filename,
    ),
    Generic3DPrinterSensorDescription(
        key="nozzle_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.hotend.current,
        suggested_display_precision=1,
        gate=_gate_unless(Capability.RESIN_STATUS),
    ),
    Generic3DPrinterSensorDescription(
        key="nozzle_target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.hotend.target,
        suggested_display_precision=1,
        gate=_gate_unless(Capability.RESIN_STATUS),
    ),
    Generic3DPrinterSensorDescription(
        key="bed_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.bed.current,
        suggested_display_precision=1,
        gate=_gate_unless(Capability.RESIN_STATUS),
    ),
    Generic3DPrinterSensorDescription(
        key="bed_target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.bed.target,
        suggested_display_precision=1,
        gate=_gate_unless(Capability.RESIN_STATUS),
    ),
    Generic3DPrinterSensorDescription(
        key="chamber_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.chamber.current,
        suggested_display_precision=1,
        gate=_gate_for(Capability.SET_CHAMBER_TEMP, Capability.CHAMBER_SENSOR, any_of=True),
    ),
    Generic3DPrinterSensorDescription(
        key="chamber_target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.chamber.target,
        suggested_display_precision=1,
        gate=_gate_for(Capability.SET_CHAMBER_TEMP),
    ),
    Generic3DPrinterSensorDescription(
        key="print_phase",
        device_class=SensorDeviceClass.ENUM,
        options=[item.value for item in ResinPhase],
        value_fn=_resin(lambda resin: resin.phase.value if resin.phase is not None else None),
        gate=_gate_for(Capability.RESIN_STATUS),
    ),
    Generic3DPrinterSensorDescription(
        key="uv_led_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_resin(lambda resin: resin.uv_led),
        suggested_display_precision=1,
        gate=_gate_for(Capability.RESIN_STATUS),
    ),
    Generic3DPrinterSensorDescription(
        key="release_film",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_resin(lambda resin: resin.release_film),
        attributes_fn=_release_film_attributes,
        gate=_gate_for(Capability.RESIN_STATUS),
    ),
    Generic3DPrinterSensorDescription(
        key="vat_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_resin(lambda resin: resin.vat.current),
        suggested_display_precision=1,
        gate=_gate_for(Capability.VAT_SENSOR),
    ),
    Generic3DPrinterSensorDescription(
        key="vat_target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_resin(lambda resin: resin.vat.target),
        suggested_display_precision=1,
        gate=_gate_for(Capability.VAT_SENSOR),
    ),
    Generic3DPrinterSensorDescription(
        key="fan_model_speed",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.fans.model,
        gate=_gate_for(Capability.SET_FAN_SPEED),
    ),
    Generic3DPrinterSensorDescription(
        key="fan_auxiliary_speed",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.fans.auxiliary,
        gate=_gate_for(Capability.SET_FAN_SPEED),
    ),
    Generic3DPrinterSensorDescription(
        key="position_x",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        value_fn=_axis("x"),
        gate=_gate_for(Capability.JOG, Capability.HOME, any_of=True),
    ),
    Generic3DPrinterSensorDescription(
        key="position_y",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        value_fn=_axis("y"),
        gate=_gate_for(Capability.JOG, Capability.HOME, any_of=True),
    ),
    Generic3DPrinterSensorDescription(
        key="position_z",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        value_fn=_axis("z"),
        gate=_gate_for(Capability.JOG, Capability.HOME, any_of=True),
    ),
    Generic3DPrinterSensorDescription(
        key="speed_factor",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.speed_factor,
        gate=_gate_for(Capability.SET_SPEED),
    ),
    Generic3DPrinterSensorDescription(
        key="flow_factor",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: snapshot.flow_factor,
        gate=_gate_for(Capability.SET_FLOW),
    ),
    Generic3DPrinterSensorDescription(
        key="protocol",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: snapshot.protocol.value,
    ),
    Generic3DPrinterSensorDescription(
        key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: snapshot.firmware,
    ),
    Generic3DPrinterSensorDescription(
        key="model",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: snapshot.model,
    ),
    Generic3DPrinterSensorDescription(
        key="serial",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: snapshot.serial,
    ),
    Generic3DPrinterSensorDescription(
        key="ip_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_configured_address,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the sensors this printer's capabilities allow."""
    coordinator = async_require_coordinator(hass, entry.entry_id)
    runtime: PrinterRuntime = entry.runtime_data
    granted = granted_capabilities(runtime)
    async_add_entities(
        Generic3DPrinterSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
        if description.gate(granted)
    )
    if granted[Capability.FILAMENT_SLOTS]:

        def build(filament: FilamentSystem) -> FilamentEntityFactories:
            factories: dict[str, Callable[[], SensorEntity]] = {
                "active": lambda: ActiveFilamentSensor(coordinator)
            }
            for slot in filament.slots:
                factories[f"{slot.unit}_{slot.slot}"] = (
                    lambda unit=slot.unit, index=slot.slot: FilamentSlotSensor(
                        coordinator, unit, index
                    )
                )
            return factories

        async_follow_filament(entry, coordinator, async_add_entities, build)


class Generic3DPrinterSensor(Generic3DPrinterEntity, SensorEntity):
    """One reading from the shared snapshot."""

    entity_description: Generic3DPrinterSensorDescription

    def __init__(
        self,
        coordinator: PrinterCoordinator,
        description: Generic3DPrinterSensorDescription,
    ) -> None:
        """Bind the entity to its coordinator and its description."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | None:
        """Return the current reading, or ``None`` when the printer has none."""
        if self.entity_description.value_fn is _configured_address:
            return self.runtime.config.host
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the reading's own attributes, when its description has any."""
        attributes_fn = self.entity_description.attributes_fn
        if attributes_fn is None:
            return super().extra_state_attributes
        return attributes_fn(self.coordinator.data)


#: The state of a slot with no spool in it.
EMPTY_SLOT: Final = "empty"
#: The state of the active-filament sensor while no slot feeds the nozzle.
NO_FILAMENT: Final = "none"


def slot_label(unit: int, slot: int) -> str:
    """Return a slot's number as the printer's own screen shows it, counting from 1.

    The first unit's slots are 1 to 4. A second unit's are written 2-1 to 2-4.
    """
    return str(slot + 1) if unit == 0 else f"{unit + 1}-{slot + 1}"


def _slot_attributes(slot: FilamentSlot) -> dict[str, Any]:
    return {
        "unit": slot.unit + 1,
        "slot": slot_label(slot.unit, slot.slot),
        "loaded": slot.loaded,
        "active": slot.active,
        "material": slot.material,
        "filament": slot.name,
        "brand": slot.brand,
        "color": slot.color,
        "min_nozzle_temperature": slot.min_temp,
        "max_nozzle_temperature": slot.max_temp,
    }


class FilamentSlotSensor(Generic3DPrinterEntity, SensorEntity):
    """What one slot of a multi-material unit holds."""

    def __init__(self, coordinator: PrinterCoordinator, unit: int, slot: int) -> None:
        """Bind the sensor to one slot, numbered from 0 on the wire."""
        key = f"filament_slot_{unit}_{slot}"
        super().__init__(coordinator, key)
        self.entity_description = SensorEntityDescription(
            key=key, translation_key="filament_slot", icon="mdi:printer-3d-nozzle"
        )
        self._attr_translation_key = "filament_slot"
        self._attr_translation_placeholders = {"slot": slot_label(unit, slot)}
        self._unit = unit
        self._slot = slot

    def _current(self) -> FilamentSlot | None:
        filament = self.coordinator.data.filament
        return filament.slot(self._unit, self._slot) if filament is not None else None

    @property
    def native_value(self) -> StateType:
        """Return the filament's name, or ``empty`` when no spool is in the slot."""
        slot = self._current()
        if slot is None:
            return None
        if not slot.loaded:
            return EMPTY_SLOT
        return slot.name or slot.material

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the slot's material, brand, colour and temperature range."""
        slot = self._current()
        return _slot_attributes(slot) if slot is not None else None


class ActiveFilamentSensor(Generic3DPrinterEntity, SensorEntity):
    """The filament feeding the nozzle, and what the unit is doing."""

    def __init__(self, coordinator: PrinterCoordinator) -> None:
        """Bind the sensor to the printer's multi-material system."""
        super().__init__(coordinator, "active_filament")
        self.entity_description = SensorEntityDescription(
            key="active_filament", icon="mdi:printer-3d-nozzle"
        )

    @property
    def native_value(self) -> StateType:
        """Return the active slot's filament, or ``none`` while no slot feeds."""
        filament = self.coordinator.data.filament
        if filament is None:
            return None
        active = filament.active
        if active is None:
            return NO_FILAMENT
        return active.name or active.material

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the active slot and, while it works, what the unit is doing."""
        filament = self.coordinator.data.filament
        if filament is None:
            return None
        active = filament.active
        attributes = _slot_attributes(active) if active is not None else {}
        attributes["activity"] = filament.activity
        attributes["auto_refill"] = filament.auto_refill
        return attributes

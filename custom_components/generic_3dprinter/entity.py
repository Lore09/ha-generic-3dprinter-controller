"""The base entity every platform shares.

The entity layer is the only place in this integration where a capability
becomes something a user can see or press, so the rule lives here once: an entity
is constructed only when the printer grants what it needs, and the platform's
description table carries that requirement as data. Nothing is created and then
left sitting at ``unavailable`` for the lifetime of the entry.

Every entity reads the same coordinator-owned :class:`PrinterSnapshot`, so a
printer is polled once per interval no matter how many entities it carries.

A multi-material unit is the one thing a capability cannot settle at setup: the
protocol can report one, but whether a unit is attached, and how many slots it
has, is only known once the printer answers. Its entities are therefore added
when the printer first reports them, by :func:`async_follow_filament`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATORS, DOMAIN, MANUFACTURER, Capability, Command
from .coordinator import PrinterCoordinator
from .models import FilamentSystem
from .protocols import PrinterConfig
from .runtime import PrinterRuntime

type Generic3DPrinterConfigEntry = ConfigEntry[PrinterRuntime]


def granted_capabilities(runtime: PrinterRuntime) -> Mapping[Capability, bool]:
    """Return ``True`` per capability this printer currently grants.

    A platform's filter reads one mapping instead of re-testing membership per
    description, and the mapping is keyed by :class:`Capability` rather than by
    string, so a gate naming a capability that does not exist fails here instead of
    silently creating nothing.
    """
    granted = runtime.capabilities
    return {item: item in granted for item in Capability}


def async_device_info(runtime: PrinterRuntime) -> DeviceInfo:
    """Return the device registry entry for one printer.

    Built from the configuration and the snapshot together, because neither owns
    the whole identity: the name and the address are configured, and the model,
    the firmware and the printer's own page are only known once it has answered.
    """
    config: PrinterConfig = runtime.config
    snapshot = runtime.snapshot
    return DeviceInfo(
        identifiers={(DOMAIN, runtime.entry_id)},
        name=config.name,
        manufacturer=MANUFACTURER,
        model=snapshot.model,
        sw_version=snapshot.firmware,
        configuration_url=config.web_url,
    )


def async_get_coordinator(
    hass: HomeAssistant, entry_id: str
) -> PrinterCoordinator | None:
    """Return the coordinator for a config entry, if it is loaded."""
    return hass.data.get(DATA_COORDINATORS, {}).get(entry_id)


def async_require_coordinator(
    hass: HomeAssistant, entry_id: str
) -> PrinterCoordinator:
    """Return the coordinator for a config entry, raising when it is absent.

    Reaching this with no coordinator means the platform was set up outside the
    entry lifecycle, which is a bug rather than a printer condition.
    """
    coordinator = async_get_coordinator(hass, entry_id)
    if coordinator is None:
        raise RuntimeError(f"no coordinator is loaded for entry {entry_id}")
    return coordinator


class Generic3DPrinterEntity(CoordinatorEntity[PrinterCoordinator]):
    """Base entity reading one printer through its coordinator."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    #: The command this entity sends, for a control. Its state rule is reported as
    #: the ``blocked_reason`` attribute while the printer refuses it.
    _command: Command | None = None

    def __init__(self, coordinator: PrinterCoordinator, key: str) -> None:
        """Bind the entity to its coordinator, its device and its translation key.

        The device is attached here rather than read from the coordinator later,
        because Home Assistant derives an entity's object id from the device name
        and skips that step entirely when an entity has no device. Without it the
        registry fills with names like ``number.temperature`` instead of
        ``number.centauri_carbon_nozzle_target``.
        """
        super().__init__(coordinator)
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._attr_device_info = async_device_info(coordinator.runtime)

    @property
    def runtime(self) -> PrinterRuntime:
        """Return everything this integration knows about the printer."""
        return self.coordinator.runtime

    @property
    def available(self) -> bool:
        """Return ``False`` while the last poll failed. A refused control stays available."""
        return self.coordinator.last_update_success

    @property
    def blocked_reason(self) -> str | None:
        """Return why the printer refuses this entity's command now, if it does."""
        snapshot = self.coordinator.data
        if self._command is None or snapshot is None:
            return None
        return snapshot.blocked.get(self._command)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Report the reason a control is refused, so automations can read it."""
        reason = self.blocked_reason
        return {"blocked_reason": reason} if reason is not None else None


type FilamentEntityFactories = Mapping[str, Callable[[], Entity]]


@callback
def async_follow_filament(
    entry: ConfigEntry,
    coordinator: PrinterCoordinator,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[FilamentSystem], FilamentEntityFactories],
) -> None:
    """Add a platform's filament entities as the printer first reports them.

    ``build`` maps each entity the current system calls for to a factory, keyed by
    something stable such as the slot. A key is built once: a slot that stops being
    reported keeps its entity, which then reads ``None``, rather than the entity
    being removed and its history lost when a unit is unplugged for a moment.
    """
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        snapshot = coordinator.data
        filament = snapshot.filament if snapshot is not None else None
        if filament is None:
            return
        fresh = {key: factory for key, factory in build(filament).items() if key not in known}
        if fresh:
            known.update(fresh)
            async_add_entities([factory() for factory in fresh.values()])

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))

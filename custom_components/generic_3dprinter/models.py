"""The normalised printer model.

`PrinterSnapshot` is the only currency between an adapter and everything else in
the integration. It is frozen, and every field a protocol might not be able to
express defaults to `None` or an empty collection.

That is not hedging. It is the measured truth of this fleet: OctoPrint and
PrusaLink report no layer count at all, Duet computes progress instead of
reporting it, and a web-only printer has no state surface whatsoever. `None`
means "this printer has no such reading", and the card hides the row rather than
the adapter inventing a number.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, NewType

from .const import Capability, Command, LightChannel, PrintState, ProtocolId

#: Branded scalars. At runtime these are floats; the brand is documentation the
#: type checker enforces, because Bambu and Anycubic report remaining time in
#: minutes while every other protocol reports seconds. That mistake is invisible
#: in a bare float.
Celsius = NewType("Celsius", float)
Seconds = NewType("Seconds", float)
Percent = NewType("Percent", float)
Millimetres = NewType("Millimetres", float)


@dataclass(frozen=True, slots=True)
class Temps:
    """Current and target temperature for one heater."""

    current: Celsius | None = None
    target: Celsius | None = None

    @property
    def known(self) -> bool:
        """Return ``True`` when either reading is present."""
        return self.current is not None or self.target is not None

    def as_dict(self) -> dict[str, float | None]:
        """Return a JSON-safe mapping."""
        return {"current": self.current, "target": self.target}


@dataclass(frozen=True, slots=True)
class Fans:
    """Fan duty per channel, as a percentage.

    Protocols that report duty in device units normalise here. Bambu reports 0 to
    15, so its adapter multiplies. Protocols that can only report revolutions per
    minute leave every channel `None` rather than publish a value in the wrong
    unit.
    """

    model: Percent | None = None
    auxiliary: Percent | None = None
    chamber: Percent | None = None
    hotend: Percent | None = None
    controller: Percent | None = None

    @property
    def known(self) -> bool:
        """Return ``True`` when any channel has a reading."""
        return any(
            getattr(self, item.name) is not None for item in fields(self)
        )

    def as_dict(self) -> dict[str, float | None]:
        """Return a JSON-safe mapping."""
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class Axis:
    """Toolhead position in millimetres."""

    x: Millimetres | None = None
    y: Millimetres | None = None
    z: Millimetres | None = None

    def as_dict(self) -> dict[str, float | None]:
        """Return a JSON-safe mapping."""
        return {"x": self.x, "y": self.y, "z": self.z}


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One stored G-code file."""

    name: str
    path: str
    size: int | None = None
    modified: datetime | None = None

    @property
    def display_name(self) -> str:
        """Return the file name without its storage path."""
        return self.name.rsplit("/", 1)[-1]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping."""
        return {
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "modified": self.modified.isoformat() if self.modified else None,
        }


@dataclass(frozen=True, slots=True)
class FilamentSlot:
    """One slot of a multi-material unit, and the filament recorded for it.

    ``loaded`` says whether a spool is in the slot. The material, name and colour
    are what the printer has on record for the slot, which it keeps while the slot
    is empty, so they are reported either way and a reader decides what to show.
    """

    unit: int
    slot: int
    loaded: bool = False
    #: The slot is feeding the nozzle now.
    active: bool = False
    material: str | None = None
    name: str | None = None
    brand: str | None = None
    #: ``#RRGGBB``.
    color: str | None = None
    min_temp: Celsius | None = None
    max_temp: Celsius | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping."""
        return {
            "unit": self.unit,
            "slot": self.slot,
            "loaded": self.loaded,
            "active": self.active,
            "material": self.material,
            "name": self.name,
            "brand": self.brand,
            "color": self.color,
            "min_temp": self.min_temp,
            "max_temp": self.max_temp,
        }


@dataclass(frozen=True, slots=True)
class FilamentUnit:
    """One multi-material unit, such as one Elegoo CANVAS, and its slots."""

    unit: int
    connected: bool = True
    #: What the unit is called, for a heading. ``None`` when the printer says nothing.
    name: str | None = None
    slots: tuple[FilamentSlot, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping."""
        return {
            "unit": self.unit,
            "connected": self.connected,
            "name": self.name,
            "slots": [item.as_dict() for item in self.slots],
        }


@dataclass(frozen=True, slots=True)
class FilamentSystem:
    """Every multi-material unit attached to a printer.

    ``None`` on the snapshot means the printer reports no such system, which is
    different from a system whose units are all disconnected.
    """

    units: tuple[FilamentUnit, ...] = ()
    #: Whether the printer switches to a matching slot when one runs out.
    auto_refill: bool | None = None
    #: What the system is doing right now, such as loading, in words.
    activity: str | None = None

    @property
    def slots(self) -> tuple[FilamentSlot, ...]:
        """Return every slot of every unit, in order."""
        return tuple(slot for unit in self.units for slot in unit.slots)

    @property
    def active(self) -> FilamentSlot | None:
        """Return the slot feeding the nozzle, if any."""
        return next((slot for slot in self.slots if slot.active), None)

    def slot(self, unit: int, slot: int) -> FilamentSlot | None:
        """Return one slot, or ``None`` when the system has no such slot."""
        return next(
            (item for item in self.slots if item.unit == unit and item.slot == slot), None
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping."""
        active = self.active
        return {
            "units": [item.as_dict() for item in self.units],
            "auto_refill": self.auto_refill,
            "activity": self.activity,
            "active": {"unit": active.unit, "slot": active.slot} if active else None,
        }


@dataclass(frozen=True, slots=True)
class PrinterSnapshot:
    """One complete, normalised view of a printer at one instant.

    The first four fields are always known, because the adapter itself owns them.
    Everything the printer reports is optional.
    """

    protocol: ProtocolId
    connected: bool
    capabilities: frozenset[Capability] = frozenset()
    print_state: PrintState = PrintState.UNKNOWN

    # --- job
    progress: Percent | None = None
    current_layer: int | None = None
    total_layers: int | None = None
    remaining: Seconds | None = None
    elapsed: Seconds | None = None
    filename: str | None = None
    job_id: str | None = None
    speed_factor: Percent | None = None
    flow_factor: Percent | None = None

    # --- heaters
    hotend: Temps = Temps()
    bed: Temps = Temps()
    chamber: Temps = Temps()

    # --- mechanism
    fans: Fans = Fans()
    position: Axis | None = None
    homed_axes: frozenset[str] = frozenset()
    lights: frozenset[LightChannel] = frozenset()

    # --- camera
    camera: bool = False

    # --- filament
    filament: FilamentSystem | None = None

    # --- provenance
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    errors: tuple[str, ...] = ()

    # --- state rules
    #: Commands the printer supports but will not accept in its current state, each
    #: with the reason a user reads. Filled by the base class from the adapter's
    #: rules, never by an adapter directly.
    blocked: Mapping[Command, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def idle(self) -> bool:
        """Return ``True`` when the machine is idle.

        Derived rather than stored, so it cannot contradict :attr:`print_state`.
        """
        return self.print_state is PrintState.IDLE

    @property
    def busy(self) -> bool:
        """Return ``True`` when a job is running or held."""
        return self.print_state in (PrintState.PRINTING, PrintState.PAUSED)

    @property
    def has_job(self) -> bool:
        """Return ``True`` when a job is in progress or just finished."""
        return self.print_state in (
            PrintState.PRINTING,
            PrintState.PAUSED,
            PrintState.FINISHED,
            PrintState.CANCELLED,
        )

    def supports(self, capability: Capability) -> bool:
        """Return ``True`` when this printer grants ``capability`` right now."""
        return capability in self.capabilities

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe document for the card, diagnostics and attributes."""
        return {
            "protocol": self.protocol.value,
            "connected": self.connected,
            "print_state": self.print_state.value,
            "capabilities": sorted(item.value for item in self.capabilities),
            "progress": self.progress,
            "current_layer": self.current_layer,
            "total_layers": self.total_layers,
            "remaining": self.remaining,
            "elapsed": self.elapsed,
            "filename": self.filename,
            "job_id": self.job_id,
            "speed_factor": self.speed_factor,
            "flow_factor": self.flow_factor,
            "hotend": self.hotend.as_dict(),
            "bed": self.bed.as_dict(),
            "chamber": self.chamber.as_dict(),
            "fans": self.fans.as_dict(),
            "position": self.position.as_dict() if self.position else None,
            "homed_axes": sorted(self.homed_axes),
            "lights": sorted(item.value for item in self.lights),
            "camera": self.camera,
            "filament": self.filament.as_dict() if self.filament else None,
            "model": self.model,
            "firmware": self.firmware,
            "serial": self.serial,
            "errors": list(self.errors),
            "blocked": {command.value: reason for command, reason in self.blocked.items()},
        }


@dataclass(slots=True)
class PrinterStatus:
    """A serialisable health and capability report, used by the card and diagnostics."""

    entry_id: str
    name: str
    protocol: ProtocolId
    model: str | None
    firmware: str | None
    online: bool
    last_error: str | None
    last_seen: datetime | None
    snapshot: PrinterSnapshot
    web_ui_url: str | None = None
    web_proxy_url: str | None = None
    camera_url: str | None = None
    snapshot_url: str | None = None
    files: list[FileEntry] = field(default_factory=list)
    unsafe_features: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe document."""
        return {
            "entry_id": self.entry_id,
            "name": self.name,
            "protocol": self.protocol.value,
            "model": self.model,
            "firmware": self.firmware,
            "online": self.online,
            "last_error": self.last_error,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "web_ui_url": self.web_ui_url,
            "web_proxy_url": self.web_proxy_url,
            "camera_url": self.camera_url,
            "snapshot_url": self.snapshot_url,
            "unsafe_features": list(self.unsafe_features),
            "printer": self.snapshot.as_dict(),
            "files": [item.as_dict() for item in self.files],
        }

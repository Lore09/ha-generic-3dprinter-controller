"""The adapter contract every protocol implements.

One adapter instance exists per config entry. Everything outside
``adapters/`` talks to a printer through this interface and through
:class:`~.models.PrinterSnapshot`, so no consumer ever asks which protocol a
printer speaks.

The guards live on :class:`BaseProtocol`, not in each adapter body, so an adapter
author writes only the happy path and a command that a printer cannot accept is
refused before any vendor code runs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

import aiohttp

from .const import (
    CREDENTIAL_KEYS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TLS,
    DEFAULT_VERIFY_SSL,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    Capability,
    STATE_CHANGING_COMMANDS,
    Command,
    ModelProfile,
    PrintState,
    ProtocolId,
    UnsafeFeature,
)
from .models import FileEntry, PrinterSnapshot
from .validation import ParamError, validate_params

if TYPE_CHECKING:
    from .discovery import DiscoveryResult

COMMAND_CAPABILITY: Final[Mapping[Command, Capability]] = MappingProxyType(
    {
        Command.START_PRINT: Capability.START_PRINT,
        Command.PAUSE: Capability.PAUSE,
        Command.RESUME: Capability.RESUME,
        Command.STOP: Capability.STOP,
        Command.SET_HOTEND_TEMP: Capability.SET_HOTEND_TEMP,
        Command.SET_BED_TEMP: Capability.SET_BED_TEMP,
        Command.SET_CHAMBER_TEMP: Capability.SET_CHAMBER_TEMP,
        Command.SET_FAN_SPEED: Capability.SET_FAN_SPEED,
        Command.SET_SPEED: Capability.SET_SPEED,
        Command.SET_FLOW: Capability.SET_FLOW,
        Command.SET_LIGHT: Capability.SET_LIGHT,
        Command.HOME: Capability.HOME,
        Command.JOG: Capability.JOG,
        Command.DELETE_FILE: Capability.FILE_DELETE,
        Command.LOAD_FILAMENT: Capability.LOAD_FILAMENT,
        Command.UNLOAD_FILAMENT: Capability.UNLOAD_FILAMENT,
        Command.SET_FILAMENT: Capability.SET_FILAMENT,
        Command.SET_AUTO_REFILL: Capability.SET_AUTO_REFILL,
    }
)


def command_capability(command: Command) -> Capability:
    """Return the capability ``command`` requires. The single source of truth."""
    return COMMAND_CAPABILITY[command]


class ConfigError(ValueError):
    """Raised when user supplied configuration is not usable."""


class ProtocolError(Exception):
    """Base class for every failure raised across the adapter boundary."""


class UnreachableError(ProtocolError):
    """The device did not answer."""


class AuthError(ProtocolError):
    """The device answered and refused the credential."""


class ProtocolShapeError(ProtocolError):
    """The device answered with data this adapter cannot parse."""


class CommandRejectedError(ProtocolError):
    """The device accepted the request and refused the command."""

    def __init__(
        self, message: str, *, code: int | str | None = None, reason: str | None = None
    ) -> None:
        """Record the device's own error code and reason alongside the message."""
        super().__init__(message)
        self.code = code
        self.reason = reason


class UnsupportedCommandError(ProtocolError):
    """The adapter does not support this command on this printer."""

    def __init__(self, command: Command, missing: Capability | None = None) -> None:
        """Name the command and the capability that is absent."""
        detail = f" (requires {missing.value})" if missing else ""
        super().__init__(f"{command.value} is not supported by this printer{detail}")
        self.command = command
        self.missing = missing


class UnsafeCommandError(ProtocolError):
    """The command is gated behind an opt-in that this entry did not grant."""

    def __init__(self, feature: UnsafeFeature) -> None:
        """Carry the feature's own wording so the user reads the real reason."""
        super().__init__(
            f"refused: {feature.label}. {feature.reason} "
            f"Enable it in the integration options if you accept the risk."
        )
        self.feature = feature


class CommandBlockedError(ProtocolError):
    """The printer supports the command but will not take it in its current state."""

    def __init__(self, command: Command, reason: str) -> None:
        """Name the command and the printer's reason."""
        super().__init__(f"{command.value} is not possible now: {reason}")
        self.command = command
        self.reason = reason


@dataclass(frozen=True, slots=True)
class BlockRule:
    """Commands a printer refuses while ``when`` holds for its latest snapshot.

    Rules are data an adapter declares, so the card, the entities and the guard in
    :meth:`Protocol.async_send` all read one computed set and cannot disagree.
    """

    commands: frozenset[Command]
    when: Callable[[PrinterSnapshot], bool]
    reason: str


def job_active(snapshot: PrinterSnapshot) -> bool:
    """Return ``True`` while a job is being prepared, printed or held."""
    return snapshot.print_state in (PrintState.PREPARING, PrintState.PRINTING, PrintState.PAUSED)


def not_idle(snapshot: PrinterSnapshot) -> bool:
    """Return ``True`` unless the printer says it is idle."""
    return snapshot.print_state is not PrintState.IDLE


#: Every printer refuses to move its head, start a job or swap filament during a
#: job. The card used to enforce this itself; it is a rule now, so an adapter can
#: tighten it and a user reads the reason.
DEFAULT_BLOCK_RULES: Final[tuple[BlockRule, ...]] = (
    BlockRule(
        STATE_CHANGING_COMMANDS,
        when=job_active,
        reason="the printer is busy with a job",
    ),
)


def _as_int(value: Any, default: int, minimum: int, maximum: int, label: str) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as err:
        raise ConfigError(f"{label} must be a number") from err
    if not minimum <= number <= maximum:
        raise ConfigError(f"{label} must be between {minimum} and {maximum}")
    return number


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off")
    return bool(value)


def validate_host(host: Any, *, label: str = "host") -> str:
    """Validate a hostname or IP address, rejecting anything with a path or scheme."""
    if not isinstance(host, str) or not host.strip():
        raise ConfigError(f"{label} is required")
    cleaned = host.strip()
    if "://" in cleaned:
        parts = urlsplit(cleaned)
        if not parts.hostname:
            raise ConfigError(f"{label} is not a usable address: {cleaned!r}")
        cleaned = parts.hostname
    if any(char in cleaned for char in "/?# "):
        raise ConfigError(f"{label} must be a bare host name or IP address")
    return cleaned


def validate_url(url: Any, *, schemes: tuple[str, ...] = ("http", "https"), label: str = "url") -> str:
    """Validate an absolute URL using one of ``schemes``."""
    if not isinstance(url, str) or not url.strip():
        raise ConfigError(f"{label} is required")
    cleaned = url.strip()
    parts = urlsplit(cleaned)
    if parts.scheme.lower() not in schemes or not parts.netloc:
        allowed = ", ".join(schemes)
        raise ConfigError(f"{label} must be an absolute URL using one of: {allowed}")
    return cleaned


@dataclass(frozen=True, slots=True)
class PrinterConfig:
    """Immutable, validated description of one printer.

    Built only by :func:`parse_config`, so everything downstream may trust it.
    """

    name: str
    protocol: ProtocolId
    host: str
    port: int | None = None
    tls: bool = DEFAULT_TLS
    verify_ssl: bool = DEFAULT_VERIFY_SSL
    scan_interval: int = DEFAULT_SCAN_INTERVAL
    web_url: str | None = None
    camera_port: int | None = None
    #: The printer's own serial number, for a protocol that addresses a printer by
    #: it rather than by its network address.
    serial: str | None = None
    #: Values, never keys. Redacted out of diagnostics.
    credentials: Mapping[str, str] = field(default_factory=dict)
    #: Ids of the :class:`UnsafeFeature` values the user explicitly accepted.
    unsafe_enabled: frozenset[str] = frozenset()

    @property
    def scheme(self) -> str:
        """Return the HTTP scheme implied by :attr:`tls`."""
        return "https" if self.tls else "http"

    @property
    def base_url(self) -> str:
        """Return the printer's base HTTP URL, without credentials."""
        if self.port is None:
            return f"{self.scheme}://{self.host}"
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def redacted_url(self) -> str:
        """Return a URL safe for logs and diagnostics."""
        return self.base_url

    def with_overrides(self, data: Mapping[str, Any]) -> PrinterConfig:
        """Return a copy with ``data`` applied, for the options flow."""
        merged = self.as_dict(include_secrets=True)
        merged.update(data)
        return parse_config(merged, existing=self)

    def as_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        """Return the configuration as a plain dict, optionally without secrets."""
        data: dict[str, Any] = {
            "name": self.name,
            "protocol": self.protocol.value,
            "host": self.host,
            "port": self.port,
            "tls": self.tls,
            "verify_ssl": self.verify_ssl,
            "scan_interval": self.scan_interval,
            "web_url": self.web_url,
            "camera_port": self.camera_port,
            "serial": self.serial,
            "unsafe_enabled": sorted(self.unsafe_enabled),
        }
        if include_secrets:
            data.update(self.credentials)
        return data


def parse_config(
    data: Mapping[str, Any], *, existing: PrinterConfig | None = None
) -> PrinterConfig:
    """Validate raw config entry data and options into a :class:`PrinterConfig`.

    Raises :class:`ConfigError`. The protocol id is checked lazily against the
    adapter registry by the caller, because the registry imports this module.
    """
    if not isinstance(data, Mapping):
        raise ConfigError("configuration must be a mapping")

    raw_protocol = str(data.get("protocol") or "").strip().lower()
    try:
        protocol = ProtocolId(raw_protocol)
    except ValueError as err:
        raise ConfigError(f"unsupported protocol: {raw_protocol or '(missing)'}") from err

    name = str(data.get("name") or "").strip()
    if not name:
        raise ConfigError("a name is required")

    host = validate_host(data.get("host"))

    port_raw = data.get("port")
    port: int | None = None
    if port_raw not in (None, ""):
        port = _as_int(port_raw, 0, 1, 65535, "port")

    camera_raw = data.get("camera_port")
    camera_port: int | None = None
    if camera_raw not in (None, ""):
        camera_port = _as_int(camera_raw, 0, 1, 65535, "camera port")

    web_url = data.get("web_url") or None
    if web_url:
        web_url = validate_url(web_url, label="web ui url")

    serial = str(data.get("serial") or "").strip() or None
    if serial is not None and (len(serial) > 64 or not serial.isalnum()):
        # The serial becomes part of an MQTT topic, where "/", "+" and "#" have
        # meaning, so only the letters and digits a real serial is made of pass.
        raise ConfigError("serial number must be letters and digits only")

    credentials: dict[str, str] = {}
    for key in CREDENTIAL_KEYS:
        value = data.get(key)
        if value not in (None, ""):
            credentials[key] = str(value)
    if existing is not None:
        # An empty field means "keep the stored value" when editing an entry.
        for key, value in existing.credentials.items():
            credentials.setdefault(key, value)

    raw_unsafe = data.get("unsafe_enabled") or ()
    if isinstance(raw_unsafe, str):
        raw_unsafe = [item for item in raw_unsafe.split(",") if item.strip()]
    unsafe_enabled = frozenset(str(item) for item in raw_unsafe)

    return PrinterConfig(
        name=name,
        protocol=protocol,
        host=host,
        port=port,
        tls=_as_bool(data.get("tls"), DEFAULT_TLS),
        verify_ssl=_as_bool(data.get("verify_ssl"), DEFAULT_VERIFY_SSL),
        scan_interval=_as_int(
            data.get("scan_interval"),
            DEFAULT_SCAN_INTERVAL,
            MIN_SCAN_INTERVAL,
            MAX_SCAN_INTERVAL,
            "scan interval",
        ),
        web_url=web_url,
        camera_port=camera_port,
        serial=serial,
        credentials=credentials,
        unsafe_enabled=unsafe_enabled,
    )


class Protocol(ABC):
    """Transport adapter for one printer. One instance per config entry."""

    #: The printer's state rules. An adapter extends the defaults rather than
    #: replacing them, unless its printer is known to allow more, and puts its own
    #: rules first: the first rule that blocks a command gives its reason.
    block_rules: tuple[BlockRule, ...] = DEFAULT_BLOCK_RULES

    def __init__(
        self,
        config: PrinterConfig,
        session: aiohttp.ClientSession,
        *,
        granted: frozenset[Capability],
        unsafe: tuple[UnsafeFeature, ...] = (),
        models: tuple[ModelProfile, ...] = (),
    ) -> None:
        """Store the configuration, the shared HTTP session and the granted set.

        ``granted`` is the registration's set after every opt-in gate; ``models``
        narrows it further once the adapter reports which model it reached.
        """
        self._config = config
        self._session = session
        self._granted = granted
        self._unsafe = unsafe
        self._models = models
        self._model_id: str | None = None
        self._last_snapshot: PrinterSnapshot | None = None

    @property
    def config(self) -> PrinterConfig:
        """Return this adapter's configuration."""
        return self._config

    @property
    def session(self) -> aiohttp.ClientSession:
        """Return the session shared by every adapter of this entry."""
        return self._session

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Return the capabilities this printer grants, after every gate.

        Narrowed to the model's profile when the adapter has reported a model its
        registration knows. An unknown model keeps the registration's whole set,
        and the snapshot says the model is unknown.
        """
        profile = self.model_profile
        if profile is None:
            return self._granted
        return self._granted & profile.capabilities

    @property
    def model_id(self) -> str | None:
        """Return the model id the printer reported, if the adapter has one."""
        return self._model_id

    @property
    def model_profile(self) -> ModelProfile | None:
        """Return the profile of the model this adapter reached, if it is known."""
        if self._model_id is None:
            return None
        return next((item for item in self._models if item.id == self._model_id), None)

    def _set_model_id(self, model_id: str | None) -> None:
        """Record which model the printer says it is. Called by an adapter in setup."""
        self._model_id = model_id

    @property
    def unsafe_features(self) -> tuple[UnsafeFeature, ...]:
        """Return the hazards this protocol declares."""
        return self._unsafe

    @property
    def filament_presets(self) -> tuple[Mapping[str, Any], ...]:
        """Return the filaments the card offers when a slot's filament is set.

        Each is ``material``, ``name``, ``min_temp``, ``max_temp`` and ``brands``.
        Empty unless the printer can record a slot's filament and keeps a list of
        its own; the card then asks for the material in words.
        """
        return ()

    @classmethod
    async def async_discover(cls, timeout: float) -> list[DiscoveryResult]:
        """Look for this protocol's printers on the local network.

        Read-only: a probe may ask a printer who it is and nothing more. The
        default finds nothing, for a protocol with no discovery of its own.
        """
        return []

    @classmethod
    async def async_identify(cls, host: str, timeout: float) -> DiscoveryResult | None:
        """Ask ``host`` whether it is one of this protocol's printers.

        Read-only, like :meth:`async_discover`. ``None`` means no, or no answer.
        """
        return None

    @classmethod
    async def async_prepare_config(cls, config: PrinterConfig) -> PrinterConfig:
        """Check a configuration before its entry is created, filling in what it can.

        Called by the config flow, never at runtime. It may probe the printer but
        must not send it a command. Raises :class:`ConfigError` with a message the
        user can act on. The default accepts the configuration unchanged.
        """
        return config

    @abstractmethod
    async def async_setup(self) -> None:
        """Open what this protocol needs and verify the credential once.

        Idempotent. Raises :class:`UnreachableError` or :class:`AuthError`. Called
        at entry setup and after a failed read, never per poll.
        """
        raise NotImplementedError

    @abstractmethod
    async def async_teardown(self) -> None:
        """Close sockets and subscriptions. Idempotent."""
        raise NotImplementedError

    @property
    def last_snapshot(self) -> PrinterSnapshot | None:
        """Return the snapshot of the last successful read, if any."""
        return self._last_snapshot

    async def async_read(self) -> PrinterSnapshot:
        """Return one complete snapshot, with capabilities and state rules applied.

        Raises :class:`UnreachableError` or :class:`AuthError`. Adapters implement
        :meth:`_async_read`; this wrapper is what makes every snapshot carry the
        granted capabilities and the blocked commands, whatever the adapter built.
        """
        snapshot = await self._async_read()
        snapshot = replace(snapshot, capabilities=self.capabilities)
        profile = self.model_profile
        if profile is not None:
            snapshot = replace(snapshot, model=profile.name)
        elif self._models and self._model_id is not None:
            snapshot = replace(snapshot, errors=(*snapshot.errors, f"unknown model {self._model_id}"))
        snapshot = replace(snapshot, blocked=MappingProxyType(self._blocked(snapshot)))
        self._last_snapshot = snapshot
        return snapshot

    def _blocked(self, snapshot: PrinterSnapshot) -> dict[Command, str]:
        """Return the granted commands the rules refuse for ``snapshot``."""
        blocked: dict[Command, str] = {}
        for rule in self.block_rules:
            if not rule.when(snapshot):
                continue
            for command in sorted(rule.commands):
                if command_capability(command) in snapshot.capabilities:
                    blocked.setdefault(command, rule.reason)
        return blocked

    @abstractmethod
    async def _async_read(self) -> PrinterSnapshot:
        """Read the printer once and return what it reported.

        Raises :class:`UnreachableError` or :class:`AuthError`. Never raises for a
        field the protocol cannot express: that field stays ``None``.
        """
        raise NotImplementedError

    def async_subscribe(self) -> AsyncIterator[PrinterSnapshot] | None:
        """Return a push iterator, or ``None`` when this adapter is poll only."""
        return None

    async def async_send(self, command: Command, **params: Any) -> None:
        """Ask the printer to do something, refusing anything it cannot accept."""
        required = command_capability(command)
        if required not in self.capabilities:
            feature = self._gating_feature(required)
            if feature is not None:
                raise UnsafeCommandError(feature)
            raise UnsupportedCommandError(command, required)

        try:
            validated = validate_params(command, params)
        except ParamError as err:
            raise ProtocolError(str(err)) from err

        # The rules need a state to judge. A command before the first read reads the
        # printer once, so none slips past them; and a command the last snapshot
        # blocks is judged again on a fresh read, because the snapshot can be a poll
        # old and a print that just ended must not keep the head locked.
        snapshot = self._last_snapshot
        if snapshot is None or command in snapshot.blocked:
            snapshot = await self.async_read()
        reason = snapshot.blocked.get(command)
        if reason is not None:
            raise CommandBlockedError(command, reason)

        await self._async_dispatch(command, validated)

    def _gating_feature(self, capability: Capability) -> UnsafeFeature | None:
        """Return the declared hazard that withholds ``capability``, if any."""
        for feature in self._unsafe:
            if capability in feature.gates:
                return feature
        return None

    @abstractmethod
    async def _async_dispatch(
        self, command: Command, params: Mapping[str, Any]
    ) -> None:
        """Adapter happy path. Only reached once every guard has passed."""
        raise NotImplementedError

    @abstractmethod
    async def async_list_files(self) -> Sequence[FileEntry]:
        """List stored print jobs."""
        raise NotImplementedError

    @abstractmethod
    async def async_upload_file(
        self, name: str, stream: AsyncIterator[bytes], *, size: int | None = None
    ) -> FileEntry:
        """Upload one G-code file and return its stored entry."""
        raise NotImplementedError

    async def async_stream_source(self) -> str:
        """Start the printer's video stream if it needs starting, and return its URL.

        Only for a printer with ``CAMERA_STREAM``. It may send the printer whatever
        switches its camera on, so it is called when someone plays the stream, not
        when the camera entity is created.
        """
        raise UnreachableError("this protocol has no video stream")

    async def async_camera_frame(self) -> bytes:
        """Return one complete JPEG frame.

        Adapters without a camera are never called, because ``CAMERA`` is absent
        from their capability set.
        """
        raise UnreachableError("this protocol has no camera")

    def __repr__(self) -> str:
        """Return a representation that never leaks a credential."""
        return (
            f"<{type(self).__name__} {self._config.name} "
            f"{self._config.protocol.value} {self._config.redacted_url}>"
        )

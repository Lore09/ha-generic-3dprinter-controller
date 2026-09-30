"""Elegoo resin printers over SDCP V3, such as the Saturn 4 Ultra 16K. Read-only for now.
It shares the socket with the Centauri Carbon adapter and can send only commands 0, 1 and 258."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from types import MappingProxyType
from typing import Any, Final

import aiohttp

from ..const import Capability, Command, ModelProfile, PrintState, ProtocolId, ResinPhase, UnsafeFeature
from ..discovery import DISCOVERY_TIMEOUT, DiscoveryResult, async_probe_udp
from ..models import FileEntry, Percent, PrinterSnapshot, ResinState, Seconds, Temps
from ..protocols import ConfigError, PrinterConfig, ProtocolError, WrongPrinterError, valid_serial
from . import sdcp
from .sdcp import (
    SDCP_DISCOVERY_PORT,
    SDCP_DISCOVERY_PROBE,
    SESSION_COMMAND,
    SdcpSession,
    _celsius,
    _integer,
    _number,
    classify_sdcp,
    sdcp_identity,
    status_flags,
)

#: The printer counts ``CurrentTicks`` and ``TotalTicks`` in milliseconds (spec en.md:149-150).
TICKS_PER_SECOND: Final = 1000.0

#: Nothing pushes the attributes unasked, so command 1 is sent again this often.
ATTRIBUTES_INTERVAL: Final = 300.0

#: ``CurrentStatus`` codes, the machine's own state (spec en.md:172-180; 8 from Elegoo's SDK).
MACHINE_BY_STATUS: Final[Mapping[int, str]] = MappingProxyType(
    {
        0: "idle",
        1: "printing",
        2: "file_transferring",
        3: "exposure_test",
        4: "self_check",
        8: "file_received",
    }
)

#: A machine that is not printing and says what else it does, with ``PrintInfo.Status`` 0.
MACHINE_PHASES: Final[Mapping[int, ResinPhase]] = MappingProxyType(
    {
        2: ResinPhase.FILE_TRANSFERRING,
        3: ResinPhase.EXPOSURE_TEST,
        4: ResinPhase.SELF_CHECK,
        8: ResinPhase.FILE_RECEIVED,
    }
)

#: ``PrintInfo.Status`` while ``CurrentStatus`` holds 1 (spec en.md:181-203; 16 from Elegoo's SDK).
JOB_PHASES: Final[Mapping[int, tuple[PrintState, ResinPhase]]] = MappingProxyType(
    {
        0: (PrintState.PREPARING, ResinPhase.STARTING),
        1: (PrintState.PREPARING, ResinPhase.HOMING),
        2: (PrintState.PRINTING, ResinPhase.DESCENDING),
        3: (PrintState.PRINTING, ResinPhase.EXPOSING),
        4: (PrintState.PRINTING, ResinPhase.LIFTING),
        5: (PrintState.PAUSED, ResinPhase.PAUSING),
        6: (PrintState.PAUSED, ResinPhase.PAUSED),
        7: (PrintState.CANCELLED, ResinPhase.STOPPING),
        8: (PrintState.CANCELLED, ResinPhase.STOPPED),
        9: (PrintState.FINISHED, ResinPhase.COMPLETED),
        10: (PrintState.PREPARING, ResinPhase.FILE_CHECKING),
        16: (PrintState.PREPARING, ResinPhase.PREHEATING),
    }
)
#: The spec says an idle machine keeps the code of the job that ended; V1.5.6 resets it to 0.
RETAINED_JOB_STATUS: Final = frozenset({8, 9})

#: The states in which the job's progress, times and layers mean the job in hand.
JOB_STATES: Final = frozenset({PrintState.PREPARING, PrintState.PRINTING, PrintState.PAUSED})

#: ``PrintInfo.ErrorNumber`` (spec en.md:206-218).
PRINT_ERRORS: Final[Mapping[int, str]] = MappingProxyType(
    {
        1: "the print file failed its MD5 check",
        2: "the print file could not be read",
        3: "the print file's resolution does not match the printer",
        4: "the print file's format does not match the printer",
        5: "the print file was sliced for another printer model",
    }
)


def build_resin_frame(
    mainboard_id: str, cmd: int, data: Mapping[str, Any] | None = None
) -> tuple[str, str]:
    """Return ``(request_id, frame)`` in the envelope a Saturn was read with: a random ``Id``,
    a ``Topic`` and the time in seconds, where the Centauri's frame has none of the three."""
    request_id = uuid.uuid4().hex
    frame = json.dumps(
        {
            "Id": uuid.uuid4().hex,
            "Data": {
                "Cmd": cmd,
                "Data": dict(data or {}),
                "RequestID": request_id,
                "MainboardID": mainboard_id,
                "TimeStamp": int(time.time()),
                "From": 1,
            },
            "Topic": f"sdcp/request/{mainboard_id}",
        }
    )
    return request_id, frame


def resin_state_for(
    flags: tuple[int, ...], print_status: int | None
) -> tuple[PrintState, ResinPhase | None]:
    """Return the print state and the phase; ``CurrentStatus`` decides first.
    A job's own code is read only while the machine prints, or when it ended and was kept."""
    if not flags:
        return PrintState.UNKNOWN, None
    if 1 in flags:
        return JOB_PHASES.get(print_status, (PrintState.PRINTING, ResinPhase.OTHER))  # type: ignore[arg-type]
    for code in flags:
        if code in MACHINE_PHASES:
            return PrintState.IDLE, MACHINE_PHASES[code]
    if 0 in flags:
        if print_status in RETAINED_JOB_STATUS:
            return JOB_PHASES[print_status]  # type: ignore[index]
        return PrintState.IDLE, ResinPhase.IDLE
    return PrintState.UNKNOWN, ResinPhase.OTHER


def machine_for(flags: tuple[int, ...]) -> str | None:
    """Return the machine's own state by name, ``other`` for a code with no name."""
    if not flags:
        return None
    code = next((item for item in flags if item != 0), flags[0])
    return MACHINE_BY_STATUS.get(code, "other")


def _job_fields(state: PrintState, info: Mapping[str, Any]) -> dict[str, Any]:
    """Return progress, times and layers, only while a job is in hand (100 % once finished).
    An idle Saturn keeps the last job's layers and ticks, which would read as a job."""
    empty = dict.fromkeys(("progress", "current_layer", "total_layers", "elapsed", "remaining"))
    if state is PrintState.FINISHED:
        return {**empty, "progress": Percent(100.0)}
    if state not in JOB_STATES:
        return empty
    layer = _integer(info.get("CurrentLayer"))
    layers = _integer(info.get("TotalLayer"))
    current = _number(info.get("CurrentTicks"))
    total = _number(info.get("TotalTicks"))
    progress = None
    if layer is not None and layers:
        progress = Percent(min(max(layer / layers * 100, 0.0), 100.0))
    remaining = None
    if current is not None and total is not None:
        remaining = Seconds(max(total - current, 0.0) / TICKS_PER_SECOND)
    return {
        "progress": progress,
        "current_layer": layer,
        "total_layers": layers,
        "elapsed": Seconds(current / TICKS_PER_SECOND) if current is not None else None,
        "remaining": remaining,
    }


def device_faults(attributes: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the ``DevicesStatus`` checks that do not report 1, which is OK."""
    devices = attributes.get("DevicesStatus")
    if not isinstance(devices, Mapping):
        return ()
    return tuple(sorted(str(name) for name, value in devices.items() if _integer(value) != 1))


def parse_resin(status: Mapping[str, Any], attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Return the snapshot fields one status and one attributes payload give.
    A pure function of the two, so every mapping is tested without a socket."""
    info = status.get("PrintInfo")
    info = info if isinstance(info, Mapping) else {}
    flags = status_flags(status.get("CurrentStatus"))
    print_status = _integer(info.get("Status"))
    state, phase = resin_state_for(flags, print_status)
    faults = device_faults(attributes)
    timelapse = _integer(status.get("TimeLapseStatus"))
    resin = ResinState(
        machine=machine_for(flags),
        phase=phase,
        phase_code=print_status,
        uv_led=_celsius(status.get("TempOfUVLED")),
        vat=Temps(current=_celsius(status.get("TempOfTank")), target=_celsius(status.get("TempTargetTank"))),
        vat_heat_status=_integer(status.get("HeatStatus")),
        release_film=_integer(status.get("ReleaseFilm")),
        release_film_max=_integer(attributes.get("ReleaseFilmMax")),
        printer_timelapse=bool(timelapse) if timelapse is not None else None,
        device_faults=faults,
        video_streams=_integer(attributes.get("NumberOfVideoStreamConnected")),
        video_streams_max=_integer(attributes.get("MaximumVideoStreamAllowed")),
    )
    errors: list[str] = []
    error = _integer(info.get("ErrorNumber"))
    if error:
        errors.append(PRINT_ERRORS.get(error, f"the printer reports print error {error}"))
    errors.extend(f"the printer's {name} check is failing" for name in faults)
    film, rated = resin.release_film, resin.release_film_max
    if film is not None and rated and film >= rated:
        errors.append(f"the release film has reached the {rated} lifts it is rated for")
    return {
        "print_state": state,
        **_job_fields(state, info),
        "filename": info.get("Filename") or None,
        "job_id": info.get("TaskId") or None,
        "resin": resin,
        "errors": tuple(errors),
    }


# ------------------------------------------------------------------ discovery


def _is_resin(reply: Mapping[str, Any]) -> bool:
    return classify_sdcp(sdcp_identity(reply)) == "resin"


def _discovery_result(reply: Mapping[str, Any], sender: str) -> DiscoveryResult:
    """Return what a resin printer said of itself; its address, else the datagram's sender."""
    data = sdcp_identity(reply)
    board = str(data.get("MainboardID") or "")
    model = str(data.get("MachineName") or data.get("Name") or "") or None
    return DiscoveryResult(
        host=str(data.get("MainboardIP") or "") or sender,
        protocol=ProtocolId.SDCP_RESIN,
        candidates=[ProtocolId.SDCP_RESIN],
        mainboard_id=board or None,
        firmware=str(data.get("FirmwareVersion") or "") or None,
        model=model,
        model_id=model,
        evidence=[f"answered the UDP discovery probe on port {SDCP_DISCOVERY_PORT} as a resin printer"],
        prefill={"serial": board} if valid_serial(board) else {},
    )


class SdcpResinProtocol(SdcpSession):
    """An Elegoo resin printer on SDCP V3. It reads the printer and sends it no command yet."""

    commands = SESSION_COMMAND

    def __init__(
        self,
        config: PrinterConfig,
        session: aiohttp.ClientSession,
        *,
        granted: frozenset[Capability],
        unsafe: tuple[UnsafeFeature, ...] = (),
        models: tuple[ModelProfile, ...] = (),
    ) -> None:
        """Create the adapter for one printer."""
        super().__init__(config, session, granted=granted, unsafe=unsafe, models=models)
        self._attributes_at: float | None = None

    # ------------------------------------------------------------ config flow

    @classmethod
    async def async_discover(cls, timeout: float) -> list[DiscoveryResult]:
        """Broadcast the SDCP discovery literal and keep the first resin printer's reply."""
        answer = await async_probe_udp(
            SDCP_DISCOVERY_PROBE, ("255.255.255.255", SDCP_DISCOVERY_PORT), timeout, accept=_is_resin
        )
        return [_discovery_result(*answer)] if answer is not None else []

    @classmethod
    async def async_identify(cls, host: str, timeout: float) -> DiscoveryResult | None:
        """Ask one host the discovery literal; only a resin printer's reply counts."""
        answer = await async_probe_udp(
            SDCP_DISCOVERY_PROBE, (host, SDCP_DISCOVERY_PORT), timeout, accept=_is_resin
        )
        return _discovery_result(*answer) if answer is not None else None

    @classmethod
    async def async_prepare_config(cls, config: PrinterConfig) -> PrinterConfig:
        """Learn the mainboard id, which every request is addressed to, and refuse
        an FDM printer or an SDCP V1 one, which speaks MQTT instead of this socket."""
        answer = await async_probe_udp(
            SDCP_DISCOVERY_PROBE, (config.host, SDCP_DISCOVERY_PORT), DISCOVERY_TIMEOUT
        )
        if answer is None:
            if config.serial:
                return config
            raise ConfigError(
                f"{config.host} did not answer the SDCP discovery request on UDP port "
                f"{SDCP_DISCOVERY_PORT}. Check the address, or enter the printer's mainboard id"
            )
        identity = sdcp_identity(answer[0])
        model = str(identity.get("MachineName") or identity.get("Name") or "") or "an SDCP printer"
        if classify_sdcp(identity) == "fdm":
            raise ConfigError(
                f"{config.host} answers as {model}, which is not a resin printer. "
                "Add it under its own protocol instead"
            )
        version = str(identity.get("ProtocolVersion") or "")
        if version.upper().startswith("V1"):
            raise ConfigError(
                f"{config.host} answers as {model} on SDCP {version}, which works over MQTT. "
                "Only SDCP V3 resin printers, such as the Saturn 4 Ultra, are supported"
            )
        board = str(identity.get("MainboardID") or "").strip()
        if config.serial and board and board != config.serial:
            raise ConfigError(
                f"the printer at {config.host} reports mainboard id {board}, not {config.serial}"
            )
        if valid_serial(board):
            return config.with_overrides({"serial": board})
        if config.serial:
            return config
        raise ConfigError(
            f"{config.host} did not say its mainboard id. Enter the printer's mainboard id"
        )

    # ------------------------------------------------------------------- hooks

    @property
    def _address(self) -> str:
        """Return the mainboard id requests go to: the entry's, else the one the printer said."""
        return self.config.serial or self._mainboard_id

    def _frame(self, cmd: int, data: Mapping[str, Any] | None) -> tuple[str, str]:
        """Return one request in the envelope the Saturn was read with."""
        return build_resin_frame(self._address, cmd, data)

    def _heartbeat_payload(self) -> str:
        """Ask for the status: the Saturn never answers the text ``ping``, and a status keeps it."""
        return self._frame(self.commands["status"], None)[1]

    async def _async_greet(self) -> None:
        """Greet as every SDCP session does, which reads the attributes once."""
        await super()._async_greet()
        self._attributes_at = time.monotonic()

    async def _check_identity(self) -> None:
        """Close the socket and refuse an FDM printer, or another resin printer than the
        entry's; then name the model, which narrows what the entry grants."""
        model = str(self._attributes.get("MachineName") or "") or None
        board = str(self._attributes.get("MainboardID") or "")
        if classify_sdcp(self._attributes, self._status) == "fdm":
            await self._async_close_socket()
            raise WrongPrinterError(
                f"{self.config.host} answers as {model or 'an SDCP printer'}, which is not a "
                "resin printer, and this entry drives an Elegoo resin printer",
                model=model,
                translation_key="not_a_resin_printer",
            )
        if self.config.serial and board and board != self.config.serial:
            await self._async_close_socket()
            raise WrongPrinterError(
                f"{self.config.host} answers with mainboard id {board}, and this entry "
                f"was set up for {self.config.serial}",
                model=model,
                translation_key="other_resin_printer",
            )
        if model is not None and model != self.model_id:
            self._set_model_id(model)

    # -------------------------------------------------------------------- read

    async def _async_refresh_attributes(self) -> None:
        """Ask for the attributes again and wait briefly for their push."""
        self._attributes_event.clear()
        await self._async_request(self.commands["attributes"])
        with suppress(TimeoutError):
            await asyncio.wait_for(self._attributes_event.wait(), timeout=sdcp.PUSH_TIMEOUT)
        self._attributes_at = time.monotonic()

    async def _async_read(self) -> PrinterSnapshot:
        """Return one snapshot, reconnecting when the socket is gone.
        The Saturn pushes nothing unasked, so the status and attributes are asked for."""
        if not self._ready:
            await self.async_setup()
        now = time.monotonic()
        if self._attributes_at is None or now - self._attributes_at > ATTRIBUTES_INTERVAL:
            await self._async_refresh_attributes()
        stale = self._status_at is not None and now - self._status_at > sdcp.STATUS_MAX_AGE
        if not self._status or stale:
            await self._async_refresh_status()
        await self._check_identity()

        return PrinterSnapshot(
            protocol=ProtocolId.SDCP_RESIN,
            connected=self._connected,
            capabilities=self.capabilities,
            **parse_resin(self._status, self._attributes),
            model=str(self._attributes.get("MachineName") or "") or None,
            firmware=str(self._attributes.get("FirmwareVersion") or "") or None,
            serial=self._mainboard_id or self.config.serial or None,
        )

    # ---------------------------------------------------------------- commands

    async def _async_dispatch(self, command: Command, params: Mapping[str, Any]) -> None:
        """Refuse every command: none is granted until it is measured on a printer."""
        raise ProtocolError(f"the resin adapter cannot send {command.value} yet")

    async def async_upload_file(
        self, name: str, stream: AsyncIterator[bytes], *, size: int | None = None
    ) -> FileEntry:
        """Refuse an upload, which this adapter does not make yet."""
        raise ProtocolError("the resin adapter does not upload files yet")

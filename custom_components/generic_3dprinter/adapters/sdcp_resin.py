"""Elegoo resin printers over SDCP V3, such as the Saturn 4 Ultra 16K. It shares the socket with
the Centauri Carbon adapter, and sends only the reads, the job controls, files and the camera."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing, asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import urlsplit

import aiohttp

from ..const import Capability, Command, ModelProfile, PrintState, ProtocolId, ResinPhase, UnsafeFeature
from ..discovery import DISCOVERY_TIMEOUT, DiscoveryResult, async_probe_udp
from ..models import FileEntry, Percent, PrinterSnapshot, ResinState, Seconds, Temps
from ..protocols import (
    DEFAULT_BLOCK_RULES,
    BlockRule,
    CommandBlockedError,
    CommandRejectedError,
    ConfigError,
    PrinterConfig,
    ProtocolError,
    UnreachableError,
    WrongPrinterError,
    valid_serial,
)
from . import sdcp
from .rtsp_frames import RtspFrames, Spawn, async_spawn
from .sdcp import (
    RESIN_FILE_TYPES,
    SDCP_DISCOVERY_PORT,
    SDCP_DISCOVERY_PROBE,
    SESSION_COMMAND,
    UPLOAD_PATH,
    SdcpSession,
    _celsius,
    _integer,
    _number,
    classify_sdcp,
    sdcp_identity,
    status_flags,
)

_LOGGER = logging.getLogger(__name__)

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

#: The ``CurrentStatus`` code a printer holds while it takes a file and checks it.
TRANSFERRING: Final = 2

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

#: The codes a resin printer is sent: the reads, the job controls, delete and the video switch
#: (spec en.md:370-535, 883-921); never 387, 403 or the Centauri's 324.
RESIN_COMMAND: Final[Mapping[str, int]] = MappingProxyType(
    {
        **SESSION_COMMAND,
        "start_print": 128,
        "pause": 129,
        "stop": 130,
        "resume": 131,
        "delete_files": 259,
        "video": 386,
    }
)

#: The job controls, each sent with an empty ``Data`` as the spec shows.
CONTROL_COMMANDS: Final[Mapping[Command, str]] = MappingProxyType(
    {Command.PAUSE: "pause", Command.RESUME: "resume", Command.STOP: "stop"}
)

#: The ``Ack`` codes start print and the video switch give their own meaning (spec en.md:410-423, 912).
RESIN_ACK_MESSAGES: Final[Mapping[str, Mapping[int, str]]] = MappingProxyType(
    {
        "start_print": MappingProxyType(
            {
                3: "the file failed its MD5 check",
                4: "the file could not be read",
                5: "the file's resolution does not match the printer",
                6: "the file's format is not one the printer knows",
                7: "the file was sliced for another printer model",
            }
        ),
        "video": MappingProxyType(
            {
                1: "every video stream the printer allows is in use",
                2: "the printer has no camera",
            }
        ),
    }
)

#: Where a file named without a folder is kept, as the file list names it.
LOCAL_FOLDER: Final = "/local"

#: The job types a Saturn 4 Ultra 16K names in ``SupportFileType``, until the attributes say.
RESIN_SUFFIXES: Final = (".ctb", ".goo")

#: Seconds an upload is given to leave ``CurrentStatus`` 2; a Saturn took about 9 s for 13.5 MB.
UPLOAD_CHECK_TIMEOUT: Final = 60.0
#: Seconds between two status reads while the printer checks an uploaded file.
UPLOAD_CHECK_INTERVAL: Final = 1.0

#: Seconds between two camera opens, so a retry or a still cannot stack RTSP sessions.
VIDEO_SPACING: Final = 10.0
#: The camera is not opened before this layer: the first layers are the ones a hang would ruin.
VIDEO_MIN_LAYER: Final = 3
#: Seconds a still is handed out again instead of opening an RTSP session: past a dashboard's
#: 10 s, short of the 30 s poll a timelapse frame comes with, so its frames do not repeat.
STILL_MAX_AGE: Final = 20.0
#: Seconds teardown waits for a camera open in progress to give up before it closes anyway.
VIDEO_CLOSE_WAIT: Final = 15.0

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


@dataclass(slots=True)
class _CameraUse:
    """One camera open: teardown sets ``closing``; ``settled`` once ffmpeg runs or the open failed."""

    closing: bool = False
    settled: asyncio.Event = field(default_factory=asyncio.Event)


def _check_open(use: _CameraUse) -> None:
    """Refuse to go on with a camera open that a teardown overtook."""
    if use.closing:
        raise UnreachableError("the printer's connection is closing")


def _closed_during(sent: FileEntry) -> UnreachableError:
    return UnreachableError(
        f"the printer's connection closed while it checked {sent.name}; list its files to see "
        "whether it kept it"
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
    # A 16K on V1.5.6 reports 1 for the minutes its vat warms to the target before a print.
    vat, target = _number(status.get("TempOfTank")), _number(status.get("TempTargetTank"))
    if phase is ResinPhase.HOMING and vat is not None and target is not None and vat < target:
        phase = ResinPhase.PREHEATING
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


def machine_busy(snapshot: PrinterSnapshot) -> bool:
    """Return ``True`` unless the machine itself says it is idle.
    It is busy for about 15 s after an upload, while its print state already reads idle."""
    return snapshot.resin is None or snapshot.resin.machine != "idle"


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
    """An Elegoo resin printer on SDCP V3: it reads, starts (opted in), pauses, resumes and stops,
    uploads and deletes files, and reads the camera (opted in)."""

    commands = RESIN_COMMAND
    #: The ffmpeg the camera runs; the camera platform sets Home Assistant's own.
    ffmpeg_binary: str | None = "ffmpeg"
    command_ack_messages = RESIN_ACK_MESSAGES
    #: Only the spec's reply counts, so a page on the port is never read as a stored file.
    upload_reply_required = True
    #: A job starts only on an idle machine, whatever the print state says; then the defaults.
    block_rules = (
        BlockRule(frozenset({Command.START_PRINT}), when=machine_busy, reason="the printer is not idle"),
        *DEFAULT_BLOCK_RULES,
    )

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
        #: Starts ffmpeg; a test hands in a fake.
        self.spawn_ffmpeg: Spawn = async_spawn
        #: Held from the camera's open to its release; a second open is refused, not queued.
        self._video_lock = asyncio.Lock()
        self._video_reader: RtspFrames | None = None
        self._video_release: asyncio.Task[None] | None = None
        #: Whether 386 ``Enable: 1`` went out with no ``Enable: 0`` after it.
        self._video_on = False
        self._video_opened_at: float | None = None
        #: The camera use holding the lock, and whether a teardown is running.
        self._video_use: _CameraUse | None = None
        self._closing = False
        #: Teardowns so far: a wait that spans one stops rather than reopen the socket.
        self._teardowns = 0
        #: The last frame, from a still or the stream, and when it came.
        self._still: bytes | None = None
        self._still_at: float | None = None
        #: The live stream's latest frame; ``None`` while no stream has produced one.
        self._stream_frame: bytes | None = None

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
    def upload_url(self) -> str:
        """Return where files are posted: the socket's own port (spec en.md:1017-1021)."""
        return f"{self.config.scheme}://{self.config.host}:{self.ws_port}{UPLOAD_PATH}"

    @property
    def upload_suffixes(self) -> tuple[str, ...]:
        """Return the resin job types the printer names in ``SupportFileType``, as suffixes;
        a type no resin printer is known to print, such as G-code, is never one."""
        named = self._attributes.get("SupportFileType")
        named = [named] if isinstance(named, str) else named
        if not isinstance(named, list):
            return RESIN_SUFFIXES
        types = (str(item).strip().lower() for item in named)
        suffixes = tuple(dict.fromkeys(f".{item}" for item in types if item in RESIN_FILE_TYPES))
        return suffixes or RESIN_SUFFIXES

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
        camera = _integer(self._attributes.get("CameraStatus")) == 1

        return PrinterSnapshot(
            protocol=ProtocolId.SDCP_RESIN,
            connected=self._connected,
            capabilities=self.capabilities,
            **parse_resin(self._status, self._attributes),
            model=str(self._attributes.get("MachineName") or "") or None,
            firmware=str(self._attributes.get("FirmwareVersion") or "") or None,
            serial=self._mainboard_id or self.config.serial or None,
            camera=camera and Capability.CAMERA in self.capabilities,
        )

    # ---------------------------------------------------------------- commands

    async def _async_dispatch(self, command: Command, params: Mapping[str, Any]) -> None:
        """Send pause, resume or stop with an empty ``Data``, or start or delete one file."""
        if command is Command.START_PRINT:
            await self._async_start_print(str(params["filename"]))
            return
        if command is Command.DELETE_FILE:
            await self._async_delete_file(str(params["filename"]))
            return
        name = CONTROL_COMMANDS.get(command)
        if name is None:
            raise ProtocolError(f"the resin adapter cannot send {command.value}")
        await self._async_send_checked(name, {})

    async def _async_start_print(self, filename: str) -> None:
        """Start one file of ``/local`` with the spec's two fields, never the Centauri's six,
        once a fresh status says the machine is idle: the last poll may be a minute old."""
        folder, _, name = filename.rpartition("/")
        if folder not in ("", LOCAL_FOLDER) or not name.lower().endswith(self.upload_suffixes):
            raise CommandRejectedError(
                f"the printer starts only its own {', '.join(self.upload_suffixes)} files in "
                f"{LOCAL_FOLDER}, not {filename}",
                reason="the file is not one the printer starts",
            )
        await self._async_refresh_status()
        flags = status_flags(self._status.get("CurrentStatus"))
        if not self._status_event.is_set() or machine_for(flags) != "idle":
            raise CommandBlockedError(Command.START_PRINT, "the printer is not idle")
        await self._async_send_checked("start_print", {"Filename": name, "StartLayer": 0})

    async def _async_delete_file(self, filename: str) -> None:
        """Delete one file, then list its folder: the printer acks 0 even for a path it lacks,
        so only the list says whether the file went."""
        path = filename if filename.startswith("/") else f"{LOCAL_FOLDER}/{filename}"
        response = await self._async_send_checked("delete_files", {"FileList": [path], "FolderList": []})
        failed = (response or {}).get("ErrData")
        folder = path.rsplit("/", 1)[0] or "/"
        listed = await self._async_list_to_confirm(f"the delete of {path}", folder)
        kept = any(item.path == path for item in listed)
        if kept or (isinstance(failed, list) and path in failed):
            raise CommandRejectedError(
                f"the printer did not delete {path}", reason="the file is still on the printer"
            )

    async def _async_list_to_confirm(self, what: str, folder: str) -> list[FileEntry]:
        """List the folder a delete or an upload touches, and raise when no list arrives,
        since a missing list would read as a folder without the file."""
        self._file_list_event.clear()
        self._file_list = []
        response = await self._async_request(self.commands["file_list"], {"Url": folder})
        ack = _integer((response or {}).get("Ack"))
        if not ack and not self._file_list_event.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(self._file_list_event.wait(), timeout=sdcp.PUSH_TIMEOUT)
        if ack or not self._file_list_event.is_set():
            raise CommandRejectedError(
                f"could not confirm {what}",
                code=ack or None,
                reason="the printer did not list the folder",
            )
        return list(self._file_list)

    async def async_upload_file(
        self, name: str, stream: AsyncIterator[bytes], *, size: int | None = None
    ) -> FileEntry:
        """Post one file in chunks, once it is a type the printer prints, the machine is idle and
        no file has its name, and return it only once the printer lists it after checking it."""
        teardowns = self._teardowns
        filename = name.rsplit("/", 1)[-1]
        suffixes = self.upload_suffixes
        if not name.lower().endswith(suffixes):
            raise CommandRejectedError(
                f"the printer prints only {', '.join(suffixes)} files, not {filename}",
                reason="the printer does not print this type of file",
            )
        if machine_busy(await self.async_read()):
            raise CommandRejectedError(
                "the printer takes a file only while it is idle", reason="the printer is not idle"
            )
        if self._teardowns != teardowns:
            # The read can outlive an unload; the list would reopen the socket.
            raise UnreachableError(f"the printer's connection closed before {filename} was sent")
        what = f"that {filename} is not already on the printer"
        listed = await self._async_list_to_confirm(what, LOCAL_FOLDER)
        if any(item.path == f"{LOCAL_FOLDER}/{filename}" for item in listed):
            # The list gives no size or date, so a discarded re-upload would read as kept.
            raise CommandRejectedError(
                f"a file named {filename} is already on the printer; delete or rename it first",
                reason="a file with that name is already on the printer",
            )
        sent = await super().async_upload_file(name, stream, size=size)
        return await self._async_confirm_upload(sent, teardowns)

    async def _async_confirm_upload(self, sent: FileEntry, teardowns: int) -> FileEntry:
        """Wait for the printer to leave ``CurrentStatus`` 2, then find the file in its folder:
        a Saturn answers every upload as stored and silently discards a file it cannot print."""
        try:
            return await self._async_await_kept(sent, teardowns)
        except (asyncio.CancelledError, ProtocolError):
            # A teardown fails or cancels the pending read; a cancel of this task goes on as one.
            task = asyncio.current_task()
            if self._teardowns == teardowns or (task is not None and task.cancelling()):
                raise
            raise _closed_during(sent) from None

    async def _async_await_kept(self, sent: FileEntry, teardowns: int) -> FileEntry:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + UPLOAD_CHECK_TIMEOUT
        while True:
            if self._teardowns != teardowns:
                raise _closed_during(sent)
            await self._async_refresh_status()
            flags = status_flags(self._status.get("CurrentStatus"))
            if self._status_event.is_set() and flags and TRANSFERRING not in flags:
                break
            if loop.time() >= deadline:
                raise UnreachableError(
                    f"the printer was still taking {sent.name} {UPLOAD_CHECK_TIMEOUT:g} s after the "
                    "upload; list its files to see whether it kept it"
                )
            await asyncio.sleep(min(UPLOAD_CHECK_INTERVAL, max(deadline - loop.time(), 0.0)))
        if self._teardowns != teardowns:
            raise _closed_during(sent)
        folder = sent.path.rsplit("/", 1)[0] or "/"
        listed = await self._async_list_to_confirm(f"the upload of {sent.path}", folder)
        entry = next((item for item in listed if item.path == sent.path), None)
        if entry is None:
            raise CommandRejectedError(
                f"the printer discarded {sent.name}: it checks each file it is sent and keeps only "
                "one it can print",
                reason="the printer discarded the file",
            )
        # The list names a file by its path and gives no size.
        return replace(entry, name=sent.name, size=sent.size if entry.size is None else entry.size)

    # ----------------------------------------------------------------- camera

    async def async_camera_frame(self) -> bytes:
        """Return one JPEG: the live stream's latest, else a still younger than
        :data:`STILL_MAX_AGE`, else a new one from the camera opened for that frame alone."""
        self._require_camera()
        if self._stream_frame is not None:
            return self._stream_frame
        if self._still is not None and self._still_at is not None:
            if time.monotonic() - self._still_at < STILL_MAX_AGE:
                return self._still
        async with self._async_video(still=True) as reader, aclosing(reader.async_frames()) as frames:
            async for frame in frames:
                self._keep_frame(frame)
                return frame
        raise UnreachableError("the camera ended before a whole frame arrived")

    async def async_camera_stream(self) -> AsyncIterator[bytes]:
        """Yield JPEG frames while the caller reads; the camera hub is the only caller."""
        async with self._async_video(still=False) as reader, aclosing(reader.async_frames()) as frames:
            try:
                async for frame in frames:
                    self._keep_frame(frame)
                    self._stream_frame = frame
                    yield frame
            finally:
                self._stream_frame = None

    def _keep_frame(self, frame: bytes) -> None:
        self._still, self._still_at = frame, time.monotonic()

    def _require_camera(self) -> None:
        if Capability.CAMERA not in self.capabilities:
            raise UnreachableError("the camera is off until it is allowed in the printer's options")

    @asynccontextmanager
    async def _async_video(self, *, still: bool) -> AsyncIterator[RtspFrames]:
        """Hold the camera from 386 to its release; refuse at once while it is held,
        since each wait would end in another of the printer's two RTSP sessions."""
        self._require_camera()
        if self._closing:
            raise UnreachableError("the printer's connection is closing")
        if self._video_lock.locked() or (self._video_release is not None and not self._video_release.done()):
            raise UnreachableError("the camera is already open")
        async with self._video_lock:
            use = self._video_use = _CameraUse()
            try:
                try:
                    url = await self._async_video_url(use)
                    _check_open(use)
                    binary = self.ffmpeg_binary or "ffmpeg"
                    self._video_reader = RtspFrames(binary, url, still=still, spawn=self.spawn_ffmpeg)
                    await self._video_reader.async_start()
                finally:
                    use.settled.set()
                yield self._video_reader
            finally:
                # A use a teardown overtook never reconnects the socket to switch the video off.
                await self._async_release_video(reconnect=not use.closing)
                self._video_use = None

    async def _async_video_url(self, use: _CameraUse) -> str:
        """Switch the video on and return its address, only when the printer is free for it:
        a camera, both sessions free by a fresh command 1, not early in a print, not just opened."""
        opened = self._video_opened_at
        if opened is not None and time.monotonic() - opened < VIDEO_SPACING:
            raise UnreachableError(f"the camera was opened less than {VIDEO_SPACING:g} s ago")
        await self._async_refresh_attributes()
        _check_open(use)
        await self._async_refresh_status()
        _check_open(use)
        if not self._attributes_event.is_set() or not self._status_event.is_set():
            raise UnreachableError("the printer did not say whether its camera is free")
        if _integer(self._attributes.get("CameraStatus")) != 1:
            raise UnreachableError("the printer reports no camera")
        used = _integer(self._attributes.get("NumberOfVideoStreamConnected"))
        if used != 0:
            count = "an unknown number" if used is None else str(used)
            raise UnreachableError(f"{count} of the printer's video streams are in use")
        info = self._status.get("PrintInfo")
        layer = _integer(info.get("CurrentLayer")) if isinstance(info, Mapping) else None
        if 1 in status_flags(self._status.get("CurrentStatus")) and (layer or 0) < VIDEO_MIN_LAYER:
            raise UnreachableError(f"the camera is not opened before layer {VIDEO_MIN_LAYER} of a print")

        self._video_opened_at = time.monotonic()
        self._video_on = True
        response = await self._async_send_checked("video", {"Enable": 1})
        reported = str((response or {}).get("VideoUrl") or "").strip()
        parts = urlsplit(reported if "://" in reported else f"rtsp://{reported}")
        if not reported or parts.scheme.lower() != "rtsp" or not parts.hostname:
            raise UnreachableError("the printer switched its video on but gave no RTSP address")
        # The printer names itself as it sees itself; the entry's address is the one that reaches it.
        port = f":{parts.port}" if parts.port else ""
        return parts._replace(netloc=f"{self.config.host}{port}").geturl()

    async def _async_release_video(self, *, reconnect: bool = True) -> None:
        """Stop ffmpeg, then send 386 ``Enable: 0``, and wait for both even when cancelled:
        a release cut short leaves a session the printer keeps until a power cycle."""
        task = self._video_release
        if task is None or task.done():
            task = self._video_release = asyncio.create_task(self._async_stop_video(reconnect=reconnect))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _async_stop_video(self, *, reconnect: bool) -> None:
        reader, self._video_reader = self._video_reader, None
        if reader is not None:
            await reader.async_stop()
        if not self._video_on or not (reconnect or self._ready):
            return
        self._video_on = False
        try:
            await self._async_send_checked("video", {"Enable": 0})
        except ProtocolError as err:
            _LOGGER.debug("%s: the video was not switched off: %s", self.config.name, err)

    async def async_teardown(self) -> None:
        """Let a camera open in progress give up, release the camera over the open socket,
        then close it. Idempotent."""
        self._closing = True
        self._teardowns += 1
        try:
            use = self._video_use
            if use is not None:
                use.closing = True
                with suppress(TimeoutError):
                    await asyncio.wait_for(use.settled.wait(), VIDEO_CLOSE_WAIT)
            await self._async_release_video(reconnect=False)
            await super().async_teardown()
        finally:
            self._closing = False

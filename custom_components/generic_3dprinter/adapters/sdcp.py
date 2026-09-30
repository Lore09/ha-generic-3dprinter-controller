"""Elegoo SDCP adapter, for the Centauri Carbon and its siblings.

SDCP is a JSON protocol over a WebSocket on port 3030. A request carries a command
number inside a fixed envelope, and the printer answers on a topic string derived
from its own mainboard id rather than on a numeric topic id.

Verified by this project against a live Centauri Carbon on firmware V1.4.49:
commands 0, 1, 258, 320 and 324 answered, status and attribute frames arrived
continuously, and the camera streamed multipart JPEG from port 3031.

A CANVAS multi-material unit is read with command 324, the one the printer's own
page sends, and only while the status says a unit is connected. The page offers
no command to load, unload or edit a slot, so neither does this adapter: an
unknown command is exactly what crashes the printer.

The socket is kept the way the printer's own page keeps it, because the printer
was measured to expect exactly that. A client that sends nothing is closed after
60 seconds, while one that sends the text ``ping`` every 30 seconds, as the page
does, stays connected; and the printer pushes its status only when asked, with
command 0 or with that ``ping``. So this adapter pings, asks for the status on
every connection instead of trusting what it knew before a power cycle, and
switches the camera on with command 386 before it reads it, as the page does
before it shows the camera.

The safety rule this adapter exists to respect. An unrecognised SDCP command code,
or a recognised code sent with an unexpected payload shape, can crash the
printer's ``app`` daemon. On this hardware ``app`` is the whole host firmware
including the motion stack, so a crash destroys an active print and needs a power
cycle at the wall. Only the codes in :data:`COMMAND` are ever sent, and start print
is additionally withheld behind the registry's opt-in because this adapter builds
that payload itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from types import MappingProxyType
from typing import Any, Final

import aiohttp

from ..const import (
    ModelProfile,
    Capability,
    Command,
    LightChannel,
    PrintState,
    ProtocolId,
    UnsafeFeature,
)
from ..mjpeg import JPEG_EOI, JPEG_SOI, jpeg_frames  # noqa: F401 - re-exported
from ..discovery import DISCOVERY_TIMEOUT, DiscoveryResult, async_probe_udp
from ..models import (
    Axis,
    Celsius,
    Fans,
    FileEntry,
    FilamentSystem,
    Millimetres,
    Percent,
    PrinterSnapshot,
    Seconds,
    Temps,
)
from ..protocols import (
    CommandRejectedError,
    ConfigError,
    PrinterConfig,
    Protocol,
    ProtocolError,
    UnreachableError,
    WrongPrinterError,
)
from .elegoo_canvas import parse_canvas

_LOGGER = logging.getLogger(__name__)

WS_PATH: Final = "/websocket"
DEFAULT_WS_PORT: Final = 3030
DEFAULT_CAMERA_PORT: Final = 3031
CAMERA_PATH: Final = "/video"
UPLOAD_PATH: Final = "/uploadFile/upload"

UPLOAD_CHUNK: Final = 1024 * 1024
MAX_FRAME_BYTES: Final = 8 * 1024 * 1024
STREAM_CHUNK: Final = 65536

#: How long to wait for the acknowledgement frame of a command.
ACK_TIMEOUT: Final = 10.0

#: How long to wait for a pushed payload after an acknowledgement. The printer
#: sends the file list and the attributes in a frame of their own, after the ack.
PUSH_TIMEOUT: Final = 5.0

#: How often the CANVAS is read while nothing else says it changed. A spool is
#: swapped by hand, and the printer pushes nothing when that happens.
CANVAS_INTERVAL: Final = 30.0

#: The printer's own page sends this text this often. Measured on a live printer: a
#: client that sends nothing is closed after 60 seconds, one that pings stays, and
#: every ping is answered with a status push.
HEARTBEAT_INTERVAL: Final = 30.0
HEARTBEAT_TEXT: Final = "ping"
#: A socket that has carried nothing for this long is dead, however open it looks.
#: The printer pushes its attributes every few seconds and answers every ping, so
#: silence means the printer went away without closing the connection.
SILENCE_TIMEOUT: Final = 75.0
#: A status older than this is asked for again before a reading is reported.
STATUS_MAX_AGE: Final = 20.0

#: The only command codes this adapter will ever put on the wire.
COMMAND: Final[Mapping[str, int]] = MappingProxyType(
    {
        "status": 0,
        "attributes": 1,
        "start_print": 128,
        "pause": 129,
        "stop": 130,
        "resume": 131,
        "file_list": 258,
        "delete_files": 259,
        "history": 320,
        "canvas": 324,
        "video": 386,
        "set_params": 403,
    }
)

#: The read-only codes every SDCP session sends, whatever the printer prints with.
SESSION_COMMAND: Final[Mapping[str, int]] = MappingProxyType(
    {"status": 0, "attributes": 1, "file_list": 258}
)

#: ``PrintInfo.Status`` is a code table, not a bit field. Values 2 to 4 and 23 to
#: 26 are resin-only and are mapped to a neutral state rather than to a guess.
STATE_BY_PRINT_STATUS: Final[Mapping[int, PrintState]] = MappingProxyType(
    {
        0: PrintState.IDLE,
        1: PrintState.PREPARING,
        5: PrintState.PAUSED,
        6: PrintState.PAUSED,
        7: PrintState.CANCELLED,
        8: PrintState.CANCELLED,
        9: PrintState.FINISHED,
        10: PrintState.PREPARING,
        11: PrintState.PREPARING,
        12: PrintState.PRINTING,
        13: PrintState.PRINTING,
        14: PrintState.ERROR,
        15: PrintState.PREPARING,
        16: PrintState.PREPARING,
        17: PrintState.PREPARING,
        18: PrintState.PREPARING,
    }
)

ACK_MESSAGES: Final[Mapping[int, str]] = MappingProxyType(
    {
        1: "the printer is busy",
        2: "the file was not found",
        3: "the command was rejected",
    }
)

#: The three temperature fields of the overloaded 403 command. The command code is
#: shared but the payload key selects the heater, so the choice stays one layer
#: down and the normalised vocabulary remains a statement about printers.
SET_HOTEND_FIELD: Final = "TempTargetNozzle"
SET_BED_FIELD: Final = "TempTargetHotbed"
SET_CHAMBER_FIELD: Final = "TempTargetBox"

_FAN_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {"model": "ModelFan", "auxiliary": "AuxiliaryFan", "chamber": "BoxFan"}
)

#: What each kind of SDCP printer slices for, in ``SupportFileType``. A Saturn 4
#: Ultra 16K lists CTB and GOO, a Centauri Carbon lists gcode.
RESIN_FILE_TYPES: Final = frozenset({"ctb", "cbddlp", "goo", "prz"})
FDM_FILE_TYPES: Final = frozenset({"gcode"})
#: Attributes and status fields only a resin printer reports, and only an FDM one.
RESIN_ATTRIBUTES: Final = ("ReleaseFilmMax", "Resolution")
RESIN_STATUS_KEYS: Final = ("TempOfUVLED", "ReleaseFilm", "TempOfTank")
FDM_STATUS_KEYS: Final = ("TempOfNozzle", "TempOfHotbed")
#: ``MachineName`` words, the last resort. ``XYZsize`` is never used: both report the same.
RESIN_NAMES: Final = ("saturn", "mars")
FDM_NAMES: Final = ("centauri", "neptune")


# --------------------------------------------------------------------- parsing


def build_frame(
    mainboard_id: str, cmd: int, data: Mapping[str, Any] | None = None
) -> tuple[str, str]:
    """Return ``(request_id, frame)`` for one SDCP request.

    ``From`` is 1, matching Elegoo's own SDK. The printer tolerates an empty ``Id``
    and no ``Topic`` on a request, which is what this integration sends.
    """
    request_id = uuid.uuid4().hex
    frame = json.dumps(
        {
            "Id": "",
            "Data": {
                "Cmd": cmd,
                "Data": dict(data or {}),
                "RequestID": request_id,
                "MainboardID": mainboard_id,
                "TimeStamp": int(time.time() * 1000),
                "From": 1,
            },
        }
    )
    return request_id, frame


def load_frame(raw: str) -> Mapping[str, Any] | None:
    """Decode one frame, tolerating the decimal length prefix some firmware adds."""
    text = raw.lstrip()
    if text[:1].isdigit():
        brace = text.find("{")
        if brace > 0:
            text = text[brace:]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        _LOGGER.debug("ignoring an undecodable SDCP frame: %s", raw[:200])
        return None
    return payload if isinstance(payload, Mapping) else None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def status_flags(value: Any) -> tuple[int, ...]:
    """Return ``CurrentStatus`` as a tuple, tolerating both a list and a scalar.

    The printer's own web UI reads this array, where 1 means a job is active, so it
    is kept as the cross-check for print states the code table does not cover.
    """
    if isinstance(value, (list, tuple)):
        return tuple(
            item for item in (_integer(entry) for entry in value) if item is not None
        )
    single = _integer(value)
    return (single,) if single is not None else ()


def parse_coord(value: Any) -> Axis | None:
    """Parse the printer's ``"x,y,z"`` string. The field name is misspelled upstream."""
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != 3:
        return None
    numbers = [_number(part) for part in parts]
    if any(number is None for number in numbers):
        return None
    return Axis(
        x=Millimetres(numbers[0]),  # type: ignore[arg-type]
        y=Millimetres(numbers[1]),  # type: ignore[arg-type]
        z=Millimetres(numbers[2]),  # type: ignore[arg-type]
    )


def state_for(print_status: int | None, flags: tuple[int, ...]) -> PrintState:
    """Return the normalised print state for one status object."""
    if print_status is not None and print_status in STATE_BY_PRINT_STATUS:
        return STATE_BY_PRINT_STATUS[print_status]
    if 1 in flags:
        return PrintState.PRINTING
    if print_status == 0:
        return PrintState.IDLE
    return PrintState.UNKNOWN


def parse_status(status: Mapping[str, Any]) -> dict[str, Any]:
    """Return the normalised scalar view of one SDCP status object.

    A pure function of the payload, so the mapping can be tested without a socket.
    """
    print_info = status.get("PrintInfo") or {}
    fans = status.get("CurrentFanSpeed") or {}
    lights = status.get("LightStatus") or {}

    return {
        "print_status": _integer(print_info.get("Status")),
        "current_layer": _integer(print_info.get("CurrentLayer")),
        "total_layers": _integer(print_info.get("TotalLayer")),
        "progress": _number(print_info.get("Progress")),
        "filename": print_info.get("Filename") or None,
        "job_id": print_info.get("TaskId") or None,
        "speed_factor": _number(print_info.get("PrintSpeedPct")),
        "hotend_current": _number(status.get("TempOfNozzle")),
        "hotend_target": _number(status.get("TempTargetNozzle")),
        "bed_current": _number(status.get("TempOfHotbed")),
        "bed_target": _number(status.get("TempTargetHotbed")),
        "chamber_current": _number(status.get("TempOfBox")),
        "chamber_target": _number(status.get("TempTargetBox")),
        "fan_model": _number(fans.get("ModelFan")),
        "fan_auxiliary": _number(fans.get("AuxiliaryFan")),
        "fan_chamber": _number(fans.get("BoxFan")),
        "elapsed": _number(print_info.get("CurrentTicks")),
        "total_ticks": _number(print_info.get("TotalTicks")),
        "chamber_light": _integer(lights.get("SecondLight")),
        "position": parse_coord(status.get("CurrenCoord")),
        "current_status": status_flags(status.get("CurrentStatus")),
        # 1 while a CANVAS is attached. Absent on firmware that predates it.
        "canvas_connected": _integer(status.get("AmsConnectStatus")),
    }


def sdcp_identity(reply: Mapping[str, Any]) -> dict[str, Any]:
    """Return the attributes of a discovery reply, with its ``Status`` when it has one.
    The spec's reply is a flat ``Data``; a Saturn is reported to nest ``Attributes`` and ``Status``."""
    data = reply.get("Data")
    data = data if isinstance(data, Mapping) else reply
    attributes = data.get("Attributes")
    identity = dict(attributes if isinstance(attributes, Mapping) else data)
    identity.pop("Status", None)
    if isinstance(data.get("Status"), Mapping):
        identity["Status"] = dict(data["Status"])
    return identity


def classify_sdcp(
    attributes: Mapping[str, Any], status: Mapping[str, Any] | None = None
) -> str | None:
    """Return ``"resin"``, ``"fdm"`` or ``None`` for an SDCP printer, from what it said.
    The file types decide first, then resin-only attributes, then the status, then the name."""
    if status is None and isinstance(attributes.get("Status"), Mapping):
        status = attributes["Status"]
    types = attributes.get("SupportFileType")
    types = [types] if isinstance(types, str) else types
    if isinstance(types, Sequence):
        lowered = {str(item).strip().lower() for item in types}
        if lowered & RESIN_FILE_TYPES:
            return "resin"
        if lowered & FDM_FILE_TYPES:
            return "fdm"
    devices = attributes.get("DevicesStatus")
    if any(key in attributes for key in RESIN_ATTRIBUTES) or (
        isinstance(devices, Mapping) and "LCDStatus" in devices
    ):
        return "resin"
    if isinstance(status, Mapping):
        if any(key in status for key in RESIN_STATUS_KEYS):
            return "resin"
        if any(key in status for key in FDM_STATUS_KEYS):
            return "fdm"
    name = str(attributes.get("MachineName") or "").lower()
    if any(word in name for word in RESIN_NAMES):
        return "resin"
    if any(word in name for word in FDM_NAMES):
        return "fdm"
    return None


def parse_file_list(payload: Any) -> list[FileEntry]:
    """Normalise the ``FileList`` array into :class:`FileEntry` values."""
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        return []
    entries: list[FileEntry] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        if not name or item.get("type") == 0:
            continue
        entries.append(FileEntry(name=name, path=name, size=_integer(item.get("FileSize"))))
    return entries


def _celsius(value: Any) -> Celsius | None:
    number = _number(value)
    return Celsius(number) if number is not None else None


def _percent(value: Any) -> Percent | None:
    number = _number(value)
    if number is None:
        return None
    return Percent(min(max(number, 0.0), 100.0))


# ------------------------------------------------------------------ discovery

#: The SDCP discovery literal, broadcast to UDP port 3000. A Centauri Carbon
#: answers with its mainboard id, address, model and firmware.
SDCP_DISCOVERY_PORT: Final = 3000
SDCP_DISCOVERY_PROBE: Final = b"M99999"


def _not_resin(reply: Mapping[str, Any]) -> bool:
    return classify_sdcp(sdcp_identity(reply)) != "resin"


async def async_discover_sdcp(timeout: float = 3.0) -> DiscoveryResult | None:
    """Broadcast the Elegoo discovery probe and parse the first reply from an FDM printer."""
    answer = await async_probe_udp(
        SDCP_DISCOVERY_PROBE, ("255.255.255.255", SDCP_DISCOVERY_PORT), timeout, accept=_not_resin
    )
    if answer is None:
        return None
    reply, sender = answer
    data = sdcp_identity(reply)
    return DiscoveryResult(
        host=str(data.get("MainboardIP") or "") or sender,
        protocol=ProtocolId.SDCP_CC1,
        candidates=[ProtocolId.SDCP_CC1],
        mainboard_id=str(data.get("MainboardID") or "") or None,
        firmware=str(data.get("FirmwareVersion") or "") or None,
        model=str(data.get("MachineName") or data.get("Name") or "") or None,
        evidence=["answered the UDP discovery probe on port 3000"],
    )


class SdcpSession(Protocol):
    """One SDCP WebSocket session: the socket, its heartbeat, requests and the files.
    It knows no kind of printer; a subclass names the ``commands`` it may send, and reads."""

    #: The command codes this session may put on the wire, by name.
    commands: Mapping[str, int] = SESSION_COMMAND
    #: What a nonzero ``Ack`` means, by code.
    ack_messages: Mapping[int, str] = ACK_MESSAGES

    def __init__(
        self,
        config: PrinterConfig,
        session: aiohttp.ClientSession,
        *,
        granted: frozenset[Capability],
        unsafe: tuple[UnsafeFeature, ...] = (),
        models: tuple[ModelProfile, ...] = (),
    ) -> None:
        """Create the session for one printer."""
        super().__init__(config, session, granted=granted, unsafe=unsafe, models=models)
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[Mapping[str, Any]]] = {}
        self._status: dict[str, Any] = {}
        self._attributes: dict[str, Any] = {}
        self._file_list: list[FileEntry] = []
        self._status_event = asyncio.Event()
        self._attributes_event = asyncio.Event()
        self._file_list_event = asyncio.Event()
        self._mainboard_id = ""
        self._send_lock = asyncio.Lock()
        #: Held while a socket opens and its printer is checked; nothing else is sent meanwhile.
        self._setup_lock = asyncio.Lock()
        self._identity_checked = False
        self._heartbeat: asyncio.Task[None] | None = None
        #: When the printer last sent anything, and when it last sent its status.
        self._last_frame_at = 0.0
        self._status_at: float | None = None

    # --------------------------------------------------------------- addresses

    @property
    def ws_port(self) -> int:
        """Return the SDCP port."""
        return self.config.port or DEFAULT_WS_PORT

    @property
    def ws_url(self) -> str:
        """Return the SDCP WebSocket URL."""
        scheme = "wss" if self.config.tls else "ws"
        return f"{scheme}://{self.config.host}:{self.ws_port}{WS_PATH}"

    @property
    def attributes(self) -> Mapping[str, Any]:
        """Return the last attributes payload the printer pushed."""
        return self._attributes

    @property
    def upload_url(self) -> str:
        """Return the address files are posted to."""
        return f"{self.config.scheme}://{self.config.host}{UPLOAD_PATH}"

    # ------------------------------------------------------------------- hooks

    def _frame(self, cmd: int, data: Mapping[str, Any] | None) -> tuple[str, str]:
        """Return ``(request_id, frame)`` for one request in this printer's envelope."""
        return build_frame(self._mainboard_id, cmd, data)

    def _heartbeat_payload(self) -> str:
        """Return the text the heartbeat sends."""
        return HEARTBEAT_TEXT

    async def _check_identity(self) -> None:
        """Refuse a printer this session must not drive. Every printer passes here."""

    def _forget_socket_state(self) -> None:
        """Drop what was known only over the socket that just opened or ended."""

    # --------------------------------------------------------------- lifecycle

    async def async_setup(self) -> None:
        """Open the control socket and request the machine attributes once.

        Idempotent for a healthy socket, and a full reconnect for a dead one. The
        distinction matters: when a printer loses power its socket object survives
        with ``closed`` set, so treating a present socket as a live one makes every
        later reconnect a silent no-op and the integration sticks at offline until
        somebody reloads it by hand.
        """
        async with self._setup_lock:
            if self._ready:
                return
            await self._async_open()

    async def _async_open(self) -> None:
        await self._reset_socket()

        self._attributes_event.clear()
        try:
            # aiohttp's own ping is off: the printer expects the page's text "ping",
            # which the heartbeat task below sends.
            self._ws = await self._session.ws_connect(
                self.ws_url,
                heartbeat=None,
                max_msg_size=16 * 1024 * 1024,
            )
        except aiohttp.WSServerHandshakeError as err:
            self._ws = None
            if err.status == 500:
                raise UnreachableError(
                    "the printer refused the connection. It allows five SDCP clients "
                    "at once and all of them are in use"
                ) from err
            raise UnreachableError(
                f"the printer refused the WebSocket handshake with HTTP {err.status}"
            ) from err
        except aiohttp.ClientError as err:
            self._ws = None
            raise UnreachableError(f"cannot reach {self.config.redacted_url}: {err}") from err
        except TimeoutError as err:
            self._ws = None
            raise UnreachableError(f"timeout contacting {self.config.redacted_url}") from err

        self._last_frame_at = time.monotonic()
        self._forget_socket_state()
        self._reader = asyncio.create_task(self._async_read_frames())
        self._heartbeat = asyncio.create_task(self._async_heartbeat())
        try:
            await self._async_greet()
        except BaseException:
            # A socket whose printer was never checked must not carry the next request.
            await self._async_close_socket()
            raise
        self._identity_checked = True
        _LOGGER.debug(
            "%s: SDCP ready, mainboard %s, firmware %s",
            self.config.name,
            self._mainboard_id or "(unknown)",
            self._attributes.get("FirmwareVersion"),
        )

    async def _async_greet(self) -> None:
        """Read the attributes, then the status, checking the printer after each."""
        await self._async_request(self.commands["attributes"], greeting=True)
        with suppress(TimeoutError):
            await asyncio.wait_for(self._attributes_event.wait(), timeout=PUSH_TIMEOUT)
        await self._check_identity()

        self._mainboard_id = str(self._attributes.get("MainboardID") or "")
        # Like the printer's own page, ask for the status on every connection. The
        # printer pushes it only when asked, so a status cached from before a power
        # cycle would otherwise be reported until somebody opened that page.
        await self._async_refresh_status(greeting=True)
        await self._check_identity()

    @property
    def _connected(self) -> bool:
        """Return ``True`` only while a live socket and a live reader are both held.

        A socket the printer has not spoken on for :data:`SILENCE_TIMEOUT` is not
        live, however open it looks: a printer that loses power leaves exactly that
        behind.
        """
        if self._ws is None or self._ws.closed:
            return False
        if self._last_frame_at and time.monotonic() - self._last_frame_at > SILENCE_TIMEOUT:
            return False
        return self._reader is not None and not self._reader.done()

    @property
    def _ready(self) -> bool:
        """Return ``True`` for a live socket whose printer has been checked."""
        return self._connected and self._identity_checked

    async def _reset_socket(self) -> None:
        """Drop the socket, its reader, its heartbeat and any request waiting on it.

        The old socket is closed, not left behind. The printer holds five client
        slots, and a socket nobody reads keeps one of them until the printer times
        it out a minute later. The status it delivered is dropped too, so what was
        true before a power cycle is never reported as true after it.
        """
        await self._async_close_socket()
        self._fail_pending(UnreachableError("the SDCP socket is not open"))
        self._status = {}
        self._status_at = None

    async def _async_close_socket(self) -> None:
        """Stop the heartbeat and the reader and close the socket. Idempotent."""
        self._identity_checked = False
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is not None and not heartbeat.done() and heartbeat is not asyncio.current_task():
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader

        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            with suppress(aiohttp.ClientError, ConnectionResetError):
                await ws.close()

    async def async_teardown(self) -> None:
        """Close the socket and stop the reader and the heartbeat. Idempotent."""
        await self._async_close_socket()

        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    # -------------------------------------------------------------- frame pump

    async def _async_read_frames(self) -> None:
        """Route every frame the printer sends until the socket closes."""
        ws = self._ws
        if ws is None:
            return
        try:
            async for message in ws:
                if message.type is aiohttp.WSMsgType.TEXT:
                    self._last_frame_at = time.monotonic()
                    self._handle_frame(message.data)
                elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, ConnectionResetError) as err:
            _LOGGER.debug("%s: SDCP socket ended: %s", self.config.name, err)
        finally:
            self._forget_socket_state()
            self._fail_pending(UnreachableError("the SDCP socket closed"))

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def _handle_frame(self, raw: str) -> None:
        """Dispatch one text frame by its topic.

        Topics are string-routed and carry the mainboard id, so the second path
        segment is what identifies the frame kind.
        """
        payload = load_frame(raw)
        if payload is None:
            return
        topic = str(payload.get("Topic") or "")
        parts = topic.split("/")
        kind = parts[1] if len(parts) > 1 else ""

        if kind == "response":
            self._resolve_response(payload)
        elif kind == "status":
            status = payload.get("Status")
            if isinstance(status, Mapping):
                self._status = dict(status)
                self._status_at = time.monotonic()
                self._status_event.set()
        elif kind == "attributes":
            attributes = payload.get("Attributes")
            if isinstance(attributes, Mapping):
                self._attributes = dict(attributes)
                self._attributes_event.set()
        elif kind == "error":
            _LOGGER.warning(
                "%s: the printer reported an SDCP error: %s", self.config.name, raw[:400]
            )

    def _resolve_response(self, payload: Mapping[str, Any]) -> None:
        """Complete the request whose ``RequestID`` this frame carries.

        The envelope nests the payload one level deeper than the routing fields:
        ``Data.RequestID`` identifies the request while ``Data.Data`` is the body.
        Reading the body off the wrong level is why a file list looks empty while
        the frame that carried it was received.
        """
        outer = payload.get("Data") or {}
        body = outer.get("Data")
        data: Mapping[str, Any] = body if isinstance(body, Mapping) else outer
        request_id = str(outer.get("RequestID") or "")

        # The file list shares its response frame with the acknowledgement and
        # carries no request id of its own, so it is captured here rather than
        # through the pending-request table.
        if "FileList" in data:
            self._file_list = parse_file_list(data.get("FileList"))
            self._file_list_event.set()

        if not request_id:
            return
        future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(data)

    async def _async_heartbeat(self) -> None:
        """Send ``ping`` as the printer's own page does, and notice a dead socket.

        The printer closes a client that has not pinged for 60 seconds and answers
        every ping with its status, so this keeps the socket and the status alive
        together. A socket that has gone silent is closed here, which ends the
        reader and fails what was waiting on it, so the next read reconnects.
        """
        ws = self._ws
        while ws is not None and not ws.closed:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if ws.closed:
                break
            if time.monotonic() - self._last_frame_at > SILENCE_TIMEOUT:
                _LOGGER.debug("%s: the printer went silent, closing the socket", self.config.name)
                with suppress(aiohttp.ClientError, ConnectionResetError):
                    await ws.close()
                break
            try:
                async with self._send_lock:
                    await ws.send_str(self._heartbeat_payload())
            except (aiohttp.ClientError, ConnectionResetError) as err:
                _LOGGER.debug("%s: the heartbeat could not be sent: %s", self.config.name, err)
                with suppress(aiohttp.ClientError, ConnectionResetError):
                    await ws.close()
                break

    async def _async_refresh_status(self, *, greeting: bool = False) -> None:
        """Ask for the status, as the page does, and wait briefly for the push."""
        self._status_event.clear()
        await self._async_request(self.commands["status"], greeting=greeting)
        with suppress(TimeoutError):
            await asyncio.wait_for(self._status_event.wait(), timeout=PUSH_TIMEOUT)

    # --------------------------------------------------------------- requests

    async def _async_request(
        self,
        cmd: int,
        data: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        greeting: bool = False,
    ) -> Mapping[str, Any] | None:
        """Send one request and wait for its acknowledgement frame.

        The socket is health-checked before every request rather than only when it is
        missing, because a printer that has been switched off leaves a socket object
        behind whose ``closed`` flag is the only sign that it is gone.
        """
        # Only the greet sends to a printer that has not been checked yet.
        if not greeting:
            if not self._ready:
                await self.async_setup()
            # The attributes or the status can arrive after the greet let the printer through.
            await self._check_identity()
        ws = self._ws
        if ws is None or ws.closed:
            raise UnreachableError("the SDCP socket is not open")

        timeout = ACK_TIMEOUT if timeout is None else timeout
        request_id, frame = self._frame(cmd, data)
        future: asyncio.Future[Mapping[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        try:
            async with self._send_lock:
                await ws.send_str(frame)
        except (aiohttp.ClientError, ConnectionResetError) as err:
            self._pending.pop(request_id, None)
            raise UnreachableError(
                f"cannot send to {self.config.redacted_url}: {err}"
            ) from err

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending.pop(request_id, None)
            # An unanswered request means the socket is no longer usable, even though
            # it still looks open. Drop it so the next attempt reconnects instead of
            # sending into the void forever.
            self._health_check()
            raise ProtocolError(
                f"the printer did not answer SDCP command {cmd} within {timeout:g}s"
            ) from None

    def _health_check(self) -> None:
        """Forget the socket when its reader has stopped or a request timed out."""
        if self._reader is not None and self._reader.done():
            self._ws = None
            self._reader = None

    async def _async_send_checked(self, name: str, data: Mapping[str, Any]) -> None:
        """Send a command and raise when the printer refuses it."""
        response = await self._async_request(self.commands[name], data)
        ack = _integer((response or {}).get("Ack"))
        if ack is not None and ack != 0:
            raise CommandRejectedError(
                f"the printer refused {name}: {self.ack_messages.get(ack, 'unrecognised reason')}",
                code=ack,
                reason=self.ack_messages.get(ack),
            )

    # ------------------------------------------------------------------ files

    async def async_list_files(self) -> Sequence[FileEntry]:
        """Return the files on the printer's internal storage."""
        self._file_list_event.clear()
        self._file_list = []
        response = await self._async_request(self.commands["file_list"], {"Url": "/local"})

        # Observed on hardware: the ack and the list are separate frames, so the
        # ack frame alone carries no files.
        if "FileList" in (response or {}):
            self._file_list = parse_file_list((response or {}).get("FileList"))
        elif not self._file_list:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._file_list_event.wait(), timeout=PUSH_TIMEOUT)
        return list(self._file_list)

    async def async_upload_file(
        self, name: str, stream: AsyncIterator[bytes], *, size: int | None = None
    ) -> FileEntry:
        """Upload a file in 1 MiB chunks over HTTP.

        The upload shares neither the port nor the socket of the control channel, so
        it cannot trip the unverified-command crash path.
        """
        payload = bytearray()
        async for chunk in stream:
            payload.extend(chunk)
        body = bytes(payload)
        total = len(body)
        filename = name.rsplit("/", 1)[-1]
        digest = hashlib.md5(body).hexdigest()  # noqa: S324 - required by the printer's protocol
        transfer_id = uuid.uuid4().hex
        url = self.upload_url

        offset = 0
        while True:
            chunk = body[offset : offset + UPLOAD_CHUNK]
            form = aiohttp.FormData()
            form.add_field("Check", "1")
            form.add_field("S-File-MD5", digest)
            form.add_field("Offset", str(offset))
            form.add_field("Uuid", transfer_id)
            form.add_field("TotalSize", str(total))
            form.add_field(
                "File", chunk, filename=filename, content_type="application/octet-stream"
            )

            try:
                async with self._session.post(
                    url, data=form, timeout=aiohttp.ClientTimeout(total=180)
                ) as response:
                    result = await _response_json(response)
            except aiohttp.ClientError as err:
                raise UnreachableError(f"the upload failed: {err}") from err

            if result is not None and str(result.get("code")) not in ("000000", "None"):
                raise CommandRejectedError(
                    f"the printer refused the upload: {result.get('messages')}",
                    code=result.get("code"),
                )

            offset += UPLOAD_CHUNK
            if offset >= total:
                break

        return FileEntry(name=filename, path=f"/local/{filename}", size=total)


class SdcpProtocol(SdcpSession):
    """SDCP over a WebSocket, with the MJPEG camera on its own HTTP port."""

    commands = COMMAND

    @classmethod
    async def async_discover(cls, timeout: float) -> list[DiscoveryResult]:
        """Broadcast the SDCP discovery literal, as the printer's own tools do."""
        found = await async_discover_sdcp(timeout)
        return [found] if found is not None else []

    @classmethod
    async def async_prepare_config(cls, config: PrinterConfig) -> PrinterConfig:
        """Refuse an address whose discovery reply is a resin printer's.
        A printer that does not answer the probe is accepted, as before."""
        answer = await async_probe_udp(
            SDCP_DISCOVERY_PROBE, (config.host, SDCP_DISCOVERY_PORT), DISCOVERY_TIMEOUT
        )
        if answer is None:
            return config
        identity = sdcp_identity(answer[0])
        if classify_sdcp(identity) == "resin":
            raise ConfigError(_wrong_printer_message(config.host, identity.get("MachineName")))
        return config

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
        self._canvas: FilamentSystem | None = None
        self._canvas_at: float | None = None
        self._canvas_print_status: int | None = None
        #: Whether command 386 switched the camera on over the current socket.
        self._video_enabled = False

    # --------------------------------------------------------------- addresses

    @property
    def camera_port(self) -> int:
        """Return the camera port, defaulting to the one this hardware uses."""
        return self.config.camera_port or DEFAULT_CAMERA_PORT

    @property
    def camera_url(self) -> str:
        """Return the MJPEG camera URL."""
        return f"http://{self.config.host}:{self.camera_port}{CAMERA_PATH}"

    @property
    def web_ui_url(self) -> str:
        """Return the printer's own web UI address."""
        return f"http://{self.config.host}/"

    def _forget_socket_state(self) -> None:
        """Ask for the camera again over the next socket."""
        # A printer that went away comes back with its camera off.
        self._video_enabled = False

    async def _check_identity(self) -> None:
        """Close the socket and refuse a resin printer, which must never get 403 or 324.
        A printer that has not said what it is yet is let through, as before."""
        if classify_sdcp(self._attributes, self._status) != "resin":
            return
        model = str(self._attributes.get("MachineName") or "") or None
        await self._async_close_socket()
        raise WrongPrinterError(_wrong_printer_message(self.config.host, model), model=model)

    # ------------------------------------------------------------------- read

    async def _async_read(self) -> PrinterSnapshot:
        """Return one snapshot, reconnecting when the session is gone.

        Connecting is part of reading, not something the caller has to remember. A
        printer that has been switched off leaves a closed socket behind and its last
        status still cached, so a read that only consulted that cache would report
        "offline" for ever and never try to reach the printer again. That is exactly
        the state a user has to reload the config entry to escape.

        The printer's push scheduler is documented as wedging on some firmware while
        a one-shot request keeps working, so the poll path is the reliable one and
        the push is the optimisation.
        """
        if not self._ready:
            await self.async_setup()

        stale = self._status_at is not None and time.monotonic() - self._status_at > STATUS_MAX_AGE
        if not self._status or stale:
            await self._async_refresh_status()
        await self._check_identity()

        parsed = parse_status(self._status)
        flags = parsed["current_status"]

        elapsed = parsed["elapsed"]
        total = parsed["total_ticks"]
        # The printer exposes no remaining-time field; the two tick counters are
        # the whole of it.
        remaining = max(total - elapsed, 0.0) if elapsed is not None and total is not None else None

        # ``lights`` holds the lights that are on, so a known-but-off light is absent.
        light_on = bool(parsed["chamber_light"])
        filament = await self._async_read_canvas(parsed)
        return PrinterSnapshot(
            protocol=ProtocolId.SDCP_CC1,
            connected=self._connected,
            capabilities=self.capabilities,
            print_state=state_for(parsed["print_status"], flags),
            progress=_percent(parsed["progress"]),
            current_layer=parsed["current_layer"],
            total_layers=parsed["total_layers"],
            remaining=Seconds(remaining) if remaining is not None else None,
            elapsed=Seconds(elapsed) if elapsed is not None else None,
            filename=parsed["filename"],
            job_id=parsed["job_id"],
            speed_factor=_percent(parsed["speed_factor"]),
            hotend=Temps(
                current=_celsius(parsed["hotend_current"]),
                target=_celsius(parsed["hotend_target"]),
            ),
            bed=Temps(
                current=_celsius(parsed["bed_current"]),
                target=_celsius(parsed["bed_target"]),
            ),
            chamber=Temps(
                current=_celsius(parsed["chamber_current"]),
                target=_celsius(parsed["chamber_target"]),
            ),
            fans=Fans(
                model=_percent(parsed["fan_model"]),
                auxiliary=_percent(parsed["fan_auxiliary"]),
                chamber=_percent(parsed["fan_chamber"]),
            ),
            position=parsed["position"],
            lights=frozenset({LightChannel.CHAMBER}) if light_on else frozenset(),
            camera=self._attributes.get("CameraStatus") == 1,
            filament=filament,
            model=str(self._attributes.get("MachineName") or "") or None,
            firmware=str(self._attributes.get("FirmwareVersion") or "") or None,
            serial=self._mainboard_id or None,
        )

    async def _async_read_canvas(self, parsed: Mapping[str, Any]) -> FilamentSystem | None:
        """Return the CANVAS, reading it again when it may have changed.

        Only a status that says a unit is connected leads to command 324. A firmware
        without the field is never sent a command it may not know. The unit is read
        again after :data:`CANVAS_INTERVAL`, and at once when the print status
        changes, because starting or ending a job changes the slot in use. A read
        that fails keeps the last answer rather than blanking the slots.
        """
        if Capability.FILAMENT_SLOTS not in self.capabilities or parsed["canvas_connected"] != 1:
            self._canvas = None
            self._canvas_at = None
            return None
        now = time.monotonic()
        fresh = self._canvas_at is not None and now - self._canvas_at < CANVAS_INTERVAL
        if fresh and parsed["print_status"] == self._canvas_print_status:
            return self._canvas
        try:
            response = await self._async_request(self.commands["canvas"])
        except UnreachableError:
            raise
        except ProtocolError as err:
            _LOGGER.debug("%s: the CANVAS did not answer: %s", self.config.name, err)
            return self._canvas
        self._canvas = parse_canvas(response)
        self._canvas_at = now
        self._canvas_print_status = parsed["print_status"]
        return self._canvas

    # --------------------------------------------------------------- commands

    async def _async_dispatch(self, command: Command, params: Mapping[str, Any]) -> None:
        """Translate one normalised command into an SDCP frame."""
        handler = _DISPATCH.get(command)
        if handler is None:
            raise ProtocolError(f"the SDCP adapter cannot dispatch {command.value}")
        await handler(self, params)

    async def _async_start_print(self, params: Mapping[str, Any]) -> None:
        """Send the six-field start-print payload, which is the whole risk surface."""
        if 1 in status_flags(self._status.get("CurrentStatus")):
            raise ProtocolError(
                "refusing to start a print: the printer reports a job already in progress"
            )
        await self._async_send_checked(
            "start_print",
            {
                "Filename": str(params["filename"]).rsplit("/", 1)[-1],
                "StartLayer": 0,
                "Calibration_switch": 1,
                "PrintPlatformType": 0,
                "Tlp_Switch": 0,
                "slot_map": [],
            },
        )

    async def _async_pause(self, _params: Mapping[str, Any]) -> None:
        await self._async_send_checked("pause", {})

    async def _async_resume(self, _params: Mapping[str, Any]) -> None:
        await self._async_send_checked("resume", {})

    async def _async_stop(self, _params: Mapping[str, Any]) -> None:
        await self._async_send_checked("stop", {})

    async def _async_set_hotend_temp(self, params: Mapping[str, Any]) -> None:
        await self.set_printer_params({SET_HOTEND_FIELD: int(params["value"])})

    async def _async_set_bed_temp(self, params: Mapping[str, Any]) -> None:
        await self.set_printer_params({SET_BED_FIELD: int(params["value"])})

    async def _async_set_chamber_temp(self, params: Mapping[str, Any]) -> None:
        await self.set_printer_params({SET_CHAMBER_FIELD: int(params["value"])})

    async def set_printer_params(self, payload: Mapping[str, Any]) -> None:
        """Send one variant of the overloaded 403 print-parameter command."""
        await self._async_send_checked("set_params", payload)

    async def _async_set_fan(self, params: Mapping[str, Any]) -> None:
        channel = str(params.get("channel", "model"))
        key = _FAN_KEYS.get(channel)
        if key is None:
            raise ProtocolError(
                f"this printer has no {channel} fan; it exposes model, auxiliary and chamber"
            )
        await self._async_send_checked(
            "set_params", {"TargetFanSpeed": {key: int(params["value"])}}
        )

    async def _async_set_speed(self, params: Mapping[str, Any]) -> None:
        await self._async_send_checked("set_params", {"PrintSpeedPct": int(params["value"])})

    async def _async_set_light(self, params: Mapping[str, Any]) -> None:
        await self._async_send_checked(
            "set_params", {"LightStatus": {"SecondLight": 1 if params["on"] else 0}}
        )

    async def _async_delete_file(self, params: Mapping[str, Any]) -> None:
        name = str(params["filename"])
        if not name.startswith("/"):
            name = f"/local/{name}"
        await self._async_send_checked("delete_files", {"FileList": [name]})

    # ----------------------------------------------------------------- camera

    async def _async_enable_video(self) -> None:
        """Switch the camera stream on, as the printer's own page does before showing it.

        After a power cycle the camera stayed dark until the printer's page had been
        opened, and the page sends command 386 with ``Enable`` set before it shows
        the camera. It is sent once per connection, and again after the camera
        failed. A camera that is already on answers it the same way.
        """
        if self._video_enabled:
            return
        try:
            response = await self._async_request(self.commands["video"], {"Enable": 1})
        except ProtocolError as err:
            _LOGGER.debug("%s: the camera was not switched on: %s", self.config.name, err)
            return
        ack = _integer((response or {}).get("Ack"))
        if ack not in (None, 0):
            _LOGGER.debug("%s: the printer refused to switch the camera on: %s", self.config.name, ack)
            return
        self._video_enabled = True

    async def async_camera_frame(self) -> bytes:
        """Return one JPEG frame read from the printer's MJPEG stream."""
        await self._async_enable_video()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=10)
        try:
            async with self._session.get(self.camera_url, timeout=timeout) as response:
                if response.status >= 400:
                    self._video_enabled = False
                    raise UnreachableError(f"the camera answered HTTP {response.status}")
                buffer = bytearray()
                async for chunk in response.content.iter_chunked(STREAM_CHUNK):
                    buffer.extend(chunk)
                    frames = jpeg_frames(buffer, first_only=True)
                    if frames:
                        return frames[0]
                    if len(buffer) > MAX_FRAME_BYTES:
                        raise UnreachableError("the camera frame exceeded the size cap")
        except aiohttp.ClientError as err:
            self._video_enabled = False
            raise UnreachableError(f"cannot reach the camera: {err}") from err
        except TimeoutError as err:
            self._video_enabled = False
            raise UnreachableError("the camera did not deliver a frame") from err
        raise UnreachableError("the camera stream ended before a complete frame arrived")

    async def async_camera_stream(self) -> AsyncIterator[bytes]:
        """Yield JPEG frames from the printer's camera until the caller stops.

        One caller holds one upstream connection. The printer's camera server has a
        very small pool of connection slots and leaks them on disconnect, so frames
        are fanned out downstream from here rather than per viewer.
        """
        await self._async_enable_video()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=15)
        try:
            async with self._session.get(self.camera_url, timeout=timeout) as response:
                if response.status >= 400:
                    raise UnreachableError(f"the camera answered HTTP {response.status}")
                buffer = bytearray()
                async for chunk in response.content.iter_chunked(STREAM_CHUNK):
                    buffer.extend(chunk)
                    for frame in jpeg_frames(buffer):
                        yield frame
        except (aiohttp.ClientError, TimeoutError, UnreachableError):
            # Ask for the camera again next time: the printer may have restarted.
            self._video_enabled = False
            raise


def _wrong_printer_message(host: str, model: Any) -> str:
    return (
        f"{host} answers as {model or 'an SDCP printer'}, a resin printer, and this entry "
        "drives a Centauri Carbon. Add it as an Elegoo resin printer instead"
    )


async def _response_json(response: aiohttp.ClientResponse) -> Mapping[str, Any] | None:
    """Decode a JSON body, returning ``None`` for an empty or non-JSON answer."""
    try:
        text = await response.text()
    except (aiohttp.ClientError, UnicodeDecodeError):
        return None
    if not text.strip():
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, Mapping) else None


#: ``Command`` to handler. The 403 payload variant is chosen by the command and
#: stays one layer down, so the normalised vocabulary remains a statement about
#: printers rather than about Elegoo.
_DISPATCH: Final[Mapping[Command, Any]] = MappingProxyType(
    {
        Command.START_PRINT: SdcpProtocol._async_start_print,
        Command.PAUSE: SdcpProtocol._async_pause,
        Command.RESUME: SdcpProtocol._async_resume,
        Command.STOP: SdcpProtocol._async_stop,
        Command.SET_FAN_SPEED: SdcpProtocol._async_set_fan,
        Command.SET_SPEED: SdcpProtocol._async_set_speed,
        Command.SET_LIGHT: SdcpProtocol._async_set_light,
        Command.DELETE_FILE: SdcpProtocol._async_delete_file,
        Command.SET_HOTEND_TEMP: SdcpProtocol._async_set_hotend_temp,
        Command.SET_BED_TEMP: SdcpProtocol._async_set_bed_temp,
        Command.SET_CHAMBER_TEMP: SdcpProtocol._async_set_chamber_temp,
    }
)

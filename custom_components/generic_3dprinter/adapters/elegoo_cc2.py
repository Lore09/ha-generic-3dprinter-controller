"""Elegoo Centauri Carbon 2 adapter: JSON requests over the printer's own MQTT broker.

The Centauri Carbon 2 does not speak SDCP. It runs an MQTT broker on port 1883, and a
client connects to it, registers a client id, and then exchanges JSON requests on
topics built from the printer's serial number:

* ``elegoo/<sn>/api_register`` carries the registration, which is answered on
  ``elegoo/<sn>/<request_id>/register_response``;
* ``elegoo/<sn>/<client_id>/api_request`` carries requests and the heartbeat, which
  are answered on ``elegoo/<sn>/<client_id>/api_response``;
* ``elegoo/<sn>/api_status`` carries status pushes. After the first full status
  they are deltas, and have to be merged into the last full status.

Sources, in order of authority: Elegoo's own elegoo-link SDK (the LAN adapter
ElegooSlicer uses), then two community projects that measured a real printer on
firmware 02.01.00.00. What this project verified on a real printer is recorded in
the registry's evidence mapping.

Three facts shape the adapter more than any other.

* The printer serves local clients only in LAN-only mode. In cloud mode its broker
  still accepts a connection and a subscription, and then nothing answers: not the
  registration, not a request, not a status push. That was measured on a live
  printer. The discovery reply's ``lan_status`` says which mode the printer is in,
  so the failure is named instead of being left as a timeout.
* The printer holds very few client slots, shared with the slicer and the phone
  app, and drops a client whose heartbeat stops. One connection is held per
  printer, and the heartbeat runs for as long as it is open.
* An unknown method is answered with error 1001 instead of crashing the printer,
  which is the Centauri Carbon's failure mode. The hazard model is therefore
  different, but the rule is the same: only methods whose payload has a source are
  sent, and starting a print stays behind an opt-in.

A CANVAS multi-material unit is read with method 2005 and driven with 2001 to load
a slot, 2002 to unload it, 2003 to record its filament and 2004 for auto-refill.
The numbers and payloads are the ones Elegoo's own page sends, as the community
elegoo-web project recorded them.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import random
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final

import aiohttp

from ..const import Capability, Command, LightChannel, PrintState, ProtocolId, UnsafeFeature
from ..discovery import DiscoveryResult, async_discover_cc2
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
from ..mjpeg import jpeg_frames
from ..mqtt_client import MqttClient, MqttError, MqttRefusedError
from ..protocols import (
    DEFAULT_BLOCK_RULES,
    AuthError,
    BlockRule,
    CommandRejectedError,
    ConfigError,
    PrinterConfig,
    Protocol,
    ProtocolError,
    UnreachableError,
    not_idle,
)
from .elegoo_canvas import (
    ACTIVITY_BY_SUB_STATUS,
    FILAMENT_PRESETS,
    edit_payload,
    parse_canvas,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_MQTT_PORT: Final = 1883
DEFAULT_CAMERA_PORT: Final = 8080
UPLOAD_PORT: Final = 80
UPLOAD_PATH: Final = "/upload"
CAMERA_PATH: Final = "/?action=stream"

MQTT_USERNAME: Final = "elegoo"
#: The password the printer expects while no access code is set, and the value of
#: the upload token in that case. Both come from Elegoo's SDK.
DEFAULT_ACCESS_CODE: Final = "123456"

#: MQTT keepalive, in seconds. The application heartbeat below is sent far more
#: often, so the broker never has to act on it.
KEEPALIVE: Final = 60
CONNECT_TIMEOUT: Final = 10.0
REGISTER_TIMEOUT: Final = 5.0
DISCOVERY_TIMEOUT: Final = 3.0

#: The printer drops a client that has not sent a heartbeat for 65 seconds. The
#: same limit is applied the other way round: a printer that has not been heard
#: from for that long is treated as gone.
HEARTBEAT_INTERVAL: Final = 10.0
HEARTBEAT_TIMEOUT: Final = 65.0

ACK_TIMEOUT: Final = 10.0
#: How long a resume waits for a refusal. The printer acknowledges a resume only
#: once it has reheated and moved back into position, measured at 122 seconds on
#: firmware 02.01.00.00, so waiting for the acknowledgement would always time out.
#: A refusal, such as "not printing", comes back at once.
RESUME_REFUSAL_WINDOW: Final = 3.0

#: Requests sent back to back trip a cooldown in which the printer drops its
#: answers, so requests are spaced out.
REQUEST_GAP: Final = 0.5

#: A full status is requested this often even while deltas keep arriving, as the
#: SDK recommends, so a missed delta cannot leave a reading wrong for long.
FULL_STATUS_INTERVAL: Final = 300.0
#: Deltas carry a sequence number. This many gaps in a row mean deltas were lost.
MAX_SEQUENCE_GAPS: Final = 5
#: How often the CANVAS is read while no status push says it changed. A spool is
#: swapped by hand, and the printer does not always report that.
CANVAS_INTERVAL: Final = 60.0

UPLOAD_CHUNK: Final = 1024 * 1024
UPLOAD_TIMEOUT: Final = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60)
STREAM_CHUNK: Final = 65536
MAX_FRAME_BYTES: Final = 8 * 1024 * 1024

#: The only methods this adapter will ever send.
METHOD: Final[Mapping[str, int]] = MappingProxyType(
    {
        "attributes": 1001,
        "status": 1002,
        "start_print": 1020,
        "pause": 1021,
        "stop": 1022,
        "resume": 1023,
        "home": 1026,
        "move": 1027,
        "set_temperature": 1028,
        "set_light": 1029,
        "set_fan": 1030,
        "set_speed_mode": 1031,
        "video_stream": 1042,
        "file_list": 1044,
        "load_filament": 2001,
        "unload_filament": 2002,
        "set_filament": 2003,
        "set_auto_refill": 2004,
        "canvas": 2005,
    }
)

EVENT_STATUS: Final = 6000
EVENT_ATTRIBUTES: Final = 6008

#: Error codes from the SDK's own table, as the user should read them.
ERROR_MESSAGES: Final[Mapping[int, str]] = MappingProxyType(
    {
        109: "filament has run out",
        1000: "the access code was not accepted",
        1001: "the printer does not know this request",
        1003: "a parameter was not accepted",
        1009: "the printer is busy",
        1010: "no print is in progress",
        1012: "the print task was not found",
        1021: "the file was not found",
        1026: "the bed has not been levelled",
        9004: "the file checksum did not match",
    }
)

#: ``machine_status.status``. Everything the printer does outside a print, such as
#: loading filament or levelling, is reported as preparing: the machine is busy and
#: will not take a job, but there is no job either.
STATE_BY_MACHINE_STATUS: Final[Mapping[int, PrintState]] = MappingProxyType(
    {
        0: PrintState.UNKNOWN,  # initialising
        1: PrintState.IDLE,
        2: PrintState.PRINTING,  # refined by the sub-status below
        3: PrintState.PREPARING,  # filament
        4: PrintState.PREPARING,  # filament
        5: PrintState.PREPARING,  # auto levelling
        6: PrintState.PREPARING,  # PID calibration
        7: PrintState.PREPARING,  # resonance test
        8: PrintState.PREPARING,  # self check
        9: PrintState.PREPARING,  # firmware update
        10: PrintState.PREPARING,  # homing
        11: PrintState.PREPARING,  # file transfer
        12: PrintState.PREPARING,  # time-lapse composition
        13: PrintState.PREPARING,  # extruder maintenance
        14: PrintState.ERROR,  # emergency stop
        15: PrintState.PREPARING,  # power-loss recovery
    }
)

#: ``machine_status.sub_status`` while ``status`` is 2, printing. A code missing
#: here is still a print in progress.
STATE_BY_PRINT_SUB_STATUS: Final[Mapping[int, PrintState]] = MappingProxyType(
    {
        1045: PrintState.PREPARING,  # nozzle heating
        1096: PrintState.PREPARING,
        1405: PrintState.PREPARING,  # bed heating
        1906: PrintState.PREPARING,
        1081: PrintState.PREPARING,  # downloading the file
        1082: PrintState.PREPARING,
        1086: PrintState.PREPARING,
        2801: PrintState.PREPARING,  # homing
        2802: PrintState.PREPARING,
        2901: PrintState.PREPARING,  # levelling
        2902: PrintState.PREPARING,
        2501: PrintState.PAUSED,  # pausing
        2502: PrintState.PAUSED,
        2505: PrintState.PAUSED,
        2077: PrintState.FINISHED,
        2503: PrintState.CANCELLED,  # stopping
        2504: PrintState.CANCELLED,
    }
)

#: ``gcode_move_inf.speed_mode`` and the speed each mode stands for.
SPEED_PERCENT_BY_MODE: Final[Mapping[int, float]] = MappingProxyType(
    {0: 50.0, 1: 100.0, 2: 150.0, 3: 200.0}
)

#: Normalised fan channel to the key the printer uses, reading and writing.
FAN_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "model": "fan",
        "auxiliary": "aux_fan",
        "chamber": "box_fan",
        "hotend": "heater_fan",
        "controller": "controller_fan",
    }
)

#: The fans the printer can be told to set. The hotend and board fans are
#: reported, and run under the firmware's own control.
SETTABLE_FANS: Final = ("model", "auxiliary", "chamber")


class _NoAnswerError(ProtocolError):
    """The printer did not answer a request in time. It may still carry it out."""


# --------------------------------------------------------------------- parsing


def new_client_id() -> str:
    """Return a client id in the form Elegoo's SDK uses, ``1_PC_`` and four digits."""
    return f"1_PC_{random.randint(1000, 9999)}"  # noqa: S311 - an id, not a secret


def build_request(request_id: int, method: int, params: Mapping[str, Any] | None = None) -> bytes:
    """Return the JSON body of one request."""
    return json.dumps(
        {"id": request_id, "method": method, "params": dict(params or {})},
        separators=(",", ":"),
    ).encode()


def load_message(payload: bytes) -> Mapping[str, Any] | None:
    """Decode one message, returning ``None`` for anything that is not a JSON object."""
    try:
        message = json.loads(payload.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        _LOGGER.debug("ignoring an undecodable message: %r", payload[:200])
        return None
    return message if isinstance(message, Mapping) else None


def deep_merge(base: Mapping[str, Any], delta: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a status delta into a full status, returning a new mapping.

    Objects merge key by key, everything else is replaced. ``exception_code`` is
    replaced whole, as the SDK does, so a cleared error does not linger.
    """
    merged: dict[str, Any] = dict(base)
    for key, value in delta.items():
        current = merged.get(key)
        if key != "exception_code" and isinstance(value, Mapping) and isinstance(current, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


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


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def fan_percent(value: Any) -> float | None:
    """Convert a fan's PWM duty, 0 to 255, into a percentage."""
    duty = _number(value)
    if duty is None:
        return None
    return float(round(min(max(duty, 0.0), 255.0) * 100.0 / 255.0))


def fan_duty(percent: float) -> int:
    """Convert a percentage into the PWM duty the printer is set with."""
    return round(min(max(percent, 0.0), 100.0) * 255.0 / 100.0)


def speed_mode_for(percent: float) -> int:
    """Return the speed mode closest to ``percent``, the slower one on a tie."""
    return min(
        SPEED_PERCENT_BY_MODE,
        key=lambda mode: (abs(SPEED_PERCENT_BY_MODE[mode] - percent), mode),
    )


def state_for(status: int | None, sub_status: int | None) -> PrintState:
    """Return the normalised state for one ``machine_status``."""
    if status is None:
        return PrintState.UNKNOWN
    state = STATE_BY_MACHINE_STATUS.get(status, PrintState.UNKNOWN)
    if state is PrintState.PRINTING and sub_status is not None:
        return STATE_BY_PRINT_SUB_STATUS.get(sub_status, PrintState.PRINTING)
    return state


def parse_status(status: Mapping[str, Any]) -> dict[str, Any]:
    """Return the normalised scalar view of one merged status.

    A pure function of the payload, so the mapping is tested without a broker. The
    fallbacks for ``gcode_move``, ``tool_head`` and ``chamber`` are the field names
    older firmware used.
    """
    machine = _mapping(status.get("machine_status"))
    job = _mapping(status.get("print_status"))
    extruder = _mapping(status.get("extruder"))
    bed = _mapping(status.get("heater_bed"))
    chamber = _mapping(status.get("ztemperature_sensor") or status.get("chamber"))
    fans = _mapping(status.get("fans"))
    move = _mapping(status.get("gcode_move_inf") or status.get("gcode_move"))
    toolhead = _mapping(status.get("toolhead") or status.get("tool_head"))
    device = _mapping(status.get("external_device"))

    led = status.get("led")
    light: bool | None = None
    if isinstance(led, Mapping) and _number(led.get("status")) is not None:
        light = (_number(led.get("status")) or 0) > 0

    position: Axis | None = None
    if move:
        x, y, z = (_number(move.get(axis)) for axis in ("x", "y", "z"))
        if x is not None or y is not None or z is not None:
            position = Axis(
                x=Millimetres(x) if x is not None else None,
                y=Millimetres(y) if y is not None else None,
                z=Millimetres(z) if z is not None else None,
            )

    progress = _number(job.get("progress"))
    if progress is None:
        progress = _number(machine.get("progress"))

    homed = toolhead.get("homed_axes")
    exceptions = machine.get("exception_status")

    return {
        "status": _integer(machine.get("status")),
        "sub_status": _integer(machine.get("sub_status")),
        "exceptions": tuple(
            code for code in (_integer(item) for item in exceptions) if code is not None
        )
        if isinstance(exceptions, Sequence) and not isinstance(exceptions, str)
        else (),
        "progress": progress,
        "filename": str(job.get("filename") or "") or None,
        "job_id": str(job.get("uuid") or "") or None,
        "current_layer": _integer(job.get("current_layer")),
        "total_layers": _integer(job.get("total_layer")),
        "elapsed": _number(job.get("print_duration")),
        "remaining": _number(job.get("remaining_time_sec")),
        "hotend_current": _number(extruder.get("temperature")),
        "hotend_target": _number(extruder.get("target")),
        "bed_current": _number(bed.get("temperature")),
        "bed_target": _number(bed.get("target")),
        "chamber_current": _number(chamber.get("temperature")),
        "fans": {
            channel: fan_percent(_mapping(fans.get(key)).get("speed"))
            for channel, key in FAN_KEYS.items()
        },
        "speed_mode": _integer(move.get("speed_mode")),
        "position": position,
        "homed_axes": frozenset(str(homed).lower()) & {"x", "y", "z"}
        if isinstance(homed, str)
        else frozenset(),
        "light": light,
        "camera": device.get("camera") if isinstance(device.get("camera"), bool) else None,
    }


def parse_file_list(result: Mapping[str, Any]) -> list[FileEntry]:
    """Normalise a method 1044 result into :class:`FileEntry` values."""
    entries = result.get("file_list")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return []
    files: list[FileEntry] = []
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("filename") or "").strip()
        if not name or str(item.get("type") or "file") != "file":
            continue
        created = _number(item.get("create_time"))
        modified = None
        if created:
            with suppress(OverflowError, OSError, ValueError):
                modified = datetime.fromtimestamp(created, tz=UTC)
        files.append(
            FileEntry(name=name, path=name, size=_integer(item.get("size")), modified=modified)
        )
    return files


def _celsius(value: float | None) -> Celsius | None:
    return Celsius(value) if value is not None else None


def _percent(value: float | None) -> Percent | None:
    if value is None:
        return None
    return Percent(min(max(value, 0.0), 100.0))


def lan_only_hint(discovery: DiscoveryResult | None) -> str:
    """Explain a registration nobody answered, as precisely as discovery allows."""
    remedy = (
        "A Centauri Carbon 2 answers local clients only in LAN-only mode: turn it on "
        "at the printer under Settings, Network, LAN Only Mode"
    )
    if discovery is not None and discovery.lan_only is False:
        return f"the printer is in cloud mode. {remedy}"
    return f"the printer accepted the connection but never answered. {remedy}"


class ElegooCC2Protocol(Protocol):
    """The Centauri Carbon 2 over its own MQTT broker, with the camera on HTTP."""

    #: Moving the head, or loading, unloading or editing a CANVAS slot, collides
    #: with the firmware's own moves unless the printer is idle; levelling and
    #: homing count as busy too.
    block_rules = (
        BlockRule(
            frozenset(
                {
                    Command.HOME,
                    Command.JOG,
                    Command.LOAD_FILAMENT,
                    Command.UNLOAD_FILAMENT,
                    Command.SET_FILAMENT,
                }
            ),
            when=not_idle,
            reason="the printer is not idle",
        ),
        *DEFAULT_BLOCK_RULES,
    )

    def __init__(
        self,
        config: PrinterConfig,
        session: aiohttp.ClientSession,
        *,
        granted: frozenset[Capability],
        unsafe: tuple[UnsafeFeature, ...] = (),
    ) -> None:
        """Create the adapter for one printer."""
        super().__init__(config, session, granted=granted, unsafe=unsafe)
        self._client: MqttClient | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._serial = config.serial or ""
        self._discovery: DiscoveryResult | None = None
        self._client_id = new_client_id()
        self._request_ids = itertools.count(1)
        self._pending: dict[int, tuple[int, asyncio.Future[Mapping[str, Any]]]] = {}
        self._registration: asyncio.Future[str] | None = None
        self._send_lock = asyncio.Lock()
        self._last_sent = 0.0
        self._last_heard = 0.0
        self._status: dict[str, Any] = {}
        self._full_status_at: float | None = None
        self._last_sequence: int | None = None
        self._sequence_gaps = 0
        self._attributes: dict[str, Any] = {}
        self._video_enabled = False
        self._canvas: FilamentSystem | None = None
        self._canvas_at: float | None = None
        #: Set when a status push mentions the CANVAS, or after a filament command.
        self._canvas_stale = True
        #: Cleared when the printer says it does not know method 2005.
        self._canvas_supported = True

    # --------------------------------------------------------------- addresses

    @property
    def mqtt_port(self) -> int:
        """Return the broker port."""
        return self.config.port or DEFAULT_MQTT_PORT

    @property
    def camera_port(self) -> int:
        """Return the camera port."""
        return self.config.camera_port or DEFAULT_CAMERA_PORT

    @property
    def camera_url(self) -> str:
        """Return the MJPEG camera URL. The printer serves the stream on any path."""
        return f"http://{self.config.host}:{self.camera_port}{CAMERA_PATH}"

    @property
    def upload_url(self) -> str:
        """Return the upload endpoint."""
        return f"http://{self.config.host}:{UPLOAD_PORT}{UPLOAD_PATH}"

    @property
    def access_code(self) -> str:
        """Return the access code, or the printer's default while none is set."""
        return self.config.credentials.get("access_code") or DEFAULT_ACCESS_CODE

    @property
    def serial(self) -> str:
        """Return the serial number every topic is built from."""
        return self._serial

    @property
    def attributes(self) -> Mapping[str, Any]:
        """Return the last attributes the printer reported."""
        return self._attributes

    @property
    def client_id(self) -> str:
        """Return the client id this adapter registered."""
        return self._client_id

    def _topic(self, leaf: str) -> str:
        return f"elegoo/{self._serial}/{leaf}"

    @property
    def _request_topic(self) -> str:
        return self._topic(f"{self._client_id}/api_request")

    @property
    def _response_topic(self) -> str:
        return self._topic(f"{self._client_id}/api_response")

    @property
    def _status_topic(self) -> str:
        return self._topic("api_status")

    @property
    def _register_topic(self) -> str:
        return self._topic(f"{self._client_id}_req/register_response")

    # ----------------------------------------------------------- config flow

    @classmethod
    async def async_prepare_config(cls, config: PrinterConfig) -> PrinterConfig:
        """Learn the serial number and refuse a printer that is not in LAN-only mode.

        The discovery request is the one Elegoo's slicer sends; it is not a command.
        A printer that does not answer it can still be added when its serial number
        is entered by hand, because the probe is UDP and may be filtered between
        networks.
        """
        found = await async_discover_cc2(config.host, timeout=DISCOVERY_TIMEOUT)
        if found is None:
            if config.serial:
                return config
            raise ConfigError(
                f"{config.host} did not answer the Centauri Carbon 2 discovery request "
                "on UDP port 52700. Check the address, or enter the serial number "
                "shown on the printer under Settings, About"
            )
        if found.lan_only is False:
            raise ConfigError(
                "the printer is in cloud mode, and a Centauri Carbon 2 answers local "
                "clients only in LAN-only mode. Turn it on at the printer under "
                "Settings, Network, LAN Only Mode, then try again"
            )
        if found.access_code_set and "access_code" not in config.credentials:
            raise ConfigError(
                "the printer has an access code set. Enter the code shown under "
                "Settings, Network, LAN Only Mode"
            )
        if config.serial and found.mainboard_id and config.serial != found.mainboard_id:
            raise ConfigError(
                f"the printer at {config.host} reports serial {found.mainboard_id}, "
                f"not {config.serial}"
            )
        return config.with_overrides({"serial": found.mainboard_id})

    # --------------------------------------------------------------- lifecycle

    @property
    def _connected(self) -> bool:
        """Return ``True`` while a registered session is open and being read."""
        return (
            self._client is not None
            and not self._client.closed
            and self._heartbeat is not None
            and not self._heartbeat.done()
        )

    async def async_setup(self) -> None:
        """Connect, register, start the heartbeat and read the printer once.

        Idempotent for a live session, and a full reconnect for a dead one.
        """
        if self._connected:
            return
        await self._async_close_session()

        if not self._serial:
            self._discovery = await async_discover_cc2(self.config.host, timeout=DISCOVERY_TIMEOUT)
            if self._discovery is None or not self._discovery.mainboard_id:
                raise UnreachableError(
                    f"{self.config.host} did not answer the discovery request on UDP "
                    "port 52700, so its serial number is unknown. Enter it in the "
                    "integration options"
                )
            self._serial = self._discovery.mainboard_id

        client = MqttClient(self._on_message)
        try:
            await client.connect(
                self.config.host,
                self.mqtt_port,
                client_id=self._client_id,
                username=MQTT_USERNAME,
                password=self.access_code,
                keepalive=KEEPALIVE,
                timeout=CONNECT_TIMEOUT,
            )
        except MqttRefusedError as err:
            if err.bad_credentials:
                raise AuthError("the printer refused the access code") from err
            raise UnreachableError(str(err)) from err
        except MqttError as err:
            raise UnreachableError(f"cannot reach {self.config.redacted_url}: {err}") from err

        self._client = client
        self._last_heard = time.monotonic()
        try:
            await client.subscribe(
                [self._response_topic, self._status_topic, self._register_topic]
            )
            await self._async_register(client)
        except (MqttError, ProtocolError) as err:
            await self._async_close_session()
            if isinstance(err, ProtocolError):
                raise
            raise UnreachableError(f"the printer broke off the session: {err}") from err

        self._heartbeat = asyncio.create_task(self._async_heartbeat())
        self._video_enabled = False

        with suppress(_NoAnswerError):
            await self._async_refresh_attributes()
        with suppress(_NoAnswerError):
            await self._async_refresh_status()
        _LOGGER.debug(
            "%s: registered as %s with printer %s, firmware %s",
            self.config.name,
            self._client_id,
            self._serial,
            self._firmware(),
        )

    async def _async_register(self, client: MqttClient) -> None:
        """Register this client id and wait for the printer to accept it."""
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._registration = future
        body = json.dumps(
            {"client_id": self._client_id, "request_id": f"{self._client_id}_req"}
        ).encode()
        try:
            await client.publish(self._topic("api_register"), body)
            answer = await asyncio.wait_for(future, timeout=REGISTER_TIMEOUT)
        except TimeoutError:
            if self._discovery is None:
                self._discovery = await async_discover_cc2(
                    self.config.host, timeout=DISCOVERY_TIMEOUT
                )
            raise UnreachableError(lan_only_hint(self._discovery)) from None
        finally:
            self._registration = None

        if answer == "ok":
            return
        if "too many clients" in answer:
            raise UnreachableError(
                "the printer has no free client slot. It shares a handful between the "
                "slicer, the phone app and integrations like this one; close one of "
                "them, or wait about a minute for a dead one to time out"
            )
        raise UnreachableError(f"the printer refused the registration: {answer}")

    async def async_teardown(self) -> None:
        """Close the session and stop the heartbeat. Idempotent."""
        await self._async_close_session()

    async def _async_close_session(self) -> None:
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is not None and not heartbeat.done() and heartbeat is not asyncio.current_task():
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        client, self._client = self._client, None
        if client is not None:
            await client.close()
        self._fail_pending(UnreachableError("the session with the printer closed"))

    def _fail_pending(self, error: Exception) -> None:
        for _method, future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _async_heartbeat(self) -> None:
        """Send the printer's heartbeat and notice when it stops answering.

        The client's own connection ending is noticed here too, so a printer that
        was switched off turns into a closed session that the next read reopens.
        """
        client = self._client
        while client is not None and not client.closed:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if client.closed:
                break
            if time.monotonic() - self._last_heard > HEARTBEAT_TIMEOUT:
                _LOGGER.debug("%s: the printer stopped answering", self.config.name)
                break
            try:
                await client.publish(self._request_topic, b'{"type":"PING"}')
            except MqttError:
                break
        if client is not None:
            await client.close()
        self._fail_pending(UnreachableError("the printer stopped answering"))

    # --------------------------------------------------------------- messages

    def _on_message(self, topic: str, payload: bytes) -> None:
        """Route one message by its topic and its method."""
        self._last_heard = time.monotonic()
        message = load_message(payload)
        if message is None:
            return

        if topic == self._register_topic:
            future = self._registration
            if (
                future is not None
                and not future.done()
                and message.get("client_id") == self._client_id
            ):
                future.set_result(str(message.get("error") or "fail"))
            return

        method = _integer(message.get("method"))
        result = _mapping(message.get("result"))

        if method == EVENT_STATUS:
            self._apply_delta(_integer(message.get("id")), result)
            if "canvas_info" in result:
                self._canvas_stale = True
        elif method == METHOD["status"] and _integer(result.get("error_code")) in (0, None):
            self._apply_full_status(result)
        elif method in (EVENT_ATTRIBUTES, METHOD["attributes"]) and result:
            self._attributes = {**self._attributes, **result}

        # An answer is matched on its id and its method together, whichever topic it
        # came on. Status pushes number themselves from the same range as requests,
        # so an id alone would let a push complete an unrelated request.
        request_id = _integer(message.get("id"))
        pending = self._pending.get(request_id) if request_id is not None else None
        if pending is not None and pending[0] == method and not pending[1].done():
            pending[1].set_result(result)

    def _apply_full_status(self, result: Mapping[str, Any]) -> None:
        self._status = {key: value for key, value in result.items() if key != "error_code"}
        self._full_status_at = time.monotonic()
        self._last_sequence = None
        self._sequence_gaps = 0

    def _apply_delta(self, sequence: int | None, delta: Mapping[str, Any]) -> None:
        """Merge one delta, counting gaps in the sequence so a loss is noticed.

        A delta that arrives before any full status is dropped, as the SDK does: it
        would describe a change to a state this adapter has never seen.
        """
        if self._full_status_at is None:
            return
        if sequence is not None:
            previous = self._last_sequence
            if previous is not None and sequence not in (previous + 1, 0):
                self._sequence_gaps += 1
            else:
                self._sequence_gaps = 0
            self._last_sequence = sequence
        self._status = deep_merge(
            self._status, {key: value for key, value in delta.items() if key != "error_code"}
        )

    # --------------------------------------------------------------- requests

    async def _async_request(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        """Send one request and return the ``result`` of its answer.

        ``timeout`` defaults to :data:`ACK_TIMEOUT`, read when the request is made.
        """
        if timeout is None:
            timeout = ACK_TIMEOUT
        if not self._connected:
            await self.async_setup()
        client = self._client
        if client is None or client.closed:
            raise UnreachableError("the session with the printer is not open")

        method = METHOD[name]
        request_id = next(self._request_ids)
        future: asyncio.Future[Mapping[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (method, future)
        try:
            async with self._send_lock:
                gap = REQUEST_GAP - (time.monotonic() - self._last_sent)
                if gap > 0:
                    await asyncio.sleep(gap)
                await client.publish(self._request_topic, build_request(request_id, method, params))
                self._last_sent = time.monotonic()
            return await asyncio.wait_for(future, timeout=timeout)
        except MqttError as err:
            raise UnreachableError(f"cannot send to the printer: {err}") from err
        except TimeoutError:
            raise _NoAnswerError(
                f"the printer did not answer method {method} ({name}) within {timeout:g}s"
            ) from None
        finally:
            self._pending.pop(request_id, None)

    async def _async_send_checked(
        self, name: str, params: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> Mapping[str, Any]:
        """Send a command and raise when the printer refuses it."""
        result = await self._async_request(name, params, timeout=timeout)
        code = _integer(result.get("error_code"))
        if code:
            reason = ERROR_MESSAGES.get(code)
            raise CommandRejectedError(
                f"the printer refused {name}: {reason or f'error code {code}'}",
                code=code,
                reason=reason,
            )
        return result

    async def _async_refresh_status(self) -> None:
        await self._async_send_checked("status")

    async def _async_refresh_attributes(self) -> None:
        await self._async_send_checked("attributes")

    def _needs_full_status(self) -> bool:
        if self._full_status_at is None or self._sequence_gaps >= MAX_SEQUENCE_GAPS:
            return True
        return time.monotonic() - self._full_status_at > FULL_STATUS_INTERVAL

    # ------------------------------------------------------------------- read

    async def _async_read(self) -> PrinterSnapshot:
        """Return one snapshot, reconnecting when the session is gone.

        Between full reads the snapshot comes from the merged deltas, which the
        printer pushes whenever something changes; a quiet printer is an unchanged
        one, and the heartbeat is what tells a quiet printer from a dead one.
        """
        if not self._connected:
            await self.async_setup()
        if self._needs_full_status():
            try:
                await self._async_refresh_status()
            except _NoAnswerError:
                if self._full_status_at is None:
                    raise UnreachableError(
                        "the printer is connected but did not report its status"
                    ) from None

        parsed = parse_status(self._status)
        state = state_for(parsed["status"], parsed["sub_status"])
        # The job fields describe the last job until the next one starts, so they
        # are reported only while one is running, as Elegoo's own client does.
        in_job = parsed["status"] == 2
        fans = parsed["fans"]
        speed = SPEED_PERCENT_BY_MODE.get(parsed["speed_mode"]) if parsed["speed_mode"] is not None else None
        camera = parsed["camera"]
        if camera is None:
            camera = self._attributes.get("camera_connected") is True
        filament = await self._async_read_canvas(parsed)

        return PrinterSnapshot(
            protocol=ProtocolId.ELEGOO_CC2,
            connected=self._connected,
            capabilities=self.capabilities,
            print_state=state,
            progress=_percent(parsed["progress"]) if in_job else None,
            current_layer=parsed["current_layer"] if in_job else None,
            total_layers=(parsed["total_layers"] or None) if in_job else None,
            remaining=Seconds(parsed["remaining"])
            if in_job and parsed["remaining"] is not None
            else None,
            elapsed=Seconds(parsed["elapsed"]) if in_job and parsed["elapsed"] is not None else None,
            filename=parsed["filename"] if in_job else None,
            job_id=parsed["job_id"] if in_job else None,
            speed_factor=Percent(speed) if speed is not None else None,
            hotend=Temps(
                current=_celsius(parsed["hotend_current"]),
                target=_celsius(parsed["hotend_target"]),
            ),
            bed=Temps(
                current=_celsius(parsed["bed_current"]),
                target=_celsius(parsed["bed_target"]),
            ),
            chamber=Temps(current=_celsius(parsed["chamber_current"])),
            fans=Fans(
                model=_percent(fans["model"]),
                auxiliary=_percent(fans["auxiliary"]),
                chamber=_percent(fans["chamber"]),
                hotend=_percent(fans["hotend"]),
                controller=_percent(fans["controller"]),
            ),
            position=parsed["position"],
            homed_axes=parsed["homed_axes"],
            lights=frozenset({LightChannel.CHAMBER}) if parsed["light"] else frozenset(),
            camera=bool(camera),
            filament=filament,
            model=str(self._attributes.get("machine_model") or "")
            or (self._discovery.model if self._discovery else None)
            or None,
            firmware=self._firmware(),
            serial=self._serial or None,
            errors=tuple(
                f"printer exception {code}: {ERROR_MESSAGES[code]}"
                if code in ERROR_MESSAGES
                else f"printer exception {code}"
                for code in parsed["exceptions"]
            ),
        )

    def _firmware(self) -> str | None:
        version = _mapping(self._attributes.get("software_version")).get("ota_version")
        return str(version) if version else None

    async def _async_read_canvas(self, parsed: Mapping[str, Any]) -> FilamentSystem | None:
        """Return the CANVAS, asking for it again when it may have changed.

        A status push that mentions ``canvas_info`` marks it stale rather than being
        merged: the push is a delta, and a delta of a list would replace the whole
        list with the one tray that changed. A printer that does not know method
        2005 is not asked again. A read that fails keeps the last answer.
        """
        if Capability.FILAMENT_SLOTS not in self.capabilities or not self._canvas_supported:
            return None
        now = time.monotonic()
        due = self._canvas_at is None or now - self._canvas_at > CANVAS_INTERVAL
        if self._canvas_stale or due:
            try:
                result = await self._async_send_checked("canvas")
            except CommandRejectedError as err:
                if err.code == 1001:
                    self._canvas_supported = False
                _LOGGER.debug("%s: the CANVAS was not read: %s", self.config.name, err)
            except _NoAnswerError as err:
                _LOGGER.debug("%s: the CANVAS did not answer: %s", self.config.name, err)
            else:
                self._canvas = parse_canvas(result.get("canvas_info"))
                self._canvas_at = now
                self._canvas_stale = False
        if self._canvas is None:
            return None
        return FilamentSystem(
            units=self._canvas.units,
            auto_refill=self._canvas.auto_refill,
            activity=ACTIVITY_BY_SUB_STATUS.get(parsed["sub_status"] or 0),
        )

    @property
    def filament_presets(self) -> tuple[Mapping[str, Any], ...]:
        """Return the filaments Elegoo's page offers for a slot."""
        if Capability.SET_FILAMENT not in self.capabilities:
            return ()
        return tuple(preset.as_dict() for preset in FILAMENT_PRESETS)

    # --------------------------------------------------------------- commands

    async def _async_dispatch(self, command: Command, params: Mapping[str, Any]) -> None:
        """Translate one normalised command into a request."""
        handler = _DISPATCH.get(command)
        if handler is None:
            raise ProtocolError(f"the Centauri Carbon 2 adapter cannot dispatch {command.value}")
        await handler(self, params)

    async def _async_start_print(self, params: Mapping[str, Any]) -> None:
        """Start a stored file, the way the community measured it works.

        ``printer_check`` is always sent, because the printer remembers the last
        value it was given: a job that leaves it out inherits whatever the previous
        job, perhaps one started from the slicer, asked for. It is sent as ``true``,
        the slicer's default, so a print never starts on a bed that was not levelled.
        An empty ``slot_map`` lets the printer choose the Canvas tray, because a
        wrong mapping is acknowledged and then silently printed from tray 0.
        """
        status = _integer(_mapping(self._status.get("machine_status")).get("status"))
        if status != 1:
            raise ProtocolError("refusing to start a print: the printer is not idle")
        await self._async_send_checked(
            "start_print",
            {
                "storage_media": "local",
                "filename": str(params["filename"]).lstrip("/"),
                "config": {"printer_check": True, "slot_map": []},
            },
        )

    async def _async_pause(self, _params: Mapping[str, Any]) -> None:
        await self._async_send_checked("pause")

    async def _async_stop(self, _params: Mapping[str, Any]) -> None:
        await self._async_send_checked("stop")

    async def _async_resume(self, _params: Mapping[str, Any]) -> None:
        """Resume, reporting a refusal but not waiting for the late acknowledgement."""
        await self._async_send_unhurried("resume")

    async def _async_send_unhurried(
        self, name: str, params: Mapping[str, Any] | None = None
    ) -> None:
        """Send a command whose acknowledgement may only come once it has finished.

        A refusal comes back at once and is raised. Silence past the refusal window
        means the printer is carrying the command out.
        """
        with suppress(_NoAnswerError):
            await self._async_send_checked(name, params, timeout=RESUME_REFUSAL_WINDOW)

    async def _async_home(self, params: Mapping[str, Any]) -> None:
        """Home the given axes, lower case as Elegoo's SDK sends them."""
        await self._async_send_unhurried("home", {"homed_axes": str(params["axes"]).lower()})

    async def _async_jog(self, params: Mapping[str, Any]) -> None:
        """Move one axis by a relative distance in millimetres."""
        await self._async_send_unhurried(
            "move", {"axes": str(params["axis"]).lower(), "distance": float(params["distance"])}
        )

    async def _async_set_hotend_temp(self, params: Mapping[str, Any]) -> None:
        await self._async_send_checked("set_temperature", {"extruder": round(params["value"])})

    async def _async_set_bed_temp(self, params: Mapping[str, Any]) -> None:
        await self._async_send_checked("set_temperature", {"heater_bed": round(params["value"])})

    async def _async_set_fan(self, params: Mapping[str, Any]) -> None:
        channel = str(params.get("channel", "model"))
        if channel not in SETTABLE_FANS:
            raise ProtocolError(
                f"this printer's {channel} fan cannot be set; it sets the model, "
                "auxiliary and chamber fans"
            )
        await self._async_send_checked(
            "set_fan", {FAN_KEYS[channel]: fan_duty(float(params["value"]))}
        )

    async def _async_set_speed(self, params: Mapping[str, Any]) -> None:
        """Pick the speed mode nearest the requested percentage.

        The printer has four modes, not a factor: 50, 100, 150 and 200 percent.
        """
        await self._async_send_checked(
            "set_speed_mode", {"mode": speed_mode_for(float(params["value"]))}
        )

    async def _async_set_light(self, params: Mapping[str, Any]) -> None:
        """Switch the chamber light with ``power``, the key Elegoo's own web page sends."""
        await self._async_send_checked("set_light", {"power": 1 if params["on"] else 0})

    # --------------------------------------------------------------- filament

    def _require_slot(self, params: Mapping[str, Any]) -> dict[str, int]:
        """Return the unit and tray a filament command addresses, if the printer has it.

        A tray the printer does not report is refused here: the printer is known to
        acknowledge a request for a tray that does not exist and act on tray 0.
        """
        unit, slot = int(params.get("unit", 0)), int(params["slot"])
        if self._canvas is not None and self._canvas.slot(unit, slot) is None:
            raise ProtocolError(f"the CANVAS has no slot {slot + 1} on unit {unit + 1}")
        return {"canvas_id": unit, "tray_id": slot}

    async def _async_load_filament(self, params: Mapping[str, Any]) -> None:
        """Feed a slot into the nozzle: the printer heats, cuts the old filament, feeds."""
        target = self._require_slot(params)
        slot = self._canvas.slot(target["canvas_id"], target["tray_id"]) if self._canvas else None
        if slot is not None and not slot.loaded:
            raise ProtocolError(f"slot {slot.slot + 1} is empty")
        self._canvas_stale = True
        await self._async_send_unhurried("load_filament", target)

    async def _async_unload_filament(self, params: Mapping[str, Any]) -> None:
        """Pull a slot's filament back out of the nozzle."""
        target = self._require_slot(params)
        self._canvas_stale = True
        await self._async_send_unhurried("unload_filament", target)

    async def _async_set_filament(self, params: Mapping[str, Any]) -> None:
        """Record the filament in a slot, in the fields Elegoo's page sends."""
        target = self._require_slot(params)
        try:
            payload = edit_payload({**params, "unit": target["canvas_id"]})
        except ValueError as err:
            raise ProtocolError(str(err)) from err
        self._canvas_stale = True
        await self._async_send_checked("set_filament", payload)

    async def _async_set_auto_refill(self, params: Mapping[str, Any]) -> None:
        """Switch auto-refill, with the key both Elegoo's SDK and its page send."""
        self._canvas_stale = True
        await self._async_send_checked("set_auto_refill", {"auto_refill": bool(params["on"])})

    # ------------------------------------------------------------------ files

    async def async_list_files(self) -> Sequence[FileEntry]:
        """Return the files in the printer's internal storage.

        The parameters are the ones Elegoo's slicer sends. The printer answers with
        every file at once; the community measured 54 in one reply.
        """
        result = await self._async_send_checked(
            "file_list", {"storage_media": "local", "path": "/"}
        )
        return parse_file_list(result)

    async def async_upload_file(
        self, name: str, stream: AsyncIterator[bytes], *, size: int | None = None
    ) -> FileEntry:
        """Upload a file over HTTP in 1 MiB ranged PUTs, as Elegoo's SDK does.

        Every chunk carries the MD5 of the whole file, so the file is read once
        before anything is sent. The chunks share one kept-alive connection and are
        never retried: measured on firmware 02.01.00.00, a fresh connection per
        chunk is answered with HTTP 429, and a chunk retried after that corrupts
        the assembled file.
        """
        payload = bytearray()
        async for chunk in stream:
            payload.extend(chunk)
        body = bytes(payload)
        total = len(body)
        if not total:
            raise ProtocolError("refusing to upload an empty file")
        filename = name.rsplit("/", 1)[-1]
        digest = hashlib.md5(body).hexdigest()  # noqa: S324 - required by the printer's protocol

        offset = 0
        while offset < total:
            chunk = body[offset : offset + UPLOAD_CHUNK]
            end = offset + len(chunk) - 1
            headers = {
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {offset}-{end}/{total}",
                "X-File-Name": filename,
                "X-File-MD5": digest,
                "X-Token": self.access_code,
                "Accept": "application/json",
            }
            try:
                async with self._session.put(
                    self.upload_url, data=chunk, headers=headers, timeout=UPLOAD_TIMEOUT
                ) as response:
                    status = response.status
                    text = await response.text()
            except (aiohttp.ClientError, TimeoutError) as err:
                raise UnreachableError(f"the upload failed at byte {offset}: {err}") from err

            if status == 401:
                raise AuthError("the printer refused the access code for the upload")
            if status != 200:
                raise CommandRejectedError(
                    f"the printer answered the upload with HTTP {status} at byte {offset}",
                    code=status,
                )
            answer = load_message(text.encode())
            code = _integer((answer or {}).get("error_code"))
            if code != 0:
                reason = ERROR_MESSAGES.get(code or -1)
                raise CommandRejectedError(
                    f"the printer refused the upload at byte {offset}: "
                    f"{reason or f'error code {code}'}",
                    code=code,
                    reason=reason,
                )
            offset += len(chunk)

        return FileEntry(name=filename, path=filename, size=total)

    # ----------------------------------------------------------------- camera

    async def _async_enable_video(self) -> None:
        """Ask the printer to run its camera stream, once per session.

        Two community clients measured that the stream needs this after every
        reconnect. It is best effort: a camera that is already running serves the
        stream either way, so a refusal is logged rather than raised.
        """
        if self._video_enabled:
            return
        try:
            await self._async_send_checked("video_stream", {"enable": 1})
        except ProtocolError as err:
            _LOGGER.debug("%s: enabling the camera failed: %s", self.config.name, err)
            return
        self._video_enabled = True

    async def async_camera_frame(self) -> bytes:
        """Return one JPEG frame read from the printer's MJPEG stream."""
        await self._async_enable_video()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=10)
        try:
            async with self._session.get(self.camera_url, timeout=timeout) as response:
                if response.status >= 400:
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
            raise UnreachableError(f"cannot reach the camera: {err}") from err
        except TimeoutError as err:
            raise UnreachableError("the camera did not deliver a frame") from err
        raise UnreachableError("the camera stream ended before a complete frame arrived")

    async def async_camera_stream(self) -> AsyncIterator[bytes]:
        """Yield JPEG frames until the caller stops.

        The printer allows a single viewer, so frames are fanned out downstream from
        this one connection rather than opened per viewer.
        """
        await self._async_enable_video()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=15)
        async with self._session.get(self.camera_url, timeout=timeout) as response:
            if response.status >= 400:
                raise UnreachableError(f"the camera answered HTTP {response.status}")
            buffer = bytearray()
            async for chunk in response.content.iter_chunked(STREAM_CHUNK):
                buffer.extend(chunk)
                for frame in jpeg_frames(buffer):
                    yield frame
                if len(buffer) > MAX_FRAME_BYTES:
                    raise UnreachableError("the camera frame exceeded the size cap")


#: ``Command`` to handler.
_DISPATCH: Final[Mapping[Command, Any]] = MappingProxyType(
    {
        Command.START_PRINT: ElegooCC2Protocol._async_start_print,
        Command.PAUSE: ElegooCC2Protocol._async_pause,
        Command.RESUME: ElegooCC2Protocol._async_resume,
        Command.STOP: ElegooCC2Protocol._async_stop,
        Command.SET_HOTEND_TEMP: ElegooCC2Protocol._async_set_hotend_temp,
        Command.SET_BED_TEMP: ElegooCC2Protocol._async_set_bed_temp,
        Command.SET_FAN_SPEED: ElegooCC2Protocol._async_set_fan,
        Command.SET_SPEED: ElegooCC2Protocol._async_set_speed,
        Command.SET_LIGHT: ElegooCC2Protocol._async_set_light,
        Command.HOME: ElegooCC2Protocol._async_home,
        Command.JOG: ElegooCC2Protocol._async_jog,
        Command.LOAD_FILAMENT: ElegooCC2Protocol._async_load_filament,
        Command.UNLOAD_FILAMENT: ElegooCC2Protocol._async_unload_filament,
        Command.SET_FILAMENT: ElegooCC2Protocol._async_set_filament,
        Command.SET_AUTO_REFILL: ElegooCC2Protocol._async_set_auto_refill,
    }
)

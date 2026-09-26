"""Klipper through Moonraker, the JSON API that fronts it.

Moonraker serves HTTP and its WebSocket on one port, 7125 by default. State is read
with a single object query per poll, the documented efficient path, and pushed by
``printer.objects.subscribe`` when the coordinator wants updates instead. The
credential is an API key in the ``X-Api-Key`` header, whose refusal is HTTP 401.

Every value reported here is Klipper's own. ``print_stats.state`` is mapped through
:data:`PRINT_STATE_MAP`, the one place those strings appear, and layer counts come
from ``print_stats.info``, whose total field is the singular ``total_layer``.
Remaining time is the one field Moonraker does not report, so it is extrapolated
from elapsed time and progress rather than read.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import quote

import aiohttp

from ..const import Capability, Command, PrintState, ProtocolId, UnsafeFeature
from ..models import (
    Axis,
    Celsius,
    Fans,
    FileEntry,
    Millimetres,
    Percent,
    PrinterSnapshot,
    Seconds,
    Temps,
)
from ..protocols import (
    AuthError,
    CommandRejectedError,
    PrinterConfig,
    Protocol,
    ProtocolShapeError,
    UnreachableError,
    UnsupportedCommandError,
)

#: Moonraker's default port, the same one the registry offers in the config flow.
DEFAULT_PORT: Final = 7125

#: The printer objects one poll asks for. A chamber is a ``temperature_sensor``
#: whose name is installation specific, so it cannot join a fixed query.
OBJECTS: Final[tuple[str, ...]] = (
    "print_stats", "heater_bed", "extruder", "virtual_sdcard", "gcode_move", "fan",
    "toolhead", "display_status",
)

#: Klipper's own state strings. The only place they appear.
PRINT_STATE_MAP: Final[Mapping[str, PrintState]] = MappingProxyType(
    {
        "standby": PrintState.IDLE,
        "printing": PrintState.PRINTING,
        "paused": PrintState.PAUSED,
        "complete": PrintState.FINISHED,
        "cancelled": PrintState.CANCELLED,
        "error": PrintState.ERROR,
    }
)

REQUEST_TIMEOUT: Final = aiohttp.ClientTimeout(total=10, sock_connect=5)

_AXIS_CHARS: Final = frozenset("xyz")


class _SizedStreamPayload(aiohttp.payload.AsyncIterablePayload):
    """An async body whose byte length is known before it is sent."""

    def __init__(self, stream: AsyncIterator[bytes], *, size: int, filename: str) -> None:
        """Wrap ``stream`` and declare its length so no chunked encoding is used."""
        super().__init__(stream, filename=filename)
        # AsyncIterablePayload swallows a ``size`` keyword into ``**kwargs``, so
        # ``Payload.size`` stays None and aiohttp falls back to chunked transfer
        # encoding. Setting ``_size`` is what the ``size`` property reads.
        self._size = size


def _number(value: Any) -> float | None:
    """Return a finite float, or ``None`` when ``value`` is not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    """Return an int, or ``None`` when ``value`` is not a number."""
    number = _number(value)
    return int(number) if number is not None else None


def _text(value: Any) -> str | None:
    """Return a non-empty stripped string, or ``None``."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _block(status: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Return one queried object, which Moonraker omits when the printer lacks it."""
    block = status.get(name)
    return block if isinstance(block, Mapping) else {}


def _celsius(value: Any) -> Celsius | None:
    """Return a temperature reading in Celsius, or ``None``."""
    number = _number(value)
    return Celsius(number) if number is not None else None


def _fraction_percent(value: Any) -> Percent | None:
    """Return a Klipper 0.0 to 1.0 fraction as a clamped percentage, or ``None``."""
    number = _number(value)
    if number is None:
        return None
    return Percent(min(max(number * 100.0, 0.0), 100.0))


def _factor_percent(value: Any) -> Percent | None:
    """Return a Klipper multiplier as a percentage, which is not capped at 100.

    ``M220`` and ``M221`` accept a factor above 1.0, so clamping here would report an
    override the printer is really running as the default speed.
    """
    number = _number(value)
    return Percent(number * 100.0) if number is not None else None


def _remaining(elapsed: float | None, progress: float | None) -> Seconds | None:
    """Extrapolate remaining seconds from measured elapsed time and progress."""
    if elapsed is None or progress is None or elapsed <= 0.0:
        return None
    if not 0.0 < progress < 1.0:
        return None
    return Seconds(elapsed * (1.0 - progress) / progress)


def _position(value: Any) -> Axis | None:
    """Parse ``toolhead.position``, whose first three entries are X, Y and Z."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) < 3:
        return None
    x, y, z = (_number(item) for item in value[:3])
    if x is None or y is None or z is None:
        return None
    return Axis(x=Millimetres(x), y=Millimetres(y), z=Millimetres(z))


def _homed_axes(value: Any) -> frozenset[str]:
    """Return the uppercase axes named in Klipper's ``toolhead.homed_axes`` string."""
    if not isinstance(value, str):
        return frozenset()
    return frozenset(char.upper() for char in value if char.lower() in _AXIS_CHARS)


def _status_mapping(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the object-status mapping, tolerating Moonraker's ``result`` wrapper."""
    result = payload.get("result")
    status = (result if isinstance(result, Mapping) else payload).get("status")
    if not isinstance(status, Mapping):
        raise ProtocolShapeError("the status payload carried no object mapping")
    return status


def _merge_status(status: dict[str, Any], values: Any) -> None:
    """Merge one status update into the accumulated object mapping."""
    if not isinstance(values, Mapping):
        return
    for name, block in values.items():
        if not isinstance(block, Mapping):
            continue
        current = status.get(name)
        status[str(name)] = {**current, **block} if isinstance(current, Mapping) else dict(block)


def _changed_objects(frame: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the objects of a ``notify_status_update``, whose params are positional."""
    params = frame.get("params")
    if not isinstance(params, Sequence) or isinstance(params, (str, bytes)) or not params:
        raise ProtocolShapeError("notify_status_update carried no parameters")
    changed = params[0]
    if not isinstance(changed, Mapping):
        raise ProtocolShapeError("notify_status_update carried no object mapping")
    return changed


def _load_frame(raw: str) -> Mapping[str, Any]:
    """Decode one WebSocket text frame, which must be a JSON object."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ProtocolShapeError(f"the Moonraker socket sent a frame that is not JSON: {err}") from err
    if not isinstance(payload, Mapping):
        raise ProtocolShapeError("the Moonraker socket sent a JSON value that is not an object")
    return payload


def _parse_files(items: Any) -> list[FileEntry]:
    """Normalise Moonraker's file records, skipping its directory entries."""
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return []
    entries: list[FileEntry] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        path = _text(item.get("path"))
        if not path or path.endswith("/"):
            continue
        modified = _number(item.get("modified"))
        try:
            stamp = datetime.fromtimestamp(modified, tz=UTC) if modified is not None else None
        except (OSError, OverflowError, ValueError):
            stamp = None
        entries.append(
            FileEntry(
                name=path.rsplit("/", 1)[-1],
                path=path,
                size=_integer(item.get("size")),
                modified=stamp,
            )
        )
    return entries


async def _json_body(response: aiohttp.ClientResponse) -> Mapping[str, Any] | None:
    """Decode a JSON object body, returning ``None`` for an empty or non-JSON one."""
    try:
        payload = await response.json(content_type=None)
    except (aiohttp.ClientError, UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _rejected(label: str, status: int, body: Mapping[str, Any] | None) -> CommandRejectedError:
    """Build a refusal from Moonraker's own ``{"error": {"code", "message"}}`` body."""
    error = body.get("error") if body else None
    error = error if isinstance(error, Mapping) else {}
    reason = _text(error.get("message")) or f"HTTP {status}"
    return CommandRejectedError(
        f"{label} was refused: {reason}", code=error.get("code", status), reason=reason
    )


def _value_script(code: str) -> Callable[[Mapping[str, Any]], str]:
    """Return a builder that sends ``code`` with the validated ``value`` parameter."""
    return lambda params: f"{code} S{float(params['value']):g}"


def _home_script(params: Mapping[str, Any]) -> str:
    """Return the homing script for the validated axis set."""
    return "G28 " + " ".join(str(params["axes"]))


def _jog_script(params: Mapping[str, Any]) -> str:
    """Return a relative single-axis move, as the protocol research documents it."""
    return f"G91\nG1 {params['axis']}{float(params['distance']):g}\nG90"


def _fan_script(params: Mapping[str, Any]) -> str:
    """Return the part-cooling fan script, refusing every other channel."""
    if str(params.get("channel", "model")) != "model":
        # M106 addresses the part-cooling fan only. Another channel is a pin name
        # that only the printer's own configuration knows.
        raise UnsupportedCommandError(Command.SET_FAN_SPEED)
    return f"M106 S{round(float(params['value']) * 255 / 100)}"


#: Commands that are one endpoint call with no argument.
_ENDPOINTS: Final[Mapping[Command, str]] = MappingProxyType(
    {
        Command.PAUSE: "/printer/print/pause",
        Command.RESUME: "/printer/print/resume",
        Command.STOP: "/printer/print/cancel",
    }
)

#: Commands that are a G-code script, built from the validated parameters.
_SCRIPTS: Final[Mapping[Command, Callable[[Mapping[str, Any]], str]]] = MappingProxyType(
    {
        Command.SET_HOTEND_TEMP: _value_script("M104"),
        Command.SET_BED_TEMP: _value_script("M140"),
        Command.SET_CHAMBER_TEMP: _value_script("M141"),
        Command.SET_SPEED: _value_script("M220"),
        Command.SET_FLOW: _value_script("M221"),
        Command.HOME: _home_script,
        Command.JOG: _jog_script,
        Command.SET_FAN_SPEED: _fan_script,
    }
)


class MoonrakerProtocol(Protocol):
    """Klipper through Moonraker's HTTP and WebSocket APIs."""

    def __init__(
        self,
        config: PrinterConfig,
        session: aiohttp.ClientSession,
        *,
        granted: frozenset[Capability],
        unsafe: tuple[UnsafeFeature, ...] = (),
    ) -> None:
        """Create the adapter and the provenance cache it owns."""
        super().__init__(config, session, granted=granted, unsafe=unsafe)
        self._model: str | None = None
        self._serial: str | None = None
        self._socket: aiohttp.ClientWebSocketResponse | None = None

    def _url(self, path: str) -> str:
        """Return the absolute URL of one Moonraker endpoint."""
        return f"{self.config.base_url}{path}"

    def _headers(self) -> dict[str, str]:
        """Return the API key header, omitted entirely when no key is configured."""
        key = self.config.credentials.get("api_key")
        return {"X-Api-Key": key} if key else {}

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        read: bool,
        json: Mapping[str, Any] | None = None,
        data: aiohttp.MultipartWriter | None = None,
    ) -> Mapping[str, Any] | None:
        """Send one request and map every failure to the contract's error types."""
        label = f"{method} {path}"
        url = self.config.redacted_url
        try:
            async with self._session.request(
                method,
                self._url(path),
                headers=self._headers(),
                json=json,
                data=data,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status in (401, 403):
                    raise AuthError(f"{self.config.name} refused the API key")
                if response.status >= 400:
                    if read:
                        raise UnreachableError(
                            f"{label} answered HTTP {response.status} on {url}"
                        )
                    raise _rejected(label, response.status, await _json_body(response))
                return await _json_body(response)
        except aiohttp.ClientError as err:
            raise UnreachableError(f"{label} failed on {url}: {err}") from err
        except TimeoutError as err:
            raise UnreachableError(f"{label} timed out on {url}") from err

    async def _async_read_json(self, path: str) -> Mapping[str, Any]:
        """Read one resource and require a JSON object body."""
        body = await self._async_request("GET", path, read=True)
        if body is None:
            raise ProtocolShapeError(f"GET {path} did not answer with a JSON object")
        return body

    async def _async_command(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        data: aiohttp.MultipartWriter | None = None,
    ) -> None:
        """Send one command and ignore the body of a successful answer."""
        await self._async_request(method, path, read=False, json=json, data=data)

    async def async_setup(self) -> None:
        """Read ``/machine/system_info`` once, which also proves the API key."""
        body = await self._async_read_json("/machine/system_info")
        # Moonraker wraps nearly every HTTP answer in a ``result`` object, while
        # this endpoint's own documentation shows the payload unwrapped.
        result = body.get("result")
        system_info = (result if isinstance(result, Mapping) else body).get("system_info")
        if not isinstance(system_info, Mapping):
            raise ProtocolShapeError("/machine/system_info carried no system info object")
        cpu_info = system_info.get("cpu_info")
        cpu_info = cpu_info if isinstance(cpu_info, Mapping) else {}
        self._model = _text(cpu_info.get("model"))
        self._serial = _text(cpu_info.get("serial_number"))

    async def async_teardown(self) -> None:
        """Close the subscription socket. Idempotent."""
        socket, self._socket = self._socket, None
        if socket is not None and not socket.closed:
            with suppress(aiohttp.ClientError, ConnectionResetError):
                await socket.close()

    async def _async_read(self) -> PrinterSnapshot:
        """Return one snapshot from a single object query."""
        body = await self._async_read_json(f"/printer/objects/query?{'&'.join(OBJECTS)}")
        return self._snapshot(_status_mapping(body))

    def _snapshot(self, status: Mapping[str, Any]) -> PrinterSnapshot:
        """Build one snapshot from the accumulated object status mapping."""
        stats = _block(status, "print_stats")
        display = _block(status, "display_status")
        virtual = _block(status, "virtual_sdcard")
        move = _block(status, "gcode_move")
        extruder = _block(status, "extruder")
        bed = _block(status, "heater_bed")
        fan = _block(status, "fan")
        toolhead = _block(status, "toolhead")
        info = stats.get("info")
        info = info if isinstance(info, Mapping) else {}

        print_state = PRINT_STATE_MAP.get(_text(stats.get("state")) or "", PrintState.UNKNOWN)
        fraction = _number(display.get("progress"))
        if fraction is None:
            fraction = _number(virtual.get("progress"))
        elapsed = _number(stats.get("print_duration"))
        message = _text(stats.get("message"))

        return PrinterSnapshot(
            protocol=ProtocolId.MOONRAKER,
            connected=True,
            capabilities=self.capabilities,
            print_state=print_state,
            progress=_fraction_percent(fraction),
            current_layer=_integer(info.get("current_layer")),
            total_layers=_integer(info.get("total_layer")),
            # Derived from two measured values, because Moonraker reports none.
            remaining=_remaining(elapsed, fraction),
            elapsed=Seconds(elapsed) if elapsed is not None else None,
            filename=_text(stats.get("filename")),
            job_id=None,
            speed_factor=_factor_percent(move.get("speed_factor")),
            flow_factor=_factor_percent(move.get("extrude_factor")),
            hotend=Temps(
                current=_celsius(extruder.get("temperature")),
                target=_celsius(extruder.get("target")),
            ),
            bed=Temps(
                current=_celsius(bed.get("temperature")), target=_celsius(bed.get("target"))
            ),
            # A chamber is a ``temperature_sensor`` whose name is installation
            # specific, so it is absent from the fixed query and stays unknown.
            chamber=Temps(),
            fans=Fans(model=_fraction_percent(fan.get("speed"))),
            position=_position(toolhead.get("position")),
            homed_axes=_homed_axes(toolhead.get("homed_axes")),
            lights=frozenset(),
            camera=Capability.CAMERA in self.capabilities,
            model=self._model,
            # ``/machine/system_info`` reports host data only: CPU, SD card,
            # distribution, Python and network. It carries no Klipper or Moonraker
            # version, so nothing is reported rather than relabelling the host OS
            # version as firmware.
            firmware=None,
            serial=self._serial,
            errors=(message,) if print_state is PrintState.ERROR and message else (),
        )

    async def _async_dispatch(self, command: Command, params: Mapping[str, Any]) -> None:
        """Send one normalised command as the Moonraker request that carries it."""
        endpoint = _ENDPOINTS.get(command)
        if endpoint is not None:
            await self._async_command("POST", endpoint)
            return
        script = _SCRIPTS.get(command)
        if script is not None:
            await self._async_command(
                "POST", "/printer/gcode/script", json={"script": script(params)}
            )
            return
        if command is Command.START_PRINT:
            name = quote(str(params["filename"]), safe="")
            await self._async_command("POST", f"/printer/print/start?filename={name}")
            return
        if command is Command.DELETE_FILE:
            path = quote(str(params["filename"]), safe="/")
            await self._async_command("DELETE", f"/server/files/gcodes/{path}")
            return
        raise UnsupportedCommandError(command)

    async def async_list_files(self) -> Sequence[FileEntry]:
        """Return the files stored in Moonraker's ``gcodes`` root."""
        body = await self._async_read_json("/server/files/list?root=gcodes")
        items = body.get("result")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            raise ProtocolShapeError("the file list carried no result array")
        return _parse_files(items)

    async def async_upload_file(
        self, name: str, stream: AsyncIterator[bytes], *, size: int | None = None
    ) -> FileEntry:
        """Upload one file into the ``gcodes`` root.

        Moonraker answers 201 with a ``Location`` header and no file record, so the
        entry is built from what was uploaded rather than read back.
        """
        parent, _, leaf = name.rpartition("/")
        writer = aiohttp.MultipartWriter("form-data")
        writer.append("gcodes", {"Content-Disposition": 'form-data; name="root"'})
        if parent:
            writer.append(parent, {"Content-Disposition": 'form-data; name="path"'})
        payload: aiohttp.payload.AsyncIterablePayload = (
            aiohttp.payload.AsyncIterablePayload(stream, filename=leaf)
            if size is None
            else _SizedStreamPayload(stream, size=size, filename=leaf)
        )
        payload.set_content_disposition("form-data", name="file", filename=leaf)
        writer.append_payload(payload)
        await self._async_command("POST", "/server/files/upload", data=writer)
        return FileEntry(name=leaf, path=name, size=size)

    def async_subscribe(self) -> AsyncIterator[PrinterSnapshot] | None:
        """Return an iterator of snapshots the printer pushes over its WebSocket."""
        return self._async_stream()

    async def _async_stream(self) -> AsyncIterator[PrinterSnapshot]:
        """Merge pushed object updates into snapshots until the socket closes."""
        scheme = "wss" if self.config.tls else "ws"
        port = self.config.port or DEFAULT_PORT
        url = self.config.redacted_url
        try:
            socket = await self._session.ws_connect(
                f"{scheme}://{self.config.host}:{port}/websocket",
                headers=self._headers(),
                timeout=REQUEST_TIMEOUT,
                heartbeat=30,
            )
        except aiohttp.ClientError as err:
            raise UnreachableError(f"cannot open the Moonraker socket on {url}: {err}") from err
        except TimeoutError as err:
            raise UnreachableError(f"the Moonraker socket timed out on {url}") from err

        self._socket = socket
        status: dict[str, Any] = {}
        try:
            await socket.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "printer.objects.subscribe",
                    "params": {"objects": {name: None for name in OBJECTS}},
                    "id": 1,
                }
            )
            async for message in socket:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                frame = _load_frame(message.data)
                if frame.get("id") == 1:
                    result = frame.get("result")
                    if not isinstance(result, Mapping):
                        raise ProtocolShapeError("the subscribe reply carried no result")
                    _merge_status(status, _status_mapping(result))
                elif frame.get("method") == "notify_status_update":
                    _merge_status(status, _changed_objects(frame))
                else:
                    continue
                yield self._snapshot(status)
        except aiohttp.ClientError as err:
            raise UnreachableError(f"the Moonraker socket failed on {url}: {err}") from err
        except TimeoutError as err:
            raise UnreachableError(f"the Moonraker socket timed out on {url}") from err
        finally:
            self._socket = None
            with suppress(aiohttp.ClientError, ConnectionResetError):
                await socket.close()

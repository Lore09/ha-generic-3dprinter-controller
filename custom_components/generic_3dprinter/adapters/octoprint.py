"""OctoPrint adapter, speaking its REST API.

OctoPrint splits state from job progress across two documents, so one poll is two
reads: ``GET /api/printer`` carries the temperatures and the human ``state`` object,
and ``GET /api/job`` carries the file name and the progress block. Both take the
``X-Api-Key`` header, and a printer with access control disabled answers either way.

``state.text`` is a display string whose set of values OctoPrint documents as not
exhaustive, so the mapping in :data:`PRINT_STATE_BY_TEXT` is a fast path and
:data:`STATE_BY_FLAG` is the authority: the boolean flags in ``state.flags`` are
machine-readable and always present. An unlisted text is therefore not a failure.

Several readings this integration models simply have no REST surface on OctoPrint:
no layer count, no axis position, no fan duty, no speed or flow factor, no firmware
string. Those snapshot fields stay ``None`` rather than carrying a guess.

The push stream is SockJS and needs a real login session, so this adapter polls.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import quote

import aiohttp

from ..const import Capability, Command, PrintState, ProtocolId
from ..models import Celsius, Fans, FileEntry, Percent, PrinterSnapshot, Seconds, Temps
from ..protocols import (
    AuthError,
    CommandRejectedError,
    Protocol,
    ProtocolError,
    ProtocolShapeError,
    UnreachableError,
    UnsupportedCommandError,
)

#: Every file this adapter lists, uploads, starts and deletes lives in OctoPrint's
#: ``local`` storage, and the location is part of the path a command accepts.
LOCATION: Final = "local"

REQUEST_TIMEOUT: Final = aiohttp.ClientTimeout(total=10, sock_connect=5)

#: OctoPrint's own state strings, from ``getStateString`` in ``octoprint/util/comm.py``.
PRINT_STATE_BY_TEXT: Final[Mapping[str, PrintState]] = MappingProxyType(
    {
        "Offline": PrintState.UNKNOWN,
        "Opening serial connection": PrintState.PREPARING,
        "Detecting serial connection": PrintState.PREPARING,
        "Connecting": PrintState.PREPARING,
        "Operational": PrintState.IDLE,
        "Starting print from SD": PrintState.PREPARING,
        "Starting to send file to SD": PrintState.PREPARING,
        "Starting": PrintState.PREPARING,
        "Printing from SD": PrintState.PRINTING,
        "Sending file to SD": PrintState.PRINTING,
        "Printing": PrintState.PRINTING,
        "Cancelling": PrintState.CANCELLED,
        "Pausing": PrintState.PAUSED,
        "Paused": PrintState.PAUSED,
        "Resuming": PrintState.PRINTING,
        "Finishing": PrintState.FINISHED,
        "Error": PrintState.ERROR,
        "Offline after error": PrintState.ERROR,
        "Transferring file to SD": PrintState.PREPARING,
    }
)

#: Walked in order, so the first flag that is set and true wins.
STATE_BY_FLAG: Final[tuple[tuple[str, PrintState], ...]] = (
    ("printing", PrintState.PRINTING),
    ("pausing", PrintState.PAUSED),
    ("paused", PrintState.PAUSED),
    ("cancelling", PrintState.CANCELLED),
    ("error", PrintState.ERROR),
    ("closedOrError", PrintState.ERROR),
    ("ready", PrintState.IDLE),
    ("operational", PrintState.IDLE),
)

#: OctoPrint marks a directory with this record type.
FOLDER_TYPE: Final = "folder"

#: ``GET /api/printer`` answers this while no printer is connected.
PRINTER_DISCONNECTED: Final = 409

#: Command to ``(method, path)``. The file operations build their path from the
#: parameter, so they are handled outside this table.
_ROUTES: Final[Mapping[Command, tuple[str, str]]] = MappingProxyType(
    {
        Command.PAUSE: ("POST", "/api/job"),
        Command.RESUME: ("POST", "/api/job"),
        Command.STOP: ("POST", "/api/job"),
        Command.SET_HOTEND_TEMP: ("POST", "/api/printer/tool"),
        Command.SET_BED_TEMP: ("POST", "/api/printer/bed"),
        Command.SET_FAN_SPEED: ("POST", "/api/printer/command"),
        Command.SET_SPEED: ("POST", "/api/printer/printhead"),
        Command.SET_FLOW: ("POST", "/api/printer/tool"),
        Command.HOME: ("POST", "/api/printer/printhead"),
        Command.JOG: ("POST", "/api/printer/printhead"),
    }
)


_PayloadBuilder = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def _constant_command(body: Mapping[str, Any]) -> _PayloadBuilder:
    """Return a payload builder that ignores its parameters and sends ``body``."""
    return lambda _params: body


def _start_print_command(_params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Build the file-select-and-print body."""
    return {"command": "select", "print": True}


def _fan_command(params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Build M106 for the part-cooling fan, refusing any other channel.

    OctoPrint has no fan endpoint, so the arbitrary-command route is the only path,
    and M106 addresses the part-cooling fan only.
    """
    if str(params.get("channel", "model")) != "model":
        raise UnsupportedCommandError(Command.SET_FAN_SPEED)
    return {"command": f"M106 S{round(float(params['value']) * 255 / 100)}"}


def _hotend_command(params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Build the target temperature of the first tool."""
    return {"command": "target", "targets": {"tool0": params["value"]}}


def _bed_command(params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Build the bed target temperature."""
    return {"command": "target", "target": params["value"]}


def _speed_command(params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Build the feedrate factor.

    OctoPrint accepts 50 to 200 here while the normalised command allows 0 to 100, so
    anything below 50 is refused by the printer rather than by this adapter.
    """
    return {"command": "feedrate", "factor": params["value"]}


def _flow_command(params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Build the flowrate factor."""
    return {"command": "flowrate", "factor": params["value"]}


def _home_command(params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Build the home request for the chosen axes."""
    return {"command": "home", "axes": [letter.lower() for letter in str(params["axes"])]}


def _jog_command(params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Build the one-axis jog body.

    OctoPrint interprets a jog value as a relative move, which is what the normalised
    command means.
    """
    return {"command": "jog", str(params["axis"]).lower(): params["distance"]}


_PAYLOADS: Final[Mapping[Command, _PayloadBuilder]] = MappingProxyType(
    {
        Command.SET_HOTEND_TEMP: _hotend_command,
        Command.SET_BED_TEMP: _bed_command,
        Command.SET_FAN_SPEED: _fan_command,
        Command.SET_SPEED: _speed_command,
        Command.SET_FLOW: _flow_command,
        Command.HOME: _home_command,
        Command.JOG: _jog_command,
        Command.PAUSE: _constant_command({"command": "pause", "action": "pause"}),
        Command.RESUME: _constant_command({"command": "pause", "action": "resume"}),
        Command.STOP: _constant_command({"command": "cancel"}),
    }
)


class _PrinterDisconnected(Exception):
    """OctoPrint's answer while its printer connection is closed.

    ``GET /api/printer`` returns HTTP 409 whenever no printer is connected. That is
    neither a bad credential nor a bad shape, so it stays out of the error types the
    caller sees and is left to the two call sites to decide.
    """


class _SizedStreamPayload(aiohttp.payload.AsyncIterablePayload):
    """An async body whose byte length is known before it is sent."""

    def __init__(self, stream: AsyncIterator[bytes], *, size: int, filename: str) -> None:
        super().__init__(stream, filename=filename)
        self._size = size


# --------------------------------------------------------------------- parsing


def _number(value: Any) -> float | None:
    """Return ``value`` as a float, or ``None`` when it is not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _celsius(value: Any) -> Celsius | None:
    """Return one temperature reading in degrees Celsius."""
    number = _number(value)
    return Celsius(number) if number is not None else None


def _percent(value: Any) -> Percent | None:
    """Return one percentage, clamped to 0-100."""
    number = _number(value)
    return None if number is None else Percent(min(max(number, 0.0), 100.0))


def _seconds(value: Any) -> Seconds | None:
    """Return one duration in seconds."""
    number = _number(value)
    return Seconds(number) if number is not None else None


def _mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return a nested object, or an empty one when it is absent or the wrong type."""
    value = payload.get(key)
    return value if isinstance(value, Mapping) else {}


def _temps(payload: Mapping[str, Any], key: str) -> Temps:
    """Read ``actual`` and ``target`` for one heater out of a temperature block."""
    block = _mapping(_mapping(payload, "temperature"), key)
    return Temps(current=_celsius(block.get("actual")), target=_celsius(block.get("target")))


def _state(printer: Mapping[str, Any]) -> PrintState:
    """Return the print state, preferring the known text and falling back to the flags."""
    state = _mapping(printer, "state")
    text = state.get("text")
    if isinstance(text, str) and text in PRINT_STATE_BY_TEXT:
        return PRINT_STATE_BY_TEXT[text]
    flags = _mapping(state, "flags")
    return next(
        (mapped for flag, mapped in STATE_BY_FLAG if flags.get(flag) is True),
        PrintState.UNKNOWN,
    )


def _errors(printer: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the printer's own error text while an error flag is set."""
    if _state(printer) is not PrintState.ERROR:
        return ()
    text = _mapping(printer, "state").get("text")
    return (text,) if isinstance(text, str) and text else ()


def _job_name(job: Mapping[str, Any]) -> str | None:
    """Return the file name of the selected job, when there is one."""
    name = _mapping(_mapping(job, "job"), "file").get("name")
    return name if isinstance(name, str) and name else None


def _job_seconds(job: Mapping[str, Any], key: str) -> Seconds | None:
    """Return one progress duration, which OctoPrint reports in seconds."""
    return _seconds(_mapping(job, "progress").get(key))


def _timestamp(value: Any) -> datetime | None:
    """Return a Unix timestamp as an aware UTC datetime."""
    number = _number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _file_records(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the file records of a listing or upload answer, in any version's shape.

    OctoPrint 2.0 answers ``{"local": {"files": [...]}}``, earlier versions answer
    ``{"files": [...]}``, and the upload answer of 1.x carries one record per storage
    under ``files``, which is a mapping of storage id to record.
    """
    files = payload.get("files")
    if isinstance(files, Mapping) and not isinstance(files.get("files"), list):
        return [item for item in files.values() if isinstance(item, Mapping)]
    if files is None:
        for storage in payload.values():
            if isinstance(storage, Mapping) and isinstance(storage.get("files"), list):
                files = storage["files"]
                break
    if not isinstance(files, list):
        raise ProtocolShapeError("OctoPrint answered without a files array")
    return [item for item in files if isinstance(item, Mapping)]


def _file_entry(item: Mapping[str, Any]) -> FileEntry | None:
    """Normalise one file record. The path is location-qualified on purpose.

    It is the exact string ``START_PRINT`` and ``DELETE_FILE`` accept, so a path from a
    listing round-trips back through a command.
    """
    name = item.get("name")
    relative = item.get("path") or name
    if not isinstance(name, str) or not name or not isinstance(relative, str):
        return None
    size = item.get("size")
    return FileEntry(
        name=PurePosixPath(name).name,
        path=f"{LOCATION}/{relative.lstrip('/')}",
        size=int(size) if isinstance(size, int) else None,
        modified=_timestamp(item.get("date")),
    )


def _uploaded_entry(payload: Mapping[str, Any]) -> FileEntry | None:
    """Return the record of the file OctoPrint just stored, when it reported one."""
    try:
        records = _file_records(payload)
    except ProtocolShapeError:
        return None
    for item in records:
        entry = _file_entry(item)
        if entry is not None:
            return entry
    return None


def _disconnected_snapshot(capabilities: frozenset[Capability]) -> PrinterSnapshot:
    """Return the snapshot of an OctoPrint host whose printer is not connected."""
    return PrinterSnapshot(
        protocol=ProtocolId.OCTOPRINT,
        connected=False,
        capabilities=capabilities,
        print_state=PrintState.UNKNOWN,
    )


async def _json_body(response: aiohttp.ClientResponse) -> Mapping[str, Any]:
    """Decode a JSON object body, raising when the device answered something else."""
    try:
        text = await response.text()
    except (aiohttp.ClientError, UnicodeDecodeError) as err:
        raise ProtocolShapeError(f"OctoPrint answered a body that is not text: {err}") from err
    if not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as err:
        raise ProtocolShapeError("OctoPrint answered a body that is not JSON") from err
    if not isinstance(payload, Mapping):
        raise ProtocolShapeError("OctoPrint answered JSON that is not an object")
    return payload


async def _error_reason(response: aiohttp.ClientResponse) -> str | None:
    """Return OctoPrint's own message for a refused command."""
    try:
        payload = await _json_body(response)
    except ProtocolShapeError:
        return None
    error = payload.get("error")
    return str(error) if error else None


async def _multipart_body(
    payload: Mapping[str, Any], stream: AsyncIterator[bytes], filename: str, size: int
) -> aiohttp.MultipartWriter:
    """Build the multipart body of an upload, whose total length is known.

    OctoPrint requires a ``Content-Length`` on an upload, which aiohttp can only send
    when the whole multipart body has a known length, so the file part declares its own.
    """
    writer = aiohttp.MultipartWriter("form-data")
    for field, value in payload.items():
        writer.append(value, {"Content-Disposition": f'form-data; name="{field}"'})
    part = _SizedStreamPayload(stream, size=size, filename=filename)
    part.set_content_disposition("form-data", name="file", filename=filename)
    writer.append_payload(part)
    return writer


class OctoPrintProtocol(Protocol):
    """OctoPrint over its REST API, polled."""

    # ---------------------------------------------------------------- lifecycle

    async def async_setup(self) -> None:
        """Ask the host for its printer state once, proving address and credential."""
        try:
            await self._async_read_json("GET", "/api/printer")
        except _PrinterDisconnected:
            # A host with no printer connected still answered, which is all setup proves.
            return

    async def async_teardown(self) -> None:
        """Do nothing. This adapter holds no socket, because it never opens SockJS."""
        return

    def async_subscribe(self) -> AsyncIterator[PrinterSnapshot] | None:
        """Return ``None``. This adapter polls."""
        # OctoPrint's push stream is SockJS at /sockjs, and subscribing to state there
        # needs an ``auth`` message carrying ``<userid>:<sessionkey>`` from a real login
        # session, which an API key alone cannot produce.
        return None

    # -------------------------------------------------------------------- reads

    async def _async_read(self) -> PrinterSnapshot:
        """Return one snapshot, read from ``/api/printer`` and ``/api/job``.

        The layer counts, the speed and flow factors, the fan duty, the axis position
        and the firmware string have no REST surface, so those fields stay empty.
        """
        printer = await self._async_read_json_or_none("/api/printer")
        if printer is None:
            return _disconnected_snapshot(self.capabilities)
        job = await self._async_read_json("GET", "/api/job")

        progress = _mapping(job, "progress")
        completion = _number(progress.get("completion"))
        return PrinterSnapshot(
            protocol=ProtocolId.OCTOPRINT,
            connected=True,
            capabilities=self.capabilities,
            print_state=_state(printer),
            progress=_percent(completion * 100.0) if completion is not None else None,
            remaining=_job_seconds(job, "printTimeLeft"),
            elapsed=_job_seconds(job, "printTime"),
            filename=_job_name(job),
            job_id=None,
            # The REST API exposes no layer count. The push stream carries currentZ only,
            # and layers come from the DisplayLayerProgress plugin, so a number here
            # would be invented.
            current_layer=None,
            total_layers=None,
            # /api/printer/printhead can set a feedrate factor but cannot read one back.
            speed_factor=None,
            flow_factor=None,
            # OctoPrint reports one temperature block per tool.
            hotend=_temps(printer, "tool0"),
            bed=_temps(printer, "bed"),
            chamber=_temps(printer, "chamber"),
            # The REST API reports no fan duty.
            fans=Fans(),
            # /api/printer has no axis field; currentZ exists only in the push stream.
            position=None,
            homed_axes=frozenset(),
            lights=frozenset(),
            camera=Capability.CAMERA in self.capabilities,
            model=None,
            serial=None,
            # /api/version is the OctoPrint server version, not the firmware, so nothing
            # is reported rather than mislabelling one as the other.
            firmware=None,
            errors=_errors(printer),
        )

    # -------------------------------------------------------------------- files

    async def async_list_files(self) -> Sequence[FileEntry]:
        """Return the files in OctoPrint's local storage, folders skipped."""
        payload = await self._async_read_json("GET", "/api/files/local?recursive=true")
        entries = [
            _file_entry(item)
            for item in _file_records(payload)
            if item.get("type") != FOLDER_TYPE
        ]
        return [entry for entry in entries if entry is not None]

    async def async_upload_file(
        self, name: str, stream: AsyncIterator[bytes], *, size: int | None = None
    ) -> FileEntry:
        """Upload one file into the local storage and return its stored entry."""
        if size is None:
            raise ProtocolError(
                "the size is required for an OctoPrint upload, because OctoPrint needs "
                "a Content-Length and this adapter will not buffer the stream to measure it"
            )

        target = PurePosixPath(name)
        folder = str(target.parent)
        fields: dict[str, str] = {}
        if folder not in (".", ""):
            fields["path"] = folder
        writer = await _multipart_body(fields, stream, target.name, size)
        response = await self._async_request("POST", "/api/files/local", data=writer)
        try:
            stored = _uploaded_entry(await _json_body(response))
        finally:
            response.release()
        if stored is not None:
            return stored
        return FileEntry(name=target.name, path=f"{LOCATION}/{name}", size=size, modified=None)

    # ----------------------------------------------------------------- commands

    async def _async_dispatch(self, command: Command, params: Mapping[str, Any]) -> None:
        """Translate one normalised command into an OctoPrint request."""
        if command is Command.DELETE_FILE:
            filename = quote(str(params["filename"]), safe="/")
            await self._async_command("DELETE", f"/api/files/{filename}")
            return
        if command is Command.START_PRINT:
            filename = quote(str(params["filename"]), safe="/")
            await self._async_command(
                "POST", f"/api/files/{filename}", body=_start_print_command(params)
            )
            return

        body = _PAYLOADS.get(command)
        if body is None:
            raise UnsupportedCommandError(command)
        method, path = _ROUTES[command]
        await self._async_command(method, path, body=body(params))

    # ---------------------------------------------------------------- transport

    def _url(self, path: str) -> str:
        """Return an absolute URL on the configured printer."""
        return f"{self.config.base_url}{path}"

    def _headers(self) -> dict[str, str]:
        """Return the request headers. An absent key sends no header at all."""
        key = self.config.credentials.get("api_key")
        return {"X-Api-Key": key} if key else {}

    async def _async_command(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        data: aiohttp.MultipartWriter | None = None,
    ) -> None:
        """Send one command and release the answer, whose body nothing reads."""
        response = await self._async_request(method, path, body=body, data=data)
        response.release()

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        data: aiohttp.MultipartWriter | None = None,
    ) -> aiohttp.ClientResponse:
        """Send one request and return its answered response.

        A read raises :class:`UnreachableError` or :class:`AuthError`. A command also
        raises :class:`CommandRejectedError`, carrying OctoPrint's own reason.
        """
        try:
            response = await self._session.request(
                method, self._url(path), headers=self._headers(), json=body, data=data,
                timeout=REQUEST_TIMEOUT,
            )
        except aiohttp.ClientError as err:
            raise UnreachableError(
                f"{method} {path} on {self.config.name} failed: {err}"
            ) from err
        except TimeoutError as err:
            raise UnreachableError(f"{method} {path} on {self.config.name} timed out") from err

        if response.status in (401, 403):
            response.release()
            raise AuthError(f"{self.config.name} refused the API key")
        if response.status == PRINTER_DISCONNECTED and path == "/api/printer":
            response.release()
            raise _PrinterDisconnected
        if response.status >= 400:
            reason = await _error_reason(response)
            status = response.status
            response.release()
            detail = reason or f"HTTP {status}"
            if body is None and data is None:
                raise UnreachableError(f"{method} {path} on {self.config.name}: {detail}")
            raise CommandRejectedError(
                f"{self.config.name} refused {method} {path}: {detail}",
                code=status,
                reason=reason,
            )
        return response

    async def _async_read_json(self, method: str, path: str) -> Mapping[str, Any]:
        """Send a read and decode its JSON object body."""
        response = await self._async_request(method, path)
        try:
            return await _json_body(response)
        finally:
            response.release()

    async def _async_read_json_or_none(self, path: str) -> Mapping[str, Any] | None:
        """Send one read, answering ``None`` while the printer connection is closed."""
        try:
            return await self._async_read_json("GET", path)
        except _PrinterDisconnected:
            return None

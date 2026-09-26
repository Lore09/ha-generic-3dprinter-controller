"""Duet adapter for RepRapFirmware served over its own HTTP port.

A standalone Duet board speaks a small family of ``rr_*`` GET endpoints. State
comes from ``GET /rr_model?key=...``, which answers the printer's object model, and
every command is G-code sent as ``GET /rr_gcode?gcode=...``. Nothing is pushed: the
standalone server accepts GET, OPTIONS, and a POST for ``rr_upload`` only, so a
board with an SBC running Duet Software Framework needs a different adapter.

Authentication is a web password over HTTP Digest plus a session key. The digest
handshake is one challenge and one response, cached here so later requests go out
pre-authenticated, and the key RRF returns is echoed on every request afterwards.
:meth:`DuetProtocol.async_setup` establishes both once.

The object model can report any field as null, so every value is read through a
small parser rather than by indexing. Progress is the one reading RRF does not
report at all: it is computed here from the job's file position against the file
size, which is what DuetWebControl does.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

import aiohttp
from yarl import URL

from ..const import Capability, Command, ModelProfile, PrintState, ProtocolId, UnsafeFeature
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
    ProtocolError,
    ProtocolShapeError,
    UnreachableError,
    UnsupportedCommandError,
)

_LOGGER = logging.getLogger(__name__)

MODEL_PATH: Final = "/rr_model"
GCODE_PATH: Final = "/rr_gcode"
FILES_PATH: Final = "/rr_files"
UPLOAD_PATH: Final = "/rr_upload"

#: The object model array is ``fans``; ``fan`` does not exist in the model at all.
READ_KEY: Final = "state,heat,job,move,tools,fans"

#: Short enough that a poll cannot outlive its interval.
REQUEST_TIMEOUT: Final[aiohttp.ClientTimeout] = aiohttp.ClientTimeout(total=15)

_AUTH_STATUSES: Final[frozenset[int]] = frozenset({401, 403})
_AXIS_LETTERS: Final[frozenset[str]] = frozenset({"X", "Y", "Z"})
_DIGEST_PARAM: Final = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))')


@dataclass(frozen=True, slots=True)
class _Answer:
    """One HTTP answer, read before the connection is released."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    @property
    def ok(self) -> bool:
        """Return ``True`` for a 2xx status."""
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        """Return the body decoded as text, never raising."""
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        """Return the decoded body, or ``None`` when it is not a JSON document."""
        if not self.body:
            return None
        try:
            return json.loads(self.text)
        except json.JSONDecodeError:
            return None


# --------------------------------------------------------------------- parsing


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


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


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _celsius(value: Any) -> Celsius | None:
    number = _number(value)
    return Celsius(number) if number is not None else None


def _first_index(value: Any) -> int | None:
    """Return the first index of an RRF index array, or of a scalar index."""
    for item in _sequence(value) or (value,):
        index = _integer(item)
        if index is not None:
            return index
    return None


def _timestamp(value: Any) -> datetime | None:
    """Parse an RRF ISO 8601 date, tolerating the ``Z`` suffix it emits."""
    text = _text(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _temps(heaters: Sequence[Any], index: int | None) -> Temps:
    """Return one heater's readings, from the index the model gave for that role."""
    if index is None or not 0 <= index < len(heaters):
        return Temps()
    heater = _mapping(heaters[index])
    return Temps(
        current=_celsius(heater.get("current")),
        target=_celsius(heater.get("active")),
    )


def _fan_percent(value: Any) -> Percent | None:
    """Return a fan duty as a percentage from RRF's 0.0 to 1.0 fraction."""
    fan = _mapping(value)
    number = _number(fan.get("value"))
    if number is None:
        number = _number(fan.get("actualValue"))
    # RRF writes -1 for a fan whose duty it does not know.
    if number is None or number < 0:
        return None
    return Percent(min(max(number * 100.0, 0.0), 100.0))


def _is_hotend_fan(value: Any) -> bool:
    fan = _mapping(value)
    if fan.get("thermostatic"):
        return True
    name = _text(fan.get("name"))
    return bool(name and ("hotend" in name.lower() or "heater" in name.lower()))


def _hotend_heater(
    heaters: Sequence[Any], tools: Sequence[Any], others: frozenset[int]
) -> int | None:
    """Return the heater index driving the first tool, or a fallback when it is absent."""
    index = _first_index(_mapping(tools[0]).get("heaters")) if tools else None
    if index is not None:
        return index
    # With no tool mapping the lowest heater that is neither the bed nor the chamber
    # is assumed to be the hotend. That is true of the boards this adapter has been
    # read against, and a guess on a machine that assigns its heaters another way.
    for candidate in range(len(heaters)):
        if candidate not in others:
            return candidate
    return None


def _axes(move: Mapping[str, Any]) -> tuple[Axis | None, frozenset[str]]:
    """Return the toolhead position and the set of homed axis letters."""
    points: dict[str, float] = {}
    homed: set[str] = set()
    for item in _sequence(move.get("axes")):
        entry = _mapping(item)
        # The letter travels with the value, so nothing here depends on axis order.
        letter = (_text(entry.get("letter")) or "").upper()
        if not letter:
            continue
        if entry.get("homed"):
            homed.add(letter)
        value = _number(entry.get("userPosition"))
        if value is not None and letter in _AXIS_LETTERS:
            points[letter] = value

    position = None
    if points:
        position = Axis(
            x=Millimetres(points["X"]) if "X" in points else None,
            y=Millimetres(points["Y"]) if "Y" in points else None,
            z=Millimetres(points["Z"]) if "Z" in points else None,
        )
    return position, frozenset(homed)


def parse_model(
    model: Mapping[str, Any], *, protocol: ProtocolId, capabilities: frozenset[Capability]
) -> PrinterSnapshot:
    """Return the normalised snapshot of one RRF object model. Pure in its arguments."""
    state = _mapping(model.get("state"))
    job = _mapping(model.get("job"))
    file = _mapping(job.get("file"))
    heat = _mapping(model.get("heat"))
    heaters = _sequence(heat.get("heaters"))
    fans = _sequence(model.get("fans"))

    bed = _first_index(heat.get("bedHeaters"))
    chamber = _first_index(heat.get("chamberHeaters"))
    taken = frozenset(index for index in (bed, chamber) if index is not None)
    hotend = _hotend_heater(heaters, _sequence(model.get("tools")), taken)

    hotend_fan = None
    for item in fans:
        if _is_hotend_fan(item):
            hotend_fan = _fan_percent(item)
            break

    file_position = _number(job.get("filePosition"))
    size = _number(file.get("size"))
    progress = None
    if file_position is not None and size:
        progress = Percent(min(max(file_position / size * 100.0, 0.0), 100.0))

    elapsed = _number(job.get("duration"))
    remaining = _number(_mapping(job.get("timesLeft")).get("file"))
    position, homed = _axes(_mapping(model.get("move")))

    return PrinterSnapshot(
        protocol=protocol,
        connected=True,
        capabilities=capabilities,
        print_state=STATE_BY_RRF_STATUS.get(
            _text(state.get("status")) or "", PrintState.UNKNOWN
        ),
        progress=progress,
        current_layer=_integer(job.get("layer")),
        total_layers=_integer(file.get("numLayers")),
        remaining=Seconds(remaining) if remaining is not None else None,
        elapsed=Seconds(elapsed) if elapsed is not None else None,
        filename=_text(file.get("fileName")),
        hotend=_temps(heaters, hotend),
        bed=_temps(heaters, bed),
        chamber=_temps(heaters, chamber),
        # Index 0 is the model fan and a thermostatic fan is the hotend fan, so one
        # fan may legitimately land on both channels.
        fans=Fans(
            model=_fan_percent(fans[0]) if fans else None,
            hotend=hotend_fan,
        ),
        position=position,
        homed_axes=homed,
    )


def _file_entry(item: Any) -> FileEntry | None:
    """Return one listed file, or ``None`` when the entry carries no usable name."""
    if isinstance(item, str):
        name = item.strip()
        return FileEntry(name=name, path=remote_path(name, "0:/")) if name else None
    if not isinstance(item, Mapping):
        return None
    name = (_text(item.get("name")) or "").strip()
    if not name:
        return None
    return FileEntry(
        name=name,
        path=remote_path(name, "0:/"),
        size=_integer(item.get("size")),
        modified=_timestamp(item.get("date")),
    )


def parse_file_list(text: str, payload: Any) -> tuple[FileEntry, ...]:
    """Parse both shapes the standalone listing endpoints answer.

    ``rr_files`` answers a newline separated list while ``rr_filelist`` answers
    JSON, and one unusable entry is skipped rather than failing the whole listing.
    """
    if isinstance(payload, list):
        items: Sequence[Any] = payload
    elif isinstance(payload, Mapping):
        return ()
    else:
        items = [line.strip() for line in text.splitlines() if line.strip()]
    return tuple(entry for entry in (_file_entry(item) for item in items) if entry is not None)


# ---------------------------------------------------------------- digest and URL


def parse_challenge(header: str) -> Mapping[str, str] | None:
    """Return the Digest challenge parameters, or ``None`` when there is no usable one."""
    if not header.lower().startswith("digest"):
        return None
    fields = {
        match.group(1).lower(): match.group(2)
        if match.group(2) is not None
        else match.group(3)
        for match in _DIGEST_PARAM.finditer(header)
    }
    return fields if fields.get("nonce") else None


def _md5(text: str) -> str:
    # The digest RRF challenges with is MD5 by definition.
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def digest_header(
    challenge: Mapping[str, str],
    *,
    method: str,
    target: str,
    username: str,
    password: str,
    nonce_count: int,
    cnonce: str,
) -> str:
    """Return the ``Authorization`` value answering one Digest challenge."""
    realm = challenge.get("realm", "")
    nonce = challenge["nonce"]
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{target}")
    qop = "auth" if "auth" in [item.strip() for item in challenge.get("qop", "").split(",")] else ""

    fields = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{target}"']
    if qop:
        count = f"{nonce_count:08x}"
        response = _md5(f"{ha1}:{nonce}:{count}:{cnonce}:{qop}:{ha2}")
        fields += [
            f'response="{response}"',
            f'qop="{qop}"',
            f'nc="{count}"',
            f'cnonce="{cnonce}"',
        ]
    else:
        fields.append(f'response="{_md5(f"{ha1}:{nonce}:{ha2}")}"')

    opaque = challenge.get("opaque")
    if opaque:
        fields.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(fields)


def build_url(base_url: str, path: str, query: Mapping[str, str] | None = None) -> URL:
    """Return the request URL as the object aiohttp sends without requoting."""
    url = URL(base_url).with_path(path)
    return url.with_query(query) if query else url


def request_target(url: URL) -> str:
    """Return the request-target as it appears on the wire, for the digest ``uri`` field.

    aiohttp requotes a pre-encoded URL string, so a digest ``uri`` taken from the
    string would not match the bytes sent. The raw path and raw query are them.
    """
    if url.raw_query_string:
        return f"{url.raw_path}?{url.raw_query_string}"
    return url.raw_path


def remote_path(name: str, prefix: str) -> str:
    """Return the printer-side path of a name that may already be one."""
    if ":" in name or name.startswith("/"):
        return name
    return f"{prefix}{name}"


def quoted_name(name: str) -> str:
    """Return ``name`` when it is safe inside a quoted G-code string.

    A name arrives from the network and is interpolated into G-code that the
    printer runs line by line, so a quote or a control character could end the
    string and start a second command.
    """
    if any(ord(char) < 0x20 or ord(char) == 0x7F or char == '"' for char in name):
        raise ProtocolError(
            "refusing a file name that contains a quote or a control character"
        )
    return name


def session_key(answer: _Answer) -> str | None:
    """Return the session key RRF issued, from its header or from its body."""
    header = answer.headers.get("x-session-key")
    if header:
        return header
    payload = answer.json()
    if isinstance(payload, Mapping):
        value = payload.get("sessionKey")
        if value not in (None, ""):
            return str(value)
    return None


def error_code(answer: _Answer) -> Any | None:
    """Return the ``err`` code from a body that carries a non-zero one."""
    payload = answer.json()
    if not isinstance(payload, Mapping):
        return None
    code = payload.get("err")
    if code is None or _number(code) == 0:
        return None
    return code


# ---------------------------------------------------------------------- scripts


#: ``state.status``, the enum RepRapFirmware documents for the object model. An
#: absent or unrecognised status is not a state this adapter will invent.
STATE_BY_RRF_STATUS: Final[Mapping[str, PrintState]] = MappingProxyType(
    {
        "disconnected": PrintState.UNKNOWN,
        "starting": PrintState.PREPARING,
        "updating": PrintState.PREPARING,
        "off": PrintState.IDLE,
        "halted": PrintState.ERROR,
        "pausing": PrintState.PAUSED,
        "paused": PrintState.PAUSED,
        "resuming": PrintState.PRINTING,
        "cancelling": PrintState.PRINTING,
        "processing": PrintState.PRINTING,
        "simulating": PrintState.PRINTING,
        "busy": PrintState.PREPARING,
        "changingTool": PrintState.PRINTING,
        "idle": PrintState.IDLE,
    }
)

#: The normalised fan channels against the ``P`` index DuetWebControl assigns them.
#: The object model exposes fan values by index and no channel names, and a hotend
#: fan is thermostatic on most Duet boards, so this is DWC's convention rather than
#: something the printer reports.
FAN_CHANNEL_INDEX: Final[Mapping[str, int]] = MappingProxyType(
    {
        "model": 0,
        "hotend": 0,
        "auxiliary": 1,
        "chamber": 2,
        "controller": 3,
    }
)


def _script_pause(_params: Mapping[str, Any]) -> str:
    return "M25"


def _script_resume(_params: Mapping[str, Any]) -> str:
    return "M24"


def _script_stop(_params: Mapping[str, Any]) -> str:
    """Return the cancel sequence.

    ``M0`` alone from a host is refused with "Pause the print before attempting to
    cancel it", so the cancel pauses first.
    """
    return "M25\nM0"


def _script_hotend_temp(params: Mapping[str, Any]) -> str:
    return f"M104 S{params['value']:g}"


def _script_bed_temp(params: Mapping[str, Any]) -> str:
    return f"M140 S{params['value']:g}"


def _script_chamber_temp(params: Mapping[str, Any]) -> str:
    return f"M141 S{params['value']:g}"


def _script_speed(params: Mapping[str, Any]) -> str:
    return f"M220 S{params['value']:g}"


def _script_fan(params: Mapping[str, Any]) -> str:
    """Return the ``M106`` for one fan channel, with the duty scaled to 0 to 255."""
    channel = str(params["channel"])
    index = FAN_CHANNEL_INDEX.get(channel)
    if index is None:
        raise ProtocolError(f"this printer has no {channel} fan channel")
    return f"M106 P{index} S{round(float(params['value']) * 255 / 100)}"


def _script_home(params: Mapping[str, Any]) -> str:
    return f"G28 {params['axes']}"


def _script_jog(params: Mapping[str, Any]) -> str:
    """Return the relative move wrapped in the absolute-mode switches.

    RRF's ``G91`` sets ``axesRelative``, which covers X, Y and Z only. Extrusion is
    governed by the separate ``drivesRelative`` state, so a move that adds an
    extrusion also has to send ``M83``.
    """
    return f"G91\nG1 {params['axis']}{params['distance']:g} F3000\nG90"


def _script_start_print(params: Mapping[str, Any]) -> str:
    return f'M32 "{quoted_name(str(params["filename"]))}"\nM24'


def _script_delete_file(params: Mapping[str, Any]) -> str:
    return f'M30 "{quoted_name(str(params["filename"]))}"'


#: ``Command`` to the script that performs it, sent as one ``rr_gcode`` request.
#: A command with no entry here is one this adapter does not send.
_SCRIPTS: Final[Mapping[Command, Callable[[Mapping[str, Any]], str]]] = MappingProxyType(
    {
        Command.PAUSE: _script_pause,
        Command.RESUME: _script_resume,
        Command.STOP: _script_stop,
        Command.SET_HOTEND_TEMP: _script_hotend_temp,
        Command.SET_BED_TEMP: _script_bed_temp,
        Command.SET_CHAMBER_TEMP: _script_chamber_temp,
        Command.SET_SPEED: _script_speed,
        Command.SET_FAN_SPEED: _script_fan,
        Command.HOME: _script_home,
        Command.JOG: _script_jog,
        Command.START_PRINT: _script_start_print,
        Command.DELETE_FILE: _script_delete_file,
    }
)


class DuetProtocol(Protocol):
    """Standalone RepRapFirmware over HTTP, with Digest auth and a session key."""

    def __init__(
        self,
        config: PrinterConfig,
        session: aiohttp.ClientSession,
        *,
        granted: frozenset[Capability],
        unsafe: tuple[UnsafeFeature, ...] = (),
        models: tuple[ModelProfile, ...] = (),
    ) -> None:
        """Create the adapter and take the password out of the credential mapping."""
        super().__init__(config, session, granted=granted, unsafe=unsafe, models=models)
        self._password = config.credentials.get("password")
        self._challenge: Mapping[str, str] | None = None
        self._session_key: str | None = None
        self._nonce_count = 0

    # ------------------------------------------------------------------ plumbing

    def _authorization(self, challenge: Mapping[str, str], method: str, target: str) -> str:
        """Return a fresh ``Authorization`` header for the cached challenge."""
        self._nonce_count += 1
        # RRF has no username setting of its own and validates the digest for its
        # conventional root user, so a configured username only overrides that.
        username = self._config.credentials.get("username") or "root"
        return digest_header(
            challenge,
            method=method,
            target=target,
            username=username,
            password=self._password or "",
            nonce_count=self._nonce_count,
            cnonce=secrets.token_hex(8),
        )

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        data: aiohttp.FormData | None = None,
    ) -> _Answer:
        """Send one request, answering a Digest challenge with a single transparent retry."""
        url = build_url(self._config.base_url, path, query)
        target = request_target(url)

        for attempt in (0, 1):
            challenge = self._challenge
            headers: dict[str, str] = {}
            if self._session_key is not None:
                headers["X-Session-Key"] = self._session_key
            if challenge is not None:
                headers["Authorization"] = self._authorization(challenge, method, target)
            try:
                async with self._session.request(
                    method, url, headers=headers, data=data, timeout=REQUEST_TIMEOUT
                ) as response:
                    answer = _Answer(
                        status=response.status,
                        headers=MappingProxyType(
                            {key.lower(): value for key, value in response.headers.items()}
                        ),
                        body=await response.read(),
                    )
            except (aiohttp.ClientError, TimeoutError) as err:
                raise UnreachableError(
                    f"cannot reach {self._config.redacted_url}: {err}"
                ) from err

            # The session key is taken before the retry decision, so the digest
            # retry already carries it.
            key = session_key(answer)
            if key is not None:
                self._session_key = key

            if answer.status != 401:
                return answer
            if attempt == 1:
                raise AuthError(
                    f"{self._config.redacted_url} refused the credential (HTTP 401)"
                )
            if self._password is None:
                raise AuthError(
                    f"{self._config.redacted_url} requires a password and none is configured"
                )
            renewed = parse_challenge(answer.headers.get("www-authenticate", ""))
            if renewed is None:
                raise AuthError(
                    f"{self._config.redacted_url} answered HTTP 401 "
                    f"without a usable Digest challenge"
                )
            _LOGGER.debug("%s: retrying with digest authentication", self._config.name)
            self._challenge = renewed
            self._nonce_count = 0

        raise AuthError(f"{self._config.redacted_url} refused the credential")

    def _raise_for_status(self, answer: _Answer) -> None:
        """Classify a non-2xx answer from a request whose body is not parsed."""
        if answer.ok:
            return
        if answer.status in _AUTH_STATUSES:
            raise AuthError(
                f"{self._config.redacted_url} refused the credential (HTTP {answer.status})"
            )
        raise UnreachableError(f"{self._config.redacted_url} answered HTTP {answer.status}")

    # ----------------------------------------------------------------- lifecycle

    async def async_setup(self) -> None:
        """Establish the Digest handshake and the session key with one model read.

        Safe to call again: it repeats the same GET and converges on whatever the
        printer currently grants.
        """
        self._raise_for_status(await self._async_request("GET", MODEL_PATH, query={"key": "state"}))

    async def async_teardown(self) -> None:
        """Drop the local authentication state. The session belongs to the coordinator."""
        self._session_key = None
        self._challenge = None
        self._nonce_count = 0

    # ---------------------------------------------------------------------- read

    async def _async_read(self) -> PrinterSnapshot:
        """Return one snapshot read from the object model."""
        answer = await self._async_request("GET", MODEL_PATH, query={"key": READ_KEY})
        if answer.status == 404:
            raise ProtocolShapeError("this printer does not serve /rr_model")
        self._raise_for_status(answer)

        payload = answer.json()
        if not isinstance(payload, Mapping):
            raise ProtocolShapeError("the /rr_model answer was not a JSON object")
        # A keyed query answers a {"key","flags","result"} envelope, and a request
        # without a key answers the bare model.
        model = payload.get("result", payload)
        if not isinstance(model, Mapping):
            raise ProtocolShapeError("the /rr_model answer carried no object model")
        return parse_model(
            model, protocol=self._config.protocol, capabilities=self.capabilities
        )

    # ------------------------------------------------------------------ commands

    async def _async_dispatch(self, command: Command, params: Mapping[str, Any]) -> None:
        """Build the G-code for ``command`` and send the whole script in one request."""
        build = _SCRIPTS.get(command)
        if build is None:
            raise UnsupportedCommandError(command)

        answer = await self._async_request("GET", GCODE_PATH, query={"gcode": build(params)})
        if answer.status in _AUTH_STATUSES:
            raise AuthError(
                f"{self._config.redacted_url} refused the credential (HTTP {answer.status})"
            )
        if not answer.ok:
            raise CommandRejectedError(
                f"the printer answered HTTP {answer.status} to {command.value}",
                code=answer.status,
            )
        code = error_code(answer)
        if code is not None:
            raise CommandRejectedError(
                f"the printer refused {command.value} with error {code}", code=code
            )

    # --------------------------------------------------------------------- files

    async def async_list_files(self) -> Sequence[FileEntry]:
        """Return the files in the printer's default directory."""
        answer = await self._async_request("GET", FILES_PATH, query={"dir": "0"})
        self._raise_for_status(answer)
        return parse_file_list(answer.text, answer.json())

    async def async_upload_file(
        self, name: str, stream: AsyncIterator[bytes], *, size: int | None = None
    ) -> FileEntry:
        """Stream one file to the printer's G-code directory.

        The remote name travels in the query string because standalone RRF reads the
        destination from there, and the body is a multipart form aiohttp fills from
        the caller's iterator without buffering it.
        """
        remote = remote_path(name, "0:/gcodes/")
        form = aiohttp.FormData()
        form.add_field("file", stream, filename=name, content_type="application/octet-stream")
        answer = await self._async_request(
            "POST", UPLOAD_PATH, query={"name": remote}, data=form
        )
        if not answer.ok:
            raise CommandRejectedError(
                f"the printer answered HTTP {answer.status} to the upload", code=answer.status
            )
        code = error_code(answer)
        if code is not None:
            raise CommandRejectedError(
                f"the printer refused the upload with error {code}", code=code
            )
        return FileEntry(name=name, path=remote, size=size)

    def async_subscribe(self) -> AsyncIterator[PrinterSnapshot] | None:
        """Return ``None``. Standalone RRF has no push channel.

        The server accepts GET, OPTIONS and a POST for ``rr_upload`` with no upgrade
        path, so polling is the whole of it. An SBC board running DSF pushes the
        model over ``ws://<host>/machine?sessionKey=<key>``, which is where an SBC
        adapter would subscribe.
        """
        return None

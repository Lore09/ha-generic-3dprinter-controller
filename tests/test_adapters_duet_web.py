"""Adapter tests for the Duet and web-only protocols over real sockets.

Aiohttp is not mocked anywhere here. Every test stands up a real
``web.Application`` on an ephemeral loopback port and drives the adapter through
a real ``aiohttp.ClientSession``, because the two things that can most easily go
wrong in these adapters are wire-level: the digest ``uri`` must equal the
request-target aiohttp actually sends, and the session key travels in a response
header that only a real response exposes.

The fake printer answers the RepRapFirmware ``rr_*`` shapes and records every
request it received, so the command assertions read the G-code that left the
socket rather than the string a builder returned.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from custom_components.generic_3dprinter import registry
from custom_components.generic_3dprinter.adapters.duet import _SCRIPTS, DuetProtocol
from custom_components.generic_3dprinter.adapters.web_only import WebOnlyProtocol
from custom_components.generic_3dprinter.const import (
    Capability,
    Command,
    PrintState,
    ProtocolId,
)
from custom_components.generic_3dprinter.models import FileEntry
from custom_components.generic_3dprinter.protocols import (
    AuthError,
    PrinterConfig,
    ProtocolError,
    ProtocolShapeError,
    UnreachableError,
    UnsupportedCommandError,
    command_capability,
)

pytestmark = pytest.mark.asyncio

#: The Duet registration in ``registry.py``, inlined because the registry drops a
#: whole protocol when any sibling adapter module is absent from the package.
DUET_CAPABILITIES = frozenset(
    {
        Capability.START_PRINT,
        Capability.PAUSE,
        Capability.RESUME,
        Capability.STOP,
        Capability.SET_HOTEND_TEMP,
        Capability.SET_BED_TEMP,
        Capability.SET_CHAMBER_TEMP,
        Capability.SET_FAN_SPEED,
        Capability.SET_SPEED,
        Capability.HOME,
        Capability.JOG,
        Capability.FILE_LIST,
        Capability.FILE_UPLOAD,
        Capability.FILE_DELETE,
        Capability.WEB_UI,
    }
)

WEB_ONLY_CAPABILITIES = frozenset({Capability.WEB_UI})


def _registration(protocol: ProtocolId) -> Any:
    """Return the registry entry for ``protocol``, or ``None`` when it is dropped."""
    return registry.ADAPTERS.get(protocol)


def _granted(protocol: ProtocolId, fallback: frozenset[Capability]) -> frozenset[Capability]:
    """Prefer the registry's capability set and fall back when it drops the protocol."""
    registration = _registration(protocol)
    return registration.capabilities if registration is not None else fallback


DUET_GRANTED = _granted(ProtocolId.DUET, DUET_CAPABILITIES)
WEB_ONLY_GRANTED = _granted(ProtocolId.WEB_ONLY, WEB_ONLY_CAPABILITIES)


def _md5(value: str) -> str:
    """Return the hex MD5 of ``value``, matching the digest scheme RRF uses."""
    return hashlib.md5(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """One request the fake printer answered, as it arrived on the wire."""

    method: str
    target: str
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class RecordedUpload:
    """One multipart upload the fake printer received."""

    name: str | None
    filename: str | None
    content_type: str | None
    payload: bytes


def default_model() -> dict[str, Any]:
    """Return an object-model result in the shape RepRapFirmware answers with.

    The bed sits at heater index 0 and the nozzle at index 1, and only
    ``tools[0].heaters`` names the nozzle, so any index-0 assumption reads the bed
    temperature into the hotend and fails.
    """
    return {
        "state": {"status": "processing"},
        "heat": {
            "heaters": [
                {"current": 21.0, "active": 0.0},
                {"current": 205.5, "active": 210.0},
            ],
            "bedHeaters": [0],
            "chamberHeaters": [],
        },
        "job": {
            "filePosition": 12500.0,
            "duration": 3725.0,
            "layer": 42,
            "file": {"size": 50000, "fileName": "cube.gcode", "numLayers": 168},
            "timesLeft": {"file": 900.0},
        },
        "move": {
            "axes": [
                {"letter": "X", "userPosition": 12.5, "homed": True},
                {"letter": "Y", "userPosition": -4.25, "homed": True},
                {"letter": "Z", "userPosition": 0.4, "homed": False},
            ]
        },
        "tools": [{"heaters": [1]}],
        "fans": [{"value": 0.6}, {"value": 1.0, "thermostatic": True}],
    }


class FakePrinter:
    """A real aiohttp application that answers the ``rr_*`` endpoints."""

    def __init__(self, *, digest: bool = False, password: str = "secret") -> None:
        """Build the application state a test then mutates before starting it."""
        self.model: dict[str, Any] = default_model()
        self.envelope = True
        self.model_status = 200
        self.model_payload: Any = None
        self.files_body = "cube.gcode\nbench.gcode\n"
        self.files_content_type = "text/plain"
        self.gcode_status = 200
        self.gcode_error: int | None = None
        self.upload_error: int | None = None
        self.web_status = 200
        self.digest = digest
        self.password = password
        self.username = "root"
        self.realm = "reprap"
        self.nonce = "c0ffee1234"
        self.session_key = "4f8a1c0d"
        self.requests: list[RecordedRequest] = []
        self.gcode_requests: list[str] = []
        self.gcode_lines: list[str] = []
        self.uploads: list[RecordedUpload] = []

    def build_app(self) -> web.Application:
        """Return the application with every endpoint the adapters call."""
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self.handle_web),
                web.get("/rr_model", self.handle_model),
                web.get("/rr_gcode", self.handle_gcode),
                web.get("/rr_files", self.handle_files),
                web.post("/rr_upload", self.handle_upload),
            ]
        )
        return app

    def challenge(self) -> str:
        """Return an RRF-style Digest challenge with one extra parameter."""
        return (
            f'Digest realm="{self.realm}", qop="auth", nonce="{self.nonce}", '
            f'opaque="opaque-value", algorithm=MD5'
        )

    def _record(self, request: web.Request) -> None:
        self.requests.append(
            RecordedRequest(
                method=request.method,
                target=request.raw_path,
                headers={key.lower(): value for key, value in request.headers.items()},
            )
        )

    def _refused(self) -> web.Response:
        """Answer 401, with a challenge only when this printer asks for digest."""
        headers = {"X-Session-Key": self.session_key}
        if self.digest:
            headers["WWW-Authenticate"] = self.challenge()
        return web.Response(status=401, headers=headers, text="authentication required")

    def _authorised(self, request: web.Request) -> bool:
        """Verify a Digest response independently, against the real request target."""
        if not self.digest:
            return True
        header = request.headers.get("Authorization", "")
        if not header.startswith("Digest "):
            return False
        params = _parse_digest_header(header)
        if params.get("uri") != request.raw_path:
            return False
        if params.get("nonce") != self.nonce or params.get("username") != self.username:
            return False
        ha1 = _md5(f"{self.username}:{self.realm}:{self.password}")
        ha2 = _md5(f"{request.method}:{request.raw_path}")
        if params.get("qop") == "auth":
            expected = _md5(
                f"{ha1}:{self.nonce}:{params.get('nc', '')}:"
                f"{params.get('cnonce', '')}:auth:{ha2}"
            )
        else:
            expected = _md5(f"{ha1}:{self.nonce}:{ha2}")
        return params.get("response") == expected

    async def handle_web(self, request: web.Request) -> web.Response:
        """Answer the printer's own page with whatever status the test set."""
        self._record(request)
        return web.Response(status=self.web_status, text="printer page")

    async def handle_model(self, request: web.Request) -> web.Response:
        """Answer the object model, wrapped in the real RRF envelope by default."""
        self._record(request)
        if not self._authorised(request):
            return self._refused()
        if self.model_status != 200:
            return web.Response(status=self.model_status, text="model unavailable")
        if self.model_payload is not None:
            return web.Response(
                body=json.dumps(self.model_payload), content_type="application/json"
            )
        body: Any = self.model
        if self.envelope:
            body = {
                "key": request.query.get("key", ""),
                "flags": "",
                "result": self.model,
            }
        return web.json_response(body, headers={"X-Session-Key": self.session_key})

    async def handle_gcode(self, request: web.Request) -> web.Response:
        """Record the G-code script and answer the RRF buffer-space body."""
        self._record(request)
        if not self._authorised(request):
            return self._refused()
        script = request.query.get("gcode", "")
        self.gcode_requests.append(script)
        self.gcode_lines.extend(line for line in script.split("\n") if line)
        if self.gcode_status != 200:
            return web.Response(status=self.gcode_status, text="rejected")
        if self.gcode_error is not None:
            return web.json_response({"err": self.gcode_error})
        return web.json_response({"buff": 3}, headers={"X-Session-Key": self.session_key})

    async def handle_files(self, request: web.Request) -> web.Response:
        """Answer the shape the test configured, JSON or newline separated."""
        self._record(request)
        return web.Response(text=self.files_body, content_type=self.files_content_type)

    async def handle_upload(self, request: web.Request) -> web.Response:
        """Read the multipart body and record the destination and the bytes."""
        self._record(request)
        posted = await request.post()
        upload = posted.get("file")
        payload = b""
        filename: str | None = None
        if upload is not None:
            if hasattr(upload, "file"):
                payload = upload.file.read()
                filename = upload.filename
            else:
                payload = str(upload).encode()
        self.uploads.append(
            RecordedUpload(
                name=request.query.get("name"),
                filename=filename,
                content_type=request.content_type,
                payload=payload,
            )
        )
        if self.upload_error is not None:
            return web.json_response({"err": self.upload_error})
        return web.json_response({"err": 0}, headers={"X-Session-Key": self.session_key})


#: A digest parameter is quoted or bare, and a quoted value may contain a comma.
#: The request target does: ``/rr_model?key=state,heat,job,move,tools,fans``.
_DIGEST_PAIR = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))')


def _parse_digest_header(header: str) -> dict[str, str]:
    """Return the parameters of a Digest Authorization header, unquoted."""
    return {
        match.group(1).lower(): (
            match.group(2) if match.group(2) is not None else match.group(3)
        )
        for match in _DIGEST_PAIR.finditer(header)
    }


@asynccontextmanager
async def printer_server(printer: FakePrinter) -> AsyncIterator[str]:
    """Run ``printer`` on an ephemeral loopback port and yield its base URL."""
    runner = web.AppRunner(printer.build_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """Return a real aiohttp session carrying the adapter's production timeout."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as created:
        yield created


def duet_config(base_url: str, *, password: str | None = "secret") -> PrinterConfig:
    """Build the configuration a Duet entry would carry."""
    parts = urlsplit(base_url)
    return PrinterConfig(
        name="duet",
        protocol=ProtocolId.DUET,
        host=parts.hostname or "127.0.0.1",
        port=parts.port,
        credentials={"password": password} if password else {},
    )


def web_config(base_url: str) -> PrinterConfig:
    """Build the configuration a web-only entry would carry."""
    parts = urlsplit(base_url)
    return PrinterConfig(
        name="web",
        protocol=ProtocolId.WEB_ONLY,
        host=parts.hostname or "127.0.0.1",
        port=parts.port,
    )


def duet_adapter(config: PrinterConfig, session: aiohttp.ClientSession) -> DuetProtocol:
    """Construct the Duet adapter with the granted capability set."""
    return DuetProtocol(config, session, granted=DUET_GRANTED, unsafe=())


def web_adapter(config: PrinterConfig, session: aiohttp.ClientSession) -> WebOnlyProtocol:
    """Construct the web-only adapter with the granted capability set."""
    return WebOnlyProtocol(config, session, granted=WEB_ONLY_GRANTED, unsafe=())


def closed_port() -> int:
    """Return a loopback port with nothing listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _stream(payload: bytes) -> AsyncIterator[bytes]:
    """Yield ``payload`` as the one-chunk async iterator an upload receives."""
    yield payload


async def test_the_inlined_capability_set_matches_the_registry() -> None:
    """The fallback set is the registration's set, so it cannot drift unnoticed."""
    registration = _registration(ProtocolId.DUET)
    if registration is None:
        pytest.skip("registry drops DUET while a sibling adapter module is absent")
    assert registration.capabilities == DUET_CAPABILITIES


async def test_every_granted_capability_has_a_script_and_nothing_else_does() -> None:
    """The script table and the granted set agree in both directions.

    A capability granted without a script is a command that passes the base
    class guard and then falls through the dispatch lookup, and a script for a
    capability the printer does not grant is dead code.
    """
    dispatched = frozenset(command_capability(command) for command in _SCRIPTS)
    assert dispatched == DUET_CAPABILITIES - {
        Capability.FILE_LIST,
        Capability.FILE_UPLOAD,
        Capability.WEB_UI,
    }


# --------------------------------------------------------------- assertion 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("processing", PrintState.PRINTING),
        ("idle", PrintState.IDLE),
        ("paused", PrintState.PAUSED),
        ("halted", PrintState.ERROR),
        ("pausing", PrintState.PAUSED),
        ("changingTool", PrintState.PRINTING),
        ("warmingUp", PrintState.UNKNOWN),
    ],
)
async def test_print_state_maps_the_rrf_status(
    status: str, expected: PrintState, session: aiohttp.ClientSession
) -> None:
    """Every RRF status string lands on its normalised state, unknown included."""
    printer = FakePrinter()
    printer.model["state"]["status"] = status
    async with printer_server(printer) as base_url:
        snapshot = await duet_adapter(duet_config(base_url), session).async_read()
    assert snapshot.print_state is expected


async def test_print_state_is_unknown_when_status_is_absent(
    session: aiohttp.ClientSession,
) -> None:
    """A missing status is not idle, it is unreadable."""
    printer = FakePrinter()
    del printer.model["state"]["status"]
    async with printer_server(printer) as base_url:
        snapshot = await duet_adapter(duet_config(base_url), session).async_read()
    assert snapshot.print_state is PrintState.UNKNOWN


# --------------------------------------------------------------- assertion 2


async def test_progress_is_computed_from_the_file_position(
    session: aiohttp.ClientSession,
) -> None:
    """12500 of 50000 is 25 percent, and no RRF field reports it."""
    printer = FakePrinter()
    async with printer_server(printer) as base_url:
        snapshot = await duet_adapter(duet_config(base_url), session).async_read()
    assert snapshot.progress == 25.0


async def test_progress_is_clamped_to_one_hundred(session: aiohttp.ClientSession) -> None:
    """A position past the file size cannot publish more than 100 percent."""
    printer = FakePrinter()
    printer.model["job"]["filePosition"] = 75000.0
    async with printer_server(printer) as base_url:
        snapshot = await duet_adapter(duet_config(base_url), session).async_read()
    assert snapshot.progress == 100.0


# --------------------------------------------------------------- assertion 3


async def test_heaters_are_read_by_index_not_by_position(
    session: aiohttp.ClientSession,
) -> None:
    """The bed is heater 0 and the nozzle is heater 1, named by tools[0].heaters."""
    printer = FakePrinter()
    async with printer_server(printer) as base_url:
        snapshot = await duet_adapter(duet_config(base_url), session).async_read()
    assert snapshot.hotend.current == 205.5
    assert snapshot.hotend.target == 210.0
    assert snapshot.bed.current == 21.0
    assert snapshot.bed.target == 0.0
    assert snapshot.chamber.current is None


async def test_the_hotend_falls_back_when_no_tool_names_a_heater(
    session: aiohttp.ClientSession,
) -> None:
    """Without a tool mapping the hotend is the lowest non-bed, non-chamber heater."""
    printer = FakePrinter()
    printer.model["tools"] = []
    printer.model["heat"]["heaters"].append({"current": 33.0, "active": 0.0})
    async with printer_server(printer) as base_url:
        snapshot = await duet_adapter(duet_config(base_url), session).async_read()
    assert snapshot.hotend.current == 205.5


# --------------------------------------------------------------- assertion 4


@pytest.mark.parametrize(
    "file_entry",
    [{"size": 0, "fileName": "cube.gcode"}, {"fileName": "cube.gcode"}],
)
async def test_progress_is_none_without_a_usable_file_size(
    file_entry: dict[str, Any], session: aiohttp.ClientSession
) -> None:
    """A zero size and an absent size both leave progress unset, never divided."""
    printer = FakePrinter()
    printer.model["job"]["file"] = file_entry
    async with printer_server(printer) as base_url:
        snapshot = await duet_adapter(duet_config(base_url), session).async_read()
    assert snapshot.progress is None


# --------------------------------------------------------------- assertion 5


async def test_fans_are_scaled_and_thermostatic_maps_to_the_hotend(
    session: aiohttp.ClientSession,
) -> None:
    """0.6 of full duty is 60 percent, and a thermostatic fan is the hotend fan."""
    printer = FakePrinter()
    async with printer_server(printer) as base_url:
        snapshot = await duet_adapter(duet_config(base_url), session).async_read()
    assert snapshot.fans.model == 60.0
    assert snapshot.fans.hotend == 100.0
    assert snapshot.fans.auxiliary is None
    assert snapshot.fans.chamber is None
    assert snapshot.fans.controller is None


@pytest.mark.parametrize(
    ("fan", "model", "hotend"),
    [
        ({"value": -1.0}, None, None),
        ({"actualValue": 0.25}, 25.0, None),
        ({"value": 0.5, "name": "Hotend Heater Fan"}, 50.0, 50.0),
    ],
)
async def test_fan_fallbacks_and_unknown_values(
    fan: dict[str, Any], model: float | None, hotend: float | None, session: aiohttp.ClientSession
) -> None:
    """The actualValue fallback, the -1 unknown marker, and the name heuristic."""
    printer = FakePrinter()
    printer.model["fans"] = [fan]
    async with printer_server(printer) as base_url:
        snapshot = await duet_adapter(duet_config(base_url), session).async_read()
    assert snapshot.fans.model == model
    assert snapshot.fans.hotend == hotend


# --------------------------------------------------------------- assertion 6


async def test_stop_pauses_before_it_cancels(session: aiohttp.ClientSession) -> None:
    """M0 alone is refused by RRF unless the job is paused first."""
    printer = FakePrinter()
    async with printer_server(printer) as base_url:
        await duet_adapter(duet_config(base_url), session).async_send(Command.STOP)
    assert printer.gcode_lines == ["M25", "M0"]
    assert printer.gcode_requests == ["M25\nM0"]


# --------------------------------------------------------------- assertion 7


@pytest.mark.parametrize(
    ("axis", "distance", "move"),
    [("X", 10.0, "G1 X10 F3000"), ("Y", -2.5, "G1 Y-2.5 F3000"), ("Z", 0.4, "G1 Z0.4 F3000")],
)
async def test_jog_is_relative_then_absolute(
    axis: str, distance: float, move: str, session: aiohttp.ClientSession
) -> None:
    """A jog brackets the move in relative mode and restores absolute mode."""
    printer = FakePrinter()
    printer.model["state"]["status"] = "idle"
    async with printer_server(printer) as base_url:
        await duet_adapter(duet_config(base_url), session).async_send(
            Command.JOG, axis=axis, distance=distance
        )
    assert printer.gcode_lines == ["G91", move, "G90"]
    assert len(printer.gcode_requests) == 1


# --------------------------------------------------------------- assertion 8


@pytest.mark.parametrize("web_status", [200, 401, 500])
async def test_web_only_treats_any_answer_as_connected(
    web_status: int, session: aiohttp.ClientSession
) -> None:
    """A page that demands a password or fails still proves the host is alive."""
    printer = FakePrinter()
    printer.web_status = web_status
    async with printer_server(printer) as base_url:
        adapter = web_adapter(web_config(base_url), session)
        await adapter.async_setup()
        snapshot = await adapter.async_read()
    assert snapshot.connected is True
    assert snapshot.protocol is ProtocolId.WEB_ONLY


# --------------------------------------------------------------- assertion 9


async def test_web_only_is_unreachable_when_nothing_listens(
    session: aiohttp.ClientSession,
) -> None:
    """A refused connection is the only thing that means unreachable for a page."""
    adapter = web_adapter(web_config(f"http://127.0.0.1:{closed_port()}"), session)
    with pytest.raises(UnreachableError):
        await adapter.async_setup()
    assert (await adapter.async_read()).connected is False


async def test_duet_is_unreachable_when_nothing_listens(
    session: aiohttp.ClientSession,
) -> None:
    """The Duet adapter turns a refused connection into UnreachableError too."""
    adapter = duet_adapter(duet_config(f"http://127.0.0.1:{closed_port()}"), session)
    with pytest.raises(UnreachableError):
        await adapter.async_read()


# -------------------------------------------------------------- assertion 10


async def test_web_only_snapshot_claims_nothing_it_cannot_know(
    session: aiohttp.ClientSession,
) -> None:
    """Every field a web page cannot report stays None, and state stays unknown."""
    printer = FakePrinter()
    async with printer_server(printer) as base_url:
        adapter = web_adapter(web_config(base_url), session)
        snapshot = await adapter.async_read()
    assert snapshot.print_state is PrintState.UNKNOWN
    assert snapshot.current_layer is None
    assert snapshot.progress is None
    assert snapshot.hotend.current is None
    assert snapshot.filename is None
    assert snapshot.position is None
    assert snapshot.bed.current is None
    assert snapshot.capabilities == WEB_ONLY_GRANTED
    assert await adapter.async_list_files() == []


async def test_web_only_refuses_every_command_it_does_not_have() -> None:
    """The dispatch method is explicit, and the file API does not exist."""
    printer = FakePrinter()
    async with printer_server(printer) as base_url, aiohttp.ClientSession() as session:
        adapter = web_adapter(web_config(base_url), session)
        with pytest.raises(UnsupportedCommandError):
            await adapter._async_dispatch(Command.PAUSE, {})
        with pytest.raises(UnsupportedCommandError):
            await adapter.async_send(Command.PAUSE)
        with pytest.raises(UnreachableError, match="no file API"):
            await adapter.async_upload_file("cube.gcode", _stream(b"x"))
        await adapter.async_teardown()
        await adapter.async_teardown()


# -------------------------------------------------------------- assertion 11


async def test_digest_round_trip_reads_state_and_reuses_the_session_key(
    session: aiohttp.ClientSession,
) -> None:
    """One challenge, one retry, then the session key and digest travel on every call."""
    printer = FakePrinter(digest=True)
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        await adapter.async_setup()
        snapshot = await adapter.async_read()

    assert snapshot.print_state is PrintState.PRINTING
    assert snapshot.bed.current == 21.0
    assert [request.target.split("?")[0] for request in printer.requests] == [
        "/rr_model",
        "/rr_model",
        "/rr_model",
    ]
    assert printer.requests[0].headers.get("authorization") is None
    assert all(
        request.headers.get("authorization", "").startswith("Digest ")
        for request in printer.requests[1:]
    )
    assert all(
        request.headers.get("x-session-key") == printer.session_key
        for request in printer.requests[1:]
    )


async def test_digest_with_a_wrong_password_raises_auth_error(
    session: aiohttp.ClientSession,
) -> None:
    """A 401 on the authenticated retry is the credential being refused."""
    printer = FakePrinter(digest=True, password="secret")
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url, password="wrong"), session)
        with pytest.raises(AuthError):
            await adapter.async_read()
    assert len(printer.requests) == 2


async def test_a_401_without_a_challenge_raises_auth_error(
    session: aiohttp.ClientSession,
) -> None:
    """Nothing to answer means the credential cannot be negotiated."""
    printer = FakePrinter()
    printer.model_status = 401
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        with pytest.raises(AuthError):
            await adapter.async_read()


async def test_a_403_refusal_raises_auth_error(session: aiohttp.ClientSession) -> None:
    """A refusal is an auth failure, not an unreachable host."""
    printer = FakePrinter()
    printer.model_status = 403
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        with pytest.raises(AuthError):
            await adapter.async_read()


# ------------------------------------------------------- remaining behaviour


async def test_the_bare_model_without_the_envelope_is_accepted(
    session: aiohttp.ClientSession,
) -> None:
    """A standalone RRF may answer the model itself, so both shapes are unwrapped."""
    printer = FakePrinter()
    printer.envelope = False
    async with printer_server(printer) as base_url:
        snapshot = await duet_adapter(duet_config(base_url), session).async_read()
    assert snapshot.print_state is PrintState.PRINTING
    assert snapshot.filename == "cube.gcode"


async def test_a_missing_object_model_is_a_shape_error(
    session: aiohttp.ClientSession,
) -> None:
    """The keyed model endpoint answering 404 means the surface is not RRF."""
    printer = FakePrinter()
    printer.model_status = 404
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        with pytest.raises(ProtocolShapeError):
            await adapter.async_read()


async def test_a_server_error_is_unreachable(session: aiohttp.ClientSession) -> None:
    """A non-404 failure is the host failing, not a shape this adapter cannot read."""
    printer = FakePrinter()
    printer.model_status = 500
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        with pytest.raises(UnreachableError):
            await adapter.async_read()


async def test_a_non_object_body_is_a_shape_error(session: aiohttp.ClientSession) -> None:
    """A JSON array where the object model belongs cannot be parsed."""
    printer = FakePrinter()
    printer.model_payload = ["not", "an", "object"]
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        with pytest.raises(ProtocolShapeError):
            await adapter.async_read()


async def test_the_job_and_position_fields_are_normalised(
    session: aiohttp.ClientSession,
) -> None:
    """Layer, filename, elapsed, remaining, position and homed axes all arrive."""
    printer = FakePrinter()
    async with printer_server(printer) as base_url:
        snapshot = await duet_adapter(duet_config(base_url), session).async_read()
    assert snapshot.filename == "cube.gcode"
    assert snapshot.current_layer == 42
    assert snapshot.total_layers == 168
    assert snapshot.elapsed == 3725.0
    assert snapshot.remaining == 900.0
    assert snapshot.position is not None
    assert (snapshot.position.x, snapshot.position.y, snapshot.position.z) == (12.5, -4.25, 0.4)
    assert snapshot.homed_axes == frozenset({"X", "Y"})
    assert snapshot.connected is True
    assert snapshot.protocol is ProtocolId.DUET
    assert snapshot.capabilities == DUET_GRANTED
    # Fields the object model does not expose are left unset rather than guessed.
    assert snapshot.speed_factor is None
    assert snapshot.flow_factor is None
    assert snapshot.job_id is None
    assert snapshot.model is None
    assert snapshot.firmware is None
    assert snapshot.serial is None
    assert snapshot.lights == frozenset()
    assert snapshot.camera is False
    assert snapshot.errors == ()


@pytest.mark.parametrize(
    ("command", "params", "lines"),
    [
        (Command.PAUSE, {}, ["M25"]),
        (Command.RESUME, {}, ["M24"]),
        (Command.SET_HOTEND_TEMP, {"value": 210.0}, ["M104 S210"]),
        (Command.SET_BED_TEMP, {"value": 60.0}, ["M140 S60"]),
        (Command.SET_CHAMBER_TEMP, {"value": 40.0}, ["M141 S40"]),
        (Command.SET_SPEED, {"value": 100.0}, ["M220 S100"]),
        (Command.SET_FAN_SPEED, {"value": 60.0, "channel": "model"}, ["M106 P0 S153"]),
        (Command.SET_FAN_SPEED, {"value": 100.0, "channel": "chamber"}, ["M106 P2 S255"]),
        (Command.HOME, {"axes": "XY"}, ["G28 XY"]),
        (Command.START_PRINT, {"filename": "cube.gcode"}, ['M32 "cube.gcode"', "M24"]),
        (Command.DELETE_FILE, {"filename": "cube.gcode"}, ['M30 "cube.gcode"']),
    ],
)
async def test_command_scripts(
    command: Command, params: dict[str, Any], lines: list[str], session: aiohttp.ClientSession
) -> None:
    """Every granted command leaves the socket as the script RRF expects."""
    printer = FakePrinter()
    # Idle, so homing and starting a print are not refused as busy.
    printer.model["state"]["status"] = "idle"
    async with printer_server(printer) as base_url:
        await duet_adapter(duet_config(base_url), session).async_send(command, **params)
    assert printer.gcode_lines == lines
    assert len(printer.gcode_requests) == 1


@pytest.mark.parametrize(
    "filename",
    ['e"vil.gcode', "evil\ngcode.gcode", "evil\x00name.gcode", "evil\tname.gcode"],
)
async def test_a_filename_that_could_inject_gcode_is_refused(
    filename: str, session: aiohttp.ClientSession
) -> None:
    """A quote or control character in a network filename never reaches the wire."""
    printer = FakePrinter()
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        for command in (Command.START_PRINT, Command.DELETE_FILE):
            with pytest.raises(ProtocolError):
                await adapter.async_send(command, filename=filename)
    assert printer.gcode_requests == []


async def test_a_command_with_no_script_is_unsupported(
    session: aiohttp.ClientSession,
) -> None:
    """SET_FLOW is absent from both the capability set and the script table."""
    printer = FakePrinter()
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        with pytest.raises(UnsupportedCommandError):
            await adapter.async_send(Command.SET_FLOW, value=100.0)
        with pytest.raises(UnsupportedCommandError):
            await adapter._async_dispatch(Command.SET_FLOW, {})
    assert printer.gcode_requests == []


async def test_a_refused_gcode_script_is_rejected(session: aiohttp.ClientSession) -> None:
    """An HTTP failure and an err field are both refusals, with the code carried."""
    printer = FakePrinter()
    printer.gcode_status = 500
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        with pytest.raises(ProtocolError):
            await adapter.async_send(Command.PAUSE)

    printer = FakePrinter()
    printer.gcode_error = 7
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        with pytest.raises(ProtocolError) as failure:
            await adapter.async_send(Command.PAUSE)
    assert getattr(failure.value, "code", None) == 7


async def test_list_files_parses_the_json_shape(session: aiohttp.ClientSession) -> None:
    """A JSON array of objects keeps the stored path, the size and the date."""
    printer = FakePrinter()
    printer.files_content_type = "application/json"
    printer.files_body = json.dumps(
        [
            {"name": "cube.gcode", "size": 1234, "date": "2026-02-03T04:05:06"},
            {"name": "0:/gcodes/bench.gcode", "size": 5678},
        ]
    )
    async with printer_server(printer) as base_url:
        entries = await duet_adapter(duet_config(base_url), session).async_list_files()
    assert [entry.name for entry in entries] == ["cube.gcode", "0:/gcodes/bench.gcode"]
    assert entries[0].path == "0:/cube.gcode"
    assert entries[0].size == 1234
    assert entries[0].modified is not None
    assert entries[0].modified.replace(tzinfo=None) == datetime(2026, 2, 3, 4, 5, 6)
    assert entries[1].path == "0:/gcodes/bench.gcode"
    assert entries[1].size == 5678
    assert entries[1].modified is None


async def test_list_files_parses_the_newline_shape(session: aiohttp.ClientSession) -> None:
    """A text body keeps every non-blank line and drops the blank ones."""
    printer = FakePrinter()
    printer.files_body = "cube.gcode\n\nbench.gcode\n"
    async with printer_server(printer) as base_url:
        entries = await duet_adapter(duet_config(base_url), session).async_list_files()
    assert [(entry.name, entry.path, entry.size) for entry in entries] == [
        ("cube.gcode", "0:/cube.gcode", None),
        ("bench.gcode", "0:/bench.gcode", None),
    ]


async def test_upload_streams_the_file_to_the_named_destination(
    session: aiohttp.ClientSession,
) -> None:
    """The bytes arrive as one multipart field and the path travels in the query."""
    payload = b"G28\nG1 X10 F3000\n"
    printer = FakePrinter()
    async with printer_server(printer) as base_url:
        entry = await duet_adapter(duet_config(base_url), session).async_upload_file(
            "cube.gcode", _stream(payload), size=len(payload)
        )
    upload = printer.uploads[0]
    assert upload.name == "0:/gcodes/cube.gcode"
    assert upload.filename == "cube.gcode"
    assert upload.content_type == "multipart/form-data"
    assert upload.payload == payload
    assert entry == FileEntry(name="cube.gcode", path="0:/gcodes/cube.gcode", size=17)


async def test_upload_keeps_an_absolute_destination(session: aiohttp.ClientSession) -> None:
    """A name that already carries storage is used as the remote path."""
    printer = FakePrinter()
    async with printer_server(printer) as base_url:
        entry = await duet_adapter(duet_config(base_url), session).async_upload_file(
            "0:/gcodes/nested/cube.gcode", _stream(b"x")
        )
    assert printer.uploads[0].name == "0:/gcodes/nested/cube.gcode"
    assert entry.path == "0:/gcodes/nested/cube.gcode"


async def test_upload_refusal_is_rejected(session: aiohttp.ClientSession) -> None:
    """A non-zero err on the upload is a refusal carrying the printer's code."""
    printer = FakePrinter()
    printer.upload_error = 4
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        with pytest.raises(ProtocolError) as failure:
            await adapter.async_upload_file("cube.gcode", _stream(b"x"))
    assert getattr(failure.value, "code", None) == 4


async def test_setup_is_idempotent_and_caches_the_challenge(
    session: aiohttp.ClientSession,
) -> None:
    """A second setup reuses the cached challenge, so it costs one request."""
    printer = FakePrinter(digest=True)
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        await adapter.async_setup()
        await adapter.async_setup()
    assert len(printer.requests) == 3
    assert printer.requests[0].headers.get("authorization") is None
    assert all(
        request.headers.get("authorization", "").startswith("Digest ")
        for request in printer.requests[1:]
    )


async def test_teardown_drops_the_auth_state_and_keeps_the_shared_session(
    session: aiohttp.ClientSession,
) -> None:
    """The coordinator owns the session, so teardown only drops local auth state."""
    printer = FakePrinter(digest=True)
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        await adapter.async_setup()
        await adapter.async_read()
        await adapter.async_teardown()
        await adapter.async_teardown()
        await adapter.async_read()
    assert session.closed is False
    assert len(printer.requests) == 5
    # The read after teardown has to negotiate again, which is the proof the cached
    # challenge and the session key were both dropped.
    assert printer.requests[3].headers.get("authorization") is None
    assert printer.requests[4].headers.get("authorization", "").startswith("Digest ")


async def test_subscribe_is_absent_because_standalone_rrf_has_no_push(
    session: aiohttp.ClientSession,
) -> None:
    """The poll-only surface reports no push iterator."""
    printer = FakePrinter()
    async with printer_server(printer) as base_url:
        adapter = duet_adapter(duet_config(base_url), session)
        assert adapter.async_subscribe() is None

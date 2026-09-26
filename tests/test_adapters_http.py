"""Adapter tests against a real loopback HTTP server.

Every body here is the recorded wire shape from `protocol-research.md`, and every
request crosses a real socket, because parsing that wire format and mapping its
failures is the whole job of an adapter.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

import aiohttp
import pytest
from aiohttp import web

from custom_components.generic_3dprinter.adapters.moonraker import MoonrakerProtocol
from custom_components.generic_3dprinter.adapters.octoprint import OctoPrintProtocol
from custom_components.generic_3dprinter.const import (
    Capability,
    Command,
    PrintState,
    ProtocolId,
)
from custom_components.generic_3dprinter.protocols import (
    CommandBlockedError,
    AuthError,
    PrinterConfig,
    ProtocolError,
    UnreachableError,
    UnsupportedCommandError,
    parse_config,
)

API_KEY = "recorded-key"
OTHER_KEY = "wrong-key"

#: Mirrors `registry.ADAPTERS`, which cannot be imported while the other adapters
#: are still unwritten.
MOONRAKER_CAPABILITIES: Final = frozenset(
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
        Capability.SET_FLOW,
        Capability.HOME,
        Capability.JOG,
        Capability.FILE_LIST,
        Capability.FILE_UPLOAD,
        Capability.FILE_DELETE,
        Capability.CAMERA,
    }
)
OCTOPRINT_CAPABILITIES: Final = frozenset(
    {
        Capability.START_PRINT,
        Capability.PAUSE,
        Capability.RESUME,
        Capability.STOP,
        Capability.SET_HOTEND_TEMP,
        Capability.SET_BED_TEMP,
        Capability.SET_FAN_SPEED,
        Capability.SET_SPEED,
        Capability.SET_FLOW,
        Capability.HOME,
        Capability.JOG,
        Capability.FILE_LIST,
        Capability.FILE_UPLOAD,
        Capability.FILE_DELETE,
        Capability.CAMERA,
        Capability.WEB_UI,
    }
)

# ------------------------------------------------------------------ recorded
#
# Moonraker, from `## 1. Klipper via Moonraker`: the object query envelope, the
# per-object fields Klipper reports, and `/machine/system_info`.
#
# OctoPrint, from `## 2. OctoPrint`: `/api/printer`, `/api/job`, and the file
# listing. The firmware string is absent on purpose: neither protocol exposes one.

MOONRAKER_OBJECTS: Final[dict[str, Any]] = {
    "eventtime": 1789656234.5,
    "status": {
        "print_stats": {
            "filename": "benchy.gcode",
            "total_duration": 620.0,
            "print_duration": 300.0,
            "filament_used": 1234.5,
            "state": "printing",
            "message": "",
            "info": {"total_layer": 200, "current_layer": 50},
        },
        "heater_bed": {"temperature": 59.8, "target": 60.0},
        "extruder": {"temperature": 219.5, "target": 220.0},
        "virtual_sdcard": {"file_path": "benchy.gcode", "progress": 0.24, "is_active": True},
        "display_status": {"progress": 0.25, "message": None},
        "gcode_move": {"speed_factor": 1.05, "extrude_factor": 0.98},
        "fan": {"speed": 0.75, "rpm": None},
        "toolhead": {"position": [10.0, 20.0, 0.4, 0.0], "homed_axes": "xyz"},
    },
}

MOONRAKER_SYSTEM_INFO: Final[dict[str, Any]] = {
    "system_info": {
        "provider": "systemd_dbus",
        "cpu_info": {
            "cpu_count": 4,
            "bits": "32bit",
            "processor": "armv7l",
            "cpu_desc": "ARMv7 Processor rev 4 (v7l)",
            "serial_number": "b898bdb4",
            "hardware_desc": "BCM2835",
            "model": "Raspberry Pi 3 Model B Rev 1.2",
            "total_memory": 945364,
            "memory_units": "kB",
        },
        "distribution": {
            "name": "Raspbian GNU/Linux 10 (buster)",
            "id": "raspbian",
            "version": "10",
        },
        "python": {"version_string": "3.9.2"},
    }
}

MOONRAKER_FILES: Final[dict[str, Any]] = {
    "result": [
        {
            "path": "benchy.gcode",
            "modified": 1700000000.0,
            "size": 4096,
            "permissions": "rw",
        },
        {
            "path": "subdir/",
            "modified": 1700000001.0,
            "size": None,
            "permissions": "rw",
        },
    ]
}

OCTOPRINT_PRINTER: Final[dict[str, Any]] = {
    "temperature": {
        "tool0": {"actual": 214.3, "target": 215.0, "offset": 0},
        "bed": {"actual": 60.1, "target": 60.0, "offset": 0},
    },
    "sd": {"ready": False},
    "state": {
        "text": "Printing",
        "flags": {
            "operational": True,
            "paused": False,
            "printing": True,
            "cancelling": False,
            "pausing": False,
            "sdReady": True,
            "error": False,
            "ready": False,
            "closedOrError": False,
        },
    },
}

OCTOPRINT_JOB: Final[dict[str, Any]] = {
    "job": {
        "file": {"name": "cube.gcode", "origin": "local", "size": 2048, "date": 1700000000},
        "estimatedPrintTime": 1200.0,
        "filament": {"tool0": {"length": 3000.0, "volume": 7.2}},
    },
    "progress": {"completion": 0.4, "filepos": 819, "printTime": 480, "printTimeLeft": 720},
    "state": "Printing",
}

OCTOPRINT_FILES: Final[dict[str, Any]] = {
    "key": "local",
    "name": "Local",
    "capabilities": {"remove_file": True, "write_file": True},
    "files": [
        {
            "name": "cube.gcode",
            "path": "cube.gcode",
            "type": "machinecode",
            "size": 2048,
            "date": 1700000000,
        },
        {"name": "folderA", "path": "folderA", "type": "folder", "children": []},
    ],
}

GCODE: Final = b"; recorded job\nG28\nG1 X10 Y10 F3000\n"


# --------------------------------------------------------------------- server


@dataclass
class Recorded:
    """What the loopback server saw while one test drove an adapter against it."""

    base_url: str = ""
    port: int = 0
    requests: list[str] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[dict[str, Any]] = field(default_factory=list)


def _json_route(payload: Any) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Build a handler that always answers with ``payload``."""

    async def handler(request: web.Request) -> web.Response:
        return web.json_response(payload)

    return handler


async def _printer(request: web.Request) -> web.Response:
    """Answer the printer state, or 409 while the printer connection is closed."""
    if request.app["offline"]:
        return web.json_response({"error": "Printer is not operational"}, status=409)
    return web.json_response(OCTOPRINT_PRINTER)


async def _command(request: web.Request) -> web.Response:
    """Record one command and answer with the documented success shape."""
    raw = await request.text() if request.can_read_body else ""
    request.app["commands"].append(
        {
            "method": request.method,
            "path": request.path,
            "query": request.query_string,
            "body": json.loads(raw) if raw.strip() else None,
        }
    )
    if request.method == "DELETE" or request.path.startswith("/api/"):
        return web.Response(status=204)
    return web.json_response({"result": "ok"})


async def _upload(request: web.Request) -> web.Response:
    """Record a multipart upload and answer in the recorded shape."""
    reader = await request.multipart()
    record: dict[str, Any] = {
        "content_length": request.headers.get("Content-Length"),
        "fields": {},
        "filenames": {},
    }
    while (part := await reader.next()) is not None:
        record["fields"][part.name] = await part.text()
        record["filenames"][part.name] = part.filename
    request.app["uploads"].append(record)

    leaf = record["filenames"].get("file") or "uploaded.gcode"
    folder = record["fields"].get("path", "")
    parent = f"{folder}/" if folder else ""
    if request.path == "/server/files/upload":
        return web.json_response({"result": "ok"}, status=201)
    return web.json_response(
        {
            "files": [
                {
                    "name": leaf,
                    "path": f"{parent}{leaf}",
                    "origin": "local",
                    "size": 2048,
                    "date": 1700000000,
                }
            ],
            "done": True,
        },
        status=201,
    )


async def _websocket(request: web.Request) -> web.WebSocketResponse:
    """Answer the Moonraker JSON-RPC handshake and push one status update."""
    request.app["requests"].append(f"{request.method} {request.path_qs}")
    if request.headers.get("X-Api-Key") != API_KEY:
        raise web.HTTPUnauthorized()
    socket_ = web.WebSocketResponse()
    await socket_.prepare(request)
    subscribe = await socket_.receive_json()
    await socket_.send_json(
        {
            "jsonrpc": "2.0",
            "id": subscribe["id"],
            "result": {"eventtime": 1.0, "status": MOONRAKER_OBJECTS["status"]},
        }
    )
    await socket_.send_json(
        {
            "jsonrpc": "2.0",
            "method": "notify_status_update",
            "params": [{"extruder": {"temperature": 231.5}}, 42.0],
        }
    )
    await socket_.close()
    return socket_


_HANDLERS: Final[dict[tuple[str, str], Callable[[web.Request], Awaitable[web.Response]]]] = {
    ("GET", "/printer/objects/query"): _json_route(MOONRAKER_OBJECTS),
    ("GET", "/server/files/list"): _json_route(MOONRAKER_FILES),
    ("GET", "/machine/system_info"): _json_route(MOONRAKER_SYSTEM_INFO),
    ("POST", "/server/files/upload"): _upload,
    ("GET", "/api/printer"): _printer,
    ("GET", "/api/job"): _json_route(OCTOPRINT_JOB),
    ("GET", "/api/files/local"): _json_route(OCTOPRINT_FILES),
    ("POST", "/api/files/local"): _upload,
    ("POST", "/api/job"): _command,
    ("POST", "/api/printer/tool"): _command,
    ("POST", "/api/printer/bed"): _command,
    ("POST", "/api/printer/printhead"): _command,
    ("POST", "/api/printer/command"): _command,
    ("POST", "/printer/print/pause"): _command,
    ("POST", "/printer/print/resume"): _command,
    ("POST", "/printer/print/cancel"): _command,
    ("POST", "/printer/gcode/script"): _command,
    ("POST", "/printer/print/start"): _command,
}

_PATH_PARAMETERISED: Final = ("/api/files/", "/server/files/gcodes/")


async def _handle(request: web.Request) -> web.Response:
    """Answer one recorded request, or refuse what this server does not model."""
    request.app["requests"].append(f"{request.method} {request.path_qs}")
    if request.headers.get("X-Api-Key") != API_KEY:
        return web.json_response({"error": "invalid api key"}, status=401)
    handler = _HANDLERS.get((request.method, request.path))
    if handler is None and request.path.startswith(_PATH_PARAMETERISED):
        handler = _command
    if handler is None:
        return web.json_response({"error": "no such route"}, status=404)
    return await handler(request)


@asynccontextmanager
async def _server(*, offline: bool = False) -> AsyncIterator[Recorded]:
    """Run the loopback printer server for one test."""
    recorded = Recorded()
    app = web.Application()
    app["requests"] = recorded.requests
    app["commands"] = recorded.commands
    app["uploads"] = recorded.uploads
    app["offline"] = offline
    app.router.add_get("/websocket", _websocket)
    app.router.add_route("*", "/{tail:.*}", _handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    recorded.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    recorded.base_url = f"http://127.0.0.1:{recorded.port}"
    try:
        yield recorded
    finally:
        await runner.cleanup()


@asynccontextmanager
async def _session() -> AsyncIterator[aiohttp.ClientSession]:
    """Open a client session; conftest pins the resolver aiodns refuses to be."""
    async with aiohttp.ClientSession() as session:
        yield session


def _config(protocol: ProtocolId, *, port: int, api_key: str = API_KEY) -> PrinterConfig:
    """Build a validated printer config pointing at a loopback port."""
    return parse_config(
        {
            "name": "Bench",
            "protocol": protocol.value,
            "host": "127.0.0.1",
            "port": port,
            "api_key": api_key,
        }
    )


def _moonraker(server: Recorded, session: aiohttp.ClientSession) -> MoonrakerProtocol:
    """Build the Moonraker adapter against the loopback server."""
    return MoonrakerProtocol(
        _config(ProtocolId.MOONRAKER, port=server.port),
        session,
        granted=MOONRAKER_CAPABILITIES,
    )


def _octoprint(server: Recorded, session: aiohttp.ClientSession) -> OctoPrintProtocol:
    """Build the OctoPrint adapter against the loopback server."""
    return OctoPrintProtocol(
        _config(ProtocolId.OCTOPRINT, port=server.port),
        session,
        granted=OCTOPRINT_CAPABILITIES,
    )


async def _gcode_stream() -> AsyncIterator[bytes]:
    """Yield the recorded G-code in two chunks, as an upload arrives."""
    yield GCODE[:8]
    yield GCODE[8:]


def _closed_port() -> int:
    """Return a loopback port with nothing listening on it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ---------------------------------------------------------------------- reads


def test_moonraker_read_maps_the_recorded_object_query() -> None:
    async def case() -> None:
        async with _server() as server, _session() as session:
            adapter = _moonraker(server, session)
            await adapter.async_setup()
            server.requests.clear()
            snapshot = await adapter.async_read()

            assert snapshot.protocol is ProtocolId.MOONRAKER
            assert snapshot.connected is True
            assert snapshot.print_state is PrintState.PRINTING
            assert snapshot.progress == pytest.approx(25.0)
            assert snapshot.hotend.current == pytest.approx(219.5)
            assert snapshot.hotend.target == pytest.approx(220.0)
            assert snapshot.bed.current == pytest.approx(59.8)
            assert snapshot.bed.target == pytest.approx(60.0)
            assert snapshot.filename == "benchy.gcode"
            assert snapshot.elapsed == pytest.approx(300.0)
            # 300s elapsed at 25 percent is 900s left, from measured values only.
            assert snapshot.remaining == pytest.approx(900.0)
            assert snapshot.current_layer == 50
            assert snapshot.total_layers == 200
            assert snapshot.speed_factor == pytest.approx(105.0)
            assert snapshot.flow_factor == pytest.approx(98.0)
            assert snapshot.fans.model == pytest.approx(75.0)
            assert snapshot.position is not None
            assert snapshot.position.x == pytest.approx(10.0)
            assert snapshot.position.y == pytest.approx(20.0)
            assert snapshot.position.z == pytest.approx(0.4)
            assert snapshot.homed_axes == frozenset({"X", "Y", "Z"})
            assert snapshot.model == "Raspberry Pi 3 Model B Rev 1.2"
            assert snapshot.serial == "b898bdb4"

            # Unsupported by this protocol, and left empty rather than invented.
            assert snapshot.firmware is None
            assert snapshot.chamber.current is None
            assert snapshot.chamber.target is None
            assert snapshot.job_id is None
            assert snapshot.lights == frozenset()

            query = server.requests[0]
            assert query.startswith("GET /printer/objects/query?")
            for name in (
                "print_stats",
                "heater_bed",
                "extruder",
                "virtual_sdcard",
                "gcode_move",
                "fan",
                "toolhead",
                "display_status",
            ):
                assert name in query

    asyncio.run(case())


def test_octoprint_read_maps_the_recorded_reads() -> None:
    async def case() -> None:
        async with _server() as server, _session() as session:
            adapter = _octoprint(server, session)
            await adapter.async_setup()
            server.requests.clear()
            snapshot = await adapter.async_read()

            assert snapshot.protocol is ProtocolId.OCTOPRINT
            assert snapshot.connected is True
            assert snapshot.print_state is PrintState.PRINTING
            assert snapshot.progress == pytest.approx(40.0)
            assert snapshot.hotend.current == pytest.approx(214.3)
            assert snapshot.hotend.target == pytest.approx(215.0)
            assert snapshot.bed.current == pytest.approx(60.1)
            assert snapshot.bed.target == pytest.approx(60.0)
            assert snapshot.filename == "cube.gcode"
            assert snapshot.elapsed == pytest.approx(480.0)
            assert snapshot.remaining == pytest.approx(720.0)

            # The REST API exposes no layer count, no fan duty, no axis position and
            # no way to read a speed or flow factor back.
            assert snapshot.current_layer is None
            assert snapshot.total_layers is None
            assert snapshot.fans.model is None
            assert snapshot.position is None
            assert snapshot.speed_factor is None
            assert snapshot.flow_factor is None
            assert snapshot.chamber.current is None
            assert snapshot.firmware is None
            assert snapshot.job_id is None
            assert snapshot.homed_axes == frozenset()

            assert server.requests == ["GET /api/printer", "GET /api/job"]

    asyncio.run(case())


def test_octoprint_reports_a_closed_printer_connection() -> None:
    async def case() -> None:
        async with _server(offline=True) as server, _session() as session:
            adapter = _octoprint(server, session)
            snapshot = await adapter.async_read()

            assert snapshot.connected is False
            assert snapshot.print_state is PrintState.UNKNOWN
            assert snapshot.progress is None
            assert snapshot.filename is None
            # A 409 is the host answering about its own closed connection, so setup
            # must not treat it as a credential or transport failure.
            assert server.requests == ["GET /api/printer"]
            await adapter.async_setup()

    asyncio.run(case())


# -------------------------------------------------------------------- failures


def test_unsupported_commands_never_reach_the_wire() -> None:
    async def case() -> None:
        async with _server() as server, _session() as session:
            moonraker = _moonraker(server, session)
            with pytest.raises(UnsupportedCommandError):
                await moonraker.async_send(Command.SET_LIGHT, on=True)
            with pytest.raises(UnsupportedCommandError):
                await moonraker.async_send(
                    Command.SET_FAN_SPEED, value=50.0, channel="auxiliary"
                )

            octoprint = _octoprint(server, session)
            with pytest.raises(UnsupportedCommandError):
                await octoprint.async_send(Command.SET_CHAMBER_TEMP, value=50.0)
            with pytest.raises(UnsupportedCommandError):
                await octoprint.async_send(Command.SET_FAN_SPEED, value=50.0, channel="chamber")

            # Reading the printer is allowed; sending it anything is not.
            assert server.commands == []

    asyncio.run(case())


def test_a_refused_credential_is_an_auth_error() -> None:
    async def case() -> None:
        async with _server() as server, _session() as session:
            config = _config(ProtocolId.MOONRAKER, port=server.port, api_key=OTHER_KEY)
            adapter = MoonrakerProtocol(config, session, granted=MOONRAKER_CAPABILITIES)
            with pytest.raises(AuthError):
                await adapter.async_read()
            assert server.requests[0].startswith("GET /printer/objects/query")

            config = _config(ProtocolId.OCTOPRINT, port=server.port, api_key=OTHER_KEY)
            adapter = OctoPrintProtocol(config, session, granted=OCTOPRINT_CAPABILITIES)
            with pytest.raises(AuthError):
                await adapter.async_read()
            assert server.requests[1] == "GET /api/printer"

    asyncio.run(case())


def test_a_refused_connection_is_unreachable() -> None:
    async def case() -> None:
        port = _closed_port()
        async with _session() as session:
            moonraker = MoonrakerProtocol(
                _config(ProtocolId.MOONRAKER, port=port),
                session,
                granted=MOONRAKER_CAPABILITIES,
            )
            with pytest.raises(UnreachableError):
                await moonraker.async_read()

            octoprint = OctoPrintProtocol(
                _config(ProtocolId.OCTOPRINT, port=port),
                session,
                granted=OCTOPRINT_CAPABILITIES,
            )
            with pytest.raises(UnreachableError):
                await octoprint.async_read()

    asyncio.run(case())


# ----------------------------------------------------------------------- files


def test_list_files_parses_the_recorded_shape() -> None:
    async def case() -> None:
        async with _server() as server, _session() as session:
            moonraker = _moonraker(server, session)
            entries = await moonraker.async_list_files()
            assert [entry.path for entry in entries] == ["benchy.gcode"]
            assert entries[0].name == "benchy.gcode"
            assert entries[0].size == 4096
            assert entries[0].modified == datetime.fromtimestamp(1700000000.0, tz=timezone.utc)

            octoprint = _octoprint(server, session)
            entries = await octoprint.async_list_files()
            assert [entry.path for entry in entries] == ["local/cube.gcode"]
            assert entries[0].name == "cube.gcode"
            assert entries[0].size == 2048
            assert entries[0].modified == datetime.fromtimestamp(1700000000, tz=timezone.utc)

    asyncio.run(case())


def test_uploads_carry_the_file_part_in_the_researched_shape() -> None:
    async def case() -> None:
        async with _server() as server, _session() as session:
            moonraker = _moonraker(server, session)
            entry = await moonraker.async_upload_file(
                "jobs/benchy.gcode", _gcode_stream(), size=len(GCODE)
            )
            assert entry.name == "benchy.gcode"
            assert entry.path == "jobs/benchy.gcode"
            assert entry.size == len(GCODE)
            upload = server.uploads[0]
            assert upload["fields"]["root"] == "gcodes"
            assert upload["fields"]["path"] == "jobs"
            assert upload["filenames"]["file"] == "benchy.gcode"
            assert upload["fields"]["file"] == GCODE.decode()

            octoprint = _octoprint(server, session)
            entry = await octoprint.async_upload_file(
                "jobs/cube.gcode", _gcode_stream(), size=len(GCODE)
            )
            assert entry.name == "cube.gcode"
            assert entry.path == "local/jobs/cube.gcode"
            upload = server.uploads[1]
            assert upload["filenames"]["file"] == "cube.gcode"
            assert upload["fields"]["path"] == "jobs"
            # OctoPrint refuses a chunked upload, so the length must be declared.
            assert upload["content_length"] is not None

            with pytest.raises(ProtocolError):
                await octoprint.async_upload_file("cube.gcode", _gcode_stream())
            assert len(server.uploads) == 2

    asyncio.run(case())


# -------------------------------------------------------------------- commands


@contextmanager
def _idle_printers() -> Iterator[None]:
    """Make both recorded printers report that no job is running."""
    stats = MOONRAKER_OBJECTS["status"]["print_stats"]
    flags = OCTOPRINT_PRINTER["state"]["flags"]
    saved = (stats["state"], OCTOPRINT_PRINTER["state"]["text"], flags["printing"], flags["ready"])
    stats["state"] = "standby"
    OCTOPRINT_PRINTER["state"]["text"] = "Operational"
    flags["printing"], flags["ready"] = False, True
    try:
        yield
    finally:
        stats["state"], OCTOPRINT_PRINTER["state"]["text"], flags["printing"], flags["ready"] = saved


def test_a_print_is_not_started_over_a_running_one() -> None:
    async def case() -> None:
        async with _server() as server, _session() as session:
            for adapter in (_moonraker(server, session), _octoprint(server, session)):
                with pytest.raises(CommandBlockedError, match="busy"):
                    await adapter.async_send(Command.START_PRINT, filename="benchy.gcode")
            assert server.commands == []

    asyncio.run(case())


def test_commands_reach_the_researched_endpoints() -> None:
    async def case() -> None:
        async with _server() as server, _session() as session:
            moonraker = _moonraker(server, session)
            await moonraker.async_send(Command.PAUSE)
            await moonraker.async_send(Command.SET_BED_TEMP, value=60.0)
            await moonraker.async_send(Command.START_PRINT, filename="benchy.gcode")

            octoprint = _octoprint(server, session)
            await octoprint.async_send(Command.STOP)
            await octoprint.async_send(Command.SET_HOTEND_TEMP, value=215.0)
            await octoprint.async_send(Command.START_PRINT, filename="local/cube.gcode")
            await octoprint.async_send(Command.DELETE_FILE, filename="local/cube.gcode")

            assert [item for item in server.requests if not item.startswith("GET ")] == [
                "POST /printer/print/pause",
                "POST /printer/gcode/script",
                "POST /printer/print/start?filename=benchy.gcode",
                "POST /api/job",
                "POST /api/printer/tool",
                "POST /api/files/local/cube.gcode",
                "DELETE /api/files/local/cube.gcode",
            ]
            assert server.commands[1]["body"] == {"script": "M140 S60"}
            assert server.commands[3]["body"] == {"command": "cancel"}
            assert server.commands[4]["body"] == {
                "command": "target",
                "targets": {"tool0": 215.0},
            }
            assert server.commands[5]["body"] == {"command": "select", "print": True}

    with _idle_printers():
        asyncio.run(case())


def test_moonraker_subscription_merges_pushed_updates() -> None:
    async def case() -> None:
        async with _server() as server, _session() as session:
            adapter = _moonraker(server, session)
            iterator = adapter.async_subscribe()
            assert iterator is not None
            first = await anext(iterator)
            second = await anext(iterator)
            await iterator.aclose()

            assert first.print_state is PrintState.PRINTING
            assert first.progress == pytest.approx(25.0)
            assert first.current_layer == 50
            # A status update carries only the changed object, so the snapshot must
            # merge it into what the subscription reply already established.
            assert second.hotend.current == pytest.approx(231.5)
            assert second.print_state is PrintState.PRINTING
            assert second.current_layer == 50
            assert server.requests == ["GET /websocket"]

    asyncio.run(case())

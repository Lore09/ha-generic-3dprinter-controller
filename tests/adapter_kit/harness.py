"""One harness per protocol, wrapping its fake printer so every adapter runs the contract tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import pytest

from custom_components.generic_3dprinter.const import Command, ProtocolId
from custom_components.generic_3dprinter.protocols import PrinterConfig, Protocol, parse_config
from custom_components.generic_3dprinter.registry import build_adapter

#: Parameters every command is sent with. Valid for every adapter, so a refusal is
#: the adapter's or the printer's, never the validator's.
SAMPLE_PARAMS: dict[Command, dict[str, Any]] = {
    Command.START_PRINT: {"filename": "benchy.gcode"},
    Command.PAUSE: {},
    Command.RESUME: {},
    Command.STOP: {},
    Command.SET_HOTEND_TEMP: {"value": 50},
    Command.SET_BED_TEMP: {"value": 40},
    Command.SET_CHAMBER_TEMP: {"value": 30},
    Command.SET_FAN_SPEED: {"value": 30, "channel": "model"},
    Command.SET_SPEED: {"value": 100},
    Command.SET_FLOW: {"value": 100},
    Command.SET_LIGHT: {"on": True},
    Command.HOME: {"axes": "XYZ"},
    Command.JOG: {"axis": "x", "distance": 1},
    Command.DELETE_FILE: {"filename": "cube.gcode"},
    Command.LOAD_FILAMENT: {"unit": 0, "slot": 1},
    Command.UNLOAD_FILAMENT: {"unit": 0, "slot": 1},
    Command.SET_FILAMENT: {"unit": 0, "slot": 1, "material": "PLA", "color": "#FF0000"},
    Command.SET_AUTO_REFILL: {"on": True},
}


@dataclass
class AdapterHarness:
    """A running fake printer and an adapter pointed at it."""

    protocol: ProtocolId
    config: PrinterConfig
    adapter: Protocol
    #: How many commands the fake has received so far.
    wire: Callable[[], int]
    #: How many client connections are open on the fake, or ``None`` when the fake
    #: cannot tell, as a plain HTTP server cannot.
    connections: Callable[[], int | None]
    #: Power the fake off and on again at the same address, or ``None``.
    power_off: Callable[[], Awaitable[None]] | None = None
    power_on: Callable[[], Awaitable[None]] | None = None


#: Requests that only read the printer. They are not commands, and a read the
#: adapter makes on its own must not look like a command reaching the wire.
SDCP_READS = frozenset({0, 1, 258, 320, 324, 386})
CC2_READS = frozenset({1001, 1002, 1042, 1044, 1048, 2005})

HarnessFactory = Callable[[aiohttp.ClientSession, pytest.MonkeyPatch], Any]


@asynccontextmanager
async def sdcp_harness(
    session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AdapterHarness]:
    """The Centauri Carbon over SDCP, printing."""
    from tests.fake_printer import FakePrinterServer

    server = FakePrinterServer()
    await server.start()
    config = parse_config(
        {
            "name": "contract",
            "protocol": ProtocolId.SDCP_CC1.value,
            "host": "127.0.0.1",
            "port": urlsplit(server.url).port,
            "camera_port": server.camera_port,
        }
    )
    try:
        yield AdapterHarness(
            protocol=ProtocolId.SDCP_CC1,
            config=config,
            adapter=build_adapter(config, session),
            wire=lambda: sum(1 for cmd in server.sent_commands if cmd not in SDCP_READS),
            connections=lambda: len(server._sockets),  # noqa: SLF001
            power_off=server.stop,
            power_on=server.start,
        )
    finally:
        await server.stop()


@asynccontextmanager
async def cc2_harness(
    session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AdapterHarness]:
    """The Centauri Carbon 2 over its MQTT broker, printing."""
    from custom_components.generic_3dprinter.adapters import elegoo_cc2
    from tests.fake_cc2_printer import SERIAL, FakeCC2Printer

    monkeypatch.setattr(elegoo_cc2, "REQUEST_GAP", 0.0)
    monkeypatch.setattr(elegoo_cc2, "REGISTER_TIMEOUT", 0.3)
    monkeypatch.setattr(elegoo_cc2, "ACK_TIMEOUT", 0.5)
    monkeypatch.setattr(elegoo_cc2, "RESUME_REFUSAL_WINDOW", 0.2)
    printer = FakeCC2Printer()
    await printer.start()
    config = parse_config(
        {
            "name": "contract",
            "protocol": ProtocolId.ELEGOO_CC2.value,
            "host": "127.0.0.1",
            "port": printer.port,
            "camera_port": printer.camera_port,
            "serial": SERIAL,
        }
    )
    try:
        yield AdapterHarness(
            protocol=ProtocolId.ELEGOO_CC2,
            config=config,
            adapter=build_adapter(config, session),
            wire=lambda: sum(1 for item in printer.requests if int(item["method"]) not in CC2_READS),
            connections=lambda: len(printer._broker.sessions),  # noqa: SLF001
            power_off=printer.stop,
            power_on=printer.start,
        )
    finally:
        await printer.stop()


@asynccontextmanager
async def _http_harness(
    protocol: ProtocolId, session: aiohttp.ClientSession
) -> AsyncIterator[AdapterHarness]:
    from tests.test_adapters_http import API_KEY, _server

    async with _server() as recorded:
        config = parse_config(
            {
                "name": "contract",
                "protocol": protocol.value,
                "host": "127.0.0.1",
                "port": recorded.port,
                "api_key": API_KEY,
            }
        )
        yield AdapterHarness(
            protocol=protocol,
            config=config,
            adapter=build_adapter(config, session),
            wire=lambda: len(recorded.commands),
            connections=lambda: None,
        )


@asynccontextmanager
async def moonraker_harness(
    session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AdapterHarness]:
    """Klipper through Moonraker, printing."""
    async with _http_harness(ProtocolId.MOONRAKER, session) as harness:
        yield harness


@asynccontextmanager
async def octoprint_harness(
    session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AdapterHarness]:
    """OctoPrint, printing."""
    async with _http_harness(ProtocolId.OCTOPRINT, session) as harness:
        yield harness


@asynccontextmanager
async def _duet_server(protocol: ProtocolId, session: aiohttp.ClientSession) -> AsyncIterator[AdapterHarness]:
    from tests.test_adapters_duet_web import FakePrinter, printer_server

    printer = FakePrinter()
    async with printer_server(printer) as base_url:
        parts = urlsplit(base_url)
        data: dict[str, Any] = {
            "name": "contract",
            "protocol": protocol.value,
            "host": parts.hostname,
            "port": parts.port,
        }
        if protocol is ProtocolId.DUET:
            data["password"] = printer.password
        else:
            data["web_url"] = base_url
        config = parse_config(data)
        yield AdapterHarness(
            protocol=protocol,
            config=config,
            adapter=build_adapter(config, session),
            wire=lambda: len(printer.gcode_requests),
            connections=lambda: None,
        )


@asynccontextmanager
async def duet_harness(
    session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AdapterHarness]:
    """A Duet board over its HTTP API."""
    async with _duet_server(ProtocolId.DUET, session) as harness:
        yield harness


@asynccontextmanager
async def web_only_harness(
    session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AdapterHarness]:
    """A printer that is only a web page."""
    async with _duet_server(ProtocolId.WEB_ONLY, session) as harness:
        yield harness


@asynccontextmanager
async def kobra_harness(
    session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AdapterHarness]:
    """An Anycubic Kobra X in LAN mode, printing, over MQTT with TLS."""
    from custom_components.generic_3dprinter.adapters import anycubic_kobra
    from tests.fake_kobra_printer import SERIAL, FakeKobraPrinter

    monkeypatch.setattr(anycubic_kobra, "ANSWER_WINDOW", 0.5)
    monkeypatch.setattr(anycubic_kobra, "FIRST_INFO_TIMEOUT", 1.0)
    monkeypatch.setattr(anycubic_kobra, "INFO_WAIT", 0.5)
    printer = FakeKobraPrinter()
    await printer.start()
    config = parse_config(
        {
            "name": "contract",
            "protocol": ProtocolId.ANYCUBIC_KOBRA.value,
            "host": "127.0.0.1",
            "port": printer.port,
            "serial": SERIAL,
        }
    )
    try:
        yield AdapterHarness(
            protocol=ProtocolId.ANYCUBIC_KOBRA,
            config=config,
            adapter=build_adapter(config, session),
            wire=lambda: len(printer.commands),
            connections=lambda: printer.sessions,
            power_off=printer.stop,
            power_on=printer.start,
        )
    finally:
        await printer.stop()
        printer.close()


#: Every protocol's harness. The registry test holds this complete.
HARNESSES: dict[ProtocolId, HarnessFactory] = {
    ProtocolId.SDCP_CC1: sdcp_harness,
    ProtocolId.ELEGOO_CC2: cc2_harness,
    ProtocolId.ANYCUBIC_KOBRA: kobra_harness,
    ProtocolId.MOONRAKER: moonraker_harness,
    ProtocolId.OCTOPRINT: octoprint_harness,
    ProtocolId.DUET: duet_harness,
    ProtocolId.WEB_ONLY: web_only_harness,
}

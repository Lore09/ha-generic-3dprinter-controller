"""The read-only resin acceptance tool against the fake Saturn: it sends only reads."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from custom_components.generic_3dprinter import discovery
from custom_components.generic_3dprinter.adapters import sdcp, sdcp_resin
from tests.fake_printer import MAINBOARD as CC1_MAINBOARD
from tests.fake_resin_printer import FakeResinPrinter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import acceptance_sdcp_resin  # noqa: E402

CENTAURI = {"Id": "x", "Data": {"MachineName": "Centauri Carbon", "MainboardID": CC1_MAINBOARD}}


@pytest.fixture(name="printer")
async def printer_fixture(request: pytest.FixtureRequest) -> AsyncIterator[FakeResinPrinter]:
    printer = FakeResinPrinter(printing=getattr(request, "param", False))
    await printer.start()
    yield printer
    await printer.stop()


def _answer(reply: dict[str, Any] | None, seen: list[Any]) -> Any:
    async def probe(payload: bytes, target: tuple[str, int], timeout: float, accept: Any = None):
        seen.append(target)
        if reply is None or (accept is not None and not accept(reply)):
            return None
        return reply, target[0]

    return probe


def _args(port: int, **overrides: Any) -> argparse.Namespace:
    values = {"host": "127.0.0.1", "port": port, "serial": None, "save": None, "timeout": 0.1,
              "keepalive": 0.0, "watch": 0.0, "interval": 0.1}
    return argparse.Namespace(**{**values, **overrides})


def _discovered(monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any], ports: list[int]) -> list[Any]:
    seen: list[Any] = []
    monkeypatch.setattr(discovery, "async_probe_udp", _answer(reply, seen))
    monkeypatch.setattr(sdcp_resin, "async_probe_udp", _answer(reply, seen))

    async def probe_ports(host: str, candidates: Any) -> list[int]:
        seen.append((host, tuple(candidates)))
        return [port for port in candidates if port in ports]

    monkeypatch.setattr(discovery, "async_probe_ports", probe_ports)
    return seen


@pytest.mark.parametrize("printer", [False, True], indirect=True, ids=["idle", "printing"])
async def test_the_default_run_sends_only_reads(
    monkeypatch: pytest.MonkeyPatch, printer: FakeResinPrinter, tmp_path: Path
) -> None:
    monkeypatch.setattr(sdcp, "HEARTBEAT_INTERVAL", 0.1)
    seen = _discovered(monkeypatch, {"Id": "x", "Data": printer.attributes}, [printer.port])
    saved = tmp_path / "saturn.json"

    code = await acceptance_sdcp_resin.run(_args(printer.port, save=str(saved), keepalive=0.35, watch=0.3))

    assert code == 0
    assert printer.forbidden == []
    assert not printer.crashed
    assert set(printer.sent_commands) <= {0, 1, 258, 320, 321}
    assert {320, 321} <= set(printer.sent_commands)
    assert printer.texts == []
    assert ("127.0.0.1", (80, 443, 554, 3030, 3031, 8080, printer.port)) in seen
    record = json.loads(saved.read_bytes())
    assert record["udp"]["Data"]["MachineName"] == "Saturn 4 Ultra 16K"
    assert record["attributes"]["MainboardID"] == printer.mainboard
    assert record["history"]["HistoryData"]
    assert set(record["sent"]) <= acceptance_sdcp_resin.READS
    for _ in range(50):
        if not printer.connections:
            break
        await asyncio.sleep(0.02)
    assert printer.connections == 0, "the tool left its socket open"


async def test_the_idle_capture_has_millisecond_ticks(
    monkeypatch: pytest.MonkeyPatch, printer: FakeResinPrinter, capsys: pytest.CaptureFixture[str]
) -> None:
    _discovered(monkeypatch, {"Id": "x", "Data": printer.attributes}, [printer.port])
    assert await acceptance_sdcp_resin.run(_args(printer.port)) == 0
    out = capsys.readouterr().out
    assert "[PASS] the last job's ticks are milliseconds - it took 2757 s, its ticks say 2757.03 s" in out
    assert "[PASS] a nozzle target is refused" in out
    assert "[PASS] nothing was sent for it" in out
    assert "[PASS] an idle printer shows no job" in out


async def test_an_fdm_printer_is_never_connected(monkeypatch: pytest.MonkeyPatch, printer: FakeResinPrinter) -> None:
    seen = _discovered(monkeypatch, CENTAURI, [printer.port])

    def no_session(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the tool connected to an FDM printer")

    monkeypatch.setattr(aiohttp, "ClientSession", no_session)
    assert await acceptance_sdcp_resin.run(_args(printer.port)) == 1
    assert seen == [("127.0.0.1", 3000)]
    assert printer.sent_commands == []


async def test_a_closed_port_stops_the_run(monkeypatch: pytest.MonkeyPatch, printer: FakeResinPrinter) -> None:
    _discovered(monkeypatch, {"Id": "x", "Data": printer.attributes}, [])
    assert await acceptance_sdcp_resin.run(_args(printer.port)) == 1
    assert printer.sent_commands == []

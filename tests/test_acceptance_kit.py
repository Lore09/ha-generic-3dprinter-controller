"""The scaffolding every hardware acceptance script shares."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from custom_components.generic_3dprinter import discovery
from custom_components.generic_3dprinter.adapters import sdcp
from tests.fake_printer import MAINBOARD as CC1_MAINBOARD
from tests.fake_printer import FakePrinterServer
from tests.fake_resin_printer import FakeResinPrinter, load_fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import acceptance_camera  # noqa: E402
import acceptance_kit  # noqa: E402
import acceptance_sdcp  # noqa: E402
import probe_sdcp  # noqa: E402
import verify_sdcp  # noqa: E402

FIXTURE = load_fixture()
SATURN = {"Id": "x", "Data": FIXTURE["attributes"]["Attributes"]}
NESTED_SATURN = {"Data": {"Attributes": FIXTURE["attributes"]["Attributes"], "Status": FIXTURE["status"]["Status"]}}
CENTAURI = {"Id": "x", "Data": {"MachineName": "Centauri Carbon", "MainboardID": CC1_MAINBOARD}}


def _probe(reply: dict[str, Any] | None, seen: list[Any]) -> Any:
    async def probe(payload: bytes, target: tuple[str, int], timeout: float, accept: Any = None):
        seen.append((payload, target))
        if reply is None or (accept is not None and not accept(reply)):
            return None
        return reply, target[0]

    return probe


def test_a_report_counts_and_names_its_failures(capsys: pytest.CaptureFixture[str]) -> None:
    report = acceptance_kit.Report()
    report.section("reads")
    assert report.check("status read", True)
    assert not report.check("temperatures read", False, "no nozzle")
    assert report.summary() == 1
    out = capsys.readouterr().out
    assert "[1] reads" in out
    assert "[PASS] status read" in out
    assert "[FAIL] temperatures read - no nozzle" in out
    assert "1 of 2 checks passed" in out
    assert "FAILED: temperatures read" in out


def test_a_clean_report_exits_zero() -> None:
    report = acceptance_kit.Report()
    report.check("one", True)
    assert report.summary() == 0


def test_an_active_step_is_skipped_unless_asked_and_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not acceptance_kit.confirm("home the printer", active=False, assume_yes=True)
    assert acceptance_kit.confirm("home the printer", active=True, assume_yes=True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert not acceptance_kit.confirm("home the printer", active=True, assume_yes=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert acceptance_kit.confirm("home the printer", active=True, assume_yes=False)


@pytest.mark.parametrize(
    ("reply", "refused"),
    [(SATURN, True), (NESTED_SATURN, True), (CENTAURI, False), (None, False)],
    ids=["flat-saturn", "nested-saturn", "centauri", "silent"],
)
async def test_the_guard_refuses_only_a_resin_reply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reply: Any, refused: bool
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(discovery, "async_probe_udp", _probe(reply, seen))
    assert await acceptance_kit.async_refuse_resin("192.0.2.43") is refused
    assert seen == [(b"M99999", ("192.0.2.43", 3000))]
    assert ("Saturn 4 Ultra 16K, a resin printer" in capsys.readouterr().out) is refused


def _no_session(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the tool opened a session to a resin printer")


@pytest.mark.parametrize(
    "run",
    [
        lambda: acceptance_sdcp.run("192.0.2.43", None, 0.0),
        lambda: acceptance_camera.run("192.0.2.43", 1.0, None),
        lambda: probe_sdcp.run("192.0.2.43", 3030, 0.0),
    ],
    ids=["acceptance_sdcp", "acceptance_camera", "probe_sdcp"],
)
async def test_an_fdm_tool_stops_before_it_reaches_a_resin_printer(monkeypatch: pytest.MonkeyPatch, run: Any) -> None:
    """acceptance_sdcp sends 386 and 324, and probe_sdcp 324: nothing may reach a Saturn."""
    monkeypatch.setattr(discovery, "async_probe_udp", _probe(SATURN, []))
    monkeypatch.setattr(aiohttp, "ClientSession", _no_session)
    monkeypatch.setattr(probe_sdcp, "discover_mainboard_id", _no_session)
    assert await run() == 1


@pytest.mark.parametrize(("printer_class", "expected"), [(FakeResinPrinter, [0, 1, 258, 320]), (FakePrinterServer, [0, 1, 258, 320, 324])], ids=["saturn", "centauri"])
async def test_the_probe_sends_324_only_to_a_printer_that_shows_it_is_fdm(
    monkeypatch: pytest.MonkeyPatch, printer_class: Any, expected: list[int]
) -> None:
    """With UDP silent the guard lets the probe through, so the printer's own frames decide."""
    monkeypatch.setattr(discovery, "async_probe_udp", _probe(None, []))
    monkeypatch.setattr(probe_sdcp, "discover_mainboard_id", lambda _host: "")
    monkeypatch.setattr(probe_sdcp, "SETTLE", 0.2)
    monkeypatch.setattr(probe_sdcp, "GAP", 0.2)
    printer = printer_class()
    url = await printer.start()
    try:
        assert await probe_sdcp.run("127.0.0.1", int(url.rsplit(":", 1)[1]), 0.0) == 0
    finally:
        await printer.stop()
    assert printer.sent_commands == expected
    assert not getattr(printer, "forbidden", [])


@pytest.mark.parametrize(
    "attributes",
    [
        SATURN["Data"],
        NESTED_SATURN["Data"],
        CENTAURI["Data"],
        {**FakePrinterServer().attributes, "Status": FakePrinterServer().status},
        {"Status": FIXTURE["status"]["Status"]},
        {"SupportFileType": "GOO"},
        {"SupportFileType": ["GCODE"], "Resolution": "15120x6230"},
        {"DevicesStatus": {"LCDStatus": 1}},
        {"MachineName": "Mars 5"},
        {},
    ],
)
def test_the_stdlib_verifier_classifies_resin_as_the_adapter_does(attributes: dict[str, Any]) -> None:
    """verify_sdcp stays stdlib only, so its copy of the check must agree with classify_sdcp."""
    flat = attributes.get("Attributes", attributes)
    status = attributes.get("Status") if isinstance(attributes.get("Status"), dict) else None
    assert verify_sdcp.is_resin(attributes) is (sdcp.classify_sdcp(flat, status) == "resin")


async def _verify(
    monkeypatch: pytest.MonkeyPatch, port: int, raw: dict[str, Any], *argv: str, flag: str = "--discover"
) -> int:
    """Run verify_sdcp against a loopback printer, found by a discovery that replies ``raw``.
    An empty ``flag`` leaves out ``--discover``, so the tool discovers because no host was given."""
    data = raw["Data"]
    entry = {"host": "127.0.0.1", "mainboard_id": data.get("MainboardID"), "model": data.get("MachineName"),
             "firmware": data.get("FirmwareVersion"), "raw": raw}
    monkeypatch.setattr(verify_sdcp, "discover", lambda: [entry])
    monkeypatch.setattr(verify_sdcp, "WS_PORT", port)
    monkeypatch.setattr(verify_sdcp, "upload_file", _no_session)
    flags = [flag] if flag else []
    monkeypatch.setattr(sys, "argv", ["verify_sdcp.py", *flags, "--no-camera", "--period-ms", "100", *argv])
    return await asyncio.to_thread(verify_sdcp.main)


@pytest.mark.parametrize("flag", ["--discover", ""], ids=["asked", "no_host"])
async def test_the_verifier_stops_at_a_resin_discovery_reply(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    printer = FakeResinPrinter()
    url = await printer.start()
    try:
        port = int(url.rsplit(":", 1)[1])
        assert await _verify(monkeypatch, port, SATURN, "--upload", "x.goo", flag=flag) == 1
    finally:
        await printer.stop()
    assert printer.sent_commands == []


async def test_the_verifier_stops_at_resin_attributes_before_it_subscribes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sparse discovery reply gets through, so Cmd 1 alone is sent: never 512 or the 6-field 128."""
    printer = FakeResinPrinter()
    url = await printer.start()
    sparse = {"Data": {"MainboardID": printer.mainboard}}
    try:
        assert await _verify(monkeypatch, int(url.rsplit(":", 1)[1]), sparse, "--start-print", "model.goo") == 1
    finally:
        await printer.stop()
    assert printer.sent_commands == [1]
    assert not printer.crashed


async def test_the_verifier_still_probes_a_centauri(monkeypatch: pytest.MonkeyPatch) -> None:
    printer = FakePrinterServer()
    url = await printer.start()
    try:
        assert await _verify(monkeypatch, int(url.rsplit(":", 1)[1]), CENTAURI) == 0
    finally:
        await printer.stop()
    assert printer.sent_commands[:2] == [1, 512]
    assert 258 in printer.sent_commands

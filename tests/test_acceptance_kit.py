"""The scaffolding every hardware acceptance script shares."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from custom_components.generic_3dprinter import discovery
from tests.fake_printer import MAINBOARD as CC1_MAINBOARD
from tests.fake_resin_printer import load_fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import acceptance_camera  # noqa: E402
import acceptance_kit  # noqa: E402
import acceptance_sdcp  # noqa: E402
import probe_sdcp  # noqa: E402

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

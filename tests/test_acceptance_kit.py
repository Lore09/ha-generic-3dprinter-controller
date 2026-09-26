"""The scaffolding every hardware acceptance script shares."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import acceptance_kit  # noqa: E402


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

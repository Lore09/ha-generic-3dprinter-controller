"""What every hardware acceptance script shares: numbered checks, ``--active`` steps confirmed
before they run, and the resin guard. Exit codes: 0 all passed, 1 a check failed, 2 bad usage."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def use_repository() -> None:
    """Make ``custom_components.generic_3dprinter`` importable from this checkout."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


class Report:
    """Numbered sections and PASS or FAIL lines, and the exit code they add up to."""

    def __init__(self) -> None:
        """Start an empty report."""
        self.checks: list[tuple[str, bool, str]] = []
        self._section = 0

    def section(self, title: str) -> None:
        """Print the next numbered heading."""
        self._section += 1
        print(f"[{self._section}] {title}")

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        """Record and print one check, returning ``ok``."""
        self.checks.append((label, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}{f' - {detail}' if detail else ''}")
        return ok

    def summary(self) -> int:
        """Print the tally and every failure, and return the exit code."""
        failures = [item for item in self.checks if not item[1]]
        print(f"\n{len(self.checks) - len(failures)} of {len(self.checks)} checks passed")
        for label, _ok, detail in failures:
            print(f"  FAILED: {label}{f' - {detail}' if detail else ''}")
        return 1 if failures else 0


def add_active_arguments(parser: argparse.ArgumentParser) -> None:
    """Add ``--active`` and ``--yes`` to a script's arguments."""
    parser.add_argument(
        "--active",
        action="store_true",
        help="also run the steps that change the printer; each is shown and confirmed first",
    )
    parser.add_argument(
        "--yes", action="store_true", help="with --active, do not ask before each step"
    )


def confirm(action: str, *, active: bool, assume_yes: bool) -> bool:
    """Whether a step that changes the printer may run: ``--active``, confirmed unless ``--yes``."""
    if not active:
        print(f"    skipped (read-only run): {action}")
        return False
    print(f"    about to: {action}")
    if assume_yes:
        return True
    return input("    send it? [y/N] ").strip().lower() in ("y", "yes")


async def async_refuse_resin(host: str, *, timeout: float = 3.0) -> bool:
    """Print why and return True when ``host`` answers SDCP discovery as a resin printer.
    The FDM tools check this first: a resin printer must never get their commands."""
    use_repository()
    from custom_components.generic_3dprinter import discovery
    from custom_components.generic_3dprinter.adapters import sdcp

    answer = await discovery.async_probe_udp(
        sdcp.SDCP_DISCOVERY_PROBE, (host, sdcp.SDCP_DISCOVERY_PORT), timeout
    )
    identity = sdcp.sdcp_identity(answer[0]) if answer is not None else {}
    if sdcp.classify_sdcp(identity) != "resin":
        return False
    model = identity.get("MachineName") or identity.get("Name") or "a resin printer"
    print(
        f"refused: {host} answers as {model}, a resin printer, and this tool sends FDM "
        "commands. Use tools/acceptance_sdcp_resin.py instead"
    )
    return True

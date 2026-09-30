"""Shared fixtures for the Generic 3D Printer Controller test suite."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import aiohttp
import pytest
import pytest_socket
from homeassistant.config_entries import ConfigEntryState

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Makes each fake printer's entities distinct within one pytest session.
_PRINTER_SEQUENCE = 0

# The Home Assistant test plugin is loaded through its pytest11 entry point.
pytest_plugins = ["pytest_homeassistant_custom_component"]

# Home Assistant discovers a custom integration by importing ``custom_components``
# and walking its ``__path__``. The test plugin ships its own ``custom_components``
# package under ``testing_config``, so whichever one is imported first wins, and
# the plugin wins by default. Claiming the namespace here - from this repository's
# root - is what makes the loader find this integration.
sys.path.insert(0, str(REPO_ROOT))
sys.modules.pop("custom_components", None)
custom_components = importlib.import_module("custom_components")
assert any(
    Path(item).resolve() == REPO_ROOT / "custom_components"
    for item in custom_components.__path__
), f"custom_components resolves to {list(custom_components.__path__)} instead of {REPO_ROOT / 'custom_components'}"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> Iterator[None]:
    """Make this custom integration discoverable by the Home Assistant test harness."""
    yield


@pytest.fixture(autouse=True)
def repo_config_dir(hass) -> Iterator[None]:
    """Point Home Assistant's config directory at this repository."""
    original = hass.config.config_dir
    hass.config.config_dir = str(REPO_ROOT)
    try:
        yield
    finally:
        hass.config.config_dir = original


@pytest.fixture(name="printer")
async def printer_fixture():
    """Run a fake SDCP printer for the duration of one test."""
    from tests.fake_printer import FakePrinterServer

    server = FakePrinterServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture(name="resin_printer")
async def resin_printer_fixture():
    """Run a fake Saturn 4 Ultra 16K for the duration of one test."""
    from tests.fake_resin_printer import FakeResinPrinter

    server = FakeResinPrinter()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture(name="config_entry")
async def config_entry_fixture(hass, printer):
    """Return an unloaded config entry pointing at the fake printer.

    The name carries a per-test counter, so two tests that both load a fake
    Centauri produce different entity ids. Without it the second test reuses the
    first test's entity ids and Home Assistant disambiguates them with a ``_2``
    suffix, which looks exactly like a naming bug in the integration.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.generic_3dprinter.const import DOMAIN

    global _PRINTER_SEQUENCE
    _PRINTER_SEQUENCE += 1
    name = f"Fake Centauri {_PRINTER_SEQUENCE}"

    port = int(printer.url.rsplit(":", 1)[1])
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=name,
        data={
            "name": name,
            "protocol": "sdcp_cc1",
            "host": "127.0.0.1",
            "port": port,
            # The fake serves its camera on its own origin, like the real hardware.
            "camera_port": printer.camera_port,
            "scan_interval": 5,
        },
        unique_id=f"sdcp_cc1:127.0.0.1:{port}:{_PRINTER_SEQUENCE}",
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
async def unload_entries_before_shutdown(hass) -> None:
    """Unload every config entry the test loaded, before the hass fixture stops.

    Home Assistant's shutdown waits for a still-loaded entry whose coordinator holds
    an open upstream socket, which turns a test that passes in a third of a second
    into a two-minute wait. Unloading first exercises the integration's own unload
    path, which is worth exercising in every test that loads an entry.
    """
    yield
    for entry in list(hass.config_entries.async_entries()):
        if entry.state is not ConfigEntryState.NOT_LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# aiohttp picks aiodns as its resolver whenever it is installed, and aiodns
# refuses to run on the Windows proactor event loop every test here uses
# ("aiodns needs a SelectorEventLoop on Windows"). The threaded resolver has no
# such constraint and is only used by the test suite; production keeps the
# aiohttp default.
_aiohttp_connector_init = aiohttp.TCPConnector.__init__


def _threaded_resolver_connector_init(self: aiohttp.TCPConnector, *args: object, **kwargs: object) -> None:
    kwargs.setdefault("resolver", aiohttp.ThreadedResolver())
    _aiohttp_connector_init(self, *args, **kwargs)


aiohttp.TCPConnector.__init__ = _threaded_resolver_connector_init  # type: ignore[method-assign]


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup() -> None:
    """Lift the socket block the Home Assistant plugin installs for every test.

    ``pytest_homeassistant_custom_component`` is auto-loaded through its
    ``pytest11`` entry point and replaces ``socket.socket`` with a guarded class
    that refuses every non-UNIX socket for the duration of each test, so the
    adapter tests could not stand up their own HTTP server on 127.0.0.1.
    ``enable_socket`` restores the real class before any fixture is created.
    """
    pytest_socket.enable_socket()

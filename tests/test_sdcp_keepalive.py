"""The Centauri Carbon's socket, kept the way the printer's own page keeps it.

A user's Centauri Carbon stopped working after a power cycle until its web page had
been opened. Measured on that printer, on firmware V1.4.49:

* a client that sends nothing is closed after 61 seconds, while one that sends the
  text ``ping`` every 30 seconds, as the page does, stays connected;
* the printer pushes its status only when asked, with command 0 or with a ping, so
  a status cached from before the power cycle was reported as current;
* the page switches the camera on with command 386 before it shows it.

The fake printer behaves the same way, with its timings scaled down, and these
tests drive the real adapter against it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import aiohttp
import pytest

from custom_components.generic_3dprinter.adapters import sdcp
from custom_components.generic_3dprinter.const import PrintState, ProtocolId
from custom_components.generic_3dprinter.protocols import ProtocolError, parse_config
from custom_components.generic_3dprinter.registry import ADAPTERS


@pytest.fixture(name="session")
async def session_fixture() -> AsyncIterator[aiohttp.ClientSession]:
    """Return a session for the adapter's socket and camera."""
    async with aiohttp.ClientSession() as session:
        yield session


def make_adapter(printer, session: aiohttp.ClientSession) -> sdcp.SdcpProtocol:
    config = parse_config(
        {
            "name": "Fake Centauri",
            "protocol": "sdcp_cc1",
            "host": "127.0.0.1",
            "port": int(printer.url.rsplit(":", 1)[1]),
            "camera_port": printer.camera_port,
        }
    )
    return sdcp.SdcpProtocol(config, session, granted=ADAPTERS[ProtocolId.SDCP_CC1].capabilities)


async def read_until_connected(adapter: sdcp.SdcpProtocol, attempts: int = 5):
    """Read until the adapter has reconnected, as the coordinator's polls would."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return await adapter.async_read()
        except ProtocolError as err:
            last = err
            await asyncio.sleep(0.1)
    raise AssertionError(f"the adapter never reconnected: {last}")


async def test_a_client_that_does_not_ping_is_dropped(
    printer, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fake drops a silent client, as the printer did after 61 seconds."""
    monkeypatch.setattr(sdcp, "HEARTBEAT_INTERVAL", 60.0)
    printer.ping_timeout = 0.3
    adapter = make_adapter(printer, session)
    try:
        await adapter.async_setup()
        await asyncio.sleep(0.6)
        assert adapter._connected is False  # noqa: SLF001
    finally:
        await adapter.async_teardown()


async def test_the_heartbeat_keeps_the_socket_open(
    printer, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdcp, "HEARTBEAT_INTERVAL", 0.1)
    printer.ping_timeout = 0.3
    adapter = make_adapter(printer, session)
    try:
        await adapter.async_setup()
        await asyncio.sleep(1.0)
        assert adapter._connected is True  # noqa: SLF001
        assert printer.pings >= 5
        assert printer.sent_commands.count(1) == 1, "the adapter had to reconnect"
    finally:
        await adapter.async_teardown()


async def test_the_status_is_asked_for_on_every_connection(
    printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(printer, session)
    try:
        await adapter.async_setup()
        assert printer.sent_commands[:2] == [1, 0], "the page's handshake asks for both"
        assert (await adapter.async_read()).print_state is PrintState.PRINTING
    finally:
        await adapter.async_teardown()


async def test_a_power_cycle_reports_the_new_status_not_the_cached_one(
    printer, session: aiohttp.ClientSession
) -> None:
    """The printer that finished or lost its job while off must not read as printing."""
    adapter = make_adapter(printer, session)
    try:
        await adapter.async_setup()
        assert (await adapter.async_read()).print_state is PrintState.PRINTING

        await printer.stop()
        printer.status["CurrentStatus"] = [0]
        printer.status["PrintInfo"]["Status"] = 0
        await printer.restart()

        snapshot = await read_until_connected(adapter)
        assert snapshot.connected is True
        assert snapshot.print_state is PrintState.IDLE
    finally:
        await adapter.async_teardown()


async def test_a_status_older_than_its_limit_is_asked_for_again(
    printer, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdcp, "STATUS_MAX_AGE", 0.1)
    adapter = make_adapter(printer, session)
    try:
        await adapter.async_setup()
        before = printer.sent_commands.count(0)
        printer.status["TempOfNozzle"] = 150.0
        await asyncio.sleep(0.2)
        snapshot = await adapter.async_read()
        assert printer.sent_commands.count(0) == before + 1
        assert snapshot.hotend.current == pytest.approx(150.0)
    finally:
        await adapter.async_teardown()


async def test_the_camera_is_switched_on_as_the_page_does(
    printer, session: aiohttp.ClientSession
) -> None:
    printer.camera_needs_enable = True
    adapter = make_adapter(printer, session)
    try:
        await adapter.async_setup()
        frame = await adapter.async_camera_frame()
        assert frame.startswith(b"\xff\xd8")
        assert [item["Data"] for item in printer.received if item["Cmd"] == 386] == [{"Enable": 1}]

        # A second frame over the same connection does not ask again.
        await adapter.async_camera_frame()
        assert printer.sent_commands.count(386) == 1

        # The printer comes back from a power cycle with its camera off.
        await printer.restart()
        await asyncio.sleep(0.1)
        frame = await adapter.async_camera_frame()
        assert frame.startswith(b"\xff\xd8")
        assert printer.sent_commands.count(386) == 2
    finally:
        await adapter.async_teardown()


async def test_a_socket_the_printer_went_silent_on_is_not_trusted() -> None:
    config = parse_config({"name": "t", "protocol": "sdcp_cc1", "host": "127.0.0.1"})
    adapter = sdcp.SdcpProtocol(config, object(), granted=frozenset())  # type: ignore[arg-type]
    adapter._ws = type("Ws", (), {"closed": False})()  # noqa: SLF001
    adapter._reader = asyncio.get_running_loop().create_future()  # noqa: SLF001
    adapter._last_frame_at = time.monotonic()  # noqa: SLF001
    assert adapter._connected is True  # noqa: SLF001
    adapter._last_frame_at = time.monotonic() - sdcp.SILENCE_TIMEOUT - 1  # noqa: SLF001
    assert adapter._connected is False  # noqa: SLF001


async def test_a_reset_closes_the_socket_it_drops() -> None:
    """A dropped socket left open would hold one of the printer's five client slots."""
    closed: list[bool] = []

    class Ws:
        closed = False

        async def close(self) -> None:
            closed.append(True)

    config = parse_config({"name": "t", "protocol": "sdcp_cc1", "host": "127.0.0.1"})
    adapter = sdcp.SdcpProtocol(config, object(), granted=frozenset())  # type: ignore[arg-type]
    reader = asyncio.get_running_loop().create_future()
    adapter._ws = Ws()  # noqa: SLF001
    adapter._reader = reader  # noqa: SLF001
    adapter._status = {"PrintInfo": {"Status": 13}}  # noqa: SLF001
    await adapter._reset_socket()  # noqa: SLF001
    assert closed == [True]
    assert reader.cancelled()
    assert adapter._ws is None  # noqa: SLF001
    assert adapter._status == {}, "a status from before the reset would be reported as current"  # noqa: SLF001


async def test_the_identity_is_checked_after_the_attributes_and_the_status(
    printer, session: aiohttp.ClientSession
) -> None:
    """A subclass sees the attributes, then the status, before the session is ready."""
    seen: list[tuple[list[int], bool, bool]] = []

    class Checked(sdcp.SdcpProtocol):
        async def _check_identity(self) -> None:
            seen.append((list(printer.sent_commands), bool(self._attributes), bool(self._status)))

    config = make_adapter(printer, session).config
    adapter = Checked(config, session, granted=ADAPTERS[ProtocolId.SDCP_CC1].capabilities)
    try:
        await adapter.async_setup()
        assert seen == [([1], True, False), ([1, 0], True, True)]
    finally:
        await adapter.async_teardown()

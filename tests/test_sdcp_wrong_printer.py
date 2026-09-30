"""A Centauri Carbon entry that reaches a resin printer stops at command 1 and sends it nothing else."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_3dprinter.adapters import sdcp
from custom_components.generic_3dprinter.const import DOMAIN, Command, ProtocolId
from custom_components.generic_3dprinter.protocols import (
    ConfigError,
    ProtocolError,
    WrongPrinterError,
    parse_config,
)
from custom_components.generic_3dprinter.registry import ADAPTERS
from tests.adapter_kit.harness import SAMPLE_PARAMS
from tests.fake_resin_printer import FakeResinPrinter, load_fixture


@pytest.fixture(name="session")
async def session_fixture() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


def _config(printer: FakeResinPrinter):
    return parse_config(
        {"name": "Stale Centauri", "protocol": "sdcp_cc1", "host": "127.0.0.1", "port": printer.port}
    )


def _adapter(printer: FakeResinPrinter, session: aiohttp.ClientSession) -> sdcp.SdcpProtocol:
    return sdcp.SdcpProtocol(
        _config(printer), session, granted=ADAPTERS[ProtocolId.SDCP_CC1].capabilities
    )


async def _closed(printer: FakeResinPrinter) -> None:
    for _ in range(50):
        if printer.connections == 0:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the adapter left its socket open")


async def test_the_attributes_stop_it_at_command_one(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    adapter = _adapter(resin_printer, session)
    with pytest.raises(WrongPrinterError, match="Saturn 4 Ultra 16K") as caught:
        await adapter.async_setup()
    assert caught.value.model == "Saturn 4 Ultra 16K"
    assert resin_printer.sent_commands == [1]
    assert adapter._ws is None  # noqa: SLF001
    await _closed(resin_printer)

    # Every command either is refused before the wire or reconnects and stops again.
    for command in Command:
        with pytest.raises(ProtocolError):
            await adapter.async_send(command, **SAMPLE_PARAMS[command])
    with pytest.raises(WrongPrinterError):
        await adapter.async_read()
    assert set(resin_printer.sent_commands) == {1}
    assert resin_printer.forbidden == []
    assert not resin_printer.crashed
    await adapter.async_teardown()


async def test_without_attributes_the_status_stops_it(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdcp, "PUSH_TIMEOUT", 0.2)
    resin_printer.withhold_attributes = True
    adapter = _adapter(resin_printer, session)
    with pytest.raises(WrongPrinterError):
        await adapter.async_setup()
    assert resin_printer.sent_commands == [1, 0]
    assert resin_printer.forbidden == []
    await _closed(resin_printer)
    await adapter.async_teardown()


async def test_an_unanswered_command_one_closes_the_socket(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A printer that ignores the Centauri's envelope leaves no unchecked socket behind."""
    monkeypatch.setattr(sdcp, "ACK_TIMEOUT", 0.2)
    resin_printer.strict_envelope = True
    adapter = _adapter(resin_printer, session)
    with pytest.raises(ProtocolError) as caught:
        await adapter.async_setup()
    assert not isinstance(caught.value, WrongPrinterError)
    assert adapter._ws is None  # noqa: SLF001
    assert [item["Cmd"] for item in resin_printer.ignored] == [1]
    await _closed(resin_printer)
    await adapter.async_teardown()


# ---------------------------------------------------------------- the entry


def _entry(hass: HomeAssistant, printer: FakeResinPrinter) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Stale Centauri",
        data={
            "name": "Stale Centauri",
            "protocol": "sdcp_cc1",
            "host": "127.0.0.1",
            "port": printer.port,
            "scan_interval": 5,
        },
        unique_id=f"sdcp_cc1:127.0.0.1:{printer.port}",
    )
    entry.add_to_hass(hass)
    return entry


async def test_the_entry_fails_for_good_and_sends_nothing(
    hass: HomeAssistant, hass_ws_client: Any, resin_printer: FakeResinPrinter
) -> None:
    entry = _entry(hass, resin_printer)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "Saturn 4 Ultra 16K" in (entry.reason or "")

    websocket = await hass_ws_client(hass)
    for index, command in enumerate(Command, start=1):
        await websocket.send_json(
            {
                "id": index,
                "type": "generic_3dprinter/send",
                "entry_id": entry.entry_id,
                "command": command.value,
                "data": SAMPLE_PARAMS[command],
            }
        )
        assert not (await websocket.receive_json())["success"]
    assert resin_printer.sent_commands == [1]
    assert resin_printer.forbidden == []
    await _closed(resin_printer)


async def test_an_unanswered_command_one_is_retried(
    hass: HomeAssistant, resin_printer: FakeResinPrinter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sdcp, "ACK_TIMEOUT", 0.2)
    resin_printer.strict_envelope = True
    entry = _entry(hass, resin_printer)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert resin_printer.forbidden == []
    await _closed(resin_printer)


# ------------------------------------------------------------ the config flow


def _probe(reply: dict[str, Any] | None, seen: list[Any]) -> Any:
    async def probe(probe: bytes, target: tuple[str, int], timeout: float, accept: Any = None):
        seen.append((probe, target))
        return None if reply is None else (reply, target[0])

    return probe


async def test_prepare_config_refuses_a_resin_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    saturn = load_fixture()["attributes"]["Attributes"]
    seen: list[Any] = []
    monkeypatch.setattr(sdcp, "async_probe_udp", _probe({"Id": "x", "Data": {"Attributes": saturn}}, seen))
    config = parse_config({"name": "p", "protocol": "sdcp_cc1", "host": "192.0.2.43"})
    with pytest.raises(ConfigError, match="Saturn 4 Ultra 16K"):
        await sdcp.SdcpProtocol.async_prepare_config(config)
    assert seen == [(b"M99999", ("192.0.2.43", 3000))]


@pytest.mark.parametrize(
    "reply",
    [None, {"Id": "x", "Data": {"MachineName": "Centauri Carbon", "MainboardID": "5c44"}}],
)
async def test_prepare_config_keeps_a_centauri_or_a_silent_printer(
    monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any] | None
) -> None:
    monkeypatch.setattr(sdcp, "async_probe_udp", _probe(reply, []))
    config = parse_config({"name": "p", "protocol": "sdcp_cc1", "host": "192.0.2.43"})
    assert await sdcp.SdcpProtocol.async_prepare_config(config) == config

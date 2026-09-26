"""An Anycubic Kobra X inside a real Home Assistant, against the fake printer."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_3dprinter.adapters import anycubic_kobra as kobra
from custom_components.generic_3dprinter.const import DOMAIN
from custom_components.generic_3dprinter.registry import protocol_menu
from tests.fake_kobra_printer import SERIAL, FakeKobraPrinter


@pytest.fixture(name="printer")
async def printer_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeKobraPrinter]:
    monkeypatch.setattr(kobra, "ANSWER_WINDOW", 0.3)
    monkeypatch.setattr(kobra, "INFO_WAIT", 0.3)
    printer = FakeKobraPrinter()
    await printer.start()
    yield printer
    await printer.stop()
    printer.close()


def test_the_kobra_is_in_the_protocol_menu() -> None:
    assert ("anycubic_kobra", "Anycubic Kobra (LAN mode)") in protocol_menu()


async def test_a_kobra_x_entry(hass: HomeAssistant, hass_ws_client, printer: FakeKobraPrinter) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Kobra X",
        data={
            "name": "Kobra X",
            "protocol": "anycubic_kobra",
            "host": "127.0.0.1",
            "port": printer.port,
            "serial": SERIAL,
            "scan_interval": 5,
        },
        unique_id=f"anycubic_kobra:{SERIAL}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    ids = {item.unique_id.removeprefix(f"{entry.entry_id}_") for item in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert {"camera", "pause", "home", "auto_refill"} <= ids
    assert "chamber_temperature" not in ids

    camera = hass.states.get(registry.async_get_entity_id("camera", DOMAIN, f"{entry.entry_id}_camera"))
    assert camera is not None
    assert printer.stream_starts == 0, "setting up the camera must not start the printer's capture"

    home = hass.states.get(registry.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_home"))
    assert home.attributes["blocked_reason"] == "the printer is not idle"

    websocket = await hass_ws_client(hass)
    await websocket.send_json({"id": 1, "type": "generic_3dprinter/describe", "entry_id": entry.entry_id})
    described = (await websocket.receive_json())["result"]
    assert described["camera_kind"] == "stream"
    assert described["camera_url"] is None
    assert described["camera_entity_id"] == camera.entity_id
    assert described["model_profile"] == {"id": "20030", "name": "Anycubic Kobra X", "verified": False}
    assert described["printer"]["filament"]["units"][0]["name"] == "Multi-colour unit"

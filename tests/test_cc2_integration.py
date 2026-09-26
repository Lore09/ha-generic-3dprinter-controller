"""The Centauri Carbon 2 inside a real Home Assistant: the config flow and one entry.

The flow test walks the path a user takes: the protocol menu shows the Centauri
Carbon once, a second step asks which model, and the Centauri Carbon 2 gets its
serial number from discovery. The entry test loads a Centauri Carbon 2 against the
fake printer and checks that the entities are the ones its protocol can back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_3dprinter.adapters import elegoo_cc2 as cc2
from custom_components.generic_3dprinter.const import (
    DATA_COORDINATORS,
    DATA_RUNTIMES,
    DOMAIN,
    Command,
)
from tests.fake_cc2_printer import SERIAL, FakeCC2Printer

LIVE_RESULT = {
    "host_name": "CC2 QAZJ",
    "machine_model": "Centauri Carbon 2",
    "protocol_version": "1.0.0",
    "sn": SERIAL,
    "token_status": 0,
}


def discovery_answering(lan_status: int):
    """Return a stand-in for the CC2 discovery request that answers like the printer."""

    async def fake(host: str | None = None, timeout: float = 0):
        reply = {"id": 0, "result": {**LIVE_RESULT, "lan_status": lan_status}}
        return cc2.parse_cc2_reply(reply, host or "192.0.2.10")

    return fake


def select_values(result: dict[str, Any]) -> list[str]:
    """Return the option values of the single select field of a form."""
    (selector,) = result["data_schema"].schema.values()
    return [option["value"] for option in selector.config["options"]]


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the adapter's waits for the suite."""
    monkeypatch.setattr(cc2, "REQUEST_GAP", 0.0)
    monkeypatch.setattr(cc2, "REGISTER_TIMEOUT", 0.3)
    monkeypatch.setattr(cc2, "ACK_TIMEOUT", 0.5)


@pytest.fixture(name="cc2_printer")
async def cc2_printer_fixture() -> AsyncIterator[FakeCC2Printer]:
    """Run a fake Centauri Carbon 2."""
    server = FakeCC2Printer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _to_model_step(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"discover": False})
    assert result["step_id"] == "protocol"
    values = select_values(result)
    assert "elegoo_centauri" in values
    assert "sdcp_cc1" not in values and "elegoo_cc2" not in values, values
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"protocol": "elegoo_centauri"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "model"
    assert select_values(result) == ["sdcp_cc1", "elegoo_cc2"]
    return result


async def test_the_flow_asks_for_the_model_and_learns_the_serial(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cc2, "async_discover_cc2", discovery_answering(lan_status=1))
    with patch("custom_components.generic_3dprinter.async_setup_entry", return_value=True):
        result = await _to_model_step(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"protocol": "elegoo_cc2"}
        )
        assert result["step_id"] == "details"
        assert result["description_placeholders"]["protocol"] == "Elegoo MQTT (Centauri Carbon 2)"
        fields = {str(key) for key in result["data_schema"].schema}
        assert {"serial", "access_code", "camera_port"} <= fields
        assert "web_url" not in fields and "tls" not in fields

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Workshop CC2", "host": "192.0.2.10"}
        )
        assert result["step_id"] == "unsafe"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["protocol"] == "elegoo_cc2"
    assert result["data"]["serial"] == SERIAL
    assert result["data"]["port"] == 1883
    assert result["data"]["unsafe_enabled"] == []


async def test_the_flow_explains_cloud_mode(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cc2, "async_discover_cc2", discovery_answering(lan_status=0))
    result = await _to_model_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"protocol": "elegoo_cc2"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Workshop CC2", "host": "192.0.2.10"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "details"
    assert result["errors"] == {"base": "invalid_config"}
    assert "LAN Only Mode" in result["description_placeholders"]["detail"]


async def test_the_first_centauri_is_still_reached_through_the_family(
    hass: HomeAssistant,
) -> None:
    result = await _to_model_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"protocol": "sdcp_cc1"}
    )
    assert result["step_id"] == "details"
    assert result["description_placeholders"]["protocol"] == "Elegoo SDCP (Centauri Carbon)"
    assert "serial" not in {str(key) for key in result["data_schema"].schema}


def cc2_entry(hass: HomeAssistant, printer: FakeCC2Printer) -> MockConfigEntry:
    """Add an entry for the fake printer, not yet set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Fake CC2",
        data={
            "name": "Fake CC2",
            "protocol": "elegoo_cc2",
            "host": "127.0.0.1",
            "port": printer.port,
            "camera_port": printer.camera_port,
            "serial": SERIAL,
            "scan_interval": 5,
        },
        unique_id=f"elegoo_cc2:127.0.0.1:{printer.port}",
    )
    entry.add_to_hass(hass)
    return entry


async def test_the_card_api(
    hass: HomeAssistant,
    hass_ws_client,
    hass_client,
    cc2_printer: FakeCC2Printer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cc2, "UPLOAD_PORT", cc2_printer.upload_port)
    entry = cc2_entry(hass, cc2_printer)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    websocket = await hass_ws_client(hass)
    await websocket.send_json({"id": 1, "type": "generic_3dprinter/list"})
    listed = await websocket.receive_json()
    assert listed["success"], listed
    (printer,) = listed["result"]["printers"]
    assert printer["protocol"] == "elegoo_cc2"
    # The state sensor is found by its real unique id.
    assert printer["entity_id"] and printer["entity_id"].startswith("sensor.")

    await websocket.send_json({"id": 2, "type": "generic_3dprinter/files", "entry_id": entry.entry_id})
    files = await websocket.receive_json()
    assert [item["name"] for item in files["result"]["files"]] == ["benchy.gcode", "cube.gcode"]

    await websocket.send_json(
        {
            "id": 3,
            "type": "generic_3dprinter/send",
            "entry_id": entry.entry_id,
            "command": "set_fan_speed",
            "data": {"value": 50, "channel": "model"},
        }
    )
    assert (await websocket.receive_json())["success"]
    assert cc2_printer.params_of(1030) == [{"fan": 128}]

    import aiohttp

    client = await hass_client()
    url = f"/api/generic_3dprinter/{entry.entry_id}/upload"

    form = aiohttp.FormData()
    form.add_field("file", b"G28\nG1 X10\n", filename="part.gcode")
    response = await client.post(url, data=form)
    assert response.status == 200, await response.text()
    assert (await response.json())["file"]["name"] == "part.gcode"
    assert cc2_printer.uploads[-1]["body"] == b"G28\nG1 X10\n"

    form = aiohttp.FormData()
    form.add_field("file", b"not a job", filename="notes.txt")
    response = await client.post(url, data=form)
    assert response.status == 400

    form = aiohttp.FormData()
    form.add_field("file", b"G28\n", filename="../../etc/part.gcode")
    response = await client.post(url, data=form)
    assert response.status == 200
    assert cc2_printer.uploads[-1]["headers"]["X-File-Name"] == "part.gcode"

    response = await client.post("/api/generic_3dprinter/nope/upload", data=b"")
    assert response.status == 404


async def test_a_centauri_carbon_2_entry(
    hass: HomeAssistant, cc2_printer: FakeCC2Printer
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Fake CC2",
        data={
            "name": "Fake CC2",
            "protocol": "elegoo_cc2",
            "host": "127.0.0.1",
            "port": cc2_printer.port,
            "camera_port": cc2_printer.camera_port,
            "serial": SERIAL,
            "scan_interval": 5,
        },
        unique_id=f"elegoo_cc2:127.0.0.1:{cc2_printer.port}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entries = [
        item for item in er.async_get(hass).entities.values() if item.config_entry_id == entry.entry_id
    ]
    keys = {item.translation_key for item in entries}

    def state(key: str) -> str:
        match = next(item for item in entries if item.translation_key == key)
        return hass.states.get(match.entity_id).state

    # What the protocol backs is there.
    assert {"pause", "resume", "stop", "home", "nozzle_target", "bed_target", "chamber_light"} <= keys
    assert {"fan_model_speed", "fan_chamber_speed", "speed_factor", "chamber_temperature"} <= keys
    assert {"position_x", "position_y", "position_z"} <= keys
    # What it cannot back is not: no chamber heater and no flow factor.
    assert not {"chamber_target", "chamber_target_temperature", "flow_factor"} & keys
    assert any(item.entity_id.startswith("camera.") for item in entries)

    assert state("printer_state") == "printing"
    assert float(state("progress")) == pytest.approx(45.0)
    assert float(state("chamber_temperature")) == pytest.approx(33.0)
    assert float(state("total_layers")) == pytest.approx(500.0)
    assert state("chamber_light") == "on"
    assert state("online") == "on"

    device = next(
        item for item in dr.async_get(hass).devices.values() if entry.entry_id in item.config_entries
    )
    assert device.model == "Centauri Carbon 2"
    assert device.sw_version == "02.01.00.00"

    coordinator = hass.data[DATA_COORDINATORS][entry.entry_id]
    await coordinator.async_send_command(Command.PAUSE)
    assert 1021 in cc2_printer.methods

    description = hass.data[DATA_RUNTIMES][entry.entry_id].describe()
    assert description["protocol"] == "elegoo_cc2"
    assert description["web_ui"] is False
    assert [item["id"] for item in description["unsafe_features"]] == ["cc2_start_print"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert cc2_printer.registered == [], "the unload left the printer's client slot taken"


async def test_a_blocked_control_says_why(
    hass: HomeAssistant, cc2_printer: FakeCC2Printer
) -> None:
    """Mid-print, home stays available and carries the printer's reason; pause does not."""
    from homeassistant.helpers import entity_registry as er

    entry = cc2_entry(hass, cc2_printer)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)

    def state_of(key: str):
        entity_id = registry.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_{key}")
        assert entity_id, key
        return hass.states.get(entity_id)

    home = state_of("home")
    assert home.state != "unavailable"
    assert home.attributes["blocked_reason"] == "the printer is not idle"
    assert "blocked_reason" not in state_of("pause").attributes

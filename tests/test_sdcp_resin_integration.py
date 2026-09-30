"""An Elegoo resin printer inside a real Home Assistant: the config flow and one entry."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_3dprinter.adapters import sdcp_resin
from custom_components.generic_3dprinter.const import DOMAIN
from custom_components.generic_3dprinter.registry import protocol_menu
from tests.fake_printer import MAINBOARD as CC1_MAINBOARD
from tests.fake_printer import FakePrinterServer
from tests.fake_resin_printer import FakeResinPrinter, load_fixture

ATTRIBUTES = load_fixture()["attributes"]["Attributes"]
SATURN = ATTRIBUTES["MainboardID"]
LABEL = "Elegoo resin (Saturn, Mars) – SDCP"

#: Every entity an idle Saturn 4 Ultra 16K gets, by its unique id's key.
EXPECTED = frozenset(
    {
        "online",
        "active_job",
        "printer_state",
        "progress",
        "current_layer",
        "total_layers",
        "remaining_time",
        "elapsed_time",
        "filename",
        "print_phase",
        "uv_led_temperature",
        "release_film",
        "vat_temperature",
        "vat_target_temperature",
        "pause",
        "resume",
        "stop",
        "ip_address",
        "protocol",
        "firmware",
        "model",
        "serial",
    }
)

#: What a resin printer must never be given, as a key or as a word in one.
NEVER = (
    "nozzle",
    "bed",
    "chamber",
    "fan",
    "position",
    "speed",
    "flow",
    "light",
    "home",
    "filament",
    "camera",
    "timelapse",
)


def _probe(reply: dict[str, Any] | None) -> Any:
    async def probe(probe: bytes, target: tuple[str, int], timeout: float, accept: Any = None):
        return None if reply is None else (reply, target[0])

    return probe


def test_the_resin_printer_is_in_the_protocol_menu() -> None:
    assert ("sdcp_resin", LABEL) in protocol_menu()


async def _details(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"discover": False})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"protocol": "sdcp_resin"})
    assert result["step_id"] == "details"
    assert result["description_placeholders"]["protocol"] == LABEL
    fields = {str(key) for key in result["data_schema"].schema}
    assert {"host", "port", "serial"} <= fields
    assert "camera_port" not in fields and "web_url" not in fields
    return result


async def test_the_flow_learns_the_mainboard_id(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdcp_resin, "async_probe_udp", _probe({"Id": "x", "Data": ATTRIBUTES}))
    with patch("custom_components.generic_3dprinter.async_setup_entry", return_value=True):
        result = await _details(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Saturn", "host": "192.0.2.43"}
        )
        assert result["step_id"] == "unsafe"
        assert "sdcp_resin_start_print" in str(result["data_schema"].schema)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"]["protocol"] == "sdcp_resin"
    assert result["data"]["serial"] == SATURN
    assert result["data"]["port"] == 3030
    assert result["data"]["unsafe_enabled"] == []
    assert result["result"].unique_id == f"sdcp_resin:{SATURN}"


@pytest.mark.parametrize(
    ("reply", "detail"),
    [
        ({"Id": "x", "Data": {"MachineName": "Centauri Carbon", "MainboardID": CC1_MAINBOARD}}, "not a resin"),
        ({"Id": "x", "Data": {**ATTRIBUTES, "ProtocolVersion": "V1.0.0"}}, "MQTT"),
        (None, "did not answer"),
    ],
    ids=["fdm", "sdcp-v1", "silent"],
)
async def test_the_flow_refuses(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any] | None, detail: str
) -> None:
    monkeypatch.setattr(sdcp_resin, "async_probe_udp", _probe(reply))
    result = await _details(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Saturn", "host": "192.0.2.43"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert detail in result["description_placeholders"]["detail"]


def _entry(hass: HomeAssistant, port: int, serial: str, **extra: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Saturn",
        data={
            "name": "Saturn",
            "protocol": "sdcp_resin",
            "host": "127.0.0.1",
            "port": port,
            "serial": serial,
            "scan_interval": 5,
            **extra,
        },
        unique_id=f"sdcp_resin:{serial}",
    )
    entry.add_to_hass(hass)
    return entry


async def test_a_saturn_entry_has_only_resin_entities(
    hass: HomeAssistant, hass_ws_client: Any, resin_printer: FakeResinPrinter
) -> None:
    entry = _entry(hass, resin_printer.port, SATURN)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    keys = {
        item.unique_id.removeprefix(f"{entry.entry_id}_")
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert keys == EXPECTED
    assert not [key for key in keys for word in NEVER if word in key]
    assert not [item for item in er.async_entries_for_config_entry(registry, entry.entry_id)
                if item.domain in ("number", "switch", "camera")]

    phase = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_print_phase")
    assert hass.states.get(phase).state == "idle"
    vat = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_vat_temperature")
    assert float(hass.states.get(vat).state) == 29

    websocket = await hass_ws_client(hass)
    await websocket.send_json({"id": 1, "type": "generic_3dprinter/describe", "entry_id": entry.entry_id})
    described = (await websocket.receive_json())["result"]
    assert described["camera_kind"] is None
    assert described["model_profile"] == {
        "id": "Saturn 4 Ultra 16K",
        "name": "Elegoo Saturn 4 Ultra 16K",
        "verified": True,
    }
    assert described["printer"]["resin"]["machine"] == "idle"
    assert "start_print" not in described["printer"]["capabilities"]
    assert [item["id"] for item in described["unsafe_features"]] == ["sdcp_resin_start_print"]
    assert described["printer"]["progress"] is None
    assert described["printer"]["filename"] == "SUP_allineatore_01_1_202609301434.goo"

    await websocket.send_json({"id": 2, "type": "generic_3dprinter/send", "entry_id": entry.entry_id,
                               "command": "set_hotend_temp", "data": {"value": 50}})
    assert not (await websocket.receive_json())["success"]
    assert set(resin_printer.sent_commands) <= {0, 1}
    assert resin_printer.forbidden == []
    assert resin_printer.texts == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_resin_entry_at_a_centauri_carbon_fails_for_good(hass: HomeAssistant) -> None:
    printer = FakePrinterServer()
    await printer.start()
    try:
        entry = _entry(hass, int(printer.url.rsplit(":", 1)[1]), CC1_MAINBOARD)
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert "Centauri Carbon" in (entry.reason or "")
        assert "not a resin printer" in (entry.reason or "")
        assert printer.sent_commands == [1]
    finally:
        await printer.stop()


async def test_the_pause_button_sends_129_with_an_empty_data(
    hass: HomeAssistant, resin_printer: FakeResinPrinter
) -> None:
    entry = _entry(hass, resin_printer.port, SATURN)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    button = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_pause")
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    acts = [(item["Cmd"], item["Data"]) for item in resin_printer.received if item["Cmd"] not in (0, 1, 258)]
    assert acts == [(129, {})]
    assert resin_printer.forbidden == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_card_uploads_a_goo_file_to_the_socket_port(
    hass: HomeAssistant, hass_client: Any, hass_ws_client: Any, resin_printer: FakeResinPrinter
) -> None:
    import aiohttp

    entry = _entry(hass, resin_printer.port, SATURN)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    websocket = await hass_ws_client(hass)
    await websocket.send_json({"id": 1, "type": "generic_3dprinter/describe", "entry_id": entry.entry_id})
    described = (await websocket.receive_json())["result"]
    assert described["upload_suffixes"] == [".ctb", ".goo"]
    assert "file_upload" in described["printer"]["capabilities"]

    client = await hass_client()
    url = f"/api/generic_3dprinter/{entry.entry_id}/upload"
    form = aiohttp.FormData()
    form.add_field("file", b"GOO layers", filename="part.goo")
    response = await client.post(url, data=form)
    assert response.status == 200, await response.text()
    assert (await response.json())["file"]["path"] == "/local/part.goo"
    assert resin_printer.uploads[-1]["File"] == b"GOO layers"
    assert {"name": "/local/part.goo", "type": 1} in resin_printer.files

    form = aiohttp.FormData()
    form.add_field("file", b"G28\n", filename="part.gcode")
    response = await client.post(url, data=form)
    assert response.status == 400
    assert ".ctb, .goo" in await response.text()
    assert len(resin_printer.uploads) == 1
    assert set(resin_printer.sent_commands) <= {0, 1, 258}
    assert resin_printer.forbidden == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_opted_in_entry_starts_a_listed_file_with_two_fields(
    hass: HomeAssistant, hass_ws_client: Any, resin_printer: FakeResinPrinter
) -> None:
    """The card sends the listed path; the printer gets its bare name and layer 0, nothing more."""
    entry = _entry(hass, resin_printer.port, SATURN, unsafe_enabled=["sdcp_resin_start_print"])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    websocket = await hass_ws_client(hass)
    await websocket.send_json({"id": 1, "type": "generic_3dprinter/describe", "entry_id": entry.entry_id})
    described = (await websocket.receive_json())["result"]
    assert "start_print" in described["printer"]["capabilities"]
    assert described["unsafe_features"] == []

    await websocket.send_json({"id": 2, "type": "generic_3dprinter/send", "entry_id": entry.entry_id,
                               "command": "start_print",
                               "data": {"filename": "/local/SUP_allineatore_01_1_202609301434.goo"}})
    assert (await websocket.receive_json())["success"]
    acts = [(item["Cmd"], item["Data"]) for item in resin_printer.received if item["Cmd"] not in (0, 1, 258)]
    assert acts == [(128, {"Filename": "SUP_allineatore_01_1_202609301434.goo", "StartLayer": 0})]
    assert resin_printer.forbidden == []
    assert not resin_printer.crashed
    assert await hass.config_entries.async_unload(entry.entry_id)

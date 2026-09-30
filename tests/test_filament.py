"""Multi-material units: Elegoo's CANVAS on both Centauri Carbon models.

The parser is tested on the answer a live Centauri Carbon gave to command 324 and
on the Centauri Carbon 2's documented answer to method 2005. The Centauri Carbon
adapter is tested without a socket, because what matters is when it asks: only
while its status says a unit is connected, since an unknown command crashes that
printer. The Centauri Carbon 2 adapter is driven against the fake printer, whose
CANVAS loads, unloads and records filament the way the printer's own page expects.
The last tests load a real entry and check the entities and the card's document.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_3dprinter.adapters import elegoo_canvas as canvas
from custom_components.generic_3dprinter.adapters import elegoo_cc2 as cc2
from custom_components.generic_3dprinter.adapters import sdcp
from custom_components.generic_3dprinter.const import (
    DATA_COORDINATORS,
    DATA_RUNTIMES,
    DOMAIN,
    Capability,
    Command,
    ProtocolId,
)
from custom_components.generic_3dprinter.protocols import (
    PrinterConfig,
    ProtocolError,
    parse_config,
)
from custom_components.generic_3dprinter.registry import ADAPTERS
from custom_components.generic_3dprinter.validation import ParamError, validate_params
from tests.fake_cc2_printer import SERIAL, FakeCC2Printer

#: The answer a live Centauri Carbon with a CANVAS gave to command 324 on firmware
#: V1.4.49: two empty trays, two loaded ones, nothing in the nozzle.
LIVE_CC1_CANVAS: dict[str, Any] = {
    "active_canvas_id": 0,
    "active_tray_id": -1,
    "auto_refill": 1,
    "canvas_list": [
        {
            "canvas_id": 0,
            "connected": 1,
            "tray_list": [
                {
                    "tray_id": 0,
                    "brand": "Generic",
                    "filament_type": "PETG",
                    "filament_name": "PETG PRO",
                    "filament_code": "0x00000",
                    "filament_color": "#000000",
                    "min_nozzle_temp": 230,
                    "max_nozzle_temp": 260,
                    "status": 0,
                },
                {
                    "tray_id": 1,
                    "brand": "Generic",
                    "filament_type": "PLA",
                    "filament_name": "PLA+",
                    "filament_code": "0x00000",
                    "filament_color": "#898989",
                    "min_nozzle_temp": 190,
                    "max_nozzle_temp": 230,
                    "status": 0,
                },
                {
                    "tray_id": 2,
                    "brand": "Generic",
                    "filament_type": "PLA",
                    "filament_name": "PLA+",
                    "filament_code": "0x00000",
                    "filament_color": "#898989",
                    "min_nozzle_temp": 190,
                    "max_nozzle_temp": 230,
                    "status": 1,
                },
                {
                    "tray_id": 3,
                    "brand": "Generic",
                    "filament_type": "PETG",
                    "filament_name": "PETG PRO",
                    "filament_code": "0x00000",
                    "filament_color": "#000000",
                    "min_nozzle_temp": 230,
                    "max_nozzle_temp": 260,
                    "status": 1,
                },
            ],
        }
    ],
    "Ack": 0,
}


# ------------------------------------------------------------------ parsing


def test_the_live_centauri_carbon_answer_is_understood() -> None:
    system = canvas.parse_canvas(LIVE_CC1_CANVAS)
    assert system is not None
    (unit,) = system.units
    assert unit.unit == 0 and unit.connected is True and unit.name == "CANVAS"
    assert [slot.slot for slot in unit.slots] == [0, 1, 2, 3]
    assert [slot.loaded for slot in unit.slots] == [False, False, True, True]
    assert system.active is None, "active_tray_id -1 means nothing is in the nozzle"
    assert system.auto_refill is True
    third = system.slot(0, 2)
    assert (third.material, third.name, third.brand) == ("PLA", "PLA+", "Generic")
    assert third.color == "#898989"
    assert (third.min_temp, third.max_temp) == (190.0, 230.0)
    # An empty tray keeps the filament it last held on record.
    assert system.slot(0, 0).name == "PETG PRO"


def test_the_active_tray_is_found_by_its_status_or_by_the_active_ids() -> None:
    by_status = copy.deepcopy(LIVE_CC1_CANVAS)
    by_status["canvas_list"][0]["tray_list"][3]["status"] = 2
    assert canvas.parse_canvas(by_status).active.slot == 3

    by_ids = copy.deepcopy(LIVE_CC1_CANVAS)
    by_ids["active_tray_id"] = 2
    active = canvas.parse_canvas(by_ids).active
    assert (active.unit, active.slot) == (0, 2)


def test_an_empty_tray_is_never_the_active_one() -> None:
    info = copy.deepcopy(LIVE_CC1_CANVAS)
    info["active_tray_id"] = 0
    assert canvas.parse_canvas(info).active is None


def test_placeholders_and_bad_values_are_dropped() -> None:
    info = copy.deepcopy(LIVE_CC1_CANVAS)
    tray = info["canvas_list"][0]["tray_list"][2]
    tray.update(
        {
            "brand": "— —",
            "filament_type": "?",
            "filament_name": "",
            "filament_color": "not a colour",
            "min_nozzle_temp": 0,
        }
    )
    slot = canvas.parse_canvas(info).slot(0, 2)
    assert slot.brand is None and slot.material is None and slot.name is None
    assert slot.color is None and slot.min_temp is None
    assert slot.loaded is True


def test_a_name_falls_back_to_the_material_and_a_colour_gets_its_hash() -> None:
    info = copy.deepcopy(LIVE_CC1_CANVAS)
    tray = info["canvas_list"][0]["tray_list"][2]
    tray.update({"filament_name": "", "filament_color": "f32ff8"})
    slot = canvas.parse_canvas(info).slot(0, 2)
    assert slot.name == "PLA"
    assert slot.color == "#F32FF8"


@pytest.mark.parametrize("info", [None, {}, {"canvas_list": []}, {"canvas_list": "x"}, "x"])
def test_no_unit_is_no_system(info: Any) -> None:
    assert canvas.parse_canvas(info) is None


def test_the_edit_payload_carries_the_code_and_temperatures_of_its_preset() -> None:
    payload = canvas.edit_payload(
        {"unit": 0, "slot": 2, "material": "PLA", "name": "PLA Matte", "color": "#112233"}
    )
    assert payload == {
        "canvas_id": 0,
        "tray_id": 2,
        "brand": "Generic",
        "filament_type": "PLA",
        "filament_name": "PLA Matte",
        "filament_code": "0x0006",
        "filament_color": "#112233",
        "filament_min_temp": 190,
        "filament_max_temp": 230,
    }


def test_an_unknown_filament_gets_its_familys_code_and_its_own_temperatures() -> None:
    payload = canvas.edit_payload(
        {
            "slot": 1,
            "material": "PETG",
            "name": "Workshop PETG",
            "brand": "Acme",
            "color": "#FFFFFF",
            "min_temp": 235,
            "max_temp": 255,
        }
    )
    assert payload["filament_code"] == "0x0100"
    assert payload["brand"] == "Acme"
    assert (payload["filament_min_temp"], payload["filament_max_temp"]) == (235, 255)
    assert canvas.filament_code(None, "unobtainium") == "0x0000"


def test_an_upside_down_temperature_range_is_refused() -> None:
    with pytest.raises(ValueError):
        canvas.edit_payload(
            {"slot": 0, "material": "PLA", "color": "#000000", "min_temp": 240, "max_temp": 200}
        )


def test_the_presets_the_card_gets_leave_the_codes_out() -> None:
    preset = canvas.FILAMENT_PRESETS[0].as_dict()
    assert preset == {
        "material": "PLA",
        "name": "PLA",
        "min_temp": 190,
        "max_temp": 230,
        "brands": ["ELEGOO", "Generic"],
    }
    assert "PLA Carbon" in {item.name for item in canvas.FILAMENT_PRESETS if not item.elegoo}


# ---------------------------------------------------------------- validation


def test_a_slots_filament_is_validated_and_normalised() -> None:
    params = validate_params(
        Command.SET_FILAMENT, {"slot": "2", "material": " PLA ", "color": "f32ff8"}
    )
    assert params == {"unit": 0, "slot": 2, "material": "PLA", "color": "#F32FF8"}


@pytest.mark.parametrize(
    "params",
    [
        {"slot": -1, "material": "PLA", "color": "#000000"},
        {"slot": 1.5, "material": "PLA", "color": "#000000"},
        {"slot": True, "material": "PLA", "color": "#000000"},
        {"slot": 64, "material": "PLA", "color": "#000000"},
        {"slot": 0, "material": "PLA", "color": "red"},
        {"slot": 0, "material": "x" * 41, "color": "#000000"},
        {"slot": 0, "color": "#000000"},
        {"slot": 0, "material": "PLA", "color": "#000000", "min_temp": 900},
    ],
)
def test_a_bad_slot_filament_is_refused(params: dict[str, Any]) -> None:
    with pytest.raises(ParamError):
        validate_params(Command.SET_FILAMENT, params)


def test_loading_needs_a_slot_and_defaults_to_the_first_unit() -> None:
    assert validate_params(Command.LOAD_FILAMENT, {"slot": 3}) == {"unit": 0, "slot": 3}
    with pytest.raises(ParamError):
        validate_params(Command.UNLOAD_FILAMENT, {})


# ------------------------------------------------------ the Centauri Carbon


def _sdcp_adapter(granted: frozenset[Capability]) -> tuple[sdcp.SdcpProtocol, list[int]]:
    """Return an SDCP adapter with a live-looking socket and a scripted request path."""
    config = PrinterConfig(name="test", protocol=ProtocolId.SDCP_CC1, host="127.0.0.1", port=3030)
    adapter = sdcp.SdcpProtocol(config, object(), granted=granted)  # type: ignore[arg-type]
    adapter._reader = asyncio.get_running_loop().create_future()  # noqa: SLF001 - a live reader
    adapter._ws = type("Ws", (), {"closed": False})()  # noqa: SLF001
    adapter._identity_checked = True  # noqa: SLF001
    sent: list[int] = []
    answers: list[Any] = []

    async def request(cmd: int, data: Any = None, **_: Any) -> Any:
        sent.append(cmd)
        answer = answers.pop(0) if answers else copy.deepcopy(LIVE_CC1_CANVAS)
        if isinstance(answer, Exception):
            raise answer
        return answer

    adapter._async_request = request  # type: ignore[method-assign]  # noqa: SLF001
    adapter.answers = answers  # type: ignore[attr-defined]
    return adapter, sent


SDCP_WITH_SLOTS = frozenset({Capability.PAUSE, Capability.FILAMENT_SLOTS})


async def test_the_centauri_carbon_reads_its_canvas_while_one_is_connected() -> None:
    adapter, sent = _sdcp_adapter(SDCP_WITH_SLOTS)
    adapter._status = {"AmsConnectStatus": 1, "PrintInfo": {"Status": 0}}  # noqa: SLF001
    snapshot = await adapter.async_read()
    assert sent == [324]
    assert [slot.loaded for slot in snapshot.filament.slots] == [False, False, True, True]

    # Within the interval, and with nothing changed, the last answer is used.
    await adapter.async_read()
    assert sent == [324]

    # A print that starts changes the slot in use, so it is read again at once.
    adapter._status = {"AmsConnectStatus": 1, "PrintInfo": {"Status": 13}}  # noqa: SLF001
    await adapter.async_read()
    assert sent == [324, 324]


@pytest.mark.parametrize(
    "status",
    [{"AmsConnectStatus": 0, "PrintInfo": {"Status": 0}}, {"PrintInfo": {"Status": 0}}],
    ids=["disconnected", "old-firmware"],
)
async def test_the_centauri_carbon_is_not_asked_without_a_canvas(status: dict[str, Any]) -> None:
    """No unit, or a firmware that never mentions one, is never sent command 324."""
    adapter, sent = _sdcp_adapter(SDCP_WITH_SLOTS)
    adapter._status = status  # noqa: SLF001
    snapshot = await adapter.async_read()
    assert sent == []
    assert snapshot.filament is None


async def test_the_centauri_carbon_keeps_its_last_answer_when_a_read_fails() -> None:
    adapter, sent = _sdcp_adapter(SDCP_WITH_SLOTS)
    adapter._status = {"AmsConnectStatus": 1, "PrintInfo": {"Status": 0}}  # noqa: SLF001
    first = await adapter.async_read()
    adapter._status = {"AmsConnectStatus": 1, "PrintInfo": {"Status": 13}}  # noqa: SLF001
    adapter.answers.append(ProtocolError("no answer"))  # type: ignore[attr-defined]
    second = await adapter.async_read()
    assert sent == [324, 324]
    assert second.filament == first.filament


async def test_the_centauri_carbon_offers_no_canvas_command() -> None:
    capabilities = ADAPTERS[ProtocolId.SDCP_CC1].capabilities
    assert Capability.FILAMENT_SLOTS in capabilities
    for command in (
        Capability.LOAD_FILAMENT,
        Capability.UNLOAD_FILAMENT,
        Capability.SET_FILAMENT,
        Capability.SET_AUTO_REFILL,
    ):
        assert command not in capabilities
    assert set(sdcp.COMMAND.values()) <= {0, 1, 128, 129, 130, 131, 258, 259, 320, 324, 386, 403}


# ---------------------------------------------------- the Centauri Carbon 2


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the adapter's waits for the suite."""
    monkeypatch.setattr(cc2, "REQUEST_GAP", 0.0)
    monkeypatch.setattr(cc2, "HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(cc2, "REGISTER_TIMEOUT", 0.3)
    monkeypatch.setattr(cc2, "ACK_TIMEOUT", 0.5)
    monkeypatch.setattr(cc2, "RESUME_REFUSAL_WINDOW", 0.2)


@pytest.fixture(name="cc2_printer")
async def cc2_printer_fixture() -> AsyncIterator[FakeCC2Printer]:
    """Run a fake Centauri Carbon 2 with a CANVAS."""
    server = FakeCC2Printer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture(name="session")
async def session_fixture() -> AsyncIterator[aiohttp.ClientSession]:
    """Return an HTTP session for the adapter."""
    async with aiohttp.ClientSession() as session:
        yield session


def _cc2_adapter(printer: FakeCC2Printer, session: aiohttp.ClientSession) -> cc2.ElegooCC2Protocol:
    config = parse_config(
        {
            "name": "Fake CC2",
            "protocol": "elegoo_cc2",
            "host": "127.0.0.1",
            "port": printer.port,
            "camera_port": printer.camera_port,
            "serial": SERIAL,
        }
    )
    return cc2.ElegooCC2Protocol(
        config, session, granted=ADAPTERS[ProtocolId.ELEGOO_CC2].capabilities
    )


async def _idle(printer: FakeCC2Printer) -> None:
    await printer.push_delta({"machine_status": {"status": 1, "sub_status": 0}})
    await asyncio.sleep(0.05)


async def test_the_centauri_carbon_2_reports_its_canvas(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = _cc2_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        snapshot = await adapter.async_read()
        system = snapshot.filament
        assert [slot.loaded for slot in system.slots] == [True, False, True, True]
        assert (system.active.unit, system.active.slot) == (0, 3)
        assert system.auto_refill is False
        assert system.slot(0, 2).color == "#F32FF8"
        # The next read inside the interval does not ask again.
        await adapter.async_read()
        assert cc2_printer.methods.count(2005) == 1
    finally:
        await adapter.async_teardown()


async def test_loading_and_unloading_a_slot(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = _cc2_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await adapter.async_read()
        # Mid-print the nozzle is in use, so nothing is loaded or unloaded.
        with pytest.raises(ProtocolError, match="not idle"):
            await adapter.async_send(Command.LOAD_FILAMENT, slot=2)
        assert 2001 not in cc2_printer.methods

        await _idle(cc2_printer)
        await adapter.async_send(Command.LOAD_FILAMENT, slot=2)
        assert cc2_printer.params_of(2001) == [{"canvas_id": 0, "tray_id": 2}]
        snapshot = await adapter.async_read()
        assert snapshot.filament.active.slot == 2, "the slot was not read again after loading"

        await adapter.async_send(Command.UNLOAD_FILAMENT, slot=2)
        assert cc2_printer.params_of(2002) == [{"canvas_id": 0, "tray_id": 2}]
        snapshot = await adapter.async_read()
        assert snapshot.filament.active is None
    finally:
        await adapter.async_teardown()


async def test_an_empty_or_missing_slot_is_not_loaded(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = _cc2_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await _idle(cc2_printer)
        await adapter.async_read()
        with pytest.raises(ProtocolError, match="empty"):
            await adapter.async_send(Command.LOAD_FILAMENT, slot=1)
        # The printer acts on tray 0 for a tray it does not have, so it is never asked.
        with pytest.raises(ProtocolError, match="no slot 8"):
            await adapter.async_send(Command.LOAD_FILAMENT, slot=7)
        assert 2001 not in cc2_printer.methods
    finally:
        await adapter.async_teardown()


async def test_a_slots_filament_is_recorded(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = _cc2_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await _idle(cc2_printer)
        await adapter.async_read()
        await adapter.async_send(
            Command.SET_FILAMENT,
            slot=1,
            material="PETG",
            name="RAPID PETG",
            brand="ELEGOO",
            color="#00ff00",
        )
        assert cc2_printer.params_of(2003) == [
            {
                "canvas_id": 0,
                "tray_id": 1,
                "brand": "ELEGOO",
                "filament_type": "PETG",
                "filament_name": "RAPID PETG",
                "filament_code": "0x0105",
                "filament_color": "#00FF00",
                "filament_min_temp": 230,
                "filament_max_temp": 260,
            }
        ]
        slot = (await adapter.async_read()).filament.slot(0, 1)
        assert (slot.material, slot.name, slot.color) == ("PETG", "RAPID PETG", "#00FF00")
    finally:
        await adapter.async_teardown()


async def test_auto_refill_is_switched(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = _cc2_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await adapter.async_read()
        # A setting, not a movement: it may change during a print.
        await adapter.async_send(Command.SET_AUTO_REFILL, on=True)
        assert cc2_printer.params_of(2004) == [{"auto_refill": True}]
        assert (await adapter.async_read()).filament.auto_refill is True
    finally:
        await adapter.async_teardown()


async def test_a_push_about_the_canvas_has_it_read_again(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    """A delta that names the CANVAS is a cue to ask, not a document to merge.

    Merging would replace the whole tray list with the one tray the delta carries.
    """
    adapter = _cc2_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await adapter.async_read()
        cc2_printer.tray(0)["status"] = 0
        await cc2_printer.push_delta({"canvas_info": {"active_tray_id": 3}})
        await asyncio.sleep(0.05)
        snapshot = await adapter.async_read()
        assert cc2_printer.methods.count(2005) == 2
        assert len(snapshot.filament.slots) == 4
        assert snapshot.filament.slot(0, 0).loaded is False
    finally:
        await adapter.async_teardown()


async def test_the_sub_status_says_what_the_canvas_is_doing(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = _cc2_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await cc2_printer.push_delta({"machine_status": {"status": 3, "sub_status": 1151}})
        await asyncio.sleep(0.05)
        snapshot = await adapter.async_read()
        assert snapshot.filament.activity == "Loading: heating the nozzle"
    finally:
        await adapter.async_teardown()


async def test_a_printer_without_a_canvas_reports_none(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    cc2_printer.canvas = None
    adapter = _cc2_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        assert (await adapter.async_read()).filament is None
    finally:
        await adapter.async_teardown()


async def test_a_firmware_without_method_2005_is_not_asked_again(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cc2, "CANVAS_INTERVAL", 0.0)
    cc2_printer.refuse[2005] = 1001
    adapter = _cc2_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        assert (await adapter.async_read()).filament is None
        await asyncio.sleep(0.01)
        await adapter.async_read()
        assert cc2_printer.methods.count(2005) == 1
    finally:
        await adapter.async_teardown()


# ----------------------------------------------------------- in Home Assistant


async def test_a_canvas_in_home_assistant(
    hass: HomeAssistant, hass_ws_client, cc2_printer: FakeCC2Printer
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

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)

    def entity_id(platform: str, key: str) -> str:
        found = registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
        assert found, f"no {platform} for {key}"
        return found

    # One sensor per slot, named as the printer's screen numbers them.
    assert entity_id("sensor", "filament_slot_0_0") == "sensor.fake_cc2_filament_slot_1"
    assert hass.states.get(entity_id("sensor", "filament_slot_0_0")).state == "PLA"
    assert hass.states.get(entity_id("sensor", "filament_slot_0_1")).state == "empty"
    third = hass.states.get(entity_id("sensor", "filament_slot_0_2"))
    assert third.state == "PLA Silk"
    assert third.attributes["color"] == "#F32FF8"
    assert third.attributes["slot"] == "3"
    active = hass.states.get(entity_id("sensor", "active_filament"))
    assert active.state == "PLA"
    assert active.attributes["slot"] == "4"
    refill = entity_id("switch", "auto_refill")
    assert hass.states.get(refill).state == "off"

    # A command is followed by a fresh reading, so the switch follows at once.
    await hass.services.async_call("switch", "turn_on", {"entity_id": refill}, blocking=True)
    assert cc2_printer.params_of(2004) == [{"auto_refill": True}]
    await hass.async_block_till_done()
    assert hass.states.get(refill).state == "on"

    description = hass.data[DATA_RUNTIMES][entry.entry_id].describe()
    assert description["printer"]["filament"]["active"] == {"unit": 0, "slot": 3}
    presets = description["filament_presets"]
    assert {"material": "PLA", "name": "PLA Matte", "min_temp": 190, "max_temp": 230, "brands": ["ELEGOO", "Generic"]} in presets
    assert all("code" not in item for item in presets)

    websocket = await hass_ws_client(hass)
    await websocket.send_json(
        {
            "id": 1,
            "type": "generic_3dprinter/send",
            "entry_id": entry.entry_id,
            "command": "load_filament",
            "data": {"slot": 0},
        }
    )
    refused = await websocket.receive_json()
    assert refused["success"] is False, "a load mid-print must be refused"
    assert "not idle" in refused["error"]["message"]

    coordinator = hass.data[DATA_COORDINATORS][entry.entry_id]
    await _idle(cc2_printer)
    await coordinator.async_send_command(Command.LOAD_FILAMENT, slot=0)
    assert cc2_printer.params_of(2001) == [{"canvas_id": 0, "tray_id": 0}]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_printer_without_a_canvas_gets_no_filament_entity(
    hass: HomeAssistant, cc2_printer: FakeCC2Printer
) -> None:
    cc2_printer.canvas = None
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

    from homeassistant.helpers import entity_registry as er

    keys = {
        item.unique_id.removeprefix(f"{entry.entry_id}_")
        for item in er.async_get(hass).entities.values()
        if item.config_entry_id == entry.entry_id
    }
    assert not {key for key in keys if "filament" in key or key == "auto_refill"}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

"""One end-to-end test that loads this component inside a real Home Assistant.

The unit tests prove the pure logic and the platform behaviour. This proves the
thing a user actually does, once, against a real ``HomeAssistant`` object: the
config entry is created, the component sets up, the device and its entities
appear, an unsupported command is refused before any frame reaches the printer, a
supported command arrives, and unloading leaves nothing behind.

It is deliberately one test. Home Assistant's shutdown waits on the fake printer's
server-side socket for two minutes, because the fake holds an aiohttp connection
open that Home Assistant cannot force closed. That cost is paid once here rather
than once per assertion, and it is a property of the test double rather than of the
integration: the integration's own teardown closes its session and its adapter in
well under a second, which the unit suite proves directly by driving
``PrinterRuntime.async_stop``.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_3dprinter.const import (
    DATA_COORDINATORS,
    DATA_RUNTIMES,
    Capability,
    Command,
)
from custom_components.generic_3dprinter.coordinator import PrinterError
from tests.fake_printer import FakePrinterServer


def registry_entries(hass: HomeAssistant, entry_id: str):
    """Return every entity registry entry belonging to one config entry."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    return [item for item in registry.entities.values() if item.config_entry_id == entry_id]


async def test_the_whole_lifecycle(
    hass: HomeAssistant, config_entry: MockConfigEntry, printer: FakePrinterServer
) -> None:
    entry_id = config_entry.entry_id

    assert await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()

    entries = registry_entries(hass, entry_id)
    assert entries, "the entry set up but produced no entities"

    domains = {item.entity_id.split(".")[0] for item in entries}
    assert domains == {
        "binary_sensor",
        "button",
        "camera",
        "number",
        "sensor",
        "switch",
    }, sorted(item.entity_id for item in entries)

    # Every named entity took its name from the integration's translations. An
    # entity that fell back to the device name would have left its own name empty
    # and collided with its siblings. The camera is the deliberate exception: it is
    # one per printer and named after the printer, which is the shape a fleet
    # dashboard expects from a printer's camera.
    device_name = config_entry.data["name"]
    for item in entries:
        if item.entity_id.startswith("camera."):
            assert item.original_name == device_name
            continue
        assert item.original_name and item.original_name != device_name, (
            f"{item.entity_id} is named {item.original_name!r}, which means its "
            f"translated name was not found"
        )

    def state_for(translation_key: str):
        match = next((item for item in entries if item.translation_key == translation_key), None)
        assert match is not None, f"no entity with translation key {translation_key!r}"
        return hass.states.get(match.entity_id)

    # The camera is the one entity with no translation key: it is a single entity
    # per printer named after the printer.
    camera = next((item for item in entries if item.entity_id.startswith("camera.")), None)
    assert camera is not None, sorted(item.entity_id for item in entries)
    assert camera.translation_key is None

    assert state_for("printer_state").state == "printing"
    assert float(state_for("progress").state) == pytest.approx(12.0)
    assert float(state_for("current_layer").state) == pytest.approx(107.0)
    assert float(state_for("total_layers").state) == pytest.approx(627.0)
    assert float(state_for("nozzle_temperature").state) == pytest.approx(219.5, abs=0.6)
    assert float(state_for("bed_temperature").state) == pytest.approx(55.0, abs=0.6)
    # The printer exposes no remaining-time field; TotalTicks - CurrentTicks is it.
    assert float(state_for("remaining_time").state) == pytest.approx(18574 - 2532.16, abs=1.0)
    assert state_for("online").state == "on"
    assert state_for("active_job").state == "on"
    assert state_for("chamber_light").state == "on"

    # SDCP declares no JOG capability, so nothing position-shaped may exist.
    keys = {item.translation_key for item in entries}
    assert "position_x" not in keys
    assert {"pause", "resume", "stop"} <= keys

    from homeassistant.helpers import device_registry as dr

    found = [
        item
        for item in dr.async_get(hass).devices.values()
        if entry_id in item.config_entries
    ]
    assert len(found) == 1
    assert found[0].model == "Centauri Carbon"
    assert found[0].sw_version == "V1.4.49"

    runtime = hass.data[DATA_RUNTIMES][entry_id]
    coordinator = hass.data[DATA_COORDINATORS][entry_id]

    # Start print is withheld behind its declared hazard, and refused before any
    # frame reaches the printer.
    assert Capability.START_PRINT not in runtime.capabilities
    printer.sent_commands.clear()
    with pytest.raises(PrinterError) as caught:
        await coordinator.async_send_command(Command.START_PRINT, filename="part.gcode")
    assert "start" in str(caught.value).lower()
    assert printer.sent_commands == [], printer.sent_commands

    # A parameter outside its range is refused at the boundary too.
    with pytest.raises(PrinterError):
        await coordinator.async_send_command(Command.SET_HOTEND_TEMP, value=400)
    assert printer.sent_commands == [], printer.sent_commands

    # A supported command does reach the printer. 129 is suspend print.
    await coordinator.async_send_command(Command.PAUSE)
    assert 129 in printer.sent_commands, printer.sent_commands

    description = runtime.describe()
    assert description["protocol"] == "sdcp_cc1"
    assert description["camera"] is True
    assert description["camera_url"].startswith("/api/generic_3dprinter/")
    assert "?" not in description["camera_url"]
    assert "start_print" not in description["printer"]["capabilities"]
    assert [item["id"] for item in description["unsafe_features"]] == ["sdcp_start_print"]
    assert description["upload_suffixes"] == [".gcode", ".gco", ".g", ".bgcode"]

    # Unloading releases the runtime, the coordinator and the entities' live state.
    # Home Assistant leaves the registry entries in place and may restore a last
    # state marked unavailable, so the assertion is that nothing is still reporting
    # a reading rather than that the entity id disappeared.
    entity_ids = {item.entity_id for item in registry_entries(hass, entry_id)}
    assert await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()
    assert entry_id not in hass.data.get(DATA_RUNTIMES, {})
    assert entry_id not in hass.data.get(DATA_COORDINATORS, {})
    for item in entity_ids:
        state = hass.states.get(item)
        assert state is None or state.state == "unavailable", (
            f"{item} still reports {state.state!r} after the entry unloaded"
        )

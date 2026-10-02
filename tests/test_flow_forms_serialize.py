"""Every form the flows show serializes the way Home Assistant sends it to the frontend:
a schema it cannot convert opens as "500 Internal Server Error" instead of a form."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_3dprinter.config_flow import Generic3DPrinterConfigFlow
from custom_components.generic_3dprinter.const import DOMAIN, ProtocolId
from custom_components.generic_3dprinter.registry import ADAPTERS

try:  # Home Assistant 2026.9 and later
    from probatio import to_field_list as _convert
except ImportError:
    from voluptuous_serialize import convert as _convert


def _serialize(schema: object) -> list[dict[str, object]]:
    return _convert(schema, custom_serializer=cv.custom_serializer)


@pytest.mark.parametrize("protocol", list(ADAPTERS))
def test_the_details_form_serializes(protocol: ProtocolId) -> None:
    flow = Generic3DPrinterConfigFlow()
    flow._data = {"protocol": protocol.value}
    assert _serialize(flow._details_schema(ADAPTERS[protocol]))


@pytest.mark.parametrize("protocol", list(ADAPTERS))
@pytest.mark.parametrize("port", [None, "", 3030])
async def test_the_options_form_serializes(
    hass: HomeAssistant, protocol: ProtocolId, port: object
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Printer",
        data={"name": "Printer", "protocol": protocol.value, "host": "192.0.2.1", "port": port},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fields = {field["name"] for field in _serialize(result["data_schema"])}
    assert {"name", "host", "port"} <= fields
    assert {f"unsafe_{feature.id}" for feature in ADAPTERS[protocol].unsafe} <= fields


async def test_the_resin_camera_is_switched_on_from_the_options(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Saturn",
        data={"name": "Saturn", "protocol": "sdcp_resin", "host": "192.0.2.1", "unsafe_enabled": []},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"name": "Saturn", "host": "192.0.2.1", "port": 3030, "unsafe_sdcp_resin_camera": True},
    )
    assert result["type"] == "create_entry"
    assert result["data"]["unsafe_enabled"] == ["sdcp_resin_camera"]
    assert result["data"]["port"] == 3030


async def test_a_port_cleared_in_the_options_falls_back_to_the_default(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Printer",
        data={"name": "Printer", "protocol": "moonraker", "host": "192.0.2.1", "port": 7130},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Printer", "host": "192.0.2.1"}
    )
    assert result["type"] == "create_entry"
    assert result["data"]["port"] is None

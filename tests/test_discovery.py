"""Discovery asks every protocol, and the config flow never names one.

Each adapter may broadcast for its printers and may identify one host; both are
read-only. The engine gathers what they answer, and the flow offers it: straight
to the form for one printer, a choice for several, and nothing already set up.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_3dprinter import discovery
from custom_components.generic_3dprinter.const import DOMAIN, ProtocolId
from custom_components.generic_3dprinter.discovery import DiscoveryResult
from custom_components.generic_3dprinter.registry import ADAPTERS


def _stub(protocol: ProtocolId, *, found=(), identity=None, fails=False) -> Any:
    class Adapter:
        @classmethod
        async def async_discover(cls, timeout: float) -> list[DiscoveryResult]:
            if fails:
                raise OSError("no network")
            return list(found)

        @classmethod
        async def async_identify(cls, host: str, timeout: float) -> DiscoveryResult | None:
            return identity

    return dataclasses.replace(ADAPTERS[protocol], adapter=Adapter)


def _result(protocol: ProtocolId, host: str, **extra: Any) -> DiscoveryResult:
    return DiscoveryResult(host=host, protocol=protocol, candidates=[protocol], **extra)


async def test_every_protocol_is_asked_and_one_host_is_one_printer() -> None:
    a = _result(ProtocolId.SDCP_CC1, "192.0.2.1")
    b = _result(ProtocolId.ELEGOO_CC2, "192.0.2.2")
    registrations = [
        _stub(ProtocolId.SDCP_CC1, found=[a]),
        _stub(ProtocolId.ELEGOO_CC2, found=[b, _result(ProtocolId.ELEGOO_CC2, "192.0.2.1")]),
        _stub(ProtocolId.MOONRAKER, fails=True),
    ]
    found = await discovery.async_discover_all(registrations, timeout=0.1)
    assert found == [a, b]


async def test_a_printer_that_names_itself_outranks_a_port_hint() -> None:
    me = _result(ProtocolId.ELEGOO_CC2, "192.0.2.9", model="Centauri Carbon 2")
    registrations = [_stub(ProtocolId.MOONRAKER), _stub(ProtocolId.ELEGOO_CC2, identity=me)]
    with patch.object(discovery, "async_probe_ports", side_effect=AssertionError("not needed")):
        assert await discovery.async_identify_host(registrations, "192.0.2.9", timeout=0.1) is me


async def test_an_open_port_is_a_hint_named_by_the_registrations() -> None:
    registrations = [_stub(ProtocolId.SDCP_CC1), _stub(ProtocolId.MOONRAKER)]

    async def ports(host: str, candidates: list[int]) -> list[int]:
        assert set(candidates) == {3030, 7125}
        return [7125]

    async def fingerprint(host: str, port: int, markers: Any) -> tuple[ProtocolId | None, list[str]]:
        return None, []

    with patch.object(discovery, "async_probe_ports", ports), patch.object(
        discovery, "async_fingerprint_http", fingerprint
    ):
        found = await discovery.async_identify_host(registrations, "192.0.2.3", timeout=0.1)
    assert found is not None and found.protocol is ProtocolId.MOONRAKER


async def _discover(hass: HomeAssistant, results: list[DiscoveryResult]) -> dict[str, Any]:
    async def all_found(registrations: Any, timeout: float = 0) -> list[DiscoveryResult]:
        return list(results)

    with patch("custom_components.generic_3dprinter.config_flow.async_discover_all", all_found):
        flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        return await hass.config_entries.flow.async_configure(flow["flow_id"], {"discover": True})


async def test_one_printer_goes_straight_to_its_form_with_what_it_said(hass: HomeAssistant) -> None:
    found = _result(
        ProtocolId.ELEGOO_CC2, "192.0.2.4", model="Centauri Carbon 2", prefill={"serial": "SN123"}
    )
    result = await _discover(hass, [found])
    assert result["step_id"] == "details"
    defaults = {str(key): key.default() for key in result["data_schema"].schema if callable(getattr(key, "default", None))}
    assert defaults["host"] == "192.0.2.4"
    assert defaults["serial"] == "SN123"


async def test_several_printers_are_offered_as_a_choice(hass: HomeAssistant) -> None:
    first = _result(ProtocolId.SDCP_CC1, "192.0.2.5", model="Centauri Carbon")
    second = _result(ProtocolId.ELEGOO_CC2, "192.0.2.6", model="Centauri Carbon 2")
    result = await _discover(hass, [first, second])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pick"
    (selector,) = result["data_schema"].schema.values()
    labels = [option["label"] for option in selector.config["options"]]
    assert labels == ["Centauri Carbon at 192.0.2.5", "Centauri Carbon 2 at 192.0.2.6"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"printer": selector.config["options"][1]["value"]}
    )
    assert result["step_id"] == "details"
    assert result["description_placeholders"]["protocol"] == ADAPTERS[ProtocolId.ELEGOO_CC2].label


async def test_a_printer_already_set_up_is_not_offered_again(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        data={"name": "old", "protocol": "elegoo_cc2", "host": "192.0.2.7", "serial": "SN7"},
        unique_id="elegoo_cc2:192.0.2.7:0",
    ).add_to_hass(hass)
    known = _result(ProtocolId.ELEGOO_CC2, "192.0.2.8", prefill={"serial": "SN7"})
    fresh = _result(ProtocolId.SDCP_CC1, "192.0.2.9", model="Centauri Carbon")
    result = await _discover(hass, [known, fresh])
    assert result["step_id"] == "details"
    assert result["description_placeholders"]["protocol"] == ADAPTERS[ProtocolId.SDCP_CC1].label

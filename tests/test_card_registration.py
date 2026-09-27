"""How the card reaches the dashboard.

The card showed "Configuration error" on some loads, and on nearly every load in the
phone app, because it was loaded as an extra module. index.html imports extra
modules in parallel with the frontend's own bundle, whose first act is to replace
``window.customElements`` with a scoped-registry polyfill; a card that ran first was
defined where Home Assistant never looks (home-assistant/frontend#52960). It was
also registered only after a printer had answered, so a printer that was off when
Home Assistant started left the dashboard without the card at all.

These tests hold the fix down on the server side: the card is a Lovelace resource,
registered as soon as the integration loads, whatever the printers are doing. The
card-side half is in tests/js/card.test.mjs.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.generic_3dprinter import frontend
from custom_components.generic_3dprinter.const import (
    CARD_URL_PATH,
    DATA_CARD_REGISTERED,
    DOMAIN,
)

URL_RE = re.compile(re.escape(CARD_URL_PATH) + r"\?v=[\w.]+-[0-9a-f]{8}$")


def resources_of(hass: HomeAssistant) -> Any:
    """Return Lovelace's resource collection, in either shape Home Assistant uses."""
    data = hass.data["lovelace"]
    return getattr(data, "resources", None) or data["resources"]


async def card_resources(hass: HomeAssistant) -> list[dict]:
    resources = resources_of(hass)
    await resources.async_get_info()
    return [item for item in resources.async_items() if item["url"].startswith(CARD_URL_PATH)]


def unreachable_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Add an entry for a printer that is switched off: nothing listens on its port."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Switched off",
        data={"name": "Switched off", "protocol": "sdcp_cc1", "host": "127.0.0.1", "port": 1},
        unique_id="sdcp_cc1:127.0.0.1:1",
    )
    entry.add_to_hass(hass)
    return entry


async def test_the_card_is_a_resource_even_with_every_printer_off(
    hass: HomeAssistant, hass_client, hass_ws_client
) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    entry = unreachable_entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.name == "SETUP_RETRY", "the printer should be unreachable in this test"

    (resource,) = await card_resources(hass)
    assert resource["type"] == "module"
    assert URL_RE.match(resource["url"]), resource["url"]

    # The file is served, so the resource resolves.
    client = await hass_client()
    response = await client.get(resource["url"])
    assert response.status == 200
    body = await response.text()
    assert "generic-3dprinter-card" in body
    assert "max-age" in response.headers.get("Cache-Control", ""), "a versioned URL may be cached"

    # And the card's own API answers, instead of "unknown command", and still lists
    # the printer, so the card can show it with the button that switches it on.
    websocket = await hass_ws_client(hass)
    await websocket.send_json({"id": 1, "type": "generic_3dprinter/list"})
    listed = await websocket.receive_json()
    assert listed["success"], listed
    (printer,) = listed["result"]["printers"]
    assert printer["entry_id"] == entry.entry_id
    assert printer["name"] == "Switched off"
    assert printer["connected"] is False
    assert printer["model"] == "Centauri Carbon", "named by its protocol until it answers"

    await websocket.send_json(
        {"id": 2, "type": "generic_3dprinter/describe", "entry_id": entry.entry_id}
    )
    described = await websocket.receive_json()
    assert described["success"], described
    description = described["result"]
    assert description["connected"] is False
    assert "has not answered" in description["last_error"]
    assert description["printer"]["capabilities"] == [], "a printer that is not there has no controls"
    assert description["camera_url"] is None

    await websocket.send_json({"id": 3, "type": "generic_3dprinter/describe", "entry_id": "nope"})
    assert (await websocket.receive_json())["error"]["code"] == "not_found"


async def test_a_resource_already_there_is_updated_and_duplicates_removed(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "lovelace", {})
    resources = resources_of(hass)
    await resources.async_get_info()
    # One added by hand, as the README once suggested, one from an older release,
    # and one that belongs to something else.
    await resources.async_create_item({"res_type": "module", "url": CARD_URL_PATH})
    await resources.async_create_item({"res_type": "js", "url": f"{CARD_URL_PATH}?v=0.4.0"})
    await resources.async_create_item({"res_type": "module", "url": "/local/other-card.js"})

    await frontend.async_register_card(hass)

    items = resources.async_items()
    ours = [item for item in items if item["url"].startswith(CARD_URL_PATH)]
    assert len(ours) == 1
    assert ours[0]["type"] == "module"
    assert URL_RE.match(ours[0]["url"])
    assert any(item["url"] == "/local/other-card.js" for item in items), "another card was touched"

    # A second registration changes nothing.
    hass.data.pop(DATA_CARD_REGISTERED)
    before = [dict(item) for item in resources.async_items()]
    await frontend.async_register_card(hass)
    assert [dict(item) for item in resources.async_items()] == before


async def test_yaml_resources_fall_back_to_an_extra_module(hass: HomeAssistant) -> None:
    """Resources kept in YAML cannot be written to; the card then defends itself."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "lovelace", {"lovelace": {"mode": "yaml"}})
    hass.config.components.add("frontend")
    added: list[str] = []
    with patch(
        "homeassistant.components.frontend.add_extra_js_url",
        side_effect=lambda _hass, url, es5=False: added.append(url),
    ):
        await frontend.async_register_card(hass)
    assert len(added) == 1
    assert URL_RE.match(added[0])
    assert hass.data[DATA_CARD_REGISTERED] is True


async def test_without_a_frontend_nothing_is_marked_done(hass: HomeAssistant) -> None:
    """A registration that delivered nothing is retried on the next occasion."""
    assert await async_setup_component(hass, "http", {})
    await frontend.async_register_card(hass)
    assert not hass.data.get(DATA_CARD_REGISTERED)


async def test_the_url_follows_the_file(hass: HomeAssistant) -> None:
    first = await frontend.async_card_url(hass)
    assert URL_RE.match(first)
    with patch.object(frontend, "_digest", return_value="0123abcd"):
        assert (await frontend.async_card_url(hass)).endswith("-0123abcd")


async def test_removing_the_last_printer_removes_the_resource(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "lovelace", {})
    entry = unreachable_entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(await card_resources(hass)) == 1

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert await card_resources(hass) == []

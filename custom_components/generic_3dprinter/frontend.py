"""Serve the Lovelace card that ships with this integration, and have dashboards load it.

The card is a plain ES module served from the integration directory. It is loaded
as a Lovelace resource, not as an ``extra_module_url``, because of how Home
Assistant's frontend starts:

* index.html imports every extra module in parallel with the frontend's own
  bundle, and nothing orders the two. The bundle's first act is to replace
  ``window.customElements`` with a scoped-registry polyfill, and dashboards look
  cards up only in that new registry. An extra module that runs first defines its
  card on the browser's registry instead, and the dashboard shows "Configuration
  error" for the rest of that page's life (home-assistant/frontend#52960). Who wins
  depends on caches and CPU speed, so it failed now and then on a desktop and
  nearly always in the phone app. Lovelace loads its resources itself, once the
  frontend has started.
* an extra module is written into index.html, which the service worker and the
  phone app's web view keep cached. A page served while Home Assistant was still
  starting has no import for the card at all. Resources are fetched over the
  WebSocket every time a dashboard loads.

The resource is added, or updated to the current version, in the Lovelace
resources the user manages, and removed with the last printer. A user who keeps
Lovelace resources in YAML cannot have one added, so for them the card falls back
to an extra module; the card defends itself against the registry race in that case.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import (
    CARD_FILENAME,
    CARD_URL_PATH,
    DATA_CARD_PATH_REGISTERED,
    DATA_CARD_REGISTERED,
    DOMAIN,
    WWW_PATH,
)

_LOGGER = logging.getLogger(__name__)

STATIC_URL_PATH: Final = f"/{DOMAIN}"
#: Where Lovelace keeps its state in ``hass.data``: a dict before HA 2025.x and a
#: ``LovelaceData`` object with a ``resources`` attribute since.
LOVELACE_KEY: Final = "lovelace"
RESOURCE_TYPE: Final = "module"


def _digest(path: Path) -> str:
    """Return a short hash of the card file, so a changed file gets a new URL."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def _card_resources(hass: HomeAssistant) -> Any | None:
    """Return Lovelace's resource collection when resources are kept in storage.

    ``None`` when Lovelace is not set up, or when its resources come from YAML, which
    cannot be written to.
    """
    data = hass.data.get(LOVELACE_KEY)
    if data is None:
        return None
    resources = getattr(data, "resources", None)
    if resources is None and isinstance(data, Mapping):
        resources = data.get("resources")
    if resources is None or not hasattr(resources, "async_create_item"):
        return None
    return resources


def _is_card(item: Mapping[str, Any]) -> bool:
    """Return ``True`` for a resource that loads this card, whatever its query string."""
    return str(item.get("url") or "").split("?", 1)[0] == CARD_URL_PATH


async def async_card_url(hass: HomeAssistant) -> str:
    """Return the card's URL, versioned by the release and by the file's content."""
    integration = await async_get_integration(hass, DOMAIN)
    card_file = integration.file_path / WWW_PATH / CARD_FILENAME
    digest = await hass.async_add_executor_job(_digest, card_file)
    return f"{CARD_URL_PATH}?v={integration.version or '0'}-{digest}"


async def _async_loaded(resources: Any) -> None:
    """Load the resource collection and let anyone loading it alongside finish.

    The collection loads lazily. A dashboard that asks for the resources while it
    is loading waits on the same read, and fills the collection from it after us;
    changes made before it has done so would be overwritten with what was on disk.
    """
    await resources.async_get_info()
    await asyncio.sleep(0)


def _is_current(resources: Any, url: str) -> bool:
    items = [item for item in resources.async_items() if _is_card(item)]
    return len(items) == 1 and items[0].get("url") == url and items[0].get("type") == RESOURCE_TYPE


async def _async_register_resource(hass: HomeAssistant, url: str) -> bool:
    """Add the card to Lovelace's resources, or bring the existing entry up to date.

    A resource the user added by hand for this card is taken over rather than
    duplicated, and any duplicate is removed, because the same card loaded twice
    would be listed twice in the card picker. The result is checked, and the work
    done once more if something loading the collection at the same time undid it.
    """
    resources = _card_resources(hass)
    if resources is None:
        return False
    await _async_loaded(resources)
    for _attempt in range(2):
        await _async_apply_resource(resources, url)
        await asyncio.sleep(0)
        if _is_current(resources, url):
            return True
    _LOGGER.warning("the card's Lovelace resource did not stay as written")
    return True


async def _async_apply_resource(resources: Any, url: str) -> None:
    matches = [item for item in resources.async_items() if _is_card(item)]
    if not matches:
        await resources.async_create_item({"res_type": RESOURCE_TYPE, "url": url})
        _LOGGER.debug("added the Lovelace resource %s", url)
        return
    first, *duplicates = matches
    if first.get("url") != url or first.get("type") != RESOURCE_TYPE:
        await resources.async_update_item(first["id"], {"res_type": RESOURCE_TYPE, "url": url})
        _LOGGER.debug("updated the Lovelace resource to %s", url)
    for item in duplicates:
        await resources.async_delete_item(item["id"])
        _LOGGER.debug("removed a duplicate Lovelace resource %s", item.get("url"))


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card over HTTP and have every dashboard load it. Idempotent.

    Registration does not wait for a printer: a printer that is switched off when
    Home Assistant starts must not leave the dashboard without its card. The flag is
    set only once the card is actually delivered, so a failed attempt is retried.
    """
    if hass.data.get(DATA_CARD_REGISTERED) or hass.http is None:
        return

    if not hass.data.get(DATA_CARD_PATH_REGISTERED):
        integration = await async_get_integration(hass, DOMAIN)
        card_path = integration.file_path / WWW_PATH
        await hass.http.async_register_static_paths(
            # The query string carries the version and a hash of the file, so the
            # browser may keep the file; a new file gets a new URL.
            [StaticPathConfig(STATIC_URL_PATH, str(card_path), True)]
        )
        hass.data[DATA_CARD_PATH_REGISTERED] = True
        _LOGGER.debug("serving %s from %s", STATIC_URL_PATH, card_path)

    url = await async_card_url(hass)
    try:
        delivered = await _async_register_resource(hass, url)
    except Exception:  # noqa: BLE001 - the extra module below still delivers the card
        _LOGGER.warning("could not add the card to the Lovelace resources", exc_info=True)
        delivered = False

    if not delivered:
        if "frontend" not in hass.config.components:
            _LOGGER.debug("the frontend is not set up; the card is not registered yet")
            return
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, url)
        _LOGGER.debug("Lovelace resources are not writable; loading the card as %s", url)

    hass.data[DATA_CARD_REGISTERED] = True


async def async_unregister_card(hass: HomeAssistant) -> None:
    """Remove the card's Lovelace resource, once no printer is left to show."""
    resources = _card_resources(hass)
    if resources is None:
        return
    await _async_loaded(resources)
    for item in [item for item in resources.async_items() if _is_card(item)]:
        await resources.async_delete_item(item["id"])
        _LOGGER.debug("removed the Lovelace resource %s", item.get("url"))
    hass.data.pop(DATA_CARD_REGISTERED, None)

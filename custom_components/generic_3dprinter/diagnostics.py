"""Diagnostics support.

Every credential key an adapter may declare is redacted, and the snapshot is
included as-is, because a printer's state is the first thing worth seeing when a
dashboard misbehaves.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ACCESS_CODE, CONF_API_KEY, CONF_PASSWORD, CONF_USERNAME
from .runtime import PrinterRuntime

TO_REDACT = {CONF_API_KEY, CONF_PASSWORD, CONF_ACCESS_CODE, CONF_USERNAME}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one printer."""
    runtime: PrinterRuntime = entry.runtime_data
    coordinator = hass.data.get("generic_3dprinter_coordinators", {}).get(entry.entry_id)
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "config": async_redact_data(runtime.config.as_dict(), TO_REDACT),
        "capabilities": sorted(item.value for item in runtime.capabilities),
        "model_id": runtime.adapter.model_id,
        "model_profile": runtime.model_profile(),
        "withheld_hazards": [
            {"id": feature.id, "label": feature.label, "evidence": feature.evidence}
            for feature in runtime.adapter.unsafe_features
        ],
        "snapshot": runtime.snapshot.as_dict(),
        "camera": runtime.camera.stats().as_dict(),
        "web_proxy": runtime.web_proxy.status().as_dict(),
        "last_error": runtime.last_error,
        "last_update_success": getattr(coordinator, "last_update_success", None),
    }

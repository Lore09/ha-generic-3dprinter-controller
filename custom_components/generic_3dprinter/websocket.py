"""WebSocket API consumed by the bundled Lovelace card.

The card never guesses a URL. It asks for a printer's description over the
authenticated WebSocket API and receives freshly signed, relative URLs, which is
what lets a plain ``<img>`` element load a camera frame without an
``Authorization`` header.

A printer that did not answer when Home Assistant started has a config entry but no
runtime, while Home Assistant retries it. It is still listed and described, as a
printer that is not answering: a printer switched off at its plug is exactly that,
and the card must still show it, with the power button that switches it back on.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    DATA_COORDINATORS,
    DOMAIN,
    WS_DESCRIBE,
    WS_FILES,
    WS_LIST,
    WS_SEND,
    Command,
    ProtocolId,
)
from .protocols import ProtocolError
from .registry import ADAPTERS
from .runtime import PrinterRuntime, get_runtime, iter_runtimes

_VALID_COMMANDS = tuple(command.value for command in Command)


def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register the commands the card uses."""
    websocket_api.async_register_command(hass, ws_list)
    websocket_api.async_register_command(hass, ws_describe)
    websocket_api.async_register_command(hass, ws_send)
    websocket_api.async_register_command(hass, ws_files)


def _resolve_entry_id(hass: HomeAssistant, msg: dict) -> str | None:
    """Return the config entry id from an explicit id or an entity id."""
    if entry_id := msg.get("entry_id"):
        return str(entry_id)
    if entity_id := msg.get("entity_id"):
        registry = er.async_get(hass)
        entity = registry.async_get(entity_id)
        return entity.config_entry_id if entity else None
    return None


def _coordinator(runtime: PrinterRuntime):
    return runtime.hass.data.get(DATA_COORDINATORS, {}).get(runtime.entry_id)


def _state_entity(hass: HomeAssistant, entry_id: str) -> str | None:
    # The state sensor's unique id is the entry id and its key, as every entity of
    # this integration builds its own.
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry_id}_printer_state")


def _printer_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Return the enabled printers, in the order the user added them."""
    return [
        entry for entry in hass.config_entries.async_entries(DOMAIN) if entry.disabled_by is None
    ]


def _not_loaded_entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry | None:
    """Return an enabled printer that has no runtime, because it has not answered yet."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or entry.disabled_by is not None:
        return None
    return entry


def _model_of(entry: ConfigEntry) -> str | None:
    """Return the model a printer's protocol stands for, before the printer said."""
    try:
        registration = ADAPTERS.get(ProtocolId(entry.data.get("protocol")))
    except ValueError:
        return None
    return (registration.model or registration.label) if registration else None


def _not_answering(entry: ConfigEntry) -> str:
    reason = getattr(entry, "reason", None)
    detail = f": {reason}" if reason else ""
    return f"the printer has not answered since Home Assistant started{detail}"


@websocket_api.websocket_command({vol.Required("type"): WS_LIST})
@websocket_api.async_response
async def ws_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return every configured printer, summarised, answering or not."""
    runtimes = {runtime.entry_id: runtime for runtime in iter_runtimes(hass)}
    printers = []
    for entry in _printer_entries(hass):
        runtime = runtimes.pop(entry.entry_id, None)
        if runtime is None:
            printers.append(
                {
                    "entry_id": entry.entry_id,
                    "name": entry.data.get("name") or entry.title,
                    "protocol": entry.data.get("protocol"),
                    "model": _model_of(entry),
                    "connected": False,
                    "print_state": "unknown",
                    "camera": False,
                    "entity_id": _state_entity(hass, entry.entry_id),
                }
            )
            continue
        printers.append(_summary(hass, runtime))
    # A runtime without an entry cannot normally exist; list it rather than hide it.
    printers.extend(_summary(hass, runtime) for runtime in runtimes.values())
    connection.send_result(msg["id"], {"printers": printers})


def _summary(hass: HomeAssistant, runtime: PrinterRuntime) -> dict:
    return {
        "entry_id": runtime.entry_id,
        "name": runtime.config.name,
        "protocol": runtime.config.protocol.value,
        "model": runtime.snapshot.model,
        "connected": runtime.snapshot.connected,
        "print_state": runtime.snapshot.print_state.value,
        "camera": runtime.camera_kind is not None,
        "entity_id": _state_entity(hass, runtime.entry_id),
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_DESCRIBE,
        vol.Optional("entry_id"): cv.string,
        vol.Optional("entity_id"): cv.entity_id,
    }
)
@websocket_api.async_response
async def ws_describe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return the full description of one printer, with signed URLs."""
    entry_id = _resolve_entry_id(hass, msg)
    runtime = get_runtime(hass, entry_id) if entry_id else None
    if runtime is None:
        entry = _not_loaded_entry(hass, entry_id) if entry_id else None
        if entry is None:
            connection.send_error(msg["id"], "not_found", "no such printer is configured")
            return
        connection.send_result(msg["id"], _not_answering_description(entry))
        return
    connection.send_result(msg["id"], runtime.describe())


def _not_answering_description(entry: ConfigEntry) -> dict:
    """Describe a printer Home Assistant is still retrying: no controls, no camera."""
    protocol = entry.data.get("protocol")
    return {
        "entry_id": entry.entry_id,
        "name": entry.data.get("name") or entry.title,
        "protocol": protocol,
        "model": _model_of(entry),
        "firmware": None,
        "serial": None,
        "connected": False,
        "last_error": _not_answering(entry),
        "camera": False,
        "camera_kind": None,
        "camera_entity_id": None,
        "web_ui": False,
        "camera_url": None,
        "snapshot_url": None,
        "status_url": None,
        "web_proxy_url": None,
        "web_ui_url": entry.data.get("web_url"),
        "camera_stats": None,
        "unsafe_features": [],
        "filament_presets": [],
        "model_profile": None,
        "printer": {
            "protocol": protocol,
            "connected": False,
            "print_state": "unknown",
            "capabilities": [],
            "errors": [],
        },
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_SEND,
        vol.Optional("entry_id"): cv.string,
        vol.Optional("entity_id"): cv.entity_id,
        vol.Required("command"): vol.In(_VALID_COMMANDS),
        vol.Optional("data", default={}): dict,
    }
)
@websocket_api.async_response
async def ws_send(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Send one normalised command, going through the same guards as a service call."""
    entry_id = _resolve_entry_id(hass, msg)
    runtime = get_runtime(hass, entry_id) if entry_id else None
    if runtime is None:
        connection.send_error(msg["id"], "not_found", "no such printer is configured")
        return

    coordinator = _coordinator(runtime)
    if coordinator is None:
        connection.send_error(msg["id"], "not_ready", "the printer is still starting up")
        return

    try:
        await coordinator.async_send_command(Command(msg["command"]), **msg["data"])
    except ProtocolError as err:
        connection.send_error(msg["id"], "command_failed", str(err))
        return
    except Exception as err:  # noqa: BLE001 - never leak a traceback to the card
        connection.send_error(msg["id"], "command_failed", str(err))
        return

    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_FILES,
        vol.Optional("entry_id"): cv.string,
        vol.Optional("entity_id"): cv.entity_id,
    }
)
@websocket_api.async_response
async def ws_files(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return the printer's stored files."""
    entry_id = _resolve_entry_id(hass, msg)
    runtime = get_runtime(hass, entry_id) if entry_id else None
    if runtime is None:
        connection.send_error(msg["id"], "not_found", "no such printer is configured")
        return

    coordinator = _coordinator(runtime)
    if coordinator is None:
        connection.send_error(msg["id"], "not_ready", "the printer is still starting up")
        return

    try:
        files = await coordinator.async_list_files()
    except Exception as err:  # noqa: BLE001
        connection.send_error(msg["id"], "files_failed", str(err))
        return

    connection.send_result(msg["id"], {"files": [item.as_dict() for item in files]})

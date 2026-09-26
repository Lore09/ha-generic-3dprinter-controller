"""WebSocket API consumed by the bundled Lovelace card.

The card never guesses a URL. It asks for a printer's description over the
authenticated WebSocket API and receives freshly signed, relative URLs, which is
what lets a plain ``<img>`` element load a camera frame without an
``Authorization`` header.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components import websocket_api
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
)
from .protocols import ProtocolError
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


@websocket_api.websocket_command({vol.Required("type"): WS_LIST})
@websocket_api.async_response
async def ws_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return every configured printer, summarised."""
    connection.send_result(
        msg["id"],
        {
            "printers": [
                {
                    "entry_id": runtime.entry_id,
                    "name": runtime.config.name,
                    "protocol": runtime.config.protocol.value,
                    "model": runtime.snapshot.model,
                    "connected": runtime.snapshot.connected,
                    "print_state": runtime.snapshot.print_state.value,
                    "camera": runtime.camera_kind is not None,
                    # The state sensor's unique id is the entry id and its key,
                    # as every entity of this integration builds its own.
                    "entity_id": er.async_get(hass).async_get_entity_id(
                        "sensor", DOMAIN, f"{runtime.entry_id}_printer_state"
                    ),
                }
                for runtime in iter_runtimes(hass)
            ]
        },
    )


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
        connection.send_error(msg["id"], "not_found", "no such printer is configured")
        return
    connection.send_result(msg["id"], runtime.describe())


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

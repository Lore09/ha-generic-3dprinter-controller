"""Buttons for the job-control commands.

One button per command this printer grants, built from a module-level table rather
than a chain of branches, so the capability each button needs sits next to the
button it belongs to. Pressing a button sends the normalised command through the
coordinator, which means a press and a service call take the same guards and the
same validation; a refusal surfaces as a ``HomeAssistantError`` in the UI instead of
being swallowed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import Capability, Command
from .coordinator import PrinterCoordinator
from .entity import (
    Generic3DPrinterEntity,
    async_require_coordinator,
    granted_capabilities,
)


@dataclass(frozen=True, kw_only=True)
class Generic3DPrinterButtonDescription(ButtonEntityDescription):
    """One command, the capability it needs, and the parameters it sends."""

    command: Command
    capability: Capability
    params: dict[str, Any]


#: Every button this platform can offer. A button whose capability is absent is
#: never constructed, so the card and the entity registry agree on what this
#: printer can do.
BUTTON_DESCRIPTIONS: Final[tuple[Generic3DPrinterButtonDescription, ...]] = (
    Generic3DPrinterButtonDescription(
        key="pause",
        icon="mdi:pause",
        command=Command.PAUSE,
        capability=Capability.PAUSE,
        params={},
    ),
    Generic3DPrinterButtonDescription(
        key="resume",
        icon="mdi:play",
        command=Command.RESUME,
        capability=Capability.RESUME,
        params={},
    ),
    Generic3DPrinterButtonDescription(
        key="stop",
        icon="mdi:stop",
        command=Command.STOP,
        capability=Capability.STOP,
        params={},
    ),
    Generic3DPrinterButtonDescription(
        key="home",
        icon="mdi:home-import-outline",
        command=Command.HOME,
        capability=Capability.HOME,
        params={"axes": "XYZ"},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one button per job-control capability this printer grants."""
    coordinator = async_require_coordinator(hass, entry.entry_id)
    granted = granted_capabilities(entry.runtime_data)
    async_add_entities(
        Generic3DPrinterButton(coordinator, description)
        for description in BUTTON_DESCRIPTIONS
        if granted[description.capability]
    )


class Generic3DPrinterButton(Generic3DPrinterEntity, ButtonEntity):
    """One job-control command, sent through the coordinator."""

    entity_description: Generic3DPrinterButtonDescription

    def __init__(
        self,
        coordinator: PrinterCoordinator,
        description: Generic3DPrinterButtonDescription,
    ) -> None:
        """Bind the entity to its coordinator and its description."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._command = description.command

    async def async_press(self) -> None:
        """Send this button's command, letting a refusal reach the caller."""
        description = self.entity_description
        await self.coordinator.async_send_command(description.command, **description.params)

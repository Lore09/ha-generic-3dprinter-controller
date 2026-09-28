"""Config and options flow.

The user picks a protocol, enters an address, and the flow proves the address
answers before the entry is created. Discovery is offered as a convenience and is
never trusted on its own: a probe that finds a product name fills in the form, and
the user still confirms it. Nothing in this flow sends a command to a printer.

A product line whose generations speak different protocols appears once in the
protocol menu, and a second step asks which model it is, so the user picks the
printer they own rather than a wire protocol they have never heard of.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ACCESS_CODE,
    CONF_API_KEY,
    CONF_CAMERA_PORT,
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_PROTOCOL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TLS,
    CONF_UNSAFE_ENABLED,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    CONF_WEB_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    ProtocolId,
)
from .discovery import DiscoveryResult, async_discover_all, async_identify_host
from .protocols import ConfigError, PrinterConfig, parse_config
from .registry import (
    ADAPTERS,
    FAMILIES,
    AdapterRegistration,
    family_members,
    get_registration,
    protocol_menu,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER = "user"
STEP_PROTOCOL = "protocol"
STEP_MODEL = "model"
STEP_DETAILS = "details"
STEP_PICK = "pick"
STEP_UNSAFE = "unsafe"

_CREDENTIAL_LABELS = {
    CONF_API_KEY: "API key",
    CONF_USERNAME: "Username",
    CONF_PASSWORD: "Password",
    CONF_ACCESS_CODE: "Access code",
}


def _port_or(value: Any, default: int) -> int:
    """Return a stored port as a number, or ``default`` when none was entered."""
    try:
        return int(value) or default
    except (TypeError, ValueError):
        return default


class Generic3DPrinterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the setup of one printer."""

    VERSION = 1

    def __init__(self) -> None:
        """Start an empty flow."""
        self._data: dict[str, Any] = {}
        self._registration: AdapterRegistration | None = None
        self._family: str | None = None
        self._found: list[DiscoveryResult] = []

    # ------------------------------------------------------------------ steps

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer discovery, then the protocol menu."""
        if user_input is not None:
            if user_input.get("discover"):
                return await self.async_step_discover()
            return await self.async_step_protocol()

        return self.async_show_form(
            step_id=STEP_USER,
            data_schema=vol.Schema(
                {vol.Optional("discover", default=False): selector.BooleanSelector()}
            ),
            description_placeholders={"count": str(len(ADAPTERS))},
        )

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask every protocol for its printers and offer the ones not set up yet."""
        found = [
            result
            for result in await async_discover_all(ADAPTERS.values())
            if not self._already_configured(result.protocol, result.host, result.prefill.get(CONF_SERIAL))
        ]
        manual = self._data.get(CONF_HOST)
        if not found and manual:
            probe = await async_identify_host(ADAPTERS.values(), str(manual))
            if probe is not None:
                found = [probe]

        if not found:
            return self.async_show_form(
                step_id=STEP_USER,
                data_schema=vol.Schema(
                    {vol.Optional("discover", default=False): selector.BooleanSelector()}
                ),
                errors={"base": "discovery_failed"},
            )
        if len(found) == 1:
            return await self._async_use_discovered(found[0])
        self._found = found
        return await self.async_step_pick()

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which of several printers that answered to set up."""
        if user_input is not None:
            return await self._async_use_discovered(self._found[int(user_input["printer"])])
        options = [
            selector.SelectOptionDict(
                value=str(index),
                label=f"{result.model or ADAPTERS[result.protocol].label} at {result.host}",
            )
            for index, result in enumerate(self._found)
        ]
        return self.async_show_form(
            step_id=STEP_PICK,
            data_schema=vol.Schema(
                {
                    vol.Required("printer"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    )
                }
            ),
        )

    async def _async_use_discovered(self, result: DiscoveryResult) -> ConfigFlowResult:
        """Fill the form with what a printer said about itself, then show it."""
        registration = ADAPTERS[result.protocol]
        self._data.update(
            {
                CONF_HOST: result.host,
                CONF_PROTOCOL: result.protocol.value,
                CONF_NAME: result.model or registration.label,
            }
        )
        # Only what this protocol's form asks for, so a printer's answer can never
        # set a field its protocol does not use.
        for key, value in result.prefill.items():
            if value and (key in registration.fields or key in registration.credentials):
                self._data[key] = value
        return await self.async_step_details()

    def _already_configured(
        self, protocol: ProtocolId, host: str, serial: Any = None, port: int | None = None
    ) -> bool:
        """Return ``True`` when an entry already drives this printer.

        Matched on the serial number where both sides know it, so a printer whose
        address changed is still recognised, and on the address otherwise. A known
        ``port`` must match too: one host can run several Moonraker or OctoPrint
        instances, one printer each. ``None`` means the port is not known yet, as
        in discovery, and any port on the address matches.
        """
        default = next(iter(get_registration(protocol).ports), 0)
        for entry in self._async_current_entries(include_ignore=False):
            data = {**entry.data, **entry.options}
            if data.get(CONF_PROTOCOL) != protocol.value:
                continue
            if serial and data.get(CONF_SERIAL) == serial:
                return True
            if data.get(CONF_HOST) != host:
                continue
            if port is None or _port_or(data.get(CONF_PORT), default) == port:
                return True
        return False

    async def async_step_protocol(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which protocol the printer speaks, or which product family it is."""
        if user_input is not None:
            choice = str(user_input[CONF_PROTOCOL])
            if choice in FAMILIES:
                self._family = choice
                members = family_members(choice)
                if len(members) == 1:
                    self._data[CONF_PROTOCOL] = members[0].id.value
                    return await self.async_step_details()
                return await self.async_step_model()
            self._data[CONF_PROTOCOL] = choice
            return await self.async_step_details()

        options = [
            selector.SelectOptionDict(value=value, label=label)
            for value, label in protocol_menu()
        ]
        return self.async_show_form(
            step_id=STEP_PROTOCOL,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PROTOCOL): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    )
                }
            ),
        )

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which model of the chosen family the printer is."""
        family = self._family
        if family is None:
            return await self.async_step_protocol()
        members = family_members(family)

        if user_input is not None:
            self._data[CONF_PROTOCOL] = user_input[CONF_PROTOCOL]
            return await self.async_step_details()

        options = [
            selector.SelectOptionDict(
                value=registration.id.value, label=registration.model or registration.label
            )
            for registration in members
        ]
        return self.async_show_form(
            step_id=STEP_MODEL,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PROTOCOL): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    )
                }
            ),
            description_placeholders={"family": FAMILIES[family]},
        )

    async def async_step_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the address and the credentials this protocol needs."""
        protocol_raw = self._data.get(CONF_PROTOCOL)
        try:
            protocol = ProtocolId(str(protocol_raw))
        except ValueError:
            return await self.async_step_protocol()

        registration = get_registration(protocol)
        self._registration = registration
        errors: dict[str, str] = {}

        if user_input is not None:
            candidate = {**self._data, **user_input}
            try:
                config = parse_config(candidate)
                config = await registration.adapter.async_prepare_config(config)
            except ConfigError as err:
                errors["base"] = "invalid_config"
                self._data["_error_detail"] = str(err)
            else:
                # A serial number outlives an address, so it names the entry when the
                # printer reports one. Entries made before this keep their ids.
                await self.async_set_unique_id(
                    f"{config.protocol.value}:{config.serial}"
                    if config.serial
                    else f"{config.protocol.value}:{config.host}:{config.port or 0}"
                )
                self._abort_if_unique_id_configured()
                port = config.port or next(iter(registration.ports), 0)
                if self._already_configured(config.protocol, config.host, config.serial, port):
                    return self.async_abort(reason="already_configured")
                self._data = config.as_dict(include_secrets=True)
                if registration.unsafe:
                    return await self.async_step_unsafe()
                return self._async_create()

        return self.async_show_form(
            step_id=STEP_DETAILS,
            data_schema=self._details_schema(registration),
            errors=errors,
            description_placeholders={
                "protocol": registration.label,
                "ports": ", ".join(str(port) for port in registration.ports) or "any",
                "evidence": "; ".join(
                    f"{key}: {value}" for key, value in registration.evidence.items()
                ),
                "detail": str(self._data.get("_error_detail", "")),
            },
        )

    async def async_step_unsafe(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask about each hazard this protocol declares, one boolean per hazard.

        The schema is generated from the registration, so a new hazard needs no
        change here, and the wording is the adapter's own.
        """
        registration = self._registration
        if registration is None:
            return self._async_create()

        if user_input is not None:
            enabled = [key for key, value in user_input.items() if value]
            self._data[CONF_UNSAFE_ENABLED] = enabled
            return self._async_create()

        schema: dict[Any, Any] = {}
        for feature in registration.unsafe:
            schema[vol.Optional(f"unsafe_{feature.id}", default=False)] = (
                selector.BooleanSelector()
            )
        return self.async_show_form(
            step_id=STEP_UNSAFE,
            data_schema=vol.Schema(schema),
            description_placeholders={
                "hazards": "\n\n".join(
                    f"{feature.label}: {feature.reason}" for feature in registration.unsafe
                )
            },
            last_step=True,
        )

    def _async_create(self) -> ConfigFlowResult:
        data = {key: value for key, value in self._data.items() if not key.startswith("_")}
        return self.async_create_entry(
            title=str(data.get(CONF_NAME) or data.get(CONF_HOST)),
            data=data,
        )

    # ----------------------------------------------------------------- schema

    def _details_schema(self, registration: AdapterRegistration) -> vol.Schema:
        """Build the form from what the protocol declares, not from a per-protocol branch."""
        fields: dict[Any, Any] = {
            vol.Required(CONF_NAME, default=self._data.get(CONF_NAME, "")): str,
            vol.Required(CONF_HOST, default=self._data.get(CONF_HOST, "")): str,
        }

        default_port = registration.ports[0] if registration.ports else None
        fields[
            vol.Optional(CONF_PORT, default=self._data.get(CONF_PORT, default_port))
        ] = vol.Coerce(int)

        if CONF_WEB_URL in registration.fields:
            fields[vol.Optional(CONF_WEB_URL, default=self._data.get(CONF_WEB_URL, ""))] = str
        if CONF_CAMERA_PORT in registration.fields:
            fields[
                vol.Optional(CONF_CAMERA_PORT, default=self._data.get(CONF_CAMERA_PORT, ""))
            ] = vol.Any("", vol.Coerce(int))
        if CONF_TLS in registration.fields:
            fields[
                vol.Optional(CONF_TLS, default=self._data.get(CONF_TLS, False))
            ] = selector.BooleanSelector()
        if CONF_SERIAL in registration.fields:
            fields[vol.Optional(CONF_SERIAL, default=self._data.get(CONF_SERIAL) or "")] = str

        fields[
            vol.Optional(CONF_VERIFY_SSL, default=self._data.get(CONF_VERIFY_SSL, True))
        ] = selector.BooleanSelector()
        fields[
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=self._data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, unit_of_measurement="s"
            )
        )

        for key in registration.credentials:
            if key == CONF_PASSWORD:
                fields[vol.Optional(key, default="")] = selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                )
            else:
                fields[vol.Optional(key, default=self._data.get(key, ""))] = str

        return vol.Schema(fields)

    # ---------------------------------------------------------------- options

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for one printer."""
        return Generic3DPrinterOptionsFlow(entry)


class Generic3DPrinterOptionsFlow(OptionsFlow):
    """Edit one printer, including its declared hazards."""

    def __init__(self, entry: ConfigEntry) -> None:
        """Store the entry being edited."""
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
        current = {**self._entry.data, **self._entry.options}
        try:
            protocol = ProtocolId(str(current.get(CONF_PROTOCOL)))
        except ValueError:
            return self.async_create_entry(title="", data={})

        registration = get_registration(protocol)
        errors: dict[str, str] = {}

        if user_input is not None:
            unsafe = [
                key.removeprefix("unsafe_")
                for key, value in user_input.items()
                if key.startswith("unsafe_") and value
            ]
            merged = {**current, **user_input, CONF_UNSAFE_ENABLED: unsafe}
            merged = {key: value for key, value in merged.items() if not key.startswith("unsafe_")}
            try:
                parse_config(merged)
            except ConfigError as err:
                errors["base"] = "invalid_config"
                _LOGGER.debug("options rejected: %s", err)
            else:
                return self.async_create_entry(title="", data=merged)

        schema: dict[Any, Any] = {
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, "")): str,
            vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
            vol.Optional(CONF_PORT, default=current.get(CONF_PORT)): vol.Any(
                None, "", vol.Coerce(int)
            ),
            vol.Optional(
                CONF_SCAN_INTERVAL, default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, unit_of_measurement="s"
                )
            ),
            vol.Optional(
                CONF_VERIFY_SSL, default=current.get(CONF_VERIFY_SSL, True)
            ): selector.BooleanSelector(),
        }

        if CONF_SERIAL in registration.fields:
            schema[vol.Optional(CONF_SERIAL, default=current.get(CONF_SERIAL) or "")] = str

        for key in registration.credentials:
            schema[vol.Optional(key, default=current.get(key, ""))] = str

        enabled = set(current.get(CONF_UNSAFE_ENABLED) or [])
        for feature in registration.unsafe:
            schema[
                vol.Optional(f"unsafe_{feature.id}", default=feature.id in enabled)
            ] = selector.BooleanSelector()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "protocol": registration.label,
                "hazards": "\n\n".join(
                    f"{feature.label}: {feature.reason}" for feature in registration.unsafe
                )
                or "This protocol declares no hazards.",
            },
        )

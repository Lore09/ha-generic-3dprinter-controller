"""The adapter registry is data, so it is checked like data.

Nothing here talks to a printer. These tests hold down the promise the registry
makes to the rest of the integration: every protocol it names is installed, and
every declaration it carries refers to something that exists.
"""

from __future__ import annotations

import importlib

import pytest

from custom_components.generic_3dprinter import registry
from custom_components.generic_3dprinter.const import ProtocolId


@pytest.mark.parametrize("name", registry.ADAPTER_MODULES)
def test_every_listed_adapter_module_imports(name: str) -> None:
    """A module listed but missing silently removes its protocol from the menu."""
    importlib.import_module(f"custom_components.generic_3dprinter.adapters.{name}")


@pytest.mark.parametrize("protocol", list(ProtocolId))
def test_every_protocol_id_has_a_registration(protocol: ProtocolId) -> None:
    """A protocol id with no registration is a menu entry that cannot be set up."""
    assert protocol in registry.ADAPTERS


@pytest.mark.parametrize("registration", list(registry.ADAPTERS.values()), ids=lambda r: r.id.value)
def test_unsafe_features_gate_only_declared_capabilities(
    registration: registry.AdapterRegistration,
) -> None:
    """An opt-in for a capability the protocol lacks is a question that changes nothing."""
    for feature in registration.unsafe:
        assert feature.gates <= registration.capabilities, feature.id


@pytest.mark.parametrize("registration", list(registry.ADAPTERS.values()), ids=lambda r: r.id.value)
def test_family_members_name_their_model(registration: registry.AdapterRegistration) -> None:
    """A protocol offered through a family is picked by its model name."""
    if registration.family is not None:
        assert registration.family in registry.FAMILIES
        assert registration.model

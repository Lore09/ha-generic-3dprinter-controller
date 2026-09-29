"""The adapter registry is data: every protocol it names exists, and so does what it refers to."""

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


@pytest.mark.parametrize("registration", list(registry.ADAPTERS.values()), ids=lambda r: r.id.value)
def test_model_profiles_are_subsets_with_unique_ids(registration: registry.AdapterRegistration) -> None:
    """A profile may only narrow its protocol, and one id names one model."""
    ids = [profile.id for profile in registration.models]
    assert len(ids) == len(set(ids))
    for profile in registration.models:
        assert profile.capabilities <= registration.capabilities, profile.id


@pytest.mark.parametrize("registration", list(registry.ADAPTERS.values()), ids=lambda r: r.id.value)
def test_a_camera_is_one_kind(registration: registry.AdapterRegistration) -> None:
    """A camera is either relayed as MJPEG or played as a stream, never both."""
    from custom_components.generic_3dprinter.const import Capability

    both = {Capability.CAMERA, Capability.CAMERA_STREAM}
    for capabilities in (registration.capabilities, *(p.capabilities for p in registration.models)):
        if registration.models and capabilities is registration.capabilities:
            continue
        assert not both <= capabilities

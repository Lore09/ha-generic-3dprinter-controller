"""Model profiles: one protocol, several printers, each with what it really has.

The adapter learns which model it reached while it sets up, and from then on the
granted capabilities are the registration's, narrowed to that model and less any
opt-in the user did not grant. A stub adapter stands in for the printer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import pytest

from custom_components.generic_3dprinter.const import (
    Capability,
    Command,
    ModelProfile,
    ProtocolId,
    UnsafeFeature,
)
from custom_components.generic_3dprinter.models import PrinterSnapshot
from custom_components.generic_3dprinter.protocols import (
    Protocol,
    UnsupportedCommandError,
    parse_config,
)

GRANTED = frozenset({Capability.PAUSE, Capability.SET_LIGHT, Capability.CHAMBER_SENSOR, Capability.HOME})
ENCLOSED = ModelProfile(
    id="1",
    name="Enclosed",
    capabilities=frozenset({Capability.PAUSE, Capability.SET_LIGHT, Capability.CHAMBER_SENSOR}),
    verified=True,
)
OPEN = ModelProfile(id="2", name="Open frame", capabilities=frozenset({Capability.PAUSE, Capability.HOME}))


class ProfiledProtocol(Protocol):
    """A printer that says which model it is when it connects."""

    def __init__(self, session: aiohttp.ClientSession, model_id: str | None) -> None:
        config = parse_config({"name": "stub", "protocol": "web_only", "host": "127.0.0.1"})
        super().__init__(config, session, granted=GRANTED, models=(ENCLOSED, OPEN))
        self._reported = model_id
        self.sent: list[Command] = []

    async def async_setup(self) -> None:
        self._set_model_id(self._reported)

    async def async_teardown(self) -> None:
        return None

    async def _async_read(self) -> PrinterSnapshot:
        # Like a real adapter, the first read sets the session up if nothing has.
        if self.model_id is None:
            await self.async_setup()
        return PrinterSnapshot(protocol=ProtocolId.WEB_ONLY, connected=True, model="wire name")

    async def _async_dispatch(self, command: Command, params: Mapping[str, Any]) -> None:
        self.sent.append(command)

    async def async_list_files(self):
        return []

    async def async_upload_file(self, name, stream, *, size=None):
        raise NotImplementedError


@pytest.fixture(name="session")
async def session_fixture():
    async with aiohttp.ClientSession() as session:
        yield session


async def test_before_setup_the_whole_registration_is_granted(session) -> None:
    adapter = ProfiledProtocol(session, "1")
    assert adapter.capabilities == GRANTED
    assert adapter.model_profile is None


async def test_a_known_model_narrows_the_capabilities(session) -> None:
    adapter = ProfiledProtocol(session, "2")
    await adapter.async_setup()
    assert adapter.model_profile is OPEN
    assert adapter.capabilities == frozenset({Capability.PAUSE, Capability.HOME})
    snapshot = await adapter.async_read()
    assert snapshot.capabilities == adapter.capabilities
    assert snapshot.model == "Open frame"
    with pytest.raises(UnsupportedCommandError):
        await adapter.async_send(Command.SET_LIGHT, on=True)
    assert adapter.sent == []


async def test_a_profile_never_grants_what_an_opt_in_withholds(session) -> None:
    config = parse_config({"name": "stub", "protocol": "web_only", "host": "127.0.0.1"})
    adapter = ProfiledProtocol(session, "1")
    feature = UnsafeFeature(
        id="light", label="light", reason="r", gates=frozenset({Capability.SET_LIGHT}), evidence=""
    )
    Protocol.__init__(
        adapter,
        config,
        session,
        granted=GRANTED - {Capability.SET_LIGHT},
        unsafe=(feature,),
        models=(ENCLOSED, OPEN),
    )
    adapter._reported = "1"
    await adapter.async_setup()
    assert Capability.SET_LIGHT not in adapter.capabilities


async def test_an_unknown_model_gets_what_every_known_model_has_and_says_so(session) -> None:
    """A command one model takes can do something else on a model nobody measured."""
    adapter = ProfiledProtocol(session, "99")
    await adapter.async_setup()
    assert adapter.model_profile is None
    assert adapter.capabilities == frozenset({Capability.PAUSE})
    snapshot = await adapter.async_read()
    assert "unknown model 99" in snapshot.errors
    assert snapshot.model == "wire name"
    assert snapshot.capabilities == frozenset({Capability.PAUSE})
    with pytest.raises(UnsupportedCommandError):
        await adapter.async_send(Command.HOME, axes="XYZ")
    assert adapter.sent == []


async def test_a_command_before_the_first_read_is_judged_on_the_model_it_finds(session) -> None:
    adapter = ProfiledProtocol(session, "99")
    with pytest.raises(UnsupportedCommandError):
        await adapter.async_send(Command.HOME, axes="XYZ")
    assert adapter.sent == []

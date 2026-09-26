"""The adapter contract, run against every protocol's fake printer.

Each adapter has tests of its own for its wire format. These hold down what every
adapter promises the rest of the integration, so a new protocol gets them by
adding a harness rather than by remembering to write them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiohttp
import pytest

from custom_components.generic_3dprinter.const import Command, ProtocolId
from custom_components.generic_3dprinter.protocols import (
    CommandBlockedError,
    CommandRejectedError,
    ProtocolError,
    UnreachableError,
    UnsafeCommandError,
    UnsupportedCommandError,
    command_capability,
)
from custom_components.generic_3dprinter.registry import ADAPTERS
from tests.adapter_kit.harness import HARNESSES, SAMPLE_PARAMS, AdapterHarness

PROTOCOLS = list(HARNESSES)


def test_every_registered_protocol_has_a_harness() -> None:
    """A protocol without a harness would skip the whole contract silently."""
    assert set(ADAPTERS) == set(HARNESSES)


@pytest.fixture(name="harness")
async def harness_fixture(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AdapterHarness]:
    """Run one protocol's fake printer with an adapter pointed at it."""
    async with aiohttp.ClientSession() as session:
        async with HARNESSES[request.param](session, monkeypatch) as harness:
            try:
                yield harness
            finally:
                await harness.adapter.async_teardown()


def _with_harness(protocols: list[ProtocolId] = PROTOCOLS):
    return pytest.mark.parametrize("harness", protocols, indirect=True, ids=lambda p: p.value)


@_with_harness()
async def test_setup_and_teardown_are_idempotent(harness: AdapterHarness) -> None:
    """Setup and teardown may each be called twice; teardown leaves nothing open."""
    await harness.adapter.async_setup()
    await harness.adapter.async_setup()
    await harness.adapter.async_teardown()
    await harness.adapter.async_teardown()
    await asyncio.sleep(0.05)
    assert harness.connections() in (None, 0)


@_with_harness()
async def test_a_read_is_a_normalised_snapshot(harness: AdapterHarness) -> None:
    """A snapshot names its protocol and stays inside the units the model promises."""
    await harness.adapter.async_setup()
    snapshot = await harness.adapter.async_read()
    registration = ADAPTERS[harness.protocol]
    assert snapshot.protocol is harness.protocol
    assert snapshot.capabilities == harness.adapter.capabilities
    assert snapshot.capabilities <= registration.capabilities
    for value in (snapshot.progress, *snapshot.fans.as_dict().values()):
        assert value is None or 0 <= value <= 100
    assert snapshot.remaining is None or snapshot.remaining >= 0


def _command_cases() -> list[tuple[ProtocolId, Command]]:
    return [
        (protocol, command)
        for protocol in PROTOCOLS
        for command in Command
        if command_capability(command) in ADAPTERS[protocol].capabilities
        or command in (Command.PAUSE, Command.SET_HOTEND_TEMP)
    ]


@pytest.mark.parametrize(
    ("harness", "command"),
    _command_cases(),
    indirect=["harness"],
    ids=lambda value: value.value,
)
async def test_a_command_reaches_the_wire_only_when_it_may(
    harness: AdapterHarness, command: Command
) -> None:
    """A granted command reaches the printer; one not granted, or blocked, never does."""
    await harness.adapter.async_setup()
    snapshot = await harness.adapter.async_read()
    before = harness.wire()
    granted = command_capability(command) in harness.adapter.capabilities
    try:
        await harness.adapter.async_send(command, **SAMPLE_PARAMS[command])
    except (UnsafeCommandError, UnsupportedCommandError):
        assert not granted
        assert harness.wire() == before
        return
    except CommandBlockedError:
        assert command in snapshot.blocked
        assert harness.wire() == before
        return
    except CommandRejectedError:
        pass
    except ProtocolError as err:
        pytest.fail(f"{command.value} was refused by the adapter, not the printer: {err}")
    assert granted
    assert command not in snapshot.blocked
    assert harness.wire() > before, f"{command.value} never reached the printer"


@_with_harness([protocol for protocol in PROTOCOLS if protocol in (ProtocolId.SDCP_CC1, ProtocolId.ELEGOO_CC2)])
async def test_a_printer_that_went_away_is_reported_and_recovered(harness: AdapterHarness) -> None:
    """A dead printer reads as unreachable, and teardown, setup and read recover it."""
    assert harness.power_off is not None and harness.power_on is not None
    await harness.adapter.async_setup()
    await harness.adapter.async_read()
    await harness.power_off()
    with pytest.raises(UnreachableError):
        for _ in range(3):
            await harness.adapter.async_read()
            await asyncio.sleep(0.2)
    await harness.power_on()
    await harness.adapter.async_teardown()
    await harness.adapter.async_setup()
    snapshot = await harness.adapter.async_read()
    assert snapshot.connected


@_with_harness()
async def test_identifying_a_printer_sends_it_nothing(harness: AdapterHarness) -> None:
    """Discovery may ask a printer who it is, and must never send it a command."""
    before = harness.wire()
    await type(harness.adapter).async_identify(harness.config.host, 0.2)
    assert harness.wire() == before

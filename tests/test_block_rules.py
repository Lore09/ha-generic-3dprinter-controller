"""State rules: commands a printer supports but will not take right now.

The base class reads the printer through the adapter, stamps the capabilities and
the blocked commands on the snapshot, and refuses a blocked command before the
adapter sees it. A stub adapter stands in for a printer, so only the base class is
under test here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import pytest

from custom_components.generic_3dprinter.const import Capability, Command, PrintState, ProtocolId
from custom_components.generic_3dprinter.models import PrinterSnapshot
from custom_components.generic_3dprinter.protocols import (
    DEFAULT_BLOCK_RULES,
    BlockRule,
    CommandBlockedError,
    Protocol,
    not_idle,
    parse_config,
)

GRANTED = frozenset({Capability.HOME, Capability.JOG, Capability.SET_BED_TEMP, Capability.PAUSE})


class StubProtocol(Protocol):
    """A printer whose state the test sets, recording what it was sent."""

    block_rules = (
        *DEFAULT_BLOCK_RULES,
        BlockRule(
            frozenset({Command.SET_BED_TEMP}),
            when=lambda snapshot: snapshot.print_state is PrintState.IDLE,
            reason="only during a print",
        ),
    )

    def __init__(self, session: aiohttp.ClientSession) -> None:
        config = parse_config({"name": "stub", "protocol": "web_only", "host": "127.0.0.1"})
        super().__init__(config, session, granted=GRANTED)
        self.state = PrintState.IDLE
        self.sent: list[Command] = []

    async def async_setup(self) -> None:
        return None

    async def async_teardown(self) -> None:
        return None

    async def _async_read(self) -> PrinterSnapshot:
        # Deliberately wrong capabilities: the template must replace them.
        return PrinterSnapshot(protocol=ProtocolId.WEB_ONLY, connected=True, print_state=self.state)

    async def _async_dispatch(self, command: Command, params: Mapping[str, Any]) -> None:
        self.sent.append(command)

    async def async_list_files(self):
        return []

    async def async_upload_file(self, name, stream, *, size=None):
        raise NotImplementedError


@pytest.fixture(name="stub")
async def stub_fixture():
    async with aiohttp.ClientSession() as session:
        yield StubProtocol(session)


async def test_the_read_carries_the_granted_capabilities(stub: StubProtocol) -> None:
    snapshot = await stub.async_read()
    assert snapshot.capabilities == GRANTED
    assert stub.last_snapshot is snapshot


async def test_an_idle_printer_blocks_what_its_rules_say(stub: StubProtocol) -> None:
    snapshot = await stub.async_read()
    assert dict(snapshot.blocked) == {Command.SET_BED_TEMP: "only during a print"}
    assert snapshot.as_dict()["blocked"] == {"set_bed_temp": "only during a print"}


async def test_a_job_blocks_motion_by_default(stub: StubProtocol) -> None:
    stub.state = PrintState.PRINTING
    snapshot = await stub.async_read()
    assert set(snapshot.blocked) == {Command.HOME, Command.JOG}
    assert "busy" in snapshot.blocked[Command.HOME]


async def test_a_command_the_printer_lacks_is_never_listed_as_blocked(stub: StubProtocol) -> None:
    stub.state = PrintState.PRINTING
    snapshot = await stub.async_read()
    assert Command.START_PRINT not in snapshot.blocked


async def test_a_blocked_command_never_reaches_the_adapter(stub: StubProtocol) -> None:
    stub.state = PrintState.PRINTING
    await stub.async_read()
    with pytest.raises(CommandBlockedError, match="busy") as caught:
        await stub.async_send(Command.HOME, axes="XYZ")
    assert caught.value.command is Command.HOME
    assert stub.sent == []
    await stub.async_send(Command.SET_BED_TEMP, value=40)
    assert stub.sent == [Command.SET_BED_TEMP]


async def test_a_command_before_the_first_read_is_judged_on_a_fresh_read(stub: StubProtocol) -> None:
    stub.state = PrintState.PRINTING
    with pytest.raises(CommandBlockedError):
        await stub.async_send(Command.HOME, axes="XYZ")
    assert stub.sent == []
    assert stub.last_snapshot is not None


def test_not_idle() -> None:
    idle = PrinterSnapshot(protocol=ProtocolId.WEB_ONLY, connected=True, print_state=PrintState.IDLE)
    busy = PrinterSnapshot(protocol=ProtocolId.WEB_ONLY, connected=True, print_state=PrintState.PREPARING)
    assert not not_idle(idle)
    assert not_idle(busy)

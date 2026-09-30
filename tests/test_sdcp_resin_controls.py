"""Pause, resume, stop and delete on the fake Saturn, with the exact frames each one sends,
and the rule that keeps a print from starting on a machine that is not idle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest

from custom_components.generic_3dprinter.adapters.sdcp_resin import (
    RESIN_COMMAND,
    SdcpResinProtocol,
    machine_busy,
)
from custom_components.generic_3dprinter.const import Capability, Command, PrintState, ProtocolId
from custom_components.generic_3dprinter.models import PrinterSnapshot, ResinState
from custom_components.generic_3dprinter.protocols import (
    CommandBlockedError,
    CommandRejectedError,
    parse_config,
)
from custom_components.generic_3dprinter.registry import build_adapter
from tests.fake_resin_printer import FakeResinPrinter, load_fixture

SATURN = load_fixture()["attributes"]["Attributes"]["MainboardID"]
CAPTURED_FILE = "/local/SUP_allineatore_01_1_202609301434.goo"


@pytest.fixture(name="session")
async def session_fixture() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture(name="printing")
async def printing_fixture() -> AsyncIterator[FakeResinPrinter]:
    """A fake Saturn mid-print, as elegoo-homeassistant issue #21 reported one."""
    server = FakeResinPrinter(printing=True)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _config(port: int) -> Any:
    return parse_config(
        {"name": "Saturn", "protocol": "sdcp_resin", "host": "127.0.0.1", "port": port, "serial": SATURN}
    )


def _adapter(port: int, session: aiohttp.ClientSession) -> SdcpResinProtocol:
    adapter = build_adapter(_config(port), session)
    assert isinstance(adapter, SdcpResinProtocol)
    return adapter


def _acts(printer: FakeResinPrinter) -> list[dict[str, Any]]:
    """Return every request that is not a read, as ``{Cmd: Data}`` pairs."""
    return [{item["Cmd"]: item["Data"]} for item in printer.received if item["Cmd"] not in (0, 1, 258)]


# ------------------------------------------------------------ pause, resume, stop


@pytest.mark.parametrize(
    ("command", "code"), [(Command.PAUSE, 129), (Command.RESUME, 131), (Command.STOP, 130)]
)
async def test_a_job_control_sends_its_code_with_an_empty_data(
    printing: FakeResinPrinter, session: aiohttp.ClientSession, command: Command, code: int
) -> None:
    adapter = _adapter(printing.port, session)
    snapshot = await adapter.async_read()
    assert snapshot.print_state is PrintState.PRINTING
    await adapter.async_send(command)
    assert _acts(printing) == [{code: {}}]
    frame = printing.received[-1]
    assert (frame["MainboardID"], frame["From"]) == (SATURN, 1)
    assert printing.forbidden == []
    assert not printing.crashed
    await adapter.async_teardown()


async def test_a_refused_control_says_why(printing: FakeResinPrinter, session: aiohttp.ClientSession) -> None:
    printing.acks[129] = 1
    adapter = _adapter(printing.port, session)
    with pytest.raises(CommandRejectedError, match="the printer is busy") as caught:
        await adapter.async_send(Command.PAUSE)
    assert (caught.value.code, caught.value.reason) == (1, "the printer is busy")
    await adapter.async_teardown()


async def test_start_print_and_video_acks_read_their_own_meaning(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The codes 128 and 386 give their own meaning, and a shared one falls back to the table."""
    adapter = _adapter(resin_printer.port, session)
    monkeypatch.setattr(adapter, "commands", {**RESIN_COMMAND, "start_print": 128, "video": 386})
    cases = [
        ("start_print", 5, "the file's resolution does not match the printer"),
        ("start_print", 7, "the file was sliced for another printer model"),
        ("start_print", 1, "the printer is busy"),
        ("video", 1, "every video stream the printer allows is in use"),
        ("video", 2, "the printer has no camera"),
        ("pause", 2, "the file was not found"),
        ("stop", 9, None),
    ]
    for name, ack, reason in cases:
        async def answer(cmd: int, data: Any = None, *, ack: int = ack, **_: Any) -> dict[str, int]:
            return {"Ack": ack}

        monkeypatch.setattr(adapter, "_async_request", answer)
        with pytest.raises(CommandRejectedError) as caught:
            await adapter._async_send_checked(name, {})  # noqa: SLF001
        assert caught.value.reason == reason, (name, ack)
    assert resin_printer.sent_commands == []


# ------------------------------------------------------------------ delete


async def test_delete_sends_the_path_and_checks_the_folder(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    adapter = _adapter(resin_printer.port, session)
    await adapter.async_send(Command.DELETE_FILE, filename=CAPTURED_FILE)
    assert _acts(resin_printer) == [{259: {"FileList": [CAPTURED_FILE], "FolderList": []}}]
    assert resin_printer.sent_commands[-2:] == [259, 258]
    assert resin_printer.received[-1]["Data"] == {"Url": "/local"}
    assert resin_printer.files == []
    assert await adapter.async_list_files() == []
    await adapter.async_teardown()


async def test_a_bare_name_is_deleted_from_local(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    adapter = _adapter(resin_printer.port, session)
    await adapter.async_send(Command.DELETE_FILE, filename=CAPTURED_FILE.rsplit("/", 1)[1])
    assert _acts(resin_printer) == [{259: {"FileList": [CAPTURED_FILE], "FolderList": []}}]
    assert resin_printer.files == []
    await adapter.async_teardown()


async def test_a_file_on_usb_is_checked_in_its_own_folder(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    resin_printer.files.append({"name": "/usb/part.goo", "type": 1})
    resin_printer.keep_deleted = True
    adapter = _adapter(resin_printer.port, session)
    with pytest.raises(CommandRejectedError, match="did not delete /usb/part.goo"):
        await adapter.async_send(Command.DELETE_FILE, filename="/usb/part.goo")
    assert resin_printer.received[-1]["Data"] == {"Url": "/usb"}
    await adapter.async_teardown()


async def test_a_file_still_listed_after_the_ack_is_refused(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    """The printer acks 0 whatever happened, so the list decides."""
    resin_printer.keep_deleted = True
    adapter = _adapter(resin_printer.port, session)
    with pytest.raises(CommandRejectedError) as caught:
        await adapter.async_send(Command.DELETE_FILE, filename=CAPTURED_FILE)
    assert caught.value.reason == "the file is still on the printer"
    assert [item.path for item in await adapter.async_list_files()] == [CAPTURED_FILE]
    await adapter.async_teardown()


async def test_a_file_the_printer_names_in_errdata_is_refused(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(resin_printer.port, session)

    async def answer(name: str, data: Any) -> dict[str, Any]:
        return {"Ack": 0, "ErrData": list(data["FileList"])}

    async def empty(url: str) -> list[Any]:
        return []

    monkeypatch.setattr(adapter, "_async_send_checked", answer)
    monkeypatch.setattr(adapter, "_async_list_folder", empty)
    with pytest.raises(CommandRejectedError, match="did not delete"):
        await adapter._async_dispatch(Command.DELETE_FILE, {"filename": CAPTURED_FILE})  # noqa: SLF001


async def test_a_refused_delete_does_not_remove_the_file(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    resin_printer.acks[259] = 2
    adapter = _adapter(resin_printer.port, session)
    with pytest.raises(CommandRejectedError, match="the file was not found"):
        await adapter.async_send(Command.DELETE_FILE, filename=CAPTURED_FILE)
    assert resin_printer.sent_commands[-1] == 259
    assert len(resin_printer.files) == 1
    await adapter.async_teardown()


# -------------------------------------------------------------- block rule


def _snapshot(resin: ResinState | None) -> PrinterSnapshot:
    return PrinterSnapshot(
        protocol=ProtocolId.SDCP_RESIN,
        connected=True,
        capabilities=frozenset({Capability.START_PRINT, Capability.PAUSE}),
        print_state=PrintState.IDLE,
        resin=resin,
    )


@pytest.mark.parametrize(
    ("machine", "busy"),
    [
        ("idle", False),
        ("file_transferring", True),
        ("self_check", True),
        ("exposure_test", True),
        ("file_received", True),
        ("printing", True),
        ("other", True),
        (None, True),
    ],
)
def test_start_print_waits_for_an_idle_machine(machine: str | None, busy: bool) -> None:
    assert machine_busy(_snapshot(ResinState(machine=machine))) is busy


def test_a_snapshot_without_resin_readings_is_busy() -> None:
    assert machine_busy(_snapshot(None))


@pytest.mark.parametrize("code", [2, 3, 4], ids=["file_transferring", "exposure_test", "self_check"])
async def test_the_block_rule_fires_on_the_wire_path(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, code: int
) -> None:
    """The print state reads idle here, so only the machine's own status blocks the start."""
    resin_printer.status["CurrentStatus"] = [code]
    granted = frozenset({Capability.START_PRINT, Capability.RESIN_STATUS, Capability.FILE_LIST})
    adapter = SdcpResinProtocol(_config(resin_printer.port), session, granted=granted)
    snapshot = await adapter.async_read()
    assert snapshot.print_state is PrintState.IDLE
    assert snapshot.blocked == {Command.START_PRINT: "the printer is not idle"}
    with pytest.raises(CommandBlockedError, match="the printer is not idle"):
        await adapter.async_send(Command.START_PRINT, filename="part.goo")
    assert set(resin_printer.sent_commands) == {0, 1}
    assert resin_printer.forbidden == []
    await adapter.async_teardown()


async def test_an_idle_machine_blocks_nothing(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    granted = frozenset({Capability.START_PRINT, Capability.RESIN_STATUS, Capability.PAUSE})
    adapter = SdcpResinProtocol(_config(resin_printer.port), session, granted=granted)
    assert (await adapter.async_read()).blocked == {}
    await adapter.async_teardown()


async def test_a_print_in_hand_blocks_start_print(
    printing: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    granted = frozenset({Capability.START_PRINT, Capability.RESIN_STATUS, Capability.PAUSE})
    adapter = SdcpResinProtocol(_config(printing.port), session, granted=granted)
    snapshot = await adapter.async_read()
    assert snapshot.blocked == {Command.START_PRINT: "the printer is not idle"}
    await adapter.async_teardown()

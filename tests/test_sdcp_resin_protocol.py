"""The resin SDCP adapter against the fake Saturn, and its pure mappings against the capture."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest

from custom_components.generic_3dprinter.adapters import sdcp, sdcp_resin
from custom_components.generic_3dprinter.adapters.sdcp_resin import (
    SdcpResinProtocol,
    build_resin_frame,
    machine_for,
    parse_resin,
    resin_state_for,
)
from custom_components.generic_3dprinter.const import Capability, Command, PrintState, ProtocolId, ResinPhase
from custom_components.generic_3dprinter.protocols import (
    ConfigError,
    ProtocolError,
    UnsafeCommandError,
    UnsupportedCommandError,
    WrongPrinterError,
    parse_config,
)
from custom_components.generic_3dprinter.registry import build_adapter
from tests.adapter_kit.harness import SAMPLE_PARAMS
from tests.fake_printer import MAINBOARD as CC1_MAINBOARD
from tests.fake_printer import FakePrinterServer
from tests.fake_resin_printer import PRINTING_STATUS, FakeResinPrinter, load_fixture

FIXTURE = load_fixture()
IDLE_STATUS: dict[str, Any] = FIXTURE["status"]["Status"]
ATTRIBUTES: dict[str, Any] = FIXTURE["attributes"]["Attributes"]
SATURN = ATTRIBUTES["MainboardID"]
#: The commands a resin printer is granted.
CONTROLS = frozenset({Command.PAUSE, Command.RESUME, Command.STOP, Command.DELETE_FILE})
#: What a Saturn 4 Ultra 16K is granted, and what every resin model has.
BASE = frozenset(
    {Capability.FILE_LIST, Capability.FILE_UPLOAD, Capability.RESIN_STATUS, Capability.PAUSE,
     Capability.RESUME, Capability.STOP, Capability.FILE_DELETE}
)


@pytest.fixture(name="session")
async def session_fixture() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


def _config(port: int, serial: str | None = SATURN):
    data = {"name": "Saturn", "protocol": "sdcp_resin", "host": "127.0.0.1", "port": port}
    if serial is not None:
        data["serial"] = serial
    return parse_config(data)


def _adapter(port: int, session: aiohttp.ClientSession, serial: str | None = SATURN) -> SdcpResinProtocol:
    adapter = build_adapter(_config(port, serial), session)
    assert isinstance(adapter, SdcpResinProtocol)
    return adapter


async def _closed(printer: Any) -> None:
    for _ in range(50):
        if not printer._sockets:  # noqa: SLF001
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the adapter left its socket open")


# ------------------------------------------------------------------- states


@pytest.mark.parametrize(
    ("flags", "status", "state", "phase"),
    [
        ((1,), 0, PrintState.PREPARING, ResinPhase.STARTING),
        ((1,), 1, PrintState.PREPARING, ResinPhase.HOMING),
        ((1,), 2, PrintState.PRINTING, ResinPhase.DESCENDING),
        ((1,), 3, PrintState.PRINTING, ResinPhase.EXPOSING),
        ((1,), 4, PrintState.PRINTING, ResinPhase.LIFTING),
        ((1,), 5, PrintState.PAUSED, ResinPhase.PAUSING),
        ((1,), 6, PrintState.PAUSED, ResinPhase.PAUSED),
        ((1,), 7, PrintState.CANCELLED, ResinPhase.STOPPING),
        ((1,), 8, PrintState.CANCELLED, ResinPhase.STOPPED),
        ((1,), 9, PrintState.FINISHED, ResinPhase.COMPLETED),
        ((1,), 10, PrintState.PREPARING, ResinPhase.FILE_CHECKING),
        ((1,), 16, PrintState.PREPARING, ResinPhase.PREHEATING),
        ((1,), 12, PrintState.PRINTING, ResinPhase.OTHER),
        ((1,), 25, PrintState.PRINTING, ResinPhase.OTHER),
        ((1,), None, PrintState.PRINTING, ResinPhase.OTHER),
        ((2,), 0, PrintState.IDLE, ResinPhase.FILE_TRANSFERRING),
        ((3,), 0, PrintState.IDLE, ResinPhase.EXPOSURE_TEST),
        ((4,), 0, PrintState.IDLE, ResinPhase.SELF_CHECK),
        ((8,), 0, PrintState.IDLE, ResinPhase.FILE_RECEIVED),
        ((0,), 0, PrintState.IDLE, ResinPhase.IDLE),
        ((), 0, PrintState.UNKNOWN, None),
        ((), None, PrintState.UNKNOWN, None),
        # The spec keeps an ended job's code at idle; V1.5.6 was seen resetting it.
        ((0,), 9, PrintState.FINISHED, ResinPhase.COMPLETED),
        ((0,), 8, PrintState.CANCELLED, ResinPhase.STOPPED),
        ((0,), 4, PrintState.IDLE, ResinPhase.IDLE),
        ((6,), 0, PrintState.UNKNOWN, ResinPhase.OTHER),
        ((1, 8), 3, PrintState.PRINTING, ResinPhase.EXPOSING),
    ],
)
def test_resin_state_for_every_row(
    flags: tuple[int, ...], status: int | None, state: PrintState, phase: ResinPhase | None
) -> None:
    assert resin_state_for(flags, status) == (state, phase)


@pytest.mark.parametrize(
    ("flags", "machine"),
    [((0,), "idle"), ((1,), "printing"), ((2,), "file_transferring"), ((3,), "exposure_test"),
     ((4,), "self_check"), ((8,), "file_received"), ((6,), "other"), ((0, 2), "file_transferring"), ((), None)],
)
def test_machine_for(flags: tuple[int, ...], machine: str | None) -> None:
    assert machine_for(flags) == machine


# -------------------------------------------------------------------- units


def test_the_idle_capture_keeps_the_filename_and_hides_the_last_job() -> None:
    """The idle Saturn still says 283 of 283 layers and 46 minutes; none of it is a job now."""
    fields = parse_resin(IDLE_STATUS, ATTRIBUTES)
    assert fields["print_state"] is PrintState.IDLE
    for key in ("progress", "current_layer", "total_layers", "elapsed", "remaining"):
        assert fields[key] is None, key
    assert fields["filename"] == "SUP_allineatore_01_1_202609301434.goo"
    assert fields["job_id"] is None
    assert fields["errors"] == ()
    resin = fields["resin"]
    assert (resin.machine, resin.phase, resin.phase_code) == ("idle", ResinPhase.IDLE, 0)
    assert resin.uv_led == pytest.approx(29.0777, abs=1e-3)
    assert (resin.vat.current, resin.vat.target, resin.vat_heat_status) == (29, 30, 1)
    assert (resin.release_film, resin.release_film_max) == (283, 60000)
    assert resin.printer_timelapse is False
    assert resin.device_faults == ()
    assert (resin.video_streams, resin.video_streams_max) == (0, 2)


def test_the_printing_capture_reads_ticks_as_milliseconds() -> None:
    status = {**IDLE_STATUS, **PRINTING_STATUS}
    fields = parse_resin(status, ATTRIBUTES)
    assert fields["print_state"] is PrintState.PRINTING
    assert fields["elapsed"] == pytest.approx(20313.461)
    assert fields["remaining"] == pytest.approx(5048.004)
    assert fields["progress"] == pytest.approx(77.84, abs=0.01)
    assert (fields["current_layer"], fields["total_layers"]) == (3995, 5132)
    assert fields["filename"] == "model.goo"
    assert fields["job_id"] == "52706856"
    resin = fields["resin"]
    assert (resin.machine, resin.phase, resin.phase_code) == ("printing", ResinPhase.LIFTING, 4)
    assert (resin.vat.current, resin.vat.target, resin.vat_heat_status) == (20, 30, 0)


def test_a_finished_job_is_whole_and_has_no_times() -> None:
    info = {**IDLE_STATUS["PrintInfo"], "Status": 9}
    fields = parse_resin({**IDLE_STATUS, "CurrentStatus": [1], "PrintInfo": info}, ATTRIBUTES)
    assert fields["print_state"] is PrintState.FINISHED
    assert fields["progress"] == 100.0
    assert fields["elapsed"] is fields["remaining"] is fields["current_layer"] is None


def test_errors_faults_and_a_worn_film_are_reported() -> None:
    info = {**IDLE_STATUS["PrintInfo"], "ErrorNumber": 3}
    status = {**IDLE_STATUS, "PrintInfo": info, "ReleaseFilm": 60000}
    devices = {**ATTRIBUTES["DevicesStatus"], "TankStatus": 0, "LCDStatus": 2}
    fields = parse_resin(status, {**ATTRIBUTES, "DevicesStatus": devices})
    assert fields["resin"].device_faults == ("LCDStatus", "TankStatus")
    assert fields["errors"] == (
        "the print file's resolution does not match the printer",
        "the printer's LCDStatus check is failing",
        "the printer's TankStatus check is failing",
        "the release film has reached the 60000 lifts it is rated for",
    )
    unknown = parse_resin({**IDLE_STATUS, "PrintInfo": {**info, "ErrorNumber": 42}}, ATTRIBUTES)
    assert unknown["errors"] == ("the printer reports print error 42",)


def test_an_empty_status_is_unknown() -> None:
    fields = parse_resin({}, {})
    assert fields["print_state"] is PrintState.UNKNOWN
    assert fields["resin"].machine is None
    assert fields["resin"].phase is None


# ---------------------------------------------------------- what it can send


def test_it_can_send_only_reads_the_job_controls_and_the_video_switch() -> None:
    codes = set(SdcpResinProtocol.commands.values())
    assert codes == {0, 1, 258, 128, 129, 130, 131, 259, 386}
    assert not codes & {324, 387, 403}
    for name in ("set_printer_params", "_async_read_canvas", "camera_url", "web_ui_url", "_async_enable_video"):
        assert not hasattr(SdcpResinProtocol, name), name
    assert not any(name.startswith("_async_set_") for name in dir(SdcpResinProtocol))


async def test_every_command_but_the_controls_is_refused_before_the_wire(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    adapter = _adapter(resin_printer.port, session)
    await adapter.async_read()
    before = list(resin_printer.sent_commands)
    for command in set(Command) - CONTROLS - {Command.START_PRINT}:
        with pytest.raises(UnsupportedCommandError):
            await adapter.async_send(command, **SAMPLE_PARAMS[command])
    with pytest.raises(UnsafeCommandError):
        await adapter.async_send(Command.START_PRINT, filename="part.goo")
    with pytest.raises(ProtocolError):
        await adapter.async_upload_file("part.gcode", _chunks(b"x"))
    assert resin_printer.sent_commands == before
    assert set(before) == {0, 1}
    assert resin_printer.forbidden == []
    await adapter.async_teardown()


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


# ------------------------------------------------------------------ envelope


def test_the_envelope_is_the_one_the_saturn_answered() -> None:
    request_id, raw = build_resin_frame(SATURN, 0)
    frame = json.loads(raw)
    assert frame["Topic"] == f"sdcp/request/{SATURN}"
    assert frame["Id"] and frame["Id"] != request_id
    inner = frame["Data"]
    assert (inner["Cmd"], inner["Data"], inner["From"]) == (0, {}, 1)
    assert inner["RequestID"] == request_id
    assert inner["MainboardID"] == SATURN
    assert isinstance(inner["TimeStamp"], int) and inner["TimeStamp"] < 10**11


async def test_a_strict_printer_answers_from_the_first_command(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    """The first command 1 already carries the entry's mainboard id, so nothing is ignored."""
    resin_printer.strict_envelope = True
    adapter = _adapter(resin_printer.port, session)
    snapshot = await adapter.async_read()
    assert resin_printer.ignored == []
    assert resin_printer.sent_commands == [1, 0]
    assert all(item["MainboardID"] == SATURN for item in resin_printer.received)
    assert snapshot.protocol is ProtocolId.SDCP_RESIN
    assert snapshot.connected
    await adapter.async_teardown()


# ----------------------------------------------------------------- the read


async def test_a_read_of_the_idle_saturn(resin_printer: FakeResinPrinter, session: aiohttp.ClientSession) -> None:
    adapter = _adapter(resin_printer.port, session)
    await adapter.async_setup()
    snapshot = await adapter.async_read()
    assert snapshot.model == "Elegoo Saturn 4 Ultra 16K"
    assert adapter.model_id == "Saturn 4 Ultra 16K"
    assert snapshot.capabilities == BASE | {Capability.VAT_SENSOR}
    assert (snapshot.firmware, snapshot.serial) == ("V1.5.6", SATURN)
    assert snapshot.print_state is PrintState.IDLE
    assert snapshot.progress is snapshot.elapsed is snapshot.current_layer is None
    assert snapshot.hotend.current is snapshot.bed.current is None
    assert snapshot.camera is False
    assert snapshot.resin is not None and snapshot.resin.vat.current == 29
    assert snapshot.errors == ()
    assert snapshot.blocked == {}
    files = await adapter.async_list_files()
    assert [item.name for item in files] == ["/local/SUP_allineatore_01_1_202609301434.goo"]
    assert resin_printer.sent_commands == [1, 0, 258]
    await adapter.async_teardown()


async def test_the_attributes_are_asked_for_again_after_five_minutes(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(resin_printer.port, session)
    await adapter.async_read()
    await adapter.async_read()
    assert resin_printer.sent_commands == [1, 0]
    monkeypatch.setattr(sdcp_resin, "ATTRIBUTES_INTERVAL", 0.0)
    resin_printer.video_streams = 1
    snapshot = await adapter.async_read()
    assert resin_printer.sent_commands == [1, 0, 1]
    assert snapshot.resin is not None and snapshot.resin.video_streams == 1
    await adapter.async_teardown()


async def test_an_unknown_model_gets_what_every_model_has(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    resin_printer.attributes["MachineName"] = "Saturn 5 Ultra"
    adapter = _adapter(resin_printer.port, session)
    snapshot = await adapter.async_read()
    assert snapshot.capabilities == BASE
    assert "unknown model Saturn 5 Ultra" in snapshot.errors
    await adapter.async_teardown()


async def test_the_heartbeat_is_a_status_request_and_keeps_the_socket(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Saturn never answers ``ping``; a printer that drops quiet clients keeps this one."""
    monkeypatch.setattr(sdcp, "HEARTBEAT_INTERVAL", 0.1)
    resin_printer.silent_close_after = 0.5
    adapter = _adapter(resin_printer.port, session)
    await adapter.async_setup()
    await asyncio.sleep(1.2)
    assert resin_printer.connections == 1
    assert adapter._connected  # noqa: SLF001
    assert resin_printer.texts == []
    assert resin_printer.sent_commands.count(0) >= 5
    assert set(resin_printer.sent_commands) == {0, 1}
    assert resin_printer.forbidden == []
    await adapter.async_teardown()


# ------------------------------------------------------------ wrong printers


async def test_another_saturn_is_refused_at_command_one(
    resin_printer: FakeResinPrinter, session: aiohttp.ClientSession
) -> None:
    adapter = _adapter(resin_printer.port, session, serial="0000000000000000")
    with pytest.raises(WrongPrinterError, match=SATURN) as caught:
        await adapter.async_setup()
    assert caught.value.translation_key == "other_resin_printer"
    assert resin_printer.sent_commands == [1]
    assert adapter._ws is None  # noqa: SLF001
    await _closed(resin_printer)
    await adapter.async_teardown()


async def test_a_centauri_carbon_is_refused_at_command_one(session: aiohttp.ClientSession) -> None:
    printer = FakePrinterServer()
    await printer.start()
    try:
        adapter = _adapter(int(printer.url.rsplit(":", 1)[1]), session, serial=CC1_MAINBOARD)
        with pytest.raises(WrongPrinterError, match="Centauri Carbon") as caught:
            await adapter.async_setup()
        assert caught.value.translation_key == "not_a_resin_printer"
        assert caught.value.model == "Centauri Carbon"
        assert printer.sent_commands == [1]
        with pytest.raises(WrongPrinterError):
            await adapter.async_read()
        assert set(printer.sent_commands) == {1}
        await _closed(printer)
        await adapter.async_teardown()
    finally:
        await printer.stop()


# ------------------------------------------------------- config and discovery


def _probe(reply: dict[str, Any] | None, seen: list[Any], sender: str = "192.0.2.43") -> Any:
    async def probe(probe: bytes, target: tuple[str, int], timeout: float, accept: Any = None):
        seen.append((probe, target))
        if reply is None or (accept is not None and not accept(reply)):
            return None
        return reply, sender

    return probe


FLAT_SATURN = {"Id": "x", "Data": {**ATTRIBUTES, "MainboardIP": "192.0.2.43"}}
NESTED_SATURN = {"Id": "x", "Data": {"Attributes": {**ATTRIBUTES, "MainboardIP": ""}, "Status": IDLE_STATUS}}
CENTAURI = {"Id": "x", "Data": {"MachineName": "Centauri Carbon", "MainboardID": CC1_MAINBOARD}}


def _bare(serial: str | None = None):
    data = {"name": "p", "protocol": "sdcp_resin", "host": "192.0.2.43"}
    if serial:
        data["serial"] = serial
    return parse_config(data)


async def test_prepare_config_learns_the_mainboard_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(sdcp_resin, "async_probe_udp", _probe(FLAT_SATURN, seen))
    config = await SdcpResinProtocol.async_prepare_config(_bare())
    assert config.serial == SATURN
    assert seen == [(b"M99999", ("192.0.2.43", 3000))]


@pytest.mark.parametrize(
    ("reply", "serial", "message"),
    [
        (CENTAURI, None, "Centauri Carbon, which is not a resin printer"),
        ({"Data": {**ATTRIBUTES, "ProtocolVersion": "V1.0.0"}}, None, "MQTT"),
        (None, None, "did not answer"),
        (FLAT_SATURN, "0000000000000000", "reports mainboard id"),
        ({"Data": {"MachineName": "Saturn 4 Ultra"}}, None, "did not say its mainboard id"),
    ],
    ids=["fdm", "sdcp-v1", "silent", "other-saturn", "no-id"],
)
async def test_prepare_config_refuses(
    monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any] | None, serial: str | None, message: str
) -> None:
    monkeypatch.setattr(sdcp_resin, "async_probe_udp", _probe(reply, []))
    with pytest.raises(ConfigError, match=message):
        await SdcpResinProtocol.async_prepare_config(_bare(serial))


async def test_prepare_config_keeps_a_typed_id_when_nothing_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdcp_resin, "async_probe_udp", _probe(None, []))
    assert (await SdcpResinProtocol.async_prepare_config(_bare(SATURN))).serial == SATURN


async def test_identify_takes_only_a_resin_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdcp_resin, "async_probe_udp", _probe(NESTED_SATURN, []))
    found = await SdcpResinProtocol.async_identify("192.0.2.43", 0.1)
    assert found is not None
    assert (found.host, found.protocol, found.model, found.model_id) == (
        "192.0.2.43", ProtocolId.SDCP_RESIN, "Saturn 4 Ultra 16K", "Saturn 4 Ultra 16K"
    )
    assert found.prefill == {"serial": SATURN}
    monkeypatch.setattr(sdcp_resin, "async_probe_udp", _probe(CENTAURI, []))
    assert await SdcpResinProtocol.async_identify("192.0.2.43", 0.1) is None


async def test_discovery_splits_the_two_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Saturn is offered as a resin printer only, and a Centauri Carbon never as one."""
    for reply, resin, fdm in ((FLAT_SATURN, 1, 0), (CENTAURI, 0, 1)):
        seen: list[Any] = []
        monkeypatch.setattr(sdcp_resin, "async_probe_udp", _probe(reply, seen))
        monkeypatch.setattr(sdcp, "async_probe_udp", _probe(reply, seen))
        assert len(await SdcpResinProtocol.async_discover(0.1)) == resin
        assert len(await sdcp.SdcpProtocol.async_discover(0.1)) == fdm
        assert {target for _, target in seen} == {("255.255.255.255", 3000)}

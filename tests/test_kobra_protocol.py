"""The Anycubic Kobra adapter against a fake printer that checks the handshake's signature."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiohttp
import pytest

from custom_components.generic_3dprinter.adapters import anycubic_kobra as kobra
from custom_components.generic_3dprinter.const import Capability, Command, LightChannel, PrintState
from custom_components.generic_3dprinter.protocols import (
    CommandBlockedError,
    CommandRejectedError,
    ConfigError,
    ProtocolError,
    UnreachableError,
    UnsafeCommandError,
    parse_config,
)
from custom_components.generic_3dprinter.registry import build_adapter
from tests.fake_kobra_printer import SERIAL, FakeKobraPrinter


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kobra, "ANSWER_WINDOW", 0.3)
    monkeypatch.setattr(kobra, "FIRST_INFO_TIMEOUT", 1.0)
    monkeypatch.setattr(kobra, "INFO_WAIT", 0.5)
    monkeypatch.setattr(kobra, "CAPTURE_KICK_DELAY", 0.0)
    monkeypatch.setattr(kobra, "VIDEO_REPORT_TIMEOUT", 1.0)
    monkeypatch.setattr(kobra, "FILE_LIST_TIMEOUT", 1.0)


@pytest.fixture(name="session")
async def session_fixture() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


async def _printer(**kwargs) -> FakeKobraPrinter:
    printer = FakeKobraPrinter(**kwargs)
    await printer.start()
    return printer


def _adapter(printer: FakeKobraPrinter, session: aiohttp.ClientSession, **extra) -> kobra.AnycubicKobraProtocol:
    config = parse_config(
        {"name": "Kobra", "protocol": "anycubic_kobra", "host": "127.0.0.1", "port": printer.port, **extra}
    )
    return build_adapter(config, session)  # type: ignore[return-value]


@pytest.fixture(name="kobra_x")
async def kobra_x_fixture() -> AsyncIterator[FakeKobraPrinter]:
    printer = await _printer()
    yield printer
    await printer.stop()
    printer.close()


async def test_a_kobra_x_is_read_through_the_signed_handshake(kobra_x, session) -> None:
    adapter = _adapter(kobra_x, session)
    try:
        snapshot = await adapter.async_read()
    finally:
        await adapter.async_teardown()
    assert kobra_x.handshakes == 1
    assert adapter.model_profile is not None and adapter.model_profile.name == "Anycubic Kobra X"
    assert Capability.HOME in snapshot.capabilities and Capability.CHAMBER_SENSOR not in snapshot.capabilities
    assert snapshot.print_state is PrintState.PRINTING
    assert (snapshot.progress, snapshot.current_layer, snapshot.total_layers) == (42, 88, 210)
    assert snapshot.remaining == 35 * 60 and snapshot.elapsed == 1500
    assert (snapshot.hotend.current, snapshot.hotend.target, snapshot.bed.target) == (219, 220, 60)
    assert snapshot.chamber.current is None
    assert snapshot.fans.model == 80 and snapshot.speed_factor == 100
    assert snapshot.lights == frozenset({LightChannel.CHAMBER})
    assert snapshot.camera is True
    assert (snapshot.model, snapshot.serial, snapshot.firmware) == ("Anycubic Kobra X", SERIAL, "1.2.8")
    unit = snapshot.filament.units[0]
    assert unit.name == "Multi-colour unit" and len(unit.slots) == 4
    assert snapshot.filament.active.slot == 0 and snapshot.filament.auto_refill is True


async def test_the_kobra_x_uses_its_own_commands(kobra_x, session) -> None:
    adapter = _adapter(kobra_x, session)
    try:
        await adapter.async_read()
        await adapter.async_send(Command.SET_HOTEND_TEMP, value=215.4)
        await adapter.async_send(Command.SET_BED_TEMP, value=65)
        await adapter.async_send(Command.SET_FAN_SPEED, value=40)
        await adapter.async_send(Command.SET_LIGHT, on=False)
        assert (await adapter.async_read()).lights == frozenset(), "the read after a command shows it"
        await adapter.async_send(Command.SET_SPEED, value=50)
        await adapter.async_send(Command.SET_AUTO_REFILL, on=False)
        with pytest.raises(ProtocolError, match="part-cooling"):
            await adapter.async_send(Command.SET_FAN_SPEED, value=40, channel="auxiliary")
    finally:
        await adapter.async_teardown()
    assert kobra_x.sent("tempature", "set") == [
        {"type": 0, "target_nozzle_temp": 215, "target_hotbed_temp": 60},
        {"type": 1, "target_nozzle_temp": 215, "target_hotbed_temp": 65},
    ]
    assert kobra_x.sent("fan", "setSpeed") == [{"fan_speed_pct": 40}]
    assert kobra_x.sent("light", "control") == [{"type": 3, "status": 0, "brightness": 0}]
    assert kobra_x.sent("print", "update") == [{"taskid": "-1", "settings": {"print_speed_mode": 1}}]
    assert kobra_x.sent("multiColorBox", "setAutoFeed") == [{"multi_color_box": [{"id": -1, "auto_feed": 0}]}]


async def test_motion_waits_for_an_idle_kobra_x(kobra_x, session) -> None:
    adapter = _adapter(kobra_x, session)
    try:
        await adapter.async_read()
        with pytest.raises(CommandBlockedError, match="not idle"):
            await adapter.async_send(Command.HOME, axes="XYZ")
        await adapter.async_send(Command.STOP)
        await adapter.async_read()
        await adapter.async_send(Command.HOME, axes="XY")
        await adapter.async_send(Command.JOG, axis="Z", distance=-2.5)
        with pytest.raises(ProtocolError, match="X and Y together"):
            await adapter.async_send(Command.HOME, axes="X")
    finally:
        await adapter.async_teardown()
    assert kobra_x.sent("print", "stop") == [{"taskid": "-1"}]
    assert kobra_x.sent("axis", "move") == [
        {"axis": 4, "move_type": 2, "distance": 0},
        {"axis": 3, "move_type": 0, "distance": 2.5},
    ]


async def test_a_kobra_3_applies_settings_only_during_a_print(session) -> None:
    printer = await _printer(model_id="20024")
    adapter = _adapter(printer, session)
    try:
        await adapter.async_read()
        assert Capability.HOME not in adapter.capabilities
        await adapter.async_send(Command.SET_BED_TEMP, value=65)
        assert printer.sent("print", "update") == [{"taskid": "-1", "settings": {"target_hotbed_temp": 65}}]
        await adapter.async_send(Command.STOP)
        snapshot = await adapter.async_read()
        assert snapshot.print_state is PrintState.IDLE
        assert snapshot.blocked[Command.SET_BED_TEMP] == kobra.JOB_SETTING
        with pytest.raises(CommandBlockedError, match="only during a print"):
            await adapter.async_send(Command.SET_HOTEND_TEMP, value=200)
        assert printer.sent("tempature", "set") == []
    finally:
        await adapter.async_teardown()
        await printer.stop()
        printer.close()


async def test_an_s1_has_a_chamber_and_a_chamber_light(session) -> None:
    printer = await _printer(model_id="20029")
    adapter = _adapter(printer, session)
    try:
        snapshot = await adapter.async_read()
        await adapter.async_send(Command.SET_LIGHT, on=True)
    finally:
        await adapter.async_teardown()
        await printer.stop()
        printer.close()
    assert Capability.CHAMBER_SENSOR in snapshot.capabilities
    assert snapshot.chamber.current == 0
    assert printer.sent("light", "control") == [{"type": 2, "status": 1, "brightness": 100}]


async def test_an_unknown_model_keeps_working_and_says_so(session) -> None:
    """It is read, but never sent the Kobra X's moves, which nobody measured on it."""
    printer = await _printer(model_id="20099")
    adapter = _adapter(printer, session)
    try:
        snapshot = await adapter.async_read()
    finally:
        await adapter.async_teardown()
        await printer.stop()
        printer.close()
    assert "unknown model 20099" in snapshot.errors
    assert snapshot.print_state is PrintState.PRINTING
    assert Capability.PAUSE in snapshot.capabilities
    assert not {Capability.HOME, Capability.JOG, Capability.CHAMBER_SENSOR} & snapshot.capabilities


async def test_a_refusal_is_reported_with_the_printers_code(kobra_x, session) -> None:
    kobra_x.refuse[("print", "pause")] = 10204
    adapter = _adapter(kobra_x, session)
    try:
        await adapter.async_read()
        with pytest.raises(CommandRejectedError) as caught:
            await adapter.async_send(Command.PAUSE)
    finally:
        await adapter.async_teardown()
    assert caught.value.code == 10204


async def test_starting_a_print_needs_the_opt_in_and_uses_the_slicers_topic(kobra_x, session) -> None:
    adapter = _adapter(kobra_x, session)
    try:
        await adapter.async_read()
        with pytest.raises(UnsafeCommandError):
            await adapter.async_send(Command.START_PRINT, filename="benchy.gcode")
    finally:
        await adapter.async_teardown()
    kobra_x.info["state"] = "free"
    adapter = _adapter(kobra_x, session, unsafe_enabled=["kobra_start_print"])
    try:
        await adapter.async_read()
        await adapter.async_send(Command.START_PRINT, filename="/benchy.gcode")
    finally:
        await adapter.async_teardown()
    (start,) = [item for item in kobra_x.received if item["action"] == "start"]
    assert start["_source"] == "slicer"
    assert start["data"] == {"taskid": "-1", "filename": "benchy.gcode", "filetype": 1}


async def test_files_are_listed_from_the_slicers_request(kobra_x, session) -> None:
    adapter = _adapter(kobra_x, session)
    try:
        files = await adapter.async_list_files()
        with pytest.raises(ProtocolError, match="not supported"):
            await adapter.async_upload_file("x.gcode", None)  # type: ignore[arg-type]
    finally:
        await adapter.async_teardown()
    assert [item.name for item in files] == ["benchy.gcode", "cube.gcode"]
    assert files[0].modified is not None and files[0].modified.year == 2024
    (request,) = [item for item in kobra_x.received if item["action"] == "listLocal"]
    assert request["_source"] == "slicer" and request["data"]["path"] == "/"


async def test_a_refused_file_list_fails_at_once(kobra_x, session, monkeypatch) -> None:
    monkeypatch.setattr(kobra, "FILE_LIST_REQUEST", {"path": "/"})
    adapter = _adapter(kobra_x, session)
    try:
        with pytest.raises(ProtocolError, match="10112"):
            await asyncio.wait_for(adapter.async_list_files(), timeout=kobra.FILE_LIST_TIMEOUT / 2)
    finally:
        await adapter.async_teardown()


async def test_the_stream_is_started_as_the_official_client_does(kobra_x, session) -> None:
    adapter = _adapter(kobra_x, session)
    try:
        await adapter.async_read()
        url = await adapter.async_stream_source()
        assert url == "http://127.0.0.1:18088/live/token1"
    finally:
        await adapter.async_teardown()
    video = [item["action"] for item in kobra_x.received if item["type"] == "video"]
    assert video == ["stopCapture", "startCapture", "stopCapture"]


async def test_cloud_mode_and_a_changed_serial_are_refused(kobra_x, session) -> None:
    kobra_x.ctrl_type = "cloud"
    adapter = _adapter(kobra_x, session)
    with pytest.raises(kobra.CloudModeError, match="LAN Mode"):
        await adapter.async_setup()
    config = parse_config({"name": "k", "protocol": "anycubic_kobra", "host": "127.0.0.1", "port": kobra_x.port})
    with pytest.raises(ConfigError, match="LAN Mode"):
        await kobra.AnycubicKobraProtocol.async_prepare_config(config)
    kobra_x.ctrl_type = "lan"
    prepared = await kobra.AnycubicKobraProtocol.async_prepare_config(config)
    assert prepared.serial == SERIAL
    other = _adapter(kobra_x, session, serial="SOMEONEELSE1")
    with pytest.raises(UnreachableError, match="SOMEONEELSE1"):
        await other.async_setup()
    assert kobra_x.handshakes == 0


async def test_a_silent_printer_is_dropped_and_a_new_session_is_made(kobra_x, session) -> None:
    adapter = _adapter(kobra_x, session)
    try:
        await adapter.async_read()
        kobra_x.silent = True
        with pytest.raises(UnreachableError, match="stopped reporting"):
            # One more read than the limit: answers to the last live round still arrive.
            for _ in range(kobra.SILENT_POLLS + 2):
                await adapter.async_read()
        kobra_x.silent = False
        await adapter.async_setup()
        snapshot = await adapter.async_read()
    finally:
        await adapter.async_teardown()
    assert snapshot.connected
    assert kobra_x.handshakes == 2, "each session gets its own credentials"


async def _until(condition, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def test_setups_at_once_open_one_session(kobra_x, session) -> None:
    """A read, a command and the camera can all find the session gone at the same moment."""
    adapter = _adapter(kobra_x, session)
    try:
        await asyncio.gather(*(adapter.async_setup() for _ in range(3)))
        assert (kobra_x.handshakes, kobra_x.sessions) == (1, 1)
    finally:
        await adapter.async_teardown()


async def test_a_teardown_waits_for_a_setup_in_flight(kobra_x, session) -> None:
    """Otherwise the setup connects after the close, and the session outlives the entry."""
    adapter = _adapter(kobra_x, session)
    setup = asyncio.create_task(adapter.async_setup())
    await asyncio.sleep(0)
    await adapter.async_teardown()
    await setup
    await _until(lambda: kobra_x.sessions == 0)


async def test_the_session_is_kept_alive_between_reads(kobra_x, session, monkeypatch) -> None:
    """Reads can be an hour apart, and a broker drops a client silent for longer than its keepalive."""
    monkeypatch.setattr(kobra, "PING_INTERVAL", 0.05)
    adapter = _adapter(kobra_x, session)
    try:
        await adapter.async_setup()
        await _until(lambda: kobra_x.pings >= 2)
    finally:
        await adapter.async_teardown()


async def test_a_new_session_never_plays_the_last_ones_stream(kobra_x, session) -> None:
    """The stream's URL carries a token of the session that started it."""
    adapter = _adapter(kobra_x, session)
    try:
        assert (await adapter.async_stream_source()).endswith("/live/token1")
        await adapter.async_teardown()
        kobra_x.refuse[("video", "startCapture")] = 10001
        assert await adapter.async_stream_source() == "http://127.0.0.1:18088/flv"
    finally:
        await adapter.async_teardown()

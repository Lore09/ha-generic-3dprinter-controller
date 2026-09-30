"""Tests for the Centauri Carbon 2 adapter.

The pure functions are tested on the payloads the printer sends, and the adapter
itself is driven against a fake printer that is its own MQTT broker, as the real
one is. The timing constants are shortened for the suite; the behaviour they gate
is the same.

Two cases are regression tests for what a live printer did or is documented to do:
a printer in cloud mode accepts the connection and then answers nothing, and a
resume is acknowledged only after the print has resumed, minutes later.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator

import aiohttp
import pytest

from custom_components.generic_3dprinter.adapters import elegoo_cc2 as cc2
from custom_components.generic_3dprinter.const import (
    Capability,
    Command,
    LightChannel,
    PrintState,
    ProtocolId,
)
from custom_components.generic_3dprinter.discovery import DiscoveryResult
from custom_components.generic_3dprinter.mqtt_client import (
    decode_publish,
    encode_length,
    encode_publish,
)
from custom_components.generic_3dprinter.protocols import (
    AuthError,
    CommandBlockedError,
    CommandRejectedError,
    ConfigError,
    PrinterConfig,
    ProtocolError,
    UnreachableError,
    UnsafeCommandError,
    command_capability,
    parse_config,
)
from custom_components.generic_3dprinter.registry import (
    ADAPTERS,
    FAMILIES,
    family_members,
    granted_capabilities,
    protocol_menu,
    withheld_features,
)
from tests.fake_cc2_printer import FULL_STATUS, SERIAL, FakeCC2Printer

#: The reply a live Centauri Carbon 2 sent to the discovery request.
LIVE_DISCOVERY_REPLY = {
    "id": 0,
    "result": {
        "host_name": "CC2 QAZJ",
        "lan_status": 0,
        "machine_model": "Centauri Carbon 2",
        "protocol_version": "1.0.0",
        "sn": "F01BXKSWL13QAZJ",
        "token_status": 0,
    },
}


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten every wait the adapter makes, keeping their order of magnitude."""
    monkeypatch.setattr(cc2, "REQUEST_GAP", 0.0)
    monkeypatch.setattr(cc2, "HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(cc2, "REGISTER_TIMEOUT", 0.3)
    monkeypatch.setattr(cc2, "ACK_TIMEOUT", 0.5)
    monkeypatch.setattr(cc2, "RESUME_REFUSAL_WINDOW", 0.2)
    monkeypatch.setattr(cc2, "DISCOVERY_TIMEOUT", 0.2)


@pytest.fixture(name="cc2_printer")
async def cc2_printer_fixture() -> AsyncIterator[FakeCC2Printer]:
    """Run a fake Centauri Carbon 2."""
    server = FakeCC2Printer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture(name="session")
async def session_fixture() -> AsyncIterator[aiohttp.ClientSession]:
    """Return an HTTP session for the adapter's camera and upload."""
    async with aiohttp.ClientSession() as session:
        yield session


def make_adapter(
    printer: FakeCC2Printer,
    session: aiohttp.ClientSession,
    *,
    granted: frozenset[Capability] | None = None,
    access_code: str | None = None,
    serial: str | None = SERIAL,
) -> cc2.ElegooCC2Protocol:
    """Return an adapter pointed at the fake, with every capability granted by default."""
    data = {
        "name": "Fake CC2",
        "protocol": "elegoo_cc2",
        "host": "127.0.0.1",
        "port": printer.port,
        "camera_port": printer.camera_port,
        "serial": serial,
    }
    if access_code is not None:
        data["access_code"] = access_code
    registration = ADAPTERS[ProtocolId.ELEGOO_CC2]
    return cc2.ElegooCC2Protocol(
        parse_config(data),
        session,
        granted=granted if granted is not None else registration.capabilities,
        unsafe=(),
    )


# ---------------------------------------------------------------- pure parsing


def test_request_body_is_the_sdk_envelope() -> None:
    body = json.loads(cc2.build_request(7, 1021, {"a": 1}))
    assert body == {"id": 7, "method": 1021, "params": {"a": 1}}
    assert json.loads(cc2.build_request(1, 1002))["params"] == {}


def test_client_id_has_the_sdk_shape() -> None:
    client_id = cc2.new_client_id()
    assert client_id.startswith("1_PC_")
    assert 1000 <= int(client_id[5:]) <= 9999


def test_deep_merge_merges_objects_and_replaces_the_rest() -> None:
    base = {"extruder": {"temperature": 200.0, "target": 220}, "led": {"status": 1}}
    merged = cc2.deep_merge(base, {"extruder": {"temperature": 219.5}, "led": {"status": 0}})
    assert merged == {"extruder": {"temperature": 219.5, "target": 220}, "led": {"status": 0}}
    assert base["extruder"]["temperature"] == 200.0, "the base must not be mutated"


def test_deep_merge_replaces_exception_codes_whole() -> None:
    base = {"exception": {"exception_code": {"109": {"time": 1}}}}
    merged = cc2.deep_merge(base, {"exception": {"exception_code": {}}})
    assert merged["exception"]["exception_code"] == {}


def test_parse_status_normalises_a_full_status() -> None:
    parsed = cc2.parse_status(FULL_STATUS)
    assert parsed["status"] == 2
    assert parsed["sub_status"] == 2075
    assert parsed["progress"] == 45
    assert parsed["filename"] == "benchy.gcode"
    assert parsed["total_layers"] == 500
    assert parsed["remaining"] == 4400
    assert parsed["hotend_current"] == pytest.approx(215.0)
    assert parsed["bed_target"] == pytest.approx(60.0)
    assert parsed["chamber_current"] == pytest.approx(33.0)
    # PWM duty, 0 to 255, not a percentage.
    assert parsed["fans"]["model"] == 100.0
    assert parsed["fans"]["auxiliary"] == 70.0
    assert parsed["fans"]["chamber"] == 10.0
    assert parsed["speed_mode"] == 1
    assert parsed["position"].x == pytest.approx(88.148)
    assert parsed["homed_axes"] == frozenset({"x", "y", "z"})
    assert parsed["light"] is True
    assert parsed["camera"] is True


def test_parse_status_reads_the_documented_field_names_too() -> None:
    """The firmware measured sends ``gcode_move`` and ``tool_head``; the docs name others."""
    parsed = cc2.parse_status(
        {"gcode_move_inf": {"x": 1, "y": 2, "z": 3}, "toolhead": {"homed_axes": "xy"}, "chamber": {"temperature": 30}}
    )
    assert parsed["position"].z == pytest.approx(3.0)
    assert parsed["homed_axes"] == frozenset({"x", "y"})
    assert parsed["chamber_current"] == pytest.approx(30.0)


def test_parse_status_tolerates_an_empty_status() -> None:
    parsed = cc2.parse_status({})
    assert parsed["status"] is None
    assert parsed["position"] is None
    assert parsed["light"] is None
    assert parsed["fans"]["model"] is None
    assert parsed["exceptions"] == ()


@pytest.mark.parametrize(
    ("status", "sub_status", "expected"),
    [
        (1, 0, PrintState.IDLE),
        (2, 2075, PrintState.PRINTING),
        (2, 1041, PrintState.PRINTING),
        (2, 9999, PrintState.PRINTING),
        (2, 1405, PrintState.PREPARING),
        (2, 2901, PrintState.PREPARING),
        (2, 2502, PrintState.PAUSED),
        (2, 2505, PrintState.PAUSED),
        (2, 2077, PrintState.FINISHED),
        (2, 2504, PrintState.CANCELLED),
        (3, 1133, PrintState.PREPARING),
        (14, 0, PrintState.ERROR),
        (0, 0, PrintState.UNKNOWN),
        (99, 0, PrintState.UNKNOWN),
        (None, None, PrintState.UNKNOWN),
    ],
)
def test_state_table(status: int | None, sub_status: int | None, expected: PrintState) -> None:
    assert cc2.state_for(status, sub_status) is expected


def test_fan_duty_round_trips_through_a_percentage() -> None:
    assert cc2.fan_duty(100) == 255
    assert cc2.fan_duty(0) == 0
    assert cc2.fan_duty(50) == 128
    assert cc2.fan_percent(255) == 100.0
    assert cc2.fan_percent(None) is None


@pytest.mark.parametrize(
    ("percent", "mode"), [(0, 0), (50, 0), (74, 0), (75, 0), (76, 1), (100, 1), (160, 2), (400, 3)]
)
def test_speed_mode_is_the_nearest_one(percent: float, mode: int) -> None:
    assert cc2.speed_mode_for(percent) == mode


def test_file_list_skips_folders_and_keeps_sizes() -> None:
    files = cc2.parse_file_list(
        {"file_list": [
            {"filename": "a.gcode", "type": "file", "size": 10, "create_time": 1706900000},
            {"filename": "models", "type": "folder"},
            {"filename": "", "type": "file"},
            {"filename": "b.gcode", "size": 5},
        ]}
    )
    assert [item.name for item in files] == ["a.gcode", "b.gcode"]
    assert files[0].size == 10
    assert files[0].modified is not None
    assert cc2.parse_file_list({}) == []


def test_the_live_discovery_reply_is_understood() -> None:
    found = cc2.parse_cc2_reply(LIVE_DISCOVERY_REPLY, "192.168.128.146")
    assert found is not None
    assert found.protocol is ProtocolId.ELEGOO_CC2
    assert found.host == "192.168.128.146"
    assert found.mainboard_id == "F01BXKSWL13QAZJ"
    assert found.model == "Centauri Carbon 2"
    assert found.lan_only is False
    assert found.access_code_set is False
    assert cc2.parse_cc2_reply({"result": {}}, "1.2.3.4") is None
    assert cc2.parse_cc2_reply({"Data": {}}, "1.2.3.4") is None


def test_the_hint_names_cloud_mode_when_discovery_saw_it() -> None:
    cloud = DiscoveryResult(host="h", protocol=ProtocolId.ELEGOO_CC2, lan_only=False)
    assert "cloud mode" in cc2.lan_only_hint(cloud)
    assert "LAN Only Mode" in cc2.lan_only_hint(None)


# ----------------------------------------------------------------- mqtt codec


@pytest.mark.parametrize(
    ("length", "encoded"),
    [(0, b"\x00"), (127, b"\x7f"), (128, b"\x80\x01"), (16383, b"\xff\x7f"), (16384, b"\x80\x80\x01")],
)
def test_remaining_length_encoding(length: int, encoded: bytes) -> None:
    assert encode_length(length) == encoded


def test_publish_round_trips() -> None:
    frame = encode_publish("elegoo/x/api_status", b'{"a":1}')
    # Skip the fixed header: one type byte and a one-byte length.
    topic, payload, qos, packet_id = decode_publish(frame[0] & 0x0F, frame[2:])
    assert (topic, payload, qos, packet_id) == ("elegoo/x/api_status", b'{"a":1}', 0, None)


# ------------------------------------------------------------------ registry


def test_the_centauri_family_is_one_menu_entry() -> None:
    menu = dict(protocol_menu())
    assert menu["elegoo_centauri"] == FAMILIES["elegoo_centauri"]
    assert "sdcp_cc1" not in menu
    assert "elegoo_cc2" not in menu
    assert [item.id for item in family_members("elegoo_centauri")] == [
        ProtocolId.SDCP_CC1,
        ProtocolId.ELEGOO_CC2,
    ]
    assert [item.model for item in family_members("elegoo_centauri")] == [
        "Centauri Carbon",
        "Centauri Carbon 2",
    ]


def test_cc2_declares_only_what_has_a_source() -> None:
    capabilities = ADAPTERS[ProtocolId.ELEGOO_CC2].capabilities
    for absent in (
        Capability.SET_CHAMBER_TEMP,
        Capability.FILE_DELETE,
        Capability.WEB_UI,
        Capability.SET_FLOW,
    ):
        assert absent not in capabilities
    assert {Capability.CHAMBER_SENSOR, Capability.HOME, Capability.JOG} <= capabilities


def test_cc2_start_print_is_withheld_until_opted_in() -> None:
    registration = ADAPTERS[ProtocolId.ELEGOO_CC2]
    assert Capability.START_PRINT not in granted_capabilities(registration, frozenset())
    assert Capability.START_PRINT in granted_capabilities(
        registration, frozenset({"cc2_start_print"})
    )
    assert [item.id for item in withheld_features(registration, frozenset())] == ["cc2_start_print"]


def test_every_granted_cc2_command_has_a_handler() -> None:
    capabilities = ADAPTERS[ProtocolId.ELEGOO_CC2].capabilities
    for command in Command:
        if command_capability(command) in capabilities:
            assert command in cc2._DISPATCH, command  # noqa: SLF001


def test_a_serial_that_could_escape_its_topic_is_refused() -> None:
    base = {"name": "p", "protocol": "elegoo_cc2", "host": "127.0.0.1"}
    assert parse_config({**base, "serial": " F01BXKSWL13QAZJ "}).serial == "F01BXKSWL13QAZJ"
    assert parse_config(base).serial is None
    for bad in ("abc/def", "abc#", "a+b", "x" * 65):
        with pytest.raises(ConfigError):
            parse_config({**base, "serial": bad})


# -------------------------------------------------------- against the printer


async def test_setup_registers_and_reads_the_printer(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        assert cc2_printer.registered == [adapter.client_id]
        assert cc2_printer.methods[:2] == [1001, 1002]

        snapshot = await adapter.async_read()
        assert snapshot.protocol is ProtocolId.ELEGOO_CC2
        assert snapshot.connected is True
        assert snapshot.print_state is PrintState.PRINTING
        assert snapshot.progress == 45
        assert snapshot.current_layer == 225
        assert snapshot.total_layers == 500
        assert snapshot.remaining == 4400
        assert snapshot.elapsed == 3600
        assert snapshot.filename == "benchy.gcode"
        assert snapshot.hotend.current == pytest.approx(215.0)
        assert snapshot.hotend.target == pytest.approx(220.0)
        assert snapshot.chamber.current == pytest.approx(33.0)
        assert snapshot.chamber.target is None
        assert snapshot.fans.model == 100.0
        assert snapshot.fans.hotend == 100.0
        assert snapshot.speed_factor == 100.0
        assert snapshot.lights == frozenset({LightChannel.CHAMBER})
        assert snapshot.camera is True
        assert snapshot.model == "Centauri Carbon 2"
        assert snapshot.firmware == "02.01.00.00"
        assert snapshot.serial == SERIAL
        # A read inside the refresh interval uses the pushed state, not a request.
        assert cc2_printer.methods.count(1002) == 1
    finally:
        await adapter.async_teardown()


async def test_pushed_deltas_are_merged_into_the_snapshot(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await cc2_printer.push_delta({"extruder": {"temperature": 219.5}, "led": {"status": 0}})
        await cc2_printer.push_delta({"machine_status": {"sub_status": 2502}})
        await asyncio.sleep(0.05)
        snapshot = await adapter.async_read()
        assert snapshot.hotend.current == pytest.approx(219.5)
        assert snapshot.hotend.target == pytest.approx(220.0), "an unchanged field was lost"
        assert snapshot.lights == frozenset()
        assert snapshot.print_state is PrintState.PAUSED
    finally:
        await adapter.async_teardown()


async def test_job_fields_are_cleared_once_the_printer_is_idle(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await cc2_printer.push_delta({"machine_status": {"status": 1, "sub_status": 0}})
        await asyncio.sleep(0.05)
        snapshot = await adapter.async_read()
        assert snapshot.print_state is PrintState.IDLE
        assert snapshot.filename is None
        assert snapshot.progress is None
        assert snapshot.total_layers is None
    finally:
        await adapter.async_teardown()


async def test_lost_deltas_trigger_a_full_status(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        for sequence in (1, 3, 5, 7, 9, 11):
            await cc2_printer.push_delta({}, sequence=sequence)
        await asyncio.sleep(0.05)
        await adapter.async_read()
        assert cc2_printer.methods.count(1002) == 2
    finally:
        await adapter.async_teardown()


async def test_commands_carry_the_documented_payloads(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await adapter.async_send(Command.PAUSE)
        await adapter.async_send(Command.STOP)
        await adapter.async_send(Command.SET_HOTEND_TEMP, value=215.4)
        await adapter.async_send(Command.SET_BED_TEMP, value=60)
        await adapter.async_send(Command.SET_FAN_SPEED, value=50, channel="chamber")
        await adapter.async_send(Command.SET_FAN_SPEED, value=100)
        await adapter.async_send(Command.SET_SPEED, value=50)
        await adapter.async_send(Command.SET_LIGHT, on=False)

        assert cc2_printer.params_of(1021) == [{}]
        assert cc2_printer.params_of(1022) == [{}]
        assert cc2_printer.params_of(1028) == [{"extruder": 215}, {"heater_bed": 60}]
        assert cc2_printer.params_of(1030) == [{"box_fan": 128}, {"fan": 255}]
        assert cc2_printer.params_of(1031) == [{"mode": 0}]
        assert cc2_printer.params_of(1029) == [{"power": 0}]
    finally:
        await adapter.async_teardown()


async def test_motion_is_refused_unless_the_printer_is_idle(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        # Mid-print: neither a home nor a jog may reach the printer.
        with pytest.raises(ProtocolError):
            await adapter.async_send(Command.HOME)
        with pytest.raises(ProtocolError):
            await adapter.async_send(Command.JOG, axis="X", distance=10)
        assert not {1026, 1027} & set(cc2_printer.methods)

        await cc2_printer.push_delta({"machine_status": {"status": 1, "sub_status": 0}})
        await asyncio.sleep(0.05)
        await adapter.async_send(Command.HOME, axes="XY")
        await adapter.async_send(Command.JOG, axis="Z", distance=-0.1)
        assert cc2_printer.params_of(1026) == [{"homed_axes": "xy"}]
        assert cc2_printer.params_of(1027) == [{"axes": "z", "distance": -0.1}]
    finally:
        await adapter.async_teardown()


async def test_a_home_acknowledged_only_when_done_does_not_time_out(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    cc2_printer.silent.add(1026)
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await cc2_printer.push_delta({"machine_status": {"status": 1, "sub_status": 0}})
        await asyncio.sleep(0.05)
        await asyncio.wait_for(adapter.async_send(Command.HOME), timeout=2)
        assert cc2_printer.params_of(1026) == [{"homed_axes": "xyz"}]
    finally:
        await adapter.async_teardown()


async def test_the_speed_mode_is_refused_outside_a_print(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    """Measured on firmware 02.01.00.00: an idle printer answers 1031 with error 1010."""
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await cc2_printer.push_delta({"machine_status": {"status": 1, "sub_status": 0}})
        await asyncio.sleep(0.05)
        with pytest.raises(CommandRejectedError) as caught:
            await adapter.async_send(Command.SET_SPEED, value=50)
        assert caught.value.code == 1010
        assert "no print is in progress" in str(caught.value)
    finally:
        await adapter.async_teardown()


async def test_a_fan_the_printer_runs_itself_is_not_set(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        with pytest.raises(ProtocolError):
            await adapter.async_send(Command.SET_FAN_SPEED, value=50, channel="hotend")
        assert 1030 not in cc2_printer.methods
    finally:
        await adapter.async_teardown()


async def test_a_refusal_carries_the_printers_reason(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    cc2_printer.refuse[1021] = 1010
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        with pytest.raises(CommandRejectedError) as caught:
            await adapter.async_send(Command.PAUSE)
        assert caught.value.code == 1010
        assert "no print is in progress" in str(caught.value)
    finally:
        await adapter.async_teardown()


async def test_resume_does_not_wait_for_the_late_acknowledgement(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        # The fake never acknowledges a resume, as the printer does not for minutes.
        await asyncio.wait_for(adapter.async_send(Command.RESUME), timeout=2)
        assert 1023 in cc2_printer.methods

        # A refusal still comes back at once and is reported.
        cc2_printer.silent.discard(1023)
        cc2_printer.refuse[1023] = 1010
        with pytest.raises(CommandRejectedError):
            await adapter.async_send(Command.RESUME)
    finally:
        await adapter.async_teardown()


async def test_an_unanswered_command_is_reported(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    cc2_printer.silent.add(1021)
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        with pytest.raises(ProtocolError) as caught:
            await adapter.async_send(Command.PAUSE)
        assert "did not answer" in str(caught.value)
    finally:
        await adapter.async_teardown()


async def test_start_print_is_refused_without_the_opt_in(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    registration = ADAPTERS[ProtocolId.ELEGOO_CC2]
    adapter = cc2.ElegooCC2Protocol(
        make_adapter(cc2_printer, session).config,
        session,
        granted=granted_capabilities(registration, frozenset()),
        unsafe=withheld_features(registration, frozenset()),
    )
    try:
        await adapter.async_setup()
        with pytest.raises(UnsafeCommandError):
            await adapter.async_send(Command.START_PRINT, filename="benchy.gcode")
        assert 1020 not in cc2_printer.methods
    finally:
        await adapter.async_teardown()


async def test_start_print_sends_levelling_and_an_empty_tray_map(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        # The printer is mid-print, so a start is refused before it is sent.
        with pytest.raises(ProtocolError):
            await adapter.async_send(Command.START_PRINT, filename="benchy.gcode")
        assert 1020 not in cc2_printer.methods

        await cc2_printer.push_delta({"machine_status": {"status": 1, "sub_status": 0}})
        await asyncio.sleep(0.05)
        await adapter.async_send(Command.START_PRINT, filename="/cube.gcode")
        assert cc2_printer.params_of(1020) == [
            {
                "storage_media": "local",
                "filename": "cube.gcode",
                "config": {"printer_check": True, "slot_map": []},
            }
        ]
    finally:
        await adapter.async_teardown()


async def test_a_move_is_refused_once_a_print_starts_between_polls(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    """The last poll said idle; a print started since must still stop the move."""
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await cc2_printer.push_delta({"machine_status": {"status": 1, "sub_status": 0}})
        await asyncio.sleep(0.05)
        assert (await adapter.async_read()).print_state is PrintState.IDLE
        await cc2_printer.push_delta({"machine_status": {"status": 2, "sub_status": 2075}})
        await asyncio.sleep(0.05)
        sent = list(cc2_printer.methods)
        with pytest.raises(CommandBlockedError):
            await adapter.async_send(Command.JOG, axis="X", distance=10)
        assert cc2_printer.methods == sent
    finally:
        await adapter.async_teardown()


async def test_the_file_list_is_read(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        files = await adapter.async_list_files()
        assert [item.name for item in files] == ["benchy.gcode", "cube.gcode"]
        assert cc2_printer.params_of(1044) == [{"storage_media": "local", "path": "/"}]
    finally:
        await adapter.async_teardown()


async def test_the_heartbeat_runs(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        await asyncio.sleep(0.3)
        assert cc2_printer.pings >= 3
    finally:
        await adapter.async_teardown()


async def test_a_silent_printer_is_dropped_after_the_heartbeat_timeout(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cc2, "HEARTBEAT_TIMEOUT", 0.2)
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        cc2_printer.answering = False
        await asyncio.sleep(0.6)
        assert adapter._connected is False  # noqa: SLF001
    finally:
        await adapter.async_teardown()


async def test_the_printer_goes_away_and_comes_back(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        assert (await adapter.async_read()).connected is True

        await cc2_printer.stop()
        await asyncio.sleep(0.1)
        with pytest.raises(UnreachableError):
            await adapter.async_read()

        await cc2_printer.start()
        snapshot = await adapter.async_read()
        assert snapshot.connected is True
        assert cc2_printer.registered == [adapter.client_id]
    finally:
        await adapter.async_teardown()


async def test_cloud_mode_is_named_instead_of_timing_out_silently(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cloud(host: str | None = None, timeout: float = 0) -> DiscoveryResult:
        return cc2.parse_cc2_reply(LIVE_DISCOVERY_REPLY, "127.0.0.1")  # type: ignore[return-value]

    monkeypatch.setattr(cc2, "async_discover_cc2", cloud)
    cc2_printer.answering = False
    adapter = make_adapter(cc2_printer, session)
    try:
        with pytest.raises(UnreachableError) as caught:
            await adapter.async_setup()
        assert "cloud mode" in str(caught.value)
        assert "LAN Only Mode" in str(caught.value)
        assert adapter._client is None  # noqa: SLF001
    finally:
        await adapter.async_teardown()


async def test_a_wrong_access_code_is_an_auth_error(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session, access_code="000000")
    with pytest.raises(AuthError):
        await adapter.async_setup()
    await adapter.async_teardown()


async def test_the_access_code_is_the_broker_password(
    session: aiohttp.ClientSession,
) -> None:
    printer = FakeCC2Printer(access_code="424242")
    await printer.start()
    adapter = make_adapter(printer, session, access_code="424242")
    try:
        await adapter.async_setup()
        assert printer.registered == [adapter.client_id]
    finally:
        await adapter.async_teardown()
        await printer.stop()


async def test_no_free_client_slot_is_explained(session: aiohttp.ClientSession) -> None:
    printer = FakeCC2Printer(max_clients=0)
    await printer.start()
    adapter = make_adapter(printer, session)
    try:
        with pytest.raises(UnreachableError) as caught:
            await adapter.async_setup()
        assert "no free client slot" in str(caught.value)
    finally:
        await adapter.async_teardown()
        await printer.stop()


async def test_the_serial_is_discovered_when_not_configured(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def lan(host: str | None = None, timeout: float = 0) -> DiscoveryResult:
        reply = {"id": 0, "result": {**LIVE_DISCOVERY_REPLY["result"], "lan_status": 1}}
        return cc2.parse_cc2_reply(reply, "127.0.0.1")  # type: ignore[return-value]

    monkeypatch.setattr(cc2, "async_discover_cc2", lan)
    adapter = make_adapter(cc2_printer, session, serial=None)
    try:
        await adapter.async_setup()
        assert adapter.serial == SERIAL
    finally:
        await adapter.async_teardown()


async def test_requests_are_spaced_out(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cc2, "REQUEST_GAP", 0.15)
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.gather(*(adapter.async_send(Command.PAUSE) for _ in range(3)))
        # Three requests after the setup's own: at least two full gaps between them.
        assert loop.time() - started >= 0.3
    finally:
        await adapter.async_teardown()


async def test_upload_sends_ranged_chunks_on_one_connection(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cc2, "UPLOAD_PORT", cc2_printer.upload_port)
    monkeypatch.setattr(cc2, "UPLOAD_CHUNK", 4)
    adapter = make_adapter(cc2_printer, session)
    data = b"0123456789"

    async def stream() -> AsyncIterator[bytes]:
        yield data[:3]
        yield data[3:]

    entry = await adapter.async_upload_file("dir/part.gcode", stream(), size=len(data))
    assert entry.name == "part.gcode"
    assert entry.size == 10
    ranges = [item["headers"]["Content-Range"] for item in cc2_printer.uploads]
    assert ranges == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]
    assert b"".join(item["body"] for item in cc2_printer.uploads) == data
    digest = hashlib.md5(data).hexdigest()  # noqa: S324
    for item in cc2_printer.uploads:
        assert item["headers"]["X-File-MD5"] == digest
        assert item["headers"]["X-File-Name"] == "part.gcode"
        assert item["headers"]["X-Token"] == "123456"
    # A fresh connection per chunk is what the printer answers with HTTP 429.
    assert len({item["peer"] for item in cc2_printer.uploads}) == 1


async def test_an_empty_upload_is_refused(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)

    async def empty() -> AsyncIterator[bytes]:
        if False:
            yield b""

    with pytest.raises(ProtocolError):
        await adapter.async_upload_file("part.gcode", empty())
    assert cc2_printer.uploads == []


async def test_the_camera_is_enabled_once_and_streams(
    cc2_printer: FakeCC2Printer, session: aiohttp.ClientSession
) -> None:
    adapter = make_adapter(cc2_printer, session)
    try:
        await adapter.async_setup()
        frame = await adapter.async_camera_frame()
        assert frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9")

        frames = []
        async for item in adapter.async_camera_stream():
            frames.append(item)
            if len(frames) == 3:
                break
        assert len(set(frames)) == 3, "the stream repeated a frame"
        assert cc2_printer.params_of(1042) == [{"enable": 1}]
    finally:
        await adapter.async_teardown()


# -------------------------------------------------------------- config checks


def _config(**extra: object) -> PrinterConfig:
    return parse_config({"name": "p", "protocol": "elegoo_cc2", "host": "127.0.0.1", **extra})


def _discovered(**result: object):
    async def fake(host: str | None = None, timeout: float = 0) -> DiscoveryResult | None:
        reply = {"id": 0, "result": {**LIVE_DISCOVERY_REPLY["result"], **result}}
        return cc2.parse_cc2_reply(reply, host or "")

    return fake


async def test_prepare_fills_in_the_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc2, "async_discover_cc2", _discovered(lan_status=1))
    prepared = await cc2.ElegooCC2Protocol.async_prepare_config(_config())
    assert prepared.serial == "F01BXKSWL13QAZJ"


async def test_prepare_refuses_cloud_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc2, "async_discover_cc2", _discovered(lan_status=0))
    with pytest.raises(ConfigError) as caught:
        await cc2.ElegooCC2Protocol.async_prepare_config(_config())
    assert "LAN Only Mode" in str(caught.value)


async def test_prepare_asks_for_the_access_code_when_one_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cc2, "async_discover_cc2", _discovered(lan_status=1, token_status=1))
    with pytest.raises(ConfigError):
        await cc2.ElegooCC2Protocol.async_prepare_config(_config())
    prepared = await cc2.ElegooCC2Protocol.async_prepare_config(_config(access_code="1234"))
    assert prepared.credentials["access_code"] == "1234"


async def test_prepare_catches_a_serial_that_belongs_to_another_printer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cc2, "async_discover_cc2", _discovered(lan_status=1))
    with pytest.raises(ConfigError):
        await cc2.ElegooCC2Protocol.async_prepare_config(_config(serial="OTHER123"))


async def test_prepare_accepts_a_silent_printer_only_with_a_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def silent(host: str | None = None, timeout: float = 0) -> None:
        return None

    monkeypatch.setattr(cc2, "async_discover_cc2", silent)
    with pytest.raises(ConfigError):
        await cc2.ElegooCC2Protocol.async_prepare_config(_config())
    prepared = await cc2.ElegooCC2Protocol.async_prepare_config(_config(serial=SERIAL))
    assert prepared.serial == SERIAL


async def test_discovery_reads_a_reply_over_udp(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real socket path, against a loopback responder on a free port."""
    loop = asyncio.get_running_loop()
    received: list[bytes] = []

    class Responder(asyncio.DatagramProtocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            self.transport = transport

        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            received.append(data)
            reply = {"id": 0, "result": {**LIVE_DISCOVERY_REPLY["result"], "lan_status": 1}}
            self.transport.sendto(json.dumps(reply).encode(), addr)  # type: ignore[attr-defined]

    transport, _ = await loop.create_datagram_endpoint(Responder, local_addr=("127.0.0.1", 0))
    try:
        monkeypatch.setattr(cc2, "CC2_DISCOVERY_PORT", transport.get_extra_info("sockname")[1])
        found = await cc2.async_discover_cc2("127.0.0.1", timeout=2)
    finally:
        transport.close()
    assert json.loads(received[0]) == {"id": 0, "method": 7000}
    assert found is not None
    assert found.host == "127.0.0.1"
    assert found.lan_only is True

"""Tests for the SDCP adapter's pure parsing and dispatch.

The live printer cannot be part of an automated suite, so what is tested here is
everything the adapter does without a socket: the frame envelope, the status
mapping, the file list, the JPEG framing, the print-state table, and the guard that
stops a gated command reaching hardware.

The nested-envelope case is a regression test. The SDCP response nests the body at
``Data.Data`` while the routing fields sit at ``Data``, and reading the body off the
wrong level makes a file list look empty even though the frame arrived.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

from generic_3dprinter.adapters import sdcp  # noqa: E402
from generic_3dprinter.const import Capability, Command, PrintState, ProtocolId  # noqa: E402

MAINBOARD = "5c441dd30105041800009c0000000000"


def response_frame(body: dict, request_id: str = "abc123") -> str:
    """Build a response frame in the printer's real nested shape."""
    return json.dumps(
        {
            "Id": "979d4C788A4a78bC777A870F1A02867A",
            "Data": {
                "Cmd": 258,
                "Data": body,
                "RequestID": request_id,
                "MainboardID": MAINBOARD,
                "TimeStamp": 1789656234,
            },
            "Topic": f"sdcp/response/{MAINBOARD}",
        }
    )


def test_build_frame_envelope() -> None:
    request_id, frame = sdcp.build_frame(MAINBOARD, 0, {})
    payload = json.loads(frame)
    assert payload["Id"] == ""
    assert payload["Data"]["Cmd"] == 0
    assert payload["Data"]["RequestID"] == request_id
    assert payload["Data"]["MainboardID"] == MAINBOARD
    assert payload["Data"]["From"] == 1
    assert isinstance(payload["Data"]["TimeStamp"], int)
    # Milliseconds, not seconds: seconds would be ten digits, not thirteen.
    assert payload["Data"]["TimeStamp"] > 10**12


def test_build_frame_mints_a_fresh_request_id_each_time() -> None:
    first, _ = sdcp.build_frame(MAINBOARD, 0)
    second, _ = sdcp.build_frame(MAINBOARD, 0)
    assert first != second


def test_load_frame_strips_a_decimal_length_prefix() -> None:
    raw = '123{"Topic": "sdcp/status/x", "Status": {}}'
    assert sdcp.load_frame(raw) == {"Topic": "sdcp/status/x", "Status": {}}


def test_load_frame_rejects_non_json() -> None:
    assert sdcp.load_frame("not json at all") is None


def test_status_flags_accepts_a_list_and_a_scalar() -> None:
    assert sdcp.status_flags([1]) == (1,)
    assert sdcp.status_flags([1, 8]) == (1, 8)
    assert sdcp.status_flags(1) == (1,)
    assert sdcp.status_flags(None) == ()


def test_parse_status_normalises_the_live_payload() -> None:
    parsed = sdcp.parse_status(
        {
            "CurrentStatus": [1],
            "TempOfNozzle": 219.86,
            "TempTargetNozzle": 220,
            "TempOfHotbed": 54.99,
            "TempTargetHotbed": 55,
            "TempOfBox": 32.69,
            "TempTargetBox": 0,
            "CurrenCoord": "101.10,77.83,22.45",
            "CurrentFanSpeed": {"ModelFan": 100, "AuxiliaryFan": 69, "BoxFan": 68},
            "LightStatus": {"SecondLight": 1, "RgbLight": [0, 0, 0]},
            "PrintInfo": {
                "Status": 13,
                "CurrentLayer": 107,
                "TotalLayer": 627,
                "CurrentTicks": 2532.16,
                "TotalTicks": 18574,
                "Filename": "part.gcode",
                "TaskId": "3d315103",
                "PrintSpeedPct": 100,
                "Progress": 12,
            },
        }
    )
    assert parsed["hotend_current"] == pytest.approx(219.86)
    assert parsed["bed_target"] == pytest.approx(55.0)
    assert parsed["chamber_current"] == pytest.approx(32.69)
    assert parsed["fan_model"] == pytest.approx(100.0)
    assert parsed["elapsed"] == pytest.approx(2532.16)
    assert parsed["total_ticks"] == pytest.approx(18574.0)
    assert parsed["filename"] == "part.gcode"
    assert parsed["position"] is not None
    assert parsed["position"].x == pytest.approx(101.10)
    assert parsed["position"].z == pytest.approx(22.45)


def test_parse_status_tolerates_a_missing_print_info() -> None:
    parsed = sdcp.parse_status({"CurrentStatus": [1]})
    assert parsed["print_status"] is None
    assert parsed["filename"] is None
    assert parsed["progress"] is None
    assert parsed["position"] is None


def test_parse_coord_rejects_a_malformed_string() -> None:
    assert sdcp.parse_coord("1,2") is None
    assert sdcp.parse_coord("1,2,three") is None
    assert sdcp.parse_coord(None) is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, PrintState.IDLE),
        (13, PrintState.PRINTING),
        (6, PrintState.PAUSED),
        (9, PrintState.FINISHED),
        (14, PrintState.ERROR),
    ],
)
def test_state_table_covers_the_documented_codes(code: int, expected: PrintState) -> None:
    assert sdcp.state_for(code, ()) is expected


def test_state_falls_back_to_the_current_status_flags() -> None:
    assert sdcp.state_for(None, (1,)) is PrintState.PRINTING
    assert sdcp.state_for(99, (1,)) is PrintState.PRINTING
    assert sdcp.state_for(99, ()) is PrintState.UNKNOWN


def test_parse_file_list_skips_folders_and_keeps_sizes() -> None:
    entries = sdcp.parse_file_list(
        [
            {"name": "/local", "type": 0},
            {"name": "/local/part.gcode", "type": 1, "FileSize": 1234},
            {"name": "", "type": 1},
            {"name": "/local/other.gcode", "type": 1, "FileSize": 42},
        ]
    )
    assert [item.name for item in entries] == ["/local/part.gcode", "/local/other.gcode"]
    assert entries[0].size == 1234
    assert entries[0].display_name == "part.gcode"


def test_parse_file_list_rejects_a_non_sequence() -> None:
    assert sdcp.parse_file_list(None) == []
    assert sdcp.parse_file_list("nope") == []


def test_jpeg_frames_extracts_complete_frames_only() -> None:
    first = b"\xff\xd8" + b"a" * 10 + b"\xff\xd9"
    second = b"\xff\xd8" + b"b" * 4 + b"\xff\xd9"
    buffer = bytearray(b"--foo\r\n" + first + b"\r\n--foo\r\n" + second + b"\r\n--foo\r\n\xff\xd8partial")
    frames = sdcp.jpeg_frames(buffer)
    assert frames == [first, second]
    # The incomplete trailing frame is held back for the next read.
    assert bytes(buffer) == b"\xff\xd8partial"


def test_jpeg_frames_drops_data_before_the_first_marker() -> None:
    buffer = bytearray(b"garbage header bytes" + b"\xff\xd8x\xff\xd9")
    assert len(sdcp.jpeg_frames(buffer)) == 1


def test_jpeg_frames_first_only_stops_at_one() -> None:
    buffer = bytearray(b"\xff\xd8a\xff\xd9\xff\xd8b\xff\xd9")
    frames = sdcp.jpeg_frames(buffer, first_only=True)
    assert len(frames) == 1


# --------------------------------------------------------------------- guards


class _Session:
    """Stands in for the aiohttp session; no request may reach it in these tests."""

    def __init__(self) -> None:
        self.requests = 0

    def get(self, *args: object, **kwargs: object) -> object:
        self.requests += 1
        raise AssertionError("no request should be made")

    def post(self, *args: object, **kwargs: object) -> object:
        self.requests += 1
        raise AssertionError("no request should be made")


def _adapter(granted: frozenset[Capability], unsafe: tuple = ()) -> sdcp.SdcpProtocol:
    from generic_3dprinter.protocols import PrinterConfig

    config = PrinterConfig(
        name="test",
        protocol=ProtocolId.SDCP_CC1,
        host="127.0.0.1",
        port=3030,
    )
    return sdcp.SdcpProtocol(config, _Session(), granted=granted, unsafe=unsafe)  # type: ignore[arg-type]


def test_unsupported_command_is_refused_before_dispatch() -> None:
    from generic_3dprinter.protocols import UnsupportedCommandError

    adapter = _adapter(frozenset({Capability.PAUSE}))
    with pytest.raises(UnsupportedCommandError):
        asyncio.run(adapter.async_send(Command.HOME))


def test_gated_command_reports_the_declared_hazard() -> None:
    from generic_3dprinter.const import UnsafeFeature
    from generic_3dprinter.protocols import UnsafeCommandError

    feature = UnsafeFeature(
        id="sdcp_start_print",
        label="Allow starting a print",
        reason="an unexpected payload shape can crash the printer daemon",
        gates=frozenset({Capability.START_PRINT}),
        evidence="test",
    )
    adapter = _adapter(frozenset({Capability.PAUSE}), unsafe=(feature,))
    with pytest.raises(UnsafeCommandError) as caught:
        asyncio.run(adapter.async_send(Command.START_PRINT, filename="part.gcode"))
    assert "crash the printer daemon" in str(caught.value)


def test_a_bad_parameter_is_refused_before_dispatch() -> None:
    from generic_3dprinter.protocols import ProtocolError

    adapter = _adapter(frozenset({Capability.SET_HOTEND_TEMP}))
    with pytest.raises(ProtocolError) as caught:
        asyncio.run(adapter.async_send(Command.SET_HOTEND_TEMP, value=900))
    assert "between 0 and 350" in str(caught.value)


def test_dispatch_table_covers_every_granted_command() -> None:
    granted = frozenset(
        {
            Capability.PAUSE,
            Capability.RESUME,
            Capability.STOP,
            Capability.SET_FAN_SPEED,
            Capability.SET_SPEED,
            Capability.SET_LIGHT,
            Capability.SET_HOTEND_TEMP,
            Capability.SET_BED_TEMP,
            Capability.SET_CHAMBER_TEMP,
            Capability.FILE_DELETE,
        }
    )
    adapter = _adapter(granted)
    for command in Command:
        if command not in sdcp._DISPATCH:  # noqa: SLF001
            from generic_3dprinter.protocols import command_capability

            assert command_capability(command) not in granted, (
                f"{command.value} is granted but has no dispatch handler"
            )


def test_response_body_is_read_from_the_nested_level() -> None:
    """The routing fields sit at ``Data``; the body sits at ``Data.Data``.

    Reading the body from the shallow level resolves the request with the routing
    envelope instead, which is how a file list comes back empty while the frame
    that carried it was received and parsed.
    """

    async def scenario() -> object:
        adapter = _adapter(frozenset({Capability.FILE_LIST}))
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        adapter._pending["abc123"] = future  # noqa: SLF001
        adapter._handle_frame(  # noqa: SLF001
            response_frame({"Ack": 0, "FileList": [{"name": "/local/a.gcode", "type": 1}]})
        )
        return await future

    body = asyncio.run(scenario())
    assert body == {"Ack": 0, "FileList": [{"name": "/local/a.gcode", "type": 1}]}


def test_file_list_frame_populates_the_adapter_buffer() -> None:
    async def scenario() -> list:
        adapter = _adapter(frozenset({Capability.FILE_LIST}))
        adapter._handle_frame(  # noqa: SLF001
            response_frame({"Ack": 0, "FileList": [{"name": "/local/a.gcode", "type": 1}]})
        )
        return adapter._file_list  # noqa: SLF001

    entries = asyncio.run(scenario())
    assert [item.name for item in entries] == ["/local/a.gcode"]


@pytest.mark.parametrize(("second_light", "expected"), [(1, True), (0, False)])
def test_the_light_is_reported_on_only_when_it_is_on(second_light: int, expected: bool) -> None:
    """A light the printer reports as off must not read as on, or a toggle only ever turns it off."""
    from generic_3dprinter.const import LightChannel

    async def scenario() -> bool:
        adapter = _adapter(frozenset({Capability.SET_LIGHT}))
        adapter._status = {"LightStatus": {"SecondLight": second_light}}  # noqa: SLF001
        adapter._reader = asyncio.get_running_loop().create_future()  # noqa: SLF001 - a live reader
        adapter._ws = type("Ws", (), {"closed": False})()  # noqa: SLF001
        adapter._identity_checked = True  # noqa: SLF001
        snapshot = await adapter.async_read()
        return LightChannel.CHAMBER in snapshot.lights

    assert asyncio.run(scenario()) is expected


def test_a_frame_without_a_request_id_does_not_raise() -> None:
    async def scenario() -> None:
        adapter = _adapter(frozenset({Capability.FILE_LIST}))
        frame = json.dumps(
            {
                "Data": {"Cmd": 258, "Data": {"Ack": 0}, "MainboardID": MAINBOARD},
                "Topic": f"sdcp/response/{MAINBOARD}",
            }
        )
        adapter._handle_frame(frame)  # noqa: SLF001

    asyncio.run(scenario())


# ------------------------------------------------------------ shared session


def test_sdcp_session_has_no_fdm_surface() -> None:
    """A printer driven by the bare session cannot be sent an FDM-only command."""
    session = sdcp.SdcpSession
    for name in ("set_printer_params", "_async_read_canvas", "camera_url", "web_ui_url"):
        assert not hasattr(session, name), name
    assert not [name for name in dir(session) if name.startswith("_async_set_")]
    assert set(session.commands.values()) == {0, 1, 258}
    assert not {324, 403, 386, 128} & set(session.commands.values())


def test_the_centauri_keeps_every_hook_as_it_was() -> None:
    """The split changes nothing on the wire for the Centauri Carbon."""
    adapter = _adapter(frozenset({Capability.FILE_UPLOAD}))
    assert isinstance(adapter, sdcp.SdcpSession)
    assert adapter.commands is sdcp.COMMAND
    assert adapter.ack_messages is sdcp.ACK_MESSAGES
    assert adapter.upload_url == "http://127.0.0.1/uploadFile/upload"
    assert adapter._heartbeat_payload() == "ping"  # noqa: SLF001
    request_id, frame = adapter._frame(0, {})  # noqa: SLF001
    assert json.loads(frame)["Data"]["RequestID"] == request_id
    assert json.loads(frame)["Data"]["MainboardID"] == ""

"""The Anycubic Kobra adapter's pure parts: handshake crypto, state and reports.

Shapes are the ones the sources captured: chrisfore/anycubic_ha_local on a Kobra
S1 Max and a Kobra X, stribor/anycubic_kobrax on a Kobra X.
"""

from __future__ import annotations

import pytest

from custom_components.generic_3dprinter.adapters import anycubic_kobra as kobra
from custom_components.generic_3dprinter.const import PrintState
from custom_components.generic_3dprinter.protocols import ProtocolShapeError

TOKEN = "0123456789abcdefFEDCBA9876543210"
LOCAL = "localtoken123456"


def test_the_signature_is_the_double_md5_the_printer_checks() -> None:
    # md5("0123456789abcdef") = 4032af8d61035123906e58e067140cc5
    assert kobra.sign(TOKEN, 1700000000000, "aB3xY9") == __import__("hashlib").md5(
        b"4032af8d61035123906e58e067140cc51700000000000aB3xY9"
    ).hexdigest()


def test_the_bundle_round_trips_through_the_printers_aes() -> None:
    bundle = {"broker": "mqtts://192.0.2.5:9883", "username": "u", "password": "p", "deviceId": "D"}
    assert kobra.decrypt_bundle(kobra.encrypt_bundle(bundle, TOKEN, LOCAL), TOKEN, LOCAL) == bundle
    with pytest.raises(ProtocolShapeError):
        kobra.decrypt_bundle(kobra.encrypt_bundle(bundle, TOKEN, LOCAL), TOKEN, "other-local-toke")


def test_cloud_mode_and_the_kobra_2_are_refused_with_their_reason() -> None:
    good = {"token": TOKEN, "ctrlInfoUrl": "http://x/ctrl", "modelId": "20030", "ctrlType": "lan"}
    kobra.check_info(good)
    with pytest.raises(kobra.CloudModeError, match="LAN Mode"):
        kobra.check_info({**good, "ctrlType": "cloud"})
    with pytest.raises(ProtocolShapeError, match="Kobra 2"):
        kobra.check_info({**good, "modelId": "20022"})
    with pytest.raises(ProtocolShapeError):
        kobra.check_info({"modelId": "20030"})


def test_the_session_keeps_the_users_address_and_the_printers_port() -> None:
    session = kobra.session_from(
        {"modelId": 20030, "cn": "SN1", "modelName": "Anycubic Kobra X"},
        {"broker": "mqtts://10.9.9.9:9993", "username": "u", "password": "p", "deviceId": "D",
         "devicecrt": "CERT", "devicepk": "KEY"},
        "printer.local",
    )
    assert (session.broker_host, session.broker_port) == ("printer.local", 9993)
    assert (session.model_id, session.serial, session.client_cert) == ("20030", "SN1", "CERT")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, PrintState.UNKNOWN),
        ({"state": "free", "project": {"state": "printing"}}, PrintState.IDLE),
        ({"state": "busy", "project": {"state": "preheating"}}, PrintState.PREPARING),
        ({"state": "busy", "project": {"state": "printing", "pause": 0}}, PrintState.PRINTING),
        ({"state": "busy", "project": {"state": "printing", "pause": 2}}, PrintState.PAUSED),
        ({"state": "busy", "project": {"state": "paused", "pause": 1}}, PrintState.PAUSED),
        ({"state": "busy", "project": {"state": "resuming", "pause": 3}}, PrintState.PRINTING),
        ({"state": "busy", "project": {"state": "stoped"}}, PrintState.CANCELLED),
        ({"state": "busy", "project": {"state": "finished"}}, PrintState.FINISHED),
        ({"state": "busy", "project": {"state": "something new"}}, PrintState.PRINTING),
    ],
)
def test_the_state_table(state: dict, expected: PrintState) -> None:
    assert kobra.state_for(state) is expected


def test_a_temperature_push_never_blanks_a_field() -> None:
    temp = {"curr_nozzle_temp": 200, "target_nozzle_temp": 210}
    kobra.fold(temp, {"curr_nozzle_temp": 205, "target_nozzle_temp": None})
    assert temp == {"curr_nozzle_temp": 205, "target_nozzle_temp": 210}


KX_UNIT = {"id": -1, "model_id": 40002, "loaded_slot": 0, "auto_feed": 1, "slots": [
    {"index": 0, "type": "PLA", "color": [255, 255, 255], "status": 5},
    {"index": 1, "type": "", "color": [0, 0, 0], "status": 0},
]}


def test_the_built_in_unit_is_unit_zero_and_an_external_one_follows() -> None:
    boxes = kobra.merge_boxes({}, {"multi_color_box": [KX_UNIT, {"id": 0, "model_id": 40001, "slots": []}]})
    system = kobra.filament_system(boxes)
    assert [unit.name for unit in system.units] == ["Multi-colour unit", "ACE Pro"]
    assert kobra.unit_numbers(boxes) == {-1: 0, 0: 1}
    first, second = system.units[0].slots
    assert (first.loaded, first.active, first.material, first.color) == (True, True, "PLA", "#FFFFFF")
    assert (second.loaded, second.material) == (False, None)
    assert system.auto_refill is True


def test_a_partial_report_merges_by_slot_and_an_empty_one_means_detached() -> None:
    boxes = kobra.merge_boxes({}, {"multi_color_box": [KX_UNIT]})
    boxes = kobra.merge_boxes(boxes, {"multi_color_box": [{"id": -1, "slots": [{"index": 1, "type": "PETG", "status": 5}]}]})
    slots = kobra.filament_system(boxes).units[0].slots
    assert [slot.material for slot in slots] == ["PLA", "PETG"]
    assert kobra.merge_boxes(boxes, {"multi_color_box": []}) == {}
    assert kobra.filament_system({}) is None


def test_the_file_list_is_read_leniently_and_a_missing_list_is_an_error() -> None:
    files = kobra.parse_file_list({"file_list": [
        {"name": "benchy.gcode", "size": 1234, "modify_time": 1700000000},
        {"filename": "cube.gcode", "size": 42, "modify_time": 1700000000123},
        {"name": "models", "is_dir": True},
        {"size": 1},
    ]})
    assert [item.name for item in files] == ["benchy.gcode", "cube.gcode"]
    assert files[0].modified == files[1].modified.replace(microsecond=0)
    with pytest.raises(ProtocolShapeError):
        kobra.parse_file_list({"list_mode": 0})


def test_speed_modes_and_jog_distances() -> None:
    assert [kobra.speed_mode_for(value) for value in (10, 75, 100, 125, 200)] == [1, 1, 2, 2, 3]
    assert kobra.distance_value(-10.0) == 10 and kobra.distance_value(0.4) == 0.4

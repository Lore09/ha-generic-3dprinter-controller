"""Which kind of SDCP printer answered: the file types, the fields and the name tell resin from FDM."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.generic_3dprinter.adapters.sdcp import classify_sdcp, sdcp_identity
from tests.fake_printer import FakePrinterServer
from tests.fake_resin_printer import load_fixture

#: A live Centauri Carbon's attributes, from docs/protocol-elegoo-sdcp-verified.md.
CENTAURI_ATTRIBUTES: dict[str, Any] = {
    "Name": "Centauri Carbon",
    "MachineName": "Centauri Carbon",
    "BrandName": "ELEGOO",
    "ProtocolVersion": "V3.0.0",
    "FirmwareVersion": "V1.4.49",
    "XYZsize": "218.88x128.88x220",
    "MainboardIP": "192.0.2.43",
    "MainboardMAC": "a4:e8:8d:2f:c5:09",
    "MainboardID": "5c441dd30105041800009c0000000000",
    "SDCPStatus": 0,
    "NumberOfVideoStreamConnected": 1,
    "MaximumVideoStreamAllowed": 4,
    "NetworkStatus": "wlan",
    "UsbDiskStatus": 0,
    "Capabilities": ["FILE_TRANSFER", "PRINT_CONTROL", "VIDEO_STREAM"],
    "SupportFileType": ["gcode"],
    "DevicesStatus": {"SgStatus": 1, "ZMotorStatus": 1, "XMotorStatus": 1, "YMotorStatus": 1},
    "CameraStatus": 1,
    "RemainingMemory": 6026862592,
    "TLPNoCapPos": 0,
    "TLPStartCapPos": 0,
    "TLPInterLayers": 0,
}

SATURN = load_fixture()
SATURN_ATTRIBUTES = SATURN["attributes"]["Attributes"]
SATURN_STATUS = SATURN["status"]["Status"]

#: Only what the spec's flat discovery reply carries.
IDENTITY_KEYS = (
    "Name", "MachineName", "BrandName", "MainboardIP", "MainboardID", "ProtocolVersion", "FirmwareVersion"
)


def _flat(attributes: dict[str, Any]) -> dict[str, Any]:
    return {"Id": "x", "Data": {key: attributes[key] for key in IDENTITY_KEYS}}


def test_the_live_centauri_is_fdm() -> None:
    assert classify_sdcp(CENTAURI_ATTRIBUTES) == "fdm"


def test_the_captured_saturn_is_resin() -> None:
    assert classify_sdcp(SATURN_ATTRIBUTES) == "resin"
    assert classify_sdcp(SATURN_ATTRIBUTES, SATURN_STATUS) == "resin"


def test_the_saturn_status_alone_says_resin() -> None:
    """Attributes may never arrive, and the status still tells."""
    assert classify_sdcp({}, SATURN_STATUS) == "resin"


def test_the_centauri_fake_is_fdm() -> None:
    fake = FakePrinterServer()
    assert classify_sdcp(fake.attributes, fake.status) == "fdm"
    assert classify_sdcp({}, fake.status) == "fdm"


def test_a_flat_discovery_reply_is_classified_by_its_name() -> None:
    assert classify_sdcp(sdcp_identity(_flat(SATURN_ATTRIBUTES))) == "resin"
    assert classify_sdcp(sdcp_identity(_flat(CENTAURI_ATTRIBUTES))) == "fdm"
    assert sdcp_identity(_flat(SATURN_ATTRIBUTES))["MainboardID"] == "78070ac4ce6d0100"


def test_a_nested_discovery_reply_is_read_through() -> None:
    reply = {"Id": "x", "Data": {"Attributes": SATURN_ATTRIBUTES, "Status": SATURN_STATUS}}
    identity = sdcp_identity(reply)
    assert identity["MainboardID"] == "78070ac4ce6d0100"
    assert identity["Status"]["TempOfTank"] == 29
    assert classify_sdcp(identity) == "resin"
    # A status alone is enough, once nested.
    assert classify_sdcp(sdcp_identity({"Data": {"Attributes": {}, "Status": SATURN_STATUS}})) == "resin"


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("saturn 4 ultra 16k", "resin"),
        ("ELEGOO MARS 5 ULTRA", "resin"),
        ("Neptune 4 Pro", "fdm"),
        ("Centauri Carbon", "fdm"),
        ("Jupiter", None),
        ("", None),
    ],
)
def test_the_name_is_the_last_resort(name: str, kind: str | None) -> None:
    assert classify_sdcp({"MachineName": name}) == kind


def test_the_file_types_decide_first() -> None:
    assert classify_sdcp({"SupportFileType": ["gcode"], "Resolution": "1x1"}) == "fdm"
    assert classify_sdcp({"SupportFileType": ["CTB"], "MachineName": "Centauri"}) == "resin"
    assert classify_sdcp({"SupportFileType": "goo"}) == "resin"


def test_the_build_volume_says_nothing() -> None:
    """Both printers report the same placeholder ``XYZsize``."""
    assert SATURN_ATTRIBUTES["XYZsize"] == CENTAURI_ATTRIBUTES["XYZsize"]
    assert classify_sdcp({"XYZsize": SATURN_ATTRIBUTES["XYZsize"]}) is None

"""Capability gating and value mapping for the entity platforms.

The entity classes are exercised directly against a real
:class:`~custom_components.generic_3dprinter.coordinator.PrinterCoordinator` built
on a fake adapter. The subject is "which entity exists, and what does it read or
send", and that rule lives in the platform's description table together with
``native_value``, ``is_on`` and the command calls, none of which need a running Home
Assistant to answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntry

from custom_components.generic_3dprinter import binary_sensor as binary_sensor_platform
from custom_components.generic_3dprinter import button as button_platform
from custom_components.generic_3dprinter import camera as camera_platform
from custom_components.generic_3dprinter import number as number_platform
from custom_components.generic_3dprinter import registry
from custom_components.generic_3dprinter import sensor as sensor_platform
from custom_components.generic_3dprinter import switch as switch_platform
from custom_components.generic_3dprinter.const import (
    DATA_COORDINATORS,
    DOMAIN,
    Capability,
    Command,
    LightChannel,
    PrintState,
    ProtocolId,
    ResinPhase,
)
from custom_components.generic_3dprinter.coordinator import PrinterCoordinator
from custom_components.generic_3dprinter.entity import (
    async_device_info,
    granted_capabilities,
)
from custom_components.generic_3dprinter.models import (
    Axis,
    Fans,
    PrinterSnapshot,
    ResinState,
    Temps,
)
from custom_components.generic_3dprinter.protocols import PrinterConfig
from custom_components.generic_3dprinter.runtime import CameraHub, PrinterRuntime

pytestmark = pytest.mark.asyncio

#: Every capability the SDCP printer this integration was written against grants.
SDCP_CAPABILITIES = frozenset(
    {
        Capability.START_PRINT,
        Capability.PAUSE,
        Capability.RESUME,
        Capability.STOP,
        Capability.SET_HOTEND_TEMP,
        Capability.SET_BED_TEMP,
        Capability.SET_CHAMBER_TEMP,
        Capability.SET_FAN_SPEED,
        Capability.SET_SPEED,
        Capability.SET_FLOW,
        Capability.SET_LIGHT,
        Capability.HOME,
        Capability.JOG,
        Capability.FILE_LIST,
        Capability.FILE_UPLOAD,
        Capability.FILE_DELETE,
        Capability.CAMERA,
        Capability.WEB_UI,
    }
)

#: What a web-only printer can do: serve a page, and nothing else.
WEB_ONLY_CAPABILITIES = frozenset({Capability.WEB_UI})


def make_hass() -> SimpleNamespace:
    """Return a stand-in for ``hass`` holding only what a coordinator touches.

    Nothing under test awaits a task, listens to an event or writes to the registry,
    so shared ``data`` is the whole contract. Building it per test keeps this module
    clear of ``pytest_homeassistant_custom_component``'s own ``hass`` fixture.
    """
    return SimpleNamespace(data={}, loop=None)


class RecordingAdapter:
    """A protocol adapter that records what it was asked to do."""

    def __init__(self, capabilities: frozenset[Capability]) -> None:
        """Store the granted capabilities and start with an empty log."""
        self._capabilities = capabilities
        self.sent: list[tuple[Command, dict[str, Any]]] = []

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Return the capabilities this adapter grants."""
        return self._capabilities

    @property
    def unsafe_features(self) -> tuple[()]:
        """Return no declared hazards, which this fake pretends is the whole truth."""
        return ()

    @property
    def config(self) -> PrinterConfig:
        """Return a configuration naming this fake, for the camera hub's logging."""
        return PrinterConfig(name="fake", protocol="sdcp_cc1")

    async def async_send(self, command: Command, **params: Any) -> None:
        """Record one command and its parameters."""
        self.sent.append((command, params))


def build_runtime(
    capabilities: frozenset[Capability],
    snapshot: PrinterSnapshot | None = None,
    *,
    host: str = "10.0.0.42",
) -> tuple[PrinterRuntime, RecordingAdapter]:
    """Return a runtime whose dependencies are fakes, bypassing live sockets."""
    entry = ConfigEntry(
        entry_id="entry-1",
        domain=DOMAIN,
        title="Test Printer",
        data={},
        options={},
        source=SOURCE_USER,
        unique_id=None,
        discovery_keys=MappingProxyType({}),
        minor_version=1,
        version=1,
    )
    config = PrinterConfig(
        name="Test Printer",
        protocol=snapshot.protocol if snapshot else "sdcp_cc1",
        host=host,
        port=80,
        web_url="http://10.0.0.42/",
    )
    adapter = RecordingAdapter(capabilities)
    runtime = object.__new__(PrinterRuntime)
    runtime.entry = entry
    runtime.config = config
    runtime.adapter = adapter
    runtime.snapshot = (
        snapshot
        if snapshot is not None
        else PrinterSnapshot(protocol=config.protocol, connected=True)
    )
    runtime.files = []
    runtime.last_error = None
    runtime.last_seen = None
    runtime.camera = CameraHub(adapter)
    entry.runtime_data = runtime
    return runtime, adapter


def build_coordinator(
    hass: SimpleNamespace,
    capabilities: frozenset[Capability],
    snapshot: PrinterSnapshot | None = None,
    runtime: PrinterRuntime | None = None,
) -> PrinterCoordinator:
    """Return a coordinator holding one snapshot, registered on ``hass.data``.

    Pass ``runtime`` when a test also needs the recording adapter, so the coordinator
    and the assertions share one adapter rather than two.
    """
    if runtime is None:
        runtime, _ = build_runtime(capabilities, snapshot)
    coordinator = PrinterCoordinator(hass, runtime, runtime.entry)
    coordinator.async_set_updated_data(runtime.snapshot)
    # A command is followed by a debounced read of the printer, which needs a
    # running Home Assistant. The integration tests cover it; here it is counted.
    coordinator.refreshes = 0

    async def count_refresh() -> None:
        coordinator.refreshes += 1

    coordinator.async_request_refresh = count_refresh
    hass.data.setdefault(DATA_COORDINATORS, {})[runtime.entry_id] = coordinator
    return coordinator


class Collector:
    """Capture the entities a platform setup hands to Home Assistant."""

    def __init__(self) -> None:
        """Start with nothing collected."""
        self.entities: list[Any] = []

    def __call__(self, entities: Any) -> None:
        """Record one batch of entities."""
        self.entities.extend(entities)

    def keys(self) -> list[str]:
        """Return the translation key of every collected entity."""
        return [entity.entity_description.key for entity in self.entities]

    def by_key(self) -> dict[str, Any]:
        """Return the collected entities indexed by translation key."""
        return {entity.entity_description.key: entity for entity in self.entities}


async def collect(
    hass: SimpleNamespace, coordinator: PrinterCoordinator, platform: Any
) -> Collector:
    """Run one platform's ``async_setup_entry`` and return what it created."""
    collector = Collector()
    await platform.async_setup_entry(hass, coordinator.config_entry, collector)
    return collector


def populated_snapshot(
    capabilities: frozenset[Capability] = SDCP_CAPABILITIES,
) -> PrinterSnapshot:
    """Return a snapshot with every reading filled in."""
    return PrinterSnapshot(
        protocol=ProtocolId.SDCP_CC1,
        connected=True,
        capabilities=capabilities,
        print_state=PrintState.PRINTING,
        progress=42.5,
        current_layer=118,
        total_layers=280,
        remaining=3600.0,
        elapsed=1800.0,
        filename="benchy.gcode",
        speed_factor=105.0,
        flow_factor=98.0,
        hotend=Temps(current=212.5, target=215.0),
        bed=Temps(current=60.0, target=60.0),
        chamber=Temps(current=31.0, target=35.0),
        fans=Fans(model=80.0, auxiliary=40.0),
        position=Axis(x=12.5, y=44.0, z=7.25),
        lights=frozenset({LightChannel.CHAMBER}),
        camera=True,
        model="Centauri Carbon",
        firmware="1.1.25",
        serial="CC-0001",
    )


async def test_buttons_follow_the_granted_capabilities() -> None:
    """A printer granting pause alone gets pause alone, not four dead buttons."""
    hass = make_hass()
    coordinator = build_coordinator(hass, frozenset({Capability.PAUSE}))
    collector = await collect(hass, coordinator, button_platform)
    assert collector.keys() == ["pause"]


async def test_a_full_capability_set_gets_every_button() -> None:
    """Every job-control capability present means every job-control button."""
    hass = make_hass()
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES)
    collector = await collect(hass, coordinator, button_platform)
    assert sorted(collector.keys()) == ["home", "pause", "resume", "stop"]


async def test_pause_only_printer_has_no_temperature_number() -> None:
    """No temperature capability means no temperature control at all."""
    hass = make_hass()
    coordinator = build_coordinator(hass, frozenset({Capability.PAUSE}))
    collector = await collect(hass, coordinator, number_platform)
    assert collector.keys() == []


async def test_a_full_capability_set_gets_every_number() -> None:
    """Every writable capability present means every writable control."""
    hass = make_hass()
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES)
    collector = await collect(hass, coordinator, number_platform)
    assert sorted(collector.keys()) == [
        "bed_target",
        "chamber_target",
        "fan_auxiliary_speed",
        "fan_chamber_speed",
        "fan_model_speed",
        "flow_factor",
        "nozzle_target",
        "speed_factor",
    ]


async def test_a_web_only_printer_gets_no_control_and_no_camera() -> None:
    """A printer that can only serve a page gets no control surface at all."""
    hass = make_hass()
    coordinator = build_coordinator(hass, WEB_ONLY_CAPABILITIES)
    buttons = await collect(hass, coordinator, button_platform)
    numbers = await collect(hass, coordinator, number_platform)
    switches = await collect(hass, coordinator, switch_platform)
    cameras = await collect(hass, coordinator, camera_platform)
    assert buttons.keys() == []
    assert numbers.keys() == []
    assert switches.keys() == []
    assert cameras.keys() == []


async def test_sensors_are_gated_on_capability() -> None:
    """Chamber sensors need the chamber capability; fan and speed need theirs."""
    hass = make_hass()
    coordinator = build_coordinator(hass, frozenset({Capability.PAUSE}))
    collector = await collect(hass, coordinator, sensor_platform)
    keys = collector.keys()
    assert "progress" in keys
    assert "chamber_temperature" not in keys
    assert "chamber_target_temperature" not in keys
    assert "fan_model_speed" not in keys
    assert "position_x" not in keys
    assert "speed_factor" not in keys


async def test_sensors_for_a_full_capability_set() -> None:
    """A full printer gets the whole table, layers included."""
    hass = make_hass()
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES)
    collector = await collect(hass, coordinator, sensor_platform)
    keys = collector.keys()
    for expected in (
        "printer_state",
        "progress",
        "current_layer",
        "total_layers",
        "remaining_time",
        "elapsed_time",
        "filename",
        "nozzle_temperature",
        "nozzle_target_temperature",
        "bed_temperature",
        "bed_target_temperature",
        "chamber_temperature",
        "chamber_target_temperature",
        "fan_model_speed",
        "fan_auxiliary_speed",
        "position_x",
        "position_y",
        "position_z",
        "speed_factor",
        "flow_factor",
        "protocol",
        "firmware",
        "model",
        "serial",
        "ip_address",
    ):
        assert expected in keys, expected


async def test_the_layer_counters_need_a_job_protocol() -> None:
    """Layer sensors follow the file-upload capability, which is the layer proxy."""
    hass = make_hass()
    no_job = await collect(
        hass,
        build_coordinator(hass, frozenset({Capability.PAUSE})),
        sensor_platform,
    )
    assert "current_layer" not in no_job.keys()

    with_job = await collect(
        hass,
        build_coordinator(hass, SDCP_CAPABILITIES),
        sensor_platform,
    )
    assert "current_layer" in with_job.keys()


async def test_the_layer_rule_also_covers_the_two_protocols_without_layers() -> None:
    """OctoPrint and PrusaLink upload files, so the rule gives them the sensors.

    This is the known cost of the chosen rule: neither protocol reports a layer
    count, so both sensors read ``unknown`` until they are disabled. The rejected
    alternatives and their costs are written up at ``SENSOR_DESCRIPTIONS``.
    """
    hass = make_hass()
    octoprint = frozenset(
        {
            Capability.FILE_LIST,
            Capability.FILE_UPLOAD,
            Capability.FILE_DELETE,
            Capability.PAUSE,
            Capability.RESUME,
            Capability.STOP,
        }
    )
    collector = await collect(hass, build_coordinator(hass, octoprint), sensor_platform)
    assert "current_layer" in collector.keys()

    prusalink = frozenset(
        {Capability.FILE_LIST, Capability.FILE_UPLOAD, Capability.STOP}
    )
    collector = await collect(hass, build_coordinator(hass, prusalink), sensor_platform)
    assert "current_layer" in collector.keys()


async def test_sensor_values_read_the_snapshot() -> None:
    """Every populated reading reaches its sensor's native value."""
    hass = make_hass()
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES, populated_snapshot())
    collector = await collect(hass, coordinator, sensor_platform)
    by_key = collector.by_key()

    assert by_key["printer_state"].native_value == "printing"
    assert by_key["printer_state"].options == [item.value for item in PrintState]
    assert by_key["progress"].native_value == 42.5
    assert by_key["current_layer"].native_value == 118
    assert by_key["total_layers"].native_value == 280
    assert by_key["remaining_time"].native_value == 3600.0
    assert by_key["elapsed_time"].native_value == 1800.0
    assert by_key["nozzle_temperature"].native_value == 212.5
    assert by_key["nozzle_target_temperature"].native_value == 215.0
    assert by_key["bed_temperature"].native_value == 60.0
    assert by_key["bed_target_temperature"].native_value == 60.0
    assert by_key["chamber_temperature"].native_value == 31.0
    assert by_key["chamber_target_temperature"].native_value == 35.0
    assert by_key["fan_model_speed"].native_value == 80.0
    assert by_key["fan_auxiliary_speed"].native_value == 40.0
    assert by_key["position_x"].native_value == 12.5
    assert by_key["position_y"].native_value == 44.0
    assert by_key["position_z"].native_value == 7.25
    assert by_key["speed_factor"].native_value == 105.0
    assert by_key["flow_factor"].native_value == 98.0
    assert by_key["filename"].native_value == "benchy.gcode"
    assert by_key["protocol"].native_value == "sdcp_cc1"
    assert by_key["firmware"].native_value == "1.1.25"
    assert by_key["model"].native_value == "Centauri Carbon"
    assert by_key["serial"].native_value == "CC-0001"
    assert by_key["ip_address"].native_value == "10.0.0.42"


async def test_missing_readings_stay_none() -> None:
    """A protocol that cannot express a reading reports ``None``, never a number."""
    hass = make_hass()
    capabilities = frozenset({Capability.PAUSE, Capability.FILE_UPLOAD, Capability.HOME})
    snapshot = PrinterSnapshot(
        protocol=ProtocolId.OCTOPRINT,
        connected=True,
        capabilities=capabilities,
        print_state=PrintState.PRINTING,
    )
    coordinator = build_coordinator(hass, capabilities, snapshot)
    collector = await collect(hass, coordinator, sensor_platform)
    by_key = collector.by_key()
    assert by_key["current_layer"].native_value is None
    assert by_key["nozzle_temperature"].native_value is None
    assert by_key["remaining_time"].native_value is None
    assert by_key["position_x"].native_value is None


async def test_binary_sensors_read_connection_and_job_state() -> None:
    """Connectivity and job state both come from the snapshot."""
    hass = make_hass()
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES, populated_snapshot())
    collector = await collect(hass, coordinator, binary_sensor_platform)
    by_key = collector.by_key()
    assert by_key["online"].is_on is True
    assert by_key["active_job"].is_on is True

    offline = build_coordinator(
        hass, SDCP_CAPABILITIES, PrinterSnapshot(protocol="sdcp_cc1", connected=False)
    )
    collector = await collect(hass, offline, binary_sensor_platform)
    by_key = collector.by_key()
    assert by_key["online"].is_on is False
    assert by_key["active_job"].is_on is False


async def test_a_button_press_sends_its_command() -> None:
    """Pressing a button sends its command and its parameters."""
    hass = make_hass()
    runtime, adapter = build_runtime(SDCP_CAPABILITIES)
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES, runtime=runtime)
    collector = await collect(hass, coordinator, button_platform)
    by_key = collector.by_key()

    await by_key["home"].async_press()
    assert adapter.sent[-1] == (Command.HOME, {"axes": "XYZ"})
    assert coordinator.refreshes == 1, "a command is followed by a fresh reading"

    await by_key["pause"].async_press()
    assert adapter.sent[-1] == (Command.PAUSE, {})

    await by_key["resume"].async_press()
    assert adapter.sent[-1] == (Command.RESUME, {})

    await by_key["stop"].async_press()
    assert adapter.sent[-1] == (Command.STOP, {})


async def test_a_number_set_sends_value() -> None:
    """Setting a number sends the command that owns it, with ``value``."""
    hass = make_hass()
    runtime, adapter = build_runtime(SDCP_CAPABILITIES)
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES, runtime=runtime)
    collector = await collect(hass, coordinator, number_platform)
    by_key = collector.by_key()

    await by_key["nozzle_target"].async_set_native_value(210)
    assert adapter.sent[-1] == (Command.SET_HOTEND_TEMP, {"value": 210})

    await by_key["bed_target"].async_set_native_value(65)
    assert adapter.sent[-1] == (Command.SET_BED_TEMP, {"value": 65})

    await by_key["chamber_target"].async_set_native_value(35)
    assert adapter.sent[-1] == (Command.SET_CHAMBER_TEMP, {"value": 35})

    await by_key["speed_factor"].async_set_native_value(120)
    assert adapter.sent[-1] == (Command.SET_SPEED, {"value": 120})

    await by_key["flow_factor"].async_set_native_value(95)
    assert adapter.sent[-1] == (Command.SET_FLOW, {"value": 95})


async def test_a_fan_number_set_sends_its_channel() -> None:
    """A fan control names the channel it addresses."""
    hass = make_hass()
    runtime, adapter = build_runtime(SDCP_CAPABILITIES)
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES, runtime=runtime)
    collector = await collect(hass, coordinator, number_platform)
    by_key = collector.by_key()

    await by_key["fan_model_speed"].async_set_native_value(70)
    assert adapter.sent[-1] == (
        Command.SET_FAN_SPEED,
        {"value": 70, "channel": "model"},
    )

    await by_key["fan_auxiliary_speed"].async_set_native_value(35)
    assert adapter.sent[-1] == (
        Command.SET_FAN_SPEED,
        {"value": 35, "channel": "auxiliary"},
    )

    await by_key["fan_chamber_speed"].async_set_native_value(20)
    assert adapter.sent[-1] == (
        Command.SET_FAN_SPEED,
        {"value": 20, "channel": "chamber"},
    )


async def test_a_number_reads_the_printer_setting() -> None:
    """A number shows what the printer reported, not what was last typed."""
    hass = make_hass()
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES, populated_snapshot())
    collector = await collect(hass, coordinator, number_platform)
    by_key = collector.by_key()
    assert by_key["nozzle_target"].native_value == 215.0
    assert by_key["bed_target"].native_value == 60.0
    assert by_key["chamber_target"].native_value == 35.0
    assert by_key["speed_factor"].native_value == 105.0
    assert by_key["flow_factor"].native_value == 98.0
    assert by_key["fan_model_speed"].native_value == 80.0
    assert by_key["fan_auxiliary_speed"].native_value == 40.0


async def test_the_light_switch_sends_on_and_off() -> None:
    """Turning the light on and off sends ``SET_LIGHT`` for the chamber channel."""
    hass = make_hass()
    runtime, adapter = build_runtime(SDCP_CAPABILITIES, populated_snapshot())
    coordinator = build_coordinator(
        hass, SDCP_CAPABILITIES, populated_snapshot(), runtime=runtime
    )
    collector = await collect(hass, coordinator, switch_platform)
    light = collector.entities[0]

    assert light.is_on is True
    await light.async_turn_off()
    assert adapter.sent[-1] == (
        Command.SET_LIGHT,
        {"on": False, "channel": "chamber"},
    )

    await light.async_turn_on()
    assert adapter.sent[-1] == (
        Command.SET_LIGHT,
        {"on": True, "channel": "chamber"},
    )


async def test_the_light_switch_reports_the_printers_own_state() -> None:
    """A light off at the printer shows as off here, because the snapshot says so."""
    hass = make_hass()
    snapshot = PrinterSnapshot(protocol="sdcp_cc1", connected=True)
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES, snapshot)
    collector = await collect(hass, coordinator, switch_platform)
    assert collector.entities[0].is_on is False


async def test_the_camera_is_created_only_with_a_camera() -> None:
    """The camera entity follows the camera capability and names the printer."""
    hass = make_hass()
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES, populated_snapshot())
    collector = await collect(hass, coordinator, camera_platform)
    assert len(collector.entities) == 1
    camera = collector.entities[0]
    assert camera.unique_id == "entry-1_camera"
    assert camera.name == "Test Printer"
    assert camera.is_streaming is True
    assert camera.frame_interval == 0.2


async def test_the_camera_serves_a_frame_from_the_shared_hub() -> None:
    """A still frame comes from the runtime's camera hub, not from the printer."""
    hass = make_hass()
    runtime, adapter = build_runtime(SDCP_CAPABILITIES, populated_snapshot())
    coordinator = build_coordinator(
        hass, SDCP_CAPABILITIES, populated_snapshot(), runtime=runtime
    )
    camera = (await collect(hass, coordinator, camera_platform)).entities[0]

    async def frame() -> bytes:
        return b"\xff\xd8jpeg"

    adapter.async_camera_frame = frame
    assert await camera.async_camera_image() == b"\xff\xd8jpeg"


async def test_device_info_comes_from_configuration_and_snapshot() -> None:
    """The device carries the configured name and the reported model and firmware."""
    runtime, _ = build_runtime(SDCP_CAPABILITIES, populated_snapshot())
    info = async_device_info(runtime)
    assert info["identifiers"] == {(DOMAIN, "entry-1")}
    assert info["name"] == "Test Printer"
    assert info["model"] == "Centauri Carbon"
    assert info["sw_version"] == "1.1.25"
    assert info["configuration_url"] == "http://10.0.0.42/"


async def test_an_entity_is_unavailable_after_a_failed_poll() -> None:
    """A failed coordinator update makes every entity unavailable, not stale."""
    hass = make_hass()
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES, populated_snapshot())
    collector = await collect(hass, coordinator, sensor_platform)
    sensor = collector.by_key()["progress"]
    assert sensor.available is True
    coordinator.last_update_success = False
    assert sensor.available is False


async def test_every_entity_key_has_a_translation() -> None:
    """Each entity's translation key names a real entry in ``strings.json``.

    Home Assistant resolves an entity's name and its descriptive entity id from
    ``component.<domain>.entity.<platform>.<key>.name``. A key with no translation
    leaves the entity without a name. This test is the cheap guard for that, because
    it needs no entity platform.
    """
    strings_path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "generic_3dprinter"
        / "strings.json"
    )
    strings = json.loads(strings_path.read_text(encoding="utf-8"))
    translated = strings["entity"]

    expected = {
        "sensor": {item.key for item in sensor_platform.SENSOR_DESCRIPTIONS},
        "binary_sensor": {
            item.key for item in binary_sensor_platform.BINARY_SENSOR_DESCRIPTIONS
        },
        "button": {item.key for item in button_platform.BUTTON_DESCRIPTIONS},
        "number": {item.key for item in number_platform.NUMBER_DESCRIPTIONS},
        "switch": {item.key for item in switch_platform.SWITCH_DESCRIPTIONS},
    }
    for platform_name, keys in expected.items():
        for key in keys:
            assert key in translated[platform_name], f"{platform_name}.{key} has no name"


async def test_every_entity_is_attached_to_the_printer_device() -> None:
    """Every entity carries the printer's device, which fixes its entity id.

    Home Assistant prefixes an entity's object id with its device name only when
    the entity has one. An entity without ``device_info`` lands in the registry as
    ``number.temperature`` rather than ``number.<printer>_nozzle_target``.
    """
    hass = make_hass()
    coordinator = build_coordinator(hass, SDCP_CAPABILITIES, populated_snapshot())
    collected = []
    for platform in (
        sensor_platform,
        binary_sensor_platform,
        button_platform,
        number_platform,
        switch_platform,
        camera_platform,
    ):
        collected.extend((await collect(hass, coordinator, platform)).entities)

    assert collected
    for entity in collected:
        info = entity.device_info
        assert info is not None, type(entity).__name__
        assert info["identifiers"] == {(DOMAIN, "entry-1")}, type(entity).__name__
        assert info["name"] == "Test Printer", type(entity).__name__


async def test_the_shipped_translation_file_matches_strings_json() -> None:
    """``translations/en.json`` is the file Home Assistant actually reads.

    ``strings.json`` is the source tree's copy and is not loaded at runtime, so a
    stale ``en.json`` leaves every entity without a name and Home Assistant falls
    back to the device class, which then collides across sensors. Regenerate with
    ``python tools/sync_translations.py``.
    """
    package = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "generic_3dprinter"
    )
    source = json.loads((package / "strings.json").read_text(encoding="utf-8"))
    shipped = json.loads(
        (package / "translations" / "en.json").read_text(encoding="utf-8")
    )
    assert shipped == source


async def test_every_opt_in_has_a_label_in_both_flows() -> None:
    """An opt-in without a label shows its raw key as the checkbox in setup and options."""
    from custom_components.generic_3dprinter.registry import ADAPTERS

    package = Path(__file__).resolve().parents[1] / "custom_components" / "generic_3dprinter"
    strings = json.loads((package / "strings.json").read_text(encoding="utf-8"))
    setup = strings["config"]["step"]["unsafe"]["data"]
    options = strings["options"]["step"]["init"]["data"]
    for registration in ADAPTERS.values():
        for feature in registration.unsafe:
            key = f"unsafe_{feature.id}"
            assert key in setup and key in options, key


async def test_granted_capabilities_answers_every_capability() -> None:
    """The gate mapping answers ``True`` exactly for the granted set."""
    runtime, _ = build_runtime(frozenset({Capability.PAUSE}))
    granted = granted_capabilities(runtime)
    assert granted[Capability.PAUSE] is True
    assert granted[Capability.STOP] is False
    assert set(granted) == set(Capability)


#: The four sensors a resin printer has no reading for.
NOZZLE_AND_BED = frozenset(
    {
        "nozzle_temperature",
        "nozzle_target_temperature",
        "bed_temperature",
        "bed_target_temperature",
    }
)

#: The sensors only a resin printer gets.
RESIN_SENSORS = frozenset(
    {
        "print_phase",
        "uv_led_temperature",
        "release_film",
        "vat_temperature",
        "vat_target_temperature",
    }
)

#: What a Saturn 4 Ultra 16K grants for its readings alone.
RESIN_CAPABILITIES = frozenset(
    {Capability.FILE_LIST, Capability.RESIN_STATUS, Capability.VAT_SENSOR}
)

#: Every key the snapshot document had before resin printers.
FDM_SNAPSHOT_KEYS = frozenset(
    {
        "protocol",
        "connected",
        "print_state",
        "capabilities",
        "progress",
        "current_layer",
        "total_layers",
        "remaining",
        "elapsed",
        "filename",
        "job_id",
        "speed_factor",
        "flow_factor",
        "hotend",
        "bed",
        "chamber",
        "fans",
        "position",
        "homed_axes",
        "lights",
        "camera",
        "filament",
        "model",
        "firmware",
        "serial",
        "errors",
        "blocked",
    }
)


def resin_snapshot(
    capabilities: frozenset[Capability] = RESIN_CAPABILITIES,
) -> PrinterSnapshot:
    """Return the idle Saturn 4 Ultra 16K as it answered Cmd 0 and Cmd 1 on V1.5.6."""
    return PrinterSnapshot(
        protocol=ProtocolId.SDCP_CC1,
        connected=True,
        capabilities=capabilities,
        print_state=PrintState.IDLE,
        filename="SUP_allineatore_01_1_202609301434.goo",
        resin=ResinState(
            machine="idle",
            phase=ResinPhase.IDLE,
            phase_code=0,
            uv_led=29.077695846557617,
            vat=Temps(current=29.0, target=30.0),
            vat_heat_status=1,
            release_film=283,
            release_film_max=60000,
            printer_timelapse=False,
            video_streams=0,
            video_streams_max=2,
        ),
        model="Saturn 4 Ultra 16K",
        firmware="V1.5.6",
        serial="78070ac4ce6d0100",
    )


def _fdm_variants() -> dict[str, frozenset[Capability]]:
    """Return every non-resin registration's grant, with and without opt-ins and per model."""
    variants = {}
    for item in registry.ADAPTERS.values():
        if Capability.RESIN_STATUS in item.capabilities:
            continue
        name = item.id.value
        variants[f"{name}-all"] = item.capabilities
        variants[f"{name}-safe"] = registry.granted_capabilities(item, frozenset())
        for model in item.models:
            variants[f"{name}-{model.id}"] = item.capabilities & model.capabilities
    return variants


FDM_VARIANTS = _fdm_variants()


@pytest.mark.parametrize("variant", list(FDM_VARIANTS))
async def test_every_fdm_registration_keeps_its_sensors(variant: str) -> None:
    """Resin support changes no existing printer: its heaters and layers stay, no resin sensor comes."""
    capabilities = FDM_VARIANTS[variant]
    granted = {item: item in capabilities for item in Capability}
    keys = {item.key for item in sensor_platform.SENSOR_DESCRIPTIONS if item.gate(granted)}
    assert NOZZLE_AND_BED <= keys
    assert not keys & RESIN_SENSORS
    job = bool({Capability.START_PRINT, Capability.PAUSE} & capabilities)
    assert ("progress" in keys) is job
    assert ("current_layer" in keys) is (Capability.FILE_UPLOAD in capabilities)
    assert ("total_layers" in keys) is (Capability.FILE_UPLOAD in capabilities)


async def test_a_resin_printer_has_no_nozzle_or_bed_sensor() -> None:
    """RESIN_STATUS drops the four heater sensors and brings the resin ones, layers and progress."""
    hass = make_hass()
    collector = await collect(
        hass, build_coordinator(hass, RESIN_CAPABILITIES), sensor_platform
    )
    keys = set(collector.keys())
    assert not keys & NOZZLE_AND_BED
    assert RESIN_SENSORS <= keys
    assert {"current_layer", "total_layers", "progress"} <= keys
    assert not keys & {"chamber_temperature", "fan_model_speed", "position_z", "speed_factor"}


async def test_the_vat_sensors_need_their_own_capability() -> None:
    """A resin printer without a vat heater gets the resin sensors but no vat ones."""
    hass = make_hass()
    capabilities = frozenset({Capability.RESIN_STATUS})
    collector = await collect(hass, build_coordinator(hass, capabilities), sensor_platform)
    keys = set(collector.keys())
    assert {"print_phase", "uv_led_temperature", "release_film"} <= keys
    assert not keys & {"vat_temperature", "vat_target_temperature"}


async def test_resin_sensors_read_the_captured_saturn() -> None:
    """Each resin sensor reads its value from the idle capture."""
    hass = make_hass()
    coordinator = build_coordinator(hass, RESIN_CAPABILITIES, resin_snapshot())
    by_key = (await collect(hass, coordinator, sensor_platform)).by_key()
    assert by_key["print_phase"].native_value == "idle"
    assert by_key["print_phase"].options == [item.value for item in ResinPhase]
    assert by_key["uv_led_temperature"].native_value == 29.077695846557617
    assert by_key["vat_temperature"].native_value == 29.0
    assert by_key["vat_target_temperature"].native_value == 30.0
    assert by_key["release_film"].native_value == 283
    assert by_key["release_film"].extra_state_attributes == {
        "max": 60000,
        "used_percent": 0.5,
    }
    assert by_key["filename"].native_value == "SUP_allineatore_01_1_202609301434.goo"
    assert by_key["uv_led_temperature"].extra_state_attributes is None


async def test_resin_sensors_stay_none_without_a_resin_state() -> None:
    """A snapshot with no resin state, such as an offline one, invents no reading."""
    hass = make_hass()
    snapshot = PrinterSnapshot(
        protocol=ProtocolId.SDCP_CC1, connected=False, capabilities=RESIN_CAPABILITIES
    )
    coordinator = build_coordinator(hass, RESIN_CAPABILITIES, snapshot)
    by_key = (await collect(hass, coordinator, sensor_platform)).by_key()
    for key in RESIN_SENSORS:
        assert by_key[key].native_value is None, key
    assert by_key["release_film"].extra_state_attributes is None

    coordinator.data = PrinterSnapshot(
        protocol=ProtocolId.SDCP_CC1, connected=True, resin=ResinState()
    )
    assert by_key["print_phase"].native_value is None
    assert by_key["release_film"].extra_state_attributes == {
        "max": None,
        "used_percent": None,
    }


async def test_an_fdm_snapshot_document_only_gains_an_empty_resin_key() -> None:
    """The card and diagnostics see the same FDM document as before, plus ``resin: None``."""
    document = populated_snapshot().as_dict()
    assert set(document) == FDM_SNAPSHOT_KEYS | {"resin"}
    assert document["resin"] is None


async def test_a_resin_snapshot_document_is_json_safe() -> None:
    """The resin state serialises with its phase as text and the film's use in percent."""
    document = resin_snapshot().as_dict()
    assert json.loads(json.dumps(document)) == document
    assert document["resin"] == {
        "machine": "idle",
        "phase": "idle",
        "phase_code": 0,
        "uv_led": 29.077695846557617,
        "vat": {"current": 29.0, "target": 30.0},
        "vat_heat_status": 1,
        "release_film": 283,
        "release_film_max": 60000,
        "release_film_used": 0.5,
        "printer_timelapse": False,
        "device_faults": [],
        "video_streams": 0,
        "video_streams_max": 2,
    }


@pytest.mark.parametrize(
    ("used", "rated", "expected"),
    [
        (283, 60000, 0.5),
        (30000, 60000, 50.0),
        (283, 0, None),
        (283, None, None),
        (None, 60000, None),
    ],
)
async def test_release_film_use_needs_both_counts(
    used: int | None, rated: int | None, expected: float | None
) -> None:
    """The film's use is a share of its rating, and unknown when either count is."""
    state = ResinState(release_film=used, release_film_max=rated)
    assert state.release_film_used == expected


async def test_every_resin_phase_has_a_state_name() -> None:
    """The phase sensor names each state, so a dashboard never shows a raw key."""
    package = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "generic_3dprinter"
    )
    strings = json.loads((package / "strings.json").read_text(encoding="utf-8"))
    states = strings["entity"]["sensor"]["print_phase"]["state"]
    assert set(states) == {item.value for item in ResinPhase}

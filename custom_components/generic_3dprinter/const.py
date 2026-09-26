"""Constants for the Generic 3D Printer Controller integration.

Every protocol difference lives behind an adapter, so this module holds only
things that are true of the fleet as a whole: the domain, the API namespace, and
the defaults applied when a printer's own configuration says nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

DOMAIN: Final = "generic_3dprinter"
NAME: Final = "Generic 3D Printer Controller"
MANUFACTURER: Final = "Generic 3D Printer Controller"
USER_AGENT: Final = "ha-generic-3dprinter-controller"

#: API namespace served by this integration.
API_BASE: Final = "/api/generic_3dprinter"

#: Lovelace card served by this integration.
CARD_FILENAME: Final = "generic-3dprinter-card.js"
CARD_URL_PATH: Final = f"/{DOMAIN}/{CARD_FILENAME}"
WWW_PATH: Final = "www"

PLATFORMS: Final = ["binary_sensor", "button", "camera", "number", "sensor", "switch"]

# ---------------------------------------------------------------- config keys
CONF_NAME: Final = "name"
CONF_PROTOCOL: Final = "protocol"
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_TLS: Final = "tls"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_WEB_URL: Final = "web_url"
CONF_CAMERA_PORT: Final = "camera_port"
CONF_UNSAFE_ENABLED: Final = "unsafe_enabled"
CONF_API_KEY: Final = "api_key"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_ACCESS_CODE: Final = "access_code"
CONF_SERIAL: Final = "serial"

# ------------------------------------------------------------------ defaults
DEFAULT_SCAN_INTERVAL: Final = 30
DEFAULT_VERIFY_SSL: Final = True
DEFAULT_TLS: Final = False

MIN_SCAN_INTERVAL: Final = 5
MAX_SCAN_INTERVAL: Final = 3600

#: Every credential key any protocol may declare. Diagnostics redact these.
CREDENTIAL_KEYS: Final[tuple[str, ...]] = (
    CONF_API_KEY,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_ACCESS_CODE,
)

#: Lifetime of the URLs handed to the browser for proxied printer pages. Short
#: enough that a leaked URL stops working, long enough that a dashboard left open
#: overnight keeps refreshing its own sub-resources.
SIGNED_URL_TTL: Final = 6 * 60 * 60

# ------------------------------------------------------------------ resources
RESOURCE_CAMERA: Final = "camera.mjpeg"
RESOURCE_SNAPSHOT: Final = "snapshot.jpg"
RESOURCE_STATUS: Final = "status"
RESOURCE_WEB: Final = "web"
RESOURCE_WEBSOCKET: Final = "ws"

#: Query parameter carrying the upstream path inside a web-proxy token.
QUERY_PATH: Final = "p"

# ------------------------------------------------------------------ websocket
WS_PREFIX: Final = DOMAIN
WS_LIST: Final = f"{WS_PREFIX}/list"
WS_DESCRIBE: Final = f"{WS_PREFIX}/describe"
WS_SEND: Final = f"{WS_PREFIX}/send"
WS_FILES: Final = f"{WS_PREFIX}/files"

# ---------------------------------------------------------------- hass.data
DATA_RUNTIMES: Final = f"{DOMAIN}_runtimes"
DATA_COORDINATORS: Final = f"{DOMAIN}_coordinators"
DATA_SERVICES_REGISTERED: Final = f"{DOMAIN}_services_registered"
DATA_CARD_REGISTERED: Final = f"{DOMAIN}_card_registered"
DATA_VIEWS_REGISTERED: Final = f"{DOMAIN}_views_registered"
DATA_ROUTES_REGISTERED: Final = f"{DOMAIN}_routes_registered"
DATA_SIGNER: Final = f"{DOMAIN}_signer"
DATA_SIGNER_LOCK: Final = f"{DOMAIN}_signer_lock"


class Capability(StrEnum):
    """One thing the entity layer and the Lovelace card may offer.

    An adapter declares the set its wire protocol can express. The card renders a
    control per capability present, so it never learns a protocol name.
    """

    START_PRINT = "start_print"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    SET_HOTEND_TEMP = "set_hotend_temp"
    SET_BED_TEMP = "set_bed_temp"
    SET_CHAMBER_TEMP = "set_chamber_temp"
    #: The printer reports a chamber temperature it cannot be told to reach. A
    #: printer with ``SET_CHAMBER_TEMP`` reports one as well, so this is only
    #: declared by a protocol whose chamber is a reading and nothing more.
    CHAMBER_SENSOR = "chamber_sensor"
    SET_FAN_SPEED = "set_fan_speed"
    SET_SPEED = "set_speed"
    SET_FLOW = "set_flow"
    SET_LIGHT = "set_light"
    HOME = "home"
    JOG = "jog"
    FILE_LIST = "file_list"
    FILE_UPLOAD = "file_upload"
    FILE_DELETE = "file_delete"
    CAMERA = "camera"
    WEB_UI = "web_ui"
    #: The printer reports the slots of a multi-material unit, such as Elegoo's
    #: CANVAS, and what is loaded in each. A printer that can express this but has
    #: no unit attached reports no slots, so the card and the entities follow the
    #: data, not only the capability.
    FILAMENT_SLOTS = "filament_slots"
    LOAD_FILAMENT = "load_filament"
    UNLOAD_FILAMENT = "unload_filament"
    #: Record which filament sits in a slot: material, brand, colour, temperatures.
    SET_FILAMENT = "set_filament"
    SET_AUTO_REFILL = "set_auto_refill"


class Command(StrEnum):
    """The normalised command vocabulary. Adapters implement a subset."""

    START_PRINT = "start_print"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    SET_HOTEND_TEMP = "set_hotend_temp"
    SET_BED_TEMP = "set_bed_temp"
    SET_CHAMBER_TEMP = "set_chamber_temp"
    SET_FAN_SPEED = "set_fan_speed"
    SET_SPEED = "set_speed"
    SET_FLOW = "set_flow"
    SET_LIGHT = "set_light"
    HOME = "home"
    JOG = "jog"
    DELETE_FILE = "delete_file"
    LOAD_FILAMENT = "load_filament"
    UNLOAD_FILAMENT = "unload_filament"
    SET_FILAMENT = "set_filament"
    SET_AUTO_REFILL = "set_auto_refill"


class PrintState(StrEnum):
    """What the machine is doing, normalised across protocols."""

    UNKNOWN = "unknown"
    IDLE = "idle"
    PREPARING = "preparing"
    PRINTING = "printing"
    PAUSED = "paused"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    ERROR = "error"


class LightChannel(StrEnum):
    """One independently switchable light."""

    CHAMBER = "chamber"
    STATUS = "status"
    RGB = "rgb"


class ProtocolId(StrEnum):
    """Every protocol this integration speaks."""

    SDCP_CC1 = "sdcp_cc1"
    ELEGOO_CC2 = "elegoo_cc2"
    MOONRAKER = "moonraker"
    OCTOPRINT = "octoprint"
    DUET = "duet"
    WEB_ONLY = "web_only"


#: Commands that ask the machine to change what it is doing. Every printer refuses
#: them while a job is running, through the default state rule in ``protocols``,
#: because interrupting a job destroys the part.
STATE_CHANGING_COMMANDS: Final[frozenset[Command]] = frozenset(
    {
        Command.START_PRINT,
        Command.HOME,
        Command.JOG,
        Command.LOAD_FILAMENT,
        Command.UNLOAD_FILAMENT,
    }
)


@dataclass(frozen=True, slots=True)
class UnsafeFeature:
    """A command or payload shape the integration will not send unless asked.

    A feature is declared by an adapter, gated behind an explicit per-printer
    opt-in that is off by default, and surfaced in the options flow in the
    adapter's own words. It is data, so the safety gate never becomes an
    ``if protocol == ...`` branch, and it carries its own evidence so a reader can
    tell a measured hazard from an inherited warning.
    """

    id: str
    label: str
    reason: str
    gates: frozenset[Capability]
    evidence: str


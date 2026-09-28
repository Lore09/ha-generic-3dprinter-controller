"""Anycubic Kobra adapter: the printer's LAN mode, a signed handshake and MQTT over TLS.

The Kobra 3, Kobra 4, Kobra S1 and Kobra X speak one protocol. Nothing about it is
typed in by the user except the address, because the printer hands out its own
broker credentials:

* ``GET http://<host>:18910/info`` answers with a token, the model id, the serial
  and whether the printer is in LAN mode;
* a ``POST`` to the ``ctrlInfoUrl`` it names, signed with
  ``md5(md5(token[:16]) + ts + nonce)``, answers with an AES-CBC encrypted bundle:
  the broker's address, a user name and a password for this session, the device id
  every topic is built from, and on some firmware a client certificate and key;
* the broker speaks MQTT over TLS on 9883 with a certificate the printer signed
  itself. Reports arrive on ``.../printer/public/<model>/<device>/<type>/report``,
  and commands go to ``.../web/printer/<model>/<device>/<type>``, except starting a
  print and listing files, which use the slicer's ``.../slicer/...`` prefix.

Sources, all read rather than measured by this project: chrisfore/anycubic_ha_local,
whose protocol notes were captured on a Kobra S1 Max and whose Kobra X support was
confirmed from a user's diagnostics; stribor/anycubic_kobrax, written by a Kobra X
owner, for the Kobra X's own command shapes; rvanderp3/kobra-connect for the file
list; and the Rinkhals documentation for starting a print. The registry's evidence
says which are measured once a printer has been checked.

Four facts shape the adapter.

* The broker credentials last one session. Every setup runs the whole handshake
  again, and nothing but the address is stored.
* ``info`` is the only full report and can go minutes between pushes, while
  ``tempature`` (the firmware's own spelling), ``fan`` and ``print`` push within a
  second of a change. Every type is folded in, by type and never by ``action``.
* A printer can go silent while its socket still looks open. Silence for four polls
  is treated as a dead session, and the next read runs the handshake again.
* On most models temperatures, fans and the speed are settings of the running job,
  and an idle printer drops them without an answer. They are state rules, so the
  card says so rather than sending them into the void. The Kobra X sets its
  temperatures and fans with commands of its own, which work while idle.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import string
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import urlencode, urlsplit

import aiohttp

from ..const import (
    Capability,
    Command,
    LightChannel,
    ModelProfile,
    PrintState,
    ProtocolId,
    UnsafeFeature,
)
from ..discovery import DiscoveryResult
from ..models import (
    Celsius,
    Fans,
    FileEntry,
    FilamentSlot,
    FilamentSystem,
    FilamentUnit,
    Percent,
    PrinterSnapshot,
    Seconds,
    Temps,
)
from ..mqtt_client import MqttClient, MqttError, MqttRefusedError, insecure_tls_context
from ..protocols import (
    DEFAULT_BLOCK_RULES,
    AuthError,
    BlockRule,
    CommandRejectedError,
    ConfigError,
    PrinterConfig,
    Protocol,
    ProtocolError,
    ProtocolShapeError,
    UnreachableError,
    job_active,
    not_idle,
    valid_serial,
)

_LOGGER = logging.getLogger(__name__)

INFO_PORT: Final = 18910
MQTT_PORT: Final = 9883
CAMERA_PORT: Final = 18088
TOPIC_ROOT: Final = "anycubic/anycubicCloud/v1"

HTTP_TIMEOUT: Final = aiohttp.ClientTimeout(total=8)
CONNECT_TIMEOUT: Final = 10.0
KEEPALIVE: Final = 60
#: How long the first read after setup waits for the full ``info`` report.
FIRST_INFO_TIMEOUT: Final = 3.0
#: How long a later read waits for the answers to its own queries, so a command's
#: effect shows in the read that follows it. Past this, the read returns what it
#: has, and a late answer is folded in when it comes.
INFO_WAIT: Final = 1.5
#: How long a command waits for the printer to refuse it. Silence is acceptance,
#: because an idle printer drops a job setting without answering at all.
ANSWER_WINDOW: Final = 3.0
FILE_LIST_TIMEOUT: Final = 10.0
#: A Kobra X 2.0.1.9 refuses ``listLocal`` without paging (10112), then lists every
#: entry whatever the page asks for. Measured on the printer.
FILE_LIST_REQUEST: Final = {"path": "/", "page_num": 1, "page_size": 500}
#: Reads without a single report before the session counts as dead.
SILENT_POLLS: Final = 4
#: The camera: the official client stops the capture, waits, starts it, and reads
#: the stream URL from the video report that answers.
CAPTURE_KICK_DELAY: Final = 1.0
VIDEO_REPORT_TIMEOUT: Final = 4.0

#: The report types asked for on every read. The unit answers ``getInfo``, not
#: ``query``; ``peripherie`` is asked once per session.
QUERY_TYPES: Final = ("info", "tempature", "fan", "light")

MODEL_KOBRA_X: Final = "20030"
ENCLOSED_MODELS: Final = frozenset({"20025", "20029"})
#: The Kobra 2 generation uses an older handshake no source documents.
KOBRA_2_MODELS: Final = frozenset({"20021", "20022", "20023"})

#: ``light.control`` types: 2 is the chamber light of the enclosed S1 models, 3 the
#: camera light the open-frame models have.
CHAMBER_LIGHT: Final = 2
CAMERA_LIGHT: Final = 3

#: ``axis.move`` groups on the Kobra X, as its owner's integration sends them.
HOME_GROUPS: Final[Mapping[str, int]] = MappingProxyType({"XYZ": 5, "XY": 4, "Z": 3})
JOG_AXES: Final[Mapping[str, int]] = MappingProxyType({"X": 1, "Y": 2, "Z": 3})

#: ``print_speed_mode`` and the speed each mode stands for.
SPEED_PERCENT_BY_MODE: Final[Mapping[int, float]] = MappingProxyType({1: 50.0, 2: 100.0, 3: 150.0})

#: ``project.state`` while the printer is busy.
PREPARING_STATES: Final = frozenset({"preheating", "auto_leveling", "vibrating", "flow_calibrating"})
PRINTING_STATES: Final = frozenset({"printing", "resuming", "resumed", "updated"})
CANCELLED_STATES: Final = frozenset({"stopping", "stoped", "stopped"})
#: ``project.pause``: 0 running, 1 paused, 2 pausing, 3 resuming, 4 stopping.
PAUSED_FLAGS: Final = frozenset({1, 2})

#: Multi-material unit models, by the ``model_id`` the unit reports.
UNIT_NAMES: Final[Mapping[int, str]] = MappingProxyType({40001: "ACE Pro", 40002: "ACE 2"})
SLOT_LOADED: Final = 5


class CloudModeError(AuthError):
    """The printer is in cloud mode and answers no LAN client."""


# --------------------------------------------------------------------- handshake


def sign(token: str, ts: int, nonce: str) -> str:
    """Return the signature the printer checks: ``md5(md5(token[:16]) + ts + nonce)``."""
    first = hashlib.md5(token[:16].encode()).hexdigest()  # noqa: S324 - the printer's scheme
    return hashlib.md5((first + str(ts) + nonce).encode()).hexdigest()  # noqa: S324


def _aes_key_iv(token: str, local_token: str) -> tuple[bytes, bytes]:
    return token[16:32].encode(), local_token.encode()[:16].ljust(16, b"\0")


def decrypt_bundle(info_b64: str, token: str, local_token: str) -> dict[str, Any]:
    """Decrypt the ``/ctrl`` answer: AES-CBC, key ``token[16:32]``, IV the local token."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    key, iv = _aes_key_iv(token, local_token)
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(base64.b64decode(info_b64)) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
        bundle = json.loads(plain.decode())
    except (ValueError, json.JSONDecodeError) as err:
        raise ProtocolShapeError(f"the printer's session bundle could not be read: {err}") from err
    if not isinstance(bundle, dict):
        raise ProtocolShapeError("the printer's session bundle is not an object")
    return bundle


def encrypt_bundle(bundle: Mapping[str, Any], token: str, local_token: str) -> str:
    """Encrypt a bundle the way the printer does. Used by the test suite's fake."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    key, iv = _aes_key_iv(token, local_token)
    padder = PKCS7(128).padder()
    padded = padder.update(json.dumps(dict(bundle)).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


@dataclass(frozen=True, slots=True)
class Session:
    """What one handshake gave: where the broker is, and who to be on it."""

    broker_host: str
    broker_port: int
    username: str
    password: str
    device_id: str
    model_id: str
    serial: str
    model_name: str | None
    firmware: str | None
    client_cert: str | None = None
    client_key: str | None = None


def check_info(info: Mapping[str, Any]) -> None:
    """Refuse an ``/info`` answer this adapter cannot go on from, saying why."""
    if info.get("ctrlType") == "cloud":
        raise CloudModeError(
            "the printer is in cloud mode. Turn LAN Mode on at the printer, under "
            "Settings, Network, LAN Mode"
        )
    if str(info.get("modelId") or "") in KOBRA_2_MODELS or not (
        info.get("token") and info.get("ctrlInfoUrl") and info.get("modelId")
    ):
        raise ProtocolShapeError(
            "this printer does not use the signed LAN handshake of the Kobra 3, Kobra 4, "
            "Kobra S1 and Kobra X, so it is not supported. The Kobra 2 uses an older one "
            "that no source documents"
        )


def parse_broker(broker: str, fallback_host: str) -> tuple[str, int]:
    """Return the broker's host and port from ``mqtts://host:port``."""
    parts = urlsplit(broker if "://" in broker else f"mqtts://{broker}")
    return parts.hostname or fallback_host, parts.port or MQTT_PORT


def session_from(info: Mapping[str, Any], bundle: Mapping[str, Any], host: str) -> Session:
    """Combine ``/info`` and the decrypted bundle into one session."""
    try:
        broker_host, broker_port = parse_broker(str(bundle["broker"]), host)
        return Session(
            # The printer names itself by the address it believes it has; the one
            # the user gave is the one known to route.
            broker_host=host,
            broker_port=broker_port,
            username=str(bundle["username"]),
            password=str(bundle["password"]),
            device_id=str(bundle["deviceId"]),
            model_id=str(info["modelId"]),
            serial=str(info.get("cn") or ""),
            model_name=str(info.get("modelName") or "") or None,
            firmware=str(info.get("version") or info.get("firmwareVersion") or "") or None,
            client_cert=str(bundle.get("devicecrt") or "") or None,
            client_key=str(bundle.get("devicepk") or "") or None,
        )
    except KeyError as err:
        raise ProtocolShapeError(f"the printer's session bundle has no {err}") from err


# ----------------------------------------------------------------------- parsing


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def fold(target: dict[str, Any], data: Mapping[str, Any]) -> None:
    """Copy every field ``data`` carries into ``target``, never blanking one it omits."""
    for key, value in data.items():
        if value is not None:
            target[key] = value


def state_for(state: Mapping[str, Any]) -> PrintState:
    """Return the normalised state for the merged ``info`` data."""
    raw = state.get("state")
    if raw is None:
        return PrintState.UNKNOWN
    if raw == "free":
        return PrintState.IDLE
    project = _mapping(state.get("project"))
    if _integer(project.get("pause")) in PAUSED_FLAGS:
        return PrintState.PAUSED
    phase = str(project.get("state") or "")
    if phase in PREPARING_STATES:
        return PrintState.PREPARING
    if phase in CANCELLED_STATES:
        return PrintState.CANCELLED
    if phase == "finished":
        return PrintState.FINISHED
    if phase in ("paused", "pausing"):
        return PrintState.PAUSED
    return PrintState.PRINTING


def _hex(color: Any) -> str | None:
    if isinstance(color, Sequence) and not isinstance(color, str) and len(color) >= 3:
        try:
            red, green, blue = (max(0, min(255, int(part))) for part in color[:3])
        except (TypeError, ValueError):
            return None
        return f"#{red:02X}{green:02X}{blue:02X}"
    return None


def merge_boxes(
    boxes: dict[int, dict[str, Any]], report: Mapping[str, Any]
) -> dict[int, dict[str, Any]]:
    """Merge one ``multiColorBox`` report into the units already known.

    A report may carry one slot or all of them, so slots merge by index. An empty
    list is the answer of a printer with no unit attached.
    """
    listed = report.get("multi_color_box")
    if not isinstance(listed, Sequence) or isinstance(listed, str):
        return boxes
    if not listed:
        return {}
    merged = {box_id: dict(box) for box_id, box in boxes.items()}
    for item in listed:
        if not isinstance(item, Mapping) or _integer(item.get("id")) is None:
            continue
        box_id = int(item["id"])
        box = merged.setdefault(box_id, {"id": box_id, "slots": {}})
        for key, value in item.items():
            if key != "slots" and value is not None:
                box[key] = value
        slots = dict(box.get("slots") or {})
        for slot in item.get("slots") or ():
            if isinstance(slot, Mapping) and _integer(slot.get("index")) is not None:
                index = int(slot["index"])
                slots[index] = {**slots.get(index, {}), **{k: v for k, v in slot.items() if v is not None}}
        box["slots"] = slots
    return merged


def unit_numbers(boxes: Mapping[int, Mapping[str, Any]]) -> dict[int, int]:
    """Number the units from 0: the built-in one first, then the attached ones."""
    return {box_id: number for number, box_id in enumerate(sorted(boxes))}


def filament_system(boxes: Mapping[int, Mapping[str, Any]]) -> FilamentSystem | None:
    """Return the units as the shared model has them, or ``None`` with none attached."""
    if not boxes:
        return None
    numbers = unit_numbers(boxes)
    units: list[FilamentUnit] = []
    auto_feed: bool | None = None
    for box_id in sorted(boxes):
        box = boxes[box_id]
        loaded_slot = _integer(box.get("loaded_slot"))
        slots = []
        for index in sorted(box.get("slots") or {}):
            slot = box["slots"][index]
            material = str(slot.get("type") or "") or None
            slots.append(
                FilamentSlot(
                    unit=numbers[box_id],
                    slot=index,
                    loaded=material is not None and _integer(slot.get("status")) in (SLOT_LOADED, None),
                    active=loaded_slot is not None and loaded_slot == index,
                    material=material,
                    name=str(slot.get("sku") or "") or None,
                    color=_hex(slot.get("color")),
                )
            )
        if box_id < 0:
            name = "Multi-colour unit"
        else:
            name = UNIT_NAMES.get(_integer(box.get("model_id")) or 0, "ACE")
        units.append(FilamentUnit(unit=numbers[box_id], name=name, slots=tuple(slots)))
        if "auto_feed" in box:
            auto_feed = bool(auto_feed) or bool(_integer(box.get("auto_feed")))
    return FilamentSystem(units=tuple(units), auto_refill=auto_feed)


def parse_file_list(data: Mapping[str, Any]) -> list[FileEntry]:
    """Normalise a ``listLocal`` answer. Folders and entries without a name are skipped.

    A Kobra X answers with ``records``; the sources recorded ``file_list``.
    """
    listed = data.get("records", data.get("file_list"))
    if not isinstance(listed, Sequence) or isinstance(listed, str):
        raise ProtocolShapeError("the printer's file list has no records")
    files: list[FileEntry] = []
    for item in listed:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or item.get("filename") or "").strip()
        if not name or item.get("is_dir") or item.get("type") == "folder":
            continue
        stamp = _number(item.get("timestamp") or item.get("modify_time") or item.get("create_time"))
        modified = None
        if stamp:
            if stamp > 1e11:
                stamp /= 1000
            with suppress(OverflowError, OSError, ValueError):
                modified = datetime.fromtimestamp(stamp, tz=UTC)
        files.append(FileEntry(name=name, path=name, size=_integer(item.get("size")), modified=modified))
    return files


def speed_mode_for(percent: float) -> int:
    """Return the speed mode closest to ``percent``, the slower one on a tie."""
    return min(SPEED_PERCENT_BY_MODE, key=lambda mode: (abs(SPEED_PERCENT_BY_MODE[mode] - percent), mode))


def distance_value(distance: float) -> int | float:
    """Return a jog distance as the printer takes it: its size, the sign elsewhere."""
    size = abs(float(distance))
    return int(size) if size.is_integer() else size


def message(kind: str, action: str, data: Any = None) -> dict[str, Any]:
    """Return one request body, with a fresh message id and the current time."""
    return {
        "type": kind,
        "action": action,
        "timestamp": int(time.time() * 1000),
        "msgid": str(uuid.uuid4()),
        "data": data,
    }


def _celsius(value: Any) -> Celsius | None:
    number = _number(value)
    return Celsius(number) if number is not None else None


def _percent(value: Any) -> Percent | None:
    number = _number(value)
    return Percent(min(max(number, 0.0), 100.0)) if number is not None else None


#: Why an idle printer is refused a job setting.
JOB_SETTING: Final = "the printer applies this only during a print"


def _load_client_chain(context: Any, certificate: str, key: str) -> None:
    """Load an in-memory certificate and key; ``ssl`` reads them only from files."""
    paths: list[str] = []
    try:
        for content in (certificate, key):
            handle = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)  # noqa: SIM115
            with handle:
                handle.write(content if content.endswith("\n") else f"{content}\n")
            paths.append(handle.name)
        context.load_cert_chain(paths[0], paths[1])
    finally:
        for path in paths:
            with suppress(OSError):
                os.unlink(path)


async def fetch_info(session: aiohttp.ClientSession, host: str, port: int) -> dict[str, Any]:
    """Return the printer's ``/info`` answer. It is a question, never a command."""
    url = f"http://{host}:{port}/info"
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as response:
            if response.status >= 400:
                raise UnreachableError(f"{url} answered HTTP {response.status}")
            info = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as err:
        raise UnreachableError(f"cannot reach {url}: {err}") from err
    if not isinstance(info, dict):
        raise ProtocolShapeError(f"{url} did not answer with an object")
    return info


class AnycubicKobraProtocol(Protocol):
    """A Kobra 3, 4, S1 or X in LAN mode, over its own MQTT broker."""

    def __init__(
        self,
        config: PrinterConfig,
        session: aiohttp.ClientSession,
        *,
        granted: frozenset[Capability],
        unsafe: tuple[UnsafeFeature, ...] = (),
        models: tuple[ModelProfile, ...] = (),
    ) -> None:
        """Create the adapter for one printer."""
        super().__init__(config, session, granted=granted, unsafe=unsafe, models=models)
        self._broker_session: Session | None = None
        self._client: MqttClient | None = None
        self._state: dict[str, Any] = {}
        self._have_info = asyncio.Event()
        #: The report types a read asked for and has not had yet, and the event set
        #: once every one of them has answered.
        self._awaiting: set[str] = set()
        self._answered = asyncio.Event()
        self._lights: list[Mapping[str, Any]] = []
        self._peripherie: Mapping[str, Any] | None = None
        self._boxes: dict[int, dict[str, Any]] = {}
        self._reports = 0
        self._silent_reads = 0
        self._answers: dict[str, asyncio.Future[Mapping[str, Any]]] = {}
        self._files: asyncio.Future[Mapping[str, Any]] | None = None
        self._video: asyncio.Future[str | None] | None = None
        self._stream_url: str | None = None
        self._capturing = False

    # --------------------------------------------------------------- addresses

    @property
    def info_port(self) -> int:
        """Return the port ``/info`` and ``/ctrl`` are served on."""
        return self.config.port or INFO_PORT

    @property
    def kobra_x(self) -> bool:
        """Return ``True`` for the Kobra X, which has commands of its own."""
        return self.model_id == MODEL_KOBRA_X

    @property
    def enclosed(self) -> bool:
        """Return ``True`` for the S1 models, with a chamber, its light and its fan."""
        return self.model_id in ENCLOSED_MODELS

    def _topic(self, source: str, kind: str) -> str:
        session = self._require_session()
        return f"{TOPIC_ROOT}/{source}/printer/{session.model_id}/{session.device_id}/{kind}"

    def _require_session(self) -> Session:
        if self._broker_session is None:
            raise UnreachableError("the session with the printer is not open")
        return self._broker_session

    # -------------------------------------------------------------- discovery

    @classmethod
    async def async_identify(cls, host: str, timeout: float) -> DiscoveryResult | None:
        """Ask ``host`` for ``/info``, which says what it is without changing anything."""
        try:
            async with aiohttp.ClientSession() as session:
                info = await asyncio.wait_for(fetch_info(session, host, INFO_PORT), timeout=timeout)
        except (ProtocolError, TimeoutError):
            return None
        if not info.get("modelId"):
            return None
        serial = str(info.get("cn") or "").strip()
        return DiscoveryResult(
            host=host,
            protocol=ProtocolId.ANYCUBIC_KOBRA,
            candidates=[ProtocolId.ANYCUBIC_KOBRA],
            model=str(info.get("modelName") or "") or None,
            model_id=str(info["modelId"]),
            firmware=str(info.get("version") or "") or None,
            evidence=[
                f"answered /info on port {INFO_PORT}",
                f"LAN mode is {'off' if info.get('ctrlType') == 'cloud' else 'on'}",
            ],
            lan_only=info.get("ctrlType") != "cloud",
            prefill={"serial": serial} if valid_serial(serial) else {},
        )

    @classmethod
    async def async_prepare_config(cls, config: PrinterConfig) -> PrinterConfig:
        """Check the printer is a supported Kobra in LAN mode and learn its serial.

        Only ``/info`` is asked. The signed handshake hands out a session, which
        waits until the entry is set up.
        """
        try:
            async with aiohttp.ClientSession() as session:
                info = await fetch_info(session, config.host, config.port or INFO_PORT)
            check_info(info)
        except ProtocolError as err:
            raise ConfigError(str(err)) from err
        serial = str(info.get("cn") or "").strip()
        if config.serial and serial and serial != config.serial:
            raise ConfigError(f"the printer at {config.host} reports serial {serial}, not {config.serial}")
        if valid_serial(serial):
            return config.with_overrides({"serial": serial})
        return config

    # --------------------------------------------------------------- lifecycle

    @property
    def _connected(self) -> bool:
        return self._client is not None and not self._client.closed

    async def async_setup(self) -> None:
        """Run the handshake, connect over TLS and ask for every report once."""
        if self._connected:
            return
        await self._async_close()

        info = await fetch_info(self._session, self.config.host, self.info_port)
        check_info(info)
        serial = str(info.get("cn") or "")
        if self.config.serial and serial and serial != self.config.serial:
            raise UnreachableError(
                f"{self.config.host} now answers for printer {serial}, not {self.config.serial}"
            )
        bundle, local_token = await self._async_ctrl(info)
        session = session_from(info, decrypt_bundle(bundle, str(info["token"]), local_token), self.config.host)
        self._set_model_id(session.model_id)

        context = insecure_tls_context()
        if session.client_cert and session.client_key:
            await asyncio.get_running_loop().run_in_executor(
                None, _load_client_chain, context, session.client_cert, session.client_key
            )
        client = MqttClient(self._on_message)
        try:
            await client.connect(
                session.broker_host,
                session.broker_port,
                client_id=f"ha-{uuid.uuid4().hex[:8]}",
                username=session.username,
                password=session.password,
                keepalive=KEEPALIVE,
                timeout=CONNECT_TIMEOUT,
                tls=context,
            )
        except MqttRefusedError as err:
            raise AuthError(f"the printer refused its own session: {err}") from err
        except MqttError as err:
            raise UnreachableError(f"cannot reach the printer's broker: {err}") from err

        self._client = client
        self._broker_session = session
        self._state = {}
        self._have_info.clear()
        self._silent_reads = 0
        try:
            await client.subscribe(
                [f"{TOPIC_ROOT}/printer/public/{session.model_id}/{session.device_id}/#"]
            )
            await self._async_query("peripherie")
            await self._async_query_all()
        except MqttError as err:
            await self._async_close()
            raise UnreachableError(f"the printer broke off the session: {err}") from err

    async def _async_ctrl(self, info: Mapping[str, Any]) -> tuple[str, str]:
        """Sign the control request and return the encrypted bundle and its IV."""
        token = str(info["token"])
        ts = int(time.time() * 1000)
        nonce = "".join(random.choices(string.ascii_letters + string.digits, k=6))  # noqa: S311
        did = "".join(random.choices(string.ascii_uppercase + string.digits, k=32))  # noqa: S311
        parts = urlsplit(str(info["ctrlInfoUrl"]))
        # The printer names itself by the address it believes it has; keep its port
        # and path, but go to the address the user gave, which is known to route.
        netloc = self.config.host if parts.port is None else f"{self.config.host}:{parts.port}"
        url = parts._replace(netloc=netloc).geturl()
        query = urlencode({"ts": ts, "nonce": nonce, "sign": sign(token, ts, nonce), "did": did})
        try:
            async with self._session.post(f"{url}?{query}", timeout=HTTP_TIMEOUT) as response:
                answer = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as err:
            raise UnreachableError(f"the printer's control request failed: {err}") from err
        data = _mapping(answer.get("data")) if isinstance(answer, Mapping) else {}
        if not isinstance(answer, Mapping) or answer.get("code") != 200 or not data.get("info"):
            detail = answer.get("message") if isinstance(answer, Mapping) else answer
            raise UnreachableError(f"the printer refused the control request: {detail}")
        return str(data["info"]), str(data.get("token") or "")

    async def async_teardown(self) -> None:
        """Stop the camera if this session started it, and close the session."""
        if self._capturing and self._connected:
            with suppress(ProtocolError, MqttError):
                await self._async_publish("web", "video", "stopCapture")
        await self._async_close()

    async def _async_close(self) -> None:
        client, self._client = self._client, None
        self._capturing = False
        if client is not None:
            await client.close()
        for future in (*self._answers.values(), self._files, self._video):
            if future is not None and not future.done():
                future.set_exception(UnreachableError("the session with the printer closed"))
        self._answers.clear()

    # --------------------------------------------------------------- messages

    def _on_message(self, topic: str, payload: bytes) -> None:
        """Fold one report into the state, by its type and never by its action."""
        parts = topic.split("/")
        if len(parts) < 2 or parts[-1] != "report":
            return
        kind = parts[-2]
        try:
            message = json.loads(payload.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return
        if not isinstance(message, Mapping):
            return
        self._reports += 1
        self._resolve_answer(kind, message)
        data = message.get("data")
        if kind == "video":
            url = _mapping(_mapping(data).get("urls")).get("rtspUrl")
            if url:
                self._stream_url = str(url)
            if self._video is not None and not self._video.done() and message.get("action") != "stopCapture":
                self._video.set_result(self._stream_url)
            return
        if kind == "file" and message.get("action") == "listLocal" and message.get("state") == "failed":
            # The refusal carries a msgid of the printer's own, so no request matches it.
            if self._files is not None and not self._files.done():
                self._files.set_exception(
                    CommandRejectedError(
                        f"the printer refused the file list: {message.get('code')} {message.get('msg')}",
                        code=message.get("code"),
                        reason=message.get("msg"),
                    )
                )
            return
        if not isinstance(data, Mapping):
            return
        if kind in self._awaiting:
            self._awaiting.discard(kind)
            if not self._awaiting:
                self._answered.set()
        if kind == "info":
            self._state = dict(data)
            self._have_info.set()
        elif kind == "tempature":
            temp = dict(_mapping(self._state.get("temp")))
            fold(temp, data)
            self._state["temp"] = temp
        elif kind == "fan":
            fold(self._state, {key: value for key, value in data.items() if key != "taskid"})
        elif kind == "print" and "progress" in data:
            project = dict(_mapping(self._state.get("project")))
            fold(project, data)
            self._state["project"] = project
        elif kind == "light":
            lights = data.get("lights")
            if isinstance(lights, Sequence) and not isinstance(lights, str):
                self._lights = [item for item in lights if isinstance(item, Mapping)]
        elif kind == "multiColorBox":
            self._boxes = merge_boxes(self._boxes, data)
        elif kind == "peripherie":
            self._peripherie = data
        elif kind == "file" and ("records" in data or "file_list" in data):
            if self._files is not None and not self._files.done():
                self._files.set_result(data)

    def _resolve_answer(self, kind: str, message: Mapping[str, Any]) -> None:
        """Hand a command's answer to the request waiting for it."""
        if "code" not in message:
            return
        msgid = message.get("msgid")
        key = msgid if isinstance(msgid, str) and msgid in self._answers else f"{kind}/{message.get('action')}"
        future = self._answers.get(key)
        if future is not None and not future.done():
            future.set_result(message)

    # --------------------------------------------------------------- requests

    async def _async_publish(
        self, source: str, kind: str, action: str, data: Any = None
    ) -> dict[str, Any]:
        client = self._client
        if client is None or client.closed:
            raise UnreachableError("the session with the printer is not open")
        body = message(kind, action, data)
        try:
            await client.publish(self._topic(source, kind), json.dumps(body, separators=(",", ":")).encode())
        except MqttError as err:
            raise UnreachableError(f"cannot send to the printer: {err}") from err
        return body

    async def _async_query(self, kind: str) -> None:
        await self._async_publish("web", kind, "getInfo" if kind == "multiColorBox" else "query")

    async def _async_query_all(self) -> None:
        kinds = [*QUERY_TYPES]
        if Capability.FILAMENT_SLOTS in self.capabilities:
            kinds.append("multiColorBox")
        self._awaiting = set(kinds)
        self._answered.clear()
        for kind in kinds:
            await self._async_query(kind)

    async def _async_command(self, kind: str, action: str, data: Any = None, *, source: str = "web") -> None:
        """Send a command, raising when the printer refuses it within the window.

        Silence is acceptance: an idle printer drops a job setting without answering,
        and the state rules keep those from being sent in the first place.
        """
        if not self._connected:
            await self.async_setup()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Mapping[str, Any]] = loop.create_future()
        body = message(kind, action, data)
        keys = (body["msgid"], f"{kind}/{action}")
        for key in keys:
            self._answers[key] = future
        client = self._client
        try:
            if client is None:
                raise UnreachableError("the session with the printer is not open")
            await client.publish(self._topic(source, kind), json.dumps(body, separators=(",", ":")).encode())
            answer = await asyncio.wait_for(asyncio.shield(future), timeout=ANSWER_WINDOW)
        except TimeoutError:
            return
        except MqttError as err:
            raise UnreachableError(f"cannot send to the printer: {err}") from err
        finally:
            for key in keys:
                if self._answers.get(key) is future:
                    del self._answers[key]
        code = _integer(answer.get("code"))
        if code not in (None, 200):
            reason = str(answer.get("msg") or "") or None
            raise CommandRejectedError(
                f"the printer refused {kind} {action}: {reason or f'code {code}'}", code=code, reason=reason
            )

    # ------------------------------------------------------------------- read

    async def _async_read(self) -> PrinterSnapshot:
        """Ask for every report and return the state folded from what arrived.

        A printer that sent nothing since the last few reads is gone, even when its
        socket still looks open, and the next read runs the handshake again.
        """
        if not self._connected:
            await self.async_setup()
        if self._reports == 0 and self._have_info.is_set():
            self._silent_reads += 1
            if self._silent_reads >= SILENT_POLLS:
                await self._async_close()
                raise UnreachableError("the printer stopped reporting")
        else:
            self._silent_reads = 0
        self._reports = 0
        first = not self._have_info.is_set()
        try:
            await self._async_query_all()
        except UnreachableError:
            await self._async_close()
            raise
        with suppress(TimeoutError):
            await asyncio.wait_for(self._answered.wait(), timeout=FIRST_INFO_TIMEOUT if first else INFO_WAIT)
        if first and not self._have_info.is_set():
            raise UnreachableError("the printer is connected but did not report its state")
        return self._snapshot()

    def _snapshot(self) -> PrinterSnapshot:
        state = self._state
        session = self._broker_session
        print_state = state_for(state)
        in_job = print_state in (PrintState.PREPARING, PrintState.PRINTING, PrintState.PAUSED)
        project = _mapping(state.get("project"))
        temp = _mapping(state.get("temp"))
        remaining = _number(project.get("remain_time"))
        elapsed = _number(project.get("print_time"))
        mode = _integer(state.get("print_speed_mode"))
        speed = SPEED_PERCENT_BY_MODE.get(mode) if mode is not None else None
        light_type = CHAMBER_LIGHT if self.enclosed else CAMERA_LIGHT
        light_on = any(
            _integer(item.get("type")) == light_type and _integer(item.get("status")) == 1
            for item in self._lights
        )
        camera = Capability.CAMERA_STREAM in self.capabilities
        if self._peripherie is not None and "camera" in self._peripherie:
            camera = camera and bool(_integer(self._peripherie.get("camera")))
        return PrinterSnapshot(
            protocol=ProtocolId.ANYCUBIC_KOBRA,
            connected=self._connected,
            capabilities=self.capabilities,
            print_state=print_state,
            progress=_percent(project.get("progress")) if in_job else None,
            current_layer=_integer(project.get("curr_layer")) if in_job else None,
            total_layers=(_integer(project.get("total_layers")) or None) if in_job else None,
            remaining=Seconds(remaining * 60) if in_job and remaining is not None else None,
            elapsed=Seconds(elapsed) if in_job and elapsed is not None else None,
            filename=str(project.get("filename") or "") or None if in_job else None,
            job_id=str(project.get("taskid") or "") or None if in_job else None,
            speed_factor=Percent(speed) if speed is not None else None,
            hotend=Temps(current=_celsius(temp.get("curr_nozzle_temp")), target=_celsius(temp.get("target_nozzle_temp"))),
            bed=Temps(current=_celsius(temp.get("curr_hotbed_temp")), target=_celsius(temp.get("target_hotbed_temp"))),
            chamber=Temps(current=_celsius(temp.get("curr_chamber_temp")))
            if Capability.CHAMBER_SENSOR in self.capabilities
            else Temps(),
            fans=Fans(
                model=_percent(state.get("fan_speed_pct")),
                auxiliary=_percent(state.get("aux_fan_speed_pct")) if self.enclosed else None,
            ),
            lights=frozenset({LightChannel.CHAMBER}) if light_on else frozenset(),
            camera=camera,
            filament=filament_system(self._boxes) if Capability.FILAMENT_SLOTS in self.capabilities else None,
            model=session.model_name if session else None,
            firmware=str(state.get("version") or "") or (session.firmware if session else None),
            serial=(session.serial or None) if session else None,
        )

    # ------------------------------------------------------------ state rules

    @property
    def block_rules(self) -> tuple[BlockRule, ...]:  # type: ignore[override]
        """Return this model's rules: job settings need a job except on the Kobra X."""
        rules = [
            BlockRule(frozenset({Command.PAUSE}), when=lambda s: s.print_state is not PrintState.PRINTING, reason="no print is running"),
            BlockRule(frozenset({Command.RESUME}), when=lambda s: s.print_state is not PrintState.PAUSED, reason="the print is not paused"),
            BlockRule(frozenset({Command.STOP}), when=lambda s: not job_active(s), reason="no print is running"),
            BlockRule(frozenset({Command.SET_SPEED}), when=lambda s: not job_active(s), reason=JOB_SETTING),
            BlockRule(frozenset({Command.HOME, Command.JOG, Command.START_PRINT}), when=not_idle, reason="the printer is not idle"),
        ]
        if not self.kobra_x:
            rules.append(
                BlockRule(
                    frozenset({Command.SET_HOTEND_TEMP, Command.SET_BED_TEMP, Command.SET_FAN_SPEED}),
                    when=lambda s: not job_active(s),
                    reason=JOB_SETTING,
                )
            )
        return (*rules, *DEFAULT_BLOCK_RULES)

    # --------------------------------------------------------------- commands

    async def _async_dispatch(self, command: Command, params: Mapping[str, Any]) -> None:
        handler = _DISPATCH.get(command)
        if handler is None:
            raise ProtocolError(f"the Anycubic Kobra adapter cannot dispatch {command.value}")
        await handler(self, params)

    async def _async_job(self, action: str) -> None:
        await self._async_command("print", action, {"taskid": "-1"})

    async def _async_pause(self, _params: Mapping[str, Any]) -> None:
        await self._async_job("pause")

    async def _async_resume(self, _params: Mapping[str, Any]) -> None:
        await self._async_job("resume")

    async def _async_stop(self, _params: Mapping[str, Any]) -> None:
        await self._async_job("stop")

    async def _async_job_setting(self, settings: Mapping[str, Any]) -> None:
        await self._async_command("print", "update", {"taskid": "-1", "settings": dict(settings)})

    async def _async_set_temperature(self, key: str, value: float) -> None:
        """Set one target: the Kobra X's own command, or a job setting elsewhere."""
        if not self.kobra_x:
            await self._async_job_setting({key: round(value)})
            return
        temp = _mapping(self._state.get("temp"))
        targets = {
            "target_nozzle_temp": round(_number(temp.get("target_nozzle_temp")) or 0),
            "target_hotbed_temp": round(_number(temp.get("target_hotbed_temp")) or 0),
            key: round(value),
        }
        kind = 0 if key == "target_nozzle_temp" else 1
        await self._async_command("tempature", "set", {"type": kind, **targets})
        # The command carries both targets, so the next one must start from this
        # one's, not from the last report, or setting the bed would undo the nozzle.
        self._state["temp"] = {**_mapping(self._state.get("temp")), **targets}

    async def _async_set_hotend_temp(self, params: Mapping[str, Any]) -> None:
        await self._async_set_temperature("target_nozzle_temp", float(params["value"]))

    async def _async_set_bed_temp(self, params: Mapping[str, Any]) -> None:
        await self._async_set_temperature("target_hotbed_temp", float(params["value"]))

    async def _async_set_fan(self, params: Mapping[str, Any]) -> None:
        channel = str(params.get("channel", "model"))
        value = round(float(params["value"]))
        if self.kobra_x:
            if channel != "model":
                raise ProtocolError("the Kobra X sets only its part-cooling fan")
            await self._async_command("fan", "setSpeed", {"fan_speed_pct": value})
            return
        keys = {"model": "fan_speed_pct"}
        if self.enclosed:
            keys.update({"auxiliary": "aux_fan_speed_pct", "chamber": "box_fan_level"})
        if channel not in keys:
            raise ProtocolError(f"this printer's {channel} fan cannot be set")
        await self._async_job_setting({keys[channel]: value})

    async def _async_set_speed(self, params: Mapping[str, Any]) -> None:
        """Pick the speed mode nearest the percentage: silent, standard or sport."""
        await self._async_job_setting({"print_speed_mode": speed_mode_for(float(params["value"]))})

    async def _async_set_light(self, params: Mapping[str, Any]) -> None:
        on = bool(params["on"])
        await self._async_command(
            "light",
            "control",
            {"type": CHAMBER_LIGHT if self.enclosed else CAMERA_LIGHT, "status": 1 if on else 0, "brightness": 100 if on else 0},
        )

    async def _async_home(self, params: Mapping[str, Any]) -> None:
        axes = str(params.get("axes", "XYZ")).upper()
        group = HOME_GROUPS.get(axes)
        if group is None:
            raise ProtocolError("the printer homes X and Y together, Z, or all three")
        await self._async_command("axis", "move", {"axis": group, "move_type": 2, "distance": 0})

    async def _async_jog(self, params: Mapping[str, Any]) -> None:
        distance = float(params["distance"])
        await self._async_command(
            "axis",
            "move",
            {"axis": JOG_AXES[str(params["axis"]).upper()], "move_type": 1 if distance > 0 else 0, "distance": distance_value(distance)},
        )

    async def _async_start_print(self, params: Mapping[str, Any]) -> None:
        """Start a stored file with the minimal request Rinkhals documents."""
        await self._async_command(
            "print",
            "start",
            {"taskid": "-1", "filename": str(params["filename"]).lstrip("/"), "filetype": 1},
            source="slicer",
        )

    async def _async_set_auto_refill(self, params: Mapping[str, Any]) -> None:
        """Switch auto-feed on every attached unit, built in or not."""
        if not self._boxes:
            raise ProtocolError("no multi-colour unit is attached")
        on = 1 if params["on"] else 0
        await self._async_command(
            "multiColorBox", "setAutoFeed", {"multi_color_box": [{"id": box_id, "auto_feed": on} for box_id in sorted(self._boxes)]}
        )

    # ------------------------------------------------------------------ files

    async def async_list_files(self) -> Sequence[FileEntry]:
        """List the printer's own storage, as the slicer asks for it."""
        if not self._connected:
            await self.async_setup()
        future: asyncio.Future[Mapping[str, Any]] = asyncio.get_running_loop().create_future()
        self._files = future
        try:
            await self._async_publish("slicer", "file", "listLocal", dict(FILE_LIST_REQUEST))
            data = await asyncio.wait_for(future, timeout=FILE_LIST_TIMEOUT)
        except TimeoutError:
            raise ProtocolError("the printer did not answer the file list") from None
        finally:
            self._files = None
        return parse_file_list(data)

    async def async_upload_file(
        self, name: str, stream: AsyncIterator[bytes], *, size: int | None = None
    ) -> FileEntry:
        """Refuse: no source records the body the printer's upload endpoint takes."""
        raise ProtocolError(
            "uploading to an Anycubic Kobra is not supported yet: no source records "
            "the request its upload endpoint takes"
        )

    # ----------------------------------------------------------------- camera

    async def async_stream_source(self) -> str:
        """Start the capture the way the official client does, and return its URL.

        The newer firmware answers a start with a per-session stream URL, and does
        not start on a bare start, so the capture is stopped first.
        """
        if not self._connected:
            await self.async_setup()
        self._video = asyncio.get_running_loop().create_future()
        try:
            await self._async_publish("web", "video", "stopCapture")
            await asyncio.sleep(CAPTURE_KICK_DELAY)
            self._video = asyncio.get_running_loop().create_future()
            await self._async_publish("web", "video", "startCapture")
            self._capturing = True
            with suppress(TimeoutError):
                await asyncio.wait_for(self._video, timeout=VIDEO_REPORT_TIMEOUT)
        finally:
            self._video = None
        reported = self._stream_url or str(_mapping(self._state.get("urls")).get("rtspUrl") or "")
        if reported:
            parts = urlsplit(reported)
            if parts.hostname:
                port = f":{parts.port}" if parts.port else ""
                return parts._replace(netloc=f"{self.config.host}{port}").geturl()
        return f"http://{self.config.host}:{CAMERA_PORT}/flv"


#: ``Command`` to handler.
_DISPATCH: Final[Mapping[Command, Any]] = MappingProxyType(
    {
        Command.PAUSE: AnycubicKobraProtocol._async_pause,
        Command.RESUME: AnycubicKobraProtocol._async_resume,
        Command.STOP: AnycubicKobraProtocol._async_stop,
        Command.SET_HOTEND_TEMP: AnycubicKobraProtocol._async_set_hotend_temp,
        Command.SET_BED_TEMP: AnycubicKobraProtocol._async_set_bed_temp,
        Command.SET_FAN_SPEED: AnycubicKobraProtocol._async_set_fan,
        Command.SET_SPEED: AnycubicKobraProtocol._async_set_speed,
        Command.SET_LIGHT: AnycubicKobraProtocol._async_set_light,
        Command.HOME: AnycubicKobraProtocol._async_home,
        Command.JOG: AnycubicKobraProtocol._async_jog,
        Command.START_PRINT: AnycubicKobraProtocol._async_start_print,
        Command.SET_AUTO_REFILL: AnycubicKobraProtocol._async_set_auto_refill,
    }
)

"""Fingerprint a printer on the local network.

Discovery exists to save the user from guessing a protocol, not to guess one for
them. A probe that answers identifies a candidate, the config flow shows what it
found, and the user confirms. Nothing here sends a command to a printer, because a
wrong guess on a machine that treats an unknown command as fatal is how hardware
gets bricked.

Three probes are used together:

* a UDP broadcast carrying the SDCP discovery literal, which a Centauri Carbon
  answers with its mainboard id and firmware version;
* a UDP datagram carrying the JSON discovery request Elegoo's own slicer sends,
  which a Centauri Carbon 2 answers with its serial number, its model, and whether
  it is in LAN-only mode;
* a TCP connect plus a small HTTP fingerprint, which identifies Moonraker,
  OctoPrint and Duet from their own headers and endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from dataclasses import dataclass, field
from typing import Any, Final

import aiohttp

from .const import ProtocolId

_LOGGER = logging.getLogger(__name__)

SDCP_DISCOVERY_PORT: Final = 3000
SDCP_DISCOVERY_PROBE: Final = b"M99999"
CC2_DISCOVERY_PORT: Final = 52700
CC2_DISCOVERY_PROBE: Final = b'{"id": 0, "method": 7000}'
DISCOVERY_TIMEOUT: Final = 3.0
CONNECT_TIMEOUT: Final = 0.6
HTTP_TIMEOUT: Final = 3.0
MAX_RESPONSE_BYTES: Final = 65536

#: Ports worth a connect probe, with the protocol each one suggests. A port that
#: answers is a hint, never a conclusion.
CANDIDATE_PORTS: Final[tuple[tuple[int, ProtocolId], ...]] = (
    (7125, ProtocolId.MOONRAKER),
    (5000, ProtocolId.OCTOPRINT),
    (80, ProtocolId.WEB_ONLY),
    (3030, ProtocolId.SDCP_CC1),
    (1883, ProtocolId.ELEGOO_CC2),
)

#: HTTP fingerprints, checked against the first response body and headers.
HTTP_FINGERPRINTS: Final[tuple[tuple[str, ProtocolId], ...]] = (
    ("octoprint", ProtocolId.OCTOPRINT),
    ("moonraker", ProtocolId.MOONRAKER),
    ("klipper", ProtocolId.MOONRAKER),
    ("duet", ProtocolId.DUET),
    ("reprap", ProtocolId.DUET),
    ("elegoo", ProtocolId.SDCP_CC1),
)


@dataclass(slots=True)
class DiscoveryResult:
    """What a probe learned about one host."""

    host: str
    protocol: ProtocolId
    #: Every protocol whose hint was seen, best guess first.
    candidates: list[ProtocolId] = field(default_factory=list)
    mainboard_id: str | None = None
    firmware: str | None = None
    model: str | None = None
    open_ports: list[int] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    #: Whether the printer says it serves local clients only. ``None`` when the
    #: protocol does not report a network mode.
    lan_only: bool | None = None
    #: Whether the printer says an access code is set. ``None`` when unknown.
    access_code_set: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe summary for the config flow."""
        return {
            "host": self.host,
            "protocol": self.protocol.value,
            "candidates": [item.value for item in self.candidates],
            "mainboard_id": self.mainboard_id,
            "firmware": self.firmware,
            "model": self.model,
            "open_ports": self.open_ports,
            "evidence": self.evidence,
            "lan_only": self.lan_only,
            "access_code_set": self.access_code_set,
        }


class _JsonDiscoveryProtocol(asyncio.DatagramProtocol):
    """Collect the first JSON object a printer sends back to a discovery probe."""

    def __init__(self, accept: Any = None) -> None:
        """Create an empty reply holder, optionally filtering replies with ``accept``."""
        self.reply: dict[str, Any] | None = None
        self.sender: str | None = None
        self.done: asyncio.Event = asyncio.Event()
        self._accept = accept

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Store the first decodable reply."""
        try:
            payload = json.loads(data.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or self.reply is not None:
            return
        if self._accept is not None and not self._accept(payload):
            return
        self.reply = payload
        self.sender = addr[0]
        self.done.set()

    def error_received(self, exc: Exception) -> None:
        """Record a transport error as "no reply"."""
        _LOGGER.debug("discovery transport error: %s", exc)


async def _async_probe_udp(
    probe: bytes,
    target: tuple[str, int],
    timeout: float,
    accept: Any = None,
) -> tuple[dict[str, Any], str] | None:
    """Send one datagram and return the first JSON reply with its sender address."""
    loop = asyncio.get_running_loop()
    transport = None
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _JsonDiscoveryProtocol(accept),
            local_addr=("0.0.0.0", 0),
            allow_broadcast=True,
            family=socket.AF_INET,
        )
        transport.sendto(probe, target)
    except OSError as err:
        _LOGGER.debug("discovery could not send to %s: %s", target, err)
        if transport is not None:
            transport.close()
        return None

    try:
        await asyncio.wait_for(protocol.done.wait(), timeout=timeout)
    except TimeoutError:
        return None
    finally:
        transport.close()

    if protocol.reply is None:
        return None
    return protocol.reply, protocol.sender or ""


async def async_discover_sdcp(timeout: float = DISCOVERY_TIMEOUT) -> DiscoveryResult | None:
    """Broadcast the Elegoo discovery probe and parse the first reply."""
    answer = await _async_probe_udp(
        SDCP_DISCOVERY_PROBE, ("255.255.255.255", SDCP_DISCOVERY_PORT), timeout
    )
    if answer is None:
        return None
    reply, _sender = answer

    data = reply.get("Data") if isinstance(reply.get("Data"), dict) else reply
    if not isinstance(data, dict):
        return None

    mainboard = str(data.get("MainboardID") or "") or None
    return DiscoveryResult(
        host=str(data.get("MainboardIP") or "") or _reply_host(reply),
        protocol=ProtocolId.SDCP_CC1,
        candidates=[ProtocolId.SDCP_CC1],
        mainboard_id=mainboard,
        firmware=str(data.get("FirmwareVersion") or "") or None,
        model=str(data.get("MachineName") or data.get("Name") or "") or None,
        evidence=["answered the UDP discovery probe on port 3000"],
    )


def _reply_host(reply: dict[str, Any]) -> str:
    data = reply.get("Data")
    if isinstance(data, dict):
        return str(data.get("MainboardIP") or "")
    return ""


def _flag(value: Any) -> bool | None:
    """Read a discovery flag the printer may send as an integer or a boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return None


def parse_cc2_reply(reply: dict[str, Any], sender: str) -> DiscoveryResult | None:
    """Return what a Centauri Carbon 2 said about itself, or ``None`` for anything else.

    The reply carries no address of its own, so the sender of the datagram is the
    printer's address. Measured on a live printer::

        {"id": 0, "result": {"host_name": "CC2 QAZJ", "lan_status": 0,
         "machine_model": "Centauri Carbon 2", "protocol_version": "1.0.0",
         "sn": "F01BXKSWL13QAZJ", "token_status": 0}}
    """
    result = reply.get("result")
    if not isinstance(result, dict):
        return None
    serial = str(result.get("sn") or "").strip()
    if not serial or not sender:
        return None
    lan_only = _flag(result.get("lan_status"))
    access_code_set = _flag(result.get("token_status"))
    evidence = [f"answered the UDP discovery probe on port {CC2_DISCOVERY_PORT}"]
    if lan_only is False:
        evidence.append("the printer is in cloud mode, not LAN-only mode")
    return DiscoveryResult(
        host=sender,
        protocol=ProtocolId.ELEGOO_CC2,
        candidates=[ProtocolId.ELEGOO_CC2],
        mainboard_id=serial,
        firmware=None,
        model=str(result.get("machine_model") or "") or None,
        evidence=evidence,
        lan_only=lan_only,
        access_code_set=access_code_set,
    )


def _is_cc2_reply(payload: dict[str, Any]) -> bool:
    return isinstance(payload.get("result"), dict) and "sn" in payload["result"]


async def async_discover_cc2(
    host: str | None = None, timeout: float = DISCOVERY_TIMEOUT
) -> DiscoveryResult | None:
    """Ask for a Centauri Carbon 2, on one host or by broadcast.

    This is the discovery request Elegoo's own slicer sends. It is answered even
    while another client holds a session, and it is the only way to learn the
    serial number every MQTT topic is built from.
    """
    target = (host or "255.255.255.255", CC2_DISCOVERY_PORT)
    answer = await _async_probe_udp(CC2_DISCOVERY_PROBE, target, timeout, _is_cc2_reply)
    if answer is None:
        return None
    reply, sender = answer
    return parse_cc2_reply(reply, sender)


async def async_probe_ports(host: str) -> list[int]:
    """Return every candidate port that accepts a TCP connection."""

    async def probe(port: int) -> int | None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT
            )
        except (OSError, TimeoutError):
            return None
        writer.close()
        del reader
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return port

    results = await asyncio.gather(*(probe(port) for port, _ in CANDIDATE_PORTS))
    return [port for port in results if port is not None]


async def async_fingerprint_http(host: str, port: int) -> tuple[ProtocolId | None, list[str]]:
    """Ask a port for its front page and match the body against known products."""
    url = f"http://{host}:{port}/"
    evidence: list[str] = []
    body = ""
    headers: dict[str, str] = {}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        ) as session:
            async with session.get(url, allow_redirects=True) as response:
                body = (await response.content.read(MAX_RESPONSE_BYTES)).decode(
                    "utf-8", "replace"
                )
                headers = {key.lower(): value for key, value in response.headers.items()}
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("HTTP fingerprint of %s failed: %s", url, err)
        return None, evidence

    haystack = f"{body}\n{' '.join(f'{k}: {v}' for k, v in headers.items())}".lower()
    for marker, protocol in HTTP_FINGERPRINTS:
        if marker in haystack:
            evidence.append(f"{url} mentions {marker!r}")
            return protocol, evidence
    if "server" in headers:
        evidence.append(f"{url} answered with Server: {headers['server']}")
    return None, evidence


async def async_discover_host(host: str) -> DiscoveryResult | None:
    """Probe one host and return the best candidate, or ``None`` when nothing answers.

    A Centauri Carbon 2 identifies itself over UDP, which outranks every port hint:
    its broker port is plain MQTT and says nothing about who is behind it.
    """
    ports, cc2 = await asyncio.gather(async_probe_ports(host), async_discover_cc2(host))
    if cc2 is not None:
        cc2.open_ports = ports
        return cc2
    if not ports:
        return None

    evidence = [f"open ports: {', '.join(str(port) for port in ports)}"]
    candidates: list[ProtocolId] = []
    for port, hint in CANDIDATE_PORTS:
        if port in ports and hint not in candidates:
            candidates.append(hint)

    # An HTTP fingerprint outranks a bare open port, because two products can share
    # a port while only one of them answers to its own name.
    for port in (7125, 5000, 80, 443):
        if port not in ports and not (port == 443):
            continue
        protocol, note = await async_fingerprint_http(host, port)
        evidence.extend(note)
        if protocol is not None:
            candidates = [protocol, *[item for item in candidates if item != protocol]]
            break

    if not candidates:
        return None

    return DiscoveryResult(
        host=host,
        protocol=candidates[0],
        candidates=candidates,
        open_ports=ports,
        evidence=evidence,
    )

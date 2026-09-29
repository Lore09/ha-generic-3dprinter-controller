"""Find printers on the network without naming a protocol. Read-only: nothing here sends a
command, since a wrong guess can crash a printer. Self-identified > HTTP fingerprint > open port."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import aiohttp

from .const import ProtocolId

if TYPE_CHECKING:
    from .registry import AdapterRegistration

_LOGGER = logging.getLogger(__name__)

DISCOVERY_TIMEOUT: Final = 3.0
CONNECT_TIMEOUT: Final = 0.6
HTTP_TIMEOUT: Final = 3.0
MAX_RESPONSE_BYTES: Final = 65536

#: Ports whose front page is worth fingerprinting, in the order they are tried.
HTTP_PORTS: Final[tuple[int, ...]] = (7125, 5000, 80, 443)


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
    #: The printer's own model id, for a protocol with model profiles.
    model_id: str | None = None
    open_ports: list[int] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    #: Whether the printer says it serves local clients only. ``None`` when the
    #: protocol does not report a network mode.
    lan_only: bool | None = None
    #: Whether the printer says an access code is set. ``None`` when unknown.
    access_code_set: bool | None = None
    #: Configuration the printer told us, such as its serial number, for the form.
    prefill: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe summary for the config flow."""
        return {
            "host": self.host,
            "protocol": self.protocol.value,
            "candidates": [item.value for item in self.candidates],
            "mainboard_id": self.mainboard_id,
            "firmware": self.firmware,
            "model": self.model,
            "model_id": self.model_id,
            "open_ports": self.open_ports,
            "evidence": self.evidence,
            "lan_only": self.lan_only,
            "access_code_set": self.access_code_set,
            "prefill": dict(self.prefill),
        }


# ------------------------------------------------------------------ UDP probe


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


async def async_probe_udp(
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


# ------------------------------------------------------------ TCP and HTTP hints


async def async_probe_ports(host: str, candidates: Sequence[int]) -> list[int]:
    """Return every candidate port that accepts a TCP connection, in order."""

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

    results = await asyncio.gather(*(probe(port) for port in candidates))
    return [port for port in results if port is not None]


async def async_fingerprint_http(
    host: str, port: int, markers: Sequence[tuple[str, ProtocolId]]
) -> tuple[ProtocolId | None, list[str]]:
    """Ask a port for its front page and match it against each protocol's markers."""
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
    for marker, protocol in markers:
        if marker in haystack:
            evidence.append(f"{url} mentions {marker!r}")
            return protocol, evidence
    if "server" in headers:
        evidence.append(f"{url} answered with Server: {headers['server']}")
    return None, evidence


# --------------------------------------------------------------------- engine


async def async_discover_all(
    registrations: Iterable[AdapterRegistration], timeout: float = DISCOVERY_TIMEOUT
) -> list[DiscoveryResult]:
    """Broadcast for every protocol's printers, one result per host (first registered wins)."""
    ordered = list(registrations)
    answers = await asyncio.gather(
        *(registration.adapter.async_discover(timeout) for registration in ordered),
        return_exceptions=True,
    )
    found: dict[str, DiscoveryResult] = {}
    for registration, answer in zip(ordered, answers, strict=True):
        if isinstance(answer, BaseException):
            _LOGGER.debug("%s discovery failed: %s", registration.id.value, answer)
            continue
        for result in answer:
            found.setdefault(result.host, result)
    return list(found.values())


async def async_identify_host(
    registrations: Iterable[AdapterRegistration], host: str, timeout: float = DISCOVERY_TIMEOUT
) -> DiscoveryResult | None:
    """Return the best guess for one host: an adapter's own probe first, then ports and pages."""
    ordered = list(registrations)
    answers = await asyncio.gather(
        *(registration.adapter.async_identify(host, timeout) for registration in ordered),
        return_exceptions=True,
    )
    for registration, answer in zip(ordered, answers, strict=True):
        if isinstance(answer, BaseException):
            _LOGGER.debug("%s did not identify %s: %s", registration.id.value, host, answer)
        elif answer is not None:
            return answer

    hints: list[tuple[int, ProtocolId]] = [
        (port, registration.id) for registration in ordered for port in registration.ports
    ]
    candidate_ports = list(dict.fromkeys(port for port, _ in hints))
    open_ports = await async_probe_ports(host, candidate_ports)
    if not open_ports:
        return None

    evidence = [f"open ports: {', '.join(str(port) for port in open_ports)}"]
    candidates: list[ProtocolId] = []
    for port, protocol in hints:
        if port in open_ports and protocol not in candidates:
            candidates.append(protocol)

    markers = [(marker, registration.id) for registration in ordered for marker in registration.http_markers]
    for port in HTTP_PORTS:
        if port not in open_ports:
            continue
        protocol, note = await async_fingerprint_http(host, port, markers)
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
        open_ports=open_ports,
        evidence=evidence,
    )

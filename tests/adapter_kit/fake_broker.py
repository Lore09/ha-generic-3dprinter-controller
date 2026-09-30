"""A loopback MQTT 3.1.1 broker (QoS 0, TCP or TLS) for fakes of printers that host one.
The printer fake decides, through two callbacks, what a connection may do."""

from __future__ import annotations

import asyncio
import datetime
import json
import ssl
import struct
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from custom_components.generic_3dprinter.mqtt_client import (
    CONNACK,
    CONNECT,
    DISCONNECT,
    PINGREQ,
    PINGRESP,
    PUBLISH,
    SUBACK,
    SUBSCRIBE,
    decode_publish,
    encode_publish,
    packet,
    read_packet,
)


class BrokerSession:
    """One client connection."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        """Wrap the connection's writer."""
        self.writer = writer
        self.client_id = ""
        self.username: str | None = None
        self.topics: set[str] = set()

    async def send(self, data: bytes) -> None:
        """Write one packet, ignoring a client that has already gone."""
        with suppress(OSError, RuntimeError):
            self.writer.write(data)
            await self.writer.drain()


#: ``(session, password) -> CONNACK return code``; 0 accepts the connection.
Authenticate = Callable[[BrokerSession, str | None], int]
#: ``(session, topic, payload)``, awaited for every PUBLISH a client sends.
OnPublish = Callable[[BrokerSession, str, bytes], Awaitable[None]]
#: ``(session)``, called when an accepted connection ends for any reason.
OnDisconnect = Callable[[BrokerSession], None]


def topic_matches(pattern: str, topic: str) -> bool:
    """Return ``True`` when ``topic`` matches a subscription ``pattern``."""
    wanted = pattern.split("/")
    parts = topic.split("/")
    for index, level in enumerate(wanted):
        if level == "#":
            return True
        if index >= len(parts):
            return False
        if level not in ("+", parts[index]):
            return False
    return len(wanted) == len(parts)


def self_signed_context(directory: Path) -> ssl.SSLContext:
    """Return a server context with a fresh self-signed certificate."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-printer")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "broker.crt"
    key_path = directory / "broker.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


def parse_connect(body: bytes) -> tuple[str, str | None, str | None]:
    """Return ``(client_id, username, password)`` from a CONNECT body."""
    offset = 2 + struct.unpack("!H", body[:2])[0]
    flags = body[offset + 1]
    offset += 4

    def string() -> str:
        nonlocal offset
        (length,) = struct.unpack("!H", body[offset : offset + 2])
        value = body[offset + 2 : offset + 2 + length].decode()
        offset += 2 + length
        return value

    client_id = string()
    username = string() if flags & 0x80 else None
    password = string() if flags & 0x40 else None
    return client_id, username, password


class FakeBroker:
    """A broker on 127.0.0.1 that hands every message to its printer."""

    def __init__(
        self,
        *,
        authenticate: Authenticate,
        on_publish: OnPublish,
        on_disconnect: OnDisconnect | None = None,
        tls: bool = False,
    ) -> None:
        """Create a stopped broker."""
        self._authenticate = authenticate
        self._on_publish = on_publish
        self._on_disconnect = on_disconnect
        self.tls = tls
        self.port = 0
        #: CONNECT packets received, accepted or not.
        self.connects = 0
        self.sessions: set[BrokerSession] = set()
        self._server: asyncio.base_events.Server | None = None
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._context: ssl.SSLContext | None = None

    async def start(self) -> None:
        """Listen, keeping the port of an earlier start so a restart looks like a reboot."""
        if self.tls and self._context is None:
            self._tempdir = tempfile.TemporaryDirectory()
            self._context = self_signed_context(Path(self._tempdir.name))
        self._server = await asyncio.start_server(
            self._serve, "127.0.0.1", self.port or 0, ssl=self._context
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Close every connection and stop listening."""
        for session in list(self.sessions):
            session.writer.close()
        self.sessions.clear()
        if self._server is not None:
            self._server.close()
            with suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=2)
            self._server = None

    def close(self) -> None:
        """Remove the certificate files, once the broker will not start again."""
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None
            self._context = None

    async def deliver(self, topic: str, message: dict[str, Any] | bytes) -> None:
        """Publish ``message`` to every session subscribed to ``topic``."""
        payload = message if isinstance(message, bytes) else json.dumps(message).encode()
        data = encode_publish(topic, payload)
        for session in list(self.sessions):
            if any(topic_matches(pattern, topic) for pattern in session.topics):
                await session.send(data)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = BrokerSession(writer)
        try:
            kind, _flags, body = await read_packet(reader)
            if kind != CONNECT:
                return
            session.client_id, session.username, password = parse_connect(body)
            self.connects += 1
            code = self._authenticate(session, password)
            await session.send(packet(CONNACK, 0, bytes([0, code])))
            if code:
                return
            self.sessions.add(session)
            while True:
                kind, flags, body = await read_packet(reader)
                if kind == SUBSCRIBE:
                    await self._subscribe(session, body)
                elif kind == PUBLISH:
                    topic, payload, _qos, _id = decode_publish(flags, body)
                    await self._on_publish(session, topic, payload)
                elif kind == PINGREQ:
                    await session.send(packet(PINGRESP, 0))
                elif kind == DISCONNECT:
                    return
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ssl.SSLError):
            return
        finally:
            if session in self.sessions and self._on_disconnect is not None:
                self._on_disconnect(session)
            self.sessions.discard(session)
            writer.close()

    async def _subscribe(self, session: BrokerSession, body: bytes) -> None:
        packet_id = body[:2]
        offset, granted = 2, b""
        while offset < len(body):
            (length,) = struct.unpack("!H", body[offset : offset + 2])
            session.topics.add(body[offset + 2 : offset + 2 + length].decode())
            offset += 2 + length + 1
            granted += b"\x00"
        await session.send(packet(SUBACK, 0, packet_id + granted))

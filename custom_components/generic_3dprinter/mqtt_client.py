"""A minimal MQTT 3.1.1 client (QoS 0, plain TCP or TLS) for printers that host their own
broker, which HA's single-broker MQTT integration cannot reach."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import ssl
import struct
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Final

_LOGGER = logging.getLogger(__name__)

CONNECT: Final = 1
CONNACK: Final = 2
PUBLISH: Final = 3
PUBACK: Final = 4
SUBSCRIBE: Final = 8
SUBACK: Final = 9
PINGREQ: Final = 12
PINGRESP: Final = 13
DISCONNECT: Final = 14

#: The largest packet this client accepts. A printer's full status is a few
#: kilobytes; anything near this size is a broken stream, not a message.
MAX_PACKET_BYTES: Final = 4 * 1024 * 1024

#: CONNACK return codes, as the MQTT 3.1.1 specification names them.
CONNACK_REASONS: Final = {
    1: "unacceptable protocol version",
    2: "client identifier rejected",
    3: "server unavailable",
    4: "bad user name or password",
    5: "not authorised",
}


class MqttError(Exception):
    """The broker could not be reached or broke the protocol."""


class MqttRefusedError(MqttError):
    """The broker answered CONNECT with a non-zero return code."""

    def __init__(self, code: int) -> None:
        """Keep the return code, so a bad password can be told from anything else."""
        super().__init__(
            f"the broker refused the connection: {CONNACK_REASONS.get(code, f'code {code}')}"
        )
        self.code = code

    @property
    def bad_credentials(self) -> bool:
        """Return ``True`` when the refusal is about the user name or password."""
        return self.code in (4, 5)


def insecure_tls_context() -> ssl.SSLContext:
    """Return a TLS context that checks neither name nor certificate: printers self-sign.
    The session is still encrypted, and the peer's fingerprint is kept on the client."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


# ------------------------------------------------------------------ encoding


def encode_length(length: int) -> bytes:
    """Encode an MQTT remaining-length field."""
    if length < 0:
        raise ValueError("a length cannot be negative")
    out = bytearray()
    while True:
        digit, length = length % 128, length // 128
        if length:
            digit |= 0x80
        out.append(digit)
        if not length:
            return bytes(out)


def encode_string(value: str) -> bytes:
    """Encode a length-prefixed UTF-8 string."""
    data = value.encode("utf-8")
    return struct.pack("!H", len(data)) + data


def packet(kind: int, flags: int, body: bytes = b"") -> bytes:
    """Frame one control packet."""
    return bytes([(kind << 4) | (flags & 0x0F)]) + encode_length(len(body)) + body


def encode_connect(
    client_id: str, *, username: str | None, password: str | None, keepalive: int
) -> bytes:
    """Build a CONNECT packet with a clean session."""
    flags = 0x02
    payload = encode_string(client_id)
    if username is not None:
        flags |= 0x80
        payload += encode_string(username)
        if password is not None:
            flags |= 0x40
            payload += encode_string(password)
    header = encode_string("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive)
    return packet(CONNECT, 0, header + payload)


def encode_subscribe(packet_id: int, topics: Sequence[str]) -> bytes:
    """Build a SUBSCRIBE packet asking for QoS 0 on every topic."""
    body = struct.pack("!H", packet_id) + b"".join(
        encode_string(topic) + b"\x00" for topic in topics
    )
    return packet(SUBSCRIBE, 0x02, body)


def encode_publish(topic: str, payload: bytes) -> bytes:
    """Build a QoS 0 PUBLISH packet."""
    return packet(PUBLISH, 0, encode_string(topic) + payload)


def decode_publish(flags: int, body: bytes) -> tuple[str, bytes, int, int | None]:
    """Return ``(topic, payload, qos, packet_id)`` from a PUBLISH body."""
    if len(body) < 2:
        raise MqttError("a PUBLISH packet is too short to carry a topic")
    (topic_length,) = struct.unpack("!H", body[:2])
    offset = 2 + topic_length
    if len(body) < offset:
        raise MqttError("a PUBLISH packet is shorter than its topic")
    topic = body[2:offset].decode("utf-8", "replace")
    qos = (flags >> 1) & 0x03
    packet_id: int | None = None
    if qos:
        if len(body) < offset + 2:
            raise MqttError("a PUBLISH packet is missing its packet id")
        (packet_id,) = struct.unpack("!H", body[offset : offset + 2])
        offset += 2
    return topic, body[offset:], qos, packet_id


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    """Read one control packet and return ``(kind, flags, body)``."""
    first = (await reader.readexactly(1))[0]
    length = 0
    multiplier = 1
    for _ in range(4):
        digit = (await reader.readexactly(1))[0]
        length += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            break
        multiplier *= 128
    else:
        raise MqttError("a packet length field is longer than four bytes")
    if length > MAX_PACKET_BYTES:
        raise MqttError(f"a packet of {length} bytes exceeds the size cap")
    body = await reader.readexactly(length) if length else b""
    return first >> 4, first & 0x0F, body


# -------------------------------------------------------------------- client


class MqttClient:
    """One connection to one broker, delivering every message to a callback.

    The callback runs on the event loop inside the reader, so it must not block.
    The reader ends when the connection does, and :attr:`closed` then turns true.
    """

    def __init__(self, on_message: Callable[[str, bytes], None]) -> None:
        """Create an unconnected client."""
        self._on_message = on_message
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._packet_id = 0
        self._subacks: dict[int, asyncio.Future[bytes]] = {}
        self.last_error: str | None = None
        #: SHA-256 of the broker's certificate, hex, for a TLS session.
        self.peer_certificate_sha256: str | None = None

    @property
    def closed(self) -> bool:
        """Return ``True`` unless the connection is open and being read."""
        return self._writer is None or self._pump is None or self._pump.done()

    async def connect(
        self,
        host: str,
        port: int,
        *,
        client_id: str,
        username: str | None,
        password: str | None,
        keepalive: int = 60,
        timeout: float = 10.0,
        tls: ssl.SSLContext | None = None,
    ) -> None:
        """Connect and wait for CONNACK; ``tls`` is a context, or ``None`` for plain TCP."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=tls), timeout=timeout
            )
        except TimeoutError as err:
            raise MqttError(f"timed out connecting to {host}:{port}") from err
        except (OSError, ssl.SSLError) as err:
            raise MqttError(f"cannot connect to {host}:{port}: {err}") from err

        if tls is not None:
            ssl_object = writer.get_extra_info("ssl_object")
            certificate = ssl_object.getpeercert(binary_form=True) if ssl_object else None
            self.peer_certificate_sha256 = (
                hashlib.sha256(certificate).hexdigest() if certificate else None
            )

        try:
            writer.write(
                encode_connect(
                    client_id, username=username, password=password, keepalive=keepalive
                )
            )
            await writer.drain()
            kind, _flags, body = await asyncio.wait_for(read_packet(reader), timeout=timeout)
        except (OSError, asyncio.IncompleteReadError, TimeoutError, MqttError) as err:
            writer.close()
            raise MqttError(f"the broker did not accept the connection: {err!r}") from err

        if kind != CONNACK or len(body) < 2:
            writer.close()
            raise MqttError(f"expected CONNACK, received packet type {kind}")
        if body[1] != 0:
            writer.close()
            raise MqttRefusedError(body[1])

        self._reader, self._writer = reader, writer
        self._pump = asyncio.create_task(self._async_pump())

    async def subscribe(self, topics: Sequence[str], *, timeout: float = 10.0) -> None:
        """Subscribe at QoS 0 and wait for the broker to grant every topic."""
        self._packet_id = self._packet_id % 0xFFFF + 1
        packet_id = self._packet_id
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._subacks[packet_id] = future
        try:
            await self._write(encode_subscribe(packet_id, topics))
            granted = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as err:
            raise MqttError("the broker did not acknowledge the subscription") from err
        finally:
            self._subacks.pop(packet_id, None)
        if any(code == 0x80 for code in granted):
            raise MqttError("the broker refused a subscription")

    async def publish(self, topic: str, payload: bytes) -> None:
        """Publish one message at QoS 0."""
        await self._write(encode_publish(topic, payload))

    async def ping(self) -> None:
        """Send an MQTT keepalive ping."""
        await self._write(packet(PINGREQ, 0))

    async def close(self) -> None:
        """Say goodbye and close the connection. Idempotent."""
        writer, self._writer = self._writer, None
        pump, self._pump = self._pump, None
        if writer is not None:
            with suppress(OSError, RuntimeError):
                writer.write(packet(DISCONNECT, 0))
                await asyncio.wait_for(writer.drain(), timeout=2)
            writer.close()
            with suppress(OSError, TimeoutError, RuntimeError):
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
        if pump is not None and not pump.done():
            pump.cancel()
            with suppress(asyncio.CancelledError):
                await pump
        self._fail_subacks()

    async def _write(self, data: bytes) -> None:
        writer = self._writer
        if writer is None or self.closed:
            raise MqttError("the connection is closed")
        try:
            async with self._write_lock:
                writer.write(data)
                await writer.drain()
        except (OSError, RuntimeError) as err:
            raise MqttError(f"cannot write to the broker: {err}") from err

    async def _async_pump(self) -> None:
        """Read packets until the connection ends."""
        reader = self._reader
        if reader is None:
            return
        try:
            while True:
                kind, flags, body = await read_packet(reader)
                if kind == PUBLISH:
                    topic, payload, qos, packet_id = decode_publish(flags, body)
                    if qos == 1 and packet_id is not None:
                        await self._write(packet(PUBACK, 0, struct.pack("!H", packet_id)))
                    try:
                        self._on_message(topic, payload)
                    except Exception:  # noqa: BLE001 - one bad message must not end the session
                        _LOGGER.exception("error handling a message on %s", topic)
                elif kind == SUBACK and len(body) >= 2:
                    (packet_id,) = struct.unpack("!H", body[:2])
                    future = self._subacks.get(packet_id)
                    if future is not None and not future.done():
                        future.set_result(body[2:])
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, OSError, MqttError) as err:
            self.last_error = str(err) or type(err).__name__
            _LOGGER.debug("MQTT connection ended: %s", self.last_error)
        finally:
            self._fail_subacks()

    def _fail_subacks(self) -> None:
        for future in self._subacks.values():
            if not future.done():
                future.set_exception(MqttError("the connection closed"))
        self._subacks.clear()

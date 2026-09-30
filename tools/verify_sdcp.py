#!/usr/bin/env python3
"""Probe an Elegoo Centauri Carbon (CC1) with the SDCP protocol.

Standard library only, so it runs anywhere without a venv. It exercises the
read path from ``docs/research/elegoo-centauri-carbon-sdcp.md`` and prints the
raw frames so a reviewer can check the report against a real printer.

Read-only by default. ``--start-print`` and ``--upload`` are opt-in because the
Home Assistant integration reports Cmd 128 crashing this model. It stops at a resin
printer (Saturn, Mars), which must get none of this; use acceptance_sdcp_resin.py there.

Examples::

    python tools/verify_sdcp.py --discover
    python tools/verify_sdcp.py --host 192.168.1.209
    python tools/verify_sdcp.py --host 192.168.1.209 --snapshot frame.jpg
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import sys
import time
from typing import Any

WS_PORT = 3030
WS_PATH = "/websocket"
HTTP_PORT = 80
CAMERA_PORT = 3031
CAMERA_PATH = "/video"
DISCOVERY_PORT = 3000
DISCOVERY_PROBE = b"M99999"

STATUS = 0
ATTRIBUTES = 1
START_PRINT = 128
PAUSE_PRINT = 129
STOP_PRINT = 130
RESUME_PRINT = 131
GET_FILE_LIST = 258
DELETE_FILES = 259
PRINT_HISTORY = 320
PRINT_HISTORY_DETAIL = 321
SET_PARAMS = 403
SUBSCRIBE = 512

MACHINE_STATE = {
    0: "IDLE", 1: "PRINTING", 2: "FILE_TRANSFERRING", 3: "EXPOSURE_TESTING",
    4: "PRINTERS_TESTING", 5: "AUTO_LEVEL", 6: "RESONANCE_TESTING",
    7: "OTHERS_BUSY", 8: "FILE_CHECKING", 9: "HOMING", 10: "FEED_OUT",
    11: "PID_DETECT",
}

PRINT_STATE = {
    0: "IDLE", 1: "HOMING", 2: "DROPPING", 3: "EXPOSING", 4: "LIFTING",
    5: "PAUSING", 6: "PAUSED", 7: "STOPPING", 8: "STOPPED", 9: "COMPLETED",
    10: "FILE_CHECKING", 11: "PRINTERS_CHECKING", 12: "RESUMING",
    13: "PRINTING", 14: "ERROR", 15: "AUTO_LEVELING", 16: "PREHEATING",
    17: "RESONANCE_TESTING", 18: "PRINT_START", 19: "AUTO_LEVELING_COMPLETED",
    20: "PREHEATING_COMPLETED", 21: "HOMING_COMPLETED",
    22: "RESONANCE_TESTING_COMPLETED", 23: "AUTO_FEEDING", 24: "UNLOADING",
    25: "UNLOADING_ABNORMAL", 26: "UNLOADING_PAUSED",
}

ACK = {
    0: "OK", 1: "BUSY", 2: "FILE_NOT_FOUND", 3: "MD5_FAILED",
    4: "FILEIO_FAILED", 5: "INVALID_RESOLUTION", 6: "UNKNOWN_FORMAT",
    7: "UNKNOWN_MODEL",
}

SOI, EOI = b"\xff\xd8", b"\xff\xd9"

#: What only a resin printer reports, as in adapters/sdcp.py, inlined to stay stdlib only.
RESIN_FILE_TYPES = frozenset({"ctb", "cbddlp", "goo", "prz"})
FDM_FILE_TYPES = frozenset({"gcode"})
RESIN_ATTRIBUTES = ("ReleaseFilmMax", "Resolution")
RESIN_STATUS_KEYS = ("TempOfUVLED", "ReleaseFilm", "TempOfTank")
FDM_STATUS_KEYS = ("TempOfNozzle", "TempOfHotbed")


def log(msg: str) -> None:
    print(msg, flush=True)


def section(title: str) -> None:
    log("")
    log(f"== {title} ==")


def is_resin(attrs: Any) -> bool:
    """Whether SDCP attributes, flat or with nested Attributes and Status, are a resin printer's.
    The same order as classify_sdcp: file types, resin-only attributes, status, then the name."""
    if not isinstance(attrs, dict):
        return False
    status = attrs.get("Status") if isinstance(attrs.get("Status"), dict) else {}
    if isinstance(attrs.get("Attributes"), dict):
        attrs = attrs["Attributes"]
    types = attrs.get("SupportFileType")
    types = [types] if isinstance(types, str) else types if isinstance(types, list) else []
    lowered = {str(item).strip().lower() for item in types}
    if lowered & RESIN_FILE_TYPES or lowered & FDM_FILE_TYPES:
        return bool(lowered & RESIN_FILE_TYPES)
    devices = attrs.get("DevicesStatus")
    if any(key in attrs for key in RESIN_ATTRIBUTES) or (isinstance(devices, dict) and "LCDStatus" in devices):
        return True
    if any(key in status for key in RESIN_STATUS_KEYS + FDM_STATUS_KEYS):
        return any(key in status for key in RESIN_STATUS_KEYS)
    name = str(attrs.get("MachineName") or "").lower()
    return "saturn" in name or "mars" in name


def refuse_resin(host: str, attrs: Any) -> bool:
    """Log why and return True when ``attrs`` are a resin printer's."""
    if not is_resin(attrs):
        return False
    log(f"refused: {host} is a resin printer, and this tool sends Cmd 512, and Cmd 128 or an "
        "upload when asked. Use tools/acceptance_sdcp_resin.py instead")
    return True


# --- discovery ------------------------------------------------------------


def discover(timeout: float = 3.0, retries: int = 3) -> list[dict[str, Any]]:
    """Broadcast M99999 on UDP 3000 and collect replies."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(timeout / max(retries, 1) / 2)
    found: dict[str, dict[str, Any]] = {}
    try:
        for _ in range(retries):
            sock.sendto(DISCOVERY_PROBE, ("255.255.255.255", DISCOVERY_PORT))
            deadline = time.monotonic() + timeout / max(retries, 1) / 2
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    break
                try:
                    payload = json.loads(data.decode("utf-8", "replace"))
                except ValueError:
                    continue
                inner = payload.get("Data") or {}
                found[addr[0]] = {
                    "host": addr[0],
                    "mainboard_id": inner.get("MainboardID"),
                    "name": inner.get("Name"),
                    "model": inner.get("MachineName"),
                    "firmware": inner.get("FirmwareVersion"),
                    "raw": payload,
                }
    finally:
        sock.close()
    return list(found.values())


# --- websocket over raw sockets ------------------------------------------


class WsError(RuntimeError):
    pass


class Ws:
    """Minimal RFC 6455 text client. Enough for the printer, nothing more."""

    def __init__(self, host: str, port: int, path: str, timeout: float) -> None:
        self.host, self.port, self.path = host, port, path
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.buf = bytearray()

    def __enter__(self) -> Ws:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock.settimeout(self.timeout)
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode())
        head = self._read_until(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("utf-8", "replace")
        if "101" not in status_line:
            # The printer answers HTTP 500 with the body "too many client"
            # once its WebSocket slots are exhausted.
            extra = b""
            try:
                extra = self.sock.recv(256)
            except OSError:
                pass
            self.close()
            raise WsError(
                f"handshake failed: {status_line!r} body={extra.decode('utf-8', 'replace')!r}"
            )
        log(f"ws handshake ok: {status_line}")

    def _read_until(self, marker: bytes) -> bytes:
        assert self.sock is not None
        while marker not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WsError("connection closed during handshake")
            self.buf.extend(chunk)
        idx = self.buf.index(marker) + len(marker)
        head, self.buf = bytes(self.buf[:idx]), self.buf[idx:]
        return head

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WsError("connection closed")
            self.buf.extend(chunk)
        out, self.buf = bytes(self.buf[:n]), self.buf[n:]
        return out

    def send_text(self, text: str) -> None:
        assert self.sock is not None
        payload = text.encode()
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self, timeout: float | None = None) -> str | None:
        """Return one text frame, or None on timeout. Answers pings."""
        assert self.sock is not None
        self.sock.settimeout(self.timeout if timeout is None else timeout)
        while True:
            try:
                b1, b2 = self._recv_exact(2)
            except (socket.timeout, TimeoutError):
                return None
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            data = self._recv_exact(length)
            if opcode == 0x8:
                raise WsError("server closed the socket")
            if opcode == 0x9:
                continue
            if opcode == 0xA:
                continue
            text = data.decode("utf-8", "replace")
            # Some firmware prefixes frames with a decimal length.
            stripped = text.lstrip()
            if stripped and stripped[0].isdigit():
                brace = stripped.find("{")
                if brace > 0:
                    text = stripped[brace:]
            return text

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


# --- frames ---------------------------------------------------------------


def build_frame(cmd: int, data: dict[str, Any] | None, mid: str) -> tuple[str, str]:
    """Return (json_text, request_id). Milliseconds, matching the Elegoo SDK."""
    request_id = secrets.token_hex(8)
    envelope = {
        "Id": mid,
        "Data": {
            "Cmd": int(cmd),
            "Data": data or {},
            "RequestID": request_id,
            "MainboardID": mid,
            "TimeStamp": int(time.time() * 1000),
            "From": 0,
        },
        "Topic": f"sdcp/request/{mid}",
    }
    return json.dumps(envelope, separators=(",", ":")), request_id


def wait_for(ws: Ws, request_id: str, timeout: float = 10.0) -> dict[str, Any] | None:
    """Read frames until the response matching request_id arrives."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = ws.recv_text(timeout=max(0.1, deadline - time.monotonic()))
        if text is None:
            return None
        try:
            msg = json.loads(text)
        except ValueError:
            log(f"  unparseable frame: {text[:200]!r}")
            continue
        topic = msg.get("Topic", "")
        inner = msg.get("Data") if isinstance(msg.get("Data"), dict) else {}
        if topic.endswith(request_id) or inner.get("RequestID") == request_id:
            return msg
        if "sdcp/response" in topic:
            log(f"  (response for another RequestID: {inner.get('RequestID')})")
        elif "sdcp/status" in topic:
            log("  (status push received)")
        elif "sdcp/attributes" in topic:
            log("  (attributes push received)")
    return None


def request(ws: Ws, cmd: int, data: dict[str, Any] | None, mid: str,
            timeout: float = 10.0, collect: float = 0.0) -> dict[str, Any] | None:
    """Send a command and return its response.

    ``collect`` keeps reading matching frames for that many extra seconds and
    returns the first one that carries a payload, because the printer splits a
    file list across an Ack frame and a data frame.
    """
    text, request_id = build_frame(cmd, data, mid)
    log(f"--> Cmd {cmd} RequestID {request_id} Data {json.dumps(data or {})}")
    ws.send_text(text)
    msg = wait_for(ws, request_id, timeout)
    if collect <= 0:
        return msg

    def score(candidate: dict[str, Any] | None) -> int:
        inner = ((candidate or {}).get("Data") or {}).get("Data") or {}
        payload = (inner.get("Data") if isinstance(inner.get("Data"), dict) else None) or inner
        return sum(1 for key in ("FileList", "HistoryData", "HistoryDetailList",
                                 "canvas_list", "VideoUrl") if key in payload)

    best = msg
    deadline = time.monotonic() + collect
    while time.monotonic() < deadline:
        candidate = wait_for(ws, request_id, max(0.1, deadline - time.monotonic()))
        if candidate is None:
            break
        if score(candidate) > score(best):
            best = candidate
        if score(candidate) > 0:
            break
    return best


def ack_of(msg: dict[str, Any] | None) -> int | None:
    if not msg:
        return None
    inner = msg.get("Data") or {}
    return (inner.get("Data") or {}).get("Ack")


def describe_ack(ack: int | None) -> str:
    if ack is None:
        return "no Ack received"
    return f"Ack={ack} ({ACK.get(ack, 'unknown')})"


# --- http (raw sockets, so no third-party deps) ---------------------------


def http_get(host: str, port: int, path: str, timeout: float = 10.0):
    """Yield (sock, status, headers, preloaded_body) for a simple GET.

    Tolerates a server that closes hard without a clean header terminator,
    which is what a Windows loopback peer does when it closes straight after
    writing. Missing status defaults to 200 so a streamed body still gets
    parsed rather than discarded.
    """
    sock = socket.create_connection((host, port), timeout)
    sock.settimeout(timeout)
    sock.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        "Connection: close\r\nAccept: */*\r\n\r\n".encode()
    )
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except (ConnectionResetError, socket.timeout, TimeoutError):
            break
        if not chunk:
            break
        buf.extend(chunk)
    if b"\r\n\r\n" not in buf:
        return sock, 200, {}, buf
    idx = buf.index(b"\r\n\r\n") + 4
    head = bytes(buf[:idx]).decode("utf-8", "replace")
    body = bytearray(buf[idx:])
    lines = head.split("\r\n")
    try:
        status = int(lines[0].split(" ", 2)[1])
    except (IndexError, ValueError):
        status = 200
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    return sock, status, headers, body


def grab_jpeg(host: str, port: int = CAMERA_PORT, path: str = CAMERA_PATH,
              cap: int = 8 * 1024 * 1024, timeout: float = 10.0) -> bytes:
    """Read one complete JPEG from the MJPEG stream, then close."""
    sock, status, headers, buf = http_get(host, port, path, timeout)
    try:
        log(f"camera HTTP {status} content-type={headers.get('content-type')!r}")
        if status != 200:
            raise WsError(f"camera returned HTTP {status}")
        start = None
        while True:
            if start is None:
                i = buf.find(SOI)
                if i >= 0:
                    start = i
            if start is not None:
                j = buf.find(EOI, start + 2)
                if j >= 0:
                    return bytes(buf[start:j + 2])
            if len(buf) > cap:
                raise WsError("frame exceeded the size cap")
            try:
                chunk = sock.recv(65536)
            except (ConnectionResetError, socket.timeout, TimeoutError) as err:
                raise WsError(f"camera stream ended early: {err}") from err
            if not chunk:
                raise WsError("stream ended before a complete JPEG arrived")
            buf.extend(chunk)
    finally:
        sock.close()


def upload_file(host: str, local_path: str, remote_name: str | None = None,
                port: int = HTTP_PORT, chunk_size: int = 1024 * 1024) -> str:
    """Chunked multipart POST to /uploadFile/upload. See report section 5."""
    name = remote_name or os.path.basename(local_path)
    total = os.path.getsize(local_path)
    if total == 0:
        raise WsError("refusing to upload an empty file")
    digest = hashlib.md5()
    with open(local_path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    file_md5 = digest.hexdigest()
    transfer_uuid = secrets.token_hex(16)
    log(f"upload {local_path} -> {name} ({total} bytes) md5 {file_md5}")
    offset = 0
    with open(local_path, "rb") as handle:
        while offset < total:
            chunk = handle.read(chunk_size)
            fields = {
                "Check": "1",
                "S-File-MD5": file_md5,
                "Offset": str(offset),
                "Uuid": transfer_uuid,
                "TotalSize": str(total),
            }
            body, content_type = _multipart(fields, "File", name, chunk)
            sock = socket.create_connection((host, port), 180)
            sock.sendall(
                f"POST /uploadFile/upload HTTP/1.1\r\nHost: {host}:{port}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            raw = bytearray()
            while True:
                piece = sock.recv(65536)
                if not piece:
                    break
                raw.extend(piece)
            sock.close()
            text = bytes(raw).decode("utf-8", "replace")
            status_line = text.split("\r\n", 1)[0]
            payload = text.split("\r\n\r\n", 1)[-1]
            try:
                parsed = json.loads(payload)
            except ValueError:
                parsed = {"raw": payload[:200]}
            ok = parsed.get("code") == "000000"
            log(f"  offset {offset}: {status_line} {parsed} ok={ok}")
            if not ok:
                raise WsError(f"upload rejected at offset {offset}: {parsed}")
            offset += len(chunk)
    return name


def _multipart(fields: dict[str, str], file_field: str, filename: str,
               content: bytes) -> tuple[bytes, str]:
    boundary = "----sdcp" + secrets.token_hex(8)
    parts = bytearray()
    for key, value in fields.items():
        parts.extend(f"--{boundary}\r\n".encode())
        parts.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        parts.extend(value.encode())
        parts.extend(b"\r\n")
    parts.extend(f"--{boundary}\r\n".encode())
    parts.extend(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    parts.extend(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.extend(content)
    parts.extend(b"\r\n")
    parts.extend(f"--{boundary}--\r\n".encode())
    return bytes(parts), f"multipart/form-data; boundary={boundary}"


# --- probes ---------------------------------------------------------------


def probe_status(ws: Ws, mid: str, period_ms: int) -> dict[str, Any] | None:
    section("status: Cmd 512 subscribe, then Cmd 0 fallback")
    request(ws, SUBSCRIBE, {"TimePeriod": period_ms}, mid)
    log(f"waiting {period_ms / 1000 + 1:.1f}s for a push")
    deadline = time.monotonic() + period_ms / 1000 + 1
    pushed = None
    while time.monotonic() < deadline:
        text = ws.recv_text(timeout=max(0.1, deadline - time.monotonic()))
        if text is None:
            break
        try:
            msg = json.loads(text)
        except ValueError:
            continue
        if "sdcp/status" in msg.get("Topic", ""):
            pushed = msg
            break
    if pushed is None:
        log("no push arrived; falling back to Cmd 0 (documented dead push scheduler)")
        text, request_id = build_frame(STATUS, {}, mid)
        ws.send_text(text)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            raw = ws.recv_text(timeout=max(0.1, deadline - time.monotonic()))
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if "sdcp/status" in msg.get("Topic", ""):
                pushed = msg
                break
    else:
        log("status push arrived as expected")
    if pushed:
        print_status(pushed)
    return pushed


def print_status(msg: dict[str, Any]) -> None:
    status = msg.get("Status") or (msg.get("Data") or {}).get("Status") or {}
    if not status:
        log(f"  raw status frame: {json.dumps(msg)[:400]}")
        return
    current = status.get("CurrentStatus") or []
    code = current[0] if isinstance(current, list) and current else current
    log(f"  machine state      : {code} ({MACHINE_STATE.get(code, 'unknown')})")
    info = status.get("PrintInfo") or {}
    pcode = info.get("Status")
    log(f"  print status       : {pcode} ({PRINT_STATE.get(pcode, 'unknown')})")
    log(f"  progress           : {info.get('Progress')}%")
    log(f"  layer              : {info.get('CurrentLayer')}/{info.get('TotalLayer')}")
    ticks_now, ticks_total = info.get("CurrentTicks"), info.get("TotalTicks")
    if isinstance(ticks_now, (int, float)) and isinstance(ticks_total, (int, float)):
        log(f"  ticks              : {ticks_now} / {ticks_total}")
        log(f"  remaining (if sec) : {max(0.0, ticks_total - ticks_now):.0f}s")
    log(f"  filename           : {info.get('Filename')}")
    log(f"  task id            : {info.get('TaskId')}")
    log(f"  nozzle             : {status.get('TempOfNozzle')} -> {status.get('TempTargetNozzle')}")
    log(f"  bed                : {status.get('TempOfHotbed')} -> {status.get('TempTargetHotbed')}")
    log(f"  chamber            : {status.get('TempOfBox')} -> {status.get('TempTargetBox')}")
    log(f"  coord              : {status.get('CurrenCoord')} (typo is the firmware's)")
    log(f"  fans               : {status.get('CurrentFanSpeed')}")
    log(f"  light              : {status.get('LightStatus')}")
    if not status.get("CurrenCoord") and status.get("CurrentCoord"):
        log("  NOTE: this firmware spells it CurrentCoord")


def probe_attributes(ws: Ws, mid: str) -> dict[str, Any] | None:
    section("attributes: Cmd 1")
    msg = request(ws, ATTRIBUTES, {}, mid)
    log(f"  {describe_ack(ack_of(msg))}")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        text = ws.recv_text(timeout=max(0.1, deadline - time.monotonic()))
        if text is None:
            break
        try:
            push = json.loads(text)
        except ValueError:
            continue
        if "sdcp/attributes" in push.get("Topic", ""):
            attrs = push.get("Attributes") or (push.get("Data") or {}).get("Attributes") or {}
            for key in ("Name", "MachineName", "BrandName", "ProtocolVersion",
                        "FirmwareVersion", "MainboardID", "XYZsize", "Capabilities",
                        "SupportFileType", "RemainingMemory", "CameraStatus",
                        "NumberOfVideoStreamConnected", "MaximumVideoStreamAllowed"):
                if key in attrs:
                    log(f"  {key:32}: {attrs[key]}")
            unexpected = set(attrs) - {
                "Name", "MachineName", "BrandName", "ProtocolVersion", "FirmwareVersion",
                "MainboardID", "XYZsize", "Capabilities", "SupportFileType",
                "RemainingMemory", "CameraStatus", "NumberOfVideoStreamConnected",
                "MaximumVideoStreamAllowed", "MainboardIP", "SDCPStatus", "NetworkStatus",
                "MainboardMAC", "UsbDiskStatus", "DevicesStatus",
                "NumberOfCloudSDCPServicesConnected", "MaximumCloudSDCPSercicesAllowed",
                "Resolution", "ReleaseFilmMax", "TempOfUVLEDMax", "TLPNoCapPos",
                "TLPStartCapPos", "TLPInterLayers",
            }
            if unexpected:
                log(f"  undocumented attributes present: {sorted(unexpected)}")
            return attrs
    log("  no attributes push received")
    return None


def probe_files(ws: Ws, mid: str, storage: str = "local") -> list[dict[str, Any]]:
    section(f"files: Cmd 258 on {storage}")
    url = "/udisk" if storage in ("udisk", "u-disk", "usb") else "/local"
    msg = request(ws, GET_FILE_LIST, {"Url": url}, mid, collect=5.0)
    log(f"  {describe_ack(ack_of(msg))}")
    files = []
    if msg:
        inner = msg.get("Data") or {}
        files = (inner.get("Data") or {}).get("FileList") or []
        for entry in files[:20]:
            log(f"  {entry.get('type')} {entry.get('storageType')} "
                f"{entry.get('name')} size={entry.get('FileSize') or entry.get('usedSize')}")
        if len(files) > 20:
            log(f"  ... {len(files) - 20} more")
        if not files:
            log("  empty list")
        shaped = [f for f in files if "usedSize" in f or "storageType" in f]
        if files and not shaped:
            log("  NOTE: entries use name/FileSize/TotalLayers/CreateTime, "
                "not the spec's usedSize/storageType")
    return files


def probe_history(ws: Ws, mid: str) -> None:
    section("history: Cmd 320 then Cmd 321")
    msg = request(ws, PRINT_HISTORY, {}, mid, collect=5.0)
    log(f"  {describe_ack(ack_of(msg))}")
    ids = ((msg or {}).get("Data") or {}).get("Data", {}).get("HistoryData") or []
    log(f"  {len(ids)} task ids (newest first per pycentauri)")
    if not ids:
        return
    detail = request(ws, PRINT_HISTORY_DETAIL, {"Id": ids[:3]}, mid, collect=5.0)
    log(f"  {describe_ack(ack_of(detail))}")
    rows = ((detail or {}).get("Data") or {}).get("Data", {}).get("HistoryDetailList") or []
    for row in rows:
        log(f"  {row.get('TaskName')} status={row.get('TaskStatus')} "
            f"layer={row.get('AlreadyPrintLayer')} thumb={row.get('Thumbnail')}")


def probe_camera(host: str, out: str | None) -> None:
    section(f"camera: GET http://{host}:{CAMERA_PORT}{CAMERA_PATH}")
    try:
        frame = grab_jpeg(host)
    except (OSError, WsError) as err:
        log(f"  failed: {err}")
        log("  if the port is open but no frame arrives, the stream may need "
            "Cmd 386 {\"Enable\": 1} first (unresolved, see report section 3)")
        return
    log(f"  got a JPEG, {len(frame)} bytes, starts {frame[:2].hex()} ends {frame[-2:].hex()}")
    if out:
        with open(out, "wb") as handle:
            handle.write(frame)
        log(f"  written to {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="printer IP. Omit to use --discover")
    parser.add_argument("--discover", action="store_true", help="UDP broadcast first")
    parser.add_argument("--period-ms", type=int, default=5000,
                        help="Cmd 512 push period (default 5000)")
    parser.add_argument("--snapshot", metavar="PATH", help="write a JPEG frame here")
    parser.add_argument("--storage", default="local", choices=["local", "udisk"])
    parser.add_argument("--history", action="store_true", help="also read Cmd 320 and 321")
    parser.add_argument("--upload", metavar="FILE",
                        help="upload FILE (write path, opt in deliberately)")
    parser.add_argument("--start-print", metavar="NAME",
                        help="send Cmd 128 for NAME already on the printer. "
                             "The HA integration reports this crashing the CC1")
    parser.add_argument("--no-camera", action="store_true")
    args = parser.parse_args()

    host, mid = args.host, None
    if args.discover or not host:
        section("discovery: M99999 on UDP 3000")
        found = discover()
        if not found:
            log("  no printers answered")
            if not host:
                return 1
        for entry in found:
            log(f"  {entry['host']} {entry['model']} fw={entry['firmware']} "
                f"id={entry['mainboard_id']}")
        if found and not host:
            host, mid = found[0]["host"], found[0]["mainboard_id"]
    if not host:
        log("no host to talk to")
        return 1
    if args.discover and any(
        refuse_resin(host, entry["raw"].get("Data")) for entry in found if entry["host"] == host
    ):
        return 1

    if args.upload:
        if not mid:
            log("upload needs the mainboard id, so run with --discover")
            return 1
        section("upload: POST /uploadFile/upload")
        try:
            remote = upload_file(host, args.upload)
            log(f"  uploaded as {remote}")
        except (OSError, WsError) as err:
            log(f"  failed: {err}")
            return 1

    section(f"websocket: ws://{host}:{WS_PORT}{WS_PATH}")
    try:
        with Ws(host, WS_PORT, WS_PATH, timeout=10.0) as ws:
            if not mid:
                log("waiting up to 10s for an Attributes push to learn MainboardID")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not mid:
                    text = ws.recv_text(timeout=max(0.1, deadline - time.monotonic()))
                    if text is None:
                        break
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        continue
                    attrs = msg.get("Attributes") or (msg.get("Data") or {}).get("Attributes")
                    if isinstance(attrs, dict):
                        if refuse_resin(host, attrs):
                            return 1
                        mid = attrs.get("MainboardID")
            if not mid:
                log("could not learn MainboardID. The printer does not push Attributes "
                    "while paused or errored; rerun with --discover")
                return 1
            log(f"MainboardID {mid}")

            attrs = probe_attributes(ws, mid)
            if refuse_resin(host, attrs):
                return 1
            probe_status(ws, mid, args.period_ms)
            probe_files(ws, mid, args.storage)
            if args.history:
                probe_history(ws, mid)

            if args.start_print and not attrs:
                log("start print skipped: the attributes never arrived, so this may be a resin "
                    "printer, and the 6-field Cmd 128 must never reach one")
            elif args.start_print:
                section("start print: Cmd 128 (opt-in)")
                log("  the HA integration reports this crashing the CC1 (#297)")
                msg = request(ws, START_PRINT, {
                    "Filename": args.start_print,
                    "StartLayer": 0,
                    "Calibration_switch": 1,
                    "PrintPlatformType": 0,
                    "Tlp_Switch": 0,
                    "slot_map": [],
                }, mid, timeout=30.0)
                log(f"  {describe_ack(ack_of(msg))}")
                # A dropped socket here is the documented daemon crash, not a
                # network glitch. Probe the HTTP ports to tell the two apart.
                try:
                    probe_camera(host, None)
                except Exception:
                    pass
    except WsError as err:
        log(f"websocket error: {err}")
        log("  'too many client' means the printer's WebSocket slots are full")
        return 1

    if not args.no_camera:
        probe_camera(host, args.snapshot)
    return 0


if __name__ == "__main__":
    sys.exit(main())

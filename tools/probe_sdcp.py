"""Probe a live Elegoo SDCP printer and dump every message it sends back.

Standalone. It uses ``aiohttp`` because that is the only HTTP/WebSocket library
Home Assistant guarantees, so the probe runs in the same interpreter the
integration does:

    python tools/probe_sdcp.py 192.168.128.143

It connects to ``ws://<host>:3030/websocket``, sends the read-only queries the
printer's own web UI sends on load, prints every raw frame, and exits. It never
sends a command that changes printer state. It refuses a resin printer, which must
never get command 324; that check imports the integration, as the acceptance tools do.
A printer that stays silent on UDP gets 324 only once its own frames show it is FDM.

Note on the wire format the printer expects: the request frame carries a bare
``Data`` object, but everything the printer *sends back* uses ``Topic`` values
like ``sdcp/status/<mainboard-id>`` with a nested ``Data`` envelope.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
import uuid
from typing import Any

from acceptance_kit import async_refuse_resin

DEFAULT_PORT = 3030
#: Seconds to wait after connecting, and between the commands.
SETTLE = 1.0
GAP = 1.2

CMD_STATUS = 0
CMD_ATTRIBUTES = 1
CMD_FILE_LIST = 258
CMD_HISTORY = 320
CMD_MATERIAL = 324

READ_ONLY_COMMANDS: tuple[tuple[int, dict[str, Any]], ...] = (
    (CMD_STATUS, {}),
    (CMD_ATTRIBUTES, {}),
    (CMD_FILE_LIST, {"Url": "/local", "Path": "/"}),
    (CMD_HISTORY, {}),
    (CMD_MATERIAL, {}),
)


def envelope(mainboard_id: str, cmd: int, data: dict[str, Any]) -> str:
    """Build the SDCP request envelope the printer's own UI sends."""
    return json.dumps(
        {
            "Id": "",
            "Data": {
                "Cmd": cmd,
                "Data": data,
                "RequestID": uuid.uuid4().hex,
                "MainboardID": mainboard_id,
                "TimeStamp": int(time.time() * 1000),
                "From": 1,
            },
        }
    )


def _find_mainboard_id(node: Any) -> str:
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() == "mainboardid" and isinstance(value, str):
                return value
            found = _find_mainboard_id(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_mainboard_id(item)
            if found:
                return found
    return ""


def discover_mainboard_id(host: str) -> str:
    """Read the MainboardID from the HTTP endpoints the printer serves it on."""
    for path in ("/sdcp/info", "/sdcp/status", "/"):
        url = f"http://{host}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
                body = response.read(65536).decode("utf-8", "replace")
        except Exception as err:  # noqa: BLE001 - diagnostic tool
            print(f"    GET {url} -> {type(err).__name__}: {err}")
            continue
        print(f"    GET {url} -> {body[:300]!r}")
        try:
            found = _find_mainboard_id(json.loads(body))
        except json.JSONDecodeError:
            continue
        if found:
            return found
    return ""


async def run(host: str, port: int, seconds: float) -> int:
    import aiohttp

    if await async_refuse_resin(host):
        return 1
    from custom_components.generic_3dprinter.adapters import sdcp

    print(f"[1] HTTP discovery on {host}")
    mainboard_id = await asyncio.to_thread(discover_mainboard_id, host)
    print(f"    MainboardID = {mainboard_id or '(not exposed over HTTP; try an empty id)'}")

    url = f"ws://{host}:{port}/websocket"
    print(f"[2] connecting {url}")
    frames = 0
    seen: dict[str, Any] = {"Attributes": {}, "Status": {}}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
            async with session.ws_connect(url, heartbeat=None, max_msg_size=16 * 1024 * 1024) as ws:
                print("    connected")

                async def reader() -> None:
                    nonlocal frames
                    async for msg in ws:
                        if msg.type is aiohttp.WSMsgType.TEXT:
                            frames += 1
                            text = msg.data
                        elif msg.type is aiohttp.WSMsgType.BINARY:
                            frames += 1
                            text = f"<binary {len(msg.data)} bytes>"
                        else:
                            print(f"    <- control {msg.type}")
                            continue
                        try:
                            parsed = json.loads(text)
                        except json.JSONDecodeError:
                            print(f"    <- frame {frames} (not JSON) {text[:400]!r}")
                            continue
                        if not isinstance(parsed, dict):
                            continue
                        for key in seen:
                            inner = parsed.get("Data") if isinstance(parsed.get("Data"), dict) else {}
                            found = parsed.get(key) or inner.get(key)
                            if isinstance(found, dict):
                                seen[key] = found
                        topic = parsed.get("Topic")
                        print(f"    <- frame {frames} Topic={topic}")
                        print(f"       {json.dumps(parsed, indent=2)[:6000]}")

                reader_task = asyncio.create_task(reader())
                await asyncio.sleep(SETTLE)
                for cmd, data in READ_ONLY_COMMANDS:
                    kind = sdcp.classify_sdcp(seen["Attributes"], seen["Status"])
                    if cmd == CMD_MATERIAL and kind != "fdm":
                        # UDP may be filtered or dropped: a Saturn must never get 324.
                        print(f"[3] skipped Cmd={cmd}: the printer's frames did not show an FDM printer ({kind})")
                        continue
                    print(f"[3] -> Cmd={cmd} {json.dumps(data)}")
                    await ws.send_str(envelope(mainboard_id, cmd, data))
                    await asyncio.sleep(GAP)
                print(f"[4] draining for {seconds}s")
                await asyncio.sleep(seconds)
                reader_task.cancel()
                try:
                    await reader_task
                except asyncio.CancelledError:
                    pass
    except Exception as err:  # noqa: BLE001 - diagnostic tool
        print(f"    connection failed: {type(err).__name__}: {err}")
        return 1
    print(f"[5] done, {frames} frames received")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    host = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0
    if sys.platform == "win32":
        # aiodns refuses to initialise on the Proactor loop that is the Windows
        # default, so pick the selector loop used in production (Linux) here.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(run(host, port, seconds))


if __name__ == "__main__":
    raise SystemExit(main())

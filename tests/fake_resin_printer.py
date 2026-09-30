"""A fake Saturn 4 Ultra 16K (V1.5.6) that sends only frames the real one sent, and "crashes"
on any command, or payload of a command that acts, the SDCP V3 spec does not give a resin printer."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).parent / "fixtures" / "sdcp_saturn4u16k_v156_idle.json"

#: The command codes the SDCP V3 spec gives a resin printer, less 387: the timelapse
#: switch is a setting that outlives the print and can disturb the next one.
ALLOWED = frozenset({0, 1, 128, 129, 130, 131, 258, 259, 320, 321, 386})
#: The one start-print payload the spec defines.
START_PRINT_KEYS = frozenset({"Filename", "StartLayer"})
#: The ``Data`` keys the spec gives each command that changes something; any other shape crashes.
PAYLOAD_KEYS: dict[int, frozenset[str]] = {
    128: START_PRINT_KEYS,
    129: frozenset(),
    130: frozenset(),
    131: frozenset(),
    259: frozenset({"FileList", "FolderList"}),
}

#: A 16K mid-print, reported in elegoo-homeassistant issue #21 (TaskId shortened there).
PRINTING_STATUS: dict[str, Any] = {
    "CurrentStatus": [1],
    "ReleaseFilm": 10854,
    "TempOfTank": 20,
    "TempTargetTank": 30,
    "HeatStatus": 0,
    "PrintInfo": {
        "Status": 4,
        "CurrentLayer": 3995,
        "TotalLayer": 5132,
        "CurrentTicks": 20313461,
        "TotalTicks": 25361465,
        "ErrorNumber": 0,
        "Filename": "model.goo",
        "TaskId": "52706856",
    },
}


def load_fixture() -> dict[str, Any]:
    """Return the captured frames, a fresh copy each time."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class FakeResinPrinter:
    """A loopback resin printer on one WebSocket port."""

    def __init__(self, *, printing: bool = False) -> None:
        """Create the printer idle, as captured, or mid-print."""
        self.frames = load_fixture()
        self.attributes: dict[str, Any] = self.frames["attributes"]["Attributes"]
        self.attributes["MainboardIP"] = "127.0.0.1"
        self.status: dict[str, Any] = self.frames["status"]["Status"]
        if printing:
            info = {**self.status["PrintInfo"], **PRINTING_STATUS["PrintInfo"]}
            self.status.update(copy.deepcopy(PRINTING_STATUS), PrintInfo=info)
        self.mainboard: str = self.attributes["MainboardID"]
        #: The constant ``Id`` the printer puts on every frame it sends.
        self.brand_id: str = self.frames["status_ack"]["Id"]
        #: Every file and folder on the printer, as command 258 names them.
        self.files: list[dict[str, Any]] = list(self.frames["file_list"]["Data"]["Data"]["FileList"])
        #: Ack a delete and keep the file, as a printer that failed to delete it would.
        self.keep_deleted = False
        #: The ``Ack`` to answer a command that acts with, by code; 0 when absent.
        self.acks: dict[int, int] = {}

        #: Every request's ``Data``, and its command code, in arrival order.
        self.received: list[dict[str, Any]] = []
        self.sent_commands: list[Any] = []
        #: Text frames that were not JSON, such as ``ping``.
        self.texts: list[str] = []
        self.crashed = False
        self.forbidden: list[Any] = []
        #: Ignore a request unless its envelope is the one the printer's own clients send.
        self.strict_envelope = False
        self.ignored: list[dict[str, Any]] = []
        #: Answer command 1 with its ack alone, never pushing the attributes.
        self.withhold_attributes = False
        #: Answer command 0 with its ack alone, never pushing the status.
        self.withhold_status = False
        #: Seconds to hold back the answer to a command, by code.
        self.answer_delay: dict[int, float] = {}
        #: Close a client that has sent no command for this many seconds. ``None`` keeps it.
        self.silent_close_after: float | None = None
        #: RTSP sessions open, as the attributes count them, and every 386 ``Enable`` sent.
        self.video_streams = 0
        self.video_enables: list[Any] = []

        self.url = ""
        self._sockets: set = set()
        self._runner = None

    @property
    def port(self) -> int:
        """Return the WebSocket port."""
        return int(self.url.rsplit(":", 1)[1])

    @property
    def connections(self) -> int:
        """Return how many sockets are open."""
        return len(self._sockets)

    async def start(self) -> str:
        """Start the server and return its ``http://`` URL."""
        from aiohttp import web

        from tests.fake_printer import _bind

        app = web.Application()
        app.router.add_get("/websocket", self._handle)
        self._runner, site = await _bind(app, self.port if self.url else None)
        if not self.url:
            self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"  # noqa: SLF001
        return self.url

    async def stop(self) -> None:
        """Close every socket, then the server."""
        await self._close_all()
        if self._runner is not None:
            with suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
        await asyncio.sleep(0)

    async def push(self, kind: str) -> None:
        """Push the ``attributes`` or the ``status`` to every open socket, unasked."""
        key, body = ("Attributes", self.attributes) if kind == "attributes" else ("Status", self.status)
        for socket in list(self._sockets):
            await socket.send_str(self._push(kind, key, body))

    async def _close_all(self) -> None:
        for socket in list(self._sockets):
            with suppress(Exception):
                await socket.close(code=1001, message=b"power off")
        self._sockets.clear()

    async def _handle(self, request):
        from aiohttp import WSMsgType, web

        ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024)
        await ws.prepare(request)
        if self.crashed:
            await ws.close(code=1011)
            return ws
        self._sockets.add(ws)
        try:
            await self._async_serve(ws, WSMsgType)
        finally:
            self._sockets.discard(ws)
        return ws

    async def _async_serve(self, ws, WSMsgType) -> None:
        loop = asyncio.get_running_loop()
        last_command = loop.time()
        while not ws.closed:
            wait = None
            if self.silent_close_after is not None:
                wait = max(self.silent_close_after - (loop.time() - last_command), 0)
            try:
                message = await asyncio.wait_for(ws.receive(), timeout=wait)
            except TimeoutError:
                await ws.close(code=1001)
                break
            if message.type is not WSMsgType.TEXT:
                break
            try:
                frame = json.loads(message.data)
            except ValueError:
                # Measured: nothing ever answers the text "ping".
                self.texts.append(message.data)
                continue
            if not isinstance(frame, dict):
                self.texts.append(message.data)
                continue
            last_command = loop.time()
            await self._async_answer(ws, frame)

    async def _async_answer(self, ws, frame: dict[str, Any]) -> None:
        inner = frame.get("Data") if isinstance(frame.get("Data"), dict) else {}
        cmd = inner.get("Cmd")
        data = inner.get("Data") if isinstance(inner.get("Data"), dict) else {}
        request_id = str(inner.get("RequestID") or "")
        self.received.append(inner)
        self.sent_commands.append(cmd)

        if cmd not in ALLOWED or (cmd in PAYLOAD_KEYS and set(data) != PAYLOAD_KEYS[cmd]):
            self.crashed = True
            self.forbidden.append(cmd)
            await self._close_all()
            return
        if self.strict_envelope and not self._envelope_ok(frame, inner):
            self.ignored.append(inner)
            return
        if cmd in self.answer_delay:
            await asyncio.sleep(self.answer_delay[cmd])
            if ws.closed:
                return

        if cmd == 1:
            await ws.send_str(self._response(cmd, request_id, {"Ack": 0}))
            if not self.withhold_attributes:
                attributes = {**self.attributes, "NumberOfVideoStreamConnected": self.video_streams}
                await ws.send_str(self._push("attributes", "Attributes", attributes))
        elif cmd == 0:
            await ws.send_str(self._response(cmd, request_id, {"Ack": 0}))
            if not self.withhold_status:
                await ws.send_str(self._push("status", "Status", self.status))
        elif cmd == 258:
            folder = str(data.get("Url") or "")
            listed = [item for item in self.files if item["name"].rsplit("/", 1)[0] == folder]
            await ws.send_str(self._response(cmd, request_id, {"Ack": 0, "FileList": listed}))
        elif cmd == 320:
            await ws.send_str(self._response(cmd, request_id, self.frames["history"]["Data"]["Data"]))
        elif cmd == 321:
            detail = self.frames["history_detail"]["Data"]["Data"]
            await ws.send_str(self._response(cmd, request_id, detail))
        elif cmd == 386:
            self.video_enables.append(data.get("Enable"))
            if data.get("Enable") and self.video_streams >= self.attributes["MaximumVideoStreamAllowed"]:
                await ws.send_str(self._response(cmd, request_id, {"Ack": 1}))
            else:
                url = "rtsp://127.0.0.1:554/video"
                await ws.send_str(self._response(cmd, request_id, {"Ack": 0, "VideoUrl": url}))
        else:
            # 259 answers Ack 0 even for a path that does not exist, as reported.
            ack = self.acks.get(cmd, 0)
            if cmd == 259 and not ack and not self.keep_deleted:
                gone = set(data["FileList"])
                self.files = [item for item in self.files if item["name"] not in gone]
            await ws.send_str(self._response(cmd, request_id, {"Ack": ack}))

    def _envelope_ok(self, frame: dict[str, Any], inner: dict[str, Any]) -> bool:
        """Return whether a request carries the envelope the capture used."""
        stamp = inner.get("TimeStamp")
        return (
            bool(frame.get("Id"))
            and frame.get("Topic") == f"sdcp/request/{self.mainboard}"
            and inner.get("MainboardID") == self.mainboard
            and isinstance(stamp, int)
            and stamp < 10**11
        )

    def _response(self, cmd: int, request_id: str, body: dict[str, Any]) -> str:
        return json.dumps(
            {
                "Id": self.brand_id,
                "Data": {
                    "Cmd": cmd,
                    "Data": body,
                    "RequestID": request_id,
                    "MainboardID": self.mainboard,
                    "TimeStamp": int(time.time()),
                },
                "Topic": f"sdcp/response/{self.mainboard}",
            }
        )

    def _push(self, kind: str, key: str, body: dict[str, Any]) -> str:
        return json.dumps(
            {
                key: body,
                "MainboardID": self.mainboard,
                "TimeStamp": int(time.time()),
                "Topic": f"sdcp/{kind}/{self.mainboard}",
            }
        )

"""A fake Anycubic Kobra: its handshake, its TLS broker and its reports.

It answers the way the sources recorded a real printer answering:

* ``/info`` and a ``/ctrl`` that checks the signature and hands out a fresh session,
  AES-encrypted, so a broken handshake fails the tests;
* a broker over TLS that accepts only credentials it issued;
* reports on ``.../printer/public/<model>/<device>/<type>/report``: ``info`` with
  the whole state, ``tempature``, ``fan``, ``light``, ``multiColorBox`` for
  ``getInfo``, ``peripherie``, ``video`` with a per-session stream URL, and ``file``
  for ``listLocal``;
* a job setting sent while idle is dropped with no answer, which is what an idle
  printer does; accepted commands are answered with ``code: 200``.

Shapes come from chrisfore/anycubic_ha_local (Kobra S1 Max, Kobra X) and
stribor/anycubic_kobrax (Kobra X).
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import suppress
from typing import Any

from aiohttp import web

from custom_components.generic_3dprinter.adapters.anycubic_kobra import encrypt_bundle
from tests.adapter_kit.fake_broker import BrokerSession, FakeBroker

ROOT = "anycubic/anycubicCloud/v1"
TOKEN = "0123456789abcdefFEDCBA9876543210"
LOCAL_TOKEN = "localtoken123456"
DEVICE_ID = "DEV0123456789"
SERIAL = "KX2026ABCDEF"
READ_ACTIONS = frozenset({"query", "getInfo", "listLocal", "reportInfo"})

#: A Kobra X printing, with its built-in four-slot unit.
INFO: dict[str, Any] = {
    "printerName": "Kobra X",
    "model": "Anycubic Kobra X",
    "version": "1.2.8",
    "state": "busy",
    "temp": {
        "curr_hotbed_temp": 60,
        "curr_nozzle_temp": 219,
        "target_hotbed_temp": 60,
        "target_nozzle_temp": 220,
        "curr_chamber_temp": 0,
    },
    "print_speed_mode": 2,
    "fan_speed_pct": 80,
    "aux_fan_speed_pct": 0,
    "box_fan_level": 0,
    "project": {
        "progress": 42,
        "curr_layer": 88,
        "total_layers": 210,
        "remain_time": 35,
        "print_time": 1500,
        "state": "printing",
        "pause": 0,
        "filename": "benchy.gcode",
        "taskid": "7788",
    },
    "urls": {"rtspUrl": "", "fileUploadurl": "http://127.0.0.1:18910/gcode_upload?s=abc"},
}

UNIT: dict[str, Any] = {
    "head_tools_model": 1,
    "multi_color_box": [
        {
            "id": -1,
            "model_id": 40002,
            "status": 1,
            "auto_feed": 1,
            "loaded_slot": 0,
            "temp": 0,
            "humidity": 0,
            "slots": [
                {"index": 0, "type": "PLA", "color": [255, 255, 255], "status": 5, "consumables_percent": 100},
                {"index": 1, "type": "PETG", "color": [255, 0, 0], "status": 5, "consumables_percent": 80},
                {"index": 2, "type": "", "color": [0, 0, 0], "status": 0},
                {"index": 3, "type": "PLA", "color": [0, 0, 255], "status": 5},
            ],
        }
    ],
}


class FakeKobraPrinter:
    """A loopback Kobra in LAN mode, printing."""

    def __init__(self, *, model_id: str = "20030", with_unit: bool = True) -> None:
        """Create a stopped printer."""
        self.model_id = model_id
        self.ctrl_type = "lan"
        self.info = copy.deepcopy(INFO)
        self.unit: dict[str, Any] = copy.deepcopy(UNIT) if with_unit else {"multi_color_box": []}
        self.lights = [{"type": 2 if model_id in ("20025", "20029") else 3, "status": 1, "brightness": 100}]
        self.files = [
            {"name": "benchy.gcode", "size": 1234567, "modify_time": 1706900000},
            {"name": "cube.gcode", "size": 42, "modify_time": 1706900100},
        ]
        #: Every message a client published, in order.
        self.received: list[dict[str, Any]] = []
        #: ``(type, action)`` to the ``code`` the printer refuses it with.
        self.refuse: dict[tuple[str, str], int] = {}
        #: Answer nothing at all, as a printer that went quiet does.
        self.silent = False
        self.handshakes = 0
        self.stream_starts = 0
        self.port = 0
        self._issued: set[tuple[str, str]] = set()
        self._broker = FakeBroker(authenticate=self._authenticate, on_publish=self._on_publish, tls=True)
        self._runner: web.AppRunner | None = None

    # ---------------------------------------------------------------- lifecycle

    @property
    def broker_port(self) -> int:
        """Return the broker's port."""
        return self._broker.port

    @property
    def sessions(self) -> int:
        """Return how many clients are connected to the broker."""
        return len(self._broker.sessions)

    @property
    def commands(self) -> list[dict[str, Any]]:
        """Return every message that asked the printer to do something."""
        return [
            item
            for item in self.received
            if item.get("action") not in READ_ACTIONS and item.get("type") != "video"
        ]

    def sent(self, kind: str, action: str) -> list[Any]:
        """Return the ``data`` of every message of one type and action."""
        return [item.get("data") for item in self.received if item.get("type") == kind and item.get("action") == action]

    async def start(self) -> None:
        """Start the handshake server and the broker, keeping earlier ports."""
        await self._broker.start()
        app = web.Application()
        app.router.add_get("/info", self._info)
        app.router.add_post("/ctrl", self._ctrl)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port or 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001

    async def stop(self) -> None:
        """Power off."""
        await self._broker.stop()
        if self._runner is not None:
            with suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
        self._issued.clear()

    def close(self) -> None:
        """Remove the broker's certificate once the printer will not start again."""
        self._broker.close()

    # ---------------------------------------------------------------- handshake

    async def _info(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "token": TOKEN,
                "ctrlInfoUrl": f"http://127.0.0.1:{self.port}/ctrl",
                "modelId": self.model_id,
                "modelName": "Anycubic Kobra X" if self.model_id == "20030" else "Anycubic Kobra",
                "cn": SERIAL,
                "ctrlType": self.ctrl_type,
                "deviceType": "fdm",
                "version": "1.2.8",
            }
        )

    async def _ctrl(self, request: web.Request) -> web.Response:
        query = request.query
        first = hashlib.md5(TOKEN[:16].encode()).hexdigest()  # noqa: S324
        expected = hashlib.md5((first + query.get("ts", "") + query.get("nonce", "")).encode()).hexdigest()  # noqa: S324
        if query.get("sign") != expected or len(query.get("did", "")) != 32:
            return web.json_response({"code": 401, "message": "sign error"})
        self.handshakes += 1
        username, password = f"user{self.handshakes}", f"pass{self.handshakes}"
        self._issued.add((username, password))
        bundle = {
            "broker": f"mqtts://127.0.0.1:{self.broker_port}",
            "username": username,
            "password": password,
            "deviceId": DEVICE_ID,
            "modelId": self.model_id,
        }
        return web.json_response(
            {"code": 200, "message": "success", "data": {"token": LOCAL_TOKEN, "info": encrypt_bundle(bundle, TOKEN, LOCAL_TOKEN)}}
        )

    # ------------------------------------------------------------------ broker

    def _authenticate(self, session: BrokerSession, password: str | None) -> int:
        return 0 if (session.username, password) in self._issued else 5

    def report_topic(self, kind: str) -> str:
        """Return the topic the printer reports ``kind`` on."""
        return f"{ROOT}/printer/public/{self.model_id}/{DEVICE_ID}/{kind}/report"

    async def report(self, kind: str, data: Any, **extra: Any) -> None:
        """Push one report."""
        await self._broker.deliver(self.report_topic(kind), {"type": kind, "action": "report", "data": data, **extra})

    async def _on_publish(self, session: BrokerSession, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if len(parts) != 8 or parts[3] not in ("web", "slicer") or parts[6] != DEVICE_ID:
            return
        message = json.loads(payload)
        message["_source"] = parts[3]
        self.received.append(message)
        if self.silent:
            return
        kind, action, data = message.get("type"), message.get("action"), message.get("data")
        if kind != parts[7]:
            return
        if action in ("query", "getInfo"):
            await self._answer_query(kind)
            return
        refusal = self.refuse.get((kind, action))
        if refusal is not None:
            await self.report(kind, None, action=action, code=refusal, msg="refused", msgid=message.get("msgid"))
            return
        await self._command(kind, action, data or {}, message)

    async def _answer_query(self, kind: str) -> None:
        temp = self.info["temp"]
        if kind == "info":
            await self.report("info", {**copy.deepcopy(self.info), "lights": None})
        elif kind == "tempature":
            await self.report("tempature", dict(temp))
        elif kind == "fan":
            await self.report("fan", {k: self.info[k] for k in ("fan_speed_pct", "aux_fan_speed_pct", "box_fan_level")})
        elif kind == "light":
            await self.report("light", {"lights": copy.deepcopy(self.lights)})
        elif kind == "multiColorBox":
            await self.report("multiColorBox", copy.deepcopy(self.unit))
        elif kind == "peripherie":
            await self.report("peripherie", {"camera": 1, "multiColorBox": 1 if self.unit["multi_color_box"] else 0, "udisk": 0})

    async def _ack(self, kind: str, action: str, message: dict[str, Any], state: str | None = None) -> None:
        await self.report(kind, None, action=action, code=200, msg="done", state=state, msgid=message.get("msgid"))

    async def _command(self, kind: str, action: str, data: dict[str, Any], message: dict[str, Any]) -> None:
        busy = self.info["state"] == "busy"
        project = self.info["project"]
        if kind == "print" and action in ("pause", "resume", "stop"):
            project["state"], project["pause"] = {
                "pause": ("paused", 1),
                "resume": ("printing", 0),
                "stop": ("stoped", 0),
            }[action]
            if action == "stop":
                self.info["state"] = "free"
            await self._ack(kind, action, message, project["state"])
        elif kind == "print" and action == "update":
            if not busy:
                return  # an idle printer drops job settings without a word
            self.info.update({k: v for k, v in data.get("settings", {}).items() if k in self.info})
            await self._ack(kind, action, message, "updated")
        elif kind == "print" and action == "start":
            self.info["state"] = "busy"
            project.update({"state": "preheating", "filename": data.get("filename"), "pause": 0})
            await self._ack(kind, action, message)
        elif kind == "tempature" and action == "set":
            self.info["temp"]["target_nozzle_temp"] = data.get("target_nozzle_temp")
            self.info["temp"]["target_hotbed_temp"] = data.get("target_hotbed_temp")
            await self._ack(kind, action, message)
        elif kind == "fan" and action == "setSpeed":
            self.info["fan_speed_pct"] = data.get("fan_speed_pct")
            await self._ack(kind, action, message)
        elif kind == "light" and action == "control":
            for light in self.lights:
                if light["type"] == data.get("type"):
                    light["status"], light["brightness"] = data.get("status"), data.get("brightness")
            await self._ack(kind, action, message)
        elif kind == "axis" and action == "move":
            await self._ack(kind, action, message)
        elif kind == "multiColorBox" and action == "setAutoFeed":
            for change in data.get("multi_color_box", []):
                for box in self.unit["multi_color_box"]:
                    if box["id"] == change.get("id"):
                        box["auto_feed"] = change.get("auto_feed")
            await self._ack(kind, action, message)
        elif kind == "video" and action == "startCapture":
            self.stream_starts += 1
            url = f"http://192.0.2.1:18088/live/token{self.stream_starts}"
            await self.report("video", {"urls": {"rtspUrl": url}}, action=action, state="initSuccess", code=200)
        elif kind == "video" and action == "stopCapture":
            await self.report("video", None, action=action, state="pushStopped", code=200)
        elif kind == "file" and action == "listLocal":
            await self.report("file", {"file_list": copy.deepcopy(self.files)}, action="listLocal", code=200)

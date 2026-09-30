# Elegoo Centauri Carbon, verified protocol surface

Every fact in this document was observed by this repository's own tools against a
live printer on the LAN, or is marked as coming from another source. The probe
transcript is reproducible with `tools/probe_sdcp.py`.

**These facts are historical.** The Centauri Carbon below has since been sold, so
nothing here can be measured again on it. Its address, `192.168.128.143`, now
belongs to an Elegoo Saturn 4 Ultra 16K, a resin printer that speaks SDCP V3 on the
same port and must never get a Centauri command; it is documented in
`protocol-elegoo-sdcp-resin.md`. Point `tools/probe_sdcp.py` and the other Centauri
tools at another Centauri Carbon: at a resin printer they stop before any command
only a Centauri takes.

Device under test:

| Property | Value |
| --- | --- |
| Model | Elegoo Centauri Carbon |
| LAN address | `192.168.128.143` at the time; now the Saturn 4 Ultra 16K's |
| Firmware | `V1.4.49` |
| SDCP protocol version | `V3.0.0` |
| MainboardID | `5c441dd30105041800009c0000000000` |
| Build volume | `218.88 x 128.88 x 220` mm |
| Capabilities | `FILE_TRANSFER`, `PRINT_CONTROL`, `VIDEO_STREAM` |
| Network | `wlan`, MAC `a4:e8:8d:2f:c5:09` |
| State during probing | printing, layer 107 of 627 |

## Open ports

Only three ports answered on this printer.

| Port | Service | Notes |
| --- | --- | --- |
| 80 | Angular single-page web UI, plus one upload route | HTTP only, no TLS |
| 3030 | SDCP JSON control over WebSocket, at `/websocket` | also answers the SPA over plain GET |
| 3031 | MJPEG camera at `/video` | HTTP only |

The following ports were probed and are closed: 22, 23, 81, 443, 554, 1883, 5000,
6600, 7125, 8080, 8083, 8088, 8266, 8554, 8880, 8883, 8888, 9000, 9090, 10000.
There is no Moonraker, no OctoPrint, no RTSP and no MQTT broker.

## Control channel

`ws://<host>:3030/websocket`

The request envelope the printer accepts, verified working:

```json
{
  "Id": "",
  "Data": {
    "Cmd": 0,
    "Data": {},
    "RequestID": "<32 hex characters>",
    "MainboardID": "<32 hex characters>",
    "TimeStamp": 1789656234000,
    "From": 1
  }
}
```

Three details that matter for implementation:

* `RequestID` must be fresh per request. The printer echoes it back on the
  response topic, which is how a reply is correlated to its request.
* `TimeStamp` is milliseconds. The printer accepted `int(time.time() * 1000)`.
* The printer tolerates `"Id": ""` and an absent `Topic` field in the request,
  even though the vendor SDK sends both. Do not rely on either being present.

### Responses

Responses are routed by a **string** `Topic`, not by a numeric topic id. Four
topics were observed:

| Topic | Carries |
| --- | --- |
| `sdcp/status/<mainboard-id>` | the `Status` object, in reply to `Cmd` 0 and to the text `ping` |
| `sdcp/attributes/<mainboard-id>` | the `Attributes` object, in reply to `Cmd` 1 |
| `sdcp/response/<mainboard-id>` | the `Ack` for a command, and command payloads such as the file list |
| `sdcp/error/<mainboard-id>` | errors; named in the printer's own UI code |

An acknowledgement looks like this. `Ack` 0 is success.

```json
{
  "Id": "979d4C788A4a78bC777A870F1A02867A",
  "Data": {
    "Cmd": 0,
    "Data": { "Ack": 0 },
    "RequestID": "fd879a84968e48c187a29c2809a21f20",
    "MainboardID": "5c441dd30105041800009c0000000000",
    "TimeStamp": 1789656234
  },
  "Topic": "sdcp/response/5c441dd30105041800009c0000000000"
}
```

Command payloads can arrive on a later frame than the acknowledgement. The file
list does: the `Ack` frame and the `FileList` frame are separate messages.

### Keeping the socket

The printer's own page opens the socket, sends `Cmd` 0, 1, 320, 134 and 258, and
then sends the plain text `ping` every 30 seconds. The printer holds a client to
that. Measured on 2026-09-25 with two sockets opened side by side on an idle
printer, each sending `Cmd` 1 once:

| Socket | Closed by the printer | Status frames received |
| --- | --- | --- |
| sends nothing more | after 61 s, close code 1006 | 1 in 61 s |
| sends `ping` every 30 s | no, still open at 150 s | 5 in 150 s |

The printer never answers `ping` with text. It answers it, often but not every time,
with a status push, and it pushes its status otherwise only in reply to `Cmd` 0.
Attributes are pushed every few seconds while a client is connected.

This is why a client that does not ping sees the socket close every minute, and why
a client that caches the status it had before the printer was switched off keeps
reporting it: nothing new arrives unless it is asked for. The adapter therefore
pings every 30 seconds, sends `Cmd` 0 on every connection and when its status is
more than 20 seconds old, and treats a socket that has carried nothing for 75
seconds as dead. With that, it held one socket for 100 seconds without a reconnect
on the same printer.

### The CANVAS

`Cmd` 324 returns the CANVAS attached to the printer. The printer's page sends it
with an empty `Data` before it shows the slots, and the answer arrives on
`sdcp/response`:

```json
{
  "active_canvas_id": 0,
  "active_tray_id": -1,
  "auto_refill": 1,
  "canvas_list": [
    {
      "canvas_id": 0,
      "connected": 1,
      "tray_list": [
        {
          "tray_id": 0, "brand": "Generic", "filament_type": "PETG",
          "filament_name": "PETG PRO", "filament_code": "0x00000",
          "filament_color": "#000000", "min_nozzle_temp": 230,
          "max_nozzle_temp": 260, "status": 0
        }
      ]
    }
  ],
  "Ack": 0
}
```

Four trays came back; one is shown. `status` is 0 for an empty tray and 1 for a
loaded one, and an empty tray keeps the filament it last held on record.
`active_tray_id` is -1 while nothing is in the nozzle. The status object carries
`AmsConnectStatus`, 1 while a CANVAS is attached, and the adapter sends `Cmd` 324
only then.

The page on this firmware sends no command to load, unload or edit a tray. Those
are done on the printer's screen.

### Command codes

Read from the printer's own bundle at `main.<hash>.js`, module 543, and each one
below is either marked verified against this printer or unverified.

| Cmd | Name in the printer's code | Verified |
| --- | --- | --- |
| 0 | `GET_PRINTER_STATUS` | yes, reply on `sdcp/status` |
| 1 | `GET_PRINTER_ATTR` | yes, reply on `sdcp/attributes` |
| 64 | `SEND_PRINTER_DISCONNECT` | no |
| 128 | `SEND_PRINTER_START_PRINT` | no, and see the hazard below |
| 129 | `SEND_PRINTER_SUSPEND_PRINT` | no |
| 130 | `SEND_PRINTER_STOP_PRINT` | no |
| 131 | `SEND_PRINTER_RESTORE_PRINT` | no |
| 134 | `GET_BLACKOUT_STATUS` | no |
| 135 | `SEND_BLACKOUT_ACTION` | no |
| 192 | `SEND_PRINTER_EDIT_NAME` | no |
| 255 | `SEND_PRINTER_SEND_FILE_END` | no |
| 257 | `EDIT_PRINTER_FILE_NAME` | no |
| 258 | `GET_PRINTER_FILE_LIST` | yes, payload on `sdcp/response` |
| 259 | `DELETE_PRINTER_FILE_LIST` | no |
| 260 | `GET_PRINTER_FILE_DETAIL` | no |
| 320 | `GET_PRINTER_HISTORY_ID` | yes, returns `HistoryData` task id list |
| 321 | `GET_PRINTER_TASK_DETAIL` | no |
| 322 | `DELETE_PRINTER_HISTORY` | no |
| 323 | `GET_PRINTER_HISTORY_VIDEO` | no |
| 324 | `GET_MATERIAL_DATA` | yes, returns the CANVAS, see above |
| 386 | `EDIT_PRINTER_VIDEO_STREAMING` | sent by the page before it shows the camera; see the camera section |
| 387 | `EDIT_PRINTER_TIME_LAPSE_STATUS` | no |
| 401 | `EDIT_PRINTER_AXIS_NUMBER` | no |
| 402 | `EDIT_PRINTER_AXIS_ZERO` | no |
| 403 | `EDIT_PRINTER_STATUS_DATA` | no, polymorphic, see below |
| 503 | `GET_FILE_COLOR_DATA` | no |

`Cmd` 403 is overloaded: the payload keys select the operation. The printer's own
bundle confirms the code, and community sources report the dispatch:

```
{"PrintSpeedPct": N}                             set print speed
{"TargetFanSpeed": {"ModelFan": N, "BoxFan": N, "AuxiliaryFan": N}}
{"TempTargetNozzle": N, "TempTargetHotbed": N, "TempTargetBox": N}
{"LightStatus": {"SecondLight": 0|1}}            chamber light
```

### Hazard: unverified command codes

Two independent claims make this load-bearing, and neither was tested here,
because the printer was running a print throughout.

1. `Cmd` 128 (start print) is omitted by an existing HACS integration for this
   exact model, with the note that the equivalent command crashed the printer.
2. An unknown command code, or a known code with an unexpected payload shape, can
   crash the printer's `app` daemon. On this device that daemon is the whole host,
   including the embedded motion stack, so an active print dies with it.

The integration therefore sends a fixed allowlist of command codes, refuses
anything else at the boundary, and gates start print behind an explicit opt-in
that is off by default. Nothing in this repository ever probes an unknown code.

### The status object

Captured live from `sdcp/status`:

```json
{
  "CurrentStatus": [1],
  "TimeLapseStatus": 0,
  "PlatFormType": 0,
  "AmsConnectStatus": 1,
  "TempOfHotbed": 54.99,
  "TempOfNozzle": 219.86,
  "TempOfBox": 32.69,
  "TempTargetHotbed": 55,
  "TempTargetNozzle": 220,
  "TempTargetBox": 0,
  "CurrenCoord": "101.10,77.83,22.45",
  "CurrentFanSpeed": { "ModelFan": 100, "AuxiliaryFan": 69, "BoxFan": 68 },
  "ZOffset": 1e-14,
  "LightStatus": { "SecondLight": 1, "RgbLight": [0, 0, 0] },
  "PrintInfo": {
    "Status": 13,
    "CurrentLayer": 107,
    "TotalLayer": 627,
    "CurrentTicks": 2532.16,
    "TotalTicks": 18574,
    "Filename": "ECC_0.4_medieval-bocco_eSUN PLA+ _0.2_5h10m.gcode",
    "TaskId": "3d315103-79cc-498c-8512-26c60b95527f",
    "PrintSpeedPct": 100,
    "Progress": 12
  }
}
```

Things to notice, because each one is a place a naive parser breaks:

* `CurrentStatus` is a **list** of bit flags, not a scalar. Bit 1 means printing.
  The printer's own UI checks `CurrentStatus.includes(1)` and `includes(8)`,
  where 8 means a file transfer just finished.
* The remaining time is `TotalTicks - CurrentTicks`, in seconds. There is no
  remaining-time field.
* `CurrenCoord` is misspelled in the protocol and is a comma-separated string.
* `Progress` is an integer percentage, already computed. It lagged `CurrentLayer`
  in the capture above, so treat it as approximate.
* Temperatures are floats with far more precision than the sensor has.

### The attributes object

Captured live from `sdcp/attributes`:

```json
{
  "Name": "Centauri Carbon",
  "MachineName": "Centauri Carbon",
  "BrandName": "ELEGOO",
  "ProtocolVersion": "V3.0.0",
  "FirmwareVersion": "V1.4.49",
  "XYZsize": "218.88x128.88x220",
  "MainboardIP": "192.168.128.143",
  "MainboardMAC": "a4:e8:8d:2f:c5:09",
  "MainboardID": "5c441dd30105041800009c0000000000",
  "SDCPStatus": 0,
  "NumberOfVideoStreamConnected": 1,
  "MaximumVideoStreamAllowed": 4,
  "NetworkStatus": "wlan",
  "UsbDiskStatus": 0,
  "Capabilities": ["FILE_TRANSFER", "PRINT_CONTROL", "VIDEO_STREAM"],
  "SupportFileType": ["gcode"],
  "DevicesStatus": {
    "SgStatus": 1, "ZMotorStatus": 1, "XMotorStatus": 1, "YMotorStatus": 1
  },
  "CameraStatus": 1,
  "RemainingMemory": 6026862592,
  "TLPNoCapPos": 0, "TLPStartCapPos": 0, "TLPInterLayers": 0
}
```

`Attributes` does not push reliably on every firmware, so the `MainboardID` must
be persisted at config time rather than learned at runtime. `MainboardID` was not
exposed on any HTTP endpoint on this printer: `/sdcp/info` and `/sdcp/status` both
returned 404. The WebSocket is the only source.

### The file list

Request `Cmd` 258 with `{"Url": "/local"}`. The `FileList` arrives on
`sdcp/response` in the frame after the `Ack` frame:

```json
{
  "name": "/local/ECC_0.4_Cube_eSUN PLA+ _0.2_2m24s.gcode",
  "type": 1,
  "CreateTime": 1789329328,
  "FileSize": 44380,
  "LayerHeight": 0,
  "TotalLayers": 2,
  "EstFilamentLength": 0
}
```

`type` 1 is a file. Names carry the full `/local/` prefix, which has to be
stripped before the name is passed to start print. `Cmd` 320 returned 49 task ids
under `HistoryData`. `Cmd` 324 (material data) produced an acknowledgement-shaped
reply and no payload on this firmware.

## Camera

`http://<host>:3031/video`

Confirmed live:

| Property | Observed |
| --- | --- |
| Status | `HTTP/1.0 200 OK` |
| Content type | `multipart/x-mixed-replace; boundary=--foo` |
| Frame type | `image/jpeg`, each part a complete JPEG |
| Frames in 6 seconds | 59, so roughly 10 fps |
| Bytes in 6 seconds | 2.7 MB, so roughly 45 KB per frame |
| Auth | none |

During the first probing the stream answered without any command. After a power
cycle, though, the camera stayed dark until the printer's page had been opened, and
the page sends `EDIT_PRINTER_VIDEO_STREAMING` (386) with `{"Enable": 1}` before it
shows the camera, reading `VideoUrl` from the answer. The adapter sends the same
request once per connection before it reads the camera, and again after the camera
failed.

A frame is extracted by reading bytes until the JPEG start-of-image marker
`FF D8` and then until the end-of-image marker `FF D9`. Do not trust
`Content-Length` to arrive with the part headers; one upstream build emits a
literal `%d` in its `Expires` header, which shows the server is hand-rolled.

## What is not known

These are open, and the integration must degrade rather than assume:

* Whether `Cmd` 128 is survivable on this model. Never tested here, deliberately.
* The maximum upload size, and the exact multipart field set for
  `POST /uploadFile/upload`. The route is reported by third-party sources and was
  not exercised here, because uploading to a printer mid-print is not safe.
* What `Cmd` 386 answers on a cold start. The request is the page's own; its
  effect after a power cycle has been reported by a user, not yet measured here.
* The meaning of `PrintInfo.Status` 13. The printer was printing, so bits are in
  use; the full enumeration is not documented for this firmware.

## Tools

| Tool | Purpose |
| --- | --- |
| `tools/probe_sdcp.py` | connect, send the read-only queries, dump every raw frame |
| `tools/dump_status.py` | print the status, attributes and file-list schemas |
| `tools/mine_printer_bundle.py` | walk the printer's own JS bundle for its endpoint and command tables |
| `tools/extract_printer_commands.py` | dump the `getMsgBodyString` call sites and status bit checks |
| `tools/verify_sdcp.py` | acceptance probe, read-only by default |

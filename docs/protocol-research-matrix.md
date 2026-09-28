# 3D printer control protocols: local network API reference

Research date: 2026-09-17. Scope: what a Home Assistant custom integration needs to talk to a mixed printer fleet over the LAN.

Every endpoint string and field name below was read from a file or documentation page fetched during this session, and is cited inline. Anything I could not confirm from a fetched source is marked **UNCONFIRMED**. Inferred statements are labelled as inference.

## How to read the evidence tags

- **[src]** read from fetched source code. Highest confidence, includes the exact string.
- **[doc]** read from fetched official protocol documentation.
- **[inf]** my inference from the above, not directly stated by any source.
- **UNCONFIRMED** no fetched source supports this.

## NORMALIZED CAPABILITY MATRIX

| Protocol | Local-only | REST | WS | MQTT | Camera | start / pause / resume / cancel | Temps | Progress | File upload | Auth type | Default port |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Klipper via Moonraker | Yes | Yes | Yes, `ws://host:7125/websocket` | Moonraker can bridge to a broker; not the printer channel | via `server/webcams/list` URLs (MJPEG) | all four | Yes | Yes | Yes (multipart) | API key `X-Api-Key`, or JWT Bearer, or trusted IP | 7125 HTTP/WS |
| OctoPrint | Yes | Yes | Yes, SockJS at `/sockjs` | No | MJPEG + snapshot URL from `api/settings` | start / pause / resume / cancel | Yes | Yes | Yes (multipart) | `X-Api-Key` header, or `Authorization: Bearer <key>`, or `?apikey=` | **UNCONFIRMED** (5000 is common) |
| PrusaLink (local) | Yes | Yes | No | No | job thumbnail JPEG only | pause / resume / continue / cancel only. No start | Yes | Yes | `upload_by_put` advertised; request UNCONFIRMED | HTTP Digest, user `maker` | **UNCONFIRMED** |
| Prusa Connect | No, cloud | Yes (cloud) | **UNCONFIRMED** | No | via Connect cloud | **UNCONFIRMED** | **UNCONFIRMED** | **UNCONFIRMED** | **UNCONFIRMED** | account token | 443 |
| Bambu Lab (LAN MQTT) | Reads yes. Writes need LAN-only plus Developer Mode on ACS firmware | No | No | Yes, TLS 8883 | X1-family RTSP on 322, A1/P1-family JPEG over 6000 | start / pause / resume / stop, gated by the `print.fun` bit | Yes | Yes | FTPS 990 | MQTT user `bblp`, password = LAN access code, SNI required | 8883 MQTT, 990 FTPS, 322 RTSP, 6000 JPEG |
| Duet RepRapFirmware | Yes | Yes | SBC only, `ws://host/machine?sessionKey=` | No, RRF is an MQTT client | none (thumbnail entity only) | all four | Yes | computed, not reported | Yes (`rr_upload`) | none, or password plus `X-Session-Key` header | 80, MQTT client to 1883 |
| Marlin over serial bridge | Yes | No native HTTP | No | No | n/a | n/a | n/a | n/a | n/a | n/a | serial only |
| Anycubic Kobra 2/3/S1/X | Yes | Only `/info` and upload | No | Yes, TLS 9883, via signed handshake | HTTP-FLV H.264 on 18088 | pause / resume / stop. Start print only via `print` `start` | Yes | Yes | Yes, `gcode_upload?s=` | printer-issued token, MD5 signature, AES-CBC | 18910 handshake, 9883 MQTT, 18088 camera |
| Anycubic Photon (old / Mono X) | Yes | No, it is a G-code text protocol | No | No | No | all four | Yes | Yes | Yes (`M28`) | none | UDP 3000, or TCP 6000 |
| Creality K1/K2/Ender 3 V3 | Yes | No REST, port 80 UI only | Yes, `ws://host:9999` subprotocol `wsslicer`, no auth | No | MJPEG 8080, or WebRTC 8000 | pause / resume / stop. Start print path UNCONFIRMED | Yes | Yes | UNCONFIRMED | none | 9999 WS, 8080 MJPEG, 8000 WebRTC, 80 UI |
| Creality Sonic Pad | Yes | UNCONFIRMED | UNCONFIRMED | No | UNCONFIRMED | via Moonraker if present | Yes | Yes | UNCONFIRMED | root account from the UI | UNCONFIRMED |

## 1. Klipper via Moonraker

Voron, RatRig, Creality K1/K2 with Moonraker, Elegoo with Klipper, Prusa with Klipper, Snapmaker with Moonraker.

### Important correction

There is no Moonraker integration in Home Assistant core. I fetched `https://api.github.com/repos/home-assistant/core` and confirmed `default_branch` is `dev`, then fetched `https://api.github.com/repos/home-assistant/core/contents/homeassistant/components/moonraker` and it returned `{"message":"Not Found"}`. The same 404 came from `.../moonraker/coordinator.py` and `.../moonraker/api.py`. The real reference implementation is the HACS custom integration `marcolivierarsenault/moonraker-home-assistant`.

### Base URL and ports

- HTTP and WebSocket share port **7125**. Read from source: `DEFAULT_PORT = 7125` in [custom_components/moonraker/const.py](https://raw.githubusercontent.com/marcolivierarsenault/moonraker-home-assistant/main/custom_components/moonraker/const.py) `[src]`.
- The HA client constructor defaults `port: int = 7125` and passes `api_key` and `ssl` through to a WebSocket client, in [custom_components/moonraker/api.py](https://raw.githubusercontent.com/marcolivierarsenault/moonraker-home-assistant/main/custom_components/moonraker/api.py) `[src]`.
- Bearer token support is documented at [moonraker authorization](https://moonraker.readthedocs.io/en/latest/external_api/authorization/) `[doc]`.

### Authentication

- Untrusted clients must send either a JSON Web Token as `Authorization: Bearer <jwt>` or an API key as `X-Api-Key: <key>`, per [moonraker authorization](https://moonraker.readthedocs.io/en/latest/external_api/authorization/) `[doc]`.
- `GET /access/info` returns `login_required` and `trusted`, so an integration can detect whether it needs a key at all `[doc]`.
- `GET /access/api_key` returns the current key. `POST /access/api_key` rotates it `[doc]`.
- `GET /access/oneshot_token` returns a token usable as `?token=<base32>` for requests that cannot set headers, and it expires in 5 seconds `[doc]`.
- The HACS integration stores the credential under `CONF_API_KEY = "api_key"` `[src]`.

### Works without internet

Yes. Moonraker is a local server on the printer host. The update manager and announcements features reach GitHub, but state, control, files, and the WebSocket are local. `[doc]` plus `[inf]`.

### State read

The efficient path is one object query per poll. From [moonraker printer administration](https://moonraker.readthedocs.io/en/latest/external_api/printer/) `[doc]` and the [Moonraker basic print status tutorial](https://moonraker.readthedocs.io/en/latest/external_api/introduction/) `[doc]`:

`GET /printer/objects/query?webhooks&virtual_sdcard&print_stats&gcode_move&toolhead&extruder&heater_bed&fan`

Response shape is `{"eventtime": <float>, "status": {<object>: {...}}}` `[doc]`.

| Value wanted | Object path | Source |
| --- | --- | --- |
| Klipper host state `ready`/`startup`/`shutdown`/`error` | `webhooks.state`, human text in `webhooks.state_message` | [Klipper status reference](https://www.klipper3d.org/Status_Reference.html) `[doc]` |
| Print state | `print_stats.state`, one of `standby`, `printing`, `paused`, `complete`, `error`, `cancelled` | [Moonraker introduction](https://moonraker.readthedocs.io/en/latest/external_api/introduction/) `[doc]` for the first five; `cancelled` from the HACS `PRINTSTATES` enum in [const.py](https://raw.githubusercontent.com/marcolivierarsenault/moonraker-home-assistant/main/custom_components/moonraker/const.py) `[src]` |
| Nozzle temp + target | `extruder.temperature`, `extruder.target` | [Klipper status reference, heater](https://www.klipper3d.org/Status_Reference.html) `[doc]` |
| Bed temp + target | `heater_bed.temperature`, `heater_bed.target` | same `[doc]` |
| Chamber temp | `temperature_sensor <name>.temperature`, typically a `temperature_sensor chamber` object | same `[doc]` |
| Current layer / total layers | `print_stats.info.current_layer`, `print_stats.info.total_layer` | same `[doc]` |
| Progress percent | `virtual_sdcard.progress` (0.0 to 1.0), or `display_status.progress` which prefers the last `M73` | same `[doc]` |
| Remaining time | no single field. Compute `metadata.estimated_time - print_stats.print_duration`, or scale by `virtual_sdcard.progress` | [Moonraker introduction](https://moonraker.readthedocs.io/en/latest/external_api/introduction/) `[doc]` |
| Filename | `print_stats.filename`, raw path in `virtual_sdcard.file_path` | [Klipper status reference](https://www.klipper3d.org/Status_Reference.html) `[doc]` |
| Speed factor / flow factor | `gcode_move.speed_factor`, `gcode_move.extrude_factor`, both floats where 1.0 is 100 percent | same `[doc]` |
| Fan speed | `fan.speed`, float 0.0 to 1.0. `fan.rpm` needs a tachometer pin | same `[doc]` |
| Position | `toolhead.position` or `gcode_move.gcode_position`, sent as a list where the first three entries are X, Y, Z | same `[doc]` |
| Homed axes | `toolhead.homed_axes`, a string containing some of `x`, `y`, `z` | same `[doc]` |

Metadata for time estimates: `GET /server/files/metadata?filename=<path>` returns `estimated_time` in seconds, plus `layer_height`, `object_height`, `filament_total`, `slicer`, and `thumbnails[]` `[doc]`.

### Commands

| Action | Request | Source |
| --- | --- | --- |
| Start print of an existing file | `POST /printer/print/start?filename=test_print.gcode`, or JSON-RPC `printer.print.start` with `{"filename": "..."}` | [printer admin](https://moonraker.readthedocs.io/en/latest/external_api/printer/) `[doc]` |
| Pause | `POST /printer/print/pause` or `printer.print.pause` | same `[doc]` |
| Resume | `POST /printer/print/resume` or `printer.print.resume` | same `[doc]` |
| Cancel | `POST /printer/print/cancel` or `printer.print.cancel` | same `[doc]` |
| Emergency stop | `POST /printer/emergency_stop` or `printer.emergency_stop`. Documented as an immediate halt into `shutdown` | same `[doc]` |
| Host restart | `POST /printer/restart`. Firmware restart is `POST /printer/firmware_restart` | same `[doc]` |
| Set nozzle temp | `POST /printer/gcode/script` with `{"script": "M104 S220"}` | [printer admin, GCode APIs](https://moonraker.readthedocs.io/en/latest/external_api/printer/) `[doc]` |
| Set bed temp | `POST /printer/gcode/script` with `{"script": "M140 S60"}` | same `[doc]` |
| Home | `POST /printer/gcode/script` with `{"script": "G28"}` | same `[doc]` |
| Jog / extrude | `POST /printer/gcode/script` with a `G1` script, for example `{"script": "G91\nG1 X10 F3000\nG90"}` | same endpoint `[doc]`. Klipper also exposes `FORCE_MOVE` and `SET_KINEMATIC_POSITION`, listed in the `/printer/gcode/help` response `[doc]` |
| Set speed factor | `M220 S<percent>` via the script endpoint, mirrored at `gcode_move.speed_factor` | `/printer/gcode/help` lists `SET_VELOCITY_LIMIT` `[doc]`; `M220` mapping to `speed_factor` from [Klipper status reference](https://www.klipper3d.org/Status_Reference.html) `[doc]` |
| Set flow factor | `M221 S<percent>` via the script endpoint, mirrored at `gcode_move.extrude_factor` | same `[doc]` |

The HACS integration exposes the same set as named enum members, `printer.print.cancel`, `printer.print.pause`, `printer.print.resume`, `printer.emergency_stop`, `printer.gcode.script`, in [const.py](https://raw.githubusercontent.com/marcolivierarsenault/moonraker-home-assistant/main/custom_components/moonraker/const.py) `[src]`.

### File list and upload

- List: `GET /server/files/list?root=gcodes`, returning objects with `path`, `modified`, `size`, `permissions` `[doc]`.
- Directory: `GET /server/files/directory?path=gcodes/<dir>&extended=true` `[doc]`.
- Roots: `GET /server/files/roots` `[doc]`.
- Upload: `POST /server/files/upload`. The route is registered in source as `self.register_upload_handler("/server/files/upload")` in [moonraker/components/application.py](https://raw.githubusercontent.com/Arksine/moonraker/master/moonraker/components/application.py) `[src]`.
- Multipart form fields, read from the `FileUploadHandler` `_targets` dict in the same file `[src]`: `root`, `print`, `path`, `checksum`, plus the file part itself. The handler builds `form_args` with keys `root`, `path`, `print`, `checksum`, `tmp_file_path`, and `current_user` `[src]`.
- `filename` is required by `_parse_upload_args` in [moonraker/components/file_manager/file_manager.py](https://raw.githubusercontent.com/Arksine/moonraker/master/moonraker/components/file_manager/file_manager.py) `[src]`. `root` defaults to `gcodes`, `path` is the relative subdirectory, and `print` is compared as the literal string `"true"` `[src]`.
- On success the handler returns HTTP 201 with a `Location` header `[src]`.

### Camera

Moonraker does not serve camera video itself. `GET /server/webcams/list` returns entries with `stream_url` and `snapshot_url`, and `service` such as `mjpegstreamer`. `stream_url` may be a full URL or a path relative to Moonraker's host `[doc]`. The HACS integration stores `CONF_OPTION_CAMERA_STREAM` and `CONF_OPTION_CAMERA_SNAPSHOT` and has a `camera.py` platform `[src]`, [const.py](https://raw.githubusercontent.com/marcolivierarsenault/moonraker-home-assistant/main/custom_components/moonraker/const.py).

### WebSocket push

- Primary socket: `ws://host:port/websocket` `[doc]`, [Moonraker introduction](https://moonraker.readthedocs.io/en/latest/external_api/introduction/).
- Bridge socket to Klipper's raw API server: `ws://host:port/klippysocket`. It needs a oneshot token when auth is on `[doc]`.
- Subscribe with JSON-RPC `printer.objects.subscribe` and the same `objects` parameter shape as the query. The response initialises local state, and later diffs arrive as the `notify_status_update` notification `[doc]`, [JSON-RPC notifications](https://moonraker.readthedocs.io/en/latest/external_api/jsonrpc_notifications/).
- `notify_status_update` params are positional: `[<changed objects>, <klippy monotonic timestamp>]` `[doc]`.
- Other useful notifications: `notify_klippy_ready`, `notify_klippy_shutdown`, `notify_klippy_disconnected`, `notify_gcode_response`, `notify_filelist_changed` `[doc]`.
- A JSON-RPC-over-HTTP alternative exists at `POST /server/jsonrpc` for clients that cannot hold a socket `[doc]`.

### Reference implementations

- `marcolivierarsenault/moonraker-home-assistant`, files `custom_components/moonraker/const.py`, `api.py`, `camera.py`, `sensor.py`, `number.py`, `button.py`. Fetched: [const.py](https://raw.githubusercontent.com/marcolivierarsenault/moonraker-home-assistant/main/custom_components/moonraker/const.py), [api.py](https://raw.githubusercontent.com/marcolivierarsenault/moonraker-home-assistant/main/custom_components/moonraker/api.py).
- Moonraker itself: [Arksine/moonraker](https://github.com/Arksine/moonraker), files [moonraker/components/file_manager/file_manager.py](https://raw.githubusercontent.com/Arksine/moonraker/master/moonraker/components/file_manager/file_manager.py) and [moonraker/components/application.py](https://raw.githubusercontent.com/Arksine/moonraker/master/moonraker/components/application.py).
- Docs: [printer administration](https://moonraker.readthedocs.io/en/latest/external_api/printer/), [file management](https://moonraker.readthedocs.io/en/latest/external_api/file_manager/), [authorization](https://moonraker.readthedocs.io/en/latest/external_api/authorization/), [webcams](https://moonraker.readthedocs.io/en/latest/external_api/webcams/), [JSON-RPC notifications](https://moonraker.readthedocs.io/en/latest/external_api/jsonrpc_notifications/).

## 2. OctoPrint

Prusa MK3 on OctoPi, Ender 3, Anycubic running OctoPrint.

### Base URL and ports

- HA's client builds `f"{protocol}://{host}:{port}{path}"` with `port: int = 80, ssl: bool = False, path: str = "/"` as the signature default, in [pyoctoprintapi/octoprint_client.py](https://raw.githubusercontent.com/rfleming71/pyoctoprintapi/main/pyoctoprintapi/octoprint_client.py) `[src]`. OctoPi's actual default is 5000 and the HA config flow takes host, port, path, and SSL as user input `[inf]` from [coordinator.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/octoprint/coordinator.py), which reads `CONF_HOST`, `CONF_PORT`, `CONF_PATH`, and `CONF_SSL` `[src]`. The OctoPrint docs I fetched use `example.com` and `localhost:8081` in examples and never state a default port, so the default port is **UNCONFIRMED**.
- WebSocket-ish push is SockJS on the same origin. Source: `SockJSRouter(self._create_socket_connection, "/sockjs", ...)` in [src/octoprint/server/\_\_init\_\_.py](https://raw.githubusercontent.com/OctoPrint/OctoPrint/main/src/octoprint/server/__init__.py) `[src]`, and the client does `new SockJS(url + "sockjs", undefined, opts)` in [src/octoprint/static/js/app/client/socket.js](https://raw.githubusercontent.com/OctoPrint/OctoPrint/main/src/octoprint/static/js/app/client/socket.js) `[src]`.

### Authentication

From [REST API general information](https://docs.octoprint.org/en/main/api/general.html) `[doc]`:

- `X-Api-Key: <key>` header, or `Authorization: Bearer <key>`, or for testing `?apikey=<key>`.
- Keys come from Settings, an API section global key, a per-user key, or the bundled Application Keys plugin workflow. Endpoints are `plugin/appkeys/probe`, `plugin/appkeys/request`, and `plugin/appkeys/request/<id>` `[doc]` and `[src]` in [pyoctoprintapi/api.py](https://raw.githubusercontent.com/rfleming71/pyoctoprintapi/main/pyoctoprintapi/api.py) which sets `self._headers.update({"X-Api-Key": api_key})` `[src]`.
- With Access Control disabled, missing or invalid keys are treated as full admin `[doc]`.
- CSRF applies only to cookie-session auth, not API keys `[doc]`.

### Works without internet

Yes. The integration manifest declares `"iot_class": "local_polling"` and discovers over SSDP and zeroconf `_octoprint._tcp.local.` in [manifest.json](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/octoprint/manifest.json) `[src]`.

### State read

- Current state: `GET /api/printer` returns `temperature`, `sd`, and `state`. `state.text` is the human string and `state.flags` carries booleans `operational`, `paused`, `printing`, `cancelling`, `pausing`, `sdReady`, `error`, `ready`, `closedOrError` `[doc]`, [printer operations](https://docs.octoprint.org/en/main/api/printer.html).
- Temperatures: in `/api/printer`, `temperature.tool0.actual`, `temperature.tool0.target`, `temperature.tool0.offset`, and the same triples under `temperature.bed` and any other tool key `[doc]`. Standalone: `GET /api/printer/tool` and `GET /api/printer/bed` `[doc]`.
- Chamber temperature: `GET /api/printer/chamber`, present only when the active printer profile has a heated chamber, otherwise HTTP 409 `[doc]`.
- Position: there is no axis position field in `/api/printer`. Z height only appears in the push stream as `currentZ` `[doc]`, [push updates](https://docs.octoprint.org/en/main/api/push.html).
- Job: `GET /api/job` returns `job.file.name`, `job.file.origin`, `job.file.size`, `job.file.date`, `job.estimatedPrintTime`, `job.filament.tool0.length`, `job.filament.tool0.volume`, `progress.completion` (a 0.0 to 1.0 float), `progress.filepos`, `progress.printTime`, `progress.printTimeLeft`, and `state` `[doc]`, [job operations](https://docs.octoprint.org/en/main/api/job.html).
- Current layer and total layers: not present in the REST response. The push stream carries `progress` and `currentZ` only `[doc]`. Treat layer counts as **UNCONFIRMED** for OctoPrint.
- Print speed and flow: not exposed as percentages by the REST API. Setting them is possible through print head `feedrate` and tool `flowrate` commands `[doc]`. Reading the current factor back is **UNCONFIRMED**.

The HA integration reads two calls per cycle, `get_job_info()` and `get_printer_info()`, and treats an HTTP 409 from the printer call as "printer offline, keep last reading", in [coordinator.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/octoprint/coordinator.py) `[src]`.

State enum values. HA maps the API's human strings to snake_case in [sensor.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/octoprint/sensor.py) `[src]`. I verified the source of those strings at the exact commit HA cites: `getStateString` at line 965 of [src/octoprint/util/comm.py](https://raw.githubusercontent.com/OctoPrint/OctoPrint/7e7d418dac467e308b24c669a03e8b4256f04b45/src/octoprint/util/comm.py) returns `Offline`, `Opening serial connection`, `Detecting serial connection`, `Connecting`, `Operational`, `Starting print from SD`, `Starting to send file to SD`, `Starting`, `Printing from SD`, `Sending file to SD`, `Printing`, `Cancelling`, `Pausing`, `Paused`, `Resuming`, `Finishing`, `Error`, `Offline after error`, `Transferring file to SD`. HA's `_API_STATE_VALUE` dict matches this list exactly `[src]`. Note that an unmatched string yields `None`, and that `state.text` is a display string whose set is explicitly documented as not exhaustive `[doc]`.

### Commands

| Action | Request | Source |
| --- | --- | --- |
| Start print of the selected file | `POST /api/job` with `{"command": "start"}` | [job operations](https://docs.octoprint.org/en/main/api/job.html) `[doc]` |
| Select a file then start | `POST /api/files/local/<path>` with `{"command": "select", "print": true}` | [file operations](https://docs.octoprint.org/en/main/api/files.html) `[doc]` |
| Pause | `POST /api/job` with `{"command": "pause", "action": "pause"}` | [job operations](https://docs.octoprint.org/en/main/api/job.html) `[doc]` |
| Resume | `POST /api/job` with `{"command": "pause", "action": "resume"}` | same `[doc]` |
| Toggle pause | `POST /api/job` with `{"command": "pause", "action": "toggle"}`, also the default when `action` is absent | same `[doc]` |
| Cancel | `POST /api/job` with `{"command": "cancel"}` | same `[doc]` |
| Restart paused job | `POST /api/job` with `{"command": "restart"}` | same `[doc]` |
| Emergency stop | No dedicated endpoint. Send arbitrary G-code via `POST /api/printer/command` with `{"command": "M112"}`, documented in [printer operations](https://docs.octoprint.org/en/main/api/printer.html) `[doc]`. Confirm actual firmware reaction per printer | `[doc]` for the endpoint, `[inf]` for the emergency semantics |
| Set nozzle temp | `POST /api/printer/tool` with `{"command": "target", "targets": {"tool0": 220}}` | [printer operations](https://docs.octoprint.org/en/main/api/printer.html) `[doc]` |
| Set bed temp | `POST /api/printer/bed` with `{"command": "target", "target": 75}` | same `[doc]` |
| Set chamber temp | `POST /api/printer/chamber` with `{"command": "target", "target": 50}` | same `[doc]` |
| Home | `POST /api/printer/printhead` with `{"command": "home", "axes": ["x", "y"]}` | same `[doc]` |
| Jog | `POST /api/printer/printhead` with `{"command": "jog", "x": 10, "y": -5, "z": 0.02}` | same `[doc]` |
| Extrude / retract | `POST /api/printer/tool` with `{"command": "extrude", "amount": 5}` | same `[doc]` |
| Set speed factor | `POST /api/printer/printhead` with `{"command": "feedrate", "factor": 105}`, range 50 to 200 percent | same `[doc]` |
| Set flow factor | `POST /api/printer/tool` with `{"command": "flowrate", "factor": 95}`, range 75 to 125 percent | same `[doc]` |
| Connect / disconnect printer | `POST /api/connection` with `{"command": "connect"}` or `{"command": "disconnect"}` | [connection handling](https://docs.octoprint.org/en/main/api/connection.html) `[doc]`, and constants in [pyoctoprintapi/const.py](https://raw.githubusercontent.com/rfleming71/pyoctoprintapi/main/pyoctoprintapi/const.py) `[src]` |
| System reboot / restart / shutdown | `POST /api/system/commands/core/<action>` with action `reboot`, `restart`, or `shutdown` | [pyoctoprintapi/api.py](https://raw.githubusercontent.com/rfleming71/pyoctoprintapi/main/pyoctoprintapi/api.py) sets `SYSTEM_COMMAND_ENDPOINT = "/api/system/commands"` `[src]`, [system API](https://docs.octoprint.org/en/main/api/system.html) `[doc]` |

`pyoctoprintapi` builds the bed URL as `f"{self._base_url}/{BED_ENDPOINT}"` while `_base_url` already ends in `/`, producing a double slash. OctoPrint tolerates it, but a new integration should normalise the join `[src]` plus `[inf]`.

### File list and upload

- List: `GET /api/files`, `GET /api/files/local`, `GET /api/files/sdcard`, with `force` and `recursive` query parameters `[doc]`.
- Upload: `POST /api/files/<location>` as `multipart/form-data` with a required `Content-Length` header. Form fields are `file`, `path`, `select`, `print`, `userdata`, and `foldername` `[doc]`, [file operations](https://docs.octoprint.org/en/main/api/files.html).
- Filenames may use the RFC 5987 `filename*` form for non-ASCII names, and OctoPrint decodes UTF-8 then ISO-8859-1 `[doc]`.
- Success is HTTP 201 with a `Location` header and a body containing `files`, `done`, `effectiveSelect`, `effectivePrint` `[doc]`.

### Camera

HA's OctoPrint camera reads `api/settings` and uses its `webcam` section, then subclasses `MjpegCamera` with `mjpeg_url=camera_settings.stream_url` and `still_image_url=camera_settings.external_snapshot_url`, in [camera.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/octoprint/camera.py) `[src]`. So the format is MJPEG multipart for the stream plus a separate JPEG snapshot URL, both supplied by the user's OctoPrint webcam plugin config rather than by a fixed OctoPrint path.

### WebSocket push

- SockJS endpoint is `/sockjs`, confirmed in source above `[src]`.
- Message types are `connected`, `current`, `history`, `event`, `slicingProgress`, `plugin`, and `reauthRequired` `[doc]`, [push updates](https://docs.octoprint.org/en/main/api/push.html).
- `current` and `history` carry `state`, `job`, `progress`, `currentZ`, `temps`, `offsets`, `logs`, `messages`, `resends`, `plugins` `[doc]`.
- Client to server messages: `auth` (expected as `<userid>:<sessionkey>`), `subscribe` with keys `state`, `plugins`, `events`, and `throttle` `[doc]`.
- Subscribing to state requires the `STATUS` permission on the socket, so the `auth` message matters even with an API key `[doc]`.

### Reference implementations

- HA core `octoprint`: [coordinator.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/octoprint/coordinator.py), [sensor.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/octoprint/sensor.py), [camera.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/octoprint/camera.py), [manifest.json](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/octoprint/manifest.json).
- `rfleming71/pyoctoprintapi`, files [api.py](https://raw.githubusercontent.com/rfleming71/pyoctoprintapi/main/pyoctoprintapi/api.py), [octoprint_client.py](https://raw.githubusercontent.com/rfleming71/pyoctoprintapi/main/pyoctoprintapi/octoprint_client.py), [printer.py](https://raw.githubusercontent.com/rfleming71/pyoctoprintapi/main/pyoctoprintapi/printer.py), [const.py](https://raw.githubusercontent.com/rfleming71/pyoctoprintapi/main/pyoctoprintapi/const.py).
- OctoPrint itself: [src/octoprint/static/js/app/client/socket.js](https://raw.githubusercontent.com/OctoPrint/OctoPrint/main/src/octoprint/static/js/app/client/socket.js), [src/octoprint/server/\_\_init\_\_.py](https://raw.githubusercontent.com/OctoPrint/OctoPrint/main/src/octoprint/server/__init__.py), [src/octoprint/util/comm.py at the pinned commit](https://raw.githubusercontent.com/OctoPrint/OctoPrint/7e7d418dac467e308b24c669a03e8b4256f04b45/src/octoprint/util/comm.py).

## 3. PrusaLink and Prusa Connect

### PrusaLink is the local protocol. Prusa Connect is cloud.

HA core has a `prusalink` integration and no Prusa Connect integration path. The manifest is `"integration_type": "device"`, `"iot_class": "local_polling"`, and it discovers via DHCP `macaddress: 109C70*` in [manifest.json](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/manifest.json) `[src]`. Prusa Connect itself is a cloud service and its local/API surface was not fetched in this session, so every Prusa Connect cell is **UNCONFIRMED**.

### Base URL and ports

- The library appends paths to a user-supplied host. HA's config flow normalises `host` by stripping a trailing slash and prefixing `http://` when no scheme is given, in [config_flow.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/config_flow.py) `[src]`.
- The API version is `2.0.0` per `/api/version`, and `ensure_printer_is_supported` accepts printers whose `api` is at least 2.0.0, plus a documented workaround for `PrusaLink I3MK3` or `PrusaLink I3MK2` at server 0.7.2 or newer `[src]`.
- The real spec HA points at is [prusa3d/Prusa-Link-Web spec/openapi.yaml](https://github.com/prusa3d/Prusa-Link-Web/blob/master/spec/openapi.yaml) `[src]`, cited in [pyprusalink/types.py](https://raw.githubusercontent.com/home-assistant-libs/pyprusalink/main/pyprusalink/types.py).
- Default port **80 over plain HTTP** is what bundled firmware serves, and 80 is why DHCP discovery alone is enough `[inf]` from the scheme normalisation plus `109C70*` DHCP matching. The exact port is **UNCONFIRMED** from a source.

### Authentication

- HTTP Digest. `client.py` constructs `DigestAuthWorkaround(username=username, password=password)` and passes `auth=self._auth` on every request, in [pyprusalink/client.py](https://raw.githubusercontent.com/home-assistant-libs/pyprusalink/main/pyprusalink/client.py) `[src]`.
- The username is hardcoded to `maker` in the config-flow schema, with the comment that `maker` is hardcoded in firmware and a link to `Prusa-Firmware-Buddy/lib/WUI/wui_api.h` `[src]`, [config_flow.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/config_flow.py).
- The password is the printer's PrusaLink password printed on the LCD `[inf]`. The library handles a firmware bug by omitting the `algorithm` field from the digest response for non-qop challenges `[src]`.
- Status codes map to typed errors: 401 `InvalidAuth`, 409 `Conflict`, 404 `NotFound` `[src]`.

### Works without internet

Yes for PrusaLink. `"iot_class": "local_polling"` and DHCP discovery only require the LAN `[src]`. Prusa Connect requires an account `[inf]`.

### State read

All paths come from [pyprusalink/\_\_init\_\_.py](https://raw.githubusercontent.com/home-assistant-libs/pyprusalink/main/pyprusalink/__init__.py) `[src]`.

| Endpoint | Returns |
| --- | --- |
| `GET /api/version` | `api`, `server`, `nozzle_diameter`, `text`, `hostname`, `capabilities`, and on firmware 6.5.1+ also `firmware` and `printer` `[src]` |
| `GET /api/v1/info` | `mmu`, `name`, `location`, `farm_mode`, `nozzle_diameter`, `min_extrusion_temp`, `serial`, `sd_ready`, `active_camera`, `hostname`, `port`, `network_error_chime`. Field presence varies by firmware and model, so use `dict.get()` `[src]` |
| `GET /api/v1/status` | `printer` and optionally `job` and `storage` `[src]` |
| `GET /api/v1/job` | job dict, or HTTP 204 with no body when no job is running `[src]` |
| `GET /api/printer` | legacy printer status, used for the `telemetry.material` sensor `[src]` |
| `GET /api/v1/storage` | `storage_list[]` `[src]` |
| `GET /api/v1/transfer` | active transfer, or HTTP 204 `[src]` |

Field names under `/api/v1/status`, from `PrinterStatusInfo` in [types.py](https://raw.githubusercontent.com/home-assistant-libs/pyprusalink/main/pyprusalink/types.py) `[src]`:

- `printer.state`. The string enum `PrinterState` is `IDLE`, `BUSY`, `PRINTING`, `PAUSED`, `FINISHED`, `STOPPED`, `ERROR`, `ATTENTION`, `READY` `[src]`. The HA sensor lowercases it `[src]`, [sensor.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/sensor.py).
- `printer.temp_nozzle`, `printer.target_nozzle`, `printer.temp_bed`, `printer.target_bed` `[src]`.
- `printer.axis_x`, `printer.axis_y`, `printer.axis_z`. `axis_x` and `axis_y` are absent on some models, and HA gates those sensors on the key being present `[src]`.
- `printer.flow` and `printer.speed`, both integers, documented in HA as `PERCENTAGE` `[src]`.
- `printer.fan_hotend` and `printer.fan_print`, in RPM per HA's `REVOLUTIONS_PER_MINUTE` unit `[src]`.
- `printer.status_printer` and `printer.status_connect`, each `{"ok": bool, "message": str}` `[src]`.
- Chamber temperature: **not present**. There is no chamber field in `PrinterStatusInfo` `[src]`.
- Layer counts: **not present** in `PrinterStatusInfo` or `JobInfo` `[src]`.

Under `/api/v1/job`, from `JobInfo` `[src]`: `id`, `state`, `progress` (int), `time_remaining` (nullable), `time_printing`, `inaccurate_estimates`, `serial_print`, and `file` with `name`, `path`, `m_timestamp`, `display_name`, `size`, `meta`, and `refs` containing `download`, `icon`, `thumbnail` `[src]`.

Note the type mismatch to handle: `printer.state` is typed `PrinterState`, but `job.state` is typed plain `str`, so comparisons against the enum must be value comparisons `[src]` plus `[inf]`.

### Commands

All read from [pyprusalink/\_\_init\_\_.py](https://raw.githubusercontent.com/home-assistant-libs/pyprusalink/main/pyprusalink/__init__.py) `[src]`:

| Action | Request |
| --- | --- |
| Pause | `PUT /api/v1/job/{job_id}/pause` |
| Resume | `PUT /api/v1/job/{job_id}/resume` |
| Continue after timelapse | `PUT /api/v1/job/{job_id}/continue` |
| Cancel | `DELETE /api/v1/job/{job_id}` |
| Cancel transfer | `DELETE /api/v1/transfer/{transfer_id}` |

**Start print of an existing file is not implemented in `pyprusalink`.** The library has no start method, and HA's button platform only exposes cancel, pause, resume, and continue, gated on `printer.state` being `PRINTING`, `PAUSED`, or `ATTENTION`, in [button.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/button.py) `[src]`. The PrusaLink v1 OpenAPI spec may define a job start, but I did not fetch it, so a start endpoint is **UNCONFIRMED**.

No set-temperature, home, jog, extrude, speed, or flow command exists in `pyprusalink` `[src]`. PrusaLink's local API for those is **UNCONFIRMED**. Practically, a Prusa Buddy printer exposes a G-code passthrough on some API generations, but I did not confirm it.

### File list and upload

- `GET /api/v1/storage` returns `storage_list[]`, each with `type`, `path`, `available`, and optionally `name`, `read_only`, `free_space`, `total_space`, `print_files`, `system_files` `[src]`.
- A `Capabilities` type exists with `upload_by_put`, and `/api/version` returns `capabilities` keyed `upload-by-put` `[src]`. The upload call itself is not implemented in `pyprusalink` beyond that, so the exact request is **UNCONFIRMED**. The `upload-by-put` name implies a `PUT` with the file body.
- `get_file(path)` does a plain `GET` on an arbitrary path, which is how thumbnails and icons are fetched `[src]`.

### Camera

There is no live camera stream in the HA integration. The camera entity is a still-image preview of the current job: it reads `job["file"]["refs"]["thumbnail"]` and fetches that path as bytes, caching by path, in [camera.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/camera.py) `[src]`. So the format is a JPEG or PNG snapshot, and there is no MJPEG or RTSP from PrusaLink itself. `/api/v1/info` has an `active_camera` boolean `[src]`; what that camera is and how it is streamed is **UNCONFIRMED**.

### WebSocket push

None. `pyprusalink` uses only request and stream requests `[src]`. Polling cadence in HA is 30 seconds normally, 5 seconds for 30 seconds after an expected change, and the coordinator also debounces `homeassistant.update_entity` calls to a 1 second floor, in [coordinator.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/coordinator.py) `[src]`.

### Reference implementations

- HA core `prusalink`: [coordinator.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/coordinator.py), [sensor.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/sensor.py), [button.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/button.py), [binary_sensor.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/binary_sensor.py), [camera.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/camera.py), [config_flow.py](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/prusalink/config_flow.py).
- `home-assistant-libs/pyprusalink`, files [\_\_init\_\_.py](https://raw.githubusercontent.com/home-assistant-libs/pyprusalink/main/pyprusalink/__init__.py), [client.py](https://raw.githubusercontent.com/home-assistant-libs/pyprusalink/main/pyprusalink/client.py), [types.py](https://raw.githubusercontent.com/home-assistant-libs/pyprusalink/main/pyprusalink/types.py).
- Prusa's own OpenAPI spec, cited by the library: [prusa3d/Prusa-Link-Web](https://github.com/prusa3d/Prusa-Link-Web/blob/master/spec/openapi.yaml).

## 4. Bambu Lab (X1C, P1S, A1)

Three premises in the brief are wrong. All three are load-bearing.

1. There is no `bambu_lab` integration in Home Assistant core. `https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/bambu_lab/pybambu/bambu_client.py`, the contents listing for that directory, and `https://www.home-assistant.io/integrations/bambu_lab/` all returned 404. The only implementation is the HACS custom integration [greghesp/ha-bambulab](https://github.com/greghesp/ha-bambulab), which bundles its own `pybambu`. Every HA-side quote below is from that repo, not from core.
2. The camera ports are inverted relative to the brief. X1, X1C, X1E, X2D, P2S, H2C, H2D, H2DPRO, and H2S use **RTSP over TLS on port 322**. A1, A1 mini, P1P, and P1S use a **TCP 6000 JPEG frame stream**. Four independent sources agree.
3. Write commands are gated by firmware-level ACS (Authorization Control System), not just by credentials. On post-January-2025 firmware, unsigned `print.*` publishes are rejected unless the printer is in LAN-only Mode plus Developer Mode. Reads always work.

### Transport, base URLs, and authentication

| Function | Base URL and port | Auth | Source |
| --- | --- | --- | --- |
| Local MQTT, control and status | `mqtts://{PRINTER_IP}:8883`, TLS required | user `bblp`, password = LAN access code | [OpenBambuAPI mqtt.md](https://raw.githubusercontent.com/Doridian/OpenBambuAPI/main/mqtt.md) |
| Local FTPS, files | `ftps://{DEVICE_IP}:990`, implicit TLS | `bblp` / access code | [OpenBambuAPI ftp.md](https://raw.githubusercontent.com/Doridian/OpenBambuAPI/main/ftp.md) |
| RTSP camera, X1/X2/H2/P2 | `rtsps://{PRINTER_IP}:322/streaming/live/1` | `bblp` / access code, RTSP Digest | [OpenBambuAPI video.md](https://raw.githubusercontent.com/Doridian/OpenBambuAPI/main/video.md) |
| TCP 6000, role A: MJPEG frames | `{PRINTER_IP}:6000` over TLS | `bblp` / access code in a binary auth packet | video.md |
| TCP 6000, role B: file tunnel | `{PRINTER_IP}:6000`, TLS 1.2 `ECDHE-RSA-AES256-GCM-SHA384` | 64-byte auth packet | [OpenBambuAPI lan-file-tunnel.md](https://raw.githubusercontent.com/Doridian/OpenBambuAPI/main/lan-file-tunnel.md) |
| Plain MQTT 1883 | **UNCONFIRMED, treat as absent.** No fetched source mentions it | | |
| Local HTTP or REST on the printer | **UNCONFIRMED.** The `local-printer-api.md` page is a third-party management server and says so | | |

TLS behaviour, from [OpenBambuAPI tls.md](https://raw.githubusercontent.com/Doridian/OpenBambuAPI/main/tls.md) `[doc]`:

- The printer certificate is issued by a Bambu Lab CA, and the repo ships `examples/ca_cert.pem` to trust.
- SNI is required. The certificate `CN` is the printer's serial number while you connect to an IP, so the client must pass `server_name=device_id`. The reference example does exactly this with `MQTTSClient(..., server_name=device_id)` in [examples/mqtt.py](https://raw.githubusercontent.com/Doridian/OpenBambuAPI/main/examples/mqtt.py).
- Do not disable verification generally, and clear `VERIFY_X509_STRICT` on Python 3.13+.

I confirmed the connection parameters independently in [custom_components/bambu_lab/pybambu/bambu_client.py](https://raw.githubusercontent.com/greghesp/ha-bambulab/main/custom_components/bambu_lab/pybambu/bambu_client.py) `[src]`: `self._port = 8883`, `self.client.username_pw_set("bblp", password=self._access_code)`, `self.client.subscribe(f"device/{self._serial}/report")`, `self.client.publish(f"device/{self._serial}/request", json.dumps(msg))`, and an FTPS `ftp.login(user='bblp', passwd=self._access_code)`.

A real trap found in that file: the local TLS context caps the protocol because "P2S firmware 01.02.00.00 never responds to a TLS 1.3 ClientHello, hanging the handshake until timeout" `[src]`. The context also sets `check_hostname = False` and clears `VERIFY_X509_STRICT` `[src]`.

### Works without internet

Yes for reads and writes, with conditions.

- Reads always work: "All read functionality will continue to work, regardless of firmware version" from [greghesp/ha-bambulab docs](https://docs.page/greghesp/ha-bambulab) `[doc]`.
- To write on ACS firmware: "you must put your printer into LAN Mode, and enable Developer Lan Mode. This will restore write functionality, however you will lose all connectivity to Bambu Cloud" `[doc]`, same URL.
- What breaks in LAN-only: Bambu Handy, cloud print, cloud slicing, MakerWorld, and OTA firmware update, which then needs an offline package over USB or microSD, per [SimplyPrint LAN-only mode](https://help.simplyprint.io/en/article/bambu-lab-lan-only-mode-and-developer-mode-how-to-enable-xa0hch/) and [offline firmware update](https://help.simplyprint.io/en/article/how-to-update-bambu-lab-firmware-in-lan-only-mode-jsw77c/) `[doc]`.
- Bambu's own statement: Developer Mode exists "to leave the MQTT channel, live stream, and FTP open. This feature must be manually enabled on the printer", from [blog.bambulab.com](https://blog.bambulab.com/updates-and-third-party-integration-with-bambu-connect/) `[doc]`.

### State read

Subscribe to `device/{SERIAL}/report` and publish commands to `device/{SERIAL}/request` `[doc]`, [mqtt.md](https://raw.githubusercontent.com/Doridian/OpenBambuAPI/main/mqtt.md).

Force a full dump with `{"pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1}}` `[doc]`. The HA integration uses `PUSH_ALL = {"pushing": {"sequence_id": "0", "command": "pushall"}}` in [pybambu/commands.py](https://raw.githubusercontent.com/greghesp/ha-bambulab/main/custom_components/bambu_lab/pybambu/commands.py) `[src]`.

Critical asymmetry, quoted from mqtt.md `[doc]`: "The X1 series always responds with the full object... Due to performance limitations the P1P uses the same object, but only actually responds with values that have been changed since the previous report." The same page warns to "refrain from executing this command at intervals less than 5 minutes on the P1P, as it may cause lag."

Field paths, all inside the `print` object of the report `[doc]`:

| Data | JSON key | Notes |
| --- | --- | --- |
| G-code state | `print.gcode_state` | sample value `"IDLE"`. HA's set: `failed`, `finish`, `idle`, `init`, `offline`, `pause`, `prepare`, `running`, `slicing`, `unknown` `[src]` |
| Nozzle temp and target | `print.nozzle_temper`, `print.nozzle_target_temper` | |
| Dual-extruder nozzle temps | `print.device.extruder.info[].temp` | packed. Low 16 bits current, high 16 bits target `[src]` |
| Bed temp and target | `print.bed_temper`, `print.bed_target_temper` | packed variant `print.device.bed.info.temp` |
| Chamber temp | `print.chamber_temper` | packed variant `print.device.ctc.info.temp`. Only on X1/X1E/X2D/P2S/H2/H2C/H2D/H2DPRO |
| Current layer | `print.layer_num` | |
| Total layers | `print.total_layer_num` | |
| Progress percent | `print.mc_percent` | |
| Remaining time | `print.mc_remaining_time` | **minutes**, read as `timedelta(minutes=...)` in pybambu `utils.py` `[src]` |
| Filename | `print.gcode_file`, `print.subtask_name` | |
| Speed | `print.spd_lvl` (1-4), `print.spd_mag` | `SPEED_PROFILE = {1:"silent", 2:"standard", 3:"sport", 4:"ludicrous"}` `[src]` |
| Fans | `print.cooling_fan_speed` part, `print.big_fan1_speed` aux, `print.big_fan2_speed` chamber, `print.heatbreak_fan_speed` | **raw range is 0 to 15, not 0 to 255.** pybambu divides by 15. Outbound commands use 0 to 255 `[src]` |
| Errors | `print.hms[]` with `attr` and `code`, `print.print_error` | |
| Lights | `print.lights_report[]` with `node` and `mode` | |
| Camera state | `print.ipcam.rtsp_url` | literal `"disable"` when off |
| Toolhead position | none | **UNCONFIRMED.** The full `pushall` sample contains no coordinate field and pybambu reads none |

### Commands

Publish JSON to `device/{SERIAL}/request` `[doc]`.

- Pause, resume, stop. mqtt.md shows `"param": ""` on all three and says to send them with QoS 1 `[doc]`. The HA integration omits `param` `[src]`. Corroboration on QoS from [bambuddy bambu_mqtt.py](https://raw.githubusercontent.com/maziggy/bambuddy/main/backend/app/services/bambu_mqtt.py): "Always use qos=1 for all MQTT publish calls! The printer ignores qos=0 messages when busy broadcasting status updates."
- Speed: `{"print": {"sequence_id": "0", "command": "print_speed", "param": "2"}}` with `1` silent through `4` ludicrous `[doc]`.
- Start print. mqtt.md documents the `project_file` command with `param` set to the gcode path plus `project_id`, `profile_id`, `task_id`, `subtask_id`, `subtask_name`, `file`, `url`, `md5`, `timelapse`, `bed_type`, `bed_levelling`, `flow_cali`, `vibration_cali`, `layer_inspect`, `ams_mapping`, and `use_ams` `[doc]`. The HA template uses `"url": "ftp://{file}"`, `"ams_mapping": [0]`, and spells the key `bed_leveling` `[src]`. Note the spelling drift and flag it.
- Set temperatures. There is no dedicated MQTT command. Send raw G-code: `{"print": {"sequence_id": "0", "command": "gcode_line", "param": "M104 S200\n"}}` `[doc]`. pybambu maps nozzle to `M104 S{}`, bed to `M140 S{}`, and chamber to `M141 S{}` plus `M145 P1` above 40 C in [utils.py](https://raw.githubusercontent.com/greghesp/ha-bambulab/main/custom_components/bambu_lab/pybambu/utils.py) `[src]`.
- Home: `HOME_GCODE = "G28\n"` `[src]`. Jog and extrude use G-code templates in `commands.py` `[src]`.
- Fans: `M106 P1 S{}` part, `P2` aux, `P3` chamber, `P10` secondary aux, with the value computed as `math.ceil(255 * percentage / 100)` `[src]`.
- Set flow. No dedicated command. `flow_cali` is only a start-print boolean. Pressure advance is reported as `[BMC] M900 K...` via `mc_print.push_info` `[doc]`; setting it is **UNCONFIRMED**.
- Emergency stop. **UNCONFIRMED.** No `estop` or `M112` command exists in mqtt.md or in pybambu `commands.py`.

### The ACS / Developer Mode gate

This is the part that decides whether a write works at all.

- HA's pybambu reads the marker from the `print.fun` bitfield: `MQTT_SIGNATURE_REQUIRED = 0x20000000`, with the source comments `# {"print":{"fun":"3EC1AFFF9CFF"}} <- dev mode disabled` and `# {"print":{"fun":"3EC18FFF9CFF"}} <- dev mode enabled` in [pybambu/const.py](https://raw.githubusercontent.com/greghesp/ha-bambulab/main/custom_components/bambu_lab/pybambu/const.py) `[src]`. I confirmed this file directly.
- On A1 and P1 the `fun` field is absent, so Bambuddy probes by sending `ams_filament_setting` and treating `"mqtt message verify failed"` as dev-mode OFF `[doc]`, [bambuddy](https://github.com/maziggy/bambuddy). That probe "can destabilize some firmware MQTT brokers, causing a reconnect, probe, disconnect feedback loop" `[doc]`.
- OpenBambuAPI's per-command auth table says that for a `print.*` publish, a TLS client certificate **and** a signed `header` envelope are required. Firmware responds with `result:"failed"`, `reason:"mqtt message verify failed"`, `err_code:0x05024007` when the envelope is missing and `0x05024009` when it is malformed `[doc]`, [cloud-x509-auth.md](https://raw.githubusercontent.com/Doridian/OpenBambuAPI/main/cloud-x509-auth.md).
- `pushing.*`, `info.*`, `system.*`, and `camera.*` need the certificate but no signed envelope `[doc]`.
- `gcode_line` needs an extra inner transform: the plaintext `param` is replaced by `param_enc`, the G-code RSA-encrypted with the printer's public key, before signing. Without it "the firmware appears to respond with `result:"failed"` and the gcode line is silently dropped" `[doc]`.
- The HA integration implements none of this, and it now blocks writes explicitly rather than silently failing. [coordinator.py](https://raw.githubusercontent.com/greghesp/ha-bambulab/main/custom_components/bambu_lab/coordinator.py) has:
  ```python
  if write_action:
      if self.get_model().print_fun.mqtt_signature_required:
          LOGGER.error("Printer firmware requires mqtt encryption. All control actions are blocked.")
          self._report_encryption_enabled_issue(True)
          return False
  ```
  `pybambu/models.py` wires a `PrintFun` model onto the device and `supports_feature` consumes `print_fun.mqtt_signature_required` `[src]`. `bambu_client.publish()` remains a bare `json.dumps(msg)` and never injects `header`, `sign_string`, `cert_id`, or `fun` `[src]`. Signed MQTT exists only as an unmerged draft in that project.
- Practical consequence for a new integration. The `fun` bit `0x20000000` is the single gate to read. If it is set, no `print.*` command works regardless of credentials, and there is no in-tree signed implementation to copy `[src]` plus `[inf]`.
- Firmware thresholds that set the gate, from `Features.MQTT_ENCRYPTION_FIRMWARE` in `pybambu/models.py` `[src]`: A1 family at `01.05.00.00` and later, H2D and H2D Pro at `01.01.00.00` and later, P1 family at `01.08.02.00` and later, X1 and X1C at `01.08.50.32` and later, X1E explicitly `False` with the source comment "Do not know if the X1E requires this", and anything unmatched returns `True`.
- Three sources give three different X1 figures. `models.py` says `01.08.50.32`, the greghesp docs say `01.08.05.00`, and SimplyPrint says `01.08.03.00`. Do not trust any single number. Read the bit `[src]` plus `[inf]`.
- Two HA diagnostic binary sensors are worth mirroring, from [definitions.py](https://raw.githubusercontent.com/greghesp/ha-bambulab/main/custom_components/bambu_lab/definitions.py) `[src]`: `developer_lan_mode` is `supports_feature(MQTT_ENCRYPTION_FIRMWARE) and not print_fun.mqtt_signature_required`, and `mqtt_encryption` is `supports_feature(MQTT_ENCRYPTION_FIRMWARE)`. Treat the derived flag as advisory, because there is an open bug where an A1 mini on `01.08.01.00` with dev mode off reports `developer_lan_mode` on. The raw `fun` bit is the truth `[src]`.

### File list and upload

- FTPS is the documented path: `ftps://{DEVICE_IP}:990`, implicit TLS, user `bblp`, password the access code `[doc]`, [ftp.md](https://raw.githubusercontent.com/Doridian/OpenBambuAPI/main/ftp.md).
- The HA client connects with `ImplicitFTP_TLS(context=self.local_tls_context)`, `ftp.connect(host=..., port=990, timeout=15)`, `ftp.login(user='bblp', passwd=self._access_code)`, and `ftp.prot_p()` `[src]`.
- Upload is `STOR`, listing is `LIST`, download is `RETR`, per [BambuTools/bambulabs_api ftp_client.py](https://raw.githubusercontent.com/BambuTools/bambulabs_api/main/bambulabs_api/ftp_client.py) `[src]`.
- Start-print URL scheme is model-dependent and contested. `pybambu/const.py` declares `LEGACY_SDCARD_PRINTERS = [X1, X1C, X1E, P1P, P1S, A1, A1MINI]` with the comment "Printers that use file:///sdcard/ URL format for print jobs. All other printers default to ftp:/// format." `[src]`. The live coordinator contradicts that table:
  ```python
  if '//' in filepath:
      command["print"]["url"] = filepath
  else:
      if self.config_entry.data["device_type"] in [Printers.H2C, Printers.H2S, Printers.H2D]:
          command["print"]["url"] = f"ftp:///{filepath}"
      else:
          command["print"]["url"] = f"file:///sdcard/{filepath}"
  ```
  from [coordinator.py](https://raw.githubusercontent.com/greghesp/ha-bambulab/main/custom_components/bambu_lab/coordinator.py) `[src]`. So `ftp:///` applies only to H2C, H2S, and H2D, and A2L, P2S, H2D Pro, and X2D get `file:///sdcard/`, which is the opposite of what `LEGACY_SDCARD_PRINTERS` implies. Whether that constant is consumed anywhere is **UNCONFIRMED**. Derive the scheme from what the printer echoes back in `print.url` rather than from a table `[inf]`.
- TCP 6000 file tunnel. Three mutually incompatible framing descriptions exist in the fetched sources, so this is not safe to implement from documentation. The OpenBambuAPI description is the 64-byte auth packet and 16-byte header with `mtype` `0x3001`/`0x3003` `[doc]`. The working HA implementation in [media_sources.py](https://raw.githubusercontent.com/greghesp/ha-bambulab/main/custom_components/bambu_lab/pybambu/media_sources.py) uses `MAGIC_LOGIN = 0x0101013F`, `MAGIC_CTRL = 0x0102013F`, a 16-byte header of `payload_len`, `magic`, `seq`, and a **16-byte** login payload `[src]`. The camera path in `bambu_client.py` uses the 80-byte `0x40`/`0x3000` packet `[src]`. What they do agree on is command semantics: `1` list, `4` download, `7` media ability, with `RESULT_CONTINUE = 1` and `RESULT_OK = 0` `[src]`. HA uses TCP 6000 for file list and download and falls back to FTPS, with `if self._client.tcp6000_media_supported is True: sources.append(Tcp6000MediaSource(...))` then `sources.append(Ftps990MediaSource(...))` and `TCP6000_PROBE_TTL_SECONDS = 300` `[src]`. Start with FTPS 990 for all file I/O and treat TCP 6000 as an optimisation you validate on your own hardware `[inf]`.
- HTTP upload on the printer: **UNCONFIRMED.** The only HTTP upload in any fetched source is `POST /api/upload_file_to_host` on the third-party `bambu-printer-manager` server, not on the printer `[doc]`.

### Camera, per model

Model to port mapping, from [bambuddy camera.py](https://raw.githubusercontent.com/maziggy/bambuddy/main/backend/app/services/camera.py) `[doc]`:

- RTSP for X1, X1C, X1E, X2D, H2C, H2D, H2DPRO, H2S, P2S on port 322.
- Chamber Image for A1, A1 mini, A2L, P1P, P1S on port 6000 with a custom binary protocol.

Corroborated by pybambu `models.py`, which has a `CAMERA_RTSP` feature set of H2, P2, X1, X1E, and X2, and a `CAMERA_IMAGE` set that includes A2L alongside A1 and P1 `[src]`, and independently by [coelacant1/Bambu-Lab-Cloud-API video.py](https://raw.githubusercontent.com/coelacant1/Bambu-Lab-Cloud-API/main/bambulab/video.py) `[src]`.

- RTSP URL format is `rtsps://{PRINTER_IP}:322/streaming/live/1`, user `bblp`, password the access code `[doc]`. HA builds it from the reported URL, defaulting the port to 322 and injecting credentials with `port = parsed_url.port or 322` and `credentials = f"bblp:{quote(access_code, safe='')}@"` in `utils.py` `[src]`. `get_authenticated_rtsp_url` returns `None` when the reported URL is empty or equals `"disable"` `[src]`.
- Port 6000 JPEG stream, from video.md `[doc]`: auth packet is 4 bytes payload size `0x40`, 4 bytes type `0x3000`, 4 bytes flags `0`, 4 bytes `0`, then 32 bytes ASCII username and 32 bytes ASCII password. Each frame header is 16 bytes carrying payload size, `itrack (0)`, `Flags (1)`, and `0`, followed by `payload_size` bytes of JPEG. HA verifies the JPEG magic `FF D8` at the start and `FF D9` at the end and reads in 4096-byte chunks `[src]`.
- Firmware gotchas: RTSP is off by default on H2S and H2D firmware `01.02.00.00`, where port 322 closes immediately and `ipcam.rtsp_url` reports `"disable"`; the toggle is the printer touchscreen under Settings, General, LAN Mode Liveview `[doc]`. HA's docs add that H2D, H2S, and X2D need the LAN Only Liveview option enabled `[doc]`, [ha-bambulab setup](https://docs.page/greghesp/ha-bambulab/setup). And the firmware allows exactly one camera connection at a time `[doc]`.

### WebSocket push

None. Push is MQTT on `device/{SERIAL}/report` `[doc]`. The `pushall` request is the full-state mechanism, documented above.

### Developer Mode, per model

- Bambu's official statement names X1, P1, A1, and A1 mini: "For advanced users of the X1, P1, A1, and A1 Mini... an option will be available to leave the MQTT channel, live stream, and FTP open." `[doc]`, [blog.bambulab.com](https://blog.bambulab.com/updates-and-third-party-integration-with-bambu-connect/).
- Firmware versions per [SimplyPrint](https://help.simplyprint.io/en/article/bambu-lab-lan-only-mode-and-developer-mode-how-to-enable-xa0hch/) `[doc]`: X1 series `01.08.03.00` and later, A1 series `01.05.00.00` and later, P1 series `01.08.02.00` and later, H2D `01.01.00.01` and later, and the P2 series shipped with Developer Mode from the start.
- HA's docs pin the ACS cutover at X1C `01.08.05.00`, P1 `01.08.02.00`, and A1 `01.05.00.00`, with an earlier break at P1 `01.07.00.00` `[doc]`.
- **UNCONFIRMED** for X1E, A2L, X2D, H2C, H2S, and H2D Pro.
- Does LAN mode require the printer to have been online once? Not to be reachable. LAN-only setup needs only the serial number, the IP, and the access code, all readable off the printer screen, and the HA integration has a dedicated LAN-only path `async_step_Lan` that stores empty `region`, `email`, `username`, and `auth_token` `[src]`. The cloud-linked path instead auto-fills the code as `default_access_code = device['dev_access_code']` `[src]`. Where LAN-only does need cloud or offline work is OTA firmware update `[doc]`.
- Access-code rotation is **unreconciled**. Bambuddy says "The access code changes every time LAN Only or Developer Mode is toggled" `[doc]`, while SimplyPrint says toggling LAN-only off and back on does not rotate it `[doc]`. Treat as firmware-dependent.

### Reference implementations

- [Doridian/OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI): `mqtt.md`, `video.md`, `ftp.md`, `tls.md`, `lan-file-tunnel.md`, `local-printer-api.md`, `cloud-x509-auth.md`, `examples/mqtt.py`. This is the protocol reference.
- [greghesp/ha-bambulab](https://github.com/greghesp/ha-bambulab): `custom_components/bambu_lab/pybambu/{bambu_client,const,commands,models,utils}.py`, `custom_components/bambu_lab/camera.py`. I fetched `bambu_client.py` and `const.py` directly and verified port, credentials, topics, and the `fun` bit.
- [maziggy/bambuddy](https://github.com/maziggy/bambuddy): `backend/app/services/{camera,bambu_mqtt}.py`. Best per-model camera table and `developer_mode` parsing.
- [BambuTools/bambulabs_api](https://github.com/BambuTools/bambulabs_api): `bambulabs_api/{camera_client,ftp_client}.py`.
- [coelacant1/Bambu-Lab-Cloud-API](https://github.com/coelacant1/Bambu-Lab-Cloud-API): `bambulab/video.py`.
- Bambu's own `bambulab/BambuStudio` documents no MQTT or LAN protocol. Its README says "The bambu networking plugin is based on non-free libraries." `[doc]`

## 5. Duet RepRapFirmware (Duet 3, object model over HTTP)

### The key structural fact

Two different HTTP surfaces share the same `rr_*` names, and they behave differently enough that an adapter must detect which one it is talking to.

- **Duet 3 standalone, no SBC.** RepRapFirmware's own web server. Base `http://<host>/`, port 80. GET-only `rr_*` endpoints, poll `rr_model` for state. **No WebSocket and no push.**
- **Duet 3 with an SBC running DSF.** DuetWebServer emulates every `rr_*` endpoint and returns `isEmulated: true`, and adds `/machine/*`. Real push arrives over `ws://<host>/machine?sessionKey=<key>`.

This distinction comes from [DuetSoftwareFramework rest-api docs](https://duet3d.github.io/DuetSoftwareFramework/articles/rest-api.html) and [components docs](https://duet3d.github.io/DuetSoftwareFramework/articles/components.html) `[doc]`, and from RRF's own [src/Networking/HttpResponder.cpp](https://raw.githubusercontent.com/Duet3D/RepRapFirmware/3.6-dev/src/Networking/HttpResponder.cpp) `[src]`. DSF's own client has a "PollConnector (legacy rr_* polling, used for standalone firmware)" and a "RestConnector (the /machine/* REST API plus an optional model WebSocket, used in SBC mode)" `[doc]`.

### Base URLs and ports

| | Standalone RRF 3.x | SBC with DSF |
| --- | --- | --- |
| Base URL | `http://<host>/` | `http://<host>/` |
| HTTP port | 80, from `DefaultHttpPort` in `NetworkDefs.h` `[src]` | repo default `http://0.0.0.0:5000` in `appsettings.json` `[src]`. Deployed value comes from `/opt/dsf/conf/http.json`, which I could not fetch, so the deployed port is **UNCONFIRMED** |
| Other ports | FTP 21, Telnet 23, MQTT client to an external broker, default 1883, and UDP multicast discovery on 10002, all from `NetworkDefs.h` `[src]` | same |
| WebSocket | none. `HttpResponder::ProcessRequest` accepts only `GET`, `OPTIONS`, and `POST` limited to `rr_upload` `[src]` | `WS /machine?sessionKey=<key>`, full model then JSON patches. The client sends `OK\n` and may send `PING\n` to get `PONG\n` `[src]`, [WebSocketController.cs](https://raw.githubusercontent.com/Duet3D/DuetSoftwareFramework/v3.6-dev/src/DuetWebServer/Controllers/WebSocketController.cs) |

### Authentication

- No API key exists. Auth is a web password plus a session key.
- Standalone: `GET /rr_connect?password=<pw>&sessionKey=yes` returns `{"err":0,"sessionTimeout":8000,"boardType":"..","apiLevel":N,"sessionKey":N}`. Subsequent requests send the header `X-Session-Key: <key>` `[src]`.
- If `sessionKey=yes` is omitted the key is 0 and authentication falls back to **client IP only**. Maximum 8 sessions, idle timeout 8000 ms. The `password` parameter must be present even when empty `[src]`.
- SBC: `GET /machine/connect?password=<pw>` returns `{"sessionKey":"<guid>"}`, then header `X-Session-Key` `[src]`, [SessionKeyAuthenticationHandler.cs](https://raw.githubusercontent.com/Duet3D/DuetSoftwareFramework/v3.6-dev/src/DuetWebServer/Authorization/SessionKeyAuthenticationHandler.cs). With no header it falls back to IP, and it only auto-creates a session when no password is set. The documented default password is `reprap` `[doc]`.
- **The three `rr_` endpoints the brief would naturally target are dead.** `rr_status` and `rr_config` are inside `#if 0 // removed because we ran out of flash memory on Duet 2` in `HttpResponder.cpp` `[src]`. Do not target them.

### Works without internet

Yes, entirely local on both surfaces `[doc]` plus `[inf]`.

### State read

Standalone: `GET /rr_model?key=<path>&flags=<flags>`, which returns the raw JSON body with no envelope, or `GET /rr_model` for the whole model `[src]`. SBC: `GET /machine/model`, also aliased as `GET /machine/status`, full model only with no key filtering `[doc]`. `GET /rr_model?key=..` also works on SBC and returns `{"key","flags","result"}` `[doc]`.

Flags are single letters parsed in `ObjectModel.cpp` `ObjectExplorationContext` `[src]`: `a<nnn>` array start index, `d<nnn>` max depth, `f` live-only, `i` include important, `n` include nulls, `o` include obsolete, `p` PanelDue mode, `s` short form, `v` include verbose. The default excludes verbose and obsolete. The G-code form is `M409 K"<key>" F"<flags>"` `[src]`.

Field names, all from the generated [RRF 3.6 Object Model Documentation](https://raw.githubusercontent.com/wiki/Duet3D/RepRapFirmware/Object-Model-Documentation.md) `[doc]`:

| Data | Path |
| --- | --- |
| Printer state | `state.status`. Exact enum: `disconnected`, `starting`, `updating`, `off`, `halted`, `pausing`, `paused`, `resuming`, `cancelling`, `processing`, `simulating`, `busy`, `changingTool`, `idle` |
| Nozzle temp and target | `heat.heaters[n].current`, `heat.heaters[n].active`, also `.standby` and `.state` which is one of `off`, `standby`, `active`, `fault`, `tuning`, `offline`. Tool to heater mapping is `tools[n].heaters[]` |
| Bed temp and target | `heat.bedHeaters[]` gives the heater indices, then `heat.heaters[i].current` and `.active` |
| Chamber temp | `heat.chamberHeaters[]` plus `heat.heaters[i].current` and `.active` |
| Current layer | `job.layer`, null when unavailable |
| Total layers | `job.file.numLayers` |
| Progress percent | **no field exists.** DWC computes `fractionPrinted = job.filePosition / job.file.size`, and `jobProgress = min(job.rawExtrusion / sum(job.file.filament), 1)` while printing, from [DWC model.ts](https://raw.githubusercontent.com/Duet3D/DuetWebControl/v3.6-dev/src/store/machine/model.ts) `[src]`. `job.layers[].fractionPrinted` exists but is SBC and DWC only |
| Remaining time | `job.timesLeft.file`, `.slicer`, `.filament`, `.toPause`, all in seconds. `job.timesLeft.layer` is obsolete and null |
| Filename | `job.file.fileName`, and `job.lastFileName` for the previous one |
| Elapsed time | `job.duration`, seconds |
| Speed factor | `move.speedFactor` |
| Flow factor | `move.extruders[n].factor` |
| Feedrate | `inputs[].feedRate`, mm/s |
| Fan | `fans[n].actualValue` in 0..1 with -1 for unknown, plus `.requestedValue` and `.rpm` |
| Position | `move.axes[n].userPosition` is the target of the last buffered move and `.machinePosition` is the real one, plus `.homed` and `.letter` |
| Other useful | `state.upTime`, `state.currentTool`, `sensors.endstops[n].triggered`, `volumes[]` on SBC |

One trap: a non-array query returns at most 9 axes. Request `move.axes` on its own, or use the `a` flag, if you need more `[doc]`.

### Commands

Every command is G-code. Standalone sends it as `GET /rr_gcode?gcode=<url-encoded G-code>`, which replies `{"buff":N}`, and then you fetch the reply with `GET /rr_reply` as `text/plain`. Multiple codes separated by `\n` are accepted in one `gcode` value `[src]`. SBC sends `POST /machine/code?async=false` with the G-code as the raw request body and gets `text/plain` back `[doc]`. DSF's `rr_gcode` returns `{"bufferSpace":255,"err":0}` `[doc]`.

| Action | G-code | Notes |
| --- | --- | --- |
| Start print | `M32 "<file>"` | filename quoted, virtual path such as `0:/gcodes/x.gcode`. Errors if a file is already printing `[src]` |
| Pause | `M25` | errors if not printing or already paused `[src]` |
| Resume | `M24` | `M24 P0` skips running `resume.g` `[src]` |
| Cancel or stop | `M25` then `M0` | `M0`, `M1`, and `M2` are one identical body. A host cancel only works while paused, otherwise RRF errors `"Pause the print before attempting to cancel it"`, so the cancel sequence is pause then cancel. It runs `cancel.g`, else `stop.g`, else switches off all heaters `[src]` |
| Emergency stop | `M112` | `DoEmergencyStop()` aborts the print on every channel, sets `stopped = true`, and emits `"Emergency Stop! Reset the controller to continue."`. While stopped, every non-status command returns `GCodeResult::stopped` and does nothing, so a **controller reset is required** and `state.status` reads `halted` `[src]` |
| Motors off / on | `M18` or `M84`, and `M17` to enable | optional axis letters `[src]` |
| Set nozzle temp | `M104 S<C> T<tool>` | sets both active and standby temperature. `T` selects the tool. `M104` never selects a tool `[src]` |
| Set and wait nozzle | `M109 S<C> T<tool>` | `M109` selects the tool if none is active. `S` sets active and standby, `R` sets the target with `waitWhileCooling = true` `[src]` |
| Set bed or chamber temp | `M140 S<C>` or `M141 S<C>` | `P<n>` picks the heater slot, bounded by `MaxBedHeaters` and `MaxChamberHeaters`, `H<n>` binds a heater number to that slot with `-1` to unassign, `S` sets active, and `R` sets standby. A near-absolute-zero `S` switches the heater off `[src]` |
| Wait for temps | `M116` | `[src]` |
| Home | `G28` | `case 28` calls `DoHome`. With no axis letters, `toBeHomed = AxesBitmap::MakeLowestNBits(numVisibleAxes)`, so it homes **all visible axes**. Named letters home only those. The remaining G28 parameter letters are **UNCONFIRMED** `[src]` |
| Jog or move | `G90` then `G1 X.. Y.. F..`, or `G91` for relative | `G0` and `G1` go through `DoStraightMove` `[src]` |
| Extrude or retract | `G1 E<mm> F<mm/min>` with `M83` for relative E | **Integration trap.** `G91` sets `axesRelative`, which covers X, Y, and Z only. Extrusion is governed by the separate `drivesRelative` state set by `M82` and `M83`. `LoadExtrusionFromGCode` reads E with `drivesRelative`, not `axesRelative`, so `G91` followed by `G1 E5` misbehaves unless you also send `M83` or use absolute E `[src]` |
| Speed factor | `M220 S<percent>` | stored as `move.speedFactor`, percent times 0.01. A factor below 0.01 errors with `"Invalid speed factor"` `[src]` |
| Flow factor | `M221 S<percent> [D<extruder>]` | percent times 0.01. Values below 0.01 are silently ignored, unlike `M220`, and it errors `"No tool selected"` when `D` is absent and no tool is active `[src]` |
| Fan | `M106 P<idx> S<0-255>` | `S` is converted through `GetPwmValue()`. No `P` applies to the current tool's mapped fans. Whether the scale is `S255` or `S1` is **UNCONFIRMED** `[src]` |
| Baby step | `M290 S<mm>` or `M290 Z<mm>` | `R0` means absolute `[src]` |

File operations, both surfaces:

- List standalone: `GET /rr_filelist?dir=0:/gcodes&first=0&max=-1` for JSON, or `GET /rr_files?dir=..&flagDirs=1` for a newline list `[src]`. SBC: `GET /machine/directory/{dir}` returns an array of `{type, name, date, size}` `[doc]`.
- Upload standalone: `POST /rr_upload?name=0:/gcodes/x.gcode` with optional `&crc32=<hex8>` and `&time=<ISO8601>`, a `Content-Length` header, and the raw file as the body `[src]`. SBC: `PUT /machine/file/{filename}?timeModified=<iso8601>` `[doc]`.
- Download: `GET /rr_download?name=..`, or `GET /machine/file/{filename}` on SBC `[src]`.
- The same operations exist as G-codes when you want a single transport `[src]`: `M20 [P"0:/gcodes"] [S2|S3] [R<start>] [C<max>]` lists files with `S2` and `S3` for JSON, `M30 <filename>` deletes, `M36 <file>` with `M36.1 P".." S<offset>` and `M36.2 P".." S<offset>` gives file info and thumbnail fragments, `M470 P"dir"` makes a directory, `M471 S"old" T"new" D1` renames with `D1` to overwrite, and `M472 P"path" R1` deletes with `R1` for recursive.
- Disconnect: `GET /rr_disconnect` returns `{"err":0}` `[src]`.

### The `rr_model` response envelope

Confirmed at source, not inferred. `RepRap::GetModelResponse` in [src/Platform/RepRap.cpp](https://raw.githubusercontent.com/Duet3D/RepRapFirmware/3.6-dev/src/Platform/RepRap.cpp) emits `"key":"...","flags":"...","result":<json>`, and on buffer shortage it returns `{"err":-1}` `[src]`. So on both standalone and SBC a keyed query yields `{"key","flags","result"}`, and reading `["result"]` is correct.

`M409` also accepts a leading `#` in the key to return an array length, as in `M409 K"#move.axes"`, which sets `wantArrayLength` in `GetModelResponse` `[src]`. `M409` counts as a status request, so it is answered even while halted or waiting for an acknowledgement `[src]`.

### Camera

Duet has no camera of its own. The community HA integration [repier37/hass-Duet3D](https://raw.githubusercontent.com/repier37/hass-Duet3D/master/custom_components/duet3d/camera.py) exposes the print's embedded G-code thumbnail as a camera entity by reading `job.file.thumbnails[0].data`, which is base64, and converting QOI to JPEG locally `[src]`. A separate webcam URL is not part of Duet. Any official Duet webcam plugin is **UNCONFIRMED**.

### Push mechanism

- Standalone is polling only. `HttpResponder::ProcessRequest` handles `GET`, `OPTIONS`, and `POST` for `rr_upload` only, with no Upgrade or WebSocket path, and G-code replies are fetched by polling `rr_reply`. There is no `rr_subscribe` `[src]`.
- DSF provides real push on `WS /machine?sessionKey=<key>`, which streams the full model and then JSON patches `[src]` plus `[doc]`.
- So the correct adapter shape is detect-then-branch: poll for standalone, WebSocket for SBC.

### MQTT

RRF 3.6 contains a built-in MQTT **client**, not a broker, in [src/Networking/MQTT/MqttClient.cpp](https://raw.githubusercontent.com/Duet3D/RepRapFirmware/3.6-dev/src/Networking/MQTT/MqttClient.cpp) `[src]`, with a default broker port of 1883 from `DefaultMqttPort` `[src]`. Configuration is `M586.1` for the enable path with parameters `U"user" K"pass" C"clientid" W"willmsg" T"willtopic" Q<0-2> R<0|1> S"subtopic" O<qos> N<clean>`, associated to an interface with `M586 P4 S1 H<broker-ip> R<port>` `[src]`. DSF exposes no MQTT protocol in `network.interfaces[].activeProtocols[]`, whose enum is only http, https, ftp, sftp, telnet, and ssh, so there is no SBC-side MQTT `[src]`. The exact `M586` token spelling deserves one live check `[src]` plus `[inf]`.

### Reference implementations

- RRF firmware: [src/Networking/HttpResponder.cpp](https://raw.githubusercontent.com/Duet3D/RepRapFirmware/3.6-dev/src/Networking/HttpResponder.cpp), `HttpResponder.h`, `NetworkDefs.h`, `MQTT/MqttClient.cpp`, [src/ObjectModel/ObjectModel.cpp](https://raw.githubusercontent.com/Duet3D/RepRapFirmware/3.6-dev/src/ObjectModel/ObjectModel.cpp), `src/GCodes/GCodes2.cpp`.
- DSF: [RepRapFirmwareController.cs](https://raw.githubusercontent.com/Duet3D/DuetSoftwareFramework/v3.6-dev/src/DuetWebServer/Controllers/RepRapFirmwareController.cs), `MachineController.cs`, `WebSocketController.cs`, `Authorization/SessionKeyAuthenticationHandler.cs`.
- Docs: [DSF REST API](https://duet3d.github.io/DuetSoftwareFramework/articles/rest-api.html), [components](https://duet3d.github.io/DuetSoftwareFramework/articles/components.html), [object model](https://duet3d.github.io/DuetSoftwareFramework/articles/object-model.html), [file management](https://duet3d.github.io/DuetSoftwareFramework/articles/file-management.html).
- Community HA integration: [repier37/hass-Duet3D](https://github.com/repier37/hass-Duet3D), files `__init__.py`, `const.py`, `services.py`, `camera.py`.
- Generated object model reference: [Object-Model-Documentation.md](https://raw.githubusercontent.com/wiki/Duet3D/RepRapFirmware/Object-Model-Documentation.md).

### Duet-specific gaps

- `https://docs.duet3d.com/en/User_manual/Reference/Object_model`, the non-`/en/` variant, and `https://docs.duet3d.com/User_manual/Reference/HTTP_requests` all returned 404. `https://docs.duet3d.com/User_manual/RepRapFirmware/Object_Model` and `.../Reference/Gcodes` returned 200 but are client-rendered SPAs that yield only a title to a text fetch, which is the same problem I hit. All field names above therefore come from RRF and DSF source plus the generated wiki page instead.
- The literal example `GET /rr_model?key=heat.heaters[0].current&flags=d99fn` was not found in any fetched source. The parameter names and flag letters are confirmed independently, so the string is constructible but not quoted.
- The deployed DSF HTTP port.
- `DoHome`'s remaining G28 parameter letters beyond the axis list.
- `G29` and `G32` bed mesh details, `M290` interaction with the object model, and the exact `M586` token for MQTT.
- Whether `M32` strips surrounding quotes from its filename argument.
- The fan `S` scaling inside `GCodeBuffer::GetPwmValue`, meaning whether the convention is `S255` or `S1`.
- `G1 H2` and `G1 H4`.
- The full fan `P` index list, and whether `fans[].actualValue` is populated on all Duet 3 boards.

## 6. RepRapFirmware generic and Marlin over a serial bridge

Short answer: **Marlin exposes no HTTP server of its own.**

- Marlin's native interfaces are serial, USB, and SD card. I found no Marlin-hosted HTTP API in this session.
- The common way to put HTTP in front of Marlin is an ESP32 running ESP3DLib, which provides the web and network layer: [luc-github/ESP3DLib](https://github.com/luc-github/ESP3DLib) `[doc]`. That HTTP surface is a third-party bridge, not Marlin, and its endpoints are not part of any Marlin contract.
- RepRapFirmware generic is the same firmware as section 5, so the object model applies unchanged. Note that RRF does carry an MQTT client, covered in section 5 `[src]`.
- Practical consequence for the adapter interface: a "Marlin" printer in the fleet is reachable only through whatever front-end is attached, OctoPrint, an ESP3D bridge, or Klipper, and the integration should key off that front-end rather than off "Marlin" `[inf]`.

## 7. Anycubic Kobra and Photon

> The Kobra generation is now implemented. `docs/protocol-anycubic-kobra.md` supersedes
> this section for it, with later sources, including the Kobra X's own commands.

Two premises in the brief are wrong, and both would send an adapter down a dead end.

1. There is no local HTTP REST API and no UDP discovery on the Kobra 2, 3, S1, or X generation. The stock LAN surface is an MQTT broker reached through a signed HTTP handshake. The `:80` in the brief is not the API. Port 71 is an OctoPrint-compatibility API that only exists once Rinkhals is installed, and 18088 is the camera.
2. Anycubic Photon resin printers do have a local network API. They are not USB or cloud only.

### Kobra 2 / 3 / S1 / X generation

Reference integration: [chrisfore/anycubic_ha_local](https://github.com/chrisfore/anycubic_ha_local), in the HACS default store. Its validated protocol capture is [research/PROTOCOL-VALIDATED.md](https://raw.githubusercontent.com/chrisfore/anycubic_ha_local/main/research/PROTOCOL-VALIDATED.md), taken live from a Kobra S1 Max on firmware 2.6.9.6. The Rinkhals firmware docs document the same MQTT topology independently at [internal MQTT server documentation](https://rinkhals-community.github.io/Rinkhals/firmware/mqtt/).

| Surface | Value | Source |
| --- | --- | --- |
| Handshake | `GET http://<ip>:18910/info` | [handshake.py](https://raw.githubusercontent.com/chrisfore/anycubic_ha_local/main/custom_components/anycubic/anycubic_local/handshake.py) |
| Auth request | `POST {ctrlInfoUrl}?ts=&nonce=&sign=&did=` | same |
| MQTT over TLS | port 9883 | PROTOCOL-VALIDATED.md |
| MQTT alternate on a rooted printer | plain, port 2883 | [Rinkhals MQTT doc](https://rinkhals-community.github.io/Rinkhals/firmware/mqtt/) |
| Camera | `http://<ip>:18088/flv`, HTTP-FLV with H.264 | PROTOCOL-VALIDATED.md |
| OctoPrint-compat API | port 71 on a rooted printer, moved from 80 | [Rinkhals network activity](https://raw.githubusercontent.com/rinkhals-community/Rinkhals/master/docs/docs/about/network-activity.md) |
| Moonraker | 7125, only with the Rinkhals app installed | same |
| UDP discovery | none found in any fetched source | **UNCONFIRMED** |

**Authentication.** Nothing user-supplied. The printer returns a `token` from `/info`, and the client signs with `sign = md5(md5(token[:16]) + str(ts) + nonce)`. The response is AES-CBC encrypted with `key=token[16:32]` and `IV=local_token`, and the decrypted payload carries the MQTT `broker`, `username`, `password`, and `deviceId`. Read from [handshake.py](https://raw.githubusercontent.com/chrisfore/anycubic_ha_local/main/custom_components/anycubic/anycubic_local/handshake.py) `[src]`. The `ctrlType` field in `/info` tells you `lan` or `cloud`, and the client raises `CloudModeError` on `cloud` `[src]`.

**Works without internet.** Yes for this generation. The README states LAN mode needs no Anycubic cloud account and no rooting, and Rinkhals confirms stock firmware only dials Anycubic's public MQTT when LAN mode is off `[src]`.

**MQTT topics.** From [anycubic_local/const.py](https://raw.githubusercontent.com/chrisfore/anycubic_ha_local/main/custom_components/anycubic/anycubic_local/const.py) `[src]`:

- Publish: `anycubic/anycubicCloud/v1/web/printer/{modelId}/{deviceId}/{type}`
- Subscribe: `anycubic/anycubicCloud/v1/printer/public/{modelId}/{deviceId}/{type}/report`

Report types are `info`, `tempature`, `fan`, `light`, `multiColorBox`, `print`, `status`, `file`, `peripherie`, `video`, `extfilbox`. Note that `tempature` is the firmware's own misspelling and must not be normalised `[src]`.

**State read.** Field names from [models.py](https://raw.githubusercontent.com/chrisfore/anycubic_ha_local/main/custom_components/anycubic/anycubic_local/models.py) `[src]`:

- `temp.curr_nozzle_temp`, `temp.target_nozzle_temp`
- `temp.curr_hotbed_temp`, `temp.target_hotbed_temp`
- `temp.curr_chamber_temp`
- `proj.progress`, `proj.curr_layer`, `proj.total_layers`
- `proj.remain_time` in **minutes**, not seconds
- `proj.filename`
- `data.state` is only `free` or `busy`. The lifecycle lives in `data.project.state` and runs `preheating`, `auto_leveling`, `vibrating`, `flow_calibrating`, `printing`, `pausing`, `paused`, `resuming`, `resumed`, `stopping`, `stoped` (wire spelling, single p), ending `finished` `[src]`.
- The authoritative pause flag is `data.project.pause`, an int where `0=running`, `1=paused`, `2=pausing`, `3=resuming`, `4=stopping` `[src]`.
- Speed is `print_speed_mode` with `1/2/3` for silent, standard, sport. Fans are `fan_speed_pct`, `aux_fan_speed_pct`, `box_fan_level` `[src]`.
- No XY position field and no flow percentage field on the FDM side `[src]`.

Two operational gotchas from the source. Temperature, fan, and speed writes are print-job settings, so when idle the printer ignores them and there is no preheat command in the LAN protocol `[src]`. And `info` can go minutes between pushes while `tempature` pushes within about a second, so a client reading temps only from `info` looks frozen `[src]`.

**Commands**, from [commands.py](https://raw.githubusercontent.com/chrisfore/anycubic_ha_local/main/custom_components/anycubic/anycubic_local/commands.py) `[src]`:

| Action | type | action | data |
| --- | --- | --- | --- |
| pause / resume / stop | `print` | `pause` / `resume` / `stop` | `{"taskid": "-1"}` |
| set temps, fans, speed | `print` | `update` | `{taskid, settings:{target_nozzle_temp, target_hotbed_temp, fan_speed_pct, aux_fan_speed_pct, box_fan_level, print_speed_mode}}` |
| light | `light` | `control` | `{type:2, status, brightness}` |
| ACE auto-feed | `multiColorBox` | `setAutoFeed` | `{multi_color_box:[{id, auto_feed}]}` |
| ACE drying | `multiColorBox` | `setDry` | `{multi_color_box:[{id, drying_status:{status, target_temp, duration}}]}` |
| camera start / stop | `video` | `startCapture` / `stopCapture` | `null` |
| start print | `print` | `start` | `{taskid:"-1", filename, filetype:1}` minimal |

The start-print minimal payload is documented by Rinkhals as `{taskid:"-1", filename:"test_model/FlexibleShark-41m.gcode", filetype:1}`, with an optional full form adding `md5`, `filesize`, `ams_settings`, and `task_settings` `[src]`, [Rinkhals MQTT doc](https://rinkhals-community.github.io/Rinkhals/firmware/mqtt/).

There is no home, jog, extrude, flow-set, or emergency-stop command in the stock LAN protocol `[src]`. Those exist only on the Klipper layer underneath, reached through Rinkhals.

**File list and upload.** The upload URL arrives in the `info` report as `urls.fileUploadurl`, for example `http://<ip>:18910/gcode_upload?s=<32-char token>` `[src]`. File listing is only reachable through MQTT `file` reports, and the request shape for `fileDetails` is explicitly marked unverified in the reference source `[src]`.

**Camera.** On-demand HTTP-FLV carrying H.264, at `http://<ip>:18088/flv`, started by an MQTT `startCapture` command `[src]`. Newer models return a per-session tokenised path instead, and the integration follows whatever URL the printer reports. `/webcam/?action=snapshot` returns 404 on stock firmware, so there is no still endpoint, and there is no RTSP `[src]`.

**Push.** MQTT subscribe. ACE box state is not pushed autonomously and must be polled with `action:"getInfo"` on the `multiColorBox` topic `[src]`.

**Model IDs** from [anycubic/const.py](https://raw.githubusercontent.com/chrisfore/anycubic_ha_local/main/custom_components/anycubic/const.py) `[src]`: 20021 Kobra 2 Pro, 20022 Kobra 2 Plus, 20023 Kobra 2 Max, 20024 Kobra 3, 20025 Kobra S1, 20026 Kobra 3 Max, 20027 Kobra 3 V2, 20028 Kobra 4, 20029 Kobra S1 Max, 20030 Kobra X. The Kobra 2 generation is marked experimental because it uses an older unsigned handshake the integration does not speak `[src]`.

### Anycubic Photon and Photon Mono (resin)

Two protocol families were confirmed from source.

**Old Photon and Chitu boards, UDP port 3000.** [shaddack's PhotonControl writeup](https://shaddack.mauriceward.com/projects/sw_PhotonControl/) documents the wire protocol from real captures `[doc]`.

- Transport is UDP to printer port 3000. Autodetect is a broadcast of `M99999`, and the reply is `ok MAC:.. IP:.. VER:.. ID:.. NAME:..`.
- No auth. The writeup states there is no password or other security `[doc]`.
- Status is G-code text: `M27` returns `SD printing byte <pos>/<total>` or `Error:It's not printing now!`, `M114` returns head position, `M115` returns the firmware date, and `M4000` returns job status with `D:` for printfile offset over total size, `T:` for total job seconds, and `F:` for fan `[doc]`.
- Control is `M24` start or resume, `M25` pause, `M29` stop, `M33 I5` abort, `M112` emergency stop, `M6030 '<file>'` start a named file, `M6032 '<file>'` select, `G28 Z0` home, and `G0`/`G1 Z<mm> F<mm/min>` to move `[doc]`.
- Files are `M20` to list, `M28 <file>` to begin an upload followed by binary packets with a six-byte tailer, `M29` to close, `M30 <file>` to delete `[doc]`.

**Mono X, Mono SE, and Photon X, TCP port 6000, ASCII "ACT" protocol.** The README of [SG-O/photoNetLib](https://raw.githubusercontent.com/SG-O/photoNetLib/master/README.md) states it supports "the Chitu-Protocol and the Anycubic - Protocol (Mono SE, Mono X, Photon X)" `[doc]`. The socket is real code: `ActNetIO io = new ActNetIO(ip, 6000, timeout)` in [ActPrinter.java](https://raw.githubusercontent.com/SG-O/photoNetLib/master/src/main/java/de/sg_o/lib/photoNet/printer/act/ActPrinter.java) `[src]`.

- Commands are comma-terminated ASCII: `getname`, `getfile`, `getstatus`, `getpara`, `goprint,<file>,`, `gopause,`, `goresume,`, `gostop,`, `setZhome,`, `setZmove,<mm>,`, `setZero,`, `sysinfo,`, `getFirmware,` `[src]`.
- Discovery broadcasts the literal string `www.usr.cn` to UDP port 48899 and parses `ip,name` replies, in [ActCommands.java](https://raw.githubusercontent.com/SG-O/photoNetLib/master/src/main/java/de/sg_o/lib/photoNet/networkIO/act/ActCommands.java) and [ActDiscover.java](https://raw.githubusercontent.com/SG-O/photoNetLib/master/src/main/java/de/sg_o/lib/photoNet/printer/act/ActDiscover.java) `[src]`. That string is a USR-IOT serial-to-WiFi module default, which means these printers expose the network through an off-the-shelf transparent bridge `[inf]`.
- No authentication was found for either resin protocol `[src]`.

Whether this applies to the current Mono 2, Mono M5s, and Mono M7 generations is **UNCONFIRMED**. The author of the Anycubic integration states Photon is a different platform with no local LAN API, which contradicts the fetched code for the models photoNetLib names. Treat modern resin as **UNCONFIRMED** and the old Photon, Mono SE, Mono X, and Photon X as confirmed network-controllable.

## 8. Creality (K1/K2 Creality OS, Ender 3 V3, Sonic Pad)

### Correction

Creality K1, K1C, K1 Max, K1 SE, the Ender 3 V3 family, and stock Sonic Pad are **not** driven through Moonraker on the network. The local API is a Creality JSON WebSocket on port 9999 with no authentication.

The Klipper foundation is real and provable, because Creality publishes the firmware source. [CrealityOfficial/K1_Series_Klipper](https://github.com/CrealityOfficial/K1_Series_Klipper) is described as a clone of upstream Klipper, and its [printer.cfg](https://raw.githubusercontent.com/CrealityOfficial/K1_Series_Klipper/main/config/K1_CR4CU220812S12/printer.cfg) is a normal Klipper config with `[mcu] serial: /dev/ttyS7`, `[stepper_x]`, `[extruder]`, `[heater_bed]`, `[virtual_sdcard] path: /usr/data/printer_data/gcodes`, and `[printer] kinematics: corexy` `[src]`. Klipper underneath is not Moonraker on the wire.

The reference integration [3dg1luk43/ha_creality_ws](https://github.com/3dg1luk43/ha_creality_ws) talks WebSocket 9999 and only falls back to Moonraker for one missing field on the K2 Base: `url = f"http://{host}:{MR_PORT}/printer/objects/query?{MR_QUERY_PARAMS}"` with `MR_PORT = 7125` and `MR_QUERY_PARAMS = "objects=temperature_fan%20chamber_fan"`, gated on `self._is_k2_base`, in [coordinator.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/coordinator.py) and [const.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/const.py) `[src]`. For Creality, Moonraker on a K1 is a rooted mod, [thejoaoguedes/creality_k1_klipper_mod](https://github.com/thejoaoguedes/creality_k1_klipper_mod), and that mod's existence is itself evidence that stock K1 has no Moonraker.

### Surfaces

| Surface | Value | Source |
| --- | --- | --- |
| WebSocket | `ws://<host>:9999`, subprotocol `wsslicer`, no auth | [const.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/const.py) |
| MJPEG stream | `http://<host>:8080/?action=stream` | same |
| Printer HTTP UI | port 80, HTTPS also probed | const.py, [image.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/image.py) |
| WebRTC signalling | `http://<host>:8000/call/webrtc_local`, POST base64 JSON | const.py, [camera.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/camera.py) |
| Preview PNG | `http(s)://<host>/downloads/original/current_print_image.png` | image.py |
| Moonraker | 7125, K2 Base only | coordinator.py |
| Discovery | Zeroconf / mDNS | [\_\_init\_\_.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/__init__.py) |
| RTSP | none found | **UNCONFIRMED** |

**Authentication.** None. No token, no API key, no access code. The only parity requirement is mirroring the subprotocol the printer's own web UI advertises, `Sec-WebSocket-Protocol: wsslicer` `[src]`.

**Works without internet.** Yes. The README states a direct WebSocket connection, local, no cloud, with push updates and no polling `[src]`. No fetched source says a cloud account is required for LAN printing on these models. The claim that newer Creality printers need Creality Cloud for LAN print is **UNCONFIRMED** in both directions.

**Wire protocol.** Requests are JSON with `method` and `params`, for example `{"method": "get", "params": {"ReqPrinterPara": 1}}`, `{"method": "get", "params": {"reqPrintObjects": 1}}`, and `{"method": "get", "params": {"boxsInfo": 1}}` in [ws_client.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/ws_client.py) `[src]`. The printer pushes `{"ModeCode": "heart_beat"}` and expects the literal string `ok` back. It does not implement WebSocket pings correctly, so the client runs an application-level staleness watchdog with `HEARTBEAT_SECS = 10.0` and `STALE_AFTER_SECS = 15` `[src]`. Cadences are `ReqPrinterPara` every 5 seconds, `reqPrintObjects` every 2 seconds, and `boxsInfo` every 300 seconds `[src]`.

**State read.** Field names from [sensor.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/sensor.py), [coordinator.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/coordinator.py), and [utils.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/utils.py) `[src]`:

| Wanted | Field |
| --- | --- |
| Nozzle temp and target | `nozzleTemp`, `targetNozzleTemp`, `maxNozzleTemp` |
| Bed temp and target | `bedTemp0`, `targetBedTemp0`, `maxBedTemp` |
| Chamber temp and target | `boxTemp`, `targetBoxTemp`, `maxBoxTemp` |
| Current layer and total layers | `layer`, `TotalLayer` (exact capitalisation) |
| Progress | `printProgress` or `dProgress` |
| Remaining time | `printLeftTime`, seconds |
| Elapsed time | `printJobTime`, seconds |
| Filename | `printFileName` |
| Speed and flow | `curFeedratePct`, `curFlowratePct`, plus `realTimeFlow` in mm3/s |
| Filament used | `usedMaterialLength`, mm |
| Position | `curPosition`, a string parsed by regex `X:(...) Y:(...) Z:(...)` |
| Model and firmware | `model`, `modelVersion`, `hostname` |
| Error and self test | `err.errcode`, `withSelfTest` |
| CFS | `cfsConnect`, `boxsInfo.materialBoxs[]` |

Position arrives as one blob and is parsed with `_POS_RE = re.compile(r"X:(?P<X>-?\d+(?:\.\d+)?)\s+Y:(?P<Y>-?\d+(?:\.\d+)?)\s+Z:(?P<Z>-?\d+(?:\.\d+)?)")` in [utils.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/utils.py) `[src]`.

**Status enum.** There is no single authoritative enum on the wire. The integration derives status from four fields and owns the set `off`, `unknown`, `error`, `self-testing`, `completed`, `paused`, `stopped`, `printing`, `processing`, `idle`, in [sensor.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/sensor.py) `[src]`. Raw signals include `state` (int), `err.errcode`, `withSelfTest`, and `printProgress`. `deviceState == 7` means busy homing `[src]`.

**Commands**, from [button.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/button.py), [number.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/number.py), [light.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/light.py), and [switch.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/switch.py) `[src]`:

| Action | Frame |
| --- | --- |
| pause | `send_set(pause=1)` |
| resume | `send_set(pause=0)` |
| stop or cancel | `send_set(stop=1)` |
| home | `send_set(autohome="X Y")` then `send_set(autohome="Z")` |
| set nozzle | `send_set(nozzleTempControl=v)` |
| set bed | `send_set(bedTempControl={"num": 0, "val": v})` |
| set chamber | `send_set(boxTempControl=v)` |
| set speed and flow | `send_set(setFeedratePct=v)`, `send_set(setFlowratePct=v)` |
| fan | `send_set(gcodeCmd="M106 P<ch> S<0-255>")` |
| light | `send_set(lightSw=1|0)` |

There is no fan field, so fan control goes through raw G-code in `gcodeCmd`. There is no start-print frame anywhere in the fetched integration, so the start path is **UNCONFIRMED**. Emergency stop is not in this integration either; Moonraker's `printer.emergency_stop` exists only on the Moonraker route.

**Camera.** The K1 family serves MJPEG on port 8080 at `/?action=stream` with no snapshot endpoint, so the integration extracts one JPEG frame by hunting for `\xff\xd8` then `\xff\xd9` in [camera.py](https://raw.githubusercontent.com/3dg1luk43/ha_creality_ws/main/custom_components/ha_creality_ws/camera.py) `[src]`. The K2 family and newer K1C firmware use WebRTC, with signalling as a single POST of a base64-wrapped JSON SDP offer to port 8000 at `/call/webrtc_local` `[src]`. No RTSP was found `[UNCONFIRMED]`. The README notes the K1C 2025 camera is not yet supported due to a go2rtc dependency issue `[src]`.

**Sonic Pad.** Root is a documented user-facing flow rather than a password: "Creality provides an easy way to get the root credentials now from Sonic Pad > Configure > Other Settings > Advanced options > Root Account" in [leafeven/sonicpad README.md](https://raw.githubusercontent.com/leafeven/sonicpad/main/README.md) `[doc]`. That README also assumes "moonraker will be able to do everything this can", which is inference rather than a port listing. The Sonic Pad port table is **UNCONFIRMED**.

**The Ender 3 V3 "Creality Cloud local HTTP API" from the brief.** I found no such API. The Ender 3 V3 family uses the same port-9999 WebSocket as the K1 `[src]`. Treat that premise as **UNCONFIRMED** and probably wrong.

## GAPS

Unconfirmed items, grouped by protocol. Items now closed by later source reads have been removed.

**Klipper / Moonraker**
- Whether the HACS integration's `camera.py` handles only MJPEG or also WebRTC. I read `const.py` and `api.py` but not `camera.py`.
- The exact slicer coverage of `estimated_time`, since Moonraker notes some metadata fields are slicer-specific.

**OctoPrint**
- Default port for a non-OctoPi install. The library default is 80 but OctoPi serves 5000; I did not fetch an authoritative statement.
- How to read back the current feedrate and flowrate factors through the REST API.
- Layer count and total layers. Not in the REST responses I read.
- Whether `M112` sent through `POST /api/printer/command` is a true emergency stop on every supported firmware.

**PrusaLink / Prusa Connect**
- The entire Prusa Connect local API. Not researched.
- The PrusaLink file upload request. `upload_by_put` capability exists; the call is not in `pyprusalink`.
- Whether a start-print endpoint exists in the v1 spec. The spec URL is known but I did not fetch it.
- Chamber temperature and layer counts. Absent from the typed models.
- The `active_camera` stream mechanism.
- The default port from a primary source.

**Bambu Lab**
- Toolhead X, Y, Z position. No coordinate key exists in the full `pushall` sample and pybambu parses none.
- Emergency stop. No `estop` or `M112` MQTT command in any fetched source.
- Setting flow or pressure advance. Only reported, via `mc_print.push_info`.
- Printer-hosted HTTP or REST. None found.
- Plain MQTT 1883. Likely absent; only 8883 with TLS appears in any source.
- The `PrintFun` body arithmetic that turns the `fun` hex into `mqtt_signature_required`. It sits past the truncation point of the 174 KB `models.py`, and the GitHub contents API hit rate limits. A local clone would settle it.
- TCP 6000 framing. Three mutually incompatible descriptions, listed in section 4.
- Whether `LEGACY_SDCARD_PRINTERS` is consumed anywhere. The live coordinator contradicts it.
- Which `bed_levelling` versus `bed_leveling` spelling the firmware accepts on which model.
- Whether the printer enforces `sequence_id` uniqueness. mqtt.md says it increments per command, HA hardcodes `"0"`.
- Payload-only keys with no parser anywhere: `mc_print_error_code`, per-tray `cali_idx`, `device.cham_temp`, `ctt`, `percent`, `mc_err`, `stg[]`.
- Developer Mode for X1E, A2L, X2D, H2C, H2S, and H2D Pro.
- The official Bambu wiki body text is unreachable to a text fetch. `https://wiki.bambulab.com/en/knowledge-sharing/enable-lan-mode` and `.../enable-developer-mode` returned HTTP 200 with only a page title, and `.../en/x1/manual/X1-connection-modes` returned 404. The official blog was quoted instead.
- The HA core `bambu_lab` path does not exist. Three separate fetches returned 404, so the HACS repo is the citation.

**Duet / RepRapFirmware**
- The deployed DSF HTTP port. The repo default is 5000 and the runtime value lives in `/opt/dsf/conf/http.json`, which is not in the repo.
- `DoHome`'s remaining G28 parameter letters beyond the axis list.
- `G29` and `G32` bed mesh details, `M290` interaction with the object model, and the exact `M586` token for MQTT.
- Whether `M32` strips surrounding quotes from its filename argument.
- The fan `S` scaling inside `GCodeBuffer::GetPwmValue`, meaning whether the convention is `S255` or `S1`.
- `G1 H2` and `G1 H4`.
- The full fan `P` index list, and whether `fans[].actualValue` is populated on all Duet 3 boards.
- The `@duet3d/connectors` package, which now owns DuetWebControl's request strings, was not located because of GitHub API rate limits.
- Per-field object-model availability. Some fields are DSF or DWC maintained and others are standalone only, so treat availability as build-dependent. No field was verified against a live board.
- Any Duet-native camera source.

**Anycubic**
- UDP discovery or broadcast on the Kobra LAN protocol. Not in any fetched source. Rinkhals mentions an SSDP or IGMP discovery helper, but that is a Rinkhals app, not stock firmware.
- Any local HTTP data API beyond `/info` and `/gcode_upload?s=`.
- Home, jog, extrude, flow-set, or emergency stop in the stock LAN protocol. Those need the Klipper layer that Rinkhals exposes.
- The file list request shape. The reference implementation marks the `fileDetails` request keys as inferred from the response echo.
- The Kobra 2 unsigned handshake. Known to differ and implemented nowhere fetched.
- Whether the Rinkhals MQTT credentials at port 2883 are per-device or fleet-wide.
- Modern resin models: Mono 2, Mono M5s, Mono M7, Photon Mono 4. Nothing fetched covers them.
- The CBD protocol port in photoNetLib's older Chitu path.

**Creality**
- Start-print and file-upload on Creality OS. The port-80 upload path is unverified.
- Emergency stop on Creality OS.
- The Sonic Pad port table.
- Whether a Creality Cloud account is required for LAN print on K2, K2 Plus, and CFS models. No source states it either way.
- RTSP anywhere in the Creality line.
- Whether K2 non-Base models ship Moonraker broadly, or only the chamber fan object is reachable.

**Marlin**
- No HTTP surface exists in Marlin itself. Any HTTP is a third-party bridge such as ESP3DLib.

## Adapter interface implications

These follow from the tables, not from additional research.

1. There is no such thing as "one local API" across this fleet. The surfaces share almost nothing, not even the notion of a print job id. The pragmatic shape is a thin transport per vendor feeding one shared printer-state model.
2. Moonraker and OctoPrint are the only two protocols that combine REST, push, upload, and full print control in one place. They should be the first two adapters.
3. Push is available in three incompatible forms. Moonraker uses JSON-RPC over WebSocket with named object subscriptions. OctoPrint uses SockJS and needs a session auth message that an API key alone does not satisfy. Duet uses a WebSocket only on SBC, and polls on standalone. Bambu, Anycubic, and Creality push over MQTT, MQTT, and a raw JSON WebSocket respectively.
4. Bambu is the only protocol with a firmware-level write gate. Read `print.fun` bit `0x20000000` before offering controls, and surface "developer mode required" as a first-class state rather than as an error.
5. Three of eight families need a bespoke binary or near-binary client. Anycubic needs a signed AES handshake then MQTT. Creality needs a WebSocket with an application-level heartbeat. Bambu TCP 6000, if you use it at all, is undocumented enough that you should start with FTPS 990.
6. Layer counts exist on Moonraker, Anycubic, Bambu, Creality, and Duet, but not on OctoPrint or PrusaLink. Do not put `current_layer` on the shared interface as a required field.
7. Progress is reported natively by Moonraker, OctoPrint, PrusaLink, Anycubic, Bambu, and Creality. Only Duet has no progress field, and there you compute it from `job.filePosition / job.file.size`.
8. Remaining time is in seconds in most protocols. Two exceptions are in minutes, Bambu's `mc_remaining_time` and Anycubic's `remain_time`. Normalise at the boundary.
9. Authentication splits into four shapes. Header key for Moonraker and OctoPrint, HTTP Digest for PrusaLink, MQTT credentials for Bambu and Anycubic, and a session key for Duet. Creality and the old Anycubic resin protocols have no auth at all.
10. Four of the brief's premises were wrong and all four would have cost time. Bambu camera ports were inverted. HA core has no `bambu_lab`. Moonraker is not in HA core. Creality is not Moonraker, and Anycubic has no local REST API. Probe a printer's actual surface before trusting any vendor or model table.

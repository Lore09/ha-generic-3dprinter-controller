# Anycubic Kobra, protocol surface

The Kobra 3, Kobra 4, Kobra S1 and Kobra X speak one LAN protocol: a signed HTTP
handshake that hands out credentials, then JSON over MQTT with TLS. The adapter is
`adapters/anycubic_kobra.py`; the hardware check is `tools/acceptance_kobra.py`.

**Nothing here has been measured by this project yet.** Everything is taken from a
source, named each time. When a printer has been checked, this document records
what it answered, the way `protocol-elegoo-cc2.md` does for the Centauri Carbon 2.

Sources, in order of authority:

1. [chrisfore/anycubic_ha_local](https://github.com/chrisfore/anycubic_ha_local),
   `research/PROTOCOL-VALIDATED.md`: captured live from a Kobra S1 Max on firmware
   2.6.9.6, with the Kobra X, Kobra 4 and Kobra 3 confirmed from users' diagnostics.
2. [stribor/anycubic_kobrax](https://github.com/stribor/anycubic_kobrax): written by
   a Kobra X owner, and the only source for the Kobra X's own commands.
3. [rvanderp3/kobra-connect](https://github.com/rvanderp3/kobra-connect),
   `docs/mqtt-commands.md`: the file list, on a Kobra 3.
4. [Rinkhals](https://rinkhals-community.github.io/Rinkhals/firmware/mqtt/): the
   start-print request, captured on a Kobra 3 through the rooted broker.

## Before anything: LAN Mode

The printer answers local clients only with **LAN Mode** on (Settings, Network, LAN
Mode). `/info` reports `ctrlType: "cloud"` otherwise, and the config flow refuses
the printer with that instruction.

## Handshake

| Step | Request | Source |
| --- | --- | --- |
| 1 | `GET http://<host>:18910/info` → `token`, `ctrlInfoUrl`, `modelId`, `modelName`, `cn` (serial), `ctrlType` | 1 |
| 2 | `POST <ctrlInfoUrl>?ts=<ms>&nonce=<6 alnum>&sign=<md5(md5(token[:16]) + ts + nonce)>&did=<32 upper alnum>` | 1, 2 |
| 3 | AES-CBC decrypt `data.info`: key `token[16:32]`, IV `data.token` padded to 16 bytes, PKCS7 | 1, 2 |
| 4 | Bundle: `broker` (`mqtts://ip:9883`), `username`, `password`, `deviceId`, and `devicecrt` and `devicepk` on some firmware | 1, 2 |
| 5 | MQTT over TLS to 9883. The certificate is self-signed and not checked; the client certificate is presented when supplied | 1, 2 |

The credentials last one session. The adapter runs the handshake on every setup and
stores only the address and the serial. The Kobra 2 (models 20021 to 20023) uses an
older, unsigned handshake that no source documents, and is refused.

## Topics

* Reports: `anycubic/anycubicCloud/v1/printer/public/<modelId>/<deviceId>/<type>/report`
* Commands: `anycubic/anycubicCloud/v1/web/printer/<modelId>/<deviceId>/<type>`
* Starting a print and listing files use `.../slicer/printer/...` instead (3, 4).

A message is `{type, action, timestamp (ms), msgid, data}`. Reports are folded by
their type, never by `action`, which varies (1).

## Reports

| Type | Asked with | Carries |
| --- | --- | --- |
| `info` | `query` | the whole state: `state` (`free`/`busy`), `temp`, fans, `print_speed_mode`, `project`, `urls` |
| `tempature` (sic) | `query` | temperatures only; pushed within a second of a change |
| `fan` | `query` | `fan_speed_pct`, `aux_fan_speed_pct`, `box_fan_level` |
| `print` | pushed | progress while printing; command answers share the topic |
| `light` | `query` | `lights: [{type, status, brightness}]` |
| `multiColorBox` | `getInfo` | the multi-colour units; not pushed while printing |
| `peripherie` | `query` | whether a camera and a unit are attached |
| `video` | answer to `startCapture` | the stream URL |
| `file` | answer to `listLocal` | `file_list` |

`project.pause` is the authoritative pause flag: 0 running, 1 paused, 2 pausing,
3 resuming, 4 stopping. `remain_time` is in minutes. `stoped` is the firmware's
spelling (1).

## Commands

| Command | Kobra X | Other models |
| --- | --- | --- |
| pause, resume, stop | `print` / `pause`, `resume`, `stop`, `{taskid: "-1"}` | same |
| nozzle, bed | `tempature` / `set`, `{type: 0 nozzle or 1 bed, both targets}`, also while idle (2) | `print` / `update`, `settings`, only during a print (1) |
| fan | `fan` / `setSpeed`, part fan only (2) | `print` / `update`, only during a print (1) |
| speed | `print` / `update`, `print_speed_mode` 1, 2, 3 | same |
| light | `light` / `control`, `type: 3` (2) | `type: 2` on the enclosed S1 models (1) |
| home | `axis` / `move`, `{axis: 5 XYZ, 4 XY, 3 Z, move_type: 2}` (2) | none |
| jog | `axis` / `move`, `{axis: 1 X, 2 Y, 3 Z, move_type: 1 plus or 0 minus, distance}` (2) | none |
| auto-feed | `multiColorBox` / `setAutoFeed` (1) | same |
| start print | `slicer/` `print` / `start`, `{taskid: "-1", filename, filetype: 1}` (4); an opt-in | same |
| file list | `slicer/` `file` / `listLocal`, `{path: "/"}` (3) | same |

A refusal comes back on the type's report topic with a `code` other than 200. An
idle printer drops a job setting without any answer (1), which is why those are
state rules and not sent while idle.

## The multi-colour unit

The Kobra X's built-in four-slot changer reports as box `-1`; an external ACE as box
0 and up (1). Box `-1` is a feeder with no dryer. Slots carry `type` (empty when no
spool is in it), `color` as RGB and `status` (5 loaded).

## Camera

Send `video` / `stopCapture`, wait a second, send `startCapture`; the answer carries
the stream URL, per session on the newer firmware (`/live/<token>` on port 18088)
and `/flv` on the S1 (1). The stream is HTTP-FLV with H.264, which Home Assistant's
stream component plays. Starting the capture switches the light on, so the camera
entity does not start it until someone watches.

## Not supported, and why

* **Upload.** The `info` report names an upload URL,
  `http://<host>:18910/gcode_upload?s=<token>`, but no source records the request
  body it takes. See the capture recipe below.
* **Deleting a file.** `file` / `deleteLocal` is documented (3) and held back until
  the file list is confirmed on a printer.
* **Loading and unloading filament.** The LAN protocol has no command for it.
* **Drying, humidity and remaining filament.** The shared model has no field for
  them yet.

## Capturing an upload

With Anycubic's slicer on one computer and the printer on the same network, record
one upload from that computer:

```bash
sudo tcpdump -i any -s 0 -w kobra-upload.pcap host <printer-ip> and port 18910
```

Send a small file to the printer from the slicer, stop the capture, and open it in
Wireshark with **Follow → HTTP Stream**. The request line, the headers and the
start of the body are what the adapter needs. The capture contains the upload
token in the URL, which is issued per session.

## Still to verify

Everything. `tools/acceptance_kobra.py <host>` reads a printer without changing it;
`--camera out.flv` checks the stream; `--active` sends the light, a nozzle target,
the part fan and, on a Kobra X, a home and a jog, each confirmed first.

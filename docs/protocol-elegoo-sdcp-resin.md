# Elegoo resin printers over SDCP V3, protocol surface

Elegoo's current resin printers, such as the Saturn 4 Ultra 16K, speak SDCP V3: the
same JSON over a WebSocket on port 3030 as the Centauri Carbon, with a different
command set and different fields. The adapter is `adapters/sdcp_resin.py`, which
shares the socket with the Centauri's adapter through `SdcpSession` and nothing else.
The read-only hardware check is `tools/acceptance_sdcp_resin.py`.

This document separates what this repository **measured** on a live printer from
what it **takes from a source**, and names the source every time, as
`protocol-elegoo-cc2.md` does. Sources, in order of authority:

1. The SDCP V3 specification,
   [cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0](https://github.com/cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0),
   `en.md`. Line numbers below are that file's.
2. [alfiedennen/sdcp-saturn-4-ultra](https://github.com/alfiedennen/sdcp-saturn-4-ultra),
   written against a Saturn 4 Ultra 16K on firmware V1.5.6, the same model and
   firmware as the printer measured here. Its `camera-stream-leak.md` is the source
   for the camera's session leak.
3. [ELEGOO-3D/elegoo-link](https://github.com/ELEGOO-3D/elegoo-link), Elegoo's own
   SDK, for the status codes the spec leaves out.
4. [danielcherubini/elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant),
   whose issue #21 holds a status read from another 16K during a print.
5. The huygens and cuprum SDCP clients, for the start-print payload and the camera's
   transport.

## Device under test

| Property | Value |
| --- | --- |
| Model | Elegoo Saturn 4 Ultra 16K |
| LAN address | `192.168.128.143`, where a Centauri Carbon used to be |
| Firmware | `V1.5.6` |
| SDCP protocol version | `V3.0.0` |
| MainboardID | `78070ac4ce6d0100`, 16 hex digits where the Centauri has 32 |
| Resolution | `15120x6230` |
| SupportFileType | `CTB`, `GOO` |
| Camera | `CameraStatus` 1, at most 2 video streams |
| Network | `wlan` |

Measured on 2026-09-30 while idle, read-only: commands 0, 1, 258, 320 and 321, the
ports below, and two sockets held for 200 seconds. The frames are kept in
`tests/fixtures/sdcp_saturn4u16k_v156_idle.json`, which the fake printer replays.

## Ports

| Port | State | Use |
| --- | --- | --- |
| 3030 | open | SDCP WebSocket at `/websocket`; the upload route and the history thumbnails too |
| 554 | open | RTSP, the camera, once command 386 switches it on |
| 80, 443, 3031, 8080, 8554 | closed | no web page, no MJPEG camera |

So the printer has no page to proxy (`WEB_UI`), and no port-80 product appears as a
candidate when the address is probed.

## Discovery and identity

The UDP discovery literal `M99999` to port 3000 is the Centauri's. The 16K answers
with a flat `Data` object (`Name`, `MachineName`, `BrandName`, `MainboardIP`,
`MainboardID`, `ProtocolVersion`, `FirmwareVersion`), measured. The spec gives a
flat `Data` in the reply (en.md:17-29); source 2 describes a nested
`Data.Attributes` and `Data.Status`. The raw reply of this printer was not saved, so
`sdcp_identity` reads both, and a reply without an address falls back to the
datagram's sender.

A resin printer and a Centauri Carbon cannot be told apart by port, protocol version
or build volume: both report `XYZsize` `218.88x128.88x220`, which on the Saturn is a
placeholder (its history entry gives the real 211.68 x 118.37 x 220 mm).
`classify_sdcp` decides, in this order:

1. `SupportFileType`: CTB, CBDDLP, GOO or PRZ means resin; `gcode` means FDM.
2. `ReleaseFilmMax`, `Resolution` or `DevicesStatus.LCDStatus` means resin.
3. The status: `TempOfUVLED`, `ReleaseFilm` or `TempOfTank` means resin;
   `TempOfNozzle` or `TempOfHotbed` means FDM.
4. `MachineName`, ignoring case: Saturn or Mars means resin; Centauri or Neptune
   means FDM.

The Saturn answers rule 1. The config flow reads the `MainboardID` from the reply
and stores it as the entry's serial, so the unique id is `sdcp_resin:<MainboardID>`.
It refuses an FDM reply, and one whose `ProtocolVersion` starts with `V1`: those
printers speak SDCP over MQTT ([vvuk/cassini](https://github.com/vvuk/cassini)). With
no reply, the mainboard id has to be typed in.

Every read checks again. A resin entry that meets an FDM printer, or a resin printer
with another mainboard id, closes the socket and stops with an error; a Centauri
Carbon entry that meets a resin printer does the same after command 1, before any
command it would send an FDM printer.

## Envelope

What the adapter sends, the shape the Saturn answered in the capture:

```json
{
  "Id": "<32 random hex characters>",
  "Data": {
    "Cmd": 0,
    "Data": {},
    "RequestID": "<32 random hex characters>",
    "MainboardID": "78070ac4ce6d0100",
    "TimeStamp": 1790780829,
    "From": 1
  },
  "Topic": "sdcp/request/78070ac4ce6d0100"
}
```

Where it differs from the Centauri's frame: a random `Id` instead of an empty one, a
`Topic`, the time in seconds instead of milliseconds, and the mainboard id on the
very first command 1. Whether the Saturn also takes the Centauri's frame was not
tested, and is not needed.

The replies match the Centauri's topics. The printer's own `Id` is a constant that
does not echo the request's. Command 1 answers `{"Ack": 0}` on `sdcp/response` and
then pushes the attributes on `sdcp/attributes`; command 0 does the same with the
status on `sdcp/status`. Command 258 differs from the Centauri: the `Ack` and the
`FileList` come in one frame.

## Keeping the socket

Measured: the Saturn pushed nothing unasked while idle, and never answered the text
`ping`, which the Centauri's page sends. A socket that sent nothing at all, and one
that sent command 0 every 20 seconds, were both still open after 200 seconds.

So the heartbeat is command 0 every 30 seconds, never `ping`, and a read asks for
the status when the last one is more than 20 seconds old. The attributes are not
pushed either, so command 1 is sent again every 300 seconds and before the camera
opens. Whether a printing Saturn pushes its status unasked is not known.

## Status

The idle status, as read:

```json
{
  "CurrentStatus": [0],
  "ReleaseFilm": 283,
  "TempOfUVLED": 29.08,
  "TimeLapseStatus": 0,
  "HeatStatus": 1,
  "TempOfTank": 29,
  "TempTargetTank": 30,
  "PrintInfo": {
    "Status": 0,
    "CurrentLayer": 283,
    "TotalLayer": 283,
    "CurrentTicks": 2757032,
    "TotalTicks": 2757032,
    "ErrorNumber": 0,
    "Filename": "SUP_allineatore_01_1_202609301434.goo",
    "TaskId": ""
  }
}
```

Things to notice:

* **Ticks are milliseconds**, where the Centauri's are seconds. The spec says so
  (en.md:149-150), and command 321 settles it on this printer: the last job ran from
  `BeginTime` 1790772080 to `EndTime` 1790774837, 2757 seconds, for `TotalTicks`
  2757032. Elapsed is `CurrentTicks / 1000`, remaining `(TotalTicks - CurrentTicks) /
  1000`.
* **There is no `Progress` field**, idle or printing (issue #21 of source 4). Progress
  is `CurrentLayer / TotalLayer`.
* **An idle printer keeps the last job's layers, ticks and file name**, with `Status`
  0 and an empty `TaskId`. The spec says the job's own code stays at 9 (completed);
  V1.5.6 resets it to 0. So progress, times and layers are shown only while a job is
  in hand, and 100 % once it finished; the file name stays.
* `TempTargetTank` is 30 on both 16Ks seen, and no command sets it. The vat is a
  reading and a target the printer keeps, not a heater this integration drives.
* `HeatStatus` is kept as sent. It was 1 here, idle at 29 of 30 °C, and 0 on the
  printing 16K of issue #21 at 20 of 30 °C, which contradicts reading it as "the vat
  is heating".
* `ReleaseFilm` counts the film's lifts against `ReleaseFilmMax` (60000 here). Issue
  #21 shows it growing across jobs; whether it resets is not known.
* `DevicesStatus` in the attributes reports each device check as 1 when it is fine.

A print, from issue #21: `CurrentStatus` [1], `Status` 4, layer 3995 of 5132, ticks
20313461 of 25361465. That reads as 77.8 %, 5 h 38 min elapsed and 1 h 24 min left.

## States

`CurrentStatus`, the machine's own state, decides first (en.md:172-180; 8 from
source 3):

| CurrentStatus | Print state | Phase |
| --- | --- | --- |
| empty | unknown | none |
| [1] | from `PrintInfo.Status`, below | |
| 2 | idle | file_transferring |
| 3 | idle | exposure_test |
| 4 | idle | self_check |
| 8 | idle | file_received |
| [0] | idle, or the kept end of a job (8, 9) | idle |

`PrintInfo.Status` while `CurrentStatus` holds 1 (en.md:181-203; 16 from source 3,
not seen on a printer):

| Status | Print state | Phase |
| --- | --- | --- |
| 0 | preparing | starting |
| 1 | preparing | homing, or preheating while the vat is below its target |
| 2 | printing | descending |
| 3 | printing | exposing |
| 4 | printing | lifting |
| 5, 6 | paused | pausing, paused |
| 7, 8 | cancelled | stopping, stopped |
| 9 | finished | completed |
| 10 | preparing | file_checking |
| 16 | preparing | preheating |
| any other | printing | other |

The print state is the one every printer shares, so automations and the card treat a
Saturn like any printer; the phase is its own sensor.

Measured through a job on a 16K V1.5.6, with the owner at the printer, starting the
file already in `/local`:

* **Start.** `CurrentStatus` [1] and `Status` 1 within 2 seconds, with a `TaskId`,
  and 1 for five minutes while the vat went from 28 to 30 °C (its target), so the
  integration calls 1 preheating while the vat is below its target. 16 never came.
  Then 3 (exposing) and 4 (lifting) in turn, one pair a layer, about 40 seconds a
  bottom layer; `ReleaseFilm` went up by one a layer, and the UV LED from 28 to 39 °C.
* **Pause** (129). 5 (pausing) at once; the printer finished the layer it was on
  and reached 6 (paused) about 60 seconds later.
* **Resume** (131). 2 (descending) at once, then 3 and 4 again.
* **Stop** (130). 7 (stopping) at once, then about 20 seconds later `CurrentStatus`
  [0] with `Status` 8 kept, so the print state stays cancelled, with nothing blocked,
  until the next job.

`PrintInfo.ErrorNumber` 1 to 5 (en.md:206-218: MD5, file read, resolution, format,
model), a device check that is not 1, and a release film at its rated count are
reported as the printer's errors.

## Commands

| Cmd | What | Payload | Evidence |
| --- | --- | --- | --- |
| 0 | status | `{}` | measured |
| 1 | attributes | `{}` | measured |
| 258 | file list | `{"Url": "/local"}` | measured on `/local` and `/usb`; entries are `{name, type}`, type 0 a folder, no size |
| 320 | history ids | `{}` | measured; sent only by the acceptance tool |
| 321 | history detail | `{"Id": [<id>]}` | measured; sent only by the acceptance tool |
| 129, 130, 131 | pause, stop, resume | `{}` | measured through a job |
| 259 | delete | `{"FileList": [<path>], "FolderList": []}` | measured: deleted an uploaded file; source 2 reports Ack 0 even for a missing path |
| 128 | start print, opted in | `{"Filename": <bare name>, "StartLayer": 0}` | measured: started a print of a file in `/local` |
| 386 | video, opted in | `{"Enable": 1}` or `{"Enable": 0}` | measured: Ack 0 and `rtsp://<host>:554/video` both ways |

128 `{"Filename": <bare name>, "StartLayer": 0}`, 129, 131 and 130 were sent to a 16K
V1.5.6 through a job, each answered `Ack` 0 and did what the spec says (see States).
259 `{"FileList": ["/local/<file>"], "FolderList": []}` deleted an uploaded file on a
16K V1.5.6, and the list afterwards confirmed it. Around them:

* **Delete** lists the file's folder afterwards and reports a failure while the file
  is still there, or when no list comes back, since the `Ack` alone says nothing.
* **Start print** takes a file of `/local` by its bare name, of a type the printer
  names in `SupportFileType`. It asks for a fresh status first and starts only when
  `CurrentStatus` is idle: source 2 reports `Ack` 1 (busy) for about 15 seconds after
  an upload, while the print state already reads idle. The Centauri's six-field
  payload never reaches a resin printer.
* **Acks.** Command 128 gives 3 to 7 their own meaning (en.md:410-423), and 386 gives
  1 (every stream in use) and 2 (no camera) (en.md:912).

Never sent, and refused by the fake printer the tests run against: 403 and 324, the
Centauri's setters and CANVAS, which the V3 spec does not have; 387, the printer's own
timelapse switch, a setting that outlives the print (en.md:874); 132 and 133, which
source 2 reports get no answer on V1.5.6; and any start print but the two fields.

## Upload

`POST http://<host>:3030/uploadFile/upload` (en.md:1017-1021, and the URL source 2
used), in the Centauri's chunked form of 1 MiB parts with the file's MD5. A part
counts only when the printer answers with code `000000`. Only files whose suffix
the printer names in `SupportFileType` are taken, `.ctb` and `.goo` on the 16K, and
only while the machine is idle. The integration's upload view caps a file at 256
MiB.

The printer answers `000000` before it has checked the file. On a 16K V1.5.6 a 13.5
MB `.goo` held `CurrentStatus` [2] for about 9 seconds, then [0], and was listed in
`/local`; 3.8 KB of junk bytes named `.goo` got the same reply and the same window,
and was never listed. So after the last part the integration reads the status every
second, for up to 60 seconds, until it leaves 2, then lists `/local` and reports the
upload only when the file is there, and otherwise that the printer discarded it.
Unloading the entry at any point of an upload, the parts included, ends it with an
error, without reopening the socket.

The list names each file by its path alone, with no size or date, so a file the
printer discards under a name already in `/local` would find the old one and read as
kept. Before the first part the integration therefore lists `/local`, and refuses
the upload when a file of that name is there: delete or rename it first. The
upload is also refused when that list does not come.

## Camera

Opened on a 16K V1.5.6 through this adapter, from a machine on the printer's LAN:
386 `{"Enable": 1}` answered `Ack` 0 and `VideoUrl` `rtsp://<host>:554/video`, a
still came in 5.2 seconds and five live frames in 6.9, and after each close
`NumberOfVideoStreamConnected` was back to 0 of 2 and 386 `{"Enable": 0}` answered
`Ack` 0. The picture is 1280x720, the vat seen from the front.

The printer sends the video over UDP to the address that asked for it, so Home
Assistant has to be on the printer's own network: Home Assistant OS, or a container
on the host network. Behind NAT, such as a container on a bridge network or WSL,
the frames never arrive, and the camera gives nothing.

The sources agree on three hazards:

* The server takes RTSP over UDP only, and its timestamps do not always increase
  (huygens), so Home Assistant's stream component, which prefers TCP, is not used.
* `NumberOfVideoStreamConnected` counts RTSP sessions, of at most 2, and a session
  drops only on an RTSP `TEARDOWN`. A client killed without one keeps its session
  until the printer is switched off and on (source 2, `camera-stream-leak.md`).
* Past the cap, 386 answers `Ack` 1 whether it switches on or off.

So the camera is an opt-in, and the integration relays JPEG frames from one ffmpeg:

1. It opens only when a fresh command 1 shows a camera and 0 of 2 sessions in use,
   not before layer 3 of a print, not within 10 seconds of the last open, and never
   while it already holds the camera: a second open is refused at once, not queued.
2. It sends 386 `Enable: 1` and reaches the `VideoUrl` at the entry's address.
3. ffmpeg runs with `-rtsp_transport udp -use_wallclock_as_timestamps 1 -fflags
   nobuffer -err_detect ignore_err`, one frame for a still and 2 frames a second for
   a live view.
4. It stops ffmpeg with `q`, then SIGTERM, waiting 5 seconds each, so ffmpeg sends
   its `TEARDOWN`, and kills it only as a last resort with a warning. Then it sends
   386 `Enable: 0`. Unloading the entry does the same over the open socket, once an
   open in progress has given up, so no ffmpeg starts and no socket reopens after it.
5. A still is handed out again for 20 seconds instead of opening the camera, since
   Home Assistant asks for one every 10 seconds while a dashboard shows it. The
   timelapse asks once a layer, at most once a poll (30 seconds by default), so at
   the default its frames never repeat; while a live view runs, a still is its
   latest frame.

## History

Command 321 `{"Id": ["<id>"]}` returns `HistoryDetailList` entries with `TaskName`
(`/media/mmcblk0p3/<file>.goo`), `BeginTime` and `EndTime` in seconds, `TaskStatus`
1, `AlreadyPrintLayer`, `MD5`, a `Thumbnail` at
`http://<host>:3030/media/mmcblk0p1/history_image/<id>.bmp`, an empty
`TimeLapseVideoUrl`, and `SliceInformation` from the slicer: resolution, layer
height, layer counts, exposure times in milliseconds, the slicer's `print_time` in
seconds and the machine's real size. The integration does not read it.

## Not supported, and why

* **The vat's temperature target.** No command sets it; only 133 skips the preheat.
* **The printer's own timelapse** (387). A persistent setting that the spec says can
  stop a print on a printer not bound to Elegoo's app.
* **SDCP V1 printers**, such as the Saturn 3 Ultra and the Mars 4 Ultra. They speak
  SDCP over MQTT, and the config flow refuses them.
* **More than one resin printer from one discovery broadcast.** The first reply is
  kept; add another by its address.

## Still to verify

Whether a printing Saturn pushes its status unasked; a job to its end (9); the camera
during a print; the phases 0 and 10; what `HeatStatus` means; and every model other than the 16K. The Saturn 4
Ultra and the Mars 5 Ultra are named by source 4, have no vat reading here, and are
marked unverified.

## Checking a printer

Close Elegoo's slicer and app first. If a Centauri Carbon entry still points at the
address, it stops with an error saying a resin printer answered: remove it.

```sh
python tools/acceptance_sdcp_resin.py <host>                  # read-only
python tools/acceptance_sdcp_resin.py <host> --save out.json  # also every raw reply
python tools/acceptance_sdcp_resin.py <host> --keepalive 150  # hold the socket on the status heartbeat
python tools/acceptance_sdcp_resin.py <host> --watch 3600     # print each status change of a print
```

It saves the discovery reply, tries the ports, sets the entry up through the
registry, checks the snapshot's units, reads the files and the history, checks that
a nozzle target is refused with nothing sent, and fails if any command other than
0, 1, 258, 320 or 321 went out. `tools/probe_sdcp.py`, `tools/acceptance_sdcp.py`,
`tools/acceptance_camera.py` and `tools/verify_sdcp.py` are for the Centauri Carbon,
and stop when the address answers as a resin printer.

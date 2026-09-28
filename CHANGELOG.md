# Changelog

## Unreleased

**Added**

* **The Anycubic Kobra 3, Kobra 4, Kobra S1 and Kobra X**, in LAN mode, added with
  their address alone. Status, temperatures, fans, speed, the light, pause, resume
  and stop; home and jog on the Kobra X; the Kobra X's built-in four-colour changer
  and any ACE as a multi-material unit with auto-feed; the camera as a stream; the
  file list; and starting a print as an opt-in. On the Kobra 3, 4 and S1,
  temperatures, fans and speed apply only during a print, and the card says so.
  The Kobra X is checked on a printer, firmware 2.0.1.9, except printing, speed and
  auto-feed. The other models are built from other projects' published work and
  marked unverified; `tools/acceptance_kobra.py` checks one.
* **Controls say why they are refused.** A printer now reports the commands it
  supports but will not take right now, with the reason, such as a Centauri Carbon
  2 that moves its head only while idle. The card draws those controls disabled and
  shows the reason; buttons, numbers and switches carry it as a `blocked_reason`
  attribute for automations; and a refused command never reaches the printer. Every
  printer refuses to move, start a print or change filament during a job, which the
  card used to enforce on its own.
* **Discovery finds every printer that answers.** When several do, the setup asks
  which one to add, and printers already set up are not offered again. A printer
  that reports a serial number is keyed by it, so a new address does not add it twice.
* **Printers whose camera is a video stream** are played by Home Assistant, and the
  card shows them with Home Assistant's own camera card. No printer uses this yet.
* One protocol can now serve several printer models, each with the controls it
  really has, and the card marks a model nobody has measured as unverified.
* MQTT over TLS, for printers whose broker needs it.

**Fixed**

* A second connection attempt to a Centauri Carbon that was already connected
  opened a second socket and kept one of the printer's five client slots until the
  printer timed it out.
* `tools/acceptance_cc2.py` failed to start, and `tools/acceptance_sdcp.py` crashed
  on an unreachable printer.

**Removed**

* PrusaLink, which was listed but never installed and so never reached the menu.

**Development**

* `requirements-test.txt` pins the test harness. The card tests need Node 22.12.
* Every adapter runs one shared contract suite against its fake printer.

## 0.5.1

**Fixed**

* **The card showed "Configuration error" on some page loads, and on nearly every
  load in the phone app.** Home Assistant's frontend replaces the browser's
  custom-element registry with one of its own as it starts, and dashboards look
  cards up only there. The card was loaded as an extra module, in parallel with the
  frontend's own code, and when it ran first it registered itself in the browser's
  registry, where the dashboard never looks; the error then stayed until the page
  was reloaded. Which one ran first depended on caches and CPU speed, which is why a
  hard reload helped, F5 sometimes did not, and a phone almost never recovered. This
  is Home Assistant frontend issue #52960. Reproduced in a real Home Assistant
  2026.9.4 by holding back the frontend's bundle: the old card failed 17 loads out of
  17, the new one none.
  * The card is now a Lovelace resource, which the dashboard loads itself once the
    frontend has started. The integration adds it, keeps it at the current version,
    takes over one added by hand instead of duplicating it, and removes it with the
    last printer. With Lovelace resources kept in YAML it still loads as an extra
    module.
  * The card also registers itself again in the frontend's registry once that
    exists, so it recovers even when it does run first.
* **A printer that was off when Home Assistant started left the dashboard without
  the card**, and the card without its data, until a printer answered: both were
  registered only after the first printer had. They are now registered as soon as
  the integration loads.
* **A printer switched off at its plug when Home Assistant started was missing from
  the card**, so the card could not offer the power button that switches it on. It
  is now listed as not answering, with its power button and no other control, until
  it answers.
* An empty `jog_steps:` or `temperature_presets:` in the card's YAML is treated as
  not set instead of breaking the card, and a drawing error goes to the browser's
  console instead of turning the card into an error card.

**Notes**

* HACS now offers the integration from Home Assistant 2025.1, the oldest version its
  tests run on. It declared 2024.6, where it could not load.

## 0.5.0

**Added**

* **Multi-material units, such as Elegoo's CANVAS.** The card gets a filament
  button and a strip on the Status tab, and both open a popup that draws the unit
  the way it stands, with each slot as a spool in its filament's colour and the slot
  in use marked. Selecting a slot shows its material, brand, colour and nozzle range.
  Where the printer allows it, the popup loads a slot into the nozzle, unloads the
  one in use, records which filament is in a slot from the printer's own list, and
  switches auto-refill. Loading and unloading ask first, and are off while the
  printer is busy. While the unit works, the popup says what it is doing.
* **A sensor per slot** with the filament's name, or `empty`, and its material,
  brand, colour and nozzle range as attributes; a sensor for the filament in use;
  and an auto-refill switch where the printer can be told. They appear when the
  printer first reports a unit, so a printer without one gets none.
* **The Centauri Carbon 2** reads its CANVAS with method 2005 and drives it with
  2001 to 2004, the methods Elegoo's own page sends.
* **The Centauri Carbon** reads its CANVAS with command 324, measured on a live
  printer, and only while its status says one is attached. Its firmware's page has
  no command to load, unload or edit a slot, so the card shows the slots and leaves
  those to the printer's screen.
* Four commands for the card and for automations: `load_filament`,
  `unload_filament`, `set_filament` and `set_auto_refill`.

**Fixed**

* **A Centauri Carbon stopped answering after a power cycle until its web page was
  opened.** Measured on the printer: it closes a client that does not send the text
  `ping` its page sends every 30 seconds, and it pushes its status only when asked.
  The integration sent neither, so its socket was closed every minute and, after a
  power cycle, it kept reporting the status it had before. It now keeps the socket
  the way the page does: it pings every 30 seconds, asks for the status on every
  connection and whenever the last one is more than 20 seconds old, and treats a
  socket that has gone silent as dead. On the live printer it held one socket for
  100 seconds without a reconnect, where one that did not ping was closed after 61.
* **The Centauri Carbon's camera stayed dark after a power cycle.** The printer's
  page switches the camera on with command 386 before it shows it. The integration
  now sends the same request before it reads the camera, and again after a failure.
* A dropped Centauri Carbon socket was sometimes left open, which kept one of the
  printer's five client slots until the printer timed it out.
* **A command's effect showed only at the next poll**, up to 30 seconds later.
  The printer is now read again as soon as a command is taken.

## 0.4.2

**Fixed**

* **A long file name pushed the card past its own edge.** The card's main column
  grew to fit its longest line, so a long name in the file list or of the job in
  progress widened the camera, the tabs and everything else with it, and they ran
  under the next card on the dashboard. The column now stays the width of the card.
* The round buttons (light, power, delete) were squeezed into ovals next to a long
  name. They keep their shape.

**Added**

* **Text that does not fit is cut with an ellipsis, and shown whole in a popup.**
  This applies to the printer's name and model line, the job, file names and sizes,
  the status and temperature tiles, the tabs and the card title. The popup opens
  when the mouse rests on the text, and on a tap on a phone. It opens only when
  the text is actually cut. It stays on its text while the card updates, and closes
  with Escape or when the file list scrolls.
* Messages, such as the note that starting a print is turned off, still wrap onto
  more lines rather than being cut. A long word in one, such as a file name, now
  breaks instead of running out of the card.

## 0.4.1

**Verified**

* **The Centauri Carbon 2 adapter has been run against a live printer** on firmware
  02.01.00.00, in LAN-only mode with an access code. The read-only acceptance check
  passed 13 of 13. The active checks, run with the owner's approval, passed 21 of
  23: status, temperatures, the three fans, the light, homing, jogging, the file
  list, an upload and the camera all answered as the adapter expects.
* Pause, resume, stop and starting a print need a print in progress and have not
  been sent to hardware yet.

**Notes**

* The two checks that did not pass are firmware behaviour. The speed mode can only
  be changed during a print; an idle printer refuses it, and the card shows that
  refusal. After homing, Y parks beyond the printable area, and the firmware clamps a
  move from there to the edge of it.
* Homing heats the nozzle to about 140 °C for the printer's probe, and leaves it hot.
  Use the card's power button with that in mind; it warns about a hot nozzle.
* No code change for users. The integration's diagnostics now report what was
  measured on hardware, and the test suite's fake printer sends what the hardware
  sends.

## 0.4.0

**Added**

* **The Elegoo Centauri Carbon 2.** It speaks JSON over an MQTT broker that runs on
  the printer, not SDCP, so it has an adapter of its own: registration, the
  heartbeat the printer expects, status deltas merged into the last full status
  with a full read when deltas go missing, pause, resume, stop, nozzle and bed
  targets, fans, speed modes, the light, homing and jogging while idle, the file
  list, uploads, and the camera on port 8080. Starting a print is an opt-in, as on
  the first Centauri Carbon. The MQTT client is part of the integration, so there
  is still no dependency to install.
* **The Centauri Carbon is one entry in the protocol menu**, followed by a step
  that asks which model it is.
* **The config flow finds a Centauri Carbon 2's serial number itself**, and refuses
  a printer in cloud mode with the instruction to turn LAN-only mode on, rather
  than creating an entry that never connects.
* **A new card that drives the whole printer**: live camera with the job on top of
  it, status, temperatures with presets and a cool-down, fan and speed sliders, a
  joystick with a Z column, homing and keyboard control, the file list with print,
  delete and upload, and a power button for the smart plug a printer runs from.
  Switching a printer off asks first, and warns when it is printing or its nozzle
  is still hot. The card has a visual editor.
* An upload endpoint for the card, authenticated like any Home Assistant API call.
* `tools/acceptance_cc2.py`, a read-only check of a real Centauri Carbon 2.

**Fixed**

* **The card's camera restarted every five seconds.** Every poll signs fresh URLs,
  and the card put the new one on a new image element, so the stream was torn down
  and reopened on each poll. The card now builds each printer once, keeps the URL
  its stream started with, and re-signs it only after an hour or a failure.
* **The card could not list printers.** The WebSocket `list` command referred to a
  name it never imported, so it failed as soon as one printer was configured.
* The state entity the `list` command reports was looked up under a unique id no
  entity has.
* The card's editor was never offered, because the card did not say it had one.
* **The Centauri Carbon's light read as on whenever its state was known**, off
  included, so the light switch showed on and a toggle could only ever turn it off.

**Notes**

* The Centauri Carbon 2 adapter follows Elegoo's elegoo-link SDK and two community
  clients measured on firmware 02.01.00.00. The test printer answered discovery and
  took the MQTT connection, but it was in cloud mode, so no request has been
  answered by hardware for this project yet.

## 0.3.0

**Fixed**

* **A printer that was switched off and back on reconnects on its own.** It did
  not before: the entry had to be reloaded by hand. Reading a printer is now what
  reconnects to it. Before, a read only consulted the last status the adapter had
  cached and reported "offline" without ever trying to reach the printer again, so
  the connection was never re-established however long the printer had been back.
* The adapter treated a socket object that was present but closed as a live
  connection, which made its own reconnect a silent no-op even when something did
  call it.
* An unanswered request now drops the socket instead of leaving it in place, so the
  next attempt reconnects rather than sending into a dead connection for ever.
* A poll that cannot reach the printer returns an offline snapshot instead of
  raising. A printer that is switched off is an expected state, not an error, and
  raising left the entities unavailable with no reading of why.

**Added**

* Five reconnect tests that power the printer off and on again against the real
  coordinator and adapter, including a long outage, a failed command while the
  printer is off, and a check that recovery clears the stale error rather than only
  flipping the connected flag.

**Notes**

* Verified on a live Elegoo Centauri Carbon: 21 of 21 adapter checks and 39
  distinct camera frames in 4 seconds, about 10 frames per second.

## 0.2.0

**Fixed**

* **The camera is live.** It was not before. The card loaded a still snapshot, which
  only changed when the card polled, so a live camera looked like a slideshow that
  updated every few seconds. The card now loads the integration's MJPEG stream and
  the picture moves.
* **The camera stream never worked at all**, which is why the snapshot was the only
  thing that appeared. The stream view handed the frame iterator to `async with`,
  and an async generator is not a context manager, so the response closed empty on
  the first line. A regression test now drives the real camera view over HTTP and
  asserts that distinct frames keep arriving.
* The camera's idle timer is cancelled on unload instead of being left sleeping on
  the event loop after the printer has gone.

**Added**

* `tools/acceptance_camera.py`, which measures a printer's camera against real
  hardware and reports the frame rate and how many frames were distinct.

**Notes**

* Verified on a live Elegoo Centauri Carbon: 49 distinct frames in 6 seconds, about
  8 frames per second, one upstream connection for the whole run.

## 0.1.0

First release.

**Added**

* Multi-protocol adapter layer. One config entry per printer, one adapter per
  protocol, one shared snapshot, and no protocol name anywhere outside
  `adapters/`.
* Elegoo SDCP adapter, developed against a live Centauri Carbon on firmware
  `V1.4.49`. Status, attributes, file list, pause, resume, stop, temperatures,
  fan duty, print speed, chamber light, and the MJPEG camera.
* Klipper via Moonraker adapter, with a WebSocket status subscription.
* OctoPrint adapter.
* Duet / RepRapFirmware adapter, including the Digest round trip and the session
  key, `M25` before `M0` for a cancel, and `G91`/`G1`/`G90` for a relative jog.
* Web-only adapter for a printer whose only interface is its own page.
* Reverse proxy for a printer's embedded web UI, with the upstream port as a path
  segment, signed path-segment tokens, response-header scrubbing, and HTML and CSS
  rewriting.
* WebSocket bridge for a proxied printer's control socket.
* Camera support with one shared upstream connection per printer, fanned out to
  every viewer. This is a requirement on hardware whose camera server keeps only a
  few connection slots and leaks them.
* Entity platforms for sensors, binary sensors, buttons, numbers, switches and
  cameras. Every entity exists only when the printer grants the capability it
  needs.
* `custom:generic-3dprinter-card`, a Lovelace card for one printer or a whole
  fleet. It draws a control per capability the backend reports.
* A per-printer opt-in for commands that are known to be risky on specific
  firmware, declared as data by the adapter rather than as a branch in the
  integration. On Elegoo SDCP, starting a print over the network is off by default.
* Diagnostics, with every credential key redacted.
* Tools for working on the SDCP protocol against real hardware: an acceptance
  check that drives the real adapter, a raw frame probe, a status schema dumper,
  and two bundle miners.

**Known gaps**

* PrusaLink is registered but not implemented, so it is not offered in the config
  flow. Its registration and capability set are in place for the adapter to fill
  in.
* The Klipper, OctoPrint and Duet adapters come from the documented API surface of
  those protocols. No printer of those kinds was available during development, so
  each registration records what is inferred rather than verified.
* Bambu Lab, Creality and Anycubic are researched but not implemented. Their
  transports need bespoke binary clients.
* The Elegoo Centauri Carbon 2 uses a different protocol and is not supported.

**Notes**

* Entity names come from `translations/en.json`. Run
  `tools/sync_translations.py` after editing `strings.json`.

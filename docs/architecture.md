# Architecture

## The problem

A home lab accumulates 3D printers that share nothing. Each vendor invents its
own LAN protocol, its own notion of a print job, its own way to say "72 percent
done". Home Assistant then has one integration per protocol, each with its own
entities and its own dashboard card, and no single place to see the fleet.

This integration inverts that. One config entry is one printer. One adapter per
protocol translates that printer into one shared model. One card renders whatever
the fleet can actually do.

## The load-bearing decision: adapters, not branches

Every protocol difference lives behind one interface. Nothing outside
`custom_components/generic_3dprinter/adapters/` may ask which protocol a printer
speaks. That rule is the whole design, and it is what makes a new protocol cost
one module rather than an edit in five files.

```
config entry ──> PrinterConfig ──> adapter (one per protocol)
                                        │
                                        ├─ PrinterSnapshot   (what the card renders)
                                        ├─ capabilities      (what the card offers)
                                        └─ CameraSource      (how frames arrive)
```

## The two transports

The fleet splits in a way that has to be modelled, not branched around.

**API printers** speak something we can drive: SDCP, MQTT, Moonraker, OctoPrint,
Duet. They give a real snapshot, and their commands map onto the normalised
command vocabulary. They may or may not also have a web UI, and may or may not
have a camera.

**Web-only printers** expose nothing but an embedded web page. There is no state
to poll and no command to send. For these the integration reverse-proxies the
printer's own page through Home Assistant and renders it inside the card, so a
dashboard on HTTPS can show a printer that only speaks HTTP on the LAN.

The second case is a real adapter with an empty command set and a `WEB_UI`
capability, not a special case threaded through the coordinator. The only thing
the coordinator does differently is skip polling, which is a property of the
snapshot (`snapshot_available = False`), not a type check.

## Normalised state

`PrinterSnapshot` is the sole currency between adapters and everything else. It
is frozen, and every field that a protocol might not be able to express is
optional with `None` meaning "this printer has no such reading". That is not
hedging: it is the measured truth. OctoPrint reports no layer count
at all, and Duet computes progress instead of reporting it.

Consequences that follow, and that the card must respect:

* `current_layer` and `total_layers` are optional. A card that assumes them is
  broken on half the fleet.
* Remaining time is normalised to **seconds** at the adapter boundary. Bambu
  reports minutes and Anycubic reports minutes, so the conversion happens in
  those adapters and never leaks upward.
* Fan speeds are normalised to **0 to 100 percent**. Bambu reports 0 to 15.
* Print state is a closed enum, because every protocol has a different string for
  "paused" and the card must not learn eight vocabularies.

## Capabilities are data

Capabilities are a declared set on the registration, not a property of the
protocol name and not a pile of booleans on the entity. Every command maps to one
capability (`protocols.COMMAND_CAPABILITY`), and an entity is created only when
its capability is granted, so an unsupported command produces no entity rather
than a button that fails when pressed. The card reads the same set, so a printer
with no camera shows no camera pane and a printer that cannot pause shows no
pause button.

Three refinements keep that rule true on real hardware.

**Model profiles.** One protocol can serve several models that differ in what
they have: the Anycubic Kobra line speaks one protocol, and only some of its
models have a chamber. A registration lists `ModelProfile`s, the adapter records
the model it reached during setup, and the granted set becomes the registration's,
narrowed to the profile, less any opt-in not granted. Setup runs before entities
are created, so they see the narrowed set. A model the registration does not know
keeps the whole set and its snapshot says so; a profile nobody measured is marked
unverified on the card.

**State rules.** Some commands are supported but refused in some states: a
Centauri Carbon 2 moves its head only while idle, and an Anycubic applies
temperatures only during a print. An adapter declares `BlockRule`s, and the base
class's `async_read` evaluates them into `snapshot.blocked`, a map from command to
the reason a user reads. `async_send` refuses a blocked command before the
adapter sees it; entities stay available and report `blocked_reason`; the card
draws the control disabled with the reason. Every printer inherits one default
rule: no motion, start or filament change while a job is running.

**Camera kinds.** `CAMERA` is a JPEG camera the integration relays through one
shared upstream connection. `CAMERA_STREAM` is a native video stream: the adapter
returns its URL from `async_stream_source()`, Home Assistant's stream component
plays it, and the card embeds Home Assistant's own camera card for it.

## Finding printers

Discovery never names a protocol outside `adapters/`. An adapter may implement
two read-only class methods, `async_discover` to broadcast for its printers and
`async_identify` to ask one host whether it is one. The engine in `discovery.py`
asks every registration: a printer that identifies itself outranks a product name
on a web page (each registration's `http_markers`), which outranks an open port
(its `ports`). A result carries `prefill`, the configuration the printer gave
about itself, such as its serial number. The config flow offers every printer
found and not yet configured.

## Holding adapters to one contract

`tests/adapter_kit/` runs the same contract against every protocol's fake
printer: setup and teardown are idempotent, a snapshot stays inside the model's
units, a granted command reaches the wire while an absent, withheld or blocked one
never does, identifying a printer sends it nothing, and a printer that went away
is recovered. A protocol with no harness fails the suite. Printers that host
their own MQTT broker share `fake_broker.py`, with TLS. `tools/acceptance_kit.py`
is the same idea for real hardware: read-only by default, and `--active` asks
before each command that changes the printer.

## Refusing what is dangerous

Some commands are known to be unsafe on specific hardware. Sending SDCP command
`128` (start print) is reported to have crashed a Centauri Carbon, and an unknown
command code can take down that printer's whole host daemon, killing an active
print. That is not a failure the user can retry.

The rule is a fixed allowlist. An adapter may only send command codes it declares
in its verified table. Codes the printer's own firmware documents but that no
source has verified on real hardware are gated behind an explicit per-printer
opt-in that defaults to off, and the config flow states the risk where the user
turns it on. Nothing in this integration ever probes an unknown code.

## Reverse proxy, for printers with no API

The web proxy is deliberately the same shape as the proven design in
`ha-generic-video-proxy`, because that design already solved the hard parts:

* every byte reaches the browser from the Home Assistant origin, so a dashboard
  on HTTPS can embed a printer that only speaks HTTP, with no mixed content;
* requests are authorised by an HMAC-signed token carried in a **path segment**,
  because a browser cannot attach an `Authorization` header to `<img>`, `<script>`
  or `<link>` sub-resources, and a token in a query string is fragile;
* the upstream address lives **inside** the signed payload, so the proxy cannot be
  pointed at an arbitrary host, and it is not an open proxy;
* the WebSocket bridge is registered as a raw aiohttp route, since
  `HomeAssistantView` only dispatches `get/post/put/delete/patch/head/options`
  and therefore cannot accept an upgrade.

The one genuinely new problem is that a printer's page and its control socket can
be on **different ports** (the Centauri Carbon uses 80 for the UI, 3030 for SDCP,
3031 for the camera) while the proxy has one origin. A per-entry port map solves
it: routes are namespaced by port, and the rewriter rewrites `ws://host:3030/...`
to the proxied equivalent rather than trying to preserve the port.

## Idempotent setup

Route and card registration happen once per Home Assistant instance, guarded by a
flag in `hass.data` and re-checked under a lock, because several config entries
load concurrently and each one would otherwise try to register the same routes.
Per-printer state lives on the config entry's own runtime object, so two printers
never share mutable state and never need serialising against each other.

## What this deliberately does not do

* No cloud. Every adapter is LAN-only. A printer that needs an account to work is
  out of scope, and Bambu's firmware-side developer-mode gate is reported as a
  state rather than retried.
* No protocol is claimed to work that was not probed. The Centauri Carbon was
  probed live. Moonraker, OctoPrint and Duet are built from the documented
  and cited API surface, and their adapters say so.
* No generic "unknown printer" auto-detection beyond a port probe that tells the
  user what it found and asks them to confirm. Guessing a protocol and then
  sending it a command is how printers get bricked.

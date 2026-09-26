/**
 * jsdom tests for the Generic 3D Printer Controller card.
 *
 * The card's one hard rule is that it draws a control only when the backend reports
 * the capability for it. Most of these tests exist to hold that rule down, because
 * the failure mode is a button that looks available and fails when pressed. The
 * rest hold down what the card does with a control: the command and parameters it
 * sends, and the confirmation it asks for before something that cannot be undone.
 *
 * Run with: npm test
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test, { after } from "node:test";

import { JSDOM } from "jsdom";

/* The card starts a refresh interval on connect, which would keep the test runner
 * alive forever. Every mounted card is torn down at the end of the run, and the
 * process is then ended explicitly so a stray timer cannot hang CI. */
const mounted = [];

after(() => {
  for (const { card, dom } of mounted) {
    try {
      card.remove();
      dom.window.close();
    } catch {
      // A closed window is not a failure.
    }
  }
  setTimeout(() => process.exit(0), 50).unref();
});

const HERE = dirname(fileURLToPath(import.meta.url));
const CARD_PATH = resolve(
  HERE,
  "../../custom_components/generic_3dprinter/www/generic-3dprinter-card.js",
);

/** A snapshot shaped exactly like the one the Python adapter produces. */
function snapshot(overrides = {}) {
  return {
    protocol: "sdcp_cc1",
    connected: true,
    print_state: "printing",
    capabilities: [
      "pause",
      "resume",
      "stop",
      "set_hotend_temp",
      "set_bed_temp",
      "set_chamber_temp",
      "set_fan_speed",
      "set_speed",
      "set_light",
      "file_list",
      "camera",
      "web_ui",
    ],
    progress: 42.0,
    current_layer: 264,
    total_layers: 627,
    remaining: 5100.5,
    elapsed: 2400.0,
    filename: "/local/ECC_0.4_part_PLA_0.2_5h10m.gcode",
    job_id: "3d315103",
    speed_factor: 100.0,
    flow_factor: null,
    hotend: { current: 219.8, target: 220 },
    bed: { current: 55.1, target: 55 },
    chamber: { current: 32.7, target: 0 },
    fans: { model: 100, auxiliary: 69, chamber: 68, hotend: null, controller: null },
    position: { x: 101.1, y: 77.83, z: 22.45 },
    homed_axes: [],
    lights: ["chamber"],
    camera: true,
    model: "Centauri Carbon",
    firmware: "V1.4.49",
    serial: "5c441dd30105041800009c0000000000",
    errors: [],
    ...overrides,
  };
}

function description(overrides = {}) {
  return {
    entry_id: "entry1",
    name: "Centauri Carbon",
    protocol: "sdcp_cc1",
    model: "Centauri Carbon",
    firmware: "V1.4.49",
    connected: true,
    camera: true,
    web_ui: true,
    camera_url: "/api/generic_3dprinter/entry1/camera.mjpeg/tok",
    snapshot_url: "/api/generic_3dprinter/entry1/snapshot.jpg/tok",
    status_url: "/api/generic_3dprinter/entry1/status/tok",
    web_proxy_url: "/api/generic_3dprinter/entry1/web/tok/p80/",
    web_ui_url: "http://192.168.128.143/",
    camera_stats: { live: true, viewers: 1, frames: 120, last_frame_bytes: 45000, last_error: null },
    unsafe_features: [],
    printer: snapshot(),
    ...overrides,
  };
}

/** A Centauri Carbon 2 that is idle, can jog and home, and reports a chamber reading. */
function idleCc2(overrides = {}) {
  return description({
    name: "Workshop CC2",
    protocol: "elegoo_cc2",
    model: "Centauri Carbon 2",
    web_ui: false,
    unsafe_features: [
      {
        id: "cc2_start_print",
        label: "Allow starting a print over the network",
        reason: "It heats and moves the printer.",
      },
    ],
    printer: snapshot({
      protocol: "elegoo_cc2",
      print_state: "idle",
      progress: null,
      current_layer: null,
      total_layers: null,
      remaining: null,
      elapsed: null,
      filename: null,
      capabilities: [
        "pause",
        "resume",
        "stop",
        "set_hotend_temp",
        "set_bed_temp",
        "chamber_sensor",
        "set_fan_speed",
        "set_speed",
        "set_light",
        "home",
        "jog",
        "file_list",
        "file_upload",
        "camera",
      ],
      chamber: { current: 30.1, target: null },
      fans: { model: 0, auxiliary: 0, chamber: 10, hotend: 100, controller: 100 },
      hotend: { current: 26, target: 0 },
      bed: { current: 24, target: 0 },
      homed_axes: ["x", "y"],
    }),
    ...overrides,
  });
}

/** Build a card in a fresh document with a scripted `hass`. */
async function mountCard({
  printers,
  descriptions,
  config = {},
  failList = false,
  states = {},
  files = [],
  confirm = true,
  describe,
} = {}) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", {
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  const { window } = dom;

  global.window = window;
  global.document = window.document;
  global.customElements = window.customElements;
  global.HTMLElement = window.HTMLElement;
  global.CustomEvent = window.CustomEvent;
  global.FormData = window.FormData;
  global.Option = window.Option;
  global.setInterval = window.setInterval.bind(window);
  global.clearInterval = window.clearInterval.bind(window);

  const confirmations = [];
  window.confirm = (message) => {
    confirmations.push(message);
    return confirm;
  };

  const source = readFileSync(CARD_PATH, "utf8");
  window.eval(source);

  const calls = [];
  const services = [];
  const uploads = [];
  const hass = {
    states,
    callWS: async (message) => {
      // Objects built in the page's realm have its prototypes, so they are copied
      // into this one before being compared.
      calls.push(plain(message));
      if (message.type === "generic_3dprinter/list") {
        if (failList) throw new Error("the integration is not loaded");
        return { printers: printers ?? [] };
      }
      if (message.type === "generic_3dprinter/describe") {
        if (describe) return describe(message);
        const value = (descriptions ?? {})[message.entry_id];
        if (value === undefined) throw new Error("no such printer");
        return value;
      }
      if (message.type === "generic_3dprinter/send") {
        return { ok: true };
      }
      if (message.type === "generic_3dprinter/files") {
        return { files };
      }
      throw new Error(`unexpected message ${message.type}`);
    },
    callService: async (domain, service, data) => {
      services.push(plain({ domain, service, data }));
    },
    fetchWithAuth: async (url, init) => {
      uploads.push({ url, init });
      const name = init.body.get("file").name;
      return {
        ok: true,
        status: 200,
        json: async () => ({ file: { name, path: name, size: 4 } }),
      };
    },
  };

  const card = window.document.createElement("generic-3dprinter-card");
  card.setConfig(config);
  window.document.body.appendChild(card);
  card.hass = hass;
  await settle(card);
  mounted.push({ card, dom });
  return { card, calls, services, uploads, confirmations, window, dom, hass };
}

/** Wait for the card's initial round trip and its render to finish. */
async function settle(card) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await tick(5);
    const root = card.shadowRoot;
    if (!root) continue;
    if (root.querySelector(".printer") || root.querySelector(".empty")) return;
  }
  await tick(40);
}

const plain = (value) => JSON.parse(JSON.stringify(value));

const tick = (ms = 20) => new Promise((resolveTick) => setTimeout(resolveTick, ms));

/** True when neither the node nor any ancestor is hidden, or in a closed dialog. */
function visible(node) {
  for (let current = node; current; current = current.parentNode) {
    if (current.hidden) return false;
    if (current.localName === "dialog" && !current.open && !current.hasAttribute("open")) return false;
    if (current.host) break;
  }
  return true;
}

function all(card, selector) {
  return [...card.shadowRoot.querySelectorAll(selector)].filter(visible);
}

function texts(card, selector) {
  return all(card, selector).map((node) => node.textContent.trim());
}

function command(card, name, extra = "") {
  return all(card, `[data-command="${name}"]${extra}`);
}

async function openTab(card, key) {
  const tab = card.shadowRoot.querySelector(`.tab[data-tab="${key}"]`);
  assert.ok(tab && visible(tab), `expected a visible ${key} tab`);
  tab.click();
  await tick();
}

const sent = (calls) => calls.filter((call) => call.type === "generic_3dprinter/send");

// ------------------------------------------------------------------- status

test("renders the printer name, state and secondary line", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Centauri Carbon", protocol: "sdcp_cc1" }],
    descriptions: { entry1: description() },
  });
  assert.deepEqual(texts(card, ".name"), ["Centauri Carbon"]);
  assert.deepEqual(texts(card, ".state"), ["Printing"]);
  assert.match(card.shadowRoot.querySelector(".subtitle").textContent, /Centauri Carbon/);
  assert.match(card.shadowRoot.querySelector(".subtitle").textContent, /V1\.4\.49/);
});

test("shows progress, layers, times and temperatures", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Centauri Carbon" }],
    descriptions: { entry1: description() },
  });
  assert.equal(card.shadowRoot.querySelector(".progress-fill").style.width, "42%");
  assert.deepEqual(texts(card, ".progress-label"), ["42%"]);
  const stat = (key) => card.shadowRoot.querySelector(`.stat[data-stat="${key}"] .stat-value`).textContent;
  assert.equal(stat("layer"), "264 / 627");
  assert.equal(stat("remaining"), "1h 25m");
  assert.equal(stat("elapsed"), "40m 00s");
  assert.equal(stat("speed"), "100%");
  assert.ok(stat("eta"), "a running job shows when it will be done");
  assert.deepEqual(texts(card, ".panel-status .temp-value"), [
    "220 °C → 220 °C",
    "55 °C → 55 °C",
    "33 °C",
  ]);
  assert.deepEqual(texts(card, ".job-name"), ["ECC_0.4_part_PLA_0.2_5h10m.gcode"]);
});

test("a chamber that is only a reading is shown", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  assert.ok(texts(card, ".temp-label").includes("Chamber"));
});

test("a printer that cannot pause gets no job controls and no progress bar", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Web only" }],
    descriptions: {
      entry1: description({
        printer: snapshot({
          protocol: "web_only",
          print_state: "unknown",
          capabilities: ["web_ui"],
          progress: null,
          current_layer: null,
          total_layers: null,
          remaining: null,
          elapsed: null,
          filename: null,
          hotend: { current: null, target: null },
          bed: { current: null, target: null },
          chamber: { current: null, target: null },
          fans: {},
        }),
        model: null,
        firmware: null,
        camera: false,
        snapshot_url: null,
      }),
    },
  });
  assert.deepEqual(all(card, "[data-command]"), []);
  assert.deepEqual(all(card, ".progress-fill"), []);
  assert.deepEqual(all(card, ".camera img"), []);
  // A printer with no API must not be described as idle, because nobody knows.
  assert.deepEqual(texts(card, ".state"), ["Unknown"]);
  await openTab(card, "controls");
  assert.deepEqual(texts(card, ".empty-panel"), ["This printer reports no settings it can be given."]);
});

test("draws only the controls the printer reports capabilities for", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Printer" }],
    descriptions: {
      entry1: description({
        printer: snapshot({ print_state: "idle", capabilities: ["home", "file_list", "web_ui"] }),
      }),
    },
  });
  assert.deepEqual(command(card, "pause"), []);
  assert.deepEqual(command(card, "stop"), []);
  await openTab(card, "controls");
  // Home without jog: the home buttons, no joystick.
  assert.ok(command(card, "home").length > 0);
  assert.deepEqual(command(card, "jog"), []);
  assert.deepEqual(all(card, ".heater"), []);
  assert.deepEqual(all(card, ".slider-row"), []);
});

test("an offline printer says so instead of showing stale numbers", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Printer" }],
    descriptions: {
      entry1: description({
        connected: false,
        last_error: "the printer is unreachable",
        printer: snapshot({ connected: false, print_state: "unknown", errors: ["the printer is unreachable"] }),
      }),
    },
  });
  const [warning] = all(card, ".warning");
  assert.ok(warning, "expected an offline warning");
  assert.match(warning.textContent, /unreachable/);
  await openTab(card, "controls");
  assert.ok(all(card, ".heater-set").every((node) => node.disabled));
});

test("printer errors are listed under the header", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Printer" }],
    descriptions: {
      entry1: description({ printer: snapshot({ errors: ["printer exception 109: filament has run out"] }) }),
    },
  });
  assert.deepEqual(texts(card, ".error-line"), ["printer exception 109: filament has run out"]);
});

// ------------------------------------------------------------------- camera

test("the camera pane loads the live stream, not a still snapshot", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Camera" }],
    descriptions: { entry1: description() },
  });
  const image = card.shadowRoot.querySelector(".camera img");
  assert.ok(image, "expected a camera image");
  assert.equal(image.getAttribute("src"), "/api/generic_3dprinter/entry1/camera.mjpeg/tok");
  assert.equal(image.dataset.mode, "stream");
});

test("without a stream url the camera falls back to the still", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Camera" }],
    descriptions: { entry1: description({ camera_url: null }) },
  });
  const image = card.shadowRoot.querySelector(".camera img");
  assert.ok(image, "expected a camera image");
  assert.equal(image.getAttribute("src"), "/api/generic_3dprinter/entry1/snapshot.jpg/tok");
  assert.equal(image.dataset.mode, "snapshot");
});

test("a stream that fails degrades to the still instead of vanishing", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Camera" }],
    descriptions: { entry1: description() },
  });
  const image = card.shadowRoot.querySelector(".camera img");
  image.dispatchEvent(new card.ownerDocument.defaultView.Event("error"));
  assert.equal(image.dataset.mode, "snapshot");
  assert.equal(image.getAttribute("src"), "/api/generic_3dprinter/entry1/snapshot.jpg/tok");
});

test("a poll that signs a new url does not restart the stream", async () => {
  let counter = 0;
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Camera" }],
    describe: () => {
      counter += 1;
      // Every real describe signs fresh URLs, so the token changes on every poll.
      return description({
        camera_url: `/api/generic_3dprinter/entry1/camera.mjpeg/tok${counter}`,
        snapshot_url: `/api/generic_3dprinter/entry1/snapshot.jpg/tok${counter}`,
      });
    },
  });
  const image = card.shadowRoot.querySelector(".camera img");
  const before = image.getAttribute("src");
  await card._refreshAll();
  await card._refreshAll();
  assert.ok(counter >= 3);
  assert.equal(card.shadowRoot.querySelector(".camera img"), image, "the image element was rebuilt");
  assert.equal(image.getAttribute("src"), before);
});

test("the camera can be hidden from the configuration", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Camera" }],
    descriptions: { entry1: description() },
    config: { show_camera: false },
  });
  assert.deepEqual(all(card, ".camera"), []);
});

// --------------------------------------------------------------------- job

test("pressing pause sends the normalised command over the websocket api", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Printer" }],
    descriptions: { entry1: description() },
  });
  const [pause] = command(card, "pause");
  assert.ok(pause, "expected a pause button while printing");
  assert.equal(pause.disabled, false);

  pause.click();
  await tick();

  const commands = sent(calls);
  assert.equal(commands.length, 1);
  assert.equal(commands[0].entry_id, "entry1");
  assert.equal(commands[0].command, "pause");
  assert.deepEqual(Object.keys(commands[0].data), []);
});

test("pause is disabled while the printer is not printing, resume is not", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Printer" }],
    descriptions: { entry1: description({ printer: snapshot({ print_state: "paused" }) }) },
  });
  assert.equal(command(card, "pause")[0].disabled, true);
  assert.equal(command(card, "resume")[0].disabled, false);
});

test("stop asks first, and a refusal sends nothing", async () => {
  const refused = await mountCard({
    printers: [{ entry_id: "entry1", name: "Printer" }],
    descriptions: { entry1: description() },
    confirm: false,
  });
  command(refused.card, "stop")[0].click();
  await tick();
  assert.equal(refused.confirmations.length, 1);
  assert.deepEqual(sent(refused.calls), []);

  const accepted = await mountCard({
    printers: [{ entry_id: "entry1", name: "Printer" }],
    descriptions: { entry1: description() },
  });
  command(accepted.card, "stop")[0].click();
  await tick();
  assert.deepEqual(sent(accepted.calls).map((call) => call.command), ["stop"]);
});

// ---------------------------------------------------------------- controls

test("a nozzle target typed in is sent as set_hotend_temp", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  await openTab(card, "controls");
  const row = card.shadowRoot.querySelector('.heater[data-heater="hotend"]');
  const input = row.querySelector(".heater-input");
  input.value = "215";
  input.dispatchEvent(new card.ownerDocument.defaultView.Event("input"));
  row.querySelector(".heater-set").click();
  await tick();
  assert.deepEqual(sent(calls).map((call) => [call.command, call.data]), [
    ["set_hotend_temp", { value: 215 }],
  ]);
});

test("the nudge buttons change the target before it is set", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  await openTab(card, "controls");
  const row = card.shadowRoot.querySelector('.heater[data-heater="bed"]');
  const [minus, plus] = row.querySelectorAll(".mini");
  plus.click();
  plus.click();
  minus.click();
  row.querySelector(".heater-set").click();
  await tick();
  assert.deepEqual(sent(calls).map((call) => call.data), [{ value: 5 }]);
});

test("a value being typed is not overwritten by the next reading", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  await openTab(card, "controls");
  const input = card.shadowRoot.querySelector('.heater[data-heater="hotend"] .heater-input');
  input.value = "199";
  input.dispatchEvent(new card.ownerDocument.defaultView.Event("input"));
  await card._refreshAll();
  assert.equal(input.value, "199");
});

test("a preset sets the nozzle and the bed together", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  await openTab(card, "controls");
  const pla = all(card, ".preset").find((node) => node.textContent.trim() === "PLA");
  pla.click();
  await tick();
  assert.deepEqual(
    sent(calls).map((call) => [call.command, call.data.value]),
    [
      ["set_hotend_temp", 210],
      ["set_bed_temp", 60],
    ],
  );
});

test("cool down turns off only the heaters the printer can set", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  await openTab(card, "controls");
  all(card, ".cooldown")[0].click();
  await tick();
  assert.deepEqual(sent(calls).map((call) => call.command).sort(), ["set_bed_temp", "set_hotend_temp"]);
  assert.ok(sent(calls).every((call) => call.data.value === 0));
});

test("the presets come from the configuration when given", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
    config: { temperature_presets: [{ name: "TPU", hotend: 225, bed: 45 }] },
  });
  await openTab(card, "controls");
  assert.deepEqual(texts(card, ".preset"), ["TPU"]);
});

test("a fan slider sends its channel and value", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  await openTab(card, "controls");
  const slider = card.shadowRoot.querySelector('.slider-row[data-fan="chamber"] .slider');
  slider.value = "40";
  slider.dispatchEvent(new card.ownerDocument.defaultView.Event("input"));
  slider.dispatchEvent(new card.ownerDocument.defaultView.Event("change"));
  await tick();
  assert.deepEqual(sent(calls).map((call) => [call.command, call.data]), [
    ["set_fan_speed", { value: 40, channel: "chamber" }],
  ]);
  // The fans the firmware runs itself are shown, never offered as sliders.
  assert.equal(card.shadowRoot.querySelector('.slider-row[data-fan="hotend"]'), null);
  assert.match(card.shadowRoot.querySelector(".reported-fans").textContent, /Hotend 100%/);
});

test("the speed slider sends set_speed", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  await openTab(card, "controls");
  const slider = card.shadowRoot.querySelector(".slider-row.speed .slider");
  slider.value = "50";
  slider.dispatchEvent(new card.ownerDocument.defaultView.Event("change"));
  await tick();
  assert.deepEqual(sent(calls).map((call) => [call.command, call.data]), [["set_speed", { value: 50 }]]);
});

test("the light button toggles the light the printer reports", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  const [light] = command(card, "set_light");
  assert.ok(light.classList.contains("on"));
  light.click();
  await tick();
  assert.deepEqual(sent(calls).map((call) => call.data), [{ on: false, channel: "chamber" }]);
});

// ------------------------------------------------------------------ motion

test("the joystick jogs by the selected step in both directions", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  await openTab(card, "controls");
  const jog = (axis, direction) =>
    all(card, `[data-command="jog"][data-axis="${axis}"][data-direction="${direction}"]`)[0];

  // A jog in flight disables the pad, so moves are not queued behind each other.
  all(card, '.step[data-step="1"]')[0].click();
  jog("X", "1").click();
  assert.equal(jog("Y", "-1").disabled, true);
  await tick();
  jog("Y", "-1").click();
  await tick();
  all(card, '.step[data-step="0.1"]')[0].click();
  jog("Z", "1").click();
  await tick();

  assert.deepEqual(sent(calls).map((call) => [call.command, call.data]), [
    ["jog", { axis: "X", distance: 1 }],
    ["jog", { axis: "Y", distance: -1 }],
    ["jog", { axis: "Z", distance: 0.1 }],
  ]);
});

test("the arrow keys move the head too", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  await openTab(card, "controls");
  const joystick = card.shadowRoot.querySelector(".joystick");
  const { KeyboardEvent } = card.ownerDocument.defaultView;
  joystick.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
  await tick();
  joystick.dispatchEvent(new KeyboardEvent("keydown", { key: "PageDown", bubbles: true }));
  await tick();
  assert.deepEqual(sent(calls).map((call) => call.data), [
    { axis: "X", distance: -10 },
    { axis: "Z", distance: -10 },
  ]);
});

test("the home buttons home the axes they name", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  await openTab(card, "controls");
  for (const axes of ["XYZ", "XY", "Z"]) {
    all(card, `[data-command="home"][data-axes="${axes}"]`)[0].click();
    await tick();
  }
  assert.deepEqual(sent(calls).map((call) => call.data.axes), ["XYZ", "XY", "Z"]);
  assert.deepEqual(texts(card, ".axis-name"), ["X", "Y", "Z"]);
  assert.equal(all(card, ".axis.homed").length, 2);
});

test("the head cannot be moved while the printer refuses it, and the card says why", async () => {
  const cc2 = idleCc2();
  cc2.printer.print_state = "printing";
  cc2.printer.blocked = { home: "the printer is not idle", jog: "the printer is not idle" };
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: cc2 },
  });
  await openTab(card, "controls");
  assert.ok(command(card, "jog").every((node) => node.disabled));
  assert.ok(command(card, "home").every((node) => node.disabled));
  const joystick = card.shadowRoot.querySelector(".joystick");
  joystick.dispatchEvent(new card.ownerDocument.defaultView.KeyboardEvent("keydown", { key: "ArrowUp" }));
  await tick();
  assert.deepEqual(sent(calls), []);
  assert.match(card.shadowRoot.querySelector(".motion-hint").textContent, /not idle/);
});

test("the card leaves motion to the printer's own rules", async () => {
  // A printer that reports nothing blocked may be moved, whatever its state.
  const cc2 = idleCc2();
  cc2.printer.print_state = "printing";
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: cc2 },
  });
  await openTab(card, "controls");
  assert.ok(command(card, "jog").every((node) => !node.disabled));
  command(card, "home")[0].click();
  await tick();
  assert.equal(sent(calls).length, 1);
});

test("a setting the printer refuses now is disabled, with the printer's reason", async () => {
  const cc2 = idleCc2();
  cc2.printer.blocked = { set_bed_temp: "the printer applies this only during a print" };
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: cc2 },
  });
  await openTab(card, "controls");
  const bed = card.shadowRoot.querySelector('.heater[data-heater="bed"]');
  const nozzle = card.shadowRoot.querySelector('.heater[data-heater="hotend"]');
  assert.ok([...bed.querySelectorAll("button, input")].every((node) => node.disabled));
  assert.ok([...nozzle.querySelectorAll("button, input")].every((node) => !node.disabled));
  assert.match(card.shadowRoot.querySelector(".heater-hint").textContent, /only during a print/);
  assert.deepEqual(sent(calls), []);
});

test("a refused fan or speed setting says why", async () => {
  const cc2 = idleCc2();
  cc2.printer.blocked = { set_speed: "only during a print", set_fan_speed: "only during a print" };
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: cc2 },
  });
  await openTab(card, "controls");
  assert.ok(all(card, ".fans input").every((node) => node.disabled));
  assert.match(card.shadowRoot.querySelector(".fan-hint").textContent, /only during a print/);
});

// ------------------------------------------------------------------- power

test("without a power entity there is no power button", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  assert.deepEqual(all(card, ".power-toggle"), []);
});

test("the power button switches an idle, cold printer off without asking", async () => {
  const { card, services, confirmations } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
    config: { power_entity: "switch.printer_plug" },
    states: { "switch.printer_plug": { entity_id: "switch.printer_plug", state: "on" } },
  });
  const [power] = all(card, ".power-toggle");
  assert.ok(power.classList.contains("on"));
  power.click();
  await tick();
  assert.equal(confirmations.length, 1, "switching off always asks once");
  assert.deepEqual(services, [
    { domain: "homeassistant", service: "turn_off", data: { entity_id: "switch.printer_plug" } },
  ]);
});

test("switching off a printing printer warns that the print is lost", async () => {
  const { card, services, confirmations } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Printer" }],
    descriptions: { entry1: description() },
    config: { power_entity: "switch.printer_plug" },
    states: { "switch.printer_plug": { entity_id: "switch.printer_plug", state: "on" } },
    confirm: false,
  });
  all(card, ".power-toggle")[0].click();
  await tick();
  assert.match(confirmations[0], /cannot be resumed/);
  assert.deepEqual(services, []);
});

test("switching off a hot nozzle warns about its fan", async () => {
  const hot = idleCc2();
  hot.printer.hotend = { current: 190, target: 0 };
  const { card, confirmations } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: hot },
    config: { power_entity: "switch.printer_plug" },
    states: { "switch.printer_plug": { entity_id: "switch.printer_plug", state: "on" } },
    confirm: false,
  });
  all(card, ".power-toggle")[0].click();
  await tick();
  assert.match(confirmations[0], /190 °C/);
});

test("a printer that is switched off says so, and turns on without asking", async () => {
  const { card, services, confirmations } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: {
      entry1: idleCc2({ connected: false, last_error: "timeout contacting the printer" }),
    },
    config: { power_entity: "light.printer" },
    states: { "light.printer": { entity_id: "light.printer", state: "off" } },
  });
  assert.deepEqual(texts(card, ".state"), ["Off"]);
  assert.deepEqual(texts(card, ".warning"), ["The printer is switched off."]);
  assert.equal(card.shadowRoot.querySelector(".camera img"), null, "no stream is opened to a printer that is off");
  all(card, ".power-toggle")[0].click();
  await tick();
  assert.deepEqual(confirmations, []);
  assert.deepEqual(services, [
    { domain: "homeassistant", service: "turn_on", data: { entity_id: "light.printer" } },
  ]);
});

test("the power button follows the entity when it changes", async () => {
  const { card, hass } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
    config: { power_entity: "switch.printer_plug" },
    states: { "switch.printer_plug": { entity_id: "switch.printer_plug", state: "on" } },
  });
  card.hass = { ...hass, states: { "switch.printer_plug": { entity_id: "switch.printer_plug", state: "off" } } };
  assert.equal(all(card, ".power-toggle")[0].classList.contains("on"), false);
});

test("a power entity that does not exist is reported", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
    config: { power_entity: "switch.missing" },
  });
  assert.match(texts(card, ".warning")[0], /switch\.missing does not exist/);
  assert.equal(all(card, ".power-toggle")[0].disabled, true);
});

// ------------------------------------------------------------------- files

const FILES = [
  { name: "benchy.gcode", path: "benchy.gcode", size: 1234567, modified: "2026-09-01T10:00:00+00:00" },
  { name: "cube.gcode", path: "cube.gcode", size: 42, modified: null },
];

test("the files tab lists the printer's files", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
    files: FILES,
  });
  await openTab(card, "files");
  await tick();
  assert.equal(calls.filter((call) => call.type === "generic_3dprinter/files").length, 1);
  assert.deepEqual(texts(card, ".file-name"), ["benchy.gcode", "cube.gcode"]);
  // Starting a print is withheld, so there is no print button and the hint says why.
  assert.deepEqual(command(card, "start_print"), []);
  assert.match(texts(card, ".files-hint")[0], /Allow starting a print over the network/);
});

test("a file is printed after confirmation", async () => {
  const cc2 = idleCc2();
  cc2.printer.capabilities = [...cc2.printer.capabilities, "start_print", "file_delete"];
  const { card, calls, confirmations } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: cc2 },
    files: FILES,
  });
  await openTab(card, "files");
  await tick();
  const row = [...card.shadowRoot.querySelectorAll(".file")].find((node) => node.dataset.file === "cube.gcode");
  row.querySelector('[data-command="start_print"]').click();
  await tick();
  assert.match(confirmations[0], /cube\.gcode/);
  assert.deepEqual(sent(calls).map((call) => [call.command, call.data]), [
    ["start_print", { filename: "cube.gcode" }],
  ]);
  row.querySelector('[data-command="delete_file"]').click();
  await tick();
  assert.deepEqual(sent(calls).map((call) => call.command), ["start_print", "delete_file"]);
});

test("a file is uploaded through the authenticated endpoint", async () => {
  const { card, uploads } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
    files: FILES,
  });
  await openTab(card, "files");
  const input = card.shadowRoot.querySelector(".upload-input");
  const { File } = card.ownerDocument.defaultView;
  Object.defineProperty(input, "files", { value: [new File(["G28\n"], "part.gcode")], configurable: true });
  input.dispatchEvent(new card.ownerDocument.defaultView.Event("change"));
  await tick(40);
  assert.equal(uploads.length, 1);
  assert.equal(uploads[0].url, "/api/generic_3dprinter/entry1/upload");
  assert.equal(uploads[0].init.method, "POST");
  assert.equal(uploads[0].init.body.get("file").name, "part.gcode");
});

// ---------------------------------------------------------------- long text

const LONG_NAME = "ECC_0.4_Crimson Wing Dragon_1789719946_generate_eSUN PLA+ _0.2_10h4m.gcode";

/** jsdom has no layout, so a text is made to overflow by giving it a scroll width. */
function overflowLongText(window) {
  Object.defineProperty(window.HTMLElement.prototype, "scrollWidth", {
    configurable: true,
    get() {
      return this.textContent.length > 20 ? 900 : 0;
    },
  });
}

async function mountWithLongFile() {
  const mountedCard = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
    files: [...FILES, { name: LONG_NAME, path: LONG_NAME, size: 66100000, modified: null }],
  });
  overflowLongText(mountedCard.window);
  await openTab(mountedCard.card, "files");
  await tick();
  return mountedCard;
}

const fileName = (card, text) => all(card, ".file-name").find((node) => node.textContent === text);

const pointer = (window, type, node, pointerType = "mouse") => {
  const event = new window.PointerEvent(type, { bubbles: true, composed: true, pointerType });
  node.dispatchEvent(event);
};

test("a file name too long for the card is cut, and shown whole on hover", async () => {
  const { card, window } = await mountWithLongFile();
  const tip = card.shadowRoot.querySelector(".tip");
  const long = fileName(card, LONG_NAME);
  assert.ok(long.classList.contains("trunc"), "expected the file name to be cut to one line");

  pointer(window, "pointerover", long);
  assert.equal(tip.hidden, true, "the popup waits for the mouse to rest");
  await tick(300);
  assert.equal(tip.hidden, false);
  assert.equal(tip.textContent, LONG_NAME);
  assert.equal(tip.getAttribute("role"), "tooltip");

  // A name that fits gets no popup, and moving onto it closes the last one.
  pointer(window, "pointerover", fileName(card, "cube.gcode"));
  assert.equal(tip.hidden, true);
  await tick(300);
  assert.equal(tip.hidden, true);
});

test("the popup stays on its text when a reading redraws the file list", async () => {
  const { card, window } = await mountWithLongFile();
  const tip = card.shadowRoot.querySelector(".tip");
  const before = fileName(card, LONG_NAME);
  pointer(window, "pointerover", before);
  await tick(300);
  assert.equal(tip.hidden, false);

  // The rebuilt row sits where the old one was.
  card.shadowRoot.elementFromPoint = () => fileName(card, LONG_NAME);
  await card.refreshOne("entry1");
  assert.equal(before.isConnected, false, "expected the reading to rebuild the row");
  assert.equal(tip.hidden, false);
  assert.equal(tip.textContent, LONG_NAME);
});

test("on a touch screen a tap shows the whole text and a second tap hides it", async () => {
  const { card, window } = await mountWithLongFile();
  const tip = card.shadowRoot.querySelector(".tip");
  const long = fileName(card, LONG_NAME);
  pointer(window, "pointerover", long, "touch");
  pointer(window, "pointerdown", long, "touch");
  long.click();
  assert.equal(tip.hidden, false, "a tap shows the popup at once");
  assert.equal(tip.textContent, LONG_NAME);
  pointer(window, "pointerdown", long, "touch");
  long.click();
  assert.equal(tip.hidden, true);
});

test("a printer without file capabilities has no files tab", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Printer" }],
    descriptions: {
      entry1: description({ printer: snapshot({ capabilities: ["pause", "stop"] }) }),
    },
  });
  const tab = card.shadowRoot.querySelector('.tab[data-tab="files"]');
  assert.equal(visible(tab), false);
});

// ---------------------------------------------------------------- filament

const FILAMENT_COMMANDS = ["load_filament", "unload_filament", "set_filament", "set_auto_refill"];

/** A CANVAS as the backend reports it: slot 2 empty, slot 4 feeding the nozzle. */
function canvasSystem(overrides = {}) {
  const slot = (index, name, color, loaded, active = false) => ({
    unit: 0,
    slot: index,
    loaded,
    active,
    material: "PLA",
    name,
    brand: "ELEGOO",
    color,
    min_temp: 190,
    max_temp: 230,
  });
  return {
    units: [
      {
        unit: 0,
        connected: true,
        name: "CANVAS",
        slots: [
          slot(0, "PLA", "#2850DF", true),
          slot(1, "PLA Basic", "#FFFFFF", false),
          slot(2, "PLA Silk", "#F32FF8", true),
          slot(3, "PLA Matte", "#000000", true, true),
        ],
      },
    ],
    auto_refill: false,
    activity: null,
    active: { unit: 0, slot: 3 },
    ...overrides,
  };
}

const PRESETS = [
  { material: "PLA", name: "PLA", min_temp: 190, max_temp: 230, brands: ["ELEGOO", "Generic"] },
  { material: "PLA", name: "PLA Carbon", min_temp: 190, max_temp: 230, brands: ["Generic"] },
  { material: "PETG", name: "PETG PRO", min_temp: 230, max_temp: 260, brands: ["ELEGOO", "Generic"] },
];

/** An idle Centauri Carbon 2 with a CANVAS, able to do `commands`. */
function canvasCc2({ commands = FILAMENT_COMMANDS, system = canvasSystem(), state = "idle" } = {}) {
  const cc2 = idleCc2({ filament_presets: PRESETS });
  cc2.printer.capabilities = [...cc2.printer.capabilities, "filament_slots", ...commands];
  cc2.printer.filament = system;
  cc2.printer.print_state = state;
  if (state !== "idle") {
    const reason = "the printer is not idle";
    cc2.printer.blocked = { load_filament: reason, unload_filament: reason, set_filament: reason, home: reason, jog: reason };
  }
  return cc2;
}

async function openFilament(card) {
  const toggle = card.shadowRoot.querySelector(".filament-toggle");
  assert.ok(toggle && visible(toggle), "expected a filament button");
  toggle.click();
  await tick();
  const dialog = card.shadowRoot.querySelector(".filament-dialog");
  assert.ok(visible(dialog.querySelector(".fd-body")), "expected the filament popup to be open");
  return dialog;
}

const spool = (dialog, key) => dialog.querySelector(`.spool[data-key="${key}"]`);

test("the filament button and strip appear only for a printer with a multi-material unit", async () => {
  const plain = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: idleCc2() },
  });
  assert.deepEqual(all(plain.card, ".filament-toggle"), []);
  assert.deepEqual(all(plain.card, ".filament-strip"), []);

  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: canvasCc2() },
  });
  assert.equal(all(card, ".filament-toggle").length, 1);
  assert.deepEqual(texts(card, ".strip-label"), ["PLA Matte in use, slot 4"]);
  assert.equal(all(card, ".strip-dot").length, 4);
});

test("the popup draws the unit the way it stands, with the slot in use selected", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: canvasCc2() },
  });
  const dialog = await openFilament(card);
  // Counter-clockwise from the top left, as on the CANVAS: 1 and 4 on top, 2 and 3 below.
  assert.deepEqual([...dialog.querySelectorAll(".spool")].map((node) => node.dataset.key), [
    "0:0",
    "0:3",
    "0:1",
    "0:2",
  ]);
  assert.ok(spool(dialog, "0:3").classList.contains("active"));
  assert.equal(spool(dialog, "0:3").getAttribute("aria-pressed"), "true");
  assert.equal(spool(dialog, "0:1").querySelector(".spool-name").textContent, "Empty");
  assert.equal(dialog.querySelector(".fd-detail-name").textContent, "4 · PLA Matte");
  assert.equal(dialog.querySelector(".fd-badge").textContent, "In use");

  dialog.querySelector(".fd-close").click();
  await tick();
  assert.equal(visible(dialog.querySelector(".fd-body")), false);
});

test("loading a slot asks first and sends its unit and slot", async () => {
  const { card, calls, confirmations } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: canvasCc2() },
  });
  const dialog = await openFilament(card);
  spool(dialog, "0:2").click();
  await tick();
  const load = dialog.querySelector(".fd-load");
  assert.equal(load.disabled, false);
  load.click();
  await tick();
  assert.match(confirmations[0], /slot 3, PLA Silk/);
  assert.deepEqual(sent(calls).map((call) => [call.command, call.data]), [
    ["load_filament", { unit: 0, slot: 2 }],
  ]);
});

test("a load that is not confirmed sends nothing", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: canvasCc2() },
    confirm: false,
  });
  const dialog = await openFilament(card);
  spool(dialog, "0:0").click();
  await tick();
  dialog.querySelector(".fd-load").click();
  await tick();
  assert.deepEqual(sent(calls), []);
});

test("load is offered for a loaded slot not in use, unload only for the one in use", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: canvasCc2() },
  });
  const dialog = await openFilament(card);
  const load = dialog.querySelector(".fd-load");
  const unload = dialog.querySelector(".fd-unload");

  spool(dialog, "0:1").click();
  await tick();
  assert.equal(load.disabled, true, "an empty slot cannot be loaded");
  assert.equal(unload.disabled, true);

  spool(dialog, "0:3").click();
  await tick();
  assert.equal(load.disabled, true, "the slot in use is already loaded");
  assert.equal(unload.disabled, false);
  unload.click();
  await tick();
  assert.deepEqual(sent(calls).map((call) => [call.command, call.data]), [
    ["unload_filament", { unit: 0, slot: 3 }],
  ]);
});

test("a slot's filament is recorded from the printer's own list", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: canvasCc2() },
  });
  const dialog = await openFilament(card);
  spool(dialog, "0:1").click();
  await tick();
  dialog.querySelector(".fd-edit").click();
  await tick();
  const form = dialog.querySelector(".fd-form");
  assert.equal(visible(form), true);
  assert.equal(visible(dialog.querySelector(".fd-detail")), false);

  const brand = form.querySelector(".fd-brand");
  assert.deepEqual([...brand.options].map((option) => option.value), ["ELEGOO", "Generic"]);
  assert.equal(brand.value, "ELEGOO");
  const material = form.querySelector(".fd-material");
  // PLA Carbon is only sold as Generic, so ELEGOO does not list it.
  assert.deepEqual([...material.querySelectorAll("option")].map((option) => option.value), ["PLA", "PETG PRO"]);
  assert.deepEqual([...material.querySelectorAll("optgroup")].map((group) => group.label), ["PLA", "PETG"]);

  material.value = "PETG PRO";
  material.dispatchEvent(new card.ownerDocument.defaultView.Event("change"));
  assert.equal(form.querySelector('[aria-label="Lowest nozzle temperature"]').value, "230");
  assert.equal(form.querySelector('[aria-label="Highest nozzle temperature"]').value, "260");
  form.querySelector(".fd-color").value = "#00ff00";

  form.querySelector(".fd-save").click();
  await tick();
  assert.deepEqual(sent(calls).map((call) => [call.command, call.data]), [
    [
      "set_filament",
      {
        unit: 0,
        slot: 1,
        brand: "ELEGOO",
        color: "#00FF00",
        material: "PETG",
        name: "PETG PRO",
        min_temp: 230,
        max_temp: 260,
      },
    ],
  ]);
  assert.equal(visible(form), false, "the form closes once the printer took it");
});

test("auto-refill is switched from the popup", async () => {
  const { card, calls } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: canvasCc2() },
  });
  const dialog = await openFilament(card);
  const refill = dialog.querySelector(".fd-refill");
  assert.equal(visible(refill), true);
  assert.equal(refill.checked, false);
  refill.checked = true;
  refill.dispatchEvent(new card.ownerDocument.defaultView.Event("change"));
  await tick();
  assert.deepEqual(sent(calls).map((call) => [call.command, call.data]), [
    ["set_auto_refill", { on: true }],
  ]);
});

test("a printer that can only report its slots gets the picture and no buttons", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC1" }],
    descriptions: { entry1: canvasCc2({ commands: [], system: canvasSystem({ auto_refill: true }) }) },
  });
  const dialog = await openFilament(card);
  assert.equal(dialog.querySelectorAll(".spool").length, 4);
  assert.deepEqual([...dialog.querySelectorAll(".fd-actions button")].filter(visible), []);
  assert.match(dialog.querySelector(".fd-note").textContent, /on its own screen/);
  assert.equal(visible(dialog.querySelector(".fd-refill")), false);
  assert.equal(dialog.querySelector(".fd-refill-text").textContent, "On");
});

test("the slots cannot be changed while the printer is busy", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: canvasCc2({ state: "printing" }) },
  });
  const dialog = await openFilament(card);
  spool(dialog, "0:0").click();
  await tick();
  for (const name of [".fd-load", ".fd-unload", ".fd-edit"]) {
    assert.equal(dialog.querySelector(name).disabled, true, `${name} is enabled mid-print`);
  }
  assert.match(dialog.querySelector(".fd-note").textContent, /not idle/);
});

test("what the unit is doing is shown on the card and in the popup", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "CC2" }],
    descriptions: { entry1: canvasCc2({ system: canvasSystem({ activity: "Loading: heating the nozzle" }) }) },
  });
  assert.deepEqual(texts(card, ".strip-label"), ["Loading: heating the nozzle"]);
  const dialog = await openFilament(card);
  assert.equal(dialog.querySelector(".fd-activity").textContent, "Loading: heating the nozzle");
});

// ------------------------------------------------------------------- fleet

test("the fleet mode renders every configured printer, compactly", async () => {
  const { card } = await mountCard({
    printers: [
      { entry_id: "entry1", name: "First" },
      { entry_id: "entry2", name: "Second" },
    ],
    descriptions: {
      entry1: description({ entry_id: "entry1", name: "First" }),
      entry2: description({ entry_id: "entry2", name: "Second", printer: snapshot({ print_state: "idle" }) }),
    },
    config: { fleet: true },
  });
  assert.deepEqual(texts(card, ".name"), ["First", "Second"]);
  assert.deepEqual(all(card, ".tabs"), []);
  assert.equal(command(card, "pause").length, 2);
});

test("an empty fleet renders a helpful message rather than nothing", async () => {
  const { card } = await mountCard({ printers: [], descriptions: {} });
  assert.match(card.shadowRoot.querySelector(".empty").textContent, /No 3D printer/);
});

test("a printer whose description fails still renders from the list, without controls", async () => {
  const { card } = await mountCard({
    printers: [{ entry_id: "entry1", name: "Printer", model: "Centauri Carbon" }],
    descriptions: {},
  });
  assert.deepEqual(texts(card, ".name"), ["Printer"]);
  assert.deepEqual(all(card, "[data-command]"), []);
  assert.equal(all(card, ".warning").length, 1, "expected an unreachable warning");
});

test("a failing websocket api is reported instead of rendering a blank card", async () => {
  const { card } = await mountCard({ failList: true });
  const empty = card.shadowRoot.querySelector(".empty");
  assert.ok(empty, "expected an error message");
  assert.match(empty.textContent, /integration is not loaded/);
});

test("a configured printer is shown instead of the first one", async () => {
  const { card } = await mountCard({
    printers: [
      { entry_id: "entry1", name: "First" },
      { entry_id: "entry2", name: "Second" },
    ],
    descriptions: {
      entry1: description({ name: "First" }),
      entry2: description({ entry_id: "entry2", name: "Second" }),
    },
    config: { entry_id: "entry2" },
  });
  assert.deepEqual(texts(card, ".name"), ["Second"]);
});

// ------------------------------------------------------------------ config

test("the card offers a visual editor", async () => {
  const { card, window } = await mountCard({ printers: [], descriptions: {} });
  const editor = card.constructor.getConfigElement();
  assert.equal(editor.localName, "generic-3dprinter-card-editor");
  window.document.body.appendChild(editor);
  editor.setConfig({ power_entity: "switch.plug" });
  const changes = [];
  editor.addEventListener("config-changed", (event) => changes.push(plain(event.detail.config)));
  const input = editor.shadowRoot.querySelector('input[data-key="power_entity"]');
  assert.equal(input.value, "switch.plug");
  input.value = "switch.other";
  input.dispatchEvent(new window.Event("change"));
  assert.deepEqual(changes, [{ power_entity: "switch.other" }]);
});

test("bad jog steps are refused with a message", async () => {
  const { card } = await mountCard({ printers: [], descriptions: {} });
  assert.throws(() => card.setConfig({ jog_steps: [1, -5] }), /jog_steps/);
});

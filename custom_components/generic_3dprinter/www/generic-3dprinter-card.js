/**
 * Generic 3D Printer Controller card.
 *
 * One card that drives a whole printer: its camera, its job, its heaters, fans and
 * speed, a joystick for the head, its stored files, and the smart plug it is
 * powered from. It reads everything from the integration's own WebSocket API and
 * never learns which protocol a printer speaks: it reads the capability array the
 * backend reports and draws a control per capability present, so a printer that
 * cannot pause has no pause button and a printer that cannot jog has no joystick.
 *
 * The DOM of each printer is built once and then updated in place. That is what
 * keeps a live camera stream from being restarted on every poll, and what keeps a
 * value the user is typing from being overwritten by the next reading.
 *
 * Ships with the integration, so `custom:generic-3dprinter-card` is available as
 * soon as the integration is installed.
 */

const CARD_VERSION = "0.5.1";

const WS_LIST = "generic_3dprinter/list";
const WS_DESCRIBE = "generic_3dprinter/describe";
const WS_SEND = "generic_3dprinter/send";
const WS_FILES = "generic_3dprinter/files";
const API_BASE = "/api/generic_3dprinter";

/* How often the printer is read. */
const REFRESH_MS = 5000;

/* Signed URLs live for six hours. The camera keeps the URL it started with, so its
 * one upstream connection is never torn down by a poll, and it is re-signed well
 * before the old signature runs out. */
const CAMERA_RESIGN_MS = 60 * 60 * 1000;

/* After a camera that failed outright, how long to wait before trying again. */
const CAMERA_RETRY_MS = 30 * 1000;

const DEFAULT_JOG_STEPS = [0.1, 1, 10, 50];
const DEFAULT_PRESETS = [
  { name: "PLA", hotend: 210, bed: 60 },
  { name: "PETG", hotend: 240, bed: 80 },
  { name: "ABS", hotend: 255, bed: 100 },
];

/* A nozzle hotter than this still needs its fan, which the plug would cut. */
const HOT_NOZZLE = 50;

const TEMPERATURE_MAX = 350;
const TEMPERATURE_NUDGE = 5;

/* How long the mouse rests on a text cut short before the whole text is shown. */
const TIP_DELAY_MS = 250;

/* How long a text shown by a tap stays up, since a finger never leaves it. */
const TIP_TOUCH_MS = 4000;

/* A four-slot unit, such as Elegoo's CANVAS, numbers its slots counter-clockwise
 * from the top left: 1 top left, 2 bottom left, 3 bottom right, 4 top right. The
 * grid is filled row by row, so the slots are laid out in this order to match the
 * unit in front of the user. */
const FOUR_SLOT_ORDER = [0, 3, 1, 2];

/* Colours offered next to the colour picker when a slot's filament is set. */
const FILAMENT_SWATCHES = [
  "#FFFFFF", "#000000", "#898989", "#E53935", "#FB8C00",
  "#FDD835", "#43A047", "#1E88E5", "#8E24AA", "#F48FB1",
];

/* Brands offered when the printer has no filament list of its own. */
const DEFAULT_BRANDS = ["Generic"];

const STATE_COLORS = {
  idle: "var(--state-inactive-color, #9e9e9e)",
  preparing: "var(--warning-color, #ff9800)",
  printing: "var(--state-active-color, #4caf50)",
  paused: "var(--warning-color, #ff9800)",
  finished: "var(--info-color, #2196f3)",
  cancelled: "var(--state-inactive-color, #9e9e9e)",
  error: "var(--error-color, #f44336)",
  unknown: "var(--disabled-text-color, #bdbdbd)",
  off: "var(--disabled-text-color, #bdbdbd)",
};

const STATE_LABELS = {
  idle: "Idle",
  preparing: "Preparing",
  printing: "Printing",
  paused: "Paused",
  finished: "Finished",
  cancelled: "Cancelled",
  error: "Error",
  unknown: "Unknown",
  off: "Off",
};

/* A job is running or held: the head must not be moved and a new print not started. */
const ACTIVE_STATES = new Set(["printing", "paused", "preparing"]);

const HEATERS = [
  { key: "hotend", label: "Nozzle", command: "set_hotend_temp" },
  { key: "bed", label: "Bed", command: "set_bed_temp" },
  { key: "chamber", label: "Chamber", command: "set_chamber_temp" },
];

/* Fans the card can set, and fans it can only report. */
const SETTABLE_FANS = [
  { key: "model", label: "Part cooling" },
  { key: "auxiliary", label: "Auxiliary" },
  { key: "chamber", label: "Chamber" },
];
const REPORTED_FANS = [
  { key: "hotend", label: "Hotend" },
  { key: "controller", label: "Board" },
];

/* Material Design Icons, Apache 2.0. */
const ICONS = {
  power:
    "M16.56,5.44L15.11,6.89C16.84,7.94 18,9.83 18,12A6,6 0 0,1 12,18A6,6 0 0,1 6,12C6,9.83 7.16,7.94 8.88,6.88L7.44,5.44C5.36,6.88 4,9.28 4,12A8,8 0 0,0 12,20A8,8 0 0,0 20,12C20,9.28 18.64,6.88 16.56,5.44M13,3H11V13H13",
  light:
    "M12,2A7,7 0 0,0 5,9C5,11.38 6.19,13.47 8,14.74V17A1,1 0 0,0 9,18H15A1,1 0 0,0 16,17V14.74C17.81,13.47 19,11.38 19,9A7,7 0 0,0 12,2M9,21A1,1 0 0,0 10,22H14A1,1 0 0,0 15,21V20H9V21Z",
  pause: "M14,19H18V5H14M6,19H10V5H6V19Z",
  play: "M8,5.14V19.14L19,12.14L8,5.14Z",
  stop: "M18,18H6V6H18V18Z",
  home: "M10,20V14H14V20H19V12H22L12,3L2,12H5V20H10Z",
  up: "M7.41,15.41L12,10.83L16.59,15.41L18,14L12,8L6,14L7.41,15.41Z",
  down: "M7.41,8.58L12,13.17L16.59,8.58L18,10L12,16L6,10L7.41,8.58Z",
  left: "M15.41,16.58L10.83,12L15.41,7.41L14,6L8,12L14,18L15.41,16.58Z",
  right: "M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z",
  upload: "M9,16V10H5L12,3L19,10H15V16H9M5,20V18H19V20H5Z",
  delete: "M19,4H15.5L14.5,3H9.5L8.5,4H5V6H19M6,19A2,2 0 0,0 8,21H16A2,2 0 0,0 18,19V7H6V19Z",
  refresh:
    "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z",
  fullscreen: "M5,5H10V7H7V10H5V5M14,5H19V10H17V7H14V5M17,14H19V19H14V17H17V14M10,17V19H5V14H7V17H10Z",
  thermometer:
    "M15 13V5A3 3 0 0 0 9 5V13A5 5 0 1 0 15 13M12 4A1 1 0 0 1 13 5V8H11V5A1 1 0 0 1 12 4Z",
  fan: "M12,11A1,1 0 0,0 11,12A1,1 0 0,0 12,13A1,1 0 0,0 13,12A1,1 0 0,0 12,11M12.5,2C17,2 17.11,5.57 14.75,6.75C13.76,7.24 13.32,8.29 13.13,9.22C13.61,9.42 14.03,9.73 14.35,10.13C18.05,8.13 22.03,8.92 22.03,12.5C22.03,17 18.46,17.1 17.28,14.73C16.78,13.74 15.72,13.3 14.79,13.11C14.59,13.59 14.28,14 13.88,14.34C15.87,18.03 15.08,22 11.5,22C7,22 6.91,18.42 9.27,17.24C10.25,16.75 10.69,15.71 10.89,14.79C10.4,14.59 9.97,14.27 9.65,13.87C5.96,15.85 2,15.07 2,11.5C2,7 5.56,6.89 6.74,9.26C7.24,10.25 8.29,10.68 9.22,10.87C9.41,10.39 9.73,9.97 10.14,9.65C8.15,5.96 8.94,2 12.5,2Z",
  file: "M13,9V3.5L18.5,9M6,2C4.89,2 4,2.89 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2H6Z",
  open: "M14,3V5H17.59L7.76,14.83L9.17,16.24L19,6.41V10H21V3M19,19H5V5H12V3H5C3.89,3 3,3.9 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19V12H19V19Z",
  nozzle: "M7,2H17V8H19V13H16.5L13,17H11L7.5,13H5V8H7V2M10,22H2V20H10A1,1 0 0,0 11,19V18H13V19A3,3 0 0,1 10,22Z",
  load: "M2 12H4V17H20V12H22V17C22 18.11 21.11 19 20 19H4C2.9 19 2 18.11 2 17V12M12 15L17.55 9.54L16.13 8.13L13 11.25V2H11V11.25L7.88 8.13L6.46 9.55L12 15Z",
  unload: "M2 12H4V17H20V12H22V17C22 18.11 21.11 19 20 19H4C2.9 19 2 18.11 2 17V12M12 2L6.46 7.46L7.88 8.88L11 5.75V15H13V5.75L16.13 8.88L17.55 7.45L12 2Z",
  edit: "M20.71,7.04C21.1,6.65 21.1,6 20.71,5.63L18.37,3.29C18,2.9 17.35,2.9 16.96,3.29L15.12,5.12L18.87,8.87M3,17.25V21H6.75L17.81,9.93L14.06,6.18L3,17.25Z",
  close: "M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z",
  refill: "M12,6V9L16,5L12,1V4A8,8 0 0,0 4,12C4,13.57 4.46,15.03 5.24,16.26L6.7,14.8C6.25,13.97 6,13 6,12A6,6 0 0,1 12,6M18.76,7.74L17.3,9.2C17.74,10.04 18,11 18,12A6,6 0 0,1 12,18V15L8,19L12,23V20A8,8 0 0,0 20,12C20,10.43 19.54,8.97 18.76,7.74Z",
};

// ------------------------------------------------------------------ helpers

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};

const icon = (name, size = 20) => {
  const ns = "http://www.w3.org/2000/svg";
  const node = document.createElementNS(ns, "svg");
  node.setAttribute("viewBox", "0 0 24 24");
  node.setAttribute("width", String(size));
  node.setAttribute("height", String(size));
  node.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", ICONS[name]);
  path.setAttribute("fill", "currentColor");
  node.appendChild(path);
  return node;
};

/** A button with an icon, a label, and the command it sends as a data attribute. */
const button = (className, label, iconName, command) => {
  const node = el("button", className);
  node.type = "button";
  if (iconName) node.appendChild(icon(iconName, 18));
  if (label) node.appendChild(el("span", "label", label));
  node.title = label || "";
  node.setAttribute("aria-label", label || iconName || "");
  if (command) node.dataset.command = command;
  return node;
};

const asNumber = (value) =>
  value === null || value === undefined || value === "" || Number.isNaN(Number(value))
    ? null
    : Number(value);

const formatTemperature = (value) => {
  const number = asNumber(value);
  return number === null ? "—" : `${number.toFixed(0)} °C`;
};

const formatDuration = (seconds) => {
  const total = asNumber(seconds);
  if (total === null || total < 0) return "—";
  const whole = Math.round(total);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(whole % 60).padStart(2, "0")}s`;
  return `${whole}s`;
};

const formatEta = (remaining, now = new Date()) => {
  const seconds = asNumber(remaining);
  if (seconds === null || seconds < 0) return null;
  const at = new Date(now.getTime() + seconds * 1000);
  const time = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (at.toDateString() === now.toDateString()) return time;
  return `${at.toLocaleDateString([], { weekday: "short" })} ${time}`;
};

const formatBytes = (value) => {
  const bytes = asNumber(value);
  if (bytes === null) return "";
  const units = ["B", "KB", "MB", "GB"];
  let index = 0;
  let size = bytes;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
};

const formatStep = (step) => (step < 1 ? String(step) : String(Math.round(step)));

const baseName = (path) => String(path || "").split("/").pop();

/** Return true when an input is being edited, so a reading does not overwrite it. */
const editing = (input) =>
  input.dataset.dirty === "1" || input.getRootNode().activeElement === input;

/** Put `nodes` in `parent` in order, touching the DOM only when something moved. */
const replaceChildrenIfChanged = (parent, nodes) => {
  const current = [...parent.children];
  if (current.length !== nodes.length || current.some((node, index) => node !== nodes[index])) {
    parent.replaceChildren(...nodes);
  }
};

/**
 * The popup that shows a text cut short by an ellipsis in full.
 *
 * Every element with the `trunc` class takes part, and only while its text is
 * actually cut off. It opens when the mouse rests on the text, and on a tap, since
 * a touch screen has no hover. The text is whole in the DOM either way, so a screen
 * reader reads all of it. The popup lives on the card rather than beside the text,
 * so the scrolling file list cannot clip it. The file list and the status tiles are
 * rebuilt on every reading, which takes the text out from under the popup, so after
 * a redraw it looks up what now sits where its text was and stays on that. A text in
 * the filament dialog gets the popup inside the dialog, since the dialog sits in the
 * browser's top layer, above the card.
 */
class TextTip {
  constructor(root, container) {
    this.root = root;
    this.container = container;
    this.host = container;
    this.node = el("div", "tip");
    this.node.setAttribute("role", "tooltip");
    this.node.hidden = true;
    container.appendChild(this.node);
    this.anchor = null;
    this.pending = null;
    this.center = null;
    this.timer = null;
    this.pointer = "mouse";

    root.addEventListener("pointerdown", (event) => {
      this.pointer = event.pointerType || "mouse";
    });
    root.addEventListener("pointerover", (event) => {
      if (event.pointerType === "touch") return;
      const target = this._target(event.target);
      if (target && (target === this.anchor || target === this.pending)) return;
      this.hide();
      if (target) this._later(target, TIP_DELAY_MS);
    });
    root.addEventListener("pointerout", (event) => {
      if (event.pointerType === "touch") return;
      if (!event.relatedTarget || !root.contains(event.relatedTarget)) this.hide();
    });
    root.addEventListener("click", (event) => {
      if (this.pointer !== "touch") return;
      const target = this._target(event.target);
      if (!target || target === this.anchor) {
        this.hide();
        return;
      }
      this.show(target);
      if (this.anchor) this._later(null, TIP_TOUCH_MS);
    });
    root.addEventListener("keydown", (event) => {
      if (event.key === "Escape") this.hide();
    });
    // Scroll events do not bubble, so the file list's is caught on the way down.
    root.addEventListener("scroll", () => this.hide(), true);
  }

  _target(node) {
    if (!node || typeof node.closest !== "function") return null;
    const target = node.closest(".trunc");
    return target && this.root.contains(target) ? target : null;
  }

  /** Show `target` after `delay`, or hide when `target` is null. */
  _later(target, delay) {
    this._cancel();
    this.pending = target;
    this.timer = window.setTimeout(() => {
      this.timer = null;
      this.pending = null;
      if (target) this._paint(target);
      else this.hide();
    }, delay);
  }

  _cancel() {
    window.clearTimeout(this.timer);
    this.timer = null;
    this.pending = null;
  }

  show(target) {
    this._cancel();
    this._paint(target);
  }

  hide() {
    this._cancel();
    this.anchor = null;
    this.center = null;
    this.node.hidden = true;
  }

  /** Follow the text after a redraw: the same element, or what replaced it. */
  refresh() {
    if (!this.anchor) return;
    if (this.anchor.isConnected) {
      this._paint(this.anchor);
      return;
    }
    const found =
      this.center && typeof this.root.elementFromPoint === "function"
        ? this.root.elementFromPoint(this.center.x, this.center.y)
        : null;
    const next = this._target(found);
    if (next) this._paint(next);
    else this.hide();
  }

  /** Show the text of `target`, unless all of it is already on screen. */
  _paint(target) {
    const text = target.textContent.trim();
    if (!target.isConnected || !text || target.scrollWidth <= target.clientWidth) {
      this.hide();
      return;
    }
    this.anchor = target;
    this.host = target.closest("dialog") || this.container;
    if (this.node.parentNode !== this.host) this.host.appendChild(this.node);
    this.node.textContent = text;
    this.node.hidden = false;
    this._place();
  }

  /** Put the popup under the text, or above it when the card ends first. */
  _place() {
    const gap = 6;
    const margin = 8;
    const card = this.host;
    this.node.style.left = "0px";
    this.node.style.top = "0px";
    const anchor = this.anchor.getBoundingClientRect();
    const box = card.getBoundingClientRect();
    const tip = this.node.getBoundingClientRect();
    // A scrolled host, such as a tall dialog, moves its positioned children with it.
    const originX = box.left + card.clientLeft - card.scrollLeft;
    const originY = box.top + card.clientTop - card.scrollTop;
    const left = Math.max(margin, Math.min(anchor.left - originX - margin, card.clientWidth - tip.width - margin));
    let top = anchor.bottom - originY + gap;
    const above = anchor.top - originY - gap - tip.height;
    if (top + tip.height > card.clientHeight - margin && above >= margin) top = above;
    this.node.style.left = `${left}px`;
    this.node.style.top = `${top}px`;
    this.center = { x: anchor.left + anchor.width / 2, y: anchor.top + anchor.height / 2 };
  }
}

// ----------------------------------------------------------------- filament

const slotKey = (unit, slot) => `${unit}:${slot}`;

/** A slot's number as the printer's own screen shows it, counting from 1. */
const slotLabel = (unit, slot) => (unit === 0 ? String(slot + 1) : `${unit + 1}-${slot + 1}`);

/** What a slot holds, in a word or two. */
const slotTitle = (slot) => slot.name || slot.material || "Unknown filament";

/** Black or white, whichever reads better on `hex`. */
const inkFor = (hex) => {
  const match = /^#?([0-9a-f]{6})$/i.exec(String(hex || ""));
  if (!match) return "#fff";
  const value = parseInt(match[1], 16);
  const luminance = (0.299 * ((value >> 16) & 255) + 0.587 * ((value >> 8) & 255) + 0.114 * (value & 255)) / 255;
  return luminance > 0.6 ? "#111" : "#fff";
};

const temperatureRange = (slot) => {
  const low = asNumber(slot.min_temp);
  const high = asNumber(slot.max_temp);
  if (low === null && high === null) return "—";
  if (low === null || high === null) return formatTemperature(low ?? high);
  return `${low.toFixed(0)}–${high.toFixed(0)} °C`;
};

/**
 * The popup that shows what is loaded in a multi-material unit, and drives it.
 *
 * It is a native `<dialog>` opened with `showModal()`, so it sits in the browser's
 * top layer: no dashboard column, card or scrolling view can clip it, and Escape
 * and the backdrop close it. It draws each unit the way the unit stands in front
 * of the user, with every slot as a spool in its filament's colour.
 *
 * What it offers follows the capabilities: a printer that can only report its
 * slots gets the picture and the details, and one that can load, unload and
 * record a slot's filament gets the buttons for that too. It is updated in place
 * on every reading, and its spools are kept by slot rather than rebuilt, so the
 * keyboard focus and a form being filled in survive a poll.
 */
class FilamentDialog {
  constructor(view) {
    this.view = view;
    this.selected = null;
    this.editing = false;
    this.spoolNodes = new Map();
    this.unitNodes = new Map();
    this.node = this._build();
  }

  get card() {
    return this.view.card;
  }

  get system() {
    return this.view.snapshot.filament || null;
  }

  get isOpen() {
    return Boolean(this.node.open) || this.node.hasAttribute("open");
  }

  can(capability) {
    return this.view.capabilities.includes(capability);
  }

  /** Every slot of every unit, in order. */
  slots() {
    const system = this.system;
    if (!system) return [];
    return (system.units || []).flatMap((unit) => unit.slots || []);
  }

  slot(key) {
    return key ? this.slots().find((item) => slotKey(item.unit, item.slot) === key) || null : null;
  }

  // -------------------------------------------------------------- build

  _build() {
    const dialog = el("dialog", "filament-dialog");
    dialog.setAttribute("aria-label", "Filament");
    const body = el("div", "fd-body");

    const head = el("div", "fd-head");
    const titles = el("div", "fd-titles");
    this.subtitleEl = el("div", "fd-subtitle trunc");
    titles.append(el("div", "fd-title", "Filament"), this.subtitleEl);
    const close = button("icon-btn fd-close", "Close", "close");
    close.addEventListener("click", () => this.close());
    head.append(icon("nozzle", 22), titles, close);

    this.activityEl = el("div", "fd-activity");

    const main = el("div", "fd-main");
    this.unitsEl = el("div", "fd-units");
    const side = el("div", "fd-side");
    this.detailEl = this._buildDetail();
    this.form = this._buildForm();
    side.append(this.detailEl, this.form);
    main.append(this.unitsEl, side);

    this.footEl = el("label", "fd-foot");
    this.refillInput = el("input", "fd-refill");
    this.refillInput.type = "checkbox";
    this.refillInput.addEventListener("change", () => {
      this.card.send(this.view.entryId, "set_auto_refill", { on: this.refillInput.checked });
    });
    this.refillText = el("span", "fd-refill-text");
    this.footEl.append(icon("refill", 18), el("span", "fd-refill-label", "Auto-refill"), this.refillText, this.refillInput);

    body.append(head, this.activityEl, main, this.footEl);
    dialog.appendChild(body);
    // A click on the backdrop lands on the dialog itself; one on its content does not.
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) this.close();
    });
    dialog.addEventListener("close", () => {
      this.editing = false;
      this.update();
    });
    return dialog;
  }

  _buildDetail() {
    const detail = el("div", "fd-detail");
    const head = el("div", "fd-detail-head");
    this.detailSwatch = el("span", "fd-swatch");
    this.detailName = el("div", "fd-detail-name trunc");
    this.detailBadge = el("span", "fd-badge");
    head.append(this.detailSwatch, this.detailName, this.detailBadge);
    this.detailRows = el("dl", "fd-rows");

    this.actions = el("div", "fd-actions");
    this.loadButton = button("ctl primary fd-load", "Load", "load", "load_filament");
    this.loadButton.addEventListener("click", () => this._load());
    this.unloadButton = button("ctl fd-unload", "Unload", "unload", "unload_filament");
    this.unloadButton.addEventListener("click", () => this._unload());
    this.editButton = button("ctl fd-edit", "Edit", "edit", "set_filament");
    this.editButton.addEventListener("click", () => this._startEdit());
    this.actions.append(this.loadButton, this.unloadButton, this.editButton);

    this.noteEl = el("div", "hint fd-note");
    detail.append(head, this.detailRows, this.actions, this.noteEl);
    return detail;
  }

  _buildForm() {
    const form = el("form", "fd-form");
    form.hidden = true;
    form.noValidate = true;
    this.formTitle = el("div", "fd-form-title");

    const field = (label, control) => {
      const wrap = el("label", "fd-field");
      wrap.append(el("span", "fd-field-label", label), control);
      return wrap;
    };

    this.brandSelect = el("select", "fd-input fd-brand");
    this.brandSelect.addEventListener("change", () => this._fillMaterials(this.materialSelect.value));
    this.materialSelect = el("select", "fd-input fd-material");
    this.materialSelect.addEventListener("change", () => this._applyPreset());
    this.materialText = el("input", "fd-input fd-material-text");
    this.materialText.type = "text";
    this.materialText.maxLength = 40;
    this.materialText.placeholder = "PLA";
    this.nameText = el("input", "fd-input fd-name-text");
    this.nameText.type = "text";
    this.nameText.maxLength = 40;
    this.nameText.placeholder = "PLA Matte";

    this.colorInput = el("input", "fd-color");
    this.colorInput.type = "color";
    this.colorInput.setAttribute("aria-label", "Colour");
    const swatches = el("div", "fd-swatches");
    for (const color of FILAMENT_SWATCHES) {
      const swatch = button("fd-swatch-btn", color, null);
      swatch.style.background = color;
      swatch.dataset.color = color;
      swatch.addEventListener("click", () => {
        this.colorInput.value = color.toLowerCase();
      });
      swatches.appendChild(swatch);
    }
    const colorRow = el("div", "fd-color-row");
    colorRow.append(this.colorInput, swatches);

    const temperature = (label) => {
      const input = el("input", "fd-input fd-temp");
      input.type = "number";
      input.min = "0";
      input.max = String(TEMPERATURE_MAX);
      input.step = "1";
      input.inputMode = "numeric";
      input.setAttribute("aria-label", label);
      return input;
    };
    this.minInput = temperature("Lowest nozzle temperature");
    this.maxInput = temperature("Highest nozzle temperature");
    const temps = el("div", "fd-temps");
    temps.append(this.minInput, el("span", "fd-temp-sep", "–"), this.maxInput, el("span", "fd-temp-unit", "°C"));

    this.brandField = field("Brand", this.brandSelect);
    this.materialField = field("Filament", this.materialSelect);
    this.materialTextField = field("Material", this.materialText);
    this.nameTextField = field("Name", this.nameText);

    const buttons = el("div", "fd-actions");
    this.saveButton = button("ctl primary fd-save", "Save", null, "set_filament");
    this.saveButton.type = "submit";
    const cancel = button("ctl fd-cancel", "Cancel", null);
    cancel.addEventListener("click", () => {
      this.editing = false;
      this.update();
    });
    buttons.append(this.saveButton, cancel);

    form.append(
      this.formTitle,
      this.brandField,
      this.materialField,
      this.materialTextField,
      this.nameTextField,
      field("Colour", colorRow),
      field("Nozzle temperature", temps),
      buttons,
    );
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      this._save();
    });
    return form;
  }

  // ----------------------------------------------------------- actions

  open(key) {
    const slots = this.slots();
    if (!slots.length) return;
    const chosen =
      this.slot(key) ||
      slots.find((item) => item.active) ||
      slots.find((item) => item.loaded) ||
      slots[0];
    this.selected = slotKey(chosen.unit, chosen.slot);
    this.editing = false;
    this.update();
    if (this.isOpen) return;
    if (typeof this.node.showModal === "function") {
      try {
        this.node.showModal();
        return;
      } catch {
        // A dialog that cannot be modal is still shown, just without a backdrop.
      }
    }
    this.node.setAttribute("open", "");
  }

  close() {
    if (typeof this.node.close === "function" && this.node.open) {
      this.node.close();
    } else if (this.node.hasAttribute("open")) {
      this.node.removeAttribute("open");
      this.editing = false;
      this.update();
    }
  }

  select(key) {
    if (this.editing) return;
    this.selected = key;
    this.update();
  }

  _load() {
    const slot = this.slot(this.selected);
    if (!slot) return;
    const question = `Load slot ${slotLabel(slot.unit, slot.slot)}, ${slotTitle(slot)}? The printer heats the nozzle, cuts the filament that is in it and feeds this one.`;
    if (!this.card.confirm(question)) return;
    this.card.send(this.view.entryId, "load_filament", { unit: slot.unit, slot: slot.slot });
  }

  _unload() {
    const slot = this.slot(this.selected);
    if (!slot) return;
    const question = `Unload ${slotTitle(slot)} from slot ${slotLabel(slot.unit, slot.slot)}? The printer heats the nozzle and pulls the filament back into the unit.`;
    if (!this.card.confirm(question)) return;
    this.card.send(this.view.entryId, "unload_filament", { unit: slot.unit, slot: slot.slot });
  }

  presets() {
    const presets = this.view.description.filament_presets;
    return Array.isArray(presets) ? presets.filter((item) => item && item.name && item.material) : [];
  }

  _startEdit() {
    const slot = this.slot(this.selected);
    if (!slot) return;
    this.editing = true;
    const presets = this.presets();
    const brands = [...new Set(presets.flatMap((item) => item.brands || []))];
    const brandList = brands.length ? brands : DEFAULT_BRANDS;
    this.brandSelect.replaceChildren(...brandList.map((brand) => new Option(brand, brand)));
    const brand = brandList.find((item) => item.toLowerCase() === String(slot.brand || "").toLowerCase());
    this.brandSelect.value = brand || brandList[brandList.length - 1];

    const listed = presets.length > 0;
    this.materialField.hidden = !listed;
    this.materialTextField.hidden = listed;
    this.nameTextField.hidden = listed;
    if (listed) {
      this._fillMaterials(slot.name || slot.material);
    } else {
      this.materialText.value = slot.material || "";
      this.nameText.value = slot.name && slot.name !== slot.material ? slot.name : "";
    }
    this.colorInput.value = (/^#[0-9a-f]{6}$/i.test(slot.color || "") ? slot.color : "#ffffff").toLowerCase();
    this.minInput.value = asNumber(slot.min_temp) !== null ? String(Math.round(slot.min_temp)) : "";
    this.maxInput.value = asNumber(slot.max_temp) !== null ? String(Math.round(slot.max_temp)) : "";
    if (listed && (!this.minInput.value || !this.maxInput.value)) this._applyPreset();
    this.formTitle.textContent = `Slot ${slotLabel(slot.unit, slot.slot)}`;
    this.update();
    this.materialSelect.focus?.();
  }

  /** List the brand's filaments, grouped by material, keeping `wanted` selected if offered. */
  _fillMaterials(wanted) {
    const brand = this.brandSelect.value;
    const offered = this.presets().filter((item) => !item.brands || !item.brands.length || item.brands.includes(brand));
    const groups = new Map();
    for (const preset of offered) {
      if (!groups.has(preset.material)) groups.set(preset.material, []);
      groups.get(preset.material).push(preset);
    }
    const nodes = [];
    for (const [material, items] of groups) {
      const group = document.createElement("optgroup");
      group.label = material;
      for (const preset of items) group.appendChild(new Option(preset.name, preset.name));
      nodes.push(group);
    }
    this.materialSelect.replaceChildren(...nodes);
    const match = offered.find((item) => item.name.toLowerCase() === String(wanted || "").toLowerCase());
    if (match) this.materialSelect.value = match.name;
  }

  _applyPreset() {
    const preset = this.presets().find((item) => item.name === this.materialSelect.value);
    if (!preset) return;
    this.minInput.value = String(preset.min_temp);
    this.maxInput.value = String(preset.max_temp);
  }

  async _save() {
    const slot = this.slot(this.selected);
    if (!slot) return;
    const data = {
      unit: slot.unit,
      slot: slot.slot,
      brand: this.brandSelect.value,
      color: this.colorInput.value.toUpperCase(),
    };
    if (!this.materialField.hidden) {
      const preset = this.presets().find((item) => item.name === this.materialSelect.value);
      if (!preset) return;
      data.material = preset.material;
      data.name = preset.name;
    } else {
      const material = this.materialText.value.trim();
      if (!material) {
        this.materialText.focus?.();
        return;
      }
      data.material = material;
      if (this.nameText.value.trim()) data.name = this.nameText.value.trim();
    }
    const low = asNumber(this.minInput.value);
    const high = asNumber(this.maxInput.value);
    if (low !== null) data.min_temp = low;
    if (high !== null) data.max_temp = high;
    if (low !== null && high !== null && low > high) {
      this.card._notify("The lowest nozzle temperature is above the highest.");
      return;
    }
    const sent = await this.card.send(this.view.entryId, "set_filament", data);
    if (sent) {
      this.editing = false;
      this.update();
    }
  }

  // ------------------------------------------------------------ update

  update() {
    const system = this.system;
    const slots = this.slots();
    if (!system || !slots.length) {
      if (this.isOpen) this.close();
      return;
    }
    if (!this.slot(this.selected)) {
      const fallback = slots.find((item) => item.active) || slots[0];
      this.selected = slotKey(fallback.unit, fallback.slot);
    }
    this.subtitleEl.textContent = this.view.nameEl.textContent;
    this.activityEl.textContent = system.activity || "";
    this.activityEl.hidden = !system.activity;
    this._paintUnits(system);
    this._paintDetail();
    this._paintRefill(system);
    this.detailEl.hidden = this.editing;
    this.form.hidden = !this.editing;
    if (this.editing) this.saveButton.disabled = !this.view.online() || this.card.isBusy(this.view.entryId, "set_filament");
  }

  _paintUnits(system) {
    const units = system.units || [];
    const seenSpools = new Set();
    const seenUnits = new Set();
    const sections = [];
    for (const unit of units) {
      // Units and spools are kept, not rebuilt: moving a focused spool would drop
      // the keyboard focus on every reading.
      let parts = this.unitNodes.get(unit.unit);
      if (!parts) {
        const section = el("div", "fd-unit");
        const head = el("div", "fd-unit-head");
        const grid = el("div", "fd-spools");
        section.append(head, grid);
        parts = { section, head, grid };
        this.unitNodes.set(unit.unit, parts);
      }
      seenUnits.add(unit.unit);
      parts.section.classList.toggle("disconnected", !unit.connected);
      const name = unit.name || "Unit";
      const heading = units.length > 1 ? `${name} ${unit.unit + 1}` : name;
      parts.head.textContent = unit.connected ? heading : `${heading} · disconnected`;
      const slots = unit.slots || [];
      const ordered = slots.length === 4 ? FOUR_SLOT_ORDER.map((index) => slots[index]) : slots;
      const spools = ordered.map((slot) => {
        const key = slotKey(slot.unit, slot.slot);
        seenSpools.add(key);
        return this._spool(key, slot);
      });
      replaceChildrenIfChanged(parts.grid, spools);
      sections.push(parts.section);
    }
    for (const key of [...this.spoolNodes.keys()]) if (!seenSpools.has(key)) this.spoolNodes.delete(key);
    for (const key of [...this.unitNodes.keys()]) if (!seenUnits.has(key)) this.unitNodes.delete(key);
    replaceChildrenIfChanged(this.unitsEl, sections);
  }

  _spool(key, slot) {
    let node = this.spoolNodes.get(key);
    if (!node) {
      node = el("button", "spool");
      node.type = "button";
      node.dataset.key = key;
      node.addEventListener("click", () => this.select(key));
      const number = el("span", "spool-num");
      const disc = el("span", "spool-disc");
      const hub = el("span", "spool-hub");
      const ink = el("span", "spool-ink");
      disc.append(hub, ink);
      const name = el("span", "spool-name trunc");
      const badge = el("span", "spool-badge", "In use");
      node.append(number, disc, name, badge);
      node._parts = { number, disc, ink, name, badge };
      this.spoolNodes.set(key, node);
    }
    const { number, disc, ink, name, badge } = node._parts;
    const color = slot.loaded && slot.color ? slot.color : null;
    number.textContent = slotLabel(slot.unit, slot.slot);
    disc.style.background = color || "transparent";
    disc.classList.toggle("empty", !slot.loaded);
    ink.textContent = slot.loaded ? slot.material || "?" : "/";
    ink.style.color = color ? inkFor(color) : "var(--secondary-text-color)";
    name.textContent = slot.loaded ? slotTitle(slot) : "Empty";
    badge.hidden = !slot.active;
    node.classList.toggle("active", Boolean(slot.active));
    node.classList.toggle("loaded", Boolean(slot.loaded));
    const selected = key === this.selected;
    node.classList.toggle("selected", selected);
    node.setAttribute("aria-pressed", String(selected));
    const state = slot.active ? "in use" : slot.loaded ? "loaded" : "empty";
    node.setAttribute("aria-label", `Slot ${slotLabel(slot.unit, slot.slot)}: ${slot.loaded ? slotTitle(slot) : "empty"}, ${state}`);
    node.disabled = this.editing && !selected;
    return node;
  }

  _paintDetail() {
    const slot = this.slot(this.selected);
    if (!slot) return;
    const color = slot.color || null;
    this.detailSwatch.style.background = color || "transparent";
    this.detailSwatch.classList.toggle("empty", !color);
    this.detailName.textContent = `${slotLabel(slot.unit, slot.slot)} · ${slot.loaded ? slotTitle(slot) : "Empty"}`;
    const state = slot.active ? "In use" : slot.loaded ? "Loaded" : "Empty";
    this.detailBadge.textContent = state;
    this.detailBadge.className = `fd-badge ${state.toLowerCase().replace(" ", "-")}`;

    // An empty slot keeps the filament it last held on record, so it is shown as that.
    const rows = [
      ["Material", slot.material || "—"],
      ["Filament", slot.name || "—"],
      ["Brand", slot.brand || "—"],
      ["Colour", slot.color || "—"],
      ["Nozzle", temperatureRange(slot)],
    ];
    this.detailRows.replaceChildren();
    for (const [label, value] of rows) {
      const term = el("dt", "", slot.loaded ? label : `${label} (last)`);
      const detail = el("dd", "trunc", value);
      this.detailRows.append(term, detail);
    }

    const online = this.view.online();
    const refused = (command) => Boolean(this.view.blockedReason(command));
    const busy = (command) => this.card.isBusy(this.view.entryId, command);
    const canLoad = this.can("load_filament");
    const canUnload = this.can("unload_filament");
    const canEdit = this.can("set_filament");
    this.loadButton.hidden = !canLoad;
    this.loadButton.disabled = !online || refused("load_filament") || !slot.loaded || slot.active || busy("load_filament");
    this.unloadButton.hidden = !canUnload;
    this.unloadButton.disabled = !online || refused("unload_filament") || !slot.active || busy("unload_filament");
    this.editButton.hidden = !canEdit;
    this.editButton.disabled = !online || refused("set_filament") || busy("set_filament");
    this.actions.hidden = !canLoad && !canUnload && !canEdit;

    let note = "";
    const slotReason = this.view.firstBlocked(["load_filament", "unload_filament", "set_filament"]);
    if (!canLoad && !canUnload && !canEdit) {
      note = "This printer reports what is in each slot. Loading, unloading and changing a slot are done on its own screen.";
    } else if (!online) {
      note = "The printer is not answering.";
    } else if (slotReason) {
      note = `The slots cannot be changed now: ${slotReason}.`;
    } else if (!slot.loaded && canLoad) {
      note = "Put a spool in this slot to load it.";
    } else {
      note = "Select a slot, then load or unload it.";
    }
    this.noteEl.textContent = note;
  }

  _paintRefill(system) {
    const known = system.auto_refill === true || system.auto_refill === false;
    this.footEl.hidden = !known;
    if (!known) return;
    const settable = this.can("set_auto_refill");
    this.refillInput.hidden = !settable;
    this.refillText.hidden = settable;
    this.refillText.textContent = system.auto_refill ? "On" : "Off";
    if (settable && this.card.shadowRoot?.activeElement !== this.refillInput) {
      this.refillInput.checked = Boolean(system.auto_refill);
    }
    this.refillInput.disabled =
      !this.view.online() ||
      this.card.isBusy(this.view.entryId, "set_auto_refill") ||
      Boolean(this.view.blockedReason("set_auto_refill"));
  }
}

// ------------------------------------------------------------------- views

/**
 * The DOM of one printer, built once and then updated in place.
 *
 * Interactive elements (inputs, sliders, buttons) are created once and have their
 * state patched. Read-only fragments (stats, temperature tiles) are rebuilt on
 * every update, because they hold nothing the user could lose.
 */
class PrinterView {
  constructor(card, entryId, compact) {
    this.card = card;
    this.entryId = entryId;
    this.compact = compact;
    this.tab = "status";
    this.description = {};
    this.snapshot = {};
    this.capabilities = [];
    this.files = null;
    this.filesError = null;
    this.filesLoading = false;
    this.uploading = null;
    const steps = card.jogSteps();
    this.jogStep = steps.includes(10) ? 10 : steps[Math.min(1, steps.length - 1)];
    this.camera = { src: null, mode: null, signedAt: 0, failedAt: 0, degradedAt: 0, suspended: false };
    this.root = this._build();
  }

  // ---------------------------------------------------------------- build

  _build() {
    const root = el("div", "printer");
    root.dataset.entryId = this.entryId;

    const header = el("div", "printer-header");
    this.dot = el("span", "dot");
    header.appendChild(this.dot);
    const names = el("div", "names");
    this.nameEl = el("div", "name trunc");
    this.subtitleEl = el("div", "subtitle trunc");
    names.append(this.nameEl, this.subtitleEl);
    header.appendChild(names);
    this.stateEl = el("div", "state");
    header.appendChild(this.stateEl);

    this.filamentButton = button("icon-btn filament-toggle", "Filament", "nozzle");
    this.filamentButton.addEventListener("click", () => this.filament.open());
    header.appendChild(this.filamentButton);

    this.lightButton = button("icon-btn light-toggle", "Light", "light", "set_light");
    this.lightButton.addEventListener("click", () => {
      const on = (this.snapshot.lights || []).includes("chamber");
      this.card.send(this.entryId, "set_light", { on: !on, channel: "chamber" });
    });
    this.powerButton = button("icon-btn power-toggle", "Power", "power");
    this.powerButton.addEventListener("click", () => this.card.togglePower(this));
    header.append(this.lightButton, this.powerButton);
    root.appendChild(header);

    this.warningEl = el("div", "warning");
    this.errorsEl = el("div", "errors");
    root.append(this.warningEl, this.errorsEl);

    this.body = el("div", "body");
    this.cameraColumn = this._buildCamera();
    this.body.appendChild(this.cameraColumn);

    const main = el("div", "main");
    if (!this.compact) {
      this.tabBar = el("div", "tabs");
      this.tabButtons = {};
      for (const [key, label] of [
        ["status", "Status"],
        ["controls", "Controls"],
        ["files", "Files"],
      ]) {
        const tab = el("button", "tab trunc", label);
        tab.type = "button";
        tab.dataset.tab = key;
        tab.addEventListener("click", () => this.selectTab(key));
        this.tabButtons[key] = tab;
        this.tabBar.appendChild(tab);
      }
      main.appendChild(this.tabBar);
    }
    this.panels = {
      status: this._buildStatus(),
      controls: this.compact ? null : this._buildControls(),
      files: this.compact ? null : this._buildFiles(),
    };
    for (const panel of Object.values(this.panels)) if (panel) main.appendChild(panel);
    this.body.appendChild(main);
    root.appendChild(this.body);

    this.footer = el("div", "footer");
    root.appendChild(this.footer);
    this.filament = new FilamentDialog(this);
    root.appendChild(this.filament.node);
    this.selectTab("status");
    return root;
  }

  _buildCamera() {
    const column = el("div", "camera");
    this.cameraFrame = el("div", "camera-frame");
    this.cameraPlaceholder = el("div", "camera-error", "Camera unavailable");
    this.cameraOverlay = el("div", "camera-overlay");
    this.fullscreenButton = button("icon-btn camera-fullscreen", "Full screen", "fullscreen");
    this.fullscreenButton.addEventListener("click", () => {
      const target = this.image || this.cameraFrame;
      if (target.requestFullscreen) target.requestFullscreen().catch(() => {});
    });
    this.cameraFrame.append(this.cameraPlaceholder, this.cameraOverlay, this.fullscreenButton);
    column.appendChild(this.cameraFrame);
    return column;
  }

  _buildStatus() {
    const panel = el("div", "panel panel-status");
    this.progressBlock = el("div", "progress-block");
    const track = el("div", "progress-track");
    this.progressFill = el("div", "progress-fill");
    track.appendChild(this.progressFill);
    this.progressLabel = el("div", "progress-label");
    this.progressBlock.append(track, this.progressLabel);
    this.statsEl = el("div", "stats");
    this.jobEl = el("div", "job");
    this.tempsEl = el("div", "temps");
    this.filamentStrip = el("button", "filament-strip");
    this.filamentStrip.type = "button";
    this.filamentStrip.addEventListener("click", () => this.filament.open());
    this.fansReadout = el("div", "fans-readout");

    this.jobControls = el("div", "controls");
    this.pauseButton = button("ctl primary", "Pause", "pause", "pause");
    this.pauseButton.addEventListener("click", () => this.card.send(this.entryId, "pause"));
    this.resumeButton = button("ctl primary", "Resume", "play", "resume");
    this.resumeButton.addEventListener("click", () => this.card.send(this.entryId, "resume"));
    this.stopButton = button("ctl danger", "Stop", "stop", "stop");
    this.stopButton.addEventListener("click", () => {
      if (!this.card.confirm("Stop the print? It cannot be resumed afterwards.")) return;
      this.card.send(this.entryId, "stop");
    });
    this.jobControls.append(this.pauseButton, this.resumeButton, this.stopButton);

    panel.append(
      this.progressBlock,
      this.statsEl,
      this.jobEl,
      this.tempsEl,
      this.filamentStrip,
      this.fansReadout,
      this.jobControls,
    );
    return panel;
  }

  _buildControls() {
    const panel = el("div", "panel panel-controls");

    // Heaters.
    this.heaterSection = el("div", "section heaters");
    this.heaterSection.appendChild(el("div", "section-title", "Temperatures"));
    this.heaterRows = {};
    for (const heater of HEATERS) {
      const row = el("div", "heater");
      row.dataset.heater = heater.key;
      const label = el("div", "heater-label");
      label.append(icon("thermometer", 16), el("span", "", heater.label));
      const current = el("div", "heater-current");
      const input = el("input", "heater-input");
      input.type = "number";
      input.min = "0";
      input.max = String(TEMPERATURE_MAX);
      input.step = "1";
      input.inputMode = "numeric";
      input.setAttribute("aria-label", `${heater.label} target`);
      input.addEventListener("input", () => {
        input.dataset.dirty = "1";
      });
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") apply();
      });
      const nudge = (delta) => {
        const value = asNumber(input.value) ?? asNumber(this.snapshot[heater.key]?.target) ?? 0;
        input.value = String(Math.max(0, Math.min(TEMPERATURE_MAX, value + delta)));
        input.dataset.dirty = "1";
      };
      const minus = button("mini", "−5", null);
      minus.addEventListener("click", () => nudge(-TEMPERATURE_NUDGE));
      const plus = button("mini", "+5", null);
      plus.addEventListener("click", () => nudge(TEMPERATURE_NUDGE));
      const set = button("mini accent heater-set", "Set", null, heater.command);
      const apply = () => {
        const value = asNumber(input.value);
        if (value === null) return;
        this.card
          .send(this.entryId, heater.command, { value })
          .finally(() => delete input.dataset.dirty);
      };
      set.addEventListener("click", apply);
      const off = button("mini heater-off", "Off", null);
      off.addEventListener("click", () => {
        input.value = "0";
        apply();
      });
      const controls = el("div", "heater-controls");
      controls.append(minus, input, plus, set, off);
      const name = el("div", "heater-name");
      name.append(label, current);
      row.append(name, controls);
      this.heaterRows[heater.key] = { row, current, input, set };
      this.heaterSection.appendChild(row);
    }
    this.presetRow = el("div", "presets");
    for (const preset of this.card.presets()) {
      const chip = button("chip preset", preset.name, null);
      chip.title = `${preset.name}: nozzle ${preset.hotend} °C, bed ${preset.bed} °C`;
      chip.addEventListener("click", () => this.applyPreset(preset));
      this.presetRow.appendChild(chip);
    }
    const cool = button("chip cooldown", "Cool down", null);
    cool.addEventListener("click", () => this.applyPreset({ name: "Cool down", hotend: 0, bed: 0, chamber: 0 }));
    this.presetRow.appendChild(cool);
    this.heaterSection.appendChild(this.presetRow);
    this.heaterHint = el("div", "hint heater-hint");
    this.heaterSection.appendChild(this.heaterHint);
    panel.appendChild(this.heaterSection);

    // Fans and speed.
    this.fanSection = el("div", "section fans");
    this.fanSection.appendChild(el("div", "section-title", "Fans and speed"));
    this.fanSliders = {};
    for (const fan of SETTABLE_FANS) {
      const slider = this._slider(fan.label, "fan", 0, 100, 1, (value) =>
        this.card.send(this.entryId, "set_fan_speed", { value, channel: fan.key }),
      );
      slider.row.dataset.fan = fan.key;
      this.fanSliders[fan.key] = slider;
      this.fanSection.appendChild(slider.row);
    }
    this.speedSlider = this._slider("Print speed", "speedometer", 10, 100, 5, (value) =>
      this.card.send(this.entryId, "set_speed", { value }),
    );
    this.speedSlider.row.classList.add("speed");
    this.fanSection.appendChild(this.speedSlider.row);
    this.reportedFans = el("div", "reported-fans");
    this.fanSection.appendChild(this.reportedFans);
    this.fanHint = el("div", "hint fan-hint");
    this.fanSection.appendChild(this.fanHint);
    panel.appendChild(this.fanSection);

    // Motion.
    this.motionSection = el("div", "section motion");
    this.motionSection.appendChild(el("div", "section-title", "Motion"));
    const motion = el("div", "motion-body");

    this.joystick = el("div", "joystick");
    this.joystick.tabIndex = 0;
    this.joystick.setAttribute("role", "group");
    this.joystick.setAttribute(
      "aria-label",
      "Move the head. Arrow keys move X and Y, Page Up and Page Down move Z.",
    );
    const jog = (axis, direction, iconName, className) => {
      const node = button(`jog ${className}`, `${axis}${direction > 0 ? "+" : "−"}`, iconName, "jog");
      node.dataset.axis = axis;
      node.dataset.direction = String(direction);
      node.addEventListener("click", () => this.jog(axis, direction));
      return node;
    };
    this.jogButtons = [
      jog("Y", 1, "up", "y-plus"),
      jog("X", -1, "left", "x-minus"),
      jog("X", 1, "right", "x-plus"),
      jog("Y", -1, "down", "y-minus"),
    ];
    this.homeXY = button("jog home-center", "Home XY", "home", "home");
    this.homeXY.dataset.axes = "XY";
    this.homeXY.addEventListener("click", () => this.home("XY"));
    this.joystick.append(...this.jogButtons, this.homeXY);
    this.joystick.addEventListener("keydown", (event) => {
      const keys = {
        ArrowUp: ["Y", 1],
        ArrowDown: ["Y", -1],
        ArrowLeft: ["X", -1],
        ArrowRight: ["X", 1],
        PageUp: ["Z", 1],
        PageDown: ["Z", -1],
      };
      const move = keys[event.key];
      if (!move) return;
      event.preventDefault();
      this.jog(move[0], move[1]);
    });

    this.zColumn = el("div", "z-column");
    const zUp = jog("Z", 1, "up", "z-plus");
    const zDown = jog("Z", -1, "down", "z-minus");
    this.homeZ = button("jog home-z", "Home Z", "home", "home");
    this.homeZ.dataset.axes = "Z";
    this.homeZ.addEventListener("click", () => this.home("Z"));
    this.zColumn.append(zUp, this.homeZ, zDown);
    this.jogButtons.push(zUp, zDown);

    const pad = el("div", "pad");
    pad.append(this.joystick, this.zColumn);
    motion.appendChild(pad);

    const side = el("div", "motion-side");
    this.stepChips = el("div", "steps");
    this.stepChips.appendChild(el("span", "steps-label", "Step (mm)"));
    for (const step of this.card.jogSteps()) {
      const chip = button("chip step", formatStep(step), null);
      chip.dataset.step = String(step);
      chip.addEventListener("click", () => {
        this.jogStep = step;
        this._paintSteps();
      });
      this.stepChips.appendChild(chip);
    }
    this.homeAll = button("ctl home-all", "Home all", "home", "home");
    this.homeAll.dataset.axes = "XYZ";
    this.homeAll.addEventListener("click", () => this.home("XYZ"));
    this.positionEl = el("div", "position");
    this.motionHint = el("div", "hint motion-hint");
    side.append(this.stepChips, this.homeAll, this.positionEl, this.motionHint);
    motion.appendChild(side);
    this.motionSection.appendChild(motion);
    panel.appendChild(this.motionSection);

    this.noControls = el("div", "empty-panel", "This printer reports no settings it can be given.");
    panel.appendChild(this.noControls);
    this._paintSteps();
    return panel;
  }

  _slider(label, iconName, min, max, step, onChange) {
    const row = el("div", "slider-row");
    const name = el("div", "slider-label");
    name.append(icon(iconName in ICONS ? iconName : "fan", 16), el("span", "", label));
    const input = el("input", "slider");
    input.type = "range";
    input.min = String(min);
    input.max = String(max);
    input.step = String(step);
    input.setAttribute("aria-label", label);
    const value = el("div", "slider-value");
    input.addEventListener("input", () => {
      input.dataset.dirty = "1";
      value.textContent = `${input.value}%`;
    });
    input.addEventListener("change", () => {
      const number = asNumber(input.value);
      if (number === null) return;
      Promise.resolve(onChange(number)).finally(() => delete input.dataset.dirty);
    });
    row.append(name, input, value);
    return { row, input, value };
  }

  _buildFiles() {
    const panel = el("div", "panel panel-files");
    const toolbar = el("div", "files-toolbar");
    this.refreshFiles = button("icon-btn files-refresh", "Refresh", "refresh");
    this.refreshFiles.addEventListener("click", () => this.loadFiles());
    this.uploadInput = el("input", "upload-input");
    this.uploadInput.type = "file";
    this.uploadInput.accept = ".gcode,.gco,.g,.bgcode";
    this.uploadInput.hidden = true;
    this.uploadInput.addEventListener("change", () => {
      const file = this.uploadInput.files && this.uploadInput.files[0];
      this.uploadInput.value = "";
      if (file) this.upload(file);
    });
    this.uploadButton = button("ctl upload", "Upload", "upload");
    this.uploadButton.addEventListener("click", () => this.uploadInput.click());
    this.printAfterUpload = el("label", "print-after");
    this.printAfterInput = el("input");
    this.printAfterInput.type = "checkbox";
    this.printAfterUpload.append(this.printAfterInput, el("span", "", "Print when uploaded"));
    toolbar.append(this.uploadButton, this.uploadInput, this.printAfterUpload, el("span", "spacer"), this.refreshFiles);
    this.filesHint = el("div", "hint files-hint");
    this.filesStatus = el("div", "files-status");
    this.fileList = el("div", "file-list");
    panel.append(toolbar, this.filesHint, this.filesStatus, this.fileList);
    return panel;
  }

  // -------------------------------------------------------------- actions

  selectTab(key) {
    this.tab = key;
    for (const [name, panel] of Object.entries(this.panels)) {
      if (panel) panel.hidden = name !== key;
    }
    if (this.tabButtons) {
      for (const [name, tab] of Object.entries(this.tabButtons)) {
        tab.classList.toggle("active", name === key);
        tab.setAttribute("aria-selected", String(name === key));
      }
    }
    if (key === "files" && this.files === null && !this.filesLoading) this.loadFiles();
  }

  jog(axis, direction) {
    const available = this.capabilities.includes("jog") && this.online() && !this.blockedReason("jog");
    if (!available) return;
    this.card.send(this.entryId, "jog", { axis, distance: direction * this.jogStep });
  }

  home(axes) {
    if (!this.capabilities.includes("home") || !this.online() || this.blockedReason("home")) return;
    this.card.send(this.entryId, "home", { axes });
  }

  /** Why the printer refuses `command` now, or null. The backend owns these rules. */
  blockedReason(command) {
    return (this.snapshot.blocked || {})[command] || null;
  }

  /** Return the first reason among `commands` the printer refuses now, or "". */
  firstBlocked(commands) {
    for (const command of commands) {
      const reason = this.blockedReason(command);
      if (reason) return reason;
    }
    return "";
  }

  async applyPreset(preset) {
    const sends = [];
    for (const heater of HEATERS) {
      const value = preset[heater.key];
      if (value === undefined || value === null) continue;
      if (!this.capabilities.includes(heater.command)) continue;
      sends.push(this.card.send(this.entryId, heater.command, { value }, { refresh: false }));
    }
    await Promise.all(sends);
    await this.card.refreshOne(this.entryId);
  }

  async loadFiles() {
    if (!this.capabilities.includes("file_list")) return;
    this.filesLoading = true;
    this.filesError = null;
    this._paintFiles();
    try {
      this.files = await this.card.listFiles(this.entryId);
    } catch (err) {
      this.filesError = err.message || String(err);
      this.files = this.files || [];
    } finally {
      this.filesLoading = false;
      this._paintFiles();
    }
  }

  async upload(file) {
    this.uploading = file.name;
    this._paintFiles();
    try {
      const stored = await this.card.upload(this.entryId, file);
      if (this.printAfterInput.checked && this.capabilities.includes("start_print") && stored) {
        await this.card.send(this.entryId, "start_print", { filename: stored.path || stored.name });
      }
    } finally {
      this.uploading = null;
      await this.loadFiles();
    }
  }

  printFile(file) {
    if (!this.card.confirm(`Start printing ${baseName(file.name)}? Make sure the bed is clear.`)) return;
    this.card.send(this.entryId, "start_print", { filename: file.path || file.name });
  }

  deleteFile(file) {
    if (!this.card.confirm(`Delete ${baseName(file.name)} from the printer?`)) return;
    this.card
      .send(this.entryId, "delete_file", { filename: file.path || file.name })
      .then(() => this.loadFiles());
  }

  // --------------------------------------------------------------- update

  online() {
    return this.description.connected === true && !this.poweredOff;
  }

  activeJob() {
    return ACTIVE_STATES.has(this.snapshot.print_state);
  }

  update(printer, description, power) {
    this.printer = printer;
    this.description = description || {};
    this.snapshot = this.description.printer || {};
    this.capabilities = this.snapshot.capabilities || [];
    this.power = power;
    this.poweredOff = Boolean(power && power.state === "off");

    const caps = this.capabilities;
    const snapshot = this.snapshot;
    const state = this.poweredOff ? "off" : snapshot.print_state || "unknown";
    const color = STATE_COLORS[state] || STATE_COLORS.unknown;

    // Header.
    this.dot.style.background = color;
    this.nameEl.textContent = this.description.name || printer.name || "3D printer";
    const subtitle = [
      this.description.model || printer.model || this.description.protocol || printer.protocol,
      this.description.firmware ? `fw ${this.description.firmware}` : null,
      this.description.model_profile && this.description.model_profile.verified === false ? "unverified model" : null,
    ]
      .filter(Boolean)
      .join(" · ");
    this.subtitleEl.textContent = subtitle;
    this.subtitleEl.hidden = !subtitle;
    this.stateEl.textContent = STATE_LABELS[state] || state;
    this.stateEl.style.color = color;

    const lightOn = (snapshot.lights || []).includes("chamber");
    this.lightButton.hidden = !caps.includes("set_light");
    this.lightButton.classList.toggle("on", lightOn);
    this.lightButton.setAttribute("aria-pressed", String(lightOn));
    this.lightButton.disabled =
      !this.online() || this.card.isBusy(this.entryId, "set_light") || Boolean(this.blockedReason("set_light"));

    this.powerButton.hidden = !power;
    if (power) {
      const on = power.state === "on";
      this.powerButton.classList.toggle("on", on);
      this.powerButton.setAttribute("aria-pressed", String(on));
      this.powerButton.disabled = !["on", "off"].includes(power.state) || this.card.powerBusy;
      this.powerButton.title = power.missing
        ? `${power.entity_id} does not exist`
        : `Power (${power.entity_id}): ${power.state}`;
    }

    // Warnings.
    let warning = "";
    if (power && power.missing) warning = `The power entity ${power.entity_id} does not exist.`;
    else if (this.poweredOff) warning = "The printer is switched off.";
    else if (!this.description.connected) {
      warning = this.description.last_error || this.description.error || "The printer is not answering.";
    }
    this.warningEl.textContent = warning;
    this.warningEl.hidden = !warning;
    this.warningEl.classList.toggle("off", this.poweredOff);

    this.errorsEl.replaceChildren();
    const errors = this.poweredOff || !this.description.connected ? [] : snapshot.errors || [];
    for (const error of errors) this.errorsEl.appendChild(el("div", "error-line", error));
    this.errorsEl.hidden = errors.length === 0;

    this._updateCamera();
    this._updateStatus();
    this._updateFilament();
    if (!this.compact) {
      this._updateControls();
      this._updateFilesAvailability();
      this._paintFiles();
      this._updateFooter();
    } else {
      this.footer.hidden = true;
    }
  }

  _updateCamera() {
    const caps = this.capabilities;
    const description = this.description;
    const live = description.camera_url;
    const still = description.snapshot_url;
    const streamed = description.camera_kind === "stream";
    const wanted =
      this.card.showCamera() &&
      (streamed
        ? caps.includes("camera_stream") && Boolean(description.camera_entity_id)
        : caps.includes("camera") && Boolean(live || still));
    this.cameraColumn.hidden = !wanted;
    this.body.classList.toggle("with-camera", wanted);
    if (!wanted) {
      this._suspendCamera();
      return;
    }

    if (this.poweredOff) {
      this._suspendCamera();
      this.cameraPlaceholder.textContent = "Switched off";
      this.cameraPlaceholder.hidden = false;
      this.cameraOverlay.hidden = true;
      return;
    }

    if (streamed) {
      this._showStream(description.camera_entity_id);
      this._paintCameraOverlay();
      return;
    }

    if (!this.image) {
      this.image = el("img");
      this.image.alt = "Printer camera";
      this.image.addEventListener("error", () => this._cameraFailed());
      this.image.addEventListener("load", () => {
        this.cameraPlaceholder.hidden = true;
      });
      this.cameraFrame.insertBefore(this.image, this.cameraFrame.firstChild);
    }

    const camera = this.camera;
    const now = Date.now();
    this.cameraPlaceholder.textContent = "Camera unavailable";
    // A camera that failed outright is left alone for a while rather than hammered.
    if (camera.failedAt && now - camera.failedAt < CAMERA_RETRY_MS) return;

    // The live stream is preferred. A stream that failed falls back to the still for
    // a while and is then tried again, because a camera server that dropped one
    // connection usually takes the next.
    const streamAllowed = Boolean(live) && (!camera.degradedAt || now - camera.degradedAt > CAMERA_RETRY_MS);
    const mode = streamAllowed || !still ? "stream" : "snapshot";
    let src = null;
    if (mode === "stream") {
      // The stream keeps the URL it started with: every poll signs a new one, and
      // changing `src` would tear down the one upstream connection.
      const stale = camera.mode !== "stream" || !camera.src || camera.suspended || camera.failedAt;
      if (stale || now - camera.signedAt > CAMERA_RESIGN_MS) src = live;
    } else {
      // A still only changes when it is fetched again, so it is fetched every poll.
      src = still;
    }
    if (src) {
      camera.mode = mode;
      camera.src = src;
      camera.signedAt = now;
      camera.suspended = false;
      camera.failedAt = 0;
      if (mode === "stream") camera.degradedAt = 0;
      this.image.dataset.mode = mode;
      if (this.image.getAttribute("src") !== src) this.image.src = src;
    }
    this.image.hidden = false;
    this._paintCameraOverlay();
  }

  /** Embed HA's own camera card for a stream camera; built once, then only handed `hass`. */
  _showStream(entityId) {
    if (this.image) this.image.hidden = true;
    const hass = this.card.hass;
    if (this.streamCard && this.streamEntity === entityId) {
      this.streamCard.hass = hass;
      this.streamCard.hidden = false;
      return;
    }
    if (this.streamLoading === entityId) return;
    this.streamLoading = entityId;
    if (this.streamCard) this.streamCard.remove();
    this.streamCard = null;
    const helpers = typeof window.loadCardHelpers === "function" ? window.loadCardHelpers() : null;
    if (!helpers) {
      this.cameraPlaceholder.textContent = "Camera unavailable";
      this.cameraPlaceholder.hidden = false;
      this.streamLoading = null;
      return;
    }
    Promise.resolve(helpers)
      .then((loaded) => {
        const element = loaded.createCardElement({
          type: "picture-entity",
          entity: entityId,
          camera_view: "live",
          show_name: false,
          show_state: false,
        });
        element.classList.add("stream-card");
        element.hass = this.card.hass;
        this.streamCard = element;
        this.streamEntity = entityId;
        this.cameraFrame.insertBefore(element, this.cameraFrame.firstChild);
        this.cameraPlaceholder.hidden = true;
      })
      .catch(() => {
        this.cameraPlaceholder.textContent = "Camera unavailable";
        this.cameraPlaceholder.hidden = false;
      })
      .finally(() => {
        this.streamLoading = null;
      });
  }

  _paintCameraOverlay() {
    const snapshot = this.snapshot;
    this.cameraOverlay.replaceChildren();
    const state = snapshot.print_state || "unknown";
    if (ACTIVE_STATES.has(state) || state === "finished") {
      const chip = el("span", "overlay-chip", STATE_LABELS[state] || state);
      chip.style.background = STATE_COLORS[state];
      this.cameraOverlay.appendChild(chip);
      const percent = asNumber(snapshot.progress);
      if (percent !== null) this.cameraOverlay.appendChild(el("span", "overlay-chip dark", `${percent.toFixed(0)}%`));
    }
    this.cameraOverlay.hidden = this.cameraOverlay.childElementCount === 0;
  }

  _cameraFailed() {
    const camera = this.camera;
    if (camera.suspended || !this.image) return;
    const still = this.description.snapshot_url;
    if (camera.mode === "stream" && still) {
      camera.mode = "snapshot";
      camera.src = still;
      camera.signedAt = Date.now();
      camera.degradedAt = Date.now();
      this.image.dataset.mode = "snapshot";
      this.image.src = still;
      return;
    }
    camera.failedAt = Date.now();
    this.image.hidden = true;
    this.cameraPlaceholder.hidden = false;
  }

  _suspendCamera() {
    if (this.streamCard) this.streamCard.hidden = true;
    if (!this.image || this.camera.suspended) return;
    this.camera.suspended = true;
    this.image.removeAttribute("src");
    this.image.hidden = true;
  }

  _updateStatus() {
    const caps = this.capabilities;
    const snapshot = this.snapshot;
    const state = snapshot.print_state || "unknown";

    const percent = asNumber(snapshot.progress);
    const showProgress = percent !== null && caps.includes("pause") && !this.poweredOff;
    this.progressBlock.hidden = !showProgress;
    if (showProgress) {
      this.progressFill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
      this.progressFill.style.background = STATE_COLORS[state] || STATE_COLORS.printing;
      this.progressLabel.textContent = `${percent.toFixed(0)}%`;
    }

    this.statsEl.replaceChildren();
    const layers =
      asNumber(snapshot.current_layer) !== null && asNumber(snapshot.total_layers) !== null
        ? `${snapshot.current_layer} / ${snapshot.total_layers}`
        : null;
    const remaining = asNumber(snapshot.remaining);
    for (const [key, label, value] of [
      ["layer", "Layer", layers],
      ["remaining", "Remaining", remaining !== null ? formatDuration(remaining) : null],
      ["elapsed", "Elapsed", asNumber(snapshot.elapsed) !== null ? formatDuration(snapshot.elapsed) : null],
      ["eta", "Done at", ACTIVE_STATES.has(state) ? formatEta(remaining) : null],
      ["speed", "Speed", asNumber(snapshot.speed_factor) !== null ? `${snapshot.speed_factor}%` : null],
    ]) {
      if (value === null || value === undefined || this.poweredOff) continue;
      const cell = el("div", "stat");
      cell.dataset.stat = key;
      cell.append(el("span", "stat-label trunc", label), el("span", "stat-value trunc", value));
      this.statsEl.appendChild(cell);
    }
    this.statsEl.hidden = this.statsEl.childElementCount === 0;

    this.jobEl.replaceChildren();
    if (snapshot.filename && !this.poweredOff) {
      this.jobEl.append(icon("file", 16), el("span", "job-name trunc", baseName(snapshot.filename)));
    }
    this.jobEl.hidden = this.jobEl.childElementCount === 0;

    this.tempsEl.replaceChildren();
    if (!this.poweredOff) {
      const chamberVisible = caps.includes("set_chamber_temp") || caps.includes("chamber_sensor");
      for (const [label, temps, visible] of [
        ["Nozzle", snapshot.hotend, true],
        ["Bed", snapshot.bed, true],
        ["Chamber", snapshot.chamber, chamberVisible],
      ]) {
        if (!visible || !temps) continue;
        if (asNumber(temps.current) === null && asNumber(temps.target) === null) continue;
        const cell = el("div", "temp");
        cell.appendChild(el("span", "temp-label trunc", label));
        const target = asNumber(temps.target);
        const current = formatTemperature(temps.current);
        cell.appendChild(el("span", "temp-value trunc", target ? `${current} → ${target.toFixed(0)} °C` : current));
        if (target) cell.classList.add("heating");
        this.tempsEl.appendChild(cell);
      }
    }
    this.tempsEl.hidden = this.tempsEl.childElementCount === 0;

    this.fansReadout.replaceChildren();
    const fans = snapshot.fans || {};
    if (!this.poweredOff) {
      for (const fan of [...SETTABLE_FANS, ...REPORTED_FANS]) {
        const value = asNumber(fans[fan.key]);
        if (value === null) continue;
        const cell = el("span", "fan-reading");
        cell.append(icon("fan", 14), el("span", "", `${fan.label} ${value.toFixed(0)}%`));
        if (value > 0) cell.classList.add("spinning");
        this.fansReadout.appendChild(cell);
      }
    }
    this.fansReadout.hidden = this.fansReadout.childElementCount === 0;

    const online = this.online();
    const printing = state === "printing";
    const paused = state === "paused";
    const busy = (command) => this.card.isBusy(this.entryId, command);
    this.pauseButton.hidden = !caps.includes("pause");
    this.pauseButton.disabled = !online || !printing || busy("pause") || Boolean(this.blockedReason("pause"));
    this.resumeButton.hidden = !caps.includes("resume");
    this.resumeButton.disabled = !online || !paused || busy("resume") || Boolean(this.blockedReason("resume"));
    this.stopButton.hidden = !caps.includes("stop");
    this.stopButton.disabled = !online || !ACTIVE_STATES.has(state) || busy("stop") || Boolean(this.blockedReason("stop"));
    this.jobControls.hidden = !["pause", "resume", "stop"].some((item) => caps.includes(item));
  }

  /** Return true when the printer reports a multi-material unit with slots. */
  hasFilament() {
    const system = this.snapshot.filament;
    if (!this.capabilities.includes("filament_slots") || !system || this.poweredOff) return false;
    return (system.units || []).some((unit) => (unit.slots || []).length > 0);
  }

  _updateFilament() {
    const shown = this.hasFilament();
    this.filamentButton.hidden = !shown;
    this.filamentStrip.hidden = !shown;
    if (!shown) {
      this.filament.update();
      return;
    }
    const system = this.snapshot.filament;
    const active = this.filament.slots().find((slot) => slot.active) || null;
    this.filamentButton.classList.toggle("on", Boolean(active));
    const dots = this.filament.slots().map((slot) => {
      const dot = el("span", "strip-dot");
      if (slot.loaded && slot.color) dot.style.background = slot.color;
      dot.classList.toggle("empty", !slot.loaded);
      dot.classList.toggle("active", Boolean(slot.active));
      return dot;
    });
    const label = system.activity
      ? system.activity
      : active
        ? `${slotTitle(active)} in use, slot ${slotLabel(active.unit, active.slot)}`
        : "No filament in the nozzle";
    this.filamentStrip.replaceChildren(icon("nozzle", 16), ...dots, el("span", "strip-label trunc", label));
    this.filamentStrip.setAttribute("aria-label", `Filament: ${label}. Open the filament panel.`);
    this.filamentStrip.classList.toggle("working", Boolean(system.activity));
    this.filament.update();
  }

  _updateControls() {
    const caps = this.capabilities;
    const snapshot = this.snapshot;
    const online = this.online();
    const busy = (command) => this.card.isBusy(this.entryId, command);

    let anyHeater = false;
    for (const heater of HEATERS) {
      const parts = this.heaterRows[heater.key];
      const allowed = caps.includes(heater.command);
      parts.row.hidden = !allowed;
      if (!allowed) continue;
      anyHeater = true;
      const temps = snapshot[heater.key] || {};
      parts.current.textContent = formatTemperature(temps.current);
      if (!editing(parts.input)) {
        const target = asNumber(temps.target);
        parts.input.value = target === null ? "" : String(Math.round(target));
      }
      const refused = Boolean(this.blockedReason(heater.command));
      parts.input.disabled = !online || refused;
      for (const node of parts.row.querySelectorAll("button")) {
        node.disabled = !online || refused || busy(heater.command);
      }
    }
    this.heaterSection.hidden = !anyHeater;
    const heaterReason = online ? this.firstBlocked(HEATERS.filter((heater) => caps.includes(heater.command)).map((heater) => heater.command)) : "";
    this.heaterHint.textContent = heaterReason ? `Temperatures: ${heaterReason}.` : "";
    this.heaterHint.hidden = !heaterReason;
    for (const node of this.presetRow.querySelectorAll("button")) node.disabled = !online;

    const fans = snapshot.fans || {};
    let anyFan = false;
    for (const fan of SETTABLE_FANS) {
      const slider = this.fanSliders[fan.key];
      const value = asNumber(fans[fan.key]);
      const shown = caps.includes("set_fan_speed") && value !== null;
      slider.row.hidden = !shown;
      if (!shown) continue;
      anyFan = true;
      if (!editing(slider.input)) {
        slider.input.value = String(Math.round(value));
        slider.value.textContent = `${Math.round(value)}%`;
      }
      slider.input.disabled = !online || Boolean(this.blockedReason("set_fan_speed"));
    }
    const speed = asNumber(snapshot.speed_factor);
    const speedShown = caps.includes("set_speed");
    this.speedSlider.row.hidden = !speedShown;
    if (speedShown) {
      if (!editing(this.speedSlider.input) && speed !== null) {
        this.speedSlider.input.value = String(Math.min(100, Math.round(speed)));
        this.speedSlider.value.textContent = `${Math.round(speed)}%`;
      }
      this.speedSlider.input.disabled = !online || Boolean(this.blockedReason("set_speed"));
    }
    this.reportedFans.replaceChildren();
    for (const fan of REPORTED_FANS) {
      const value = asNumber(fans[fan.key]);
      if (value === null) continue;
      this.reportedFans.appendChild(el("span", "fan-reading", `${fan.label} ${value.toFixed(0)}%`));
    }
    this.reportedFans.hidden = this.reportedFans.childElementCount === 0;
    this.fanSection.hidden = !anyFan && !speedShown;
    const fanReason = online
      ? this.firstBlocked([anyFan ? "set_fan_speed" : null, speedShown ? "set_speed" : null].filter(Boolean))
      : "";
    this.fanHint.textContent = fanReason ? `Fans and speed: ${fanReason}.` : "";
    this.fanHint.hidden = !fanReason;

    const canJog = caps.includes("jog");
    const canHome = caps.includes("home");
    const jogReason = this.blockedReason("jog");
    const homeReason = this.blockedReason("home");
    this.motionSection.hidden = !canJog && !canHome;
    this.joystick.hidden = !canJog;
    this.zColumn.hidden = !canJog && !canHome;
    this.stepChips.hidden = !canJog;
    for (const node of this.jogButtons) {
      node.hidden = !canJog;
      node.disabled = !online || Boolean(jogReason) || busy("jog");
    }
    for (const node of [this.homeXY, this.homeZ, this.homeAll]) {
      node.hidden = !canHome;
      node.disabled = !online || Boolean(homeReason) || busy("home");
    }
    this.homeXY.hidden = !canHome || !canJog;
    const position = snapshot.position;
    this.positionEl.replaceChildren();
    if (position && !this.poweredOff) {
      for (const axis of ["x", "y", "z"]) {
        const value = asNumber(position[axis]);
        const cell = el("span", "axis");
        const homed = (snapshot.homed_axes || []).includes(axis);
        cell.classList.toggle("homed", homed);
        cell.title = homed ? `${axis.toUpperCase()} is homed` : `${axis.toUpperCase()} is not homed`;
        cell.append(el("span", "axis-name", axis.toUpperCase()), el("span", "axis-value", value === null ? "—" : value.toFixed(2)));
        this.positionEl.appendChild(cell);
      }
    }
    this.positionEl.hidden = this.positionEl.childElementCount === 0;
    const motionReason = (canJog && jogReason) || (canHome && homeReason) || "";
    this.motionHint.textContent = online && motionReason ? `The head cannot be moved: ${motionReason}.` : "";
    this.motionHint.hidden = !this.motionHint.textContent;

    this.noControls.hidden = anyHeater || anyFan || speedShown || canJog || canHome;
  }

  _paintSteps() {
    if (!this.stepChips) return;
    for (const chip of this.stepChips.querySelectorAll(".step")) {
      const active = Number(chip.dataset.step) === this.jogStep;
      chip.classList.toggle("active", active);
      chip.setAttribute("aria-pressed", String(active));
    }
  }

  _updateFilesAvailability() {
    const caps = this.capabilities;
    const online = this.online();
    const listed = caps.includes("file_list");
    if (this.tabButtons) this.tabButtons.files.hidden = !listed && !caps.includes("file_upload");
    this.uploadButton.hidden = !caps.includes("file_upload");
    this.uploadButton.disabled = !online || Boolean(this.uploading);
    this.printAfterUpload.hidden = !caps.includes("file_upload") || !caps.includes("start_print");
    this.refreshFiles.hidden = !listed;
    this.refreshFiles.disabled = !online || this.filesLoading;

    const withheld = (this.description.unsafe_features || []).find((feature) =>
      /print/i.test(`${feature.id} ${feature.label}`),
    );
    const hint =
      listed && !caps.includes("start_print") && withheld
        ? `Starting a print is turned off for this printer. Turn on “${withheld.label}” in the integration's options to print from here.`
        : "";
    this.filesHint.textContent = hint;
    this.filesHint.hidden = !hint;

    // The tab can be open before the first reading says whether files are listed.
    if (this.tab === "files" && listed && this.files === null && !this.filesLoading && online) {
      this.loadFiles();
    }
  }

  _paintFiles() {
    if (!this.fileList) return;
    let status = "";
    if (this.uploading) status = `Uploading ${this.uploading}…`;
    else if (this.filesLoading) status = "Reading the printer's files…";
    else if (this.filesError) status = this.filesError;
    else if (this.files && this.files.length === 0) status = "No files on the printer.";
    else if (!this.capabilities.includes("file_list") && this.capabilities.includes("file_upload")) {
      status = "This printer can receive files but cannot list them.";
    }
    this.filesStatus.textContent = status;
    this.filesStatus.hidden = !status;
    this.filesStatus.classList.toggle("error", Boolean(this.filesError) && !this.filesLoading);

    this.fileList.replaceChildren();
    const caps = this.capabilities;
    const online = this.online();
    const current = baseName(this.snapshot.filename);
    const files = [...(this.files || [])].sort((a, b) =>
      String(b.modified || "").localeCompare(String(a.modified || "")) || baseName(a.name).localeCompare(baseName(b.name)),
    );
    for (const file of files) {
      const row = el("div", "file");
      row.dataset.file = file.name;
      const info = el("div", "file-info");
      info.appendChild(el("div", "file-name trunc", baseName(file.name)));
      const meta = [formatBytes(file.size), file.modified ? new Date(file.modified).toLocaleString() : ""]
        .filter(Boolean)
        .join(" · ");
      if (meta) info.appendChild(el("div", "file-meta trunc", meta));
      row.append(icon("file", 18), info);
      if (caps.includes("start_print")) {
        const print = button("icon-btn file-print", "Print", "play", "start_print");
        print.disabled = !online || Boolean(this.blockedReason("start_print")) || this.card.isBusy(this.entryId, "start_print");
        print.addEventListener("click", () => this.printFile(file));
        row.appendChild(print);
      }
      if (caps.includes("file_delete")) {
        const remove = button("icon-btn file-delete danger", "Delete", "delete", "delete_file");
        remove.disabled = !online || (this.activeJob() && baseName(file.name) === current);
        remove.addEventListener("click", () => this.deleteFile(file));
        row.appendChild(remove);
      }
      this.fileList.appendChild(row);
    }
    // The list is also redrawn outside a reading, when files arrive or an upload ends.
    this.card.refreshTip();
  }

  _updateFooter() {
    const description = this.description;
    this.footer.replaceChildren();
    if (this.capabilities.includes("web_ui") && description.web_ui_url) {
      const link = el("a", "link");
      link.href = description.web_ui_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.append(icon("open", 14), el("span", "", "Open printer page"));
      this.footer.appendChild(link);
    }
    const stats = description.camera_stats;
    if (stats && stats.viewers) {
      this.footer.appendChild(el("span", "hint", `${stats.viewers} watching`));
    }
    this.footer.hidden = this.footer.childElementCount === 0;
  }
}

// --------------------------------------------------------------------- card

class Generic3DPrinterCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._printers = [];
    this._descriptions = new Map();
    this._views = new Map();
    this._timer = null;
    this._busy = new Set();
    this._loaded = false;
    this.powerBusy = false;
  }

  static getStubConfig() {
    return {};
  }

  static getConfigElement() {
    return document.createElement("generic-3dprinter-card-editor");
  }

  setConfig(config) {
    const next = { ...(config || {}) };
    // An empty YAML key, `jog_steps:`, arrives as null and means "not set".
    for (const key of ["jog_steps", "temperature_presets"]) {
      if (next[key] === null) delete next[key];
    }
    if (next.jog_steps !== undefined) {
      const steps = Array.isArray(next.jog_steps) ? next.jog_steps.map(Number) : [];
      if (!steps.length || steps.some((step) => !(step > 0))) {
        throw new Error("jog_steps must be a list of positive distances in millimetres");
      }
    }
    if (next.temperature_presets !== undefined && !Array.isArray(next.temperature_presets)) {
      throw new Error("temperature_presets must be a list of {name, hotend, bed}");
    }
    this._config = next;
    // The layout depends on the configuration, so the views are rebuilt. Their
    // streams are stopped first, so a discarded view does not keep one open.
    for (const view of this._views.values()) view._suspendCamera();
    this._views.clear();
    this._sync();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._start();
      return;
    }
    // Home Assistant hands a new object on every state change anywhere. Only the
    // power entity is read from it, so only its change is worth a redraw.
    const entity = this._config.power_entity;
    const power = entity ? hass.states && hass.states[entity] : undefined;
    if (power !== this._lastPower) {
      this._lastPower = power;
      this._sync();
    }
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (this._hass) this._start();
  }

  disconnectedCallback() {
    this._stop();
    if (this._tip) this._tip.hide();
    for (const view of this._views.values()) view._suspendCamera();
  }

  getCardSize() {
    return this._config.fleet ? Math.max(3, this._printers.length * 4) : 10;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  // ------------------------------------------------------------- settings

  jogSteps() {
    const steps = this._config.jog_steps;
    return Array.isArray(steps) && steps.length ? steps.map(Number) : DEFAULT_JOG_STEPS;
  }

  presets() {
    const presets = this._config.temperature_presets;
    if (!Array.isArray(presets)) return DEFAULT_PRESETS;
    return presets.filter((preset) => preset && preset.name);
  }

  showCamera() {
    return this._config.show_camera !== false;
  }

  confirm(message) {
    return window.confirm(message);
  }

  isBusy(entryId, command) {
    return this._busy.has(`${entryId}:${command}`);
  }

  // ------------------------------------------------------------- polling

  _start() {
    if (this._timer) return;
    this._refreshAll();
    this._timer = window.setInterval(() => this._refreshAll(), REFRESH_MS);
  }

  _stop() {
    if (this._timer) {
      window.clearInterval(this._timer);
      this._timer = null;
    }
  }

  _wanted() {
    if (this._config.entry_id) {
      return this._printers.filter((item) => item.entry_id === this._config.entry_id);
    }
    return this._config.fleet ? this._printers : this._printers.slice(0, 1);
  }

  async _refreshAll() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: WS_LIST });
      this._printers = result.printers || [];
      this._error = null;
    } catch (err) {
      this._printers = [];
      this._error = err.message || String(err);
    }
    this._visible = this._wanted();
    await Promise.all(this._visible.map((printer) => this._describe(printer.entry_id)));
    this._loaded = true;
    this._sync();
  }

  async _describe(entryId) {
    try {
      const description = await this._hass.callWS({ type: WS_DESCRIBE, entry_id: entryId });
      this._descriptions.set(entryId, description);
    } catch (err) {
      this._descriptions.set(entryId, { error: err.message || String(err) });
    }
  }

  async refreshOne(entryId) {
    await this._describe(entryId);
    this._sync();
  }

  // ------------------------------------------------------------- actions

  async send(entryId, command, data = {}, { refresh = true } = {}) {
    const key = `${entryId}:${command}`;
    this._busy.add(key);
    this._sync();
    try {
      await this._hass.callWS({ type: WS_SEND, entry_id: entryId, command, data });
      return true;
    } catch (err) {
      this._notify(err.message || String(err));
      return false;
    } finally {
      this._busy.delete(key);
      if (refresh) await this.refreshOne(entryId);
      else this._sync();
    }
  }

  async listFiles(entryId) {
    const result = await this._hass.callWS({ type: WS_FILES, entry_id: entryId });
    return result.files || [];
  }

  async upload(entryId, file) {
    const form = new FormData();
    form.append("file", file, file.name);
    try {
      const response = await this._hass.fetchWithAuth(`${API_BASE}/${entryId}/upload`, {
        method: "POST",
        body: form,
      });
      let body = {};
      try {
        body = await response.json();
      } catch {
        body = {};
      }
      if (!response.ok) throw new Error(body.error || body.message || `HTTP ${response.status}`);
      this._notify(`${file.name} uploaded to the printer.`);
      return body.file || null;
    } catch (err) {
      this._notify(`Upload failed: ${err.message || String(err)}`);
      return null;
    }
  }

  _power() {
    const entity = this._config.power_entity;
    if (!entity || !this._hass) return null;
    const stateObj = this._hass.states && this._hass.states[entity];
    if (!stateObj) return { entity_id: entity, state: "unavailable", missing: true };
    return { entity_id: entity, state: stateObj.state, missing: false };
  }

  async togglePower(view) {
    const power = this._power();
    if (!power || power.missing) return;
    const on = power.state === "on";
    if (on) {
      const snapshot = view.snapshot || {};
      const nozzle = asNumber(snapshot.hotend && snapshot.hotend.current);
      let question = "Switch the printer off?";
      if (ACTIVE_STATES.has(snapshot.print_state)) {
        question =
          "The printer is busy. Switching it off ends the job, and the print cannot be resumed. Switch it off anyway?";
      } else if (nozzle !== null && nozzle > HOT_NOZZLE) {
        question = `The nozzle is still at ${nozzle.toFixed(0)} °C. Switching the printer off stops the fan that cools it. Switch it off anyway?`;
      }
      if (!this.confirm(question)) return;
    }
    this.powerBusy = true;
    this._sync();
    try {
      await this._hass.callService("homeassistant", on ? "turn_off" : "turn_on", {
        entity_id: power.entity_id,
      });
    } catch (err) {
      this._notify(err.message || String(err));
    } finally {
      this.powerBusy = false;
      this._sync();
    }
  }

  _notify(message) {
    this.dispatchEvent(
      new CustomEvent("hass-notification", {
        detail: { message },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // ------------------------------------------------------------- drawing

  _sync() {
    if (!this.shadowRoot) return;
    // A drawing error must not reach Home Assistant, which would replace the card with
    // its error card for the rest of the page's life. It goes to the console instead,
    // and the next reading draws again.
    try {
      this._draw();
      this.refreshTip();
    } catch (err) {
      console.error("generic-3dprinter-card: drawing failed", err);
    }
  }

  /** Keep the popup of a text cut short on its text after the DOM under it changed. */
  refreshTip() {
    if (this._tip) this._tip.refresh();
  }

  _draw() {
    if (!this._container) {
      this.shadowRoot.replaceChildren();
      this.shadowRoot.appendChild(this._style());
      this._container = el("ha-card", "card");
      this._titleEl = el("div", "card-title trunc");
      this._list = el("div", "printers");
      this._container.append(this._titleEl, this._list);
      this.shadowRoot.appendChild(this._container);
      this._tip = new TextTip(this.shadowRoot, this._container);
    }
    this._titleEl.textContent = this._config.title || "";
    this._titleEl.hidden = !this._config.title;

    const existing = this.shadowRoot.querySelector(".empty");
    if (existing) existing.remove();
    if (!this._loaded) return;

    if (this._error || !this._visible || this._visible.length === 0) {
      this._views.clear();
      this._list.replaceChildren();
      const message = this._error
        ? `Cannot reach the integration: ${this._error}`
        : "No 3D printer yet. Add one from Settings, Devices and Services.";
      this._container.appendChild(el("div", "empty", message));
      return;
    }

    const compact = Boolean(this._config.fleet) && !this._config.entry_id;
    const power = compact ? null : this._power();
    const order = [];
    for (const printer of this._visible) {
      let view = this._views.get(printer.entry_id);
      if (!view) {
        view = new PrinterView(this, printer.entry_id, compact);
        this._views.set(printer.entry_id, view);
      }
      view.update(printer, this._descriptions.get(printer.entry_id) || {}, power);
      order.push(view.root);
    }
    for (const [entryId, view] of this._views) {
      if (!this._visible.some((printer) => printer.entry_id === entryId)) {
        view._suspendCamera();
        this._views.delete(entryId);
      }
    }
    const current = [...this._list.children];
    if (current.length !== order.length || current.some((node, index) => node !== order[index])) {
      this._list.replaceChildren(...order);
    }
  }

  _style() {
    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; container-type: inline-size; }
      ha-card, .card {
        display: block;
        background: var(--ha-card-background, var(--card-background-color, #fff));
        border-radius: var(--ha-card-border-radius, 12px);
        box-shadow: var(--ha-card-box-shadow, none);
        border: var(--ha-card-border-width, 1px) solid var(--ha-card-border-color, var(--divider-color, #e0e0e0));
        padding: 16px;
        color: var(--primary-text-color);
        font-family: var(--ha-font-family-body, var(--paper-font-body1_-_font-family, sans-serif));
        box-sizing: border-box;
        position: relative;
      }
      [hidden] { display: none !important; }
      /* One line, cut with an ellipsis; the popup shows the rest. */
      .trunc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
      .tip {
        position: absolute; z-index: 10; max-width: calc(100% - 16px); box-sizing: border-box;
        padding: 6px 10px; border-radius: 8px; font-size: 0.82rem; line-height: 1.35;
        /* The theme's text and page colours swapped, so it stands out in either theme. */
        background: var(--primary-text-color, #212121);
        color: var(--primary-background-color, var(--card-background-color, #fff));
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35); pointer-events: none;
        white-space: normal; overflow-wrap: anywhere;
      }
      button { font: inherit; }
      .card-title { font-size: 1.15rem; font-weight: 600; margin-bottom: 12px; }
      .printer + .printer { border-top: 1px solid var(--divider-color, #e0e0e0); margin-top: 16px; padding-top: 16px; }

      .printer-header { display: flex; align-items: center; gap: 10px; }
      .dot { width: 12px; height: 12px; border-radius: 50%; flex: 0 0 auto; }
      .names { flex: 1 1 auto; min-width: 0; }
      .name { font-weight: 600; font-size: 1.05rem; }
      .subtitle { font-size: 0.78rem; color: var(--secondary-text-color); }
      .state { font-size: 0.78rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; white-space: nowrap; }

      .icon-btn {
        display: inline-flex; align-items: center; justify-content: center;
        width: 38px; height: 38px; border-radius: 50%; flex: 0 0 auto;
        border: 1px solid var(--divider-color, #e0e0e0);
        background: transparent; color: var(--secondary-text-color); cursor: pointer; padding: 0;
        transition: background 0.2s, color 0.2s, border-color 0.2s;
      }
      .icon-btn .label { display: none; }
      .icon-btn:hover:not(:disabled) { background: var(--secondary-background-color, #f5f5f5); }
      .icon-btn:disabled { opacity: 0.4; cursor: default; }
      .light-toggle.on { color: #ffb300; border-color: #ffb300; background: rgba(255, 179, 0, 0.12); }
      .power-toggle.on { color: var(--success-color, #43a047); border-color: var(--success-color, #43a047); background: rgba(67, 160, 71, 0.12); }
      .icon-btn.danger { color: var(--error-color, #f44336); }

      /* Messages wrap rather than cut, and a long file name or address in one breaks. */
      .warning, .error-line, .hint, .files-status, .empty, .empty-panel, .camera-error { overflow-wrap: anywhere; }
      .warning { margin-top: 10px; font-size: 0.85rem; color: var(--error-color, #f44336); }
      .warning.off { color: var(--secondary-text-color); }
      .errors { margin-top: 6px; }
      .error-line { font-size: 0.8rem; color: var(--warning-color, #ff9800); }

      /* minmax(0, 1fr), not 1fr: a 1fr column grows to its longest line, a file name
       * included, and takes the whole card past its edge. */
      .body { display: grid; grid-template-columns: minmax(0, 1fr); gap: 14px; margin-top: 12px; }
      .body > * { min-width: 0; }
      @container (min-width: 720px) {
        .body.with-camera { grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); align-items: start; }
      }
      .camera-frame {
        position: relative; border-radius: 10px; overflow: hidden; background: #111;
        aspect-ratio: 16 / 9; display: flex; align-items: center; justify-content: center;
      }
      .camera img { width: 100%; height: 100%; object-fit: contain; display: block; background: #000; }
      .camera .stream-card { width: 100%; height: 100%; display: block; --ha-card-border-radius: 0; --ha-card-box-shadow: none; }
      .camera-error { color: #bbb; font-size: 0.85rem; }
      .camera-overlay { position: absolute; left: 8px; bottom: 8px; display: flex; gap: 6px; }
      .overlay-chip { color: #fff; font-size: 0.72rem; font-weight: 700; padding: 3px 8px; border-radius: 999px; text-transform: uppercase; letter-spacing: 0.04em; }
      .overlay-chip.dark { background: rgba(0, 0, 0, 0.6); }
      .camera-fullscreen { position: absolute; top: 8px; right: 8px; width: 32px; height: 32px; background: rgba(0, 0, 0, 0.45); color: #fff; border: none; }

      .tabs { display: flex; gap: 4px; padding: 3px; border-radius: 10px; background: var(--secondary-background-color, #f2f2f2); margin-bottom: 12px; }
      .tab {
        flex: 1 1 0; border: none; background: transparent; color: var(--secondary-text-color);
        padding: 7px 10px; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 0.85rem;
      }
      .tab.active { background: var(--card-background-color, #fff); color: var(--primary-color); box-shadow: 0 1px 3px rgba(0, 0, 0, 0.15); }

      .progress-track { height: 10px; border-radius: 5px; background: var(--divider-color, #e0e0e0); overflow: hidden; }
      .progress-fill { height: 100%; transition: width 0.4s ease; border-radius: 5px; }
      .progress-label { font-size: 1.6rem; font-weight: 700; margin-top: 6px; font-variant-numeric: tabular-nums; }
      .stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(84px, 1fr)); gap: 10px; margin-top: 10px; }
      .stat { display: flex; flex-direction: column; min-width: 0; }
      .stat-label, .temp-label, .section-title, .steps-label {
        font-size: 0.7rem; text-transform: uppercase; color: var(--secondary-text-color); letter-spacing: 0.05em;
      }
      .stat-value { font-variant-numeric: tabular-nums; font-weight: 600; }
      .job { display: flex; align-items: center; gap: 6px; margin-top: 12px; font-size: 0.88rem; color: var(--secondary-text-color); }
      .job-name { color: var(--primary-text-color); }
      .temps { display: grid; grid-template-columns: repeat(auto-fill, minmax(128px, 1fr)); gap: 8px; margin-top: 12px; }
      .temp { display: flex; flex-direction: column; padding: 8px 10px; border-radius: 10px; background: var(--secondary-background-color, #f5f5f5); min-width: 0; }
      .temp.heating { box-shadow: inset 3px 0 0 var(--warning-color, #ff9800); }
      .temp-value { font-variant-numeric: tabular-nums; font-weight: 600; font-size: 0.95rem; }
      .fans-readout, .reported-fans { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; font-size: 0.8rem; color: var(--secondary-text-color); }
      .fan-reading { display: inline-flex; align-items: center; gap: 4px; }
      .fan-reading.spinning svg { animation: spin 1.2s linear infinite; color: var(--primary-color); }
      @keyframes spin { to { transform: rotate(360deg); } }
      @media (prefers-reduced-motion: reduce) { .fan-reading.spinning svg { animation: none; } }

      .controls { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
      .ctl {
        display: inline-flex; align-items: center; gap: 6px;
        border: 1px solid var(--divider-color, #e0e0e0); background: transparent; color: var(--primary-text-color);
        border-radius: 10px; padding: 8px 14px; font-size: 0.88rem; cursor: pointer;
      }
      .ctl:hover:not(:disabled) { background: var(--secondary-background-color, #f5f5f5); }
      .ctl:disabled, .mini:disabled, .chip:disabled, .jog:disabled { opacity: 0.4; cursor: default; }
      .ctl.primary { border-color: var(--primary-color); color: var(--primary-color); }
      .ctl.danger { border-color: var(--error-color, #f44336); color: var(--error-color, #f44336); }

      .section { padding: 12px 0; border-top: 1px solid var(--divider-color, #e0e0e0); }
      .section:first-child { border-top: none; padding-top: 0; }
      .section-title { margin-bottom: 8px; font-weight: 600; }
      .heater { display: grid; grid-template-columns: 96px 1fr; align-items: center; gap: 8px; padding: 5px 0; }
      .heater-name { display: flex; flex-direction: column; }
      .heater-label { display: inline-flex; align-items: center; gap: 4px; font-weight: 600; }
      .heater-current { font-variant-numeric: tabular-nums; color: var(--secondary-text-color); font-size: 0.85rem; padding-left: 20px; }
      .heater-controls { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; justify-content: flex-end; }
      .heater-input {
        width: 56px; padding: 6px 4px; border-radius: 8px; border: 1px solid var(--divider-color, #ccc);
        background: var(--card-background-color, #fff); color: var(--primary-text-color); font: inherit; text-align: center;
      }
      .mini {
        border: 1px solid var(--divider-color, #e0e0e0); background: transparent; color: var(--primary-text-color);
        border-radius: 8px; padding: 5px 7px; cursor: pointer; font-size: 0.8rem;
      }
      .mini.accent { border-color: var(--primary-color); color: var(--primary-color); font-weight: 600; }
      .presets, .steps { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; align-items: center; }
      .chip {
        border: 1px solid var(--divider-color, #e0e0e0); background: transparent; color: var(--primary-text-color);
        border-radius: 999px; padding: 4px 12px; cursor: pointer; font-size: 0.82rem;
      }
      .chip.active { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
      .chip.cooldown { color: var(--info-color, #2196f3); border-color: var(--info-color, #2196f3); }
      .slider-row { display: grid; grid-template-columns: 130px 1fr 48px; gap: 8px; align-items: center; padding: 3px 0; }
      .slider-label { display: inline-flex; align-items: center; gap: 6px; font-size: 0.88rem; }
      .slider { width: 100%; accent-color: var(--primary-color); }
      .slider-value { text-align: right; font-variant-numeric: tabular-nums; font-size: 0.85rem; }

      .motion-body { display: flex; flex-wrap: wrap; gap: 18px; align-items: center; }
      .pad { display: flex; gap: 14px; align-items: center; }
      .joystick {
        position: relative; width: 168px; height: 168px; border-radius: 50%;
        background: radial-gradient(circle at 50% 45%, var(--card-background-color, #fff) 0 30%, var(--secondary-background-color, #eee) 31% 100%);
        border: 1px solid var(--divider-color, #ddd); outline: none;
      }
      .joystick:focus-visible { box-shadow: 0 0 0 2px var(--primary-color); }
      .jog {
        display: inline-flex; align-items: center; justify-content: center; flex-direction: column;
        border: none; background: transparent; color: var(--primary-text-color); cursor: pointer; border-radius: 12px;
        font-size: 0.7rem; font-weight: 700;
      }
      .jog:hover:not(:disabled) { background: rgba(127, 127, 127, 0.15); color: var(--primary-color); }
      .jog:active:not(:disabled) { transform: scale(0.94); }
      .joystick .jog { position: absolute; width: 52px; height: 52px; }
      .jog.y-plus { top: 4px; left: 58px; }
      .jog.y-minus { bottom: 4px; left: 58px; }
      .jog.x-minus { left: 4px; top: 58px; }
      .jog.x-plus { right: 4px; top: 58px; }
      .jog.home-center { top: 58px; left: 58px; border-radius: 50%; color: var(--primary-color); }
      .jog .label { line-height: 1; }
      .home-center .label, .home-z .label { font-size: 0.6rem; }
      .z-column {
        display: flex; flex-direction: column; gap: 4px; padding: 6px; border-radius: 30px;
        background: var(--secondary-background-color, #eee); border: 1px solid var(--divider-color, #ddd);
      }
      .z-column .jog { width: 50px; height: 50px; }
      .home-z { color: var(--primary-color); }
      .motion-side { display: flex; flex-direction: column; gap: 10px; min-width: 150px; }
      .position { display: flex; gap: 10px; font-variant-numeric: tabular-nums; }
      .axis { display: flex; flex-direction: column; padding: 4px 8px; border-radius: 8px; background: var(--secondary-background-color, #f5f5f5); }
      .axis-name { font-size: 0.68rem; color: var(--secondary-text-color); font-weight: 700; }
      .axis.homed .axis-name { color: var(--success-color, #43a047); }
      .empty-panel { color: var(--secondary-text-color); font-size: 0.9rem; }

      .files-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
      .spacer { flex: 1 1 auto; }
      .print-after { display: inline-flex; align-items: center; gap: 6px; font-size: 0.85rem; }
      .files-status { margin-top: 10px; font-size: 0.85rem; color: var(--secondary-text-color); }
      .files-status.error { color: var(--error-color, #f44336); }
      .file-list { margin-top: 8px; display: flex; flex-direction: column; max-height: 360px; overflow-y: auto; }
      .file { display: flex; align-items: center; gap: 10px; padding: 8px 4px; border-bottom: 1px solid var(--divider-color, #eee); }
      .file > svg { color: var(--secondary-text-color); flex: 0 0 auto; }
      .file-info { flex: 1 1 auto; min-width: 0; }
      .file-meta { font-size: 0.75rem; color: var(--secondary-text-color); }
      .file .icon-btn { width: 34px; height: 34px; }
      .file-print { color: var(--primary-color); }

      .footer { display: flex; gap: 12px; align-items: center; margin-top: 12px; font-size: 0.8rem; }
      .link { display: inline-flex; align-items: center; gap: 4px; color: var(--primary-color); text-decoration: none; }
      .hint { color: var(--secondary-text-color); font-size: 0.8rem; }
      .empty { color: var(--secondary-text-color); font-size: 0.9rem; }

      .filament-toggle.on { color: var(--primary-color); border-color: var(--primary-color); }
      .filament-strip {
        display: flex; align-items: center; gap: 6px; width: 100%; margin-top: 10px; box-sizing: border-box;
        padding: 7px 10px; border-radius: 10px; border: 1px solid var(--divider-color, #e0e0e0);
        background: transparent; color: var(--secondary-text-color); cursor: pointer; font-size: 0.82rem; text-align: left;
      }
      .filament-strip:hover { background: var(--secondary-background-color, #f5f5f5); }
      .filament-strip.working { border-color: var(--warning-color, #ff9800); }
      .strip-dot {
        width: 14px; height: 14px; border-radius: 50%; flex: 0 0 auto; box-sizing: border-box;
        border: 1px solid rgba(127, 127, 127, 0.55);
      }
      .strip-dot.empty { background: transparent; border-style: dashed; }
      .strip-dot.active { box-shadow: 0 0 0 2px var(--card-background-color, #fff), 0 0 0 4px var(--success-color, #43a047); }
      .strip-label { flex: 1 1 auto; margin-left: 4px; color: var(--primary-text-color); }

      .filament-dialog {
        padding: 0; border: none; border-radius: 16px; box-sizing: border-box;
        width: min(660px, calc(100vw - 32px)); max-height: calc(100vh - 32px);
        background: var(--ha-card-background, var(--card-background-color, #fff)); color: var(--primary-text-color);
        box-shadow: 0 12px 40px rgba(0, 0, 0, 0.45); font-family: inherit;
      }
      .filament-dialog::backdrop { background: rgba(0, 0, 0, 0.55); }
      .fd-body { padding: 16px 18px 18px; display: flex; flex-direction: column; gap: 14px; }
      .fd-head { display: flex; align-items: center; gap: 10px; color: var(--primary-color); }
      .fd-titles { flex: 1 1 auto; min-width: 0; color: var(--primary-text-color); }
      .fd-title { font-size: 1.1rem; font-weight: 600; }
      .fd-subtitle { font-size: 0.8rem; color: var(--secondary-text-color); }
      .fd-activity {
        padding: 8px 12px; border-radius: 10px; font-weight: 600; font-size: 0.88rem;
        background: rgba(255, 152, 0, 0.12); color: var(--warning-color, #ff9800);
      }
      .fd-main { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); gap: 16px; align-items: start; }
      @media (max-width: 580px) { .fd-main { grid-template-columns: minmax(0, 1fr); } }
      .fd-units { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
      .fd-unit {
        padding: 12px; border-radius: 14px; background: var(--secondary-background-color, #f2f2f2);
        border: 1px solid var(--divider-color, #e0e0e0);
      }
      .fd-unit.disconnected { opacity: 0.55; }
      .fd-unit-head {
        font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
        color: var(--secondary-text-color); margin-bottom: 10px;
      }
      .fd-spools { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
      .spool {
        position: relative; display: flex; flex-direction: column; align-items: center; gap: 6px; min-width: 0;
        padding: 12px 6px 8px; border-radius: 12px; border: 2px solid transparent; font: inherit;
        background: var(--card-background-color, #fff); color: var(--primary-text-color); cursor: pointer;
      }
      .spool:hover:not(:disabled) { border-color: var(--divider-color, #e0e0e0); }
      .spool.selected, .spool.selected:hover { border-color: var(--primary-color); }
      .spool:disabled { opacity: 0.45; cursor: default; }
      .spool-num { position: absolute; top: 6px; left: 9px; font-size: 0.72rem; font-weight: 700; color: var(--secondary-text-color); }
      .spool-disc {
        position: relative; width: 64px; height: 64px; border-radius: 50%; box-sizing: border-box;
        border: 4px solid rgba(127, 127, 127, 0.35);
      }
      .spool-disc.empty { border-style: dashed; }
      .spool-hub {
        position: absolute; top: 50%; left: 50%; width: 20px; height: 20px; margin: -10px 0 0 -10px; box-sizing: border-box;
        border-radius: 50%; background: var(--card-background-color, #fff); border: 2px solid rgba(127, 127, 127, 0.35);
      }
      .spool-ink {
        position: absolute; bottom: 6px; left: 2px; right: 2px; text-align: center;
        font-size: 0.6rem; font-weight: 800; letter-spacing: 0.02em; overflow: hidden; white-space: nowrap;
      }
      .spool.active .spool-disc {
        box-shadow: 0 0 0 3px var(--card-background-color, #fff), 0 0 0 5px var(--success-color, #43a047);
      }
      .spool-name { max-width: 100%; font-size: 0.8rem; font-weight: 600; }
      .spool-badge {
        font-size: 0.62rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;
        color: var(--success-color, #43a047);
      }
      .fd-side { min-width: 0; }
      .fd-detail-head { display: flex; align-items: center; gap: 10px; }
      .fd-swatch {
        width: 22px; height: 22px; border-radius: 50%; flex: 0 0 auto; box-sizing: border-box;
        border: 1px solid rgba(127, 127, 127, 0.55);
      }
      .fd-swatch.empty { background: transparent; border-style: dashed; }
      .fd-detail-name { flex: 1 1 auto; font-weight: 600; font-size: 1rem; }
      .fd-badge {
        flex: 0 0 auto; font-size: 0.68rem; font-weight: 700; padding: 2px 8px; border-radius: 999px;
        text-transform: uppercase; letter-spacing: 0.04em;
        background: var(--secondary-background-color, #f2f2f2); color: var(--secondary-text-color);
      }
      .fd-badge.in-use { background: rgba(67, 160, 71, 0.15); color: var(--success-color, #43a047); }
      .fd-badge.loaded { color: var(--primary-color); }
      .fd-rows { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 6px 12px; margin: 12px 0; font-size: 0.86rem; }
      .fd-rows dt { color: var(--secondary-text-color); }
      .fd-rows dd { margin: 0; font-weight: 600; }
      .fd-actions { display: flex; flex-wrap: wrap; gap: 8px; }
      .fd-note { margin-top: 10px; }
      .fd-foot {
        display: flex; align-items: center; gap: 8px; padding-top: 12px; font-size: 0.88rem;
        border-top: 1px solid var(--divider-color, #e0e0e0); color: var(--secondary-text-color);
      }
      .fd-refill-label { flex: 1 1 auto; color: var(--primary-text-color); }
      .fd-refill { width: 18px; height: 18px; margin: 0; accent-color: var(--primary-color); }
      .fd-refill-text { font-weight: 600; color: var(--primary-text-color); }
      .fd-form { display: flex; flex-direction: column; gap: 10px; }
      .fd-form-title { font-weight: 600; }
      .fd-field { display: flex; flex-direction: column; gap: 4px; font-size: 0.8rem; color: var(--secondary-text-color); }
      .fd-input {
        padding: 7px 8px; border-radius: 8px; border: 1px solid var(--divider-color, #ccc); min-width: 0;
        background: var(--card-background-color, #fff); color: var(--primary-text-color); font: inherit; font-size: 0.9rem;
      }
      .fd-color-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .fd-color {
        width: 40px; height: 32px; padding: 0; border: 1px solid var(--divider-color, #ccc); border-radius: 8px;
        background: none; cursor: pointer;
      }
      .fd-swatches { display: flex; flex-wrap: wrap; gap: 5px; }
      .fd-swatch-btn { width: 20px; height: 20px; border-radius: 50%; border: 1px solid rgba(127, 127, 127, 0.6); padding: 0; cursor: pointer; }
      .fd-swatch-btn .label { display: none; }
      .fd-temps { display: flex; align-items: center; gap: 6px; color: var(--primary-text-color); }
      .fd-temp { width: 64px; text-align: center; }
      @container (max-width: 420px) {
        .heater { grid-template-columns: 1fr; }
        .heater-controls { justify-content: flex-start; }
        .slider-row { grid-template-columns: 1fr 48px; }
        .slider-label { grid-column: 1 / -1; }
      }
    `;
    return style;
  }
}

// ------------------------------------------------------------------ editor

const EDITOR_LABELS = {
  entry_id: "Printer",
  power_entity: "Power switch (smart plug)",
  title: "Title",
  fleet: "Show every printer, compact",
  show_camera: "Show the camera",
};

class Generic3DPrinterCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._printers = null;
  }

  setConfig(config) {
    this._config = { ...(config || {}) };
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._loadPrinters();
    if (this._form) this._form.hass = hass;
    else this._render();
  }

  async _loadPrinters() {
    try {
      const result = await this._hass.callWS({ type: WS_LIST });
      this._printers = result.printers || [];
    } catch {
      this._printers = [];
    }
    this._form = null;
    this._render();
  }

  _schema() {
    const printers = this._printers || [];
    return [
      {
        name: "entry_id",
        selector: {
          select: {
            mode: "dropdown",
            options: printers.map((printer) => ({ value: printer.entry_id, label: printer.name })),
          },
        },
      },
      { name: "power_entity", selector: { entity: { domain: ["switch", "light", "input_boolean"] } } },
      { name: "title", selector: { text: {} } },
      { name: "show_camera", selector: { boolean: {} } },
      { name: "fleet", selector: { boolean: {} } },
    ];
  }

  _changed(config) {
    const cleaned = { ...config };
    for (const key of Object.keys(cleaned)) {
      if (cleaned[key] === "" || cleaned[key] === undefined || cleaned[key] === null) delete cleaned[key];
    }
    this._config = cleaned;
    this.dispatchEvent(
      new CustomEvent("config-changed", { detail: { config: cleaned }, bubbles: true, composed: true }),
    );
  }

  _render() {
    if (!this.shadowRoot) return;
    // Home Assistant's own form, when the frontend has it, gives the entity picker
    // and the dropdown their native look. The plain fallback keeps the editor usable
    // anywhere else.
    if (customElements.get("ha-form") && this._hass) {
      if (!this._form) {
        this._form = document.createElement("ha-form");
        this._form.computeLabel = (field) => EDITOR_LABELS[field.name] || field.name;
        this._form.addEventListener("value-changed", (event) => this._changed(event.detail.value));
        this.shadowRoot.replaceChildren(this._form);
      }
      this._form.hass = this._hass;
      this._form.schema = this._schema();
      this._form.data = { show_camera: true, ...this._config };
      return;
    }
    this._renderPlain();
  }

  _renderPlain() {
    const wrapper = el("div", "editor");
    wrapper.style.display = "grid";
    wrapper.style.gap = "12px";
    wrapper.style.padding = "8px 0";

    const field = (key, control) => {
      const label = el("label");
      label.style.display = "grid";
      label.style.gap = "4px";
      label.appendChild(el("span", "", EDITOR_LABELS[key]));
      label.appendChild(control);
      wrapper.appendChild(label);
    };

    const printer = el("select");
    printer.dataset.key = "entry_id";
    printer.appendChild(new Option("First printer", ""));
    for (const item of this._printers || []) printer.appendChild(new Option(item.name, item.entry_id));
    printer.value = this._config.entry_id || "";
    printer.addEventListener("change", () => this._changed({ ...this._config, entry_id: printer.value }));
    field("entry_id", printer);

    for (const key of ["power_entity", "title"]) {
      const input = el("input");
      input.dataset.key = key;
      input.value = this._config[key] || "";
      input.placeholder = key === "power_entity" ? "switch.printer_plug" : "";
      input.addEventListener("change", () => this._changed({ ...this._config, [key]: input.value.trim() }));
      field(key, input);
    }

    for (const key of ["show_camera", "fleet"]) {
      const toggle = el("input");
      toggle.type = "checkbox";
      toggle.dataset.key = key;
      toggle.checked = key === "show_camera" ? this._config.show_camera !== false : Boolean(this._config[key]);
      toggle.addEventListener("change", () => this._changed({ ...this._config, [key]: toggle.checked }));
      field(key, toggle);
    }

    this.shadowRoot.replaceChildren(wrapper);
  }
}

// ------------------------------------------------------------ registration

/*
 * Home Assistant's frontend replaces `window.customElements` with a scoped-registry
 * polyfill as it starts, and dashboards look cards up only in that registry. A copy
 * of this module that runs before the swap, which happens when it is loaded as an
 * extra module and wins the race against the frontend's own bundle, defines the
 * card on the browser's registry, which Home Assistant never consults: the card
 * then shows as "Configuration error" for the life of the page
 * (home-assistant/frontend#52960). Who wins depends on caches and CPU speed.
 *
 * So the elements are defined on the registry present now and, once Home
 * Assistant's root element exists, on the registry present then. The polyfill takes
 * a class the browser already has, and defining it resolves the `whenDefined` a
 * dashboard is waiting on, so a card already shown as an error rebuilds itself.
 */
const ELEMENTS = [
  ["generic-3dprinter-card", Generic3DPrinterCard],
  ["generic-3dprinter-card-editor", Generic3DPrinterCardEditor],
];

const defineElements = () => {
  const registry = window.customElements;
  for (const [name, element] of ELEMENTS) {
    if (registry.get(name)) continue;
    try {
      registry.define(name, element);
    } catch (err) {
      console.warn(`generic-3dprinter-card: could not define ${name}`, err);
    }
  }
};

const registryAtLoad = window.customElements;
defineElements();
const defineOnSwappedRegistry = () => {
  if (window.customElements !== registryAtLoad) defineElements();
};
registryAtLoad.whenDefined("home-assistant").then(defineOnSwappedRegistry);
// In case the root element was defined somewhere the browser's registry cannot see.
for (const delay of [1000, 5000, 15000]) window.setTimeout(defineOnSwappedRegistry, delay);

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "generic-3dprinter-card")) {
  window.customCards.push({
    type: "generic-3dprinter-card",
    name: "3D Printer",
    description:
      "Full control of a 3D printer: live camera, job, temperatures, fans, a joystick for the head, stored files, and the smart plug it runs from.",
    preview: true,
    documentationURL: "https://github.com/dnviti/ha-generic-3dprinter-controller",
  });
}

/* eslint-disable no-console */
console.info(
  `%c GENERIC-3DPRINTER-CARD %c ${CARD_VERSION} `,
  "color:#fff;background:#3f51b5;font-weight:700",
  "color:#3f51b5;background:#fff;font-weight:700",
);

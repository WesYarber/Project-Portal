// Runs the real voice recorder out of app/static/app.js against stub media
// APIs and prints what it did, as JSON.
//
// Wes, 2026-08-04: "improve the interface when recording a voice note ... so
// that it shows something responding to the audio coming in in real time,
// shows the current length of the recording, and allows for pausing,
// resuming, playback, and deleting a recorded voice note."
//
// What can go wrong is choreography, not markup: the running length must
// exclude paused stretches (and must not read 0:00 on a take that was never
// paused - the browser-verified bug this harness was written against), done
// must attach the file AND build a playback row, discard must attach nothing,
// delete must pull the file back out of the input, and the mic must be
// released on every path. Called by tests/test_recorder.py.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");

function slice(from, to, what) {
  const start = appjs.indexOf(from);
  const end = to ? appjs.indexOf(to, start) : appjs.length;
  if (start < 0 || end < 0 || end <= start) {
    throw new Error("could not find the " + what + " section in app.js");
  }
  return appjs.slice(start, end);
}

const src =
  slice("function addFiles(input, files) {", "function initDropzones() {", "file helpers") +
  slice("function initRecorder(form, input, refresh) {",
    "// --- Folds that remember", "recorder");

// --- stub DOM + media APIs ---------------------------------------------------

function makeElement(tag) {
  const el = {
    tagName: (tag || "div").toUpperCase(),
    hidden: false,
    disabled: false,
    childNodes: [],
    handlers: {},
    classes: new Set(),
    clientWidth: 300,
    clientHeight: 36,
    get textContent() {
      if (el.childNodes.length) return el.childNodes.map((c) => c.textContent).join("");
      return el._text || "";
    },
    set textContent(v) {
      el._text = v;
      el.childNodes.length = 0;
    },
    classList: {
      add: (c) => el.classes.add(c),
      remove: (c) => el.classes.delete(c),
      contains: (c) => el.classes.has(c),
    },
    appendChild(child) {
      child.parentNode = el;
      el.childNodes.push(child);
      return child;
    },
    remove() {
      if (!el.parentNode) return;
      const kids = el.parentNode.childNodes;
      const i = kids.indexOf(el);
      if (i >= 0) kids.splice(i, 1);
      el.parentNode = null;
    },
    addEventListener(type, fn) {
      (el.handlers[type] = el.handlers[type] || []).push(fn);
    },
    querySelector(sel) {
      return sel === "canvas" ? canvas : null;
    },
    getContext() {
      return null; // no waveform drawing in a stub - the meter is visual-only
    },
  };
  return el;
}

function fire(el, type) {
  (el.handlers[type] || []).forEach((fn) => fn({}));
}

const btn = makeElement("button");
btn.hidden = true;
const canvas = makeElement("canvas");
const panel = makeElement("div");
panel.hidden = true;
const shelf = makeElement("div");
const timeEl = makeElement("span");
const pauseBtn = makeElement("button");
const doneBtn = makeElement("button");
const cancelBtn = makeElement("button");
const statusEl = makeElement("span");
const submitBtns = [makeElement("button"), makeElement("button"), makeElement("button")];

const input = { files: [] };
const form = {
  querySelector(sel) {
    if (sel === "[data-record]") return btn;
    if (sel === "[data-rec-panel]") return panel;
    if (sel === "[data-rec-shelf]") return shelf;
    if (sel === "[data-rec-time]") return timeEl;
    if (sel === "[data-rec-pause]") return pauseBtn;
    if (sel === "[data-rec-done]") return doneBtn;
    if (sel === "[data-rec-cancel]") return cancelBtn;
    if (sel === "[data-attach-status]") return statusEl;
    return null;
  },
  querySelectorAll(sel) {
    return sel.indexOf("submit") >= 0 ? submitBtns : [];
  },
};

let refreshes = 0;
const refresh = () => { refreshes += 1; };

// Controllable clock. Date is only used for the memo's filename.
let now = 50000;
globalThis.performance = { now: () => now };
let intervalFn = null;
globalThis.setInterval = (fn) => { intervalFn = fn; return 1; };
globalThis.clearInterval = () => { intervalFn = null; };
globalThis.requestAnimationFrame = () => 1; // no loop: one registration, never run
globalThis.cancelAnimationFrame = () => {};
globalThis.getComputedStyle = () => ({ color: "#0f0" });
globalThis.window = { devicePixelRatio: 1 };
globalThis.document = { createElement: makeElement };
globalThis.DataTransfer = class {
  constructor() {
    this.files = [];
    this.items = { add: (f) => this.files.push(f) };
  }
};
let urls = 0;
let revoked = 0;
globalThis.URL = {
  createObjectURL: () => `blob:${++urls}`,
  revokeObjectURL: () => { revoked += 1; },
};

let stoppedTracks = 0;
function makeStream() {
  return { getTracks: () => [{ stop: () => { stoppedTracks += 1; } }] };
}

const recorders = [];
class FakeMediaRecorder {
  constructor(stream) {
    this.stream = stream;
    this.state = "inactive";
    recorders.push(this);
  }
  start() { this.state = "recording"; }
  pause() { this.state = "paused"; }
  resume() { this.state = "recording"; }
  stop() {
    this.state = "inactive";
    if (this.onstop) this.onstop();
  }
}
globalThis.MediaRecorder = FakeMediaRecorder;

let denyMic = false;
globalThis.navigator = {
  mediaDevices: {
    getUserMedia: () =>
      denyMic ? Promise.reject(new Error("denied")) : Promise.resolve(makeStream()),
  },
};

class FakeAudioContext {
  createAnalyser() {
    return { fftSize: 0, getByteTimeDomainData() {} };
  }
  createMediaStreamSource() {
    return { connect() {} };
  }
  close() {}
}
globalThis.window.AudioContext = FakeAudioContext;

const flush = () => new Promise((r) => setTimeout0(r));
const setTimeout0 = (fn) => Promise.resolve().then(fn);

// eslint-disable-next-line no-new-func
new Function(src + "; return initRecorder;")()(form, input, refresh);

const out = {};
out.buttonRevealed = btn.hidden === false;

const rec = () => recorders[recorders.length - 1];
const tick = () => { if (intervalFn) intervalFn(); };
// The row's length label lives in its <span>; textContent on the whole row
// would drag the delete button's label in with it.
const rowMeta = (row) => {
  if (!row) return "NO ROW";
  const span = row.childNodes.find((c) => c.tagName === "SPAN");
  return span ? span.textContent : "NO SPAN";
};

await (async () => {
  // --- 1. Start: panel opens, submits sleep, the clock runs ------------------
  fire(btn, "click");
  await flush(); await flush();
  out.started = {
    panelShown: !panel.hidden,
    recordingClass: btn.classes.has("recording"),
    submitsDisabled: submitBtns.every((b) => b.disabled),
    time: timeEl.textContent,
  };
  now += 5000;
  tick();
  out.after5s = timeEl.textContent;

  // --- 2. Pause holds the clock, resume restarts it --------------------------
  fire(pauseBtn, "click");
  out.pausedState = rec().state;
  out.pauseLabel = pauseBtn.textContent;
  now += 3000;
  tick();
  out.timeWhilePaused = timeEl.textContent;
  fire(pauseBtn, "click");
  now += 2000;
  tick();
  out.timeAfterResume = timeEl.textContent;

  // --- 3. Done: the take is attached and gets a playback row -----------------
  rec().ondataavailable({ data: new Blob(["audio-bytes"], { type: "audio/webm" }) });
  fire(doneBtn, "click");
  await flush(); await flush();
  const row = shelf.childNodes[0];
  out.take = {
    panelHidden: panel.hidden,
    recordingClass: btn.classes.has("recording"),
    submitsWoken: submitBtns.every((b) => !b.disabled),
    files: input.files.length,
    name: input.files.length ? input.files[0].name : "",
    micReleased: stoppedTracks,
    rowText: rowMeta(row),
    rowHasAudio: !!(row && row.childNodes.some((c) => c.tagName === "AUDIO")),
    // The recorder CLAIMS the file it just put in the input, so the staged-file
    // shelf leaves it alone. Without this the memo gets a second row down
    // there - an ordinary file row beside its own playback row - with two
    // different delete buttons for one recording.
    //
    // Asserted here rather than in the shelf's own harness because that one
    // sets the claim by hand to build its fixture, so it cannot see whether
    // the recorder ever makes one. A sweep found exactly that hole.
    claimed: Object.keys(input._recordedNames || {}),
  };

  // --- 4. Delete pulls the file back out of the input ------------------------
  const delBtn = row.childNodes.find((c) => c.tagName === "BUTTON");
  fire(delBtn, "click");
  out.afterDelete = {
    files: input.files.length,
    rows: shelf.childNodes.length,
    urlRevoked: revoked,
    // And the claim is released with it. Left behind, the name would be a
    // permanent instruction to the staged shelf to hide any file called that -
    // so re-attaching a file under the deleted memo's name would show nothing.
    claimed: Object.keys(input._recordedNames || {}),
  };

  // --- 5. Discard: nothing is attached, the mic is still released ------------
  fire(btn, "click");
  await flush(); await flush();
  rec().ondataavailable({ data: new Blob(["more"], { type: "audio/webm" }) });
  fire(cancelBtn, "click");
  out.discard = {
    files: input.files.length,
    rows: shelf.childNodes.length,
    micReleased: stoppedTracks,
    panelHidden: panel.hidden,
  };

  // --- 6. The mic button doubles as done while a take is open ----------------
  fire(btn, "click");
  await flush(); await flush();
  rec().ondataavailable({ data: new Blob(["third"], { type: "audio/webm" }) });
  now += 61000;
  tick();
  out.longTakeClock = timeEl.textContent;
  fire(btn, "click"); // second press = stop
  await flush(); await flush();
  const rows = shelf.childNodes;
  out.micToggle = {
    files: input.files.length,
    rowText: rowMeta(rows.length ? rows[rows.length - 1] : null),
  };

  // --- 7. A denied mic disables the button and says so -----------------------
  denyMic = true;
  fire(btn, "click");
  await flush(); await flush();
  out.denied = { disabled: btn.disabled, status: statusEl.textContent };
})();

console.log(JSON.stringify(out));

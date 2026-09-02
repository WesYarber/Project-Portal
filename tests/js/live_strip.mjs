// Drives the dashboard strip's poller out of app/static/app.js against a stub
// row, and prints what it painted, as JSON.
//
// The strip is hydrated by /api/active-run every five seconds, and a run's
// hold state (app/midrun.py) changes without anyone pressing a button on THIS
// page: a pause pressed on the project page or another phone, and the hold
// engaging when the run reaches its next tool call. So the poller has to
// repaint the dot, the pausing/paused badge and the pause/resume button from
// each poll - and a test of the template alone cannot see any of that.
//
// Called by tests/test_live_strip.py.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");

function slice(from, to, what) {
  const start = appjs.indexOf(from);
  const end = appjs.indexOf(to, start);
  if (start < 0 || end < 0 || end <= start) {
    throw new Error("could not find the " + what + " section in app.js");
  }
  return appjs.slice(start, end);
}

const liveRunSrc = slice(
  "function startLiveRunPoll() {",
  "// --- Reading a transcript",
  "active-run poller"
);

// --- the stub world --------------------------------------------------------

function classes(initial) {
  const set = new Set(initial);
  return {
    toggle(name, force) {
      if (force === undefined) force = !set.has(name);
      if (force) set.add(name);
      else set.delete(name);
      return force;
    },
    contains: (name) => set.has(name),
    list: () => Array.from(set).sort(),
  };
}

// One strip row as the template renders it, with only what the poller reads.
function makeRow(runId, opts) {
  const dot = { classList: classes(opts.paused ? ["dot", "held"] : ["dot", "running"]) };
  const badge = { hidden: !opts.paused, textContent: opts.engaged ? "paused" : "pausing" };
  const button = { textContent: opts.paused ? "resume" : "pause" };
  const form = opts.canPause
    ? {
        attrs: { action: "/run/" + runId + "/" + (opts.paused ? "resume" : "pause") },
        setAttribute(n, v) { this.attrs[n] = v; },
        querySelector: (sel) => (sel === "button" ? button : null),
      }
    : null;
  const activity = { textContent: "" };
  const meta = { textContent: "" };
  const row = {
    classList: classes(opts.paused ? ["live-run-row", "held"] : ["live-run-row"]),
    querySelector(sel) {
      if (sel === ".live-activity") return activity;
      if (sel === ".live-meta") return meta;
      if (sel === ".dot") return dot;
      if (sel === ".live-hold") return badge;
      if (sel === ".live-hold-form") return form;
      return null;
    },
    snapshot() {
      return {
        row: this.classList.list(),
        dot: dot.classList.list(),
        badgeHidden: badge.hidden,
        badge: badge.textContent,
        action: form ? form.attrs.action : null,
        button: form ? button.textContent : null,
        activity: activity.textContent,
      };
    },
  };
  return row;
}

let world;

function makeWorld(rows) {
  const ids = Object.keys(rows).sort().join(",");
  // The first tick answers with the same run set the strip was rendered with,
  // as the real API would: a different set means a run started or finished,
  // and the poller hands that to liveReload instead of painting anything.
  return {
    hidden: false,
    responder: () => ({ active: true, runs: [], run_ids: ids }),
    timers: new Map(),
    nextTimer: 1,
    liveReloads: 0,
    rows,
    strip: {
      getAttribute: (n) => (n === "data-run-ids" ? Object.keys(rows).sort().join(",") : null),
      querySelector(sel) {
        const m = /data-run-id="(\d+)"/.exec(sel);
        return m && rows[m[1]] ? rows[m[1]] : null;
      },
      querySelectorAll: () => [],
    },
  };
}

function load() {
  const document = {
    get hidden() { return world.hidden; },
    getElementById: (id) => (id === "live-run" ? world.strip : null),
    querySelectorAll: () => [],
    addEventListener() {},
  };
  const fetch = (url) => Promise.resolve({ json: () => Promise.resolve(world.responder(url)) });
  const setInterval = (fn, ms) => { const id = world.nextTimer++; world.timers.set(id, { fn, ms }); return id; };
  const clearInterval = (id) => { world.timers.delete(id); };
  const liveReload = () => { world.liveReloads += 1; };
  const body = liveRunSrc + "\nreturn { startLiveRunPoll: startLiveRunPoll };";
  return new Function("document", "fetch", "setInterval", "clearInterval", "liveReload", body)(
    document, fetch, setInterval, clearInterval, liveReload
  );
}

async function settle() {
  for (let i = 0; i < 8; i++) await Promise.resolve();
}

async function poll(data) {
  world.responder = () => data;
  for (const t of Array.from(world.timers.values())) t.fn();
  await settle();
}

function run(id, extra) {
  return Object.assign(
    { run_id: id, model: "fable", elapsed: "1m", events: 3, last_activity: "> Bash(ls)", paused: false, engaged: false, can_pause: true },
    extra
  );
}

const out = {};

// --- 1. A pause pressed elsewhere reaches the strip, and the hold engaging does too
{
  const rows = { 7: makeRow(7, { paused: false, engaged: false, canPause: true }) };
  world = makeWorld(rows);
  const app = load();
  app.startLiveRunPoll();
  await settle();
  out.running = rows[7].snapshot();
  await poll({ active: true, run_ids: "7", runs: [run(7, { paused: true })] });
  out.pausing = rows[7].snapshot();
  await poll({ active: true, run_ids: "7", runs: [run(7, { paused: true, engaged: true })] });
  out.paused = rows[7].snapshot();
  await poll({ active: true, run_ids: "7", runs: [run(7)] });
  out.resumed = rows[7].snapshot();
  out.liveReloads = world.liveReloads;
}

// --- 2. A run the portal cannot reach never grows a pause button from a poll
{
  const rows = { 8: makeRow(8, { paused: false, engaged: false, canPause: false }) };
  world = makeWorld(rows);
  const app = load();
  app.startLiveRunPoll();
  await settle();
  await poll({ active: true, run_ids: "8", runs: [run(8, { can_pause: false })] });
  out.unreachable = rows[8].snapshot();
}

// --- 3. Two runs: only the held one is painted held
{
  const rows = {
    9: makeRow(9, { paused: false, engaged: false, canPause: true }),
    10: makeRow(10, { paused: false, engaged: false, canPause: true }),
  };
  world = makeWorld(rows);
  const app = load();
  app.startLiveRunPoll();
  await settle();
  await poll({ active: true, run_ids: "10,9", runs: [run(9), run(10, { paused: true, engaged: true })] });
  out.twoRuns = { 9: rows[9].snapshot(), 10: rows[10].snapshot() };
}

console.log(JSON.stringify(out));

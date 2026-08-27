// Runs the real console / active-run pollers out of app/static/app.js against a
// stub DOM and a fake clock, and prints what they did, as JSON.
//
// Wes, 2026-08-13, relaying a diagnosis from his Mac: an open portal tab was
// fetching /api/run/N/log every 2 seconds forever - on a run that had finished
// hours before, in a background tab - which held one Tailscale HTTP/2
// connection open indefinitely and eventually wedged Safari's networking
// process at 99% CPU.
//
// The whole bug is about the LIFETIME of a timer, so nothing about it can be
// asserted from the source text: "clearInterval appears in app.js" would have
// been true before the fix too. What is driven here is the sequence - start,
// tick, finish, morph, hide, show - against a clock this file owns, so the
// question "is a timer still armed" has a real answer.
//
// Called by tests/test_console_poll.py.
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

const consoleSrc = slice(
  "// Exactly one console poller at a time",
  "// A transcript is read from the end",
  "console poller"
);
const liveRunSrc = slice(
  "function startLiveRunPoll() {",
  "// --- Reading a transcript",
  "active-run poller"
);

// --- the stub world --------------------------------------------------------

let world;

function makeWorld() {
  return {
    hidden: false,
    fetched: [],
    responder: () => ({ text: "", offset: 0, running: true }),
    timers: new Map(),
    nextTimer: 1,
    liveReloads: 0,
    painted: [],
    listeners: { visibilitychange: [] },
    box: null,
    strip: null,
  };
}

function makeBox(runId, live) {
  return {
    getAttribute(n) {
      if (n === "data-run-id") return runId;
      if (n === "data-live") return live ? "1" : "0";
      return null;
    },
  };
}

// The transcript <pre>. The scroll numbers are only there so the "keep it
// pinned to the bottom" arithmetic has something to read.
function makeOut() {
  return { scrollTop: 0, clientHeight: 100, scrollHeight: 100, textContent: "" };
}

function makeStrip(runIds) {
  return {
    getAttribute: (n) => (n === "data-run-ids" ? runIds : null),
    querySelector: () => null,
    querySelectorAll: () => [],
  };
}

function stubs() {
  return {
    document: {
      get hidden() {
        return world.hidden;
      },
      getElementById(id) {
        if (id === "agent-console") return world.box;
        if (id === "console-out") return world.out;
        if (id === "live-run") return world.strip;
        return null;
      },
      querySelectorAll: () => [],
      addEventListener(name, fn) {
        (world.listeners[name] || (world.listeners[name] = [])).push(fn);
      },
    },
    fetch(url) {
      world.fetched.push(url);
      const body = world.responder(url);
      return Promise.resolve({ json: () => Promise.resolve(body) });
    },
    setInterval(fn, ms) {
      const id = world.nextTimer++;
      world.timers.set(id, { fn, ms });
      return id;
    },
    clearInterval(id) {
      world.timers.delete(id);
    },
    renderConsole(out, text, replace) {
      world.painted.push({ text, replace });
    },
    liveReload() {
      world.liveReloads += 1;
    },
  };
}

// Both sections are loaded into ONE function scope, exactly as they share one
// scope in the browser - the console poller's module-level `consolePoll` has to
// be the same variable both functions see.
function load() {
  const s = stubs();
  const body =
    consoleSrc +
    "\n" +
    liveRunSrc +
    "\nreturn { startConsolePoll: startConsolePoll, startLiveRunPoll: startLiveRunPoll," +
    " stopConsolePoll: stopConsolePoll, peek: function () { return consolePoll; } };";
  return new Function(
    "document",
    "fetch",
    "setInterval",
    "clearInterval",
    "renderConsole",
    "liveReload",
    body
  )(s.document, s.fetch, s.setInterval, s.clearInterval, s.renderConsole, s.liveReload);
}

// Fire every armed interval once, then drain the microtask queue so each
// fetch's .then has actually run before anything is asserted.
async function advance() {
  for (const t of Array.from(world.timers.values())) t.fn();
  await settle();
}

async function settle() {
  for (let i = 0; i < 8; i++) await Promise.resolve();
}

function armed() {
  return Array.from(world.timers.values()).map((t) => t.ms);
}

function visibility(hidden) {
  world.hidden = hidden;
  world.listeners.visibilitychange.forEach((fn) => fn());
}

const out = {};

// --- 1. A live run polls, and STOPS the moment its run is over -------------
{
  world = makeWorld();
  world.out = makeOut();
  world.box = makeBox("12", true);
  const app = load();

  app.startConsolePoll();
  await settle();
  const afterStart = { fetches: world.fetched.length, timers: armed() };

  await advance();
  await advance();
  const whileRunning = { fetches: world.fetched.length, timers: armed() };

  // The run ends.
  world.responder = () => ({ text: "done\n", offset: 40, running: false });
  await advance();
  const atFinish = {
    fetches: world.fetched.length,
    timers: armed(),
    liveReloads: world.liveReloads,
  };

  // The thing that was broken: hours later, nothing is still fetching.
  const before = world.fetched.length;
  for (let i = 0; i < 100; i++) await advance();
  out.liveRunStops = {
    afterStart,
    whileRunning,
    atFinish,
    fetchesInTheNext100Ticks: world.fetched.length - before,
  };
}

// --- 2. A finished run arms no timer at all --------------------------------
{
  world = makeWorld();
  world.out = makeOut();
  world.box = makeBox("12", false);
  world.responder = () => ({ text: "all of it\n", offset: 9, running: false });
  const app = load();

  app.startConsolePoll();
  await settle();
  out.finishedRun = { fetches: world.fetched.length, timers: armed(), liveReloads: world.liveReloads };
}

// --- 3. A hidden tab does not poll; coming back to it polls at once --------
{
  world = makeWorld();
  world.out = makeOut();
  world.box = makeBox("12", true);
  const app = load();

  app.startConsolePoll();
  await settle();
  const beforeHiding = world.fetched.length;

  visibility(true);
  await advance();
  await advance();
  const whileHidden = world.fetched.length - beforeHiding;
  const stillArmedWhileHidden = armed();

  visibility(false);
  await settle();
  const onReturning = world.fetched.length - beforeHiding - whileHidden;

  out.hiddenTab = { whileHidden, stillArmedWhileHidden, onReturning };
}

// --- 4. reinit() calls this on every patch: it must not stack pollers ------
{
  world = makeWorld();
  world.out = makeOut();
  world.box = makeBox("12", true);
  const app = load();

  app.startConsolePoll();
  await settle();
  const fetchesAfterFirstStart = world.fetched.length;

  for (let i = 0; i < 20; i++) app.startConsolePoll();
  await settle();

  out.repeatedStarts = {
    timers: armed(),
    // Restarting would re-fetch from offset 0 and jump the reader to the
    // bottom of the transcript on every live patch.
    extraFetches: world.fetched.length - fetchesAfterFirstStart,
    visibilityListeners: world.listeners.visibilitychange.length,
  };
}

// --- 5. The box is reused, so the poller has to follow it to the new run ---
{
  world = makeWorld();
  world.out = makeOut();
  world.box = makeBox("12", true);
  const app = load();

  app.startConsolePoll();
  await settle();

  // project.html morphs #agent-console to the run that just started.
  world.box = makeBox("13", true);
  app.startConsolePoll();
  await settle();

  world.fetched = [];
  await advance();

  out.runChanged = { timers: armed(), urls: world.fetched };
}

// --- 6. A reply arriving after the poller was superseded must not paint ----
{
  world = makeWorld();
  world.out = makeOut();
  world.box = makeBox("12", true);
  let release;
  const held = new Promise((r) => (release = r));
  const app = load();

  // Run 12's fetch is still in flight when run 13 takes over.
  world.responder = () => held;
  app.startConsolePoll();
  world.box = makeBox("13", true);
  world.responder = () => ({ text: "run 13\n", offset: 7, running: true });
  app.startConsolePoll();
  await settle();

  const paintedBefore = world.painted.map((p) => p.text);
  release({ text: "run 12's stale tail\n", offset: 99, running: false });
  await settle();

  out.supersededMidFlight = {
    paintedBefore,
    paintedAfter: world.painted.map((p) => p.text),
    // Run 12 reporting running:false must not tear down run 13's poller.
    timers: armed(),
    liveReloads: world.liveReloads,
  };
}

// --- 7. The same run going live again has to re-arm the timer -------------
// Found by the sweep: guarding on the run id ALONE passed every test above.
// project.html renders #agent-console for the newest run whether or not it is
// running, so the id can stay put while data-live flips - a page rendered a
// beat before the run's status row commits is corrected by the very next
// morph, and under an id-only guard that page would never poll at all.
{
  world = makeWorld();
  world.out = makeOut();
  world.box = makeBox("12", false);
  const app = load();

  app.startConsolePoll();
  await settle();
  const whileIdle = armed();

  world.box = makeBox("12", true); // same run, now reported as running
  app.startConsolePoll();
  await settle();
  const afterGoingLive = armed();

  world.fetched = [];
  await advance();

  out.sameRunGoesLive = { whileIdle, afterGoingLive, urls: world.fetched };
}

// --- 8. The console leaving the page takes its poller with it -------------
// Also found by the sweep. #agent-console is inside a fold that the morph can
// remove outright (a project whose newest run has been deleted renders no
// console at all), and a timer whose box is gone is the original leak exactly.
{
  world = makeWorld();
  world.out = makeOut();
  world.box = makeBox("12", true);
  const app = load();

  app.startConsolePoll();
  await settle();
  const beforeItWent = armed();

  world.box = null;
  app.startConsolePoll();
  await settle();

  world.fetched = [];
  for (let i = 0; i < 10; i++) await advance();

  out.boxRemoved = { beforeItWent, timers: armed(), fetches: world.fetched.length };
}

// --- 9. The active-run poller is gated the same way ------------------------
{
  world = makeWorld();
  world.strip = makeStrip("");
  world.responder = () => ({ usage: {}, run_ids: "", active: false });
  const app = load();

  app.startLiveRunPoll();
  await settle();
  const onLoad = world.fetched.length;
  const interval = armed();

  visibility(true);
  await advance();
  await advance();
  const whileHidden = world.fetched.length - onLoad;

  visibility(false);
  await settle();
  const onReturning = world.fetched.length - onLoad - whileHidden;

  out.activeRunPoller = { onLoad, interval, whileHidden, onReturning };
}

console.log(JSON.stringify(out, null, 2));

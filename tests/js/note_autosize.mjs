// Runs the real autosize() out of app/static/app.js against a stub DOM and
// prints what it did, as JSON.
//
// String-matching the source would only prove the file says "heightCap". This
// proves the arithmetic Wes reported: that typing into a note box already at
// its cap moves neither the page nor the caret, that a box below the cap still
// grows a line at a time, and that a delete shrinks it without dragging the
// page along. Called by tests/test_note_autosize.py.
//
// The one fact the whole harness turns on: `height: auto` on a <textarea> does
// NOT mean "as tall as the content". It means the `rows` attribute. That is
// why the old measurement was destructive - it collapsed a 500px note box to
// three lines to read scrollHeight back, and the browser clamped the page
// scroll to the document that briefly left behind.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");

const start = appjs.indexOf("var SIZED_AT");
const end = appjs.indexOf('document.addEventListener("input"');
if (start < 0 || end < 0 || end <= start) {
  throw new Error("could not find the autosize section in app.js");
}
const src = appjs.slice(start, end);

// --- a stub DOM ------------------------------------------------------------

const LINE_H = 20; // one rendered line
const PAD = 16; // 0.5rem top + bottom
const BORDER = 2; // 1px each side; box-sizing is border-box everywhere here
const COLS = 40; // characters before the text wraps
const ROWS = 3; // the note box's rows="3"

// Everything on the page above and below the textarea. The page is taller than
// the window, so there is a real scroll position for a collapse to clamp.
const BASE_DOC = 4000;
const WINDOW_H = 700;

function renderedLines(value) {
  if (!value) return 1;
  let n = 0;
  for (const line of value.split("\n")) n += Math.max(1, Math.ceil(line.length / COLS));
  return n;
}

function makeBox(opts) {
  const o = opts || {};
  const cap = o.cap === undefined ? 300 : o.cap; // border-box max-height
  const minH = 37; // --control-h

  const world = { scrollY: 0, scrollX: 0 };

  const el = {
    tagName: "TEXTAREA",
    value: o.value || "",
    style: { height: "" },
    scrollTop: 0,
    writes: 0,
  };

  // Layout, as the browser would run it when a geometry property is read: the
  // used height follows from the specified height, then the document height
  // and both scroll positions are clamped to it. Those clamps are the bug.
  function layout() {
    const contentH = renderedLines(el.value) * LINE_H + PAD;
    const spec = el.style.height;
    let border;
    if (spec === "" || spec === "auto") border = ROWS * LINE_H + PAD + BORDER;
    else border = parseFloat(spec);
    if (cap) border = Math.min(border, cap);
    border = Math.max(border, minH);

    const clientHeight = border - BORDER;
    const scrollHeight = Math.max(contentH, clientHeight);
    el.scrollTop = Math.min(el.scrollTop, Math.max(0, scrollHeight - clientHeight));

    const docH = BASE_DOC + border;
    world.scrollY = Math.min(world.scrollY, Math.max(0, docH - WINDOW_H));

    return { border, clientHeight, scrollHeight };
  }

  Object.defineProperty(el, "clientHeight", { get: () => layout().clientHeight });
  Object.defineProperty(el, "scrollHeight", { get: () => layout().scrollHeight });
  Object.defineProperty(el, "offsetHeight", { get: () => layout().border });

  // Count every ASSIGNMENT to the height, not every change of it. Writing the
  // value a box already has still invalidates layout, which is the whole
  // reason the code checks before it writes - so counting only changes would
  // make that check unfalsifiable.
  let h = "";
  Object.defineProperty(el.style, "height", {
    get: () => h,
    set: (v) => {
      el.writes += 1;
      h = v;
    },
  });

  globalThis.window = {
    get scrollY() {
      layout();
      return world.scrollY;
    },
    get scrollX() {
      return world.scrollX;
    },
    scrollTo: (x, y) => {
      world.scrollX = x;
      world.scrollY = y;
      layout();
    },
    getComputedStyle: () => ({ maxHeight: cap ? cap + "px" : "none" }),
  };

  return { el, world, cap };
}

function load() {
  // eslint-disable-next-line no-new-func
  return new Function(src + "; return { autosize: autosize, heightCap: heightCap };")();
}

// The measurement as it was written before 2026-08-18, run in the same world so
// the scenes below can report what it did rather than assert it from memory.
function legacyAutosize(el) {
  el.style.height = "auto";
  el.style.height = el.scrollHeight + 2 + "px";
}

// Scroll to the bottom of the page and let layout clamp it there, the way a
// browser does. The add-note form is the last thing on a project page, so this
// is where a person typing a note actually is.
function toBottom(world) {
  world.scrollY = 1e9;
  return globalThis.window.scrollY; // the getter lays out, which clamps
}

// Put the box at its cap with a long note in it, the reader at the bottom.
// 15 wrapped lines against a 300px cap: it has been overflowing for a while.
function cappedNote(measure) {
  const box = makeBox({ value: "x".repeat(600) });
  const api = load();
  (measure || api.autosize)(box.el); // the sizing pass that got it there
  const before = toBottom(box.world);
  box.el.writes = 0;
  return { ...box, api, before };
}

// --- the scenes ------------------------------------------------------------

const scenes = {};

scenes.the_old_measurement_yanked_the_page = () => {
  const { el, world, before } = cappedNote(legacyAutosize);
  el.value += "d"; // one autocorrect-sized edit
  legacyAutosize(el);
  const after = globalThis.window.scrollY;
  return { before, after, moved: before - after, writes: el.writes };
};

scenes.at_the_cap_an_edit_moves_nothing = () => {
  const { el, api, before } = cappedNote();
  el.value += "d";
  api.autosize(el);
  return { before, after: globalThis.window.scrollY, writes: el.writes };
};

scenes.at_the_cap_a_same_length_autocorrect_moves_nothing = () => {
  const { el, api, before } = cappedNote();
  el.value = el.value.slice(0, -3) + "the"; // "teh" -> "the"
  api.autosize(el);
  return { before, after: globalThis.window.scrollY, writes: el.writes };
};

scenes.below_the_cap_a_new_line_still_grows_the_box = () => {
  // rows="3", so three lines is the floor `height: auto` reports and the box
  // only starts growing on the fourth.
  const box = makeBox({ value: "one\ntwo\nthree" });
  const api = load();
  api.autosize(box.el);
  const threeLines = box.el.style.height;
  box.el.value = "one\ntwo\nthree\nfour";
  api.autosize(box.el);
  const fourLines = box.el.style.height;
  box.el.value = "one\ntwo\nthree\nfour\nfive";
  api.autosize(box.el);
  return { threeLines, fourLines, fiveLines: box.el.style.height };
};

scenes.below_the_cap_typing_within_a_line_writes_nothing = () => {
  const box = makeBox({ value: "one line" });
  const api = load();
  api.autosize(box.el);
  box.el.writes = 0;
  box.el.value = "one liner"; // still one rendered line
  api.autosize(box.el);
  return { writes: box.el.writes, height: box.el.style.height };
};

// A delete at the cap is the one edit that HAS to collapse the box to measure
// it, so it is the one place the old hazard still exists. Both measurements
// run against the same world, and the gap between them is the over-clamp the
// scroll restore removes: the page owes the reader the two lines the box
// genuinely lost, not the twelve the collapse briefly took away.
function deleteAtTheCap(measure) {
  const box = makeBox({ value: "x".repeat(600) });
  const api = load();
  const size = measure || api.autosize;
  size(box.el);
  const before = toBottom(box.world);
  box.el.value = "x".repeat(520); // 15 wrapped lines down to 13
  size(box.el);
  return { before, after: globalThis.window.scrollY, height: box.el.style.height };
}

scenes.a_delete_at_the_cap_moves_the_page_only_by_what_it_lost = () => deleteAtTheCap();
scenes.the_old_measurement_overshot_a_delete = () => deleteAtTheCap(legacyAutosize);

scenes.a_delete_shrinks_the_box = () => {
  const box = makeBox({ value: "a\nb\nc\nd\ne\nf" });
  const api = load();
  api.autosize(box.el);
  const tall = box.el.style.height;
  box.el.value = "a\nb";
  api.autosize(box.el);
  return { tall, short: box.el.style.height };
};

scenes.the_box_never_grows_past_its_cap = () => {
  const box = makeBox({ value: "x".repeat(4000), cap: 300 });
  const api = load();
  api.autosize(box.el);
  return { height: box.el.style.height, offsetHeight: box.el.offsetHeight };
};

scenes.an_uncapped_box_still_grows_to_fit = () => {
  const box = makeBox({ value: "x".repeat(600), cap: 0 });
  const api = load();
  api.autosize(box.el);
  return { height: box.el.style.height, cap: api.heightCap(box.el) };
};

const out = {};
for (const [name, fn] of Object.entries(scenes)) out[name] = fn();
console.log(JSON.stringify(out, null, 2));

// Runs the real selBarShow() out of app/static/app.js against a stub DOM and
// prints where it decided to put the selection bar, as JSON.
//
// String-matching the source would only prove the code mentions the journal's
// right edge; this proves the arithmetic - that a wide window parks the bar in
// the gutter beside the journal and a narrow one falls back to floating over
// the selection. Called by tests/test_journal_quote.py.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");

// Slice out the positioning code: the two constants plus selBarShow itself.
const start = appjs.indexOf("var SEL_GAP");
const end = appjs.indexOf("function selBarHide");
if (start < 0 || end < 0 || end <= start) {
  throw new Error("could not find selBarShow in app.js");
}
const src = appjs.slice(start, end) + "\nreturn selBarShow;";

function run(scene) {
  const classes = new Set();
  const root = {
    hidden: true,
    style: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
    },
    // A column of stacked buttons is narrower and taller than a row of them;
    // the caller supplies both so the fallback path measures differently.
    get offsetWidth() {
      return classes.has("side") ? scene.sideW : scene.rowW;
    },
    get offsetHeight() {
      return classes.has("side") ? scene.sideH : scene.rowH;
    },
  };

  globalThis.selBarBuild = () => root;
  globalThis.window = { innerWidth: scene.winW, innerHeight: scene.winH };
  globalThis.document = {
    getElementById: (id) =>
      id === "journal" && scene.journal
        ? { getBoundingClientRect: () => scene.journal }
        : null,
  };

  const selBarShow = new Function(src)();
  selBarShow({ text: "a quoted passage", rect: scene.sel });
  return {
    side: classes.has("side"),
    left: parseFloat(root.style.left),
    top: parseFloat(root.style.top),
    hidden: root.hidden,
    quote: globalThis.selQuote,
  };
}

// A desktop monitor: the 1080px page column centred in a 1920px window leaves
// a 420px gutter each side, so the bar goes beside the journal.
const wide = run({
  winW: 1920, winH: 1080,
  journal: { left: 420, right: 1500, top: 200, bottom: 900 },
  sel: { left: 500, right: 900, top: 400, bottom: 424, width: 400, height: 24 },
  sideW: 130, sideH: 66, rowW: 240, rowH: 34,
});

// A phone: the journal card runs to the window edge, so there is no gutter and
// the bar floats above the selection as before.
const narrow = run({
  winW: 390, winH: 844,
  journal: { left: 8, right: 382, top: 100, bottom: 700 },
  sel: { left: 40, right: 300, top: 400, bottom: 424, width: 260, height: 24 },
  sideW: 130, sideH: 66, rowW: 240, rowH: 34,
});

// A selection at the very top of a wide window: still beside the journal, but
// clamped so the bar cannot hang off the top edge.
const wideTop = run({
  winW: 1920, winH: 1080,
  journal: { left: 420, right: 1500, top: -300, bottom: 900 },
  sel: { left: 500, right: 900, top: 2, bottom: 20, width: 400, height: 18 },
  sideW: 130, sideH: 66, rowW: 240, rowH: 34,
});

// A window with a gutter too thin for the stacked bar: fall back, don't spill
// off the right edge of the monitor.
const thinGutter = run({
  winW: 1180, winH: 900,
  journal: { left: 50, right: 1130, top: 100, bottom: 800 },
  sel: { left: 100, right: 400, top: 400, bottom: 424, width: 300, height: 24 },
  sideW: 130, sideH: 66, rowW: 240, rowH: 34,
});

// No room above the selection: the fallback drops below it instead of covering
// the words being quoted.
const noRoomAbove = run({
  winW: 390, winH: 844,
  journal: { left: 8, right: 382, top: 0, bottom: 700 },
  sel: { left: 40, right: 300, top: 4, bottom: 28, width: 260, height: 24 },
  sideW: 130, sideH: 66, rowW: 240, rowH: 34,
});

console.log(JSON.stringify({ wide, narrow, wideTop, thinGutter, noRoomAbove }));

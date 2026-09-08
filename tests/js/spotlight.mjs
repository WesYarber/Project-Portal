// Runs the real spotlightHashTarget() out of app/static/app.js against a stub
// document, and prints what it did, as JSON.
//
// What matters: a `#question-12` hash lights that card and scrolls to it, a
// hash naming nothing on the page does nothing, an unrelated hash (a section
// anchor like `#todos`) is left alone, and a second call moves the light
// rather than leaving two cards lit.
//
// Called by tests/test_spotlight_js.py.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");
const start = appjs.indexOf("function spotlightHashTarget()");
if (start < 0) throw new Error("spotlightHashTarget is not in app.js");
const end = appjs.indexOf("\n}", start) + 2;
const src = appjs.slice(start, end);

function element(id) {
  const classes = new Set();
  return {
    id,
    offsetWidth: 10,
    scrolled: [],
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
    },
    scrollIntoView(opts) {
      this.scrolled.push(opts);
    },
  };
}

function run(hash, ids, previouslyLit) {
  const els = {};
  ids.forEach((id) => (els[id] = element(id)));
  (previouslyLit || []).forEach((id) => els[id].classList.add("spotlight"));
  const document = {
    getElementById: (id) => els[id] || null,
    querySelectorAll: (sel) =>
      sel === ".spotlight" ? Object.values(els).filter((e) => e.classList.contains("spotlight")) : [],
  };
  const location = { hash };
  let threw = null;
  try {
    new Function("document", "location", src + "; return spotlightHashTarget();")(document, location);
  } catch (err) {
    threw = String(err && err.message);
  }
  const lit = Object.values(els).filter((e) => e.classList.contains("spotlight")).map((e) => e.id);
  const scrolled = Object.values(els).filter((e) => e.scrolled.length).map((e) => [e.id, e.scrolled[0]]);
  return { lit, scrolled, threw };
}

console.log(
  JSON.stringify({
    question: run("#question-12", ["question-12", "question-13"]),
    missing: run("#question-99", ["question-12"]),
    section: run("#todos", ["todos", "question-12"]),
    none: run("", ["question-12"]),
    moves: run("#question-13", ["question-12", "question-13"], ["question-12"]),
    proposal: run("#proposal-3", ["proposal-3"]),
  })
);

// Runs the real single-key jump handler out of app/static/app.js against a stub
// DOM and prints what it did, as JSON.
//
// String-matching the source would only prove the file mentions "scrollIntoView";
// this proves the behaviour Wes asked for - that the section's top edge is what
// gets aligned, that the cursor lands in the box, and above all that the key
// does nothing while you are typing a letter into a field. Called by
// tests/test_jump_keys.py.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");

// Slice out the jump section: the key map through the keydown listener that
// uses it, stopping before the footer-hint code (which needs a real footer).
const start = appjs.indexOf("var JUMP_KEYS");
const end = appjs.indexOf("function jumpHintSync");
if (start < 0 || end < 0 || end <= start) {
  throw new Error("could not find the jump section in app.js");
}
const src = appjs.slice(start, end);

// --- a stub DOM ------------------------------------------------------------

function makeEl(spec, world) {
  const classes = new Set();
  const el = {
    tagName: spec.tag || "DIV",
    isContentEditable: !!spec.contentEditable,
    type: spec.type,
    attrs: spec.attrs || {},
    _parent: null,
    getAttribute: (name) => (name in el.attrs ? el.attrs[name] : null),
    classList: {
      add: (c) => { classes.add(c); world.classAdds.push([spec.name, c]); },
      remove: (c) => { classes.delete(c); world.classRemovals.push([spec.name, c]); },
      contains: (c) => classes.has(c),
    },
    // Walks the stubbed parent chain, like the real Element.closest.
    closest: (sel) => {
      let node = el;
      while (node) {
        if (sel === "details" && node.tagName === "DETAILS") return node;
        node = node._parent;
      }
      return null;
    },
    scrollIntoView: (opts) => { world.scrolls.push({ name: spec.name, opts }); },
    focus: (opts) => { world.focuses.push({ name: spec.name, opts: opts || null }); },
  };
  if (spec.tag === "DETAILS") el.open = !!spec.open;
  return el;
}

function run(scene) {
  const world = { scrolls: [], focuses: [], classAdds: [], classRemovals: [], defaultPrevented: false };
  const byName = {};
  const bySelector = {};

  for (const spec of scene.elements) {
    byName[spec.name] = makeEl(spec, world);
  }
  for (const spec of scene.elements) {
    if (spec.parent) byName[spec.name]._parent = byName[spec.parent];
    if (spec.attrs && spec.attrs["data-jump"]) {
      bySelector['[data-jump="' + spec.attrs["data-jump"] + '"]'] = byName[spec.name];
    }
    if (spec.selector) bySelector[spec.selector] = byName[spec.name];
  }

  let listener = null;
  const bodyClasses = new Set(scene.bodyClasses || []);
  globalThis.window = {
    matchMedia: (q) => ({ matches: !!(scene.media && scene.media[q]) }),
  };
  globalThis.document = {
    body: { classList: { contains: (c) => bodyClasses.has(c) } },
    querySelector: (sel) => bySelector[sel] || null,
    querySelectorAll: () => [],
    getElementById: (id) =>
      id === "img-lightbox" && scene.lightbox ? { hidden: scene.lightbox.hidden } : null,
    addEventListener: (type, fn) => { if (type === "keydown") listener = fn; },
  };

  // eslint-disable-next-line no-new-func
  new Function(src)();
  if (!listener) throw new Error("the jump section registered no keydown listener");

  // The event target: either a stubbed field you are typing into, or the
  // document body (nothing focused), which is the case the keys are for.
  const target = scene.target
    ? byName[scene.target]
    : { tagName: "BODY", isContentEditable: false };

  listener({
    key: scene.key,
    ctrlKey: !!scene.ctrlKey,
    metaKey: !!scene.metaKey,
    altKey: !!scene.altKey,
    shiftKey: !!scene.shiftKey,
    target,
    preventDefault: () => { world.defaultPrevented = true; },
  });

  return {
    scrolls: world.scrolls,
    focuses: world.focuses,
    classAdds: world.classAdds,
    defaultPrevented: world.defaultPrevented,
    detailsOpen: scene.detailsName ? byName[scene.detailsName].open : null,
  };
}

// --- the scenes ------------------------------------------------------------

// A project page: the four jumpable sections, plus the note textarea the N key
// is supposed to put the cursor in.
const PROJECT_PAGE = [
  { name: "projectCard", attrs: { "data-jump": "project" } },
  { name: "todoHead", attrs: { "data-jump": "todo" } },
  { name: "journalHead", attrs: { "data-jump": "journal" } },
  {
    name: "noteHead",
    attrs: { "data-jump": "note", "data-jump-focus": ".note-form textarea[name='note']" },
  },
  { name: "noteBox", tag: "TEXTAREA", selector: ".note-form textarea[name='note']" },
];

// The dashboard: no note section at all, an idea section instead.
const DASHBOARD = [
  { name: "ideaHead", attrs: { "data-jump": "idea", "data-jump-focus": "#title" } },
  { name: "titleBox", tag: "INPUT", type: "text", selector: "#title" },
];

const scenes = {
  // N on a project page: scroll the "Add note" heading to the top, cursor in
  // the box, ring around it.
  noteOnProject: { elements: PROJECT_PAGE, key: "n" },
  // Shift is not a modifier here - Wes wrote "N", and shift is how you type it.
  shiftedNote: { elements: PROJECT_PAGE, key: "N", shiftKey: true },
  // The three navigation-only keys.
  journal: { elements: PROJECT_PAGE, key: "j" },
  todo: { elements: PROJECT_PAGE, key: "t" },
  project: { elements: PROJECT_PAGE, key: "p" },
  // N on the dashboard finds `idea` instead, and focuses the title.
  ideaOnDashboard: { elements: DASHBOARD, key: "n" },
  // J on the dashboard: no journal there, so nothing at all happens.
  journalOnDashboard: { elements: DASHBOARD, key: "j" },
  // The whole point of "when no text box is being typed into".
  whileTypingInTextarea: { elements: PROJECT_PAGE, key: "n", target: "noteBox" },
  whileTypingInInput: { elements: DASHBOARD, key: "n", target: "titleBox" },
  // Ctrl+N is a new window and Cmd+P is print. Both stay the browser's.
  ctrlN: { elements: PROJECT_PAGE, key: "n", ctrlKey: true },
  metaP: { elements: PROJECT_PAGE, key: "p", metaKey: true },
  // A key with no target of its own does nothing and, crucially, does not
  // swallow the keystroke.
  unmappedKey: { elements: PROJECT_PAGE, key: "k" },
  // Arrow keys and the like are multi-character names, never a jump.
  arrowKey: { elements: PROJECT_PAGE, key: "ArrowDown" },
  // The image viewer is modal: scrolling the page behind it is not the answer.
  lightboxOpen: { elements: PROJECT_PAGE, key: "j", lightbox: { hidden: false } },
  lightboxClosed: { elements: PROJECT_PAGE, key: "j", lightbox: { hidden: true } },
  // A folded target is unfolded rather than scrolled to an empty summary.
  foldedTarget: {
    elements: [
      { name: "fold", tag: "DETAILS", open: false },
      { name: "todoHead", attrs: { "data-jump": "todo" }, parent: "fold" },
    ],
    key: "t",
    detailsName: "fold",
  },
  // Motion preferences: both the appearance setting and the OS one drop the
  // smooth scroll.
  animOff: { elements: PROJECT_PAGE, key: "j", bodyClasses: ["anim-off"] },
  reducedMotion: {
    elements: PROJECT_PAGE,
    key: "j",
    media: { "(prefers-reduced-motion: reduce)": true },
  },
};

const out = {};
for (const [name, scene] of Object.entries(scenes)) out[name] = run(scene);
console.log(JSON.stringify(out, null, 2));

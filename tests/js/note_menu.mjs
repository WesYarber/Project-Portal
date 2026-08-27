// Runs the real press-and-hold menu on the note form's green button out of
// app/static/app.js against a stub DOM and prints what it did, as JSON.
//
// Wes, 2026-08-04: "change the 'add note' to be able to be clicked and held on
// mobile to reveal a sort of dropdown menu with the 'add and run now' and
// 'queue and don't run' options." (That second entry now reads "queue note",
// per his 2026-08-10 note.)
//
// What can go wrong is behavioral: whether the held button opens the menu,
// whether picking an entry submits THROUGH the matching alternate button (so
// its then=... value rides along), whether the click that ends the hold is
// swallowed (otherwise holding for the menu would also plain-submit the note),
// and whether a mouse or a drift arms nothing. Called by tests/test_note_menu.py.
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

const src = slice(
  "function initNoteMenu() {",
  'document.addEventListener("DOMContentLoaded", initProjectDrag);',
  "note menu",
);

// --- stub DOM ---------------------------------------------------------------

function makeElement(tag) {
  const el = {
    tagName: (tag || "span").toUpperCase(),
    nodeType: 1,
    className: "",
    childNodes: [],
    style: {},
    parentNode: null,
    handlers: {},
    attrs: {},
    get textContent() {
      if (el.childNodes.length) return el.childNodes.map((c) => c.textContent).join("");
      return el._text || "";
    },
    set textContent(v) {
      el._text = v;
      el.childNodes.length = 0;
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
    contains(node) {
      if (node === el) return true;
      return el.childNodes.some((c) => c.contains && c.contains(node));
    },
    addEventListener(type, fn) {
      (el.handlers[type] = el.handlers[type] || []).push(fn);
    },
    getBoundingClientRect() {
      return { width: 180, height: 70 };
    },
  };
  return el;
}

const docHandlers = {};
const winHandlers = {};
const body = makeElement("body");

globalThis.document = {
  createElement: makeElement,
  body,
  addEventListener(type, fn) {
    (docHandlers[type] = docHandlers[type] || []).push(fn);
  },
};
globalThis.window = {
  innerWidth: 390,
  innerHeight: 844,
  addEventListener(type, fn) {
    (winHandlers[type] = winHandlers[type] || []).push(fn);
  },
};

// Controllable time and timers, so a long press is a function call rather
// than a real 500ms.
let now = 100000;
globalThis.Date = { now: () => now };
let pendingTimer = null;
let clearedTimers = 0;
globalThis.setTimeout = (fn) => {
  pendingTimer = fn;
  return 1;
};
globalThis.clearTimeout = () => {
  pendingTimer = null;
  clearedTimers += 1;
};

// --- the note form as the template renders it -------------------------------

const submissions = [];
// The alternates the SERVER rendered, in DOM order. Both exist when the green
// button will not run on its own; on a project that can be run now the template
// drops "add & run now", and the menu has to follow it - hence the argument.
const BOTH = [
  ["add & run now", "run"],
  ["queue note", "queue"],
];
function makeForm(alts) {
  const buttons = (alts || BOTH).map(function (pair) {
    const btn = {
      name: "then",
      value: pair[1],
      textContent: "\n        " + pair[0] + "\n      ",
      clicked: 0,
      click() { btn.clicked += 1; },
    };
    return btn;
  });
  const form = {
    querySelectorAll(sel) {
      if (sel.indexOf('button[name="then"]') >= 0) return buttons;
      return [];
    },
    requestSubmit(submitter) {
      submissions.push(submitter ? submitter.value : "(none)");
    },
  };
  const goBtn = {
    closest: (sel) => (sel === "form" ? form : sel === ".note-form button.go" ? goBtn : null),
  };
  return { form, goBtn };
}

function fire(map, type, ev) {
  ev.prevented = false;
  ev.preventDefault = () => {
    ev.prevented = true;
  };
  ev.stopPropagation = ev.stopPropagation || (() => {});
  (map[type] || []).forEach((fn) => fn(ev));
  return ev;
}

const offForm = { closest: () => null };

function openMenu() {
  return body.childNodes.find((c) => c.className === "ctx-menu") || null;
}

function hold(goBtn) {
  fire(docHandlers, "pointerdown", { pointerType: "touch", target: goBtn, clientX: 200, clientY: 700 });
  if (pendingTimer) {
    const t = pendingTimer;
    pendingTimer = null;
    t();
  }
}

// eslint-disable-next-line no-new-func
new Function(src + "; initNoteMenu();")();

const out = {};

// 1. A long press on the green button opens the menu with exactly the two
//    alternates, and the click that ends the press is swallowed - it would be
//    a plain submit otherwise - without closing the menu it just opened.
{
  const { goBtn } = makeForm();
  hold(goBtn);
  const menu = openMenu();
  out.opened = menu ? menu.childNodes.map((c) => c.textContent) : null;
  const endClick = fire(docHandlers, "click", { target: goBtn });
  out.endClickSwallowed = endClick.prevented;
  out.menuSurvivedEndClick = openMenu() !== null;
  now += 5000;
  const lateClick = fire(docHandlers, "click", { target: offForm });
  out.lateClickLeftAlone = !lateClick.prevented;
  out.lateClickClosed = openMenu() === null;
}

// 2. Picking "add & run now" submits through the run button, so its
//    then=run value rides exactly as if the hidden button had been pressed.
{
  const { goBtn } = makeForm();
  hold(goBtn);
  const menu = openMenu();
  const item = menu.childNodes.find((c) => c.textContent === "add & run now");
  fire(item.handlers, "click", { target: item });
  out.runPick = { submittedVia: submissions.pop(), closed: openMenu() === null };
}

// 3. And "queue note" through the queue button.
{
  const { goBtn } = makeForm();
  hold(goBtn);
  const menu = openMenu();
  const item = menu.childNodes.find((c) => c.textContent === "queue note");
  fire(item.handlers, "click", { target: item });
  out.queuePick = { submittedVia: submissions.pop(), closed: openMenu() === null };
  now += 5000;
  fire(docHandlers, "click", { target: offForm });
}

// 3b. On a project that can be run now the template renders only "queue note",
//     because the green button runs by itself. The menu is built off the form,
//     so it offers that one entry and it still submits through the real button
//     - a hardcoded pair would offer an "add & run now" that does nothing.
{
  const { goBtn } = makeForm([["queue note", "queue"]]);
  hold(goBtn);
  const menu = openMenu();
  out.runsNowMenu = menu.childNodes.map((c) => c.textContent);
  const item = menu.childNodes[0];
  fire(item.handlers, "click", { target: item });
  out.runsNowPick = { submittedVia: submissions.pop(), closed: openMenu() === null };
  now += 5000;
  fire(docHandlers, "click", { target: offForm });
}

// 4. A mouse never arms the press: on a desktop all three buttons are visible.
{
  const { goBtn } = makeForm();
  fire(docHandlers, "pointerdown", { pointerType: "mouse", target: goBtn, clientX: 10, clientY: 10 });
  out.mouseArmed = pendingTimer !== null;
  pendingTimer = null;
}

// 5. Drifting more than a few px is a scroll, not a hold - the press cancels.
{
  const { goBtn } = makeForm();
  const before = clearedTimers;
  fire(docHandlers, "pointerdown", { pointerType: "touch", target: goBtn, clientX: 100, clientY: 100 });
  fire(docHandlers, "pointermove", { clientX: 100, clientY: 130 });
  out.driftCanceled = clearedTimers === before + 1 && pendingTimer === null;
}

// 6. Lifting the finger before the timer fires cancels it too.
{
  const { goBtn } = makeForm();
  const before = clearedTimers;
  fire(docHandlers, "pointerdown", { pointerType: "touch", target: goBtn, clientX: 100, clientY: 100 });
  fire(docHandlers, "pointerup", {});
  out.liftCanceled = clearedTimers === before + 1 && pendingTimer === null;
}

console.log(JSON.stringify(out));

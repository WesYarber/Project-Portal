// Runs the real todo context menu out of app/static/app.js against a stub DOM
// and prints what it did, as JSON.
//
// Wes, 2026-08-04: "compress the +tag, whose? and x buttons into a right click
// menu for a todo list item. Also, fold the tags into this menu, aside from
// the 'blocked' tag."
//
// String-matching for "contextmenu" would only prove the file contains the
// word. What can go wrong is behavioral: which entries the menu builds from a
// row's data attributes, what each entry posts, whether a long press opens it
// and a drift cancels it, and whether the click that ends the long press
// closes the menu it just opened. Called by tests/test_todo_menu.py.
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
  "function initTodoMenu() {",
  'document.addEventListener("DOMContentLoaded", initProjectDrag);',
  "todo menu",
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
    value: "",
    get textContent() {
      if (el.childNodes.length) return el.childNodes.map((c) => c.textContent).join("");
      return el._text || "";
    },
    set textContent(v) {
      el._text = v;
      el.childNodes.length = 0;
    },
    set innerHTML(v) {
      if (v === "") el.childNodes.length = 0;
      else throw new Error("stub innerHTML only supports clearing");
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
    setAttribute(name, value) {
      el.attrs[name] = value;
    },
    getAttribute(name) {
      return name in el.attrs ? el.attrs[name] : null;
    },
    getBoundingClientRect() {
      return { width: 200, height: 150 };
    },
    focus() {
      el.focused = true;
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
  innerWidth: 1400,
  innerHeight: 900,
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

const posted = [];
globalThis.postForm = (action, fields) => {
  posted.push({ action, fields });
};
let confirmAnswer = true;
globalThis.confirm = () => confirmAnswer;

// --- a todo row as the template renders it ----------------------------------

function makeRow(spec) {
  const list = { getAttribute: (n) => (n === "data-here" ? spec.here || "" : null) };
  const card = {
    getAttribute: (n) => (n === "data-refile" ? JSON.stringify(spec.refile || []) : null),
  };
  const row = {
    attrs: { "data-todo": String(spec.id), "data-tags": (spec.tags || []).join(",") },
    getAttribute(n) {
      return n in row.attrs ? row.attrs[n] : null;
    },
    classList: { contains: (c) => (c === "done" ? !!spec.done : false) },
    closest: (sel) => (sel === ".todo-list" ? list : sel === ".todo-card" ? card : null),
    querySelector: (sel) => (sel === ".todo-text" ? { textContent: spec.text } : null),
  };
  return row;
}

function fire(map, type, ev) {
  ev.preventDefault = ev.preventDefault || (() => {});
  ev.stopPropagation = ev.stopPropagation || (() => {});
  (map[type] || []).forEach((fn) => fn(ev));
}

function targetOn(row) {
  // ev.target.closest resolves the row for row events and nothing for others.
  return { closest: (sel) => (sel === ".todo-item[data-todo]" ? row : null) };
}

const offRow = { closest: () => null, };

function openMenu() {
  return body.childNodes.find((c) => c.className === "ctx-menu") || null;
}

function serialize(menu) {
  return menu.childNodes.map((c) => ({ cls: c.className, text: c.textContent }));
}

function clickEntry(menu, text) {
  const item = menu.childNodes.find((c) => c.textContent === text);
  if (!item) throw new Error("no menu entry: " + text);
  fire(item.handlers, "click", { target: item });
}

// eslint-disable-next-line no-new-func
new Function(src + "; initTodoMenu();")();

const out = {};
const REFILE = [
  { value: "agent", label: "the agent" },
  { value: "3", label: "Wes" },
  { value: "5", label: "Karli" },
  { value: "", label: "nobody" },
];

// 1. Right-click on a row builds the whole menu: head, tags, add-tag, move-to
//    (skipping where it already is), delete.
{
  const row = makeRow({ id: 7, text: "wire the thing up", tags: ["ui", "verify"], here: "3", refile: REFILE });
  fire(docHandlers, "contextmenu", { target: targetOn(row), clientX: 40, clientY: 60 });
  const menu = openMenu();
  out.built = menu ? serialize(menu) : null;
}

// 2. Picking a tag entry posts its removal; the menu closes.
{
  const menu = openMenu();
  clickEntry(menu, "× ui");
  out.removeTag = { posted: posted.pop(), closed: openMenu() === null };
}

// 3. Move-to posts the person value.
{
  const row = makeRow({ id: 7, text: "wire the thing up", tags: [], here: "3", refile: REFILE });
  fire(docHandlers, "contextmenu", { target: targetOn(row), clientX: 40, clientY: 60 });
  clickEntry(openMenu(), "Karli");
  out.refile = { posted: posted.pop() };
}

// 4. Delete asks first. Cancel posts nothing; confirm posts the delete.
{
  const row = makeRow({ id: 9, text: "an old one", tags: [] });
  confirmAnswer = false;
  fire(docHandlers, "contextmenu", { target: targetOn(row), clientX: 0, clientY: 0 });
  clickEntry(openMenu(), "delete...");
  out.deleteCanceled = { posted: posted.length };
  confirmAnswer = true;
  fire(docHandlers, "contextmenu", { target: targetOn(row), clientX: 0, clientY: 0 });
  clickEntry(openMenu(), "delete...");
  out.deleteConfirmed = { posted: posted.pop() };
}

// 5. The add-a-tag entry swaps the menu for an input; Enter posts the tag.
{
  const row = makeRow({ id: 7, text: "wire the thing up", tags: [], refile: [] });
  fire(docHandlers, "contextmenu", { target: targetOn(row), clientX: 0, clientY: 0 });
  const menu = openMenu();
  clickEntry(menu, "add a tag...");
  const input = menu.childNodes[1].childNodes[0];
  input.value = "  polish  ";
  fire(input.handlers, "keydown", { key: "Enter" });
  out.addTag = { posted: posted.pop(), focused: !!input.focused, closed: openMenu() === null };
}

// 6. A done row offers no add-tag and no move-to - only its tags and delete.
{
  const row = makeRow({ id: 8, text: "finished", tags: ["ui"], done: true, refile: REFILE });
  fire(docHandlers, "contextmenu", { target: targetOn(row), clientX: 0, clientY: 0 });
  out.doneRow = serialize(openMenu());
  fire(docHandlers, "keydown", { key: "Escape" });
  out.escapeClosed = openMenu() === null;
}

// 7. A long press opens the menu, and the click that ends it does not close
//    what it opened - but a later click does.
{
  const row = makeRow({ id: 7, text: "wire the thing up", tags: [], refile: [] });
  fire(docHandlers, "pointerdown", { pointerType: "touch", target: targetOn(row), clientX: 30, clientY: 30 });
  const held = pendingTimer;
  held();
  out.longPress = { opened: openMenu() !== null };
  fire(docHandlers, "click", { target: offRow });
  out.longPress.survivedItsOwnClick = openMenu() !== null;
  now += 5000;
  fire(docHandlers, "click", { target: offRow });
  out.longPress.laterClickClosed = openMenu() === null;
}

// 8. Drifting cancels the press: scrolling a list must not open menus.
{
  const row = makeRow({ id: 7, text: "wire the thing up", tags: [], refile: [] });
  const clearedBefore = clearedTimers;
  fire(docHandlers, "pointerdown", { pointerType: "touch", target: targetOn(row), clientX: 30, clientY: 30 });
  fire(docHandlers, "pointermove", { clientX: 30, clientY: 80 });
  out.drift = { canceled: clearedTimers === clearedBefore + 1, timerGone: pendingTimer === null };
}

// 9. A mouse pointerdown never arms the long press - the mouse has a real
//    right button for this.
{
  const row = makeRow({ id: 7, text: "wire the thing up", tags: [], refile: [] });
  pendingTimer = null;
  fire(docHandlers, "pointerdown", { pointerType: "mouse", target: targetOn(row), clientX: 0, clientY: 0 });
  out.mouse = { armed: pendingTimer !== null };
}

console.log(JSON.stringify(out, null, 2));

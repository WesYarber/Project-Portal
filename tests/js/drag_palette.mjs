// Runs the real initProjectDrag out of app/static/app.js against a stub DOM
// and prints what it did, as JSON.
//
// Wes, 2026-09-19: "when dragging project cards on the dashboard to change
// their status, have a sort of pop-up menu show up next to the project card
// for you to drag the project card onto to set that status rather than having
// to scroll through the page while dragging that project card to find the
// correct project status area to drop it into."
//
// Grepping app.js for "drag-palette" would only prove the file contains the
// word. What can actually go wrong is behavioral: whether the palette is
// built from the zones rather than a hardcoded list, whether it lands inside
// the viewport for a card at any edge of a long page, whether dropping on a
// chip posts the right status, whether the card's own status is offered as a
// target, and whether the palette is taken down again. Called by
// tests/test_drag_palette.py.
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

const src = slice("var dragged = null;", "// The right-click menu.", "project drag");

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
    // The palette's own size, used by the placement math. A real browser
    // measures the laid-out element; the stub reports a fixed box so the
    // clamping can be asserted against known numbers.
    box: { width: 160, height: 220 },
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
    setAttribute(name, value) {
      el.attrs[name] = value;
    },
    getAttribute(name) {
      return name in el.attrs ? el.attrs[name] : null;
    },
    getBoundingClientRect() {
      return { left: 0, top: 0, right: el.box.width, bottom: el.box.height, ...el.box };
    },
    querySelectorAll(sel) {
      if (sel !== "[data-palette-status]") throw new Error("stub querySelectorAll: " + sel);
      return el.childNodes.filter((c) => c.getAttribute("data-palette-status"));
    },
    classList: {
      add(c) {
        if (!el.className.split(" ").includes(c)) el.className = (el.className + " " + c).trim();
      },
      remove(c) {
        el.className = el.className.split(" ").filter((x) => x && x !== c).join(" ");
      },
      contains: (c) => el.className.split(" ").includes(c),
    },
  };
  return el;
}

const body = makeElement("body");
body.classList.add = function (c) {
  if (!body.className.split(" ").includes(c)) body.className = (body.className + " " + c).trim();
};

// --- the dashboard as the template renders it -------------------------------

const ZONES = [
  ["active", "active"],
  ["review", "review"],
  ["paused", "paused"],
  ["backlog", "backlog"],
  ["done", "done"],
];

function makeZone(status, label) {
  const z = makeElement("div");
  z.setAttribute("data-status-zone", status);
  z.setAttribute("data-zone-label", label);
  return z;
}

function makeCell(slug, status, rect) {
  const c = makeElement("a");
  c.setAttribute("data-slug", slug);
  c.setAttribute("data-status", status);
  c.setAttribute("href", "/project/" + slug);
  c.getBoundingClientRect = () => rect;
  return c;
}

const zones = ZONES.map((p) => makeZone(p[0], p[1]));
const cells = [
  // Roomy middle of the screen: the palette should sit to its right.
  makeCell("metronome", "active", { left: 200, top: 300, right: 500, bottom: 460, width: 300, height: 160 }),
  // Hard against the right edge: it must flip to the left rather than run off.
  makeCell("cork", "review", { left: 1100, top: 40, right: 1380, bottom: 200, width: 280, height: 160 }),
  // Near the bottom: the vertical position must be clamped into view.
  makeCell("shop", "done", { left: 200, top: 820, right: 500, bottom: 890, width: 300, height: 70 }),
];

globalThis.document = {
  createElement: makeElement,
  body,
  querySelectorAll(sel) {
    if (sel === "[data-status-zone]") return zones;
    if (sel === ".project-cell[data-slug]") return cells;
    throw new Error("stub querySelectorAll: " + sel);
  },
  addEventListener() {},
};
globalThis.window = { innerWidth: 1400, innerHeight: 900, addEventListener() {} };

const posted = [];
globalThis.postForm = (action, fields) => {
  posted.push({ action, fields });
};

// eslint-disable-next-line no-new-func
new Function(src + "; initProjectDrag();")();

// --- driving it -------------------------------------------------------------

function fire(el, type, ev) {
  const dt = { setData(k, v) { dt.data = { key: k, value: v }; } };
  const e = Object.assign({ preventDefault() { e.defaulted = true; }, dataTransfer: dt }, ev || {});
  (el.handlers[type] || []).forEach((fn) => fn(e));
  return e;
}

function palette() {
  return body.childNodes.find((c) => c.className === "drag-palette") || null;
}

function chips(p) {
  return p.childNodes
    .filter((c) => c.getAttribute("data-palette-status"))
    .map((c) => ({ status: c.getAttribute("data-palette-status"), text: c.textContent, cls: c.className }));
}

function chip(p, status) {
  const c = p.childNodes.find((x) => x.getAttribute("data-palette-status") === status);
  if (!c) throw new Error("no chip for " + status);
  return c;
}

const out = {};

// 1. Dragging a card opens a palette listing every zone, labeled as the zone
//    labels itself, with the card's own status present but marked current.
{
  fire(cells[0], "dragstart", {});
  const p = palette();
  out.opened = p ? chips(p) : null;
  out.head = p ? p.childNodes[0].textContent : null;
  out.placedMiddle = p ? { left: p.style.left, top: p.style.top } : null;
}

// 2. A chip is a live drop target: dragover takes the default (which is what
//    makes a drop possible at all) and lights up; the drop posts the status.
{
  const p = palette();
  const target = chip(p, "review");
  const over = fire(target, "dragover", {});
  out.dragoverDefaulted = over.defaulted === true;
  out.dragoverLit = target.classList.contains("drop-ready");
  fire(target, "dragleave", {});
  out.dragleaveUnlit = target.classList.contains("drop-ready") === false;
  fire(target, "drop", {});
  out.dropPosted = posted.slice();
  out.closedOnDrop = palette() === null;
}

// 3. Dropping on the chip for where the card already is posts nothing.
{
  posted.length = 0;
  fire(cells[0], "dragstart", {});
  const p = palette();
  const here = chip(p, "active");
  const over = fire(here, "dragover", {});
  out.currentChipRefusesDragover = over.defaulted !== true;
  fire(here, "drop", {});
  out.currentChipPosted = posted.slice();
  fire(cells[0], "dragend", {});
  out.closedOnDragend = palette() === null;
}

// 4. The original zones still work - the palette is an addition, not a
//    replacement, and dropping a card on the section it is already in is
//    still the same no-op.
{
  posted.length = 0;
  fire(cells[0], "dragstart", {});
  fire(zones[1], "dragover", {});
  out.zoneLit = zones[1].classList.contains("drop-ready");
  fire(zones[1], "drop", {});
  fire(zones[0], "drop", {});
  out.zonePosted = posted.slice();
  fire(cells[0], "dragend", {});
}

// 5. Placement: a card at the right edge flips the palette to its left, and a
//    card at the bottom is clamped so the whole palette stays on screen.
{
  fire(cells[1], "dragstart", {});
  out.placedRightEdge = { left: palette().style.left, top: palette().style.top };
  fire(cells[1], "dragend", {});

  fire(cells[2], "dragstart", {});
  out.placedBottom = { left: palette().style.left, top: palette().style.top };
  fire(cells[2], "dragend", {});
}

// 6. A second dragstart without an intervening dragend (a drag begun while a
//    stale palette is up) leaves exactly one palette on the page.
{
  fire(cells[0], "dragstart", {});
  fire(cells[1], "dragstart", {});
  out.paletteCount = body.childNodes.filter((c) => c.className === "drag-palette").length;
  fire(cells[1], "dragend", {});
  out.allClosed = body.childNodes.filter((c) => c.className === "drag-palette").length;
}

out.viewport = { width: window.innerWidth, height: window.innerHeight };
out.paletteBox = { width: 160, height: 220 };
console.log(JSON.stringify(out, null, 2));

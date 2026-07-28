// Runs the REAL live-refresh morph out of app/static/app.js against a stub DOM
// and prints what survived a patch, as JSON.
//
// Wes, 2026-07-28: "the theme setting is not sticking when I pick it."
//
// It was not the save. The settings page patches itself in place every couple
// of seconds off /api/version, and `findMatch` decided what pairs with what by
// id BEFORE it considered the themed-dropdown wrapper. enhanceSelect moves the
// real <select> inside a <div class="sel">, so the id the server renders is a
// child's, not the wrapper's - the id branch found no sibling match, returned
// null, and morphChildren treated the incoming select as a brand-new node and
// deleted the widget holding the pick. Every appearance select has an id, so
// this fired on the theme dropdown every time; the ones without an id (the
// activity filters) took the fallback path and were preserved, which is why it
// went unnoticed for so long.
//
// A source assertion would only prove the file contains the word "sel". This
// runs morphChildren for real and reads the value back off the live tree.
//
// Called by tests/test_theme_sticks.py.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");

// The morph section, verbatim, from the keep-node predicate through to the end
// of morphChildren. Sliced rather than copied so this fails when it drifts.
function slice(from, to, what) {
  const start = appjs.indexOf(from);
  const end = appjs.indexOf(to, start);
  if (start < 0 || end < 0 || end <= start) {
    throw new Error("could not find the " + what + " section in app.js");
  }
  return appjs.slice(start, end);
}

const morphSrc = slice("var MORPH_KEEP", "// True while a patch would", "morph");

// --- A stub DOM, exactly as wide as the morph actually reaches -------------

class Node {
  constructor(tagName, attrs = {}) {
    this.nodeType = 1;
    this.tagName = tagName;
    this.childNodes = [];
    this.parentNode = null;
    this._attrs = new Map();
    for (const [k, v] of Object.entries(attrs)) this._attrs.set(k, String(v));
  }
  get id() { return this._attrs.get("id") || ""; }
  get className() { return this._attrs.get("class") || ""; }
  get classList() {
    const self = this;
    return {
      contains(c) { return self.className.split(/\s+/).includes(c); },
    };
  }
  get attributes() {
    return [...this._attrs].map(([name, value]) => ({ name, value }));
  }
  hasAttribute(n) { return this._attrs.has(n); }
  getAttribute(n) { return this._attrs.has(n) ? this._attrs.get(n) : null; }
  setAttribute(n, v) { this._attrs.set(n, String(v)); }
  removeAttribute(n) { this._attrs.delete(n); }
  // MORPH_KEEP is a selector for script-owned nodes; nothing in these scenes
  // is one, and saying so is the honest stub.
  matches() { return false; }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const kids = this.parentNode.childNodes;
    return kids[kids.indexOf(this) + 1] || null;
  }
  append(...kids) {
    for (const k of kids) { k.parentNode = this; this.childNodes.push(k); }
    return this;
  }
  insertBefore(node, ref) {
    if (node.parentNode) {
      const old = node.parentNode.childNodes;
      old.splice(old.indexOf(node), 1);
    }
    node.parentNode = this;
    const at = ref ? this.childNodes.indexOf(ref) : this.childNodes.length;
    this.childNodes.splice(at < 0 ? this.childNodes.length : at, 0, node);
    return node;
  }
  removeChild(node) {
    this.childNodes.splice(this.childNodes.indexOf(node), 1);
    node.parentNode = null;
    return node;
  }
  replaceChild(fresh, old) {
    const at = this.childNodes.indexOf(old);
    if (fresh.parentNode) {
      const prev = fresh.parentNode.childNodes;
      prev.splice(prev.indexOf(fresh), 1);
    }
    fresh.parentNode = this;
    this.childNodes[at] = fresh;
    old.parentNode = null;
    return old;
  }
  querySelector(sel) {
    for (const k of this.childNodes) {
      if (k.nodeType !== 1) continue;
      if (k.tagName === sel.toUpperCase()) return k;
      const deep = k.querySelector(sel);
      if (deep) return deep;
    }
    return null;
  }
}

class Option extends Node {
  constructor(value, label) {
    super("OPTION", { value });
    this.value = value;
    this.textContent = label || value;
    this.disabled = false;
  }
}

class Select extends Node {
  constructor(id, values, selected) {
    super("SELECT", { id, name: id });
    this.name = id;
    this.disabled = false;
    this.dataset = {};
    this.options = values.map((v) => new Option(v));
    this._value = selected;
  }
  get value() { return this._value; }
  set value(v) { this._value = v; }
  get selectedIndex() { return this.options.findIndex((o) => o.value === this._value); }
}

// The live DOM as enhanceSelect leaves it: the real select hidden inside a
// wrapper it built, with the visible widget beside it. Structure only - the
// real one also builds a trigger label, an option list and its listeners, none
// of which the morph ever looks at.
function enhanced(select) {
  const wrap = new Node("DIV", { class: "sel" });
  const trigger = new Node("BUTTON", { class: "sel-trigger" });
  trigger.textContent = select.value;
  const menu = new Node("UL", { class: "sel-menu" });
  wrap.append(select, trigger, menu);
  return wrap;
}

// What morphNode calls when it decides the widget has to be rebuilt. The real
// one wraps a select that is already in the tree, so this has to as well - a
// stub that just returned a detached wrapper would let the rebuild scenes pass
// while the page lost the control.
function enhanceSelectStub(sel) {
  const parent = sel.parentNode;
  const wrap = new Node("DIV", { class: "sel" });
  parent.insertBefore(wrap, sel);
  wrap.append(sel);
  wrap.append(new Node("BUTTON", { class: "sel-trigger" }), new Node("UL", { class: "sel-menu" }));
  return wrap;
}

// --- The scenes ------------------------------------------------------------

const morph = new Function(
  "enhanceSelect",
  morphSrc + "; return { findMatch: findMatch, morphChildren: morphChildren };"
)(enhanceSelectStub);

function themeField(inner) {
  const field = new Node("DIV", { class: "field theme-field" });
  const label = new Node("LABEL", { for: "ui_theme" });
  field.append(label, inner);
  return field;
}

// What the server sends: a plain <select id="ui_theme"> showing the SAVED value.
function serverRender(saved) {
  return themeField(new Select("ui_theme", ["terminal", "paper", "meadow"], saved));
}

function run() {
  const out = {};

  // 1. The bug, and the fix. The user has picked "paper" but not saved; the
  //    server still renders "terminal". After the patch the pick must still be
  //    there, and it must still be the same widget (the trigger the user is
  //    looking at, not a fresh one).
  {
    const live = new Node("DIV");
    const sel = new Select("ui_theme", ["terminal", "paper", "meadow"], "terminal");
    const wrap = enhanced(sel);
    live.append(themeField(wrap).childNodes[0], wrap);
    const liveField = new Node("DIV", { class: "field theme-field" });
    liveField.append(new Node("LABEL", { for: "ui_theme" }), wrap);

    sel.value = "paper"; // the pick, unsaved
    morph.morphChildren(liveField, serverRender("terminal"));

    const after = liveField.querySelector("select");
    out.pickSurvivesAPatch = after ? after.value : null;
    out.widgetWasKept = liveField.childNodes[1] === wrap;
    out.stillWrapped = liveField.childNodes[1].classList.contains("sel");
  }

  // 2. findMatch pairs the incoming select with the wrapper holding it. This is
  //    the exact line that was returning null.
  {
    const sel = new Select("ui_theme", ["terminal", "paper"], "terminal");
    const wrap = enhanced(sel);
    const holder = new Node("DIV");
    holder.append(wrap);
    const incoming = new Select("ui_theme", ["terminal", "paper"], "terminal");
    out.matchesWrapperById = morph.findMatch(wrap, incoming) === wrap;
  }

  // 3. An id that genuinely names nothing here still finds no match - the fix
  //    must not make findMatch pair a select with any wrapper it meets.
  {
    const sel = new Select("ui_font", ["mono", "sans"], "mono");
    const wrap = enhanced(sel);
    const incoming = new Select("ui_theme", ["terminal", "paper"], "terminal");
    out.aDifferentIdDoesNotMatch = morph.findMatch(wrap, incoming) === null;
  }

  // 4. The behavior the id branch exists for is untouched: a reordered card
  //    with an id is still found among later siblings and keeps its node.
  {
    const a = new Node("DIV", { id: "p-a" });
    const b = new Node("DIV", { id: "p-b" });
    const holder = new Node("DIV");
    holder.append(a, b);
    const incoming = new Node("DIV", { id: "p-b" });
    out.reorderedCardStillFoundById = morph.findMatch(a, incoming) === b;
  }

  // 5. When the option list genuinely changes the widget SHOULD be rebuilt -
  //    otherwise a renamed theme leaves a menu offering something that no
  //    longer exists. The pick is carried across only if it is still on offer.
  {
    const liveField = new Node("DIV", { class: "field theme-field" });
    const sel = new Select("ui_theme", ["terminal", "paper"], "terminal");
    const wrap = enhanced(sel);
    liveField.append(new Node("LABEL", { for: "ui_theme" }), wrap);
    sel.value = "paper";

    const next = themeField(new Select("ui_theme", ["terminal", "paper", "meadow"], "terminal"));
    morph.morphChildren(liveField, next);

    const after = liveField.querySelector("select") || liveField.childNodes[1];
    out.rebuiltOnANewOptionList = after.options.length === 3;
    out.pickCarriedAcrossTheRebuild = after.value === "paper";
  }

  // 6. A pick the new list no longer offers falls back to what the server says,
  //    rather than leaving the control holding a value it cannot submit.
  {
    const liveField = new Node("DIV", { class: "field theme-field" });
    const sel = new Select("ui_theme", ["terminal", "paper"], "terminal");
    const wrap = enhanced(sel);
    liveField.append(new Node("LABEL", { for: "ui_theme" }), wrap);
    sel.value = "paper"; // about to be retired server-side

    morph.morphChildren(liveField, themeField(new Select("ui_theme", ["terminal", "meadow"], "terminal")));
    const after = liveField.querySelector("select") || liveField.childNodes[1];
    out.aRetiredPickFallsBack = after.value === "terminal";
  }

  return out;
}

process.stdout.write(JSON.stringify(run(), null, 2));

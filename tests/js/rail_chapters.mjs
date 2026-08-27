// Runs the real chapter-list builder out of app/static/app.js against a stub
// DOM and prints what it produced, as JSON.
//
// Wes, 2026-08-01: "I want the option to jump to sections from the side-bar
// even if they are not sections I can jump to with a hot-key."
//
// String-matching for "h2" in app.js would only prove the file contains the
// word. This proves the behavior: that a bare <h2> nobody annotated becomes a
// chapter, that it lands in the right place relative to the annotated ones,
// that a badge riding inside a heading is not part of its name, and that the
// anchor built for a nameless heading actually holds the node it points at -
// which is the only way a chapter with no `data-jump` can be clicked.
//
// Called by tests/test_rail_chapters.py.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");

function slice(from, to, what) {
  const start = appjs.indexOf(from);
  const end = to ? appjs.indexOf(to) : appjs.length;
  if (start < 0 || end < 0 || end <= start) {
    throw new Error("could not find the " + what + " section in app.js");
  }
  return appjs.slice(start, end);
}

// chapterKey, chapterLabel and railChapters, which are one unit: the label and
// the key are both properties of the same row.
const src = slice(
  "function chapterKey(name)",
  "// Delegated, so it survives the rebuild",
  "chapter list",
);

// ---------------------------------------------------------------------------
// A stub DOM. Only what railChapters actually touches - enough of a tree to
// have a document order, an attribute lookup and an ancestor walk.
// ---------------------------------------------------------------------------

function text(value) {
  return { nodeType: 3, textContent: value, className: undefined };
}

function el(tag, spec = {}) {
  const node = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    attrs: Object.assign({}, spec.attrs),
    className: spec.className || "",
    children: [],
    parent: null,
    hidden: false,
    href: "",
    getAttribute(name) {
      return name in this.attrs ? this.attrs[name] : null;
    },
    setAttribute(name, value) {
      this.attrs[name] = String(value);
    },
    appendChild(child) {
      child.parent = this;
      this.children.push(child);
      return child;
    },
    closest(sel) {
      let at = this;
      while (at) {
        if (matches(at, sel)) return at;
        at = at.parent;
      }
      return null;
    },
    querySelectorAll(sel) {
      const parts = sel.split(",").map((s) => s.trim());
      const out = [];
      walk(this, (node) => {
        if (node !== this && parts.some((p) => matches(node, p))) out.push(node);
      });
      return out;
    },
    querySelector(sel) {
      return this.querySelectorAll(sel)[0] || null;
    },
  };
  Object.defineProperty(node, "childNodes", { get() { return this.children; } });
  Object.defineProperty(node, "textContent", {
    get() {
      return this.children.map((c) => c.textContent || "").join("");
    },
    set(value) {
      this.children = [];
      if (value) this.appendChild(text(value));
    },
  });
  (spec.children || []).forEach((c) => node.appendChild(c));
  if (spec.text) node.appendChild(text(spec.text));
  return node;
}

function matches(node, sel) {
  if (node.nodeType !== 1) return false;
  if (sel.startsWith("[") && sel.endsWith("]")) {
    return node.getAttribute(sel.slice(1, -1)) !== null;
  }
  if (sel.startsWith("#")) return node.attrs.id === sel.slice(1);
  // `details.fold-section` - a tag and a class together, which is what the
  // real selector uses to pick the portal's collapsible sections out of every
  // other <details> on a page (the file tree's folders are <details> too).
  const dot = sel.indexOf(".", 1);
  if (dot > 0) {
    return matches(node, sel.slice(0, dot)) && matches(node, sel.slice(dot));
  }
  if (sel.startsWith(".")) {
    return (" " + node.className + " ").indexOf(" " + sel.slice(1) + " ") !== -1;
  }
  return node.tagName === sel.toUpperCase();
}

function walk(node, fn) {
  fn(node);
  (node.children || []).forEach((child) => walk(child, fn));
}

// ---------------------------------------------------------------------------
// The scenes, each a content body as one of the portal's real pages builds it.
// ---------------------------------------------------------------------------

const SCENES = {
  // /activity as it stands today: four headings and not one annotation. Before
  // this change its chapter list was empty, which is the whole complaint.
  activity: () => [
    el("h1", { text: "Activity" }),
    el("h2", { text: "Last 14 days" }),
    el("h2", { text: "By project" }),
    el("h2", { text: "Prompt size" }),
    el("h2", { text: "Runs" }),
  ],

  // A project page: annotated targets and bare headings interleaved, which is
  // the case that has to come out in document order rather than in two blocks.
  project: () => [
    el("details", { attrs: { "data-jump": "ask", "data-jump-label": "Ask" }, text: "a whole form" }),
    el("h2", {
      className: "work-summary-head",
      attrs: { "data-jump": "summary", "data-jump-label": "Since you last looked" },
      text: "Since you last looked",
    }),
    el("div", {
      attrs: { "data-jump": "project", "data-jump-label": "Overview" },
      children: [el("h2", { text: "a heading inside the card" })],
    }),
    el("h2", { attrs: { "data-jump": "todo" }, text: "Todo" }),
    el("h2", { attrs: { "data-jump": "journal" }, text: "Journal" }),
    el("div", { attrs: { "data-jump": "journal-box", "data-jump-nav": "off" }, text: "the box" }),
  ],

  // A heading that carries a live count. The count is a fact about the page,
  // not part of the section's name.
  badges: () => [
    el("h2", {
      children: [text("phone push "), el("span", { className: "badge warn", text: "none enrolled" })],
    }),
    el("h2", { text: "where the portal answers" }),
  ],

  // One chapter is a link, not a table of contents.
  lonely: () => [el("h2", { text: "Open questions" })],

  // A heading long enough to need cutting, so the cut is pinned somewhere.
  wordy: () => [
    el("h2", { text: "What we've learned about each person" }),
    el("h2", { text: "Revisions" }),
  ],

  // Wes, 2026-08-01: "add additional click points for Agent console, sub
  // projects (only if there are some), and files". Those three are <details
  // class="fold-section">, not headings, so before this they listed nothing.
  //
  // The summary of one is "<label> <a sentence of muted detail>", and only the
  // label is the chapter's name; a fold with no children opts out with
  // data-jump-nav="off"; and a heading INSIDE a fold is that fold's content
  // ("Uploaded" and "Workspace" live in "Files"), not a chapter beside it.
  folds: () => [
    el("h2", { attrs: { "data-jump": "questions", "data-jump-label": "Questions" }, text: "Questions" }),
    el("details", {
      className: "fold-section console-details",
      attrs: { id: "agent-console-details" },
      children: [
        el("summary", {
          children: [
            el("span", { className: "fold-section-label", text: "Agent console" }),
            el("span", { className: "muted small", text: "last run - 3 minutes ago" }),
          ],
        }),
        el("pre", { text: "> Bash(pytest)" }),
        // A heading inside a fold that carries NO data-jump of its own. This is
        // the case the "Files" one below cannot test: Files declares
        // data-jump="files", so a heading inside it is already dropped by the
        // ancestor-target rule one branch earlier, and the fold rule is never
        // reached. A delete-the-fix sweep found exactly that on 2026-08-01 -
        // removing the rule broke no test.
        // Short on purpose: a label over 28 characters is cut to a word
        // boundary and an ellipsis, so a test asserting the full string is
        // absent passes whether the rule works or not.
        el("h2", { text: "Inside a fold" }),
      ],
    }),
    el("details", {
      className: "fold-section",
      attrs: { id: "subprojects", "data-jump-nav": "off" },
      children: [
        el("summary", {
          children: [el("span", { className: "fold-section-label", text: "Sub-projects" })],
        }),
      ],
    }),
    el("details", {
      className: "fold-section files-block",
      attrs: { id: "files", "data-jump": "files", "data-jump-label": "Files" },
      children: [
        el("summary", {
          children: [el("span", { className: "fold-section-label", text: "Files" })],
        }),
        el("h3", { className: "files-sub", text: "Uploaded" }),
        el("h2", { text: "a heading somebody put inside the fold" }),
      ],
    }),
  ],

  // The dashboard. Wes, 2026-08-01: "When on the dashboard, the left tab bar
  // should not show the recent activity entries as individual items as it
  // currently does." Every agent progress entry opens with an `##` heading, so
  // the feed rendered a dozen <h2>s of other projects' journal text.
  dashboard: () => [
    el("h2", { attrs: { "data-jump": "idea" }, text: "New idea" }),
    el("h2", { text: "Recent activity" }),
    el("div", {
      className: "card",
      children: [
        el("div", {
          className: "journal-entry",
          children: [
            el("div", {
              className: "content",
              children: [
                el("h2", { text: "A nav rail down the side" }),
                el("h2", { text: "1. The usage tab" }),
              ],
            }),
          ],
        }),
      ],
    }),
  ],
};

function run(name) {
  const slot = el("nav", { attrs: { id: "rail-chapters" } });
  slot.hidden = true;
  const content = el("div", { className: "terminal-body", children: SCENES[name]() });
  const root = el("body", { children: [slot, content] });

  globalThis.document = {
    getElementById: (id) => (id === "rail-chapters" ? slot : null),
    querySelector: (sel) => (matches(content, sel) ? content : null),
    createElement: (tag) => el(tag),
  };
  // The real bindings ride on <body data-jump-keys>; the two that matter here
  // are a target that has a letter and a target that does not.
  globalThis.JUMP_KEYS = { t: ["todo"], j: ["journal", "journal-box"] };

  // eslint-disable-next-line no-new-func
  const railChapters = new Function(src + "; return railChapters;")();
  railChapters();

  const list = slot.children.find((c) => c.tagName === "UL");
  return {
    hidden: slot.hidden,
    head: slot.children.length ? slot.children[0].textContent : "",
    chapters: (list ? list.children : []).map((li) => {
      const a = li.children[0];
      const key = a.children.find((c) => c.className === "rail-chapter-key");
      const label = a.children.find((c) => c.className === "rail-name");
      return {
        label: label ? label.textContent : "",
        key: key ? key.textContent : "",
        jumpTo: a.getAttribute("data-jump-to"),
        // The node the anchor will actually scroll to, identified by its own
        // text - proving the link is wired to the heading and not to nothing.
        target: a._chapterEl ? a._chapterEl.textContent : null,
        targetTag: a._chapterEl ? a._chapterEl.tagName : null,
      };
    }),
  };
}

const out = {};
Object.keys(SCENES).forEach((name) => {
  out[name] = run(name);
});
console.log(JSON.stringify(out, null, 2));

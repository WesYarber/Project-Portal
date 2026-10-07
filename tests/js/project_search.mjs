// Runs the real dashboard search (projectSearchMatches + projectSearchApply)
// out of app/static/app.js against a stub dashboard, and prints what it did to
// the shelves as JSON.
//
// What matters: every word has to match, in any order; only a card's title,
// slug, description and parent are searched (never its badges); a shut shelf
// with a match opens and one with none is hidden along with its heading; and
// clearing the field puts every shelf back exactly as it was.
//
// Called by tests/test_project_search.py.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");
const start = appjs.indexOf("function projectSearchMatches(");
const end = appjs.indexOf("function initProjectSearch(");
if (start < 0 || end < 0 || end <= start) throw new Error("the project search is not in app.js");
const src = appjs.slice(start, end);

function el(attrs, extra) {
  const a = { ...(attrs || {}) };
  return Object.assign(
    {
      tagName: "DIV",
      hidden: false,
      getAttribute: (n) => (n in a ? a[n] : null),
      hasAttribute: (n) => n in a,
      setAttribute: (n, v) => (a[n] = String(v)),
      removeAttribute: (n) => delete a[n],
    },
    extra || {}
  );
}

function card(title, slug, desc, parent) {
  const kids = {};
  if (desc) kids[".cell-desc"] = { textContent: desc };
  if (parent) kids[".cell-parent"] = { textContent: "↳ " + parent };
  // The badges are in the card's text but must never be searched.
  return el(
    { "data-title": title, "data-slug": slug, href: "/project/" + slug },
    { textContent: title + " active agent working", querySelector: (s) => kids[s] || null }
  );
}

function zone(name, tag, cards, open) {
  const empty = cards.length ? [] : [el({}, { className: "zone-empty" })];
  const z = el(
    { "data-status-zone": name },
    {
      tagName: tag,
      open: !!open,
      cards,
      empty,
      querySelectorAll: (s) => (s === ".project-cell" ? cards : s === ".zone-empty" ? empty : []),
    }
  );
  return z;
}

function board(bare) {
  const zones = bare ? [zone("active", "DIV", [])] : [
    zone("active", "DIV", [
      card("Project Portal", "project-portal", "The portal itself"),
      card("ProxyTable", "proxytable", "Play MTG with proxies"),
    ]),
    zone("review", "DIV", [card("KvK Planner", "kvk-planner", "Kingshot scheduling", "Alliance tools")]),
    zone("paused", "DETAILS", [], true),
    zone("backlog", "DETAILS", [card("Card Case", "commander-case", "A deck box to print")], false),
    zone("done", "DETAILS", [card("Old Thing", "old-thing", "")], false),
  ];
  const heads = {};
  ["active", "review"].forEach((n) => (heads[n] = el({ "data-shelf-head": n })));
  const input = { value: "" };
  const count = el({}, { textContent: "", hidden: true });
  const none = el({}, { hidden: true });
  const byId = { "project-search": input, "project-search-count": count, "project-search-none": none };
  const document = {
    getElementById: (id) => byId[id] || null,
    querySelectorAll: (s) => (s === "[data-status-zone]" ? zones : []),
    querySelector: (s) => {
      const m = /^\[data-shelf-head="(.*)"\]$/.exec(s);
      return m ? heads[m[1]] || null : null;
    },
  };
  const api = new Function(
    "document",
    src + "; return { apply: projectSearchApply, matches: projectSearchMatches };"
  )(document);
  return { zones, heads, input, count, none, api };
}

// What a person would see: per shelf, whether it shows, whether it is open,
// and which cards in it are visible.
function snapshot(b, first) {
  const shelves = {};
  b.zones.forEach((z) => {
    shelves[z.getAttribute("data-status-zone")] = {
      hidden: z.hidden,
      open: z.tagName === "DETAILS" ? z.open : null,
      was: z.getAttribute("data-search-was"),
      cards: z.cards.filter((c) => !c.hidden).map((c) => c.getAttribute("data-slug")),
      emptyHidden: z.empty.map((e) => e.hidden),
    };
  });
  return {
    shelves,
    heads: Object.fromEntries(Object.entries(b.heads).map(([k, v]) => [k, v.hidden])),
    count: b.count.hidden ? null : b.count.textContent,
    none: !b.none.hidden,
    first: first ? first.getAttribute("data-slug") : null,
  };
}

function search(q, b) {
  b = b || board();
  b.input.value = q;
  return snapshot(b, b.api.apply());
}

const out = {};
out.empty = search("");
out.oneShutShelf = search("card");
{
  const b = board();
  search("card", b);
  out.cleared = search("", b);
}
out.anyOrder = search("CASE card");
out.allWords = search("case zebra");
out.bySlug = search("commander");
out.byDescription = search("proxies");
out.byParent = search("alliance");
out.notBadges = search("agent");
out.spaces = search("   ");
// One shelf holding a match and a miss, and two matches in page order.
out.partial = search("portal");
out.twoMatches = search("pro");
// Typed a letter at a time: the second keystroke must not overwrite what the
// first one parked about each shelf, or clearing restores the search's state.
{
  const b = board();
  search("c", b);
  search("ca", b);
  search("caz", b);
  out.typedThenCleared = search("", b);
}
out.bareBoard = search("", board(true));
out.matches = {
  words: b0().matches("portal proj", "Project Portal"),
  missing: b0().matches("portal x", "Project Portal"),
  blank: b0().matches("", "anything"),
};
function b0() {
  return board().api;
}

console.log(JSON.stringify(out));

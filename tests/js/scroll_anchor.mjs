// Runs the real scroll-anchoring code out of app/static/app.js against a stub
// DOM and prints what it did, as JSON.
//
// String-matching the source would only prove the file says "holdAnchor". This
// proves the arithmetic Wes actually asked about: that content inserted ABOVE
// the viewport does not move the line he is reading, that content inserted
// below moves nothing at all, and that a second correction converges instead of
// doubling. Called by tests/test_scroll_anchor.py.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");

// Slice out the anchoring section: the scroll selector through holdEverything.
const start = appjs.indexOf("var SCROLL_SEL");
const end = appjs.indexOf("// Per-element enhancers");
if (start < 0 || end < 0 || end <= start) {
  throw new Error("could not find the anchoring section in app.js");
}
const src = appjs.slice(start, end);

// --- a stub DOM ------------------------------------------------------------
//
// The world is a flat list of boxes, each with a document-space `y` and a
// height. A viewport scroll is one number; an element's client rect is its
// document position minus that number, which is exactly the relationship the
// real thing has. That is enough to make the arithmetic real: inserting
// content above the viewport is `y += n` on everything below it.

function makeWorld() {
  const world = {
    scrollY: 0,
    innerHeight: 800,
    nodes: [],
    detached: new Set(),
  };

  function el(spec) {
    const node = {
      id: spec.id || "",
      y: spec.y,
      h: spec.h,
      position: spec.position || "static",
      children: [],
      parentElement: null,
      scrollTop: 0,
      clientHeight: spec.clientHeight || 0,
      scrollHeight: spec.scrollHeight || 0,
      getBoundingClientRect() {
        // A fixed element is placed against the viewport, so scrolling does
        // not move it - which is precisely why it is useless as an anchor.
        if (node.position === "fixed") {
          return { top: node.y, bottom: node.y + node.h, height: node.h };
        }
        // Inside a scrolling panel the panel's own scrollTop shifts its
        // children as well as the page scroll does.
        let offset = world.scrollY;
        for (let p = node.parentElement; p; p = p.parentElement) {
          offset += p.scrollTop;
        }
        return {
          top: node.y - offset,
          bottom: node.y + node.h - offset,
          height: node.h,
        };
      },
    };
    world.nodes.push(node);
    (spec.children || []).forEach((c) => {
      const child = el(c);
      child.parentElement = node;
      node.children.push(child);
    });
    return node;
  }

  world.build = el;
  return world;
}

function install(world, root, scrollers) {
  globalThis.window = {
    scrollY: world.scrollY,
    innerHeight: world.innerHeight,
    getComputedStyle: (node) => ({ position: node.position }),
    scrollBy: (_x, dy) => {
      world.scrollY += dy;
      globalThis.window.scrollY = world.scrollY;
    },
    requestAnimationFrame: null,
  };
  globalThis.document = {
    body: root,
    documentElement: { clientHeight: world.innerHeight },
    contains: (node) => !world.detached.has(node),
    querySelectorAll: () => scrollers,
  };
}

function load() {
  // eslint-disable-next-line no-new-func
  return new Function(
    src +
      "; return { viewAnchor: viewAnchor, holdAnchor: holdAnchor," +
      " anchorNode: anchorNode, snapshotScrolls: snapshotScrolls," +
      " restoreScrolls: restoreScrolls, holdEverything: holdEverything };"
  )();
}

// --- the scenes ------------------------------------------------------------

const scenes = {};

// A page: a pinned header, then three stacked cards. The viewport is 800 tall
// and the reader is 1000px down, so card two's top edge is the line in view.
function page() {
  const world = makeWorld();
  const root = world.build({
    id: "body",
    y: 0,
    h: 3000,
    children: [
      { id: "header", y: 0, h: 60, position: "fixed" },
      { id: "banner", y: 60, h: 400 },
      { id: "card-one", y: 460, h: 600 },
      {
        id: "card-two",
        y: 1060,
        h: 900,
        children: [
          { id: "para-a", y: 1060, h: 40 },
          { id: "para-b", y: 1100, h: 860 },
        ],
      },
      { id: "card-three", y: 1960, h: 1040 },
    ],
  });
  return { world, root };
}

function grow(world, aboveY, by) {
  world.nodes.forEach((n) => {
    if (n.y >= aboveY) n.y += by;
    else if (n.y + n.h > aboveY) n.h += by;
  });
}

scenes.growth_above_the_view_does_not_move_the_reader = () => {
  const { world, root } = page();
  world.scrollY = 1000;
  install(world, root, []);
  const api = load();

  const anchor = api.viewAnchor(null);
  const anchoredOn = anchor.chain[0].el.id;

  // A summary banner arrives above everything the reader can see.
  grow(world, 60, 300);
  api.holdAnchor(anchor);

  return { anchoredOn, scrollY: world.scrollY, chain: anchor.chain.map((l) => l.el.id) };
};

scenes.growth_below_the_view_moves_nothing = () => {
  const { world, root } = page();
  world.scrollY = 1000;
  install(world, root, []);
  const api = load();

  const anchor = api.viewAnchor(null);
  grow(world, 2500, 500); // a new journal entry far below the fold
  api.holdAnchor(anchor);

  return { scrollY: world.scrollY };
};

scenes.holding_twice_converges = () => {
  const { world, root } = page();
  world.scrollY = 1000;
  install(world, root, []);
  const api = load();

  const anchor = api.viewAnchor(null);
  grow(world, 60, 300);
  api.holdAnchor(anchor);
  const once = world.scrollY;
  api.holdAnchor(anchor); // the requestAnimationFrame second pass
  return { once, twice: world.scrollY };
};

scenes.the_anchor_is_a_leaf_not_the_card_around_it = () => {
  // Scrolled so the reader is part-way into card-two: the anchor must be the
  // paragraph in view, not the card, whose own top edge is off screen above.
  const { world, root } = page();
  world.scrollY = 1100;
  install(world, root, []);
  const api = load();
  const anchor = api.viewAnchor(null);
  return { chain: anchor.chain.map((l) => l.el.id) };
};

scenes.a_replaced_anchor_falls_back_to_its_ancestor = () => {
  const { world, root } = page();
  world.scrollY = 1100;
  install(world, root, []);
  const api = load();

  const anchor = api.viewAnchor(null);
  const leaf = anchor.chain[0].el;
  world.detached.add(leaf); // the patch rebuilt that paragraph outright
  grow(world, 60, 300);
  api.holdAnchor(anchor);

  return { leaf: leaf.id, fellBackTo: anchor.chain[1].el.id, scrollY: world.scrollY };
};

scenes.at_the_top_there_is_no_anchor = () => {
  const { world, root } = page();
  world.scrollY = 0;
  install(world, root, []);
  const api = load();
  return { anchor: api.viewAnchor(null) };
};

scenes.a_pinned_header_is_never_the_anchor = () => {
  const { world, root } = page();
  // Scrolled so the fixed header (top 0, height 60) is the first thing the
  // walk meets. Anchoring on it would report moved === 0 forever.
  world.scrollY = 500;
  install(world, root, []);
  const api = load();
  const anchor = api.viewAnchor(null);
  grow(world, 60, 300);
  api.holdAnchor(anchor);
  return { anchoredOn: anchor.chain[0].el.id, scrollY: world.scrollY };
};

// A journal box that scrolls inside itself, with an entry added at its top.
scenes.an_inner_panel_holds_its_own_place = () => {
  const world = makeWorld();
  const box = world.build({
    id: "journal-box",
    y: 200,
    h: 600,
    children: [
      { id: "entry-1", y: 200, h: 400 },
      { id: "entry-2", y: 600, h: 400 },
      { id: "entry-3", y: 1000, h: 400 },
    ],
  });
  box.clientHeight = 600;
  box.scrollHeight = 1200;
  box.scrollTop = 400; // reading entry-2
  world.scrollY = 0;
  install(world, box, [box]);
  const api = load();

  const saved = api.snapshotScrolls();
  const anchoredOn = saved[0].anchor.chain[0].el.id;

  // A new entry lands at the top of the box, pushing everything down.
  world.nodes.forEach((n) => {
    if (n.id.startsWith("entry")) n.y += 350;
  });
  box.scrollHeight += 350;

  api.restoreScrolls(saved);
  api.holdEverything(null, saved);

  return { anchoredOn, scrollTop: box.scrollTop };
};

scenes.a_panel_pinned_to_its_bottom_stays_pinned = () => {
  const world = makeWorld();
  const box = world.build({ id: "console-out", y: 0, h: 400, children: [] });
  box.clientHeight = 400;
  box.scrollHeight = 1000;
  box.scrollTop = 600; // exactly at the bottom
  install(world, box, [box]);
  const api = load();

  const saved = api.snapshotScrolls();
  box.scrollHeight = 1400; // more output arrived
  api.restoreScrolls(saved);

  return { atBottom: saved[0].atBottom, scrollTop: box.scrollTop };
};

scenes.a_vanished_panel_does_not_get_another_panels_place = () => {
  const world = makeWorld();
  const a = world.build({ id: "box-a", y: 0, h: 200, children: [] });
  const b = world.build({ id: "box-b", y: 300, h: 200, children: [] });
  a.clientHeight = b.clientHeight = 200;
  a.scrollHeight = b.scrollHeight = 900;
  a.scrollTop = 500;
  b.scrollTop = 0;
  install(world, a, [a, b]);
  const api = load();

  const saved = api.snapshotScrolls();
  world.detached.add(a); // the project's journal box went away with the patch
  api.restoreScrolls(saved);

  // The index-matching version handed a's 500 to b here.
  return { boxB: b.scrollTop };
};

const name = process.argv[3];
if (name) {
  console.log(JSON.stringify(scenes[name]()));
} else {
  const out = {};
  for (const key of Object.keys(scenes)) out[key] = scenes[key]();
  console.log(JSON.stringify(out));
}

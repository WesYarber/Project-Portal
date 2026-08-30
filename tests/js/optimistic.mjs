// Runs the real [data-optimistic] machinery out of app/static/app.js against a
// stub DOM and prints what it did, as JSON.
//
// Wes, 2026-08-29: "Apply UI actions on the client immediately instead of
// waiting on the server and a reload - acknowledge on the 'since you last
// checked in' banner, add note, run agent - and let the next real page load
// correct any mismatch."
//
// Everything here is about WHEN, so nothing can be string-matched. The three
// questions this harness exists to answer:
//
//   1. Did the page change before the POST came back? (that is the feature)
//   2. Did the POST still carry the right payload? (the ordering trap: the
//      note effect empties the very textarea the payload is built from)
//   3. Does a refusal put the page back? (a button reading "agent running..."
//      over an alert saying the run did not start is worse than the old wait)
//
// Sliced in file order and registered against one stub document, exactly as the
// browser would, because the busy guard and the poster have to see the press in
// that order for any of this to be reached at all.
//
// Called by tests/test_optimistic.py.
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

const busySrc = slice("// --- One press, one action", "// Ctrl/Cmd+Enter", "busy guard");
const optimisticSrc = slice(
  "// --- Showing the result before the server confirms it",
  "// --- Acting on a row without leaving the page",
  "optimistic effects"
);
const inplaceSrc = slice(
  "// Opt-in per form (`data-inplace`)",
  "// --- Attachments: drop, paste, record",
  "in-place submit"
);
const postFormSrc = slice(
  "// Returns a promise that settles when the action is DONE",
  "// Which zone means",
  "postForm"
);
const liveReloadSrc = slice("function liveReload(force) {", "function initLiveRefresh", "liveReload");

// A probe, appended rather than sliced: `pressStartedAt` is a module-level var
// inside the function this bundle runs in, and nothing outside can see it. It
// is what connects markBusy() to pressBlocked() far below in the file, and
// without a way to read it "the clock is never started" is a change no test can
// see - the hold has its own tests, but they set the clock themselves.
const SRC = [busySrc, optimisticSrc, inplaceSrc, postFormSrc, liveReloadSrc].join("\n") +
  "\n;globalThis.readPressStartedAt = function () { return pressStartedAt; };";

// --- a small DOM ------------------------------------------------------------
// Enough of a node to express the three shapes of change under test: an
// attribute going on and coming off, a label and a disabled flag being swapped,
// and a node being inserted at the FRONT of a list and taken out again.
//
// `innerHTML` is a write-only trap rather than a real property. echoNote() puts
// a person's typed text back onto the page without it passing the server's
// markdown renderer, so "was innerHTML ever assigned" is a question this
// harness has to be able to answer with evidence rather than by reading the
// source.
let world;

function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    attrs: {},
    children: [],
    parent: null,
    className: "",
    textContent: "",
    disabled: false,
    value: "",
    name: "",
    getAttribute(n) { return n in node.attrs ? node.attrs[n] : null; },
    hasAttribute(n) { return n in node.attrs; },
    setAttribute(n, v) { node.attrs[n] = String(v); },
    removeAttribute(n) { delete node.attrs[n]; },
    appendChild(child) {
      child.parent = node;
      node.children.push(child);
      return child;
    },
    insertBefore(child, ref) {
      child.parent = node;
      const at = ref ? node.children.indexOf(ref) : -1;
      if (at < 0) node.children.push(child);
      else node.children.splice(at, 0, child);
      return child;
    },
    remove() {
      if (!node.parent) return;
      const at = node.parent.children.indexOf(node);
      if (at >= 0) node.parent.children.splice(at, 1);
      node.parent = null;
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    matches: () => false,
    contains: () => false,
  };
  Object.defineProperty(node, "firstChild", {
    get: () => node.children[0] || null,
  });
  Object.defineProperty(node, "innerHTML", {
    get: () => "",
    set: (v) => { world.innerHTMLWrites.push(v); },
  });
  return node;
}

// How a node reads once the dust has settled: enough to tell an echo apart from
// the real thing, and to see the marker class the morph is expected to strip.
function snap(node) {
  if (!node) return null;
  return {
    tag: node.tagName,
    className: node.className,
    text: node.textContent,
    children: node.children.map(snap),
  };
}

// --- the scenes' furniture --------------------------------------------------

function makeButton(spec) {
  const b = makeNode("button");
  b.name = spec.name || "";
  b.value = spec.value === undefined ? "" : spec.value;
  b.textContent = spec.label || "";
  b.disabled = !!spec.disabled;
  if (spec.optimisticLabel) b.attrs["data-optimistic-label"] = spec.optimisticLabel;
  return b;
}

function makeForm(spec) {
  const form = makeNode("form");
  form.attrs.action = spec.action || "/project/portal/note";
  if (spec.inplace !== false) form.attrs["data-inplace"] = "";
  if (spec.compose) form.attrs["data-compose"] = "";
  if (spec.optimistic) form.attrs["data-optimistic"] = spec.optimistic;
  if (spec.target) form.attrs["data-optimistic-target"] = spec.target;
  form.fields = spec.fields || {};

  const box = spec.noteText === undefined ? null : makeNode("textarea");
  if (box) {
    box.name = "note";
    box.value = spec.noteText;
  }
  form.noteBox = box;

  form.matches = (sel) => {
    if (sel === "form[data-inplace]") return "data-inplace" in form.attrs;
    if (sel === "[data-compose]") return "data-compose" in form.attrs;
    return false;
  };
  form.querySelector = (sel) => {
    if (sel === "textarea[name='note']") return box;
    // isMultipartForm() and the missing-submitter fallback. Neither is what
    // this harness is about, but both are on the path to it.
    if (sel === 'input[type="file"]') return null;
    if (sel === "[formaction]") return null;
    return null;
  };
  form.querySelectorAll = (sel) => {
    // clearComposeForm()'s sweep, which runs on the SUCCESS path only. The
    // textarea is answered here so a scene can prove the box stays empty after
    // a post that worked, rather than being refilled by anything downstream.
    if (sel === "textarea, input[type=text]") return box ? [box] : [];
    if (sel === "[data-busy]") {
      return (spec.buttons || []).filter((b) => b.hasAttribute("data-busy"));
    }
    return [];
  };
  world.forms.push(form);
  return form;
}

let submitListeners;

function dispatchSubmit(form, submitter) {
  const ev = {
    type: "submit",
    target: form,
    submitter: submitter || null,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
  };
  submitListeners.forEach((fn) => fn(ev));
  return ev;
}

function build(scene) {
  world = {
    posted: [],
    alerts: [],
    patched: 0,
    forms: [],
    innerHTMLWrites: [],
    releasePost: null,
  };
  submitListeners = [];

  globalThis.alert = (m) => { world.alerts.push(m); };

  // The two nodes the effects reach for by id/selector. A scene that leaves one
  // out is testing what happens when the page does not have it - an empty
  // project renders no journal box at all.
  const banner = scene.banner === false ? null : makeNode("div");
  if (banner) banner.attrs.id = "work-summary";
  const feed = scene.feed === false ? null : makeNode("div");
  if (feed) {
    feed.attrs.id = "journal";
    // One entry already in the feed, so "prepended" can be told apart from
    // "appended" - the journal is newest-first.
    const existing = makeNode("div");
    existing.className = "journal-entry from-agent";
    existing.textContent = "an older entry";
    feed.appendChild(existing);
  }

  globalThis.document = {
    addEventListener(type, fn) { if (type === "submit") submitListeners.push(fn); },
    createElement: (tag) => makeNode(tag),
    getElementById: (id) => (id === "journal" ? feed : null),
    querySelector: (sel) => {
      if (sel === "#work-summary") return banner;
      if (sel === "form[data-busy]") {
        return world.forms.filter((f) => f.hasAttribute("data-busy"))[0] || null;
      }
      return null;
    },
    querySelectorAll: (sel) => {
      if (sel !== "form[data-busy]") return [];
      return world.forms.filter((f) => f.hasAttribute("data-busy"));
    },
    body: { tagName: "BODY" },
    activeElement: null,
  };
  globalThis.window = {
    addEventListener() {},
    fetch: true,
    DOMParser: function () {},
    FormData: true,
  };
  globalThis.URLSearchParams = URLSearchParams;
  globalThis.liveRefreshNow = () => {
    world.patched += 1;
    return Promise.resolve();
  };
  globalThis.FormData = class {
    constructor(form) {
      // Built from the LIVE fields, which is the whole point: a note effect
      // that ran too early would be visible here as an empty `note`.
      this.pairs = Object.keys(form.fields).map((k) => [k, form.fields[k]]);
      if (form.noteBox) this.pairs.push(["note", form.noteBox.value]);
    }
    forEach(cb) { this.pairs.forEach(([k, v]) => cb(v, k)); }
    set(k, v) {
      const at = this.pairs.findIndex(([name]) => name === k);
      if (at >= 0) this.pairs[at] = [k, v];
      else this.pairs.push([k, v]);
    }
  };

  globalThis.fetch = (action, opts) => {
    world.posted.push({ action, body: opts.body });
    // The portal restarts itself after it modifies its own source, so a POST
    // landing in that window does not come back with a status at all - it
    // rejects. That is a refusal as far as the page is concerned.
    if (scene.networkDown) return Promise.reject(new Error("connection refused"));
    const answer = {
      ok: scene.serverSaysNo !== true,
      json: () => Promise.resolve({ detail: "a run is already in flight" }),
    };
    // A scene may hold the POST open, which is how "the page changed BEFORE the
    // server answered" is measured rather than assumed.
    if (!scene.holdPost) return Promise.resolve(answer);
    return new Promise((resolve) => { world.releasePost = () => resolve(answer); });
  };

  world.banner = banner;
  world.feed = feed;

  // eslint-disable-next-line no-new-func
  new Function(SRC)();
}

// Let every already-resolved promise in the chain run.
async function settle() {
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
}

const out = {};

// 1. Acknowledging folds the banner away on the PRESS, not two round trips
//    later - and the fold survives the patch that follows.
{
  build({ holdPost: true });
  const form = makeForm({
    action: "/project/portal/acknowledge",
    optimistic: "hide",
    target: "#work-summary",
  });
  dispatchSubmit(form, makeButton({ label: "acknowledged" }));
  await settle();
  const beforeTheServerAnswered = world.banner.hasAttribute("hidden");
  const postedYet = world.posted.length;
  world.releasePost();
  await settle();
  out.acknowledge = {
    hiddenBeforeTheServerAnswered: beforeTheServerAnswered,
    postedYet,
    hiddenAfter: world.banner.hasAttribute("hidden"),
    patched: world.patched,
    alerts: world.alerts,
  };
}

// 2. A refused acknowledge puts the banner back. The route is not one that
//    refuses today, but the undo is one mechanism for all three effects and
//    the run route certainly does.
{
  build({ serverSaysNo: true });
  const form = makeForm({
    action: "/project/portal/acknowledge",
    optimistic: "hide",
    target: "#work-summary",
  });
  dispatchSubmit(form, makeButton({ label: "acknowledged" }));
  await settle();
  out.acknowledgeRefused = {
    hiddenAfter: world.banner.hasAttribute("hidden"),
    alerts: world.alerts,
    patched: world.patched,
    // The busy mark has to come off too, or the button is dead until reload.
    stillBusy: form.hasAttribute("data-busy"),
  };
}

// 3. A banner the server already rendered hidden is left alone: there is
//    nothing to hide, so there must be nothing to un-hide on a refusal either.
//    Without the guard, a refused post would REVEAL a banner nobody had seen.
{
  build({ serverSaysNo: true });
  world.banner.setAttribute("hidden", "");
  const form = makeForm({
    action: "/project/portal/acknowledge",
    optimistic: "hide",
    target: "#work-summary",
  });
  dispatchSubmit(form, makeButton({ label: "acknowledged" }));
  await settle();
  out.acknowledgeAlreadyHidden = { hiddenAfter: world.banner.hasAttribute("hidden") };
}

// 4. The note: the box empties and the note appears at the top of the journal
//    on the press - and the POST still carries the text. That last one is the
//    ordering trap. Run the effect a line earlier and `new FormData(form)`
//    reads the box it just cleared, so every note posts blank while the page
//    shows it going out perfectly.
{
  build({ holdPost: true });
  const form = makeForm({
    action: "/project/portal/note",
    optimistic: "note",
    compose: true,
    noteText: "the printer jammed again",
    fields: {},
  });
  dispatchSubmit(form, makeButton({ label: "add note", name: "then", value: "" }));
  await settle();
  const body = String(world.posted[0].body);
  out.note = {
    boxEmptiedBeforeTheServerAnswered: form.noteBox.value === "",
    postedBody: body,
    postCarriesTheText: body.indexOf("the+printer+jammed+again") >= 0,
    feedBeforeTheServerAnswered: snap(world.feed),
    // Prepended, not appended: the journal is newest-first.
    echoIsFirst: world.feed.children[0].className.indexOf("optimistic-echo") >= 0,
    innerHTMLWrites: world.innerHTMLWrites,
  };
  world.releasePost();
  await settle();
  out.note.boxAfter = form.noteBox.value;
  out.note.patched = world.patched;
}

// 5. A refused note gives back what was typed and takes the echo off the feed.
//    Losing a note he had typed would be the worst failure this feature could
//    have, so it is checked with the text itself rather than with a length.
{
  build({ serverSaysNo: true });
  const form = makeForm({
    action: "/project/portal/note",
    optimistic: "note",
    compose: true,
    noteText: "the printer jammed again",
  });
  dispatchSubmit(form, makeButton({ label: "add note" }));
  await settle();
  out.noteRefused = {
    boxAfter: form.noteBox.value,
    feedAfter: snap(world.feed),
    entries: world.feed.children.length,
    alerts: world.alerts,
  };
}

// 6. A note is a person's typed text going back onto the page WITHOUT passing
//    the server's markdown renderer, which makes this the one place in app.js
//    where a note could inject markup into its own project page.
{
  build({ holdPost: true });
  const nasty = '<img src=x onerror="alert(1)">';
  const form = makeForm({
    action: "/project/portal/note",
    optimistic: "note",
    compose: true,
    noteText: nasty,
  });
  dispatchSubmit(form, makeButton({ label: "add note" }));
  await settle();
  const echo = world.feed.children[0];
  const content = echo.children.filter((c) => c.className === "content")[0];
  out.noteMarkup = {
    sent: nasty,
    asText: content ? content.textContent : null,
    innerHTMLWrites: world.innerHTMLWrites,
  };
}

// 7. An empty box (or one holding only whitespace) is not a note. Nothing is
//    echoed, and there is nothing to give back on a refusal.
//     The post is held open on purpose. Let it succeed and clearComposeForm()
//     empties the box on the way through, which would make "the effect did
//     nothing" indistinguishable from "the effect ran" - the scene would pass
//     either way.
{
  build({ holdPost: true });
  const form = makeForm({
    action: "/project/portal/note",
    optimistic: "note",
    compose: true,
    noteText: "   \n  ",
  });
  dispatchSubmit(form, makeButton({ label: "add note" }));
  await settle();
  out.noteBlank = {
    entries: world.feed.children.length,
    // Left exactly as it was: with no effect there is no undo either, and the
    // success path's own clearComposeForm is what empties it later.
    boxBeforeTheServerAnswered: form.noteBox.value,
  };
}

// 8. No journal box on the page at all - an empty project renders none. The box
//    must still empty, and the missing feed must not take the whole effect (or
//    the post) down with it.
//     Held open for the same reason as the scene above: on the success path
//     clearComposeForm() empties the box anyway, so only a held post can show
//     that the effect itself survived the missing feed.
{
  build({ feed: false, holdPost: true });
  const form = makeForm({
    action: "/project/portal/note",
    optimistic: "note",
    compose: true,
    noteText: "first note on a new project",
  });
  dispatchSubmit(form, makeButton({ label: "add note" }));
  await settle();
  out.noteNoFeed = {
    boxEmptiedBeforeTheServerAnswered: form.noteBox.value === "",
    posted: world.posted.length,
    alerts: world.alerts,
  };
}

// 9. Starting a run relabels and disables the button on the press - and the
//    POST still carries the pressed button's own name and value, which is the
//    same ordering trap as the note's. `then` on the note form is the whole
//    difference between "add & run now" and a note that quietly queues.
{
  build({ holdPost: true });
  const button = makeButton({
    label: "run agent now",
    name: "then",
    value: "run",
    optimisticLabel: "agent running...",
  });
  const form = makeForm({ action: "/project/portal/run", optimistic: "pending" });
  dispatchSubmit(form, button);
  await settle();
  out.run = {
    labelBeforeTheServerAnswered: button.textContent,
    disabledBeforeTheServerAnswered: button.disabled,
    postedBody: String(world.posted[0].body),
  };
  world.releasePost();
  await settle();
  out.run.labelAfter = button.textContent;
}

// 10. The run route refuses when one is already in flight. The alert says so,
//     so the button must not be left claiming an agent is running.
{
  build({ serverSaysNo: true });
  const button = makeButton({
    label: "run agent now",
    name: "then",
    value: "run",
    optimisticLabel: "agent running...",
  });
  const form = makeForm({ action: "/project/portal/run", optimistic: "pending" });
  dispatchSubmit(form, button);
  await settle();
  out.runRefused = {
    label: button.textContent,
    disabled: button.disabled,
    alerts: world.alerts,
  };
}

// 11. Safari before 15.4 names no submitter. The effect needs one to know which
//     button to relabel, so it does nothing - and the post still goes.
{
  build({});
  const form = makeForm({ action: "/project/portal/run", optimistic: "pending" });
  dispatchSubmit(form, null);
  await settle();
  out.runNoSubmitter = { posted: world.posted.length, alerts: world.alerts };
}

// 12. An ordinary in-place form carries no data-optimistic and must be
//     untouched by any of this - a ticked todo, a swept list, a deleted file.
{
  build({});
  const form = makeForm({ action: "/todo/7/toggle", fields: { done: "1" } });
  dispatchSubmit(form, makeButton({ label: "tick" }));
  await settle();
  out.plainForm = {
    posted: world.posted.length,
    patched: world.patched,
    bannerHidden: world.banner.hasAttribute("hidden"),
    entries: world.feed.children.length,
  };
}

// 13. A pending form whose button declares no label. There is nothing to say,
//     so the button must be left exactly as it is - blanking it and disabling
//     it would leave a dead, wordless control on the page.
{
  build({ holdPost: true });
  const button = makeButton({ label: "run agent now", name: "then", value: "run" });
  const form = makeForm({ action: "/project/portal/run", optimistic: "pending" });
  dispatchSubmit(form, button);
  await settle();
  out.pendingNoLabel = {
    label: button.textContent,
    disabled: button.disabled,
    posted: world.posted.length,
  };
}

// 14. The portal restarts itself after it modifies its own source, so a POST
//     sent in that window never comes back with a status - it rejects. The page
//     is then showing a change that certainly did not happen, on a server that
//     is not there to correct it.
{
  build({ networkDown: true });
  const form = makeForm({
    action: "/project/portal/acknowledge",
    optimistic: "hide",
    target: "#work-summary",
  });
  dispatchSubmit(form, makeButton({ label: "acknowledged" }));
  await settle();
  out.serverGone = {
    hiddenAfter: world.banner.hasAttribute("hidden"),
    alerts: world.alerts,
    stillBusy: form.hasAttribute("data-busy"),
  };
}

// 15. The same, with a note - because this is the case where a rejection loses
//     something the reader cannot get back by pressing again.
{
  build({ networkDown: true });
  const form = makeForm({
    action: "/project/portal/note",
    optimistic: "note",
    compose: true,
    noteText: "the printer jammed again",
  });
  dispatchSubmit(form, makeButton({ label: "add note" }));
  await settle();
  out.serverGoneNote = {
    boxAfter: form.noteBox.value,
    entries: world.feed.children.length,
    alerts: world.alerts,
  };
}

// 16. The clock behind the press hold. markBusy() stamps it and pressBlocked()
//     reads it; they are three thousand lines apart in the file and nothing
//     else connects them, so this is what stops the stamp from being deleted
//     without a single test going red.
{
  build({ holdPost: true });
  const before = globalThis.readPressStartedAt();
  const form = makeForm({ action: "/todo/7/toggle", fields: { done: "1" } });
  dispatchSubmit(form, makeButton({ label: "tick" }));
  await settle();
  const after = globalThis.readPressStartedAt();
  out.pressClock = {
    beforeAnyPress: before,
    startedOnThePress: after > 0,
    // Stamped from the same clock pressBlocked() measures against, so an
    // implausible age here would mean the hold never expires or never applies.
    ageMs: after > 0 ? Date.now() - after : null,
  };
}

process.stdout.write(JSON.stringify(out, null, 2));

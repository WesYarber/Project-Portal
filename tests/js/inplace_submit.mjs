// Runs the real [data-inplace] submit machinery out of app/static/app.js
// against a stub DOM and prints what it did, as JSON.
//
// Wes, 2026-08-04: "Checking a todo task jumps to the top of the page, but it
// shouldn't."
//
// String-matching for "data-inplace" would only prove the file contains the
// word. What can actually go wrong here is all about ORDER and interaction
// between listeners that were written years apart, so the sections are sliced
// out in FILE ORDER and registered against one stub document, exactly as the
// browser would:
//
//   1. the confirm handler (top of the file) - cancels by preventDefault
//   2. the busy guard - swallows a repeat press before anyone else sees it
//   3. the scroll stash (must not stash for a form that never navigates)
//   4. the in-place handler itself
//
// Called by tests/test_inplace_submit.py.
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

// In file order, because that is the contract under test.
const confirmSrc = slice("// Confirm destructive actions.", "// --- One press, one action", "confirm");
// Wes, 2026-08-27: no feedback on a press, and a second press repeats the
// action. Sliced separately from the confirm handler above it even though the
// two are adjacent, because the ORDER of these three is the contract: confirm
// cancels, then the busy guard swallows a repeat, and only then may the stash
// and the poster act on the press.
const busySrc = slice("// --- One press, one action", "// Ctrl/Cmd+Enter", "busy guard");
const scrollSrc = slice(
  'var SCROLL_KEY = "portal:scroll-after-submit";',
  "// --- Acting on a row without leaving the page",
  "scroll stash"
);
const inplaceSrc = slice(
  "// Opt-in per form (`data-inplace`)",
  "// --- Attachments: drop, paste, record",
  "in-place submit"
);
const postFormSrc = slice("// Returns a promise that settles when the action is DONE", "// Which zone means", "postForm");
const liveReloadSrc = slice("function liveReload(force) {", "function initLiveRefresh", "liveReload");
// The morph's attribute policy. It is here rather than with the other morph
// tests because the reason it has a hidden-input exception is entirely this
// feature: the toggle posts its target state out of a hidden input, so a
// preserved (stale) one made the second click a no-op.
const preservedSrc = slice("function isFormField(el) {", "function syncAttrs(", "preservedAttr");
// What a patch waits for. The forced/unforced split is the other half of Wes's
// 2026-08-27 note ("it often hangs a bit before completing the task I clicked
// the button for"), and it is a decision rather than a string, so it is driven
// rather than matched. Sliced with the real SUBMIT_ON_CHORD, because which
// input types count as "typing" is part of the same decision.
const chordSrc = slice("var SUBMIT_ON_CHORD =", "\n\n", "SUBMIT_ON_CHORD");
const blockedSrc = slice(
  "// Somebody is part-way through a sentence.",
  "// --- Holding the view still across a patch",
  "refreshBlocked"
);
const heldSrc = slice("var refreshQueued = false;", "function liveRefreshNow", "refreshHeld");

const SRC = [confirmSrc, busySrc, scrollSrc, inplaceSrc, postFormSrc, liveReloadSrc].join("\n");

// preservedAttr is a pure function, so it gets driven on its own rather than
// through the stub document the listeners need.
const preservedAttr = new Function(preservedSrc + "; return preservedAttr;")();

function field(tag, type) {
  return { tagName: tag, getAttribute: (n) => (n === "type" ? type : null) };
}

// --- the stub world --------------------------------------------------------

// The busy guard is all classList, so the stub needs a real one rather than the
// no-op the older scenes could get away with.
function makeClassList(classes) {
  return {
    add: (c) => { if (classes.indexOf(c) < 0) classes.push(c); },
    remove: (c) => { const i = classes.indexOf(c); if (i >= 0) classes.splice(i, 1); },
    contains: (c) => classes.indexOf(c) >= 0,
  };
}

// A submit button. `formaction` is what a question card hangs its three
// destinations off; `name`/`value` is what a tapped quick option carries.
function makeButton(spec) {
  const attrs = {};
  if (spec.formaction) attrs.formaction = spec.formaction;
  if (spec.confirm) attrs["data-confirm"] = spec.confirm;
  const classes = (spec.classes || []).slice();
  return {
    tagName: "BUTTON",
    name: spec.name || "",
    value: spec.value === undefined ? "" : spec.value,
    classes,
    attrs,
    classList: makeClassList(classes),
    getAttribute: (n) => (n in attrs ? attrs[n] : null),
    hasAttribute: (n) => n in attrs,
    setAttribute: (n, v) => { attrs[n] = v; },
    removeAttribute: (n) => { delete attrs[n]; },
  };
}

function makeForm(spec) {
  const attrs = Object.assign({ action: spec.action || "/todo/7/toggle" }, spec.attrs || {});
  if (spec.inplace) attrs["data-inplace"] = "";
  if (spec.confirm) attrs["data-confirm"] = spec.confirm;
  // A compose form: the note box. `enctype` and a file input are what
  // isMultipartForm() reads, and both are on the real form already for the
  // no-script path, so the fixture carries them the same way.
  if (spec.compose) attrs["data-compose"] = "";
  if (spec.multipart) attrs.enctype = "multipart/form-data";
  const buttons = spec.buttons || [];
  const inside = spec.contains || [];
  // What clearComposeForm() empties. Held as real stub nodes so the assertion
  // is "the box is empty afterwards" rather than "a function was called".
  const fileInput = spec.fileInput || null;
  const textFields = spec.textFields || [];
  const shelfRows = spec.shelfRows || [];
  const chips = spec.chips || [];
  const statuses = spec.statuses || [];
  const classes = [];
  const form = {
    tagName: "FORM",
    method: spec.method || "post",
    fields: spec.fields || {},
    attrs,
    classes,
    classList: makeClassList(classes),
    getAttribute: (n) => (n in attrs ? attrs[n] : null),
    hasAttribute: (n) => n in attrs,
    setAttribute: (n, v) => { attrs[n] = v; },
    removeAttribute: (n) => { delete attrs[n]; },
    matches: (sel) => {
      if (sel === "form[data-inplace]") return "data-inplace" in attrs;
      if (sel === "[data-compose]") return "data-compose" in attrs;
      return false;
    },
    querySelector: (sel) => {
      // The one thing that decides whether a missing submitter is recoverable.
      if (sel === "[formaction]") {
        return buttons.filter((b) => b.getAttribute("formaction"))[0] || null;
      }
      // The second half of isMultipartForm(): a form carrying a file input is
      // multipart whatever its enctype says.
      if (sel === 'input[type="file"]') return fileInput;
      return null;
    },
    // clearBusy sweeps its own form for marked controls. "button" is answered
    // too, and not because anything in app.js asks for it: it is what lets a
    // mutation mark EVERY submit button in the form, so the assertion that the
    // pressed one's siblings stay clean has something that can falsify it. A
    // fixture that cannot express the wrong behavior cannot catch it.
    querySelectorAll: (sel) => {
      if (sel === "button") return buttons;
      if (sel === "textarea, input[type=text]") return textFields;
      if (sel === 'input[type="file"]') return fileInput ? [fileInput] : [];
      if (sel === ".rec-row, .attach-row-item") return shelfRows;
      if (sel === ".quote-chip") return chips;
      if (sel === "[data-attach-status]") return statuses;
      if (sel !== "[data-busy]") return [];
      return buttons.filter((b) => b.hasAttribute("data-busy"));
    },
    contains: (el) => inside.indexOf(el) >= 0,
    submit: () => { world.plainSubmits.push(attrs.action); },
  };
  if (spec.requestSubmit !== false) {
    form.requestSubmit = () => { world.requested.push(attrs.action); dispatchSubmit(form); };
  }
  world.forms.push(form);
  return form;
}

let world;
let submitListeners;
let pageshowListeners;

function firePageshow() {
  pageshowListeners.forEach((fn) => fn({ type: "pageshow", persisted: true }));
}

function dispatchSubmit(form, opts) {
  const ev = {
    type: "submit",
    target: form,
    submitter: (opts && opts.submitter) || null,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
  };
  submitListeners.forEach((fn) => fn(ev));
  return ev;
}

function build(scene) {
  world = {
    posted: [],       // {action, body} - what actually went over fetch
    plainSubmits: [], // forms that fell through to a real navigation
    requested: [],
    stashed: [],      // what the scroll-restore listener wrote to sessionStorage
    reloaded: 0,      // liveRefreshNow() calls - a patch, not a navigation
    forcedReloads: 0, // ...of which asked to jump the refreshBlocked() queue
    alerts: [],
    blurred: [],      // what releaseFocus() let go of
    forms: [],        // every form this scene built, for document.querySelectorAll
    order: [],        // what happened, in the order it happened
  };
  submitListeners = [];
  pageshowListeners = [];

  // A scene may answer a run of confirms differently - a canceled delete
  // followed by a confirmed one is how you tell "the guard let go" apart from
  // "the confirm said no twice".
  let confirmIndex = 0;
  globalThis.confirm = () => {
    if (!scene.confirmAnswers) return scene.confirmAnswer !== false;
    const answer = scene.confirmAnswers[confirmIndex];
    confirmIndex += 1;
    return answer !== false;
  };
  globalThis.alert = (m) => { world.alerts.push(m); };

  globalThis.document = {
    addEventListener(type, fn) { if (type === "submit") submitListeners.push(fn); },
    querySelector: () => null,
    querySelectorAll: (sel) => {
      // The pageshow thaw: every form still wearing the busy mark.
      if (sel !== "form[data-busy]") return [];
      return world.forms.filter((f) => f.hasAttribute("data-busy"));
    },
    documentElement: { scrollTop: 0 },
    body: { tagName: "BODY" },
    activeElement: scene.activeElement || null,
  };
  globalThis.sessionStorage = {
    setItem: (k, v) => { world.stashed.push({ key: k, value: v }); },
    getItem: () => null,
    removeItem: () => {},
  };
  globalThis.location = { pathname: "/project/portal", hash: "" };
  globalThis.history = {};
  globalThis.URLSearchParams = URLSearchParams;
  // clearComposeForm() empties a file input by assigning it a fresh, empty
  // FileList - which can only be got from a DataTransfer. Present here so the
  // real path is the one under test rather than the `input.value = ""`
  // fallback for browsers that have no DataTransfer.
  globalThis.DataTransfer = class {
    constructor() { this.files = []; }
  };

  // The two capability gates the code reads, both present unless a scene turns
  // one off to check the no-JS fallback.
  globalThis.window = {
    addEventListener(type, fn) { if (type === "pageshow") pageshowListeners.push(fn); },
    scrollY: scene.scrollY === undefined ? 900 : scene.scrollY,
    scrollTo() {},
    fetch: scene.noFetch ? undefined : true,
    DOMParser: scene.noFetch ? undefined : function () {},
    FormData: scene.noFetch ? undefined : true,
  };
  // Returns a promise, because the whole point of chaining onDone through
  // postForm is that a caller can wait for the PATCH rather than for the POST.
  globalThis.liveRefreshNow = (force) => {
    world.reloaded += 1;
    if (force) world.forcedReloads += 1;
    world.order.push("patched");
    // A scene can hold the patch open to prove what the busy mark waits for.
    if (!scene.holdPatch) return Promise.resolve();
    return new Promise((resolve) => { world.releasePatch = resolve; });
  };

  // `set` as well as `forEach`, because the submitter's name/value is written
  // onto the FormData now rather than onto a plain object beside it - a
  // multipart post has no plain-object stage to write it into. `set` rather
  // than `append` is the contract being modeled: pressing "queue note" must
  // REPLACE any `then` already in the form, never add a second one.
  globalThis.FormData = class {
    constructor(form) { this.pairs = Object.keys(form.fields).map((k) => [k, form.fields[k]]); }
    forEach(cb) { this.pairs.forEach(([k, v]) => cb(v, k)); }
    set(k, v) {
      const at = this.pairs.findIndex(([name]) => name === k);
      if (at >= 0) this.pairs[at] = [k, v];
      else this.pairs.push([k, v]);
    }
    get(k) { const p = this.pairs.find(([name]) => name === k); return p ? p[1] : null; }
  };

  globalThis.fetch = (action, opts) => {
    // Both halves of what a multipart post has to get right are recorded: that
    // the body is the FormData itself (not a flattened string, which would
    // leave every file behind), and that NO Content-Type header was set (only
    // the browser can write the boundary that belongs beside it, and a header
    // without one makes the server read the whole body as one bad part).
    world.posted.push({
      action,
      body: opts.body,
      method: opts.method,
      isFormData: opts.body instanceof globalThis.FormData,
      // Explicitly null rather than left undefined: JSON.stringify drops an
      // undefined value, and a key that is simply MISSING makes "no
      // Content-Type was set" indistinguishable from "the harness forgot to
      // record it" on the Python side.
      contentType: opts.headers ? opts.headers["Content-Type"] || null : null,
      headers: opts.headers === undefined ? "absent" : "present",
    });
    return Promise.resolve({
      ok: scene.serverSaysNo !== true,
      json: () => Promise.resolve({ detail: "a run is in flight" }),
    });
  };

  // eslint-disable-next-line no-new-func
  new Function(SRC)();
}

// --- the scenes ------------------------------------------------------------

const out = {};

// 1. Ticking a todo posts over fetch, prevents the navigation, and patches the
//    page in place - so nothing ever touches the scroll position.
{
  build({});
  const form = makeForm({ inplace: true, fields: { done: "1" } });
  const ev = dispatchSubmit(form);
  await Promise.resolve();
  await Promise.resolve();
  out.tick = {
    prevented: ev.defaultPrevented,
    posted: world.posted,
    stashed: world.stashed,
    plainSubmits: world.plainSubmits,
    patched: world.reloaded,
  };
}

// 2. A checkbox using submitForm() actually reaches the listener. form.submit()
//    fires no submit event at all, which is the silent way this whole feature
//    could have shipped doing nothing.
{
  build({});
  const form = makeForm({ inplace: true, fields: { done: "1" } });
  globalThis.window.submitForm(form);
  await Promise.resolve();
  await Promise.resolve();
  out.viaSubmitForm = { requested: world.requested, posted: world.posted.length };

  build({});
  const old = makeForm({ inplace: true, fields: { done: "1" }, requestSubmit: false });
  globalThis.window.submitForm(old);
  out.viaSubmitFormLegacy = { plainSubmits: old && world.plainSubmits };
}

// 3. An ordinary form is untouched: it navigates, and its scroll position is
//    stashed the way it always was.
{
  build({});
  const form = makeForm({ inplace: false, fields: { text: "hi" } });
  const ev = dispatchSubmit(form);
  await Promise.resolve();
  out.ordinary = {
    prevented: ev.defaultPrevented,
    posted: world.posted.length,
    stashedCount: world.stashed.length,
  };
}

// 4. A canceled delete posts nothing. The confirm handler runs first and
//    prevents; the in-place handler must respect that.
{
  build({ confirmAnswer: false });
  const form = makeForm({ inplace: true, confirm: "Delete this todo?", fields: {} });
  const ev = dispatchSubmit(form);
  await Promise.resolve();
  out.canceledDelete = {
    prevented: ev.defaultPrevented,
    posted: world.posted.length,
    stashedCount: world.stashed.length,
  };
}

// 5. No fetch: fall all the way back to a real navigation rather than swallowing
//    the click.
{
  build({ noFetch: true });
  const form = makeForm({ inplace: true, fields: { done: "1" } });
  const ev = dispatchSubmit(form);
  out.noFetch = { prevented: ev.defaultPrevented, stashedCount: world.stashed.length };
}

// 6. The route refusing shows its reason instead of silently doing nothing.
{
  build({ serverSaysNo: true });
  const form = makeForm({ inplace: true, fields: { done: "1" } });
  dispatchSubmit(form);
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  out.refused = { alerts: world.alerts, patched: world.reloaded };
}

// 7. A question card. One form, three destinations hung off its buttons as
//    `formaction`, so the post has to follow the button that was pressed
//    rather than the form's own action.
{
  build({});
  const del = makeButton({ formaction: "/questions/4/delete" });
  const form = makeForm({
    inplace: true,
    action: "/questions/4/answer",
    fields: { answer: "", next: "/questions" },
    buttons: [del],
  });
  dispatchSubmit(form, { submitter: del });
  await Promise.resolve();
  await Promise.resolve();
  out.formaction = { posted: world.posted.map((p) => p.action) };
}

// 8. A tapped quick option. Its name/value is part of a real browser
//    submission and is NOT in `new FormData(form)` - dropped, the answer
//    posts blank and the question settles against nothing.
{
  build({});
  const pick = makeButton({ name: "choice", value: "merge it" });
  const form = makeForm({
    inplace: true,
    action: "/questions/4/answer",
    fields: { answer: "and here is why", next: "/questions" },
    buttons: [pick],
  });
  dispatchSubmit(form, { submitter: pick });
  await Promise.resolve();
  await Promise.resolve();
  out.quickOption = { body: world.posted[0] && world.posted[0].body };
}

// 9. No submitter at all (Safari before 15.4). A single-destination form is
//    unaffected; a multi-destination one must navigate the old way rather than
//    guess, or a delete posts to the answer route.
{
  build({});
  const form = makeForm({ inplace: true, fields: { done: "1" } });
  const ev = dispatchSubmit(form, { submitter: null });
  await Promise.resolve();
  out.noSubmitterSimple = { prevented: ev.defaultPrevented, posted: world.posted.length };

  build({});
  const many = makeForm({
    inplace: true,
    action: "/questions/4/answer",
    fields: {},
    buttons: [makeButton({ formaction: "/questions/4/delete" })],
  });
  const ev2 = dispatchSubmit(many, { submitter: null });
  await Promise.resolve();
  out.noSubmitterMulti = {
    prevented: ev2.defaultPrevented,
    posted: world.posted.length,
    // ...and because it really does navigate, the scroll stash has to fire for
    // it. The two listeners ask the same function, so they cannot disagree.
    stashedCount: world.stashed.length,
  };
}

// 10. The focus inside the posted form is released, or refreshBlocked() holds
//     the patch back and answering a question you typed into looks like it did
//     nothing at all until you tap somewhere else.
{
  const box = { tagName: "TEXTAREA", blur() { world.blurred.push("textarea"); } };
  build({ activeElement: box });
  const form = makeForm({
    inplace: true,
    action: "/questions/4/answer",
    fields: { answer: "yes" },
    contains: [box],
  });
  dispatchSubmit(form, { submitter: makeButton({}) });
  await Promise.resolve();
  out.releasedFocus = { blurred: world.blurred };

  // A field somebody is typing in SOMEWHERE ELSE on the page is not ours to
  // touch - ticking a todo must not close the keyboard over the note box.
  const elsewhere = { tagName: "TEXTAREA", blur() { world.blurred.push("elsewhere"); } };
  build({ activeElement: elsewhere });
  const row = makeForm({ inplace: true, fields: { done: "1" }, contains: [] });
  dispatchSubmit(row, { submitter: makeButton({}) });
  await Promise.resolve();
  out.leftOtherFocusAlone = { blurred: world.blurred };
}

// 11. The morph must let a hidden input's value change, and must still refuse to
//    touch a field somebody could be part-way through editing.
out.preserved = {
  hiddenValue: preservedAttr(field("INPUT", "hidden"), "value", false),
  textValue: preservedAttr(field("INPUT", "text"), "value", false),
  checkboxChecked: preservedAttr(field("INPUT", "checkbox"), "checked", false),
  textareaValue: preservedAttr(field("TEXTAREA", null), "value", false),
  optionSelected: preservedAttr(field("OPTION", null), "selected", false),
  // Not a blanket "hidden inputs are unprotected": everything else about one
  // still rides the same rules as any other element.
  hiddenClass: preservedAttr(field("INPUT", "hidden"), "class", false),
  hiddenAttribute: preservedAttr(field("INPUT", "hidden"), "hidden", false),
};

// --- Wes, 2026-08-27: one press, one action --------------------------------
// "when I click a button to answer a question, add a note, etc, it often hangs
//  a bit before completing the task I clicked the button for. There is no
//  feedback that anything was done when clicking the button, though, and
//  clicking it again multiple times will repeat the action a few times."

const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

// 12. The press is visible immediately - on the button that was pressed, and
//     only on that one. The note form has three submit buttons.
{
  build({});
  const go = makeButton({ name: "then", value: "" });
  const quiet = makeButton({ name: "then", value: "queue" });
  const form = makeForm({ inplace: true, fields: { note: "hi" }, buttons: [go, quiet] });
  dispatchSubmit(form, { submitter: go });
  // .slice() every time: `classes` is the live array the stub's classList
  // mutates, so handing the reference to `out` would report whatever it held
  // when this file printed rather than what it held at the moment measured.
  out.pressIsVisible = {
    formBusy: form.hasAttribute("data-busy"),
    pressed: go.attrs["data-busy"] === "" ? ["data-busy"] : [],
    pressedAria: go.attrs["aria-busy"] || null,
    // The sibling submit is untouched: marking the whole form would light up
    // three buttons for one press.
    sibling: quiet.attrs["data-busy"] === "" ? ["data-busy"] : [],
  };
}

// 13. The second press of an in-place form posts NOTHING. This is the bug:
//     answering a question twice files the answer twice.
{
  build({});
  const btn = makeButton({});
  const form = makeForm({ inplace: true, action: "/questions/4/answer", fields: { answer: "yes" }, buttons: [btn] });
  dispatchSubmit(form, { submitter: btn });
  const second = dispatchSubmit(form, { submitter: btn });
  const third = dispatchSubmit(form, { submitter: btn });
  out.repeatPressInPlace = {
    posts: world.posted.length,
    secondPrevented: second.defaultPrevented,
    thirdPrevented: third.defaultPrevented,
  };
  // ...and once it has settled the form is usable again, because a todo you
  // ticked by mistake has to be tickable back.
  await flush();
  out.repeatPressInPlace.busyAfter = form.hasAttribute("data-busy");
  out.repeatPressInPlace.markAfter = btn.attrs["data-busy"] === "" ? ["data-busy"] : [];
}

// 14. The same guard on an ORDINARY navigating form - which is what "add a
//     note" is. The browser is mid-navigation and has no page to show yet;
//     nothing but this stops the second tap posting a duplicate note.
{
  build({});
  const btn = makeButton({ name: "then", value: "" });
  const form = makeForm({ inplace: false, action: "/project/p/note", fields: { note: "hi" }, buttons: [btn] });
  const first = dispatchSubmit(form, { submitter: btn });
  const second = dispatchSubmit(form, { submitter: btn });
  out.repeatPressNavigating = {
    firstPrevented: first.defaultPrevented,   // must navigate for real
    secondPrevented: second.defaultPrevented, // must not
    busy: form.hasAttribute("data-busy"),
    // The stash is the reason this listener is registered ahead of it: a second
    // press must not write a scroll position for a navigation that is not
    // coming, which the next ordinary navigation here would then eat.
    stashedCount: world.stashed.length,
  };
}

// 15. Coming back to a submitted page. The back button restores it from the
//     bfcache exactly as it was left - a form frozen busy, with no load event
//     coming to thaw it, so the note box would be dead until a manual reload.
{
  build({});
  const btn = makeButton({});
  const form = makeForm({ inplace: false, action: "/project/p/note", fields: { note: "hi" }, buttons: [btn] });
  dispatchSubmit(form, { submitter: btn });
  const frozen = form.hasAttribute("data-busy");
  firePageshow();
  out.backButtonThaws = {
    frozen: frozen,
    thawed: !form.hasAttribute("data-busy"),
    markCleared: !btn.hasAttribute("data-busy"),
    ariaCleared: btn.attrs["aria-busy"] === undefined,
    // And it submits again, which is the whole point of thawing it.
    resubmits: !dispatchSubmit(form, { submitter: btn }).defaultPrevented,
  };
}

// 16. A canceled [data-confirm] never happened. Marking it busy would leave a
//     dead delete button until the page was reloaded - which is why this
//     listener sits BEHIND the confirm handler in the file.
{
  build({ confirmAnswers: [false, true] });
  const btn = makeButton({ confirm: "Delete this?" });
  const form = makeForm({ inplace: true, fields: {}, buttons: [btn] });
  dispatchSubmit(form, { submitter: btn });
  out.canceledLeavesNothingBusy = {
    busy: form.hasAttribute("data-busy"),
    marked: btn.attrs["data-busy"] === "" ? ["data-busy"] : [],
    // ...and it is still pressable, so answering the confirm "yes" next time
    // actually deletes.
    retryPosts: (dispatchSubmit(form, { submitter: btn }), world.posted.length),
  };
}

// 17. A route that REFUSED hands the control back. There is no morph on this
//     path - the page still shows what it showed - so nothing else would.
{
  build({ serverSaysNo: true });
  const btn = makeButton({});
  const form = makeForm({ inplace: true, fields: { done: "1" }, buttons: [btn] });
  dispatchSubmit(form, { submitter: btn });
  await flush();
  out.refusedHandsItBack = {
    alerts: world.alerts.length,
    busy: form.hasAttribute("data-busy"),
    marked: btn.attrs["data-busy"] === "" ? ["data-busy"] : [],
  };
}

// 18. The mark comes off after the PATCH, not after the POST. Between those two
//     the page still shows the old state, and a live-looking button over stale
//     text is the double press this whole section exists to stop.
{
  build({ holdPatch: true });
  const btn = makeButton({});
  const form = makeForm({ inplace: true, fields: { done: "1" }, buttons: [btn] });
  dispatchSubmit(form, { submitter: btn });
  await flush();
  const duringPatch = form.hasAttribute("data-busy");
  world.releasePatch();
  await flush();
  out.markOutlivesThePost = {
    patchStarted: world.reloaded,
    busyWhilePatching: duringPatch,
    busyAfterPatch: form.hasAttribute("data-busy"),
  };
}

// 19. That patch is FORCED. Wes: "it often hangs a bit before completing the
//     task I clicked the button for" - a patch answering a press used to wait
//     behind refreshBlocked() for a text box focused ELSEWHERE on the page
//     (ticking a todo half-way through writing a note), and the drain only came
//     round on the next 2.5s poll.
{
  build({});
  const btn = makeButton({});
  const form = makeForm({ inplace: true, fields: { done: "1" }, buttons: [btn] });
  dispatchSubmit(form, { submitter: btn });
  await flush();
  out.thePatchIsForced = { patches: world.reloaded, forced: world.forcedReloads };
}

// 21. ...and what "forced" buys, decided rather than string-matched. Built on
//     its own stub document because refreshHeld() asks the page questions the
//     submit scenes never do.
{
  const held = new Function(
    "doc, win",
    chordSrc + blockedSrc + heldSrc +
      ";var document = doc, window = win;" +
      "return function (scene) {" +
      "  document = scene.doc; window = scene.win; refreshForced = !!scene.forced;" +
      "  return refreshHeld();" +
      "};"
  )();

  // A page with nothing going on, and the four states that hold a patch back.
  function page(opts) {
    const o = opts || {};
    return {
      doc: {
        activeElement: o.activeElement || null,
        querySelector: (sel) => (o.openWidget ? { sel: sel } : null),
        body: { classList: { contains: () => !!o.dragging } },
      },
      win: { getSelection: () => ({ isCollapsed: !o.selecting }) },
      forced: !!o.forced,
    };
  }

  const typing = { tagName: "TEXTAREA" };
  const inANumberField = { tagName: "INPUT", type: "number" };
  const onACheckbox = { tagName: "INPUT", type: "checkbox" };

  out.whatAPatchWaitsFor = {
    // Nothing happening: neither kind waits.
    idle: held(page({})),
    idleForced: held(page({ forced: true })),

    // A sentence in progress. THIS is the one the force overrides - ticking a
    // todo half-way through writing a note used to leave the press unanswered
    // until the next 2.5s poll came round.
    typing: held(page({ activeElement: typing })),
    typingForced: held(page({ activeElement: typing, forced: true })),
    numberField: held(page({ activeElement: inANumberField })),
    numberFieldForced: held(page({ activeElement: inANumberField, forced: true })),
    // A checkbox is not typing, so nothing was ever held for it.
    checkbox: held(page({ activeElement: onACheckbox })),

    // The four a patch DESTROYS rather than interrupts. Forced or not, they
    // still hold: being right about what was pressed is no reason to throw away
    // what the reader was holding.
    openWidget: held(page({ openWidget: true })),
    openWidgetForced: held(page({ openWidget: true, forced: true })),
    dragging: held(page({ dragging: true })),
    draggingForced: held(page({ dragging: true, forced: true })),
    selecting: held(page({ selecting: true })),
    selectingForced: held(page({ selecting: true, forced: true })),
  };
}

// 20. No submitter at all (Safari before 15.4). There is nothing to pulse, but
//     the guard lives on the FORM, so the double-press protection still holds -
//     which is the half that costs him a duplicate note.
{
  build({});
  const form = makeForm({ inplace: false, action: "/project/p/note", fields: { note: "hi" } });
  dispatchSubmit(form, { submitter: null });
  const second = dispatchSubmit(form, { submitter: null });
  out.noSubmitterStillGuards = {
    busy: form.hasAttribute("data-busy"),
    secondPrevented: second.defaultPrevented,
  };
}

// --- The note box: sent without leaving the page ---------------------------
//
// Wes, 2026-08-28: "When I click add note (and maybe other things now on the
// project page), it reloads the page now and puts me back at the top of the
// page. This is unacceptable - the tool should be seamless and should not throw
// the user's view around when they are on the page."
//
// The note form is the first [data-inplace] form that carries FILES, and the
// first that has to be EMPTIED afterwards, so both of those are driven here
// rather than matched in the source.

// A compose form standing in for the note box: a textarea with a note in it, a
// file input holding a dropped screenshot, a staged row on the shelf, a quote
// chip and the attach-status line.
function makeComposeForm(spec) {
  const opts = spec || {};
  const textarea = { tagName: "TEXTAREA", value: "a note I just typed" };
  const fileInput = { files: ["shot.png"], value: "C:\\fakepath\\shot.png" };
  const shelfRow = { removed: false, remove() { this.removed = true; } };
  const chip = { removed: false, remove() { this.removed = true; } };
  const statusClasses = ["error"];
  const status = {
    textContent: "1 file: shot.png",
    classList: makeClassList(statusClasses),
  };
  const form = makeForm({
    inplace: true,
    compose: opts.compose !== false,
    multipart: opts.multipart !== false,
    action: "/project/p/note",
    fields: { note: "a note I just typed", then: "" },
    buttons: opts.buttons || [],
    fileInput: opts.noFiles ? null : fileInput,
    textFields: [textarea],
    shelfRows: [shelfRow],
    chips: [chip],
    statuses: [status],
  });
  return { form, textarea, fileInput, shelfRow, chip, status };
}

function composeState(parts) {
  return {
    textarea: parts.textarea.value,
    fileCount: parts.fileInput ? parts.fileInput.files.length : null,
    shelfRowRemoved: parts.shelfRow.removed,
    chipRemoved: parts.chip.removed,
    status: parts.status.textContent,
  };
}

// 21. Sending a note posts over fetch as multipart, patches the page in place,
//     and empties the box. Nothing navigates, so nothing can scroll.
{
  build({});
  const parts = makeComposeForm();
  const ev = dispatchSubmit(parts.form);
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  out.noteSentInPlace = {
    prevented: ev.defaultPrevented,
    navigated: world.plainSubmits.length,
    patched: world.reloaded,
    forced: world.forcedReloads,
    posted: world.posted.map((p) => ({
      action: p.action,
      isFormData: p.isFormData,
      contentType: p.contentType,
      headers: p.headers,
    })),
    // The scroll stash is the thing that used to put him back at the top. An
    // in-place form must not write one at all: an entry nothing consumes fires
    // on the next ordinary navigation to this page instead.
    stashed: world.stashed,
    after: composeState(parts),
  };
}

// 22. The same note, refused by the server. What he typed has to survive: the
//     note did not go anywhere, so emptying the box would lose it outright.
{
  build({ serverSaysNo: true });
  const parts = makeComposeForm();
  dispatchSubmit(parts.form);
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  out.noteRefusedKeepsTheText = {
    alerted: world.alerts.length,
    patched: world.reloaded,
    after: composeState(parts),
  };
}

// 23. A form with no files posts the old way - urlencoded, with a
//     Content-Type. The multipart branch is opt-in on the form's own shape, not
//     a change to how every in-place form posts.
{
  build({});
  const form = makeForm({ inplace: true, fields: { done: "1" } });
  dispatchSubmit(form);
  await Promise.resolve();
  await Promise.resolve();
  out.plainFormStillUrlencoded = {
    isFormData: world.posted[0].isFormData,
    contentType: world.posted[0].contentType,
    body: world.posted[0].body,
  };
}

// Read a posted body WITHOUT assuming which kind it is.
//
// This is not defensive coding for its own sake - it is what keeps a mutation
// sweep honest. Under the mutation that makes a note post urlencoded, the body
// is a plain STRING with no `.get`, and a scene that called `.get` on it threw
// and took the whole harness down with it. bun then exits non-zero, the
// module-scoped `ran` fixture raises, and pytest reports that as
// `ERROR tests/...::test_x` rather than `FAILED tests/...::test_x` - so the
// sweep's FAILED parser saw nothing and filed a mutation its tests DO catch as
// a miss. A scene that assumes the fixed behavior cannot observe the broken
// one, which is the whole job.
function bodyPairs(body) {
  if (body && Array.isArray(body.pairs)) return body.pairs;
  // A urlencoded string, which is what the non-multipart path posts.
  if (typeof body === "string") {
    return body.split("&").filter(Boolean).map((p) => {
      const at = p.indexOf("=");
      const k = at < 0 ? p : p.slice(0, at);
      const v = at < 0 ? "" : p.slice(at + 1);
      return [decodeURIComponent(k.replace(/\+/g, " ")), decodeURIComponent(v.replace(/\+/g, " "))];
    });
  }
  return [];
}

function bodyField(body, name) {
  const hit = bodyPairs(body).find(([k]) => k === name);
  return hit ? hit[1] : null;
}

function bodyCount(body, name) {
  return bodyPairs(body).filter(([k]) => k === name).length;
}

// 24. Which button was pressed rides along. "queue note" must arrive as
//     then=queue: dropped, the server reads the note as an ordinary add and
//     `note_runs_now` starts a run he explicitly asked it not to.
{
  build({});
  const queue = makeButton({ name: "then", value: "queue" });
  const parts = makeComposeForm({ buttons: [queue] });
  dispatchSubmit(parts.form, { submitter: queue });
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  out.submitterRidesAlong = {
    then: bodyField(world.posted[0].body, "then"),
    // set(), not append(): the form already carries an empty `then`, and a
    // second one would leave the server reading whichever came first.
    thenCount: bodyCount(world.posted[0].body, "then"),
  };
}

// 24b. A form that DECLARES multipart but has no file input in it still posts
//      as multipart. Two markers decide this and the note form carries both, so
//      without a fixture that separates them either one alone would look
//      load-bearing while doing nothing - a sweep deleting the enctype check
//      changed no behavior at all, because the file input was answering for it.
//
//      It is the declaration that has to win: a form saying multipart/form-data
//      is a form whose route was written to parse multipart, whether or not
//      there is a file in it at the moment it is submitted.
{
  build({});
  const parts = makeComposeForm({ noFiles: true });
  dispatchSubmit(parts.form);
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  out.enctypeAloneIsEnough = {
    isFormData: world.posted[0].isFormData,
    headers: world.posted[0].headers,
  };
}

// 25. A compose form that is NOT marked data-compose keeps its text. The
//     clearing is the marker's doing, not something every in-place post now
//     does to any field it can find - the settings page is full of in-place
//     forms whose fields are meant to survive.
{
  build({});
  const parts = makeComposeForm({ compose: false });
  dispatchSubmit(parts.form);
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  out.withoutComposeMarkerNothingIsCleared = composeState(parts);
}

console.log(JSON.stringify(out, null, 2));

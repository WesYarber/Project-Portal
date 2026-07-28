// Runs the real Escape-to-let-go handler out of app/static/app.js against a stub
// DOM and prints what it did, as JSON.
//
// Wes, 2026-07-28: "hitting escape should de-select whatever text field is
// selected."
//
// String-matching for ".blur()" would only prove the file contains the word.
// This proves the behavior: that the field is actually let go of, that a key
// which is not Escape leaves it alone, that Escape on something which is not a
// field does nothing at all, and - the point of the whole feature - that after
// the blur the jump keys answer again, which is checked by feeding the real
// `typingInto` from the same file the element the focus fell to.
//
// Called by tests/test_escape_blur.py.
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

// The handler under test, and `typingInto` from the jump section - the two are
// a pair (one exists to undo what the other forbids), so the round-trip test
// below uses the real gate rather than a copy of its rules.
const escapeSrc = slice("function escapeBlurTarget", null, "escape-blur");
const typingIntoSrc = slice("function typingInto", "function jumpTarget", "typingInto");

function makeField(spec, world) {
  return {
    tagName: spec.tag || "DIV",
    isContentEditable: !!spec.contentEditable,
    // A real element always has blur(); `noBlur` is the defensive case - a
    // stubbed or exotic target that does not, which must not throw.
    blur: spec.noBlur ? undefined : () => { world.blurred.push(spec.name); },
  };
}

function run(scene) {
  const world = { blurred: [], defaultPrevented: false, propagationStopped: false };
  let listener = null;

  globalThis.document = {
    addEventListener: (type, fn) => { if (type === "keydown") listener = fn; },
  };

  // eslint-disable-next-line no-new-func
  new Function(escapeSrc)();
  if (!listener) throw new Error("the escape section registered no keydown listener");

  const target = scene.target
    ? makeField(scene.target, world)
    : { tagName: "BODY", isContentEditable: false, blur: () => { world.blurred.push("body"); } };

  listener({
    key: scene.key,
    target,
    preventDefault: () => { world.defaultPrevented = true; },
    stopPropagation: () => { world.propagationStopped = true; },
  });

  return world;
}

// The round trip Wes is actually asking for: jump into the note box with N, and
// get back out with Escape so the letters are letters again. `typingInto` is
// the real gate from the jump section, so this fails if either half drifts.
function roundTrip() {
  // eslint-disable-next-line no-new-func
  const typingInto = new Function(typingIntoSrc + "; return typingInto;")();
  const box = { tagName: "TEXTAREA", isContentEditable: false };
  const body = { tagName: "BODY", isContentEditable: false };
  return {
    // While the cursor is in the box, the jump keys are correctly inert...
    jumpsBlockedWhileFocused: typingInto(box),
    // ...and after Escape hands focus back to <body>, they answer again.
    jumpsWorkAfterBlur: typingInto(body),
  };
}

const scenes = {
  // The three kinds of field a person can be typing into.
  textarea: { key: "Escape", target: { name: "noteBox", tag: "TEXTAREA" } },
  input: { key: "Escape", target: { name: "titleBox", tag: "INPUT" } },
  select: { key: "Escape", target: { name: "ownerPick", tag: "SELECT" } },
  contentEditable: {
    key: "Escape",
    target: { name: "richText", tag: "DIV", contentEditable: true },
  },

  // Escape with nothing focused: the body is not a field, so nothing is let go
  // of. Blurring <body> would be harmless but it would also be a lie in the
  // log, and it is the case that fires every time Escape closes a menu.
  nothingFocused: { key: "Escape" },

  // A plain div that happens to be the target - not a field, not blurred.
  nonField: { key: "Escape", target: { name: "card", tag: "DIV" } },

  // Every other key leaves the cursor exactly where it is. Without this the
  // note box would empty itself of focus on the first letter typed.
  otherKeyInField: { key: "a", target: { name: "noteBox", tag: "TEXTAREA" } },
  enterInField: { key: "Enter", target: { name: "noteBox", tag: "TEXTAREA" } },

  // A target with no blur() must not throw - this handler runs on every Escape
  // on every page, so an exception here would break the menus and the lightbox
  // as collateral.
  targetWithoutBlur: {
    key: "Escape",
    target: { name: "odd", tag: "INPUT", noBlur: true },
  },
};

const out = { roundTrip: roundTrip() };
for (const [name, scene] of Object.entries(scenes)) out[name] = run(scene);
console.log(JSON.stringify(out, null, 2));

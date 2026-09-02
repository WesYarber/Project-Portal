// Minimal vanilla JS: no framework, no build step.

// Confirm destructive actions.
document.addEventListener("submit", function (ev) {
  var form = ev.target;
  // The message can sit on the form or on the button that submitted it - a
  // form with two submit buttons only wants the confirm on the dangerous one.
  var msg = form.getAttribute("data-confirm") ||
    (ev.submitter && ev.submitter.getAttribute("data-confirm"));
  if (msg && !confirm(msg)) {
    ev.preventDefault();
  }
});

// --- One press, one action -------------------------------------------------
// Wes, 2026-08-27:
//
//   "when I click a button to answer a question, add a note, etc, it often
//    hangs a bit before completing the task I clicked the button for. There is
//    no feedback that anything was done when clicking the button, though, and
//    clicking it again multiple times will repeat the action a few times."
//
// Three complaints, one gap: from the press until the page changes, a submitted
// form looks exactly like an unsubmitted one. Nothing here makes the round trip
// faster - it makes the wait visible and makes the second press free.
//
// It covers BOTH kinds of form, because both of the things he named are one
// each: answering a question is [data-inplace] (fetch, then a patch - two round
// trips, and until today up to a whole poll interval of nothing on top), and
// adding a note is an ordinary navigation. The navigating one has no browser
// spinner to lean on either: he reads this from a Home Screen install, where
// there is no tab and no chrome to show one.
//
// Registered FIRST, ahead of the scroll stash and the in-place poster below,
// because a repeat press has to die before either of them acts on it. The stash
// would write a scroll position for a navigation that is not going to happen,
// and the next ordinary navigation here would then eat it and scroll a page the
// reader had only just opened. The poster would send the second copy of the
// answer that this section exists to stop.
//
// Behind the confirm handler above, though: a canceled delete never happened,
// and marking it busy would leave a dead button until the page was reloaded.

// A `data-*` attribute rather than a class, and that is load-bearing rather
// than a style choice. The live-refresh morph refuses to REMOVE a data-*
// attribute the server did not render (preservedAttr), because those are
// script-set markers it knows nothing about - so the mark survives a background
// patch landing mid-press. A class does not: the fresh HTML has no busy form in
// it, the morph syncs `class` from it, and the guard would come off a press
// that is still in flight. That is not theoretical; the browser check in
// scripts/press_feedback_shot.py found it, with the 2.5s version poll as the
// thing that stripped it.
var BUSY_ATTR = "data-busy";

// When the most recent press went out. Read by pressBlocked() far below, which
// is what keeps a background patch off a page that is showing an optimistic
// state the server has not confirmed yet.
var pressStartedAt = 0;

function formIsBusy(form) {
  return !!(form && form.hasAttribute && form.hasAttribute(BUSY_ATTR));
}

// The pulse goes on the button that was PRESSED, not on the form: the note form
// carries three submit buttons ("add note", "queue note", "add & run now") and
// only one of them was asked for. The guard, though, is the class on the FORM -
// so a browser that names no submitter (Safari before 15.4) still gets the
// double-press protection even where there is nothing to aim the pulse at.
function markBusy(form, submitter) {
  if (!form || !form.setAttribute) return;
  form.setAttribute(BUSY_ATTR, "");
  pressStartedAt = Date.now();
  if (submitter && submitter.setAttribute) {
    submitter.setAttribute(BUSY_ATTR, "");
    // aria-busy, and deliberately NOT `disabled`. Disabling a submit button
    // from inside its own submit event is the classic way to drop its
    // name/value from the payload that is still being serialized - which on the
    // note form is the whole difference between "add & run now" and a note that
    // quietly queues, and on a question card between answering and deleting.
    submitter.setAttribute("aria-busy", "true");
  }
}

function clearBusy(form) {
  if (!form || !form.removeAttribute) return;
  form.removeAttribute(BUSY_ATTR);
  if (!form.querySelectorAll) return;
  Array.prototype.forEach.call(form.querySelectorAll("[" + BUSY_ATTR + "]"), function (el) {
    el.removeAttribute(BUSY_ATTR);
    el.removeAttribute("aria-busy");
  });
}

document.addEventListener("submit", function (ev) {
  if (ev.defaultPrevented) return;
  var form = ev.target;
  if (!form || !form.hasAttribute) return;
  if (formIsBusy(form)) {
    // Already on its way. Swallowing the press is the point - this is the
    // "clicking it again multiple times will repeat the action" half.
    ev.preventDefault();
    return;
  }
  markBusy(form, ev.submitter || null);
});

// A navigating form stays busy until its page goes away, which is the honest
// end of the press. `pageshow` rather than `load`, because the way back to a
// page you submitted from is the back button, and that restores it from the
// bfcache with every class exactly as it was left - a form frozen busy, with no
// load event coming to thaw it.
window.addEventListener("pageshow", function () {
  Array.prototype.forEach.call(document.querySelectorAll("form[" + BUSY_ATTR + "]"), clearBusy);
});

// Ctrl/Cmd+Enter - and, on a real keyboard, Shift+Enter - submits the textarea
// you're typing in, so answering a question or dropping a note never needs a
// trip to the mouse. Plain Enter always inserts a newline.
//
// The pointer test is not decoration. iOS turns its shift key on by itself for
// auto-capitalization, which is exactly the state a note box is in when you
// start typing, and the return key then arrives as a keydown with
// shiftKey === true. That made every Enter on a phone submit the note
// mid-sentence. Shift+Enter stays for anything with a fine pointer (a mouse,
// hence a hardware keyboard); touch devices get plain newline behavior and
// keep Ctrl/Cmd+Enter for the keyboard-case users.
function hasHardwareKeyboard() {
  return !!(window.matchMedia && window.matchMedia("(pointer: fine)").matches);
}

// The input types where Enter means "I have finished typing". Deliberately not
// checkbox/radio/file/submit, where Enter and the chord both already mean
// something else to the browser.
var SUBMIT_ON_CHORD = /^(text|search|url|email|tel|number|password|)$/;

document.addEventListener("keydown", function (ev) {
  if (ev.key !== "Enter") return;
  var chord = ev.ctrlKey || ev.metaKey || (ev.shiftKey && hasHardwareKeyboard());
  if (!chord) return;
  var el = ev.target;
  if (!el.form) return;
  // Textareas are the reason this exists (Enter must insert a newline there,
  // so submitting needs a chord). Single-line inputs already submit on plain
  // Enter, and are included so the chord does the same thing everywhere rather
  // than silently doing nothing in the one field that looks identical - the
  // title box that sits directly above the idea box on the dashboard.
  if (el.tagName !== "TEXTAREA" && !(el.tagName === "INPUT" && SUBMIT_ON_CHORD.test(el.type))) {
    return;
  }
  ev.preventDefault();
  if (el.form.requestSubmit) {
    el.form.requestSubmit();
  } else {
    el.form.submit();
  }
});

// Copy the text of the element a button points at (the ssh-into-this-project
// command).
//
// navigator.clipboard does not exist on plain http, which is how the portal is
// reached on the LAN - so on the machine Wes actually uses this, the modern API
// is never the one that runs. The old fallback only *selected* the text, which
// is why pressing copy appeared to do nothing at all: the button flashed no
// confirmation and the clipboard was untouched.
//
// document.execCommand("copy") is deprecated but it is the only thing that
// copies in an insecure context, and every browser still implements it. So:
// try the real API, fall back to execCommand over a selection, and only if
// BOTH fail leave the text selected and say so, which at least turns the
// button into one keystroke rather than a no-op.
function legacyCopy(el) {
  selectText(el);
  try {
    return document.execCommand("copy");
  } catch (e) {
    return false;
  }
}

document.addEventListener("click", function (ev) {
  var btn = ev.target.closest ? ev.target.closest("[data-copy]") : null;
  if (!btn) return;
  var source = document.querySelector(btn.getAttribute("data-copy"));
  if (!source) return;
  ev.preventDefault();
  var text = source.textContent.trim();
  var was = btn.getAttribute("data-copy-label") || btn.textContent;
  btn.setAttribute("data-copy-label", was);
  var flash = function (label) {
    btn.textContent = label;
    setTimeout(function () { btn.textContent = was; }, 1800);
  };
  var fallback = function () {
    flash(legacyCopy(source) ? "copied" : "press ctrl-c");
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () {
      // Clearing any selection the fallback left behind keeps repeat presses
      // from looking different from the first one.
      flash("copied");
    }, fallback);
  } else {
    fallback();
  }
});

function selectText(el) {
  var range = document.createRange();
  range.selectNodeContents(el);
  var sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

// Grow textareas to fit their content instead of showing an inner scrollbar.
//
// Wes, 2026-08-18: "at some point when typing a paragraph into the add note
// field, the page starts jumping around any time there is an auto-correct
// suggestion or an auto-correct that just happens... might start happening
// once the text box is at its maximum allowed height. Selecting text also
// becomes a bit buggy at this point."
//
// The mechanism was the measurement, not the growing. `height: auto` on a
// <textarea> does NOT resolve to the content height - it resolves to the
// `rows` attribute, three lines - so the old first line collapsed a note box
// from its 60vh cap down to about 88px, and reading scrollHeight back forced
// layout at that size. The document lost 400-odd pixels for the length of one
// statement, the browser clamped the page scroll to the shorter document, and
// restoring the height did not restore the scroll. iOS fires an `input` event
// for every autocorrect suggestion it draws, so a long note paid that on every
// keystroke - and worst at the cap, where the collapse is biggest and the
// height it computes cannot change anything anyway.
//
// So: measure by collapsing only when a collapse can tell us something new.
var SIZED_AT = "_autosizeChars";

// The used max-height in pixels (getComputedStyle resolves the 60vh), or 0
// when the box is uncapped. parseFloat("none") is NaN and NaN > 0 is false, so
// the uncapped case needs no test of its own.
function heightCap(el) {
  var px = parseFloat(window.getComputedStyle(el).maxHeight);
  return px > 0 ? px : 0;
}

function autosize(el) {
  var cap = heightCap(el);
  var overflowing = el.scrollHeight > el.clientHeight;
  var chars = el.value.length;
  var shrank = el[SIZED_AT] === undefined || chars < el[SIZED_AT];
  el[SIZED_AT] = chars;

  var target;
  if (overflowing) {
    // Content already spills out of the box, so scrollHeight IS the height it
    // wants - no collapse needed to find that out. This is the ordinary
    // typing-a-new-line case.
    target = el.scrollHeight + 2;
  } else if (shrank) {
    // Only a deletion can leave the box taller than its content, and a
    // collapse is the only way to measure how much shorter it should be. The
    // height goes back before anything paints, and the page scroll goes back
    // with it: the document was briefly short enough to clamp it, and the page
    // owes the reader only the height the box actually lost.
    //
    // The box's own scrollTop is deliberately NOT saved here. Collapsing to
    // three rows only ever makes its scrollable range larger, so there is no
    // clamp to undo - a restore would be a line no test could ever fail.
    var keptY = window.scrollY;
    var was = el.style.height;
    el.style.height = "auto";
    target = el.scrollHeight + 2;
    el.style.height = was;
    if (window.scrollY !== keptY) window.scrollTo(window.scrollX, keptY);
  } else {
    // It fits and it did not get shorter: nothing about the height can have
    // changed. An autocorrect that swaps one word for another of the same
    // length lands here, which is the point.
    return;
  }

  if (cap && target > cap) target = cap;
  var next = target + "px";
  // Never write a height that is already set. A no-op assignment still
  // invalidates layout, and on a focused textarea that is enough for WebKit to
  // re-run its scroll-the-caret-into-view pass over both the box and the page.
  if (next !== el.style.height) el.style.height = next;
}
document.addEventListener("input", function (ev) {
  if (ev.target.tagName === "TEXTAREA") autosize(ev.target);
});
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("textarea").forEach(function (el) {
    if (el.value) autosize(el);
  });
  startLiveRunPoll();
  // The fold before the poller: on a finished run the poller fetches once and
  // there is a beat where the whole unfolded transcript is on screen, and this
  // costs nothing when the poller then replaces it.
  initConsoleFoldToggle();
  foldServerConsole();
  startConsolePoll();
  pinConsole();
  restoreDrafts();
  watchForOffline();
  initDropzones();
  initFileTree();
  initLazyFolds();
  initTitleRename();
  restoreScroll();
});

// --- Click the project name to rename it ------------------------------------
//
// The <h1> and the <input> are the same string in the same form; clicking swaps
// which one is showing. Enter submits (it is a lone text input in a form, so
// that is the browser's own behavior, not ours), Escape puts the original text
// back and closes without submitting.
//
// Blur does NOT save. An edit that commits when you click away is a rename you
// can trigger by scrolling past the top of the page on a phone, and this is the
// one field on here whose value is the project's name.
function initTitleRename() {
  var form = document.querySelector(".title-rename");
  // The guard is a JS property, not a data- attribute, on purpose: a live
  // refresh morph keeps the node (so the property survives) but resets its
  // attributes to the server's render (so an attribute guard would not).
  // Same pattern on every per-element initializer reinit() re-runs.
  if (!form || form._enhanced) return;
  form._enhanced = true;
  var head = form.querySelector("[data-rename-title]");
  var input = form.querySelector('input[name="title"]');
  var hint = form.querySelector("[data-rename-hint]");
  if (!head || !input) return;

  function close() {
    input.hidden = true;
    if (hint) hint.hidden = true;
    head.hidden = false;
  }
  head.addEventListener("click", function () {
    head.hidden = true;
    input.hidden = false;
    if (hint) hint.hidden = false;
    input.focus();
    input.select();
  });
  input.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    input.value = head.textContent.trim();
    close();
  });
  input.addEventListener("blur", close);
}

// --- Workspace file tree ----------------------------------------------------
//
// Folders arrive shut and empty. The first time one is opened, its contents are
// fetched from /tree/<slug>/<path> and swapped in; after that it is just a
// <details> and opening it costs nothing. That is why a project with a
// node_modules in it has the same page weight as one without.
//
// The fetch is delegated from the document rather than bound per folder,
// because the markup that comes back contains folders of its own - binding at
// init time would enhance the root level and nothing below it.
function initFileTree() {
  document.addEventListener("toggle", function (ev) {
    var d = ev.target;
    if (!d.matches || !d.matches(".tree-dir[data-tree-src]")) return;
    if (!d.open || d.dataset.treeLoaded) return;
    // Set before the request, not after: a fast double-click on a folder would
    // otherwise fire two fetches and render the children twice.
    d.dataset.treeLoaded = "1";
    var box = d.querySelector(".tree-children");
    fetch(d.dataset.treeSrc, { headers: { "X-Requested-With": "fetch" } })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.text();
      })
      .then(function (html) {
        box.innerHTML = html;
      })
      .catch(function () {
        // Leave the folder open saying so, and allow a retry by clearing the
        // flag - a folder that silently stayed empty would read as "this
        // directory has nothing in it", which is a different claim.
        delete d.dataset.treeLoaded;
        box.innerHTML = '<p class="tree-loading muted small">could not load - close and reopen to retry</p>';
      });
  }, true);
}

// --- Folds that fetch their own contents ------------------------------------
//
// The file tree above does this for workspace folders; this is the same idea
// with no directory in it, for any <details data-lazy-src="..."> whose body is
// a .lazy-body. The dashboard's "Recent activity" is the first user: 25 agent
// journal entries is tens of KB of rendered markdown on a page Wes opens from
// his phone many times a day and says he almost never scrolls that far down.
//
// Rendering it hidden would have cost exactly the same, so the server sends the
// fold empty and the entries are fetched here the first time it is opened.
//
// The same handler serves the "show more" button inside the fetched fragment,
// which carries its own data-lazy-src with a bigger limit. That one is a click,
// not a toggle, and it REPLACES the body rather than appending - so pressing it
// twice cannot double the feed.
function initLazyFolds() {
  function fill(box, url, flagOn) {
    // Set before the request, not after: a fast double-click would otherwise
    // fire two fetches and render the contents twice.
    flagOn();
    box.setAttribute("aria-busy", "true");
    fetch(url, { headers: { "X-Requested-With": "fetch" } })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.text();
      })
      .then(function (html) {
        box.innerHTML = html;
        box.removeAttribute("aria-busy");
      })
      .catch(function () {
        // Say so and allow a retry. A fold that silently stayed empty would
        // read as "there is nothing here", which is a different claim.
        box.removeAttribute("aria-busy");
        box.innerHTML =
          '<p class="muted small lazy-placeholder">could not load - close and reopen to retry</p>';
        box.parentNode && delete box.parentNode.dataset.lazyLoaded;
      });
  }

  document.addEventListener("toggle", function (ev) {
    var d = ev.target;
    if (!d.matches || !d.matches("details[data-lazy-src]")) return;
    if (!d.open || d.dataset.lazyLoaded) return;
    var box = d.querySelector(".lazy-body");
    if (!box) return;
    fill(box, d.dataset.lazySrc, function () { d.dataset.lazyLoaded = "1"; });
  }, true);

  // "show more", which lives inside the fragment the fetch above brought back.
  // Delegated for that reason - there is no such button at init time.
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest("button[data-lazy-src]");
    if (!b) return;
    var box = b.closest(".lazy-body");
    if (!box) return;
    ev.preventDefault();
    // Wes: a button with no visible response gets pressed again. The label
    // changes in the same turn as the click, and the button is disabled so the
    // second press is swallowed rather than queued.
    b.disabled = true;
    b.textContent = "loading...";
    fill(box, b.dataset.lazySrc, function () {});
  });
}

// --- Stay where you were across a submit -----------------------------------
// Every form here is a POST that redirects back to the same page, and the
// browser lands that fresh GET at the top. Answering the third question down,
// or ticking a todo, threw you back to the header every time - which is fine
// once and miserable when you are working through a batch. So the scroll
// position is stashed on submit and put back on the page that follows.
//
// One-shot on purpose: the entry is consumed the moment it is read, so a later
// ordinary navigation to the same page still starts at the top.

var SCROLL_KEY = "portal:scroll-after-submit";
var pendingScroll = null;

document.addEventListener("submit", function (ev) {
  // A [data-confirm] form that was canceled never navigates - remembering
  // where it was would fire on whatever page loads next instead.
  if (ev.defaultPrevented) return;
  var form = ev.target;
  // Nor does a [data-inplace] form, which is handled below by fetch and never
  // leaves the page. This listener runs FIRST (registered earlier in the file),
  // so it cannot lean on defaultPrevented for that one and has to name it: a
  // stashed position nothing consumes would fire on the next ordinary
  // navigation here instead, scrolling a page the reader had just opened.
  // Both listeners ask the same function, so they cannot disagree about it.
  if (inPlaceAction(ev)) return;
  if (!form || !form.method || form.method.toLowerCase() !== "post") return;
  var y = window.scrollY || document.documentElement.scrollTop || 0;
  if (!y) return;
  try {
    sessionStorage.setItem(SCROLL_KEY, JSON.stringify({ path: location.pathname, y: y }));
  } catch (e) {
    /* private mode / storage full: staying put is a nicety, not a feature */
  }
});

function restoreScroll() {
  var raw = null;
  try {
    raw = sessionStorage.getItem(SCROLL_KEY);
    sessionStorage.removeItem(SCROLL_KEY);
  } catch (e) {
    return;
  }
  if (!raw) return;
  var saved;
  try {
    saved = JSON.parse(raw);
  } catch (e) {
    return;
  }
  if (!saved || saved.path !== location.pathname || !saved.y) return;
  // An explicit #anchor in the URL is a deliberate destination - a link the
  // user followed on purpose. Never fight it. (No POST redirect adds one any
  // more: the todo routes used to, and it beat this restore every time.)
  if (location.hash) return;
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";
  window.scrollTo(0, saved.y);
  // What we asked for and what we got can differ: at DOMContentLoaded the
  // images have no height yet, so the document may be too short to reach it
  // and the browser clamps. Both numbers are kept so the load handler can tell
  // "the page grew" from "the user scrolled".
  pendingScroll = { want: saved.y, got: window.scrollY || 0 };
}

// Late-loading images in the journal shift the page under us, so the position
// is re-applied once everything has settled - but only if the user has not
// scrolled away in the meantime.
window.addEventListener("load", function () {
  if (!pendingScroll) return;
  var p = pendingScroll;
  pendingScroll = null;
  if (Math.abs((window.scrollY || 0) - p.got) < 4 && p.want !== p.got) {
    window.scrollTo(0, p.want);
  }
});

// --- Showing the result before the server confirms it -----------------------
// Wes, 2026-08-29: "Apply UI actions on the client immediately instead of
// waiting on the server and a reload - acknowledge on the 'since you last
// checked in' banner, add note, run agent - and let the next real page load
// correct any mismatch."
//
// The section above made the PRESS visible. This one makes the RESULT visible,
// which is the half that was still missing: from the press until the patch
// lands, the page shows the OLD state under a button that says it is working.
// The busy mark makes that wait honest; it does not make it short. Two round
// trips (the POST, then the refetch the morph patches from) is a quarter of a
// second on the LAN and rather more over Tailscale from a phone.
//
// So the page is changed at press time and reconciled afterwards. Nothing here
// is authoritative: every effect is a guess at what the server is about to
// render, and the forced patch that follows overwrites it either way.
//
// Three verbs, not a general "swap this HTML in". Each of the three actions Wes
// named is a different SHAPE of change - something goes away, a control changes
// state, something appears - and a generic version would have to be handed
// server-rendered markup, which is the round trip this whole section skips.
//
// `data-optimistic` is opt-in per form, for the same reason `data-inplace` is:
// an effect is only safe where this file can predict the server's answer, and
// most routes on this page it cannot.

// Every effect returns a function that puts the page back, or null when there
// was nothing to do. The undo is not decoration - postBody() now reports
// whether the route ACCEPTED the post, and several of these routes refuse
// (starting a run while one is in flight, a note on a deleted project). A page
// still showing the run that did not start is worse than one that waited.
function optimisticEffect(form, submitter) {
  if (!form || !form.getAttribute) return null;
  var kind = form.getAttribute("data-optimistic");
  if (kind === "hide") return optimisticHide(form);
  if (kind === "pending") return optimisticPending(submitter);
  if (kind === "note") return optimisticNote(form);
  return null;
}

// The banner folds away now. `hidden` rather than removing the node, and that
// is the morph's attribute policy rather than caution: preservedAttr() refuses
// to let a patch either add or remove `hidden`, so a background patch landing
// mid-flight cannot bring the banner back for the quarter second before the
// server agrees it is gone. Removing the node would look identical and then
// fail exactly there, with the patch re-inserting it from a render that has not
// heard about the press yet.
//
// It is also what bounds the mismatch Wes allows for. If the post is lost the
// banner stays hidden until the next REAL page load - which renders fresh HTML
// with no hidden attribute anywhere in it, so the correction costs nothing and
// needs no bookkeeping to survive.
function optimisticHide(form) {
  var sel = form.getAttribute("data-optimistic-target");
  var el = sel && document.querySelector ? document.querySelector(sel) : null;
  if (!el || !el.setAttribute || el.hasAttribute("hidden")) return null;
  el.setAttribute("hidden", "");
  return function () { el.removeAttribute("hidden"); };
}

// The pressed button says now what the server is about to say. Disabled as well
// as relabeled: "agent running..." over a button that still depresses is an
// invitation to press it again, and the busy guard above swallows a repeat only
// until the patch lands and rebuilds the form.
//
// Reached only when the browser names a submitter (Safari before 15.4 does
// not), which costs that browser the effect and nothing else.
function optimisticPending(submitter) {
  if (!submitter || !submitter.getAttribute) return null;
  var label = submitter.getAttribute("data-optimistic-label");
  if (!label) return null;
  var wasLabel = submitter.textContent;
  var wasDisabled = !!submitter.disabled;
  submitter.textContent = label;
  submitter.disabled = true;
  return function () {
    submitter.textContent = wasLabel;
    submitter.disabled = wasDisabled;
  };
}

// A note is two changes at once, because both are what "sent" looks like: the
// box empties, and the note appears in the journal above it.
//
// Staged files and recorded takes are deliberately NOT cleared here, unlike in
// clearComposeForm(). Their object URLs are revoked on the way out, so that
// clear cannot be undone - and a post the server refused must leave a voice
// memo you have not sent exactly where you left it. They keep the old timing,
// cleared once the post has actually been accepted.
function optimisticNote(form) {
  if (!form.querySelector) return null;
  var box = form.querySelector("textarea[name='note']");
  var text = box ? box.value : "";
  if (!text || !text.trim()) return null;
  box.value = "";
  if (typeof autosize === "function") autosize(box);
  var echo = echoNote(text);
  return function () {
    box.value = text;
    if (typeof autosize === "function") autosize(box);
    if (echo && echo.remove) echo.remove();
  };
}

// A stand-in for the journal entry the server is about to write. Prepended,
// because the feed is newest-first (db._JOURNAL_ORDER).
//
// Marked with a CLASS and never a data-* attribute, and that is load-bearing in
// the opposite direction to the busy mark above: preservedAttr() refuses to
// REMOVE a data-* the server did not render, so a data-marked echo would keep
// its half-sent look forever. `class` is synced straight from the server's
// render, so the morph turns this node into the real entry - byline, timestamp,
// attachments, edit controls and all - and the marker comes off in the same
// patch that fills the rest in.
//
// Nor is it in MORPH_KEEP, for that same reason. Everything in that list is
// client-only state the server knows nothing about; this is a placeholder for
// something the server is about to know about, so being replaced is the point.
function echoNote(text) {
  var feed = document.getElementById ? document.getElementById("journal") : null;
  if (!feed || !document.createElement) return null;
  var entry = document.createElement("div");
  entry.className = "journal-entry from-user note-unsent optimistic-echo";
  var meta = document.createElement("div");
  meta.className = "meta";
  var badge = document.createElement("span");
  badge.className = "badge badge-unsent";
  badge.textContent = "sending...";
  meta.appendChild(badge);
  var content = document.createElement("div");
  content.className = "content";
  // textContent, never innerHTML. This is the one place in this file where a
  // person's typed text goes back onto the page without passing the server's
  // markdown renderer, so it is the one place a note could put markup into its
  // own project page.
  content.textContent = text;
  entry.appendChild(meta);
  entry.appendChild(content);
  feed.insertBefore(entry, feed.firstChild);
  return entry;
}

// --- Acting on a row without leaving the page -------------------------------
// Wes, 2026-08-04: "Checking a todo task jumps to the top of the page, but it
// shouldn't."
//
// It did, and the stash-and-restore above was never going to fix it. The todo
// section is a <details> the server always renders CLOSED - initFoldMemory()
// reopens it from localStorage a moment later, because the server cannot know
// what you last had open - so at the instant restoreScroll() asks for a
// position the fresh document is short by the entire height of the list. The
// browser clamps to as far as the page can reach, which on a long list is
// nowhere near where you were, and the list then unfolds underneath a scroll
// position that has already been decided.
//
// A one-click action on a row does not need a navigation at all, so these no
// longer do one: the form is posted with fetch and the page is patched in place
// by the same live-refresh morph a running agent uses, which already holds the
// reader's line of text still. Nothing ever sets the scroll position, so
// nothing can set it wrong.
//
// Opt-in per form (`data-inplace`) rather than "every POST on this page". The
// rule for what may carry it is that submitting CONSUMES the form - a ticked
// row, an answered question, a dismissed banner.
//
// A compose box was excluded here for a while on the grounds that "the fields
// are still yours after the post". They are not. Wes, 2026-08-28: "When I click
// add note (and maybe other things now on the project page), it reloads the page
// now and puts me back at the top of the page. This is unacceptable - the tool
// should be seamless and should not throw the user's view around when they are
// on the page."
//
// He is right, and the old reasoning had it backwards: sending a note is
// exactly the gesture that consumes the box. What you typed is gone from your
// hands and into the journal, so blurring the field is correct (on a phone it
// also puts the keyboard away), and the fields have to be EMPTIED rather than
// preserved - which the morph will not do on its own, because it deliberately
// keeps live field values so a background patch cannot stomp what you are
// mid-way through typing. `data-compose` marks those forms, and
// clearComposeForm() below empties one once its post has been accepted.
function canPostInPlace() {
  return !!(window.fetch && window.DOMParser && window.FormData);
}

// A form that carries files has to post as multipart. Detected from the form
// rather than declared, because the two markers that would say so - enctype and
// an <input type=file> - are both already on the form for the no-script path.
function isMultipartForm(form) {
  if (!form || !form.getAttribute) return false;
  if ((form.getAttribute("enctype") || "").toLowerCase() === "multipart/form-data") return true;
  return !!(form.querySelector && form.querySelector('input[type="file"]'));
}

// Empty a compose form after its contents have been accepted by the server.
//
// Everything here is state the morph is right to leave alone during an ordinary
// background patch, which is why none of it can be left to the morph: a
// textarea's live text is the user's typing (morphNode returns early on one), a
// FileList is not in the server's render at all, and `.rec-row` is in
// MORPH_KEEP precisely so a patch cannot eat a voice memo you have not sent.
// After a send, all three are stale rather than precious.
function clearComposeForm(form) {
  if (!form || !form.querySelectorAll) return;
  form.querySelectorAll("textarea, input[type=text]").forEach(function (el) {
    el.value = "";
    // autosize() sized the box to what was in it; an empty box that stays six
    // rows tall reads as a send that did not happen.
    if (typeof autosize === "function") autosize(el);
  });
  form.querySelectorAll('input[type="file"]').forEach(function (input) {
    if (typeof DataTransfer !== "undefined") input.files = new DataTransfer().files;
    else input.value = "";
  });
  // Client-built shelves: recorded takes and staged uploads. Their object URLs
  // are revoked on the way out so a long session of dropping and sending files
  // does not hold every one of them in memory.
  form.querySelectorAll(".rec-row, .attach-row-item").forEach(function (row) {
    if (row._objectUrl && window.URL && URL.revokeObjectURL) URL.revokeObjectURL(row._objectUrl);
    row.remove();
  });
  // The quoted passage went out with the note; leaving the chip up would put it
  // on the NEXT note too.
  form.querySelectorAll(".quote-chip").forEach(function (chip) { chip.remove(); });
  form.querySelectorAll("[data-attach-status]").forEach(function (el) {
    el.textContent = "";
    el.classList.remove("error");
  });
}

// Where an in-place submit should post, or null if this submit is not ours.
//
// One function rather than a copy of the test in each listener, because the
// scroll stash above and the handler below must never disagree: a position
// stashed for a submit that then never navigates is eaten by the NEXT ordinary
// navigation to this page, scrolling a page the reader had only just opened.
//
// The submitter decides the answer on a question card, whose one form has three
// destinations hung off its buttons as `formaction` - answer, save for later,
// delete. Reading `form.action` there would send every one of them to the first.
function inPlaceAction(ev) {
  var form = ev.target;
  if (!form || !form.matches || !form.matches("form[data-inplace]")) return null;
  // No fetch means no morph; falling through to a normal submit is a worse
  // experience, not a broken one.
  if (!canPostInPlace()) return null;
  var submitter = ev.submitter || null;
  if (submitter && submitter.getAttribute && submitter.getAttribute("formaction")) {
    return submitter.getAttribute("formaction");
  }
  // Safari before 15.4 reports no submitter at all. On a single-destination
  // form that costs nothing, but on one with `formaction` buttons there is no
  // way to tell a delete from an answer - so it navigates the old way rather
  // than guessing, and posting a delete to the answer route.
  if (!submitter && form.querySelector && form.querySelector("[formaction]")) return null;
  return form.getAttribute("action");
}

// Let go of whatever inside the just-posted form had focus.
//
// refreshBlocked() holds a live patch back while a text field has focus, so a
// form you typed into would post and then appear to do nothing until you
// clicked somewhere else. That used to be a ban on text fields in an in-place
// form; it is a two-line release instead, because what was in those fields went
// out with the post and the form itself is about to be replaced. On a phone it
// also puts the keyboard away, which is what answering a question means.
function releaseFocus(form) {
  var el = document.activeElement;
  if (!el || el === document.body) return;
  if (!form || !form.contains || !form.contains(el)) return;
  if (el.blur) el.blur();
}

// The one trap in the whole mechanism, and it is silent: form.submit() does NOT
// fire a submit event. The templates' `onchange="this.form.submit()"` idiom
// therefore walks straight past every listener in this file, including the one
// below - so a checkbox using it would have gone on doing a full navigation
// while the markup said data-inplace. requestSubmit() is the one that behaves
// like a person pressing the button. Where it does not exist (Safari before 16)
// the plain submit is the old behavior, which is worse and not broken.
function submitForm(form) {
  if (form.requestSubmit) form.requestSubmit();
  else form.submit();
}
window.submitForm = submitForm;

document.addEventListener("submit", function (ev) {
  // Behind [data-confirm]: that handler is registered at the top of this file,
  // so by the time this runs a canceled delete has already prevented itself.
  if (ev.defaultPrevented) return;
  var action = inPlaceAction(ev);
  if (!action) return;
  ev.preventDefault();
  var form = ev.target;
  var data = new FormData(form);
  // The pressed button's own name and value. `new FormData(form)` leaves it
  // out - only the browser's own submission carries it - so without this a
  // tapped quick option posts an empty `choice` and answers the question blank,
  // and a note sent with "queue note" arrives with no `then` at all and starts
  // a run he did not ask for.
  if (ev.submitter && ev.submitter.name) data.set(ev.submitter.name, ev.submitter.value);
  releaseFocus(form);
  // AFTER the payload is built, and that order is the sharpest trap in this
  // file. The note effect EMPTIES the textarea it is echoing - run a line
  // earlier and `new FormData(form)` reads the box it just cleared, so every
  // note posts blank while the page shows it going out perfectly. The pending
  // effect disables the pressed button, which is the same bug on the other
  // form: `then` and `choice` ride on the submitter's own name/value.
  var undo = optimisticEffect(form, ev.submitter || null);
  // Forced: the patch that follows is the answer to a button this reader just
  // pressed, so it does not wait behind a text box they left focused somewhere
  // else on the page. See refreshHeld().
  //
  // The busy mark comes off when the patch has landed, not when the POST
  // returned - between those two the page still shows the old state, and a
  // button that looks live over stale text is the double-press this whole
  // section is here to stop. The morph would strip the class anyway (the fresh
  // HTML has no busy form in it); this is what covers a post that was REFUSED,
  // where there is no morph at all and the control has to come back.
  //
  // A compose form is emptied inside onDone, BEFORE the patch rather than after
  // it. The morph reads the live DOM to decide what to change, so a textarea
  // still holding the sent text at that moment is text the morph then protects,
  // and the note would sit in the box looking unsent next to its own copy in
  // the journal. It runs only on the success path, for the same reason the busy
  // mark does: a post the server refused must leave what you typed alone.
  var compose = !!(form.matches && form.matches("[data-compose]"));
  var done = function () {
    if (compose) clearComposeForm(form);
    return liveReload(true);
  };
  var posted = isMultipartForm(form)
    ? postMultipart(action, data, done)
    : postForm(action, formFields(data), done);
  // `ok` is false when the route REFUSED the post - postBody has already put
  // its reason in an alert. The optimistic change has to come back off in that
  // case, or the alert says "a run is in flight" over a button reading "agent
  // running..." that is describing a run which never started.
  posted.then(function (ok) {
    clearBusy(form);
    if (!ok && undo) undo();
  });
});

// A FormData flattened to the plain object postForm wants. Only reached for a
// form with no files in it, so a File value here would be a bug rather than a
// case to handle.
function formFields(data) {
  var fields = {};
  data.forEach(function (value, name) { fields[name] = value; });
  return fields;
}

// --- Attachments: drop, paste, record --------------------------------------
// Everything here funnels into the form's own <input type=file>. Nothing is
// uploaded on drop; the file rides along when the note is submitted. That keeps
// one code path on the server, means a half-written note never leaves orphaned
// uploads behind, and degrades to a plain file picker with scripting off.

function fileLabel(input) {
  var n = input.files ? input.files.length : 0;
  if (!n) return "";
  var names = [];
  for (var i = 0; i < n && i < 3; i++) names.push(input.files[i].name);
  if (n > 3) names.push("+" + (n - 3) + " more");
  return n + " file" + (n === 1 ? "" : "s") + ": " + names.join(", ");
}

// The size limit is not written on the page - it's noise until it matters. It
// shows up here, naming the offending file, only once something too big is
// actually picked. The server enforces it either way.
function oversizeWarning(input) {
  var max = parseInt(input.getAttribute("data-max-bytes") || "0", 10);
  if (!max || !input.files) return "";
  var over = [];
  for (var i = 0; i < input.files.length; i++) {
    if (input.files[i].size > max) over.push(input.files[i].name);
  }
  if (!over.length) return "";
  var label = input.getAttribute("data-max-label") || max + " bytes";
  return "too big (max " + label + " each): " + over.join(", ");
}

// FileList is read-only, so adding to it means rebuilding a DataTransfer with
// the existing entries plus the new ones - dropping twice should accumulate,
// not replace.
function addFiles(input, files) {
  if (!files || !files.length || typeof DataTransfer === "undefined") return false;
  var dt = new DataTransfer();
  var existing = input.files || [];
  for (var i = 0; i < existing.length; i++) dt.items.add(existing[i]);
  for (var j = 0; j < files.length; j++) dt.items.add(files[j]);
  input.files = dt.files;
  return true;
}

// The inverse, by name: deleting one voice memo must not throw away the
// screenshot picked alongside it. Names are unique here - recordings carry a
// millisecond timestamp.
function removeFile(input, name) {
  if (typeof DataTransfer === "undefined") return;
  var dt = new DataTransfer();
  var existing = input.files || [];
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].name !== name) dt.items.add(existing[i]);
  }
  input.files = dt.files;
}

// Rename one staged file, in place and in order.
//
// A File's name is read-only, so a rename is a new File built from the same
// bytes - and because the whole FileList has to be rebuilt to swap one entry,
// this walks the list rather than removing and re-adding, which would move the
// renamed file to the end. Wes reorders nothing here, but a picture jumping to
// the bottom of the shelf because you fixed its name is exactly the "nothing
// moves that he didn't move" complaint.
//
// Returns the name actually used, or "" if the rename could not happen: an
// empty name, a name already taken by another staged file (the server stores by
// name, and two files claiming one name is a silent overwrite), or a browser
// with no File constructor.
function renameFile(input, from, to) {
  if (typeof DataTransfer === "undefined" || typeof File !== "function") return "";
  var want = (to || "").trim();
  if (!want || want === from) return "";
  var existing = input.files || [];
  var i;
  for (i = 0; i < existing.length; i++) {
    if (existing[i].name === want) return "";
  }
  var dt = new DataTransfer();
  var renamed = "";
  for (i = 0; i < existing.length; i++) {
    var f = existing[i];
    if (f.name === from) {
      // lastModified is carried over so the file the server receives is the
      // same file, differing only in what it is called.
      dt.items.add(new File([f], want, { type: f.type, lastModified: f.lastModified }));
      renamed = want;
    } else {
      dt.items.add(f);
    }
  }
  if (!renamed) return "";
  input.files = dt.files;
  return renamed;
}

// Names the recorder put into the input, so the staged shelf can leave them to
// the recorder's own shelf instead of listing every voice memo twice.
//
// A set carried on the input rather than a name pattern: "starts with
// voice-memo-" would also swallow a file the user happened to call that, and
// silently hide it from the only list that can remove it.
function recordedNames(input) {
  if (!input._recordedNames) input._recordedNames = {};
  return input._recordedNames;
}

// One row per staged file: look at it, rename it, take it back off the note.
//
// Rebuilt from input.files every time rather than patched, because the FileList
// is the only source of truth about what will actually be posted - a shelf kept
// in step by hand would eventually disagree with it, and the direction it would
// disagree in is "shows a file that is no longer going to be sent".
//
// Object URLs are revoked on every rebuild. Dropping ten screenshots into a
// note and changing your mind about them would otherwise pin all ten in memory
// until the page was navigated away from.
function renderAttachShelf(input, shelf) {
  if (!shelf) return;
  // How a row's own remove/rename button asks for the redraw, without every
  // button in every row closing over the renderer. initDropzones overwrites
  // this with its fuller refresh (which also updates the oversize status line);
  // the default here is what makes the shelf correct on its own, so that
  // "whether a removed file disappears from the list" does not depend on who
  // happened to call this first.
  if (!input._afterFiles) {
    input._afterFiles = function () { renderAttachShelf(input, shelf); };
  }
  var i;
  var old = shelf.querySelectorAll(".attach-row-item");
  for (i = 0; i < old.length; i++) {
    if (old[i]._objectUrl && window.URL && URL.revokeObjectURL) URL.revokeObjectURL(old[i]._objectUrl);
  }
  shelf.textContent = "";
  var files = input.files || [];
  var recorded = recordedNames(input);
  var max = parseInt(input.getAttribute("data-max-bytes") || "0", 10);
  for (i = 0; i < files.length; i++) {
    // A voice memo already has a row with a player on it, on the recorder's own
    // shelf. Listing it again here would offer two different delete buttons for
    // one file.
    if (recorded[files[i].name]) continue;
    shelf.appendChild(attachRow(input, shelf, files[i], max));
  }
}

function attachRow(input, shelf, file, max) {
  var row = document.createElement("div");
  row.className = "attach-row-item";
  var oversize = max > 0 && file.size > max;
  if (oversize) row.classList.add("oversize");

  // View: the picture itself where there is one, since that is what "what have
  // I included" means for a screenshot. Everything else gets its extension,
  // which is the most any of this can honestly show without opening the file.
  var thumb = document.createElement("span");
  thumb.className = "attach-row-thumb";
  if (file.type && file.type.indexOf("image/") === 0 && window.URL && URL.createObjectURL) {
    var img = document.createElement("img");
    row._objectUrl = URL.createObjectURL(file);
    img.src = row._objectUrl;
    img.alt = file.name;
    thumb.appendChild(img);
  } else {
    var dot = file.name.lastIndexOf(".");
    thumb.textContent = dot > 0 ? file.name.slice(dot + 1).toLowerCase().slice(0, 4) : "file";
    thumb.classList.add("attach-row-ext");
  }

  var body = document.createElement("span");
  body.className = "attach-row-body";
  var nameEl = document.createElement("span");
  nameEl.className = "attach-row-name";
  nameEl.textContent = file.name;
  var sizeEl = document.createElement("span");
  sizeEl.className = "small muted attach-row-size";
  sizeEl.textContent = oversize ? fileSize(file.size) + " - too big to send" : fileSize(file.size);
  body.appendChild(nameEl);
  body.appendChild(sizeEl);

  var actions = document.createElement("span");
  actions.className = "attach-row-actions";

  // Rename is offered only where it can actually work. A browser with no File
  // constructor cannot rebuild a file under a new name, and a button that does
  // nothing is worse than one that is not there.
  if (typeof File === "function" && typeof DataTransfer !== "undefined") {
    var rename = document.createElement("button");
    rename.type = "button";
    rename.className = "btn secondary small";
    rename.textContent = "rename";
    rename.addEventListener("click", function () {
      // An inline field rather than prompt(): prompt() is blocked in an
      // installed iOS home-screen app, which is where he does most of this.
      startRename(input, shelf, row, nameEl, file.name);
    });
    actions.appendChild(rename);
  }

  var remove = document.createElement("button");
  remove.type = "button";
  remove.className = "btn danger small";
  remove.textContent = "remove";
  remove.addEventListener("click", function () {
    removeFile(input, file.name);
    if (input._afterFiles) input._afterFiles();
  });
  actions.appendChild(remove);

  row.appendChild(thumb);
  row.appendChild(body);
  row.appendChild(actions);
  return row;
}

// Swap the name for a text field, apply on Enter or blur, abandon on Escape.
function startRename(input, shelf, row, nameEl, current) {
  if (row.querySelector(".attach-row-rename")) return;
  var field = document.createElement("input");
  field.type = "text";
  field.className = "attach-row-rename";
  field.value = current;
  field.setAttribute("aria-label", "new name for " + current);
  nameEl.hidden = true;
  nameEl.insertAdjacentElement("afterend", field);
  field.focus();
  // The extension selected along with the stem would be retyped by anyone
  // fixing a name, so the selection stops at the dot.
  var dot = current.lastIndexOf(".");
  if (field.setSelectionRange) field.setSelectionRange(0, dot > 0 ? dot : current.length);

  var settled = false;
  function finish(apply) {
    if (settled) return;
    settled = true;
    var want = field.value;
    field.remove();
    nameEl.hidden = false;
    if (!apply) return;
    if (renameFile(input, current, want) && input._afterFiles) input._afterFiles();
  }
  field.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") {
      // Or the note is submitted by the form around it.
      ev.preventDefault();
      finish(true);
    } else if (ev.key === "Escape") {
      // Stopped here so the page-wide Escape handler does not also read this
      // as "close whatever is open".
      ev.preventDefault();
      ev.stopPropagation();
      finish(false);
    }
  });
  field.addEventListener("blur", function () { finish(true); });
}

// Bytes as something a person reads. The server's own filter is the Jinja
// `filesize`; this is the client-side twin, and the two only ever describe
// files, so they agree on where the units start.
function fileSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function initDropzones() {
  document.querySelectorAll("[data-dropzone]").forEach(function (form) {
    if (form._enhanced) return;
    form._enhanced = true;
    var input = form.querySelector('input[type="file"]');
    if (!input) return;
    var status = form.querySelector("[data-attach-status]");
    var shelf = form.querySelector("[data-attach-shelf]");

    function refresh() {
      renderAttachShelf(input, shelf);
      if (!status) return;
      var warning = oversizeWarning(input);
      // With a shelf on the page every file is already named on it, so the
      // status line goes back to being what it was for: the one thing the rows
      // cannot say, which is that something is too big to send at all.
      status.textContent = warning || (shelf ? "" : fileLabel(input));
      status.classList.toggle("error", !!warning);
    }
    // Hung off the input so a row's own remove/rename button can ask for the
    // redraw without every one of them closing over this function.
    input._afterFiles = refresh;
    input.addEventListener("change", refresh);
    refresh();

    ["dragenter", "dragover"].forEach(function (name) {
      form.addEventListener(name, function (ev) {
        if (!ev.dataTransfer) return;
        ev.preventDefault();
        form.classList.add("dragging");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      form.addEventListener(name, function () {
        form.classList.remove("dragging");
      });
    });
    form.addEventListener("drop", function (ev) {
      if (!ev.dataTransfer || !ev.dataTransfer.files.length) return;
      ev.preventDefault();
      if (addFiles(input, ev.dataTransfer.files)) refresh();
    });

    // Paste a screenshot straight into the note box. Only files are taken -
    // pasted text must still land in the textarea as text.
    form.addEventListener("paste", function (ev) {
      if (!ev.clipboardData || !ev.clipboardData.files || !ev.clipboardData.files.length) return;
      if (addFiles(input, ev.clipboardData.files)) refresh();
    });

    initRecorder(form, input, refresh);
  });
}

// Voice memos, mainly for phones. getUserMedia only exists in a secure context,
// so over plain http on a LAN hostname the button stays hidden rather than
// being present and throwing when pressed.
//
// The full recorder Wes asked for (2026-08-04): a waveform responding to the
// mic in real time, the running length, pause/resume, playback of a take
// before it is sent, and delete. Each finished take becomes a row in the shelf
// with its own player and delete button, and the file itself rides in the
// form's <input type=file> like any other attachment - the server never hears
// about a recording that was thrown away.
function initRecorder(form, input, refresh) {
  var btn = form.querySelector("[data-record]");
  if (!btn) return;
  var supported =
    navigator.mediaDevices &&
    navigator.mediaDevices.getUserMedia &&
    typeof MediaRecorder !== "undefined";
  if (!supported) {
    // The button stays hidden and says nothing. It used to insert a link to
    // the https copy of the page here, which Wes asked to be removed: it put
    // a sentence of explanation on the add-note row of every project, on a
    // machine where the feature is not the thing he came to use. The https
    // address is still named on Settings > access, which is where you go when
    // you want it.
    return;
  }
  var panel = form.querySelector("[data-rec-panel]");
  var shelf = form.querySelector("[data-rec-shelf]");
  var timeEl = form.querySelector("[data-rec-time]");
  var pauseBtn = form.querySelector("[data-rec-pause]");
  var doneBtn = form.querySelector("[data-rec-done]");
  var cancelBtn = form.querySelector("[data-rec-cancel]");
  var canvas = panel ? panel.querySelector("canvas") : null;
  if (!panel || !shelf || !timeEl || !pauseBtn || !doneBtn || !cancelBtn || !canvas) return;
  btn.hidden = false;

  var recorder = null; // a MediaRecorder while a take is open, else null
  var stream = null;
  var chunks = [];
  var discard = false; // set by the discard button before stop()
  var audioCtx = null;
  var meter = null;
  var raf = 0;
  var timer = 0;
  var running = false; // this closure's own "is the clock ticking"
  var startedAt = 0; // performance.now() when recording last (re)started
  var accumulated = 0; // ms of recording banked before the current stretch
  var bars = []; // recent mic peak levels, newest last

  // Recorded time excludes pauses: bank the stretch on pause, restart the
  // clock on resume. MediaRecorder itself offers no elapsed-time reading.
  // `running` is tracked here rather than read from recorder.state because by
  // the time onstop fires the state is already "inactive" - a state-based
  // clock reads 0:00 on any take that was never paused. Caught in a real
  // browser; pinned by tests/js/recorder.mjs.
  function recordedMs() {
    var ms = accumulated;
    if (running) ms += performance.now() - startedAt;
    return ms;
  }
  function fmt(ms) {
    var s = Math.floor(ms / 1000);
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  // The canvas is styled with `color`; reading it back means the waveform
  // follows the theme without this code knowing any palette.
  function sizeCanvas() {
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(60, Math.floor(canvas.clientWidth * dpr));
    canvas.height = Math.floor(canvas.clientHeight * dpr) || 36;
  }
  function drawWave() {
    var g = canvas.getContext("2d");
    if (!g) return;
    var w = canvas.width;
    var h = canvas.height;
    g.clearRect(0, 0, w, h);
    g.fillStyle = getComputedStyle(canvas).color;
    g.globalAlpha = recorder && recorder.state === "paused" ? 0.35 : 0.9;
    var dpr = window.devicePixelRatio || 1;
    var step = 3 * dpr; // 2px bar + 1px gap, in device pixels
    var max = Math.floor(w / step);
    if (bars.length > max) bars.splice(0, bars.length - max);
    for (var i = 0; i < bars.length; i++) {
      // sqrt lifts quiet speech into the visible range; a linear map reads
      // as a flatline for anything but shouting.
      var level = Math.sqrt(bars[i]);
      var bh = Math.max(2 * dpr, level * h);
      var x = w - (bars.length - i) * step;
      g.fillRect(x, (h - bh) / 2, 2 * dpr, bh);
    }
    g.globalAlpha = 1;
  }
  function meterLoop() {
    raf = requestAnimationFrame(meterLoop);
    if (meter && recorder && recorder.state === "recording") {
      var data = new Uint8Array(meter.fftSize);
      meter.getByteTimeDomainData(data);
      var peak = 0;
      for (var i = 0; i < data.length; i++) {
        var v = Math.abs(data[i] - 128) / 128;
        if (v > peak) peak = v;
      }
      bars.push(peak);
    }
    drawWave();
  }

  // A navigation while the mic is hot would lose the take silently, so the
  // form's submit buttons sleep until the recording is finished or discarded.
  function setSubmits(disabled) {
    form.querySelectorAll('button[type="submit"], button:not([type])').forEach(function (b) {
      b.disabled = disabled;
    });
  }

  function finishTake() {
    // Read the clock before stopping it - recordedMs owns the pause
    // arithmetic; this function only has to take the final reading.
    var length = fmt(recordedMs());
    running = false;
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    stream = null;
    if (audioCtx) {
      try { audioCtx.close(); } catch (e) { /* already closed */ }
      audioCtx = null;
      meter = null;
    }
    cancelAnimationFrame(raf);
    clearInterval(timer);
    panel.hidden = true;
    btn.classList.remove("recording");
    setSubmits(false);
    var take = chunks;
    chunks = [];
    var rec = recorder;
    recorder = null;
    if (discard || !take.length) return;
    var type = rec.mimeType || "audio/webm";
    var blob = new Blob(take, { type: type });
    var ext = type.indexOf("ogg") >= 0 ? "ogg" : type.indexOf("mp4") >= 0 ? "m4a" : "webm";
    var name = "voice-memo-" + new Date().toISOString().replace(/[:.]/g, "-") + "." + ext;
    var file = new File([blob], name, { type: type });
    if (!addFiles(input, [file])) return;
    // Claimed before the redraw, or the staged shelf lists this memo as an
    // ordinary file alongside the playback row added two lines down - two rows
    // and two delete buttons for one recording.
    recordedNames(input)[name] = true;
    refresh();
    addTakeRow(file, blob, length);
  }

  // One row per finished take: play it back, or delete it (which pulls the
  // file back out of the input - nothing of it ever reaches the server).
  function addTakeRow(file, blob, length) {
    var row = document.createElement("div");
    row.className = "rec-row";
    var audio = document.createElement("audio");
    audio.controls = true;
    var url = URL.createObjectURL(blob);
    audio.src = url;
    var meta = document.createElement("span");
    meta.className = "small muted";
    meta.textContent = length + " voice note";
    var del = document.createElement("button");
    del.type = "button";
    del.className = "btn danger small";
    del.textContent = "delete";
    del.addEventListener("click", function () {
      removeFile(input, file.name);
      delete recordedNames(input)[file.name];
      URL.revokeObjectURL(url);
      row.remove();
      refresh();
    });
    row.appendChild(audio);
    row.appendChild(meta);
    row.appendChild(del);
    shelf.appendChild(row);
  }

  function start() {
    navigator.mediaDevices
      .getUserMedia({ audio: true })
      .then(function (s) {
        stream = s;
        chunks = [];
        bars = [];
        discard = false;
        accumulated = 0;
        recorder = new MediaRecorder(s);
        recorder.ondataavailable = function (ev) {
          if (ev.data && ev.data.size) chunks.push(ev.data);
        };
        // Always through finishTake, even if the browser stops the recorder
        // itself (the mic being unplugged) - the mic must be released and the
        // buttons woken on every path.
        recorder.onstop = finishTake;
        recorder.start();
        running = true;
        startedAt = performance.now();
        // The meter is a side-chain: mic -> AnalyserNode, connected onward to
        // nothing, so it can never change what is recorded. Losing it (an
        // exotic browser without AudioContext) loses the waveform, not the
        // recording.
        try {
          var AC = window.AudioContext || window.webkitAudioContext;
          audioCtx = new AC();
          meter = audioCtx.createAnalyser();
          meter.fftSize = 1024;
          audioCtx.createMediaStreamSource(s).connect(meter);
        } catch (e) {
          meter = null;
        }
        // pause/resume shipped later than MediaRecorder itself; a browser
        // without them gets a recorder without that one button.
        pauseBtn.hidden = typeof recorder.pause !== "function";
        pauseBtn.textContent = "pause";
        timeEl.textContent = "0:00";
        panel.hidden = false;
        btn.classList.add("recording");
        setSubmits(true);
        sizeCanvas();
        // no-poll-gate: a display clock for the recording in progress. It
        // makes no requests, and it is cleared when the recorder stops.
        timer = setInterval(function () {
          timeEl.textContent = fmt(recordedMs());
        }, 200);
        raf = requestAnimationFrame(meterLoop);
      })
      .catch(function () {
        btn.disabled = true;
        btn.title = "microphone unavailable";
        var status = form.querySelector("[data-attach-status]");
        if (status) status.textContent = "microphone unavailable";
      });
  }

  btn.addEventListener("click", function () {
    // While a take is open the mic button is a second "done" - the familiar
    // toggle - and the panel's own buttons are the finer controls.
    if (recorder) {
      recorder.stop();
      return;
    }
    start();
  });
  pauseBtn.addEventListener("click", function () {
    if (!recorder) return;
    if (recorder.state === "recording") {
      accumulated += performance.now() - startedAt;
      running = false;
      recorder.pause();
      pauseBtn.textContent = "resume";
    } else if (recorder.state === "paused") {
      recorder.resume();
      running = true;
      startedAt = performance.now();
      pauseBtn.textContent = "pause";
    }
  });
  doneBtn.addEventListener("click", function () {
    if (recorder) recorder.stop();
  });
  cancelBtn.addEventListener("click", function () {
    if (!recorder) return;
    discard = true;
    recorder.stop();
  });
}

// --- Folds that remember whether you opened them ---------------------------
// A `<details data-fold-remember id="...">` keeps its open/shut state on this
// browser, per page path and id.
//
// This exists because of the todo list. Wes, 2026-08-01: "have the todo section
// be collapsed by default" - and every tick, tag, refile and add in that list
// is a POST that redirects back to the project page. Without a memory, each one
// would shut the list you were working in, so "collapsed by default" would read
// as "collapsed no matter what you do".
//
// So the server's `open` attribute is the state a page STARTS in, and this is
// the state a person put it in. A live refresh needs none of this: the morph
// already preserves `open` on any <details> (see preservedAttr).
//
// Deliberately opt-in. Three folds on the project page have their open state
// decided by the server from live facts - the console opens while a run is
// going, the ask box opens when there is a question pending, sub-projects open
// when there are some - and remembering those would be overruling a fact with
// a stale click.
var FOLD_PREFIX = "portal-fold:";

function foldKey(el) {
  return el.id ? FOLD_PREFIX + location.pathname + "|" + el.id : null;
}

function initFoldMemory() {
  document.querySelectorAll("details[data-fold-remember]").forEach(function (el) {
    var key = foldKey(el);
    if (!key) return;
    if (!el._foldBound) {
      el._foldBound = true;
      el.addEventListener("toggle", function () {
        try {
          localStorage.setItem(key, el.open ? "1" : "0");
        } catch (e) {}
      });
    }
    var saved = null;
    try {
      saved = localStorage.getItem(key);
    } catch (e) {}
    if (saved === "1") el.open = true;
    else if (saved === "0") el.open = false;
  });
}

document.addEventListener("DOMContentLoaded", initFoldMemory);

// --- Unsaved drafts --------------------------------------------------------
// Anything typed into a note, answer or idea box is kept in localStorage until
// it is actually submitted, so a stray reload (or the service restarting itself
// mid-sentence) doesn't eat it. Keyed on page path + form action + field name,
// which stays stable across reloads and never collides between projects.
// Opt out with data-no-draft (e.g. the delete-confirmation field, where a
// remembered value would pre-arm a destructive form).

var DRAFT_PREFIX = "portal-draft:";

function draftKey(el) {
  if (!el.name || el.hasAttribute("data-no-draft")) return null;
  if (el.type === "password" || el.type === "hidden") return null;
  var action = (el.form && el.form.getAttribute("action")) || "";
  return DRAFT_PREFIX + location.pathname + "|" + action + "|" + el.name;
}

function draftFields() {
  return document.querySelectorAll("textarea, input[type=text]");
}

function saveDraft(el) {
  var key = draftKey(el);
  if (!key) return;
  try {
    if (el.value.trim()) localStorage.setItem(key, el.value);
    else localStorage.removeItem(key);
  } catch (e) {
    /* storage full or disabled: drafts are a convenience, never a blocker */
  }
}

function restoreDrafts() {
  draftFields().forEach(function (el) {
    var key = draftKey(el);
    // Never overwrite a value the server rendered - that one is the truth.
    if (!key || el.value) return;
    var saved = null;
    try {
      saved = localStorage.getItem(key);
    } catch (e) {
      return;
    }
    if (!saved) return;
    el.value = saved;
    if (el.tagName === "TEXTAREA") autosize(el);
    var note = document.createElement("p");
    note.className = "draft-note";
    note.textContent = "restored unsaved draft";
    el.insertAdjacentElement("afterend", note);
  });
}

document.addEventListener("input", function (ev) {
  var el = ev.target;
  if (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && el.type === "text")) saveDraft(el);
});

// Clear on submit rather than on load: the browser may restore the page from
// bfcache, and a draft cleared too early would vanish from a still-open form.
document.addEventListener("submit", function (ev) {
  var form = ev.target;
  if (!form || !form.querySelectorAll) return;
  form.querySelectorAll("textarea, input[type=text]").forEach(function (el) {
    var key = draftKey(el);
    if (!key) return;
    try {
      localStorage.removeItem(key);
    } catch (e) {}
  });
});

// --- Offline / restarting overlay -----------------------------------------
// The portal restarts itself after it modifies its own source. Rather than let
// the browser show "can't reach this site", detect the gap and hold a page here
// that reloads as soon as the service answers again.

function watchForOffline() {
  var overlay = document.getElementById("offline-overlay");
  if (!overlay) return;
  var detail = document.getElementById("offline-detail");
  var IDLE_MS = 3000;
  var DOWN_MS = 500;
  var misses = 0;
  var downSince = 0;
  var wasDown = false;
  var timer = null;

  function tick() {
    // The heaviest of the lot, and the one the diagnosis missed: this runs on
    // EVERY page, every 3 seconds, and never stops. An overlay saying the
    // server is down is worth nothing in a tab nobody is looking at, and the
    // tab learns the moment it is looked at again (listener below).
    if (document.hidden) return;
    fetch("/api/ping", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("bad status");
        // Reload rather than just hiding: the page was rendered by the old
        // code and may not match what is now running.
        if (wasDown) window.location.reload();
        misses = 0;
        downSince = 0;
        overlay.hidden = true;
        schedule(IDLE_MS);
      })
      .catch(function () {
        misses += 1;
        if (!downSince) downSince = Date.now();
        // Two misses, not one: a single dropped request during a slow page is
        // not an outage, and flashing this overlay would be worse than useless.
        // But start probing hard from the first miss, so the moment the
        // service comes back we notice in half a second rather than three.
        schedule(DOWN_MS);
        if (misses >= 2) {
          overlay.hidden = false;
          wasDown = true;
          if (detail) {
            var secs = Math.round((Date.now() - downSince) / 1000);
            detail.textContent = "offline for " + secs + "s - retrying...";
          }
        }
      });
  }

  // Elapsed time comes from the clock, not from counting probes, because the
  // probe interval changes as soon as something looks wrong.
  function schedule(interval) {
    if (timer && interval === schedule.current) return;
    if (timer) clearInterval(timer);
    schedule.current = interval;
    timer = setInterval(tick, interval);
  }

  schedule(IDLE_MS);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick();
  });
}

// --- Live agent activity ---------------------------------------------------
// Both pollers are best-effort: a failed fetch just skips a tick rather than
// tearing the widget down, so a brief restart of the server doesn't leave a
// dead panel behind.

function startLiveRunPoll() {
  var strip = document.getElementById("live-run");
  if (!strip) return;
  // The *set* of live runs, not a single id: runs are parallel, so one of two
  // finishing must still be detected as a change.
  var lastRunIds = strip.getAttribute("data-run-ids") || "";

  function paint(data) {
    var usage = data.usage || {};
    document.querySelectorAll("[data-usage]").forEach(function (el) {
      var key = el.getAttribute("data-usage");
      if (usage[key] !== undefined) el.textContent = usage[key];
    });

    // A run starting or finishing changes the whole page (cells, counts) -
    // refresh it in place rather than reloading, so the view doesn't jump.
    var runIds = data.run_ids || "";
    if (runIds !== lastRunIds) {
      lastRunIds = runIds;
      liveReload();
      return;
    }
    if (!data.active) {
      var why = strip.querySelector(".live-idle-reason");
      if (why) why.textContent = data.idle_reason || "";
      return;
    }
    (data.runs || []).forEach(function (run) {
      var row = strip.querySelector('.live-run-row[data-run-id="' + run.run_id + '"]');
      if (!row) return;
      var activity = row.querySelector(".live-activity");
      if (activity) activity.textContent = run.last_activity;
      var meta = row.querySelector(".live-meta");
      if (meta) meta.textContent = run.model + " · " + run.elapsed + " · " + run.events + " events";
      paintHold(row, run);
    });
  }

  // The hold state of one row (app/midrun.py): a pause pressed on the project
  // page, the run page or another phone has to show here without a reload,
  // and the hold ENGAGING - the run reaching the tool call it stops at - is a
  // change nobody pressed a button for. Same three parts the template
  // renders: the dot, the pausing/paused badge, and the button's direction.
  // The button is only patched, never created: whether a run can be paused
  // at all (`can_pause`) is settled when the run starts and rendered by the
  // server, and a run whose hooks this portal cannot reach must not grow a
  // pause button from a poll.
  function paintHold(row, run) {
    var paused = !!run.paused;
    if (row.classList) row.classList.toggle("held", paused);
    var dot = row.querySelector(".dot");
    if (dot && dot.classList) {
      dot.classList.toggle("held", paused);
      dot.classList.toggle("running", !paused);
    }
    var badge = row.querySelector(".live-hold");
    if (badge) {
      badge.hidden = !paused;
      badge.textContent = run.engaged ? "paused" : "pausing";
    }
    var form = row.querySelector(".live-hold-form");
    if (form) {
      form.setAttribute("action", "/run/" + run.run_id + "/" + (paused ? "resume" : "pause"));
      var button = form.querySelector("button");
      if (button) button.textContent = paused ? "resume" : "pause";
    }
  }

  function tick() {
    // Same rule as the console poller and initLiveRefresh: a tab nobody is
    // looking at does not poll. This one never stops on its own - there is
    // always a next run to notice - so without the gate an open background tab
    // fetches every 5 seconds for as long as the browser is running.
    if (document.hidden) return;
    fetch("/api/active-run", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(paint)
      .catch(function () {});
  }
  tick();
  setInterval(tick, 5000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick();
  });
}

// --- Reading a transcript --------------------------------------------------
// Wes, 2026-07-28: "make all the tool calls and '>' lines show as indented
// gray/dimmed text with the other lines where the agent is just talking or
// thinking show as non-indented text that is not dimmed."
//
// The first character of a line says what kind it is. This table is the same
// one app/runlog.py writes from (runlog.MARKERS) - it has to live here as well
// because lines arriving mid-run are classified in the browser, and there is
// no round trip to ask Python about them. tests/test_console.py parses this
// object out of this file and fails if the two ever disagree.
var CONSOLE_KINDS = { ">": "tool", "<": "result", "!": "error", "*": "status", "~": "think" };

// What the agent said is the unmarked case, so a marker only counts when it is
// followed by a space (or is the whole line). Agent prose is markdown and is
// full of "> quoted" and "* bullet" lines that must NOT be read as tool calls;
// runlog.py escapes those with one leading space, which lands them here as
// ordinary prose because their first character is then a space.
function consoleKind(line) {
  var kind = CONSOLE_KINDS[line.charAt(0)];
  if (kind && (line.length === 1 || line.charAt(1) === " ")) return kind;
  return "say";
}

// The agent's prose is markdown, and drawing it as raw source text is what
// Wes reported (2026-08-04): "**My own 540→550 change was wrong.**" reading as
// asterisks rather than bold. This renders the inline half of markdown - bold,
// `code` and a # heading line - because those are what agent summaries are
// actually full of. Block constructs (lists, quotes, fences) stay as source:
// the console draws line by line so a chunk split across two polls redraws
// correctly, and per-line statelessness is what makes that work. Built as DOM
// nodes, never innerHTML - the transcript is untrusted text.
var MD_INLINE = /(`+)([^`]+?)\1|\*\*([^*\s](?:[^*]*[^*\s])?)\*\*/g;

function mdAppend(el, text) {
  // A FRESH regex per call, not the shared one: mdAppend recurses for the
  // inside of a bold span, and a recursive call resetting the shared object's
  // lastIndex mid-iteration turns the outer loop into an infinite one.
  var re = new RegExp(MD_INLINE.source, "g");
  var at = 0;
  var m;
  while ((m = re.exec(text))) {
    if (m.index > at) el.appendChild(document.createTextNode(text.slice(at, m.index)));
    var span = document.createElement("span");
    if (m[2] != null) {
      span.className = "cl-md-c";
      span.textContent = m[2];
    } else {
      span.className = "cl-md-b";
      // One level of nesting: bold around a `path` is common in summaries.
      mdAppend(span, m[3]);
    }
    el.appendChild(span);
    at = m.index + m[0].length;
  }
  if (at < text.length) el.appendChild(document.createTextNode(text.slice(at)));
}

// Thinking is the one marked kind that is drawn WITHOUT its marker.
//
// A marker earns its place when it carries something the styling does not.
// For the machinery it does: .cl-tool and .cl-result are deliberately styled
// the same (one quiet indented column), so ">" and "<" are the only thing
// saying which is the call and which is the answer. For thinking there is
// nothing left to say - it sits at the margin in italic, which no other kind
// does - and a "~" per SOURCE line means a wrapped paragraph of reasoning
// draws a column of tildes down the left of the page.
//
// The log itself keeps the marker on every line. Stripping it here rather
// than at the writer is what lets the classifier stay per-line and stateless,
// which is what makes a line split across two polls classify correctly; and
// `cat`ing the raw log still shows what each line is.
function consoleLine(line) {
  var kind = consoleKind(line);
  var el = document.createElement("span");
  el.className = "cl cl-" + kind;
  if (kind === "say") {
    // A heading line loses its hashes and gains the weight they meant. Only
    // prose is markdown-rendered: machinery and errors are quoted verbatim,
    // and thinking keeps its plain italic.
    var head = /^(#{1,6}) (.*)$/.exec(line);
    if (head) {
      el.className += " cl-md-h";
      mdAppend(el, head[2]);
    } else {
      mdAppend(el, line);
    }
  } else {
    // A marked line is exactly "<marker> <text>", so dropping two characters
    // is lossless. Escaped prose is never touched: its leading space may be
    // the escape from runlog.escape_prose or may be a fenced code block's own
    // indentation, and nothing here can tell those apart.
    el.textContent = kind === "think" ? line.slice(2) : line;
  }
  // The line as the LOG holds it, which is not always what is drawn: thinking
  // loses its "~ " here (one marker per source line draws a column of tildes
  // down the page). Without this, reading a console back out to redraw it - what
  // the show/hide toggle does - would quietly reclassify a paragraph of
  // reasoning as something the agent said out loud.
  el._raw = line;
  return el;
}

// --- Folding the machinery -------------------------------------------------
// Wes, 2026-08-01: "on the top line where it says 'last run transcript' or
// whatever it says when active, have an option that compressed down/hides and
// prints a sort of summary (but not using AI to summarize) of what commands
// have been run and whatnot in between the white text it sends. It could say
// something like '10 tools called' or '5 commands run' or whatever it is that
// happens. Have this option on by default where these are compressed down."
//
// So a run of consecutive tool calls and their results collapses to one line
// that COUNTS them. Not summarizes - counts. The numbers come from the same
// per-line classification the coloring already uses, so the fold can never
// claim something the transcript does not say.
//
// Deliberately NOT folded, at either setting:
//
//   - prose and thinking, which are the thing you are reading;
//   - `*` status, which is four lines a run (session start, run complete) and
//     each of them is a fact about the run rather than machinery.
//
// `!` errors DO fold, as of Wes, 2026-08-06: "hide these errored lines from
// the agent inside the tool call collapsed sections". They used to stay in the
// clear under "nothing fails quietly", but the 2026-08-06 triage of 201 run
// logs showed 82% of them are the agent's own try/read-the-error/adjust loop
// (a test failing before the fix, a grep with no match) - noise wearing alarm
// colors. The compromise that keeps a real failure visible: the fold's head
// counts them ("3 commands run · 1 error"), so a collapsed console still says
// an error happened without showing it.
var CONSOLE_FOLD_KEY = "portal-console-tools";

function consoleFolded() {
  try {
    return localStorage.getItem(CONSOLE_FOLD_KEY) !== "0";
  } catch (e) {
    return true; // on by default, and a browser refusing storage does not change that
  }
}

// The name out of "> Bash(pytest -q)". A tool line is written by runlog.py as
// exactly `> <Name>(<summary>)`, so this is a parse of a format this repo
// owns, not a guess at free text.
function consoleToolName(line) {
  var open = line.indexOf("(");
  return (open > 2 ? line.slice(2, open) : line.slice(2)).trim();
}

function consoleFoldLabel(counts) {
  var bits = [];
  // Bash on its own line, because "ran a command" is the thing Wes named first
  // and is a different act from reading a file.
  if (counts.bash) {
    bits.push(counts.bash + (counts.bash === 1 ? " command run" : " commands run"));
  }
  if (counts.tool) {
    bits.push(counts.tool + (counts.tool === 1 ? " tool called" : " tools called"));
  }
  if (!bits.length) {
    // Results with no call in front of them: a chunk that begins mid-exchange,
    // which is what the first poll of a run already in progress looks like.
    // Errors are not "tool output", so a fold holding only an error says
    // nothing here and lets the error count be its whole label.
    var output = counts.lines - (counts.error || 0);
    if (output) bits.push(output + (output === 1 ? " line" : " lines") + " of tool output");
  }
  return bits.join(" · ");
}

// The head is text plus, when the fold holds errors, a count of them in its
// own span - tinted its own quiet red so a collapsed console still says a
// failure happened without drawing the failure.
function consoleFoldHeadPaint(fold) {
  var label = consoleFoldLabel(fold.counts);
  fold.head.textContent = "";
  fold.head.appendChild(document.createTextNode(label));
  if (fold.counts.error) {
    var err = document.createElement("span");
    err.className = "cl-fold-err";
    err.textContent =
      (label ? " · " : "") +
      fold.counts.error + (fold.counts.error === 1 ? " error" : " errors");
    fold.head.appendChild(err);
  }
}

function consoleFoldNew(out) {
  var wrap = document.createElement("span");
  wrap.className = "cl cl-fold";
  var head = document.createElement("button");
  head.type = "button";
  head.className = "cl-fold-head";
  var body = document.createElement("span");
  body.className = "cl-fold-body";
  body.hidden = true;
  wrap.appendChild(head);
  wrap.appendChild(body);
  out.appendChild(wrap);
  // `lines` is the total and is its own bucket. It used to double as the bucket
  // for anything that is not a tool call, which meant a result line incremented
  // it twice and one line reported itself as "2 lines of tool output".
  return {
    wrap: wrap,
    head: head,
    body: body,
    counts: { bash: 0, tool: 0, other: 0, error: 0, lines: 0 },
  };
}

// Append one line, into the open fold if it belongs there. Returns the element
// so the caller can take it back off again when a split line arrives whole.
function consoleAppend(out, line) {
  var kind = consoleKind(line);
  var foldable =
    consoleFolded() && (kind === "tool" || kind === "result" || kind === "error");
  if (!foldable) {
    out._fold = null;
    var plain = consoleLine(line);
    out.appendChild(plain);
    return plain;
  }
  if (!out._fold) out._fold = consoleFoldNew(out);
  var fold = out._fold;
  var el = consoleLine(line);
  fold.body.appendChild(el);
  el._foldCount =
    kind === "error" ? "error"
      : kind !== "tool" ? "other"
      : consoleToolName(line) === "Bash" ? "bash" : "tool";
  fold.counts[el._foldCount] += 1;
  fold.counts.lines += 1;
  consoleFoldHeadPaint(fold);
  el._fold = fold;
  return el;
}

// Undo the last append. The poller reads by BYTE offset, so a chunk can end
// mid-line; that partial line is drawn (it is the newest thing on screen) and
// then taken back when the rest of it arrives.
function consoleUnappend(out, el) {
  if (!el || !el.parentNode) return;
  el.parentNode.removeChild(el);
  var fold = el._fold;
  if (!fold) return;
  fold.counts[el._foldCount] -= 1;
  fold.counts.lines -= 1;
  if (!fold.counts.lines) {
    if (fold.wrap.parentNode) fold.wrap.parentNode.removeChild(fold.wrap);
    if (out._fold === fold) out._fold = null;
    return;
  }
  consoleFoldHeadPaint(fold);
}

// Opening a fold is per-fold and sticky: you opened THAT group to read it, and
// a later patch appending to a different group must not shut it again.
document.addEventListener("click", function (ev) {
  var head = ev.target.closest ? ev.target.closest(".cl-fold-head") : null;
  if (!head) return;
  var body = head.nextSibling;
  if (!body) return;
  body.hidden = !body.hidden;
  head.classList.toggle("open", !body.hidden);
});

// Draw a chunk into the box, one element per line so each can be styled by
// what it is. `replace` starts the transcript over.
//
// The poller reads by BYTE offset, not by line, so a chunk can end in the
// middle of a line. That tail is drawn (it is the newest thing on screen and
// hiding it would make the console lag its own run) but remembered, and
// redrawn from the start when the rest of it arrives - otherwise a tool call
// split across two polls would be classified from half of its first character
// and then have its own remainder appended as a second line.
function renderConsole(out, text, replace) {
  if (replace) {
    out.textContent = "";
    out.dataset.tail = "";
    out._fold = null;
    out._tailEl = null;
  }
  if (out.dataset.tail) consoleUnappend(out, out._tailEl);
  out._tailEl = null;
  var lines = ((out.dataset.tail || "") + (text || "")).split("\n");
  var tail = lines.pop();
  for (var i = 0; i < lines.length; i++) consoleAppend(out, lines[i]);
  out.dataset.tail = tail;
  if (tail) out._tailEl = consoleAppend(out, tail);
}

// The transcript the server rendered into the <pre> is plain text; run it back
// through the folder so the box does not sit unfolded until the first poll -
// which on a finished run (where the poller fetches once) would be the whole
// point of the setting missed by a second, and on a shut <details> forever.
function foldServerConsole() {
  var out = document.getElementById("console-out");
  if (!out || out._folded) return;
  var text = out.textContent || "";
  if (!text || text.charAt(0) === "(") return; // "(nothing yet)" / "(empty)"
  out._folded = true;
  renderConsole(out, text, true);
}

function initConsoleFoldToggle() {
  var btn = document.getElementById("console-fold-toggle");
  if (!btn) return;
  function paint() {
    var folded = consoleFolded();
    btn.textContent = folded ? "[ show tool calls ]" : "[ hide tool calls ]";
    btn.setAttribute("aria-pressed", folded ? "true" : "false");
  }
  paint();
  // Only now: the folding happens in the browser, so with scripting off this
  // control would be a button that does nothing.
  btn.hidden = false;
  if (btn._bound) return;
  btn._bound = true;
  btn.addEventListener("click", function () {
    try {
      localStorage.setItem(CONSOLE_FOLD_KEY, consoleFolded() ? "0" : "1");
    } catch (e) {}
    paint();
    // Redraw from the text we already have rather than re-fetching: the
    // transcript is in the DOM, and the setting only changes how it is drawn.
    var out = document.getElementById("console-out");
    if (out) {
      var text = consoleText(out);
      out._folded = true;
      renderConsole(out, text, true);
      out.scrollTop = out.scrollHeight;
    }
  });
}

// The transcript as text, folds and all. `textContent` on the <pre> would run
// the fold headings ("12 tools called") in with the lines they stand for, so
// the headings are skipped and the bodies are read.
//
// `_raw` before `textContent` throughout: the drawn text of a thinking line is
// missing the "~ " the log holds, and reading that back would turn reasoning
// into prose on every toggle.
function lineText(node) {
  return node._raw != null ? node._raw : node.textContent || "";
}

function consoleText(out) {
  var parts = [];
  Array.prototype.forEach.call(out.childNodes, function (node) {
    if (node.nodeType !== 1) {
      parts.push(node.textContent || "");
      return;
    }
    if (node.classList.contains("cl-fold")) {
      var body = node.querySelector(".cl-fold-body");
      if (body) {
        Array.prototype.forEach.call(body.childNodes, function (line) {
          parts.push(lineText(line));
        });
      }
      return;
    }
    parts.push(lineText(node));
  });
  return parts.join("\n") + "\n";
}

// Exactly one console poller at a time, and it knows which run it is watching.
//
// Wes, 2026-08-13, relaying a diagnosis from his Mac: an open portal tab kept
// fetching /api/run/N/log every 2 seconds forever, on a run that had finished
// hours earlier, in a background tab. Two reasons it never stopped: the
// interval handle was thrown away, so clearInterval was impossible; and the
// end of a run calls liveReload(), which patches the DOM in place instead of
// navigating, so the page never unloads and nothing tears the timer down.
// That kept one HTTP/2 connection alive indefinitely over Tailscale, which is
// eventually what wedged Safari's networking process at 99% CPU.
//
// Keyed on the run because the box is REUSED: project.html morphs the same
// #agent-console to whichever run is current, so a poller left running after a
// run ends is not merely wasteful, it is chasing the wrong run's transcript.
var consolePoll = null; // { runId, live, tick, timer }

function stopConsolePoll() {
  if (consolePoll && consolePoll.timer) clearInterval(consolePoll.timer);
  if (consolePoll) consolePoll.timer = null;
}

function startConsolePoll() {
  var box = document.getElementById("agent-console");
  var runId = box ? box.getAttribute("data-run-id") : null;
  var live = !!box && box.getAttribute("data-live") === "1";
  if (!box || !runId) {
    stopConsolePoll();
    consolePoll = null;
    return;
  }
  // Already watching exactly this run in exactly this state. reinit() calls
  // this on every live patch, and restarting would re-fetch the whole
  // transcript from offset 0 and jump the reader back to the bottom.
  if (consolePoll && consolePoll.runId === runId && consolePoll.live === live) return;
  stopConsolePoll();
  var out = document.getElementById("console-out");
  var offset = null; // null until the first fetch replaces the server-rendered tail
  var state = { runId: runId, live: live, tick: tick, timer: null };
  consolePoll = state;

  function tick() {
    // A hidden tab is not being read, so it has no reason to hold a connection
    // open. Coming back to the tab ticks immediately (see the listener below),
    // so nothing is stale by the time anybody looks at it.
    if (document.hidden) return;
    var url = "/api/run/" + runId + "/log?offset=" + (offset === null ? 0 : offset);
    fetch(url, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (consolePoll !== state) return; // superseded mid-flight by a newer run
        var first = offset === null;
        var atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
        if (first) {
          if (data.text) renderConsole(out, data.text, true);
          else out.textContent = "(nothing yet)";
        } else if (data.text) {
          renderConsole(out, data.text, false);
        }
        offset = data.offset;
        // The first paint replaces the whole transcript, so it lands at the
        // end regardless of where the box happened to be scrolled.
        if (atBottom || first) out.scrollTop = out.scrollHeight;
        if (state.live && !data.running) {
          // Once, not every tick: the page refreshed in place still says
          // data.running is false on every later poll.
          state.live = false;
          stopConsolePoll();
          liveReload();
        }
      })
      .catch(function () {});
  }
  tick();
  if (live) state.timer = setInterval(tick, 2000);
}

// Registered once, at module scope. Inside startConsolePoll it would stack a
// fresh listener on every live patch - the same leak in a different coat.
document.addEventListener("visibilitychange", function () {
  if (!document.hidden && consolePoll) consolePoll.tick();
});

// A transcript is read from the end: the last thing the agent did is the thing
// you opened it for. The server renders the tail into the <pre>, which then
// sits scrolled to the *top* of that tail - so pin it to the bottom.
//
// While the <details> is shut the box has no layout and scrollHeight is 0, so
// setting scrollTop then does nothing. That is why this also runs on toggle.
function pinConsole() {
  var out = document.getElementById("console-out");
  if (!out) return;
  var bottom = function () { out.scrollTop = out.scrollHeight; };
  bottom();
  var details = document.getElementById("agent-console-details");
  if (details) {
    details.addEventListener("toggle", function () {
      if (details.open) bottom();
    });
  }
}

// --- Settings sub-tabs -----------------------------------------------------
// The panels ship visible and JS hides the inactive ones, so a scripting
// failure degrades to the old long scroll rather than to an unreachable panel.
// The choice lives in the URL hash, which means a save (which redirects with
// #<section>) lands you back on the panel you were editing.

// Set by initSubtabs; reinit() calls it after a live refresh, because the
// morph resets the tabs' classes and the panels' hidden state to the server's
// render (which shows everything) and the user's chosen panel must win.
var subtabsApply = null;
// Open a panel by name, from outside this section. jumpTo uses it so a rail
// chapter can reach a section on a tab you are not looking at.
var subtabsShow = null;

function initSubtabs() {
  var bar = document.getElementById("settings-tabs");
  if (!bar || bar._enhanced) return;
  bar._enhanced = true;
  var tabs = Array.prototype.slice.call(bar.querySelectorAll(".subtab"));
  var panels = Array.prototype.slice.call(document.querySelectorAll(".settings-panel"));
  if (!tabs.length || !panels.length) return;

  var current = null;

  function show(name) {
    var known = tabs.some(function (t) {
      return t.dataset.panel === name;
    });
    if (!known) name = tabs[0].dataset.panel;
    tabs.forEach(function (t) {
      t.classList.toggle("active", t.dataset.panel === name);
    });
    panels.forEach(function (p) {
      p.hidden = p.dataset.panel !== name;
    });
    current = name;
    return name;
  }

  subtabsApply = function () {
    bar.classList.add("ready");
    if (current) show(current);
  };

  // replaceState, not a hash assignment: changing location.hash pushes a
  // history entry per tab click, which turns Back into "walk every tab you
  // looked at" instead of "leave this page". A save redirects with
  // #<section>, so keeping the hash current is also what lands you back on the
  // panel you were editing.
  function selectPanel(name) {
    var chosen = show(name);
    history.replaceState(null, "", "#" + chosen);
    return chosen;
  }
  subtabsShow = selectPanel;

  tabs.forEach(function (t) {
    t.addEventListener("click", function () {
      selectPanel(t.dataset.panel);
    });
  });

  bar.classList.add("ready");
  show((location.hash || "").replace(/^#/, ""));
}

document.addEventListener("DOMContentLoaded", initSubtabs);

// --- Trying a look on, before you save it -----------------------------------
// Wes, 2026-07-28: "when one is chosen in from the drop-down in settings,
// instantly change that page to preview that theme that it was changed to."
//
// Every appearance setting is one class on <body> (see config.APPEARANCE_
// CLASS_PREFIX), which is what makes this cheap: swapping the class re-renders
// the whole page in the new look with no request and nothing saved. The page
// you are standing on IS the preview - and the sample strip in the panel
// carries the things a settings page has none of.
//
// The prefixes are read off the selects rather than listed here, so a new
// appearance setting previews on the day it is added.

// What the server rendered when this page loaded, so "changed back to what it
// was" stops saying there is something unsaved. Module-scoped and seeded once:
// a live patch re-renders the panel from the SAVED value, so re-reading this
// after one would call an unsaved preview clean and drop the marker.
var appearanceSaved = null;

// The same, for the one appearance setting that is not a dropdown. Seeded
// beside `appearanceSaved` and for the same reason: a live patch re-renders
// the panel from the SAVED order, so re-reading it after one would call an
// unsaved rearrangement clean and drop the "not saved yet" marker.
var sectionOrderSaved = null;

function appearanceSelects() {
  return document.querySelectorAll("select[data-appearance-prefix]");
}

// Whether the page arrangement in the panel differs from what is stored.
// Module-scoped rather than local to either initializer, because both of them
// need the answer: the arrange buttons to light the marker the moment a row
// moves, and the appearance preview so that repainting for a theme change does
// not clear a marker the arrangement is still earning.
function arrangementDirty() {
  var field = document.getElementById("section-order");
  if (!field || sectionOrderSaved === null) return false;
  return field.value !== sectionOrderSaved;
}

function initAppearancePreview() {
  var selects = appearanceSelects();
  if (!selects.length) return;

  if (appearanceSaved === null) {
    appearanceSaved = {};
    selects.forEach(function (sel) {
      appearanceSaved[sel.name] = sel.value;
    });
  }

  function paint() {
    // Re-queried on every paint rather than closed over: a patch can replace
    // these nodes (a renamed theme changes the option list, which rebuilds the
    // widget), and a closure over the old NodeList would then be painting the
    // page from elements no longer in the document.
    var field = document.querySelector(".theme-field");
    var chrome = {};
    var stock = {};
    var types = {};
    var favicons = {};
    try {
      chrome = JSON.parse((field && field.dataset.themeChrome) || "{}");
      stock = JSON.parse((field && field.dataset.themeStock) || "{}");
      types = JSON.parse((field && field.dataset.themeType) || "{}");
      favicons = JSON.parse((field && field.dataset.themeFavicon) || "{}");
    } catch (e) {
      /* a malformed table costs the browser chrome its tint, not the preview */
    }
    var dirty = false;
    appearanceSelects().forEach(function (sel) {
      // Dirty first, prefix second. A setting the browser cannot preview - one
      // the server renders from, like which projects the rail lists - carries
      // an empty prefix and returns below, and it still has to count as an
      // unsaved change or the "not saved yet" line lies about it.
      if (sel.value !== appearanceSaved[sel.name]) dirty = true;
      var prefix = sel.dataset.appearancePrefix;
      if (!prefix) return;
      // Remove whatever class this layer currently contributes, whatever it
      // is: matching on the prefix means the class list cannot accumulate two
      // themes if a value is renamed server-side.
      Array.prototype.slice.call(document.body.classList).forEach(function (cls) {
        if (cls.indexOf(prefix + "-") === 0) document.body.classList.remove(cls);
      });
      document.body.classList.add(prefix + "-" + sel.value);
      if (prefix !== "theme") return;
      // <html> is outside <body> and carries the overscroll fill and the
      // color-scheme that paints the scrollbars and the checkbox glyphs -
      // none of which any body-scoped stylesheet can reach.
      var tint = chrome[sel.value];
      if (tint) {
        document.documentElement.style.backgroundColor = tint;
        var meta = document.querySelector('meta[name="theme-color"]');
        if (meta) meta.setAttribute("content", tint);
      }
      // The tab icon is drawn per theme too, and it is the one part of the
      // preview that lives outside the page - so without this the tab keeps
      // the old theme's mark while everything under it has changed. EVERY
      // themed link moves, not just the PNG: Chrome prefers the SVG one, so
      // swapping the PNG alone would leave the tab on the old mark in the
      // browser most likely to be looking at it. Each link says which file it
      // is the themed form of, rather than the name being parsed back out of
      // an href that already carries a version and a boot id.
      var marks = favicons[sel.value] || {};
      document.querySelectorAll("link[data-icon-base]").forEach(function (link) {
        var url = marks[link.dataset.iconBase];
        if (url && link.getAttribute("href") !== url) link.setAttribute("href", url);
      });
      // The stock class, which is the half a preview cannot skip: the light
      // themes get all their structure from it (no scanlines, no glow, the
      // chrome re-faced, the terminal's borrowed punctuation emptied). Without
      // this the preview showed meadow's colors wearing the CRT's clothes -
      // a scanlined console, bracketed tabs and a hatched footer.
      var scheme = stock[sel.value] || "dark";
      document.body.classList.toggle("theme-stock-light", scheme === "light");
      document.documentElement.style.colorScheme = scheme;
      // And the type class, the other half of the same job. It is a separate
      // question from the stock - workbench and blueprint are dark themes with
      // nothing monospaced in them - so a preview that only swapped the stock
      // showed them in Fira Code with bracketed tabs.
      var voice = types[sel.value] || "mono";
      document.body.classList.toggle("theme-type-prose", voice === "prose");
      // The CRT dials are inert under any prose theme, and the panel fades them
      // to say so. Server-rendered from the SAVED theme, so a preview has to
      // move it too or the page shows scanlines it is not applying. Keyed on
      // the type and not the stock, because that is what kills the layers.
      document.querySelectorAll(".field-grid").forEach(function (grid) {
        if (grid.querySelector('[name="crt_scanlines"]')) {
          grid.classList.toggle("layers-inert", voice === "prose");
        }
      });
    });
    document.body.classList.toggle("appearance-previewing", dirty || arrangementDirty());
  }

  // Re-runnable: reinit() calls this after every patch, so a select the morph
  // did replace gets its listener back. The flag makes re-binding a no-op for
  // the ones that survived.
  selects.forEach(function (sel) {
    if (sel._previewBound) return;
    sel._previewBound = true;
    sel.addEventListener("change", paint);
  });

  // The morph resets <body>'s class attribute to the server's render, which
  // would snap an unsaved preview back mid-look. reinit() calls this whole
  // function, the same way it re-applies the settings sub-tab.
  paint();
}

document.addEventListener("DOMContentLoaded", initAppearancePreview);

// --- Where the sections of a project page sit -------------------------------
// Wes, 2026-07-28, on Karli's own theme: "all of the functional pieces are
// still there, but she can change how they appear, where they appear, how they
// look." Themes cover the two "how"s; this is the "where".
//
// The list here IS the value: the hidden input is rewritten from the row order
// after every nudge, so the form posts a full permutation and the server never
// has to reconstruct one from a sequence of moves. That also makes the panel
// its own preview - the order you can see in the list is the order the page
// will be in - without a request or a re-render.
//
// Arrows rather than drag, deliberately. Wes reads this page on his phone, and
// dragging a row inside a page that is itself scrolling is the worst gesture on
// a touch screen: it fights the scroll, it has no keyboard equivalent, and it
// can half-happen. Two buttons cannot.
function arrangeRows(list) {
  return Array.prototype.slice.call(list.querySelectorAll("[data-arrange-row]"));
}

function syncArrangeOrder(list, field) {
  var names = arrangeRows(list).map(function (row) {
    return row.getAttribute("data-arrange-row");
  });
  field.value = names.join(",");
  markArrangementUnsaved();
}

// A hidden input assigned from a script fires no event, so the "previewing -
// not saved yet" marker has to be told. Only ever turns the marker ON: the
// dropdowns can be earning it too, and `paint()` is the one place that decides
// it is off.
function markArrangementUnsaved() {
  if (arrangementDirty()) document.body.classList.add("appearance-previewing");
}

function initSectionArrange() {
  var list = document.getElementById("arrange-list");
  var field = document.getElementById("section-order");
  if (!list || !field) return;
  if (sectionOrderSaved === null) sectionOrderSaved = field.value;
  // Re-runnable, like initAppearancePreview: reinit() calls this after a live
  // patch, and the flag makes re-binding a no-op for a list the morph left
  // alone while a replaced one gets its listener back.
  if (list._arrangeBound) return;
  list._arrangeBound = true;

  list.addEventListener("click", function (ev) {
    var button = ev.target.closest && ev.target.closest(".arrange-up, .arrange-down");
    if (!button) return;
    var row = button.closest("[data-arrange-row]");
    if (!row) return;
    var up = button.classList.contains("arrange-up");
    // insertBefore with a null reference appends, which would wrap the last
    // row round to the end of the list on a "down" - a no-op that reads as a
    // control that did nothing. Both ends are checked instead.
    var neighbor = up ? row.previousElementSibling : row.nextElementSibling;
    if (!neighbor) return;
    if (up) list.insertBefore(row, neighbor);
    else list.insertBefore(neighbor, row);
    syncArrangeOrder(list, field);
    // The row has moved out from under the thumb (and, for a keyboard, out
    // from under the focus ring), so the focus goes with it. Without this,
    // pressing "up" three times moves three different sections.
    var moved = row.querySelector(up ? ".arrange-up" : ".arrange-down");
    if (moved) moved.focus();
  });

  var reset = document.getElementById("arrange-reset");
  if (reset) {
    reset.addEventListener("click", function () {
      // Blank is what the server reads as "follow the shipped page", which is
      // not the same as posting today's default order back - see
      // sections.clean. The rows are left where they are until the save
      // reloads the page: silently re-sorting the list under the tap would be
      // movement nobody asked for, and the hint says the save is what applies.
      field.value = "";
      markArrangementUnsaved();
      reset.disabled = true;
      reset.textContent = "will follow the page again when you save";
    });
  }
}

document.addEventListener("DOMContentLoaded", initSectionArrange);

// --- The "i" bubbles --------------------------------------------------------
// Wes, 2026-07-28: "There is a lot of texts on the settings pages defining what
// different variables/parameters do. Please add these as a little 'i' in a
// circle to hover over to get more information."
//
// Hover is CSS (:hover on the wrapper, and :focus-within so the keyboard gets
// there too). This is the other half: a tap, because a phone has no hover at
// all and Wes reads this page on one. Only one is open at a time - two bubbles
// overlapping is how a tooltip becomes unreadable.

// The bubble hangs from the dot's left edge, which is right for a label near
// the left of a column and runs off the screen for one in the last column - or
// for any of them on a phone. Measured and nudged back rather than flipped to
// right-anchored: flipping needs to know which side it is on, and this needs to
// know only how far off it went.
function keepOnScreen(bubble) {
  bubble.style.marginLeft = "";
  var limit = (window.innerWidth || document.documentElement.clientWidth) - 8;
  // The viewport is not the only edge, and on a desktop it is not even the
  // one that bites: `.terminal-window` is `overflow: hidden`, so a bubble that
  // fits the screen perfectly is still cut off at the window frame. Measured
  // in a browser - the fix looked correct and the text was still clipped.
  for (var el = bubble.parentElement; el; el = el.parentElement) {
    if (!window.getComputedStyle) break;
    if (window.getComputedStyle(el).overflow === "visible") continue;
    limit = Math.min(limit, el.getBoundingClientRect().right - 8);
    break;
  }
  var over = bubble.getBoundingClientRect().right - limit;
  if (over > 0) bubble.style.marginLeft = -Math.round(over) + "px";
}

function initInfoDots() {
  // Hover opens it with CSS alone, so the nudge has to run then too.
  document.addEventListener("mouseover", function (ev) {
    var wrap = ev.target.closest ? ev.target.closest(".info-wrap") : null;
    if (wrap) keepOnScreen(wrap.querySelector(".info-bubble"));
  });
  document.addEventListener("click", function (ev) {
    var dot = ev.target.closest ? ev.target.closest(".info-dot") : null;
    var open = document.querySelectorAll(".info-wrap.open");
    Array.prototype.forEach.call(open, function (wrap) {
      if (dot && wrap.contains(dot)) return;
      wrap.classList.remove("open");
      wrap.querySelector(".info-bubble").hidden = true;
      wrap.querySelector(".info-dot").setAttribute("aria-expanded", "false");
    });
    if (!dot) return;
    // Inside a form whose other button is a save; type="button" already stops
    // the submit, and this stops a click on the dot toggling the <label> or
    // <details> it may be sitting in.
    ev.preventDefault();
    ev.stopPropagation();
    var wrap = dot.closest(".info-wrap");
    var bubble = wrap.querySelector(".info-bubble");
    var nowOpen = !wrap.classList.contains("open");
    wrap.classList.toggle("open", nowOpen);
    bubble.hidden = !nowOpen;
    dot.setAttribute("aria-expanded", nowOpen ? "true" : "false");
    if (nowOpen) keepOnScreen(bubble);
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    document.querySelectorAll(".info-wrap.open").forEach(function (wrap) {
      wrap.classList.remove("open");
      wrap.querySelector(".info-bubble").hidden = true;
      wrap.querySelector(".info-dot").setAttribute("aria-expanded", "false");
    });
  });
}

document.addEventListener("DOMContentLoaded", initInfoDots);

// --- Pull to refresh (installed-to-home-screen only) ------------------------
// Chromeless standalone mode has no reload button and no browser pull-to-
// refresh, which on a phone leaves a stale page with no way back short of
// tapping a link. This puts the gesture back - but ONLY in standalone mode,
// because in a normal browser tab the platform already does it and two
// refreshes fighting over one drag feels broken.

var PULL_THRESHOLD = 70; // px dragged before a release actually reloads
var PULL_MAX = 110; // how far the indicator can travel, so it can't be flung

function isStandalone() {
  if (window.navigator.standalone === true) return true; // iOS home screen
  return !!(window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);
}

function initPullToRefresh() {
  if (!isStandalone() || !("ontouchstart" in window)) return;

  var el = document.createElement("div");
  el.id = "pull-refresh";
  el.setAttribute("aria-hidden", "true");
  el.innerHTML = '<span class="pull-spin"></span><span class="pull-label">pull to refresh</span>';
  document.body.appendChild(el);

  var startY = 0;
  var pulling = false;
  var distance = 0;

  function paint(d) {
    el.style.transform = "translate(-50%, " + Math.min(d, PULL_MAX) + "px)";
    el.classList.toggle("armed", d >= PULL_THRESHOLD);
    el.querySelector(".pull-label").textContent =
      d >= PULL_THRESHOLD ? "release to refresh" : "pull to refresh";
  }

  function reset() {
    el.classList.remove("visible", "armed");
    el.style.transform = "";
    pulling = false;
    distance = 0;
  }

  document.addEventListener(
    "touchstart",
    function (ev) {
      // Only from the very top, and never mid-pinch: a two-finger gesture is
      // a zoom, not a pull.
      if (window.scrollY > 0 || ev.touches.length !== 1) return;
      startY = ev.touches[0].clientY;
      pulling = true;
      distance = 0;
    },
    { passive: true }
  );

  document.addEventListener(
    "touchmove",
    function (ev) {
      if (!pulling) return;
      // Scrolled away mid-gesture (or started scrolling up): hand the drag
      // back to the page rather than half-owning it.
      if (window.scrollY > 0) {
        reset();
        return;
      }
      distance = ev.touches[0].clientY - startY;
      if (distance <= 0) {
        reset();
        return;
      }
      el.classList.add("visible");
      // Resistance: the indicator moves at a third of the finger, which is
      // what makes it feel attached to something rather than free.
      paint(distance / 3);
      if (ev.cancelable) ev.preventDefault(); // suppress the rubber-band
    },
    { passive: false }
  );

  function release() {
    if (!pulling) return;
    var fire = distance / 3 >= PULL_THRESHOLD;
    if (fire) {
      el.classList.add("loading");
      el.querySelector(".pull-label").textContent = "refreshing...";
      location.reload();
      return; // leave the indicator up; the new page replaces it
    }
    reset();
  }

  document.addEventListener("touchend", release, { passive: true });
  document.addEventListener("touchcancel", reset, { passive: true });
}

document.addEventListener("DOMContentLoaded", initPullToRefresh);

// --------------------------------------------------------------------------
// Themed dropdowns
// --------------------------------------------------------------------------
// A native <select>'s popup list is painted by the operating system: the CSS
// on this page reaches the closed box and stops there, which is why every
// dropdown opened into a white platform menu in the middle of a dark terminal
// UI, and why the colored statuses lost their color the moment you went
// looking for one. There is no styling fix for that - the list has to be real
// elements to be themeable at all.
//
// So the real <select> stays in the DOM, keeps its name and value, and is what
// the form submits; it is only hidden from view. Everything visible is built
// beside it. With scripting off, or before this runs, the page is exactly the
// working native control it always was.
function enhanceSelect(sel) {
  if (sel.multiple || sel.size > 1 || sel.dataset.enhanced) return;
  sel.dataset.enhanced = "1";

  var wrap = document.createElement("div");
  wrap.className = "sel";
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);

  var trigger = document.createElement("button");
  trigger.type = "button"; // never a submit: these sit inside forms
  trigger.className = "sel-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  if (sel.disabled) trigger.disabled = true;
  wrap.appendChild(trigger);

  var menu = document.createElement("ul");
  menu.className = "sel-menu";
  menu.setAttribute("role", "listbox");
  menu.hidden = true;
  wrap.appendChild(menu);

  var items = [];
  Array.prototype.forEach.call(sel.options, function (opt, i) {
    var li = document.createElement("li");
    li.className = "sel-opt";
    // The option carries its own color class from the template, so the list
    // reads the same way the closed control does - Wes's ask was that the
    // options be colored "like they appear once selected".
    if (opt.dataset.optClass) li.className += " " + opt.dataset.optClass;
    if (opt.disabled) li.className += " disabled";
    li.setAttribute("role", "option");
    li.dataset.index = String(i);
    li.textContent = opt.textContent;
    menu.appendChild(li);
    items.push(li);
  });

  function optClassOf(i) {
    var opt = sel.options[i];
    return (opt && opt.dataset.optClass) || "";
  }

  function sync() {
    var i = sel.selectedIndex;
    trigger.textContent = i >= 0 ? sel.options[i].textContent : "";
    // The trigger wears both the select's own classes (status-select and the
    // like, which existing CSS colors) and the selected option's class.
    trigger.className = "sel-trigger " + sel.className + " " + optClassOf(i);
    trigger.disabled = sel.disabled;
    items.forEach(function (li, n) {
      li.setAttribute("aria-selected", n === i ? "true" : "false");
      li.classList.toggle("selected", n === i);
    });
  }

  function close() {
    menu.hidden = true;
    wrap.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
  }

  function open() {
    // One open menu at a time, or two absolutely-positioned lists overlap.
    document.querySelectorAll(".sel.open").forEach(function (other) {
      if (other === wrap) return;
      other.classList.remove("open");
      other.querySelector(".sel-menu").hidden = true;
      other.querySelector(".sel-trigger").setAttribute("aria-expanded", "false");
    });
    menu.hidden = false;
    wrap.classList.add("open");
    trigger.setAttribute("aria-expanded", "true");
    var current = items[sel.selectedIndex];
    if (current) current.scrollIntoView({ block: "nearest" });
  }

  function choose(i) {
    if (i < 0 || i >= sel.options.length || sel.options[i].disabled) return;
    var changed = i !== sel.selectedIndex;
    sel.selectedIndex = i;
    sync();
    close();
    trigger.focus();
    // Dispatched so the inline onchange="this.form.submit()" on the project
    // controls keeps working - that is how every one of these saves.
    if (changed) sel.dispatchEvent(new Event("change", { bubbles: true }));
  }

  trigger.addEventListener("click", function () {
    if (menu.hidden) open(); else close();
  });
  menu.addEventListener("click", function (ev) {
    var li = ev.target.closest(".sel-opt");
    if (li) choose(parseInt(li.dataset.index, 10));
  });
  trigger.addEventListener("keydown", function (ev) {
    var i = sel.selectedIndex;
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (menu.hidden) { open(); return; }
      choose(ev.key === "ArrowDown" ? Math.min(i + 1, sel.options.length - 1) : Math.max(i - 1, 0));
    } else if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      if (menu.hidden) open(); else close();
    } else if (ev.key === "Escape" && !menu.hidden) {
      ev.preventDefault();
      close();
    }
  });
  // Anything that sets the value in code (draft restore, another script) still
  // has to show up on the visible control.
  sel.addEventListener("change", sync);
  sync();
}

// --------------------------------------------------------------------------
// Dashboard: drag a project between sections, right-click a project for actions
// --------------------------------------------------------------------------
// Both features post to the same routes the buttons on the project page use -
// there is no separate API, so the drag and the picker can never disagree
// about what "move to review" means.

// Returns a promise that settles when the action is DONE - including the patch
// that shows it, because onDone/liveReload are chained rather than fired and
// forgotten. That is what lets a caller hold a busy control until the page has
// actually caught up. It never rejects: every branch here ends in an alert or a
// patch, so a caller can chain a cleanup with a plain .then().
//
// It resolves TRUE when the route accepted the post and false when it refused
// or never answered. That distinction used to die inside postBody with the
// alert, which was fine while the only thing chained on it was clearBusy - the
// mark comes off either way. It is not fine now that a caller may have already
// changed the page on the strength of this post (see the optimistic section):
// an undo needs to know the difference between "done" and "no".
function postForm(action, fields, onDone) {
  var body = new URLSearchParams();
  Object.keys(fields || {}).forEach(function (k) { body.append(k, fields[k]); });
  return postBody(
    action,
    body.toString(),
    { "Content-Type": "application/x-www-form-urlencoded" },
    onDone
  );
}

// The same post, carrying a FormData instead - which is how files travel.
//
// The Content-Type header is deliberately ABSENT rather than set to
// multipart/form-data: only the browser can write the boundary parameter that
// belongs beside it, and a hand-set header without one makes the server read
// the body as a single unparseable part. Every upload arrives empty, with no
// error anywhere, which is why this is a separate function with the reason
// written on it rather than an `if` inside postForm.
function postMultipart(action, formData, onDone) {
  return postBody(action, formData, null, onDone);
}

function postBody(action, body, headers, onDone) {
  var init = { method: "POST", body: body };
  if (headers) init.headers = headers;
  return fetch(action, init)
    .then(function (r) {
      if (!r.ok) {
        // The route said no (a run in flight, a parent with children). Its
        // reason is a JSON detail; surface it rather than reloading into a
        // page that silently did not change.
        return r.json().then(
          function (data) { alert(data.detail || "That didn't work."); },
          function () { alert("That didn't work."); }
        ).then(function () { return false; });
      }
      // Resolved THROUGH the patch, not beside it: a caller waiting on this
      // promise is waiting for the page to have caught up, so `true` must not
      // arrive while the morph is still running.
      return Promise.resolve(onDone ? onDone() : liveReload()).then(function () {
        return true;
      });
    })
    .catch(function () {
      alert("The portal didn't answer - is it restarting?");
      return false;
    });
}

// Which zone means which stored status is written on the zone itself
// (data-status-zone), so this file never holds a second copy of that mapping.
// Module-level, not closed over per call: reinit() re-runs initProjectDrag
// after a live refresh brings in new cards, and a card enhanced by a later
// call must set the same variable the zones (enhanced by an earlier one) read.
var dragged = null;

function initProjectDrag() {
  var zones = Array.prototype.slice.call(document.querySelectorAll("[data-status-zone]"));
  var cells = Array.prototype.slice.call(document.querySelectorAll(".project-cell[data-slug]"));
  if (!zones.length || !cells.length) return;

  cells.forEach(function (cell) {
    if (cell._enhanced) return;
    cell._enhanced = true;
    cell.addEventListener("dragstart", function (ev) {
      dragged = cell;
      // Some text payload is required or Safari cancels the drag outright.
      if (ev.dataTransfer) {
        ev.dataTransfer.setData("text/plain", cell.getAttribute("data-slug"));
        ev.dataTransfer.effectAllowed = "move";
      }
      cell.classList.add("drag-source");
      document.body.classList.add("dragging-project");
    });
    cell.addEventListener("dragend", function () {
      dragged = null;
      cell.classList.remove("drag-source");
      document.body.classList.remove("dragging-project");
      zones.forEach(function (z) { z.classList.remove("drop-ready"); });
    });
  });

  zones.forEach(function (zone) {
    if (zone._enhanced) return;
    zone._enhanced = true;
    zone.addEventListener("dragover", function (ev) {
      if (!dragged) return; // a file or a text drag is not a project
      ev.preventDefault();
      if (ev.dataTransfer) ev.dataTransfer.dropEffect = "move";
      zone.classList.add("drop-ready");
    });
    zone.addEventListener("dragleave", function (ev) {
      // Leaving into a child of the same zone is not leaving.
      if (ev.relatedTarget && zone.contains(ev.relatedTarget)) return;
      zone.classList.remove("drop-ready");
    });
    zone.addEventListener("drop", function (ev) {
      ev.preventDefault();
      zone.classList.remove("drop-ready");
      if (!dragged) return;
      var slug = dragged.getAttribute("data-slug");
      var status = zone.getAttribute("data-status-zone");
      // Dropping a card back where it came from is a no-op, not a POST: the
      // status route journals every change, and "building -> building" lines
      // would turn the journal into a record of shaky mouse work.
      if (!slug || !status || dragged.getAttribute("data-status") === status) return;
      postForm("/project/" + slug + "/status", { status: status });
    });
  });
}

// The right-click menu. Real elements styled by the page's own CSS, because
// that was the ask: a context menu that matches the theme. Left-click,
// Escape and scrolling all close it; right-clicking empty page keeps the
// browser's own menu.
var MENU_STATUSES = [
  ["active", "active"],
  ["review", "review"],
  ["paused", "paused"],
  ["backlog", "backlog"],
  ["done", "done"],
  ["abandoned", "abandoned"],
];

function initProjectMenu() {
  if (!document.querySelector(".project-cell[data-slug]")) return;
  var menu = null;

  function close() {
    if (menu) { menu.remove(); menu = null; }
  }
  document.addEventListener("click", close);
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") close(); });
  window.addEventListener("scroll", close, true);

  function item(label, cls, onPick) {
    var li = document.createElement("li");
    li.className = "ctx-item " + (cls || "");
    li.textContent = label;
    li.addEventListener("click", function (ev) {
      ev.stopPropagation();
      onPick(li);
    });
    return li;
  }

  function build(cell) {
    var slug = cell.getAttribute("data-slug");
    var title = cell.getAttribute("data-title") || slug;
    var status = cell.getAttribute("data-status");
    var el = document.createElement("ul");
    el.className = "ctx-menu";

    var head = document.createElement("li");
    head.className = "ctx-head";
    head.textContent = title;
    el.appendChild(head);

    el.appendChild(item("open", "", function () {
      window.location.href = cell.getAttribute("href");
    }));
    el.appendChild(item("run agent now", "", function () {
      close();
      postForm("/project/" + slug + "/run", {});
    }));
    el.appendChild(item("rename...", "", function () {
      // The menu becomes the rename form rather than falling back to a
      // browser prompt() - the ask was that this stays in the theme.
      el.innerHTML = "";
      el.appendChild(head);
      var row = document.createElement("li");
      row.className = "ctx-rename";
      var input = document.createElement("input");
      input.type = "text";
      input.value = title;
      input.setAttribute("aria-label", "new project name");
      row.appendChild(input);
      el.appendChild(row);
      input.focus();
      input.select();
      input.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          var name = input.value.trim();
          close();
          if (name && name !== title) postForm("/project/" + slug + "/rename", { title: name });
        }
        // Escape bubbles to the document handler, which closes the menu.
      });
      input.addEventListener("click", function (ev) { ev.stopPropagation(); });
    }));

    var label = document.createElement("li");
    label.className = "ctx-label";
    label.textContent = "move to";
    el.appendChild(label);
    MENU_STATUSES.forEach(function (pair) {
      if (pair[0] === status) return; // where it already is
      el.appendChild(item(pair[1], "ctx-status status-" + pair[0], function () {
        close();
        postForm("/project/" + slug + "/status", { status: pair[0] });
      }));
    });

    el.appendChild(item("delete...", "ctx-danger", function () {
      close();
      // The typed-slug confirmation on the project page is friction by
      // design; from a quick menu one explicit confirm naming the project
      // carries the same decision.
      if (confirm("Delete " + title + " (" + slug + ") for good? This cannot be undone.")) {
        postForm("/project/" + slug + "/delete", { confirm: slug });
      }
    }));
    return el;
  }

  document.addEventListener("contextmenu", function (ev) {
    var cell = ev.target.closest ? ev.target.closest(".project-cell[data-slug]") : null;
    if (!cell) { close(); return; }
    ev.preventDefault();
    close();
    menu = build(cell);
    document.body.appendChild(menu);
    // Clamp to the viewport after measuring, so a card at the bottom edge
    // opens its menu upward instead of half off-screen.
    var x = ev.clientX, y = ev.clientY;
    var r = menu.getBoundingClientRect();
    if (x + r.width > window.innerWidth - 8) x = Math.max(8, window.innerWidth - r.width - 8);
    if (y + r.height > window.innerHeight - 8) y = Math.max(8, window.innerHeight - r.height - 8);
    menu.style.left = x + "px";
    menu.style.top = y + "px";
  });
}

// The right-click menu on a todo row. Wes, 2026-08-04: "compress the +tag,
// whose? and x buttons into a right click menu for a todo list item. Also,
// fold the tags into this menu, aside from the 'blocked' tag. Too many tags
// can be present and restrict the space available for the todo item itself."
//
// So the row itself is a checkbox, the text, and at most a [blocked] chip;
// the tags, add-tag, re-file and delete all live here. On a phone there is no
// right click, so a long press (the platform's own analog) opens the same
// menu - and .todo-item suppresses text selection under coarse pointers so
// the press does not fight the OS over the words.
//
// Everything posts through postForm, which is fetch + the live-refresh morph:
// no navigation, so acting on a row never moves the scroll - the same promise
// the checkbox already makes.
function initTodoMenu() {
  var menu = null;
  var openedAt = 0;

  function close() {
    if (menu) { menu.remove(); menu = null; }
  }
  document.addEventListener("click", function (ev) {
    // A long press ends in a click on the row under the finger; closing on
    // that click would shut the menu the instant it opened.
    if (menu && menu.contains(ev.target)) return;
    if (Date.now() - openedAt < 600) return;
    close();
  });
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") close(); });
  window.addEventListener("scroll", close, true);

  function item(label, cls, onPick) {
    var li = document.createElement("li");
    li.className = "ctx-item " + (cls || "");
    li.textContent = label;
    li.addEventListener("click", function (ev) {
      ev.stopPropagation();
      onPick(li);
    });
    return li;
  }

  function label(text) {
    var li = document.createElement("li");
    li.className = "ctx-label";
    li.textContent = text;
    return li;
  }

  function build(row) {
    var id = row.getAttribute("data-todo");
    var textEl = row.querySelector(".todo-text");
    var title = "#" + id + " " + (textEl ? textEl.textContent : "");
    var done = row.classList.contains("done");
    var tags = (row.getAttribute("data-tags") || "").split(",").filter(Boolean);
    var list = row.closest(".todo-list");
    var here = list ? list.getAttribute("data-here") || "" : "";
    var card = row.closest(".todo-card");
    var choices = [];
    try {
      choices = JSON.parse((card && card.getAttribute("data-refile")) || "[]");
    } catch (e) { /* a menu without move-to beats no menu */ }

    var el = document.createElement("ul");
    el.className = "ctx-menu";
    var head = document.createElement("li");
    head.className = "ctx-head";
    head.textContent = title;
    el.appendChild(head);

    if (tags.length) {
      el.appendChild(label("tags - pick to remove"));
      tags.forEach(function (tag) {
        el.appendChild(item("× " + tag, "ctx-tag", function () {
          close();
          postForm("/todo/" + id + "/tag", { remove: tag });
        }));
      });
    }
    if (!done) {
      el.appendChild(item("add a tag...", "", function () {
        // The menu becomes the input, the way the project menu's rename does.
        el.innerHTML = "";
        el.appendChild(head);
        var rowEl = document.createElement("li");
        rowEl.className = "ctx-rename";
        var input = document.createElement("input");
        input.type = "text";
        input.maxLength = 24;
        input.placeholder = "tag";
        input.setAttribute("aria-label", "new tag for: " + title);
        rowEl.appendChild(input);
        el.appendChild(rowEl);
        input.focus();
        input.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter") {
            ev.preventDefault();
            var tag = input.value.trim();
            close();
            if (tag) postForm("/todo/" + id + "/tag", { add: tag });
          }
          // Escape bubbles to the document handler, which closes the menu.
        });
        input.addEventListener("click", function (ev) { ev.stopPropagation(); });
      }));
      if (choices.length) {
        el.appendChild(label("move to"));
        choices.forEach(function (c) {
          if (c.value === here) return; // where it already is
          el.appendChild(item(c.label, "", function () {
            close();
            postForm("/todo/" + id + "/person", { person: c.value });
          }));
        });
      }
    }
    el.appendChild(item("delete...", "ctx-danger", function () {
      close();
      if (confirm("Delete this todo? “" + title + "”")) {
        postForm("/todo/" + id + "/delete", {});
      }
    }));
    return el;
  }

  function open(row, x, y) {
    close();
    menu = build(row);
    document.body.appendChild(menu);
    openedAt = Date.now();
    var r = menu.getBoundingClientRect();
    if (x + r.width > window.innerWidth - 8) x = Math.max(8, window.innerWidth - r.width - 8);
    if (y + r.height > window.innerHeight - 8) y = Math.max(8, window.innerHeight - r.height - 8);
    menu.style.left = x + "px";
    menu.style.top = y + "px";
  }

  document.addEventListener("contextmenu", function (ev) {
    var row = ev.target.closest ? ev.target.closest(".todo-item[data-todo]") : null;
    if (!row) return;
    ev.preventDefault();
    open(row, ev.clientX, ev.clientY);
  });

  // The long press. Pointer events rather than touch events so one code path
  // covers pens too; canceled by lifting, by the browser taking the gesture
  // (scrolling does exactly that), or by drifting more than a few px.
  var pressTimer = null;
  var pressX = 0;
  var pressY = 0;
  function cancelPress() {
    if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
  }
  document.addEventListener("pointerdown", function (ev) {
    if (ev.pointerType === "mouse") return;
    var row = ev.target.closest ? ev.target.closest(".todo-item[data-todo]") : null;
    if (!row) return;
    pressX = ev.clientX;
    pressY = ev.clientY;
    pressTimer = setTimeout(function () {
      pressTimer = null;
      open(row, pressX, pressY);
    }, 500);
  });
  document.addEventListener("pointermove", function (ev) {
    if (!pressTimer) return;
    if (Math.abs(ev.clientX - pressX) + Math.abs(ev.clientY - pressY) > 12) cancelPress();
  });
  document.addEventListener("pointerup", cancelPress);
  document.addEventListener("pointercancel", cancelPress);
}

// --- The note form's held-down submit menu ---------------------------------
// On a phone the note form's three submit choices were a messy second row;
// Wes, 2026-08-04: shrink to three buttons on one line and put the alternates
// ("add & run now", "queue note") behind a press-and-hold on the green
// button. Under 560px CSS hides the alternate buttons and this menu is how
// they are reached. A mouse never arms the press - on a desktop all three
// buttons are visible anyway - and the same drift/scroll rules as the todo
// menu apply: moving your finger means scrolling, not holding.
function initNoteMenu() {
  var menu = null;
  var openedAt = 0;
  var suppressUntil = 0;

  function close() {
    if (menu) { menu.remove(); menu = null; }
  }
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") close(); });
  window.addEventListener("scroll", close, true);
  document.addEventListener("click", function (ev) {
    if (menu && menu.contains(ev.target)) return;
    if (Date.now() - openedAt < 600) return;
    close();
  });
  // The click that ends the long press lands on a SUBMIT button: unless it is
  // swallowed, holding for the menu would also add the note the plain way.
  // Capture phase, so it dies before the browser's own submit behavior.
  document.addEventListener(
    "click",
    function (ev) {
      if (Date.now() >= suppressUntil) return;
      if (ev.target.closest && ev.target.closest(".note-form button.go")) {
        ev.preventDefault();
        ev.stopPropagation();
      }
    },
    true
  );

  function open(btn, x, y) {
    close();
    var form = btn.closest("form");
    if (!form) return;
    // Read off the form rather than hardcoded: which alternates exist is a
    // server-side decision now ("add & run now" is only rendered when the green
    // button would not run anyway), and a menu item pointing at a button that
    // is not there would silently do nothing when tapped.
    var alts = form.querySelectorAll('button[name="then"]');
    if (!alts.length) return;
    menu = document.createElement("ul");
    menu.className = "ctx-menu";
    Array.prototype.forEach.call(alts, function (alt) {
      var li = document.createElement("li");
      li.className = "ctx-item";
      li.textContent = (alt.textContent || "").trim();
      li.addEventListener("click", function (ev) {
        ev.stopPropagation();
        close();
        // requestSubmit with the (display:none) button as submitter carries
        // its then=... exactly as if it had been pressed; the fallback click()
        // does the same on the odd browser without it.
        if (form.requestSubmit) form.requestSubmit(alt);
        else alt.click();
      });
      menu.appendChild(li);
    });
    document.body.appendChild(menu);
    openedAt = Date.now();
    suppressUntil = openedAt + 600;
    var r = menu.getBoundingClientRect();
    if (x + r.width > window.innerWidth - 8) x = Math.max(8, window.innerWidth - r.width - 8);
    // Above the finger by default: the button lives near the bottom of its
    // card and a thumb covers whatever sits under it.
    var top = y - r.height - 10;
    if (top < 8) top = Math.min(y + 10, window.innerHeight - r.height - 8);
    menu.style.left = x + "px";
    menu.style.top = top + "px";
  }

  var pressTimer = null;
  var pressX = 0;
  var pressY = 0;
  function cancelPress() {
    if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
  }
  document.addEventListener("pointerdown", function (ev) {
    if (ev.pointerType === "mouse") return;
    var btn = ev.target.closest ? ev.target.closest(".note-form button.go") : null;
    if (!btn) return;
    pressX = ev.clientX;
    pressY = ev.clientY;
    pressTimer = setTimeout(function () {
      pressTimer = null;
      open(btn, pressX, pressY);
    }, 500);
  });
  document.addEventListener("pointermove", function (ev) {
    if (!pressTimer) return;
    if (Math.abs(ev.clientX - pressX) + Math.abs(ev.clientY - pressY) > 12) cancelPress();
  });
  document.addEventListener("pointerup", cancelPress);
  document.addEventListener("pointercancel", cancelPress);
}

document.addEventListener("DOMContentLoaded", initProjectDrag);
document.addEventListener("DOMContentLoaded", initProjectMenu);
document.addEventListener("DOMContentLoaded", initTodoMenu);
document.addEventListener("DOMContentLoaded", initNoteMenu);

// --------------------------------------------------------------------------
// Live refresh: patch the page in place when the data changes
// --------------------------------------------------------------------------
// The portal used to update the page the blunt way - window.location.reload()
// when a run started or finished, and not at all for anything else. Wes asked
// for pages that refresh on their own, without the view shifting around as
// content loads in. So: poll /api/version (a cheap database change counter),
// and when it moves, fetch this same page again and MORPH the live DOM to
// match - walking both trees and patching only what differs, so unchanged
// nodes keep their identity, their listeners, their focus and their scroll.
//
// Nothing here runs while Wes is mid-something: typing, dragging a card,
// holding a menu or a selection open all defer the patch until the moment the
// interaction ends. A full reload still happens when the server's boot id
// changes, because new code means the CSS and JS this page is holding may no
// longer match what the server would render.

// Live nodes that exist only client-side and must survive a morph.
// `.attach-row-item` for the same reason `.rec-row` is here: a staged file is
// client-only state that the server's render knows nothing about, so a
// background patch that rebuilt the note form would throw away a screenshot
// dropped in but not yet sent.
var MORPH_KEEP = ".draft-note, .ctx-menu, #pull-refresh, #img-lightbox, " +
  "#sel-actions, .quote-chip, .rec-row, .attach-row-item";

function isKeepNode(node) {
  return node.nodeType === 1 && node.matches && node.matches(MORPH_KEEP);
}

function isFormField(el) {
  var t = el.tagName;
  return t === "INPUT" || t === "TEXTAREA" || t === "SELECT" || t === "OPTION";
}

// Attributes the morph must not touch, because their live state belongs to
// the user or to another script, not to the server's render.
function preservedAttr(live, name, removing) {
  // A fold the user opened (or shut) stays that way.
  if (live.tagName === "DETAILS" && name === "open") return true;
  // Field state: the value/checked/selected attributes would stomp the live
  // properties back to the server's defaults mid-edit. A type=hidden input is
  // the exception and has to be: nobody is mid-edit in one, it is pure server
  // state, and the todo checkbox posts its TARGET state out of one
  // (`done=0|1`). Preserved, that value went stale the moment the row was
  // patched, so unticking an item you had just ticked posted "done" a second
  // time and the row would not come back.
  if (isFormField(live) && (name === "value" || name === "checked" || name === "selected")) {
    if (!(live.tagName === "INPUT" && live.getAttribute("type") === "hidden")) return true;
  }
  // autosize()'s work on textareas.
  if (live.tagName === "TEXTAREA" && name === "style") return true;
  // hidden is JS-owned everywhere it is dynamic here (settings panels, the
  // rename input, the recorder button, the offline overlay) - the server
  // renders a static default and scripts take it from there, in both
  // directions, so neither adding nor removing it can be the morph's call.
  if (name === "hidden") return true;
  // data-* markers set by scripts (data-tree-loaded and friends) are not in
  // the server's render; only removal is skipped, so a data attribute the
  // server DOES render still updates to its new value.
  if (removing && name.indexOf("data-") === 0) return true;
  return false;
}

function syncAttrs(live, next) {
  var i, name;
  for (i = live.attributes.length - 1; i >= 0; i--) {
    name = live.attributes[i].name;
    if (preservedAttr(live, name, true)) continue;
    if (!next.hasAttribute(name)) live.removeAttribute(name);
  }
  for (i = 0; i < next.attributes.length; i++) {
    name = next.attributes[i].name;
    if (preservedAttr(live, name, false)) continue;
    var v = next.attributes[i].value;
    if (live.getAttribute(name) !== v) live.setAttribute(name, v);
  }
}

// What makes two selects "the same control": everything enhanceSelect bakes
// into the widget it builds. Signature unchanged -> the widget stays; changed
// -> rebuild it from the new select.
function selSignature(sel) {
  var parts = [sel.name, sel.className, sel.disabled ? "1" : "0"];
  Array.prototype.forEach.call(sel.options, function (opt) {
    parts.push(
      opt.value + "" + opt.textContent + "" +
      (opt.disabled ? "1" : "0") + "" + (opt.getAttribute("data-opt-class") || "")
    );
  });
  return parts.join("");
}

function morphNode(live, next) {
  if (live.nodeType === 3 || live.nodeType === 8) {
    if (live.nodeValue !== next.nodeValue) live.nodeValue = next.nodeValue;
    return;
  }
  if (live.nodeType !== 1) return;

  // A themed dropdown: the live wrapper was built by enhanceSelect around the
  // very select the server is now rendering again.
  if (live.classList.contains("sel") && next.tagName === "SELECT") {
    var liveSel = live.querySelector("select");
    if (liveSel && selSignature(liveSel) === selSignature(next)) return;
    var value = liveSel ? liveSel.value : null;
    live.parentNode.replaceChild(next, live);
    if (value !== null) {
      // Keep the user's pick if the new option list still offers it.
      var still = Array.prototype.some.call(next.options, function (o) { return o.value === value; });
      if (still) next.value = value;
    }
    enhanceSelect(next);
    return;
  }

  syncAttrs(live, next);
  // The live text of a textarea is the user's typing, not server content.
  if (live.tagName === "TEXTAREA") return;
  // The console poller owns this text (incremental appends by offset).
  if (live.id === "console-out") return;
  // Folder contents were fetched on demand; the server renders them empty.
  if (live.matches && live.matches(".tree-dir[data-tree-loaded]")) return;
  // Same for a lazy fold the reader has opened (the dashboard's activity feed).
  // Without this the next patch would replace a feed he is reading with the
  // empty shell the server sends, which is Wes's "nothing moves that he did not
  // move" in its most literal form.
  if (live.matches && live.matches("details[data-lazy-loaded]")) return;
  morphChildren(live, next);
}

function findMatch(fromNode, nextChild) {
  if (nextChild.nodeType === 3 || nextChild.nodeType === 8) {
    return fromNode && fromNode.nodeType === nextChild.nodeType ? fromNode : null;
  }
  if (nextChild.nodeType !== 1 || !fromNode) return null;
  var id = nextChild.id;
  if (id) {
    // An id is identity: find it among the remaining siblings even if things
    // moved, so a reordered card keeps its node (and its listeners).
    for (var n = fromNode; n; n = n.nextSibling) {
      if (n.nodeType !== 1) continue;
      if (n.id === id && n.tagName === nextChild.tagName) return n;
      // ...except that enhanceSelect moved the real <select> INSIDE a wrapper
      // it built, so for a themed dropdown the id the server is rendering is
      // now a child's, not this node's. Without this, an id-bearing select
      // never pairs with its own widget: it falls through to the plain-tag
      // branch below, which the `if (id)` return makes unreachable, and the
      // morph deletes the widget and the user's unsaved pick with it. Every
      // appearance select has an id, which is why picking a theme did not
      // stick - the next patch, 2.5s later, put the saved one back.
      if (nextChild.tagName === "SELECT" && n.classList.contains("sel")) {
        var inner = n.querySelector("select");
        if (inner && inner.id === id) return n;
      }
    }
    return null;
  }
  if (fromNode.nodeType !== 1 || fromNode.id) return null;
  if (fromNode.tagName === nextChild.tagName) return fromNode;
  if (nextChild.tagName === "SELECT" && fromNode.classList.contains("sel")) return fromNode;
  return null;
}

function morphChildren(live, next) {
  var liveChild = live.firstChild;
  var nextChild = next.firstChild;
  while (nextChild) {
    // Capture before any move: inserting nextChild into the live tree
    // destroys its sibling links in the parsed one.
    var upcoming = nextChild.nextSibling;
    while (liveChild && isKeepNode(liveChild)) liveChild = liveChild.nextSibling;
    var match = findMatch(liveChild, nextChild);
    if (match) {
      if (match !== liveChild) live.insertBefore(match, liveChild);
      else liveChild = match.nextSibling;
      // Advance happened first: morphNode may replace `match` outright (the
      // themed-select rebuild), which would strand a pointer taken after.
      morphNode(match, nextChild);
    } else {
      live.insertBefore(nextChild, liveChild);
    }
    nextChild = upcoming;
  }
  while (liveChild) {
    var doomed = liveChild;
    liveChild = liveChild.nextSibling;
    if (!isKeepNode(doomed)) live.removeChild(doomed);
  }
}

// True while a patch would fight the user for the page.
// Somebody is part-way through a sentence. A patch nobody asked for waits for
// them; a patch they asked for by pressing a button does not (see refreshHeld).
// Interrupting a sentence is all a patch can do to a text box - it cannot eat
// one, because preservedAttr refuses to write a field's value across a morph.
function typingBlocked() {
  var ae = document.activeElement;
  if (!ae) return false;
  if (ae.tagName === "TEXTAREA" || ae.isContentEditable) return true;
  return !!(ae.tagName === "INPUT" && SUBMIT_ON_CHORD.test(ae.type));
}

// Transient state a patch DESTROYS rather than merely interrupts: an open
// dropdown and a context menu are rebuilt by reinit() and lose their open-ness,
// a drag loses the card in flight, a selection is gone the moment its text node
// is replaced. Honored even for a patch the reader asked for - being right
// about what they pressed is no reason to throw away what they were holding.
function interactionBlocked() {
  if (document.querySelector(".sel.open, .ctx-menu, [data-record].recording")) return true;
  if (document.body.classList.contains("dragging-project")) return true;
  var sel = window.getSelection && window.getSelection();
  return !!(sel && !sel.isCollapsed);
}

// A press whose answer has not landed yet. Between the press and the patch the
// page is knowingly showing a state the server has not confirmed (see the
// optimistic section), so an unforced background patch would rub that out and
// the forced one would put it back a moment later - a flicker on the exact
// control the reader just pressed, which is "nothing moves that he didn't move"
// in miniature. The patch the press ASKED for is forced, and refreshHeld()
// never routes a forced patch through here.
//
// Time-bounded, and that bound is the whole reason this is not just "is a form
// busy". A fetch that never settles leaves the mark on forever, and without a
// ceiling the page would then never refresh again - so an optimistic state old
// enough to be stale stops being something to protect and becomes exactly the
// mismatch a patch should be allowed to correct.
var PRESS_HOLD_MS = 10000;

function pressBlocked() {
  if (!pressStartedAt || Date.now() - pressStartedAt >= PRESS_HOLD_MS) return false;
  return !!(document.querySelector && document.querySelector("form[" + BUSY_ATTR + "]"));
}

function refreshBlocked() {
  return typingBlocked() || interactionBlocked() || pressBlocked();
}

// --- Holding the view still across a patch ---------------------------------
// Wes, 2026-07-28:
//
//   "when in the journal or somewhere on a page and the page is updating
//    because a run is ongoing, just finished and is adding summary text above,
//    or something else that changes the dimensions/size of content on the page,
//    it is moving my current view around. I instead want it to, if it is adding
//    something outside my screen, to not disturb my current view but instead
//    sort of extend the view above outside my screen."
//
// That is scroll anchoring. Chrome implements it in the engine (`overflow-
// anchor`); WebKit does not, and Wes reads this portal on an iPhone - so on the
// browser he actually uses, this code IS the feature rather than a fallback for
// one. It is also why the bug is invisible in a headless chromium: the engine
// quietly cleans up after us there.
//
// The rule, in one line: measure where the reader's line of text is BEFORE the
// patch, and scroll by however far it moved AFTER everything has finished
// moving it.

// The panels that scroll internally get the same treatment as the page - and
// one pinned to its bottom (a transcript being followed) stays pinned.
var SCROLL_SEL = ".scroll-cap, #console-out";

// How deep the anchor walk descends. Purely a runaway guard: the walk stops on
// its own when a node has no on-screen element children.
var ANCHOR_MAX_DEPTH = 24;

// An element that does not move when the document does is no use as an anchor -
// it would report `moved === 0` for every patch and silently disable the whole
// mechanism. The site header is exactly this.
function isPinned(el) {
  if (!window.getComputedStyle) return false;
  var pos = window.getComputedStyle(el).position;
  return pos === "fixed" || pos === "sticky";
}

// The deepest element whose box is inside the band, found by descending into
// the first on-screen child at each level.
//
// Deepest, not "the topmost thing with an id", which is what this used to look
// for. An id here is a fact about what somebody once needed to link to, not
// about what is being read: on a page whose only ids are its <section>s, the
// nearest one can be a screenful above the line in view, and it is the wrong
// thing to hold still if the growth happened in between.
function anchorNode(root, top, bottom) {
  var node = root;
  for (var depth = 0; depth < ANCHOR_MAX_DEPTH; depth++) {
    var kids = node.children;
    var next = null;
    for (var i = 0; i < kids.length; i++) {
      var el = kids[i];
      // The offline overlay covers the viewport and belongs to no position in
      // the document, so anchoring to it means anchoring to nothing.
      if (el.id === "offline-overlay") continue;
      var r = el.getBoundingClientRect();
      if (!r.height) continue;
      if (r.bottom <= top || r.top >= bottom) continue;
      if (isPinned(el)) continue;
      next = el;
      break; // first in document order that is on screen
    }
    if (!next) break;
    node = next;
  }
  return node === root ? null : node;
}

// A chain of NODE references (leaf first, then each ancestor), each with the
// top edge it had. Nodes, not ids, because the morph reuses the nodes it
// matches - that is its whole purpose - so the anchor survives the patch
// without needing a name. The ancestors are the fallback: if the patch really
// did replace the paragraph being read, its section is still there and still
// moved by the same amount.
function viewAnchor(scroller) {
  var top, bottom, root;
  if (scroller) {
    if (!scroller.scrollTop) return null; // at its top; the top is the anchor
    var box = scroller.getBoundingClientRect();
    top = box.top;
    bottom = box.bottom;
    root = scroller;
  } else {
    if (!(window.scrollY > 0)) return null;
    top = 0;
    bottom = window.innerHeight || document.documentElement.clientHeight;
    root = document.body;
  }
  var node = anchorNode(root, top, bottom);
  if (!node) return null;
  var chain = [];
  for (var el = node; el && el !== root; el = el.parentElement) {
    chain.push({ el: el, top: el.getBoundingClientRect().top });
  }
  return { scroller: scroller || null, chain: chain };
}

// Put the anchor back where it was. Idempotent on purpose: it corrects against
// the position recorded at snapshot time, never against the last correction, so
// calling it twice converges instead of accumulating.
function holdAnchor(anchor) {
  if (!anchor) return;
  for (var i = 0; i < anchor.chain.length; i++) {
    var link = anchor.chain[i];
    if (!document.contains(link.el)) continue;
    var moved = link.el.getBoundingClientRect().top - link.top;
    if (moved) {
      if (anchor.scroller) anchor.scroller.scrollTop += moved;
      else window.scrollBy(0, moved);
    }
    return; // deepest surviving link wins; the rest are only fallbacks
  }
}

function snapshotScrolls() {
  return Array.prototype.map.call(document.querySelectorAll(SCROLL_SEL), function (el) {
    return {
      // The element itself, not its index. Two patches apart the set of
      // scrolling panels can differ by one (a journal box appearing on a
      // project that had no entries), and index-matching then hands one
      // panel's scroll position to another.
      el: el,
      top: el.scrollTop,
      atBottom: el.scrollTop + el.clientHeight >= el.scrollHeight - 4,
      anchor: viewAnchor(el),
    };
  });
}

function restoreScrolls(saved) {
  saved.forEach(function (s) {
    if (!document.contains(s.el)) return;
    if (s.atBottom) s.el.scrollTop = s.el.scrollHeight;
    else if (s.top) s.el.scrollTop = s.top;
  });
}

// Every anchor, page and panels, held in one place - called after the DOM has
// stopped changing rather than in the middle of it.
function holdEverything(pageAnchor, scrolls) {
  holdAnchor(pageAnchor);
  scrolls.forEach(function (s) {
    if (s.atBottom || !document.contains(s.el)) return;
    holdAnchor(s.anchor);
  });
}

// Per-element enhancers, re-run for whatever the patch brought in. Every one
// of them guards against nodes it already enhanced.
function reinit() {
  document.querySelectorAll("select").forEach(enhanceSelect);
  document.querySelectorAll("textarea").forEach(function (el) {
    if (el.value && !el.style.height) autosize(el);
  });
  initDropzones();
  initTitleRename();
  initProjectDrag();
  // The footer hint is written by the client, so the server's copy of that
  // span - empty and hidden - would blank it on every live patch.
  if (typeof jumpHintSync === "function") jumpHintSync();
  // Same reason as the hint above it: the rail's chapter list is written by
  // the client, so the server's copy of that element - empty and hidden -
  // blanks it on every live patch unless it is rebuilt here.
  if (typeof railChapters === "function") railChapters();
  // The console box is reused: on a project page the morph repoints
  // #agent-console at whichever run is current. Without this the poller keeps
  // watching the run it was born with, so a newly started run's transcript
  // never appears - and it is also what starts a poller for that new run,
  // since the previous one stopped itself when its run ended.
  if (typeof startConsolePoll === "function") startConsolePoll();
  // The morph replaces the console head (its text carries the elapsed time), so
  // the toggle needs its label and its listener back.
  if (typeof initConsoleFoldToggle === "function") initConsoleFoldToggle();
  initFoldMemory();
  if (subtabsApply) subtabsApply();
  // The morph resets <body>'s class attribute to the server's render, so an
  // unsaved theme preview has to be re-applied or it snaps back on the next
  // patch - which on a page that patches every couple of seconds reads as the
  // dropdown not working. init rather than apply: a select the patch DID
  // replace needs its change listener again, and re-binding the survivors is
  // a no-op.
  initAppearancePreview();
  initSectionArrange();
  syncAppBadge();
}

// The number on the Home Screen icon of an installed portal.
//
// A web push paints it from the server (`app_badge` in the declarative
// payload), which is the only thing that can reach the icon while the app is
// closed. But nothing pushes when you answer the last question in the browser,
// so without this the icon would keep the old number until some unrelated
// notification happened along. Re-run after every live patch, because <body>'s
// data attribute is re-rendered by the same morph.
//
// Guarded rather than assumed: setAppBadge is iOS 16.4+/Chrome-only, and in a
// browser tab (as opposed to an installed app) it is a no-op or a rejection
// neither of which is worth reporting.
function syncAppBadge() {
  if (!navigator.setAppBadge) return;
  var raw = document.body.getAttribute("data-open-questions");
  var count = parseInt(raw, 10);
  if (isNaN(count) || count < 0) return;
  try {
    var done = count > 0 ? navigator.setAppBadge(count) : navigator.clearAppBadge();
    if (done && done.catch) done.catch(function () {});
  } catch (err) {
    /* not installed, or permission withheld - the badge is decoration */
  }
}

document.addEventListener("DOMContentLoaded", syncAppBadge);

var refreshQueued = false;
var refreshing = false;

// A patch the reader ASKED for by pressing a button, rather than one a poller
// noticed. Sticky rather than an argument, because a forced patch that has to
// queue behind one already in flight is drained by a site that has long since
// forgotten who asked for it - and arriving a poll interval late is exactly the
// "it often hangs a bit before completing the task I clicked" that made this
// distinction necessary (Wes, 2026-08-27). Cleared by the patch it forces.
var refreshForced = false;

// Whether a patch has to wait. Everything waits on the transient UI a patch
// would destroy; only an unforced one also waits on a sentence in progress.
function refreshHeld() {
  return refreshForced ? interactionBlocked() : refreshBlocked();
}

function liveRefreshNow(force) {
  if (force) refreshForced = true;
  if (refreshing || refreshHeld()) {
    refreshQueued = true;
    return;
  }
  refreshForced = false;
  refreshing = true;
  return fetch(location.href, { cache: "no-store", headers: { "X-Live-Refresh": "1" } })
    .then(function (r) {
      if (!r.ok) throw new Error("bad status");
      return r.text();
    })
    .then(function (html) {
      var doc = new DOMParser().parseFromString(html, "text/html");
      if (!doc || !doc.body) return;
      var scrolls = snapshotScrolls();
      var anchor = viewAnchor(null);
      if (doc.title && doc.title !== document.title) document.title = doc.title;
      morphNode(document.body, doc.body);
      restoreScrolls(scrolls);
      // reinit() BEFORE the correction, not after. It re-enhances the selects,
      // re-hides the settings panels the user is not looking at and re-sizes
      // the textareas - all of which change heights above the viewport. Held
      // in the old order the correction was computed against a layout that
      // existed for one frame and was then thrown away, so the leftover shift
      // stuck, and every patch added another one in the same direction. That
      // is the settings page "scrolling up every second or so".
      reinit();
      holdEverything(anchor, scrolls);
      // Layout still is not final: an image the patch brought in has no height
      // until it decodes, and a rebuilt dropdown settles on its own width. One
      // more correction on the next frame catches those. Skipped if the reader
      // has scrolled in the meantime - their scroll wins over ours, always.
      var settled = window.scrollY || 0;
      if (window.requestAnimationFrame) {
        window.requestAnimationFrame(function () {
          if (Math.abs((window.scrollY || 0) - settled) < 2) {
            holdEverything(anchor, scrolls);
          }
        });
      }
    })
    .catch(function () {
      // A miss is a skipped patch, never an error surface: the offline
      // overlay is the thing that reports the server being gone.
    })
    .then(function () {
      refreshing = false;
      if (refreshQueued && !refreshHeld()) {
        refreshQueued = false;
        return liveRefreshNow();
      }
    });
}

// The older pollers call this where they used to call location.reload().
// `force` means the reader pressed something and this patch is the answer.
function liveReload(force) {
  if (window.fetch && window.DOMParser) return liveRefreshNow(force);
  window.location.reload();
}

function initLiveRefresh() {
  if (!window.fetch || !window.DOMParser) return;
  var last = null;
  var POLL_MS = 2500;

  function apply(token) {
    if (token === last) return;
    if (last === null) {
      last = token;
      return;
    }
    var sameBoot = token.split(":")[0] === last.split(":")[0];
    last = token;
    if (!sameBoot) {
      // The server restarted: this page's CSS/JS may no longer match its
      // templates, so patching is not safe - reload for real. Deferred the
      // same way a patch is, so it cannot eat a note mid-sentence (drafts
      // save on input regardless).
      if (refreshBlocked()) {
        refreshQueued = true;
        window.addEventListener("focusout", function () {
          setTimeout(function () {
            if (!refreshBlocked()) window.location.reload();
          }, 100);
        }, { once: true });
      } else {
        window.location.reload();
      }
      return;
    }
    liveRefreshNow();
  }

  function tick() {
    if (document.hidden) return;
    fetch("/api/version", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.v) apply(d.v);
        else if (refreshQueued && !refreshHeld()) {
          refreshQueued = false;
          liveRefreshNow();
        }
      })
      .catch(function () {});
    // A patch held back by an interaction gets applied on a later tick even
    // if the version has not moved again since.
    if (refreshQueued && !refreshing && !refreshHeld()) {
      refreshQueued = false;
      liveRefreshNow();
    }
  }
  setInterval(tick, POLL_MS);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick();
  });
}

document.addEventListener("DOMContentLoaded", initLiveRefresh);

function initSelects() {
  document.querySelectorAll("select").forEach(enhanceSelect);
  document.addEventListener("click", function (ev) {
    if (ev.target.closest(".sel")) return;
    document.querySelectorAll(".sel.open").forEach(function (wrap) {
      wrap.classList.remove("open");
      wrap.querySelector(".sel-menu").hidden = true;
      wrap.querySelector(".sel-trigger").setAttribute("aria-expanded", "false");
    });
  });
}

document.addEventListener("DOMContentLoaded", initSelects);

// ---------------------------------------------------------------------------
// In-page image viewer (the lightbox). Wes's ask: opening an image in the
// journal should pop it up ON the page - zoomable, pannable, closable - not
// send you to the raw file on another page.
//
// mediamd.py wraps a plain journal image in `<a class="journal-media-link"
// data-lightbox href="<raw>">`. We intercept a plain left-click on that anchor
// and open the viewer here; the anchor keeps its href so a modified click
// (cmd/ctrl/middle) or JS-off still opens the raw image in a new tab.
//
// The overlay is built once, lazily, and appended to <body>; it is a
// client-only node, so `#img-lightbox` is in MORPH_KEEP and a live refresh
// leaves it (and any open image) alone.
var lb = null;

function lbBuild() {
  if (lb) return lb;
  var root = document.createElement("div");
  root.id = "img-lightbox";
  root.hidden = true;
  root.innerHTML =
    '<div class="lb-bar">' +
      '<button type="button" class="lb-btn" data-lb="out" title="Zoom out" aria-label="Zoom out">−</button>' +
      '<span class="lb-zoom-label" data-lb="zoom">100%</span>' +
      '<button type="button" class="lb-btn" data-lb="in" title="Zoom in" aria-label="Zoom in">+</button>' +
      '<button type="button" class="lb-btn" data-lb="reset" title="Fit to screen" aria-label="Fit to screen">↺</button>' +
      '<span class="lb-title" data-lb="title"></span>' +
      '<a class="lb-btn" data-lb="open" target="_blank" rel="noopener" title="Open raw file in a new tab" aria-label="Open in new tab">↗</a>' +
      '<button type="button" class="lb-btn" data-lb="close" title="Close (Esc)" aria-label="Close">✕</button>' +
    '</div>' +
    '<div class="lb-stage"><img alt=""></div>' +
    '<div class="lb-caption"></div>';
  document.body.appendChild(root);

  lb = {
    root: root,
    stage: root.querySelector(".lb-stage"),
    img: root.querySelector(".lb-stage img"),
    caption: root.querySelector(".lb-caption"),
    title: root.querySelector('[data-lb="title"]'),
    open: root.querySelector('[data-lb="open"]'),
    zoomLabel: root.querySelector('[data-lb="zoom"]'),
    scale: 1, minScale: 1, x: 0, y: 0,
    natW: 0, natH: 0,
    pointers: {}, // active pointers by id, for pinch
    pinchDist: 0,
    // Pixels traveled since the pointer went down, so the click handler can
    // tell a click on the backdrop from the end of a pan across it.
    moved: 0,
    dragId: null, dragX: 0, dragY: 0,
  };

  root.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-lb]");
    if (!b) {
      // Wes asked for both of these, three days apart, and they are not in
      // conflict once you say what "off the image" means:
      //
      //   2026-07-25: the viewer must NOT close on a click - clicking the
      //   backdrop used to close, and every misjudged pan and stray click
      //   threw away the image you were reading.
      //   2026-07-28: "clicking off the side of the image should close it."
      //
      // So it closes on a click that is genuinely off to the side and is
      // genuinely a click:
      //
      // - `ev.target === lb.stage` - the click landed on the letterbox area
      //   around the image, not on the image itself. That is the whole of the
      //   2026-07-25 complaint: a misjudged pan lands ON the image (it is
      //   what you were dragging), and a zoomed image fills the stage
      //   entirely, so there is no backdrop left to hit by accident.
      // - `lb.moved` under a few pixels - a drag that starts on the backdrop
      //   and ends there still fires a click, and letting go of a pan must
      //   never be read as "shut it".
      if ((ev.target === lb.stage || ev.target === lb.root) && lb.moved < 5) lbClose();
      return;
    }
    var act = b.getAttribute("data-lb");
    if (act === "close") lbClose();
    else if (act === "in") lbZoomBy(1.4);
    else if (act === "out") lbZoomBy(1 / 1.4);
    else if (act === "reset") lbFit();
    // "open" is a real link - let it navigate.
  });

  // Wheel zooms toward the cursor. The zoom is proportional to how far the
  // wheel/trackpad actually moved: a trackpad fires a stream of tiny-delta
  // events, so a fixed 15%-per-event step (what this used to do) compounded
  // into a runaway zoom the moment two fingers grazed the pad. exp() keeps
  // successive nudges smooth, and the clamp stops a single chunky mouse notch
  // from leaping.
  lb.stage.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var rect = lb.stage.getBoundingClientRect();
    var dy = ev.deltaY;
    if (ev.deltaMode === 1) dy *= 16;                        // lines -> px
    else if (ev.deltaMode === 2) dy *= (rect.height || 600); // pages -> px
    dy = Math.max(-50, Math.min(50, dy));
    lbZoomAt(Math.exp(-dy * 0.0025),
      ev.clientX - rect.left, ev.clientY - rect.top);
  }, { passive: false });

  // Double-click / double-tap toggles fit <-> 2x actual, at the point clicked.
  lb.stage.addEventListener("dblclick", function (ev) {
    var rect = lb.stage.getBoundingClientRect();
    if (lb.scale > lb.minScale * 1.01) lbFit();
    else lbZoomAt(2 / lb.minScale, ev.clientX - rect.left, ev.clientY - rect.top);
  });

  // Pointer events unify mouse drag and touch pan/pinch.
  lb.stage.addEventListener("pointerdown", function (ev) {
    lb.pointers[ev.pointerId] = { x: ev.clientX, y: ev.clientY };
    lb.stage.setPointerCapture(ev.pointerId);
    var ids = Object.keys(lb.pointers);
    if (ids.length === 1) lb.moved = 0;
    if (ids.length === 1) {
      lb.dragId = ev.pointerId;
      lb.dragX = ev.clientX; lb.dragY = ev.clientY;
      lb.stage.classList.add("panning");
    } else if (ids.length === 2) {
      lb.dragId = null;
      lb.stage.classList.remove("panning");
      lb.pinchDist = lbPointerSpread();
    }
  });
  lb.stage.addEventListener("pointermove", function (ev) {
    var p = lb.pointers[ev.pointerId];
    if (!p) return;
    // Accumulated, not straight-line: a pan that wanders out and comes back
    // to where it started is still a pan, and a straight-line measure would
    // read it as a click and close the image the person was reading.
    lb.moved += Math.abs(ev.clientX - p.x) + Math.abs(ev.clientY - p.y);
    p.x = ev.clientX; p.y = ev.clientY;
    var ids = Object.keys(lb.pointers);
    if (ids.length >= 2) {
      var d = lbPointerSpread();
      if (lb.pinchDist > 0 && d > 0) {
        var rect = lb.stage.getBoundingClientRect();
        var c = lbPointerCenter();
        lbZoomAt(d / lb.pinchDist, c.x - rect.left, c.y - rect.top);
      }
      lb.pinchDist = d;
    } else if (ev.pointerId === lb.dragId) {
      lb.x += ev.clientX - lb.dragX;
      lb.y += ev.clientY - lb.dragY;
      lb.dragX = ev.clientX; lb.dragY = ev.clientY;
      lbClamp();
      lbApply();
    }
  });
  function endPointer(ev) {
    delete lb.pointers[ev.pointerId];
    if (lb.stage.hasPointerCapture && lb.stage.hasPointerCapture(ev.pointerId)) {
      lb.stage.releasePointerCapture(ev.pointerId);
    }
    var ids = Object.keys(lb.pointers);
    if (ids.length < 2) lb.pinchDist = 0;
    if (ids.length === 1) {
      lb.dragId = Number(ids[0]);
      lb.dragX = lb.pointers[ids[0]].x; lb.dragY = lb.pointers[ids[0]].y;
    } else if (ids.length === 0) {
      lb.dragId = null;
      lb.stage.classList.remove("panning");
    }
  }
  lb.stage.addEventListener("pointerup", endPointer);
  lb.stage.addEventListener("pointercancel", endPointer);

  return lb;
}

function lbPointerSpread() {
  var ids = Object.keys(lb.pointers);
  if (ids.length < 2) return 0;
  var a = lb.pointers[ids[0]], b = lb.pointers[ids[1]];
  return Math.hypot(a.x - b.x, a.y - b.y);
}
function lbPointerCenter() {
  var ids = Object.keys(lb.pointers);
  var a = lb.pointers[ids[0]], b = lb.pointers[ids[1]];
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

// Keep the image within sane bounds: never let it drift entirely out of the
// stage. x/y are the offset of the image center from the stage center.
function lbClamp() {
  var rect = lb.stage.getBoundingClientRect();
  var halfW = (lb.natW * lb.scale) / 2;
  var halfH = (lb.natH * lb.scale) / 2;
  // Allow panning up to the point where an edge of the image reaches the
  // center of the stage - generous, but never fully off-screen.
  var maxX = Math.max(halfW, rect.width / 2);
  var maxY = Math.max(halfH, rect.height / 2);
  lb.x = Math.max(-maxX, Math.min(maxX, lb.x));
  lb.y = Math.max(-maxY, Math.min(maxY, lb.y));
}

function lbApply() {
  // The img is anchored at the stage center (top/left 50%) with origin 0,0,
  // so we translate by -half the scaled size (to center it) plus the pan
  // offset, then scale.
  var tx = lb.x - (lb.natW * lb.scale) / 2;
  var ty = lb.y - (lb.natH * lb.scale) / 2;
  lb.img.style.transform =
    "translate(" + tx + "px," + ty + "px) scale(" + lb.scale + ")";
  var pct = Math.round((lb.scale / lb.minScale) * 100);
  lb.zoomLabel.textContent = pct + "%";
}

function lbSetScale(s) {
  lb.scale = Math.max(lb.minScale * 0.5, Math.min(lb.minScale * 8, s));
}

function lbZoomBy(f) {
  // Zoom about the stage center.
  lbSetScale(lb.scale * f);
  lbClamp();
  lbApply();
}

// Zoom by factor f keeping the stage-space point (px,py) fixed under the
// cursor/fingers.
function lbZoomAt(f, px, py) {
  var rect = lb.stage.getBoundingClientRect();
  var cx = px - rect.width / 2;   // point relative to stage center
  var cy = py - rect.height / 2;
  var before = lb.scale;
  lbSetScale(lb.scale * f);
  var ratio = lb.scale / before;
  // Move the pan offset so the world point under the cursor stays put.
  lb.x = cx - (cx - lb.x) * ratio;
  lb.y = cy - (cy - lb.y) * ratio;
  lbClamp();
  lbApply();
}

// Fit the image to the stage (the default view), centered.
function lbFit() {
  var rect = lb.stage.getBoundingClientRect();
  if (!lb.natW || !lb.natH) { lb.minScale = 1; lb.scale = 1; }
  else {
    var fit = Math.min(rect.width / lb.natW, rect.height / lb.natH, 1);
    lb.minScale = fit > 0 ? fit : 1;
    lb.scale = lb.minScale;
  }
  lb.x = 0; lb.y = 0;
  lbApply();
}

function lbOpen(src, alt) {
  lbBuild();
  lb.pointers = {};
  lb.title.textContent = (src || "").split("/").pop();
  lb.open.href = src;
  lb.caption.textContent = alt || "";
  lb.img.alt = alt || "";
  lb.root.classList.remove("closing");
  lb.root.hidden = false;
  document.body.classList.add("lb-open");
  var apply = function () {
    lb.natW = lb.img.naturalWidth || lb.img.clientWidth || 1;
    lb.natH = lb.img.naturalHeight || lb.img.clientHeight || 1;
    lbFit();
  };
  // If it is already cached the load event may not fire; handle both.
  lb.img.onload = apply;
  lb.img.src = src;
  if (lb.img.complete && lb.img.naturalWidth) apply();
  else { lb.natW = 0; lb.natH = 0; lbApply(); }
}

function lbClose() {
  if (!lb || lb.root.hidden) return;
  lb.root.classList.add("closing");
  document.body.classList.remove("lb-open");
  var root = lb.root;
  window.setTimeout(function () {
    root.hidden = true;
    root.classList.remove("closing");
    lb.img.src = "";
  }, 130);
}

// Open on a plain click of a portal-generated journal-image self-link. A
// modified click (new tab / new window / download) is left to the browser.
document.addEventListener("click", function (ev) {
  if (ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
  var link = ev.target.closest("a.journal-media-link[data-lightbox]");
  if (!link) return;
  var img = link.querySelector("img");
  ev.preventDefault();
  lbOpen(link.getAttribute("href"), img ? img.getAttribute("alt") : "");
});

// Escape closes the viewer (before any other Escape handler acts on the page).
document.addEventListener("keydown", function (ev) {
  if (ev.key !== "Escape") return;
  if (lb && !lb.root.hidden) { ev.stopPropagation(); lbClose(); }
}, true);

// ---------------------------------------------------------------------------
// Highlight a passage in the journal, then ask or note about it.
//
// Wes, 2026-07-25: "When highlighting text in the journal, allow me to ask a
// question or make a new note with reference to that text."
//
// Selecting text inside a journal entry floats a two-button bar over the
// selection. Pressing one does NOT open a new composer: it drops the quoted
// passage into the page's existing ask form or note form as a chip carrying a
// hidden `quote` field, opens that form and focuses it. Everything those forms
// already do - drafts, Ctrl+Enter, attachments, "add & run now" - therefore
// keeps working, and the submit is the browser's own; there is no second
// posting path that could disagree with the buttons.
//
// Both the bar and the chip are client-only nodes in MORPH_KEEP, so a live
// refresh landing mid-thought cannot take a quote back out of the form.
// ---------------------------------------------------------------------------

var selBar = null;
var selQuote = "";

function selBarBuild() {
  if (selBar) return selBar;
  var root = document.createElement("div");
  root.id = "sel-actions";
  root.hidden = true;
  root.innerHTML =
    '<button type="button" class="sel-act" data-sel-act="ask">ask about this</button>' +
    '<button type="button" class="sel-act" data-sel-act="note">note about this</button>';
  document.body.appendChild(root);
  root.addEventListener("mousedown", function (ev) { ev.preventDefault(); });
  root.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-sel-act]");
    if (!btn) return;
    quoteInto(btn.getAttribute("data-sel-act"), selQuote);
  });
  selBar = root;
  return root;
}

// The selection, but only when it really is a passage of a journal entry.
// Anchor and focus are both checked: a drag that starts in an entry and ends
// in the page furniture below it would otherwise quote the furniture too.
function journalSelection() {
  var sel = window.getSelection && window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
  var text = sel.toString().trim();
  if (text.length < 2) return null;
  if (!inJournalContent(sel.anchorNode) || !inJournalContent(sel.focusNode)) return null;
  var rect = sel.getRangeAt(0).getBoundingClientRect();
  if (!rect || (!rect.width && !rect.height)) return null;
  return { text: text, rect: rect };
}

function inJournalContent(node) {
  if (!node) return false;
  var el = node.nodeType === 1 ? node : node.parentNode;
  return !!(el && el.closest && el.closest("#journal .journal-entry .content"));
}

var SEL_GAP = 10; // between the journal card and a bar parked beside it
var SEL_EDGE = 8; // keep the bar off the viewport edge

function selBarShow(hit) {
  var root = selBarBuild();
  selQuote = hit.text;
  root.hidden = false;

  // Wes, 2026-07-25: "if there is room off to the right side of the journal
  // area for them to show without going off the monitor, show them off to the
  // right side instead." The page column is a fixed max-width centered in the
  // window, so on a desktop monitor there is a real gutter beside the journal
  // card, and a bar parked there covers no text at all. The buttons stack into
  // a narrow column in that mode (`.side`), because a gutter wide enough for
  // one button is much commoner than one wide enough for a two-button row.
  //
  // Sizes are read after unhiding and after the class is applied - a hidden
  // element has no size, and a row and a column measure differently.
  var journal = document.getElementById("journal");
  var jr = journal ? journal.getBoundingClientRect() : null;
  root.classList.add("side");
  var w = root.offsetWidth;
  var h = root.offsetHeight;
  if (jr && jr.right + SEL_GAP + w + SEL_EDGE <= window.innerWidth) {
    // Vertically centered on the selection, so it still reads as belonging to
    // the passage, then clamped so a selection near an edge stays on screen.
    var sideTop = hit.rect.top + hit.rect.height / 2 - h / 2;
    sideTop = Math.max(SEL_EDGE, Math.min(window.innerHeight - h - SEL_EDGE, sideTop));
    root.style.left = Math.round(jr.right + SEL_GAP) + "px";
    root.style.top = Math.round(sideTop) + "px";
    return;
  }

  // No gutter (a phone, a narrow window): back to floating over the selection,
  // above it where there is room and below it where there is not, so the bar
  // never covers the words being quoted.
  root.classList.remove("side");
  w = root.offsetWidth;
  h = root.offsetHeight;
  var left = hit.rect.left + hit.rect.width / 2 - w / 2;
  left = Math.max(SEL_EDGE, Math.min(window.innerWidth - w - SEL_EDGE, left));
  var top = hit.rect.top - h - SEL_EDGE;
  if (top < SEL_EDGE) {
    top = Math.min(window.innerHeight - h - SEL_EDGE, hit.rect.bottom + SEL_EDGE);
  }
  root.style.left = Math.round(left) + "px";
  root.style.top = Math.round(top) + "px";
}

function selBarHide() {
  if (selBar) selBar.hidden = true;
}

function selBarSync() {
  var hit = journalSelection();
  if (hit) selBarShow(hit);
  else selBarHide();
}

// selectionchange is the only event that fires for a touch selection on iOS,
// and it fires continuously during a mouse drag - hence the debounce, which
// also stops the bar flickering as the selection grows.
var selTimer = null;
document.addEventListener("selectionchange", function () {
  if (selTimer) clearTimeout(selTimer);
  selTimer = setTimeout(selBarSync, 180);
});
window.addEventListener("scroll", selBarHide, true);
window.addEventListener("resize", selBarHide);
document.addEventListener("keydown", function (ev) {
  if (ev.key === "Escape") selBarHide();
});

var QUOTE_TARGETS = {
  ask: { form: ".ask-form", field: 'textarea[name="question"]', fold: "#ask" },
  note: { form: ".note-form", field: 'textarea[name="note"]', fold: "" }
};

function quoteInto(kind, text) {
  var target = QUOTE_TARGETS[kind];
  if (!target || !text) return;
  var form = document.querySelector(target.form);
  if (!form) return;
  var chip = form.querySelector(".quote-chip");
  if (!chip) {
    chip = document.createElement("div");
    chip.className = "quote-chip";
    chip.innerHTML =
      '<input type="hidden" name="quote">' +
      '<blockquote class="quote-chip-text"></blockquote>' +
      '<button type="button" class="quote-chip-x" title="Drop the quoted passage" ' +
      'aria-label="Drop the quoted passage">✕</button>';
    chip.querySelector(".quote-chip-x").addEventListener("click", function () {
      chip.remove();
    });
    form.insertBefore(chip, form.firstChild);
  }
  // Set as values/text, never as HTML: this string came off the page and must
  // not be able to put markup back into it.
  chip.querySelector('input[name="quote"]').value = text;
  chip.querySelector(".quote-chip-text").textContent = text;

  if (target.fold) {
    var fold = document.querySelector(target.fold);
    if (fold && fold.tagName === "DETAILS") fold.open = true;
  }
  // Drop the selection first: leaving it up keeps refreshBlocked() true and
  // leaves the native iOS callout sitting over the form we just opened.
  var sel = window.getSelection && window.getSelection();
  if (sel && sel.removeAllRanges) sel.removeAllRanges();
  selBarHide();

  var field = form.querySelector(target.field);
  if (field) {
    field.focus();
    autosize(field);
  }
  form.scrollIntoView({ block: "center", behavior: "smooth" });
}

// ---------------------------------------------------------------------------
// Single-key jumps to the section you want.
//
// Wes, 2026-07-27: "Hitting the 'N' key when no text box is being typed into
// in a project page should jump to the 'add note' section and highlight the
// text box for it so a note can begin being typed immediately. If on the
// overall project dashboard, hitting n should jump to the new ideas section
// and highlight to start typing in a title. The page scroll should be set so
// that the title section is at the top of the page rather than the bottom or
// something. Same for the add note bit. Set the journal section up to be
// jumped to in project pages by pressing J... T should do the same with todo,
// and p with the top project status/settings stuff."
//
// The page declares its own targets with `data-jump="<name>"`, so this file
// never learns the shape of a template: adding a jumpable section is one
// attribute in the HTML. `data-jump-focus` names the field to put the cursor
// in, and is optional - J, T and P are pure navigation.
// ---------------------------------------------------------------------------

// key -> the target names it will settle for, in preference order. A page only
// ever declares one of them, which is how N means "the box I type into here"
// on both pages: `note` exists on a project page, `idea` on the dashboard.
//
// Wes, the same morning: "Allow these key commands to be reconfigured in
// settings." So these letters are a default, not a fact. app/jumpkeys.py owns
// the bindings and renders them onto `<body data-jump-keys>` as this exact
// shape; this file reads them there rather than fetching, because the map has
// to be known before the first keystroke - a page that answered N only after a
// round trip would drop the key you pressed while it was still asking.
// Kept byte-identical to jumpkeys.bindings({}) on the server - a test asserts
// the two agree, because the only page that uses this copy is one rendered
// before the attribute existed, and a silent disagreement there would present
// as "the keys do something different in my other tab".
var JUMP_KEYS_DEFAULT = {
  n: ["note", "idea"],
  a: ["ask"],
  // The scrolling journal box first, its heading second: Wes asked for the
  // box's top edge at the top of the window, and the heading is what an empty
  // journal (which renders no box) has instead.
  j: ["journal-box", "journal"],
  t: ["todo"],
  p: ["project"],
  s: ["summary"],
  f: ["files"]
};

// An absent attribute means a page that predates the setting (a cached tab, a
// template rendered elsewhere) and falls back to the shipped letters. An empty
// object does NOT: `{}` is somebody having turned every jump off on purpose,
// and reviving the defaults there would be the portal overruling them.
function readJumpKeys() {
  var raw = document.body && document.body.getAttribute
    ? document.body.getAttribute("data-jump-keys")
    : null;
  if (raw === null || raw === "") return JUMP_KEYS_DEFAULT;
  var parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    return JUMP_KEYS_DEFAULT;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return JUMP_KEYS_DEFAULT;
  }
  // Keep only entries this file could actually act on. A single character,
  // because that is what a keydown gives us to match; a non-empty list of
  // target names, because anything else would make jumpTarget iterate garbage
  // and, worse, make the footer hint advertise a letter that does nothing.
  var out = {};
  Object.keys(parsed).forEach(function (key) {
    var names = parsed[key];
    if (key.length !== 1 || !Array.isArray(names) || !names.length) return;
    var clean = names.filter(function (name) { return typeof name === "string" && name; });
    if (clean.length) out[key.toLowerCase()] = clean;
  });
  return out;
}

var JUMP_KEYS = readJumpKeys();

// Where the key must NOT act: anywhere the letter is a letter. Without this,
// typing "not now" into the note box would fire N, T and O's worth of jumps
// and lose the sentence.
function typingInto(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  var tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
    tag === "OPTION";
}

function jumpTarget(key) {
  var names = JUMP_KEYS[key];
  if (!names) return null;
  for (var i = 0; i < names.length; i++) {
    var el = document.querySelector('[data-jump="' + names[i] + '"]');
    if (el) return el;
  }
  return null;
}

// Smooth unless the reader has asked for stillness - the appearance setting
// paints `anim-off` on <body>, and the OS-level preference is honored too,
// because a long page scrolling under you is exactly the motion both mean.
function jumpBehavior() {
  if (document.body.classList.contains("anim-off")) return "auto";
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return "auto";
  }
  return "smooth";
}

function jumpTo(el) {
  // A target inside a settings tab that is not the open one. Same idea as the
  // <details> unfold below, and the reason it is here rather than in the rail:
  // the settings page is the one place in the portal where a section that
  // exists is not on screen, so a chapter pointing into a closed tab would be a
  // link that scrolls to nothing at all. Wes asked for "one click access to any
  // given tab/section" - the tab half is this line.
  var panel = el.closest ? el.closest(".settings-panel[hidden]") : null;
  if (panel && subtabsShow) subtabsShow(panel.getAttribute("data-panel"));

  // A target folded inside a <details> would otherwise scroll to a summary
  // with nothing under it.
  var fold = el.closest ? el.closest("details") : null;
  while (fold) {
    fold.open = true;
    fold = fold.parentElement && fold.parentElement.closest
      ? fold.parentElement.closest("details")
      : null;
  }

  var sel = el.getAttribute("data-jump-focus");
  var field = sel ? document.querySelector(sel) : null;
  if (field) {
    // Focus BEFORE the scroll, not after. focus() scrolls the field into view
    // by itself, which lands the field near the top of the window and throws
    // away the section heading we are here to align - so the scroll has to be
    // the last word. preventScroll asks it not to fight in the first place.
    try {
      field.focus({ preventScroll: true });
    } catch (e) {
      field.focus();
    }
    jumpFlash(field);
  }
  // block:start is the whole ask: the section's top edge against the top of
  // the window, not centered and not scrolled just barely into view.
  el.scrollIntoView({ block: "start", behavior: jumpBehavior() });
}

// A brief ring around the field so it is obvious where the cursor went. The
// class is dropped again so a live refresh never inherits a stale highlight.
var jumpFlashTimer = null;
function jumpFlash(field) {
  if (jumpFlashTimer) clearTimeout(jumpFlashTimer);
  document.querySelectorAll(".jump-flash").forEach(function (el) {
    el.classList.remove("jump-flash");
  });
  field.classList.add("jump-flash");
  jumpFlashTimer = setTimeout(function () {
    field.classList.remove("jump-flash");
  }, 1400);
}

document.addEventListener("keydown", function (ev) {
  // Modified keys belong to the browser (Cmd+N is a new window, Ctrl+P is
  // print). Shift is allowed through unmodified: Wes wrote "N" and "J".
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  if (typingInto(ev.target)) return;
  // The image viewer is a modal: scrolling the page behind it does nothing you
  // can see, and Escape is the way out.
  var lightbox = document.getElementById("img-lightbox");
  if (lightbox && !lightbox.hidden) return;
  if (!ev.key || ev.key.length !== 1) return;
  var el = jumpTarget(ev.key.toLowerCase());
  if (!el) return;
  ev.preventDefault();
  jumpTo(el);
});

// The footer says which keys this page answers to, derived from the targets
// the page declares - so it lists exactly the keys that work, and stays empty
// on a page with none. Only on a device with a real keyboard: on a phone the
// hint would be advertising something you cannot press.
function jumpHintSync() {
  var slot = document.querySelector(".jump-hint");
  if (!slot) return;
  if (!hasHardwareKeyboard()) return;
  var keys = Object.keys(JUMP_KEYS).filter(function (key) {
    return !!jumpTarget(key);
  });
  if (!keys.length) return;
  slot.textContent = keys.join(" ") + " jump to a section";
  slot.hidden = false;
}
document.addEventListener("DOMContentLoaded", jumpHintSync);

// ---------------------------------------------------------------------------
// A digit goes to a project on the rail.
//
// Wes, 2026-08-01: "Allow me to press number keys to jump to the projects over
// there based on their order on the list."
//
// A navigation, not a scroll - the rows are links to other pages - so this is a
// separate handler from the jump keys above rather than another entry in their
// table. It also could not be one: jumpkeys.py bans digits from a binding on
// purpose (a JS object puts integer-like keys first whatever order they were
// inserted in, which would silently reorder the footer hint), and these are
// positions in a list rather than names of sections.
//
// Only when the rail is actually on screen. Below its placement's breakpoint
// (1400px on `use existing space`, 1100px on `interface shift`), and on the
// `off` setting, `#side-rail` is display:none - and a digit that navigated
// somewhere invisible would be a key that teleports you for no reason you can
// see. Asked of the layout rather than of the window width, so this can never
// drift from the CSS.
//
// getClientRects().length, and NOT offsetParent: the spec says offsetParent is
// null whenever the element's own computed position is `fixed`, which the rail
// always is. So the test that was here answered "not on screen" at every width
// on every setting, and the number keys Wes asked for had in fact never once
// worked. Found by probing a real browser (offsetParent null, offsetWidth 183,
// one client rect) while checking something else. No DOM test could have shown
// it: a stub DOM has no layout, so whatever it answers here is whatever the
// stub was written to answer.
function railDigitTarget(key) {
  var rail = document.getElementById("side-rail");
  if (!rail || !rail.getClientRects().length) return null;
  return rail.querySelector('[data-rail-digit="' + key + '"]');
}

document.addEventListener("keydown", function (ev) {
  if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.shiftKey) return;
  if (typingInto(ev.target)) return;
  var lightbox = document.getElementById("img-lightbox");
  if (lightbox && !lightbox.hidden) return;
  if (!ev.key || ev.key.length !== 1 || ev.key < "0" || ev.key > "9") return;
  var link = railDigitTarget(ev.key);
  if (!link) return;
  ev.preventDefault();
  window.location.href = link.getAttribute("href");
});

// ---------------------------------------------------------------------------
// The side rail's chapter list.
//
// Wes, 2026-08-01: "Would also be good to have one click access to any given
// tab/section in the project I'm in. Like click the 'Journal' sort of chapter
// on the side bar to jump to the journal view."
//
// Built here rather than on the server for the same reason the footer hint is:
// base.html renders around a content block and cannot see what that block
// declared.
//
// Two kinds of chapter, merged in document order because to a reader they are
// the same thing - a place on this page to go:
//
//   - every `[data-jump]` target, which is also what the keyboard jumps use, so
//     one attribute makes a section reachable by click and by key;
//   - every plain `<h2>` in the content body, with no attribute at all.
//
// The second kind is Wes's follow-up of 2026-08-01: "I want the option to jump
// to sections from the side-bar even if they are not sections I can jump to
// with a hot-key." Before it, /activity, /settings and /memory - four, eight and
// ten headings respectively - listed no chapters whatsoever, because none of
// them had ever been annotated. There are only eight jump keys and there are
// far more sections than that, so a list built only from bound targets can
// never be a table of contents; it is a list of the shortcuts.
//
// A chapter's label comes from `data-jump-label` when the element's own text is
// wrong for a nav (a whole card, a <details> with a sentence in its summary),
// and from the element's own text otherwise, minus any badge or count riding
// along in it. `data-jump-nav="off"` leaves a target out, which is how the
// journal declares two targets - the scrolling box and its heading, so the J key
// still works on an empty project - and gets one chapter.
// ---------------------------------------------------------------------------

// The letter bound to this target, or "". Read off the live bindings, so a
// rebound or unbound key is right here without this function knowing anything
// about the settings page.
function chapterKey(name) {
  var keys = Object.keys(JUMP_KEYS);
  for (var i = 0; i < keys.length; i++) {
    if (JUMP_KEYS[keys[i]].indexOf(name) !== -1) return keys[i];
  }
  return "";
}

// A count or a status badge inside a heading is part of the page, not part of
// the heading's name: "phone push 1 device" and "questions 2" both read as a
// chapter whose title keeps changing under the reader.
var CHAPTER_NOISE = "badge,nav-count,rail-count,work-summary-count";

function chapterLabel(el) {
  var given = el.getAttribute("data-jump-label");
  if (given) return given;
  // A <details> section names itself in its summary, and a summary is
  // "<label> <a sentence of muted detail>" - the label is the chapter, the
  // sentence is the page. Read the label span when there is one rather than
  // running the whole fold's contents through the trimmer below.
  if (el.tagName === "DETAILS") {
    var sum = el.querySelector("summary");
    var lab = sum ? sum.querySelector(".fold-section-label") : null;
    el = lab || sum || el;
  }
  var text = "";
  var kids = el.childNodes || [];
  for (var i = 0; i < kids.length; i++) {
    var kid = kids[i];
    if (kid.nodeType === 1 && kid.className) {
      var classes = (" " + kid.className + " ").replace(/\s+/g, " ");
      var noisy = false;
      CHAPTER_NOISE.split(",").forEach(function (name) {
        if (classes.indexOf(" " + name + " ") !== -1) noisy = true;
      });
      if (noisy) continue;
    }
    text += kid.textContent || "";
  }
  // One line, trimmed: a heading is one line, but a <details> summary can run
  // to a sentence and a card contains a whole page section.
  text = text.replace(/\s+/g, " ").trim();
  if (text.length <= 28) return text;
  // Cut on a word boundary when there is one to cut on: "What we've learned
  // about…" is a chapter title, "What we've learned about eac…" looks like a
  // rendering fault. A single 28-character word still gets cut mid-word,
  // because the alternative is an empty label.
  var cut = text.slice(0, 28);
  var space = cut.lastIndexOf(" ");
  return (space > 12 ? cut.slice(0, space) : cut).trim() + "…";
}

function railChapters() {
  var slot = document.getElementById("rail-chapters");
  if (!slot) return;
  // Scoped to the content body, which is what excludes the rail's own links
  // (they would be chapters pointing at themselves), the nav tabs and the
  // footer. One selector with a comma, so the browser returns both kinds in
  // document order and the list reads down the page the way the page does.
  var body = document.querySelector(".terminal-body");
  var targets = body
    ? body.querySelectorAll("[data-jump], h2, details.fold-section")
    : [];
  var rows = [];
  var seen = {};
  for (var i = 0; i < targets.length; i++) {
    var el = targets[i];
    if (el.getAttribute("data-jump-nav") === "off") continue;
    // Headings inside rendered markdown are somebody's prose, not this page's
    // structure. Wes, 2026-08-01: "When on the dashboard, the left tab bar
    // should not show the recent activity entries as individual items as it
    // currently does." Every agent progress entry opens with an `##` heading,
    // and the dashboard's activity feed renders a dozen of them - so the
    // chapter list was a list of other pages' journal entries. `.content` is
    // the wrapper every markdown render in the portal goes into, and nothing
    // else uses it.
    if (el.closest && el.closest(".content")) continue;
    var name = el.getAttribute("data-jump");
    if (name) {
      if (seen[name]) continue;
      seen[name] = true;
    } else if (el.closest && el.closest("[data-jump]")) {
      // A heading inside a block that already declared itself a target is that
      // block's own title, not a second place to go.
      continue;
    } else if (
      el.tagName !== "DETAILS" &&
      el.closest &&
      el.closest("details.fold-section")
    ) {
      // Same rule one level down: a heading inside a fold is that fold's
      // content. "Files" is the chapter; "Uploaded" and "Workspace" are what
      // is in it.
      continue;
    }
    var label = chapterLabel(el);
    if (!label) continue;
    rows.push({ el: el, name: name || "", label: label, key: name ? chapterKey(name) : "" });
  }
  // One chapter is not a table of contents, it is a link - and on the
  // dashboard, whose only target is the new-idea form, a "Sections" heading
  // over a single row would be chrome carrying no information.
  if (rows.length < 2) {
    slot.hidden = true;
    return;
  }

  var head = document.createElement("h3");
  head.className = "rail-head";
  head.textContent = "This page";
  var list = document.createElement("ul");
  rows.forEach(function (row) {
    var li = document.createElement("li");
    var a = document.createElement("a");
    // A real href, so the row is a link a person can middle-click or read in
    // the status bar; the click handler below is what makes it a scroll rather
    // than a navigation.
    a.href = "#";
    if (row.name) a.setAttribute("data-jump-to", row.name);
    // The element itself, not a name to look up later. A heading discovered by
    // tag has no name to look up, and holding the node means the anchor and its
    // target are built in the same pass and cannot drift apart. This whole list
    // is rebuilt after every live patch, so the reference is never stale for
    // longer than the patch that replaced it.
    a._chapterEl = row.el;
    if (row.key) {
      var key = document.createElement("span");
      key.className = "rail-chapter-key";
      key.textContent = row.key;
      a.appendChild(key);
    }
    var name = document.createElement("span");
    name.className = "rail-name";
    name.textContent = row.label;
    a.appendChild(name);
    li.appendChild(a);
    list.appendChild(li);
  });

  // Replaced wholesale rather than diffed: this runs after every live patch
  // (the morph resets this element to the server's empty copy), and the list is
  // a handful of nodes nobody is interacting with mid-scroll.
  slot.textContent = "";
  slot.appendChild(head);
  slot.appendChild(list);
  slot.hidden = false;
}

// Delegated, so it survives the rebuild above without re-binding.
document.addEventListener("click", function (ev) {
  var link = ev.target.closest ? ev.target.closest("#rail-chapters a") : null;
  if (!link) return;
  // The node the list was built from, falling back to a lookup by name so an
  // anchor that somehow outlived its build (a stray `data-jump-to` in a
  // template, say) still goes somewhere.
  var el = link._chapterEl;
  if (!el || !el.isConnected) {
    var named = link.getAttribute("data-jump-to");
    el = named ? document.querySelector('[data-jump="' + named + '"]') : null;
  }
  if (!el) return;
  // Only once we know there is somewhere to go: a preventDefault on a chapter
  // whose section has gone would leave a link that does nothing at all.
  ev.preventDefault();
  jumpTo(el);
});

document.addEventListener("DOMContentLoaded", railChapters);

// ---------------------------------------------------------------------------
// Escape lets go of the field.
//
// Wes, 2026-07-28: "hitting escape should de-select whatever text field is
// selected."
//
// This is the missing half of the jump keys rather than a separate nicety. The
// jumps deliberately do nothing while you are typing (see `typingInto`), and N
// deliberately puts the cursor in the note box - so once you have jumped, every
// letter you press is text and there is no keyboard way back out to the keys.
// Escape is that way out: blur, focus falls to <body>, and n/j/t/p answer again.
//
// Three things about where this sits:
//
// - It is the LAST keydown listener in this file, on purpose. Every other
//   Escape handler here reads `ev.target` to find the thing it closes (the
//   context menus, the rename input), and blurring first would
//   not break that - the target is the field either way - but running last
//   means those handlers decide first and this only ever adds to what they did.
//   Each of them hides its field anyway, so letting go of it is never wrong.
// - The lightbox's handler is on the capture phase and calls stopPropagation,
//   so while the image viewer is open Escape closes the viewer and this never
//   runs. That is the right precedence: the modal owns the key.
// - No preventDefault. Escape has browser-level meanings (stopping a load,
//   dismissing an IME candidate window) and swallowing it to blur a textarea
//   would be taking more than was asked for.
//
// Deliberately NOT reconfigurable, unlike the jumps. Escape-to-let-go is a
// convention older than this app - it is what Escape does in vim, in a native
// dialog and in every <select> - and a setting that could turn it off would be
// a setting that strands the cursor.
function escapeBlurTarget(el) {
  if (!el) return null;
  if (el.isContentEditable) return el;
  var tag = el.tagName;
  // Same set as `typingInto`, minus OPTION: an <option> is never focused
  // itself, and the browser owns Escape while a select popup is open.
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return el;
  return null;
}

document.addEventListener("keydown", function (ev) {
  if (ev.key !== "Escape") return;
  var el = escapeBlurTarget(ev.target);
  if (!el || typeof el.blur !== "function") return;
  el.blur();
});

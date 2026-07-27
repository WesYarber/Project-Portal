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

// Ctrl/Cmd+Enter - and, on a real keyboard, Shift+Enter - submits the textarea
// you're typing in, so answering a question or dropping a note never needs a
// trip to the mouse. Plain Enter always inserts a newline.
//
// The pointer test is not decoration. iOS turns its shift key on by itself for
// auto-capitalisation, which is exactly the state a note box is in when you
// start typing, and the return key then arrives as a keydown with
// shiftKey === true. That made every Enter on a phone submit the note
// mid-sentence. Shift+Enter stays for anything with a fine pointer (a mouse,
// hence a hardware keyboard); touch devices get plain newline behaviour and
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

// The "+tag" button on a todo row swaps itself for a tiny input. Delegated so
// rows inserted by a live-refresh morph work without re-enhancement, and the
// swap lives in `hidden`, which the morph treats as JS-owned state.
function closeTagAdd(form) {
  form.hidden = true;
  var wrap = form.closest(".tag-add");
  var btn = wrap && wrap.querySelector(".tag-add-btn");
  if (btn) btn.hidden = false;
}

document.addEventListener("click", function (ev) {
  // Clicking anywhere else while an opened input is still empty puts its +
  // back. Deliberately not focusout-based: blur events do not fire at all in
  // an unfocused (e.g. headless) window, and a click is what actually says
  // "I moved on".
  document.querySelectorAll(".tag-add-form:not([hidden])").forEach(function (form) {
    var wrap = form.closest(".tag-add");
    if (wrap && wrap.contains(ev.target)) return;
    var input = form.querySelector("input");
    if (input && input.value.trim()) return;
    closeTagAdd(form);
  });

  var btn = ev.target.closest ? ev.target.closest('[data-act="tag-add"]') : null;
  if (!btn) return;
  var form = btn.parentElement.querySelector(".tag-add-form");
  if (!form) return;
  btn.hidden = true;
  form.hidden = false;
  var input = form.querySelector("input");
  if (input) input.focus();
});

// Escape clears and closes.
document.addEventListener("keydown", function (ev) {
  if (ev.key !== "Escape") return;
  var form = ev.target.closest ? ev.target.closest(".tag-add-form") : null;
  if (!form) return;
  var input = form.querySelector("input");
  if (input) input.value = "";
  closeTagAdd(form);
});

function selectText(el) {
  var range = document.createRange();
  range.selectNodeContents(el);
  var sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

// Grow textareas to fit their content instead of showing an inner scrollbar.
function autosize(el) {
  el.style.height = "auto";
  el.style.height = el.scrollHeight + 2 + "px";
}
document.addEventListener("input", function (ev) {
  if (ev.target.tagName === "TEXTAREA") autosize(ev.target);
});
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("textarea").forEach(function (el) {
    if (el.value) autosize(el);
  });
  startLiveRunPoll();
  startConsolePoll();
  pinConsole();
  restoreDrafts();
  watchForOffline();
  initDropzones();
  initFileTree();
  initTitleRename();
  restoreScroll();
});

// --- Click the project name to rename it ------------------------------------
//
// The <h1> and the <input> are the same string in the same form; clicking swaps
// which one is showing. Enter submits (it is a lone text input in a form, so
// that is the browser's own behaviour, not ours), Escape puts the original text
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
  // A [data-confirm] form that was cancelled never navigates - remembering
  // where it was would fire on whatever page loads next instead.
  if (ev.defaultPrevented) return;
  var form = ev.target;
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

function initDropzones() {
  document.querySelectorAll("[data-dropzone]").forEach(function (form) {
    if (form._enhanced) return;
    form._enhanced = true;
    var input = form.querySelector('input[type="file"]');
    if (!input) return;
    var status = form.querySelector("[data-attach-status]");

    function refresh() {
      if (!status) return;
      var warning = oversizeWarning(input);
      status.textContent = warning || fileLabel(input);
      status.classList.toggle("error", !!warning);
    }
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
  btn.hidden = false;

  var recorder = null;
  var chunks = [];

  btn.addEventListener("click", function () {
    if (recorder && recorder.state === "recording") {
      recorder.stop();
      return;
    }
    navigator.mediaDevices
      .getUserMedia({ audio: true })
      .then(function (stream) {
        chunks = [];
        recorder = new MediaRecorder(stream);
        recorder.ondataavailable = function (ev) {
          if (ev.data && ev.data.size) chunks.push(ev.data);
        };
        recorder.onstop = function () {
          // Always release the microphone, even if the blob turns out empty -
          // a live mic indicator left on after recording is alarming.
          stream.getTracks().forEach(function (t) {
            t.stop();
          });
          btn.textContent = "record audio";
          btn.classList.remove("recording");
          if (!chunks.length) return;
          var type = recorder.mimeType || "audio/webm";
          var blob = new Blob(chunks, { type: type });
          var ext = type.indexOf("ogg") >= 0 ? "ogg" : type.indexOf("mp4") >= 0 ? "m4a" : "webm";
          var name = "voice-memo-" + new Date().toISOString().replace(/[:.]/g, "-") + "." + ext;
          if (addFiles(input, [new File([blob], name, { type: type })])) refresh();
        };
        recorder.start();
        btn.textContent = "stop recording";
        btn.classList.add("recording");
      })
      .catch(function () {
        btn.textContent = "microphone unavailable";
        btn.disabled = true;
      });
  });
}

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
    });
  }

  function tick() {
    fetch("/api/active-run", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(paint)
      .catch(function () {});
  }
  tick();
  setInterval(tick, 5000);
}

function startConsolePoll() {
  var box = document.getElementById("agent-console");
  if (!box) return;
  var runId = box.getAttribute("data-run-id");
  if (!runId) return;
  var out = document.getElementById("console-out");
  var offset = null; // null until the first fetch replaces the server-rendered tail
  var live = box.getAttribute("data-live") === "1";

  function tick() {
    var url = "/api/run/" + runId + "/log?offset=" + (offset === null ? 0 : offset);
    fetch(url, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var first = offset === null;
        var atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
        if (first) {
          out.textContent = data.text || "(nothing yet)";
        } else if (data.text) {
          out.textContent += data.text;
        }
        offset = data.offset;
        // The first paint replaces the whole transcript, so it lands at the
        // end regardless of where the box happened to be scrolled.
        if (atBottom || first) out.scrollTop = out.scrollHeight;
        if (live && !data.running) {
          // Once, not every tick: the page refreshed in place still says
          // data.running is false on every later poll.
          live = false;
          liveReload();
        }
      })
      .catch(function () {});
  }
  tick();
  if (live) setInterval(tick, 2000);
}

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

  tabs.forEach(function (t) {
    t.addEventListener("click", function () {
      var name = show(t.dataset.panel);
      // replaceState, not a hash assignment: changing location.hash pushes a
      // history entry per tab click, which turns Back into "walk every tab
      // you looked at" instead of "leave this page".
      history.replaceState(null, "", "#" + name);
    });
  });

  bar.classList.add("ready");
  show((location.hash || "").replace(/^#/, ""));
}

document.addEventListener("DOMContentLoaded", initSubtabs);

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
// UI, and why the coloured statuses lost their colour the moment you went
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
    // The option carries its own colour class from the template, so the list
    // reads the same way the closed control does - Wes's ask was that the
    // options be coloured "like they appear once selected".
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
    // like, which existing CSS colours) and the selected option's class.
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

function postForm(action, fields, onDone) {
  var body = new URLSearchParams();
  Object.keys(fields || {}).forEach(function (k) { body.append(k, fields[k]); });
  fetch(action, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  })
    .then(function (r) {
      if (!r.ok) {
        // The route said no (a run in flight, a parent with children). Its
        // reason is a JSON detail; surface it rather than reloading into a
        // page that silently did not change.
        return r.json().then(
          function (data) { alert(data.detail || "That didn't work."); },
          function () { alert("That didn't work."); }
        );
      }
      if (onDone) onDone();
      else liveReload();
    })
    .catch(function () { alert("The portal didn't answer - is it restarting?"); });
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

document.addEventListener("DOMContentLoaded", initProjectDrag);
document.addEventListener("DOMContentLoaded", initProjectMenu);

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
var MORPH_KEEP = ".draft-note, .ctx-menu, #pull-refresh, #img-lightbox, " +
  "#sel-actions, .quote-chip";

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
  // properties back to the server's defaults mid-edit.
  if (isFormField(live) && (name === "value" || name === "checked" || name === "selected")) return true;
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
      if (n.nodeType === 1 && n.id === id && n.tagName === nextChild.tagName) return n;
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
function refreshBlocked() {
  var ae = document.activeElement;
  if (ae) {
    if (ae.tagName === "TEXTAREA" || ae.isContentEditable) return true;
    if (ae.tagName === "INPUT" && SUBMIT_ON_CHORD.test(ae.type)) return true;
  }
  if (document.querySelector(".sel.open, .ctx-menu, [data-record].recording")) return true;
  if (document.body.classList.contains("dragging-project")) return true;
  var sel = window.getSelection && window.getSelection();
  if (sel && !sel.isCollapsed) return true;
  return false;
}

// The panels that scroll internally keep their place across a patch - and one
// pinned to its bottom (a transcript being followed) stays pinned.
var SCROLL_SEL = ".scroll-cap, #console-out";

function snapshotScrolls() {
  return Array.prototype.map.call(document.querySelectorAll(SCROLL_SEL), function (el) {
    return {
      top: el.scrollTop,
      atBottom: el.scrollTop + el.clientHeight >= el.scrollHeight - 4,
    };
  });
}

function restoreScrolls(saved) {
  Array.prototype.forEach.call(document.querySelectorAll(SCROLL_SEL), function (el, i) {
    var s = saved[i];
    if (!s || !s.top) return;
    el.scrollTop = s.atBottom ? el.scrollHeight : s.top;
  });
}

// Keep what Wes is looking at where it is: remember the topmost on-screen
// element with an id, and after the patch scroll by however far it moved -
// so content growing above the viewport (a new journal entry, a new card)
// cannot push the line he is reading down the page.
function viewAnchor() {
  if (!(window.scrollY > 0)) return null; // at the top, the top is the anchor
  var best = null;
  var above = null;
  var els = document.body.querySelectorAll("[id]");
  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    if (el.id === "offline-overlay") continue;
    var r = el.getBoundingClientRect();
    if (!r.height || r.bottom <= 0) continue;
    if (r.top >= 0) {
      if (!best || r.top < best.top) best = { id: el.id, top: r.top };
    } else if (!above || r.top > above.top) {
      above = { id: el.id, top: r.top };
    }
  }
  return best || above;
}

function holdAnchor(anchor) {
  if (!anchor) return;
  var el = document.getElementById(anchor.id);
  if (!el) return;
  var moved = el.getBoundingClientRect().top - anchor.top;
  if (moved) window.scrollBy(0, moved);
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
  if (subtabsApply) subtabsApply();
}

var refreshQueued = false;
var refreshing = false;

function liveRefreshNow() {
  if (refreshing || refreshBlocked()) {
    refreshQueued = true;
    return;
  }
  refreshing = true;
  fetch(location.href, { cache: "no-store", headers: { "X-Live-Refresh": "1" } })
    .then(function (r) {
      if (!r.ok) throw new Error("bad status");
      return r.text();
    })
    .then(function (html) {
      var doc = new DOMParser().parseFromString(html, "text/html");
      if (!doc || !doc.body) return;
      var scrolls = snapshotScrolls();
      var anchor = viewAnchor();
      if (doc.title && doc.title !== document.title) document.title = doc.title;
      morphNode(document.body, doc.body);
      restoreScrolls(scrolls);
      holdAnchor(anchor);
      reinit();
    })
    .catch(function () {
      // A miss is a skipped patch, never an error surface: the offline
      // overlay is the thing that reports the server being gone.
    })
    .then(function () {
      refreshing = false;
      if (refreshQueued && !refreshBlocked()) {
        refreshQueued = false;
        liveRefreshNow();
      }
    });
}

// The older pollers call this where they used to call location.reload().
function liveReload() {
  if (window.fetch && window.DOMParser) liveRefreshNow();
  else window.location.reload();
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
        else if (refreshQueued && !refreshBlocked()) {
          refreshQueued = false;
          liveRefreshNow();
        }
      })
      .catch(function () {});
    // A patch held back by an interaction gets applied on a later tick even
    // if the version has not moved again since.
    if (refreshQueued && !refreshing && !refreshBlocked()) {
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
    dragId: null, dragX: 0, dragY: 0,
  };

  root.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-lb]");
    if (!b) {
      // Nothing. Wes, 2026-07-25: the viewer must NOT close on a click - only
      // Escape or the ✕ in the bar closes it. Clicking the backdrop used to
      // close, which made every misjudged pan, double-click-to-fit and stray
      // click on the dark surround throw away the image you were reading.
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
// stage. x/y are the offset of the image centre from the stage centre.
function lbClamp() {
  var rect = lb.stage.getBoundingClientRect();
  var halfW = (lb.natW * lb.scale) / 2;
  var halfH = (lb.natH * lb.scale) / 2;
  // Allow panning up to the point where an edge of the image reaches the
  // centre of the stage - generous, but never fully off-screen.
  var maxX = Math.max(halfW, rect.width / 2);
  var maxY = Math.max(halfH, rect.height / 2);
  lb.x = Math.max(-maxX, Math.min(maxX, lb.x));
  lb.y = Math.max(-maxY, Math.min(maxY, lb.y));
}

function lbApply() {
  // The img is anchored at the stage centre (top/left 50%) with origin 0,0,
  // so we translate by -half the scaled size (to centre it) plus the pan
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
  // Zoom about the stage centre.
  lbSetScale(lb.scale * f);
  lbClamp();
  lbApply();
}

// Zoom by factor f keeping the stage-space point (px,py) fixed under the
// cursor/fingers.
function lbZoomAt(f, px, py) {
  var rect = lb.stage.getBoundingClientRect();
  var cx = px - rect.width / 2;   // point relative to stage centre
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

// Fit the image to the stage (the default view), centred.
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
  // right side instead." The page column is a fixed max-width centred in the
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
    // Vertically centred on the selection, so it still reads as belonging to
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
var JUMP_KEYS_DEFAULT = {
  n: ["note", "idea"],
  j: ["journal"],
  t: ["todo"],
  p: ["project"]
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
// paints `anim-off` on <body>, and the OS-level preference is honoured too,
// because a long page scrolling under you is exactly the motion both mean.
function jumpBehavior() {
  if (document.body.classList.contains("anim-off")) return "auto";
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return "auto";
  }
  return "smooth";
}

function jumpTo(el) {
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
  // the window, not centred and not scrolled just barely into view.
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

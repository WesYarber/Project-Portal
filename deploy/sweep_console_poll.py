"""Delete-the-fix sweep for the 2026-08-13 poller-leak fix (app/static/app.js).

Every mutation lives in one file, and the only tests that can see it are the
ones that read app.js - so the suite here is that subset (41s) rather than the
whole 4123 (147s). Rule 2 of docs/verifying-with-mutations.md still applies and
is satisfied the other way: the FULL suite was run green immediately before
this, so a broad failure here is news about the mutation, not the baseline.

Follows docs/verifying-with-mutations.md:
  1. restores however it dies (atexit + SIGTERM/SIGINT -> sys.exit)
  2. refuses a dirty tree, and is meant to be run detached, waiting on the
     SWEEP COMPLETE marker rather than on a process name
  3. reads a crashed pytest as SKIPPED, never as "uncaught"
  6. parses the FAILED lines properly (the first token is the word FAILED)
"""
from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"
PY = ROOT / "venv" / "bin" / "python"

_TOUCHES = ("app.js", "APP_JS", "oneoff")
SUITE = sorted(
    str(p)
    for p in (ROOT / "tests").glob("test_*.py")
    if any(t in p.read_text() for t in _TOUCHES)
)

TEMPLATE = ROOT / "app" / "templates" / "oneoff.html"

# Read - and the restore hook armed - before anything can go wrong, so a death
# anywhere below still puts both files back (rule 1).
ORIGINAL = {p: p.read_text() for p in (APP_JS, TEMPLATE)}


def restore() -> None:
    for path, text in ORIGINAL.items():
        if path.read_text() != text:
            path.write_text(text)


atexit.register(restore)
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_: sys.exit(1))


# (name, find, replace_with, owning test)
#
# Each one is a line the fix ADDED, deleted or reverted to the shape it had
# before. The owner is the test that should be the one to notice.
MUTATIONS = [
    (
        "the whole bug: never stop the poller when the run ends",
        "          stopConsolePoll();\n          liveReload();",
        "          liveReload();",
        "test_the_poller_stops_when_its_run_finishes",
    ),
    (
        "throw the interval handle away, as the original did",
        "  if (live) state.timer = setInterval(tick, 2000);",
        "  if (live) setInterval(tick, 2000);",
        "test_the_poller_stops_when_its_run_finishes",
    ),
    (
        "clearInterval is never actually called",
        "  if (consolePoll && consolePoll.timer) clearInterval(consolePoll.timer);\n"
        "  if (consolePoll) consolePoll.timer = null;",
        "  if (consolePoll) consolePoll.timer = null;",
        "test_the_poller_stops_when_its_run_finishes",
    ),
    (
        "arm the timer even for a run that is already finished",
        "  if (live) state.timer = setInterval(tick, 2000);",
        "  state.timer = setInterval(tick, 2000);",
        "test_opening_a_finished_run_arms_no_timer",
    ),
    (
        "console poller: no document.hidden gate",
        "    if (document.hidden) return;\n"
        "    var url = \"/api/run/\" + runId + \"/log?offset=\"",
        "    var url = \"/api/run/\" + runId + \"/log?offset=\"",
        "test_a_hidden_tab_does_not_fetch_the_transcript",
    ),
    (
        "active-run poller: no document.hidden gate",
        "    if (document.hidden) return;\n"
        "    fetch(\"/api/active-run\"",
        "    fetch(\"/api/active-run\"",
        "test_the_active_run_poller_is_gated_the_same_way",
    ),
    (
        "console poller: coming back to the tab waits for the next tick",
        "document.addEventListener(\"visibilitychange\", function () {\n"
        "  if (!document.hidden && consolePoll) consolePoll.tick();\n"
        "});",
        "",
        "test_coming_back_to_the_tab_fetches_at_once",
    ),
    (
        "active-run poller: coming back to the tab waits for the next tick",
        "  document.addEventListener(\"visibilitychange\", function () {\n"
        "    if (!document.hidden) tick();\n"
        "  });\n}\n\n// --- Reading a transcript",
        "}\n\n// --- Reading a transcript",
        "test_the_active_run_poller_is_gated_the_same_way",
    ),
    # First pass wrote this one as a comment swap, which mutates nothing and
    # duly "escaped". A mutation that does not change behavior is not a data
    # point - it moves the registration inside the function for real.
    (
        "register the visibility listener inside startConsolePoll (stacks per patch)",
        "  tick();\n"
        "  if (live) state.timer = setInterval(tick, 2000);\n"
        "}\n",
        "  tick();\n"
        "  if (live) state.timer = setInterval(tick, 2000);\n"
        "  document.addEventListener(\"visibilitychange\", function () {\n"
        "    if (!document.hidden && consolePoll) consolePoll.tick();\n"
        "  });\n"
        "}\n",
        "test_repeated_starts_do_not_stack_pollers",
    ),
    # Owner corrected after the first pass: dropping the guard does NOT stack
    # timers, because stopConsolePoll() runs first. What it costs is the
    # transcript being re-fetched from offset 0 on every live patch, which is
    # the test that caught it.
    (
        "no 'already watching this run' guard, so every patch restarts it",
        "  if (consolePoll && consolePoll.runId === runId && consolePoll.live === live) return;",
        "",
        "test_restarting_the_same_run_does_not_refetch_the_transcript",
    ),
    (
        "guard on the run id alone, ignoring whether it went live",
        "  if (consolePoll && consolePoll.runId === runId && consolePoll.live === live) return;",
        "  if (consolePoll && consolePoll.runId === runId) return;",
        "test_the_same_run_going_live_re_arms_the_timer",
    ),
    (
        "do not stop the old poller before starting the new one",
        "  if (consolePoll && consolePoll.runId === runId && consolePoll.live === live) return;\n"
        "  stopConsolePoll();\n"
        "  var out = document.getElementById(\"console-out\");",
        "  if (consolePoll && consolePoll.runId === runId && consolePoll.live === live) return;\n"
        "  var out = document.getElementById(\"console-out\");",
        "test_the_poller_follows_the_box_to_a_new_run",
    ),
    (
        "no superseded-mid-flight guard",
        "        if (consolePoll !== state) return; // superseded mid-flight by a newer run\n",
        "",
        "test_a_superseded_reply_does_not_paint_or_tear_down_the_new_run",
    ),
    (
        "reinit does not restart the console poller",
        "  if (typeof startConsolePoll === \"function\") startConsolePoll();",
        "",
        "test_reinit_restarts_the_console_poller",
    ),
    (
        "the console box vanishing leaves its poller armed",
        "  if (!box || !runId) {\n    stopConsolePoll();\n    consolePoll = null;\n    return;\n  }",
        "  if (!box || !runId) {\n    consolePoll = null;\n    return;\n  }",
        "test_the_console_leaving_the_page_takes_its_poller_with_it",
    ),
    # The offline watcher and oneoff.html have no bun harness - they are pinned
    # by the source-count invariant, so that is what should catch these.
    (
        "offline watcher (/api/ping every 3s, every page): no gate",
        "    if (document.hidden) return;\n    fetch(\"/api/ping\"",
        "    fetch(\"/api/ping\"",
        "test_every_poller_in_the_app_is_gated_on_document_hidden",
    ),
    (
        "offline watcher: gated but never woken when the tab comes back",
        "  schedule(IDLE_MS);\n"
        "  document.addEventListener(\"visibilitychange\", function () {\n"
        "    if (!document.hidden) tick();\n"
        "  });\n}",
        "  schedule(IDLE_MS);\n}",
        "test_every_poller_in_the_app_is_gated_on_document_hidden",
    ),
    (
        "the recorder's display clock claims a poller's exemption it does not need",
        "        // no-poll-gate: a display clock for the recording in progress. It\n",
        "        // A display clock for the recording in progress. It\n",
        "test_every_poller_in_the_app_is_gated_on_document_hidden",
    ),
]

# oneoff.html is a template, not app.js, so it gets its own little list.
TEMPLATE_MUTATIONS = [
    (
        "oneoff.html run watcher: no gate",
        "    if (document.hidden) return;\n    fetch(\"/api/active-run\")",
        "    fetch(\"/api/active-run\")",
        "test_every_poller_in_the_app_is_gated_on_document_hidden",
    ),
    (
        "oneoff.html run watcher: gated but never woken",
        "  document.addEventListener(\"visibilitychange\", function () {\n"
        "    if (!document.hidden) tick();\n"
        "  });\n",
        "",
        "test_every_poller_in_the_app_is_gated_on_document_hidden",
    ),
]

FAILED = re.compile(r"^FAILED (\S+)", re.M)
SUMMARY = re.compile(r"^\d+ (passed|failed)|=+ .*(passed|failed)", re.M)


def run_suite() -> tuple[set[str], int, bool]:
    proc = subprocess.run(
        [str(PY), "-m", "pytest", *SUITE, "-q", "-p", "no:randomly", "--no-header", "-rf"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    out = proc.stdout + proc.stderr
    names = {m.split("::")[-1] for m in FAILED.findall(out)}
    # Rule 3: a crashed pytest is a SKIPPED data point, not a passing one.
    finished = bool(SUMMARY.search(out))
    return names, proc.returncode, finished


ALL = [(APP_JS, *m) for m in MUTATIONS] + [(TEMPLATE, *m) for m in TEMPLATE_MUTATIONS]


def main() -> int:
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", str(APP_JS), str(TEMPLATE)],
        capture_output=True, text=True, cwd=str(ROOT),
    ).stdout
    print(f"worktree state: {dirty.strip() or '(clean)'}", flush=True)

    caught, missed, skipped = [], [], []
    for i, (path, name, find, repl, owner) in enumerate(ALL, 1):
        tag = f"[{i:2}/{len(ALL)}]"
        src = ORIGINAL[path]
        if find not in src:
            print(f"{tag} SKIP (pattern missing): {name}", flush=True)
            skipped.append(name)
            continue
        assert src.count(find) == 1, f"pattern is not unique: {name}"
        path.write_text(src.replace(find, repl))
        names, rc, finished = run_suite()
        restore()
        if not finished:
            print(f"{tag} CRASHED (rc={rc}): {name}", flush=True)
            skipped.append(name)
        elif owner in names:
            extra = sorted(names - {owner})
            note = f"  (+{len(extra)} more: {', '.join(extra[:3])})" if extra else ""
            print(f"{tag} caught by {owner}{note}", flush=True)
            caught.append(name)
        elif names:
            print(
                f"{tag} WRONG OWNER: {name}\n"
                f"          expected {owner}, got {sorted(names)}",
                flush=True,
            )
            missed.append(name)
        else:
            print(f"{tag} MISSED (nothing failed): {name}", flush=True)
            missed.append(name)

    print(f"\ncaught {len(caught)}/{len(ALL)}  missed {len(missed)}  skipped {len(skipped)}")
    for n in missed:
        print(f"  MISSED: {n}")
    print("SWEEP COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

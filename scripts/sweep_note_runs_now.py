#!/usr/bin/env python3
"""Delete-the-fix sweep for "the Add note button runs now".

Follows all three safety rules in docs/verifying-with-mutations.md:

1. REFUSES a dirty tree, and restores however it dies (atexit + SIGTERM/SIGINT
   that call sys.exit, since a bare handler that returns does not run atexit).
2. Prints `SWEEP COMPLETE` at the end; nothing may read app/ until it appears.
3. The caller re-runs the plain suite afterwards.

Four files are mutated, because the decision is spread across four: the
predicate and the action in app/worker.py, the route that calls it in
app/main.py, the button row that reflects it in project.html, and the phone
menu that has to follow that row in app.js. Rule 4 of the doc - mutate the
wiring, not only the logic - is the whole reason the last three are here.

Scoped to the owning test files rather than the full suite, per the trade commit
b1dcd35 made: a CAUGHT here is conclusive, while an ESCAPED only means these
files do not hold the line and has to be re-checked more widely.
"""
import atexit, signal, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WK = ROOT / "app" / "worker.py"
MN = ROOT / "app" / "main.py"
TP = ROOT / "app" / "templates" / "project.html"
JS = ROOT / "app" / "static" / "app.js"
TESTS = [
    "tests/test_note_runs_now.py",
    "tests/test_ui_notes_4.py",
    "tests/test_note_menu.py",
    "tests/test_state_model.py",
]

if subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT).returncode != 0:
    sys.exit("REFUSING: tree is dirty. A sweep must start from a committed tree.")

ORIGINAL = {p: p.read_text(encoding="utf-8") for p in (WK, MN, TP, JS)}


def restore():
    for path, text in ORIGINAL.items():
        path.write_text(text, encoding="utf-8")


atexit.register(restore)
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_: sys.exit("killed by signal"))

# (path, label, find, replace). Each is one decision the code makes.
MUTATIONS = [
    # --- app/worker.py: can_run_now, the predicate ---
    (WK, "the shelf stops mattering, so a done project runs on a note",
     '    if str(project["stage"]) not in RUNNABLE_STAGES:\n        return False',
     "    if False:\n        return False"),
    (WK, "backlog joins the runnable shelves",
     'RUNNABLE_STAGES = {"active", "review"}',
     'RUNNABLE_STAGES = {"active", "review", "backlog"}'),
    (WK, "review drops off the runnable shelves",
     'RUNNABLE_STAGES = {"active", "review"}',
     'RUNNABLE_STAGES = {"active"}'),
    (WK, "an agent already in the workspace no longer holds a note-triggered run",
     '    if db.is_project_running(int(project["id"])):\n        return False',
     "    if False:\n        return False"),
    (WK, "the kernel lease is not consulted, only the runs table",
     '    return not workspace_leased(str(project["slug"]))',
     "    return True"),
    # --- app/worker.py: note_arrived, the action ---
    (WK, "a note no longer wakes a parked project",
     "    if await reactivate_on_note(project):\n        return True",
     "    if False:\n        return True"),
    (WK, "the predicate is ignored and every note queues a run",
     "    if not can_run_now(project):\n        return False",
     "    if False:\n        return False"),
    (WK, "the run is never actually queued",
     '    await queue_manual_run(int(project["id"]))\n    return True',
     "    return True"),
    # --- app/main.py: the wiring ---
    (MN, "the route keeps the old behavior and only reactivates",
     "            await worker.note_arrived(project)",
     "            await worker.reactivate_on_note(project)"),
    (MN, "a voice memo's continuation reverts to the old behavior",
     "            transcribe.kick(audio_ids, after=worker.note_arrived(project))",
     "            transcribe.kick(audio_ids, after=worker.reactivate_on_note(project))"),
    (MN, "the page is never told whether the green button will run",
     '            "note_runs_now": worker.can_run_now(project),\n',
     ""),
    # --- app/templates/project.html: the button row ---
    (TP, "add & run now is rendered beside a green button that already runs",
     "      {% if not note_runs_now %}",
     "      {% if True %}"),
    (TP, "add & run now is dropped even where it is the only way to run",
     "      {% if not note_runs_now %}",
     "      {% if False %}"),
    (TP, "the group loses the margin that right-justifies it",
     "class=\"btn secondary{{ ' note-actions' if note_runs_now }}\"",
     'class="btn secondary"'),
    (TP, "the queue button goes back to its old label",
     "        queue note\n",
     "        queue &amp; don't run\n"),
    (TP, "the green button's title no longer says it will run",
     "title=\"{{ 'Add this note and start an agent run on it now' if note_runs_now\n"
     "                        else 'Add this note for the agent to read on its next run' }}\"",
     'title="Add this note"'),
    # --- app/static/app.js: the phone menu ---
    (JS, "the menu goes back to a hardcoded pair, offering a button that is not there",
     "    var alts = form.querySelectorAll('button[name=\"then\"]');\n"
     "    if (!alts.length) return;",
     "    var alts = [{ value: 'run', textContent: 'add & run now' },\n"
     "                { value: 'queue', textContent: 'queue note' }];"),
    (JS, "the menu label stops coming off the button it submits",
     '      li.textContent = (alt.textContent || "").trim();',
     '      li.textContent = "an option";'),
]

caught = escaped = skipped = 0
for path, label, find, repl in MUTATIONS:
    text = ORIGINAL[path]
    if find not in text:
        print(f"SKIP (pattern missing): {label}", flush=True)
        skipped += 1
        continue
    path.write_text(text.replace(find, repl, 1), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-qx", "--no-header", "-p", "no:randomly"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f"ESCAPED: {label}", flush=True)
        escaped += 1
    else:
        print(f"caught:  {label}", flush=True)
        caught += 1
    restore()

print(f"\n{caught} caught, {escaped} escaped, {skipped} skipped, of {len(MUTATIONS)}")
print("SWEEP COMPLETE", flush=True)

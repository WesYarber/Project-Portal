#!/usr/bin/env python3
"""Delete-the-fix sweep for the board-wide per-project daily run cap.

Breaks one decision point at a time, runs the owning test files, and records
which test fails. A mutation nothing fails on is a line no test is holding in
place.

Follows all three rules in docs/verifying-with-mutations.md:

1. restores however it dies - atexit plus SIGTERM/SIGINT handlers that
   sys.exit (a bare handler that returns does not run atexit);
2. meant to be run detached with `python -u`, waited on by its final
   `SWEEP COMPLETE` marker rather than by the process;
3. refuses a dirty baseline - it captures the original text from files it has
   confirmed are green, so a leftover mutation from a killed sweep cannot
   become the new baseline.

While this runs, NOTHING may read app/ - a detached sweep makes the whole
working tree untrustworthy for its entire run, not just at the moment it dies.
That matters more than usual here: the portal self-updates from its own runs,
so a mutation left in the tree at the wrong moment is a mutation that DEPLOYS.
"""
from __future__ import annotations

import atexit
import signal
import subprocess
import sys
from pathlib import Path

# Derived, not written out: tests/test_leakscan.py fails this tree while any
# source file names a personal path, and that guard covers untracked files too.
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "app" / "db.py"
WK = ROOT / "app" / "worker.py"
CF = ROOT / "app" / "config.py"
SF = ROOT / "app" / "settings_form.py"

ORIGINAL: dict[Path, str] = {}


def restore_all() -> None:
    for path, text in ORIGINAL.items():
        try:
            if path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
                print(f"  restored {path.name}", flush=True)
        except OSError as exc:  # noqa: PERF203
            print(f"  COULD NOT RESTORE {path}: {exc}", flush=True)


def _die(signum, _frame):
    print(f"!! signal {signum}, restoring", flush=True)
    sys.exit(1)


# (file, find, replace, label)
MUTATIONS = [
    # --- the default itself --------------------------------------------------
    (DB,
     '        return max(0, int(get_setting("project_max_runs_per_day") or "6"))',
     '        return max(0, int(get_setting("project_max_runs_per_day") or "0"))',
     "the default is off unless set, which is the state this fixed"),

    (DB,
     '        return max(0, int(get_setting("project_max_runs_per_day") or "6"))',
     '        return max(0, int(get_setting("project_max_runs_per_day") or "999"))',
     "the default is set so high it can never bind"),

    (DB,
     "    except ValueError:\n        return 6",
     "    except ValueError:\n        raise",
     "a junk setting stops falling back and takes the worker down with it"),

    (CF,
     '    "project_max_runs_per_day": "6",',
     '    "project_max_runs_per_day": "0",',
     "a fresh install seeds the cap off"),

    # --- whose number wins ---------------------------------------------------
    (WK,
     "    if not cap:\n"
     "        if pacing.spending_down():\n"
     "            return False\n"
     "        cap = db.default_project_max_runs()",
     "    cap = db.default_project_max_runs()",
     "the project's own cap is ignored and the default always used"),

    (WK,
     "    if not cap:\n"
     "        if pacing.spending_down():\n"
     "            return False\n"
     "        cap = db.default_project_max_runs()",
     "    if not cap:\n"
     "        if pacing.spending_down():\n"
     "            return False\n"
     "        cap = db.default_project_max_runs()\n"
     "    cap = min(int(cap), db.default_project_max_runs() or 10**6)",
     "a project's own HIGHER cap is clamped by the default, losing the escape hatch"),

    # --- the spend-down carve-out -------------------------------------------
    (WK,
     "    if not cap:\n"
     "        if pacing.spending_down():\n"
     "            return False\n"
     "        cap = db.default_project_max_runs()",
     "    if pacing.spending_down():\n"
     "        return False\n"
     "    if not cap:\n"
     "        cap = db.default_project_max_runs()",
     "a spend-down lifts a cap Wes set by hand, not just the default"),

    (WK,
     "    if not cap:\n"
     "        if pacing.spending_down():\n"
     "            return False\n"
     "        cap = db.default_project_max_runs()",
     "    if not cap:\n"
     "        cap = db.default_project_max_runs()",
     "a spend-down stops lifting the default, so an expiring window goes unspent"),

    # --- the comparison ------------------------------------------------------
    (WK,
     '    return db.count_runs_today(project["id"]) >= int(cap)',
     '    return db.count_runs_today(project["id"]) > int(cap)',
     "off by one: a project gets one run more than its cap"),

    (WK,
     '    return db.count_runs_today(project["id"]) >= int(cap)',
     "    return False",
     "the cap is computed and then never applied"),

    (WK,
     "    if not cap:  # 0 either way means no cap at all\n        return False",
     "    pass",
     "0 stops meaning off, so it becomes a cap of zero runs"),

    # --- what the scheduler does with it -------------------------------------
    (WK,
     "        if not project_at_daily_cap(candidate):\n            return candidate, False",
     "        return candidate, False",
     "the scheduler stops consulting the cap at all"),

    (WK,
     "        if not project_at_daily_cap(candidate):\n            return candidate, False",
     "        if project_at_daily_cap(candidate):\n            return None, False\n"
     "        return candidate, False",
     "a capped project idles the whole board behind it instead of being skipped"),

    # --- the dashboard saying so ---------------------------------------------
    (WK,
     '            "every active project has taken all its runs for today - "\n'
     '            f"they reset in {resets_in}"',
     '            "every actionable project has hit its own per-project daily cap"',
     "the dashboard goes back to blaming a per-project cap that is usually not the cause"),

    # --- the settings field ---------------------------------------------------
    (SF,
     '        Field("project_max_runs_per_day", _positive_int("6", low=0, high=999)),',
     "",
     "the field vanishes from the settings form, so the number cannot be changed"),
]

# The test files that own these lines. Running these instead of the whole suite
# is a deliberate trade, and the same one commits b1dcd35 and 87dfafc made: a
# full-suite sweep is ~135s per mutation, which is longer than the run
# supervising it, and a sweep killed part way is the exact failure
# docs/verifying-with-mutations.md was written about. What it costs is one
# direction of certainty - a "caught" here is conclusive, an "ESCAPED" only
# means these files do not hold the line and has to be re-checked against
# everything before it is believed.
SUITE = [
    "tests/test_project_run_cap.py",
    "tests/test_parallel.py",
    "tests/test_pacing.py",
    "tests/test_settings_form.py",
    "tests/test_priority_removed.py",
]


def run_suite() -> tuple[int, list[str]]:
    # No `-x`: the point is which test owns the line, so every failure has to
    # be visible. Stopping at the first one names whichever test collection
    # order reached first, which is not the same question.
    proc = subprocess.run(
        [str(ROOT / "venv" / "bin" / "python"), "-m", "pytest", *SUITE, "-q",
         "--no-header", "--tb=no", "-p", "no:warnings"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=1800,
    )
    failures = [
        line.split("::")[-1].split()[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED")
    ]
    return proc.returncode, failures


def main() -> int:
    for path in (DB, WK, CF, SF):
        ORIGINAL[path] = path.read_text(encoding="utf-8")
    atexit.register(restore_all)
    signal.signal(signal.SIGTERM, _die)
    signal.signal(signal.SIGINT, _die)

    print("baseline: running the suite unmutated", flush=True)
    rc, failures = run_suite()
    if rc != 0:
        print(f"BASELINE IS NOT GREEN ({failures}); a sweep would mean nothing", flush=True)
        print("SWEEP COMPLETE", flush=True)
        return 1
    print("baseline green\n", flush=True)

    caught = 0
    for i, (path, find, replace, label) in enumerate(MUTATIONS, 1):
        text = ORIGINAL[path]
        if find not in text:
            print(f"{i:2}. SKIP (pattern missing) - {label}", flush=True)
            continue
        path.write_text(text.replace(find, replace, 1), encoding="utf-8")
        rc, failures = run_suite()
        restore_all()
        if rc == 0:
            print(f"{i:2}. ESCAPED  - {label}", flush=True)
        else:
            caught += 1
            print(f"{i:2}. caught   - {label}", flush=True)
            print(f"      by {', '.join(sorted(set(failures))[:3])}", flush=True)

    print(f"\n{caught}/{len(MUTATIONS)} caught", flush=True)
    print("SWEEP COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

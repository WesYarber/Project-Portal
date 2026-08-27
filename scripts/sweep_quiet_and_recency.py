#!/usr/bin/env python3
"""Delete-the-fix sweep for quiet hours and the rail's recency sort.

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
This matters more than usual here: the portal self-updates from its own runs,
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
QU = ROOT / "app" / "quiet.py"
SB = ROOT / "app" / "sidebar.py"
DB = ROOT / "app" / "db.py"
WK = ROOT / "app" / "worker.py"
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
    # --- quiet hours: the window and its wrap -------------------------------
    (QU,
     "    return hour >= start or hour < end if start > end else start <= hour < end",
     "    return start <= hour < end",
     "the midnight wrap is dropped, so 23->7 is never quiet"),

    (QU,
     "    return hour >= start or hour < end if start > end else start <= hour < end",
     "    return hour >= start or hour < end",
     "a non-wrapping window is read as a wrapping one"),

    (QU,
     "    return hour >= start or hour < end if start > end else start <= hour < end",
     "    return hour >= start or hour <= end if start > end else start <= hour < end",
     "the end hour becomes inclusive, so runs never resume at the named hour"),

    (QU,
     "    return None if start == end else (start, end)",
     "    return (start, end)",
     "equal hours stop turning the feature off"),

    # --- quiet hours: the zone ----------------------------------------------
    (QU,
     "    return now.astimezone(zone())",
     "    return now.astimezone()",
     "the hours are read in the host's zone instead of the configured one"),

    (QU,
     "        now = now.replace(tzinfo=timezone.utc)",
     "        now = now.astimezone()",
     "a naive timestamp is read as host-local rather than as UTC"),

    (QU,
     '    name = (db.get_setting(ZONE_SETTING) or "").strip() or DEFAULT_ZONE',
     '    name = "UTC"',
     "the configured zone is ignored entirely"),

    (QU,
     "    if current and current not in common:\n        common.append(current)",
     "    pass",
     "a hand-set zone vanishes from its own dropdown"),

    # --- quiet hours: the resume arithmetic ---------------------------------
    (QU,
     "    if resume <= local:\n        resume += timedelta(days=1)",
     "    pass",
     "the pre-midnight resume lands in the past"),

    (QU,
     "        if not is_quiet(now):\n            return None",
     "        if is_quiet(now):\n            return None",
     "the hold fires while he is awake and not while he is asleep"),

    (QU,
     "    except Exception:  # noqa: BLE001 - fail open, never idle the portal on a bug\n"
     "        log.exception(\"quiet hours guard failed\")\n"
     "        return None",
     "    except Exception:  # noqa: BLE001\n"
     "        raise",
     "the guard stops failing open"),

    # --- the worker consulting it -------------------------------------------
    (WK,
     "        if quiet.quiet_hold() is not None:\n            return False",
     "        pass",
     "the scheduler stops asking about quiet hours at all"),

    (WK,
     "    quiet_hours = quiet.quiet_hold()\n"
     "    if quiet_hours is not None:\n"
     "        return quiet.quiet_reason(quiet_hours)",
     "    pass",
     "the dashboard stops saying why it is holding overnight"),

    # --- the settings fields ------------------------------------------------
    (SF,
     "        Field(quiet.ZONE_SETTING, _timezone(quiet.DEFAULT_ZONE)),",
     "        Field(quiet.ZONE_SETTING, _text),",
     "an unreal zone is stored unchallenged"),

    (SF,
     "        Field(quiet.START_SETTING, _hour_of_day(quiet.DEFAULT_START)),",
     "        Field(quiet.START_SETTING, _hour),",
     "a bad quiet hour falls back to the day-reset hour"),

    # --- the rail's recency sort --------------------------------------------
    (DB,
     "                SELECT project_id, started_at AS ts FROM runs WHERE project_id IS NOT NULL",
     "                SELECT project_id, NULL AS ts FROM runs WHERE 0",
     "a run STARTING stops counting as activity"),

    (DB,
     "                SELECT project_id, ended_at AS ts FROM runs\n"
     "                 WHERE project_id IS NOT NULL AND ended_at IS NOT NULL",
     "                SELECT project_id, NULL AS ts FROM runs WHERE 0",
     "a run ENDING stops counting as activity"),

    (DB,
     "                SELECT project_id, ts FROM journal WHERE project_id IS NOT NULL",
     "                SELECT project_id, NULL AS ts FROM journal WHERE 0",
     "journal entries and notes stop counting as activity"),

    # ESCAPES, and is MEANT to - an equivalent mutant, kept here so the next
    # sweep does not rediscover it and write a test pinning a no-op. Every
    # branch of that UNION is already NULL-free: journal.ts and runs.started_at
    # are NOT NULL columns, and the ended_at branch carries its own
    # `ended_at IS NOT NULL`. So the added WHERE can remove no row and empty no
    # group, and MAX() ignores NULLs regardless. Verified against the live
    # database, not assumed: zero NULLs in either column.
    (DB,
     "            ) GROUP BY project_id",
     "            ) WHERE ts IS NOT NULL GROUP BY project_id",
     "MAX becomes a filter (EQUIVALENT - expected to escape)"),

    (SB,
     '            "since": max(\n'
     '                str(activity.get(pid) or ""),\n'
     '                str(db._row_get(project, "updated_at", "") or ""),  # noqa: SLF001\n'
     "            ),",
     '            "since": str(db._row_get(project, "updated_at", "") or ""),  # noqa: SLF001',
     "the rail goes back to sorting on updated_at (the reported bug)"),

    (SB,
     '            "since": max(\n'
     '                str(activity.get(pid) or ""),\n'
     '                str(db._row_get(project, "updated_at", "") or ""),  # noqa: SLF001\n'
     "            ),",
     '            "since": str(activity.get(pid) or ""),',
     "a project with no runs and no journal loses its floor"),
]

# The test files that own these lines. Running these instead of the whole suite
# is a deliberate trade, and the same one commit b1dcd35 made: 21 mutations
# against the full 3977-test suite is ~50 minutes, longer than the run that has
# to supervise it, and a sweep killed part way is the exact failure
# docs/verifying-with-mutations.md was written about. What it costs is one
# direction of certainty - a "caught" here is conclusive, an "ESCAPED" only
# means these files do not hold the line and has to be re-checked against
# everything before it is believed.
SUITE = [
    "tests/test_quiet_hours.py",
    "tests/test_rail_recency.py",
    "tests/test_side_rail.py",
    "tests/test_pacing.py",
    "tests/test_saturation.py",
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
    for path in (QU, SB, DB, WK, SF):
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

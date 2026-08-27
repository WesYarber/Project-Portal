#!/usr/bin/env python3
"""Delete-the-fix sweep for the per-project runs/day control (Wes, 2026-08-13).

Two things changed and both are logic:

- `projects.max_runs_per_day` grew a third state. NULL inherits the board
  default, a number binds in both directions, and 0 - which the route used to
  fold away to NULL - now means "no cap on this project at all".
- The control that sets it is back in the project page's control bar, showing
  the number actually in force.

So the mutations cover the predicate (`worker.effective_project_cap`), the
route that writes the value, the context that feeds the page, and the template
itself. Rule 4 of docs/verifying-with-mutations.md is why the last two are in
here: a pure function is easy to test and a call site is easy to forget.

Follows the same three rules as scripts/sweep_project_run_cap.py: restores
however it dies, is meant to be run detached and waited on by its final
`SWEEP COMPLETE` marker, and refuses a dirty baseline.

While this runs, NOTHING may read app/ - the portal self-updates from its own
runs, so a mutation left in the tree at the wrong moment is one that DEPLOYS.
"""
from __future__ import annotations

import atexit
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WK = ROOT / "app" / "worker.py"
MN = ROOT / "app" / "main.py"
PT = ROOT / "app" / "templates" / "project.html"
ST = ROOT / "app" / "templates" / "settings.html"

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
    # --- the three states, in worker.effective_project_cap -------------------
    (WK,
     "    if cap is not None:\n        return max(0, int(cap))",
     "    if cap:\n        return max(0, int(cap))",
     "0 on the project folds back to 'inherit', which is the bug he reported"),

    (WK,
     "    if cap is not None:\n        return max(0, int(cap))",
     "    if cap is not None:\n        return max(1, int(cap))",
     "'no cap' becomes a cap of one run a day"),

    (WK,
     "    if cap is not None:\n        return max(0, int(cap))",
     "",
     "the project's own number is ignored entirely and the default always used"),

    (WK,
     "    if cap is not None:\n        return max(0, int(cap))\n"
     "    if pacing.spending_down():\n        return 0",
     "    if pacing.spending_down():\n        return 0\n"
     "    if cap is not None:\n        return max(0, int(cap))",
     "a spend-down lifts a cap Wes set by hand, not just the default"),

    (WK,
     "    if pacing.spending_down():\n        return 0\n",
     "",
     "a spend-down stops lifting the default, so an expiring window goes unspent"),

    (WK,
     "    return db.default_project_max_runs()",
     "    return 0",
     "an uncapped project stops inheriting the board default"),

    (WK,
     "    except (IndexError, KeyError):  # row from a pre-migration query\n        return 0",
     "    except (IndexError, KeyError):  # row from a pre-migration query\n        raise",
     "a pre-migration row takes the scheduler down instead of reading as uncapped"),

    # --- and what the scheduler does with the number -------------------------
    (WK,
     "    cap = effective_project_cap(project)\n    if not cap:\n        return False",
     "    cap = effective_project_cap(project)",
     "0 stops meaning 'no cap' and becomes a cap of zero runs"),

    (WK,
     '    return db.count_runs_today(project["id"]) >= cap',
     '    return db.count_runs_today(project["id"]) > cap',
     "off by one: a project gets one run more than its cap"),

    # --- the route that stores what the control picks ------------------------
    (MN,
     "            cap = max(0, int(raw))",
     "            cap = max(0, int(raw)) or None",
     "the route folds 0 back to NULL, exactly as it did before his note"),

    (MN,
     "    if not raw:\n        cap: Optional[int] = None",
     "    if not raw:\n        cap: Optional[int] = 0",
     "'default' stores 'no cap', so inheriting the board limit is unreachable"),

    # --- the context the page reads (rule 4: mutate the wiring) --------------
    (MN,
     '            "project_cap": worker.effective_project_cap(project),',
     '            "project_cap": db.default_project_max_runs(),',
     "the hint's denominator ignores the project's own cap and shows the default"),

    (MN,
     '            "default_project_cap": db.default_project_max_runs(),',
     '            "default_project_cap": 0,',
     "the 'default (N)' option loses the board's number"),

    # --- the control itself --------------------------------------------------
    (PT,
     '    <form method="post" action="/project/{{ project.slug }}/run-cap" class="control control-runs">',
     '    <form method="post" action="/project/{{ project.slug }}/run-cap" class="control control-hidden">',
     "the control is on the page but not in the control bar"),

    (PT,
     '<option value="0" {{ \'selected\' if project.max_runs_per_day == 0 else \'\' }}>no cap</option>',
     "",
     "there is no way to take one project off the board limit"),

    (PT,
     "{{ 'selected' if project.max_runs_per_day is none else '' }}>default",
     "{{ 'selected' if not project.max_runs_per_day else '' }}>default",
     "a project set to 'no cap' displays as inheriting the default"),

    (PT,
     "{% if project.max_runs_per_day and project.max_runs_per_day not in caps %}\n"
     "    {% set caps = (caps + [project.max_runs_per_day])|sort %}\n"
     "    {% endif %}",
     "",
     "a cap set from Telegram or sqlite off the preset list vanishes from the control"),

    (PT,
     "onchange=\"this.form.submit()\">\n        <option value=\"\" {{ 'selected' if project.max_runs_per_day is none else '' }}>",
     "onchange=\"\">\n        <option value=\"\" {{ 'selected' if project.max_runs_per_day is none else '' }}>",
     "the control no longer saves on change, unlike both of its siblings"),

    (PT,
     "{{ runs_today }}{% if project_cap %}/{{ project_cap }}{% endif %} run",
     "{{ runs_today }} run",
     "the hint stops saying what the number is measured against"),

    # --- and the settings field pointing at it -------------------------------
    (ST,
     '<label for="project_max_runs_per_day">Default per-project runs a day',
     '<label for="project_max_runs_per_day">Per-project runs a day',
     "the settings field stops calling itself a default - the misreading he had"),

    (ST,
     " One project's own limit is the runs/day box on its project page, which overrides this in both directions.",
     "",
     "settings stops pointing at where a single project's own limit is set"),
]

# The files that own these lines. A "caught" here is conclusive; an "ESCAPED"
# only means these files do not hold the line, and is re-checked against the
# whole suite before it is believed.
SUITE = [
    "tests/test_run_cap_control.py",
    "tests/test_project_run_cap.py",
    "tests/test_usage.py",
    "tests/test_ui_notes_4.py",
    "tests/test_ui_notes_3.py",
    "tests/test_parallel.py",
    "tests/test_approval.py",
]


def run_suite() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [str(ROOT / "venv" / "bin" / "python"), "-m", "pytest", *SUITE, "-q",
         "--no-header", "--tb=no", "-p", "no:warnings"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=1800,
    )
    # Rule 3: a crashed run is a skipped data point, not a passing one - so the
    # verdict reads the return code, and the failure names are only there to
    # say WHO caught it.
    failures = [
        line.split("::")[-1].split()[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED")
    ]
    return proc.returncode, failures


def main() -> int:
    for path in (WK, MN, PT, ST):
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

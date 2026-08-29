#!/usr/bin/env python3
"""Delete-the-fix sweep for the rail's nav and its recency sort (2026-08-15).

Each mutation takes out one decision this change ADDED and runs the test files
that own the touched sources. A mutation nothing fails on is a line nothing is
holding in place.

Read docs/verifying-with-mutations.md before editing this. The rules it costs
the most to relearn, all of them applied here:

  - restore however you die (atexit + signal handlers that sys.exit);
  - refuse to start on a dirty tree, so a killed sweep cannot poison the next;
  - a crashed pytest is a SKIPPED data point, never a passing one;
  - parse the FAILED lines properly - the first token of one is "FAILED";
  - mutate the wiring (the template, the call site) and not only the logic.
"""
from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIDEBAR = ROOT / "app" / "sidebar.py"
MAIN = ROOT / "app" / "main.py"
BASE = ROOT / "app" / "templates" / "base.html"
CSS = ROOT / "app" / "static" / "style.css"

# The files that read the sources above. Not the whole suite: the full run is
# what happens before the sweep and again before the commit.
SUITE = [
    "tests/test_side_rail.py",
    "tests/test_rail_nav.py",
    "tests/test_rail_recency.py",
    "tests/test_rail_chapters.py",
    "tests/test_shelves.py",
    "tests/test_appearance.py",
]

FILES = (SIDEBAR, MAIN, BASE, CSS)
ORIGINAL: dict[Path, str] = {p: p.read_text(encoding="utf-8") for p in FILES}


def restore_all() -> None:
    for path, text in ORIGINAL.items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore_all)
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, lambda *_: sys.exit(2))


# (label, file, before, after, the test that should catch it)
MUTATIONS = [
    (
        "the dashboard tab matches by prefix, so every page lights it",
        SIDEBAR,
        'if href == "/":\n        return path == "/"',
        'if href == "/":\n        return path.startswith(href)',
        "test_a_project_page_lights_no_section_rather_than_all_of_them",
    ),
    (
        "a run console no longer belongs to the activity tab",
        SIDEBAR,
        ' or any(path.startswith(p) for p in prefixes)',
        '',
        "test_the_current_section_comes_off_the_path",
    ),
    (
        "the everyone door is shown to everybody",
        SIDEBAR,
        'if key == "everyone" and not everyone:\n            continue',
        'if False:\n            continue',
        "test_the_everyone_door_is_the_owners_and_only_when_shared",
    ),
    (
        "the nav badges always read zero",
        SIDEBAR,
        '"count": counts.get(key, 0),',
        '"count": 0,',
        "test_the_counts_ride_the_rows_they_belong_to",
    ),
    (
        "the everyone door opens for a person who is not the owner",
        MAIN,
        'everyone=bool(person and person["is_owner"] and len(people.everyone()) > 1)',
        'everyone=bool(person and len(people.everyone()) > 1)',
        "test_the_everyone_tab_is_not_offered_to_the_person_who_is_not_the_owner",
    ),
    (
        "a failed badge count takes the whole nav down with it",
        MAIN,
        'except Exception:  # noqa: BLE001\n        log.debug("Could not count the nav badges; drawing bare links", exc_info=True)\n        return sidebar.nav_rows(path)',
        'except ValueError:\n        return sidebar.nav_rows(path)',
        "test_the_nav_survives_a_database_that_cannot_be_counted",
    ),
    (
        "the rail draws no nav at all",
        BASE,
        '<nav class="rail-section rail-nav" id="rail-nav" aria-label="portal sections">',
        '<nav class="rail-section rail-nav" id="rail-nav-off" aria-label="portal sections">',
        "test_every_page_carries_the_nav_in_its_rail",
    ),
    (
        "the rail's nav never marks where you are",
        BASE,
        '<a href="{{ item.href }}"{% if item.current %} aria-current="page"{% endif %}>\n            <span class="rail-name">{{ item.label }}</span>',
        '<a href="{{ item.href }}">\n            <span class="rail-name">{{ item.label }}</span>',
        "test_the_rail_marks_the_section_you_are_in",
    ),
    (
        "the rail's nav grows a number key and steals one from the projects",
        BASE,
        '<li id="rail-nav-{{ item.key }}" class="{{ \'here\' if item.current }}">\n          <a href="{{ item.href }}"',
        '<li id="rail-nav-{{ item.key }}" class="{{ \'here\' if item.current }}">\n          <a data-rail-digit="1" href="{{ item.href }}"',
        "test_the_nav_never_takes_a_number_key_from_the_project_list",
    ),
    (
        "the tabs go back to a hand-written second copy of the nav",
        BASE,
        '{% for item in nav %}\n        <a class="tab-btn {{ \'active\' if item.current }}" href="{{ item.href }}">{{ item.label }}{% if item.count %}<span class="nav-count">{{ item.count }}</span>{% endif %}</a>\n        {% endfor %}',
        '<a class="tab-btn" href="/">dashboard</a>\n        <a class="tab-btn" href="/questions">questions</a>',
        "test_the_template_draws_both_copies_from_the_same_list",
    ),
    (
        "a parked project is no longer marked dim",
        SIDEBAR,
        '"dim": shelf not in buckets,',
        '"dim": False,',
        "test_a_paused_project_says_it_is_paused_rather_than_only_a_date",
    ),
    (
        "the pause outranks a question the row could still answer",
        SIDEBAR,
        'if not status and shelf == "paused":',
        'if shelf == "paused":',
        "test_a_question_on_a_paused_project_still_outranks_the_pause",
    ),
    (
        "recent mode goes back to listing only the working shelves",
        SIDEBAR,
        'recent.append(row)\n        if shelf not in buckets:\n            extras.append(row)\n            continue\n        buckets[shelf].append(row)',
        'if shelf not in buckets:\n            extras.append(row)\n            continue\n        buckets[shelf].append(row)\n        recent.append(row)',
        "test_recent_is_one_list_and_status_decides_nothing_about_where_a_row_lands",
    ),
    (
        "recent mode draws the More tail as well, listing parked projects twice",
        SIDEBAR,
        'if extras and mode != "recent":',
        'if extras:',
        "test_a_project_appears_once_in_the_recent_list",
    ),
    (
        "shelf mode loses its grouping too",
        SIDEBAR,
        'if mode == "recent":',
        'if mode != "shelf-never":',
        "test_shelf_mode_still_groups_by_status",
    ),
    (
        "the dim row is styled by nothing",
        CSS,
        '.rail-section li.dim .rail-name { color: var(--terminal-dim); }',
        '.rail-section li.nodim .rail-name { color: var(--terminal-dim); }',
        "test_a_parked_project_is_dimmed_one_row_at_a_time_too",
    ),
    (
        "the nav flexes, so it pushes the project list off a short window",
        CSS,
        '.rail-status,\n#rail-nav,\n#rail-chapters { flex: none; }',
        '.rail-status,\n#rail-chapters { flex: none; }',
        "test_the_nav_is_styled_as_part_of_the_rail",
    ),
]


def run_suite() -> tuple[int, set[str], bool]:
    proc = subprocess.run(
        # -n0: pytest.ini parallelizes by default, but a sweep runs one small file
        # at a time (where that buys nothing) and then PARSES this output, which
        # interleaves across workers.
        [sys.executable, "-m", "pytest", *SUITE, "-q", "--no-header",
         "-p", "no:randomly", "-n0"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    out = proc.stdout + proc.stderr
    failed = set(re.findall(r"^FAILED (\S+?)::(\S+)", out, re.MULTILINE))
    # A parametrized case comes back as `test_x[/run/12-activity]`. Comparing
    # that whole string against an owner name reports a catch as a MISS - §6 of
    # the doc in the other direction, and it cost this sweep one re-run.
    names = {name.split("[")[0] for _, name in failed}
    # A crashed run emits no summary line at all; that is a skipped data point,
    # never a pass. (§3 of docs/verifying-with-mutations.md.)
    summarized = bool(re.search(r"\d+ (passed|failed|error)", out))
    return proc.returncode, names, summarized


def main() -> int:
    dirty = subprocess.run(
        ["git", "status", "--porcelain", *[str(p.relative_to(ROOT)) for p in FILES]],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    print(f"working tree for the swept files:\n{dirty or '  (clean)'}", flush=True)

    code, names, ok = run_suite()
    print(f"baseline: rc={code} failures={sorted(names) or 'none'}", flush=True)
    if code != 0 or not ok:
        print("BASELINE NOT GREEN - a sweep off a red tree proves nothing", flush=True)
        return 1

    caught, missed, skipped = [], [], []
    for i, (label, path, before, after, owner) in enumerate(MUTATIONS, 1):
        text = ORIGINAL[path]
        if before not in text:
            print(f"{i:2d}. SKIP (pattern missing): {label}", flush=True)
            skipped.append(label)
            continue
        if text.count(before) != 1:
            print(f"{i:2d}. SKIP (pattern is not unique): {label}", flush=True)
            skipped.append(label)
            continue
        path.write_text(text.replace(before, after), encoding="utf-8")
        try:
            code, names, ok = run_suite()
        finally:
            path.write_text(text, encoding="utf-8")
        if not ok:
            print(f"{i:2d}. SKIPPED (pytest crashed): {label}", flush=True)
            skipped.append(label)
        elif owner in names:
            print(f"{i:2d}. caught by {owner} ({len(names)} failing): {label}", flush=True)
            caught.append(label)
        else:
            print(
                f"{i:2d}. MISSED: {label}\n"
                f"    expected {owner}, got {sorted(names) or 'nothing'}",
                flush=True,
            )
            missed.append(label)

    print(f"\ncaught {len(caught)}/{len(MUTATIONS)}, missed {len(missed)}, skipped {len(skipped)}")
    for label in missed:
        print(f"  MISSED: {label}")
    print("SWEEP COMPLETE", flush=True)
    return 0 if not missed and not skipped else 1


if __name__ == "__main__":
    sys.exit(main())

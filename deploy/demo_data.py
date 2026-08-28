#!/usr/bin/env python
"""Boot a throwaway portal full of invented data, purely to photograph it.

This is what the screenshots at the top of the README are taken from. It has
to be a script rather than a set of saved images because the UI keeps moving:
a shot is only worth having if it can be retaken in one command when the page
it shows changes.

Everything in here is made up. That is the point twice over — a README shot of
a real board would publish somebody's projects, their machine's hostname and
their notification channels to anyone who clones the repo, and a board that
happens to be quiet the day the shot is taken makes the tool look empty.

    venv/bin/python deploy/demo_data.py 8598          # then open it
    HOST=0.0.0.0 venv/bin/python deploy/demo_data.py  # reachable from a browser box

It cannot touch the real board: its own data directory under /tmp, its own
database, and `worker_enabled=0` written before uvicorn starts, so no run, no
reflect and no compaction can fire out of it.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TMP = Path("/tmp/portal-readme-demo")
if TMP.exists():
    shutil.rmtree(TMP)

from app import config  # noqa: E402

config.DATA_DIR = TMP
config.DB_PATH = TMP / "portal.db"
config.MEMORY_DIR = TMP / "memory"
config.PROJECTS_DIR = TMP / "projects"
config.RUNS_DIR = TMP / "runs"
config.TASKS_DIR = TMP / "tasks"
config.INCOMING_DIR = TMP / "incoming"
config.PROFILE_MD = TMP / "memory" / "profile.md"
config.LEARNINGS_MD = TMP / "memory" / "learnings.md"
config.SUGGESTIONS_MD = TMP / "memory" / "suggestions.md"

# The installation's own identity, replaced wholesale. Left alone, every shot
# would carry the real owner's name in the header and their real hostname in
# every URL the page prints - which is exactly the kind of thing app/leakscan.py
# exists to keep out of a public repo, and a PNG is the one file it cannot read.
config.SITE = dataclasses.replace(
    config.SITE,
    owner="Sam",
    gender="female",
    host="home-server",
    ssh_user="sam",
    ntfy_topic="portal-demo",
    contact_email="",
)

from app import db, limits, main as _main, people  # noqa: E402

db._seed_data = lambda: None
db.init_db()
db.set_setting("max_parallel_runs", "3")

# The worker reads as ON, because a screenshot captioned "worker offline" says
# the tool is broken rather than idle - but the loops that would act on that
# are never started (see below), so nothing here can spawn a run, message
# anybody, or spend a token.
db.set_setting("worker_enabled", "1")

# Every background loop, replaced with one that does nothing. `limits.poll_loop`
# is the one that matters most: left alone it reaches the real Claude usage
# endpoint with the real stored OAuth token, and the answer - somebody's actual
# subscription percentages and reset times - lands in the header of every shot.
_main._BACKGROUND_TASKS.clear()
_main.on_startup = lambda: None
_main.app.router.on_startup = [
    h for h in _main.app.router.on_startup if h.__name__ != "on_startup"
]

# An invented usage reading in the real shape, so the pacing header has
# something honest-looking to show without asking anyone's account.
_FAKE_USAGE = limits.parse({
    "ok": True,
    "plan": "max",
    "tier": "",
    "raw": {
        "five_hour": {"utilization": 34,
                      "resets_at": (datetime.now(timezone.utc)
                                    + timedelta(hours=2, minutes=41)).isoformat()},
        "seven_day": {"utilization": 41,
                      "resets_at": (datetime.now(timezone.utc)
                                    + timedelta(days=3, hours=6)).isoformat()},
    },
})
limits.cached = lambda max_age_sec=None: _FAKE_USAGE
limits.refresh = lambda timeout=15.0: _FAKE_USAGE
limits.fetch_raw = lambda *a, **k: {"ok": False, "error": "demo"}


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


# --------------------------------------------------------------------------
# The people
# --------------------------------------------------------------------------

owner = int(people.owner()["id"])
alex = people.add(
    name="Alex",
    gender="male",
    background=(
        "Comfortable with the shell and with git, new to running agents "
        "unattended. Wants to see the diff before anything is merged."
    ),
)

# --------------------------------------------------------------------------
# The board
# --------------------------------------------------------------------------

PROJECTS = [
    dict(title="Greenhouse sensor logger", slug="greenhouse",
         description="An ESP32 in the greenhouse reporting temperature and soil "
                     "moisture to a page I can check from the kitchen.",
         stage="active", kind="mixed", build_approved=True),
    dict(title="Recipe box", slug="recipe-box",
         description="Somewhere to keep the recipes that are currently on paper, "
                     "with a print view that fits one card.",
         stage="active", kind="software", build_approved=True),
    dict(title="Bookshelf catalog", slug="bookshelf",
         description="Point a phone camera at a shelf and get back a list of "
                     "what is on it.",
         stage="review", kind="software", build_approved=True),
    dict(title="Weekly meal planner", slug="meal-planner",
         description="Pick seven dinners, get one shopping list grouped by aisle.",
         stage="active", kind="software", build_approved=True),
    dict(title="Doorbell clip archive", slug="doorbell",
         description="Keep the last 30 days of doorbell clips somewhere that is "
                     "not the camera company's cloud.",
         stage="active", kind="software", build_approved=True),
    dict(title="Garage door notifier", slug="garage-door",
         description="Tell me if the garage door has been open for more than "
                     "twenty minutes.",
         stage="done", kind="mixed", build_approved=True),
    dict(title="Photo wall for the hallway iPad", slug="photo-wall",
         description="A slideshow that pulls from the family album and never "
                     "needs touching.",
         stage="backlog", kind="software"),
    dict(title="Rain barrel level gauge", slug="rain-barrel",
         description="A float sensor and a number on a page. Not started.",
         stage="backlog", kind="hardware"),
]

made: dict[str, object] = {}
for spec in PROJECTS:
    made[spec["slug"]] = db.create_project(**spec)

people.set_members(made["recipe-box"]["id"], [owner, alex])
people.set_members(made["bookshelf"]["id"], [owner, alex])

# --------------------------------------------------------------------------
# What has happened on them
# --------------------------------------------------------------------------

db.add_journal(
    made["greenhouse"]["id"], "user", "note",
    "The soil sensor reads 0 whenever the pump runs. I think it is a power "
    "problem rather than the sensor - the 3.3V rail probably sags.",
)
db.add_journal(
    made["greenhouse"]["id"], "agent", "progress",
    "## The rail was sagging, and now it is measured rather than guessed\n\n"
    "Added a 470uF bulk capacitor on the sensor's supply and a 20 ms settle "
    "before each read. Logged 400 pump cycles overnight: the rail now bottoms "
    "out at 3.19 V instead of 2.71 V, and no read has come back 0 since. The "
    "firmware change is committed; the capacitor is a physical change you "
    "already made, so nothing is waiting on you.\n\n"
    "The old readings are kept rather than deleted - they are real data about "
    "a real fault, and the graph marks where the fix landed.",
)
db.add_journal(
    made["recipe-box"]["id"], "user", "note",
    "Can the print view fit two columns on letter paper? Right now one recipe "
    "takes a whole page and I want a card.",
    person_id=alex,
)
db.add_journal(
    made["bookshelf"]["id"], "agent", "progress",
    "## Spine OCR works on a straight shelf, and says so when it does not\n\n"
    "Recognizes 47 of 52 spines on the test shelf. The five it misses are all "
    "at an angle past 30 degrees, so rather than guessing it now marks them as "
    "unread and puts a tap-to-type box on each one. Ready for you to look at.",
)

db.add_todo(made["greenhouse"]["id"], "Graph the last 30 days rather than 24 hours", "agent")
db.add_todo(made["greenhouse"]["id"], "Solder the second sensor to the spare header", "user")
db.add_todo(made["recipe-box"]["id"], "Two-column print layout for letter paper", "agent")
db.add_todo(made["recipe-box"]["id"], "Import the recipes from the blue folder", "user",
            person_id=alex)
db.add_todo(made["meal-planner"]["id"], "Work out where the aisle groupings come from",
            "agent", tags=["research"])
db.add_todo(made["meal-planner"]["id"], "Decide whether to buy the grocery API key", "user",
            tags=["blocked"])
db.add_todo(made["bookshelf"]["id"], "Handle spines rotated past 30 degrees", "agent")

import json  # noqa: E402

db.create_question(
    made["meal-planner"]["id"],
    "The shopping list can group by aisle two ways: the layout of one specific "
    "store (accurate, breaks if you shop somewhere else) or a generic order "
    "that is roughly right everywhere. Which do you want?",
    quick_options=json.dumps(["my store", "generic"]),
)
# On the review shelf deliberately, not on an active project. An open question
# folds an *active* project onto the Paused shelf (db.project_shelf), so putting
# both questions on active projects empties the Active column - which is a
# correct portal and a terrible photograph of one.
db.create_question(
    made["bookshelf"]["id"],
    "A book scanned twice - once from the spine, once from the cover - should "
    "it merge into one entry or stay as two?",
    quick_options=json.dumps(["merge them", "keep both"]),
)

# --------------------------------------------------------------------------
# Runs, so the activity page and the usage sparklines have a shape
# --------------------------------------------------------------------------

HISTORY = [
    ("greenhouse", "build", "ok", 3, 41, 128_000, 9_400),
    ("greenhouse", "build", "ok", 9, 33, 96_000, 7_100),
    ("recipe-box", "build", "ok", 14, 52, 141_000, 12_200),
    ("bookshelf", "build", "ok", 20, 61, 168_000, 15_800),
    ("recipe-box", "plan", "ok", 27, 18, 52_000, 3_900),
    ("meal-planner", "triage", "ok", 31, 11, 34_000, 2_100),
    ("garage-door", "build", "ok", 44, 29, 88_000, 6_400),
    ("bookshelf", "build", "error", 50, 4, 11_000, 600),
    ("greenhouse", "build", "ok", 55, 37, 112_000, 8_800),
    ("recipe-box", "build", "ok", 62, 44, 133_000, 10_500),
]
for slug, task, status, hours, turns, cache_read, out in HISTORY:
    run_id = db.create_run(made[slug]["id"], task, "claude-opus-5")
    db.record_run_usage(run_id, input_tokens=4_200, output_tokens=out,
                        cache_write_tokens=18_000, cache_read_tokens=cache_read,
                        prompt_bytes=86_000)
    # A cost is set as well as the token counts: the activity page's "weight"
    # column reads `cost_usd`, and a table of 0.00w reads as the tool failing to
    # measure itself rather than as a quiet week.
    db.finish_run(run_id, status, summary="", num_turns=turns,
                  cost_usd=round(0.02 + turns * 0.031, 2))
    conn = db.get_conn()
    with db._LOCK:
        conn.execute("UPDATE runs SET started_at = ?, ended_at = ? WHERE id = ?",
                     (ago(hours=hours), ago(hours=hours, minutes=-22), run_id))
        conn.commit()

# One run in flight, with a console to show. The dashboard's "what an agent is
# doing right now" rail is half of what the tool is, and it is blank on a
# screenshot of an idle board.
live = db.create_run(made["recipe-box"]["id"], "build", "claude-opus-5")
conn = db.get_conn()
with db._LOCK:
    conn.execute(
        "UPDATE runs SET started_at = ?, last_activity = ?, events = ?, "
        "last_event_at = ? WHERE id = ?",
        (ago(minutes=6), "Running the print-layout tests (18 passed)", 74,
         ago(seconds=12), live),
    )
    conn.commit()

from app import runlog  # noqa: E402

runlog.RunLog(live).append([
    "> Read app/templates/print.html",
    "> Edited app/static/print.css - two-column grid at 8.5in",
    "> Bash: venv/bin/python -m pytest tests/test_print_view.py -q",
    "  18 passed in 1.4s",
    "> Reading the card width back off the rendered page rather than trusting",
    "  the stylesheet: 3.5in x 5in, which is what the recipe cards actually are.",
])

# --------------------------------------------------------------------------
# Memory, so /memory is not an empty pair of boxes
# --------------------------------------------------------------------------

config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
config.PROFILE_MD.write_text(
    "# Profile: Sam\n\n"
    "## Who she is\n\n"
    "- Self-hoster, runs everything on one home server and reads it from a "
    "phone. Comfortable with a shell, not interested in maintaining a "
    "toolchain.\n"
    "- Tests by using rather than by reading code: say whether a change is "
    "live, on what URL, and show a picture of it.\n\n"
    "## How she wants things built\n\n"
    "- Dependency-free by preference. SQLite over anything that needs a "
    "server of its own.\n"
    "- A blocker gets a button, not a report: anything the pipeline rejects "
    "needs an in-page way to fix or skip it.\n"
    "- Nothing fails quietly. A stalled sync, a partial import and a dropped "
    "sensor all have to say so on the page.\n",
    encoding="utf-8",
)
config.LEARNINGS_MD.write_text(
    "# Learnings\n\n"
    "- The greenhouse ESP32 browns out when the pump starts; anything reading "
    "that rail needs a settle delay before it trusts a number.\n"
    "- The hallway iPad is stuck on an old iOS, so wall-screen pages have to "
    "avoid newer CSS.\n"
    "- Screenshots she sends are 2x Retina crops - halve them before measuring "
    "anything against them.\n",
    encoding="utf-8",
)

import uvicorn  # noqa: E402

from app import main  # noqa: E402

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8598
uvicorn.run(main.app, host=os.environ.get("HOST", "127.0.0.1"), port=port,
            log_level="warning")

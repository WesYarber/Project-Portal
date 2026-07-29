"""Boot a throwaway portal with two people in it, purely to photograph the UI.

Used to verify the people feature by looking at it rather than inferring it from
a test. It is a separate data directory and a separate port, so it cannot touch
the real board - and `worker_enabled=0` is written before uvicorn starts, so it
cannot spawn an agent run, spend weekly allowance or rewrite memory. (That gate
covers scheduled runs, the daily reflect and compaction alike; a previous run
found it only covered the first of the three and fixed it.)

    venv/bin/python deploy/demo_people.py 8599
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TMP = Path("/tmp/portal-people-demo")
if TMP.exists():
    shutil.rmtree(TMP)

from app import config  # noqa: E402

config.DATA_DIR = TMP
config.DB_PATH = TMP / "portal.db"
config.MEMORY_DIR = TMP / "memory"
config.PROJECTS_DIR = TMP / "projects"
config.RUNS_DIR = TMP / "runs"
config.TASKS_DIR = TMP / "tasks"
config.PROFILE_MD = TMP / "memory" / "profile.md"
config.LEARNINGS_MD = TMP / "memory" / "learnings.md"
config.SUGGESTIONS_MD = TMP / "memory" / "suggestions.md"

from app import db, people  # noqa: E402

db._seed_data = lambda: None
db.init_db()
db.set_setting("worker_enabled", "0")

erin = people.add(
    name="Erin",
    # `pronouns` until 2026-07-28, when the column was replaced by `gender`
    # (Wes: "get rid of the pronoun stuff and just ask someone if they are male
    # or female"). This script kept passing the old keyword and had been dead
    # with a TypeError ever since - nothing imports it, so nothing noticed.
    gender="female",
    background=(
        "Newer to self-hosting and to agent tooling - explain the concepts "
        "rather than naming them, and teach the thing behind the answer."
    ),
)
# Her own notification channels, so the routing fields have something in them
# to photograph. Empty would be the truthful default for somebody just added -
# and it is what the placeholder text in the panel is there to explain.
people.update(erin, ntfy_topic="erin-portal", telegram_chat_id="88214417")
hers = db.create_project(
    title="Recipe box",
    description="Somewhere to keep the recipes that are currently on paper.",
    stage="active",
    person_id=erin,
)
ours = db.create_project(
    title="The tool we are building together",
    description="Both of ours - one journal, one todo list, either of us can prompt it.",
    stage="active",
)
people.set_members(ours["id"], [int(people.owner()["id"]), erin])
db.add_journal(hers["id"], "user", "note", "How do I get started with this?", person_id=erin)

# A checklist with something on both halves, so the "whose?" re-file control
# has both of its cases to photograph: handing a person's item back to the
# agent, and handing an agent item to a person. The solo project below is the
# one-member case, where the menu is the agent and one name with no "nobody" -
# there being nobody to be undecided between.
db.add_todo(ours["id"], "Work out why the sync stalls after an hour", "agent")
db.add_todo(ours["id"], "Scope the export format", "agent", tags=["research"])
db.add_todo(ours["id"], "Pick which of the two layouts to keep", "user", person_id=erin)
db.add_todo(ours["id"], "Buy the second SSD", "user", tags=["blocked"])
db.add_todo(hers["id"], "Type in the recipes from the blue folder", "user")
db.add_todo(hers["id"], "Build the print view", "agent")

import uvicorn  # noqa: E402

from app import main  # noqa: E402

uvicorn.run(main.app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")

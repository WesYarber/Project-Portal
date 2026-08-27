"""Which projects a person sees.

Wes, 2026-07-28:

  "I only want users to see projects they are included on. with the exception
   of me as the admin - I want a way where I can go view other users' projects,
   but dont want them in my main feed."

Two rules, and keeping them apart is the whole design:

- **The feed is yours.** Every listing surface - the dashboard shelves, the
  journal feed under them, the open-question banner, the nav badge, the
  questions page, the activity table and the project menu on it - shows only
  projects you are a member of. That includes the owner. He asked for other
  people's work not to be in his main feed, so he is filtered exactly like
  everybody else rather than exempted, and the admin view is a *place he goes*
  rather than a thing that leaks back in.
- **The owner has a separate door.** `/everyone` lists the whole board grouped
  by whose it is, and only he can reach it.

A note on what this is NOT, because the difference matters and the code above
this line cannot enforce it:

    This is a VIEW filter, not an authorization boundary.

The portal has no passwords. Identity comes from a cookie you can change from
the header dropdown (or from `tailscale whois`), and a project page fetched by
its own URL still renders for anybody who asks for it. The schema comment at
db.py:278 says the same thing about the membership table itself - it is
"emphatically NOT a tenant column: nothing about a journal, a todo or a run is
partitioned by person". What this buys is a board that shows you your own work
instead of thirty cards that are not yours. What it does not buy is secrecy
from someone already on the tailnet who goes looking. Anything that needs the
second thing needs real authentication first, and would be a different change.
"""
from __future__ import annotations

import sqlite3

from app import db, people


def visible_ids(person: sqlite3.Row | None) -> set[int]:
    """The project ids `person` sees in their own feed.

    A project with no members at all falls to the owner. That state is not
    reachable through the app - `db.create_project` always adds a member and
    `people.set_members` refuses to empty a project - but if it ever arises the
    alternatives are both worse: showing it to nobody makes live work vanish
    with no way to find it again, and showing it to everybody puts unclaimed
    work into a second person's feed, which is the thing being fixed here.
    """
    if person is None:
        return set()
    ids = people.project_ids_for(person["id"])
    if person["is_owner"]:
        ids |= db.memberless_project_ids()
    return ids


def pending_questions(person: sqlite3.Row | None) -> list[sqlite3.Row]:
    """The open questions that count as waiting on `person` right now.

    One definition, three surfaces: the nav badge, the top of /questions, and
    the `app_badge` a web push paints on the home-screen icon. They drifting
    apart is the failure worth designing out - an icon saying 3 that opens onto
    a list of 1 is worse than an icon saying nothing at all.

    Shelved projects (paused by hand, or still in the backlog) are left out.
    Wes asked the number to mean "things waiting on me right now"; those
    questions are still answerable, in their own section below the fold.
    """
    shelved = db.shelved_project_ids()
    ids = visible_ids(person)
    return [
        q for q in db.open_questions()
        if q["project_id"] in ids and q["project_id"] not in shelved
    ]


def is_admin(person: sqlite3.Row | None) -> bool:
    """Who may look at somebody else's board. The install owner, and only him.

    Read off the `is_owner` column rather than from config, so it follows the
    same row every other owner-shaped rule already follows.
    """
    return bool(person is not None and person["is_owner"])


def only_runs(snapshot: dict, ids: set[int], admin: bool = False) -> dict:
    """The live-run strip, narrowed to runs on projects `ids` can see.

    The strip carries project titles and slugs, so an unfiltered one announces
    what everybody else is working on from the top of every page - the one
    surface that leaks a title without ever looking like a project listing.

    `idle_reason` goes with it for anybody but the owner, because the worker
    writes lines like "about to start a run on His Thing" and "pacing the next
    run - His Thing starts in about four minutes". Scrubbing those by matching
    titles against the text would be guesswork; dropping the line is not, and
    it costs nothing real. The reason describes the install's scheduler queue,
    which is an operational detail of the whole portal rather than anything a
    second person can see the projects in or act on.

    One-off task runs carry no `project_id`, so a bare membership test drops
    them - which had the side rail saying "1 agent working" over a strip
    showing "no agent running" on the very same page. They follow the rule
    `sidebar.visible_runs` already applies: counted for the owner, whose task
    console they belong to, and left out for anybody else rather than leaking
    a task title into their chrome.
    """
    runs = [
        r for r in snapshot.get("runs", [])
        if r.get("project_id") in ids or (admin and not r.get("project_id"))
    ]
    if not runs:
        return {
            "active": False,
            "runs": [],
            "run_ids": "",
            "project_ids": [],
            "idle_reason": snapshot.get("idle_reason", "") if admin else "",
        }
    return {
        **runs[0],
        "runs": runs,
        "run_ids": ",".join(str(r["run_id"]) for r in sorted(runs, key=lambda r: r["run_id"])),
        "project_ids": [r["project_id"] for r in runs if r["project_id"]],
        "idle_reason": "",
    }


def by_person(viewer: sqlite3.Row | None) -> list[dict]:
    """The whole board for `/everyone`, grouped by whose it is.

    Other people first and the viewer last: he asked to go and look at *other
    users'* projects, so his own thirty-odd cards - which are already his whole
    front page - must not be what greets him here. Archived people are included
    when they still hold projects, because retiring somebody does not retire
    their work and a group that quietly vanished would read as projects going
    missing.
    """
    everyone = people.everyone(include_archived=True)
    grouped = []
    for person in everyone:
        ids = people.project_ids_for(person["id"])
        if not ids:
            continue
        rows = [p for p in db.list_projects() if p["id"] in ids]
        if not rows:
            continue
        grouped.append({
            "person": person,
            "projects": rows,
            "is_viewer": viewer is not None and person["id"] == viewer["id"],
        })
    grouped.sort(key=lambda g: (g["is_viewer"], g["person"]["name"].lower()))

    orphans = [p for p in db.list_projects() if p["id"] in db.memberless_project_ids()]
    if orphans:
        grouped.append({"person": None, "projects": orphans, "is_viewer": False})
    return grouped

"""The desktop side rail: what an agent is doing right now, and where to go next.

Wes, 2026-08-01:

  "When on desktop with extra unused horizontal space, let's add a nav bar to
  the side. at the top, it should show a compressed status widget showing how
  many agents are active at the time and for what projects. Then, under that,
  it should show a list of active projects, then projects in review as well as
  their status. They should update in real time and allow you to click on them
  to jump to that project. ... Would also be good to have one click access to
  any given tab/section in the project I'm in."

Three of those four are here. The fourth - the section list for the page you
are on - is built in the browser (`railChapters` in static/app.js) from the
page's own `[data-jump]` elements, because base.html renders around a content
block and cannot see what that block declared.

"They should update in real time" costs nothing and is why the rail is
server-rendered into base.html rather than fetched: the live refresh already
re-fetches the current URL and morphs the whole `<body>` on any database
commit, so a rail inside that body is live by construction. There is no second
poller and no second endpoint to keep in step with this one.

Everything below is a pure function of rows and dicts - no database, no
request, no `me()` - which is what makes the precedence rules testable without
standing anything up. main.py does the gathering and hands the results in.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from . import config, db

# The shelves the rail carries, in Wes's order ("a list of active projects,
# then projects in review"). Deliberately not every shelf: a rail is a place to
# go back to work, and paused, backlog and done are all "not now". The
# dashboard remains the whole board.
RAIL_SHELVES: tuple[tuple[str, str], ...] = (
    ("active", "Active"),
    ("review", "In review"),
)

# How many projects the rail will list at most.
#
# Wes, 2026-08-01: "have it show as many of the most recent projects that have
# been worked on as will fit on the remainder of the screen (without having to
# scroll). Allow this section to be scrolled to view older ones."
#
# "As many as will fit" is a fact about the window, so it is decided in CSS -
# the list flexes into whatever is left under the status widget and the chapter
# list, and scrolls past that. This number is only the point past which more
# rows would be a page of markup nobody will ever scroll to: at ~2.4rem a row,
# forty is around 1000px of list, comfortably more than any window has left.
RAIL_MAX_ROWS = 40

# Which digit jumps to which row. Wes: "Allow me to press number keys to jump to
# the projects over there based on their order on the list." Ten rows get a
# digit and the rest do not, because there are ten digits - and 0 is tenth
# rather than first, which is where it sits on the keyboard.
RAIL_DIGITS: str = "1234567890"

# The portal's own top-level sections, in the order the tabs across the top have
# always run: key, label, href, and the extra path prefixes that also count as
# being "in" that section.
#
# Wes, 2026-08-15: "I want the dashboard, tasks, etc to show up on the nav bar on
# the side even when in project pages."
#
# They were already on every page - as the tabs at the top of the content, which
# scroll away. On a project page, which is the longest page in the portal, that
# means reaching /tasks costs a scroll to the top first. The rail is fixed, so
# putting the same list in it is the whole fix.
#
# This table is the ONE definition of the nav: base.html draws both the top tabs
# and the rail's copy from `nav_rows`, so a section added here appears in both
# and the two can never come to disagree about which one is current.
NAV_LINKS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("dashboard", "dashboard", "/", ()),
    ("tasks", "tasks", "/tasks", ()),
    ("questions", "questions", "/questions", ()),
    # A run console is reached from the activity log and belongs to it, which is
    # what the tabs have always said - the rail says the same thing.
    ("activity", "activity", "/activity", ("/run/",)),
    ("memory", "memory", "/memory", ()),
    ("everyone", "everyone", "/everyone", ()),
    ("settings", "settings", "/settings", ()),
)

# The sections the rail leaves out. Wes, 2026-08-16: "From the side-bar on
# projects, remove the memory, everyone, and settings options."
#
# A shorter rail, not a smaller portal: these three keep their tab across the
# top of every page, which is where they were reached from before the rail
# carried a nav at all. The rail is the way back to work - the board, the tasks,
# the questions, what an agent is doing - and memory, everyone and settings are
# none of those; they are places you go on purpose, once.
#
# Named keys rather than a fifth column on the table above, so the omission
# reads as one line and `nav_rows` stays the only thing that knows the shape of
# a row. `test_the_rail_omits_only_sections_that_exist` is what stops a rename
# in NAV_LINKS from quietly putting settings back in the rail.
RAIL_OMIT: frozenset[str] = frozenset({"memory", "everyone", "settings"})


def nav_current(href: str, prefixes: Sequence[str], path: str) -> bool:
    """Is `path` inside the section reached by `href`?

    The dashboard matches exactly and nothing else: every path in the portal
    starts with "/", so a prefix test there would light every tab on every page.
    """
    path = path or "/"
    if href == "/":
        return path == "/"
    return path.startswith(href) or any(path.startswith(p) for p in prefixes)


def nav_rows(
    path: str = "",
    *,
    questions: int = 0,
    tasks: int = 0,
    everyone: bool = False,
) -> list[dict]:
    """The top-level nav, for the tabs and for the rail alike.

    `everyone` is the admin door into other people's boards: only the owner has
    one, and only once there is somebody else to look at, so the caller decides
    and this stays a pure function of its arguments.

    A count of 0 is carried as 0 rather than dropped, so a template can decide
    whether a zero is worth drawing - the tabs draw nothing, since a badge
    reading 0 is noise where a badge reading 2 is the point.

    `rail` says whether the row also belongs in the side rail (see RAIL_OMIT).
    Every row is still returned, so this remains ONE list drawn twice and the
    two copies cannot disagree about which section is current - the rail simply
    draws fewer of them.
    """
    counts = {"questions": int(questions or 0), "tasks": int(tasks or 0)}
    rows: list[dict] = []
    for key, label, href, prefixes in NAV_LINKS:
        if key == "everyone" and not everyone:
            continue
        rows.append(
            {
                "key": key,
                "label": label,
                "href": href,
                "count": counts.get(key, 0),
                "current": nav_current(href, prefixes, path),
                "rail": key not in RAIL_OMIT,
            }
        )
    return rows


def current_slug(path: str) -> str:
    """The project slug the reader is looking at, or "".

    Used only to mark a row as the current one, so anything that is not a
    project page is correctly an empty string rather than an error. Matches the
    project page and everything hanging off it (`/project/<slug>/todos/history`
    and friends), because on those pages the rail is still "the project you are
    in".
    """
    parts = [p for p in (path or "").split("/") if p]
    if len(parts) >= 2 and parts[0] == "project":
        return parts[1]
    return ""


def visible_runs(snapshot: Mapping, ids: set[int], admin: bool = False) -> list[dict]:
    """The in-flight runs this reader may be told about, newest first.

    Runs on projects go by exactly the rule `scope.only_runs` already applies to
    the dashboard's live strip - membership - so the rail's count and the
    strip's list can never disagree about the same run. This function does not
    re-derive that rule; it is given the same `ids`.

    One-off sessions are the wrinkle. They carry no `project_id` at all, so a
    membership test drops every one of them, and the rail would then say "no
    agents working" with an agent visibly working - the exact quiet lie this
    portal keeps refusing to ship. They are counted for the owner, who is the
    person the one-off console belongs to, and left out for anybody else rather
    than leaking somebody else's task title into their chrome.
    """
    out: list[dict] = []
    for run in snapshot.get("runs", []) or []:
        project_id = run.get("project_id")
        if project_id:
            if project_id not in ids:
                continue
            label = run.get("project_title") or run.get("project_slug") or "a project"
            slug = run.get("project_slug") or ""
            href = f"/project/{slug}" if slug else f"/run/{run.get('run_id')}"
        else:
            if not admin:
                continue
            label = run.get("project_title") or "a one-off task"
            href = f"/tasks/{run['oneoff_id']}" if run.get("oneoff_id") else "/tasks"
        out.append(
            {
                "run_id": run.get("run_id"),
                "project_id": project_id,
                "label": label,
                "href": href,
                "elapsed": run.get("elapsed") or "",
                "last_activity": run.get("last_activity") or "",
                # What it last said, for the line under its name in the rail;
                # "" until it has said anything (app/runlog.py `said`).
                "last_said": run.get("last_said") or "",
            }
        )
    return out


def project_status(
    project,
    open_questions: int = 0,
    running: bool = False,
    gated: bool = False,
    paused_run: bool = False,
) -> tuple[str, str]:
    """The one line under a project's name in the rail, and its tone.

    A rail row has room for one fact, so this is a precedence order rather than
    a list, and it runs most-urgent first:

    1. an agent is working on it *now* - the only thing on the rail that is
       moving, and the reason he asked for the widget at all;
    2. `blocked_on` - something only he can do. A blocked project keeps its
       place on the Active shelf (his note of 2026-07-30), so on the rail the
       word `blocked` is the whole of what distinguishes it from a project
       genuinely being worked;
    3. an open question - the same waiting, but answerable from his phone;
    4. a build request he has not answered.

    Anything else returns "", and the caller falls back to how long the project
    has been sitting - which is the useful status for a review row, and reads as
    nothing at all on a row that is plainly being worked.

    The tone is a class name, not a color: the palette belongs to the theme.
    """
    if running:
        # A held run (app/midrun.py) is an agent doing nothing on purpose;
        # "working now" would be the one lie the rail exists not to tell.
        return ("run paused", "working") if paused_run else ("working now", "working")
    if str(db._row_get(project, "blocked_on", "") or "").strip():  # noqa: SLF001
        return "blocked", "blocked"
    if open_questions:
        n = int(open_questions)
        return f"{n} question{'s' if n != 1 else ''}", "asking"
    if gated:
        return "needs your OK", "gate"
    return "", ""


def build(
    projects: Iterable,
    *,
    question_counts: Optional[Mapping[int, int]] = None,
    running_ids: Optional[set[int]] = None,
    paused_ids: Optional[set[int]] = None,
    gated_ids: Optional[set[int]] = None,
    activity: Optional[Mapping[int, str]] = None,
    runs: Sequence[dict] = (),
    usage: Sequence[dict] = (),
    path: str = "",
    mode: str = "recent",
) -> dict:
    """Everything base.html needs to draw the rail, in one pass over `projects`.

    One pass and one call per render, because this runs on *every* page of the
    portal rather than only the dashboard: a rail that cost a query per project
    would put that cost on the settings page and the run console too.

    `projects` are already scoped to the reader by the caller. Shelving goes
    through `db.shelf_of`, which is the same function the dashboard uses, so a
    project can never be Active on the rail and Paused on the board.

    `mode` picks what the list is, and it is a setting because Wes asked for
    both (2026-08-01):

      "recent" - one list, most recently worked on first, whatever shelf it is
                 on. This is what he asked to have by default: the rail is a way
                 back to what you were just doing, and what you were just doing
                 is not sorted by status.
      "shelf"  - the original: Active, then In review, each under its own
                 heading. "have a section in settings to change this from recent
                 back to kind of what we have now which is just based on status."

    Either way a row is a row, they are numbered in display order, and the first
    ten carry the digit that jumps to them.
    """
    question_counts = question_counts or {}
    running_ids = running_ids or set()
    gated_ids = gated_ids or set()
    activity = activity or {}
    here = current_slug(path)

    buckets: dict[str, list[dict]] = {name: [] for name, _ in RAIL_SHELVES}
    recent: list[dict] = []
    extras: list[dict] = []
    for project in projects:
        stage = str(db._row_get(project, "stage", "backlog"))  # noqa: SLF001
        if stage in config.DONE_STAGES:
            continue
        pid = int(project["id"])
        running = pid in running_ids
        shelf = db.shelf_of(project, question_counts.get(pid, 0), running)
        gated = pid in gated_ids
        status, tone = project_status(
            project, question_counts.get(pid, 0), running, gated,
            paused_run=pid in (paused_ids or set()),
        )
        # Last, so nothing waiting on a person is displaced by it: a paused
        # project with an open question still says so, because that question is
        # answerable from a phone whatever the project's pause says. It matters
        # in recent mode, where a parked project sits in the same list as the
        # work in flight and the row has to explain why it is dimmed.
        if not status and shelf == "paused":
            status, tone = "paused", "muted"
        slug = project["slug"]
        row = {
            "id": pid,
            "slug": slug,
            "title": project["title"],
            "href": f"/project/{slug}",
            "running": running,
            "status": status,
            "tone": tone,
            # The newest of "something happened to this project" and "this
            # project's row was written" - shared with the dashboard rather
            # than spelled out twice, so the rail and the board cannot come to
            # rank the same projects differently. See `db.worked_on_at`.
            "since": db.worked_on_at(project, activity),
            "current": bool(here) and slug == here,
            "shelf": shelf,
            # Not on a working shelf, so the row is drawn dimmed wherever it
            # ends up. In shelf mode that is the "More" group, which is dimmed
            # as a group; in recent mode the row sits in the one list among
            # projects that ARE being worked, and has to say so for itself.
            "dim": shelf not in buckets,
        }
        # `recent` is everything the rail will ever show, in one list; the
        # buckets are the shelf mode's grouping of the same rows, and `extras`
        # its tail. Done and abandoned stay off the rail entirely either way;
        # the dashboard remains the whole board.
        recent.append(row)
        if shelf not in buckets:
            extras.append(row)
            continue
        buckets[shelf].append(row)

    if mode == "recent":
        # Sorted here rather than trusting the caller's order: the caller may
        # hand over any of the dashboard's orders, and two of them (newest
        # first, title a-z) are not this question at all. `since` is the answer
        # to it - the newest real event on the project, not the last time its
        # row happened to be written; see `db.last_activity_at` for the day the
        # difference was obvious. Ties break on id descending, so
        # two projects touched in the same second come back in a stable order
        # rather than in whatever order SQLite felt like - the same rule
        # config.PROJECT_SORTS follows.
        #
        # ONE list, and status decides nothing about where a row lands. Wes,
        # 2026-08-15: "I also want the projects shown along the side to be
        # sorted by what was most recently run or modified." Until then a
        # paused or backlogged project was pushed behind every active one, so
        # the project he had been working on an hour earlier (ProxyTable, on a
        # pause) sat ten rows down under "More" below projects last touched a
        # week ago. Recent means recent; the shelf mode beside it is where
        # grouping by status lives, and it still groups.
        recent.sort(key=lambda r: (str(r["since"] or ""), r["id"]), reverse=True)
        shelves = (
            [{"name": "recent", "label": "Recent", "rows": recent[:RAIL_MAX_ROWS]}]
            if recent
            else []
        )
    else:
        # Empty shelves are dropped rather than drawn with a heading and nothing
        # under them: "hide what is empty, show what is live". A rail with no
        # sections at all renders no rail, which is the right answer on a fresh
        # install where the whole thing would be three headings and a zero.
        # `rows`, not `items`. Jinja resolves a dotted name to the attribute
        # first, and every dict has an `.items` method - so `shelf.items` in a
        # template silently yields a bound method instead of this list, and
        # `|length` on it raises from inside base.html on every page in the
        # portal. Naming the key something no dict already answers to is the fix
        # that cannot be forgotten.
        shelves = [
            {"name": name, "label": label, "rows": buckets[name][:RAIL_MAX_ROWS]}
            for name, label in RAIL_SHELVES
            if buckets[name]
        ]

    # The "More" tail, in shelf mode only: what is left when the working
    # shelves run out, most recently touched first, inside the same overall row
    # budget so a long backlog cannot crowd the markup. Recency order because
    # the group exists to answer "where did X go", and the X you ask about is
    # the one you touched last.
    #
    # Recent mode has no tail at all - the same projects are already in its one
    # list, in date order, and appending them again would list each of them
    # twice.
    if extras and mode != "recent":
        extras.sort(key=lambda r: (str(r["since"] or ""), r["id"]), reverse=True)
        room = RAIL_MAX_ROWS - sum(len(s["rows"]) for s in shelves)
        if room > 0:
            shelves.append({"name": "more", "label": "More", "rows": extras[:room]})

    # The digit that jumps to a row, assigned across the whole visible list in
    # display order so it matches what a person counts down the rail. Written
    # onto the row rather than derived in the template, because in shelf mode
    # the numbering runs across two sections and a per-loop index would restart
    # at 1 under the second heading.
    listed = 0
    for shelf in shelves:
        for row in shelf["rows"]:
            row["digit"] = RAIL_DIGITS[listed] if listed < len(RAIL_DIGITS) else ""
            listed += 1

    return {
        "runs": list(runs),
        "running": len(runs),
        # The account's Claude windows (app/limits.compact_windows), passed
        # through rather than read here: this module is a pure function of its
        # arguments, which is what makes the whole rail testable with no
        # database and no network behind it.
        "usage": list(usage),
        "shelves": shelves,
        "listed": listed,
        "current": here,
        "mode": mode,
        "any": bool(shelves or runs),
    }


def empty() -> dict:
    """A rail with nothing in it, for the failure path.

    base.html calls this global on every page in the portal, including the ones
    that render while something is already wrong. A rail is chrome: it may go
    missing, and it may never be the reason a page 500s.
    """
    return {
        "runs": [],
        "running": 0,
        "usage": [],
        "shelves": [],
        "listed": 0,
        "current": "",
        "mode": "recent",
        "any": False,
    }

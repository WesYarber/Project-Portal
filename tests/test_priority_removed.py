"""Priority is gone, and the board ranks by what was worked on last.

Wes, 2026-08-16: "Get rid of the notion of project 'priority' values. Instead,
within project statuses on the dashboard, I want to sort by most recently
modified similar to how the left nav bar is done."

The history, because it explains why the removal is this wide. Priority shipped
as an integer on the project row and became the FIRST key in every ORDER BY the
dashboard, the scheduler, the sub-project list and the approval queue used. On
2026-07-27 he said he did not think he used it; the answer then was a settings
switch (`show_priority`), because he HAD used it on two of thirty projects and
turning it off silently would have changed which project ran next. Twenty days
later he has decided. So the number, the switch, the sort option, the prompt
line, the Telegram listing and the column itself all come out together - a
half-removal that left the column in the schema would be the `pronouns`
situation over again, with a cleanup item aging on the todo list.

What replaces it is not "nothing". Two different questions, two answers:

- **The dashboard** ranks by most recently worked on, inside each status shelf.
  That is `db.worked_on_at` - the newest of a run, a note, a journal entry and
  the project row's own `updated_at` - which is the same key the side rail has
  used since 2026-08-07 and the reason his note says "similar to how the left
  nav bar is done". `projects.updated_at` alone is NOT that question: it moves
  only when the project's own row is written, so a project an agent ran on an
  hour ago can carry a week-old stamp.
- **The scheduler** takes least recently touched first, which was already the
  tiebreak under priority and is a fairness round robin rather than a ranking.

The tests below are in that order: the column is gone, the sorting is right,
and nothing on any page still shows a number.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db, settings_form, sidebar, subprojects

APP = Path(config.APP_ROOT) / "app"
TEMPLATES = APP / "templates"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _shelf(client, zone: str = "active") -> str:
    """Just one shelf's grid out of the dashboard.

    Comparing positions in the whole page is a trap: the idle-reason line above
    the shelves names a project, and the recent-activity feed below them names
    every project with a journal entry - so an ordering assertion against the
    raw HTML can pass or fail on markup that has nothing to do with the order
    of the cards.
    """
    html = client.get("/").text
    start = html.index(f'data-status-zone="{zone}"')
    # The last shelf on the page has no zone after it, and an `.index` that
    # raises here would read as the shelf being missing rather than as this
    # helper walking off the end of the document.
    nxt = html.find('data-status-zone="', start + 1)
    return html[start:nxt if nxt != -1 else len(html)]


def _stamp(slug: str, ts: str) -> None:
    """Set a project's `updated_at` directly.

    `db.update_project` stamps `now()`, so there is no way to write an old
    timestamp through the API - and every ordering test here needs two rows
    whose relative age it controls rather than guesses at from creation order.
    """
    conn = db.get_conn()
    conn.execute("UPDATE projects SET updated_at = ? WHERE slug = ?", (ts, slug))
    conn.commit()


# --------------------------------------------------------------------------
# The column, the setting and the sort option are gone
# --------------------------------------------------------------------------

def test_the_column_is_off_a_fresh_database(temp_data_dir):
    cols = {
        row["name"]
        for row in db.get_conn().execute("PRAGMA table_info(projects)")
    }
    assert "priority" not in cols
    # And the CREATE TABLE does not declare it either. The PRAGMA above cannot
    # tell that apart from "declared, then dropped by the migration a
    # millisecond later", which is what a fresh install would actually do if
    # the column were still in SCHEMA - working, and wrong.
    create = db.SCHEMA.split("CREATE TABLE IF NOT EXISTS projects")[1]
    assert "priority" not in create.split(");")[0]


def test_the_column_is_dropped_from_an_existing_database(temp_data_dir):
    """The migration, run against a database that still has the column.

    `_drop_priority` is idempotent by inspecting the table rather than by a
    settings flag, so re-adding the column and calling init_db again is a fair
    test of the real path - which is what every install of this portal did on
    the restart after this commit.
    """
    conn = db.get_conn()
    conn.execute("ALTER TABLE projects ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    db.create_project("Fridge Board", stage="active", slug="fridge")
    conn.execute("UPDATE projects SET priority = 6 WHERE slug = 'fridge'")
    conn.commit()

    db.init_db()

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    assert "priority" not in cols
    # The project itself survives the drop - this is a column migration, not a
    # rebuild, and losing a row to it would be the worst possible outcome.
    assert db.get_project_by_slug("fridge") is not None


def test_the_migration_clears_the_two_settings_rows(temp_data_dir):
    """His live install has `dashboard_sort=priority` stored and a
    `show_priority` row beside it. The first is a preference for a sort that no
    longer exists - safe, but taken as a fallback on every page load rather
    than as a preference - and the second is a row no code can explain."""
    conn = db.get_conn()
    conn.execute("ALTER TABLE projects ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    db.set_setting("dashboard_sort", "priority")
    db.set_setting("show_priority", "1")

    db.init_db()

    assert db.get_setting("show_priority") is None
    assert db.get_setting("dashboard_sort") == config.DEFAULT_PROJECT_SORT


def test_the_migration_keeps_a_sort_he_actually_chose(temp_data_dir):
    """Only the dead value is rewritten. Someone reading their board by title
    picked that, and a migration that resets it to recency because it was
    nearby is a setting changed under them."""
    conn = db.get_conn()
    conn.execute("ALTER TABLE projects ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    db.set_setting("dashboard_sort", "title")

    db.init_db()

    assert db.get_setting("dashboard_sort") == "title"


def test_dropping_it_twice_is_not_an_error(temp_data_dir):
    """Every startup calls this. Only the first one has anything to do."""
    db.create_project("Fridge Board", stage="active", slug="fridge")
    db.init_db()
    db.init_db()
    cols = {row["name"] for row in db.get_conn().execute("PRAGMA table_info(projects)")}
    assert "priority" not in cols
    assert db.get_project_by_slug("fridge") is not None


def test_nothing_in_the_app_still_reads_the_column(temp_data_dir):
    """A grep, because a leftover `project["priority"]` on a page nobody opened
    in a test would raise only when Wes opened it.

    The migration is allowed to name the column - it is the code whose whole
    job is to take it away - so it is the one file excluded.
    """
    migration = db._drop_priority.__code__.co_firstlineno  # noqa: SLF001
    offenders = []
    for path in sorted(APP.rglob("*.py")) + sorted(TEMPLATES.rglob("*.html")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if path.name == "db.py" and migration <= n <= migration + 40:
                continue
            if any(
                token in line
                for token in ('["priority"]', "['priority']", "p.priority", "c.priority")
            ):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == [], offenders


def test_the_settings_page_has_no_priority_switch(client):
    body = client.get("/settings").text
    assert "show_priority" not in body
    assert "Show project priority" not in body
    assert "show_priority" not in config.DEFAULT_SETTINGS
    # And the form no longer declares a field the page cannot submit: a
    # declared-but-absent field is written as its unchecked value on every
    # save, which is how a dead checkbox goes on writing a settings row.
    assert settings_form.apply({}, declared="show_priority") == {}


def test_the_sort_menu_has_no_priority_option(client):
    assert "priority" not in config.PROJECT_SORTS
    assert config.DEFAULT_PROJECT_SORT == "recent"
    assert "priority, then recent" not in client.get("/").text


def test_the_seeded_dashboard_sort_is_a_sort_that_exists(temp_data_dir):
    """`dashboard_sort` is seeded from DEFAULT_SETTINGS, and it named
    `priority` until today. A seeded value that is not a key of PROJECT_SORTS
    means every fresh install starts on the fallback path rather than on the
    order it says it starts on - which works, and is therefore the kind of
    wrong that survives for months."""
    assert config.DEFAULT_SETTINGS["dashboard_sort"] in config.PROJECT_SORTS
    assert db.get_setting("dashboard_sort") in config.PROJECT_SORTS


def test_a_stored_preference_for_the_gone_sort_falls_back(client):
    """Every install of this portal has `dashboard_sort=priority` stored, since
    that was the seeded default until today. It must not reach SQLite, and it
    must not leave the dashboard ranking by nothing."""
    db.set_setting("dashboard_sort", "priority")
    db.create_project("Older", stage="active", slug="older")
    db.create_project("Newer", stage="active", slug="newer")
    _stamp("older", "2020-01-01T00:00:00+00:00")
    _stamp("newer", "2030-01-01T00:00:00+00:00")

    body = client.get("/").text
    shelf = _shelf(client)
    assert shelf.index("Newer") < shelf.index("Older")
    # ...and the sort row SAYS so. The board falling back while `active_sort`
    # still reads "priority" leaves no link marked current, so the page is in
    # recency order and claims to be in none - which is the silent-behavior
    # failure this project keeps refusing to ship, in miniature.
    assert '<a class="sort-link active" href="/?sort=recent">' in body


def test_an_unknown_sort_name_still_never_reaches_sqlite(temp_data_dir):
    db.create_project("Anything", stage="active", slug="anything")
    assert db.list_projects_sorted("priority") is not None
    assert db.list_projects_sorted("'; drop table projects; --") is not None
    assert db.list_projects_sorted(None) is not None
    assert len(db.list_projects()) >= 1


def test_creating_a_project_rejects_a_priority(temp_data_dir):
    """Loudly, rather than by ignoring it. A caller still passing the argument
    is a caller that believes it is setting something."""
    with pytest.raises(TypeError):
        db.create_project("Fridge Board", slug="fridge", priority=9)


def test_a_child_no_longer_steps_down_from_its_parent(temp_data_dir):
    """The step-down existed so a family could not crowd the run rotation. The
    rotation is a least-recently-touched round robin now, which spreads a
    family across the board without a number to maintain."""
    parent = db.create_project("Board Games", stage="active", slug="games")
    child = subprojects.create_child(parent, "Catan")
    assert "priority" not in child.keys()


# --------------------------------------------------------------------------
# What the dashboard sorts by now
# --------------------------------------------------------------------------

def _two_active(temp_ok=None):
    """One project whose ROW is fresh, one that was actually worked on later."""
    quiet = db.create_project("Quiet Row", stage="active", slug="quiet")
    worked = db.create_project("Worked On", stage="active", slug="worked")
    # The row of the one nobody has worked on is the newer of the two, so any
    # ordering that reads `updated_at` alone puts it first. That is the wrong
    # answer, and the whole point of the pair.
    #
    # BOTH stamps are in the past, deliberately. A future stamp would beat any
    # activity the test can create - `add_journal` writes `now()` - so a
    # fixture dated 2030 makes the activity half unprovable while looking like
    # it is testing exactly that.
    _stamp("worked", "2020-01-01T00:00:00+00:00")
    _stamp("quiet", "2021-01-01T00:00:00+00:00")
    return quiet, worked


def test_the_dashboard_ranks_by_what_was_worked_on_not_by_the_row(client):
    quiet, worked = _two_active()
    # A journal entry is a real event: a note, an agent report, a status change
    # all land there, and none of them writes the project's own row.
    db.add_journal(worked["id"], "agent", "progress", "shipped something")

    shelf = _shelf(client)
    assert shelf.index("Worked On") < shelf.index("Quiet Row")


def test_the_row_still_decides_when_nothing_has_happened(client):
    """The control case. With no activity on either, `updated_at` is the only
    answer there is - so the pair above must not be passing because the code
    ignores the row entirely."""
    _two_active()
    shelf = _shelf(client)
    assert shelf.index("Quiet Row") < shelf.index("Worked On")


def test_the_card_shows_the_time_it_was_ranked_on(client):
    """A card reading "5 days ago" at the top of a shelf above one reading "2
    hours ago" reads as the sort being broken. The label and the order have to
    answer the same question."""
    _, worked = _two_active()
    db.add_journal(worked["id"], "agent", "progress", "shipped something")

    shelf = _shelf(client)
    cell = shelf[shelf.index("Worked On"):]
    cell = cell[:cell.index("</a>")]
    # Its row stamp is from 2020; its activity is from a moment ago. Asserted
    # as the presence of "just now" rather than the ABSENCE of some older
    # phrasing: `timeago` caps at "{n}d ago" and never says "years", so
    # `"years ago" not in cell` - the first cut of this - was true of every
    # possible rendering and held nothing in place. Two mutations walked
    # straight through it.
    assert "just now" in cell, cell
    assert "d ago" not in cell, cell


def test_every_status_shelf_is_ranked_the_same_way(client):
    """His words are "within project statuses" - so the backlog shelf sorts by
    recency too, not only the active one. One sorted list feeding all four
    shelves is what makes that true by construction."""
    db.create_project("Old Idea", stage="backlog", slug="old-idea")
    fresh = db.create_project("Fresh Idea", stage="backlog", slug="fresh-idea")
    _stamp("fresh-idea", "2020-01-01T00:00:00+00:00")
    _stamp("old-idea", "2021-01-01T00:00:00+00:00")
    db.add_journal(fresh["id"], "user", "note", "thought of something")

    shelf = _shelf(client, "backlog")
    assert shelf.index("Fresh Idea") < shelf.index("Old Idea")


def test_the_ordering_is_stable_when_two_share_a_timestamp(temp_data_dir):
    """Timestamps here have whole-second resolution, so a board touched in one
    tick would otherwise reshuffle under every live refresh - which is Wes's
    "nothing moves that I didn't move"."""
    a = db.create_project("A", stage="active", slug="a")
    b = db.create_project("B", stage="active", slug="b")
    same = "2026-08-16T12:00:00+00:00"
    _stamp("a", same)
    _stamp("b", same)
    activity = {a["id"]: same, b["id"]: same}

    order = [p["slug"] for p in db.by_recency(db.list_projects(), activity)]
    assert order == ["b", "a"]  # id descending
    assert order == [p["slug"] for p in db.by_recency(db.list_projects(), activity)]


def test_the_rail_and_the_board_ask_the_same_question(temp_data_dir):
    """One definition, because "similar to how the left nav bar is done" is a
    request for them to agree. If the rail kept its own copy of the key, the
    two could drift and only one of them would be wrong on screen."""
    project = db.create_project("Fridge Board", stage="active", slug="fridge")
    _stamp("fridge", "2020-01-01T00:00:00+00:00")
    row = db.get_project_by_slug("fridge")
    activity = {project["id"]: "2026-08-16T12:00:00+00:00"}

    rail = sidebar.build([row], activity=activity, path="/")
    since = rail["shelves"][0]["rows"][0]["since"]
    assert since == db.worked_on_at(row, activity)
    assert since == "2026-08-16T12:00:00+00:00"


def test_the_rails_shelf_mode_is_ranked_by_recency_too(client):
    """The rail has two modes. "recent" sorts for itself; "shelf" groups by
    status and keeps whatever order its caller handed over - so the ranking
    inside a shelf-mode group is decided entirely by `_side_rail` passing the
    activity map down. Nothing owned that call, which is exactly the shape
    docs/verifying-with-mutations.md §4 warns about: the pure function is easy
    to test and the call site is easy to forget.
    """
    db.set_setting("ui_rail_projects", "shelf")
    quiet, worked = _two_active()
    db.add_journal(worked["id"], "agent", "progress", "shipped something")

    # Through the real page, not by calling `sidebar.build` with hand-picked
    # arguments: the whole point is the argument `_side_rail` passes, and a
    # test that supplies it itself proves nothing about the call site.
    from app import main

    client.get("/")  # sets the acting-person ContextVar the rail reads
    shelves = main.side_rail("/")["shelves"]
    rows = [row["slug"] for shelf in shelves for row in shelf["rows"]]
    assert rows.index("worked") < rows.index("quiet")


def test_a_project_with_no_activity_still_sorts_somewhere(temp_data_dir):
    """`last_activity_at` has no row for a project that has never been run and
    never been journalled. Falling to the empty string would sort it below
    everything forever; the row's own stamp is the answer."""
    project = db.create_project("Fridge Board", stage="active", slug="fridge")
    assert db.worked_on_at(db.get_project(project["id"]), {}) == project["updated_at"]


def test_no_activity_map_leaves_the_base_order(temp_data_dir):
    """`list_projects_sorted` is called without one from tests and from any
    caller that has no map to hand. It must degrade to `updated_at DESC` - a
    near-miss - rather than raise or return the rows unordered."""
    db.create_project("Older", stage="active", slug="older")
    db.create_project("Newer", stage="active", slug="newer")
    _stamp("older", "2020-01-01T00:00:00+00:00")
    _stamp("newer", "2030-01-01T00:00:00+00:00")
    assert [p["slug"] for p in db.list_projects_sorted("recent")] == ["newer", "older"]


# --------------------------------------------------------------------------
# What the scheduler does, which is a different question
# --------------------------------------------------------------------------

def test_the_scheduler_takes_least_recently_touched_first(temp_data_dir):
    """A fairness queue, not a ranking: whatever has waited longest goes next.

    Deliberately the OPPOSITE order from the dashboard's, and that is not a
    bug. The board answers "what have I been working on"; the queue answers
    "what is owed a turn".
    """
    db.create_project("Stale", stage="active", slug="stale")
    db.create_project("Fresh", stage="active", slug="fresh")
    _stamp("stale", "2020-01-01T00:00:00+00:00")
    _stamp("fresh", "2030-01-01T00:00:00+00:00")
    assert [p["slug"] for p in db.list_schedulable_projects()] == ["stale", "fresh"]


def test_the_sub_project_list_is_a_queue(temp_data_dir):
    """The parent page lists its children in the order they will actually be
    worked - the same least-recently-touched rule the scheduler uses. It used
    to be priority first, then this; taking priority out left the tiebreak as
    the whole rule, and nothing owned it."""
    parent = db.create_project("Board Games", stage="active", slug="games")
    db.create_project("Tak", stage="backlog", slug="games-tak", parent_id=parent["id"])
    db.create_project("Catan", stage="backlog", slug="games-catan", parent_id=parent["id"])
    _stamp("games-tak", "2020-01-01T00:00:00+00:00")
    _stamp("games-catan", "2021-01-01T00:00:00+00:00")
    order = [c["slug"] for c in db.child_projects(parent["id"])]
    assert order == ["games-tak", "games-catan"]


def test_no_project_listing_deadlocks(temp_data_dir):
    """Kept from the toggle this replaces, because the trap it guards is still
    live. `project_order()` read a setting, and `get_setting` takes `db._LOCK`
    - a plain Lock, not an RLock - so interpolating the call INSIDE a
    `with _LOCK:` block made the same thread take the lock twice and hang the
    dashboard, the worker's project pick and the sub-project list.

    The setting read is gone with priority, but the next person to want a
    settings-dependent ORDER BY will reach for exactly that shape. A watchdog
    rather than a plain call, so a reintroduction fails in ten seconds instead
    of hanging the whole suite the way it did the first time.
    """
    import threading

    db.create_project("Anything", stage="active", slug="anything")
    listings = {
        "list_projects": lambda: db.list_projects(),
        "list_projects_by_stage": lambda: db.list_projects_by_stage(["active"]),
        "list_schedulable_projects": db.list_schedulable_projects,
        "child_projects": lambda: db.child_projects(1),
        "projects_awaiting_build_approval": db.projects_awaiting_build_approval,
        "list_projects_sorted": lambda: db.list_projects_sorted(None),
    }
    for name, call in listings.items():
        done = threading.Event()

        def run(call=call, done=done):
            call()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        assert done.wait(10), f"{name} deadlocked"


# --------------------------------------------------------------------------
# What the pages show
# --------------------------------------------------------------------------

def test_the_project_page_never_shows_a_picker(client):
    db.create_project("Fridge Board", stage="active", slug="fridge")
    body = client.get("/project/fridge").text
    assert 'name="priority"' not in body
    assert "control-priority" not in body


def test_the_dashboard_cells_carry_no_p_number(client):
    db.create_project("Fridge Board", stage="active", slug="fridge")
    body = client.get("/").text
    assert "&middot; p0" not in body
    assert "· p0" not in body


def test_the_sub_project_list_carries_no_p_number(client):
    parent = db.create_project("Board Games", stage="active", slug="games")
    db.create_project("Tak", stage="active", slug="games-tak", parent_id=parent["id"])
    body = client.get("/project/games").text
    assert "· p0" not in body
    assert "&middot; p0" not in body


def test_the_prompt_no_longer_states_a_priority(temp_data_dir):
    """An agent told "Priority: 0" every run spends attention on a number that
    means nothing and can act on it - the contract right above it is about
    what to work on next."""
    from app import agent_runner

    project = db.create_project("Fridge Board", stage="active", slug="fridge")
    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "- Priority:" not in prompt
    # The lines either side of where it used to sit are still there, so this
    # cannot pass because the whole project block stopped rendering.
    assert "- Slug: fridge" in prompt
    assert "- Build approval:" in prompt


# --------------------------------------------------------------------------
# `pause building` - unrelated, and inherited from the file this replaces
# --------------------------------------------------------------------------

def test_the_pause_building_button_is_gone(client):
    db.create_project("Fridge Board", stage="active", build_approved=True, slug="fridge")
    body = client.get("/project/fridge").text
    assert "pause building" not in body
    assert "revoke-build" not in body


def test_undoing_an_approval_still_works(client):
    # The route is the only implementation of "un-approve this", so it stays
    # reachable even though nothing on the page points at it now.
    project = db.create_project("Fridge Board", stage="active", build_approved=True, slug="fridge")
    assert db.get_project(project["id"])["build_approved"] == 1
    resp = client.post("/project/fridge/revoke-build", follow_redirects=False)
    assert resp.status_code == 303
    assert db.get_project(project["id"])["build_approved"] == 0

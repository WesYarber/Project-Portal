"""Priority becomes optional, and `pause building` comes off the page.

Wes, 2026-07-27: "Is the 'Pause building' button used for anything? I know I've
never used it, and I'm wondering why it is still there and if it should be
removed. Same for priority - I dont think I use it, and might want to remove it.
I think maybe it would be more useful in cases where someone has less tokens to
burn through and wants to keep less things running in parallel and less runs in
total. What do you think? At very least, maybe let's add in an option in
settings to hide the priority."

Two different answers, because the evidence on his own install differs:

- `pause building` had been pressed zero times. Its route writes a "Build
  approval withdrawn" journal entry every time it runs, and across 841 journal
  entries there were none. The button is gone; the route stays.
- priority he HAS used, on two of thirty projects, and it really does steer the
  scheduler. So it gets a switch rather than the axe.

The interesting half is what "hide" has to mean. Priority is the first key in
every ORDER BY the dashboard and the scheduler use, so hiding only the control
would leave a project sitting at 6 front-running the queue forever with nothing
on screen to explain it. Off therefore takes it out of the ordering too.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db, settings_form

TEMPLATES = Path(config.APP_ROOT) / "app" / "templates"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _hide_priority():
    db.set_setting("show_priority", "0")


# --------------------------------------------------------------------------
# First, because a deadlock here wedges every test below it
# --------------------------------------------------------------------------

def test_no_project_listing_deadlocks_on_the_settings_read(temp_data_dir):
    """The bug the first cut of this shipped, caught by its own tests hanging.

    `project_order()` reads a setting, and `get_setting` takes `db._LOCK` -
    which is a plain Lock, not an RLock. Interpolating the call into the
    f-string INSIDE each `with _LOCK:` block therefore made the same thread
    take the lock twice and hang forever: the dashboard, the worker's project
    pick and the sub-project list, all wedged.

    A watchdog rather than a plain call, so a reintroduction fails in a second
    instead of hanging the whole suite the way it did the first time.
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
        for hidden in (False, True):
            db.set_setting("show_priority", "0" if hidden else "1")
            done = threading.Event()

            def run(call=call, done=done):
                call()
                done.set()

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            assert done.wait(10), f"{name} deadlocked (show_priority hidden={hidden})"


# --------------------------------------------------------------------------
# The setting itself
# --------------------------------------------------------------------------

def test_priority_is_on_by_default(temp_data_dir):
    assert db.show_priority() is True
    assert config.DEFAULT_SETTINGS["show_priority"] == "1"


def test_the_setting_turns_it_off(temp_data_dir):
    _hide_priority()
    assert db.show_priority() is False


def test_an_unchecked_box_writes_zero(temp_data_dir):
    # A checkbox submits nothing at all when unchecked, so absence has to mean
    # "0" here rather than "leave it alone" - otherwise it could never be
    # turned off from the settings page.
    assert settings_form.apply({}, declared="show_priority") == {"show_priority": "0"}
    assert settings_form.apply({"show_priority": "1"}, declared="show_priority") == {
        "show_priority": "1"
    }


def test_the_settings_form_section_owns_the_field(temp_data_dir):
    # A field missing from its section's `_fields` is silently dropped on save,
    # which reads exactly like the setting refusing to stick.
    html = (TEMPLATES / "settings.html").read_text()
    assert "ui_density,show_priority" in html
    assert 'name="show_priority"' in html


# --------------------------------------------------------------------------
# The ordering, which is the half that could have gone wrong quietly
# --------------------------------------------------------------------------

def _two_projects():
    """A high-priority project touched long ago, and a plain one touched now."""
    stale = db.create_project("Stale But Important", stage="active", priority=9, slug="stale")
    fresh = db.create_project("Fresh And Plain", stage="active", priority=0, slug="fresh")
    conn = db.get_conn()
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?",
                 ("2020-01-01T00:00:00+00:00", stale["id"]))
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?",
                 ("2030-01-01T00:00:00+00:00", fresh["id"]))
    conn.commit()
    return stale, fresh


def test_priority_on_puts_the_high_number_first(temp_data_dir):
    _two_projects()
    assert [p["slug"] for p in db.list_schedulable_projects()] == ["stale", "fresh"]
    assert [p["slug"] for p in db.list_projects()] == ["stale", "fresh"]


def test_priority_off_falls_back_to_least_recently_touched(temp_data_dir):
    # This is the whole point. With the control hidden, a 9 nobody can see and
    # nobody can change must stop deciding which project the worker picks.
    _two_projects()
    _hide_priority()
    assert [p["slug"] for p in db.list_schedulable_projects()] == ["stale", "fresh"]
    # ...and that order is now the timestamps talking, not the 9: flip which
    # one was touched most recently and the order flips with it.
    conn = db.get_conn()
    conn.execute("UPDATE projects SET updated_at = ? WHERE slug = 'stale'",
                 ("2031-01-01T00:00:00+00:00",))
    conn.commit()
    assert [p["slug"] for p in db.list_schedulable_projects()] == ["fresh", "stale"]


def test_priority_on_ignores_the_same_timestamp_flip(temp_data_dir):
    # The control case for the test above: with priority ON, the 9 still wins
    # however the timestamps move. Without this pair, the test above would pass
    # just as happily against code that never sorted by priority at all.
    _two_projects()
    conn = db.get_conn()
    conn.execute("UPDATE projects SET updated_at = ? WHERE slug = 'stale'",
                 ("2031-01-01T00:00:00+00:00",))
    conn.commit()
    assert [p["slug"] for p in db.list_schedulable_projects()] == ["stale", "fresh"]


def test_every_project_listing_goes_through_one_definition(temp_data_dir):
    # Five listings used to carry their own copy of "priority DESC, ...", which
    # is five chances for one of them to keep ranking by a hidden number.
    src = (Path(config.APP_ROOT) / "app" / "db.py").read_text()
    hits = [ln for ln in src.splitlines() if "priority DESC" in ln]
    assert len(hits) == 1, hits
    assert "def project_order" in src


def test_the_sort_menu_loses_the_priority_option(temp_data_dir):
    assert "priority" in db.project_sorts()
    _hide_priority()
    assert "priority" not in db.project_sorts()
    # ...and the default sort moves off it rather than naming a missing key.
    assert db.default_project_sort() == "recent"


def test_a_stored_preference_for_a_gone_sort_does_not_break_the_dashboard(client):
    # He may well have `dashboard_sort=priority` stored from before the switch.
    db.set_setting("dashboard_sort", "priority")
    _hide_priority()
    db.create_project("Anything", stage="active", slug="anything")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "priority, then recent" not in resp.text


def test_an_unknown_sort_name_still_never_reaches_sqlite(temp_data_dir):
    _hide_priority()
    db.create_project("Anything", stage="active", slug="anything")
    # Including the name that IS a real key but is off the menu.
    assert db.list_projects_sorted("priority") is not None
    assert db.list_projects_sorted("'; drop table projects; --") is not None
    assert db.list_projects_sorted(None) is not None


# --------------------------------------------------------------------------
# What the pages show
# --------------------------------------------------------------------------

def test_the_project_page_shows_the_control_by_default(client):
    db.create_project("Fridge Board", stage="active", slug="fridge")
    body = client.get("/project/fridge").text
    assert 'name="priority"' in body


def test_hiding_it_takes_the_control_off_the_project_page(client):
    db.create_project("Fridge Board", stage="active", slug="fridge")
    _hide_priority()
    body = client.get("/project/fridge").text
    assert 'name="priority"' not in body
    assert "control-priority" not in body


def test_hiding_it_takes_the_number_off_the_dashboard_cells(client):
    db.create_project("Fridge Board", stage="active", priority=7, slug="fridge")
    assert "p7" in client.get("/").text
    _hide_priority()
    assert "p7" not in client.get("/").text


def test_hiding_it_takes_the_number_off_the_sub_project_list(client):
    parent = db.create_project("Board Games", stage="active", slug="games")
    db.create_project("Tak", stage="active", priority=4, slug="games-tak",
                      parent_id=parent["id"])
    assert "p4" in client.get("/project/games").text
    _hide_priority()
    assert "p4" not in client.get("/project/games").text


def test_the_settings_page_says_what_off_does_to_the_ordering(client):
    # A switch whose blank state silently changes which project runs next has
    # to say so on the page, not only in a commit message.
    body = client.get("/settings").text
    assert "Show project priority" in body
    assert "out of the ordering" in body


# --------------------------------------------------------------------------
# `pause building`
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

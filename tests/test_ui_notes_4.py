"""Wes's 2026-07-23 15:09 note, all three asks.

- The "N active / X/Y runs today" readout comes off the dashboard's top-right
  header (the status card lower down still reports the budget).
- The per-project runs/day cap control comes off the project page entirely -
  he never sets one - while the route and the worker's enforcement stay for a
  cap set elsewhere. REVERSED on 2026-08-13, when he asked for it back ("I
  don't see where I can increase daily limits on runs of single projects"):
  the on-page assertions live in tests/test_run_cap_control.py now, and only
  the route test is left here.
- The Add-note row gains a "queue & don't run" button and the submit trio is
  right-justified with the green "add note" on the far right.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from app import config, db, main, worker


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clean_worker_state():
    def reset():
        worker._inflight.clear()
        worker._wake = asyncio.Event()
        while not worker.manual_queue.empty():
            worker.manual_queue.get_nowait()

    reset()
    yield
    reset()


def project():
    return db.create_project("Fridge Board", slug="fridge", stage="active")


# --------------------------------------------------------------------------
# Dashboard header
# --------------------------------------------------------------------------


def test_dashboard_header_no_longer_counts_runs(client):
    project()
    html = client.get("/").text
    # The old override read "1 active &middot; 0/40 runs today"; gone means the
    # base template's default header shows instead.
    assert "active &middot;" not in html
    # The default header stat is this install's own user@host label.
    assert config.SITE.handle in html


def test_the_status_card_still_reports_the_budget(client):
    """Removing the header stat must not take the budget readout with it."""
    html = client.get("/").text
    assert "runs today:" in html


# --------------------------------------------------------------------------
# Project page: the runs/day cap route
#
# The "the control is gone from the page" pair that used to sit here was
# deleted on 2026-08-13 - he asked for the control back, so an assertion that
# it is absent is now an assertion against what he wants. What the page shows
# is pinned in tests/test_run_cap_control.py.
# --------------------------------------------------------------------------


def test_run_cap_route_still_enforceable_from_elsewhere(client):
    p = project()
    resp = client.post(f"/project/{p['slug']}/run-cap",
                       data={"max_runs_per_day": "3"}, follow_redirects=False)
    assert resp.status_code == 303
    assert db.get_project(p["id"])["max_runs_per_day"] == 3


# --------------------------------------------------------------------------
# The Add-note button row
# --------------------------------------------------------------------------


def test_note_buttons_in_wes_order(client):
    """Left to right: add & run now, queue note, add note - the green default on
    the far right, the group pushed right by .note-actions.

    Shown on a project that cannot be run now (backlog), which since 2026-08-10
    is the only case where "add & run now" is rendered at all."""
    p = db.create_project("Someday", slug="someday", stage="backlog")
    html = client.get(f"/project/{p['slug']}").text
    i_run = html.index('name="then" value="run"')
    i_queue = html.index('name="then" value="queue"')
    i_add = html.index(">add note</button>")
    assert i_run < i_queue < i_add
    assert "note-actions" in html


def test_the_queue_button_says_queue_note(client):
    """Wes, 2026-08-10: "Change the 'Queue and don't run' button to just say
    'queue note'"."""
    p = project()
    html = client.get(f"/project/{p['slug']}").text
    assert "queue note" in html
    # The old label, which was two words longer and said what it would not do.
    assert "queue &amp; don" not in html


def test_queue_and_dont_run_stores_the_note_quietly(client):
    p = project()
    client.post(f"/project/{p['slug']}/note",
                data={"note": "for later", "then": "queue"})
    bodies = [j["content_md"] for j in db.list_journal(p["id"])]
    assert any("for later" in b for b in bodies)
    assert worker.manual_queue.qsize() == 0


def test_queue_does_not_wake_a_parked_project(client):
    """The whole point of the third button: stacking thoughts on a reviewed or
    paused project without yanking it back to active."""
    p = db.create_project("Parked", slug="parked", stage="review")
    client.post("/project/parked/note", data={"note": "thought", "then": "queue"})
    row = db.get_project(p["id"])
    assert row["stage"] == "review"
    assert worker.manual_queue.qsize() == 0

    q = db.create_project("Napping", slug="napping", stage="active")
    db.pause_project(q["id"])
    client.post("/project/napping/note", data={"note": "thought", "then": "queue"})
    assert db.is_paused(db.get_project(q["id"]))
    assert worker.manual_queue.qsize() == 0


def test_plain_add_note_still_wakes_a_parked_project(client):
    p = db.create_project("Parked", slug="parked", stage="review")
    client.post("/project/parked/note", data={"note": "go"})
    assert db.get_project(p["id"])["stage"] == "active"
    assert worker.manual_queue.qsize() == 1

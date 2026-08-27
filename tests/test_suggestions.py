"""Suggestions: accepting one, undoing a dismissal, and dismissals timing out.

Wes, 2026-07-28:

    "When accepting a suggested project from memory, I get an internal server
    error. I also want to be able to undo where I've told it some projects that
    I dont want it to work on in the suggested section from the past. And I want
    past declined ones to time out after a week."

Three separate claims, and the first is a plain bug: `accept_suggestion` still
called `db.create_project(status=...)` after the 2026-07-22 state-model change
renamed that parameter to `stage`, so every accept raised TypeError. Nothing
called the route in a test, which is why it survived six days.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import config, db


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def _dismissed_at(suggestion_id: int, when: datetime) -> None:
    """Backdate a dismissal, which is the only way to test a week-long timeout
    without a week. `status_ts` is the field the expiry reads; `ts` is when the
    suggestion was made and must NOT be what the clock runs from."""
    conn = db.get_conn()
    conn.execute(
        "UPDATE suggestions SET status = 'dismissed', status_ts = ? WHERE id = ?",
        (when.isoformat(), suggestion_id),
    )
    conn.commit()


# --- accepting ------------------------------------------------------------

def test_accepting_a_suggestion_creates_a_backlog_project(client):
    s = db.add_suggestion("A dice roller", "Roll dice on the phone.")

    r = client.post(f"/suggestions/{s['id']}/accept", follow_redirects=False)

    assert r.status_code == 303
    project = db.get_project_by_slug(r.headers["location"].rsplit("/", 1)[-1])
    assert project is not None
    assert project["title"] == "A dice roller"
    assert project["description"] == "Roll dice on the phone."
    # A suggestion the portal thought of is still only an idea: it lands in the
    # backlog, unapproved for building, exactly like one Wes typed himself.
    assert project["stage"] == "backlog"
    assert not project["build_approved"]
    assert db.get_suggestion(s["id"])["status"] == "accepted"


def test_accepting_a_missing_suggestion_is_a_404_not_a_500(client):
    r = client.post("/suggestions/99999/accept", follow_redirects=False)
    assert r.status_code == 404


def test_an_accepted_suggestion_belongs_to_whoever_accepted_it(client):
    # Same rule as the idea form (Wes, 2026-08-06): a project someone creates
    # goes on their board, and pressing accept is creating it.
    from app import people

    karli = people.add(name="Karli", gender="female")
    s = db.add_suggestion("A dice roller", "Roll dice on the phone.")

    client.cookies.set(people.COOKIE, "karli")
    r = client.post(f"/suggestions/{s['id']}/accept", follow_redirects=False)

    project = db.get_project_by_slug(r.headers["location"].rsplit("/", 1)[-1])
    assert people.member_ids(project["id"]) == {karli}


# --- undoing a dismissal --------------------------------------------------

def test_a_dismissal_can_be_undone(client):
    s = db.add_suggestion("A dice roller")
    client.post(f"/suggestions/{s['id']}/dismiss", follow_redirects=False)
    assert db.get_suggestion(s["id"])["status"] == "dismissed"

    r = client.post(f"/suggestions/{s['id']}/restore", follow_redirects=False)

    assert r.status_code == 303
    assert db.get_suggestion(s["id"])["status"] == "proposed"


def test_the_memory_page_offers_undo_only_on_a_dismissed_one(client):
    kept = db.add_suggestion("Still proposed")
    tossed = db.add_suggestion("Dismissed one")
    db.set_suggestion_status(tossed["id"], "dismissed")

    html = client.get("/memory").text

    assert f"/suggestions/{tossed['id']}/restore" in html
    # The proposed one gets accept/dismiss, never an undo - there is nothing
    # to undo, and a button that does nothing is worse than no button.
    assert f"/suggestions/{kept['id']}/restore" not in html
    assert f"/suggestions/{kept['id']}/dismiss" in html


# --- the week-long timeout ------------------------------------------------

def test_a_dismissal_older_than_a_week_comes_back(client):
    s = db.add_suggestion("A dice roller")
    _dismissed_at(
        s["id"],
        datetime.now(timezone.utc) - timedelta(days=db.SUGGESTION_DISMISSAL_DAYS, hours=1),
    )

    assert db.expire_dismissed_suggestions() == 1
    assert db.get_suggestion(s["id"])["status"] == "proposed"


def test_a_fresh_dismissal_stays_dismissed(client):
    s = db.add_suggestion("A dice roller")
    _dismissed_at(
        s["id"],
        datetime.now(timezone.utc) - timedelta(days=db.SUGGESTION_DISMISSAL_DAYS, hours=-1),
    )

    assert db.expire_dismissed_suggestions() == 0
    assert db.get_suggestion(s["id"])["status"] == "dismissed"


def test_the_clock_runs_from_the_dismissal_not_from_the_suggestion(client):
    """A suggestion made a month ago and dismissed this morning is dismissed.

    This is the whole reason `status_ts` exists rather than the expiry reading
    `ts` - which would un-dismiss an old suggestion in the same breath as
    dismissing it, and the button would look broken.
    """
    s = db.add_suggestion("An old idea")
    conn = db.get_conn()
    conn.execute(
        "UPDATE suggestions SET ts = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(days=40)).isoformat(), s["id"]),
    )
    conn.commit()
    db.set_suggestion_status(s["id"], "dismissed")

    assert db.expire_dismissed_suggestions() == 0
    assert db.get_suggestion(s["id"])["status"] == "dismissed"


def test_accepting_is_permanent(client):
    """Only dismissals time out. An accepted suggestion already became a
    project; putting it back on the list would offer to create a second one."""
    s = db.add_suggestion("A dice roller")
    db.set_suggestion_status(s["id"], "accepted")
    conn = db.get_conn()
    conn.execute(
        "UPDATE suggestions SET status_ts = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(days=400)).isoformat(), s["id"]),
    )
    conn.commit()

    assert db.expire_dismissed_suggestions() == 0
    assert db.get_suggestion(s["id"])["status"] == "accepted"


def test_listing_expires_on_read(client):
    """No timer anywhere: the page reading the list is what ages the dismissals,
    so the page and the database can never disagree about a row's status."""
    s = db.add_suggestion("A dice roller")
    _dismissed_at(s["id"], datetime.now(timezone.utc) - timedelta(days=30))

    rows = {r["id"]: r["status"] for r in db.list_suggestions()}

    assert rows[s["id"]] == "proposed"


def test_an_upgrade_does_not_resurrect_every_old_dismissal():
    """A dismissal made before `status_ts` existed serves its week from the
    upgrade. Backdating it to `ts` instead would have put every no Wes ever
    gave back on the page at the moment he restarted the portal."""
    conn = db.get_conn()
    # A row as it looked before the column: dismissed, status_ts never written.
    conn.execute(
        "INSERT INTO suggestions (ts, title, description, status, status_ts) "
        "VALUES (?, 'Legacy', '', 'dismissed', '')",
        ((datetime.now(timezone.utc) - timedelta(days=200)).isoformat(),),
    )
    conn.commit()

    assert db.expire_dismissed_suggestions() == 0

    # ...and once init_db stamps it, the week runs from that stamp.
    db.init_db()
    row = conn.execute("SELECT * FROM suggestions WHERE title = 'Legacy'").fetchone()
    assert row["status"] == "dismissed"
    assert row["status_ts"] == ""  # already-migrated DB: the guard is `added`

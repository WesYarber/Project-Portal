"""The state-model redesign end to end: the migration from the old eight-value
status enum, the two-button idea form, notes that wake a project up, and the
build-where-unblocked scheduling rules. See docs/state-model.md.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, daycycle, db, main, worker


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clean_worker_state(temp_data_dir):
    db.set_setting("last_reflect_date", daycycle.current_day())

    def reset():
        worker._inflight.clear()
        while not worker.manual_queue.empty():
            worker.manual_queue.get_nowait()

    reset()
    yield
    reset()


# --- the migration ----------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'inbox',
    priority INTEGER NOT NULL DEFAULT 0,
    resume_status TEXT,
    paused_by_user TEXT,
    build_approved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id),
    ts TEXT NOT NULL,
    author TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_md TEXT NOT NULL
);
"""


def _old_project(conn, slug, status, paused_by_user=None, build_approved=0):
    conn.execute(
        "INSERT INTO projects (slug, title, status, paused_by_user, build_approved,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, '2026-01-01', '2026-01-01')",
        (slug, slug, status, paused_by_user, build_approved),
    )


def _migrated(tmp_path, monkeypatch, seed):
    old_db = tmp_path / "old.db"
    conn = sqlite3.connect(old_db)
    conn.executescript(OLD_SCHEMA)
    seed(conn)
    conn.commit()
    conn.close()

    db._CONN.close()  # noqa: SLF001
    monkeypatch.setattr(db, "_CONN", None, raising=False)
    monkeypatch.setattr(config, "DB_PATH", old_db)
    db.init_db()


def test_the_migration_maps_every_old_status(tmp_path, monkeypatch):
    def seed(conn):
        _old_project(conn, "was-inbox", "inbox")
        _old_project(conn, "was-planning", "planning")
        _old_project(conn, "was-building", "building", build_approved=1)
        _old_project(conn, "was-needs-input", "needs_input")
        _old_project(conn, "was-review", "review")
        _old_project(conn, "was-done", "done")
        _old_project(conn, "was-abandoned", "abandoned")

    _migrated(tmp_path, monkeypatch, seed)
    expect = {
        "was-inbox": "backlog",
        "was-planning": "active",
        "was-building": "active",
        "was-needs-input": "active",
        "was-review": "review",
        "was-done": "done",
        "was-abandoned": "abandoned",
    }
    for slug, stage in expect.items():
        row = db.get_project_by_slug(slug)
        assert row["stage"] == stage, slug
        assert not db.is_paused(row), slug
    # Approval survived untouched.
    assert db.get_project_by_slug("was-building")["build_approved"] == 1


def test_the_migration_splits_waiting_user_three_ways(tmp_path, monkeypatch):
    """The old value meant three different things; each lands in its own
    column. Wes's pause keeps its original timestamp, the build-gate park
    becomes a build request, and an agent block points at the journal."""
    def seed(conn):
        _old_project(conn, "wes-paused", "waiting_user", paused_by_user="2026-07-01T00:00:00+00:00")
        _old_project(conn, "gate-parked", "waiting_user")
        conn.execute(
            "INSERT INTO journal (project_id, ts, author, kind, content_md)"
            " VALUES (2, '2026-07-01', 'system', 'status',"
            " 'The agent says this is ready to build. Waiting for your OK.')"
        )
        _old_project(conn, "agent-blocked", "waiting_user")

    _migrated(tmp_path, monkeypatch, seed)

    paused = db.get_project_by_slug("wes-paused")
    assert paused["stage"] == "active"
    assert paused["paused"] == "2026-07-01T00:00:00+00:00"

    gated = db.get_project_by_slug("gate-parked")
    assert gated["stage"] == "active"
    assert gated["build_requested"] == 1
    assert not db.is_paused(gated)

    blocked = db.get_project_by_slug("agent-blocked")
    assert blocked["stage"] == "active"
    assert blocked["blocked_on"]
    assert blocked["build_requested"] == 0


def test_the_migration_keeps_the_old_column_as_a_hedge(tmp_path, monkeypatch):
    _migrated(tmp_path, monkeypatch, lambda conn: _old_project(conn, "p", "planning"))
    assert db.get_project_by_slug("p")["status_old"] == "planning"


def test_the_migration_runs_once(tmp_path, monkeypatch):
    _migrated(tmp_path, monkeypatch, lambda conn: _old_project(conn, "p", "inbox"))
    db.set_user_state(db.get_project_by_slug("p"), "active")
    db.init_db()  # a restart must not re-derive the stage from status_old
    assert db.get_project_by_slug("p")["stage"] == "active"


def test_a_fresh_database_never_has_the_old_column(temp_data_dir):
    cols = {row["name"] for row in db.get_conn().execute("PRAGMA table_info(projects)")}
    assert "status" not in cols
    assert "status_old" not in cols
    assert {"stage", "paused", "build_requested", "blocked_on"} <= cols


# --- the two idea buttons ---------------------------------------------------

def test_plain_add_parks_the_idea_in_the_backlog(client):
    client.post("/ideas", data={"idea": "a thing", "then": ""})
    row = db.list_projects()[0]
    assert row["stage"] == "backlog"
    assert worker.manual_queue.qsize() == 0  # no model, per the ask


def test_add_and_plan_activates_and_starts_an_agent(client):
    client.post("/ideas", data={"idea": "a thing", "then": "plan"})
    row = db.list_projects()[0]
    assert row["stage"] == "active"
    assert row["build_approved"] == 0  # planning is not a build approval
    assert worker.manual_queue.qsize() == 1


def test_the_form_offers_both_doors(client):
    html = client.get("/").text
    assert 'name="then" value=""' in html
    assert 'name="then" value="plan"' in html


# --- notes wake a project up ------------------------------------------------

def _note(client, slug, text="do the thing"):
    return client.post(f"/project/{slug}/note", data={"note": text})


def test_a_note_on_a_paused_project_reactivates_and_runs(client):
    p = db.create_project("Fridge", slug="fridge", stage="active")
    db.pause_project(p["id"])
    _note(client, "fridge")
    row = db.get_project(p["id"])
    assert row["stage"] == "active"
    assert not db.is_paused(row)
    assert worker.manual_queue.qsize() == 1
    journal = [j["content_md"] for j in db.list_journal(p["id"], limit=5)]
    assert any("moved back to **active**" in j for j in journal)


def test_a_note_on_a_review_project_reactivates_and_runs(client):
    p = db.create_project("Fridge", slug="fridge", stage="review")
    _note(client, "fridge")
    assert db.get_project(p["id"])["stage"] == "active"
    assert worker.manual_queue.qsize() == 1


def test_reactivation_is_not_an_approval(client):
    p = db.create_project("Fridge", slug="fridge", stage="review")
    _note(client, "fridge")
    assert db.get_project(p["id"])["build_approved"] == 0


def test_a_note_on_an_active_project_changes_nothing(client):
    p = db.create_project("Fridge", slug="fridge", stage="active")
    _note(client, "fridge")
    assert worker.manual_queue.qsize() == 0


def test_a_note_on_a_backlog_or_done_project_stays_parked(client):
    for stage, slug in (("backlog", "b"), ("done", "d"), ("abandoned", "a")):
        p = db.create_project(slug, slug=slug, stage=stage)
        _note(client, slug)
        assert db.get_project(p["id"])["stage"] == stage
    assert worker.manual_queue.qsize() == 0


def test_a_telegram_note_also_wakes_the_project(monkeypatch):
    from app import notify, telegram_bot

    p = db.create_project("Fridge", slug="fridge", stage="active")
    db.pause_project(p["id"])
    monkeypatch.setattr(notify, "send_telegram_text", _noop_async)
    monkeypatch.setattr(telegram_bot.notify, "send_telegram_text", _noop_async)
    asyncio.run(
        telegram_bot._dispatch_intent(
            {"intent": "note", "project_slug": "fridge", "text": "fix it", "confidence": 1.0},
            "fix it", "1",
        )
    )
    assert not db.is_paused(db.get_project(p["id"]))
    assert worker.manual_queue.qsize() == 1


# --- build where unblocked --------------------------------------------------

def test_a_blocked_project_with_open_todos_still_schedules(temp_data_dir):
    p = db.create_project("Fridge", slug="fridge", stage="active", build_approved=True)
    db.update_project(p["id"], blocked_on="a part on order")
    db.add_todo(p["id"], "the part that does not need the part", owner="agent")
    picked, _ = worker._pick_project(None)
    assert picked is not None and picked["id"] == p["id"]


def test_a_blocked_project_with_nothing_workable_is_skipped(temp_data_dir):
    p = db.create_project("Fridge", slug="fridge", stage="active", build_approved=True)
    db.update_project(p["id"], blocked_on="a part on order")
    assert worker._pick_project(None) == (None, False)


def test_open_questions_alone_do_not_stop_scheduling(temp_data_dir):
    """The contract says asking does not mean stopping - a project with an
    open question and workable todos keeps its place in the rotation."""
    p = db.create_project("Fridge", slug="fridge", stage="active", build_approved=True)
    db.create_question(p["id"], "Which color?")
    db.add_todo(p["id"], "everything except the color", owner="agent")
    picked, _ = worker._pick_project(None)
    assert picked is not None


def test_a_question_with_no_workable_todos_parks_the_rotation(temp_data_dir):
    p = db.create_project("Fridge", slug="fridge", stage="active", build_approved=True)
    db.create_question(p["id"], "Which color?")
    assert worker._pick_project(None) == (None, False)


def test_a_paused_project_is_never_scheduled(temp_data_dir):
    p = db.create_project("Fridge", slug="fridge", stage="active", build_approved=True)
    db.add_todo(p["id"], "work", owner="agent")
    db.pause_project(p["id"])
    assert worker._pick_project(None) == (None, False)


# --- the new report facts ---------------------------------------------------

def _apply(project, **report):
    base = {"summary": "s", "journal_entry_md": "j"}
    base.update(report)
    worker._apply_report(project, agent_runner.RunResult(ok=True, report=base))
    return db.get_project(project["id"])


@pytest.mark.anyio
async def test_the_new_shape_request_build(temp_data_dir):
    p = db.create_project("Fridge", slug="fridge", stage="active")
    after = _apply(db.get_project(p["id"]), request_build=True)
    assert after["build_requested"] == 1


@pytest.mark.anyio
async def test_the_new_shape_blocked_on(temp_data_dir):
    p = db.create_project("Fridge", slug="fridge", stage="active", build_approved=True)
    after = _apply(db.get_project(p["id"]), blocked_on="the tailscale ACL click")
    assert after["blocked_on"] == "the tailscale ACL click"
    # And the next run's prompt carries it, since reporting clears it.
    prompt = agent_runner.build_prompt("build", after)
    assert "the tailscale ACL click" in prompt


@pytest.mark.anyio
async def test_the_new_shape_new_stage_review(temp_data_dir):
    p = db.create_project("Fridge", slug="fridge", stage="active", build_approved=True)
    after = _apply(db.get_project(p["id"]), new_stage="review")
    assert after["stage"] == "review"


@pytest.mark.anyio
async def test_an_agent_cannot_invent_other_stage_moves(temp_data_dir):
    p = db.create_project("Fridge", slug="fridge", stage="active", build_approved=True)
    assert _apply(db.get_project(p["id"]), new_stage="done")["stage"] == "active"
    assert _apply(db.get_project(p["id"]), new_stage="abandoned")["stage"] == "active"


async def _noop_async(*args, **kwargs):
    return None

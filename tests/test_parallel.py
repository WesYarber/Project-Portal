"""Parallel per-project runs, and the idle reason shown when none are running.

The worker used to refuse to start anything while `is_run_running()` was true,
so one long run froze every project. Now the gate is a concurrency cap plus a
one-run-per-project rule: two agents in a single workspace would fight over the
same files and the same git checkout, which is the one thing that must stay
impossible no matter how high the cap goes.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from app import db, main, worker


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture
def projects():
    return [
        db.create_project("Alpha", stage="active", build_approved=True, slug="alpha"),
        db.create_project("Beta", stage="active", build_approved=True, slug="beta"),
        db.create_project("Gamma", stage="active", build_approved=True, slug="gamma"),
    ]


@pytest.fixture(autouse=True)
def _clean_worker_state():
    """`_inflight` and `manual_queue` are module state that outlives the
    per-test database, so a leftover from one test would otherwise occupy a
    slot - or queue a run - in the next."""
    def reset():
        worker._inflight.clear()
        while not worker.manual_queue.empty():
            worker.manual_queue.get_nowait()

    reset()
    yield
    reset()


@pytest.fixture
def spawned(monkeypatch):
    """Replace the actual Claude invocation with a run that never finishes, so
    a "running" row behaves like a real in-flight run without a subprocess."""
    started: list[tuple[str, str]] = []

    async def fake_execute(project, task, run_id, model):
        started.append((project["slug"], task))
        await asyncio.Event().wait()  # in flight until canceled

    monkeypatch.setattr(worker, "_execute_run", fake_execute)
    return started


# --- concurrency ----------------------------------------------------------

def test_active_runs_reports_every_run_in_flight(projects):
    a = db.create_run(projects[0]["id"], "build", "opus")
    b = db.create_run(projects[1]["id"], "build", "opus")
    rows = db.active_runs()
    assert {row["id"] for row in rows} == {a, b}
    assert db.count_running() == 2
    # active_run() still answers with one - the newest - for "stop it".
    assert db.active_run()["id"] == b


def test_running_projects_are_not_picked_again(projects):
    db.create_run(projects[0]["id"], "build", "opus")
    picked, _ = worker._pick_project(None)
    assert picked["slug"] == "beta"


def test_a_project_is_never_picked_twice_even_manually(projects):
    db.create_run(projects[0]["id"], "build", "opus")
    picked, _ = worker._pick_project(projects[0]["id"])
    # Falls through to another project rather than doubling up on alpha.
    assert picked["slug"] != "alpha"


async def tick() -> None:
    """One worker tick, plus a turn of the event loop. `spawn_run` uses
    `create_task`, so the run coroutine does not actually begin until the loop
    next gets control - the worker's own bookkeeping is in the DB and is
    correct immediately, but the test's record of what started is not."""
    await worker._tick()
    await asyncio.sleep(0)


@pytest.fixture
def pacing_open(monkeypatch):
    """Pretend the pacing interval has elapsed. Backdating `started_at` would
    do it too, but that also backdates the run out of *today*, which is the
    thing the budget tests below are measuring. Pacing itself is covered by its
    own tests further down."""
    monkeypatch.setattr(worker, "_seconds_until_scheduled", lambda: 0)


@pytest.mark.asyncio
async def test_a_second_run_starts_while_the_first_is_still_going(
    projects, spawned, monkeypatch
):
    """The whole point: one long run no longer freezes every other project."""
    db.set_setting("max_parallel_runs", "2")
    await tick()
    assert len(spawned) == 1

    monkeypatch.setattr(worker, "_seconds_until_scheduled", lambda: 0)
    await tick()
    assert [slug for slug, _ in spawned] == ["alpha", "beta"]
    assert db.count_running() == 2  # alpha is still going, not replaced


@pytest.mark.asyncio
async def test_the_cap_is_respected(projects, spawned, pacing_open):
    db.set_setting("max_parallel_runs", "2")
    for _ in range(4):
        await tick()
    assert len(spawned) == 2


@pytest.mark.asyncio
async def test_cap_of_one_restores_serial_behavior(projects, spawned, pacing_open):
    db.set_setting("max_parallel_runs", "1")
    for _ in range(3):
        await tick()
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_pacing_holds_a_second_scheduled_run_back(projects, spawned):
    """Free slots are not license to launch everything at once - the interval
    still spaces scheduled runs out."""
    db.set_setting("max_parallel_runs", "3")
    await tick()
    await tick()
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_parallel_runs_cannot_overspend_the_daily_budget(projects, spawned, pacing_open):
    db.set_setting("max_parallel_runs", "5")
    db.set_setting("max_runs_per_day", "2")
    for _ in range(5):
        await tick()
    assert len(spawned) == 2
    assert db.count_runs_today() == 2


@pytest.mark.asyncio
async def test_queued_manual_runs_all_start_in_one_tick(projects, spawned):
    """Manual requests bypass pacing, so several queued at once go together
    rather than one per minute."""
    db.set_setting("max_parallel_runs", "3")
    for p in projects:
        await worker.queue_manual_run(p["id"])
    await tick()
    assert sorted(slug for slug, _ in spawned) == ["alpha", "beta", "gamma"]


@pytest.mark.asyncio
async def test_two_manual_requests_for_one_project_do_not_double_up(projects, spawned):
    """`spawn_run` writes the run row before returning, so the second request
    in the same tick already sees the project as busy."""
    db.set_setting("max_parallel_runs", "3")
    await worker.queue_manual_run(projects[0]["id"])
    await worker.queue_manual_run(projects[0]["id"])
    await tick()
    assert [slug for slug, _ in spawned] == ["alpha"]
    assert db.count_running() == 1


@pytest.mark.asyncio
async def test_a_manual_run_on_a_busy_project_waits_rather_than_being_dropped(
    projects, spawned
):
    db.set_setting("max_parallel_runs", "3")
    db.create_run(projects[0]["id"], "build", "opus")  # alpha already busy
    await worker.queue_manual_run(projects[0]["id"])

    await tick()
    assert spawned == []  # nothing else started ahead of the queued request
    assert worker.manual_queue.qsize() == 1  # still pending, not lost

    db.finish_run(db.active_run()["id"], "ok")
    await tick()
    assert [slug for slug, _ in spawned] == ["alpha"]


@pytest.mark.asyncio
async def test_a_crashed_run_does_not_leave_a_running_row(projects, monkeypatch):
    async def boom(project, task, run_id=None, model=None):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(worker, "run_project_task", boom)
    run_id = worker.spawn_run(projects[0], "build")
    await worker._inflight[run_id]
    assert db.get_run(run_id)["status"] == "error"
    assert db.count_running() == 0


# --- pacing ---------------------------------------------------------------

def test_pacing_uses_the_last_start_while_a_run_is_in_flight(projects):
    """Otherwise a long run - which has no `ended_at` - leaves the gate open
    and the worker fills every free slot instantly."""
    db.set_setting("worker_interval_min", "10")
    db.create_run(projects[0]["id"], "build", "opus")
    assert worker._seconds_until_scheduled() > 0


def test_pacing_is_open_again_once_the_interval_passes(projects):
    db.set_setting("worker_interval_min", "10")
    run_id = db.create_run(projects[0]["id"], "build", "opus")
    conn = db.get_conn()
    conn.execute(
        "UPDATE runs SET started_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", run_id),
    )
    conn.commit()
    assert worker._seconds_until_scheduled() == 0


# --- idle reason ----------------------------------------------------------

def test_no_reason_while_something_is_running(projects):
    db.create_run(projects[0]["id"], "build", "opus")
    assert worker.idle_reason() == ""


def test_reason_when_the_worker_is_paused(projects):
    db.set_setting("worker_enabled", "0")
    assert "paused" in worker.idle_reason()


def test_reason_when_the_budget_is_spent(projects):
    db.set_setting("max_runs_per_day", "1")
    db.finish_run(db.create_run(projects[0]["id"], "build", "opus"), "ok")
    reason = worker.idle_reason()
    assert "budget is spent (1/1)" in reason
    assert "resets in" in reason


def test_reason_when_there_is_nothing_to_work_on(temp_data_dir):
    assert "active and unpaused" in worker.idle_reason()


def test_reason_when_every_project_is_capped(client, projects):
    for p in projects:
        client.post(f"/project/{p['slug']}/run-cap", data={"max_runs_per_day": "1"})
        db.finish_run(db.create_run(p["id"], "build", "opus"), "ok")
    assert "per-project daily cap" in worker.idle_reason()


def test_reason_when_backing_off(projects):
    from datetime import datetime, timedelta, timezone

    until = datetime.now(timezone.utc) + timedelta(minutes=42)
    db.set_setting("backoff_until", until.isoformat(timespec="seconds"))
    assert "backing off" in worker.idle_reason()


def test_reason_names_the_project_that_is_next_up(projects):
    reason = worker.idle_reason()
    assert "Alpha" in reason


def test_pacing_reason_names_the_wait_and_the_project(projects):
    db.set_setting("worker_interval_min", "60")
    db.finish_run(db.create_run(projects[0]["id"], "build", "opus"), "ok")
    reason = worker.idle_reason()
    assert reason.startswith("pacing the next run - Alpha starts in about")


# --- what the UI is told --------------------------------------------------

def test_dashboard_shows_the_idle_reason(client):
    body = client.get("/").text
    assert "no agent running" in body
    assert "active and unpaused" in body


def test_dashboard_lists_every_live_run(client, projects):
    db.create_run(projects[0]["id"], "build", "opus")
    db.create_run(projects[1]["id"], "plan", "opus")
    body = client.get("/").text
    assert "Alpha" in body and "Beta" in body
    assert body.count('class="live-run-row"') == 2


def test_api_active_run_carries_all_runs_and_no_reason(client, projects):
    a = db.create_run(projects[0]["id"], "build", "opus")
    b = db.create_run(projects[1]["id"], "build", "opus")
    data = client.get("/api/active-run").json()
    assert data["active"] is True
    assert {r["run_id"] for r in data["runs"]} == {a, b}
    assert data["run_ids"] == f"{min(a, b)},{max(a, b)}"
    assert data["idle_reason"] == ""


def test_api_active_run_explains_an_idle_portal(client):
    data = client.get("/api/active-run").json()
    assert data["active"] is False
    assert data["runs"] == []
    assert data["idle_reason"]


def test_a_project_page_reports_only_its_own_run(client, projects):
    """The newest run belongs to Beta; Alpha's page must still show Alpha's."""
    alpha_run = db.create_run(projects[0]["id"], "build", "opus")
    db.create_run(projects[1]["id"], "build", "opus")
    body = client.get("/project/alpha").text
    assert f"/run/{alpha_run}/cancel" in body
    assert "stop this run" in body


def test_a_project_page_is_idle_when_only_others_are_running(client, projects):
    db.create_run(projects[1]["id"], "build", "opus")
    body = client.get("/project/alpha").text
    assert "run agent now" in body  # not disabled by Beta's agent
    assert "stop this run" not in body


def test_parallel_runs_setting_round_trips(client):
    client.post("/settings", data={"_fields": "max_parallel_runs", "max_parallel_runs": "4"})
    assert db.max_parallel_runs() == 4


def test_parallel_runs_setting_is_clamped(client):
    client.post("/settings", data={"_fields": "max_parallel_runs", "max_parallel_runs": "0"})
    assert db.max_parallel_runs() == 2
    assert db.get_setting("max_parallel_runs") == "2"

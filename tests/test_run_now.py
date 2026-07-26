"""The "run agent now" button (Wes's 03:01 note): it starts the agent
immediately - the worker loop is woken out of its between-tick sleep rather
than waiting for the minute boundary - and it puts the project back on the
active shelf from whatever state it was in. It is deliberately NOT a build
approval: on a gated project the same button reads "run planning pass", and
running one must not open the gate.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from app import db, main, worker


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clean_worker_state():
    """`manual_queue` and `_wake` are module state that outlives the per-test
    database; a leftover would queue a run - or skip a sleep - in the next
    test."""
    def reset():
        worker._inflight.clear()
        # Recreated, not cleared: an asyncio.Event binds to the first loop
        # that awaits it, and pytest-asyncio gives every test a fresh loop.
        worker._wake = asyncio.Event()
        while not worker.manual_queue.empty():
            worker.manual_queue.get_nowait()

    reset()
    yield
    reset()


def journal_bodies(project_id: int) -> list[str]:
    return [row["content_md"] for row in db.list_journal(project_id)]


# --------------------------------------------------------------------------
# The state gesture: run now means active, from anywhere
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["backlog", "review", "done", "abandoned"])
def test_run_now_moves_any_stage_to_active_and_queues_the_run(client, stage):
    project = db.create_project("Thing", stage=stage, slug="thing")
    resp = client.post("/project/thing/run", follow_redirects=False)
    assert resp.status_code == 303

    fresh = db.get_project_by_slug("thing")
    assert fresh["stage"] == "active"
    assert not db.is_paused(fresh)
    assert worker.manual_queue.qsize() == 1
    assert any(
        f"`{stage}` -> `active` (run agent now)" in body
        for body in journal_bodies(project["id"])
    )


def test_run_now_lifts_a_pause(client):
    project = db.create_project("Thing", stage="active", slug="thing")
    db.pause_project(project["id"])
    client.post("/project/thing/run", follow_redirects=False)

    fresh = db.get_project_by_slug("thing")
    assert fresh["stage"] == "active"
    assert not db.is_paused(fresh)
    assert any(
        "`paused` -> `active` (run agent now)" in body
        for body in journal_bodies(project["id"])
    )


def test_run_now_on_an_active_project_queues_without_journal_noise(client):
    project = db.create_project("Thing", stage="active", slug="thing")
    client.post("/project/thing/run", follow_redirects=False)
    assert worker.manual_queue.qsize() == 1
    assert not any("run agent now" in body for body in journal_bodies(project["id"]))


def test_run_now_is_not_a_build_approval(client):
    """A gated project's run button reads "run planning pass"; pressing it must
    leave the gate exactly as it was - request still open, approval still off."""
    project = db.create_project("Thing", stage="review", slug="thing")
    db.update_project(project["id"], build_requested=1)
    client.post("/project/thing/run", follow_redirects=False)

    fresh = db.get_project_by_slug("thing")
    assert fresh["stage"] == "active"
    assert db.build_requested(fresh)
    assert not db.build_approved(fresh)


def test_telegram_run_is_the_same_gesture(client, monkeypatch):
    """"run X" from the phone activates the project too, with the door named
    in the journal line."""
    project = db.create_project("Thing", stage="review", slug="thing")
    asyncio.run(worker.run_now(project, via=" via Telegram"))

    fresh = db.get_project_by_slug("thing")
    assert fresh["stage"] == "active"
    assert any(
        "via Telegram: `review` -> `active` (run agent now)" in body
        for body in journal_bodies(project["id"])
    )
    assert worker.manual_queue.qsize() == 1


# --------------------------------------------------------------------------
# Immediacy: the request wakes the worker loop
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_manual_run_sets_the_wake_event():
    assert not worker._wake.is_set()
    await worker.queue_manual_run(123)
    assert worker._wake.is_set()


@pytest.mark.asyncio
async def test_a_manual_request_wakes_the_sleeping_loop(monkeypatch):
    """With the tick interval effectively infinite, the only way a second tick
    can happen is the wake - which is the whole point of the event."""
    ticks: list[int] = []

    async def fake_tick():
        ticks.append(1)

    monkeypatch.setattr(worker, "_tick", fake_tick)
    monkeypatch.setattr(worker, "LOOP_INTERVAL_SEC", 3600)
    task = asyncio.create_task(worker.worker_loop())
    try:
        await asyncio.sleep(0.05)
        assert len(ticks) == 1  # the immediate first tick, then asleep

        await worker.queue_manual_run(123)
        await asyncio.sleep(0.05)
        assert len(ticks) == 2  # woken by the request, not the hour timer
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_the_wake_is_cleared_so_the_loop_sleeps_again(monkeypatch):
    ticks: list[int] = []

    async def fake_tick():
        ticks.append(1)

    monkeypatch.setattr(worker, "_tick", fake_tick)
    monkeypatch.setattr(worker, "LOOP_INTERVAL_SEC", 3600)
    task = asyncio.create_task(worker.worker_loop())
    try:
        await worker.queue_manual_run(123)
        await asyncio.sleep(0.05)
        woken = len(ticks)
        await asyncio.sleep(0.1)
        assert len(ticks) == woken  # no busy-spin: one wake, one extra tick
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

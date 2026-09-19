"""The "run now" queue survives a restart (app/manualqueue.py).

It was an in-memory `asyncio.Queue`, and on 2026-09-19 that cost the board
nine and a half hours. The worker let manual runs through a pending
self-update restart *because* the queue would not survive it; a queued run
for a project whose only busy-ness was a run paused for the usage window
could never start, so the queue never emptied, so the restart never fired,
so `_tick` returned before `_resume_paused_runs` - which is what would have
freed the project. Ten projects sat paused at 0% of the allowance used.

The deadlock itself is pinned in tests/test_run_failures.py. These pin the
persistence that let the exception be deleted.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import db, manualqueue, worker


@pytest.fixture
def project():
    return db.create_project(
        "Metronome", description="A thing.", stage="active", build_approved=True, slug="metronome",
    )


@pytest.mark.asyncio
async def test_a_queued_id_is_written_to_the_database(project):
    q = manualqueue.ManualQueue()
    await q.put(project["id"])
    assert json.loads(db.get_setting(manualqueue.SETTING)) == [project["id"]]


@pytest.mark.asyncio
async def test_taking_one_off_writes_the_shorter_queue(project):
    other = db.create_project("Other", description="x", stage="active",
                              build_approved=True, slug="other")
    q = manualqueue.ManualQueue()
    await q.put(project["id"])
    await q.put(other["id"])
    assert await q.get() == project["id"]
    assert json.loads(db.get_setting(manualqueue.SETTING)) == [other["id"]]


@pytest.mark.asyncio
async def test_a_fresh_process_gets_the_queue_back(project):
    before = manualqueue.ManualQueue()
    await before.put(project["id"])

    after = manualqueue.ManualQueue()
    assert after.restore() == 1
    assert after.ids() == [project["id"]]


@pytest.mark.asyncio
async def test_restoring_drops_a_project_that_is_gone(project):
    q = manualqueue.ManualQueue()
    await q.put(project["id"])
    await q.put(99999)

    after = manualqueue.ManualQueue()
    assert after.restore() == 1
    assert after.ids() == [project["id"]]
    # And the pruned list is what the next process will read.
    assert json.loads(db.get_setting(manualqueue.SETTING)) == [project["id"]]


@pytest.mark.asyncio
async def test_a_run_queued_before_the_restore_lands_is_not_lost(project):
    """The window this closes: a "run now" posted in the moment between the
    process starting and the worker loop's restore. Without the restore-on-
    first-touch that press would persist a one-item queue over everything
    the previous process was holding, and the explicit restore would then
    read back its own overwrite."""
    other = db.create_project("Other", description="x", stage="active",
                              build_approved=True, slug="other")
    saved = manualqueue.ManualQueue()
    await saved.put(project["id"])

    after = manualqueue.ManualQueue()
    await after.put(other["id"])       # the press lands first...
    assert after.restore() == 0        # ...and the loop's restore is a no-op
    assert after.ids() == [project["id"], other["id"]]


@pytest.mark.asyncio
async def test_restoring_twice_does_not_double_the_queue(project):
    q = manualqueue.ManualQueue()
    await q.put(project["id"])
    fresh = manualqueue.ManualQueue()
    assert fresh.restore() == 1
    assert fresh.restore() == 0
    assert fresh.ids() == [project["id"]]


def test_unreadable_saved_state_is_dropped_rather_than_crashing():
    db.set_setting(manualqueue.SETTING, "{not json")
    q = manualqueue.ManualQueue()
    assert q.restore() == 0
    assert q.ids() == []

    db.set_setting(manualqueue.SETTING, json.dumps({"nope": 1}))
    assert manualqueue.ManualQueue().restore() == 0

    db.set_setting(manualqueue.SETTING, json.dumps(["not-an-id"]))
    assert manualqueue.ManualQueue().restore() == 0


def test_nothing_saved_restores_nothing():
    assert manualqueue.ManualQueue().restore() == 0


def test_it_keeps_the_asyncio_queue_surface():
    """Every existing caller and test treats this as an asyncio.Queue."""
    q = manualqueue.ManualQueue()
    assert q.empty() is True
    assert q.qsize() == 0
    q.put_nowait(4)
    assert q.empty() is False
    assert q.qsize() == 1
    assert q.get_nowait() == 4
    with pytest.raises(asyncio.QueueEmpty):
        q.get_nowait()


@pytest.mark.asyncio
async def test_a_database_that_will_not_take_the_write_does_not_break_the_queue(monkeypatch):
    """A queue that cannot be saved still works: this is bookkeeping, and the
    run somebody asked for matters more than the record of it."""
    def boom(key, value):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(manualqueue.db, "set_setting", boom)
    q = manualqueue.ManualQueue()
    await q.put(7)
    assert q.ids() == [7]


@pytest.mark.asyncio
async def test_the_worker_restores_on_its_first_loop(project, monkeypatch):
    """The restore hangs off `worker_loop`, which is the one thing that runs
    exactly once per service start - never in a smoke test, never in an
    ad-hoc `python -c` against the live database."""
    saved = manualqueue.ManualQueue()
    await saved.put(project["id"])
    worker.manual_queue = manualqueue.ManualQueue()

    async def one_tick():
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_tick", one_tick)
    with pytest.raises(asyncio.CancelledError):
        await worker.worker_loop()
    assert worker.manual_queue.ids() == [project["id"]]

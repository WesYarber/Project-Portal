"""Stopping a run mid-flight.

The interesting cases aren't "does killpg work" but the bookkeeping around it:
a canceled run must not read as a failure, must not leave the `runs` row stuck
on 'running' (which would deadlock the worker via `is_run_running()`), and must
still settle sanely when the process it names no longer exists.
"""
from __future__ import annotations

import asyncio

import pytest

from app import agent_runner, config, db, usage, worker

from tests.test_stream import fake_claude  # noqa: F401 - fixture reuse


@pytest.fixture
def project():
    return db.create_project("Cancel Me", "desc")


@pytest.mark.asyncio
async def test_cancel_stops_a_live_run(tmp_path, fake_claude):  # noqa: F811
    # Emits one event, then hangs. Long enough to cancel deterministically.
    fake_claude(
        """
        printf '%s\\n' '{"type":"system","subtype":"init","model":"opus","tools":[]}'
        sleep 120
        """
    )
    run_id = db.create_run(None, "build", "opus")

    started = asyncio.Event()

    def on_event(event, lines):
        started.set()

    task = asyncio.create_task(
        agent_runner.run_claude(
            "p", tmp_path / "ws", "opus", timeout_min=10, on_event=on_event, run_id=run_id
        )
    )
    await asyncio.wait_for(started.wait(), timeout=10)

    assert agent_runner.cancel_run(run_id) is True
    result = await asyncio.wait_for(task, timeout=10)

    assert result.cancelled is True
    assert result.ok is False
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_canceling_a_live_run_marks_it_canceled_not_errored(tmp_path, fake_claude, project):  # noqa: F811
    fake_claude("sleep 120\n")
    task = asyncio.create_task(worker.run_project_task(project, "build"))

    # Wait for the worker to have created the run row and registered the process.
    for _ in range(200):
        row = db.active_run()
        if row is not None and row["id"] in agent_runner._ACTIVE_PROCS:  # noqa: SLF001
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - only on a badly broken runner
        pytest.fail("run never became live")

    assert worker.cancel_run(row["id"]) == "cancelled"
    await asyncio.wait_for(task, timeout=10)

    run = db.get_run(row["id"])
    assert run["status"] == "cancelled"
    assert run["ended_at"]
    # The whole point: the worker is free to pick up work again.
    assert db.is_run_running() is False


def test_canceling_an_orphaned_row_settles_it(project):
    """A 'running' row with no live process is a leftover from a restart.
    Killing nothing and leaving it alone would block the worker forever."""
    run_id = db.create_run(project["id"], "build", "opus")
    assert run_id not in agent_runner._ACTIVE_PROCS  # noqa: SLF001

    assert worker.cancel_run(run_id) == "orphaned"

    run = db.get_run(run_id)
    assert run["status"] == "cancelled"
    assert "orphaned" in (run["summary"] or "").lower()
    assert db.is_run_running() is False


def test_canceling_a_finished_or_missing_run_is_a_no_op(project):
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok")
    assert worker.cancel_run(run_id) == "not_running"
    assert db.get_run(run_id)["status"] == "ok"

    assert worker.cancel_run(9999) == "missing"


def test_cancel_registry_does_not_leak_between_runs():
    """A stale entry in `_CANCEL_REQUESTED` would make the *next* run using that
    id report itself canceled the moment it finished."""
    assert agent_runner.cancel_run(4242) is False
    assert agent_runner.cancel_requested(4242) is False
    assert 4242 not in agent_runner._CANCEL_REQUESTED  # noqa: SLF001

    agent_runner._CANCEL_REQUESTED.add(7)  # noqa: SLF001
    agent_runner._ACTIVE_PROCS[7] = object()  # noqa: SLF001
    agent_runner._forget(7)  # noqa: SLF001
    assert agent_runner.cancel_requested(7) is False
    assert 7 not in agent_runner._ACTIVE_PROCS  # noqa: SLF001


# --------------------------------------------------------------------------
# Canceled runs in the usage maths
# --------------------------------------------------------------------------

def _run(status: str, cost: float = 0.0, ts: str = "2026-07-21T10:00:00+00:00") -> dict:
    return {"status": status, "started_at": ts, "ended_at": ts, "cost_usd": cost,
            "num_turns": 1, "project_id": None}


def test_canceled_runs_are_not_counted_as_failures():
    buckets = usage.bucket_by_day(
        [_run("ok"), _run("cancelled"), _run("error")], days=1, today="2026-07-21"
    )
    day = buckets[0]
    assert (day["ok"], day["failed"], day["cancelled"], day["runs"]) == (1, 1, 1, 3)


def test_success_rate_ignores_canceled_and_running_runs():
    """Canceling a run is Wes's decision; it shouldn't dent the success rate,
    and neither should a run that hasn't finished yet."""
    buckets = usage.bucket_by_day(
        [_run("ok"), _run("cancelled"), _run("running")], days=1, today="2026-07-21"
    )
    totals = usage.summarize(buckets)
    assert totals["success_rate"] == 100.0
    assert totals["cancelled"] == 1


def test_canceled_is_a_filterable_run_status():
    """`/activity?status=` only honors values in RUN_STATUSES, so a canceled
    run would be unfilterable if it were missing here."""
    assert "cancelled" in config.RUN_STATUSES

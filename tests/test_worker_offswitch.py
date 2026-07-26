"""The "run agents automatically" switch has to cover every scheduled job.

Found on 2026-07-26 while standing up a throwaway portal instance to photograph
a layout fix: the instance was booted with `worker_enabled=0` precisely so it
could not touch the real board, and it started a daily reflect anyway - a real
`claude` run, spending window allowance and rewriting `profile.md`.

The gate was real but only covered one of the three kinds of scheduled work.
`_start_one` (scheduled project runs) read it; `_maybe_reflect` and
`_maybe_compact` were called unconditionally from the tick and read nothing. So
the switch Wes would reach for to stop the portal spawning agents - to protect a
weekly window, or while debugging - silently left two agent-spawning jobs armed.

The distinction these pin: a *manual* run deliberately ignores the switch,
because pressing the button is the request. Only scheduled work consults it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app import db, worker


def _quiet_and_due(monkeypatch) -> None:
    """Past the day boundary, nothing running: every other guard says "go", so
    the switch is the only thing that can stop these."""
    monkeypatch.setattr(worker.daycycle, "reset_hour", lambda: 5)
    monkeypatch.setattr(
        worker.daycycle, "local_now",
        lambda: datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(worker.db, "is_run_running", lambda: False)


def _write_learnings(lines: int) -> None:
    worker.config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    worker.config.LEARNINGS_MD.write_text(
        "\n".join(f"- fact {i}" for i in range(lines)), encoding="utf-8"
    )


# --- the switch itself -----------------------------------------------------


def test_the_switch_defaults_to_on(temp_data_dir):
    assert worker.scheduled_work_enabled() is True


@pytest.mark.parametrize("value,expected", [("1", True), ("0", False), ("", True)])
def test_the_switch_reads_the_setting(temp_data_dir, value, expected):
    db.set_setting("worker_enabled", value)
    assert worker.scheduled_work_enabled() is expected


# --- the reflect ------------------------------------------------------------


def test_the_reflect_does_not_run_with_the_worker_off(temp_data_dir, monkeypatch):
    _quiet_and_due(monkeypatch)
    db.set_setting("worker_enabled", "0")
    started = []
    monkeypatch.setattr(worker, "run_reflect", lambda: started.append(True))

    asyncio.run(worker._maybe_reflect())  # noqa: SLF001

    assert started == []
    # And nothing was stamped, so turning the worker back on still reflects today.
    assert not db.get_setting("last_reflect_date")


def test_the_reflect_still_runs_with_the_worker_on(temp_data_dir, monkeypatch):
    """The delete-the-fix direction: the new gate must not have simply disabled
    the job."""
    _quiet_and_due(monkeypatch)
    db.set_setting("worker_enabled", "1")
    started = []

    async def fake_reflect():
        started.append(True)

    monkeypatch.setattr(worker, "run_reflect", fake_reflect)

    async def drive():
        await worker._maybe_reflect()  # noqa: SLF001
        # It is spawned as a task, not awaited, so yield once to let it start.
        assert worker.REFLECT_SLOT in worker._inflight  # noqa: SLF001
        await asyncio.sleep(0)

    try:
        asyncio.run(drive())
        assert started == [True]
    finally:
        worker._inflight.clear()  # noqa: SLF001


# --- the learnings compaction ---------------------------------------------


def test_the_compaction_does_not_run_with_the_worker_off(temp_data_dir, monkeypatch):
    _quiet_and_due(monkeypatch)
    db.set_setting("learnings_cap_lines", "10")
    _write_learnings(30)
    db.set_setting("worker_enabled", "0")
    kicked = []
    monkeypatch.setattr(worker, "start_compaction", lambda: kicked.append(True) or True)

    asyncio.run(worker._maybe_compact())  # noqa: SLF001

    assert kicked == []
    # Not stamped either: the day's one attempt must not be burned by a tick
    # that never actually ran anything.
    assert not db.get_setting("last_auto_compact_date")


def test_the_compaction_still_runs_with_the_worker_on(temp_data_dir, monkeypatch):
    _quiet_and_due(monkeypatch)
    db.set_setting("learnings_cap_lines", "10")
    _write_learnings(30)
    db.set_setting("worker_enabled", "1")
    kicked = []
    monkeypatch.setattr(worker, "start_compaction", lambda: kicked.append(True) or True)

    asyncio.run(worker._maybe_compact())  # noqa: SLF001

    assert kicked == [True]


# --- what the switch must NOT stop ----------------------------------------


def test_a_manual_run_ignores_the_switch(temp_data_dir):
    """Pressing "run now" is the request. If the switch blocked manual runs too,
    the only way to use the portal with the scheduler off would be to turn the
    scheduler on."""
    import inspect

    src = inspect.getsource(worker._start_one)  # noqa: SLF001
    # The manual branch is taken before the enabled check bites: the check sits
    # on the `elif`, i.e. only on the scheduled path.
    assert "elif not worker_enabled:" in src

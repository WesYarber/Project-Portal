"""learnings.md auto-compaction (research §4, todo #220).

learnings.md is injected into every run's prompt, so it must not grow without
bound. It used to shrink only when Wes pressed the compact button on /memory;
now the portal watches its line count and, once it crosses a configurable cap,
runs a compaction itself at the next quiet day-boundary - snapshotting the file
into /memory revisions first, exactly as the button path does, so the automatic
rewrite can never lose anything unrecoverably.

These tests defend each guard on that trigger and the plumbing around it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, settings_form, worker


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _write_learnings(n_lines: int) -> None:
    config.LEARNINGS_MD.parent.mkdir(parents=True, exist_ok=True)
    config.LEARNINGS_MD.write_text("\n".join(f"- line {i}" for i in range(n_lines)), encoding="utf-8")


def _make_it_quiet(monkeypatch) -> None:
    """Put the clock past the reset hour and record any compaction kick."""
    monkeypatch.setattr(worker.daycycle, "reset_hour", lambda: 5)
    monkeypatch.setattr(
        worker.daycycle, "local_now", lambda: datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
    )


# --------------------------------------------------------------------------
# The cap reading
# --------------------------------------------------------------------------

def test_cap_defaults_to_200(temp_data_dir):
    assert worker.learnings_cap() == 200


def test_cap_reads_the_setting(temp_data_dir):
    db.set_setting("learnings_cap_lines", "50")
    assert worker.learnings_cap() == 50


def test_zero_cap_disables_the_trigger(temp_data_dir):
    db.set_setting("learnings_cap_lines", "0")
    assert worker.learnings_cap() == 0
    _write_learnings(9999)
    assert worker.learnings_over_cap() is False


def test_junk_cap_falls_back_to_200(temp_data_dir):
    db.set_setting("learnings_cap_lines", "not a number")
    assert worker.learnings_cap() == 200


def test_over_cap_is_a_line_comparison(temp_data_dir):
    db.set_setting("learnings_cap_lines", "10")
    _write_learnings(10)
    assert worker.learnings_over_cap() is False
    _write_learnings(11)
    assert worker.learnings_over_cap() is True


def test_missing_file_is_not_over_cap(temp_data_dir):
    assert worker.learnings_over_cap() is False


# --------------------------------------------------------------------------
# The scheduled trigger
# --------------------------------------------------------------------------

def test_over_cap_after_the_boundary_kicks_a_compaction(temp_data_dir, monkeypatch):
    db.set_setting("learnings_cap_lines", "10")
    _write_learnings(30)
    _make_it_quiet(monkeypatch)
    kicked = []
    monkeypatch.setattr(worker, "start_compaction", lambda: kicked.append(True) or True)

    asyncio.run(worker._maybe_compact())

    assert kicked == [True]
    # Stamped up front so it is at most one attempt a day, win or lose.
    assert db.get_setting("last_auto_compact_date") == worker.daycycle.current_day()
    entry = db.list_journal(project_id=None, limit=1)[0]
    assert "auto-compact" in entry["content_md"]
    assert "revisions" in entry["content_md"]


def test_under_cap_does_nothing(temp_data_dir, monkeypatch):
    db.set_setting("learnings_cap_lines", "100")
    _write_learnings(30)
    _make_it_quiet(monkeypatch)
    kicked = []
    monkeypatch.setattr(worker, "start_compaction", lambda: kicked.append(True) or True)

    asyncio.run(worker._maybe_compact())

    assert kicked == []
    assert not db.get_setting("last_auto_compact_date")


def test_only_once_per_day(temp_data_dir, monkeypatch):
    db.set_setting("learnings_cap_lines", "10")
    _write_learnings(30)
    _make_it_quiet(monkeypatch)
    # Record "already compacted" for the day the patched clock reports, not the
    # real wall-clock day - otherwise the stored date and the check-time day
    # disagree whenever the suite runs on a different date than the fixture.
    db.set_setting("last_auto_compact_date", worker.daycycle.current_day())
    kicked = []
    monkeypatch.setattr(worker, "start_compaction", lambda: kicked.append(True) or True)

    asyncio.run(worker._maybe_compact())

    assert kicked == []


def test_before_the_reset_hour_it_waits(temp_data_dir, monkeypatch):
    db.set_setting("learnings_cap_lines", "10")
    _write_learnings(30)
    monkeypatch.setattr(worker.daycycle, "reset_hour", lambda: 5)
    monkeypatch.setattr(
        worker.daycycle, "local_now", lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc)
    )
    kicked = []
    monkeypatch.setattr(worker, "start_compaction", lambda: kicked.append(True) or True)

    asyncio.run(worker._maybe_compact())

    assert kicked == []


def test_a_zero_cap_never_fires(temp_data_dir, monkeypatch):
    db.set_setting("learnings_cap_lines", "0")
    _write_learnings(9999)
    _make_it_quiet(monkeypatch)
    kicked = []
    monkeypatch.setattr(worker, "start_compaction", lambda: kicked.append(True) or True)

    asyncio.run(worker._maybe_compact())

    assert kicked == []


def test_a_run_in_flight_blocks_it(temp_data_dir, monkeypatch):
    db.set_setting("learnings_cap_lines", "10")
    _write_learnings(30)
    _make_it_quiet(monkeypatch)
    monkeypatch.setattr(worker.db, "is_run_running", lambda: True)
    kicked = []
    monkeypatch.setattr(worker, "start_compaction", lambda: kicked.append(True) or True)

    asyncio.run(worker._maybe_compact())

    assert kicked == []


# --------------------------------------------------------------------------
# The prompt and the surfaces
# --------------------------------------------------------------------------

def test_the_compact_prompt_names_the_cap(temp_data_dir):
    db.set_setting("learnings_cap_lines", "150")
    prompt = agent_runner.build_prompt("compact", None)
    assert "150 lines" in prompt


def test_a_zero_cap_leaves_no_hard_target_in_the_prompt(temp_data_dir):
    db.set_setting("learnings_cap_lines", "0")
    prompt = agent_runner.build_prompt("compact", None)
    assert "Hard target" not in prompt


def test_the_cap_field_validates(temp_data_dir):
    clean = settings_form.REGISTRY["learnings_cap_lines"].clean
    assert clean("300") == "300"
    assert clean("0") == "0"  # 0 is the real "disabled" value
    assert clean("junk") == "200"
    assert clean("-5") == "200"


def test_the_memory_page_warns_when_over_cap(client, temp_data_dir):
    db.set_setting("learnings_cap_lines", "10")
    _write_learnings(40)
    body = client.get("/memory").text
    assert "Over the 10-line cap" in body


def test_the_memory_page_is_quiet_when_under_cap(client, temp_data_dir):
    db.set_setting("learnings_cap_lines", "100")
    _write_learnings(5)
    body = client.get("/memory").text
    assert "Over the" not in body

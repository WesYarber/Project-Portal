"""learnings.md auto-compaction (research §4, todo #220).

learnings.md is injected into every run's prompt, so it must not grow without
bound. It used to shrink only when Wes pressed the compact button on /memory;
now the portal watches its size and, once it crosses a configurable cap, runs a
compaction itself at the next quiet day-boundary - snapshotting the file into
/memory revisions first, exactly as the button path does, so the automatic
rewrite can never lose anything unrecoverably.

**The cap counts BYTES, and until 2026-08-07 it counted lines.** That unit was
the whole bug: a prompt spends bytes (`promptbudget.learnings_for_prompt` fills
a 16 KB budget from the top of the file), so on the live file - 189 lines
against a 200-line cap - the trigger read "comfortably under" and stayed asleep
for ten days while the file reached 58 KB, of which 43 KB could not reach a
prompt at all. A cap in the wrong unit is not a loose cap, it is no cap. The
tests below pin the unit as hard as they pin the guards.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, memory, settings_form, worker


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _write_learnings(n_lines: int, *, width: int = 8) -> None:
    """A file of `n_lines` bullets, each roughly `width` bytes wide."""
    config.LEARNINGS_MD.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"- {str(i).ljust(width, 'x')}" for i in range(n_lines))
    config.LEARNINGS_MD.write_text(body, encoding="utf-8")


def _write_learnings_kb(kb: float) -> None:
    config.LEARNINGS_MD.parent.mkdir(parents=True, exist_ok=True)
    want = int(kb * 1024)
    lines = []
    size = 0
    i = 0
    while size < want:
        line = f"- learning {i} " + "x" * 200
        lines.append(line)
        size += len(line) + 1
        i += 1
    config.LEARNINGS_MD.write_text("\n".join(lines), encoding="utf-8")


def _forget(key: str) -> None:
    """Drop a settings row - the fixture seeds every default, and the migration
    is only reachable on an install that has never had the new key."""
    conn = db.get_conn()
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    conn.commit()


def _make_it_quiet(monkeypatch) -> None:
    """Put the clock past the reset hour and record any compaction kick."""
    monkeypatch.setattr(worker.daycycle, "reset_hour", lambda: 5)
    monkeypatch.setattr(
        worker.daycycle, "local_now", lambda: datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
    )


# --------------------------------------------------------------------------
# The cap reading
# --------------------------------------------------------------------------

def test_cap_defaults_to_24_kb(temp_data_dir):
    assert worker.learnings_cap_kb() == 24


def test_cap_reads_the_setting(temp_data_dir):
    db.set_setting("learnings_cap_kb", "50")
    assert worker.learnings_cap_kb() == 50


def test_zero_cap_disables_the_trigger(temp_data_dir):
    db.set_setting("learnings_cap_kb", "0")
    assert worker.learnings_cap_kb() == 0
    _write_learnings_kb(400)
    assert worker.learnings_over_cap() is False


def test_junk_cap_falls_back_to_24(temp_data_dir):
    db.set_setting("learnings_cap_kb", "not a number")
    assert worker.learnings_cap_kb() == 24


def test_over_cap_is_a_byte_comparison(temp_data_dir):
    db.set_setting("learnings_cap_kb", "4")
    _write_learnings_kb(3)
    assert worker.learnings_over_cap() is False
    _write_learnings_kb(6)
    assert worker.learnings_over_cap() is True


def _write_the_live_file_shape() -> str:
    """189 lines weighing ~58 KB - the shape of the real learnings.md on
    2026-08-07, which is the file that kept the old trigger asleep."""
    config.LEARNINGS_MD.parent.mkdir(parents=True, exist_ok=True)
    long_line = "- " + "x" * 313
    config.LEARNINGS_MD.write_text("\n".join([long_line] * 189), encoding="utf-8")
    return config.LEARNINGS_MD.read_text(encoding="utf-8")


def test_the_cap_a_line_count_would_have_missed(temp_data_dir):
    """The regression that must never come back, pinned in BOTH directions.

    The file is 189 lines and 58 KB, so a line comparison and a byte comparison
    disagree about it for every cap between 25 and 200 - which is exactly the
    range a sane setting sits in. Asserting only "over at 24 KB" would pass
    under a line comparison too (189 > 24), so the load-bearing half of this
    test is the second one: at a 60 KB cap the file is UNDER, where a line
    comparison would read 189 > 60 and fire.
    """
    text = _write_the_live_file_shape()
    assert len(text.splitlines()) == 189
    assert 55 * 1024 < len(text) < 60 * 1024

    db.set_setting("learnings_cap_kb", "24")
    assert worker.learnings_over_cap() is True

    db.set_setting("learnings_cap_kb", "60")
    assert worker.learnings_over_cap() is False


def test_the_old_line_cap_would_have_said_this_file_was_fine(temp_data_dir):
    """The incident itself, stated as an assertion rather than a comment: the
    live file sat under the 200-LINE cap it shipped with while three quarters
    of it could not reach a prompt."""
    text = _write_the_live_file_shape()
    assert len(text.splitlines()) < 200  # what the old trigger compared

    db.set_setting("prompt_learnings_kb", "16")
    reach = worker.learnings_reach()
    assert reach.bytes_out > 40 * 1024
    assert worker.learnings_over_cap() is True


def test_missing_file_is_not_over_cap(temp_data_dir):
    assert worker.learnings_over_cap() is False


def test_the_target_is_the_prompt_budget(temp_data_dir):
    """One number, not two. The compaction aims at what a prompt carries."""
    assert worker.learnings_target_kb() == 16
    db.set_setting("prompt_learnings_kb", "9")
    assert worker.learnings_target_kb() == 9


def test_the_cap_sits_above_the_target(temp_data_dir):
    """Hysteresis, by construction: fire at 24, aim at 16. Equal values would
    re-fire the day after every compaction over a single bullet."""
    assert worker.learnings_cap_kb() > worker.learnings_target_kb()


# --------------------------------------------------------------------------
# The reach measurement
# --------------------------------------------------------------------------

def test_a_small_file_reaches_a_prompt_whole(temp_data_dir):
    _write_learnings(5)
    reach = worker.learnings_reach()
    assert reach.fits
    assert reach.entries_out == 0
    assert reach.unreachable == ()


def test_a_big_file_reports_what_never_reaches_a_prompt(temp_data_dir):
    db.set_setting("prompt_learnings_kb", "2")
    config.LEARNINGS_MD.parent.mkdir(parents=True, exist_ok=True)
    config.LEARNINGS_MD.write_text(
        "# Learnings\n\nWhat earns a place here.\n\n"
        "## Near the top\n\n" + "\n".join(f"- top {i} " + "a" * 100 for i in range(10))
        + "\n\n## Down the bottom\n\n"
        + "\n".join(f"- tail {i} " + "b" * 100 for i in range(30)),
        encoding="utf-8",
    )
    reach = worker.learnings_reach()

    assert not reach.fits
    assert reach.entries_total == 40
    assert reach.entries_out > 0
    assert reach.entries_in_prompt + reach.entries_out == reach.entries_total
    # The section a prompt never reaches is named, not just counted.
    assert "Down the bottom" in [u.heading for u in reach.unreachable]
    assert reach.bytes_out > 0


def test_a_missing_file_measures_as_fitting(temp_data_dir):
    reach = worker.learnings_reach()
    assert reach.fits
    assert reach.total == 0


# --------------------------------------------------------------------------
# The scheduled trigger
# --------------------------------------------------------------------------

def test_over_cap_after_the_boundary_kicks_a_compaction(temp_data_dir, monkeypatch):
    db.set_setting("learnings_cap_kb", "4")
    _write_learnings_kb(20)
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
    assert "KB" in entry["content_md"]


def test_the_journal_line_says_how_much_is_unread(temp_data_dir, monkeypatch):
    """The size on its own reads as housekeeping. The sentence that says why
    the run is worth its allowance is how much of the file nothing reads."""
    db.set_setting("learnings_cap_kb", "4")
    db.set_setting("prompt_learnings_kb", "2")
    _write_learnings_kb(20)
    _make_it_quiet(monkeypatch)
    monkeypatch.setattr(worker, "start_compaction", lambda: True)

    asyncio.run(worker._maybe_compact())

    entry = db.list_journal(project_id=None, limit=1)[0]
    assert "never reach a prompt" in entry["content_md"]


def test_under_cap_does_nothing(temp_data_dir, monkeypatch):
    db.set_setting("learnings_cap_kb", "100")
    _write_learnings_kb(20)
    _make_it_quiet(monkeypatch)
    kicked = []
    monkeypatch.setattr(worker, "start_compaction", lambda: kicked.append(True) or True)

    asyncio.run(worker._maybe_compact())

    assert kicked == []
    assert not db.get_setting("last_auto_compact_date")


def test_only_once_per_day(temp_data_dir, monkeypatch):
    db.set_setting("learnings_cap_kb", "4")
    _write_learnings_kb(20)
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
    db.set_setting("learnings_cap_kb", "4")
    _write_learnings_kb(20)
    monkeypatch.setattr(worker.daycycle, "reset_hour", lambda: 5)
    monkeypatch.setattr(
        worker.daycycle, "local_now", lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc)
    )
    kicked = []
    monkeypatch.setattr(worker, "start_compaction", lambda: kicked.append(True) or True)

    asyncio.run(worker._maybe_compact())

    assert kicked == []


def test_a_zero_cap_never_fires(temp_data_dir, monkeypatch):
    db.set_setting("learnings_cap_kb", "0")
    _write_learnings_kb(400)
    _make_it_quiet(monkeypatch)
    kicked = []
    monkeypatch.setattr(worker, "start_compaction", lambda: kicked.append(True) or True)

    asyncio.run(worker._maybe_compact())

    assert kicked == []


def test_a_run_in_flight_blocks_it(temp_data_dir, monkeypatch):
    db.set_setting("learnings_cap_kb", "4")
    _write_learnings_kb(20)
    _make_it_quiet(monkeypatch)
    monkeypatch.setattr(worker.db, "is_run_running", lambda: True)
    kicked = []
    monkeypatch.setattr(worker, "start_compaction", lambda: kicked.append(True) or True)

    asyncio.run(worker._maybe_compact())

    assert kicked == []


# --------------------------------------------------------------------------
# The migration off the line cap
# --------------------------------------------------------------------------

def test_a_disabled_line_cap_stays_disabled(temp_data_dir):
    """0 meant "never auto-compact". Re-enabling a scheduled agent run somebody
    turned off is exactly the kind of thing that burns allowance overnight with
    nobody having asked for it."""
    db.set_setting("learnings_cap_lines", "0")
    _forget("learnings_cap_kb")

    db._migrate_learnings_cap()

    assert db.get_setting("learnings_cap_kb") == "0"


def test_a_non_zero_line_cap_is_not_translated(temp_data_dir):
    """A line count says nothing about bytes - that is the whole point of the
    change - so the new cap takes its default rather than a made-up conversion."""
    db.set_setting("learnings_cap_lines", "200")
    _forget("learnings_cap_kb")

    db._migrate_learnings_cap()

    assert db.get_setting("learnings_cap_kb") is None  # falls to the default seed
    assert worker.learnings_cap_kb() == 24


def test_the_migration_never_overwrites_a_chosen_value(temp_data_dir):
    db.set_setting("learnings_cap_lines", "0")
    db.set_setting("learnings_cap_kb", "40")

    db._migrate_learnings_cap()

    assert db.get_setting("learnings_cap_kb") == "40"


# --------------------------------------------------------------------------
# The prompt and the surfaces
# --------------------------------------------------------------------------

def test_the_compact_prompt_names_the_cap(temp_data_dir):
    db.set_setting("learnings_cap_kb", "30")
    _write_learnings(5)
    prompt = agent_runner.build_prompt("compact", None)
    assert "30 KB" in prompt


def test_the_compact_prompt_states_what_a_run_never_sees(temp_data_dir):
    """The old block told the agent the file auto-compacts at N lines and
    nothing else, which left it believing the whole file is read. It is not."""
    db.set_setting("prompt_learnings_kb", "2")
    config.LEARNINGS_MD.parent.mkdir(parents=True, exist_ok=True)
    config.LEARNINGS_MD.write_text(
        "## Up top\n\n" + "\n".join(f"- top {i} " + "a" * 100 for i in range(10))
        + "\n\n## The long tail\n\n"
        + "\n".join(f"- tail {i} " + "b" * 100 for i in range(30)),
        encoding="utf-8",
    )
    prompt = agent_runner.build_prompt("compact", None)

    assert "not in any prompt today" in prompt
    assert "The long tail" in prompt
    # And the target, which is the prompt budget rather than the cap.
    assert "Finish under 2 KB" in prompt


def test_a_zero_cap_leaves_no_hard_target_in_the_prompt(temp_data_dir):
    db.set_setting("learnings_cap_kb", "0")
    prompt = agent_runner.build_prompt("compact", None)
    assert "Hard target" not in prompt


def test_a_measurement_failure_still_gives_a_target(temp_data_dir, monkeypatch):
    """A bug in the reach measurement must never cost the compaction its
    instructions - the run is already spending allowance by the time this is
    built."""
    monkeypatch.setattr(
        agent_runner.memory, "learnings_reach",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    prompt = agent_runner.build_prompt("compact", None)
    assert "Hard target" in prompt
    assert "Finish under 16 KB" in prompt


def test_the_cap_field_validates(temp_data_dir):
    clean = settings_form.REGISTRY["learnings_cap_kb"].clean
    assert clean("40") == "40"
    assert clean("0") == "0"  # 0 is the real "disabled" value
    assert clean("junk") == "24"
    assert clean("-5") == "24"


def test_the_memory_page_warns_when_over_cap(client, temp_data_dir):
    db.set_setting("learnings_cap_kb", "4")
    _write_learnings_kb(20)
    body = client.get("/memory").text
    assert "Over the 4 KB cap" in body


def test_the_memory_page_names_the_unread_learnings(client, temp_data_dir):
    """Worth saying whether or not the file is over its cap: an entry a prompt
    never carries is written and never read, and nothing else says so."""
    db.set_setting("learnings_cap_kb", "0")  # cap off, so only the reach can warn
    db.set_setting("prompt_learnings_kb", "2")
    config.LEARNINGS_MD.parent.mkdir(parents=True, exist_ok=True)
    config.LEARNINGS_MD.write_text(
        "## Up top\n\n" + "\n".join(f"- top {i} " + "a" * 100 for i in range(10))
        + "\n\n## The long tail\n\n"
        + "\n".join(f"- tail {i} " + "b" * 100 for i in range(30)),
        encoding="utf-8",
    )
    body = client.get("/memory").text

    assert "never reach a prompt" in body
    assert "The long tail" in body


def test_the_memory_page_is_quiet_when_under_cap(client, temp_data_dir):
    db.set_setting("learnings_cap_kb", "100")
    _write_learnings(5)
    body = client.get("/memory").text
    assert "Over the" not in body
    assert "never reach a prompt" not in body


def test_the_memory_page_states_the_reach(client, temp_data_dir):
    _write_learnings(5)
    body = client.get("/memory").text
    assert "reaches a prompt" in body


def test_the_accessors_are_one_implementation(temp_data_dir):
    """worker re-exports memory's, rather than reading the row twice."""
    assert worker.learnings_cap_kb is memory.learnings_cap_kb
    assert worker.learnings_target_kb is memory.learnings_target_kb
    assert worker.learnings_reach is memory.learnings_reach

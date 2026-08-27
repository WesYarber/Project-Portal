"""What the memory jobs do when they run out of turns.

On 2026-08-07 both of them did, four hours apart, and neither said so.

    892  reflect  05:45  31 turns  $5.13  status=error  summary=''
    905  compact  10:31  31 turns  $6.16  status=error  summary=''

Both were killed by the CLI at a hardcoded 30-turn ceiling, part-way through
rewriting a file. Both were recorded in the database as failures. And both then
fell through to the success path underneath and wrote the opposite into the
journal:

    "Daily reflect ran. profile.md reviewed and updated: 27366 -> 17482 chars"
    "Learnings compacted: 189 lines / 59239 chars -> 61 lines / 17189 chars"

The second one is the dangerous shape. Those sizes are real - both jobs measure
the file off disk rather than trusting the agent's own account of its edit,
which is normally the careful thing to do. Here it made a half-made edit read as
a deliberate 71% distillation, so the one version Wes might have restored is the
one he had no reason to think was damaged.

So these tests defend two separate claims: that a memory job gets enough turns
to finish, and that when one does not finish it says so instead of claiming the
opposite. The second is guarded on `result.ok` rather than on the turn limit
specifically, because enumerating failure modes is what left this one silent.
"""
from __future__ import annotations

import asyncio

import pytest

from app import agent_runner, config, db, memory, worker


def _write(stem: str, text: str) -> None:
    path = memory.tracked()[stem]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _killed_at_the_turn_limit() -> agent_runner.RunResult:
    """What the CLI actually hands back: a failure, no message, and a subtype.

    The empty `result_text` is the whole reason the old summary was blank, so a
    fixture that puts words in it would test a case that never happens.
    """
    return agent_runner.RunResult(
        ok=False,
        result_text="",
        report=None,
        subtype="error_max_turns",
        num_turns=31,
        cost_usd=6.16,
        session_id="s-905",
    )


def _system_journal() -> list[str]:
    return [r["content_md"] for r in db.list_journal(project_id=None, limit=20)]


def _last_run():
    """The newest run row. `db.list_runs` takes a project id and these have none."""
    return db.get_conn().execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


# --------------------------------------------------------------------------
# The ceiling itself
# --------------------------------------------------------------------------

def test_a_memory_job_is_not_still_capped_at_the_thirty_turns_that_killed_two(
    temp_data_dir, monkeypatch
):
    seen = {}

    async def fake_run(*args, **kwargs):
        seen["max_turns"] = kwargs["max_turns"]
        return agent_runner.RunResult(ok=True, result_text="done", report=None)

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_compaction())
    assert seen["max_turns"] == worker.DEFAULT_MEMORY_MAX_TURNS
    assert seen["max_turns"] > 31, "31 turns is what killed run 905"


def test_the_reflect_gets_the_same_ceiling_as_the_compaction(temp_data_dir, monkeypatch):
    seen = {}

    async def fake_run(*args, **kwargs):
        seen["max_turns"] = kwargs["max_turns"]
        return agent_runner.RunResult(ok=True, result_text="done", report=None)

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_reflect())
    assert seen["max_turns"] == worker.DEFAULT_MEMORY_MAX_TURNS


def test_the_ceiling_is_a_knob_and_not_a_constant(temp_data_dir, monkeypatch):
    db.set_setting("memory_max_turns", "45")
    seen = {}

    async def fake_run(*args, **kwargs):
        seen["max_turns"] = kwargs["max_turns"]
        return agent_runner.RunResult(ok=True, result_text="done", report=None)

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_compaction())
    assert seen["max_turns"] == 45


def test_a_nonsense_setting_falls_back_rather_than_crashing_the_memory_job(temp_data_dir):
    db.set_setting("memory_max_turns", "not a number")
    assert worker.memory_max_turns() == worker.DEFAULT_MEMORY_MAX_TURNS


def test_the_memory_ceiling_is_separate_from_the_project_one(temp_data_dir):
    # Different shape of work, different number. Sharing `run_max_turns` would
    # mean raising a project's ceiling silently raised what a memory job may
    # spend on Wes's allowance, and lowering it could reintroduce this bug.
    db.set_setting("run_max_turns", "400")
    db.set_setting("memory_max_turns", "60")
    assert worker.memory_max_turns() == 60
    assert worker.run_max_turns() == 400


# --------------------------------------------------------------------------
# A compaction that did not finish
# --------------------------------------------------------------------------

def test_a_killed_compaction_does_not_announce_that_it_compacted(temp_data_dir, monkeypatch):
    _write("learnings", "- one\n- two\n- three\n- four\n")

    async def fake_run(*args, **kwargs):
        # Killed part-way through, exactly like run 905: the file on disk has
        # been cut, but the job never got to finish or check it.
        config.LEARNINGS_MD.write_text("- one\n", encoding="utf-8")
        return _killed_at_the_turn_limit()

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_compaction())
    assert not any("Learnings compacted" in e for e in _system_journal())


def test_a_killed_compaction_says_the_edit_may_be_half_made(temp_data_dir, monkeypatch):
    _write("learnings", "- one\n- two\n- three\n- four\n")

    async def fake_run(*args, **kwargs):
        config.LEARNINGS_MD.write_text("- one\n", encoding="utf-8")
        return _killed_at_the_turn_limit()

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_compaction())
    entry = _system_journal()[0]
    assert "turn" in entry.lower()
    # The sizes still get reported - they are the reason to go and look - but
    # framed as a possible part-made edit rather than as an accomplishment.
    assert "4 lines" in entry and "1 lines" in entry
    assert "part-made" in entry
    assert "memory_max_turns" in entry


def test_a_killed_compaction_points_at_the_revision_that_predates_it(
    temp_data_dir, monkeypatch
):
    # The recovery path is the whole point of the snapshot, so the failure line
    # has to name it. Wes restores from /memory, not from the reflog.
    _write("learnings", "- the version worth keeping\n")

    async def fake_run(*args, **kwargs):
        config.LEARNINGS_MD.write_text("", encoding="utf-8")
        return _killed_at_the_turn_limit()

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_compaction())
    assert "revisions" in _system_journal()[0]
    assert memory.revisions("learnings")[0].path.read_text() == "- the version worth keeping\n"


# --------------------------------------------------------------------------
# A reflect that did not finish
# --------------------------------------------------------------------------

def test_a_killed_reflect_does_not_announce_that_it_ran(temp_data_dir, monkeypatch):
    _write("profile", "- the about-me text\n")

    async def fake_run(*args, **kwargs):
        return _killed_at_the_turn_limit()

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_reflect())
    assert not any("Daily reflect ran" in e for e in _system_journal())


def test_a_killed_reflect_names_the_ceiling_and_the_knob(temp_data_dir, monkeypatch):
    async def fake_run(*args, **kwargs):
        return _killed_at_the_turn_limit()

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_reflect())
    entry = _system_journal()[0]
    assert str(worker.DEFAULT_MEMORY_MAX_TURNS) in entry
    assert "memory_max_turns" in entry


def test_a_killed_reflect_does_not_try_again_the_same_day(temp_data_dir, monkeypatch):
    # It spends real allowance and would fail the same way on the same input,
    # so one attempt a day - matching what the compaction does at its kick.
    from app import daycycle

    async def fake_run(*args, **kwargs):
        return _killed_at_the_turn_limit()

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_reflect())
    assert db.get_setting("last_reflect_date") == daycycle.current_day()


# --------------------------------------------------------------------------
# The run row
# --------------------------------------------------------------------------

@pytest.mark.parametrize("job", ["reflect", "compact"])
def test_a_killed_memory_run_records_why_instead_of_an_empty_summary(
    temp_data_dir, monkeypatch, job
):
    async def fake_run(*args, **kwargs):
        return _killed_at_the_turn_limit()

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_reflect() if job == "reflect" else worker.run_compaction())

    row = _last_run()
    assert row["status"] == "error"
    assert (row["summary"] or "").strip(), "runs 892 and 905 both stored ''"
    assert "turn" in row["summary"].lower()


@pytest.mark.parametrize("job", ["reflect", "compact"])
def test_a_killed_memory_run_still_records_what_it_spent(temp_data_dir, monkeypatch, job):
    # Between them runs 892 and 905 cost $11.29 and produced nothing. A failure
    # that drops the cost makes exactly that pattern invisible in the history.
    async def fake_run(*args, **kwargs):
        return _killed_at_the_turn_limit()

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_reflect() if job == "reflect" else worker.run_compaction())

    row = _last_run()
    assert row["cost_usd"] == pytest.approx(6.16)
    assert row["num_turns"] == 31


# --------------------------------------------------------------------------
# The guard is on "did it succeed", not on "was it this one failure"
# --------------------------------------------------------------------------

@pytest.mark.parametrize("job", ["reflect", "compact"])
def test_a_failure_that_is_not_the_turn_limit_is_caught_too(temp_data_dir, monkeypatch, job):
    """The turn limit is the one that fired; it must not be the only one caught.

    Guarding on the subtype would leave the next new failure mode falling
    through to the success line exactly as this one did.
    """
    async def fake_run(*args, **kwargs):
        return agent_runner.RunResult(
            ok=False, result_text="", report=None,
            subtype="error_during_execution", num_turns=3,
        )

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_reflect() if job == "reflect" else worker.run_compaction())

    journal = _system_journal()
    assert not any("Daily reflect ran" in e for e in journal)
    assert not any("Learnings compacted" in e for e in journal)
    assert "error_during_execution" in journal[0]


# --------------------------------------------------------------------------
# ...and a job that does finish is untouched
# --------------------------------------------------------------------------

def test_a_compaction_that_finishes_still_reports_the_before_and_after(
    temp_data_dir, monkeypatch
):
    _write("learnings", "- one\n- two\n- three\n- four\n")

    async def fake_run(*args, **kwargs):
        config.LEARNINGS_MD.write_text("- one distilled line\n", encoding="utf-8")
        return agent_runner.RunResult(ok=True, result_text="done", report=None)

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_compaction())
    entry = _system_journal()[0]
    assert "Learnings compacted" in entry
    assert "4 lines" in entry and "1 lines" in entry
    assert _last_run()["status"] == "ok"


def test_a_reflect_that_finishes_still_reports_the_profile_size(temp_data_dir, monkeypatch):
    _write("profile", "- one\n- two\n")

    async def fake_run(*args, **kwargs):
        config.PROFILE_MD.write_text("- one\n", encoding="utf-8")
        return agent_runner.RunResult(ok=True, result_text="done", report=None)

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_reflect())
    assert "Daily reflect ran" in _system_journal()[0]
    assert _last_run()["status"] == "ok"


# --------------------------------------------------------------------------
# What the compaction archives, and what it must not
#
# The same half-made-edit hazard, one layer down. A compaction rewrites the
# whole file without going through the write gate that archives a superseded
# learning, so until now nothing it cut was archived at all - and the cut is the
# big one. But the trail must be built from a FINISHED edit: diffing against a
# file the agent was killed part-way through would archive every line it had not
# reached yet as a deliberate drop, writing a fiction into the permanent record.
# --------------------------------------------------------------------------

_KEPT = (
    "The cloudflared container reaches host-published services via "
    "10.0.0.21, never localhost, because an ingress on localhost 502s."
)
_CUT = (
    "Waveshare IT8951 e-paper driver HATs take 5V on VCC but their SPI logic "
    "is 3.3V with no level translator, and HRDY is inverted."
)


def _compaction_writing(after: str):
    """A run that actually rewrites learnings.md, the way the real agent does."""
    async def fake_run(*args, **kwargs):
        config.LEARNINGS_MD.write_text(after, encoding="utf-8")
        return agent_runner.RunResult(ok=True, result_text="done", report=None)
    return fake_run


def test_a_finished_compaction_archives_what_it_dropped(temp_data_dir, monkeypatch):
    _write("learnings", f"- {_KEPT}\n- {_CUT}\n")
    monkeypatch.setattr(agent_runner, "run_claude", _compaction_writing(f"- {_KEPT}\n"))
    asyncio.run(worker.run_compaction())
    archived = memory.archived_learnings()
    assert [a.text for a in archived] == [_CUT]
    assert archived[0].reason == "compacted"


def test_the_journal_says_the_dropped_learnings_were_kept(temp_data_dir, monkeypatch):
    _write("learnings", f"- {_KEPT}\n- {_CUT}\n")
    monkeypatch.setattr(agent_runner, "run_claude", _compaction_writing(f"- {_KEPT}\n"))
    asyncio.run(worker.run_compaction())
    line = next(c for c in _system_journal() if "Learnings compacted" in c)
    assert "1 dropped learning" in line
    assert "does not expire" in line


def test_a_killed_compaction_archives_nothing_at_all(temp_data_dir, monkeypatch):
    # The file on disk is a part-made edit. Every line the agent had not reached
    # is missing from it, and none of them were dropped on purpose.
    _write("learnings", f"- {_KEPT}\n- {_CUT}\n")

    async def killed_midway(*args, **kwargs):
        config.LEARNINGS_MD.write_text("- part\n", encoding="utf-8")
        return _killed_at_the_turn_limit()

    monkeypatch.setattr(agent_runner, "run_claude", killed_midway)
    asyncio.run(worker.run_compaction())
    assert memory.archived_learnings() == []


def test_a_compaction_that_dropped_nothing_leaves_the_journal_line_alone(
    temp_data_dir, monkeypatch
):
    _write("learnings", f"- {_KEPT}\n")
    monkeypatch.setattr(agent_runner, "run_claude", _compaction_writing(f"- {_KEPT}\n"))
    asyncio.run(worker.run_compaction())
    line = next(c for c in _system_journal() if "Learnings compacted" in c)
    assert "archive" not in line

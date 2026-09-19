"""A run that runs out of allowance is paused, not stopped (app/limitpause.py).

Wes, 2026-09-08: "Runs should be paused instead of stopped when they run out of
tokens during the run." Run 1485 had spent 71 turns and was one tool call from
its report when the CLI refused with "You've hit your session limit · resets
7:30pm (UTC)" - a line the portal did not even recognize as a usage limit.

These pin: the CLI's wording is read as a limit; the reset time in it is read
out; a limited run with a session goes to `paused` with the resume time on it,
and one without a session stays an error; the tick wakes it once the window
has reopened with `--resume`, on the same row, appending to the same log and
summing the cost; it is woken at most MAX_RESUMES times; a paused run holds
its project and can be canceled; and the run page says what is going on.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import agent_runner, apiretry, db, limitpause, limits, runlog, worker

CLI_TEXT = "You've hit your session limit · resets 7:30pm (UTC)"


@pytest.fixture
def project():
    return db.create_project(
        "Metronome", description="A thing.", stage="active", build_approved=True, slug="metronome",
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No usage-endpoint reads: the reset time comes from the message."""
    async def no_network(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(limits, "refresh_async", no_network)


def _fake_run(monkeypatch, result: agent_runner.RunResult) -> dict:
    seen: dict = {}

    async def fake(prompt, cwd, model, timeout_min, **kwargs):
        seen.update(kwargs, prompt=prompt, cwd=cwd, model=model)
        return result

    monkeypatch.setattr(agent_runner, "run_claude", fake)
    return seen


def _limited(session="s-1", cost=11.3, turns=71) -> agent_runner.RunResult:
    return agent_runner.RunResult(
        ok=False, is_rate_limited=True, result_text=CLI_TEXT,
        session_id=session, cost_usd=cost, num_turns=turns,
    )


def _journal(project_id: int) -> list[str]:
    return [r["content_md"] for r in db.list_journal(project_id)]


# --- the CLI's wording is a usage limit --------------------------------------


def test_the_cli_session_limit_line_reads_as_a_limit():
    assert agent_runner._looks_rate_limited(CLI_TEXT) is True
    assert agent_runner._looks_rate_limited("You've hit your usage limit") is True
    # The three original words still count.
    assert agent_runner._looks_rate_limited("rate limit exceeded") is True
    # And prose with no "limit" in it never does.
    assert agent_runner._looks_rate_limited("connection failed") is False
    assert agent_runner._looks_rate_limited("hit your head") is False
    # "limit" alone is not enough: the turn limit and the budget limit are the
    # CLI's own ceilings, not the account's allowance.
    assert agent_runner._looks_rate_limited("the run hit the 400-turn limit") is False
    assert agent_runner._looks_rate_limited("max budget limit of $5 spent") is False


# --- the reset time in the message ------------------------------------------


NOW = datetime(2026, 9, 8, 16, 9, tzinfo=timezone.utc)


def test_a_clock_time_still_ahead_today_is_today():
    assert apiretry.parse_reset_hint(CLI_TEXT, NOW) == datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)


def test_a_clock_time_already_past_is_tomorrow():
    later = datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc)
    assert apiretry.parse_reset_hint(CLI_TEXT, later) == datetime(2026, 9, 9, 19, 30, tzinfo=timezone.utc)


def test_the_boundary_is_strict_the_same_minute_is_tomorrow():
    at = datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)
    assert apiretry.parse_reset_hint(CLI_TEXT, at) == datetime(2026, 9, 9, 19, 30, tzinfo=timezone.utc)


def test_twelve_hour_edges():
    assert apiretry.parse_reset_hint("resets 12am (UTC)", NOW).hour == 0
    assert apiretry.parse_reset_hint("resets 12pm (UTC)", NOW).hour == 12
    assert apiretry.parse_reset_hint("resets 4am (UTC)", NOW) == datetime(2026, 9, 9, 4, 0, tzinfo=timezone.utc)


def test_a_month_and_day_pin_the_date():
    assert apiretry.parse_reset_hint("resets Sep 10 at 4am (UTC)", NOW) == \
        datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)
    assert apiretry.parse_reset_hint("resets Sep 10, 4:15am (UTC)", NOW) == \
        datetime(2026, 9, 10, 4, 15, tzinfo=timezone.utc)


def test_a_named_zone_is_honored_and_an_unknown_one_reads_as_utc():
    assert apiretry.parse_reset_hint("resets 2pm (America/Chicago)", NOW) == \
        datetime(2026, 9, 8, 19, 0, tzinfo=timezone.utc)
    assert apiretry.parse_reset_hint("resets 2pm (Nowhere/Land)", NOW) == \
        datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)


def test_junk_reads_as_no_hint():
    assert apiretry.parse_reset_hint("", NOW) is None
    assert apiretry.parse_reset_hint("nothing about time", NOW) is None
    assert apiretry.parse_reset_hint("resets 13pm (UTC)", NOW) is None
    assert apiretry.parse_reset_hint("resets 25:00 (UTC)", NOW) is None
    assert apiretry.parse_reset_hint("resets 7:75pm (UTC)", NOW) is None


@pytest.mark.asyncio
async def test_the_hint_sets_the_backoff_when_there_are_no_headers():
    hint = datetime.now(timezone.utc) + timedelta(hours=2)
    until, why = await worker._rate_limit_backoff(None, hint=hint)
    assert until == hint
    assert "CLI" in why
    assert db.get_setting("backoff_until") == hint.isoformat(timespec="seconds")


@pytest.mark.asyncio
async def test_headers_beat_the_hint():
    hint = datetime.now(timezone.utc) + timedelta(hours=2)
    header_reset = datetime.now(timezone.utc) + timedelta(minutes=20)
    quota = apiretry.Retry(category=apiretry.QUOTA, resets_at=header_reset, limit_type="five_hour")
    until, why = await worker._rate_limit_backoff(quota, hint=hint)
    assert until == header_reset
    assert "five_hour" in why


@pytest.mark.asyncio
async def test_a_hint_in_the_past_is_ignored():
    hint = datetime.now(timezone.utc) - timedelta(minutes=5)
    until, why = await worker._rate_limit_backoff(None, hint=hint)
    assert until > datetime.now(timezone.utc)
    assert "CLI" not in why


@pytest.mark.asyncio
async def test_the_hint_is_capped_like_everything_else():
    hint = datetime.now(timezone.utc) + timedelta(days=3)
    until, _ = await worker._rate_limit_backoff(None, hint=hint)
    assert until <= datetime.now(timezone.utc) + limits.MAX_BACKOFF


# --- the run is paused, not stopped -----------------------------------------


@pytest.mark.asyncio
async def test_a_limited_run_with_a_session_is_paused(project, monkeypatch):
    _fake_run(monkeypatch, _limited())
    await worker.run_project_task(project, "build")

    run = db.list_runs(project["id"])[0]
    assert run["status"] == "paused"
    assert run["session_id"] == "s-1"
    assert run["cost_usd"] == pytest.approx(11.3)
    assert run["num_turns"] == 71
    assert "Paused for the usage window" in run["summary"]
    assert "19:30 UTC" in run["summary"]
    # The wake time is on the row, from the CLI's own message - uncapped, the
    # exact reset the refusal named.
    until = limitpause.resume_after(run)
    assert until is not None and until.hour == 19 and until.minute == 30
    assert until == apiretry.parse_reset_hint(CLI_TEXT)
    # The scheduler backs off to the same moment, or its ceiling if sooner.
    backoff = datetime.fromisoformat(db.get_setting("backoff_until"))
    assert backoff == min(until, backoff)
    assert backoff <= datetime.now(timezone.utc) + limits.MAX_BACKOFF
    # The journal says paused, not stopped - and not the old error line.
    bodies = _journal(project["id"])
    assert any("paused, not stopped" in b for b in bodies)
    assert not any("Usage/rate limit detected" in b for b in bodies)
    # And the timeline carries it.
    events = db.midrun_events_for_run(run["id"])
    assert [e["decision"] for e in events] == [limitpause.PAUSED]


@pytest.mark.asyncio
async def test_a_limited_run_without_a_session_is_paused_to_start_over(project, monkeypatch):
    """Wes, 2026-09-19: a run that "never starts because it is waiting for
    usage limit to refresh" must come back too. It has no session to continue
    and no work to lose, so the row is paused in RESTART mode rather than
    dropped on the floor as it used to be."""
    _fake_run(monkeypatch, _limited(session=None, cost=0.0, turns=1))
    await worker.run_project_task(project, "build")
    run = db.list_runs(project["id"])[0]
    assert run["status"] == "paused"
    assert limitpause.hold_mode(run) == limitpause.RESTART
    assert "starts over" in run["summary"]
    assert db.get_setting("backoff_until")
    assert any("starts over on this same row" in b for b in _journal(project["id"]))


@pytest.mark.asyncio
async def test_the_restart_is_a_fresh_run_of_the_task_on_the_same_row(project, monkeypatch):
    _fake_run(monkeypatch, _limited(session=None, cost=0.0, turns=1))
    await worker.run_project_task(project, "build")
    run_id = db.list_runs(project["id"])[0]["id"]
    db.set_run_scope_record(run_id, "hold_state", json.dumps({
        "limit_until": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "why": "test", "mode": limitpause.RESTART,
    }))

    seen = _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="s-9", cost_usd=2.0, num_turns=9, result_text="done",
    ))
    assert worker.resume_run(db.get_run(run_id)) is True
    await worker._inflight[run_id]

    # No --resume, and the prompt is the ordinary task prompt built now - not
    # the "pick up where you stopped" one, which would be a lie here.
    assert seen.get("resume_session") is None
    assert "Resumed after the usage window" not in seen["prompt"]
    run = db.get_run(run_id)
    assert run["status"] == "ok"
    assert run["id"] == run_id
    assert limitpause.resumes_so_far(run_id) == 1
    assert any("starts from the top" in b for b in _journal(project["id"]))


def test_the_pause_writes_down_which_kind_of_wake_it_decided_on(project):
    """The mode is settled where the failure is still in hand and stored on
    the row, not re-derived at wake time from whatever is left. A row paused
    with a session says so; one paused without says so too."""
    with_session = db.create_run(project["id"], "build", "opus")
    limitpause.pause(project, with_session, "build", _limited(),
                     datetime.now(timezone.utc) + timedelta(hours=1), "test")
    without = db.create_run(project["id"], "build", "opus")
    limitpause.pause(project, without, "build", _limited(session=None),
                     datetime.now(timezone.utc) + timedelta(hours=1), "test")

    def stored(run_id):
        return json.loads(db._row_get(db.get_run(run_id), "hold_state"))

    assert stored(with_session)["mode"] == limitpause.RESUME
    assert stored(without)["mode"] == limitpause.RESTART


@pytest.mark.asyncio
async def test_a_restart_counts_against_the_same_cap_as_a_resume(project, monkeypatch):
    run_id = db.create_run(project["id"], "build", "opus")
    for _ in range(limitpause.MAX_RESUMES):
        db.add_hook_event(run_id, "midrun", "limit", limitpause.RESTARTED, "woke")
    assert limitpause.resumes_so_far(run_id) == limitpause.MAX_RESUMES
    assert limitpause.pause(
        project, run_id, "build", _limited(session=None),
        datetime.now(timezone.utc) + timedelta(hours=1), "test",
    ) is False


@pytest.mark.asyncio
async def test_a_paused_run_holds_its_project(project, monkeypatch):
    _fake_run(monkeypatch, _limited())
    await worker.run_project_task(project, "build")
    assert project["id"] in db.busy_project_ids()
    assert project["id"] not in db.running_project_ids()
    picked, _ = worker._pick_project(None)
    assert picked is None
    picked, _ = worker._pick_project(project["id"])
    assert picked is None


@pytest.mark.asyncio
async def test_a_paused_run_is_not_due_before_its_time(project, monkeypatch):
    _fake_run(monkeypatch, _limited())
    await worker.run_project_task(project, "build")
    run = db.list_runs(project["id"])[0]
    until = limitpause.resume_after(run)
    assert limitpause.due(until - timedelta(seconds=1)) == []
    assert [r["id"] for r in limitpause.due(until)] == [run["id"]]


def test_a_paused_row_with_no_record_is_due_at_once(project):
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "paused", "s-1", 1.0, 3, "paused")
    assert [r["id"] for r in limitpause.due()] == [run_id]


# --- and comes back on the same row ------------------------------------------


def _pause_now(project, until: datetime) -> int:
    run_id = db.create_run(project["id"], "build", "opus")
    db.set_run_session(run_id, "s-1")
    db.set_run_start_head(run_id, "abc123")
    limitpause.pause(project, run_id, "build", _limited(), until, "test")
    return run_id


@pytest.mark.asyncio
async def test_resume_continues_the_session_on_the_same_row(project, monkeypatch):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    runlog.RunLog(run_id).append(["> Bash(before the pause)"])
    seen = _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="s-2", cost_usd=2.0, num_turns=9, result_text="done",
    ))

    assert worker.resume_run(db.get_run(run_id)) is True
    assert db.get_run(run_id)["status"] == "running"
    await worker._inflight[run_id]

    assert seen["resume_session"] == "s-1"
    assert "Resumed after the usage window" in seen["prompt"]
    assert "do not start over" in seen["prompt"]
    run = db.get_run(run_id)
    assert run["status"] == "ok"
    # Same row, cost and turns summed across both segments.
    assert run["cost_usd"] == pytest.approx(11.3 + 2.0)
    assert run["num_turns"] == 71 + 9
    # The forked session is the one on the row now.
    assert run["session_id"] == "s-2"
    # The log kept what came before the pause.
    text, _ = runlog.read_log(run_id)
    assert "before the pause" in text
    # Timeline: paused, then resumed.
    assert [e["decision"] for e in db.midrun_events_for_run(run_id)] == \
        [limitpause.PAUSED, limitpause.RESUMED]
    assert any("resumed after" in b for b in _journal(project["id"]))


@pytest.mark.asyncio
async def test_the_resumed_segment_keeps_the_original_start_head(project, monkeypatch):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    heads = {}
    monkeypatch.setattr(worker, "_record_workspace_heads",
                        lambda p, rid, before, repo=None: heads.setdefault(rid, before))
    _fake_run(monkeypatch, agent_runner.RunResult(ok=True, session_id="s-2"))
    worker.resume_run(db.get_run(run_id))
    await worker._inflight[run_id]
    assert heads[run_id] == "abc123"


@pytest.mark.asyncio
async def test_the_live_logger_forces_the_forked_session_id_on_resume(project):
    run_id = _pause_now(project, datetime.now(timezone.utc))
    on_event = worker._live_logger(run_id, fresh=False)
    on_event({"type": "system", "subtype": "init", "session_id": "s-2"}, ["init"])
    assert db.get_run(run_id)["session_id"] == "s-2"
    # A fresh run's logger keeps the first id it saw, as before.
    fresh_id = db.create_run(project["id"], "build", "opus")
    db.set_run_session(fresh_id, "first")
    worker._live_logger(fresh_id)({"type": "system", "subtype": "init", "session_id": "second"}, [])
    assert db.get_run(fresh_id)["session_id"] == "first"


@pytest.mark.asyncio
async def test_the_tick_wakes_a_due_run_and_leaves_one_that_is_not(project, monkeypatch):
    other = db.create_project("Other", description="", stage="active", build_approved=True, slug="other")
    due = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    later = _pause_now(other, datetime.now(timezone.utc) + timedelta(hours=1))
    _fake_run(monkeypatch, agent_runner.RunResult(ok=True, session_id="s-2"))
    await worker._resume_paused_runs()
    assert db.get_run(due)["status"] == "running"
    assert db.get_run(later)["status"] == "paused"
    await worker._inflight[due]
    assert db.get_run(due)["status"] == "ok"


@pytest.mark.asyncio
async def test_the_tick_does_not_wake_anything_while_backing_off(project, monkeypatch):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    db.set_setting("backoff_until", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    called = []

    async def spy():
        called.append(True)

    monkeypatch.setattr(worker, "_resume_paused_runs", spy)
    for name in ("_reap_inflight", "_reap_adopted", "_daily_audit_prune"):
        monkeypatch.setattr(worker, name, lambda: None)
    for name in ("_sweep_strays", "_daily_model_check", "_publish_mirror", "_maybe_spend_down",
                 "_maybe_reflect", "_maybe_compact"):
        async def noop(*a, **k):
            return None
        monkeypatch.setattr(worker, name, noop)
    await worker._tick()
    assert called == []
    assert db.get_run(run_id)["status"] == "paused"
    db.set_setting("backoff_until", "")
    await worker._tick()
    assert called == [True]


def test_resume_respects_the_parallel_cap(project, monkeypatch):
    _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    monkeypatch.setattr(worker.pacing, "parallel_cap", lambda n: 0)
    resumed = []
    monkeypatch.setattr(worker, "resume_run", lambda row: resumed.append(row["id"]))
    import asyncio
    asyncio.run(worker._resume_paused_runs())
    assert resumed == []


def test_a_leased_workspace_keeps_the_run_paused(project, monkeypatch):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    monkeypatch.setattr(worker, "workspace_leased", lambda slug: True)
    assert worker.resume_run(db.get_run(run_id)) is False
    assert db.get_run(run_id)["status"] == "paused"


@pytest.mark.asyncio
async def test_a_paused_row_without_a_session_starts_over_rather_than_erroring(project, monkeypatch):
    """A row paused before the mode field existed, or by anything that never
    recorded a session: judged on the row itself, it can only start over."""
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "paused", None, 1.0, 3, "paused")
    row = db.get_run(run_id)
    assert limitpause.hold_mode(row) == limitpause.RESTART
    _fake_run(monkeypatch, agent_runner.RunResult(ok=True, session_id="s-2"))
    assert worker.resume_run(row) is True
    assert db.get_run(run_id)["status"] == "running"
    await worker._inflight[run_id]
    assert db.get_run(run_id)["status"] == "ok"


def test_a_session_on_the_row_beats_a_restart_mode_written_beside_it(project):
    """And the other way round is not symmetrical: a row that says RESTART
    starts over even though it has a session, because that is what the
    failure decided - but a row with no session can never claim RESUME."""
    run_id = db.create_run(project["id"], "build", "opus")
    db.set_run_session(run_id, "s-1")
    db.finish_run(run_id, "paused", "s-1", 1.0, 3, "paused")
    db.set_run_scope_record(run_id, "hold_state", json.dumps({"mode": limitpause.RESTART}))
    assert limitpause.hold_mode(db.get_run(run_id)) == limitpause.RESTART
    db.set_run_scope_record(run_id, "hold_state", json.dumps({"mode": "nonsense"}))
    assert limitpause.hold_mode(db.get_run(run_id)) == limitpause.RESUME


def test_reopen_is_one_shot(project):
    run_id = _pause_now(project, datetime.now(timezone.utc))
    assert db.reopen_run(run_id) is True
    assert db.reopen_run(run_id) is False
    assert db.get_run(run_id)["ended_at"] is None


def test_add_run_cost_keeps_null_null(project):
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok", "s", None, None, "")
    db.add_run_cost(run_id, 3.0, 4)
    run = db.get_run(run_id)
    assert run["cost_usd"] is None and run["num_turns"] is None
    db.finish_run(run_id, "ok", "s", 1.5, 2, "")
    db.add_run_cost(run_id, 3.0, 4)
    run = db.get_run(run_id)
    assert run["cost_usd"] == pytest.approx(4.5) and run["num_turns"] == 6


@pytest.mark.asyncio
async def test_a_lost_session_on_resume_is_an_error_that_says_so(project, monkeypatch):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=False, result_text="No conversation found with session ID s-1",
    ))
    worker.resume_run(db.get_run(run_id))
    await worker._inflight[run_id]
    run = db.get_run(run_id)
    assert run["status"] == "error"
    assert "could not be resumed" in run["summary"]
    assert any("no longer has the session" in b for b in _journal(project["id"]))


# --- bounded ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_limited_again_is_paused_again_with_the_new_session(project, monkeypatch):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    _fake_run(monkeypatch, _limited(session="s-2", cost=1.0, turns=4))
    worker.resume_run(db.get_run(run_id))
    await worker._inflight[run_id]
    run = db.get_run(run_id)
    assert run["status"] == "paused"
    assert run["session_id"] == "s-2"
    assert run["cost_usd"] == pytest.approx(11.3 + 1.0)
    assert run["num_turns"] == 75
    assert limitpause.resumes_so_far(run_id) == 1


@pytest.mark.asyncio
async def test_after_max_resumes_the_next_limit_is_an_error(project, monkeypatch):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    # Two earlier wakes on record; this resume is the third and last allowed.
    for _ in range(limitpause.MAX_RESUMES - 1):
        db.add_hook_event(run_id, "midrun", "limit", limitpause.RESUMED, "woken")
    _fake_run(monkeypatch, _limited(session="s-9"))
    worker.resume_run(db.get_run(run_id))
    await worker._inflight[run_id]
    run = db.get_run(run_id)
    assert run["status"] == "error"
    assert "Rate limited" in run["summary"]
    assert any(e["decision"] == limitpause.GAVE_UP for e in db.midrun_events_for_run(run_id))


@pytest.mark.asyncio
async def test_one_fewer_than_the_cap_still_pauses(project, monkeypatch):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    # One earlier wake on record; this resume is the second of three.
    for _ in range(limitpause.MAX_RESUMES - 2):
        db.add_hook_event(run_id, "midrun", "limit", limitpause.RESUMED, "woken")
    _fake_run(monkeypatch, _limited(session="s-9"))
    worker.resume_run(db.get_run(run_id))
    await worker._inflight[run_id]
    assert db.get_run(run_id)["status"] == "paused"


def test_reset_moment_prefers_headers_then_the_hint_then_the_fallback():
    now = datetime.now(timezone.utc)
    header = apiretry.Retry(category=apiretry.QUOTA, resets_at=now + timedelta(days=3))
    hint = now + timedelta(hours=14)
    fallback = now + timedelta(hours=6)
    assert limitpause.reset_moment(header, hint, fallback) == header.resets_at
    assert limitpause.reset_moment(None, hint, fallback) == hint
    assert limitpause.reset_moment(None, None, fallback) == fallback
    stale = apiretry.Retry(category=apiretry.QUOTA, resets_at=now - timedelta(minutes=1))
    assert limitpause.reset_moment(stale, now - timedelta(minutes=1), fallback) == fallback


@pytest.mark.asyncio
async def test_a_paused_run_waits_for_the_named_reset_even_past_the_backoff_cap(project, monkeypatch):
    far = datetime.now(timezone.utc) + timedelta(days=2)
    text = far.strftime("You've hit your weekly limit · resets %b %d at %I:%M%p (UTC)")
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=False, is_rate_limited=True, result_text=text, session_id="s-1", cost_usd=1.0, num_turns=5,
    ))
    await worker.run_project_task(project, "build")
    run = db.list_runs(project["id"])[0]
    assert run["status"] == "paused"
    assert limitpause.resume_after(run) == far.replace(second=0, microsecond=0)
    assert datetime.fromisoformat(db.get_setting("backoff_until")) <= \
        datetime.now(timezone.utc) + limits.MAX_BACKOFF


@pytest.mark.asyncio
async def test_resume_records_the_model_it_actually_runs_on(project, monkeypatch):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    monkeypatch.setattr(agent_runner, "resolve_model", lambda p, t: "sonnet")
    started = {}

    async def fake_execute(project, task, rid, model, **kwargs):
        started.update(model=model, **kwargs)

    monkeypatch.setattr(worker, "_execute_run", fake_execute)
    assert worker.resume_run(db.get_run(run_id)) is True
    await worker._inflight[run_id]
    assert started["model"] == "sonnet"
    assert started["resume_session"] == "s-1"
    assert db.get_run(run_id)["model"] == "sonnet"


# --- a person can still stop it ----------------------------------------------


def test_cancel_settles_a_paused_run(project):
    run_id = _pause_now(project, datetime.now(timezone.utc) + timedelta(hours=1))
    assert worker.cancel_run(run_id) == "cancelled"
    run = db.get_run(run_id)
    assert run["status"] == "cancelled"
    assert "paused" in run["summary"]
    assert limitpause.due(datetime.now(timezone.utc) + timedelta(days=1)) == []
    assert project["id"] not in db.busy_project_ids()


def test_the_run_page_says_paused_and_offers_stop(project):
    from app.main import app

    run_id = _pause_now(project, datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc))
    with TestClient(app) as client:
        page = client.get(f"/run/{run_id}").text
    assert 'id="limit-hold"' in page
    assert "Paused, not stopped" in page
    assert "19:30 UTC" in page
    assert f'action="/run/{run_id}/cancel"' in page
    assert "run-status-paused" in page


def test_the_run_page_for_an_ordinary_run_has_no_hold(project):
    from app.main import app

    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok", "s", 1.0, 2, "fine")
    with TestClient(app) as client:
        page = client.get(f"/run/{run_id}").text
    assert 'id="limit-hold"' not in page


# --- the real stream path ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_cli_refusal_on_the_stream_sets_is_rate_limited(tmp_path, monkeypatch):
    import os
    import stat
    import textwrap

    bindir = tmp_path / "bin"
    bindir.mkdir()
    events = [
        {"type": "system", "subtype": "init", "session_id": "s-1"},
        {"type": "result", "is_error": True, "result": CLI_TEXT, "session_id": "s-1",
         "num_turns": 71, "total_cost_usd": 11.3},
    ]
    body = "\n".join(json.dumps(e) for e in events)
    script = bindir / "claude"
    script.write_text("#!/bin/sh\n" + textwrap.dedent(f"cat <<'PORTAL_EOF'\n{body}\nPORTAL_EOF\n"))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    monkeypatch.setattr(agent_runner, "_extra_env", lambda: dict(env))

    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.is_rate_limited is True
    assert limitpause.can_pause(result) is True


# --- the corners -------------------------------------------------------------


def test_a_date_already_past_in_late_december_is_next_year():
    late = datetime(2026, 12, 30, 12, 0, tzinfo=timezone.utc)
    assert apiretry.parse_reset_hint("resets Jan 2 at 4am (UTC)", late) == \
        datetime(2027, 1, 2, 4, 0, tzinfo=timezone.utc)
    # A day or two back is still this year - the clock skew of a slow read.
    assert apiretry.parse_reset_hint("resets Dec 29 at 4am (UTC)", late) == \
        datetime(2026, 12, 29, 4, 0, tzinfo=timezone.utc)


def test_a_paused_parallel_branch_is_not_merged_while_it_waits(project, monkeypatch):
    run_id = db.create_run(project["id"], "build", "opus", parallel=True)
    db.finish_run(run_id, "paused", "s-1", 1.0, 2, "paused")
    drained = []
    monkeypatch.setattr(worker.parallel_runs, "pending", lambda slug: [run_id])
    monkeypatch.setattr(worker.parallel_runs, "drain",
                        lambda slug, running, run_ids: drained.extend(run_ids) or [])
    worker.merge_parallel_work(project)
    assert drained == []
    # Once it has settled, the same branch is merged as any other.
    db.finish_run(run_id, "ok", "s-2", 2.0, 3, "done")
    worker.merge_parallel_work(project)
    assert drained == [run_id]


@pytest.mark.asyncio
async def test_a_resumed_parallel_run_goes_back_into_its_own_worktree(project, monkeypatch, tmp_path):
    run_id = db.create_run(project["id"], "build", "opus", parallel=True)
    db.set_run_session(run_id, "s-1")
    limitpause.pause(project, run_id, "build", _limited(), datetime.now(timezone.utc), "test")
    tree = tmp_path / "worktree"
    tree.mkdir()
    monkeypatch.setattr(worker.parallel_runs, "worktree_for", lambda slug, rid: tree)

    def refuse(slug, rid):
        raise AssertionError("open_worktree would delete the paused run's edits")

    monkeypatch.setattr(worker.parallel_runs, "open_worktree", refuse)
    monkeypatch.setattr(worker.parallel_runs, "prompt_section", lambda *a, **k: "")
    seen = _fake_run(monkeypatch, agent_runner.RunResult(ok=True, session_id="s-2"))
    worker.resume_run(db.get_run(run_id))
    await worker._inflight[run_id]
    assert seen["cwd"] == tree
    assert db.get_run(run_id)["status"] == "ok"


# --- a one-off task gets the same hold ---------------------------------------
#
# Wes, 2026-09-19, of a run that "never starts because it is waiting for usage
# limit to refresh": have it start working again once the window rolls over.
# A one-off is the case that needed this most - he starts one by hand and
# nothing else ever picks one up - and it is the one the first pass left out,
# because `resume_run` wanted a project_id.


@pytest.fixture
def task():
    return db.create_oneoff("Fix the cron mail on testhost\n\nIt stopped on Sunday.")


def _oneoff_messages(task_id: int) -> list[str]:
    return [m["content_md"] for m in db.list_oneoff_messages(task_id)]


def _pending(task_id: int) -> list[str]:
    return [m["content_md"] for m in db.pending_oneoff_messages(task_id)]


async def _run_oneoff(task_id: int, result, monkeypatch) -> tuple[int, dict]:
    seen = _fake_run(monkeypatch, result)
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task_id)
    await worker.run_oneoff_task(task_id, run_id, "opus")
    return run_id, seen


@pytest.mark.asyncio
async def test_a_one_off_refused_before_it_started_is_paused_and_keeps_the_message(
    task, monkeypatch
):
    run_id, _ = await _run_oneoff(task["id"], _limited(session=None, cost=0.0, turns=1), monkeypatch)
    run = db.get_run(run_id)
    assert run["status"] == "paused"
    assert limitpause.hold_mode(run) == limitpause.RESTART
    # The message went back in the queue: nobody read it, and the restart is
    # what will.
    assert _pending(task["id"]) == ["Fix the cron mail on testhost\n\nIt stopped on Sunday."]
    assert "queued, not lost" in _oneoff_messages(task["id"])[-1]


@pytest.mark.asyncio
async def test_a_one_off_refused_mid_run_keeps_its_session_and_its_delivered_message(
    task, monkeypatch
):
    run_id, _ = await _run_oneoff(task["id"], _limited(), monkeypatch)
    run = db.get_run(run_id)
    assert run["status"] == "paused"
    assert limitpause.hold_mode(run) == limitpause.RESUME
    assert run["session_id"] == "s-1"
    # The agent read it, so it stays spent - delivering it again would have
    # the resumed session answer the same message twice.
    assert _pending(task["id"]) == []
    assert "paused, not stopped" in _oneoff_messages(task["id"])[-1]


@pytest.mark.asyncio
async def test_the_restart_of_a_one_off_reads_the_message_for_the_first_time(task, monkeypatch):
    run_id, _ = await _run_oneoff(task["id"], _limited(session=None, cost=0.0, turns=1), monkeypatch)
    db.set_run_scope_record(run_id, "hold_state", json.dumps({
        "limit_until": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "why": "test", "mode": limitpause.RESTART,
    }))
    seen = _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="s-9", cost_usd=2.0, num_turns=9, result_text="mail is flowing",
    ))

    assert worker.resume_run(db.get_run(run_id)) is True
    await worker._inflight[run_id]

    assert seen.get("resume_session") is None
    assert "It stopped on Sunday." in seen["prompt"]
    assert "Resumed after the usage window" not in seen["prompt"]
    run = db.get_run(run_id)
    assert run["status"] == "ok" and run["id"] == run_id
    assert _pending(task["id"]) == []
    assert _oneoff_messages(task["id"])[-1] == "mail is flowing"
    assert any("window has reopened" in m for m in _oneoff_messages(task["id"]))
    assert limitpause.resumes_so_far(run_id) == 1


def _pause_oneoff_now(task_id: int, until: datetime, session: str | None = "s-1") -> int:
    """A run that took the queue on its way in, as every real one does, and
    was then refused for the usage window."""
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task_id)
    delivered = [m["id"] for m in db.pending_oneoff_messages(task_id)]
    db.mark_oneoff_delivered(delivered)
    if session:
        db.set_run_session(run_id, session)
    limitpause.pause_oneoff(task_id, run_id, _limited(session=session), until, "test",
                            delivered_ids=delivered)
    return run_id


@pytest.mark.asyncio
async def test_the_resumed_one_off_continues_its_session_on_the_same_row(task, monkeypatch):
    run_id = _pause_oneoff_now(task["id"], datetime.now(timezone.utc) - timedelta(minutes=1))
    runlog.RunLog(run_id).append(["> Bash(before the pause)"])
    seen = _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="s-2", cost_usd=2.0, num_turns=9, result_text="done, and here is why",
    ))

    assert worker.resume_run(db.get_run(run_id)) is True
    await worker._inflight[run_id]

    assert seen["resume_session"] == "s-1"
    assert "Resumed after the usage window" in seen["prompt"]
    # The one-off wake, not the project one: there is no report file here, and
    # what it prints is the reply.
    assert "StructuredOutput" not in seen["prompt"]
    assert "your reply on the task page" in seen["prompt"]
    run = db.get_run(run_id)
    assert run["status"] == "ok"
    assert run["cost_usd"] == pytest.approx(11.3 + 2.0)
    assert run["num_turns"] == 71 + 9
    assert _oneoff_messages(task["id"])[-1] == "done, and here is why"
    # The thread, not a journal, is where this task's person is told it woke.
    assert any("resumed after" in m for m in _oneoff_messages(task["id"]))
    # One row, one log: the turns from before the pause are still in it.
    text, _ = runlog.read_log(run_id)
    assert "before the pause" in text
    assert [e["decision"] for e in db.midrun_events_for_run(run_id)] == \
        [limitpause.PAUSED, limitpause.RESUMED]
    # The forked session is the task's session now, so the next message lands
    # in the conversation that actually has the work in it.
    assert db.get_oneoff(task["id"])["cli_session_id"] == "s-2"


def test_a_paused_one_off_holds_its_task(task, monkeypatch):
    _pause_oneoff_now(task["id"], datetime.now(timezone.utc) + timedelta(hours=1))
    assert db.oneoff_busy(task["id"]) is True
    assert db.oneoff_running(task["id"]) is False
    # A message typed during the hold does not start a second agent in the
    # same workspace: it waits, exactly as it waits for a run that is mid-turn.
    db.add_oneoff_message(task["id"], "wes", "one more thing")
    assert worker.spawn_oneoff(task["id"]) is None
    assert _pending(task["id"]) == ["one more thing"]


@pytest.mark.asyncio
async def test_a_message_typed_during_the_hold_runs_once_the_woken_run_settles(task, monkeypatch):
    run_id = _pause_oneoff_now(task["id"], datetime.now(timezone.utc) - timedelta(minutes=1))
    db.add_oneoff_message(task["id"], "wes", "one more thing")
    spawned: list[int] = []
    monkeypatch.setattr(worker, "spawn_oneoff", lambda tid: spawned.append(tid))
    seen = _fake_run(monkeypatch, agent_runner.RunResult(ok=True, session_id="s-2", result_text="ok"))

    worker.resume_run(db.get_run(run_id))
    await worker._inflight[run_id]

    # The resumed session is handed the wake, not the new message - that one
    # is still queued, and starts the next run now that this one has settled.
    assert "one more thing" not in seen["prompt"]
    assert _pending(task["id"]) == ["one more thing"]
    assert spawned == [task["id"]]


@pytest.mark.asyncio
async def test_a_one_off_woken_too_often_settles_as_the_error_it_used_to_be(task, monkeypatch):
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    for _ in range(limitpause.MAX_RESUMES):
        db.add_hook_event(run_id, "midrun", "limit", limitpause.RESUMED, "woken")
    _fake_run(monkeypatch, _limited())
    await worker.run_oneoff_task(task["id"], run_id, "opus")
    run = db.get_run(run_id)
    assert run["status"] == "error"
    assert "Rate limited" in run["summary"]
    assert "Send your message again" in _oneoff_messages(task["id"])[-1]


def test_a_paused_one_off_whose_task_is_gone_settles_rather_than_waiting_forever(task):
    run_id = _pause_oneoff_now(task["id"], datetime.now(timezone.utc) - timedelta(minutes=1))
    db.set_oneoff_status(task["id"], "archived")
    assert worker.resume_run(db.get_run(run_id)) is False
    run = db.get_run(run_id)
    assert run["status"] == "error"
    assert "archived" in run["summary"]


def test_a_busy_task_workspace_keeps_the_one_off_paused(task, monkeypatch):
    run_id = _pause_oneoff_now(task["id"], datetime.now(timezone.utc) - timedelta(minutes=1))
    monkeypatch.setattr(worker.worklock, "is_busy", lambda ws: True)
    assert worker.resume_oneoff_run(db.get_run(run_id)) is False
    assert db.get_run(run_id)["status"] == "paused"


@pytest.mark.asyncio
async def test_the_tick_wakes_a_due_one_off(task, monkeypatch):
    run_id = _pause_oneoff_now(task["id"], datetime.now(timezone.utc) - timedelta(minutes=1))
    _fake_run(monkeypatch, agent_runner.RunResult(ok=True, session_id="s-2", result_text="ok"))
    await worker._resume_paused_runs()
    assert db.get_run(run_id)["status"] == "running"
    await worker._inflight[run_id]
    assert db.get_run(run_id)["status"] == "ok"


def test_stopping_a_paused_one_off_says_so_in_the_thread(task):
    run_id = _pause_oneoff_now(task["id"], datetime.now(timezone.utc) + timedelta(hours=1))
    assert worker.cancel_run(run_id) == "cancelled"
    assert db.get_run(run_id)["status"] == "cancelled"
    assert "will not come back" in _oneoff_messages(task["id"])[-1]
    assert db.oneoff_busy(task["id"]) is False


def test_undelivering_touches_only_the_ids_it_is_given(task):
    db.add_oneoff_message(task["id"], "wes", "second")
    ids = [m["id"] for m in db.pending_oneoff_messages(task["id"])]
    db.mark_oneoff_delivered(ids)
    assert _pending(task["id"]) == []
    db.undeliver_oneoff_messages([])
    assert _pending(task["id"]) == []
    db.undeliver_oneoff_messages(ids[:1])
    assert _pending(task["id"]) == ["Fix the cron mail on testhost\n\nIt stopped on Sunday."]


def test_the_task_page_says_it_is_held_and_offers_stop(task):
    from app.main import app

    run_id = _pause_oneoff_now(task["id"], datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc))
    with TestClient(app) as client:
        page = client.get(f"/tasks/{task['id']}").text
        listing = client.get("/tasks").text
    assert 'id="oneoff-hold"' in page
    assert "Paused, not stopped" in page
    assert "19:30 UTC" in page
    assert f'action="/run/{run_id}/cancel"' in page
    assert "held for the usage window" in listing


def test_the_task_page_of_an_ordinary_task_has_no_hold(task):
    from app.main import app

    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    db.finish_run(run_id, "ok", "s", 1.0, 2, "fine")
    with TestClient(app) as client:
        assert 'id="oneoff-hold"' not in client.get(f"/tasks/{task['id']}").text


# --- resuming a system pause by hand -----------------------------------------
# Wes, 2026-09-19 (dictated): "Please resume all of the currently paused tasks
# and tell the project portal to add a feature that enables resuming runs that
# were paused by the system rather than me as well." A run HE paused has had a
# resume button since app/midrun.py; a run the usage window paused had only
# "stop this run" and a sentence about when it would come back on its own.


def test_a_run_inside_its_window_cannot_be_woken_and_says_why(project):
    """Waking a run into a spent allowance parks it again a few cents later,
    so the button is off - with the reason, and with the hour on it."""
    until = datetime.now(timezone.utc) + timedelta(hours=2)
    run_id = _pause_now(project, until)
    blocked = limitpause.wake_blocked(db.get_run(run_id))
    assert "still shut" in blocked
    assert limitpause._fmt(until) in blocked
    # And nothing happens if the route is posted anyway.
    assert "still shut" in worker.resume_now(run_id, by="Wes")
    assert db.get_run(run_id)["status"] == limitpause.STATUS


def test_a_run_whose_window_has_reopened_can_be_woken(project):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    assert limitpause.wake_blocked(db.get_run(run_id)) == ""


def test_the_button_and_the_tick_agree_at_the_exact_boundary(project):
    """`due` wakes a run at `until <= now`, so the button must be live at
    `until == now` too. Off by one instant in the other direction and a run
    the tick is about to pick up shows a grayed-out button explaining that it
    cannot be resumed yet."""
    # Whole seconds: the hold record is written with timespec="seconds", so
    # a sub-second offset is not a boundary the stored value even has.
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
    run_id = _pause_now(project, until)
    row = db.get_run(run_id)
    assert [r["id"] for r in limitpause.due(until)] == [run_id]
    assert limitpause.wake_blocked(row, now=until) == ""
    just_before = until - timedelta(seconds=1)
    assert limitpause.due(just_before) == []
    assert limitpause.wake_blocked(row, now=just_before) != ""


def test_a_run_that_is_not_paused_cannot_be_woken(project):
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok", "s-1", 1.0, 2, "done")
    assert "not paused" in limitpause.wake_blocked(db.get_run(run_id))
    assert "not paused" in worker.resume_now(run_id, by="Wes")
    assert "gone" in worker.resume_now(9999, by="Wes")


@pytest.mark.asyncio
async def test_resume_now_puts_the_run_back_in_flight(project, monkeypatch):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    seen = _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="s-2", cost_usd=2.0, num_turns=9, result_text="done",
    ))

    assert worker.resume_now(run_id, by="Wes") == ""
    assert db.get_run(run_id)["status"] == "running"
    await worker._inflight[run_id]

    assert seen["resume_session"] == "s-1"
    assert db.get_run(run_id)["status"] == "ok"


@pytest.mark.asyncio
async def test_a_hand_resume_does_not_count_against_the_cap(project, monkeypatch):
    """The cap stops the PORTAL waking a run into the same shut window
    forever. A person pressing a button is not a loop, so the wake is written
    under its own decision and `resumes_so_far` does not see it."""
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="s-2", cost_usd=1.0, num_turns=2, result_text="done",
    ))

    assert worker.resume_now(run_id, by="Wes") == ""
    await worker._inflight[run_id]

    assert limitpause.resumes_so_far(run_id) == 0
    decisions = [e["decision"] for e in db.midrun_events_for_run(run_id)]
    assert decisions == [limitpause.PAUSED, limitpause.HAND_RESUMED]
    assert any("resumed by Wes" in b for b in _journal(project["id"]))


@pytest.mark.asyncio
async def test_an_automatic_resume_still_counts(project, monkeypatch):
    """The other half of the same rule: the tick's own wake is unchanged."""
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="s-2", cost_usd=1.0, num_turns=2, result_text="done",
    ))

    assert worker.resume_run(db.get_run(run_id)) is True
    await worker._inflight[run_id]

    assert limitpause.resumes_so_far(run_id) == 1
    assert limitpause.RESUMED in [e["decision"] for e in db.midrun_events_for_run(run_id)]


@pytest.mark.asyncio
async def test_a_hand_restart_does_not_count_either(project, monkeypatch):
    """A run that never got going is started over by hand the same way."""
    run_id = db.create_run(project["id"], "build", "opus")
    limitpause.pause(project, run_id, "build", _limited(session=None),
                     datetime.now(timezone.utc) - timedelta(minutes=1), "test")
    seen = _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="s-9", cost_usd=1.0, num_turns=2, result_text="done",
    ))

    assert worker.resume_now(run_id, by="Karli") == ""
    await worker._inflight[run_id]

    assert seen.get("resume_session") is None
    assert limitpause.resumes_so_far(run_id) == 0
    assert [e["decision"] for e in db.midrun_events_for_run(run_id)] == \
        [limitpause.PAUSED, limitpause.HAND_RESTARTED]
    assert any("started by Karli" in b for b in _journal(project["id"]))


@pytest.mark.asyncio
async def test_resume_now_refuses_while_the_service_is_restarting(project, monkeypatch):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    monkeypatch.setattr(worker, "_restarting", True)
    assert "restarting" in worker.resume_now(run_id, by="Wes")
    assert db.get_run(run_id)["status"] == limitpause.STATUS


@pytest.mark.asyncio
async def test_resume_now_leaves_a_run_paused_when_the_workspace_is_busy(project, monkeypatch):
    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    monkeypatch.setattr(worker, "workspace_leased", lambda slug: True)
    assert "workspace is busy" in worker.resume_now(run_id, by="Wes")
    assert db.get_run(run_id)["status"] == limitpause.STATUS


@pytest.mark.asyncio
async def test_resume_all_wakes_the_due_ones_and_leaves_the_rest(monkeypatch):
    """They pause in batches - ten at once on 2026-09-19 - so the board gets
    one button. A run still inside its window is left where it is."""
    due_projects = [
        db.create_project(f"P{i}", description="x", stage="active",
                          build_approved=True, slug=f"p{i}")
        for i in range(3)
    ]
    due_ids = [
        _pause_now(p, datetime.now(timezone.utc) - timedelta(minutes=1))
        for p in due_projects[:2]
    ]
    not_due = _pause_now(due_projects[2], datetime.now(timezone.utc) + timedelta(hours=1))
    # The cap is lifted out of the way deliberately: at the test database's
    # default it alone would have held the third run back, and the sweep
    # caught exactly that - the window guard could be deleted and this test
    # still passed.
    monkeypatch.setattr(worker.pacing, "parallel_cap", lambda n: 99)
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="s-2", cost_usd=1.0, num_turns=2, result_text="done",
    ))

    woken, left = worker.resume_all_paused(by="Wes")
    assert (woken, left) == (2, 1)
    for run_id in due_ids:
        await worker._inflight[run_id]
        assert db.get_run(run_id)["status"] == "ok"
    assert db.get_run(not_due)["status"] == limitpause.STATUS


@pytest.mark.asyncio
async def test_resume_all_respects_the_parallel_cap(monkeypatch):
    """Ten CLI processes at once is not what anybody means by "resume them";
    the ticks that follow take the rest."""
    projects = [
        db.create_project(f"Q{i}", description="x", stage="active",
                          build_approved=True, slug=f"q{i}")
        for i in range(3)
    ]
    for p in projects:
        _pause_now(p, datetime.now(timezone.utc) - timedelta(minutes=1))
    monkeypatch.setattr(worker.pacing, "parallel_cap", lambda n: 1)
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="s-2", cost_usd=1.0, num_turns=2, result_text="done",
    ))

    woken, left = worker.resume_all_paused(by="Wes")
    assert (woken, left) == (1, 2)
    for handle in list(worker._inflight.values()):
        await handle


def test_the_run_page_offers_resume_now_once_the_window_has_reopened(project):
    from app.main import app

    run_id = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    with TestClient(app) as client:
        page = client.get(f"/run/{run_id}").text
    assert f'action="/run/{run_id}/wake"' in page
    assert 'id="wake-ready"' in page
    assert 'id="wake-blocked"' not in page


def test_the_run_page_grays_resume_now_out_with_the_reason(project):
    """Never removed, never silently dead: the disabled button is on the page
    and the sentence under it says what it is waiting for."""
    from app.main import app

    until = datetime.now(timezone.utc) + timedelta(hours=2)
    run_id = _pause_now(project, until)
    with TestClient(app) as client:
        page = client.get(f"/run/{run_id}").text
    assert 'id="wake-run" disabled' in page
    assert f'action="/run/{run_id}/wake"' not in page
    assert limitpause._fmt(until) in page
    assert 'id="wake-blocked"' in page


def test_posting_the_wake_route_too_early_says_so_on_the_page(project):
    from app.main import app

    run_id = _pause_now(project, datetime.now(timezone.utc) + timedelta(hours=2))
    with TestClient(app) as client:
        page = client.post(f"/run/{run_id}/wake", data={"next": f"/run/{run_id}"})
    assert page.status_code == 200
    assert 'id="wake-error"' in page.text
    assert "still shut" in page.text
    assert db.get_run(run_id)["status"] == limitpause.STATUS


def test_the_activity_page_lists_held_runs_and_offers_one_button(project, monkeypatch):
    from app.main import app

    ready = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    other = db.create_project("Other", description="x", stage="active",
                              build_approved=True, slug="other")
    waiting = _pause_now(other, datetime.now(timezone.utc) + timedelta(hours=2))
    with TestClient(app) as client:
        page = client.get("/activity").text
    assert 'id="paused-runs"' in page
    assert f'href="/run/{ready}"' in page
    assert f'href="/run/{waiting}"' in page
    assert 'action="/runs/wake-paused"' in page
    assert "resume 1 now" in page


def test_the_activity_button_is_grayed_when_nothing_is_due(project):
    from app.main import app

    _pause_now(project, datetime.now(timezone.utc) + timedelta(hours=2))
    with TestClient(app) as client:
        page = client.get("/activity").text
    assert 'id="wake-all" disabled' in page
    assert 'action="/runs/wake-paused"' not in page


def test_the_activity_page_hides_a_held_run_on_someone_elses_project(project):
    """The hold card is filtered by the same membership rule the rest of the
    board is: a run held on a project you are not on is not yours to see, let
    alone to wake."""
    from app import people
    from app.main import app

    karli = people.add(name="Karli")
    hers = db.create_project("Hers", description="x", stage="active",
                             build_approved=True, slug="hers")
    people.set_members(hers["id"], [karli])
    mine = _pause_now(project, datetime.now(timezone.utc) - timedelta(minutes=1))
    theirs = _pause_now(hers, datetime.now(timezone.utc) - timedelta(minutes=1))

    with TestClient(app) as client:
        page = client.get("/activity").text
    assert f'href="/run/{mine}"' in page
    assert f'href="/run/{theirs}"' not in page
    assert "resume 1 now" in page


def test_the_activity_page_has_no_hold_card_when_nothing_is_paused(project):
    from app.main import app

    with TestClient(app) as client:
        assert 'id="paused-runs"' not in client.get("/activity").text


# --- and a one-off task's held run wakes by hand too --------------------------


@pytest.mark.asyncio
async def test_a_held_one_off_run_can_be_woken_by_hand(task, monkeypatch):
    """`resume_now` goes through `resume_run`, which dispatches a row with an
    `oneoff_id` to `resume_oneoff_run` - so the button works on the kind of
    run a person started by hand, which is the kind somebody is most likely
    to be sitting there waiting for."""
    run_id = _pause_oneoff_now(task["id"], datetime.now(timezone.utc) - timedelta(minutes=1))
    seen = _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="s-2", cost_usd=1.0, num_turns=2, result_text="here you go",
    ))

    assert worker.resume_now(run_id, by="Wes") == ""
    await worker._inflight[run_id]

    assert seen["resume_session"] == "s-1"
    assert db.get_run(run_id)["status"] == "ok"
    assert limitpause.resumes_so_far(run_id) == 0
    assert [e["decision"] for e in db.midrun_events_for_run(run_id)] == \
        [limitpause.PAUSED, limitpause.HAND_RESUMED]
    # The notice went to the thread the person is reading, not a journal.
    assert any("resumed by Wes" in m for m in _oneoff_messages(task["id"]))


def test_the_task_page_offers_resume_now_once_the_window_has_reopened(task):
    from app.main import app

    run_id = _pause_oneoff_now(task["id"], datetime.now(timezone.utc) - timedelta(minutes=1))
    with TestClient(app) as client:
        page = client.get(f"/tasks/{task['id']}").text
    assert f'action="/run/{run_id}/wake"' in page
    assert 'id="oneoff-wake" disabled' not in page


def test_the_task_page_grays_resume_now_out_while_the_window_is_shut(task):
    from app.main import app

    run_id = _pause_oneoff_now(task["id"], datetime.now(timezone.utc) + timedelta(hours=2))
    with TestClient(app) as client:
        page = client.get(f"/tasks/{task['id']}").text
    assert 'id="oneoff-wake" disabled' in page
    assert f'action="/run/{run_id}/wake"' not in page

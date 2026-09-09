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
async def test_a_limited_run_without_a_session_stays_an_error(project, monkeypatch):
    _fake_run(monkeypatch, _limited(session=None, cost=0.0, turns=1))
    await worker.run_project_task(project, "build")
    run = db.list_runs(project["id"])[0]
    assert run["status"] == "error"
    assert "Rate limited" in run["summary"]
    assert db.get_setting("backoff_until")


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


def test_a_paused_row_without_a_session_settles_as_an_error(project):
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "paused", None, 1.0, 3, "paused")
    assert worker.resume_run(db.get_run(run_id)) is False
    assert db.get_run(run_id)["status"] == "error"
    assert any("could not be resumed" in b for b in _journal(project["id"]))


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

"""Reading the CLI's own `api_retry` events instead of guessing from prose.

The events are real: their shape is lifted verbatim from the SDK message schema
compiled into the installed CLI (2.1.223), which declares
`system`/`api_retry` as attempt/max_retries/retry_delay_ms/error_status plus an
`error` snapshot whose `rate_limits` field is documented as "Quota-429 headers
surfaced by the retry banner; null when not a quota 429".
"""
from __future__ import annotations

import os
import stat
import textwrap
from datetime import datetime, timedelta, timezone

import pytest

from app import agent_runner, apiretry, db, limits, runlog, worker


def retry_event(
    *,
    status=429,
    rate_limits=None,
    connection=None,
    is_network_down=False,
    attempt=1,
    max_retries=10,
    delay_ms=8000,
    message="Rate limit reached",
) -> dict:
    """One `api_retry` event in the shape the CLI actually emits."""
    error = {
        "message": message,
        "formatted": message,
        "connection": connection,
        "is_network_down": is_network_down,
        "rate_limits": rate_limits,
    }
    if status is not None:
        error["status"] = status
    return {
        "type": "system",
        "subtype": "api_retry",
        "attempt": attempt,
        "max_retries": max_retries,
        "retry_delay_ms": delay_ms,
        "error_status": status,
        "error": error,
        "uuid": "u-1",
        "session_id": "s-1",
    }


def in_minutes(n: int) -> datetime:
    """A moment `n` minutes from now, truncated to whole seconds - which is the
    only resolution `resets_at` has, being a unix timestamp in seconds."""
    return (datetime.now(timezone.utc) + timedelta(minutes=n)).replace(microsecond=0)


def quota_event(resets_at: datetime, *, limit_type="seven_day", **kw) -> dict:
    return retry_event(
        rate_limits={
            "resets_at": int(resets_at.timestamp()),
            "rate_limit_type": limit_type,
        },
        **kw,
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_a_quota_429_is_read_as_a_usage_limit_with_its_real_reset():
    reset = datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc)
    retry = apiretry.classify(quota_event(reset))
    assert retry.category == apiretry.QUOTA
    assert retry.resets_at == reset
    assert retry.limit_type == "seven_day"
    assert retry.attempt == 1 and retry.max_retries == 10


def test_a_429_without_quota_headers_is_a_throttle_not_an_exhausted_allowance():
    """The distinction the string match could never make. A short-term throttle
    clears in seconds; treating it as a spent allowance would hold the whole
    scheduler for hours over nothing."""
    retry = apiretry.classify(retry_event(status=429, rate_limits=None))
    assert retry.category == apiretry.THROTTLED
    assert retry.resets_at is None


@pytest.mark.parametrize(
    "status,expected",
    [
        (529, apiretry.OVERLOADED),
        (503, apiretry.OVERLOADED),
        (500, apiretry.SERVER),
        (401, apiretry.AUTH),
        (403, apiretry.AUTH),
        (400, apiretry.OTHER),
    ],
)
def test_statuses_map_to_their_categories(status, expected):
    assert apiretry.classify(retry_event(status=status)).category == expected


def test_a_dropped_connection_is_the_network_not_the_allowance():
    retry = apiretry.classify(
        retry_event(
            status=None,
            connection={"code": "ETIMEDOUT", "message": "timed out", "is_ssl_error": False},
        )
    )
    assert retry.category == apiretry.NETWORK


def test_a_missing_status_with_no_connection_detail_is_still_the_network():
    """The CLI's schema says error_status is null exactly when there was no HTTP
    response, so nothing reached their servers even if the cause chain was
    empty."""
    assert apiretry.classify(retry_event(status=None)).category == apiretry.NETWORK


def test_is_network_down_wins_over_a_status_code():
    retry = apiretry.classify(retry_event(status=500, is_network_down=True))
    assert retry.category == apiretry.NETWORK


def test_events_that_are_not_api_retries_are_ignored():
    assert apiretry.classify({"type": "system", "subtype": "init"}) is None
    assert apiretry.classify({"type": "assistant", "message": {}}) is None
    assert apiretry.classify({}) is None
    assert apiretry.classify("nonsense") is None


def test_a_garbled_event_degrades_instead_of_raising():
    """Every field here comes off somebody else's wire format. A run must not
    die because one arrived as a string."""
    retry = apiretry.classify(
        {
            "type": "system",
            "subtype": "api_retry",
            "attempt": "two",
            "retry_delay_ms": None,
            "error_status": "429",
            "error": {"rate_limits": {"resets_at": "not-a-number"}},
        }
    )
    assert retry.category == apiretry.QUOTA
    assert retry.resets_at is None  # unusable, so treated as absent
    assert retry.attempt == 0 and retry.delay_ms == 0


def test_an_absurd_reset_timestamp_is_treated_as_absent():
    retry = apiretry.classify(
        {
            "type": "system", "subtype": "api_retry", "error_status": 429,
            "error": {"rate_limits": {"resets_at": 10**20}},
        }
    )
    assert retry.category == apiretry.QUOTA
    assert retry.resets_at is None


def test_the_error_body_is_optional_entirely():
    retry = apiretry.classify(
        {"type": "system", "subtype": "api_retry", "error_status": 529}
    )
    assert retry.category == apiretry.OVERLOADED


# ---------------------------------------------------------------------------
# Accumulating across a run
# ---------------------------------------------------------------------------


def test_the_log_counts_retries_and_the_time_they_cost():
    log = apiretry.RetryLog()
    log.observe(retry_event(status=529, delay_ms=2000))
    log.observe(retry_event(status=529, delay_ms=4000))
    log.observe({"type": "assistant", "message": {}})
    assert log.count == 2
    assert log.delay_seconds == 6.0
    assert log.categories == {apiretry.OVERLOADED: 2}
    assert log.saw_quota is False
    assert "overloaded x2" in log.summary()
    assert "6s waiting" in log.summary()


def test_a_run_with_no_retries_has_nothing_to_say():
    assert apiretry.RetryLog().summary() is None


def test_the_latest_reset_wins_so_the_portal_does_not_wake_into_a_wall():
    early = in_minutes(10)
    late = in_minutes(40)
    log = apiretry.RetryLog()
    log.observe(quota_event(early))
    log.observe(quota_event(late))
    assert log.quota.resets_at == late


def test_a_later_retry_without_headers_does_not_erase_the_reset_we_had():
    """A follow-up 429 whose headers were thinner must not cost us the
    authoritative reset the first one carried."""
    reset = in_minutes(30)
    log = apiretry.RetryLog()
    log.observe(quota_event(reset))
    log.observe(retry_event(status=429, rate_limits={}))
    assert log.quota.resets_at == reset


# ---------------------------------------------------------------------------
# The live console
# ---------------------------------------------------------------------------


def test_a_retry_is_drawn_in_the_console():
    """The only output a run produces while the CLI waits out a failed call.
    Dropped, the console shows a run that has silently stopped moving."""
    lines = runlog.render_event(
        quota_event(datetime.now(timezone.utc) + timedelta(hours=1), delay_ms=12000)
    )
    assert len(lines) == 1
    # STATUS, not ERROR: the console folds errors away behind "show tool calls",
    # so an ERROR line would leave a three-minute stall reading as "3 errors" in
    # a collapsed summary - quiet again, which is what this is meant to fix.
    assert lines[0].startswith(runlog.STATUS)
    assert not lines[0].startswith(runlog.ERROR)
    assert "usage limit" in lines[0]
    assert "12s" in lines[0]
    assert "attempt 1/10" in lines[0]


def test_the_console_names_the_kind_of_failure():
    line = runlog.render_event(retry_event(status=529, rate_limits=None))[0]
    assert "overloaded" in line
    assert "HTTP 529" in line


# ---------------------------------------------------------------------------
# End to end, through a real subprocess
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)

    def install(body: str) -> None:
        script = bindir / "claude"
        script.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    monkeypatch.setattr(agent_runner, "_extra_env", lambda: dict(env))
    return install


def _emit(events: list[dict], stderr: str = "", rc: int = 0) -> str:
    import json

    lines = "\n".join(json.dumps(e) for e in events)
    body = f"cat <<'PORTAL_EOF'\n{lines}\nPORTAL_EOF\n"
    if stderr:
        body += f"echo {stderr!r} >&2\n"
    body += f"exit {rc}\n"
    return body


@pytest.mark.asyncio
async def test_a_quota_retry_makes_a_failed_run_authoritatively_rate_limited(
    tmp_path, fake_claude
):
    reset = in_minutes(25)
    fake_claude(
        _emit(
            [
                quota_event(reset),
                {"type": "result", "is_error": True, "result": "stopped"},
            ]
        )
    )
    result = await agent_runner.run_claude(
        "prompt", tmp_path / "ws", "opus", timeout_min=1
    )
    assert result.is_rate_limited is True
    # And the reset came with it, so the backoff needs no second opinion.
    assert result.retries.quota.resets_at == reset


@pytest.mark.asyncio
async def test_an_outage_is_not_mistaken_for_a_usage_limit(tmp_path, fake_claude):
    """The false positive the string match produces. `_looks_rate_limited` fires
    on any text carrying "limit" plus "rate" - so a network failure mentioning
    rate limiting used to park the entire scheduler for as long as the usage
    endpoint said the window was shut. The CLI named the cause; believe it."""
    fake_claude(
        _emit(
            [
                retry_event(status=None, is_network_down=True, message="offline"),
                {"type": "result", "is_error": True, "result": "connection failed"},
            ],
            stderr="could not reach the rate limit service",
        )
    )
    result = await agent_runner.run_claude(
        "prompt", tmp_path / "ws", "opus", timeout_min=1
    )
    assert result.ok is False
    assert result.is_rate_limited is False
    assert result.retries.categories == {apiretry.NETWORK: 1}


@pytest.mark.asyncio
async def test_with_no_retry_events_the_old_prose_match_still_decides(
    tmp_path, fake_claude
):
    """Absence proves nothing: the CLI only retries what it deems retryable, so
    a hard refusal emits no event at all and the fallback has to hold."""
    fake_claude(
        _emit([{"type": "result", "is_error": True, "result": "usage limit reached"}])
    )
    result = await agent_runner.run_claude(
        "prompt", tmp_path / "ws", "opus", timeout_min=1
    )
    assert result.is_rate_limited is True
    assert result.retries.count == 0


@pytest.mark.asyncio
async def test_a_run_that_retried_through_the_wall_and_won_still_records_it(
    tmp_path, fake_claude
):
    """The case that changes scheduling. The CLI retries internally, so this run
    finishes green - and nothing about its outcome would otherwise say that the
    allowance is spent and the next run is about to be wasted."""
    reset = datetime.now(timezone.utc) + timedelta(minutes=40)
    fake_claude(
        _emit(
            [
                quota_event(reset),
                {"type": "result", "subtype": "success", "is_error": False,
                 "result": "all done", "session_id": "s-1"},
            ]
        )
    )
    result = await agent_runner.run_claude(
        "prompt", tmp_path / "ws", "opus", timeout_min=1
    )
    assert result.ok is True
    assert result.is_rate_limited is False  # it did not die of it
    assert result.retries.saw_quota is True


@pytest.mark.asyncio
async def test_a_timed_out_run_still_reports_what_it_spent_the_time_on(
    tmp_path, fake_claude
):
    """Otherwise a run that spent its whole wall-clock budget waiting on
    somebody else's server is indistinguishable from a runaway agent."""
    import json

    events = "\n".join(json.dumps(retry_event(status=529, delay_ms=1000)) for _ in range(3))
    fake_claude(f"cat <<'PORTAL_EOF'\n{events}\nPORTAL_EOF\nsleep 30\n")
    result = await agent_runner.run_claude(
        "prompt", tmp_path / "ws", "opus", timeout_min=2 / 60
    )
    assert result.timed_out is True
    assert result.retries.count == 3


# ---------------------------------------------------------------------------
# What the scheduler does about it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_backoff_prefers_the_reset_that_came_with_the_refusal(monkeypatch):
    """Ground truth off Anthropic's own 429 headers beats a second opinion
    fetched afterwards from the usage endpoint - which is a network call that
    can itself fail, and does, on the kind of afternoon that produces a rate
    limit in the first place."""
    async def must_not_be_called(*a, **kw):
        raise AssertionError("the usage endpoint should not be consulted")

    monkeypatch.setattr(limits, "refresh_async", must_not_be_called)

    reset = datetime.now(timezone.utc) + timedelta(minutes=18)
    until, why = await worker._rate_limit_backoff(apiretry.classify(quota_event(reset)))
    assert abs((until - reset).total_seconds()) < 2
    assert "seven_day" in why
    assert worker._in_backoff() is True
    assert db.get_setting("backoff_until")


@pytest.mark.asyncio
async def test_a_weekly_reset_is_still_capped_so_the_portal_does_not_idle_for_days(
    monkeypatch
):
    async def unused(*a, **kw):
        return {}

    monkeypatch.setattr(limits, "refresh_async", unused)
    far = datetime.now(timezone.utc) + timedelta(days=4)
    until, _ = await worker._rate_limit_backoff(apiretry.classify(quota_event(far)))
    ceiling = datetime.now(timezone.utc) + limits.MAX_BACKOFF
    assert abs((until - ceiling).total_seconds()) < 5


@pytest.mark.asyncio
async def test_a_reset_already_in_the_past_falls_through_to_the_usage_meter(monkeypatch):
    """A stale header must not produce a backoff that has already expired - the
    account reading is the right answer then, exactly as before."""
    called = {}

    async def fake_refresh(*a, **kw):
        called["yes"] = True
        raise RuntimeError("no network")

    monkeypatch.setattr(limits, "refresh_async", fake_refresh)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    until, why = await worker._rate_limit_backoff(apiretry.classify(quota_event(past)))
    assert called
    assert "flat 60 min" in why
    assert until > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_no_quota_evidence_leaves_the_old_path_untouched(monkeypatch):
    async def fake_refresh(*a, **kw):
        raise RuntimeError("no network")

    monkeypatch.setattr(limits, "refresh_async", fake_refresh)
    until, why = await worker._rate_limit_backoff(
        apiretry.classify(retry_event(status=529))
    )
    assert "flat 60 min" in why
    assert until > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_a_successful_run_that_met_the_wall_holds_the_next_one_back(monkeypatch):
    """The whole point: learn from the run that got through, not from the two
    that die afterwards."""
    project = db.create_project("Wall", "meets a wall")
    project_id = project["id"]

    retries = apiretry.RetryLog()
    retries.observe(quota_event(in_minutes(30)))
    result = agent_runner.RunResult(ok=True, retries=retries)

    await worker._note_quota_wall(project, "build", result)

    assert worker._in_backoff() is True
    entries = db.list_journal(project_id)
    assert any("hit a usage limit mid-run" in e["content_md"] for e in entries)


@pytest.mark.asyncio
async def test_a_wall_that_has_since_reset_holds_nobody_back(monkeypatch):
    project = db.create_project("Past", "wall already gone")

    retries = apiretry.RetryLog()
    retries.observe(quota_event(datetime.now(timezone.utc) - timedelta(minutes=1)))

    await worker._note_quota_wall(project, "build", agent_runner.RunResult(ok=True, retries=retries))

    assert worker._in_backoff() is False


@pytest.mark.asyncio
async def test_a_rate_limited_run_does_not_back_off_twice(monkeypatch):
    """The failing path sets its own backoff and writes its own journal line;
    this one must stay out of its way or the run reads as two separate
    incidents."""
    project = db.create_project("Dup", "one incident")
    project_id = project["id"]

    retries = apiretry.RetryLog()
    retries.observe(quota_event(datetime.now(timezone.utc) + timedelta(minutes=30)))
    result = agent_runner.RunResult(ok=False, is_rate_limited=True, retries=retries)

    await worker._note_quota_wall(project, "build", result)

    assert worker._in_backoff() is False
    assert not db.list_journal(project_id)


@pytest.mark.asyncio
async def test_a_run_with_no_retry_log_at_all_is_harmless():
    """Every RunResult built by hand elsewhere in the portal - and in tests -
    leaves `retries` unset."""
    project = db.create_project("None", "no retries")
    await worker._note_quota_wall(
        project, "build", agent_runner.RunResult(ok=True)
    )
    assert worker._in_backoff() is False

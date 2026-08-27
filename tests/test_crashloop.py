"""The 128 KiB prompt ceiling, and the retry loop it drove.

Two independent failures produced the wall of red Wes screenshotted on
2026-07-27: a prompt that could not be spawned, and a scheduler that would
retry an unspawnable prompt forever. Both are pinned here, because either one
alone still hurts - a fixed ceiling with the loop intact means the next
spawn-time failure burns the day's budget just as quietly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import agent_runner, crashloop


# --- the ceiling ------------------------------------------------------------

# Linux caps a single argv string at 32 pages, separately from ARG_MAX and with
# no way to raise it. This is the number OpenJournal's 146 KB prompt crossed.
MAX_ARG_STRLEN = 32 * 4096


def test_the_prompt_is_not_in_argv():
    """The regression itself: no element of the command line is the prompt.

    Asserted as "nothing here is huge" rather than "index 2 is not the prompt",
    because the point is that the prompt cannot reach argv by any route.
    """
    cmd = agent_runner.build_cmd("opus", 400)
    assert "-p" in cmd
    # `claude -p` must be followed by a flag or nothing - never by a payload.
    after = cmd[cmd.index("-p") + 1]
    assert after.startswith("--"), f"-p is carrying a positional argument: {after[:80]!r}"


def test_build_cmd_has_no_prompt_parameter():
    """Removing it from the signature is what stops it being put back.

    A function that accepted a prompt and ignored it would read as working at
    every call site, and the failure it reintroduced would only show up on the
    one project whose context had grown past 128 KiB.
    """
    import inspect

    params = inspect.signature(agent_runner.build_cmd).parameters
    assert "prompt" not in params


def test_a_prompt_far_past_the_kernel_limit_still_builds_a_spawnable_argv():
    """The size that used to be fatal now changes nothing about the argv."""
    huge = "x" * (MAX_ARG_STRLEN * 2)
    cmd = agent_runner.build_cmd("opus", 400)
    assert all(len(arg.encode()) < MAX_ARG_STRLEN for arg in cmd)
    # And the prompt is nowhere in it.
    assert not any(huge[:1000] in arg for arg in cmd)


def test_every_real_project_prompt_would_have_fit_in_a_pipe_but_not_in_argv():
    """Documents why this was found the hard way rather than by a size check.

    The failure needed a project to cross 128 KiB. Several were within a few
    kilobytes of it at once, which is why it presented as one project breaking
    rather than as a limit anybody had designed against.
    """
    # 146245 bytes was OpenJournal's rendered build prompt on 2026-07-27.
    assert 146245 > MAX_ARG_STRLEN
    # 126104 was ProxyTable's the same morning - under the wall, barely.
    assert 126104 < MAX_ARG_STRLEN
    assert MAX_ARG_STRLEN - 126104 < 6000


@pytest.mark.asyncio
async def test_a_prompt_larger_than_a_pipe_buffer_is_delivered_whole():
    """The deadlock guard, driven against a real process.

    A pipe buffer is 64 KiB. Writing the prompt to completion *before* reading
    stdout would hang forever on anything larger the moment the child wrote
    back, so the feeder runs concurrently with the reader. `cat` is the
    smallest possible stand-in for that shape: it echoes while we are still
    writing.
    """
    prompt = "".join(f"line {i}\n" for i in range(40_000))
    assert len(prompt.encode()) > 3 * 64 * 1024

    proc = await asyncio.create_subprocess_exec(
        "cat",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    feeder = asyncio.create_task(agent_runner._feed_prompt(proc, prompt))
    out = await asyncio.wait_for(proc.stdout.read(), timeout=30)
    await proc.wait()
    await feeder

    assert out.decode() == prompt


@pytest.mark.asyncio
async def test_writing_the_prompt_blocks_until_something_reads_it():
    """Why the feeder is a task and not an await.

    Against a child that is not reading, writing a prompt bigger than the pipe
    buffer never returns. `run_claude` therefore starts the feeder and goes
    straight to reading stdout; awaiting it first would hang every run whose
    prompt crossed 64 KiB - trading a crash for a freeze, which is worse,
    because a frozen run holds its slot until the timeout instead of failing.
    """
    proc = await asyncio.create_subprocess_exec(
        "sleep", "30",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                agent_runner._feed_prompt(proc, "x" * (512 * 1024)), timeout=2
            )
    finally:
        proc.kill()
        await proc.wait()


@pytest.mark.asyncio
async def test_feeding_a_process_that_already_exited_does_not_raise():
    """A CLI that dies before reading must surface *its* error, not ours."""
    proc = await asyncio.create_subprocess_exec(
        "true",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    # Big enough that the write cannot fit in the (now dead) pipe's buffer.
    await agent_runner._feed_prompt(proc, "x" * (1024 * 1024))


# --- the loop ---------------------------------------------------------------


class FakeRun(dict):
    """A runs row, as sqlite3.Row is indexed by name in the code under test."""

    def __getitem__(self, key):
        return super().__getitem__(key)


def run(status="error", events=0, started_at="2026-07-27T05:00:00+00:00", **cols):
    return FakeRun(status=status, events=events, started_at=started_at, **cols)


def test_a_run_with_no_events_is_a_dead_start():
    assert crashloop.is_dead_start(run(status="error", events=0))


def test_the_auth_outage_shape_is_a_dead_start():
    """The 2026-08-06 incident: an expired OAuth session had the CLI boot,
    print one error and quit - 3 events (session start, the error, the failure
    result), zero cost, zero output. The zero-events test missed it and the
    scheduler retried once a minute for twenty runs straight."""
    assert crashloop.is_dead_start(
        run(status="error", events=3, cost_usd=0.0, output_tokens=0)
    )


def test_a_short_run_that_billed_something_is_not_a_dead_start():
    """Few events but real usage means the model was reached - an ordinary
    (if brief) bad run, not a starting-line crash."""
    assert not crashloop.is_dead_start(
        run(status="error", events=3, cost_usd=0.31, output_tokens=0)
    )
    assert not crashloop.is_dead_start(
        run(status="error", events=3, cost_usd=0.0, output_tokens=812)
    )


def test_a_few_event_run_with_unknown_billing_is_not_a_dead_start():
    """NULL usage is 'unknown', not 'zero' - an old row or an unparsed result.
    Only explicit zeros, which the CLI writes when it dies at the starting
    line, may condemn a run that emitted events."""
    assert not crashloop.is_dead_start(
        run(status="error", events=3, cost_usd=None, output_tokens=None)
    )


def test_a_few_event_run_missing_usage_columns_is_not_a_dead_start():
    """A row from before the usage columns existed has no verdict to give."""
    assert not crashloop.is_dead_start(run(status="error", events=3))


def test_zeroed_billing_does_not_condemn_a_run_with_many_events():
    """Run 704 recorded 770 events with zeroed token columns. The event count
    proves work happened, so it must win over what the billing says."""
    assert not crashloop.is_dead_start(
        run(status="error", events=770, cost_usd=0.0, output_tokens=0)
    )


def test_a_failed_run_that_did_work_is_not_a_dead_start():
    """The distinction the whole module rests on.

    An agent that ran for twenty minutes and then failed made progress and
    wrote a journal entry. Backing that off would punish ordinary hard work.
    """
    assert not crashloop.is_dead_start(run(status="error", events=812))


def test_a_successful_run_is_never_a_dead_start():
    assert not crashloop.is_dead_start(run(status="ok", events=0))


def test_a_null_event_count_is_not_a_dead_start():
    """NULL is 'unknown', not 'zero' - a row from an older portal, or one
    still being written. Treating it as zero would flag healthy runs."""
    assert not crashloop.is_dead_start(run(status="error", events=None))


def test_the_delay_doubles_and_then_stops():
    assert crashloop.delay_min(0) == 0
    assert crashloop.delay_min(1) == 5
    assert crashloop.delay_min(2) == 10
    assert crashloop.delay_min(3) == 20
    assert crashloop.delay_min(4) == 40
    # ...and never past the ceiling, however long the streak runs.
    assert crashloop.delay_min(257) == crashloop.MAX_DELAY_MIN


def test_the_delay_ceiling_still_allows_recovery_without_a_human():
    """A cap that meant 'never again' would need Wes to clear it by hand,
    which is exactly what he is asleep for when this fires."""
    attempts_per_day = (24 * 60) / crashloop.MAX_DELAY_MIN
    assert attempts_per_day >= 4


def test_the_streak_counts_only_the_unbroken_tail(monkeypatch):
    rows = [run(), run(), run(status="ok", events=40), run(), run(), run()]
    monkeypatch.setattr(crashloop.db, "list_runs", lambda pid, limit=40: rows)
    assert crashloop.consecutive_dead_starts(1) == 2


def test_one_healthy_run_resets_the_streak(monkeypatch):
    """Automatic recovery: nothing has to be cleared for the project to
    become schedulable again once a run actually gets going."""
    rows = [run(status="ok", events=12), run(), run(), run()]
    monkeypatch.setattr(crashloop.db, "list_runs", lambda pid, limit=40: rows)
    assert crashloop.consecutive_dead_starts(1) == 0
    assert crashloop.held(1) is None


def test_a_run_in_flight_does_not_break_the_streak(monkeypatch):
    """Runs go in parallel, so the newest row for a project can be the very
    attempt being decided. Counting it as healthy would defeat the hold."""
    rows = [run(status="running", events=None), run(), run(), run()]
    monkeypatch.setattr(crashloop.db, "list_runs", lambda pid, limit=40: rows)
    assert crashloop.consecutive_dead_starts(1) == 3


def _booted_before(monkeypatch, when="2026-07-27T04:00:00+00:00"):
    """Pin the portal's boot to before the failures, so the one-free-attempt
    rule does not mask what a test is actually about."""
    monkeypatch.setattr(crashloop, "BOOT_TIME", datetime.fromisoformat(when))


def test_a_project_in_a_loop_is_held(monkeypatch):
    _booted_before(monkeypatch)
    now = datetime(2026, 7, 27, 5, 1, tzinfo=timezone.utc)
    rows = [run(started_at="2026-07-27T05:00:00+00:00")] * 3
    monkeypatch.setattr(crashloop.db, "list_runs", lambda pid, limit=40: rows)
    held = crashloop.held(1, now=now)
    assert held and "died before the agent started" in held


def test_the_hold_expires_so_the_project_is_retried(monkeypatch):
    """The backoff is a wait, not a ban."""
    _booted_before(monkeypatch)
    rows = [run(started_at="2026-07-27T05:00:00+00:00")] * 3
    monkeypatch.setattr(crashloop.db, "list_runs", lambda pid, limit=40: rows)
    # 3 dead starts owes 20 minutes; 21 minutes later it is workable again.
    later = datetime(2026, 7, 27, 5, 21, tzinfo=timezone.utc)
    assert crashloop.held(1, now=later) is None


def test_a_restart_buys_one_attempt_however_long_the_streak(monkeypatch):
    """The trap this fix itself nearly set.

    OpenJournal had 257 dead starts behind it when the breaker was written. A
    purely historical streak would have held the project at the 6h ceiling
    from the moment the fix shipped - suppressing the one project the fix was
    for, on the evidence of code that no longer existed.
    """
    rows = [run(started_at="2026-07-27T05:00:00+00:00")] * 257
    monkeypatch.setattr(crashloop.db, "list_runs", lambda pid, limit=40: rows)
    # The portal came up after those failures - as it does on every deploy.
    monkeypatch.setattr(
        crashloop, "BOOT_TIME", datetime(2026, 7, 27, 6, 0, tzinfo=timezone.utc)
    )
    now = datetime(2026, 7, 27, 6, 1, tzinfo=timezone.utc)
    assert crashloop.held(1, now=now) is None


def test_the_free_attempt_is_not_free_twice(monkeypatch):
    """One run per restart, not a reset. A project that is still broken after
    the restart goes straight back into the full backoff."""
    boot = datetime(2026, 7, 27, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(crashloop, "BOOT_TIME", boot)
    # The retry happened after the boot, and died like the rest.
    rows = [run(started_at="2026-07-27T06:01:00+00:00")] + [
        run(started_at="2026-07-27T05:00:00+00:00")
    ] * 257
    monkeypatch.setattr(crashloop.db, "list_runs", lambda pid, limit=40: rows)
    now = datetime(2026, 7, 27, 6, 2, tzinfo=timezone.utc)
    assert crashloop.held(1, now=now) is not None


def test_the_257_run_flood_would_have_been_stopped(monkeypatch):
    """The actual incident, replayed.

    OpenJournal failed 257 times between 02:38 on the 26th and 05:51 on the
    27th - roughly one a minute. With the breaker in place the fourth attempt
    is already 40 minutes away.
    """
    start = datetime(2026, 7, 26, 2, 38, tzinfo=timezone.utc)
    rows: list[FakeRun] = []
    attempts = 0
    now = start
    for _ in range(600):  # far more ticks than the incident had
        streak = 0
        for r in rows:
            if not crashloop.is_dead_start(r):
                break
            streak += 1
        if rows:
            last = datetime.fromisoformat(rows[0]["started_at"])
            if now < last + timedelta(minutes=crashloop.delay_min(streak)):
                now += timedelta(minutes=1)
                continue
        rows.insert(0, run(started_at=now.isoformat()))
        attempts += 1
        now += timedelta(minutes=1)

    # 600 minutes of ticking; the real portal spent every one of them on a run.
    assert attempts <= 10, f"still a flood: {attempts} attempts"


def test_the_auth_outage_flood_would_have_been_stopped():
    """2026-08-06, replayed: twenty auth failures at one a minute, each with
    the CLI's 3 boot-and-die events. The zero-events breaker let every one of
    them through; with billing considered, the fourth attempt is 40 minutes
    out and the twenty-minute outage costs three runs."""
    start = datetime(2026, 8, 6, 16, 10, tzinfo=timezone.utc)
    rows: list[FakeRun] = []
    attempts = 0
    now = start
    for _ in range(20):
        streak = 0
        for r in rows:
            if not crashloop.is_dead_start(r):
                break
            streak += 1
        if rows:
            last = datetime.fromisoformat(rows[0]["started_at"])
            if now < last + timedelta(minutes=crashloop.delay_min(streak)):
                now += timedelta(minutes=1)
                continue
        rows.insert(
            0,
            run(started_at=now.isoformat(), events=3, cost_usd=0.0, output_tokens=0),
        )
        attempts += 1
        now += timedelta(minutes=1)

    assert attempts <= 4, f"still a flood: {attempts} attempts"


def test_the_first_dead_start_is_not_announced():
    """One is noise - a spawn racing a service restart does this."""
    assert not crashloop.should_announce(1, 0)
    assert not crashloop.should_announce(2, 0)


def test_a_sustained_loop_is_announced():
    assert crashloop.should_announce(3, 0)


def test_the_same_loop_is_not_announced_over_and_over():
    """Wes has already told the portal off once for asking the same thing
    repeatedly. A stuck project must not file a notice every few minutes."""
    assert not crashloop.should_announce(4, 3)
    assert not crashloop.should_announce(5, 3)
    # ...but a loop that has doubled in length is news again.
    assert crashloop.should_announce(6, 3)


def test_the_note_says_what_happened_and_that_it_self_heals():
    note = crashloop.note_for(3)
    assert "died before the agent started" in note
    assert "clears itself" in note


# --- the scheduler, against a real database ---------------------------------


@pytest.fixture
def projects(temp_data_dir):
    from app import db as real_db

    return [
        real_db.create_project("Alpha", stage="active", build_approved=True, slug="alpha"),
        real_db.create_project("Beta", stage="active", build_approved=True, slug="beta"),
    ]


def _die(project_id: int, n: int) -> None:
    """`n` runs that were created and died without emitting anything."""
    from app import db as real_db

    for _ in range(n):
        run_id = real_db.create_run(project_id, "build", "opus")
        real_db.finish_run(run_id, "error", summary="Run crashed; see the service log.")


def _die_at_auth(project_id: int, n: int) -> None:
    """`n` runs shaped exactly like the 2026-08-06 auth failures: the CLI
    booted, emitted 3 events, billed nothing and died."""
    from app import db as real_db

    for _ in range(n):
        run_id = real_db.create_run(project_id, "build", "opus")
        real_db.update_run_activity(run_id, "session start", 3)
        real_db.record_run_usage(
            run_id, input_tokens=0, output_tokens=0,
            cache_write_tokens=0, cache_read_tokens=0,
        )
        real_db.finish_run(run_id, "error", cost_usd=0.0, num_turns=1,
                           summary="Failed to authenticate")


def test_an_auth_looping_project_is_passed_over_for_a_healthy_one(projects):
    """The 2026-08-06 shape, against the real scheduler: rows written the way
    agent_runner writes them must trip the breaker."""
    from app import worker

    _die_at_auth(projects[0]["id"], 3)
    picked, _ = worker._pick_project(None)
    assert picked["slug"] == "beta"


def test_a_looping_project_is_passed_over_for_a_healthy_one(projects):
    """The behavior Wes actually needed: OpenJournal stops hogging the slots
    it cannot use, and the rest of the board gets them."""
    from app import worker

    _die(projects[0]["id"], 3)
    picked, _ = worker._pick_project(None)
    assert picked["slug"] == "beta"


def test_a_manual_run_ignores_the_hold(projects):
    """Pressing 'Run now' is Wes saying try it anyway - and is how he would
    check whether a fix took. A breaker he cannot override is a worse bug."""
    from app import worker

    _die(projects[0]["id"], 20)
    picked, is_manual = worker._pick_project(projects[0]["id"])
    assert picked["slug"] == "alpha"
    assert is_manual


def test_nothing_is_held_back_before_a_project_has_failed(projects):
    """The hold must be invisible on a healthy board."""
    from app import worker

    picked, _ = worker._pick_project(None)
    assert picked["slug"] == "alpha"


def test_runs_started_in_the_same_second_are_ordered_by_id(projects):
    """`started_at` has one-second resolution, and the streak walk stops at the
    first healthy run. If a recovery run sorted below the failures it ended,
    the project would stay held forever - a breaker with no way out."""
    from app import db as real_db

    _die(projects[0]["id"], 3)
    ok = real_db.create_run(projects[0]["id"], "build", "opus")
    real_db.update_run_activity(ok, "working", 120)
    real_db.finish_run(ok, "ok")

    rows = real_db.list_runs(projects[0]["id"], limit=10)
    assert rows[0]["id"] == ok, "the newest run must sort first regardless of ties"
    assert crashloop.consecutive_dead_starts(projects[0]["id"]) == 0


def test_a_project_that_failed_after_doing_work_is_still_scheduled(projects):
    """An ordinary bad run is not a crash loop and must not be spaced out."""
    from app import db as real_db, worker

    for _ in range(5):
        run_id = real_db.create_run(projects[0]["id"], "build", "opus")
        real_db.update_run_activity(run_id, "did things", 400)
        real_db.finish_run(run_id, "error", summary="the agent failed")
    picked, _ = worker._pick_project(None)
    assert picked["slug"] == "alpha"


@pytest.mark.asyncio
async def test_the_loop_is_announced_once_then_journalled_on_doubling(projects, monkeypatch):
    from app import db as real_db, worker

    sent: list[str] = []

    async def fake_notify(title, message, **kw):
        sent.append(title)

    monkeypatch.setattr(worker.notify, "notify", fake_notify)
    project = real_db.get_project(projects[0]["id"])

    _die(project["id"], 3)
    await worker._announce_crash_loop(project)
    assert len(sent) == 1

    # Two more failures: still the same loop, so still one notification.
    _die(project["id"], 2)
    await worker._announce_crash_loop(project)
    assert len(sent) == 1

    # The journal carries the explanation Wes would otherwise have to dig for.
    entries = real_db.list_journal(project_id=project["id"], limit=10)
    assert any("died before the agent started" in (e["content_md"] or "") for e in entries)


@pytest.mark.asyncio
async def test_a_recovered_project_can_announce_a_future_loop(projects, monkeypatch):
    """The marker has to be cleared on recovery, or the next outage is
    silently deduplicated against the last one - months later."""
    from app import db as real_db, worker

    sent: list[str] = []

    async def fake_notify(title, message, **kw):
        sent.append(title)

    monkeypatch.setattr(worker.notify, "notify", fake_notify)
    project = real_db.get_project(projects[0]["id"])

    _die(project["id"], 3)
    await worker._announce_crash_loop(project)
    assert len(sent) == 1

    # A healthy run, which ends the loop.
    ok = real_db.create_run(project["id"], "build", "opus")
    real_db.update_run_activity(ok, "working", 120)
    real_db.finish_run(ok, "ok")
    await worker._announce_crash_loop(project)
    assert real_db.get_setting(worker._CRASHLOOP_SETTING.format(project["id"])) == "0"

    # A fresh outage is news again.
    _die(project["id"], 3)
    await worker._announce_crash_loop(project)
    assert len(sent) == 2

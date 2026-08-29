"""The other three kinds of run, and the directories they share (#435).

`worklock` shipped covering project runs only, deliberately: mixing "lease the
workspace" with "lease everything else" in one commit would have mixed two kinds
of risk. This file is the rest of the answer, and the answer is not "yes" for
everything.

**Reflect and compaction lease `config.MEMORY_DIR`, and it is one lease, not
two.** They work in the same directory and rewrite the same files - the reflect
rewrites profile.md wholesale (which is how Wes's own "about me" text was lost
once), the compaction rewrites learnings.md, and the reflect touches that too.
Their only previous guard was a pair of in-memory slots, `REFLECT_SLOT` and
`COMPACT_SLOT`, which are separate from each other and die with the portal
process. On a box that restarts itself several times an hour to load its own new
code, "dies with the process" is not a guard.

**A one-off leases its own workspace.** Its guard was `db.oneoff_running`, a
SELECT on `runs.status` - the exact derived answer this module exists to stop
trusting on its own, and the one that failed on 2026-07-29. The stake is higher
here than a shared checkout: one-offs resume a CLI session, and two agents
resuming one session fork the conversation as well as the files.

**An ask leases nothing, on purpose.** It is read-only by construction and the
lease is `--nonblock`, so giving it one would turn "ask a question about a
project that happens to be mid-run" into a refusal. That is pinned below too,
because a later reader sweeping for unleased spawns should find the reason here
rather than "fixing" it.
"""
from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import module_state

from app import agent_runner, ask, config, db, oneoff, worker, worklock

# Probed without leaving the answer memoized in `worklock._available`: this runs
# at collection, before any fixture exists to clear it. Clearing it around each
# test is `conftest`'s `module_state_is_never_inherited`, which is why the local
# fixture that used to do it here is gone.
_HAVE_FLOCK = module_state.probe_without_memoizing(
    "app.worklock", "_available", worklock.available)


@pytest.fixture(autouse=True)
def _clean_worker_state():
    worker._inflight.clear()  # noqa: SLF001
    yield
    worker._inflight.clear()  # noqa: SLF001


@pytest.fixture
def task():
    return db.create_oneoff("Fix the cron mail\n\nIt stopped arriving on Sunday.")


def _fake_run(monkeypatch, result: agent_runner.RunResult | None = None) -> dict:
    """Record what `run_claude` was called with, and answer with `result`."""
    seen: dict = {}
    answer = result if result is not None else agent_runner.RunResult(
        ok=True, result_text="done"
    )

    async def fake(prompt, cwd, model, timeout_min, **kwargs):
        seen.update(kwargs, prompt=prompt, cwd=cwd, model=model)
        return answer

    monkeypatch.setattr(agent_runner, "run_claude", fake)
    return seen


def _holder(path: Path, seconds: float = 30) -> subprocess.Popen:
    """A real process holding a real lease on `path`, the way a run does."""
    return subprocess.Popen(
        worklock.wrap([sys.executable, "-c", "import time; time.sleep(%r)" % seconds], path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_until_leased(path: Path, proc: subprocess.Popen) -> None:
    """`flock` acquires a moment after the spawn, so waiting for the lock to
    actually appear is the difference between a real test and a flaky one."""
    import time as _time

    deadline = _time.monotonic() + 10
    while worklock.is_busy(path) is not True and _time.monotonic() < deadline:
        assert proc.poll() is None, "the lock holder died before taking the lease"
        _time.sleep(0.02)
    assert worklock.is_busy(path) is True


# --- what each kind of run leases --------------------------------------------


def test_the_reflect_leases_the_memory_directory(monkeypatch):
    seen = _fake_run(monkeypatch)

    asyncio.run(worker.run_reflect())

    assert seen["lock_dir"] == config.MEMORY_DIR


def test_the_compaction_leases_the_memory_directory(monkeypatch):
    seen = _fake_run(monkeypatch)

    asyncio.run(worker.run_compaction())

    assert seen["lock_dir"] == config.MEMORY_DIR


def test_the_reflect_and_the_compaction_lease_the_same_directory(monkeypatch):
    """The claim that makes the two of them mutually exclusive. Two different
    lock targets would be two locks nobody contends for, and the pair of them
    would go on being able to rewrite learnings.md at the same time - which
    their two separate `_inflight` slots have always allowed."""
    reflect = _fake_run(monkeypatch)
    asyncio.run(worker.run_reflect())
    compact = _fake_run(monkeypatch)
    asyncio.run(worker.run_compaction())

    assert reflect["lock_dir"] == compact["lock_dir"]


def test_a_oneoff_leases_its_own_task_workspace(task, monkeypatch):
    run_id = db.create_run(None, "oneoff", "claude-opus-5", oneoff_id=task["id"])
    seen = _fake_run(monkeypatch)

    asyncio.run(worker.run_oneoff_task(task["id"], run_id, "claude-opus-5"))

    assert seen["lock_dir"] == oneoff.workspace(task["id"])


def test_every_run_claude_call_site_asks_for_a_lease():
    """A structural check, because the failure it catches is an *omission*.

    Nothing about `run_claude` makes `lock_dir` compulsory - it defaults to
    None, and it has to, since making it required would only move the decision
    to whoever types `lock_dir=None` fastest. So a fifth kind of run added later
    leases nothing and no behavioral test in this file goes red. This one does.

    If a new caller genuinely should not lease anything - the way an ask should
    not - it does not belong on `run_claude` at all: an unleased spawn is a
    read-only spawn, and `ask.run_ask` is the shape that expresses that.
    """
    tree = ast.parse(Path("app/worker.py").read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_claude"
    ]
    assert len(calls) == 4, "a run_claude call site was added or removed"
    for call in calls:
        names = {kw.arg for kw in call.keywords}
        assert "lock_dir" in names, f"run_claude at line {call.lineno} takes no lease"


# --- an ask deliberately takes none ------------------------------------------


def test_an_ask_spawns_the_cli_directly_and_takes_no_lease(monkeypatch, tmp_path):
    """Read-only by construction, so it collides with nothing - and a
    `--nonblock` lease would refuse any question asked about a live run."""
    argv: list[str] = []

    async def fake_exec(*cmd, **kwargs):
        argv.extend(cmd)
        raise FileNotFoundError("not actually spawning")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(ask.run_ask("q", tmp_path, "claude-opus-5"))

    assert argv[0] == "claude"
    assert worklock._FLOCK not in argv  # noqa: SLF001


def test_an_ask_cannot_write_which_is_why_it_needs_no_lease():
    """The premise the decision above rests on. If these flags ever go, the
    reasoning goes with them and an ask needs a lease like everything else."""
    cmd = ask.build_command("q", "claude-opus-5")
    for tool in ("Bash", "Edit", "Write"):
        assert tool in cmd[cmd.index("--disallowedTools"):]
        assert tool not in cmd[cmd.index("--allowedTools"):cmd.index("--disallowedTools")]


# --- refusals -----------------------------------------------------------------


def test_a_refused_reflect_leaves_the_day_unreflected(monkeypatch):
    """`last_reflect_date` is stamped on success only, and a refusal must not
    change that: nothing ran, so the day has not been reflected on and the next
    quiet tick should try again."""
    _fake_run(monkeypatch, agent_runner.RunResult(ok=False, lock_conflict=True))

    asyncio.run(worker.run_reflect())

    assert (db.get_setting("last_reflect_date") or "") == ""
    run = db.get_run(db.list_recent_runs(1)[0]["id"])
    assert run["status"] == "error"
    assert "still holds the shared memory directory" in run["summary"]


def test_a_refused_reflect_says_which_directory_was_held(monkeypatch):
    """The note names the memory directory, not a project slug. Reusing the
    project wording here would print "another agent still holds the `` workspace"
    at somebody trying to work out what happened."""
    _fake_run(monkeypatch, agent_runner.RunResult(ok=False, lock_conflict=True))

    asyncio.run(worker.run_reflect())

    notes = [e["content_md"] for e in db.list_journal(None, limit=10)]
    assert any(worklock.MEMORY_RESOURCE in n for n in notes)


def test_a_refused_compaction_changes_nothing_and_says_so(monkeypatch):
    _fake_run(monkeypatch, agent_runner.RunResult(ok=False, lock_conflict=True))

    asyncio.run(worker.run_compaction())

    run = db.get_run(db.list_recent_runs(1)[0]["id"])
    assert run["status"] == "error"
    assert worklock.MEMORY_RESOURCE in run["summary"]
    # And no "compacted: N lines -> M lines" entry, which would be a lie about
    # a file nothing touched.
    notes = [e["content_md"] for e in db.list_journal(None, limit=10)]
    assert not any("Learnings compacted" in n for n in notes)


def test_a_refused_oneoff_tells_the_person_to_send_it_again(task, monkeypatch):
    """The backstop path, where the messages are already spent. There is no
    undeliver, so the only honest thing left is to say so."""
    db.add_oneoff_message(task["id"], "user", "any progress?")
    run_id = db.create_run(None, "oneoff", "claude-opus-5", oneoff_id=task["id"])
    _fake_run(monkeypatch, agent_runner.RunResult(ok=False, lock_conflict=True))

    asyncio.run(worker.run_oneoff_task(task["id"], run_id, "claude-opus-5"))

    assert db.get_run(run_id)["status"] == "error"
    said = [m["content_md"] for m in db.list_oneoff_messages(task["id"]) if m["role"] == "system"]
    assert any("Send it again" in s for s in said)


# --- against a real kernel lock ----------------------------------------------


@pytest.mark.skipif(not _HAVE_FLOCK, reason="flock(1) with --close not available")
def test_a_held_memory_directory_stops_a_second_compaction(monkeypatch):
    """The case `compaction_running()` cannot see: the slot is in memory and
    empty here, exactly as it is in a portal process that has just restarted."""
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    proc = _holder(config.MEMORY_DIR)
    try:
        _wait_until_leased(config.MEMORY_DIR, proc)
        assert worker.compaction_running() is False

        assert worker.start_compaction() is False
    finally:
        proc.kill()
        proc.wait()

    assert db.list_recent_runs(1) == [] or db.list_recent_runs(1)[0]["task"] != "compact"


@pytest.mark.skipif(not _HAVE_FLOCK, reason="flock(1) with --close not available")
def test_a_free_memory_directory_still_lets_a_compaction_start(monkeypatch):
    """The counterpart, so the test above is not just observing that
    `start_compaction` returns False for some other reason. Run inside a loop
    because it schedules the compaction as a task rather than awaiting it."""
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    _fake_run(monkeypatch)

    async def go():
        assert worker.start_compaction() is True
        handle = worker._inflight.get(worker.COMPACT_SLOT)  # noqa: SLF001
        assert handle is not None
        await handle

    asyncio.run(go())


def test_a_memory_directory_that_cannot_be_asked_about_does_not_block(monkeypatch):
    """Fail open, the same rule `workspace_leased` follows and for the same
    reason: `is_busy` answers None when it could not find out - a directory that
    does not exist yet, a filesystem with no BSD locks - and reading that as
    "busy" would stop the daily reflect and the compact button forever on any
    machine where leasing does not work. A hardening feature that can silently
    switch off the memory jobs is worse than the problem it solves."""
    monkeypatch.setattr(config, "MEMORY_DIR", config.MEMORY_DIR / "not-created-yet")
    assert worklock.is_busy(config.MEMORY_DIR) is None

    assert worker.memory_leased() is False


def _quiet_and_due(monkeypatch) -> None:
    """Past the day boundary, nothing running: every guard on `_maybe_reflect`
    says "go", so the lease is the only thing left that can stop it."""
    monkeypatch.setattr(worker.daycycle, "reset_hour", lambda: 5)
    monkeypatch.setattr(
        worker.daycycle, "local_now",
        lambda: datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(worker.db, "is_run_running", lambda: False)


@pytest.mark.skipif(not _HAVE_FLOCK, reason="flock(1) with --close not available")
def test_a_held_memory_directory_stops_the_reflect(monkeypatch):
    """`REFLECT_SLOT in _inflight` cannot see a reflect adopted across a
    restart - the slot is in this process's memory and the restart emptied it.
    The lock is on the directory and outlives every portal process."""
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    _quiet_and_due(monkeypatch)
    db.set_setting("worker_enabled", "1")
    started: list[bool] = []
    monkeypatch.setattr(worker, "run_reflect", lambda: started.append(True))

    proc = _holder(config.MEMORY_DIR)
    try:
        _wait_until_leased(config.MEMORY_DIR, proc)
        asyncio.run(worker._maybe_reflect())  # noqa: SLF001
    finally:
        proc.kill()
        proc.wait()

    assert started == []
    # And nothing was stamped, so the next quiet tick still reflects today.
    assert not db.get_setting("last_reflect_date")


@pytest.mark.skipif(not _HAVE_FLOCK, reason="flock(1) with --close not available")
def test_a_free_memory_directory_still_lets_the_reflect_run(monkeypatch):
    """The delete-the-fix direction: the new gate must not have disabled the
    daily reflect outright."""
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    _quiet_and_due(monkeypatch)
    db.set_setting("worker_enabled", "1")
    started: list[bool] = []

    async def fake_reflect():
        started.append(True)

    monkeypatch.setattr(worker, "run_reflect", fake_reflect)

    async def drive():
        await worker._maybe_reflect()  # noqa: SLF001
        await asyncio.sleep(0)

    asyncio.run(drive())

    assert started == [True]


@pytest.mark.skipif(not _HAVE_FLOCK, reason="flock(1) with --close not available")
def test_the_memory_page_says_why_the_compact_button_is_dead(monkeypatch):
    """"Nothing fails quietly." `start_compaction` now returns False for a
    reason `compaction_running()` cannot see, so without this the button would
    be live, press to no effect, and explain nothing."""
    from starlette.testclient import TestClient

    from app import main

    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    client = TestClient(main.app)

    free = client.get("/memory").text
    assert "compact this file" in free
    assert "memory in use" not in free

    proc = _holder(config.MEMORY_DIR)
    try:
        _wait_until_leased(config.MEMORY_DIR, proc)
        busy = client.get("/memory").text
    finally:
        proc.kill()
        proc.wait()

    assert "memory in use" in busy
    assert "compact this file" not in busy
    assert "working in the memory directory right now" in busy


@pytest.mark.skipif(not _HAVE_FLOCK, reason="flock(1) with --close not available")
def test_a_held_task_workspace_refuses_a_oneoff_without_spending_its_messages(
    task, monkeypatch
):
    """The ordering that matters. `db.mark_oneoff_delivered` is one-way, so a
    refusal discovered only from the spawn's exit code would burn the person's
    message on a run that never read a word of it. The pre-flight is what keeps
    the message pending for whichever agent finishes first to pick up."""
    ws = oneoff.workspace(task["id"])
    ws.mkdir(parents=True, exist_ok=True)
    db.add_oneoff_message(task["id"], "user", "any progress?")
    run_id = db.create_run(None, "oneoff", "claude-opus-5", oneoff_id=task["id"])

    spawned = []

    async def never(*a, **k):
        spawned.append(a)
        return agent_runner.RunResult(ok=True, result_text="")

    monkeypatch.setattr(agent_runner, "run_claude", never)

    proc = _holder(ws)
    try:
        _wait_until_leased(ws, proc)
        asyncio.run(worker.run_oneoff_task(task["id"], run_id, "claude-opus-5"))
    finally:
        proc.kill()
        proc.wait()

    assert spawned == [], "the run spawned into a workspace somebody else holds"
    assert db.get_run(run_id)["status"] == "error"
    still_waiting = [m["id"] for m in db.pending_oneoff_messages(task["id"])]
    assert still_waiting, "the message was spent on a run that never read it"


@pytest.mark.skipif(not _HAVE_FLOCK, reason="flock(1) with --close not available")
def test_a_refused_oneoff_does_not_respawn_itself(task, monkeypatch):
    """A refusal must not call `_continue_if_messages_waiting`. The messages are
    still pending by design, so re-spawning on a lease that is still held is a
    tight loop burning a run row per pass."""
    ws = oneoff.workspace(task["id"])
    ws.mkdir(parents=True, exist_ok=True)
    db.add_oneoff_message(task["id"], "user", "any progress?")
    run_id = db.create_run(None, "oneoff", "claude-opus-5", oneoff_id=task["id"])

    spawns = []
    monkeypatch.setattr(worker, "spawn_oneoff", lambda tid: spawns.append(tid))

    proc = _holder(ws)
    try:
        _wait_until_leased(ws, proc)
        asyncio.run(worker.run_oneoff_task(task["id"], run_id, "claude-opus-5"))
    finally:
        proc.kill()
        proc.wait()

    assert spawns == []

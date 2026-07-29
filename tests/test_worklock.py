"""One agent per workspace, enforced by the kernel instead of by a SELECT.

Defect 4 of the 2026-07-29 double-run incident. Defects 1-3 made the portal's
"is this project busy" query *correct*; none of them made it impossible to get
wrong, because the answer was still derived from a column that any bug can
clear. `app/worklock.py` replaces the derivation with a `flock` on the workspace
directory, held for exactly as long as the agent lives.

Almost nothing here is mocked, and that is deliberate. The scope work in
`test_restart_survivors.py` needed mocks because it asks systemd questions, and
the run that wrote it lost an afternoon to a mock that agreed with the code's
wrong assumption about an exit code. A BSD lock needs no such thing: a real lock
on a real temporary directory, taken by a real subprocess, is both cheaper than
a mock and the only version that can catch the failures that matter here.

Every claim below was checked by deleting the fix and watching this file fail.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import agent_runner, config, db, runlimit, worker, worklock


@pytest.fixture(autouse=True)
def _fresh_probe():
    """`available()` memoizes, and a test that fakes it away would otherwise
    decide the answer for every test that runs after it."""
    worklock._available = None  # noqa: SLF001
    yield
    worklock._available = None  # noqa: SLF001


@pytest.fixture(autouse=True)
def _clean_worker_state():
    worker._inflight.clear()  # noqa: SLF001
    worker._restarting = False  # noqa: SLF001
    yield
    worker._inflight.clear()  # noqa: SLF001
    worker._restarting = False  # noqa: SLF001


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return d


def _holder(path: Path, seconds: float = 30) -> subprocess.Popen:
    """A real process holding a real lease on `path`, the way a run does."""
    proc = subprocess.Popen(
        worklock.wrap([sys.executable, "-c", "import time; time.sleep(%r)" % seconds], path),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(200):
        if worklock.is_busy(path) is True:
            return proc
        time.sleep(0.02)
    proc.kill()
    raise AssertionError("the holder never took the lease")


def _await_free(path: Path, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if worklock.is_busy(path) is False:
            return True
        time.sleep(0.02)
    return False


# --- reading the lease -------------------------------------------------------


def test_an_unheld_workspace_reads_as_free(ws):
    assert worklock.is_busy(ws) is False


def test_a_held_workspace_reads_as_busy(ws):
    proc = _holder(ws)
    try:
        assert worklock.is_busy(ws) is True
    finally:
        proc.kill()
        proc.wait()


def test_a_missing_directory_is_unknown_rather_than_free(tmp_path):
    """None, not False. "There is no such workspace yet" is not evidence that
    it is safe to start a second agent in one, and a caller that cannot tell
    the two apart is how a maybe becomes a fact."""
    assert worklock.is_busy(tmp_path / "never-created") is None


def test_no_directory_at_all_is_unknown(ws):
    assert worklock.is_busy(None) is None


def test_the_portal_cannot_fool_itself_about_a_lease_it_holds(ws):
    """flock treats two opens of one file as independent descriptions, so the
    holding process is refused through a second fd exactly like anybody else.
    Without that, the pre-flight check would read a workspace this very process
    had leased as free."""
    import fcntl

    fd = os.open(str(ws), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert worklock.is_busy(ws) is True
    finally:
        os.close(fd)


def test_the_probe_leaves_the_lease_free_for_somebody_else(ws):
    """`is_busy` reads by taking the lock, because a BSD lock has no query
    interface. If it failed to drop it again, asking the question would be the
    thing that locked the workspace."""
    assert worklock.is_busy(ws) is False
    assert worklock.is_busy(ws) is False
    proc = subprocess.Popen(
        worklock.wrap([sys.executable, "-c", "pass"], ws),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert proc.wait(timeout=30) == 0, "the probe left the workspace locked"


# --- holding the lease -------------------------------------------------------


def test_a_wrapped_command_holds_the_workspace_while_it_runs(ws):
    proc = _holder(ws)
    try:
        assert worklock.is_busy(ws) is True
    finally:
        proc.kill()
        proc.wait()


def test_a_second_agent_is_refused_with_the_conflict_code(ws):
    """The whole point, and note what is asserted: not just the exit code, but
    that the command never ran. A refusal that still executed the agent would
    be the double-run with an error message attached."""
    holder = _holder(ws)
    marker = ws / "the-second-agent-ran"
    try:
        second = subprocess.run(
            worklock.wrap(
                [sys.executable, "-c", "open(%r, 'w').close()" % str(marker)], ws
            ),
            capture_output=True, timeout=30,
        )
        assert second.returncode == worklock.CONFLICT_RC
        assert not marker.exists(), "the second agent ran inside a leased workspace"
    finally:
        holder.kill()
        holder.wait()


def test_the_lease_ends_with_the_agent_even_when_it_leaves_a_daemon_behind(ws):
    """`--close`, and the reason it is not optional.

    The same incident found five finished runs whose scopes were still held
    open by something the agent had detached - a preview server, a bun process,
    a leftover reverse SSH tunnel. Without `--close` each of those would inherit
    the lease descriptor and hold its project's workspace locked forever, which
    is a far worse failure than the one being prevented: a permanently unusable
    project, with nothing in the UI to say why.

    Found by running it rather than by reading the man page.

    The daemon is detached *by a shell*, and that detail is the test. The first
    version of this used `subprocess.Popen(start_new_session=True)`, which
    defaults to `close_fds=True` - so the grandchild never inherited the lock
    descriptor whatever `flock` did, and the test passed just as happily with
    `--close` deleted. It was pinning CPython's default, not the fix. A shell
    passes descriptors straight through, which is what the real leaked preview
    servers and SSH tunnels were started by.
    """
    pidfile = ws / "daemon.pid"
    proc = subprocess.Popen(
        worklock.wrap(
            ["bash", "-c", f"sleep 60 & echo $! > {pidfile}; sleep 0.5"], ws
        ),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    daemon = None
    try:
        proc.wait(timeout=30)
        daemon = int(pidfile.read_text().strip())
        # The assertion below is only meaningful while the daemon is genuinely
        # still running; if it had already exited the lease would be free for
        # the boring reason.
        os.kill(daemon, 0)
        assert _await_free(ws), "a detached daemon is still holding the lease"
        os.kill(daemon, 0)
    finally:
        if daemon:
            try:
                os.kill(daemon, 9)
            except OSError:
                pass


def test_the_lease_survives_the_death_of_whoever_asked_for_it(ws):
    """The property no database column has. The portal being SIGKILLed - or
    restarted, which is the case that caused the incident - must not release a
    lease on a workspace an agent is still working in."""
    proc = _holder(ws)
    try:
        assert worklock.is_busy(ws) is True
        # Nothing in this process is holding anything; the child is.
        worklock._available = None  # noqa: SLF001
        assert worklock.is_busy(ws) is True
    finally:
        proc.kill()
        proc.wait()
    assert _await_free(ws)


# --- the shape of the wrap ---------------------------------------------------


def test_the_wrap_passes_the_commands_own_flags_through(ws):
    """`flock(1)` takes no `--` separator and dies with "failed to execute --"
    if given one - which presents as a run that produces no output at all. It
    needs none: option parsing stops at the lock target. This runs a real
    command with real flags rather than asserting on the argv, because the argv
    is exactly what a wrong assumption would have gotten right."""
    out = subprocess.run(
        worklock.wrap([sys.executable, "-c", "print('hi')"], ws),
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "hi"
    assert "--" not in worklock.wrap(["cmd"], ws)


def test_no_lock_directory_means_no_wrapping(ws):
    assert worklock.wrap(["claude", "-p"], None) == ["claude", "-p"]


def test_leasing_fails_open_when_flock_is_missing(ws, monkeypatch):
    """A hardening feature that can stop every run on the board from starting
    is worse than the problem it solves."""
    monkeypatch.setattr(worklock.shutil, "which", lambda _: None)
    worklock._available = None  # noqa: SLF001
    assert worklock.wrap(["claude", "-p"], ws) == ["claude", "-p"]
    assert worklock.available() is False


def test_a_flock_that_cannot_close_the_descriptor_counts_as_no_flock(ws, monkeypatch):
    """Degrading to a lease that leaks into every detached process would be a
    silent downgrade to the worse failure, so it is refused outright."""
    monkeypatch.setattr(worklock.shutil, "which", lambda _: "/usr/bin/flock")
    monkeypatch.setattr(
        worklock.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, b"usage: flock [-sxun]", b""),
    )
    worklock._available = None  # noqa: SLF001
    assert worklock.available() is False
    assert worklock.wrap(["claude", "-p"], ws) == ["claude", "-p"]


def test_the_conflict_code_is_not_one(ws):
    """Without `--conflict-exit-code` a refusal is exit 1, which is
    indistinguishable from the agent's own ordinary failure - so every refused
    run would be reported to Wes as a crashed one."""
    assert worklock.CONFLICT_RC not in (0, 1)


# --- the scheduler asks the kernel too ---------------------------------------


@pytest.fixture
def project():
    return db.create_project("Alpha", stage="active", build_approved=True, slug="alpha")


def _workspace_for(slug: str) -> Path:
    d = config.PROJECTS_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_the_scheduler_skips_a_project_whose_workspace_is_leased(project):
    """The delete-the-fix headline. The runs table says this project is idle -
    exactly as it did at 01:19:20 on 2026-07-29, when the boot sweep had just
    cleared five live runs - and an agent is nevertheless in the workspace.

    Remove the `workspace_leased` check from `_pick_project` and this test
    watches the portal hand out an occupied checkout.
    """
    holder = _holder(_workspace_for("alpha"))
    try:
        assert db.running_project_ids() == set() or project["id"] not in db.running_project_ids()
        picked, _ = worker._pick_project(None)  # noqa: SLF001
        assert picked is None, "the scheduler picked a project another agent is working in"
    finally:
        holder.kill()
        holder.wait()


def test_a_manual_run_is_refused_onto_a_leased_workspace_too(project):
    """Wes pressing "run now" bypasses the pacing and the per-project cap on
    purpose. It has never bypassed the one-agent-per-workspace rule, and the
    lease is not an exception to that."""
    holder = _holder(_workspace_for("alpha"))
    try:
        picked, is_manual = worker._pick_project(project["id"])  # noqa: SLF001
        assert picked is None
    finally:
        holder.kill()
        holder.wait()


def test_the_scheduler_still_picks_a_project_whose_workspace_is_free(project):
    _workspace_for("alpha")
    picked, _ = worker._pick_project(None)  # noqa: SLF001
    assert picked is not None and picked["slug"] == "alpha"


def test_an_unanswerable_lease_does_not_stop_the_board(project, monkeypatch):
    """Fail open: None means "could not find out", and a machine where leasing
    does not work must schedule exactly as it did before."""
    monkeypatch.setattr(worklock, "is_busy", lambda _: None)
    picked, _ = worker._pick_project(None)  # noqa: SLF001
    assert picked is not None


def test_a_project_with_no_workspace_yet_is_schedulable(project):
    """A brand new project has no directory, so the lease is unknowable. Its
    very first run must not be the one that can never start."""
    assert not (config.PROJECTS_DIR / "alpha").exists()
    assert worker.workspace_leased("alpha") is False


# --- what a refused run looks like -------------------------------------------


def test_the_wrappers_nest_with_the_lease_inside_the_scope(monkeypatch, tmp_path):
    """Composed the other way the lock holder would sit in the portal's own
    cgroup, so restarting the service would kill it while the agent carried on
    in its sibling scope - releasing the lease on a workspace that is still
    occupied, which is the precise failure this all exists to prevent."""
    seen: dict = {}

    async def fake_exec(*argv, **kwargs):
        seen["argv"] = list(argv)
        raise FileNotFoundError("stop here")

    monkeypatch.setattr(agent_runner, "build_cmd", lambda *a, **k: ["claude", "-p"])
    monkeypatch.setattr(runlimit, "configured_max_bytes", lambda: 2 * 1024**3)
    monkeypatch.setattr(runlimit, "available", lambda refresh=False: True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    ws = tmp_path / "ws"
    asyncio.run(agent_runner.run_claude(
        "prompt", ws, "model", timeout_min=1, lock_dir=ws,
    ))

    argv = seen["argv"]
    assert argv[0] == "systemd-run", "the memory scope must be the outer wrapper"
    assert argv.index("systemd-run") < argv.index("flock") < argv.index("claude")


def test_a_refusal_is_reported_as_a_refusal_not_a_crash(monkeypatch, tmp_path, project):
    """A run that never started is not a broken project: no allowance was
    spent, no file was touched, and the next run must not be sent to tidy up
    "uncommitted work" that is another agent's live edits."""
    ws = _workspace_for("alpha")
    monkeypatch.setattr(
        agent_runner, "build_cmd",
        lambda *a, **k: [sys.executable, "-c", "import sys; sys.exit(0)"],
    )
    monkeypatch.setattr(runlimit, "configured_max_bytes", lambda: None)
    orphan_notes: list = []
    monkeypatch.setattr(worker, "_note_orphaned_work",
                        lambda *a, **k: orphan_notes.append(a))

    holder = _holder(ws)
    run_id = db.create_run(project["id"], "build", "claude-opus-5")
    try:
        asyncio.run(worker.run_project_task(project, "build", run_id=run_id))
    finally:
        holder.kill()
        holder.wait()

    row = db.get_run(run_id)
    assert row["status"] == "error"
    assert "still holds" in (row["summary"] or "")
    assert not orphan_notes, "a refused run must not leave an uncommitted-work warning"
    assert any("still holds" in (j["content_md"] or "")
               for j in db.list_journal(project["id"]))


def test_an_agent_that_merely_exits_75_is_not_mistaken_for_a_refusal(tmp_path, monkeypatch):
    """`--conflict-exit-code` is only meaningful on a spawn we actually
    wrapped. With no lock directory the code is just an exit code, and reading
    it as a refusal would hide a real failure behind a reassuring message."""
    monkeypatch.setattr(
        agent_runner, "build_cmd",
        lambda *a, **k: [sys.executable, "-c", "import sys; sys.exit(75)"],
    )
    monkeypatch.setattr(runlimit, "configured_max_bytes", lambda: None)
    result = asyncio.run(agent_runner.run_claude(
        "prompt", tmp_path / "ws", "model", timeout_min=1, lock_dir=None,
    ))
    assert result.lock_conflict is False
    assert result.ok is False


def test_a_real_refusal_sets_the_flag(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(
        agent_runner, "build_cmd",
        lambda *a, **k: [sys.executable, "-c", "import sys; sys.exit(0)"],
    )
    monkeypatch.setattr(runlimit, "configured_max_bytes", lambda: None)
    holder = _holder(ws)
    try:
        result = asyncio.run(agent_runner.run_claude(
            "prompt", ws, "model", timeout_min=1, lock_dir=ws,
        ))
    finally:
        holder.kill()
        holder.wait()
    assert result.lock_conflict is True
    assert result.ok is False


# --- against real systemd ----------------------------------------------------
#
# The nesting test above reads the argv. This one runs it. The distinction
# earned its place: the first draft of `worklock.wrap` put a `--` before the
# command, the way every other wrapper in this codebase does, and `flock(1)` is
# the one that does not accept it - which failed with "failed to execute --" and
# would have presented in production as runs that produce no output at all. No
# argv assertion catches that. Skipped where there is no user systemd manager,
# matching test_restart_survivors.py.


@pytest.mark.asyncio
@pytest.mark.skipif(
    not runlimit.available(refresh=True),
    reason="no user systemd manager to make a transient scope in",
)
async def test_the_lease_and_the_memory_scope_actually_compose(tmp_path, monkeypatch):
    """Both wrappers, for real, at once - and the two things the inner one
    could plausibly have broken.

    `runlimit` resolves a run's cgroup from the pid of the process it spawned,
    and cancel/timeout kill that process's whole group. Putting a `flock` in
    between moves what that pid *is*, so both are re-checked here rather than
    assumed: the pid must still land in the run's own scope (or the memory watch
    silently reports the portal's numbers as the run's), and it must still be
    its own process-group leader (or the cancel button stops reaching the agent).
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    run_id = db.create_run(
        db.create_project("Beta", stage="active", slug="beta")["id"],
        "build", "claude-opus-5",
    )
    monkeypatch.setattr(
        agent_runner, "build_cmd",
        lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(3)"],
    )
    monkeypatch.setattr(runlimit, "configured_max_bytes", lambda: 2 * 1024**3)

    task = asyncio.create_task(agent_runner.run_claude(
        "prompt", ws, "model", timeout_min=1, run_id=run_id, lock_dir=ws,
    ))
    try:
        for _ in range(60):
            await asyncio.sleep(0.05)
            if worklock.is_busy(ws) is True:
                break
        assert worklock.is_busy(ws) is True, "the run never took its workspace lease"

        unit = db.get_run(run_id)["scope_unit"]
        assert unit and runlimit.scope_is_active(unit) is True

        pid = agent_runner._ACTIVE_PROCS[run_id].pid  # noqa: SLF001
        cgroup = Path(f"/proc/{pid}/cgroup").read_text().strip()
        assert cgroup.rsplit("/", 1)[-1] == unit, (
            "the lease wrapper moved the run's pid out of its own memory scope"
        )
        assert os.getpgid(pid) == pid, (
            "the lease wrapper cost the run its process group, so cancel and "
            "the timeout kill would no longer reach the agent"
        )
    finally:
        await task

    assert _await_free(ws), "the lease outlived the run that took it"


def test_a_run_in_a_free_workspace_is_untouched_by_any_of_this(tmp_path, monkeypatch):
    """The ordinary case, which is every run: the lease is invisible."""
    ws = tmp_path / "ws"
    monkeypatch.setattr(
        agent_runner, "build_cmd",
        lambda *a, **k: [sys.executable, "-c", "print('{\"type\":\"result\","
                         "\"subtype\":\"success\",\"result\":\"done\"}')"],
    )
    monkeypatch.setattr(runlimit, "configured_max_bytes", lambda: None)
    result = asyncio.run(agent_runner.run_claude(
        "prompt", ws, "model", timeout_min=1, lock_dir=ws,
    ))
    assert result.lock_conflict is False
    assert result.ok is True
    assert result.result_text == "done"

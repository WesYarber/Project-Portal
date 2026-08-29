"""The last hole in the restart story: a run whose scope is held open forever.

Filed as todo #436 by the run that shipped the stray sweep, which could not
reach this case and said so. The chain, every step of it routine on this box:

    1. A run records its scope in `runs.scope_unit`.
    2. The portal self-updates and restarts. `db._reconcile_orphaned_runs` asks
       systemd, hears "active", and correctly ADOPTS the run rather than
       declaring it dead - the workspace stays locked, which is the point.
    3. The agent finishes, detaching a preview server on its way out.
    4. The adopting process has no `Popen` to await, so `worker._reap_adopted`
       must settle the row, and its only signal was the scope dying. The
       preview server holds the scope active. It never dies.
    5. The row says 'running' forever -> `db.running_project_ids()` lists the
       project forever -> THE PROJECT CAN NEVER GET ANOTHER RUN, with nothing
       in the UI to say why.

And the stray sweep cannot break the deadlock either, because it reads the same
'running' rows to build its protected set: the leftover holding the scope open
is precisely the thing protected from being moved out of it.

The workspace lease is the way out. `worklock.wrap` passes `--close`, so the
lease descriptor is NOT inherited by anything the agent detaches - which makes a
definitely-free lease proof that the agent has exited, whatever its scope says.

The danger of that signal is the mirror image of the bug, and worse: a run that
never took a lease has a workspace that reads free the whole time it runs, so
trusting a free lease indiscriminately marks LIVE runs dead. That is why the
lease is recorded per-run and why "no lease recorded" is a hard no here. The
tests below are weighted accordingly - more of them guard against settling a
live run than against failing to settle a dead one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import module_state

from app import db, runlimit, worker, worklock

# See the note in test_shared_leases.py: probed for real, but without leaving the
# answer in a process-wide global that collection fills before any fixture runs.
# The worker state this file used to clear by hand is cleared for every test in
# the suite by `conftest`'s `module_state_is_never_inherited`.
_HAVE_FLOCK = module_state.probe_without_memoizing(
    "app.worklock", "_available", worklock.available)


@pytest.fixture
def project():
    return db.create_project("Alpha", stage="active", build_approved=True, slug="alpha")


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "projects" / "alpha"
    ws.mkdir(parents=True)
    return ws


def _adopted_run(project_id: int, workspace: Path | None) -> int:
    """A run that outlived the process which started it: a scope name minted by
    some *other* pid, so `runlimit.minted_here` does not claim it."""
    run_id = db.create_run(project_id, "build", "claude-opus-5")
    db.set_run_scope(run_id, f"portal-run-{run_id}-{os.getpid() + 1}-1.scope")
    if workspace is not None:
        db.set_run_lease(run_id, str(workspace))
    return run_id


def _scope_held_open(monkeypatch):
    """systemd answering the way it does when a detached helper is still in the
    scope: the unit is alive long after the agent has gone."""
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: True)


def _lease_reads(monkeypatch, answer):
    monkeypatch.setattr(worker.worklock, "is_busy", lambda lock_dir: answer)


def _confirmed(monkeypatch):
    """Collapse the confirmation window, for the tests that are not about it."""
    monkeypatch.setattr(worker, "LEASE_FREE_CONFIRM_S", 0.0)


# --- the defect itself, reproduced -------------------------------------------


def test_a_held_open_scope_strands_the_run_when_the_lease_is_the_only_other_signal(
    project, workspace, monkeypatch
):
    """The bug, before the fix reaches it: the scope signal alone never settles
    this row. Every other test here is a solution to THIS, not to a hypothesis.
    """
    run_id = _adopted_run(project["id"], workspace)
    _scope_held_open(monkeypatch)
    # The agent is still in there, so the lease signal declines too.
    _lease_reads(monkeypatch, True)

    for _ in range(5):
        worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "running"
    assert project["id"] in db.running_project_ids()


def test_the_lease_settles_a_run_whose_scope_will_never_die(project, workspace, monkeypatch):
    """The fix. The agent has exited - its lease is free - while the preview
    server it detached keeps the scope active for good."""
    run_id = _adopted_run(project["id"], workspace)
    _scope_held_open(monkeypatch)
    _lease_reads(monkeypatch, False)
    _confirmed(monkeypatch)

    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "error"
    assert row["summary"] == worker.STRANDED_SUMMARY
    assert project["id"] not in db.running_project_ids()


def test_settling_the_row_releases_the_other_half_of_the_deadlock(
    project, workspace, monkeypatch
):
    """The two mechanisms deadlocked: the sweep protects scopes of 'running'
    rows, so the leftover holding the scope open was the one thing it could not
    move. Settling the row is what unprotects the unit."""
    run_id = _adopted_run(project["id"], workspace)
    unit = db.get_run(run_id)["scope_unit"]
    monkeypatch.setattr(runlimit, "known_scopes", lambda: [])
    _scope_held_open(monkeypatch)
    _lease_reads(monkeypatch, False)
    _confirmed(monkeypatch)

    assert unit in worker._protected_scopes()

    worker._reap_adopted()

    assert unit not in worker._protected_scopes()


def test_a_stranded_run_asks_for_a_sweep_on_the_next_tick(project, workspace, monkeypatch):
    """Settling proves there is something left in that scope to rehouse, so the
    ten-minute sweep timer is cleared rather than waited out."""
    _adopted_run(project["id"], workspace)
    _scope_held_open(monkeypatch)
    _lease_reads(monkeypatch, False)
    _confirmed(monkeypatch)
    worker._last_stray_sweep = time.monotonic()

    worker._reap_adopted()

    assert worker._last_stray_sweep is None


def test_an_ordinary_dead_scope_still_settles_the_old_way(project, workspace, monkeypatch):
    """The lease is an ADDITION. A scope that is simply gone must still settle
    with the original summary, and must not be made to wait out a confirmation
    window that exists for the other path."""
    run_id = _adopted_run(project["id"], workspace)
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: False)
    _lease_reads(monkeypatch, lambda *_: pytest.fail("should not need the lease"))

    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "error"
    assert row["summary"] == worker.ADOPTED_SUMMARY


# --- guards: everything that must NOT settle a run ---------------------------


def test_a_run_that_took_no_lease_is_never_settled_by_a_free_workspace(
    project, workspace, monkeypatch
):
    """THE test in this file. A run that took no lease has a directory that
    reads free for the whole time it runs, so if "no lease recorded" were
    treated as "lease is free" it would be marked dead the moment it started -
    a far worse bug than the one this file fixes, and the reason the column is
    written only where the lease actually applied.

    Every `run_claude` caller leases something as of #435, so the live cases are
    now the fail-open ones: a machine with no `flock(1)`, or one whose `flock`
    lacks `--close`. Those spawn unleased exactly as reflect and compaction used
    to, and this is what stops the reaper killing them."""
    run_id = _adopted_run(project["id"], None)
    assert db.get_run(run_id)["lock_dir"] is None
    _scope_held_open(monkeypatch)
    # The workspace genuinely is free. It always was; it means nothing here.
    _lease_reads(monkeypatch, False)
    _confirmed(monkeypatch)

    worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "running"


def test_a_busy_lease_never_settles_a_run(project, workspace, monkeypatch):
    """The normal answer for a live adopted run: it is in there working."""
    run_id = _adopted_run(project["id"], workspace)
    _scope_held_open(monkeypatch)
    _lease_reads(monkeypatch, True)
    _confirmed(monkeypatch)

    worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "running"


def test_an_unknowable_lease_never_settles_a_run(project, workspace, monkeypatch):
    """`None` is "could not ask" - a deleted workspace, a filesystem with no
    BSD locks. Reading it as free would unlock a workspace on a failed probe,
    which is the shape of the original incident."""
    run_id = _adopted_run(project["id"], workspace)
    _scope_held_open(monkeypatch)
    _lease_reads(monkeypatch, None)
    _confirmed(monkeypatch)

    worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "running"


def test_the_lease_path_leaves_this_process_own_runs_alone(project, workspace, monkeypatch):
    """A run in `_inflight` settles itself. The lease of a live run reads busy
    anyway, but the guard must not depend on that - a run mid-spawn has not
    taken its lock yet."""
    run_id = db.create_run(project["id"], "build", "claude-opus-5")
    db.set_run_scope(run_id, f"portal-run-{run_id}-{os.getpid() + 1}-1.scope")
    db.set_run_lease(run_id, str(workspace))
    worker._inflight[run_id] = object()
    _scope_held_open(monkeypatch)
    _lease_reads(monkeypatch, False)
    _confirmed(monkeypatch)

    worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "running"


def test_the_lease_path_never_touches_a_run_this_process_minted(project, workspace, monkeypatch):
    """The reflect registers in `_inflight` under a fixed slot key rather than
    its run id, so the pid in the scope name is what says "mine". It also takes
    no lease, but this must hold even if it ever does."""
    run_id = db.create_run(None, "reflect", "claude-opus-5")
    db.set_run_scope(run_id, f"portal-run-{run_id}-{os.getpid()}-1.scope")
    db.set_run_lease(run_id, str(workspace))
    _scope_held_open(monkeypatch)
    _lease_reads(monkeypatch, False)
    _confirmed(monkeypatch)

    worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "running"


# --- the confirmation window --------------------------------------------------


def test_one_free_reading_is_not_enough(project, workspace, monkeypatch):
    """The lease is written down when the process is SPAWNED, and `flock` takes
    it a moment later - so there is a window where the row claims a lease nobody
    holds yet. A single free reading landing in it would settle a run that has
    not started working."""
    run_id = _adopted_run(project["id"], workspace)
    _scope_held_open(monkeypatch)
    _lease_reads(monkeypatch, False)

    worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "running"


def test_two_readings_a_window_apart_do_settle_it(project, workspace, monkeypatch):
    """The other half: the wait is bounded, not indefinite. Time is advanced
    rather than slept, so this pins the arithmetic without costing 120 seconds.
    """
    run_id = _adopted_run(project["id"], workspace)
    _scope_held_open(monkeypatch)
    _lease_reads(monkeypatch, False)

    clock = [1000.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])

    worker._reap_adopted()
    assert db.get_run(run_id)["status"] == "running"

    clock[0] += worker.LEASE_FREE_CONFIRM_S + 1
    worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "error"


def test_a_busy_reading_restarts_the_window(project, workspace, monkeypatch):
    """An agent whose lease momentarily reads free (or that we probed during the
    spawn window) and then reads busy is alive. Its earlier free reading must be
    void, not banked - otherwise a single spurious reading arms the settle for
    the rest of the run's life."""
    run_id = _adopted_run(project["id"], workspace)
    _scope_held_open(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])

    _lease_reads(monkeypatch, False)
    worker._reap_adopted()

    clock[0] += worker.LEASE_FREE_CONFIRM_S + 1
    _lease_reads(monkeypatch, True)
    worker._reap_adopted()
    assert db.get_run(run_id)["status"] == "running"
    assert run_id not in worker._lease_free_since

    # Free again, but the clock starts over from here.
    _lease_reads(monkeypatch, False)
    worker._reap_adopted()
    assert db.get_run(run_id)["status"] == "running"

    clock[0] += worker.LEASE_FREE_CONFIRM_S + 1
    worker._reap_adopted()
    assert db.get_run(run_id)["status"] == "error"


def test_an_unknowable_reading_also_restarts_the_window(project, workspace, monkeypatch):
    """Same rule for `None`: a probe that failed cannot count toward proving the
    workspace has been free the whole time."""
    run_id = _adopted_run(project["id"], workspace)
    _scope_held_open(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])

    _lease_reads(monkeypatch, False)
    worker._reap_adopted()
    assert run_id in worker._lease_free_since

    _lease_reads(monkeypatch, None)
    worker._reap_adopted()
    assert run_id not in worker._lease_free_since


def test_the_window_state_does_not_leak_for_runs_that_ended_another_way(
    project, workspace, monkeypatch
):
    """A run canceled from the portal settles without passing through here, so
    nothing else would ever drop its entry."""
    run_id = _adopted_run(project["id"], workspace)
    _scope_held_open(monkeypatch)
    _lease_reads(monkeypatch, False)

    worker._reap_adopted()
    assert run_id in worker._lease_free_since

    db.finish_run(run_id, "cancelled", summary="Canceled from the portal.")
    worker._reap_adopted()

    assert worker._lease_free_since == {}


# --- the recording side -------------------------------------------------------


def test_the_lease_survives_the_process_that_wrote_it(project, workspace):
    """`running_run_handles` is what a foreign portal process reads. Both
    handles have to come back off the row, or the reaper has nothing to act on.
    """
    run_id = _adopted_run(project["id"], workspace)

    handles = {h.run_id: h for h in db.running_run_handles()}

    assert handles[run_id].lock_dir == str(workspace)
    assert handles[run_id].scope_unit == db.get_run(run_id)["scope_unit"]
    assert handles[run_id].project_id == project["id"]


def _spawn(monkeypatch, run_id, cwd, lock_dir):
    """A whole `run_claude` against a trivial process that exits at once, so the
    recording side is exercised through the real code path rather than by
    calling `set_run_lease` by hand."""
    import asyncio
    import sys

    from app import agent_runner

    monkeypatch.setattr(
        agent_runner, "build_cmd",
        lambda *a, **k: [sys.executable, "-c", "print('{\"type\":\"result\","
                         "\"subtype\":\"success\",\"result\":\"done\"}')"],
    )
    asyncio.run(agent_runner.run_claude(
        "prompt", cwd, "model", timeout_min=1, run_id=run_id, lock_dir=lock_dir,
    ))


@pytest.mark.skipif(not _HAVE_FLOCK, reason="needs flock(1) with --close")
def test_a_leased_run_records_the_workspace_it_leased(project, workspace, monkeypatch):
    run_id = db.create_run(project["id"], "build", "claude-opus-5")

    _spawn(monkeypatch, run_id, workspace, workspace)

    assert db.get_run(run_id)["lock_dir"] == str(workspace)


def test_a_run_spawned_without_a_lock_directory_records_no_lease(
    project, workspace, monkeypatch
):
    """`run_claude`'s own behavior for a caller that asks for no lease. No
    caller does since #435, so this pins the default rather than a live path -
    which matters because that default is what a new caller inherits."""
    run_id = db.create_run(project["id"], "reflect", "claude-opus-5")

    _spawn(monkeypatch, run_id, workspace, None)

    assert db.get_run(run_id)["lock_dir"] is None


def test_leasing_that_failed_open_records_no_lease_either(project, workspace, monkeypatch):
    """The subtle one. `worklock` fails open by design: no `flock(1)`, or one
    without `--close`, and runs spawn exactly as before - unleased. The row must
    not claim a lease that was never taken, or the reaper would settle every run
    on such a machine the moment it started. This is why `leased` is read back
    off the argv that was actually built instead of from `lock_dir is not None`.
    """
    from app import worklock as wl

    monkeypatch.setattr(wl, "available", lambda refresh=False: False)
    run_id = db.create_run(project["id"], "build", "claude-opus-5")

    _spawn(monkeypatch, run_id, workspace, workspace)

    assert db.get_run(run_id)["lock_dir"] is None


def test_a_run_with_no_lease_reports_none_rather_than_being_absent(project):
    """The reflect still has to appear in the list - it is reaped by the scope
    signal. Only its lease is missing."""
    run_id = db.create_run(None, "reflect", "claude-opus-5")
    db.set_run_scope(run_id, "portal-run-1-2-3.scope")

    handles = {h.run_id: h for h in db.running_run_handles()}

    assert run_id in handles
    assert handles[run_id].lock_dir is None


# --- against the real kernel --------------------------------------------------


@pytest.mark.skipif(not _HAVE_FLOCK, reason="needs flock(1) with --close")
def test_a_detached_grandchild_holds_the_scope_but_not_the_lease(tmp_path):
    """The claim the whole fix rests on, checked against the kernel rather than
    against the man page.

    A run's shape, in miniature: `flock` takes the lease, the agent detaches a
    server that outlives it, the agent exits. If `--close` works, the lease is
    gone the instant the agent is - which is exactly the difference between this
    signal and the scope, since the detached process would keep a cgroup alive.

    Started through `/bin/bash -c` deliberately: a shell passes its descriptors
    straight through, where CPython's `subprocess` quietly closes them - the
    trap that made an earlier version of this check vacuous.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    marker = tmp_path / "still-here"

    assert worklock.is_busy(ws) is False

    # The agent: takes the lease, detaches a grandchild, exits immediately.
    subprocess.run(
        [
            shutil.which("flock"), "--close", "--nonblock", str(ws),
            "/bin/bash", "-c",
            f"setsid bash -c 'touch {marker}; sleep 30' >/dev/null 2>&1 < /dev/null &",
        ],
        check=True, timeout=30,
    )

    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "the detached grandchild never started"

    try:
        # The grandchild is alive and would hold a cgroup open. The lease is
        # already free, because `--close` kept it out of its descriptor set.
        assert worklock.is_busy(ws) is False
    finally:
        # By pid, found via the tmp_path marker in the command line. NOT
        # `pkill -f`: a bare command match has killed the live agent run
        # executing the suite twice on this box, once per incident write-up.
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                cmdline = (pid_dir / "cmdline").read_bytes()
            except OSError:
                continue
            if str(marker).encode() in cmdline:
                try:
                    os.kill(int(pid_dir.name), 9)
                except OSError:
                    pass


@pytest.mark.skipif(not _HAVE_FLOCK, reason="needs flock(1) with --close")
def test_a_live_agent_reads_busy_through_the_same_path(tmp_path):
    """The counterpart, so the test above is not just observing that `is_busy`
    always says False. A process actually holding the lease reads busy."""
    ws = tmp_path / "ws"
    ws.mkdir()

    holder = subprocess.Popen(
        [shutil.which("flock"), "--close", "--nonblock", str(ws), "sleep", "30"],
    )
    try:
        deadline = time.monotonic() + 10
        while worklock.is_busy(ws) is not True and time.monotonic() < deadline:
            time.sleep(0.05)
        assert worklock.is_busy(ws) is True
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert worklock.is_busy(ws) is False

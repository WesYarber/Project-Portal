"""A service restart must never hand an occupied workspace to a second agent.

The incident this file exists for, 2026-07-29 01:19 UTC. A self-improvement run
committed, the portal armed a 3-second restart timer, and then:

    01:19:09  the timer finally fires - 59 SECONDS late
    01:19:10  the worker, still alive, starts run 671 (project-portal)
    01:19:11  the worker starts run 672 (home-work)
    01:19:19  the service stops and starts
    01:19:20  init_db() marks 671 and 672 'error' - "Orphaned"
    01:19:20  the fresh worker sees both projects idle and starts 673 and 674
              - the same two projects, the same two workspaces
    01:26     671 and 673 are both alive, both editing the portal's own source

Two independent defects, and this file pins the fix for each:

1. `RESTART_DELAY_SEC` was a hope. A transient `systemd-run --on-active` timer
   inherits `AccuracySec=1min`, and `_pending_restart` is cleared *before* the
   timer is armed, so nothing stopped the worker scheduling into the gap.

2. `init_db`'s sweep asserted that "nothing can still be running at startup".
   That was true while a run was a child of the service's cgroup. It stopped
   being true when `runlimit.wrap` began putting each run in its own transient
   scope - a SIBLING of project-portal.service, which a service restart leaves
   completely alone. The sweep therefore did not merely mislabel a live run: by
   clearing the only column `running_project_ids()` reads, it was the step that
   unlocked the workspace.

Every claim below is checked by deleting the fix and watching this file fail.
"""
from __future__ import annotations

import os



import pytest

from app import agent_runner, db, runlimit, worker


@pytest.fixture(autouse=True)
def _clean_worker_state():
    """`_inflight` and `_restarting` are module state that outlives the
    per-test database. `_restarting` especially: in production it is a one-way
    latch that only the process ending clears, so a test that arms it would
    otherwise silently stop every later test from starting a run."""
    worker._inflight.clear()
    worker._restarting = False
    yield
    worker._inflight.clear()
    worker._restarting = False


@pytest.fixture
def project():
    return db.create_project("Alpha", stage="active", build_approved=True, slug="alpha")


def _running_run(project_id: int, unit: str | None) -> int:
    run_id = db.create_run(project_id, "build", "claude-opus-5")
    db.set_run_scope(run_id, unit)
    return run_id


def _restart_db(monkeypatch, live_units: set[str]):
    """Re-run the boot path against the same database, with systemd answering
    for `live_units`. This is what a service restart does."""
    monkeypatch.setattr(
        runlimit, "scope_is_active", lambda unit: unit in live_units
    )
    db.init_db()


# --- defect 2: the boot sweep now asks instead of assuming --------------------


def test_a_run_whose_scope_is_still_alive_is_adopted_not_orphaned(project, monkeypatch):
    run_id = _running_run(project["id"], "portal-run-671-123-1.scope")

    _restart_db(monkeypatch, {"portal-run-671-123-1.scope"})

    assert db.get_run(run_id)["status"] == "running"


def test_an_adopted_run_keeps_its_project_locked(project, monkeypatch):
    """The whole point. `running_project_ids()` is the only mutual exclusion
    there is, and it reads nothing but the status column - so a survivor whose
    row gets cleared is a workspace handed to the next agent that asks."""
    _running_run(project["id"], "portal-run-671-123-1.scope")

    _restart_db(monkeypatch, {"portal-run-671-123-1.scope"})

    assert project["id"] in db.running_project_ids()
    assert db.is_project_running(project["id"]) is True


def test_a_run_whose_scope_is_gone_is_still_orphaned(project, monkeypatch):
    """The old behavior has to survive for the case it was right about."""
    run_id = _running_run(project["id"], "portal-run-660-99-1.scope")

    _restart_db(monkeypatch, set())

    row = db.get_run(run_id)
    assert row["status"] == "error"
    assert row["summary"] == db.ORPHAN_SUMMARY
    assert project["id"] not in db.running_project_ids()


def test_a_run_that_was_never_scoped_is_orphaned_without_asking(project, monkeypatch):
    """No unit means capping was off or systemd was absent, so the run really
    was a child of the service and really did die with it. Asking systemd about
    a unit that never existed would be a guess dressed as a fact."""
    run_id = _running_run(project["id"], None)
    asked: list[str] = []

    def spy(unit):
        asked.append(unit)
        return True

    monkeypatch.setattr(runlimit, "scope_is_active", spy)
    db.init_db()

    assert asked == []
    assert db.get_run(run_id)["status"] == "error"


def test_an_unknown_answer_from_systemd_does_not_invent_a_survivor(project, monkeypatch):
    """`scope_is_active` returns None when it could not find out - no user
    manager, no session bus, a timeout. That is not evidence of life, and
    treating it as such would lock a project out forever on any box where the
    probe cannot run. Unknown keeps the long-standing behavior."""
    run_id = _running_run(project["id"], "portal-run-671-123-1.scope")

    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: None)
    db.init_db()

    assert db.get_run(run_id)["status"] == "error"


def test_the_survivor_is_journaled_so_wes_can_see_why_it_is_still_running(project, monkeypatch):
    _running_run(project["id"], "portal-run-671-123-1.scope")

    _restart_db(monkeypatch, {"portal-run-671-123-1.scope"})

    bodies = [e["content_md"] for e in db.list_journal(project["id"])]
    assert any("survived the restart" in b for b in bodies)


def test_two_runs_are_settled_independently(project, monkeypatch):
    """The sweep was one blanket UPDATE; per-row decisions are the change. A
    second project is what proves the loop is not just early-returning."""
    other = db.create_project("Beta", stage="active", build_approved=True, slug="beta")
    alive = _running_run(project["id"], "portal-run-671-123-1.scope")
    dead = _running_run(other["id"], "portal-run-672-123-2.scope")

    _restart_db(monkeypatch, {"portal-run-671-123-1.scope"})

    assert db.get_run(alive)["status"] == "running"
    assert db.get_run(dead)["status"] == "error"
    assert db.running_project_ids() == {project["id"]}


# --- the other half: an adopted run has to be able to END ---------------------


def test_the_reaper_settles_an_adopted_run_once_its_scope_dies(project, monkeypatch):
    """Adopting keeps the workspace locked, which is the point - but the
    adopting process has no `Popen` to await, so without this the row would say
    'running' forever and the project would be locked out for good."""
    run_id = _running_run(project["id"], "portal-run-671-123-1.scope")
    _restart_db(monkeypatch, {"portal-run-671-123-1.scope"})

    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: True)
    worker._reap_adopted()
    assert db.get_run(run_id)["status"] == "running"

    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: False)
    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "error"
    assert row["summary"] == worker.ADOPTED_SUMMARY
    assert project["id"] not in db.running_project_ids()


def test_the_reaper_leaves_this_process_own_runs_alone(project, monkeypatch):
    """A run in `_inflight` is supervised here and settles itself. Reaping it
    on a stale systemd answer would mark a live run dead and free its
    workspace - the very bug, arriving by the other door."""
    run_id = _running_run(project["id"], "portal-run-700-1-1.scope")
    # A stand-in for the asyncio.Task the real thing holds: `_reap_adopted`
    # only ever asks whether the key is present.
    worker._inflight[run_id] = object()

    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: False)
    worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "running"


def test_the_reaper_leaves_an_unscoped_run_alone(project, monkeypatch):
    """Absence of a scope is not evidence the run is over, and reaping on it
    would settle every live run on a machine with capping switched off."""
    run_id = _running_run(project["id"], None)

    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: False)
    worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "running"


def test_the_reaper_never_touches_a_run_this_process_minted(project, monkeypatch):
    """The reflect and the compaction create a real `runs` row and then register
    in `_inflight` under a fixed slot key (-1, -2) instead of under their run
    id, so `run_id in _inflight` does not recognize them. Their scope also dies
    a few seconds before their row settles - the window where the report is
    parsed and journaled - so trusting `_inflight` alone would mark a perfectly
    healthy daily reflect as an error. The pid in the scope name is what says
    "this one is mine"."""
    reflect = db.create_run(None, "reflect", "claude-opus-5")
    db.set_run_scope(reflect, f"portal-run-{reflect}-{os.getpid()}-1.scope")
    assert reflect not in worker._inflight

    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: False)
    worker._reap_adopted()

    assert db.get_run(reflect)["status"] == "running"


def test_minted_here_reads_the_pid_rather_than_matching_a_substring():
    """`f"-{os.getpid()}-" in unit` also matches the RUN ID segment, so a portal
    whose pid happened to equal a run id would refuse to reap that run for as
    long as the process lived."""
    import os

    pid = os.getpid()
    assert runlimit.minted_here(f"portal-run-9-{pid}-1.scope") is True
    assert runlimit.minted_here(f"portal-run-{pid}-4242-1.scope") is False
    assert runlimit.minted_here(f"portal-run-x-{pid}-2.scope") is True
    assert runlimit.minted_here("portal-run-9-4242-1.scope") is False
    assert runlimit.minted_here(None) is False
    assert runlimit.minted_here("something-else.scope") is False


def test_a_freshly_minted_scope_name_is_recognized_as_ours():
    """Pins the two halves against each other: if `scope_name`'s format ever
    changes, `minted_here` must change with it or the reaper silently starts
    settling this process's own reflect runs."""
    name = runlimit.scope_name(4242)
    try:
        assert runlimit.minted_here(name) is True
    finally:
        runlimit.forget_scope(4242)


def test_the_reaper_does_not_settle_on_an_unknown_answer(project, monkeypatch):
    """Only a definite "the scope is gone" ends a run. `None` means the probe
    failed, and a failed probe must not be able to unlock a workspace."""
    run_id = _running_run(project["id"], "portal-run-671-123-1.scope")

    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: None)
    worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "running"


# --- canceling a run that outlived the process that started it ---------------


def test_cancel_stops_the_scope_of_an_adopted_run(project, monkeypatch):
    """Before this, cancel on an adopted run settled the row and killed
    nothing: `_ACTIVE_PROCS` is in-memory and the restart emptied it. That
    leaves the agent editing a workspace the portal now believes is free."""
    run_id = _running_run(project["id"], "portal-run-671-123-1.scope")
    stopped: list[str] = []

    monkeypatch.setattr(agent_runner, "cancel_run", lambda rid: False)
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: True)
    monkeypatch.setattr(runlimit, "stop_scope", lambda unit: stopped.append(unit) or True)

    assert worker.cancel_run(run_id) == "cancelled"
    assert stopped == ["portal-run-671-123-1.scope"]
    assert db.get_run(run_id)["status"] == "cancelled"


def test_cancel_still_reports_orphaned_when_the_scope_is_gone_too(project, monkeypatch):
    run_id = _running_run(project["id"], "portal-run-671-123-1.scope")

    monkeypatch.setattr(agent_runner, "cancel_run", lambda rid: False)
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: False)
    monkeypatch.setattr(runlimit, "stop_scope", lambda unit: pytest.fail("must not stop a dead scope"))

    assert worker.cancel_run(run_id) == "orphaned"
    assert db.get_run(run_id)["status"] == "cancelled"


def test_cancel_prefers_the_live_process_over_the_scope(project, monkeypatch):
    """A run this process owns is killed by process group, as before - the
    scope path is the fallback, not the new normal."""
    run_id = _running_run(project["id"], "portal-run-700-1-1.scope")

    monkeypatch.setattr(agent_runner, "cancel_run", lambda rid: True)
    monkeypatch.setattr(runlimit, "stop_scope", lambda unit: pytest.fail("should not reach the scope"))

    assert worker.cancel_run(run_id) == "cancelled"


# --- defect 1: the restart latch ---------------------------------------------


@pytest.fixture
def armed(monkeypatch):
    """A recorder in place of systemd-run, so `_fire_restart` can be called."""
    calls: list[list[str]] = []

    def record(cmd, **kwargs):
        calls.append(cmd)

        class Done:
            returncode = 0
        return Done()

    monkeypatch.setattr(worker.subprocess, "run", record)
    monkeypatch.setattr(worker, "_pending_restart", None)
    return calls


def test_the_restart_timer_pins_its_accuracy(project, armed):
    """Without this the 3-second delay was a 59-second one: a transient
    `systemd-run --on-active` timer inherits AccuracySec=1min."""
    worker._fire_restart(project["id"], "abcdef1234")

    assert armed, "no timer was armed"
    assert "--timer-property=AccuracySec=1ms" in armed[0]


def test_arming_the_restart_latches_the_worker(project, armed):
    assert worker.restart_armed() is False
    worker._fire_restart(project["id"], "abcdef1234")
    assert worker.restart_armed() is True


def test_a_failed_arming_does_not_latch(project, monkeypatch):
    """If the timer could not be scheduled the process is NOT about to die, and
    latching would silently stop the board until somebody noticed."""
    import subprocess as sp

    def boom(cmd, **kwargs):
        raise sp.CalledProcessError(1, cmd)

    monkeypatch.setattr(worker.subprocess, "run", boom)
    worker._fire_restart(project["id"], "abcdef1234")

    assert worker.restart_armed() is False


@pytest.mark.asyncio
async def test_no_run_starts_once_the_restart_is_armed(project, armed, monkeypatch):
    """The 59-second window, closed. The tick is what spawned runs 671 and 672
    into a process that was already being torn down."""
    started: list[bool] = []

    async def start(*_a, **_k):
        started.append(True)
        # Occupy the slot as the real `_start_one` does. Without this the fill
        # loop in `_tick` never terminates, so removing the latch would hang the
        # suite instead of failing this test - and a mutation that hangs is one
        # nobody reads the result of.
        worker._inflight[len(started)] = object()
        return True

    monkeypatch.setattr(worker, "_start_one", start)
    worker._fire_restart(project["id"], "abcdef1234")

    await worker._tick()

    assert started == []


@pytest.mark.asyncio
async def test_start_one_refuses_on_its_own(project, armed):
    """Guarded in `_tick` and again here, because `_tick`'s pending-restart
    branch deliberately lets manual runs through right up until the arming."""
    worker._fire_restart(project["id"], "abcdef1234")

    assert await worker._start_one() is False


# --- the handle itself --------------------------------------------------------


def test_the_scope_unit_is_read_back_out_of_the_argv():
    """Recorded from the argv rather than asked of `scope_name`, which happily
    mints a name for a run that was never scoped."""
    wrapped = ["systemd-run", "--user", "--scope", "--unit=portal-run-9-1-1.scope",
               "--", "claude", "-p"]
    assert runlimit.unit_of(wrapped) == "portal-run-9-1-1.scope"
    assert runlimit.unit_of(["claude", "-p"]) is None


def test_an_unwrapped_spawn_records_no_scope(project, monkeypatch):
    """Capping off means no scope, and a NULL column is what tells the boot
    sweep not to bother asking."""
    monkeypatch.setattr(runlimit, "configured_max_bytes", lambda: None)
    run_id = db.create_run(project["id"], "build", "claude-opus-5")

    db.set_run_scope(run_id, runlimit.unit_of(runlimit.wrap(["claude"], run_id)))

    assert db.get_run(run_id)["scope_unit"] is None


def test_a_wrapped_spawn_records_the_unit_it_actually_got(project, monkeypatch):
    monkeypatch.setattr(runlimit, "configured_max_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(runlimit, "available", lambda refresh=False: True)
    run_id = db.create_run(project["id"], "build", "claude-opus-5")

    argv = runlimit.wrap(["claude"], run_id)
    db.set_run_scope(run_id, runlimit.unit_of(argv))

    unit = db.get_run(run_id)["scope_unit"]
    assert unit == runlimit.scope_name(run_id)
    assert unit.startswith(f"portal-run-{run_id}-")
    runlimit.forget_scope(run_id)


# --- scope_is_active: three answers, not two ----------------------------------


def _fake_systemctl(monkeypatch, returncode: int, stdout: bytes = b""):
    class Done:
        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = b""

    monkeypatch.setattr(runlimit.subprocess, "run", lambda *a, **k: Done())


def test_is_active_reports_a_live_scope(monkeypatch):
    _fake_systemctl(monkeypatch, 0, b"active\n")
    assert runlimit.scope_is_active("portal-run-1-1-1.scope") is True


def test_a_collected_scope_reads_as_gone_not_as_unknown(monkeypatch):
    """The case that matters most, and the one an exit code gets wrong.

    Every scope this portal makes carries `--collect`, so when a run ends its
    unit is UNLOADED rather than inactive. systemd 259 answers that with exit
    **4** - not the documented 3 - while still printing "inactive". Reading the
    exit code alone therefore misread the normal end of every single run as "I
    could not find out", which is the one answer that leaves a project locked
    for good. Found by running it against real systemd; no mock would have said
    so, because the mock was written from the same wrong assumption as the code.
    """
    _fake_systemctl(monkeypatch, 4, b"inactive\n")
    assert runlimit.scope_is_active("portal-run-1-1-1.scope") is False


def test_a_failed_scope_reads_as_gone(monkeypatch):
    _fake_systemctl(monkeypatch, 3, b"failed\n")
    assert runlimit.scope_is_active("portal-run-1-1-1.scope") is False


def test_a_scope_still_shutting_down_counts_as_alive(monkeypatch):
    """`deactivating` means the processes are still in the cgroup, so the
    workspace is still occupied and handing it over would be the whole bug."""
    _fake_systemctl(monkeypatch, 3, b"deactivating\n")
    assert runlimit.scope_is_active("portal-run-1-1-1.scope") is True


def test_is_active_says_it_does_not_know_rather_than_guessing(monkeypatch):
    """A word nobody recognizes is us failing to ask, not an answer. Conflating
    it with "gone" is what would unlock a workspace on a box whose systemd
    speaks a dialect this was not written against - which, per the test above,
    is a thing that actually happens."""
    _fake_systemctl(monkeypatch, 1, b"")
    assert runlimit.scope_is_active("portal-run-1-1-1.scope") is None
    _fake_systemctl(monkeypatch, 1, b"something-new\n")
    assert runlimit.scope_is_active("portal-run-1-1-1.scope") is None


def test_is_active_survives_systemctl_being_absent(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(runlimit.subprocess, "run", boom)
    assert runlimit.scope_is_active("portal-run-1-1-1.scope") is None


def test_is_active_on_no_unit_is_unknown_not_dead(monkeypatch):
    """Answered without asking, because `systemctl is-active ""` is not a
    question with an answer - and a run with no unit must never be reported as
    a dead one, which is what its non-zero exit would have meant."""
    monkeypatch.setattr(
        runlimit.subprocess, "run",
        lambda *a, **k: pytest.fail("asked systemd about an empty unit name"),
    )
    assert runlimit.scope_is_active("") is None
    assert runlimit.scope_is_active(None) is None
    assert runlimit.stop_scope("") is False


def test_stop_scope_reports_whether_systemd_took_it(monkeypatch):
    _fake_systemctl(monkeypatch, 0)
    assert runlimit.stop_scope("portal-run-1-1-1.scope") is True
    _fake_systemctl(monkeypatch, 5)
    assert runlimit.stop_scope("portal-run-1-1-1.scope") is False


# --- against real systemd -----------------------------------------------------
#
# Everything above mocks the probe. This one does not: it spawns a genuine
# transient scope through the real `run_claude` path and reads the row back
# while the process is still in it. Skipped on a machine with no user systemd
# manager, matching test_runlimit.py's end-to-end checks.


@pytest.mark.asyncio
@pytest.mark.skipif(
    not runlimit.available(refresh=True),
    reason="no user systemd manager to make a transient scope in",
)
async def test_a_real_spawn_records_its_scope_while_the_run_is_still_live(
    project, tmp_path, monkeypatch
):
    """The handle has to be written down DURING the run, not when it ends.

    A restart lands mid-run by definition - that is the only time any of this
    matters - so a scope name recorded on completion would be recorded exactly
    never for the runs that need it. Asserting after the fact would pass on a
    version that saved it last thing, so this reads the column while the child
    is still alive and checks systemd agrees the unit is up.
    """
    import asyncio

    monkeypatch.setattr(
        agent_runner, "build_cmd",
        lambda *a, **k: ["/bin/sh", "-c", "sleep 3"],
    )
    monkeypatch.setattr(runlimit, "configured_max_bytes", lambda: 2 * 1024**3)
    run_id = db.create_run(project["id"], "build", "claude-opus-5")

    task = asyncio.create_task(
        agent_runner.run_claude(
            "prompt", tmp_path / "ws", "model", timeout_min=1, run_id=run_id
        )
    )
    try:
        unit = None
        for _ in range(40):
            await asyncio.sleep(0.05)
            unit = db.get_run(run_id)["scope_unit"]
            if unit:
                break
        assert unit, "the run was live and its scope was never written down"
        assert unit == f"portal-run-{run_id}-{os.getpid()}-" + unit.rsplit("-", 1)[1]
        assert runlimit.scope_is_active(unit) is True, (
            "the recorded unit does not name the scope the run is actually in"
        )
        assert runlimit.minted_here(unit) is True
    finally:
        await task

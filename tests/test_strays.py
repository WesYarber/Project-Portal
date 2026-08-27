"""What a finished run leaves behind, and where it ends up.

Deliberately light on mocks. The parsing helpers are pure and tested as such,
but the claims that matter here - a process can be moved between cgroups, the
scope it lands in survives the placeholder that created it, and a live run is
never swept - are claims about systemd and the kernel. A mock written from the
same assumption as the code would agree with it happily, which is exactly how
the last incident's `--close` test managed to pass while testing nothing. So the
integration tests spawn real processes into real scopes and skip when the
machine cannot host them.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

import pytest

from app import runlimit, strays

# Every scope this file creates carries a tag in the 9000s, and nothing else on
# the machine does. See the fence below - this pattern is what makes it work.
_TEST_UNIT_RE = re.compile(r"^portal-(?:run|stray)-9\d{3}-\d+-\d+\.scope$")


@pytest.fixture(autouse=True)
def only_this_suites_own_scopes(monkeypatch):
    """No test in this file may reach a scope it did not create.

    Written after this suite swept the whole machine. `strays.sweep` is asked
    what exists by `_list_units`, which asks the real systemd, so these tests
    were one predicate away from acting on real units - and the delete-the-fix
    harness deletes predicates for a living. The mutation that made
    `finished_scopes` ignore its protected set turned `test_a_live_run_is_never_
    swept` into a machine-wide sweep: it evicted eight genuinely leaked scopes
    and the live agent run executing the harness. The test failed exactly as it
    was supposed to, and the damage was already done by the time it did.

    So the blast radius cannot be left to a predicate under test. The fence is
    outside the code being mutated: whatever `strays` believes, it is only ever
    shown units this suite named. A test wanting a specific list still
    monkeypatches `_list_units` over the top, which is fine - those never reach
    systemd at all.
    """
    real = strays._list_units
    monkeypatch.setattr(
        strays, "_list_units",
        lambda pattern: [u for u in real(pattern) if _TEST_UNIT_RE.match(u)],
    )


# --- naming and parsing (pure) ---------------------------------------------


def test_a_stray_scope_keeps_the_name_of_the_run_that_dropped_it():
    # The suffix is reused verbatim so the leftover still says where it came
    # from, and cannot collide - that suffix was already unique per run, per
    # portal process and per spawn.
    assert strays.stray_unit_for("portal-run-678-2305725-1.scope") == (
        "portal-stray-678-2305725-1.scope"
    )
    assert strays.stray_unit_for("portal-run-x-1234-9.scope") == (
        "portal-stray-x-1234-9.scope"
    )


@pytest.mark.parametrize(
    "unit",
    [
        "",
        "portal-app-someproject.service",
        "user@1000.service",
        "portal-run-678.scope",
        "not-ours-678-1-1.scope",
    ],
)
def test_a_name_that_is_not_a_run_scope_yields_no_stray_name(unit):
    # `evict` refuses to act on anything it cannot name, which is what keeps a
    # sweep from touching units the portal did not create.
    assert strays.stray_unit_for(unit) is None


def test_the_run_id_is_readable_from_both_kinds_of_scope_name():
    assert strays.run_id_of("portal-run-678-2305725-1.scope") == 678
    assert strays.run_id_of("portal-stray-678-2305725-1.scope") == 678
    # A run spawned with run_id=None is tagged `x` and has no id to report.
    assert strays.run_id_of("portal-stray-x-2305725-1.scope") is None
    assert strays.run_id_of("garbage") is None


def test_stop_refuses_any_unit_that_is_not_a_stray_scope(monkeypatch):
    # The settings-page button posts a unit name. Stopping an arbitrary user
    # unit on request is a much larger thing than stopping a leftover the portal
    # itself created, so the prefix is checked before systemd is ever asked.
    called = []
    monkeypatch.setattr(runlimit, "stop_scope", lambda u: called.append(u) or True)

    assert strays.stop("portal-run-678-2305725-1.scope") is False
    assert strays.stop("dbus.service") is False
    assert strays.stop("portal-stray-678-1-1") is False
    assert strays.stop("../../etc") is False
    assert called == []

    assert strays.stop("portal-stray-678-2305725-1.scope") is True
    assert called == ["portal-stray-678-2305725-1.scope"]


# --- the safety set --------------------------------------------------------


def test_a_scope_in_the_protected_set_is_never_swept(monkeypatch):
    # Minted by this pid, so the cross-process fence added later stays out of
    # the way and this stays a test about `protected` alone. It used to use a
    # made-up pid of 100, which is a live process on this machine and made the
    # new fence look like a regression here.
    mine = [f"portal-run-{n}-{os.getpid()}-1.scope" for n in (1, 2, 3)]
    monkeypatch.setattr(strays, "_list_units", lambda pattern: list(mine))

    finished = strays.finished_scopes({mine[1]})

    assert finished == [mine[0], mine[2]]


def test_known_scopes_covers_a_run_whose_name_is_not_in_the_database_yet():
    # The window this exists for: `runlimit.wrap` mints the name, the process
    # spawns, and only then does `db.set_run_scope` record it. A sweep built on
    # the database alone would call that live run finished.
    runlimit.forget_scope(4242)
    minted = runlimit.scope_name(4242)
    try:
        assert minted in runlimit.known_scopes()
        assert strays.finished_scopes(runlimit.known_scopes()) == [] or (
            minted not in strays.finished_scopes(runlimit.known_scopes())
        )
    finally:
        runlimit.forget_scope(4242)
    assert minted not in runlimit.known_scopes()


def test_the_fence_hides_every_unit_this_suite_did_not_create():
    """The guard that keeps a mutated predicate from sweeping the machine.

    Asserted rather than assumed, because it is invisible when it works and
    catastrophic when it does not: without it, deleting the protected-set check
    turns a unit test into a sweep of every run scope on the box.
    """
    listed = strays._list_units("portal-*")
    assert all(_TEST_UNIT_RE.match(u) for u in listed), (
        f"a real unit reached this suite: {listed}"
    )
    # And with the safety predicate gone entirely, the fence still holds.
    assert all(_TEST_UNIT_RE.match(u) for u in strays.finished_scopes(set()))


# --- integration: real scopes, real processes ------------------------------


def _scopes_available() -> bool:
    return runlimit.available(refresh=True)


requires_systemd = pytest.mark.skipif(
    not _scopes_available(),
    reason="needs a user systemd manager that can make transient scopes",
)


class _Scope:
    """A real run-shaped scope holding a real detached helper, torn down hard."""

    def __init__(self, tag: str):
        self.unit = f"portal-run-{tag}-{os.getpid()}-1.scope"
        self.stray_unit = strays.stray_unit_for(self.unit)
        self.daemon_pid: int | None = None
        self._holder: subprocess.Popen | None = None

    def start(self, hold: int = 120) -> "_Scope":
        # A shell that detaches a background child and then exits, which is how
        # every real leak on this box was made (a serve.sh, a `bun ... &`, an
        # `ssh -f -N -R`). Started through a shell on purpose: a shell passes
        # its file descriptors and its cgroup straight through, where CPython's
        # subprocess would quietly close them.
        self._holder = subprocess.Popen(
            [
                "systemd-run", "--user", "--scope", "--quiet", "--collect",
                f"--unit={self.unit}", "-p", "MemoryMax=512M", "--",
                "/bin/bash", "-c", f"sleep {hold} & echo $!; sleep {hold}",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, text=True, start_new_session=True,
        )
        line = self._holder.stdout.readline().strip()
        self.daemon_pid = int(line) if line.isdigit() else None
        return self

    def end_the_agent(self) -> None:
        """The agent exits; only what it detached is left in the scope."""
        if self._holder is not None:
            self._holder.terminate()
            try:
                self._holder.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self._holder.kill()

    def cleanup(self) -> None:
        self.end_the_agent()
        for unit in (self.unit, self.stray_unit):
            subprocess.run(
                ["systemctl", "--user", "stop", unit],
                capture_output=True, timeout=15,
            )
        if self.daemon_pid:
            # Kill by pid, never by `pkill -f`: a full-command-line match also
            # matches the shell running the test suite, which is how an earlier
            # run of this work killed its own session.
            try:
                os.kill(self.daemon_pid, 9)
            except ProcessLookupError:
                pass


@pytest.fixture
def run_scope():
    made: list[_Scope] = []

    def make(tag: str) -> _Scope:
        scope = _Scope(tag).start()
        made.append(scope)
        return scope

    yield make
    for scope in made:
        scope.cleanup()


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@requires_systemd
def test_a_detached_helper_really_does_hold_its_run_scope_open(run_scope):
    """The defect itself, reproduced: this is why a finished run reads alive.

    Without this the rest of the module is a solution to a hypothesis. The
    agent exits, its scope stays `active` because the thing it detached is
    still inside, and `runlimit.scope_is_active` - the portal's only liveness
    signal for an adopted run - keeps answering True.
    """
    scope = run_scope("9001")
    assert scope.daemon_pid and _alive(scope.daemon_pid)

    scope.end_the_agent()

    assert _wait_for(lambda: _alive(scope.daemon_pid))
    assert runlimit.scope_is_active(scope.unit) is True
    survivors = strays.processes_in(scope.unit)
    assert survivors is not None
    assert scope.daemon_pid in [s.pid for s in survivors]


@requires_systemd
def test_eviction_rehouses_the_helper_and_lets_the_run_scope_die(run_scope):
    """The fix, end to end, and the two halves that both have to hold.

    The helper must still be running afterwards (it is usually the preview
    server Wes clicks "open it" on, so killing it would be worse than the leak),
    and the run's own scope must be gone (that is what makes `scope_is_active`
    truthful again, with no change to any of its callers).
    """
    scope = run_scope("9002")
    scope.end_the_agent()
    assert _wait_for(lambda: runlimit.scope_is_active(scope.unit) is True)

    eviction = strays.evict(scope.unit)

    assert eviction is not None
    assert eviction.stray_unit == scope.stray_unit
    assert scope.daemon_pid in [s.pid for s in eviction.moved]
    # Still alive - the whole point of moving rather than killing.
    assert _alive(scope.daemon_pid)
    # And it really is in the new cgroup, asked of the kernel rather than of
    # our own return value.
    assert _wait_for(
        lambda: scope.daemon_pid in [
            s.pid for s in (strays.processes_in(scope.stray_unit) or [])
        ]
    )
    # The run scope is empty and therefore gone, which is the bug closing.
    assert _wait_for(lambda: runlimit.scope_is_active(scope.unit) is not True)


@requires_systemd
def test_the_rehoused_helper_outlives_the_placeholder_that_made_its_scope(run_scope):
    """A scope cannot be created empty, so something has to hold it open while
    the strays are moved in. If that placeholder's exit took the scope (or its
    memory cap) with it, eviction would be a slower way of losing the process.
    """
    scope = run_scope("9003")
    scope.end_the_agent()
    assert _wait_for(lambda: runlimit.scope_is_active(scope.unit) is True)

    strays.evict(scope.unit)

    # `evict` has returned, so the placeholder is already terminated.
    assert runlimit.scope_is_active(scope.stray_unit) is True
    assert _alive(scope.daemon_pid)
    procs = strays.processes_in(scope.stray_unit) or []
    assert [s.pid for s in procs] == [scope.daemon_pid], (
        "the placeholder should be gone and only the rescued helper left"
    )
    cap = subprocess.run(
        ["systemctl", "--user", "show", "-p", "MemoryMax", "--value", scope.stray_unit],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    assert cap and cap != "infinity", "the rehoused helper kept a memory ceiling"


@requires_systemd
def test_an_empty_but_active_scope_is_collected(run_scope):
    """`--collect` is documented to reap an empty scope and usually does, but
    two of the nine leaked scopes on this machine were `active` with
    `TasksCurrent=0`. A sweep that assumed otherwise would leave them forever.
    """
    scope = _Scope("9004")
    subprocess.Popen(
        [
            "systemd-run", "--user", "--scope", "--quiet", "--collect",
            f"--unit={scope.unit}", "--", "sleep", "60",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        assert _wait_for(lambda: runlimit.scope_is_active(scope.unit) is True)
        # Empty it without letting systemd notice through the normal path.
        procs = strays.processes_in(scope.unit) or []
        for proc in procs:
            try:
                os.kill(proc.pid, 9)
            except ProcessLookupError:
                pass
        assert _wait_for(lambda: strays.processes_in(scope.unit) in ([], None))

        eviction = strays.evict(scope.unit)

        if eviction is not None:  # the husk was still there and got stopped
            assert eviction.moved == ()
            assert eviction.stray_unit is None
            assert eviction.husk_stopped is True
        assert runlimit.scope_is_active(scope.unit) is not True
    finally:
        scope.cleanup()


@requires_systemd
def test_a_live_run_is_never_swept(run_scope):
    """The one mistake that would be unforgivable: moving a *running* agent out
    of the cgroup capping it. The protected set is what prevents it, and it is
    built to be over-inclusive on purpose.
    """
    scope = run_scope("9005")  # deliberately NOT ended - this run is live
    assert _wait_for(lambda: runlimit.scope_is_active(scope.unit) is True)

    protected = {scope.unit}
    assert scope.unit not in strays.finished_scopes(protected)
    evictions = strays.sweep(protected)
    assert all(ev.unit != scope.unit for ev in evictions)

    # Untouched: still active, still holding both the agent and its helper.
    assert runlimit.scope_is_active(scope.unit) is True
    pids = [s.pid for s in (strays.processes_in(scope.unit) or [])]
    assert scope.daemon_pid in pids
    assert runlimit.scope_is_active(scope.stray_unit) is not True


@requires_systemd
def test_a_rehoused_helper_is_listed_and_can_be_stopped(run_scope):
    """What Wes actually sees: the leftover shows up with its command and the
    run it came from, and the stop button ends it."""
    scope = run_scope("9006")
    scope.end_the_agent()
    assert _wait_for(lambda: runlimit.scope_is_active(scope.unit) is True)
    strays.evict(scope.unit)

    listed = [s for s in strays.listing() if s.unit == scope.stray_unit]
    assert len(listed) == 1
    assert listed[0].run_id == 9006
    assert [p.pid for p in listed[0].processes] == [scope.daemon_pid]
    assert "sleep" in listed[0].processes[0].command

    assert strays.stop(scope.stray_unit) is True
    assert _wait_for(lambda: not _alive(scope.daemon_pid))
    assert not [s for s in strays.listing() if s.unit == scope.stray_unit]


# --- failing open ----------------------------------------------------------


def test_a_machine_with_no_systemd_sweeps_nothing_and_raises_nothing(monkeypatch):
    # A housekeeping job that can stop a run is worse than the leak it fixes.
    def no_systemctl(*args, **kwargs):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(subprocess, "run", no_systemctl)
    assert strays.control_group("portal-run-1-1-1.scope") is None
    assert strays.processes_in("portal-run-1-1-1.scope") is None
    assert strays.finished_scopes(set()) == []
    assert strays.sweep(set()) == []
    assert strays.listing() == []
    assert strays.evict("portal-run-1-1-1.scope") is None


def test_an_unreadable_cgroup_is_not_treated_as_an_empty_one(monkeypatch, tmp_path):
    """"We could not read it" and "there is nothing in it" want opposite
    handling: only the second may lead to stopping the unit."""
    monkeypatch.setattr(strays, "control_group", lambda unit: tmp_path / "missing")
    assert strays.processes_in("portal-run-1-1-1.scope") is None

    stopped = []
    monkeypatch.setattr(runlimit, "stop_scope", lambda u: stopped.append(u) or True)
    assert strays.evict("portal-run-1-1-1.scope") is None
    assert stopped == [], "an unreadable scope must never be stopped"


# --- the settings page -----------------------------------------------------


@pytest.fixture
def client(temp_data_dir):
    from app import main

    from starlette.testclient import TestClient

    return TestClient(main.app)


def test_the_settings_page_lists_a_leftover_with_its_project_and_command(
    client, monkeypatch
):
    from app import db, main

    project = db.create_project("Room Server", stage="active", slug="room-server")
    run_id = db.create_run(project["id"], "build", "claude-opus-5")
    monkeypatch.setattr(
        strays, "listing",
        lambda: [
            strays.StrayScope(
                unit=f"portal-stray-{run_id}-999-1.scope",
                run_id=run_id,
                processes=(strays.Stray(pid=4242, command="bun server.js"),),
            )
        ],
    )
    view = main._stray_view()
    assert view == [{
        "unit": f"portal-stray-{run_id}-999-1.scope",
        "run_id": run_id,
        "project_title": "Room Server",
        "processes": [{"pid": 4242, "command": "bun server.js"}],
    }]

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Left running" in resp.text
    assert "bun server.js" in resp.text
    assert "Room Server" in resp.text


def test_a_leftover_whose_run_row_is_gone_is_still_shown(client, monkeypatch):
    """A run row can be pruned while its helper is still up. "Something is
    still running" is worth showing even when the portal can no longer say
    which project asked for it."""
    from app import main

    monkeypatch.setattr(
        strays, "listing",
        lambda: [
            strays.StrayScope(
                unit="portal-stray-999999-1-1.scope",
                run_id=999999,
                processes=(strays.Stray(pid=1, command="bun server.js"),),
            )
        ],
    )
    assert main._stray_view()[0]["project_title"] is None
    assert "unknown project" in client.get("/settings").text


def test_the_empty_state_says_so_rather_than_rendering_nothing(client, monkeypatch):
    monkeypatch.setattr(strays, "listing", lambda: [])
    text = client.get("/settings").text
    assert "Left running" in text
    assert "Nothing left running." in text


def test_the_stop_button_stops_only_that_unit(client, monkeypatch):
    stopped = []
    monkeypatch.setattr(strays, "stop", lambda u: stopped.append(u) or True)
    resp = client.post(
        "/settings/stray/stop",
        data={"unit": "portal-stray-5-1-1.scope"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings#strays"
    assert stopped == ["portal-stray-5-1-1.scope"]


# --------------------------------------------------------------------------
# The fence that `protected` cannot be: another portal process's live runs
# --------------------------------------------------------------------------
# Written on 2026-07-29, immediately after causing the thing it prevents. A
# throwaway portal instance was started under /tmp to screenshot a page. It
# booted with an empty database, ran its first tick, correctly concluded that
# no scope on the machine was protected - its runs table said so - and rehoused
# the LIVE service's in-flight agent run out of its own cgroup.
#
# That is the third firing of this hazard. The first two were the test suite
# and were fenced in `conftest.py`; this one was real code, so a test-only
# fence would never have touched it. The rule below lives in `app/` for that
# reason: `protected` describes the caller's own runs, and a second reader's
# runs table is not a description of this machine.


def _a_definitely_dead_pid() -> int:
    """A pid that existed and does not any more. Linux allocates pids
    increasing, so the number is not back in use a moment after the wait."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_a_scope_minted_by_a_live_stranger_is_never_swept(monkeypatch):
    """The incident itself. Pid 1 stands in for the live portal service: a real
    process, definitely not us, whose scopes are none of our business."""
    theirs = "portal-run-9001-1-1.scope"
    monkeypatch.setattr(strays, "_list_units", lambda pattern: [theirs])

    # Nothing is protected, which is exactly the state the throwaway booted in.
    assert strays.finished_scopes(set()) == []


def test_a_scope_minted_by_a_dead_process_is_still_swept(monkeypatch):
    """The delete-the-fix direction, and the whole point of the module: a run
    that outlived the portal process which started it is precisely what wants
    rehousing. A fence that also caught these would switch the sweep off."""
    orphan = f"portal-run-9002-{_a_definitely_dead_pid()}-1.scope"
    monkeypatch.setattr(strays, "_list_units", lambda pattern: [orphan])

    assert strays.finished_scopes(set()) == [orphan]


def test_our_own_finished_scopes_are_still_swept(monkeypatch):
    """The common case, every time a run ends: we minted it, so `protected` is
    the thing that decides and the pid rule must stay out of the way."""
    ours = f"portal-run-9003-{os.getpid()}-1.scope"
    monkeypatch.setattr(strays, "_list_units", lambda pattern: [ours])

    assert strays.finished_scopes(set()) == [ours]
    assert strays.finished_scopes({ours}) == []


def test_the_pid_is_read_from_the_name_rather_than_matched_as_a_substring():
    """`minting_pid` parses, because the run-id segment is a number too: a
    portal whose pid equalled a run id would otherwise claim that run's scope."""
    assert runlimit.minting_pid("portal-run-9004-4321-1.scope") == 4321
    assert runlimit.minting_pid("portal-run-4321-9004-1.scope") == 9004
    assert runlimit.minting_pid("portal-stray-9004-4321-1.scope") is None
    assert runlimit.minting_pid("something-else.scope") is None
    assert runlimit.minting_pid(None) is None

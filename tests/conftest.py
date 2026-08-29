"""Test fixtures: each test gets a throwaway data dir and a fresh DB.

`app.config` exposes its paths as module-level constants and `app.db` caches a
single connection, so the fixture repoints the constants and clears the cached
connection before `init_db()` runs.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, quiet, transcribe  # noqa: E402

# 13:00 in America/Chicago, the middle of a working afternoon. Quiet hours (23
# -> 07) are the one guard in the scheduler that reads the wall clock with no
# way for a caller to pass a moment in, so every test that drives `_start_one`
# or `idle_reason` was really asking "is it night on this machine right now?".
# Between 23:00 and 07:00 Central that made 18 tests fail on a clean tree -
# found on 2026-08-10 at 01:23 Chicago, when a run needed a green suite before
# it could commit and could not get one. A suite that is red for eight hours a
# night is worse than useless overnight, which is exactly when this portal runs
# unattended.
_DAYTIME = datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc)


class _FixedNow(datetime):
    """`datetime` with a pinned `now()`. Everything else is inherited, so the
    module's own arithmetic (`replace`, `astimezone`, `timedelta`) is real."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - matches datetime.now
        return _DAYTIME.astimezone(tz) if tz else _DAYTIME.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path, monkeypatch):
    # The clock quiet hours reads. Tests of quiet hours themselves pass their
    # own moment in (`quiet.is_quiet(_at(3))`) or stub `quiet_hold`, so they are
    # unaffected; this only pins what "right now" means for everyone else.
    monkeypatch.setattr(quiet, "datetime", _FixedNow)

    # No test may reach the real Docker daemon: an audio upload kicks off
    # transcription (app/transcribe.py), and on the production box the
    # portal-whisper image exists, so an unpinned available() would happily
    # spend a second of container start per uploaded blob of fake audio. Tests
    # that want the engine "present" re-monkeypatch this themselves.
    monkeypatch.setattr(transcribe, "available", lambda: False)
    monkeypatch.setattr(transcribe, "_available", None, raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "portal.db")
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(config, "INCOMING_DIR", tmp_path / "incoming")
    monkeypatch.setattr(config, "PROFILE_MD", tmp_path / "memory" / "profile.md")
    monkeypatch.setattr(config, "LEARNINGS_MD", tmp_path / "memory" / "learnings.md")
    monkeypatch.setattr(config, "SUGGESTIONS_MD", tmp_path / "memory" / "suggestions.md")

    # `config.cli_version()` shells out to `claude --version` and memoizes the
    # answer in a module global, so exactly one test per process paid for a
    # real subprocess - whichever one happened to run first. That made
    # `test_the_tick_holds_new_runs_and_fires_once_quiet` (which asserts a tick
    # spawns nothing) pass in a full run and fail on its own, and it meant the
    # suite behaved differently on a machine with no CLI installed. Pinned, so
    # no test can reach the real binary.
    monkeypatch.setattr(config, "_cli_version_cache", config.DEFAULT_CLI_VERSION,
                        raising=False)

    if db._CONN is not None:  # noqa: SLF001
        db._CONN.close()  # noqa: SLF001
    monkeypatch.setattr(db, "_CONN", None, raising=False)
    # Tests want an empty portal, not the first-run demo project + question.
    monkeypatch.setattr(db, "_seed_data", lambda: None)

    db.init_db()
    yield tmp_path

    if db._CONN is not None:  # noqa: SLF001
        db._CONN.close()  # noqa: SLF001
    db._CONN = None  # noqa: SLF001


# The units this suite is allowed to see. `test_strays.py` names everything it
# creates in the 9000s precisely so that this pattern can exclude every real
# one; nothing else in the suite creates a unit it needs to discover.
_TEST_UNIT_RE = re.compile(r"^portal-(?:run|stray)-9\d{3}-\d+-\d+\.scope$")


@pytest.fixture(autouse=True)
def no_test_may_sweep_a_real_scope(monkeypatch):
    """Nothing in this suite may discover a systemd scope it did not create.

    This fence is global rather than per-file because the damage was, and it is
    not hypothetical - it has now happened to two live agent runs.

    `worker._tick()` calls `_sweep_strays()`, which builds its protected set by
    reading `runs` and then asks the REAL systemd what exists. Under pytest that
    database is the empty throwaway one, so every genuine `portal-run-*` scope on
    the machine is unprotected, and the sweep rehouses the lot - including the
    live agent run executing the tests, which loses the scope name its own row
    records and would be declared dead by the next restart. Eleven test files
    call `_tick()`; `test_approval.py` alone was enough to move a live run out of
    its cgroup. The suite passed, every time, while doing it.

    `test_strays.py` already had a fence of exactly this shape. It was scoped to
    that one file because that was the file caught doing it, which turned out to
    be the wrong lesson: the hazard is not "the strays tests are dangerous", it
    is "any test that reaches production code which enumerates system state is
    dangerous, whether or not it knows it does". So the fence sits here, where a
    new test file inherits it without having to know it exists.

    Only discovery is fenced. `strays.evict(unit)` acts on a unit it was handed
    by name - always the caller's own, from `run_claude`'s teardown - and stays
    working, so the tests that spawn real scoped processes still clean up.
    """
    from app import strays

    real = strays._list_units  # noqa: SLF001
    monkeypatch.setattr(
        strays, "_list_units",
        lambda pattern: [u for u in real(pattern) if _TEST_UNIT_RE.match(u)],
    )


# --- The filesystem half of the same fence ---------------------------------
#
# The fence above stops a test discovering a systemd unit it did not create.
# This one stops a test deleting, moving or overwriting a file it does not own.
#
# The hazard is the same shape and it is not hypothetical either. Production
# code here removes directories for a living - `main._remove_workspace` is a
# literal `rm -rf` on a path built from a user-supplied slug, `worker._sync_skills`
# rmtrees whatever is stale under a workspace, `memory.delete_promoted_skill`
# removes a named directory. Every one of them is kept inside the data dir by a
# single predicate ("the resolved parent really is PROJECTS_DIR", "the name
# matches _SKILL_NAME_RE"). The delete-the-fix harness deletes predicates like
# those on purpose, to prove a test notices. If the only thing standing between
# `shutil.rmtree` and the live `data/projects` directory - every other project's
# working checkout - is the line the harness just deleted, the test does not
# fail. The machine does.
#
# So the bound sits out here, where nothing under test can reach it: an
# operation that destroys or overwrites must land inside this test's own
# `tmp_path`. Nothing in the suite and nothing in `app/` uses `tempfile`, so
# that one directory is the whole legitimate write surface and the fence needs
# no allowlist.
#
# Reads are deliberately NOT fenced. Tests legitimately read the real tree -
# `app/static/style.css`, `deploy/project-portal.service`, the skill sources
# `_sync_skills` copies FROM - and a read has no blast radius.
#
# `builtins.open` is deliberately not fenced either, because in `app/` every
# `open()` for writing is append mode, which cannot truncate; every truncating
# write goes through `Path.write_text`/`write_bytes`, which is fenced.
# `test_conftest_fence.py` pins that claim, so the exclusion stays honest
# rather than being a comment that quietly goes stale.

_FS_ROOTS: list[str] = []


def _fs_targets(path) -> list[str]:
    """The real path an operation on `path` would land on.

    `realpath` follows symlinks, which is what closes the obvious way around a
    path check: a link planted inside `tmp_path` pointing at the real tree is
    judged by where it points, not by where it sits.

    That makes the fence slightly stricter than the syscalls it wraps, and
    deliberately so. `unlink` removes a symlink rather than following it, so
    deleting a link inside `tmp_path` that points outside is refused even though
    it would have been harmless. Nothing in the suite does that, and the error
    says exactly where the fence is if something ever needs to. Erring toward
    "refuse" is the only direction that is safe to be wrong in here.

    (An earlier version also judged the non-following path - parent resolved,
    basename appended - on the theory that both readings should be in bounds.
    It was dead weight: that path is a prefix of this one except when the final
    component is a symlink, which is the case `realpath` already handles. A
    mutation deleting it failed no test, which is how it was found.)

    Returns nothing for a file descriptor - an int names no path to judge, and
    it can only refer to a file the caller already opened.
    """
    try:
        raw = os.fsdecode(os.fspath(path))
    except TypeError:
        return []
    return [os.path.realpath(raw)]


def _fs_is_allowed(target: str) -> bool:
    if any(target == root or target.startswith(root + os.sep)
           for root in _FS_ROOTS):
        return True
    return _fs_is_our_own_cgroup_control_file(target)


def _fs_is_our_own_cgroup_control_file(target: str) -> bool:
    """The one write outside `tmp_path` the suite genuinely needs.

    Moving a process between cgroups IS writing its pid into the destination's
    `cgroup.procs`; there is no other API for it, and `strays._migrate` does
    exactly that. The three tests that spawn real scoped processes have to reach
    the real kernel to be worth anything - a test of "the leftover is rehoused
    and the run's scope dies" against a mocked cgroup tests nothing.

    So this stays narrow instead of admitting `/sys/fs/cgroup` wholesale, which
    would hand a mutated `evict` the whole machine's process tree. It admits one
    file name, inside a unit whose name the suite is already allowed to touch -
    the same `_TEST_UNIT_RE` that fences systemd discovery above, so the "9000s
    are ours" convention is stated once and enforced in both halves.
    """
    head, _, name = target.rpartition(os.sep)
    return name == "cgroup.procs" and bool(
        _TEST_UNIT_RE.match(head.rpartition(os.sep)[2]))


def _fs_guard(what: str, *paths, dir_fd=None) -> None:
    # A `dir_fd` makes the path relative to an already-open directory, so the
    # string says nothing about where it lands and resolving it against the
    # process cwd would be a lie. This is not an edge case: on Linux
    # `shutil.rmtree` walks with file descriptors, calling `os.unlink(name,
    # dir_fd=fd)` with a bare basename for every file it removes. The top-level
    # `shutil.rmtree(path)` was already checked with a real path, which is the
    # check that matters - everything below it is inside what that call opened.
    if dir_fd is not None:
        return
    for path in paths:
        for target in _fs_targets(path):
            if not _fs_is_allowed(target):
                raise AssertionError(
                    f"{what} tried to write outside this test's tmp_path.\n"
                    f"  target: {target}\n"
                    f"  allowed: {_FS_ROOTS[0] if _FS_ROOTS else '(none)'}\n"
                    "This fence is in tests/conftest.py and it is load-bearing: "
                    "the code under test destroys directories for a living, and "
                    "the delete-the-fix harness deletes the predicates that keep "
                    "it in bounds. If this fired during a mutation run, the "
                    "mutation is exactly the bug it was meant to find. If it "
                    "fired during an ordinary run, the test is reaching the real "
                    "machine - point it at tmp_path instead of widening this."
                )


# Which arguments of each call actually get written to. Only those are checked:
# `shutil.copytree(src, dst)` legitimately reads a real source (that is how the
# skills in `app/skills` reach a workspace), so checking every argument would
# fence the copy that is supposed to happen. `move` names both because it
# removes the source as well as creating the destination.
_FS_SHUTIL_WRITES = {
    "rmtree": (0,), "move": (0, 1), "copytree": (1,),
    "copy": (1,), "copy2": (1,), "copyfile": (1,), "copystat": (1,),
}
_FS_OS_WRITES = {
    "remove": (0,), "unlink": (0,), "rmdir": (0,), "removedirs": (0,),
    "mkdir": (0,), "makedirs": (0,), "truncate": (0,), "chmod": (0,),
    "rename": (0, 1), "replace": (0, 1), "link": (1,), "symlink": (1,),
}
# Path methods whose target is `self`, plus the ones that also name a second
# path. `Path.open` is here because it reaches `io.open`, not `builtins.open`.
_FS_PATH_SELF_ONLY = (
    "unlink", "rmdir", "write_text", "write_bytes", "mkdir", "touch",
    "chmod", "lchmod", "symlink_to", "hardlink_to",
)
_FS_PATH_WITH_ARG = ("rename", "replace")

_FS_WRITE_MODE = frozenset("wxa+")


@pytest.fixture(autouse=True)
def no_test_may_write_outside_tmp_path(tmp_path, monkeypatch):
    """Destroying or overwriting anything outside this test's `tmp_path` fails.

    See the long note above for why this is not tidiness. Short version: the
    predicates that keep production `rmtree` calls inside the data directory are
    themselves things the mutation harness deletes, so the bound has to live
    somewhere the mutation cannot reach.
    """
    # Set in place rather than rebound: `_fs_guard` reads the module global, and
    # the wrappers that call it are removed by monkeypatch at teardown, so a
    # stale root left behind between tests is unreachable rather than permissive.
    _FS_ROOTS[:] = [os.path.realpath(tmp_path)]

    for name, indexes in _FS_SHUTIL_WRITES.items():
        real = getattr(shutil, name, None)
        if real is None:
            continue

        def wrapper(*args, _real=real, _name=name, _idx=indexes, **kwargs):
            _fs_guard(f"shutil.{_name}",
                      *[args[i] for i in _idx if i < len(args)])
            return _real(*args, **kwargs)

        monkeypatch.setattr(shutil, name, wrapper)

    for name, indexes in _FS_OS_WRITES.items():
        real = getattr(os, name, None)
        if real is None:
            continue

        def wrapper(*args, _real=real, _name=name, _idx=indexes, **kwargs):
            _fs_guard(f"os.{_name}",
                      *[args[i] for i in _idx if i < len(args)],
                      dir_fd=kwargs.get("dir_fd"))
            return _real(*args, **kwargs)

        monkeypatch.setattr(os, name, wrapper)

    for name in _FS_PATH_SELF_ONLY:
        real = getattr(Path, name, None)
        if real is None:
            continue

        def wrapper(self, *args, _real=real, _name=name, **kwargs):
            _fs_guard(f"Path.{_name}", self)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, wrapper)

    for name in _FS_PATH_WITH_ARG:
        real = getattr(Path, name)

        def wrapper(self, target, *args, _real=real, _name=name, **kwargs):
            _fs_guard(f"Path.{_name}", self, target)
            return _real(self, target, *args, **kwargs)

        monkeypatch.setattr(Path, name, wrapper)

    real_open = Path.open

    def guarded_open(self, mode="r", *args, **kwargs):
        if _FS_WRITE_MODE & set(mode):
            _fs_guard("Path.open", self)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)


# --- and the third half: no test may stop or start a real unit --------------
#
# The fence at the top of this file stops a test DISCOVERING a unit it did not
# create. It does not stop one being handed a real name and acting on it, and
# the suite is full of real names as string literals.
#
# The worst of them is not a scope. `worker._fire_restart` shells out to
#
#     systemd-run --user --on-active=N ... systemctl --user restart <the service>
#
# and what held that back was `monkeypatch.setattr(worker.subprocess, "run", ...)`
# in a fixture inside the test file that calls it. That is precisely the shape
# the last incident was about: the safety lives beside the code under test, so a
# test that reaches the restart path without knowing it does - through a
# mutation, or through a new caller - restarts the live portal, killing every
# agent in flight including the one running the suite.
#
# So the check moves out here, to the subprocess boundary, where a call reaching
# systemd at all is judged on the units it names rather than on which test
# remembered to stub which module.
#
# Reads are allowed. `systemctl is-active <a real unit>` tells the truth about
# the machine and changes nothing, and `orphans`/`_reap_adopted` genuinely want
# that answer. It is starting, stopping and restarting that are fenced.

_SYSTEMD_MUTATING_VERBS = frozenset({
    "start", "stop", "restart", "try-restart", "reload", "reload-or-restart",
    "kill", "reset-failed", "enable", "disable", "mask", "unmask",
    "daemon-reload", "daemon-reexec", "set-property", "freeze", "thaw",
})
_UNIT_SUFFIXES = (".service", ".scope", ".timer", ".socket", ".slice",
                  ".target", ".path", ".mount")


def _looks_like_a_unit(token: str) -> bool:
    return token.startswith("portal-") or token.endswith(_UNIT_SUFFIXES)


def _is_a_unit_this_process_owns(unit: str) -> bool:
    """Two ways to own a unit, and the second is the one that carries the load.

    `_TEST_UNIT_RE` is the hand-written convention: `test_strays.py` names
    everything it creates in the 9000s so a real unit can never match.

    But most units the suite creates are not hand-named at all - they come from
    `runlimit.scope_name`, which mints `portal-run-<tag>-<pid>-<seq>.scope` from
    the pid of the process doing the spawning. Under pytest that pid is pytest's
    own, and a live agent's scope carries the portal service's pid instead, so
    "the name contains my pid" is proof of ownership that no test has to
    remember to arrange and no live unit can accidentally satisfy.

    `project-portal.service` satisfies neither, which is the entire point.

    The pid is matched as a whole dash-separated component rather than as a
    substring, so the rule does not depend on where in the name it sits -
    `portal-run-<tag>-<pid>-<seq>.scope` from `scope_name` and the
    `portal-killtest-<pid>.scope` that `test_runlimit.py` builds by hand are
    both owned, and neither `1234` nor `12345` is mistaken for pid `234`.
    """
    if _TEST_UNIT_RE.match(unit):
        return True
    stem = unit.rsplit(".", 1)[0]
    return str(os.getpid()) in stem.split("-")


def _systemd_guard(argv) -> None:
    """Refuse a systemd command that would change a unit this suite does not own.

    The rule is deliberately about the whole argv rather than about `argv[0]`,
    because the dangerous call is nested: the outer command is `systemd-run`,
    and the thing that restarts the portal is the `systemctl restart` sitting in
    its arguments. Scanning for the verb wherever it appears catches both, and
    catches whatever the next wrapper looks like too.
    """
    try:
        argv = [os.fsdecode(a) if isinstance(a, bytes) else str(a) for a in argv]
    except TypeError:
        return  # a string command for a shell; nothing here builds one
    if not argv or os.path.basename(argv[0]) not in ("systemctl", "systemd-run"):
        return

    named = [a.split("=", 1)[1] for a in argv if a.startswith("--unit=")]
    mutating = [a for a in argv if a in _SYSTEMD_MUTATING_VERBS]
    if not named and not mutating:
        return  # a read, or an anonymous transient scope with no name to collide

    units = named + [a for a in argv[1:] if not a.startswith("-")
                     and _looks_like_a_unit(a)]
    for unit in units:
        if not _is_a_unit_this_process_owns(unit):
            raise AssertionError(
                f"a test tried to run `{' '.join(argv[:2])} ... {mutating[:1] or named}` "
                f"against `{unit}`, which this suite does not own.\n"
                "This fence is in tests/conftest.py. The suite may read the real "
                "systemd all it likes, but it may only start, stop or restart a "
                "unit it created itself - named in the 9000s, the same convention "
                "the scope-discovery fence above uses.\n"
                "The call this exists for is `worker._fire_restart`, which "
                "restarts the portal service and would take every agent run on "
                "the machine, including this one, with it. If you meant to test "
                "that path, record the command with a stub rather than letting it "
                "reach systemd - and do not widen this."
            )


@pytest.fixture(autouse=True)
def no_test_may_change_a_unit_it_does_not_own(monkeypatch):
    import subprocess

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        real = getattr(subprocess, name)

        def wrapper(*args, _real=real, **kwargs):
            if args:
                _systemd_guard(args[0])
            elif "args" in kwargs:
                _systemd_guard(kwargs["args"])
            return _real(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, wrapper)


@pytest.fixture(autouse=True)
def the_run_memory_pool_is_never_written_to_real_systemd(monkeypatch):
    """Keep `runlimit.apply_pool` off the machine's own `portal-runs.slice`.

    Taking the fence above at its word - "record the command with a stub rather
    than letting it reach systemd" - rather than widening it. `apply_pool` runs
    `systemctl --user set-property --runtime portal-runs.slice MemoryMax=...`,
    and that slice is where the live portal's agent runs are contained: a test
    suite writing a ceiling onto it would be setting a real limit on real work,
    including the run executing the suite.

    The stub answers the only question callers ask - "is a pool in force" - and
    records what was asked for on `runlimit.POOL_WRITES`, so a test can assert
    the number without a subprocess. It says yes exactly when a real one would:
    a limit was configured and slices work on this machine.
    """
    from app import runlimit

    writes: list = []
    monkeypatch.setattr(runlimit, "POOL_WRITES", writes, raising=False)

    def stub(limit):
        writes.append(limit)
        return limit is not None and runlimit.pool_available()

    monkeypatch.setattr(runlimit, "apply_pool", stub)


@pytest.fixture(autouse=True)
def worker_module_state_is_never_inherited(monkeypatch):
    """Every test starts from `app.worker`'s module-import state.

    The worker keeps its live state in module globals - the manual-run queue,
    the in-flight task map, the "already said this" sets, the restart latch.
    They are process-wide, so what one test leaves behind the next one in that
    process inherits, and only a handful of files clean up after themselves.

    Serial running hid this almost perfectly: run the files in the same order
    every time and the leaks happen to fall where nothing reads them. They stop
    being invisible the moment the suite is distributed across workers, because
    then the set of tests sharing a process changes from run to run. Found on
    2026-08-29 while parallelizing the suite, as four tests that failed once in
    ten runs and never in the same combination - `_start_one` called twice
    because a *previous file* had left an id in `manual_queue`, and a research
    burst that came back as a build.

    That is not a parallelism bug, it is an order-dependency the suite has
    always had, and it is worth closing on its own account: a test that passes
    only because of what ran before it is not testing what it says it is.
    """
    from app import worker

    while not worker.manual_queue.empty():
        worker.manual_queue.get_nowait()
    monkeypatch.setattr(worker, "_inflight", {})
    monkeypatch.setattr(worker, "_lease_free_since", {})
    monkeypatch.setattr(worker, "_PARALLEL_SAID", {})
    monkeypatch.setattr(worker, "_pending_restart", None)
    monkeypatch.setattr(worker, "_restarting", False)
    monkeypatch.setattr(worker, "_last_stray_sweep", None)
    monkeypatch.setattr(worker, "_audit_pruned_day", None)
    monkeypatch.setattr(worker, "_model_checked_day", None)
    yield
    # The queue is the one that cannot be monkeypatched back, since tests put
    # into the same object the worker reads from.
    while not worker.manual_queue.empty():
        worker.manual_queue.get_nowait()

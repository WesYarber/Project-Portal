"""Tests for the fence in `conftest.py` that keeps this suite off the real disk.

A fence is invisible when it works, so it needs its own tests more than most
code does - and it needs them to fail for the right reason. Every test here
asserts on the *effect*: the file or directory outside `tmp_path` is still
there afterward. An `AssertionError` was raised is a weaker claim, because a
fence that raised on everything would satisfy it while breaking the suite; so
the "allowed" half below is as load-bearing as the "refused" half.

Why this fence exists at all is written up at length in `conftest.py`. The short
version: `app/` removes directories for a living, every one of those calls is
kept in bounds by a single predicate, and the delete-the-fix harness deletes
predicates like those on purpose. The bound therefore has to live somewhere the
mutation cannot reach.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

import conftest


def _outside(tmp_path: Path) -> Path:
    """A directory that is real, is not under `tmp_path`, and is ours to lose.

    A sibling of `tmp_path` rather than somewhere in the source tree: if any of
    these tests ever stops raising, the damage should land on a scratch
    directory and not on the repo the suite is running from.
    """
    victim = tmp_path.parent / (tmp_path.name + "-outside-the-fence")
    conftest._FS_ROOTS.append(os.path.realpath(victim))  # build it, then leave
    try:
        victim.mkdir(exist_ok=True)
        (victim / "precious.txt").write_text("do not lose me", encoding="utf-8")
    finally:
        conftest._FS_ROOTS.pop()
    return victim


# --- what the fence refuses --------------------------------------------------

def test_rmtree_outside_tmp_path_is_refused_and_the_directory_survives(tmp_path):
    """The headline case: `shutil.rmtree` is what `main._remove_workspace`,
    `worker._sync_skills` and `memory.delete_promoted_skill` all reach for."""
    victim = _outside(tmp_path)
    with pytest.raises(AssertionError, match="outside this test's tmp_path"):
        shutil.rmtree(victim)
    assert (victim / "precious.txt").read_text(encoding="utf-8") == "do not lose me"


def test_an_overwrite_outside_tmp_path_is_refused_and_the_contents_survive(tmp_path):
    """Truncation destroys just as thoroughly as removal, and every truncating
    write in `app/` goes through `write_text`/`write_bytes`."""
    victim = _outside(tmp_path) / "precious.txt"
    with pytest.raises(AssertionError):
        victim.write_text("clobbered", encoding="utf-8")
    with pytest.raises(AssertionError):
        victim.write_bytes(b"clobbered")
    with pytest.raises(AssertionError):
        victim.open("w")
    assert victim.read_text(encoding="utf-8") == "do not lose me"


def test_unlink_and_rmdir_outside_tmp_path_are_refused(tmp_path):
    victim = _outside(tmp_path)
    with pytest.raises(AssertionError):
        (victim / "precious.txt").unlink()
    with pytest.raises(AssertionError):
        os.remove(victim / "precious.txt")
    with pytest.raises(AssertionError):
        os.rmdir(victim)
    assert (victim / "precious.txt").is_file()


def test_a_move_is_refused_for_its_source_as_well_as_its_destination(tmp_path):
    """`shutil.move` and `os.rename` remove the source, so a move that reads
    from outside is a delete outside however innocent the destination looks.
    `subprojects.py` renames a workspace directory, which is this shape.

    The `match=` pins which layer refuses, and that is the point of it rather
    than fussiness. `shutil.move` is guarded in its own right AND happens to
    call `os.rename` first, so dropping its guard changes nothing today - a
    mutation deleting it survived. But "today" is a detail of CPython's
    `shutil`: a cross-device move never reaches `os.rename` at all, it copies
    and then unlinks. Asserting the refusal comes from `shutil.move` keeps the
    outer guard real instead of quietly depending on the inner one.
    """
    victim = _outside(tmp_path)
    with pytest.raises(AssertionError, match=r"shutil\.move tried to write"):
        shutil.move(str(victim / "precious.txt"), str(tmp_path / "taken.txt"))
    with pytest.raises(AssertionError, match=r"os\.rename tried to write"):
        os.rename(victim / "precious.txt", tmp_path / "taken.txt")

    (tmp_path / "mine.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(AssertionError):
        shutil.move(str(tmp_path / "mine.txt"), str(victim / "pushed.txt"))

    assert (victim / "precious.txt").is_file()
    assert not (victim / "pushed.txt").exists()


def test_a_symlink_out_of_tmp_path_cannot_be_written_through(tmp_path):
    """The obvious way around a path check: the link sits innocently inside
    `tmp_path` and the write lands wherever it points. `_fs_targets` resolves
    with `realpath`, so the destination is what gets judged."""
    victim = _outside(tmp_path)
    bridge = tmp_path / "bridge.txt"
    bridge.symlink_to(victim / "precious.txt")
    with pytest.raises(AssertionError):
        bridge.write_text("clobbered", encoding="utf-8")
    assert (victim / "precious.txt").read_text(encoding="utf-8") == "do not lose me"


def test_the_cgroup_exemption_does_not_admit_any_other_unit(tmp_path):
    """The one write outside `tmp_path` the suite is allowed is narrow on
    purpose. A `cgroup.procs` belonging to a unit the suite did not create is
    somebody's live process, and writing a pid into it moves that process."""
    ours = "/sys/fs/cgroup/whatever/portal-stray-9006-2405324-1.scope/cgroup.procs"
    assert conftest._fs_is_allowed(ours)

    for theirs in (
        "/sys/fs/cgroup/whatever/portal-run-679-2339162-1.scope/cgroup.procs",
        "/sys/fs/cgroup/whatever/portal-stray-9006-2405324-1.scope/cgroup.kill",
        "/sys/fs/cgroup/user.slice/cgroup.procs",
        "/sys/fs/cgroup/cgroup.procs",
    ):
        assert not conftest._fs_is_allowed(theirs), theirs


def test_a_file_descriptor_is_not_judged_as_a_path(tmp_path):
    """`os.truncate` and friends accept an open fd, which names no path. An int
    is not evidence of anything, and it can only reach a file the caller already
    opened - so `_fs_targets` returns nothing rather than guessing."""
    assert conftest._fs_targets(3) == []


# --- what the fence must NOT break -------------------------------------------
#
# Without these, a fence that refused every write would pass everything above.

def test_ordinary_work_inside_tmp_path_still_happens(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "f.txt").write_text("hello", encoding="utf-8")
    (deep / "f.txt").rename(deep / "g.txt")
    assert (deep / "g.txt").read_text(encoding="utf-8") == "hello"

    # rmtree walks with file descriptors on Linux, calling `os.unlink(name,
    # dir_fd=fd)` with a bare basename that resolves against neither the cwd nor
    # anything else the fence can see. A tree deep enough to exercise that walk
    # is the point of this case, not the removal itself.
    shutil.rmtree(tmp_path / "a")
    assert not (tmp_path / "a").exists()


def test_copying_from_the_real_tree_into_tmp_path_still_works(tmp_path):
    """`worker._sync_skills` copies `app/skills` - a real directory in the
    source tree - into a workspace. Reads are not fenced and must not be: only
    the destination of a copy is a write."""
    src = Path(__file__).resolve().parents[1] / "app" / "skills"
    assert src.is_dir(), "the skills the worker syncs should be in the tree"
    shutil.copytree(src, tmp_path / "skills")
    assert (tmp_path / "skills").is_dir()


def test_reading_the_real_tree_is_not_fenced(tmp_path):
    """Plenty of tests read `style.css`, the systemd unit and the templates.
    A read has no blast radius and fencing it would buy nothing."""
    css = Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"
    assert len(css.read_text(encoding="utf-8")) > 0


# --- no test may push to the real public repository --------------------------

def test_the_public_mirror_is_pointed_away_from_the_real_one(tmp_path):
    """`app/mirror.py` publishes and pushes to GitHub from the worker tick, and
    eleven test files drive that tick.

    Its target is a sibling of the *source* checkout rather than of the data
    directory, so none of the `config` redirections in `temp_data_dir` move it:
    left alone it is `../project-portal-public`, a real repo holding a real
    deploy key with write access. The only thing standing between the suite and
    a push would be `mirror.pending()` happening to say no.

    Pointing it at a path that does not exist makes `configured()` False on one
    `.exists()` call, before git is shelled out to at all.
    """
    from app import mirror

    assert mirror.TARGET.parent == tmp_path
    assert not mirror.TARGET.exists()
    assert mirror.configured() is False
    assert mirror.pending() is None


# --- no test may start, stop or restart a unit it does not own ---------------

def test_restarting_the_portal_service_is_refused(tmp_path):
    """The call this fence exists for.

    `worker._fire_restart` shells out to exactly this, and what held it back was
    a `monkeypatch.setattr(worker.subprocess, "run", ...)` in a fixture inside
    the file that calls it. A test reaching that path without knowing it does -
    through a mutation, or through a new caller - would restart the live portal
    and take every agent run on the machine with it, including itself.
    """
    with pytest.raises(AssertionError, match="does not own"):
        conftest._systemd_guard([
            "systemd-run", "--user", "--on-active=3",
            "--timer-property=AccuracySec=1ms",
            "systemctl", "--user", "restart", "project-portal.service",
        ])


def test_stopping_another_processes_run_scope_is_refused(tmp_path):
    """A real agent's scope carries the portal service's pid, never pytest's.
    Stopping one is how a live run loses the handle its own row records."""
    with pytest.raises(AssertionError, match="does not own"):
        conftest._systemd_guard(
            ["systemctl", "--user", "stop", "portal-run-679-2339162-1.scope"])


def test_a_unit_carrying_this_processes_pid_may_be_stopped(tmp_path):
    """Ownership without a convention to remember: `runlimit.scope_name` mints
    names from the spawning process's pid, so anything this suite spawned says
    so in its own name. Both shapes in the suite are accepted."""
    pid = os.getpid()
    conftest._systemd_guard(
        ["systemctl", "--user", "stop", f"portal-run-1-{pid}-1.scope"])
    conftest._systemd_guard(
        ["systemd-run", "--user", "--scope", f"--unit=portal-killtest-{pid}.scope",
         "--", "sleep", "1"])
    conftest._systemd_guard(
        ["systemctl", "--user", "stop", "portal-stray-9006-2405324-1.scope"])


def test_a_pid_is_matched_as_a_whole_component_not_a_substring(tmp_path):
    """`portal-run-1-{pid}9-1.scope` is somebody else's unit that merely starts
    with our digits. A substring test would hand it over."""
    with pytest.raises(AssertionError, match="does not own"):
        conftest._systemd_guard(
            ["systemctl", "--user", "stop", f"portal-run-1-{os.getpid()}9-1.scope"])


def test_reading_the_real_systemd_is_still_allowed(tmp_path):
    """`is-active` and `show` tell the truth about the machine and change
    nothing, and `orphans` and `_reap_adopted` genuinely want that answer. Only
    starting, stopping and restarting are fenced."""
    conftest._systemd_guard(
        ["systemctl", "--user", "is-active", "portal-run-679-2339162-1.scope"])
    conftest._systemd_guard(
        ["systemctl", "--user", "show", "-p", "ControlGroup", "--value",
         "project-portal.service"])
    # `runlimit._probe` - an anonymous transient scope with no name to collide.
    conftest._systemd_guard(
        ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", "true"])


def test_the_guard_ignores_commands_that_are_not_systemd(tmp_path):
    """The verb list means nothing outside systemd, and this matters because
    the portal runs commands it did not write: `launch.py` executes whatever a
    project's `.portal/serve.json` says, which on this machine is as likely to
    be a `docker` line as anything else. Judging those by systemd's vocabulary
    would refuse a perfectly good serve recipe.

    Both arguments below would trip every other rule in the guard - a mutating
    verb and a unit-shaped token - and are let through purely on `argv[0]`.
    """
    conftest._systemd_guard(["docker", "restart", "project-portal.service"])
    conftest._systemd_guard(["git", "stash", "drop"])
    conftest._systemd_guard(["flock", "--close", "-n", str(tmp_path), "--", "true"])


def test_the_fence_reaches_subprocess_and_asyncio_alike(tmp_path):
    """Not a separate mechanism for each spawn API: `asyncio`'s unix event loop
    builds its child through `subprocess.Popen`, so wrapping `subprocess` covers
    `create_subprocess_exec` too. `test_runlimit.py` spawns that way and this
    fence caught it, which is how the pid rule came to be written."""
    import subprocess

    with pytest.raises(AssertionError, match="does not own"):
        subprocess.run(["systemctl", "--user", "stop", "project-portal.service"])
    with pytest.raises(AssertionError, match="does not own"):
        subprocess.Popen(["systemctl", "--user", "restart", "project-portal.service"])


# --- the reason `builtins.open` is left alone --------------------------------

def test_no_module_in_app_opens_a_file_in_a_truncating_mode():
    """`conftest.py` does not fence `builtins.open`, because doing so would put
    a wrapper in front of every read pytest itself performs for a class of write
    that `app/` does not make. That claim is only safe while it stays true, so
    it is pinned here rather than left as a comment.

    Append mode is fine and deliberately allowed: `a` cannot truncate, and it is
    how the archive, the run log and `learnings.md` are written.
    """
    app_dir = Path(__file__).resolve().parents[1] / "app"
    # `open(x, "w")`, `open(x, mode="w+b")`, `io.open(x, 'x')` - any mode
    # literal containing w or x, which are the two that destroy what was there.
    pattern = re.compile(r"\bopen\([^)\n]*['\"][rbt+]*[wx][^'\"]*['\"]")
    offenders = [
        f"{path.relative_to(app_dir.parent)}:{n}: {line.strip()}"
        for path in sorted(app_dir.rglob("*.py"))
        if "__pycache__" not in path.parts
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, (
        "app/ now truncates a file through open(), which the conftest fence does "
        "not see. Either route the write through Path.write_text/write_bytes, or "
        "fence builtins.open and delete this test:\n" + "\n".join(offenders)
    )

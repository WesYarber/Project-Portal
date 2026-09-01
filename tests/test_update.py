"""`deploy/update.py` - how an install that is not the development box keeps up.

The portal is developed on one machine and, since 2026-09-01, run on more than
one. The development box publishes itself to GitHub automatically (see
`app/mirror.py` and `tests/test_mirror.py`); this is the other end of that
wire, and it is the end that runs on a machine nobody is watching.

Which is why most of what is tested here is the script declining to do things.
An update replaces working code, and the only outcomes worse than "did not
update" are "updated into a fork nobody can reason about" and "restarted into
code that does not import". Both have their own test, and both assert that the
checkout was left exactly as it was found.

Everything runs against real local git repositories - a bare repo standing in
for GitHub and a clone standing in for the second machine - so the fetch, the
fast-forward and the refusal to merge are git's real answers.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy import update  # noqa: E402
from deploy.setup import Report  # noqa: E402


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check
    )


def _identify(repo):
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")


def _commit(repo, name, body, message="a commit"):
    (repo / name).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """A bare repo standing in for GitHub, with one commit on `main`."""
    bare = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True)
    _identify(seed)
    _commit(seed, "requirements.txt", "fastapi\n")
    _commit(seed, "app.py", "print('one')\n")
    _git(seed, "push", "-q", "origin", "main")
    return bare


@pytest.fixture
def install(tmp_path, upstream, monkeypatch):
    """A clone standing in for Wes's second machine, pointed at by the script."""
    repo = tmp_path / "install"
    subprocess.run(["git", "clone", "-q", str(upstream), str(repo)], check=True)
    _identify(repo)
    monkeypatch.setattr(update, "ROOT", repo)
    return repo


def _push_upstream(tmp_path, upstream, name, body, message="upstream work"):
    """Make a new commit on the bare repo, the way the development box would."""
    work = tmp_path / "pusher"
    if not work.exists():
        subprocess.run(["git", "clone", "-q", str(upstream), str(work)], check=True)
        _identify(work)
    _git(work, "pull", "-q", "--ff-only")
    head = _commit(work, name, body, message)
    _git(work, "push", "-q", "origin", "main")
    return head


# --- before anything is touched ---------------------------------------------


def test_a_directory_that_is_not_a_checkout_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "ROOT", tmp_path / "not-a-repo")
    report = Report()
    assert update.check_repo(report) is False
    assert "not a git checkout" in report.failures[0]


def test_a_checkout_with_no_origin_is_refused_with_the_command_to_fix_it(tmp_path, monkeypatch):
    """It follows nothing, and the fix is one line, so print the line."""
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _identify(repo)
    _commit(repo, "a.py", "x")
    monkeypatch.setattr(update, "ROOT", repo)

    report = Report()
    assert update.check_repo(report) is False
    assert "git remote add origin" in report.failures[0]


def test_an_uncommitted_edit_stops_the_update_and_names_the_files(install):
    """Stopped before git is asked to do anything, because the alternatives are
    merging somebody's edit or discarding it, and a script does neither."""
    (install / "app.py").write_text("print('local edit')\n")
    report = Report()

    assert update.check_repo(report) is False
    assert "app.py" in report.failures[0]


def test_an_untracked_file_does_not_stop_the_update(install):
    """`data/`, `secrets/` and `portal.toml` are untracked and present on every
    real install. Counting them would make every update refuse forever."""
    (install / "portal.toml").write_text("host = 'box'\n")
    report = Report()

    assert update.check_repo(report) is True


def test_a_clean_follower_passes_the_checks(install):
    report = Report()
    assert update.check_repo(report) is True
    assert update.fetch(report) is True
    assert report.failed is False


# --- the fast-forward -------------------------------------------------------


def test_an_install_level_with_origin_changes_nothing(install, capsys):
    report = Report()
    update.fetch(report)
    ok, files = update.fast_forward(report, check_only=False)

    assert ok is True
    assert files == []
    # Asserted on the output, because the return value cannot tell these apart:
    # without the early return the merge runs as a no-op and hands back the same
    # `(True, [])`. What changes is what the person reading it is told, and
    # whether the script ran git for nothing.
    assert "up to date with origin" in capsys.readouterr().out


def test_new_upstream_commits_are_fast_forwarded(tmp_path, upstream, install):
    before = _git(install, "rev-parse", "HEAD").stdout.strip()
    head = _push_upstream(tmp_path, upstream, "app.py", "print('two')\n")
    report = Report()
    update.fetch(report)

    ok, files = update.fast_forward(report, check_only=False)

    assert ok is True
    assert files == ["app.py"]
    assert _git(install, "rev-parse", "HEAD").stdout.strip() == head
    assert head != before


def test_check_reports_the_move_without_making_it(tmp_path, upstream, install):
    before = _git(install, "rev-parse", "HEAD").stdout.strip()
    _push_upstream(tmp_path, upstream, "app.py", "print('two')\n")
    report = Report()
    update.fetch(report)

    ok, files = update.fast_forward(report, check_only=True)

    assert ok is True
    assert files == ["app.py"]  # so --check can still say pip would run
    assert _git(install, "rev-parse", "HEAD").stdout.strip() == before
    assert report.pending  # and says so as work outstanding


def test_a_diverged_install_is_a_hard_stop_that_moves_nothing(tmp_path, upstream, install):
    """The single most important refusal in this file. A follower that has
    fast-forwarded is always at a published commit; one that has merged local
    work is a fork, and the first sign of it is a conflict mid-update."""
    _push_upstream(tmp_path, upstream, "app.py", "print('upstream')\n")
    local = _commit(install, "local.py", "print('mine')\n")
    report = Report()
    update.fetch(report)

    assert update.diverged() is True
    ok, files = update.fast_forward(report, check_only=False)

    assert ok is False
    assert "cannot be fast-forwarded" in report.failures[0]
    assert _git(install, "rev-parse", "HEAD").stdout.strip() == local


def test_the_ff_only_flag_still_refuses_if_the_divergence_check_is_wrong(
    tmp_path, upstream, install, monkeypatch
):
    """`--ff-only` and `diverged()` guard the same invariant, and that is on
    purpose: `diverged()` exists to produce a good message before anything is
    touched, and `--ff-only` is git itself enforcing it on the one irreversible
    step. Redundant guards are usually a smell, so this test makes the second
    one load-bearing - with the first stubbed away, a plain `git merge` would
    write a merge commit into a follower install and `--ff-only` does not."""
    _push_upstream(tmp_path, upstream, "app.py", "print('upstream')\n")
    local = _commit(install, "local.py", "print('mine')\n")
    monkeypatch.setattr(update, "diverged", lambda: False)
    report = Report()
    update.fetch(report)

    ok, _ = update.fast_forward(report, check_only=False)

    assert ok is False
    assert _git(install, "rev-parse", "HEAD").stdout.strip() == local


def test_an_install_merely_behind_is_not_diverged(tmp_path, upstream, install):
    """The boundary the refusal above turns on: behind is fine, ahead is not."""
    _push_upstream(tmp_path, upstream, "app.py", "print('upstream')\n")
    report = Report()
    update.fetch(report)

    assert update.diverged() is False


# --- dependencies -----------------------------------------------------------


def test_pip_runs_only_when_requirements_actually_moved(install, monkeypatch):
    """An unconditional `pip install -r` is half a minute of network on every
    update, and almost every update touches Python files only."""
    calls = []
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: calls.append(a) or None)
    report = Report()

    assert update.install_requirements(report, ["app/main.py", "README.md"], False) is True
    assert calls == []


def test_pip_runs_when_requirements_moved(install, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setattr(update, "venv_python", lambda: install / "venv" / "bin" / "python")
    (install / "venv" / "bin").mkdir(parents=True)
    (install / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    report = Report()

    assert update.install_requirements(report, ["requirements.txt"], False) is True
    assert any("pip" in part for part in calls[0])


def test_a_failed_pip_install_stops_before_the_restart(install, monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "no matching distribution")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setattr(update, "venv_python", lambda: install / "venv" / "bin" / "python")
    (install / "venv" / "bin").mkdir(parents=True)
    (install / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    report = Report()

    assert update.install_requirements(report, ["requirements.txt"], False) is False
    assert "no matching distribution" in report.failures[0]


# --- checking the new code before running it --------------------------------


def test_code_that_does_not_import_leaves_the_service_alone(install, monkeypatch):
    """The check that happens while the old process is still serving. If it
    fails, the restart never happens and the portal stays up on the old code."""
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "ModuleNotFoundError: no module named 'httpx'")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setattr(update, "venv_python", lambda: install / "venv" / "bin" / "python")
    (install / "venv" / "bin").mkdir(parents=True)
    (install / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    report = Report()

    assert update.import_check(report) is False
    assert "running service has been left alone" in report.failures[0]


def test_the_import_check_boots_nothing(install, monkeypatch):
    """An import, not `uvicorn`. Booting `app.main:app` against a live data
    directory settles the running service's in-flight runs as orphans and binds
    the preview port out from under it - a health check that breaks the thing
    it is checking."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env", {})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setattr(update, "venv_python", lambda: install / "venv" / "bin" / "python")
    (install / "venv" / "bin").mkdir(parents=True)
    (install / "venv" / "bin" / "python").write_text("#!/bin/sh\n")

    update.import_check(Report())

    assert "uvicorn" not in " ".join(seen["cmd"])
    assert "import app.main" in " ".join(seen["cmd"])
    assert seen["env"].get("PORTAL_SMOKE_TEST") == "1"


# --- the restart ------------------------------------------------------------


def test_no_systemd_unit_is_a_note_to_a_person_not_a_failure(install, monkeypatch):
    """Running the portal in a terminal or a container is a real install. It
    just has to be restarted by whoever started it."""
    monkeypatch.setattr(update, "service_active", lambda: False)
    report = Report()

    assert update.restart(report, check_only=False) is True
    assert report.failed is False
    assert any("restart the portal" in item for item in report.human)

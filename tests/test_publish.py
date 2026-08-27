"""Building the public repository, and the one step that never ran.

`deploy/publish.py` had six commits sitting in the public tree, none of them on
any remote, and `--push` returned 0 without reaching the push on every single
invocation - because "nothing changed since the last publish" was treated as an
answer to both questions at once. The tests here drive the real script against
a real local bare repository, so "it pushed" means a ref actually moved.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("portal_publish", ROOT / "deploy" / "publish.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish = _load()


def _git(where: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=where, capture_output=True, text=True, check=True)


def _source_tree(at: Path) -> Path:
    """A stand-in for the private repo: a git repo with one innocuous file.

    It has to be a real repo because the script publishes `git ls-files` and
    nothing else - that is the property that keeps `data/` and `secrets/` out,
    and a fixture that faked it would not be testing the script that ships.
    """
    at.mkdir(parents=True, exist_ok=True)
    _git(at, "init", "-q", "-b", "main")
    _git(at, "config", "user.name", "A Tester")
    _git(at, "config", "user.email", "tester@example.com")
    (at / "README.md").write_text("# a project\n\nNothing personal in here.\n")
    _git(at, "add", "-A")
    _git(at, "commit", "-q", "-m", "initial")
    return at


def _bare_origin(at: Path) -> Path:
    subprocess.run(["git", "init", "-q", "--bare", str(at)], check=True)
    return at


def _run(monkeypatch, source: Path, target: Path, *argv: str) -> int:
    monkeypatch.setattr(publish, "ROOT", source)
    monkeypatch.setattr(sys, "argv", ["publish.py", "--to", str(target), *argv])
    return publish.main()


def _pushed_refs(origin: Path) -> str:
    """The branches the bare repo actually has.

    Not `rev-parse HEAD`: a bare repo initialized here defaults its HEAD to
    whatever `init.defaultBranch` says, and the script pushes `main`, so the
    symref points at a branch that does not exist and HEAD resolves to nothing
    on a repo that received the push perfectly well. That read the same as "it
    never pushed" and cost this test two runs.
    """
    out = subprocess.run(
        ["git", "--git-dir", str(origin), "for-each-ref", "--format=%(refname)"],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


# --- the defect ------------------------------------------------------------

def test_push_still_happens_when_nothing_changed_since_the_last_publish(tmp_path, monkeypatch):
    """The first push after a remote is added is EXACTLY the case where nothing
    has changed locally, so returning early there means the repository can
    never make it out at all."""
    source = _source_tree(tmp_path / "src")
    target = tmp_path / "public"
    origin = _bare_origin(tmp_path / "origin.git")

    assert _run(monkeypatch, source, target, ) == 0  # first publish, commits
    _git(target, "remote", "add", "origin", str(origin))
    assert _pushed_refs(origin) == "", "nothing should be on the remote yet"

    # Second run: the source has not moved, so there is nothing new to commit.
    assert _run(monkeypatch, source, target, "--push") == 0
    assert "refs/heads/main" in _pushed_refs(origin), "the remote never received anything"


def test_nothing_changed_and_no_push_asked_for_still_returns_early(tmp_path, monkeypatch):
    """The other direction, so the fix above did not simply delete a branch."""
    source = _source_tree(tmp_path / "src")
    target = tmp_path / "public"
    assert _run(monkeypatch, source, target) == 0
    assert _run(monkeypatch, source, target) == 0


def test_a_change_is_committed_and_pushed_in_one_go(tmp_path, monkeypatch):
    source = _source_tree(tmp_path / "src")
    target = tmp_path / "public"
    origin = _bare_origin(tmp_path / "origin.git")

    assert _run(monkeypatch, source, target) == 0
    _git(target, "remote", "add", "origin", str(origin))
    (source / "NEW.md").write_text("# something new\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "add a file")

    assert _run(monkeypatch, source, target, "--push") == 0
    listing = subprocess.run(
        ["git", "--git-dir", str(origin), "ls-tree", "--name-only", "refs/heads/main"],
        capture_output=True,
        text=True,
    ).stdout
    assert "NEW.md" in listing


def test_a_failed_push_reports_the_remedy_and_a_nonzero_status(tmp_path, monkeypatch, capsys):
    """No remote configured is the state a fresh public tree is in, and the
    message is the only thing standing between the reader and a guess."""
    source = _source_tree(tmp_path / "src")
    target = tmp_path / "public"
    assert _run(monkeypatch, source, target) == 0
    assert _run(monkeypatch, source, target, "--push") == 1
    assert "remote add origin" in capsys.readouterr().err


# --- what it will and will not carry ---------------------------------------

def test_only_tracked_files_are_published(tmp_path, monkeypatch):
    """`.gitignore` has already decided what is secret, and re-deciding it here
    is how `data/` or `secrets/` would eventually be copied by accident."""
    source = _source_tree(tmp_path / "src")
    (source / ".gitignore").write_text("secrets/\n")
    (source / "secrets").mkdir()
    (source / "secrets" / "key.txt").write_text("a credential\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "add a gitignore")

    target = tmp_path / "public"
    assert _run(monkeypatch, source, target) == 0
    assert not (target / "secrets").exists()
    assert (target / "README.md").exists()


def test_a_leak_refuses_the_publish_and_commits_nothing(tmp_path, monkeypatch):
    """The gate, and the reason it scans the STAGED tree rather than the source
    - this is also where a private path that failed to be excluded is caught."""
    source = _source_tree(tmp_path / "src")
    (source / "README.md").write_text("# ssh to ada-box\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "name a machine")

    target = tmp_path / "public"
    monkeypatch.setattr(publish.leakscan, "extra_patterns", lambda: ["ada-box"])
    assert _run(monkeypatch, source, target) == 1
    assert not (target / ".git").exists(), "a refused publish must not have committed"


def test_it_refuses_to_publish_over_the_source_tree(tmp_path, monkeypatch):
    """`stage()` deletes everything in the target that is not `.git`."""
    source = _source_tree(tmp_path / "src")
    assert _run(monkeypatch, source, source) == 2
    assert (source / "README.md").exists()

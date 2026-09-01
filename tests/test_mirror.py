"""The public mirror follows the source without anyone remembering to push.

The bug this guards is not a crash, it is a silence: `deploy/publish.py` has
always worked and three runs in a row simply did not run it, so the public repo
sat five commits behind while every test stayed green. Wes then went to install
the portal on a second computer, which follows this one by pulling from GitHub -
at which point "an agent remembered" became the synchronization mechanism
between two of his machines.

So these tests are mostly about *not* publishing: the four states in which a
tick must decide the mirror is fine, and the two in which it must decide it is
not. They run against real local git repositories, with the mirror's `origin`
pointed at a bare repo on disk, so the push and the remote-tracking ref it
updates are the real ones rather than a stub's idea of them.
"""
from __future__ import annotations

import inspect
import subprocess

import pytest

from app import config, db, mirror


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check
    )


def _init(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.invalid")
    return path


def _commit(repo, name="file.txt", body="hello"):
    (repo / name).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a commit")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def source(tmp_path, monkeypatch):
    """A stand-in for the portal's own checkout, as `config.APP_ROOT`."""
    repo = _init(tmp_path / "source")
    _commit(repo)
    monkeypatch.setattr(config, "APP_ROOT", repo)
    return repo


@pytest.fixture
def target(tmp_path):
    """A mirror with a real `origin`, so pushing is a real push."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    repo = _init(tmp_path / "public")
    _git(repo, "remote", "add", "origin", str(bare))
    return repo


# --- is this the machine that publishes? -----------------------------------


def test_a_directory_that_is_not_a_repo_is_not_the_publishing_machine(tmp_path):
    """The whole guard against this running anywhere but Wes's server. A fresh
    clone on his other computer has no sibling public tree at all, and must
    never try to push one."""
    assert mirror.configured(tmp_path / "nothing-here") is False


def test_a_mirror_with_no_remote_is_not_the_publishing_machine(tmp_path):
    """Also the documented off switch: `git remote remove origin` stops it."""
    repo = _init(tmp_path / "public")
    assert mirror.configured(repo) is False


def test_a_mirror_with_a_remote_is_the_publishing_machine(target):
    assert mirror.configured(target) is True


def test_an_unconfigured_mirror_is_never_pending(source, tmp_path):
    """Not merely "does not publish" - reports nothing to publish, so no tick
    on any other install can journal an alarm about a repo it does not have."""
    assert mirror.pending(tmp_path / "nothing-here") is None


# --- the trailer, which is the only thing linking the two histories ---------


def test_the_trailer_round_trips_from_publish_to_mirror(source, target):
    """`publish.stamped` writes it and `mirror.published_head` reads it; the
    two live in different files and nothing else connects the repositories,
    since the public history starts fresh and shares no commit id."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_publish", config.BASE_DIR / "deploy" / "publish.py"
    )
    publish_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish_mod)

    (target / "x").write_text("x")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", publish_mod.stamped("Update from upstream"))

    assert mirror.published_head(target) == mirror.source_head()


def test_a_mirror_commit_with_no_trailer_reads_as_unknown(target):
    """Every publish made before this existed. Unknown is treated as behind,
    which costs one extra publish and then knows."""
    _commit(target, "x", "x")
    assert mirror.published_head(target) is None


def test_an_empty_mirror_reads_as_unknown(target):
    assert mirror.published_head(target) is None


def test_a_trailer_with_nothing_after_it_reads_as_unknown(target):
    """`Source-commit:` and then nothing is not a sha, and treating it as one
    would compare the source head against the empty string forever - a mirror
    permanently, silently behind."""
    (target / "x").write_text("x")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", f"Update from upstream\n\n{mirror.TRAILER}   \n")

    assert mirror.published_head(target) is None


def test_the_publish_script_stamps_the_commit_it_makes(source):
    """Pins the wiring rather than the function. `stamped()` can be perfect and
    unused: `publish.py` committing the plain message would leave every mirror
    commit untrailered, so `published_head` would read None forever and the
    portal would republish on every single tick."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_publish_wiring", config.BASE_DIR / "deploy" / "publish.py"
    )
    publish_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish_mod)

    assert "stamped(message)" in inspect.getsource(publish_mod.main)


# --- has it fallen behind? --------------------------------------------------


def _publish_by_hand(source, target):
    """Commit the source's head into the mirror the way a real publish does."""
    head = mirror.source_head()
    (target / "copy.txt").write_text((source / "file.txt").read_text())
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", f"Update from upstream\n\n{mirror.TRAILER} {head}\n")
    return head


def test_a_mirror_at_the_source_head_and_pushed_is_not_pending(source, target):
    _publish_by_hand(source, target)
    _git(target, "push", "-q", "origin", "HEAD")
    assert mirror.pending(target) is None


def test_a_source_commit_the_mirror_has_not_seen_is_pending(source, target):
    _publish_by_hand(source, target)
    _git(target, "push", "-q", "origin", "HEAD")
    new_head = _commit(source, "file.txt", "changed")
    reason = mirror.pending(target)
    assert reason is not None and new_head[:7] in reason


def test_a_mirror_committed_but_never_pushed_is_pending(source, target):
    """The failure that made this repo six commits unpushable once already:
    the tree matches the source, so every "is it up to date" check that looks
    at content alone says yes, and GitHub has none of it."""
    _publish_by_hand(source, target)
    reason = mirror.pending(target)
    assert reason == "the last publish was committed but never pushed"


def test_a_mirror_that_has_never_pushed_at_all_is_unpushed(source, target):
    """No remote-tracking ref exists yet, which is the state right after a
    remote is first added - and is not the same as being level with it."""
    _publish_by_hand(source, target)
    assert mirror.unpushed(target) is True


def test_a_pushed_mirror_is_not_unpushed(source, target):
    _publish_by_hand(source, target)
    _git(target, "push", "-q", "origin", "HEAD")
    assert mirror.unpushed(target) is False


def test_a_commit_made_after_a_push_is_unpushed_again(source, target):
    """The case that actually compares the two heads. Every other state answers
    from a returncode - no HEAD at all, or no remote-tracking ref - so without
    this the comparison could be a constant `False` and nothing would notice.
    It is also the real-world shape: the mirror is pushed for months, then one
    publish commits and the push fails."""
    _publish_by_hand(source, target)
    _git(target, "push", "-q", "origin", "HEAD")
    _commit(source, "file.txt", "changed")
    _publish_by_hand(source, target)

    assert mirror.unpushed(target) is True


def test_a_mirror_with_no_commits_at_all_has_nothing_to_push(target):
    """Not "everything is unpushed": there is nothing there. The publish that
    is about to run makes the first commit and pushes it in one go."""
    assert mirror.unpushed(target) is False


# --- the clean-tree guard ---------------------------------------------------


def test_a_dirty_source_tree_publishes_nothing(source, target):
    """`publish.py` copies the working tree, not a commit. Automatically, that
    would mirror a run's half-written edit and stamp it with a source commit
    that does not contain it."""
    _publish_by_hand(source, target)
    _git(target, "push", "-q", "origin", "HEAD")
    _commit(source, "file.txt", "committed change")
    (source / "file.txt").write_text("uncommitted edit")

    assert mirror.source_clean() is False
    assert mirror.pending(target) is None


def test_an_untracked_file_does_not_count_as_dirty(source, target):
    """`data/`, `secrets/` and every run's scratch file are untracked and at
    least one of them exists at all times on the live box. Counting them would
    mean never publishing again - and none of them can reach the mirror, which
    copies what `git ls-files` lists."""
    _publish_by_hand(source, target)
    _git(target, "push", "-q", "origin", "HEAD")
    new_head = _commit(source, "file.txt", "changed")
    (source / "scratch.tmp").write_text("a run's leftovers")

    assert mirror.source_clean() is True
    reason = mirror.pending(target)
    assert reason is not None and new_head[:7] in reason


def test_a_source_that_is_not_a_repo_publishes_nothing(tmp_path, monkeypatch, target):
    monkeypatch.setattr(config, "APP_ROOT", tmp_path / "not-a-repo")
    assert mirror.source_head() == ""
    assert mirror.pending(target) is None


# --- the tick ---------------------------------------------------------------


@pytest.fixture
def meta_project():
    return db.create_project(
        config.META_PROJECT_SLUG, "Project Portal", "the portal itself"
    )


def test_the_tick_publishes_when_the_mirror_is_behind(source, target, monkeypatch):
    calls = []
    monkeypatch.setattr(
        mirror, "publish", lambda t=mirror.TARGET: calls.append(t) or mirror.Outcome(True, "Pushed.")
    )
    _publish_by_hand(source, target)
    _git(target, "push", "-q", "origin", "HEAD")
    _commit(source, "file.txt", "changed")

    outcome = mirror.tick(target)
    assert outcome is not None and outcome.ok
    assert calls == [target]


def test_the_tick_publishes_nothing_when_the_mirror_is_level(source, target, monkeypatch):
    calls = []
    monkeypatch.setattr(mirror, "publish", lambda t=mirror.TARGET: calls.append(t))
    _publish_by_hand(source, target)
    _git(target, "push", "-q", "origin", "HEAD")

    assert mirror.tick(target) is None
    assert calls == []


def test_a_failed_publish_backs_off_instead_of_retrying_every_minute(
    source, target, monkeypatch, meta_project
):
    """The tick runs every minute forever and a push fails for reasons that do
    not clear in a minute, so without this a dead network costs three seconds
    of git a minute and a journal entry a minute alongside it."""
    calls = []
    monkeypatch.setattr(
        mirror, "publish",
        lambda t=mirror.TARGET: calls.append(t) or mirror.Outcome(False, "could not reach github"),
    )
    _commit(source, "file.txt", "changed")

    assert mirror.tick(target, now=1000.0) is not None
    assert len(calls) == 1
    # Still behind, but inside the backoff window.
    assert mirror.tick(target, now=1000.0 + mirror.RETRY_BACKOFF_SEC - 1) is None
    assert len(calls) == 1
    assert mirror.tick(target, now=1000.0 + mirror.RETRY_BACKOFF_SEC) is not None
    assert len(calls) == 2


def test_the_backoff_widens_with_each_failure(source, target, monkeypatch, meta_project):
    monkeypatch.setattr(
        mirror, "publish", lambda t=mirror.TARGET: mirror.Outcome(False, "still down")
    )
    _commit(source, "file.txt", "changed")

    mirror.tick(target, now=0.0)
    first = mirror._next_attempt
    mirror.tick(target, now=first)
    second = mirror._next_attempt
    assert second - first > first


def test_a_repeated_failure_is_journalled_once_not_once_a_minute(
    source, target, monkeypatch, meta_project
):
    monkeypatch.setattr(
        mirror, "publish", lambda t=mirror.TARGET: mirror.Outcome(False, "could not reach github")
    )
    _commit(source, "file.txt", "changed")

    for i in range(4):
        mirror.tick(target, now=i * mirror.RETRY_BACKOFF_MAX_SEC * 2)

    entries = db.list_journal(int(meta_project["id"]))
    alarms = [e for e in entries if "could not be published" in (e["content_md"] or "")]
    assert len(alarms) == 1


def test_a_failure_that_changes_shape_is_reported_again(
    source, target, monkeypatch, meta_project
):
    """"No network" and "the leak scan refused" are different problems with
    different fixes, and the second must not be swallowed by the first."""
    reasons = iter(["could not reach github", "REFUSING TO PUBLISH - 1 personal string"])
    monkeypatch.setattr(
        mirror, "publish", lambda t=mirror.TARGET: mirror.Outcome(False, next(reasons))
    )
    _commit(source, "file.txt", "changed")

    mirror.tick(target, now=0.0)
    mirror.tick(target, now=mirror.RETRY_BACKOFF_MAX_SEC * 2)

    entries = db.list_journal(int(meta_project["id"]))
    alarms = [e for e in entries if "could not be published" in (e["content_md"] or "")]
    assert len(alarms) == 2


def test_a_recovery_closes_the_alarm_it_opened(source, target, monkeypatch, meta_project):
    """An unclosed alarm is how a fixed problem keeps being re-investigated by
    the next run that reads the journal."""
    outcomes = iter([mirror.Outcome(False, "could not reach github"), mirror.Outcome(True, "Pushed.")])
    monkeypatch.setattr(mirror, "publish", lambda t=mirror.TARGET: next(outcomes))
    _commit(source, "file.txt", "changed")

    mirror.tick(target, now=0.0)
    mirror.tick(target, now=mirror.RETRY_BACKOFF_MAX_SEC * 2)

    entries = db.list_journal(int(meta_project["id"]))
    assert any("publishing again" in (e["content_md"] or "") for e in entries)


def test_a_first_success_says_nothing_at_all(source, target, monkeypatch, meta_project):
    """The ordinary case is a publish on every self-improving run, several a
    day. Announcing each one would bury the journal in the least interesting
    fact the portal knows."""
    monkeypatch.setattr(mirror, "publish", lambda t=mirror.TARGET: mirror.Outcome(True, "Pushed."))
    _commit(source, "file.txt", "changed")

    mirror.tick(target, now=0.0)

    entries = db.list_journal(int(meta_project["id"]))
    assert not [e for e in entries if "mirror" in (e["content_md"] or "")]


def test_a_publish_that_raises_never_stops_the_worker_tick(source, target, monkeypatch):
    """This runs inside the loop that starts every run on the board."""
    def boom(t=mirror.TARGET):
        raise RuntimeError("git went away")

    monkeypatch.setattr(mirror, "publish", boom)
    _commit(source, "file.txt", "changed")

    assert mirror.tick(target, now=0.0) is None


# --- the worker wiring ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_worker_tick_publishes_the_mirror(meta_project, monkeypatch):
    """`mirror.tick` can be flawless and never called. Deleting the one line in
    `worker._tick` that calls it puts the portal straight back to the state
    this whole module was built to end: publishing only when somebody
    remembers."""
    from app import worker

    called: list[bool] = []
    monkeypatch.setattr(mirror, "tick", lambda *a, **k: called.append(True))

    async def no_start():
        return False

    monkeypatch.setattr(worker, "_start_one", no_start)

    await worker._tick()

    assert called == [True]


@pytest.mark.asyncio
async def test_a_deferred_restart_does_not_hold_the_publish_back(meta_project, monkeypatch):
    """The ordering decision, pinned. A self-update restart waits for every
    other run on the board to finish, which is routinely half an hour - and the
    source change waiting to be loaded is exactly the one another machine is
    waiting to pull. Below the restart branch instead of above it, the mirror's
    worst lag becomes the board's slowest run."""
    from app import worker

    called: list[bool] = []
    monkeypatch.setattr(mirror, "tick", lambda *a, **k: called.append(True))

    async def no_start():
        return False

    monkeypatch.setattr(worker, "_start_one", no_start)
    monkeypatch.setattr(worker, "_pending_restart", (int(meta_project["id"]), "abcdef1234"))
    # With nothing in flight the tick fires the restart it is holding, and the
    # conftest fence rightly refuses to let a test restart a real unit. The
    # restart is not what this asserts.
    monkeypatch.setattr(worker, "_fire_restart", lambda project_id, new_head: None)

    await worker._tick()

    assert called == [True]


# --- the publish subprocess -------------------------------------------------


def test_publish_runs_the_real_script_with_push(source, monkeypatch, tmp_path):
    """Not an import of it: the automatic path and the hand path must be the
    same program, so a change to one is a change to both."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, "Pushed.", "")

    monkeypatch.setattr(mirror.subprocess, "run", fake_run)
    outcome = mirror.publish(tmp_path / "public")

    assert outcome.ok
    assert str(config.APP_ROOT / "deploy" / "publish.py") in seen["cmd"]
    assert "--push" in seen["cmd"]
    assert seen["timeout"] == mirror.PUBLISH_TIMEOUT_SEC


def test_a_nonzero_publish_reports_its_stderr(source, monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "REFUSING TO PUBLISH - 1 personal string")

    monkeypatch.setattr(mirror.subprocess, "run", fake_run)
    outcome = mirror.publish(tmp_path / "public")

    assert outcome.ok is False
    assert "REFUSING TO PUBLISH" in outcome.detail


def test_a_hanging_publish_is_a_failure_not_a_hang(source, monkeypatch, tmp_path):
    """A push against a dead network can hang for a very long time, and this
    tick is what the dashboard is waiting on."""
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, mirror.PUBLISH_TIMEOUT_SEC)

    monkeypatch.setattr(mirror.subprocess, "run", fake_run)
    outcome = mirror.publish(tmp_path / "public")

    assert outcome.ok is False
    assert "did not finish" in outcome.detail

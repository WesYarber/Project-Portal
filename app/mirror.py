"""Keep the public mirror following the source, so nobody has to remember to.

`deploy/publish.py` rebuilds the public repository from this tree, scans it for
leaks and pushes it. It works. It has one failure mode that no
amount of care inside it can fix: a person or an agent has to *run* it. Over
2026-08-29 and 2026-08-30 three consecutive runs changed the portal's own
source and none of them published; the public repo sat five commits behind the
code it claims to be, and that was noticed only because a fourth run happened
to look at it.

That was a cosmetic problem while this machine was the only install. It stopped
being one when Wes went to put the portal on a second computer: an install
elsewhere follows this one by pulling from GitHub, so "an agent remembered to
push" was the entire synchronization mechanism between two of his machines.
This module removes the remembering.

The worker calls `tick()` on its ordinary cadence. Four things decide whether
anything happens:

- **Is this the machine that publishes?** Only if the target tree exists, is a
  git repo, and has an `origin` remote. On a fresh clone somewhere else none of
  that is true, so `tick()` does nothing, silently, forever - which is the
  correct behavior for every install but this one. Removing the remote is also
  the off switch here, and the only one: a settings row for a thing that exists
  on exactly one machine in the world is not worth its own UI.
- **Is the source tree clean?** `publish.py` copies the *working tree* of every
  tracked file, not the contents of a commit. Run by hand that is a feature -
  you can publish a fix before committing it. Run automatically it is a hazard:
  a tick landing in the middle of a run would mirror half-written code and
  stamp it with a source commit that does not describe it. So a dirty tree
  waits, and the wait ends by itself when the run commits.
- **Has the mirror fallen behind?** The public repo has a fresh history on
  purpose (the private log contains a machine password at `f76611b`), so the
  two repos share no commit id and there is nothing to compare directly. The
  publish commit therefore carries a `Source-commit:` trailer naming what it
  was built from, and that trailer is the only record of how far the mirror has
  followed.
- **Did the last publish actually reach GitHub?** Committing locally and
  pushing are different questions, and answering the second with the first is
  precisely how this repo became six commits unpushable once before. A tree
  that matches the source but sits ahead of `origin/main` still needs a tick.

A leak stops everything, loudly. That check lives in `publish.py` and is a hard
stop there; all this module does is refuse to hide the exit code.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app import config, db

log = logging.getLogger(__name__)

# Where `deploy/publish.py` builds the public tree, and the same default it
# uses. The one machine where this path is a git repo with a remote is the one
# machine that publishes.
TARGET = config.APP_ROOT.parent / "project-portal-public"

# The trailer a publish commit carries, naming the source commit its tree was
# copied from. Kept in the commit message rather than in a file or a settings
# row for two reasons: it is then readable from GitHub itself, by anyone
# wondering which upstream commit a release corresponds to, and it cannot drift
# from the commit it describes the way a separate file could.
TRAILER = "Source-commit:"

# A publish is a few hundred file copies, a leak scan over the result and a
# network push; it measures at about three seconds. The timeout is not a
# performance budget, it is a guard against a push hanging on a dead network
# and holding the tick that starts every run on the board.
PUBLISH_TIMEOUT_SEC = 180

# A publish fails for reasons that do not clear themselves in a minute: no
# network, a rotated deploy key, a leak in the tree. The tick runs forever, so
# a failure backs off rather than spending three seconds a minute on the same
# refusal.
RETRY_BACKOFF_SEC = 300.0
RETRY_BACKOFF_MAX_SEC = 3600.0

# Earliest monotonic time a publish may be attempted, and the current backoff.
# Module state rather than a stored value because it is about this process's
# recent luck with the network, and a restart is a fine reason to try again.
_next_attempt: float = 0.0
_backoff: float = RETRY_BACKOFF_SEC

# The failure detail last written to the journal. A publish that keeps failing
# the same way should say so once, not once a minute - but a failure that
# changes shape is news again, and so is the success that ends it.
_reported: Optional[str] = None


@dataclass(frozen=True)
class Outcome:
    """What a publish attempt did, for the journal and for a test to assert on."""

    ok: bool
    detail: str


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True
    )


def source_head() -> str:
    """The source repo's HEAD, or "" if this is not a git checkout at all.

    Shared with `deploy/publish.py`, which stamps the answer into the commit it
    makes, so the two halves cannot disagree about what "the source commit"
    means.
    """
    if not (config.APP_ROOT / ".git").exists():
        return ""
    done = _git(config.APP_ROOT, "rev-parse", "HEAD")
    return done.stdout.strip() if done.returncode == 0 else ""


def source_clean() -> bool:
    """True when every tracked file matches HEAD.

    Untracked files are deliberately ignored: `publish.py` copies what `git
    ls-files` lists, so an untracked file cannot reach the mirror and cannot
    make the trailer a lie. `data/`, `secrets/` and a run's scratch files are
    all untracked, and on a live box at least one of them exists at all times -
    counting them would mean never publishing again.
    """
    done = _git(config.APP_ROOT, "status", "--porcelain", "--untracked-files=no")
    return done.returncode == 0 and not done.stdout.strip()


def configured(target: Optional[Path] = None) -> bool:
    """True when this machine is the one that publishes the mirror.

    `target` resolves `TARGET` at call time rather than defaulting to it in the
    signature. A default argument is evaluated once, at import, so
    `target: Path = TARGET` binds the real public repo into every one of these
    functions permanently - and `tests/conftest.py` repointing `mirror.TARGET`
    away from it would have had no effect at all. The suite would then have
    been one clean tree away from pushing to GitHub from a unit test.
    """
    target = target or TARGET
    if not (target / ".git").exists():
        return False
    return _git(target, "remote", "get-url", "origin").returncode == 0


def published_head(target: Optional[Path] = None) -> Optional[str]:
    """The source commit the mirror's HEAD was built from, or None.

    None means "no idea", which is treated as behind. That covers a mirror
    whose last publish predates this trailer and an empty repo alike; both are
    fixed by publishing once, which is cheap and idempotent.
    """
    target = target or TARGET
    done = _git(target, "log", "-1", "--format=%B")
    if done.returncode != 0:
        return None
    for line in done.stdout.splitlines():
        line = line.strip()
        if line.startswith(TRAILER):
            sha = line[len(TRAILER):].strip()
            return sha or None
    return None


def unpushed(target: Optional[Path] = None) -> bool:
    """True when the mirror has a commit that GitHub has not got.

    Read from the remote-tracking ref rather than by fetching: `git push`
    updates `refs/remotes/origin/*` itself, so the local answer is exact for
    everything this machine did, and this machine is the only writer. It costs
    no network on a tick that runs every minute.

    A mirror with no remote-tracking ref at all has never pushed, so everything
    it has is unpushed - which is the state right after a remote is first
    added, and the one case the old `--push` path got wrong by returning early
    on "nothing changed locally".
    """
    target = target or TARGET
    local = _git(target, "rev-parse", "--verify", "HEAD")
    if local.returncode != 0:
        return False  # nothing committed yet; publishing will make the commit
    branch = _git(target, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    remote = _git(target, "rev-parse", "--verify", f"refs/remotes/origin/{branch}")
    if remote.returncode != 0:
        return True
    return local.stdout.strip() != remote.stdout.strip()


def pending(target: Optional[Path] = None) -> Optional[str]:
    """Why the mirror needs publishing, or None when it is up to date.

    The string is the reason, so the caller can journal it and a test can
    assert which of the two triggers fired - they fail in different ways and a
    single boolean would hide that.
    """
    target = target or TARGET
    if not configured(target):
        return None
    head = source_head()
    if not head:
        return None
    if not source_clean():
        return None
    if published_head(target) != head:
        return f"the source moved to {head[:7]}"
    if unpushed(target):
        return "the last publish was committed but never pushed"
    return None


def publish(target: Optional[Path] = None) -> Outcome:
    """Run the real publish script, and report what it said.

    A subprocess rather than an import on purpose. `publish.py` wipes and
    rebuilds a directory tree, shells out to git a dozen times and is written
    to be run as a program; running it as one means the automatic path and the
    hand path are byte-identical, so a change to either is a change to both,
    and nothing it does can corrupt the state of the process serving the site.
    """
    target = target or TARGET
    cmd = [
        str(config.APP_ROOT / "venv" / "bin" / "python"),
        str(config.APP_ROOT / "deploy" / "publish.py"),
        "--to", str(target),
        "--push",
    ]
    try:
        done = subprocess.run(
            cmd, cwd=str(config.APP_ROOT), capture_output=True, text=True,
            timeout=PUBLISH_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return Outcome(False, f"the publish did not finish within {PUBLISH_TIMEOUT_SEC}s")
    except OSError as exc:  # pragma: no cover - defensive
        return Outcome(False, f"the publish could not be started ({exc})")
    if done.returncode == 0:
        return Outcome(True, (done.stdout or "").strip())
    detail = (done.stderr or done.stdout or "").strip()
    # The leak scan's refusal is the one failure worth naming as itself: every
    # other failure is "try again later", and this one is "a person has to look
    # at what is in the tree".
    return Outcome(False, detail or f"publish.py exited {done.returncode}")


def _note(detail: str) -> None:
    """Put an outcome on the meta-project's journal, if there is one to put it on."""
    project = db.get_project_by_slug(config.META_PROJECT_SLUG)
    if project is None:
        return
    db.add_journal(int(project["id"]), "system", "status", detail)


def tick(target: Optional[Path] = None, now: Optional[float] = None) -> Optional[Outcome]:
    """Publish the mirror if it has fallen behind. Returns None when it had not.

    Called from the worker's ordinary tick, so it is defensive to a fault: it
    is bookkeeping running inside the loop that starts every run on the board,
    and no failure of it may ever stop that loop.
    """
    global _next_attempt, _backoff, _reported
    target = target or TARGET
    moment = time.monotonic() if now is None else now
    try:
        reason = pending(target)
        if reason is None:
            return None
        if moment < _next_attempt:
            return None
        outcome = publish(target)
    except Exception:  # noqa: BLE001 - see docstring
        log.exception("Mirror publish check failed")
        return None

    if outcome.ok:
        # Report the recovery as well as the failure. A run reading the journal
        # after a week of "could not push" needs to see where it stopped, and
        # an unclosed alarm is how a fixed problem keeps being re-investigated.
        if _reported is not None:
            _note(f"The public mirror is publishing again - {reason}, and it pushed.")
        _reported = None
        _backoff = RETRY_BACKOFF_SEC
        _next_attempt = 0.0
        log.info("Published the public mirror (%s)", reason)
        return outcome

    _next_attempt = moment + _backoff
    _backoff = min(_backoff * 2, RETRY_BACKOFF_MAX_SEC)
    if outcome.detail != _reported:
        _reported = outcome.detail
        _note(
            "The public mirror could not be published, so an install elsewhere "
            f"is not following this one. Reason: {outcome.detail.splitlines()[0] if outcome.detail else 'unknown'}. "
            "Publish by hand with `venv/bin/python deploy/publish.py --push`."
        )
    log.warning("Mirror publish failed: %s", outcome.detail)
    return outcome

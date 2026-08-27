"""Undoing what one run committed, without destroying what came after it.

This is the second half of RESEARCH.md §3's "per-task git worktrees for
parallel tasks and clean revert". The first half - worktrees - is answered in
the negative and the reasoning is recorded at the bottom of this docstring, so
that a future run reaches for it only if something here has actually changed.

The problem this solves is one Wes has stated as a value more than once: any
tool holding the only copy of his work needs a revision history he can roll
back with a button. Memory files already have that (`memory.revisions`). A
project workspace did not. An agent could commit a change he hates - or one
that breaks a live app - and the only way back was to ssh in and drive git by
hand from his phone, which is to say: not from his phone.

**A revert, never a reset.** `git reset --hard <before>` is the obvious
implementation and it is the wrong one. It throws away every commit made after
the run being undone, so undoing run #40 silently deletes runs #41 and #42 as
well; and because it moves a branch pointer, the work it deletes is reachable
only through the reflog, which is not a button. `git revert` instead *adds* a
commit that applies the inverse patch. Nothing is lost, later work is left
standing, the undo is itself in the history, and the undo can itself be undone.
That is the safe intermediate step rather than the irreversible one.

The cost is honest and is surfaced rather than hidden: a revert can conflict,
where a reset never can. If a later run edited the same lines, the inverse
patch will not apply and the whole thing is aborted and reported. Refusing to
guess is the right answer there - resolving that conflict needs to know what
both runs were trying to do, which is an agent's job, not a button's.

**The clean-tree precondition is what makes the failure path safe.** The undo
refuses to start unless the working tree is clean. That looks like politeness
and is not: it is what lets the conflict cleanup use `git reset --hard HEAD` as
a backstop when `git revert --abort` itself fails. With a verified-clean tree,
that reset provably discards nothing but the half-applied revert. Without the
precondition it could eat a live agent's uncommitted work, which would make
this module a bigger risk than the problem it solves.

**It holds the same lease a run holds.** The git work happens in this process,
so `worklock.wrap` (which wraps a spawn) is no use; `worklock.held` takes the
same BSD lock on the same directory instead. This is real mutual exclusion and
not a check: an agent cannot start editing the workspace between the decision
and the commit, because the kernel will not let it. A revert racing a live run
would hand that agent a tree that changed under it mid-edit.

**Why not worktrees, for the parallel half.** A per-task worktree would let two
agents run on one project at once. Rejected, for reasons that are about this
portal rather than about git:

* A workspace is not only a git checkout. It is `.portal/journal.md`,
  `attachments/`, `.claude/skills/`, `shots/`, and often a preview server bound
  to a port. A worktree shares none of that safely, so the second agent either
  works blind or races the first over the parts git does not track.
* The journal is the handoff between runs, and it is written at the *end* of a
  run. Two agents starting together both read the same pre-run state, so the
  second one cannot see what the first is doing - which is the exact
  duplicated-work failure `orphans.py` exists to prevent, reintroduced by
  design.
* Merging the two branches afterwards needs somebody to resolve conflicts, and
  the whole premise of the portal is that nobody is watching while it runs.

Parallelism across *projects* already exists and is where the wall-clock
actually is. Parallelism within one project buys little and costs a merge
problem with no one around to solve it.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app import config, orphans, worklock

log = logging.getLogger("portal.revert")

# Same bound as orphans: git can hang on a repo whose index was left locked by a
# process that died badly, which is one of the situations this runs in.
GIT_TIMEOUT_S = 30

# The revert commit is made by the portal, not by a person and not by an agent.
# Spelled out on the command line because a fresh workspace has no git identity
# configured at all (`user.email` unset makes `git commit` fail outright), and
# because a commit that claims to be from Wes when he only pressed a button is
# a small lie in a place people read to find out what happened.
# `.invalid` rather than a real host: it is the RFC 2606 reserved TLD, so the
# address is guaranteed never to route anywhere, and naming this install's own
# hostname here would put a personal string in a tree meant to be published
# (tests/test_leakscan.py fails on one, which is how this line got written).
COMMIT_NAME = "Project Portal"
COMMIT_EMAIL = "portal@project-portal.invalid"

# How many of the run's commits to name in the UI before summarizing the rest as
# a count. A run commits one or two things; a long tail means something unusual
# and the count says more than forty subjects would.
MAX_NAMED_COMMITS = 10


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str

    @property
    def short(self) -> str:
        return self.sha[:7]


@dataclass
class Landed:
    """What a run committed, and whether it can still be undone.

    Only built for a run that actually moved HEAD. `blocker` is None when the
    undo is available; otherwise it is a sentence saying why not, written to be
    shown to a person rather than logged.
    """

    repo: Path
    before: str
    after: str
    commits: list[Commit] = field(default_factory=list)
    reverted_at: Optional[str] = None
    blocker: Optional[str] = None
    is_source: bool = False

    @property
    def can_undo(self) -> bool:
        return self.blocker is None and self.reverted_at is None

    @property
    def named(self) -> list[Commit]:
        return self.commits[:MAX_NAMED_COMMITS]

    @property
    def unnamed(self) -> int:
        return max(0, len(self.commits) - MAX_NAMED_COMMITS)


@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str
    sha: Optional[str] = None


def _run(repo: Path, *args: str) -> tuple[int, str, str]:
    """A git call that reports how it failed.

    `orphans._git` collapses every failure to None, which is right for a
    read-only scan and wrong here: when a revert fails, the reason is the whole
    message shown to Wes.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git %s in %s failed: %s", args[0] if args else "?", repo, exc)
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _commits_between(repo: Path, before: str, after: str) -> list[Commit]:
    """The commits `before..after`, newest first, or [] if the range is unreadable."""
    rc, out, _ = _run(repo, "log", "--format=%H%x00%s", "--no-merges", f"{before}..{after}")
    if rc != 0:
        return []
    found: list[Commit] = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\0")
        if sha.strip():
            found.append(Commit(sha.strip(), subject.strip() or "(no subject)"))
    return found


def _is_dirty(repo: Path) -> bool:
    """Anything staged, unstaged or untracked, ignoring the portal's own
    bookkeeping - `.portal/report.json` is rewritten on every run, so counting it
    would make almost every workspace permanently un-revertable. Same pathspec
    exclusion, and for the same reason, as orphans.scan."""
    rc, out, _ = _run(
        repo, "status", "--porcelain", "--", ".", *orphans.EXCLUDE_PATHSPEC
    )
    if rc != 0:
        return True  # cannot tell -> treat as dirty; never revert into the unknown
    return bool(out.strip())


def _blocker(repo: Path, before: str, after: str, run_status: str) -> Optional[str]:
    """Why this run's commits cannot be undone right now, or None if they can.

    Evaluated both to render the button and again, under the lease, to act on
    it: the second call is the one that counts, because everything here can
    change between a page load and a tap.
    """
    if run_status == "running":
        return "This run is still going. Stop it first, then undo what it committed."
    rc, _, _ = _run(repo, "cat-file", "-e", f"{after}^{{commit}}")
    if rc != 0:
        return (
            "The commits this run made are no longer in this repo - its history "
            "has been rewritten or the workspace was replaced."
        )
    rc, _, _ = _run(repo, "merge-base", "--is-ancestor", after, "HEAD")
    if rc != 0:
        return (
            "This run's commits are not in the current branch's history any more, "
            "so there is nothing here to undo."
        )
    rc, out, _ = _run(repo, "rev-list", "--merges", f"{before}..{after}")
    if rc == 0 and out.strip():
        return (
            "This run's history contains a merge commit, which cannot be undone "
            "automatically - it needs a person to say which side to keep."
        )
    if _is_dirty(repo):
        return (
            "There is uncommitted work in this workspace. Commit or discard it "
            "first - undoing on top of it could throw it away."
        )
    return None


def landed(row, run_status: Optional[str] = None) -> Optional[Landed]:
    """What this run committed to its project's repo, or None if there is
    nothing to offer an undo for.

    None covers all the ordinary "no button here" cases: a run from before the
    heads were recorded, a run that committed nothing, a project whose workspace
    is not a git repo, and every one-off task (which has no project and so no
    workspace history worth a button).
    """
    slug = _row_get(row, "project_slug")
    if not slug:
        return None
    before = _row_get(row, "ws_head_before")
    after = _row_get(row, "ws_head_after")
    if not before or not after or before == after:
        return None
    repo = orphans.repo_for(slug)
    if repo is None:
        return None
    commits = _commits_between(repo, before, after)
    if not commits:
        return None
    status = run_status if run_status is not None else (_row_get(row, "status") or "")
    return Landed(
        repo=repo,
        before=before,
        after=after,
        commits=commits,
        reverted_at=_row_get(row, "reverted_at"),
        blocker=_blocker(repo, before, after, status),
        is_source=slug == config.META_PROJECT_SLUG,
    )


def _row_get(row, key: str):
    """sqlite3.Row has no .get, and not every query selects every column."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _abort(repo: Path) -> None:
    """Put the tree back after a failed revert.

    `--abort` is the right tool and is tried first. When the sequencer is in a
    state it will not unwind (it exits non-zero on a revert that never got as
    far as recording one), `--quit` drops the sequencer state and the hard reset
    puts the tree back. That reset is safe here *only* because `_blocker`
    established the tree was clean before any of this started - see the module
    docstring.
    """
    rc, _, _ = _run(repo, "revert", "--abort")
    if rc == 0:
        return
    _run(repo, "revert", "--quit")
    _run(repo, "reset", "--hard", "HEAD")


def _message(run_id: int, project_title: str, commits: list[Commit], who: str) -> str:
    lines = [
        f"Undo run #{run_id} on {project_title}",
        "",
        f"Reverts the {len(commits)} commit(s) that run committed. "
        f"Undone from the portal by {who}.",
        "",
    ]
    lines += [f"  {c.short} {c.subject}" for c in commits[:MAX_NAMED_COMMITS]]
    if len(commits) > MAX_NAMED_COMMITS:
        lines.append(f"  ... and {len(commits) - MAX_NAMED_COMMITS} more")
    return "\n".join(lines)


def undo(row, who: str = "Wes") -> Outcome:
    """Revert everything a run committed, as one new commit.

    Every precondition is re-checked here under the workspace lease rather than
    trusted from the page that drew the button, because all of them can change
    while somebody is looking at it.
    """
    plan = landed(row)
    if plan is None:
        return Outcome(False, "This run committed nothing that can be undone.")
    if plan.reverted_at:
        return Outcome(False, "This run has already been undone.")

    run_id = int(row["id"])
    title = _row_get(row, "project_title") or _row_get(row, "project_slug") or "this project"

    try:
        with worklock.held(plan.repo):
            blocker = _blocker(
                plan.repo, plan.before, plan.after, _row_get(row, "status") or ""
            )
            if blocker:
                return Outcome(False, blocker)

            rc, _, err = _run(
                plan.repo, "revert", "--no-commit", f"{plan.before}..{plan.after}"
            )
            if rc != 0:
                _abort(plan.repo)
                detail = (err or "").strip().splitlines()
                hint = detail[0] if detail else "git gave no reason"
                if "conflict" in (err or "").lower():
                    hint = (
                        "later commits changed the same lines, so the undo does "
                        "not apply cleanly"
                    )
                # No "could not undo this run" prefix: the card this renders in
                # is already headed with exactly that, and the page read as a
                # stutter.
                return Outcome(
                    False,
                    f"{hint[:1].upper()}{hint[1:]}. The workspace was left "
                    f"exactly as it was.",
                )

            rc, _, err = _run(
                plan.repo,
                "-c", f"user.name={COMMIT_NAME}",
                "-c", f"user.email={COMMIT_EMAIL}",
                "commit",
                "-m", _message(run_id, title, plan.commits, who),
            )
            if rc != 0:
                _abort(plan.repo)
                return Outcome(
                    False,
                    f"The undo applied but could not be committed: "
                    f"{(err or '').strip()[:200]}. The workspace was left as it was.",
                )
            _, out, _ = _run(plan.repo, "rev-parse", "HEAD")
            sha = out.strip() or None
    except worklock.Busy:
        return Outcome(
            False,
            "An agent is working in this workspace right now. Wait for it to "
            "finish, or stop it, and try again.",
        )
    except worklock.Unavailable as exc:
        return Outcome(False, f"Could not lock the workspace to undo safely: {exc}")

    log.info("Reverted run %s in %s -> %s", run_id, plan.repo, (sha or "?")[:7])
    return Outcome(
        True,
        f"Undid the {len(plan.commits)} commit(s) run #{run_id} made. "
        f"The undo is itself a commit, so it can be undone with git if it was wrong.",
        sha=sha,
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def journal_note(run_id: int, plan: Landed, who: str, sha: Optional[str]) -> str:
    """The journal line recording an undo, so the next agent reads about it
    before it wonders where its work went.

    This is the whole reason the undo writes to the journal at all: an agent
    that finds its previous run's feature missing and no explanation will build
    it again, which is the failure `orphans.py` exists to prevent, arriving by a
    different road.
    """
    subjects = "\n".join(f"- `{c.short}` {c.subject}" for c in plan.named)
    if plan.unnamed:
        subjects += f"\n- ... and {plan.unnamed} more"
    tail = f" The undo is `{sha[:7]}`." if sha else ""
    return (
        f"{who} undid what run #{run_id} committed, from the portal.{tail} "
        f"The work below was reverted - it was **not** deleted, so `git show` "
        f"still has it, but treat it as unwanted unless {who} says otherwise, "
        f"and do not simply rebuild it:\n\n{subjects}"
    )

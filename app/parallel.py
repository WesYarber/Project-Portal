"""A second agent on one project at once, each in its own git worktree.

Wes, 2026-08-28: *"I want to be able to run parallel agents for projects. I
think the way to implement it would be to show it as an option when adding a
note and a run is already going. Have it as an additional option next to queue
note. Call it 'parallel run'."*

The portal's oldest invariant is one agent per workspace, and it is not a
preference: it is the 2026-07-29 double-run incident, where two agents shared
one checkout and each committed over the other's half-finished edits. That
invariant is enforced by the kernel now (`app/worklock.py` takes a `flock` on
the workspace directory), so "just start a second run" does not quietly work
around it - the second agent is refused at the lock and the run dies on the
launch pad.

So a parallel run does not share the workspace. It gets a **git worktree**:

    data/projects/<slug>          the ordinary workspace, on its own branch
    data/parallel/<slug>-run<id>  a second checkout, on `portal/parallel-<id>`

A worktree is a separate directory with a separate working tree and a separate
checked-out branch, backed by the *same* object store. Three things follow, and
all three are why this shape was chosen over copying the folder:

* **The lease stops fighting.** Two directories, two `flock` targets, no
  contention - and the original invariant is untouched, because there is still
  exactly one agent in `data/projects/<slug>`.
* **Merging back is a plain `git merge`.** The commits are already in the
  workspace repo's object database the moment the parallel agent makes them;
  the merge only has to move a ref. Nothing is copied, nothing is re-pushed,
  and the parallel agent's history survives intact rather than arriving as one
  squashed blob.
* **Nothing is ever stranded.** If the merge cannot happen - the workspace is
  busy, or the two agents edited the same lines - the branch is simply left
  where it is and said out loud in the journal. `drain()` retries it on every
  later tick, so a conflict is a delay rather than a loss. Wes's rule: a sync
  that cannot reconcile two copies fails loudly and refuses to delete.

**The merge is never attempted while anything holds the workspace.** Not while
a run is in flight, not while the lease reads busy, and not while the tree is
dirty - a `git merge` into a checkout somebody is editing is the double-run
failure wearing a different hat. All three checks are "only a definite yes
counts": a git that will not answer leaves the branch pending, which is the
safe direction.

**One limitation is inherent and worth knowing.** A worktree isolates the
*workspace*, not whatever else a project's agent writes to. For the portal's
own meta-project the code lives at `config.APP_ROOT`, outside any workspace, so
two parallel runs on it edit one source tree with no lease between them. The
prompt section below says so in as many words; there is no mechanism that can
fix it from here.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app import config, worklock

log = logging.getLogger(__name__)

# How many agents may be inside one project at once, counting the ordinary run.
# Two is the number Wes asked for ("a run is already going" plus one); the
# setting exists because the ceiling that is right for this box is not the one
# that is right for somebody else's.
DEFAULT_MAX_AGENTS = 2
MAX_AGENTS_SETTING = "max_agents_per_project"

BRANCH_PREFIX = "portal/parallel-"

_GIT_TIMEOUT = 60


def root() -> Path:
    """Where parallel checkouts live. Beside `data/projects/`, never inside it:
    the projects directory is scanned as "one folder per project" in half a
    dozen places, and a worktree in there would read as a project of its own."""
    return config.DATA_DIR / "parallel"


def branch_for(run_id: int) -> str:
    return f"{BRANCH_PREFIX}{int(run_id)}"


def worktree_for(slug: str, run_id: int) -> Path:
    return root() / f"{slug}-run{int(run_id)}"


def workspace_for(slug: str) -> Path:
    return config.PROJECTS_DIR / slug


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    """Run git in `repo`. Never raises: every caller here treats a git that
    would not answer as "leave it pending", not as a reason to lose work."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git %s in %s failed: %s", " ".join(args), repo, exc)
        return 1, "", str(exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _has_commits(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--verify", "HEAD")[0] == 0


def _is_dirty(repo: Path) -> Optional[bool]:
    """True/False, or None when git would not say. None is not False: an
    unreadable tree must not be merged into."""
    code, out, _ = _git(repo, "status", "--porcelain")
    if code != 0:
        return None
    return bool(out.strip())


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")[1]


def _seed_initial_commit(repo: Path) -> bool:
    """A repo with no commits has no HEAD to branch from, and `git worktree
    add` refuses outright. A brand-new workspace is exactly that, so give it an
    empty root commit rather than refusing the feature on a fresh project.
    Empty on purpose: committing whatever happens to be lying around would put
    an in-flight agent's half-written files into history."""
    code, _, err = _git(repo, "commit", "--allow-empty", "-m",
                        "Initial commit (opening a parallel run)")
    if code != 0:
        log.warning("Could not seed an initial commit in %s: %s", repo, err)
    return code == 0


# --------------------------------------------------------------------------
# Opening one
# --------------------------------------------------------------------------

def open_worktree(slug: str, run_id: int) -> Optional[Path]:
    """A fresh checkout for run `run_id`, or None if git would not make one.

    None means the caller must not start a parallel run: falling back to the
    shared workspace would be the double-run this module exists to avoid.
    """
    workspace = workspace_for(slug)
    if not (workspace / ".git").exists():
        log.warning("No git repo at %s; cannot open a parallel worktree", workspace)
        return None
    if not _has_commits(workspace) and not _seed_initial_commit(workspace):
        return None

    target = worktree_for(slug, run_id)
    if target.exists():
        # A leftover from a run that died between creating this and recording
        # it. Reusing it would hand the new agent the dead one's edits.
        _remove_worktree(workspace, target)
    target.parent.mkdir(parents=True, exist_ok=True)

    code, _, err = _git(workspace, "worktree", "add", "--quiet",
                        "-b", branch_for(run_id), str(target), "HEAD")
    if code != 0:
        log.warning("git worktree add failed for %s run %s: %s", slug, run_id, err)
        return None
    _carry_uncommitted_attachments(workspace, target)
    return target


def _carry_uncommitted_attachments(workspace: Path, target: Path) -> None:
    """Copy across any attachment the workspace has and the worktree does not.

    The worktree is cut from HEAD, and the file Wes just dropped onto the note
    that *started* this run is by definition not committed yet - so without
    this the prompt names a screenshot that is not on disk beside the agent.
    Copy rather than symlink: the directory is tracked in git, so a symlink
    would collide with the checkout and would also let a parallel agent's
    `git clean` delete the original.
    """
    src = workspace / "attachments"
    if not src.is_dir():
        return
    dest = target / "attachments"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.is_file() and not (dest / item.name).exists():
                shutil.copy2(item, dest / item.name)
    except OSError as exc:  # pragma: no cover - defensive
        log.warning("Could not carry attachments into %s: %s", target, exc)


def _remove_worktree(workspace: Path, target: Path) -> None:
    """Drop a checkout, keeping its branch. The branch is the work; the
    directory is only where it was typed."""
    _git(workspace, "worktree", "remove", "--force", str(target))
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    _git(workspace, "worktree", "prune")


def close_worktree(slug: str, run_id: int) -> None:
    _remove_worktree(workspace_for(slug), worktree_for(slug, run_id))


def _drop_branch(workspace: Path, slug: str, run_id: int, branch: str) -> None:
    """Remove a parallel run's checkout and its branch together.

    Always in that order. A branch checked out in a worktree cannot be deleted
    - git refuses even with `-D` - so deleting first silently leaves the branch
    behind, and `pending()` then hands it back on every tick forever.
    """
    _remove_worktree(workspace, worktree_for(slug, run_id))
    _git(workspace, "branch", "-D", branch)


# --------------------------------------------------------------------------
# Merging back
# --------------------------------------------------------------------------

@dataclass
class Merged:
    """What happened to one parallel branch.

    `status` is one of:

    * `merged`   - its commits are in the workspace now.
    * `empty`    - it committed nothing, so the branch was simply dropped.
    * `busy`     - the workspace is occupied; the branch is untouched and will
                   be retried. Not a failure.
    * `conflict` - the two agents edited the same lines. The merge was aborted,
                   the branch is untouched, and a person has to look.
    * `error`    - git would not say. Same treatment as a conflict: keep it.
    """

    status: str
    branch: str
    run_id: int
    commits: int = 0
    detail: str = ""

    @property
    def kept(self) -> bool:
        """Is the branch still sitting there waiting for somebody?"""
        return self.status in {"busy", "conflict", "error"}


def workspace_is_free(slug: str, running: bool) -> bool:
    """Can something safely `git merge` into this workspace this second?

    `running` is passed in rather than read here so this module never imports
    the worker (which imports this one). Every one of the three checks below
    fails towards "no": a lease that reads busy, a tree that is dirty, and a
    tree git would not describe all mean leave it alone.
    """
    if running:
        return False
    if worklock.is_busy(workspace_for(slug)) is True:
        return False
    return _is_dirty(workspace_for(slug)) is False


_BRANCH_RE = re.compile(rf"^{re.escape(BRANCH_PREFIX)}(\d+)$")
_DIR_RE = re.compile(r"^(?P<slug>.+)-run(?P<run>\d+)$")


def projects_with_branches() -> list[str]:
    """Slugs that may still have a parallel branch waiting to be merged.

    Read off the *directory* listing rather than by asking git about every
    project on the board: this is called once a minute from the worker loop,
    and one `git branch` per project would be dozens of subprocesses a tick to
    answer "no" almost every time. A worktree is removed as soon as its branch
    lands, so a directory here means a branch that has not - and a directory
    somebody deleted by hand only costs a delay, since the next run on that
    project drains it from its own `finally`.
    """
    base = root()
    if not base.is_dir():
        return []
    slugs: list[str] = []
    try:
        for item in base.iterdir():
            m = _DIR_RE.match(item.name)
            if item.is_dir() and m and m.group("slug") not in slugs:
                slugs.append(m.group("slug"))
    except OSError as exc:  # pragma: no cover - defensive
        log.warning("Could not list %s: %s", base, exc)
    return slugs


def pending(slug: str) -> list[int]:
    """Run ids whose parallel branch still exists, oldest first."""
    code, out, _ = _git(workspace_for(slug), "branch", "--list",
                        f"{BRANCH_PREFIX}*", "--format=%(refname:short)")
    if code != 0:
        return []
    ids: list[int] = []
    for line in out.splitlines():
        m = _BRANCH_RE.match(line.strip())
        if m:
            ids.append(int(m.group(1)))
    return sorted(ids)


def merge_back(slug: str, run_id: int, running: bool = False) -> Merged:
    """Fold one parallel branch into the workspace's own branch."""
    workspace = workspace_for(slug)
    branch = branch_for(run_id)
    if _git(workspace, "rev-parse", "--verify", branch)[0] != 0:
        return Merged("empty", branch, run_id, detail="the branch is already gone")

    # Ahead-count first: a branch with nothing on it is not worth waiting for a
    # free workspace, and dropping it here is what stops `pending()` growing a
    # tail of empty branches from runs that only read.
    code, out, _ = _git(workspace, "rev-list", "--count", f"HEAD..{branch}")
    ahead = int(out) if code == 0 and out.isdigit() else -1
    if ahead == 0:
        _drop_branch(workspace, slug, run_id, branch)
        return Merged("empty", branch, run_id, detail="the parallel run committed nothing")

    if not workspace_is_free(slug, running):
        return Merged("busy", branch, run_id, commits=max(ahead, 0),
                      detail="the workspace is occupied; the branch is kept and retried")

    # The checkout goes before the merge, not after it: git refuses to delete a
    # branch that is checked out in another worktree, so leaving it until the
    # end left every merged branch behind and `pending()` never emptied. By
    # this point the run has finished, so the worktree holds nothing but the
    # commits that are about to be merged.
    _remove_worktree(workspace, worktree_for(slug, run_id))

    onto = _current_branch(workspace)
    code, out, err = _git(
        workspace, "merge", "--no-ff", branch,
        "-m", f"Merge parallel run {run_id} into {onto}",
    )
    if code == 0:
        _git(workspace, "branch", "-d", branch)
        return Merged("merged", branch, run_id, commits=max(ahead, 0))

    # Abort before anything else: a half-applied merge left in the index is a
    # workspace the next ordinary run walks into and cannot commit from.
    _git(workspace, "merge", "--abort")
    # `git merge` reports a conflict on STDOUT ("CONFLICT (content): Merge
    # conflict in x.py") and keeps stderr for the summary line, so reading only
    # stderr filed every real conflict as an unexplained "error" - and the
    # journal line for one of those does not tell Wes to go resolve anything.
    said = f"{out}\n{err}".lower()
    return Merged(
        "conflict" if "conflict" in said else "error", branch, run_id,
        commits=max(ahead, 0),
        detail=(err or out)[:400] or "git refused the merge",
    )


def drain(
    slug: str, running: bool = False, run_ids: Optional[list[int]] = None
) -> list[Merged]:
    """Try every pending branch. Called when a run finishes and on each tick,
    so a branch parked as `busy` lands by itself once the workspace clears.

    `run_ids` narrows it to specific branches - the caller's job, because only
    the caller knows which parallel runs have actually finished, and merging a
    live agent's half-written history is exactly as wrong as merging into a
    tree somebody is editing.
    """
    out: list[Merged] = []
    for run_id in (pending(slug) if run_ids is None else sorted(run_ids)):
        result = merge_back(slug, run_id, running=running)
        out.append(result)
        if result.status == "merged":
            close_worktree(slug, run_id)
        elif result.status == "empty":
            close_worktree(slug, run_id)
        elif result.status == "busy":
            # The workspace will not have freed up between two iterations of
            # this loop, so there is nothing to gain from trying the rest.
            break
    return out


# --------------------------------------------------------------------------
# What the journal says, and what the agent is told
# --------------------------------------------------------------------------

def journal_note(slug: str, result: Merged) -> Optional[str]:
    """The line for the project journal, or None when there is nothing worth
    saying (an empty branch is bookkeeping, not news)."""
    if result.status == "merged":
        return (
            f"Parallel run {result.run_id} merged back into the workspace "
            f"({result.commits} commit(s) from `{result.branch}`)."
        )
    if result.status == "busy":
        return (
            f"Parallel run {result.run_id} finished with {result.commits} commit(s), "
            f"but the workspace is still occupied. Its work is safe on branch "
            f"`{result.branch}` and will be merged as soon as the workspace frees up."
        )
    if result.status == "conflict":
        return (
            f"**Parallel run {result.run_id} could not be merged automatically.** Its "
            f"{result.commits} commit(s) conflict with what the workspace has since "
            f"committed. Nothing is lost - the work is on branch `{result.branch}` - but "
            f"a person or a later run has to resolve it:\n\n"
            f"```\ngit merge {result.branch}\n```\n\n"
            f"git said: `{result.detail}`"
        )
    if result.status == "error":
        return (
            f"Parallel run {result.run_id}'s branch `{result.branch}` was left in place: "
            f"git would not merge it (`{result.detail}`). Its commits are intact."
        )
    return None


def prompt_section(slug: str, run_id: int, worktree: Path, others: int) -> str:
    """What a parallel agent must know before it touches anything.

    Deliberately blunt about the three things it cannot see for itself: that
    somebody else is live in this project right now, that its own cwd is not
    the workspace it will read about everywhere else, and that on the portal's
    own project the isolation stops at the workspace edge.
    """
    lines = [
        "## You are a PARALLEL run",
        "",
        f"Another agent is working on this project right now ({others} other run(s) "
        "in flight). You were started deliberately alongside it, so the note above "
        "is yours to act on and its work is not.",
        "",
        f"You are NOT in the usual workspace. Your working directory is a git "
        f"worktree at `{worktree}`, checked out on branch `{branch_for(run_id)}` from "
        f"the workspace's HEAD. Everything you commit lands on that branch, and the "
        f"portal merges it back into `{workspace_for(slug)}` as soon as that workspace "
        f"is free.",
        "",
        "What that means for how you work:",
        "",
        "- **Commit.** Work left uncommitted in a worktree is thrown away when the "
        "worktree is removed. Nothing else carries it back.",
        "- **Stay in your own lane.** Pick work the other run is unlikely to be "
        "touching - a different file, a different feature, the note you were given. "
        "Two agents editing the same lines becomes a merge conflict a person has to "
        "resolve by hand.",
        "- **Do not try to merge, rebase or push anything yourself**, and do not "
        "touch the ordinary workspace directory. The portal does the merge.",
    ]
    if slug == config.META_PROJECT_SLUG:
        lines += [
            "",
            "**One warning specific to this project.** Its real code is not in the "
            "workspace - it is at the portal's source root - and a worktree does not "
            "isolate that. The other agent is editing the same source tree you are, "
            "with nothing between you. Keep your edits small and to files the note "
            "names, and re-read a file immediately before you edit it.",
        ]
    return "\n".join(lines)

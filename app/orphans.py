"""Finds work a dead run left behind, uncommitted, in a project's git repo.

Six-plus times now a run has died - session limit, timeout, the OOM killer -
with a complete, passing feature sitting unstaged in the working tree, and
nothing in the portal noticed. The next run opened on a project that looked
untouched and started building the same thing again.

This module answers one question, read-only and cheaply: *is there uncommitted
work in this project's repo right now, and what is it?* The worker journals the
answer when a run fails, and `build_prompt` puts it in front of the next agent
before it decides anything.

Everything here is deliberately non-destructive. It never stages, commits,
stashes or cleans - deciding what to do with a half-finished feature is the
agent's job, and a module that could throw work away while diagnosing a crash
would be worse than the problem it exists to solve.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app import config

log = logging.getLogger("portal.orphans")

# A crashed run can leave a lot behind (a `npm install` that half-finished, a
# build directory). Naming every path would bury the prompt, so the listing is
# capped and the remainder is counted.
MAX_NAMED_FILES = 12

# git can hang on a repo whose index is locked by a process that died badly -
# exactly the situation this runs in. A short timeout is better than a worker
# tick that never returns.
GIT_TIMEOUT_S = 15

# The portal writes .portal/report.json into every workspace on every run, and
# deletes it again at the start of the next one. In repos where it was committed
# once it is therefore *always* dirty: without this, 13 of 17 live projects
# carried a "a previous run left work uncommitted" warning on every prompt.
# A warning that fires on almost every run is not read, which would cost exactly
# the thing this module exists to buy. It is portal bookkeeping, not the agent's
# work, so it is excluded from every git call rather than filtered afterwards -
# that way the file list and the line counts agree with each other.
EXCLUDE_PATHSPEC = (":(exclude,glob).portal/**", ":(exclude).portal")


@dataclass
class OrphanedWork:
    """Uncommitted state in a repo. Only built when something is actually dirty."""

    repo: Path
    files: list[str] = field(default_factory=list)
    total_files: int = 0
    insertions: int = 0
    deletions: int = 0
    branch: str = ""
    last_commit: str = ""

    @property
    def named(self) -> list[str]:
        return self.files[:MAX_NAMED_FILES]

    @property
    def unnamed(self) -> int:
        return max(0, self.total_files - len(self.named))

    def describe(self) -> str:
        """One markdown paragraph naming what is sitting there, for the journal
        and for the next run's prompt."""
        bits = [f"**{self.total_files} uncommitted file{'s' if self.total_files != 1 else ''}**"]
        if self.insertions or self.deletions:
            bits.append(f"+{self.insertions}/-{self.deletions} in tracked files")
        if self.branch:
            bits.append(f"on `{self.branch}`")
        head = ", ".join(bits)

        listed = "\n".join(f"- `{name}`" for name in self.named)
        if self.unnamed:
            listed += f"\n- ...and {self.unnamed} more"
        tail = f"\n\nLast commit: {self.last_commit}" if self.last_commit else ""
        return f"{head} in `{self.repo}`:\n\n{listed}{tail}"


def repo_for(slug: str) -> Optional[Path]:
    """Which git repo holds this project's code.

    For every ordinary project that is its workspace. The portal's own project
    is the exception: its workspace is a near-empty folder kept for consistency
    with the per-project model, and its actual source lives at APP_ROOT - which
    is precisely the repo that has been left dirty by a dead run most often.
    """
    if slug == config.META_PROJECT_SLUG:
        candidate = config.APP_ROOT
    else:
        candidate = config.PROJECTS_DIR / slug
    return candidate if (candidate / ".git").exists() else None


def scan(repo: Optional[Path]) -> Optional[OrphanedWork]:
    """Return what is uncommitted in `repo`, or None if it is clean, missing,
    not a repo, or unreadable.

    None means "nothing to say", never "definitely clean" - a git that fails is
    reported the same as a git that finds nothing, because the alternative is a
    scary journal entry every time something unrelated breaks. The cost of
    missing one dirty tree is a rebuilt feature; the cost of crying wolf on
    every run is that the warning stops being read.
    """
    if repo is None or not (repo / ".git").exists():
        return None

    porcelain = _git(
        repo, "status", "--porcelain=v1", "--untracked-files=all", "--", *EXCLUDE_PATHSPEC
    )
    if porcelain is None:
        return None
    files = _parse_porcelain(porcelain)
    if not files:
        return None

    work = OrphanedWork(repo=repo, files=files, total_files=len(files))
    work.insertions, work.deletions = _diffstat(repo)
    # `branch --show-current`, not `rev-parse HEAD`: a repo with no commits yet
    # has no HEAD to resolve, and a freshly-`git init`ed workspace is a normal
    # state here rather than an error worth logging about.
    work.branch = (_git(repo, "branch", "--show-current") or "").strip()
    work.last_commit = (_git(repo, "log", "-1", "--format=%h %s", quiet=True) or "").strip()
    return work


def _parse_porcelain(out: str) -> list[str]:
    """Pull the paths out of `git status --porcelain=v1`.

    Two shapes matter. A rename is `R  old -> new`, and the interesting path is
    the new one. A path with a space, a quote or a non-ASCII byte comes back
    quoted and C-escaped, and is left exactly as git wrote it - unescaping it
    would produce a name that no longer round-trips into a git command.
    """
    names: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip()
        if path:
            names.append(path)
    return sorted(set(names))


def _diffstat(repo: Path) -> tuple[int, int]:
    """Lines added/removed across staged and unstaged changes.

    Untracked files contribute nothing here, by design: `git diff` does not see
    them, and the file count already says they exist. A number that only covers
    tracked edits is honest; one that silently counted a node_modules tree would
    not be.
    """
    ins = dels = 0
    for args in (("diff", "--numstat"), ("diff", "--numstat", "--cached")):
        # --cached against a repo with no commits errors rather than diffing
        # against the empty tree, which is expected, not noteworthy.
        out = _git(repo, *args, "--", *EXCLUDE_PATHSPEC, quiet=True)
        if not out:
            continue
        for line in out.splitlines():
            cols = line.split("\t")
            if len(cols) < 3:
                continue
            # "-" in place of a count means a binary file; it has no line delta.
            if cols[0].isdigit():
                ins += int(cols[0])
            if cols[1].isdigit():
                dels += int(cols[1])
    return ins, dels


def _git(repo: Path, *args: str, quiet: bool = False) -> Optional[str]:
    """`quiet` suppresses the non-zero-exit warning for calls that fail in
    states that are perfectly normal here - chiefly a repo with no commits yet,
    where `git log` and `git diff --cached` both exit 128."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git %s in %s failed: %s", args[0], repo, exc)
        return None
    if out.returncode != 0:
        if not quiet:
            log.warning(
                "git %s in %s exited %s: %s", args[0], repo, out.returncode, out.stderr[:200]
            )
        return None
    return out.stdout


def journal_note(
    slug: str, task: str, how: str, repo: Optional[Path] = None
) -> Optional[str]:
    """The markdown the worker adds when a run ends badly, or None if the repo
    is clean. `how` is why the run ended - "errored", "timed out", "canceled".

    `repo` overrides which tree is scanned, and exists for parallel runs: those
    work in a git worktree of their own, so scanning the project's ordinary
    workspace would describe the *other* agent's live edits as this run's
    orphaned work and send the next run to tidy up something still being
    written. Same trap as the lock-conflict path in worker.run_project_task.
    """
    work = scan(repo if repo is not None else repo_for(slug))
    if work is None:
        return None
    return (
        f"That run ({task}) {how} with work still uncommitted. "
        f"**Nothing has been lost** - it is sitting in the working tree, and the "
        f"next run is told to read it before building anything.\n\n"
        f"{work.describe()}"
    )


def prompt_section(slug: str) -> str:
    """The block `build_prompt` puts in front of the next agent. Empty string
    when the repo is clean, so an ordinary run's prompt is unchanged.

    This is computed live rather than read from a flag set on the failure path,
    because the runs that hurt most are the ones that never reached that path -
    a run killed outright leaves no trace in the database, and a flag would say
    the tree was clean while a finished feature sat in it.
    """
    work = scan(repo_for(slug))
    if work is None:
        return ""
    return (
        "## Uncommitted work already in the repo\n"
        "A previous run left changes in the working tree without committing "
        "them. This is usually a run that died partway through, and the work is "
        "often complete and passing.\n\n"
        "**Read it before you build anything.** `git diff`, `git status`, and "
        "run the tests. If it is finished, finish it and commit it rather than "
        "starting the same feature again. If it is half-done, continue it. Only "
        "discard it if you have read it and it is genuinely wrong.\n\n"
        f"{work.describe()}\n"
    )

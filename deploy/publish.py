#!/usr/bin/env python
"""Build the public repository from this tree, with a fresh history.

    venv/bin/python deploy/publish.py [--to ../project-portal-public] [--push]

Why a fresh history rather than pushing this repo. Removing a secret from the
working tree does not remove it from the history, and `git push` publishes the
history: this repo's own log still contains a machine's sudo password in a file
that was cleaned up months ago. Rewriting with `git filter-repo` would be more
work and would leave the secret in every clone anyone had already taken - here
nobody has taken one, so starting at "initial commit" is both simpler and
strictly safer.

What that costs is the log, which is the honest trade: the private repo keeps
its full history, and the public one starts today.

The safety property this script is built around is that **it never guesses what
is safe**. It copies only what git already tracks (so anything gitignored -
`data/`, `secrets/`, `portal.toml` - cannot be picked up by accident), removes
the handful of paths that are tracked but private, and then runs the leak scan
over what it is about to commit. A leak is a hard stop, not a warning.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, leakscan, mirror  # noqa: E402

ROOT = config.APP_ROOT
DEFAULT_TARGET = ROOT.parent / "project-portal-public"

INITIAL_COMMIT_MESSAGE = """Project Portal

A self-hosted dashboard that runs headless Claude Code agents against a
workspace per project: they plan, ask when they need a decision, build once
approved, and report back over the web, Telegram or push.

This repository starts here on purpose. It is developed in a private repo whose
history contains credentials from the author's own machines, and history cannot
be un-published once pushed, so the public tree begins at a single commit.
"""


def stamped(message: str) -> str:
    """Add the `Source-commit:` trailer naming what this tree was copied from.

    The two repositories share no commit id - the public history starts fresh
    on purpose - so without this line there is nothing at all to compare, and
    "has the mirror fallen behind the source?" is a question only a person
    holding both checkouts can answer. `app/mirror.py` reads it back on every
    worker tick to decide whether to publish; a reader on GitHub gets the same
    answer for free.

    It is honest only because the automatic path refuses to publish a dirty
    tree (see `mirror.source_clean`). A publish run by hand over uncommitted
    edits still stamps the commit it is closest to, which is the best available
    answer and the reason this says "Source-commit" and not "this is commit".
    """
    head = mirror.source_head()
    return f"{message.rstrip()}\n\n{mirror.TRAILER} {head}\n" if head else message


def tracked_files() -> list[str]:
    """What git tracks, which is the only thing this script will publish.

    Copying a directory listing instead would mean re-deciding what is secret
    every time something new lands in the tree; `.gitignore` has already made
    that decision, and `data/`, `secrets/` and `portal.toml` are in it.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [p for p in out.stdout.split("\0") if p]


def publishable() -> tuple[list[str], list[str]]:
    keep, held = [], []
    for rel in tracked_files():
        (held if rel in leakscan.PRIVATE_PATHS else keep).append(rel)
    return keep, held


def stage(target: Path, files: list[str]) -> None:
    """Copy into a clean directory, preserving the .git of an existing repo.

    Wiping and re-initializing every time would give the public repo a fresh
    history on every publish, which defeats the point of the second run onwards.
    """
    keep_git = target / ".git"
    if target.exists():
        for child in sorted(target.iterdir()):
            if child == keep_git:
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    target.mkdir(parents=True, exist_ok=True)
    for rel in files:
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)


def check(target: Path) -> list[leakscan.Leak]:
    """Scan what is about to be committed, not what it was copied from.

    Scanning the source tree would be the same check one step too early - this
    one also catches a private path that failed to be excluded.
    """
    return leakscan.scan(root=target)


def git(target: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=target, capture_output=True, text=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--to", type=Path, default=DEFAULT_TARGET)
    ap.add_argument("--message", default="", help="commit message (default: a summary of what changed)")
    ap.add_argument("--push", action="store_true", help="push to origin after committing")
    ap.add_argument("--dry-run", action="store_true", help="stage and scan, commit nothing")
    args = ap.parse_args()

    target = args.to.expanduser().resolve()
    if target == ROOT:
        print("Refusing to publish over the source tree.", file=sys.stderr)
        return 2

    keep, held = publishable()
    print(f"{len(keep)} tracked files to publish, {len(held)} held back:")
    for rel in held:
        print(f"  private: {rel}")

    stage(target, keep)

    leaks = check(target)
    if leaks:
        print(f"\nREFUSING TO PUBLISH - {len(leaks)} personal string(s) in the staged tree:", file=sys.stderr)
        for leak in leaks[:40]:
            print(f"  {leak}", file=sys.stderr)
        # The staged copy is left in place deliberately: the finding is easier
        # to act on when the offending file is still sitting there to look at.
        return 1
    print(f"\nLeak scan clean ({len(leakscan.needles(extra=leakscan.extra_patterns()))} needles).")

    if args.dry_run:
        print(f"Dry run: staged at {target}, nothing committed.")
        return 0

    first = not (target / ".git").exists()
    if first:
        git(target, "init", "-q", "-b", "main")
        # The identity is set per-repository here, not globally, so a fresh
        # repo can commit at all. Without it git refuses with an auto-detected
        # `user@host.(none)` address, which is a confusing way for a publish to
        # fail on its very first run.
        for key in ("user.name", "user.email"):
            value = subprocess.run(
                ["git", "config", key], cwd=ROOT, capture_output=True, text=True
            ).stdout.strip()
            if value:
                git(target, "config", key, value)
    git(target, "add", "-A")
    status = git(target, "status", "--porcelain")
    if not status.stdout.strip():
        print("Nothing changed since the last publish.")
        # Committing and pushing are separate questions, and answering the
        # second with the first is how this repo became unpushable: six commits
        # sat here, none of them on any remote, and `--push` returned 0 here
        # every time without ever reaching the push. "Up to date with the
        # source tree" does not imply "up to date with the remote" - the very
        # first push after a remote is added is exactly the case where nothing
        # has changed locally.
        if not args.push:
            return 0
    else:
        message = args.message or (INITIAL_COMMIT_MESSAGE if first else "Update from upstream")
        commit = git(target, "commit", "-q", "-m", stamped(message))
        if commit.returncode != 0:
            print(commit.stderr, file=sys.stderr)
            return 1
        print(f"Committed to {target} ({'initial commit' if first else 'update'}).")

    if args.push:
        pushed = git(target, "push", "origin", "HEAD")
        if pushed.returncode != 0:
            print(pushed.stderr.strip(), file=sys.stderr)
            print("\nAdd a remote first:  git -C %s remote add origin <url>" % target, file=sys.stderr)
            return 1
        print("Pushed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

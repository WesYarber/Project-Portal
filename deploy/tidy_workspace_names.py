#!/usr/bin/env python
"""Apply the short-folder-name rename to every project still carrying raw idea text.

Wes, 2026-07-22 01:25: "Please update the existing project folders that are
waiting to be renamed."

The offer already exists per project on the page and on the settings page; this
is the same operation done in one pass over the ones that are pending, because
five clicks to fix five folders he never chose the names of is five clicks too
many.

It goes through `app.main`'s own `_check_rename` and `_move_workspace` rather
than reimplementing the move, so it cannot end up with weaker guards than the
button: the portal's own project is refused, a project with a live agent in it
is refused (its cwd would become a deleted inode mid-run), and a name already
taken is refused.

    venv/bin/python deploy/tidy_workspace_names.py --dry-run
    venv/bin/python deploy/tidy_workspace_names.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from app import db, main  # noqa: E402


def main_(dry_run: bool) -> int:
    pending = db.projects_with_suggested_slugs()
    if not pending:
        print("Nothing to rename - every workspace folder is already a folder name.")
        return 0

    failures = 0
    for project, target in pending:
        slug = project["slug"]
        print(f"{slug}\n  -> {target}   ({project['title']})")
        if dry_run:
            continue
        try:
            main._check_rename(project, target)
            main._move_workspace(slug, target)
        except HTTPException as exc:
            # Skipped, not fatal: a busy project is worth coming back to rather
            # than a reason to leave the other four as they are.
            print(f"  SKIPPED: {exc.detail}")
            failures += 1
            continue
        db.update_project(project["id"], slug=target, slug_locked=1)
        db.add_journal(
            project["id"],
            "user",
            "status",
            f"Renamed workspace `{slug}` -> `{target}` - folder names are now short "
            "directory names rather than copies of the project title.",
        )
        print("  renamed")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="say what would move")
    args = parser.parse_args()
    raise SystemExit(main_(args.dry_run))

"""The workspace section as a file tree, one directory at a time.

The workspace list used to be every file in the project, flattened to a
relative path, sorted, and rendered in full on every project page. For the
portal's own workspace (two files) that was fine. For a real project it was
not: SimpleClickTrack has 3366 files, almost all of them inside
`node_modules`, and all 3366 were walked, sorted, and written into the HTML of
a page whose author only ever wanted to see `PLAN.md`.

So this module answers one narrow question - *what is directly inside this one
directory* - and the page asks it again per folder as folders are opened.

Two consequences worth being explicit about, because they are the reason this
is a module rather than four lines in main.py:

  * **Nothing recurses.** `children()` does a single `scandir`, so opening a
    project page costs one directory read regardless of how much is below it.
    A folder nobody opens is never read at all.
  * **A directory is not sized.** There is deliberately no per-folder file
    count in the tree: producing one means walking everything under it, which
    is exactly the cost this module exists to avoid. The count in the section
    header is a separate, capped walk (`count_files`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Never shown in the tree, at any depth. `.git` is the project's own history -
# thousands of hash-named objects that mean nothing rendered as a list, and
# `_list_workspace_files` has always hidden it.
HIDDEN_DIRS = {".git"}

# The workspace-root `attachments/` directory has its own section on the page,
# with real previews. Hidden only at the root, so a project that legitimately
# keeps an `attachments/` folder deeper in its own source still shows it.
ROOT_ONLY_HIDDEN = {"attachments"}

# Hidden by exact relative path rather than by name. The journal mirror is the
# portal's own file, written into the workspace before each run so an agent can
# read a journal entry its prompt only summarized (app/journalfile.py). Its
# content is already on this very page, rendered properly, in the journal
# timeline - and at its natural size it is over `fileview.MAX_TEXT_BYTES`, so
# the row would open on "too large to display" anyway. The rest of `.portal/`
# stays visible: report.json and serve.json are small and worth a look.
HIDDEN_PATHS = {".portal/journal.md"}

# `count_files` stops here rather than walking a pathological tree to the end.
# The header then reads "5000+ files", which tells the reader the same thing
# the true number would ("more than you want to scroll") for a bounded cost.
COUNT_CAP = 5000


@dataclass(frozen=True)
class Entry:
    """One row in the tree: a directory to open, or a file to view."""

    name: str
    path: str  # relative to the workspace root, POSIX-separated
    is_dir: bool


def _visible(name: str, *, at_root: bool, is_dir: bool) -> bool:
    if not is_dir:
        return True
    if name in HIDDEN_DIRS:
        return False
    return not (at_root and name in ROOT_ONLY_HIDDEN)


def children(workspace: Path, rel: str = "") -> list[Entry]:
    """The entries directly inside `workspace/rel`, directories first.

    Returns [] rather than raising when the directory is missing or unreadable:
    a workspace that has not been created yet, and one whose permissions are
    wrong, both mean "nothing to show here" to the page, and neither is worth
    turning a project page into a 500.

    Symlinks to directories are listed as *files*, not as openable folders. A
    symlink out of the workspace would otherwise be a tree the reader could
    walk straight out of, and the file routes resolve() before their
    containment check, so such a link simply 400s when clicked - which is the
    correct answer, arrived at without this module having to police it.
    """
    base = workspace / rel if rel else workspace
    try:
        raw = list(os.scandir(base))
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return []

    at_root = not rel
    entries: list[Entry] = []
    for item in raw:
        try:
            is_dir = item.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        if not _visible(item.name, at_root=at_root, is_dir=is_dir):
            continue
        path = f"{rel}/{item.name}" if rel else item.name
        if path in HIDDEN_PATHS:
            continue
        entries.append(Entry(name=item.name, path=path, is_dir=is_dir))

    # Directories first, then files, each case-insensitively alphabetical - the
    # order a file browser uses, and the one that puts `src/` above `bun.lock`.
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def count_files(workspace: Path, cap: int = COUNT_CAP) -> tuple[int, bool]:
    """Total files in the workspace, and whether the count hit the cap.

    Returns (count, capped). This is the one full walk left, and it exists only
    to put a number in the section header. It prunes the same directories the
    tree hides, so the number matches what opening every folder would show.
    """
    if not workspace.exists():
        return 0, False
    total = 0
    for root, dirs, files in os.walk(workspace):
        at_root = Path(root) == workspace
        dirs[:] = [
            d for d in dirs if _visible(d, at_root=at_root, is_dir=True)
        ]
        rel = Path(root).relative_to(workspace).as_posix()
        total += sum(
            1 for f in files
            if (f if at_root else f"{rel}/{f}") not in HIDDEN_PATHS
        )
        if total >= cap:
            return cap, True
    return total, False

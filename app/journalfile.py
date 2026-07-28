"""Mirror a project's journal into its own workspace, as a file an agent can read.

The prompt's journal section is under a byte budget (`app/promptbudget.py`): the
newest entry is whole, and older ones degrade to their heading plus opening
paragraph. That trade is only honest if there is somewhere to go when the
paragraph is not enough - and until now there was not. `learnings.md` already
had this: it is a file on disk, and the trimmed block in the prompt names its
path and grants an explicit license to read it. The journal had no equivalent,
because it lives in the portal's SQLite database, which an agent working inside
its own workspace cannot reach and should not be taught to.

So the portal writes the project's whole journal to `<workspace>/.portal/journal.md`
before every run, and the trimmed notice names that path.

WHY IT IS WRITTEN BEFORE THE PROMPT IS BUILT, and why that ordering is the whole
correctness argument: the two read the journal in separate queries, so an entry
created between them would be in the prompt and not in the file. That cannot
break the promise, because such an entry is by definition the *newest* one, and
`journal_for_prompt` always shows the newest entry in full. Reverse the order and
the race lands on an entry that got shortened - a heading pointing at a file that
does not contain it. Ordering, not locking, is what makes this safe.

The ask side thread is left out, exactly as the prompt leaves it out: an ask is a
parallel question to the portal, not an instruction to a run, and putting the
thread in the fallback would smuggle back in precisely the content the prompt
excludes on purpose.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from . import config, db

log = logging.getLogger(__name__)

# Relative to the workspace root, and quoted into the prompt exactly as written.
RELPATH = ".portal/journal.md"

# The largest journal on this install is 523 KB over 348 entries (project-portal,
# measured 2026-07-28); the next largest is 240 KB. A 2 MB ceiling therefore holds
# every project's entire history with room to quadruple. It exists so one runaway
# project cannot write a file nobody can open, not to be reached in normal use -
# which is why it drops whole oldest entries and says how many, rather than
# truncating.
MAX_BYTES = 2_000_000

# How many entries to even consider. Well past the ceiling above for any project
# here, and it keeps a pathological journal from being pulled into memory whole.
MAX_ENTRIES = 2000

# An entry starts at a line of this shape. A progress report's own body is full of
# `## ` headings, so the delimiter is not "a heading" - it is a heading whose text
# is a bracketed ISO timestamp, which prose does not accidentally produce.
ENTRY_RE = re.compile(r"^## \[\d{4}-\d{2}-\d{2}T[\d:]{8}", re.M)

_HEADER = """\
# Journal: {title}

Every entry the portal holds for this project, oldest first. The portal rewrites
this file before each run - **edit it and your edits are gone next run**, and it
is excluded from this repository's git, so do not commit it.

It is here because the journal section of your prompt is under a byte budget:
older entries reach you as their heading and opening paragraph only. When that is
not enough, the full text is below. Find an entry by searching for the timestamp
your prompt showed for it.

One entry begins at each line of the form `## [<timestamp>] <author>/<kind>`.
The ask side thread (user/ask and agent/answer) is left out here for the same
reason it is left out of your prompt: an ask is a parallel question, not an
instruction to a run.
"""


def _lf(text: str) -> str:
    """One line ending for the whole file.

    A note dictated on a phone reaches the database with CRLF, because that is
    what a browser posts from a textarea, while everything an agent writes is LF.
    Rendered as-is the mirror comes out with both, which shows up as a column of
    `^M` in anything that does not normalize, and makes a multi-line search behave
    differently over one entry than over the next. Caught by rendering the real
    journal rather than a fixture - the fixtures were all LF, so they shared the
    assumption with the code and could not see it.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def render(title: str, rows: Iterable[sqlite3.Row], dropped: int = 0) -> str:
    """The file's text: a header that explains itself, then the entries."""
    parts = [_HEADER.format(title=title or "this project")]
    if dropped:
        parts.append(
            f"The {dropped} oldest entries are not in this file - it is capped at "
            f"{MAX_BYTES // 1024} KB. They are on the project page in the portal."
        )
    body = []
    for row in rows:
        body.append(f"## [{row['ts']}] {row['author']}/{row['kind']}\n\n"
                    f"{_lf(row['content_md'] or '').strip()}\n")
    return "\n".join(parts) + "\n" + "\n".join(body)


def _fit(rows: list[sqlite3.Row]) -> tuple[list[sqlite3.Row], int]:
    """The newest entries that fit under the ceiling, oldest dropped first.

    Whole entries only. A journal entry cut in the middle reads as a complete
    report of work that was in fact done differently, which is worse than an
    entry that is openly absent and counted.
    """
    kept: list[sqlite3.Row] = []
    used = 0
    for row in reversed(rows):
        cost = len(row["content_md"] or "") + len(str(row["ts"])) + 64
        if kept and used + cost > MAX_BYTES:
            break
        kept.append(row)
        used += cost
    kept.reverse()
    return kept, len(rows) - len(kept)


def write(project: sqlite3.Row, workspace: Path) -> Optional[Path]:
    """Refresh `<workspace>/.portal/journal.md`. Returns the path, or None.

    Never raises: a workspace the portal cannot write to is a run that goes ahead
    without a fallback, not a run that does not happen. `pointer` reads the same
    file from disk, so a failure here simply means the prompt stops promising it.
    """
    try:
        rows = db.list_journal_asc(int(project["id"]), limit=MAX_ENTRIES,
                                   exclude=db.SIDE_THREAD)
        kept, dropped = _fit(rows)
        path = workspace / RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(project["title"], kept, dropped), encoding="utf-8")
        return path
    except (OSError, sqlite3.Error, KeyError, TypeError) as exc:
        log.warning("could not write %s for %s: %s", RELPATH,
                    project["slug"] if project else "?", exc)
        return None


def pointer(project: sqlite3.Row) -> str:
    """`RELPATH` if the file is actually there, else "".

    The prompt must not name a file that is not on disk - a pointer into nothing
    is worse than no pointer, because an agent that follows it and finds nothing
    concludes the history does not exist rather than that the write failed. An
    older file left by a previous run is still a good answer: everything the
    budget shortens is by definition not the newest entry, so a file one run
    behind still holds it.
    """
    try:
        if (config.PROJECTS_DIR / project["slug"] / RELPATH).is_file():
            return RELPATH
    except (OSError, KeyError, TypeError):  # pragma: no cover - defensive
        pass
    return ""

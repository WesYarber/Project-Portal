"""Wes's notes between runs: one block in the prompt, editable until it goes.

Two asks, one mechanism.

*"Previous notes that haven't yet been sent to a model should be able to be
edited."* A note only stops being editable once an agent has actually read it,
so the portal has to know when that happened - `journal.delivered_at`, stamped
here and nowhere else.

*"Each note sent between model runs should be grouped together and sent to the
model as one single note."* Notes used to reach an agent only as scattered lines
in the journal tail, interleaved with status changes and run reports, in a
section headed "recent journal" that reads as history rather than as
instructions. Four notes typed over an hour were four easily-missed lines. They
now arrive as one block, above the journal, headed with what it is - and are
left out of the journal tail on that run so the agent is not reading the same
sentences twice in two different framings.

The stamping happens at the moment the text is rendered into a prompt, in the
same call, because "sent to a model" is exactly that moment. Doing it after the
run instead would leave a note editable while an agent was already acting on it,
which is the one state that would make the edit button a lie.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app import config, db

log = logging.getLogger(__name__)


@dataclass
class Delivery:
    """What went into the prompt, and which entries that used up."""

    text: str = ""
    ids: list[int] = field(default_factory=list)


def pending(project_id: int) -> list[db.sqlite3.Row]:
    return db.pending_notes(project_id)


def is_pending(row: db.sqlite3.Row) -> bool:
    """Whether the page should offer to edit this journal entry. Reads the row
    the template already has rather than re-querying per entry."""
    try:
        if not db.is_note(row["author"], row["kind"]):
            return False
        return row["delivered_at"] in (None, "")
    except (IndexError, KeyError):  # a row from a query without the column
        return False


def render(rows: list[db.sqlite3.Row]) -> str:
    """The block, without touching the database - so a test (and a preview of
    what an agent would see) can have the text without spending the notes."""
    if not rows:
        return ""
    if len(rows) == 1:
        head = (
            f"## A note from {config.SITE.owner} since your last run\n"
            "He wrote this after the previous run finished. Treat it as part of "
            "your instructions for this run, not as background."
        )
    else:
        head = (
            f"## {len(rows)} notes from {config.SITE.owner} since your last run\n"
            "He wrote these after the previous run finished, oldest first. Treat "
            "them together as one instruction for this run, not as background - "
            "and read all of them before you start, because a later one may "
            "correct an earlier one."
        )
    body = "\n\n".join(
        f"**[{row['ts']}]**\n{(row['content_md'] or '').strip()}" for row in rows
    )
    return f"{head}\n\n{body}"


def deliver(project_id: int) -> Delivery:
    """Render the pending notes AND mark them sent. Never raises: a prompt that
    loses this block still carries the notes in its journal tail, because the
    tail only drops the ids this returns."""
    try:
        rows = pending(project_id)
        if not rows:
            return Delivery()
        text = render(rows)
        ids = [int(r["id"]) for r in rows]
        db.mark_notes_delivered(ids)
        return Delivery(text=text, ids=ids)
    except Exception:  # pragma: no cover - defensive
        log.exception("Could not deliver pending notes for project %s", project_id)
        return Delivery()

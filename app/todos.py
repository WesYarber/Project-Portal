"""The per-project working checklist.

Two things live here that the plain DB helpers in `app.db` deliberately don't:
how the list is rendered *into an agent's prompt*, and how a `todo_updates`
block in `.portal/report.json` is applied back.

The point of the feature is memory. A run reads twenty journal entries; Wes's
request from three days and six runs ago is somewhere further up, and the model
that would have acted on it never sees it. An explicit list that goes into
every prompt and that the agent is told to maintain turns "remember to do this
eventually" from a context-window gamble into a row in a table.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app import config, db

log = logging.getLogger("portal.todos")

# Anything past this and the prompt section starts crowding out the journal.
# Open items are never dropped; only the completed tail is trimmed.
MAX_DONE_SHOWN = 15


def _line(row) -> str:
    box = "[x]" if row["done"] else "[ ]"
    chips = "".join(f"[{t}] " for t in db.todo_tags(row))
    return f"- {box} #{row['id']} {chips}{row['text']}"


def prompt_section(project_id: int) -> str:
    """The `## Todo list` block for a run prompt, or '' if the list is empty.

    Omitted entirely rather than emitted as an empty heading: a project with no
    todos should not have the agent reasoning about a list that isn't there.
    """
    rows = db.list_todos(project_id)
    if not rows:
        return ""

    mine = [r for r in rows if r["owner"] == "agent"]
    theirs = [r for r in rows if r["owner"] == "user"]

    def block(items, heading: str, empty: str) -> str:
        if not items:
            return f"{heading}\n{empty}"
        open_items = [r for r in items if not r["done"]]
        done_items = [r for r in items if r["done"]][-MAX_DONE_SHOWN:]
        lines = [_line(r) for r in open_items] + [_line(r) for r in done_items]
        return f"{heading}\n" + "\n".join(lines)

    parts = [
        "## Todo list for this project",
        "This list is the project's memory between runs. Keep it accurate.",
        "",
        block(mine, "### Yours (the agent's)", "(nothing outstanding)"),
        "",
        block(theirs,
              f"### {config.SITE.owners} (only {config.SITE.they} can do these)",
              "(nothing outstanding)"),
        "",
        f"Report `todo_updates` to change it: `add` anything {config.SITE.owner} has asked for "
        "that you are not finishing this run (so it survives into the next "
        "prompt), and `done` the ids you actually completed. Do not tick "
        "something off you have not verified. The bracketed words on an item "
        "are its tags; retag with `\"tags\": {\"<id>\": [...]}` (the list "
        "replaces the item's tags). The tag `blocked` has teeth: the scheduler "
        "does not count a blocked item as workable, so tag what truly waits on "
        f"{config.SITE.owner} - and clear the tag the moment it no longer does.",
    ]
    return "\n".join(parts)


def apply_updates(project_id: int, updates: Any) -> dict[str, int]:
    """Apply a report's `todo_updates` block. Returns a count of what changed.

    Tolerant by design: the block is one field of a JSON blob written by a
    language model, and a malformed half should not cost the other half or blow
    up the run's report handling.
    """
    counts = {"added": 0, "done": 0, "tagged": 0}
    if not isinstance(updates, dict):
        return counts

    for item in updates.get("add") or []:
        text: Optional[str]
        owner = "agent"
        tags = None
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = item.get("text")
            owner = item.get("owner") or "agent"
            tags = item.get("tags")
        else:
            continue
        before = db.count_open_todos(project_id)
        row = db.add_todo(project_id, text or "", owner, tags=tags)
        # add_todo is idempotent on text, so "added" counts real new rows only.
        if row is not None and db.count_open_todos(project_id) > before:
            counts["added"] += 1

    for ref in updates.get("done") or []:
        if db.complete_todo_by_ref(project_id, ref) is not None:
            counts["done"] += 1
        else:
            log.info("todo_updates.done referenced unknown todo %r on project %s", ref, project_id)

    for todo_id, tags in _tag_ops(updates.get("tags")):
        row = db.get_todo(todo_id)
        if row is None or row["project_id"] != project_id:
            log.info("todo_updates.tags referenced unknown todo %r on project %s", todo_id, project_id)
            continue
        if db.set_todo_tags(todo_id, tags) is not None:
            counts["tagged"] += 1

    return counts


def _tag_ops(block: Any) -> list[tuple[int, Any]]:
    """The `tags` half of a report, as (todo_id, new_tags) pairs.

    Two shapes are accepted - `{"14": ["blocked"]}` and
    `[{"id": 14, "tags": ["blocked"]}]` - because a model told about a mapping
    writes a list of objects often enough that rejecting it would quietly lose
    the retag. The value always REPLACES the item's tags; `[]` untags."""
    ops: list[tuple[int, Any]] = []
    if isinstance(block, dict):
        items = block.items()
    elif isinstance(block, list):
        items = [(d.get("id"), d.get("tags")) for d in block if isinstance(d, dict)]
    else:
        return ops
    for ref, tags in items:
        try:
            ops.append((int(str(ref).lstrip("#")), tags))
        except (TypeError, ValueError):
            continue
    return ops

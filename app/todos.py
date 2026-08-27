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
import re
import sqlite3
from typing import Any, Optional

from app import config, db, people

log = logging.getLogger("portal.todos")

# Anything past this and the prompt section starts crowding out the journal.
# Open items are never dropped; only the completed tail is trimmed.
#
# A BYTE budget since 2026-07-29, not a count. The count was 15 items per half,
# which sounds small until you notice that a portal todo runs to 500 characters
# - so the cap admitted 15 KB of work that is already finished, and measured on
# this project's own prompt it was letting through more bytes than the whole
# open list. A count is a proxy for size that any one verbose entry breaks; the
# journal and learnings budgets moved off counts for exactly this reason.
DONE_TAIL_BYTES = 2048


def _line(row) -> str:
    box = "[x]" if row["done"] else "[ ]"
    chips = "".join(f"[{t}] " for t in db.todo_tags(row))
    return f"- {box} #{row['id']} {chips}{row['text']}"


# ---------------------------------------------------------------------------
# Whose item is it?
# ---------------------------------------------------------------------------
#
# `owner` says agent-or-human. `person_id` says which human, and is NULL on
# every row written before 2026-07-28 because nothing was back-filled - see the
# column's comment in db.py.
#
# The gap between them is closed by deduction rather than by a guess: an
# unassigned item on a project with exactly one member is that member's,
# because the set of people who could do it has one element in it. That single
# rule is also what keeps a one-person install unchanged, since there the sole
# member is the owner and every human item resolves to him.


def sole_member(project_id: int) -> Optional[sqlite3.Row]:
    """The one active person on this project, or None if it isn't exactly one.

    Archived people don't count: retiring somebody is the act of saying they
    are not doing things any more, and an item deduced onto them would be an
    open task assigned to a person the portal has been told to stop asking.
    """
    active = [m for m in people.members(project_id) if not m["archived_at"]]
    return active[0] if len(active) == 1 else None


def responsible_for(row, fallback: Optional[sqlite3.Row] = None) -> Optional[sqlite3.Row]:
    """Which person has to do this item, or None when nobody can say.

    `fallback` is the project's `sole_member`, passed in rather than looked up
    so that grouping a list costs one membership query instead of one per row.
    """
    if row["owner"] != "user":
        return None
    person = people.get(row["person_id"]) if row["person_id"] else None
    return person if person is not None else fallback


def by_person(rows, project_id: int) -> list[tuple[Optional[sqlite3.Row], list]]:
    """The human half of a list, grouped by whose it is.

    People in `people.everyone()` order (the owner first, then by name), with
    the un-attributable items last. Groups with nothing in them are dropped, so
    a list where every item resolves to one person is one group - which is the
    one-person install, and every project of a two-person install that only one
    of them is on.
    """
    fallback = sole_member(project_id)
    buckets: dict[Optional[int], list] = {}
    for row in rows:
        person = responsible_for(row, fallback)
        buckets.setdefault(int(person["id"]) if person is not None else None, []).append(row)
    order = [int(p["id"]) for p in people.everyone()]
    known = [pid for pid in order if pid in buckets]
    # Anybody holding items who is not in `everyone()` - archived, or deleted
    # out from under the row - still gets a group rather than vanishing from
    # the page. Losing an open task because its owner retired would be a bug
    # that presents as work quietly disappearing.
    known += [pid for pid in buckets if pid is not None and pid not in order]
    groups: list[tuple[Optional[sqlite3.Row], list]] = [
        (people.get(pid), buckets[pid]) for pid in known
    ]
    if None in buckets:
        groups.append((None, buckets[None]))
    return groups


# The value the /todo/{id}/person route reads to mean "the agent". Empty string
# already meant "nobody", and nobody and the agent are genuinely different
# destinations, so this needed a word of its own rather than a second meaning
# hung on the blank.
AGENT_CHOICE = "agent"


def refile_choices(project_id: int) -> list[dict[str, str]]:
    """Every place a checklist item on this project could be moved to.

    One entry per destination, in menu order, each carrying the `person` value
    the re-file route accepts. The agent comes first because it is the half
    that exists on every project; then the people who could actually do the
    work; then "nobody" - but only where there is more than one person to be
    undecided *between*. On a one-person project an unattributed human item
    already resolves to that person by deduction, so offering "nobody" beside
    their name would be two buttons for one outcome.

    Archived people are left out for the same reason `sole_member` skips them:
    retiring somebody is the act of saying they are not doing things any more,
    so handing them an open task is a destination nobody wants.

    A list shorter than two entries comes back empty. That is the "a control
    that changes nothing you can see" rule the one-member case used to be an
    instance of - it just moved from the caller into here, where it is one
    check instead of a condition repeated at every call site.
    """
    members = [m for m in people.members(project_id) if not m["archived_at"]]
    choices = [{"value": AGENT_CHOICE, "label": "the agent"}]
    choices += [{"value": str(int(m["id"])), "label": m["name"]} for m in members]
    if len(members) > 1:
        choices.append({"value": "", "label": "nobody"})
    return choices if len(choices) > 1 else []


def refile_value(person: Optional[sqlite3.Row]) -> str:
    """Which `refile_choices` entry describes a list the page is rendering.

    Taken from the group heading rather than the row: `by_person` has already
    done the deduction, and every item under one heading is by construction the
    same person's - so this costs no query, where asking per row would cost a
    membership lookup per row.
    """
    return str(int(person["id"])) if person is not None else ""


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
        done_lines, dropped = _done_tail([r for r in items if r["done"]])
        lines = [_line(r) for r in open_items] + done_lines
        text = f"{heading}\n" + "\n".join(lines)
        if dropped:
            # Said rather than silently done. An agent reading a list that
            # stops at some arbitrary point has no way to tell whether the
            # older work was never done or merely not shown, and the first
            # reading is the one that makes it redo something.
            text += (
                f"\n({dropped} older completed item(s) not shown - the whole "
                "list is on the project page.)"
            )
        return text

    parts = [
        "## Todo list for this project",
        "This list is the project's memory between runs. Keep it accurate.",
        "",
        block(mine, "### Yours (the agent's)", "(nothing outstanding)"),
    ]
    # An empty human half still gets a heading, and it is the heading of
    # whoever the next item added there would land on - not "nobody", which
    # would read as a fact about a list that has nothing in it to be a fact
    # about.
    groups = by_person(theirs, project_id) or [(sole_member(project_id), [])]
    for person, items in groups:
        parts += ["", block(items, _human_heading(person, project_id), "(nothing outstanding)")]
    # The principal, not unconditionally the install owner: on a project only
    # Karli is on, "what truly waits on Wes" told the agent to route her own
    # project's blockers through him. See people.principal.
    principal_name = people.name_of(people.principal(project_id))
    parts += [
        "",
        f"Report `todo_updates` to change it: `add` anything {principal_name} has asked for "
        "that you are not finishing this run (so it survives into the next "
        "prompt), and `done` the ids you actually completed. Do not tick "
        "something off you have not verified. Keep this list SHORT and each "
        "item to ONE sentence - it is a checklist, not a journal; put the "
        "background in your journal entry and let the item name the action. "
        "Close items aggressively: an item you opened that no longer matters "
        "is yours to `done` or delete, and a stale 'watch/verify' note is "
        "noise, not memory. Open an item only when a CONCRETE action would "
        "otherwise be forgotten - never to log what you already did, and "
        "never to ask a person to go admire or verify a feature; say that "
        "in the summary instead. An item for a person is only for something "
        "that genuinely needs their hands (a purchase, a credential, a "
        "click). The bracketed words on an item are its tags - never write "
        "tags into the text itself; retag with `\"tags\": {\"<id>\": [...]}` "
        "(the list replaces the item's tags). The tag `blocked` has teeth: "
        "the scheduler does not count a blocked item as workable, so tag "
        f"what truly waits on {principal_name} - and clear the tag the moment it no "
        "longer does." + _who_clause(),
    ]
    return "\n".join(parts)


def _done_tail(done_items: list, budget: Optional[int] = None) -> tuple[list[str], int]:
    """The most recently completed items that fit in `budget` bytes.

    Newest first while filling, then reversed back into completion order,
    because the list reads oldest-to-newest everywhere else and a tail that
    ran backwards would be the only part of the prompt that did.

    At least one item always survives, however long it is. A section that
    admits nothing would tell an agent the half is empty, which is a different
    and worse claim than "here is the last thing you finished".

    `budget` is a parameter rather than a straight read of `DONE_TAIL_BYTES` so
    that a test can state its own size instead of scaling its fixture off the
    module constant. That is not tidiness: a test building `"x" * (
    DONE_TAIL_BYTES * 3)` allocates 3 GB the moment a delete-the-fix mutation
    raises the constant, so pytest is killed inside the run's memory cgroup
    before it can emit a single failure - and the sweep reads a crashed run as
    "this mutation was uncaught". A fixture must never take its size from the
    constant it is testing.
    """
    if budget is None:
        budget = DONE_TAIL_BYTES
    kept: list[str] = []
    used = 0
    for row in reversed(done_items):
        line = _line(row)
        cost = len(line) + 1
        if used + cost > budget and kept:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    return kept, len(done_items) - len(kept)


def _human_heading(person: Optional[sqlite3.Row], project_id: int) -> str:
    """The `###` line over one person's half of the list.

    On a one-person install this is byte-for-byte what the single hard-coded
    heading used to be, because the sole member of every project there is the
    owner and `people.possessive` follows the same Chicago rule `SITE.owners`
    does.

    The un-attributable heading names the candidates rather than stopping at
    "somebody". Saying which of two people has to do a thing is a claim the
    portal cannot make; listing the two who could is a fact it holds, and it is
    the difference between an agent that knows who to ask and one that does not.
    """
    if person is not None:
        they = people.pronouns_of(person)[0]
        return f"### {people.possessive(people.name_of(person))} (only {they} can do these)"
    names = ", ".join(m["name"] for m in people.members(project_id) if not m["archived_at"])
    if not names:
        return "### Waiting on a person (nobody recorded which)"
    return f"### Waiting on a person (nobody recorded which of: {names})"


def _who_clause() -> str:
    """The sentence that teaches `person`, on installs where it means anything.

    Absent while there is one person: a field whose only possible value is the
    one person reading it is noise, and the prompt is under a byte budget.
    """
    if not people.more_than_one():
        return ""
    names = ", ".join(p["name"] for p in people.everyone())
    return (
        " More than one person uses this portal, so an item for a human says "
        "WHICH one: add it with `\"person\"` set to their name "
        f"({names}) alongside `\"owner\": \"user\"`. Leave `person` out when you "
        "genuinely do not know - an item filed against the wrong person is "
        "worse than one filed against nobody, because only the second one "
        "reads as the open question it is."
    )


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
        person_id = None
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = item.get("text")
            owner = item.get("owner") or "agent"
            tags = item.get("tags")
            person_id = _person_ref(item.get("person"))
            if person_id is None and owner not in db.TODO_OWNERS:
                # A model told to write "agent" or "user" and also told about
                # people writes `"owner": "Karli"` sooner or later. Reading it
                # as a name is the only interpretation that isn't a silent
                # loss: the old code dropped anything unrecognized to "agent",
                # which put an item somebody has to do onto the agent's backlog
                # and told nobody.
                person_id = _person_ref(owner)
        else:
            continue
        text, embedded = _split_embedded_tags(text or "")
        if embedded:
            tags = list(dict.fromkeys([*(tags or []), *embedded]))
        before = db.count_open_todos(project_id)
        row = db.add_todo(project_id, text, owner, tags=tags, person_id=person_id)
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


def _split_embedded_tags(text: str) -> tuple[str, list[str]]:
    """Leading `[tag]` tokens an agent wrote into an item's text, split off.

    The prompt shows an item's tags as bracketed chips in front of its text, so
    a model restating an item writes them back into `text` sooner or later.
    Wes, 2026-08-04: "The tags often show up in the body of the todo item as
    [tag] [tag2], and they should not." They become real tags instead of words.

    Deliberately narrow: only tag-shaped tokens (short, kebab-ish) at the very
    front of the text. "[RESEARCH.md §1]" mid-sentence, a markdown checkbox, a
    literal bracketed clause - none of those match, because eating a word
    somebody meant is worse than leaving a chip in the text.
    """
    tags: list[str] = []
    rest = (text or "").lstrip()
    while True:
        m = re.match(r"\[([A-Za-z0-9][A-Za-z0-9_-]{0,23})\]\s*", rest)
        if not m:
            break
        tags.append(m.group(1).lower())
        rest = rest[m.end():]
    return (rest if rest else (text or "")), tags


def _person_ref(value: Any) -> Optional[int]:
    """A person named in a report, as an id - or None if nobody matches.

    Accepts a name, a slug or a numeric id, because a report is written by a
    language model reading a prompt that shows it names. An unresolvable value
    is dropped rather than guessed at: `people.known_name` exists for exactly
    this reason on the answer path, and an item filed against the wrong person
    reads as a decision somebody made.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        row = people.get(value)
        return int(row["id"]) if row is not None else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in ("agent", "user", "me", "you"):
        return None
    if text.isdigit():
        row = people.get(int(text))
        if row is not None:
            return int(row["id"])
    wanted = text.lower()
    # Archived people are eligible on purpose: an agent told about somebody in
    # an older journal entry may still name them, and recording the attribution
    # it actually meant is better than filing the item against nobody. They
    # are simply not offered anywhere new.
    for row in people.everyone(include_archived=True):
        if (row["name"] or "").strip().lower() == wanted or row["slug"] == people.slugify_name(text):
            return int(row["id"])
    log.info("todo_updates.add named a person nobody matches: %r", text)
    return None


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

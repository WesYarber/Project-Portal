"""Who a notification is for, and which concrete channels that means.

Wes, on the multi-person portal: *"a question for her does not go to his
phone."* Until now every notification went to exactly one place per channel -
one ntfy topic, one Telegram chat, every enrolled device - because the install
had one person in it and the question never came up. This module is the part
that changes when it does.

## The shape

Two steps, deliberately separate:

1. **Recipients** - which *people* this notification is addressed to. A
   notification about a project is addressed to that project's members;
   anything install-wide (a new model, a suggestion, the test button) is
   addressed to everybody.
2. **Targets** - the concrete topic / chat id / push endpoint each recipient
   can actually be reached on.

Keeping them apart is what makes the fallback safe, because the dedupe happens
on the *target*, not on the person. Two people who both fall back to the
install's topic produce one send, not two.

## Empty is not "send nowhere"

A person with no channel of their own falls back to the install's channel. This
is the single most important rule here and it is not the obvious one - the
obvious one is "route strictly, and somebody with no topic gets nothing", which
means adding a person to a project silently stops that project's notifications
reaching anyone at all. Wes's standing complaint about software is that nothing
should fail quietly, and a notification that reaches nobody is the quietest
failure available.

So the guarantee is: **a notification always reaches at least the install's own
channels, until somebody has explicitly been given their own.** Once she has a
topic, her projects stop landing on his phone - and that is a consequence of a
field somebody filled in, not of a default nobody chose.

## What this means on a one-person install

Exactly nothing. One person, no personal channels, so every route resolves to
the install's own topic and chat id and every enrolled device - byte-identical
to the behavior before this module existed. That is asserted by tests rather
than hoped for; see tests/test_routing.py.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Optional, Sequence

from app import db, people


def recipients(project_id: Optional[int] = None) -> list[sqlite3.Row]:
    """The people a notification is addressed to.

    A project's members, or everybody. A project with *no* members falls back
    to everybody rather than to nobody: an unassigned project is not a private
    one, and the alternative is that taking the last member off a project
    silently mutes it.
    """
    if project_id is not None:
        members = people.members(int(project_id))
        if members:
            return members
    return people.everyone()


def _dedupe(values: Iterable[str]) -> list[str]:
    """Order-preserving dedupe, dropping blanks. Order is preserved because it
    is the order people are listed in, which makes a test's expectations
    readable and a log line stable."""
    seen: list[str] = []
    for value in values:
        value = (value or "").strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def _channel(
    rows: Sequence[sqlite3.Row], column: str, install_default: str
) -> list[str]:
    """Each recipient's own channel, falling back to the install's."""
    if not rows:
        # No people at all should be impossible (people.ensure_owner runs at
        # boot), but a notification is not the place to find that out.
        return _dedupe([install_default])
    out: list[str] = []
    for row in rows:
        try:
            own = row[column]
        except (IndexError, KeyError):
            # A portal.db that has not been through init_db yet - the column is
            # added there. Fall back rather than raise: this is a notification.
            own = ""
        out.append((own or "").strip() or install_default)
    return _dedupe(out)


def ntfy_topics(rows: Sequence[sqlite3.Row], install_topic: str) -> list[str]:
    return _channel(rows, "ntfy_topic", install_topic)


def telegram_chats(rows: Sequence[sqlite3.Row], install_chat_id: str) -> list[str]:
    return _channel(rows, "telegram_chat_id", install_chat_id)


def push_subscriptions(
    rows: Sequence[sqlite3.Row], subs: Sequence[sqlite3.Row]
) -> list[sqlite3.Row]:
    """The enrolled devices to push to.

    A subscription with no person on it goes to everything. See the schema note
    on `push_subscriptions.person_id`: every device enrolled before that column
    existed is Wes's, and the failure mode of guessing wrong here is a phone
    that stops telling him things without ever saying so.
    """
    ids = {int(row["id"]) for row in rows}
    keep: list[sqlite3.Row] = []
    for sub in subs:
        try:
            person_id = sub["person_id"]
        except (IndexError, KeyError):
            person_id = None
        if person_id is None or int(person_id) in ids:
            keep.append(sub)
    return keep


def telegram_allowlist() -> set[str]:
    """Every Telegram chat the bot will accept a message from.

    The install's own chat id, plus any person who has one. Before this, the
    bot compared the incoming chat against `telegram_chat_id` and dropped
    anything else on the floor with a log line - which meant a second person
    could not use the bot at all, however carefully the rest of the portal had
    learned to tell them apart. The allowlist is still an allowlist: this
    widens it to the people who live here, not to the internet.

    Archived people are *not* admitted, because archiving is the act of
    retiring somebody from this portal and a bot that kept taking their notes
    would make that act a lie. Attribution is the other way round - see
    `people.by_telegram_chat_id`, which does look at archived rows, so a
    message that already arrived keeps its author.
    """
    allowed = {(db.get_setting("telegram_chat_id") or "").strip()}
    for person in people.everyone():
        try:
            chat = (person["telegram_chat_id"] or "").strip()
        except (IndexError, KeyError):
            chat = ""
        if chat:
            allowed.add(chat)
    allowed.discard("")
    return allowed

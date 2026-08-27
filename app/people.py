"""More than one person using the portal.

Wes, 2026-07-28:

  "would it be feasible to add additional users that can have their own
  projects? I want my wife to be able to start using this tool and have her own
  projects. All current projects should be assigned to me, and they should be
  able to be reassigned if desired. They should be able to belong to multiple
  users who can each prompt separately while using the same context of work
  history and whatnot, but the model should be able to recognize us as different
  people to understand when we might need things explained different or
  something like that. She is less technical and is newer to all of this stuff
  where I have a bit more experience. It would be good if it could learn what
  she understands and speak to her at that level and teach her things that she
  wouldn't otherwise know. We are wanting to develop a tool together, though!"

Four requirements hide in that paragraph, and they pull in different directions:

1. People have their OWN projects.
2. A project can belong to SEVERAL people, who each prompt it separately.
3. Those several people share one context - one journal, one todo list, one run
   history. "the same context of work history and whatnot" rules out the obvious
   design, which is a tenant column on everything.
4. The agent must tell them apart *as people*, so it can pitch an explanation at
   the person actually reading it.

So this is not multi-tenancy. Nothing is partitioned; a project gains a set of
members, and a request gains an acting person. Everything else - journals,
todos, runs, questions - stays exactly as shared as it already was. That is a
much smaller change than a tenant model, and it is the one that matches what he
asked for: he and his wife building a tool *together*, not two portals.

## What "the model recognizes us" actually needs

Not an access-control system. The agent needs two facts at prompt time:

- **who wrote the thing it is reading** (a note, an answer), so it can address
  them, and
- **what that person already knows**, so it can pitch at them.

Hence `background`: free text, deliberately not a level enum. "Less technical,
new to self-hosting and to agent tooling; teach the concepts rather than
assuming them" tells a model far more than `skill_level=2`, and it is a field a
person can grow by editing a sentence. It is the same shape as `profile.md`, and
for the same reason.

## Identity

`resolve()` is the whole rule, and it is a pure function so it can be tested
without a browser, a cookie or a tailnet. See its docstring for the precedence
and why it is that way round.

## The owner is not special, except once

`is_owner` marks the person the install was set up for - `SITE.owner`, the name
in the agent contract and in every prompt. It buys exactly one privilege: there
is always at least one, so `owner()` can never return None and the portal can
never end up with nobody to attribute a note to. It is NOT an admin flag; a
second person can do everything the owner can.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from app import config, db, sections, site

log = logging.getLogger(__name__)

# The cookie that remembers who this browser is. A slug rather than an id
# because it survives a database restore, and because it is legible in devtools
# - if the wrong name is showing, the reason is readable rather than "17".
COOKIE = "portal_person"

# Ten years. This is a "which of the two of us is holding the phone" cookie, not
# a session: expiring it would silently start attributing her notes to him,
# which is the exact failure this whole module exists to prevent.
COOKIE_MAX_AGE = 10 * 365 * 24 * 3600

# Set once the existing projects have been handed to the owner. A settings flag
# rather than a "does the table look empty" check, because after the backfill
# somebody may deliberately take themselves off a project - and re-adding them
# on the next boot would be the portal overruling a decision a person made.
BACKFILL_KEY = "people_backfilled"


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def slugify_name(name: str) -> str:
    """A stable handle for a person: lowercase, letters/digits/dashes.

    Falls back to `person` rather than to the empty string, because the slug is
    a cookie value and a URL segment; an empty one would resolve to everybody
    and nobody.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "person"


def unique_slug(name: str, exclude_id: Optional[int] = None) -> str:
    """`slugify_name` plus a numeric suffix if that handle is taken.

    Two people called Sam is a thing that happens, and a UNIQUE constraint
    blowing up in a form handler is not the way to find out.
    """
    base = slugify_name(name)
    conn = db.get_conn()
    candidate = base
    n = 1
    while True:
        row = conn.execute(
            "SELECT id FROM people WHERE slug = ?", (candidate,)
        ).fetchone()
        if row is None or (exclude_id is not None and int(row["id"]) == exclude_id):
            return candidate
        n += 1
        candidate = f"{base}-{n}"


def possessive(name: str) -> str:
    """"Wes" -> "Wes's". Matches SITE.owners, including for a name ending in s.

    Chicago's rule, which is what `site.py` already applies to the owner: always
    add 's. Keeping the two the same means "Wes's projects" reads identically
    whether it came from the site config or from a row in this table.
    """
    return f"{(name or '').strip()}'s"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _row(sql: str, args: Sequence = ()) -> Optional[sqlite3.Row]:
    return db.get_conn().execute(sql, tuple(args)).fetchone()


def get(person_id: Optional[int]) -> Optional[sqlite3.Row]:
    if person_id is None:
        return None
    return _row("SELECT * FROM people WHERE id = ?", (int(person_id),))


def by_slug(slug: str) -> Optional[sqlite3.Row]:
    if not slug:
        return None
    return _row("SELECT * FROM people WHERE slug = ?", (slug,))


def by_tailnet_login(login: str) -> Optional[sqlite3.Row]:
    """The person whose tailnet login this is, if any.

    Empty logins can never match: `tailnet_login` defaults to '' for everybody
    who has not been given one, so a lookup of '' would otherwise return an
    arbitrary person - the classic empty-string-matches-everyone bug.
    """
    if not (login or "").strip():
        return None
    return _row(
        "SELECT * FROM people WHERE tailnet_login = ? AND archived_at IS NULL",
        (login.strip().lower(),),
    )


def by_telegram_chat_id(chat_id: str) -> Optional[sqlite3.Row]:
    """The person this Telegram chat belongs to, if anybody has claimed it.

    Archived people are included on purpose, and this is the opposite of the
    rule in `routing.telegram_allowlist`. The two answer different questions:
    the allowlist decides whether a *new* message is accepted, so retiring
    somebody has to close the door; this decides who *wrote* a message that was
    accepted, and a retired person is still the author of what they said.

    Returns None rather than guessing. The install's own `telegram_chat_id` is
    deliberately not consulted here even though on a one-person portal it is
    obviously the owner's - `people.known_name` exists for exactly this reason,
    and last week's lesson holds: an invented attribution reads identically to a
    real one and sends the next agent to talk to the wrong person.
    """
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return None
    return _row("SELECT * FROM people WHERE telegram_chat_id = ?", (chat_id,))


def everyone(include_archived: bool = False) -> list[sqlite3.Row]:
    """Everybody, owner first, then by name.

    Owner first is not vanity: it is the order the switcher and the member
    checkboxes render in, and a stable first entry means the list does not
    reshuffle when somebody is renamed.
    """
    where = "" if include_archived else "WHERE archived_at IS NULL"
    return list(
        db.get_conn().execute(
            f"SELECT * FROM people {where} ORDER BY is_owner DESC, name COLLATE NOCASE, id"
        )
    )


def owner() -> sqlite3.Row:
    """The person this install belongs to. Never None.

    Goes through `ensure_owner` on every call rather than trusting the row to
    already be right. That is one extra comparison per call and it buys the
    invariant by construction: the owner exists, and their name is the site
    config's, no matter which code path got here first or whether the config
    changed since boot. A cached row would be a staleness bug waiting for
    somebody to edit `portal.toml` and restart nothing.
    """
    person_id = ensure_owner()
    row = _row("SELECT * FROM people WHERE id = ?", (person_id,))
    assert row is not None  # ensure_owner has just written or found one
    return row


def gender_of(person: Optional[sqlite3.Row]) -> str:
    """The stored answer for a person: `male`, `female`, or '' for not asked."""
    if person is not None:
        try:
            return site.gender_key(person["gender"] or "")
        except (IndexError, KeyError):
            pass
    return site.DEFAULT_GENDER


def pronouns_of(person: Optional[sqlite3.Row]) -> tuple[str, str, str, str]:
    """(they, them, their, theirs) for a person, defaulting to they/them.

    Derived from the one question the portal asks - male or female - rather
    than from a field anybody types pronouns into. See `site.GENDERS`.
    """
    return site.gender_forms(gender_of(person))


def name_of(person: Optional[sqlite3.Row]) -> str:
    """The name to print, falling back to the site owner's.

    Used on the paths that must never render a blank - the prompt heading over
    a note, the byline in the journal - where "" would read as a bug and the
    owner is the only defensible guess.
    """
    if person is not None:
        try:
            if (person["name"] or "").strip():
                return person["name"].strip()
        except (IndexError, KeyError):
            pass
    return config.SITE.owner


def known_name(person_id: Optional[int]) -> str:
    """The name of a person the portal actually recorded, or "" if it did not.

    Deliberately NOT `name_of`, whose owner fallback is right for a byline that
    must never render blank and wrong here. This is used where the answer feeds
    an agent's prompt, and there the difference between "Wes answered" and
    "nobody recorded who answered" is the whole point: falling back to the owner
    would manufacture an attribution and get the pitch of the next reply wrong
    for the one person it matters for.
    """
    if not person_id:
        return ""
    row = get(int(person_id))
    if row is None:
        return ""
    try:
        return (row["name"] or "").strip()
    except (IndexError, KeyError):  # pragma: no cover - defensive
        return ""


def more_than_one() -> bool:
    """True when this install has more than one active person.

    The gate on printing who did something. On a one-person install naming them
    is noise in every prompt and on every page - and the prompt is under a byte
    budget (app/promptbudget.py), so noise there has a price.
    """
    return len(everyone()) > 1


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

DEFAULT_OWNER_BACKGROUND = (
    "Set this portal up and has been using it alone until now. Assume real "
    "software experience."
)


def ensure_owner() -> int:
    """Create the owner row from the site config, and keep it in step with it.

    The name and the gender are the site config's, not this table's, and that
    is a deliberate exception to everything else here.

    `config.SITE.owner` already names this person in dozens of places an agent
    reads - the agent contract, the todo list headings ("Ada Lovelace's (only
    she can do these)"), the ask instructions, every notification template. If
    the row and the config could disagree, one prompt would name two different
    people for the same human, and there would be no way to tell from either
    screen which one was winning. One source of truth, and it is the one that
    was already load-bearing.

    So the owner's name is edited in `portal.toml` (the settings page says so
    and points at it), while `background` and `tailnet_login` - which are new
    and have no config home - are edited here like anybody else's. Everyone
    who is *not* the owner is owned entirely by this table.

    Idempotent and cheap: the UPDATE is skipped when nothing has changed, so
    the common boot writes nothing at all.
    """
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM people WHERE is_owner = 1 LIMIT 1").fetchone()
    name = (config.SITE.owner or "").strip() or "Owner"
    gender = site.gender_key(config.SITE.gender)
    if row is None:
        person_id = add(
            name=name,
            gender=gender,
            background=DEFAULT_OWNER_BACKGROUND,
            is_owner=True,
        )
        log.info("Created the owner person %r (id=%s)", name, person_id)
        return person_id
    if row["name"] != name or row["gender"] != gender:
        with db._LOCK:
            conn.execute(
                "UPDATE people SET name = ?, gender = ?, slug = ? WHERE id = ?",
                (name, gender, unique_slug(name, exclude_id=int(row["id"])), int(row["id"])),
            )
            conn.commit()
        log.info("The owner is now %r (was %r), following the site config", name, row["name"])
    return int(row["id"])


def add(
    name: str,
    gender: str = site.DEFAULT_GENDER,
    background: str = "",
    tailnet_login: str = "",
    is_owner: bool = False,
) -> int:
    conn = db.get_conn()
    clean_name = (name or "").strip() or "Someone"
    with db._LOCK:
        cur = conn.execute(
            "INSERT INTO people (slug, name, gender, background, tailnet_login, "
            "is_owner, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                unique_slug(clean_name),
                clean_name,
                site.gender_key(gender),
                (background or "").strip(),
                (tailnet_login or "").strip().lower(),
                1 if is_owner else 0,
                db.now(),
            ),
        )
        conn.commit()
    return int(cur.lastrowid)


# `ntfy_topic` and `telegram_chat_id` are editable for everybody including the
# owner - unlike name and gender, which belong to portal.toml. They are per
# *person*, not per install, and the owner is a person like the others here.
_EDITABLE = ("name", "gender", "background", "tailnet_login", "ntfy_topic", "telegram_chat_id")


def update(person_id: int, **fields) -> None:
    """Edit a person. Renaming re-slugs, which is why this is not a generic
    UPDATE: the slug is what the cookie holds, so it has to be reissued
    together with the name or a rename would log that person out of their own
    identity.

    The owner's name and gender are silently ignored here rather than
    written: they belong to `portal.toml` (see `ensure_owner`), and the next
    `owner()` call would put them straight back. Accepting the write and then
    reverting it is the worst of the three options - the settings page renders
    the owner's name as read-only text with a pointer to the file, so this is
    the belt to that braces rather than the only guard."""
    person = get(person_id)
    if person is None:
        return
    is_owner = bool(person["is_owner"])
    sets: list[str] = []
    args: list = []
    for key in _EDITABLE:
        if key not in fields:
            continue
        if is_owner and key in ("name", "gender"):
            continue
        value = fields[key]
        if key == "gender":
            value = site.gender_key(value)
        elif key == "tailnet_login":
            value = (value or "").strip().lower()
        elif key == "name":
            value = (value or "").strip() or person["name"]
            sets.append("slug = ?")
            args.append(unique_slug(value, exclude_id=int(person_id)))
        else:
            value = (value or "").strip()
        sets.append(f"{key} = ?")
        args.append(value)
    if not sets:
        return
    args.append(int(person_id))
    conn = db.get_conn()
    with db._LOCK:
        conn.execute(f"UPDATE people SET {', '.join(sets)} WHERE id = ?", tuple(args))
        conn.commit()


# ---------------------------------------------------------------------------
# Each person's own look
# ---------------------------------------------------------------------------
#
# Wes, 2026-07-28: "It would be cool as well if she was able to customize the
# theme of the site for her user to her liking."
#
# The appearance layers (scanlines, glow, animations, typeface, density) were
# one global setting each, which is the right answer for one person and the
# wrong one for two: her turning the scanlines off would turn them off on his
# phone as well. So the settings row becomes the *install's* look and each
# person may override any subset of it.
#
# A subset, not a copy. Somebody who has never opened the appearance panel
# follows the install as it changes; somebody who has turned one layer off has
# pinned that one layer and still follows the rest. Storing a full copy on
# first save would silently freeze the other four at whatever they were that
# afternoon, which is the kind of quiet divergence nobody would ever trace.


def _valid_appearance(values: Mapping[str, str]) -> dict[str, str]:
    """Only recognized keys, only recognized values.

    Anything else is dropped rather than stored. This is the boundary between
    a form (or a hand-edited database) and a `<body>` class name, so a value
    that got through would be painted into the page as `scan-<junk>` and match
    no rule - a setting that appears to save and then does nothing.
    """
    out: dict[str, str] = {}
    for key, choices in config.APPEARANCE_CHOICES.items():
        if key not in values:
            continue
        value = str(values[key] or "")
        if value in {v for v, _ in choices}:
            out[key] = value
    # The one personal preference that is not a dropdown: the order of the
    # blocks on a project page. It rides in the same blob because it is the
    # same kind of thing - a fact about the reader, kept per person - but it is
    # a permutation rather than a value from a list, so it validates through
    # `sections.clean` instead of against `choices`. Named explicitly here
    # because the loop above drops every key it does not recognize, which is
    # what makes a bad byte harmless; a key it silently dropped instead would
    # be a setting that appears to save and reverts on the next load.
    if sections.SETTING_KEY in values:
        arranged = sections.clean(str(values[sections.SETTING_KEY] or ""))
        if arranged:
            out[sections.SETTING_KEY] = arranged
    return out


def appearance_of(person: Optional[sqlite3.Row]) -> dict[str, str]:
    """The appearance keys this person has explicitly chosen.

    Returns only their overrides - never a full set - so the caller can tell
    "she picked no-scanlines" apart from "she has never picked anything". A
    row written before this column existed, or holding junk that a restore or
    a hand edit put there, reads as no overrides rather than raising: a bad
    byte in a preference must not be able to stop a page rendering.
    """
    if person is None:
        return {}
    try:
        raw = person["appearance"]
    except (IndexError, KeyError):  # a row selected before the column existed
        return {}
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        log.debug("Ignoring unreadable appearance for person row", exc_info=True)
        return {}
    if not isinstance(stored, dict):
        return {}
    return _valid_appearance(stored)


def set_appearance(person_id: int, values: Mapping[str, str]) -> dict[str, str]:
    """Store this person's overrides, merged over whatever they had.

    Merged rather than replaced because the appearance panel may one day post
    a subset - and because `apply()` already drops a field the running code
    does not recognize, so a partial submission reaching here is a normal
    event rather than a bug to be strict about.

    Returns what is now stored, which is what the caller needs to re-render
    the page it is about to redirect to.
    """
    person = get(person_id)
    if person is None:
        return {}
    submitted = _valid_appearance(values)
    merged = {**appearance_of(person), **submitted}
    # An override submitted as blank is a REMOVAL, not a blank override, and
    # the merge above cannot express that on its own. Only the page
    # arrangement can be blank - every dropdown posts one of its own values -
    # and blank is how it says "put me back on the shipped order". Without
    # this, dragging a section back where it started would merge the old
    # arrangement straight back over the top and the reset would look broken.
    for key in list(merged):
        if key in values and key not in submitted:
            merged.pop(key)
    conn = db.get_conn()
    with db._LOCK:
        conn.execute(
            "UPDATE people SET appearance = ? WHERE id = ?",
            (json.dumps(merged, sort_keys=True) if merged else "", int(person_id)),
        )
        conn.commit()
    return merged


def clear_appearance(person_id: int) -> None:
    """Drop this person's overrides so they follow the install's look again.

    Distinct from setting every layer back to the shipped default, and the
    difference is the whole point of storing a subset: this re-attaches them
    to the install, so a later change to the install's look reaches them.
    """
    conn = db.get_conn()
    with db._LOCK:
        conn.execute("UPDATE people SET appearance = '' WHERE id = ?", (int(person_id),))
        conn.commit()


def archive(person_id: int) -> bool:
    """Retire somebody without deleting what they wrote.

    Refused for the owner: `owner()` must always find a row, and the alternative
    to refusing is a portal that cannot attribute the next note to anyone.
    Archiving is the right verb rather than DELETE because their notes, answers
    and questions stay in the journal with their name on - deleting the row
    would either orphan those or cascade away real history.
    """
    person = get(person_id)
    if person is None or int(person["is_owner"]):
        return False
    conn = db.get_conn()
    with db._LOCK:
        conn.execute(
            "UPDATE people SET archived_at = ? WHERE id = ?", (db.now(), int(person_id))
        )
        conn.commit()
    return True


def restore(person_id: int) -> None:
    conn = db.get_conn()
    with db._LOCK:
        conn.execute(
            "UPDATE people SET archived_at = NULL WHERE id = ?", (int(person_id),)
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Who is a project's?
# ---------------------------------------------------------------------------

def members(project_id: int) -> list[sqlite3.Row]:
    return list(
        db.get_conn().execute(
            "SELECT p.* FROM people p JOIN project_people pp ON pp.person_id = p.id "
            "WHERE pp.project_id = ? ORDER BY p.is_owner DESC, p.name COLLATE NOCASE, p.id",
            (int(project_id),),
        )
    )


def member_ids(project_id: int) -> set[int]:
    return {int(r["id"]) for r in members(project_id)}


def is_member(project_id: int, person_id: Optional[int]) -> bool:
    if person_id is None:
        return False
    return int(person_id) in member_ids(project_id)


def set_members(project_id: int, person_ids: Iterable[int]) -> list[int]:
    """Replace a project's membership, and return what it ended up as.

    An empty selection is refused and falls back to the owner. A project with
    nobody on it is not a useful state - it would appear on no dashboard, and
    the agent prompt would have no one to address - and it is a state a person
    can reach with two clicks by unticking the last box. Refusing here costs
    nothing and removes a way to lose a project.
    """
    wanted = {int(i) for i in person_ids}
    wanted &= {int(p["id"]) for p in everyone(include_archived=True)}
    if not wanted:
        wanted = {int(owner()["id"])}
    conn = db.get_conn()
    with db._LOCK:
        conn.execute("DELETE FROM project_people WHERE project_id = ?", (int(project_id),))
        conn.executemany(
            "INSERT OR IGNORE INTO project_people (project_id, person_id, added_at) "
            "VALUES (?, ?, ?)",
            [(int(project_id), pid, db.now()) for pid in sorted(wanted)],
        )
        conn.commit()
    return sorted(wanted)


def add_member(project_id: int, person_id: int) -> None:
    conn = db.get_conn()
    with db._LOCK:
        conn.execute(
            "INSERT OR IGNORE INTO project_people (project_id, person_id, added_at) "
            "VALUES (?, ?, ?)",
            (int(project_id), int(person_id), db.now()),
        )
        conn.commit()


def principal(project_id: Optional[int]) -> sqlite3.Row:
    """The person a run on this project works on behalf of.

    The install owner - unless the project has members and the owner is not one
    of them, in which case it is the project's first member. That is what makes
    a project Karli created hers in the agent's eyes and not just in the
    dashboard's: before this, the contract was rendered once at import with the
    owner's name, so a run on her project opened with "you are working on
    behalf of Wes" and every question, blocked_on and approval in it pointed at
    him (Wes, 2026-08-06: "it keeps talking to her as if she were me, or as if
    she needed to get me to do stuff").

    "First member" is `members()` order (owner first, then name), so on a
    shared project the owner stays the principal and nothing about an existing
    install shifts; only a project the owner is not on changes hands.
    """
    if project_id is not None:
        folk = members(int(project_id))
        if folk and not any(int(p["is_owner"] or 0) for p in folk):
            return folk[0]
    return owner()


def template_vars_for(person: Optional[sqlite3.Row]) -> dict[str, str]:
    """`config.SITE.template_vars()`, re-addressed to this person.

    The machine half ($HOST, $BASE_URL, $PORTAL_ROOT) is always the install's;
    the person half ($OWNER and the pronouns) is swapped for the given person
    when they are not the install owner. For the owner (or None) this returns
    the site vars untouched, byte for byte, so every existing prompt renders
    exactly as it always has.
    """
    vars = config.SITE.template_vars()
    if person is None:
        return vars
    try:
        if int(person["is_owner"] or 0):
            return vars
    except (IndexError, KeyError):
        return vars
    name = name_of(person)
    they, them, their, theirs = pronouns_of(person)
    vars.update(
        OWNER=name, OWNERS=possessive(name),
        THEY=they, THEM=them, THEIR=their, THEIRS=theirs,
    )
    return vars


def project_ids_for(person_id: int) -> set[int]:
    return {
        int(r["project_id"])
        for r in db.get_conn().execute(
            "SELECT project_id FROM project_people WHERE person_id = ?", (int(person_id),)
        )
    }


def members_by_project() -> dict[int, list[sqlite3.Row]]:
    """Every project's members in one query, for the dashboard.

    The dashboard renders up to a hundred cards; a `members()` call per card is
    a hundred queries for a page that is already the most-loaded one here.
    """
    out: dict[int, list[sqlite3.Row]] = {}
    for row in db.get_conn().execute(
        "SELECT pp.project_id AS project_id, p.* FROM project_people pp "
        "JOIN people p ON p.id = pp.person_id "
        "ORDER BY p.is_owner DESC, p.name COLLATE NOCASE, p.id"
    ):
        out.setdefault(int(row["project_id"]), []).append(row)
    return out


# ---------------------------------------------------------------------------
# Which of them is holding the phone?
# ---------------------------------------------------------------------------

def resolve(
    cookie_slug: str = "",
    tailnet_login: str = "",
) -> sqlite3.Row:
    """Who is making this request. Never None.

    The precedence, and why it is this way round:

    1. **The cookie.** Somebody who has said "I am Erin" on this device has
       made a statement about themselves, and nothing the network can infer
       should be allowed to overrule it. This is also the only mechanism that
       works everywhere the portal is reachable - the LAN address, the Tailscale
       address, and a Cloudflare tunnel - which matters because Wes reads the
       board from his phone off wifi.
    2. **The tailnet login.** `tailscale whois` on the connecting address gives
       the login that owns that *node*, with no password and no setup, so once
       two people are two tailnet users the portal knows them apart on sight.
       It is second rather than first because it is a property of the device's
       owner, not of who is typing: today Wes's wife is signed in under his
       tailnet user, so whois would confidently call her Wes.
    3. **The owner.** One person, no cookie yet, nothing to infer from - which
       is every request this portal has ever served until now, and must keep
       behaving exactly as it did.

    An archived person still resolves from a live cookie. Archiving retires
    somebody from the pickers; it is not a lockout, and silently becoming a
    different person mid-session is worse than seeing a grayed-out name.
    """
    person = by_slug(cookie_slug)
    if person is not None:
        return person
    person = by_tailnet_login(tailnet_login)
    if person is not None:
        return person
    return owner()


# ip -> (login, when). `tailscale whois` is a subprocess talking to a local
# daemon; a few milliseconds is nothing once, and everything on a page that
# refreshes itself every few seconds to stay live.
_WHOIS_CACHE: dict[str, tuple[str, float]] = {}
_WHOIS_TTL = 300.0


def tailnet_login_cached(ip: str) -> str:
    """`tailnet_login_for` with a five-minute memory, for the request path.

    A device's tailnet owner does not change between two page loads, and the
    consequence of a stale answer is bounded: the wrong *hint*, which the
    cookie outranks anyway.
    """
    now_s = time.monotonic()
    hit = _WHOIS_CACHE.get(ip)
    if hit is not None and now_s - hit[1] < _WHOIS_TTL:
        return hit[0]
    login = tailnet_login_for(ip)
    _WHOIS_CACHE[ip] = (login, now_s)
    return login


def tailnet_login_for(ip: str) -> str:
    """The tailnet login owning the node at `ip`, or ''.

    Fails soft in every direction - no tailscale binary, a LAN address that is
    not on the tailnet, a daemon that is not answering - because this is one
    hint feeding `resolve`, and the fallback (the owner) is exactly what the
    portal did before people existed. It must never be able to fail a request.
    """
    ip = (ip or "").strip()
    if not ip or ip.startswith("127.") or ip == "::1":
        return ""
    try:
        from app import netinfo

        raw = netinfo._tailscale("whois", "--json", ip)
    except Exception:  # pragma: no cover - defensive
        log.debug("whois lookup failed for %s", ip, exc_info=True)
        return ""
    if not isinstance(raw, dict):
        return ""
    profile = raw.get("UserProfile")
    if not isinstance(profile, dict):
        return ""
    return str(profile.get("LoginName") or "").strip().lower()


# ---------------------------------------------------------------------------
# The learned half of a background
# ---------------------------------------------------------------------------
#
# `background` is what a person typed about themselves, and no agent may ever
# write it. This is the other half: what working with them has actually shown,
# grown by the daily reflect job from the notes and answers they wrote.
#
# The two halves are stored apart rather than merged into one field, and that
# separation is the whole design:
#
# - Wes's own sentence can never be quietly replaced by a rewrite. That exact
#   failure is why app/memory.py exists at all ("Wes typed things about himself
#   into the profile that a later reflect quietly replaced"), and the cheapest
#   fix is to give the agent somewhere else to write.
# - The learned half is disposable. If it goes wrong it is one button on
#   /memory, and what a person said about themselves survives untouched.
#
# It lives as a file in MEMORY_DIR because MEMORY_DIR is the *cwd* of the
# reflect agent: growing it is then an ordinary file write, with no new field
# on the report schema, no parsing of a model's prose, and no contract text
# taxing the prompt of every other run.
LEARNED_DIRNAME = "people"

# What one person's learned file may contribute to a prompt. It is injected
# into every run of every project they are on, so an unbounded file is an
# unbounded tax - and a reflect agent that ignores its line limit must degrade
# to "a bit is dropped", never to "every prompt grows forever".
LEARNED_PROMPT_CHARS = 1200

# What the reflect agent is asked to stay under. Deliberately well inside the
# render cap above, so hitting the cap means the agent misbehaved rather than
# being a routine occurrence nobody notices.
LEARNED_MAX_LINES = 8


def learned_dir() -> Path:
    """Resolved per call, never bound at import: config's paths are module
    constants the test fixture repoints at a throwaway directory, and caching
    them would write into the live data directory from a test run. Same
    posture as app/memory.py, for the same reason."""
    return config.MEMORY_DIR / LEARNED_DIRNAME


def learned_path(slug: str) -> Optional[Path]:
    """Where one person's learned file sits, or None for a slug that could
    escape the directory.

    The slug arrives from a URL on the clear route, so it is shape-checked
    here rather than at each call site - `slugify` already restricts what a
    real slug can contain, so anything failing this check is not a person."""
    slug = (slug or "").strip().lower()
    if not slug or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        return None
    return learned_dir() / f"{slug}.md"


def read_learned(slug: str) -> str:
    """What has been learned about this person, or "". Fails soft in every
    direction - a missing directory, an unreadable file, a bad slug - because
    this feeds a prompt, and no memory file is worth failing a run over."""
    path = learned_path(slug)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def clear_learned(slug: str) -> bool:
    """Throw away what has been learned about somebody. Wes-only, a button on
    /memory: the file is an agent's inference about a person, and this is how
    it gets overruled. Their hand-written `background` is untouched - it is in
    the database and was never this file's to lose."""
    path = learned_path(slug)
    if path is None:
        return False
    try:
        if not path.is_file():
            return False
        path.unlink()
        return True
    except OSError:
        return False


def learned_overview() -> list[dict]:
    """Both halves of every person's background, for /memory.

    Driven off `everyone()` rather than off the directory listing, so a file
    left behind by a deleted person is never shown. It stays on disk on
    purpose: deleting somebody should not silently reach into the memory
    directory, and an orphan there costs nothing but a few hundred bytes.

    People with nothing learned yet are included, because "the reflect has not
    worked this person out yet" is the answer to the question the page is
    being asked, and an absence you can see beats a row that is missing."""
    out: list[dict] = []
    for person in everyone():
        slug = str(person["slug"])
        try:
            said = (person["background"] or "").strip()
        except (IndexError, KeyError):  # pragma: no cover - defensive
            said = ""
        learned = read_learned(slug)
        out.append(
            {
                "slug": slug,
                "name": name_of(person),
                "background": said,
                "learned": learned,
                "lines": [ln for ln in learned.splitlines() if ln.strip()],
            }
        )
    return out


def _learned_for_prompt(slug: str) -> str:
    """One person's learned lines, trimmed to the render cap at a line
    boundary. Trimming mid-sentence would put a half-claim about a person into
    a prompt, which is worse than dropping the line entirely."""
    text = read_learned(slug)
    if not text:
        return ""
    if len(text) <= LEARNED_PROMPT_CHARS:
        return text
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        if used + len(line) + 1 > LEARNED_PROMPT_CHARS:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept).strip()


# ---------------------------------------------------------------------------
# What the agent is told
# ---------------------------------------------------------------------------

def describe(person: sqlite3.Row, role: str = "") -> str:
    """One person, as a bullet in the prompt."""
    they, them, their, _ = pronouns_of(person)
    bits = [f"- **{name_of(person)}** ({they}/{them})"]
    if role:
        bits.append(f" - {role}")
    background = ""
    try:
        background = (person["background"] or "").strip()
    except (IndexError, KeyError):
        pass
    line = "".join(bits)
    if background:
        line += f"\n  {background}"
    learned = ""
    try:
        learned = _learned_for_prompt(str(person["slug"]))
    except (IndexError, KeyError):  # pragma: no cover - a row without a slug
        pass
    if learned:
        # Labeled as observed, not stated. An agent that reads "she knows what
        # a commit is" has to be able to tell whether she said so or whether a
        # previous run decided it, because only one of those is worth trusting
        # against her own words on the line above.
        # Phrased to avoid conjugating a verb to the pronoun: "what she wrote"
        # reads correctly for he, she and they alike, where "has written" does
        # not, and a person's own pronouns are not a place to be sloppy.
        line += (
            f"\n  Noticed while working with {them}, not stated by {them} - "
            f"inferred from what {they} wrote, so treat it as a working guess "
            f"and let {them} correct it:"
        )
        for entry in learned.splitlines():
            line += "\n  " + entry.rstrip()
    return line


def prompt_section(project_id: Optional[int]) -> str:
    """The `## People` block, or '' when there is only ever one person.

    The empty return is the point of this function. A single-person install -
    which is every install until somebody adds a second person - gets no block
    at all, so the prompt is byte-for-byte what it was and none of the existing
    behavior shifts under a feature nobody is using. The section appears the
    moment there is genuinely more than one person to tell apart.
    """
    folk = everyone()
    if len(folk) < 2:
        return ""
    on_project = member_ids(project_id) if project_id is not None else set()
    lines = [
        "## People",
        "",
        "More than one person uses this portal. They share this project's whole "
        "context - one journal, one todo list, one run history - but they are "
        "different people, so read who wrote a thing before answering it.",
        "",
    ]
    for person in folk:
        role = ""
        if project_id is not None:
            role = "on this project" if int(person["id"]) in on_project else "not on this project"
        lines.append(describe(person, role))
    lines += [
        "",
        "Pitch each answer at the person you are answering. If a note came from "
        "someone the notes above say is newer to this, explain the thing rather "
        "than naming it, and say why it works that way - teaching them something "
        "they would not otherwise have run into is part of the job, not a "
        "digression. If it came from someone experienced, do not pad.",
        "",
        "Your report is read by all of them, so write the summary bullets so "
        "they land for whoever opens the page.",
    ]
    return "\n".join(lines)


# How much of one journal entry the reflect job sees as evidence. Long enough
# for a note to keep its shape, short enough that a dozen of them across every
# person still leave room for the rest of the reflect prompt.
EVIDENCE_CHARS = 600

# How many of a person's own entries to show. Newest-first, so a person who has
# been here a year is judged on this week rather than on their first day.
EVIDENCE_LIMIT = 25


def reflect_section() -> str:
    """The `## What each person understands` block for the daily reflect.

    Empty on a single-person install, exactly like `prompt_section` and for the
    same reason: nothing about the reflect changes until there is a second
    person to tell apart. The owner is included once there is - "what Wes
    already knows" is as much a thing to pitch at as anybody else's, and
    leaving him out would make the one file that is never maintained the one
    belonging to the person who uses the portal most.

    Each person gets three things: what they said about themselves (read-only
    here), what previous reflects concluded, and the words they actually wrote.
    The third is the point. Everything else in this prompt is somebody's
    opinion; this is the evidence, and the guidance tells the agent to write
    nothing that is not visible in it.
    """
    folk = everyone()
    if len(folk) < 2:
        return ""
    lines = [
        "## What each person understands",
        "",
        "You also maintain one short file per person, at "
        f"`{LEARNED_DIRNAME}/<slug>.md` in your cwd. Every run of every "
        "project they are on reads it, directly under what that person wrote "
        "about themselves, so it is how a future agent knows whether to "
        "explain a concept or just name it.",
        "",
        "Rules, in the order they matter:",
        "",
        "1. **Write only what the evidence below actually shows.** Each line "
        "must be traceable to something the person typed. If somebody has "
        "written nothing new, leave their file exactly as it is - a reflect "
        "that finds nothing should change nothing.",
        "2. **Only write what would change how an agent EXPLAINS something to "
        "them.** \"Now knows what a git commit is, so it can be named rather "
        "than explained\" earns its place. \"Interested in the portal\" does "
        "not - it changes no sentence anyone would write.",
        "3. **Do not touch what they said about themselves.** That is the "
        "`Says about themselves` line below, it lives in the database, and it "
        "is theirs. If the evidence contradicts it, say so in the file as a "
        "line of its own; do not try to overrule it.",
        f"4. **At most {LEARNED_MAX_LINES} lines each**, one plain sentence "
        "per line, written as a markdown list. Past that the file is trimmed "
        "when it reaches a prompt and your last lines are silently lost.",
        "5. **Drop what has been overtaken.** Once somebody has clearly "
        "learned a thing, \"new to it\" is no longer true and keeping it "
        "makes every future agent talk down to them.",
        "6. Never write anything you would not be comfortable with that "
        "person reading, because they can - it is on the /memory page with a "
        "button to throw it away.",
        "",
    ]
    for person in folk:
        slug = str(person["slug"])
        lines.append(f"### {name_of(person)} (`{slug}`)")
        lines.append("")
        said = ""
        try:
            said = (person["background"] or "").strip()
        except (IndexError, KeyError):
            pass
        lines.append(f"Says about themselves: {said or '(nothing)'}")
        lines.append("")
        current = read_learned(slug)
        lines.append(
            f"Current `{LEARNED_DIRNAME}/{slug}.md`:\n\n"
            + (current if current else "(the file does not exist yet)")
        )
        lines.append("")
        try:
            writings = db.list_person_writings(int(person["id"]), EVIDENCE_LIMIT)
        except Exception:  # pragma: no cover - defensive; evidence is optional
            log.exception("Could not read what %s has written", slug)
            writings = []
        if writings:
            lines.append(f"What {name_of(person)} has written, newest first:")
            lines.append("")
            for row in writings:
                body = " ".join((row["content_md"] or "").split())[:EVIDENCE_CHARS]
                where = row["project_title"] or "no project"
                lines.append(f"- [{row['ts']}] ({where}) {row['kind']}: {body}")
        else:
            lines.append(
                f"{name_of(person)} has written nothing the portal recorded "
                "against them, so there is no evidence and their file must "
                "not change."
            )
        lines.append("")
    return "\n".join(lines).rstrip()

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

## What "the model recognises us" actually needs

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
from typing import Iterable, Mapping, Optional, Sequence

from app import config, db, site

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


def pronouns_of(person: Optional[sqlite3.Row]) -> tuple[str, str, str, str]:
    """(they, them, their, theirs) for a person, defaulting to they/them."""
    raw = ""
    if person is not None:
        try:
            raw = person["pronouns"] or ""
        except (IndexError, KeyError):
            raw = ""
    return site.pronoun_forms(raw)


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


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

DEFAULT_OWNER_BACKGROUND = (
    "Set this portal up and has been using it alone until now. Assume real "
    "software experience."
)


def ensure_owner() -> int:
    """Create the owner row from the site config, and keep it in step with it.

    The name and the pronouns are the site config's, not this table's, and that
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
    pronouns = site.pronoun_key(config.SITE.pronouns)
    if row is None:
        person_id = add(
            name=name,
            pronouns=pronouns,
            background=DEFAULT_OWNER_BACKGROUND,
            is_owner=True,
        )
        log.info("Created the owner person %r (id=%s)", name, person_id)
        return person_id
    if row["name"] != name or row["pronouns"] != pronouns:
        with db._LOCK:
            conn.execute(
                "UPDATE people SET name = ?, pronouns = ?, slug = ? WHERE id = ?",
                (name, pronouns, unique_slug(name, exclude_id=int(row["id"])), int(row["id"])),
            )
            conn.commit()
        log.info("The owner is now %r (was %r), following the site config", name, row["name"])
    return int(row["id"])


def add(
    name: str,
    pronouns: str = site.DEFAULT_PRONOUNS,
    background: str = "",
    tailnet_login: str = "",
    is_owner: bool = False,
) -> int:
    conn = db.get_conn()
    clean_name = (name or "").strip() or "Someone"
    with db._LOCK:
        cur = conn.execute(
            "INSERT INTO people (slug, name, pronouns, background, tailnet_login, "
            "is_owner, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                unique_slug(clean_name),
                clean_name,
                site.pronoun_key(pronouns),
                (background or "").strip(),
                (tailnet_login or "").strip().lower(),
                1 if is_owner else 0,
                db.now(),
            ),
        )
        conn.commit()
    return int(cur.lastrowid)


_EDITABLE = ("name", "pronouns", "background", "tailnet_login")


def update(person_id: int, **fields) -> None:
    """Edit a person. Renaming re-slugs, which is why this is not a generic
    UPDATE: the slug is what the cookie holds, so it has to be reissued
    together with the name or a rename would log that person out of their own
    identity.

    The owner's name and pronouns are silently ignored here rather than
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
        if is_owner and key in ("name", "pronouns"):
            continue
        value = fields[key]
        if key == "pronouns":
            value = site.pronoun_key(value)
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
    merged = {**appearance_of(person), **_valid_appearance(values)}
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
    different person mid-session is worse than seeing a greyed-out name.
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
    return line


def prompt_section(project_id: Optional[int]) -> str:
    """The `## People` block, or '' when there is only ever one person.

    The empty return is the point of this function. A single-person install -
    which is every install until somebody adds a second person - gets no block
    at all, so the prompt is byte-for-byte what it was and none of the existing
    behaviour shifts under a feature nobody is using. The section appears the
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

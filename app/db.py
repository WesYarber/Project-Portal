"""SQLite persistence layer for Project Portal.

Single-user app: one shared connection guarded by a threading.Lock is
sufficient (check_same_thread=False so it can be used from the asyncio
worker thread pool as well as request handlers).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, NamedTuple, Optional, Sequence

from app import config, qdedupe

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA foreign_keys = ON")
    return _CONN


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    -- What Wes typed when he first had the idea. `description` drifts as the
    -- agent rewrites it to match what the project has become; this never
    -- changes, so the original intent stays in the prompt forever.
    initial_idea TEXT DEFAULT '',
    description_locked INTEGER NOT NULL DEFAULT 0,
    title_locked INTEGER NOT NULL DEFAULT 0,
    -- Has Wes okayed building this? Agents can triage and plan on their own,
    -- but writing code is gated on this being 1 (see app/worker.py). Set when
    -- he moves a project to `building` himself, or presses "approve build".
    build_approved INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'unknown',
    -- The user-owned lifecycle: backlog | active | review | done | abandoned.
    -- One of exactly the five things Wes ever chooses, and exactly the five
    -- dashboard shelves. Everything else that used to hide inside the old
    -- status enum lives in its own column below (see docs/state-model.md).
    stage TEXT NOT NULL DEFAULT 'backlog',
    -- When Wes put this project down, or NULL. Orthogonal to stage and only
    -- ever set by him: a paused project is never scheduled and folds to the
    -- Paused shelf whatever its stage.
    paused TEXT,
    -- The agent's "plan is ready, may I write code?". Cleared by approval,
    -- revocation, or Wes moving the stage himself.
    build_requested INTEGER NOT NULL DEFAULT 0,
    -- The agent's "I need something from Wes" (a purchase, a credential, a
    -- click). Cleared automatically when the next run on the project reports,
    -- so it cannot go stale silently.
    blocked_on TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    max_runs_per_day INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id),
    ts TEXT NOT NULL,
    author TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_md TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    ts TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    answer TEXT,
    answered_at TEXT,
    telegram_msg_id INTEGER
);

-- A working checklist per project, split by who has to do the thing. The
-- agent's half is written into every run prompt (see app/todos.py) so a
-- request Wes made ten runs ago doesn't fall off the end of a context window;
-- the user's half is the list of things only he can unblock.
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    owner TEXT NOT NULL DEFAULT 'agent',
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    done_at TEXT,
    -- When a completed item stopped being shown in the live list. Set by the
    -- "clear completed" button; a completed item also drops out on its own
    -- after TODO_DONE_TTL_HOURS. Either way the row stays, so the history
    -- view can still show everything that was ever ticked off.
    cleared_at TEXT,
    -- Comma-separated short labels ("blocked,ready-to-build"). Free-form,
    -- except that 'blocked' has mechanical meaning: an open agent item wearing
    -- it does not count as workable when the scheduler decides whether a run
    -- could do anything but repeat itself. See count_workable_todos.
    tags TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id),
    task TEXT NOT NULL,
    model TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    session_id TEXT,
    cost_usd REAL,
    num_turns INTEGER,
    summary TEXT,
    -- The one-line `summary` field from the agent's report.json, as opposed to
    -- `summary` above which is whatever the CLI printed last. This is the line
    -- shown in the "since you last looked" banner on the project page.
    report_summary TEXT,
    events INTEGER NOT NULL DEFAULT 0,
    last_activity TEXT,
    last_event_at TEXT,
    -- The one-off task this run belongs to, for runs that have no project.
    -- See the oneoff_tasks table below and app/oneoff.py.
    oneoff_id INTEGER
);

CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed'
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Files dropped onto a project (images, audio, video, anything). The bytes live
-- in the project workspace under attachments/; this table is the index. See
-- app/attachments.py. journal_id is the note the file was attached to, and is
-- NULL for a file uploaded without one.
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    journal_id INTEGER REFERENCES journal(id),
    created_at TEXT NOT NULL,
    orig_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    mime TEXT NOT NULL DEFAULT 'application/octet-stream',
    size INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

-- One-off agent tasks: a scratch chat session with an agent, for work that
-- does not deserve a whole project. Each gets a workspace under data/tasks/
-- and keeps conversational continuity across runs through the Claude CLI's
-- own session (cli_session_id + --resume), so a follow-up message reaches an
-- agent that remembers the whole exchange. See app/oneoff.py.
CREATE TABLE IF NOT EXISTS oneoff_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',  -- open | archived
    cli_session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- The exchange inside a one-off task. role is 'wes', 'agent' or 'system'.
-- delivered_at plays the same part journal.delivered_at plays for notes: NULL
-- on a wes message means no agent has seen it yet, so it is still queued for
-- the next run (and several typed in a row arrive as one batch).
CREATE TABLE IF NOT EXISTS oneoff_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES oneoff_tasks(id),
    role TEXT NOT NULL,
    content_md TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    delivered_at TEXT,
    ts TEXT NOT NULL
);

-- Audit trail of the run hooks (app/hookguard.py): one row per tool call the
-- PreToolUse guardrail denied, one per Stop-hook bounce of a run trying to
-- finish without its report, and - when the audit toggle is on - one
-- 'post_tool_use' row per tool call a run makes, so a run's page can answer
-- "what did this run actually do" after its transcript is pruned. Audit rows
-- are capped per run and aged out after AUDIT_RETENTION_DAYS; denials and
-- bounces are kept forever.
CREATE TABLE IF NOT EXISTS hook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id),
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    tool TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    detail TEXT
);

-- Web-push subscriptions: one row per enrolled device (Wes's phone, mainly).
-- The endpoint is the push service URL the browser handed out and is the
-- device's identity - re-enrolling the same device replaces its keys in
-- place. Rows are dropped when the push service says the subscription is
-- gone (404/410). See app/webpush.py.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    ua TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_ok_at TEXT,
    failures INTEGER NOT NULL DEFAULT 0
);

-- The people who use this portal. Wes, 2026-07-28: "would it be feasible to
-- add additional users that can have their own projects? ... They should be
-- able to belong to multiple users who can each prompt separately while using
-- the same context of work history and whatnot."
--
-- Note what is NOT here: no password, no email, no role. This is not an auth
-- system and does not pretend to be one - it is a way for the portal (and the
-- agent) to tell apart the two or three people who share a house and a home
-- server. See app/people.py for the identity rule and why it is that way round.
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Stable handle. This is the cookie value, so it must survive a database
    -- restore; it is reissued (together with the name) on a rename.
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    -- One of `they` / `he` / `she`, normalised by site.pronoun_key.
    pronouns TEXT NOT NULL DEFAULT 'they',
    -- What the agent should assume this person already knows, in their own
    -- words. Free text on purpose rather than a level enum: "newer to
    -- self-hosting, teach the concepts" tells a model something a 1-5 scale
    -- cannot, and it is a sentence somebody can grow as they learn.
    background TEXT NOT NULL DEFAULT '',
    -- The `tailscale whois` LoginName that identifies this person on sight,
    -- or ''. Only useful once two people are two tailnet users.
    tailnet_login TEXT NOT NULL DEFAULT '',
    -- This person's own look: a JSON object of the appearance keys they have
    -- picked, '' while they have picked none. See people.appearance_of for the
    -- fallback chain (person -> the install's setting -> the shipped default).
    appearance TEXT NOT NULL DEFAULT '',
    -- The person the install was set up for (config's OWNER). Exactly one row
    -- carries it, and it buys one thing only: people.owner() can never return
    -- None, so there is always somebody to attribute a note to.
    is_owner INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    -- Retired from the pickers without deleting what they wrote.
    archived_at TEXT
);

-- Which projects are whose. A project can have several members who each prompt
-- it separately and share all of its context, so this is a plain join table and
-- emphatically NOT a tenant column: nothing about a journal, a todo or a run is
-- partitioned by person.
CREATE TABLE IF NOT EXISTS project_people (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (project_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_project_people_person ON project_people(person_id);
"""


# Columns added after v1. `init_db` adds any that are missing so an existing
# portal.db upgrades in place.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "projects": [
        # The state-model redesign's columns, for a portal.db that predates it.
        # The old `status` column is migrated into them (and renamed to
        # `status_old`) by `_migrate_status_to_stage`, which runs right after
        # these ALTERs.
        ("stage", "TEXT NOT NULL DEFAULT 'backlog'"),
        ("paused", "TEXT"),
        ("build_requested", "INTEGER NOT NULL DEFAULT 0"),
        ("blocked_on", "TEXT"),
        ("model", "TEXT"),
        ("max_runs_per_day", "INTEGER"),
        ("initial_idea", "TEXT DEFAULT ''"),
        ("description_locked", "INTEGER NOT NULL DEFAULT 0"),
        ("title_locked", "INTEGER NOT NULL DEFAULT 0"),
        ("build_approved", "INTEGER NOT NULL DEFAULT 0"),
        # Set once Wes has had his say about the folder name - either by
        # renaming it himself or by dismissing the suggestion. See
        # `suggested_slug`.
        ("slug_locked", "INTEGER NOT NULL DEFAULT 0"),
        # When Wes last pressed "acknowledged" on the work summary. Run
        # summaries newer than this are what the banner shows; NULL means he
        # has never acknowledged, which is harmless because no run recorded a
        # report_summary before this feature existed.
        ("work_ack_at", "TEXT"),
        # When Wes queued this project for a heavy research burst. The burst
        # only runs inside a spend-down window (see app/pacing.py), so this is
        # a standing "when there is spare weekly allowance, go and read about
        # this" rather than a request for a run now. Cleared the moment the
        # research run starts, so one queueing buys one burst.
        ("research_queued_at", "TEXT"),
        # The project this one was split out of. A project like "Board games
        # for my site" is really six games that each want their own
        # context, their own workspace and their own run history - so each one
        # becomes a project in its own right, with this pointing back at the
        # parent. See app/subprojects.py.
        #
        # Deliberately NOT declared REFERENCES: it is added by ALTER TABLE on
        # every existing portal.db, and SQLite cannot add a column with a
        # foreign key constraint to a populated table. The invariants that
        # matter (one level deep, no cycles, no orphans) are enforced in
        # subprojects.py and checked by tests, which is where the useful error
        # messages live anyway.
        ("parent_id", "INTEGER"),
        # Where to click to see what this project built, when the portal cannot
        # work it out by itself. Empty for the common case - a workspace with an
        # index.html in it is served by the preview server without anyone
        # declaring anything. Set by Wes, or by an agent that has just bound a
        # port and is the only one who knows the number. See app/preview.py.
        ("preview_url", "TEXT NOT NULL DEFAULT ''"),
    ],
    "runs": [
        ("events", "INTEGER NOT NULL DEFAULT 0"),
        ("last_activity", "TEXT"),
        ("last_event_at", "TEXT"),
        ("report_summary", "TEXT"),
        ("oneoff_id", "INTEGER"),
    ],
    # The short number a question is referred to by ("Q7"). Recycled: see
    # `next_question_slot`. NULL once the question is answered or dismissed.
    # quick_options: JSON list of one-tap answers offered on the Telegram
    # message ('' = none); frozen at creation so a tapped index always
    # resolves to the text that was actually on the button. See quickreplies.
    "questions": [("slot", "INTEGER"), ("quick_options", "TEXT NOT NULL DEFAULT ''")],
    "todos": [
        ("cleared_at", "TEXT"),
        ("tags", "TEXT NOT NULL DEFAULT ''"),
    ],
    # When this entry was rendered into a prompt an agent actually received.
    # NULL means "written, but no model has seen it yet" - which is exactly the
    # window in which one of Wes's notes is still his to edit or take back. Set
    # by app/notes.py at the moment the note goes into a prompt, never before.
    # Only meaningful for user notes; everything else is back-filled at creation
    # (see below) so it can never masquerade as pending.
    # Who wrote this entry, once more than one person could have. NULL means
    # "written before people existed, or written by the portal itself" - the
    # backfill in init_db turns the first kind into the owner and leaves the
    # second alone, because an agent's progress report has no person behind it.
    #
    # Deliberately NOT declared REFERENCES, for the same reason as
    # projects.parent_id: SQLite cannot add a column with a foreign key to a
    # populated table, and this is added by ALTER on every existing portal.db.
    "journal": [("delivered_at", "TEXT"), ("person_id", "INTEGER")],
    # This person's own look, as a JSON object of appearance keys they have
    # chosen (see config.APPEARANCE_CHOICES). Wes, 2026-07-28: "It would be
    # cool as well if she was able to customize the theme of the site for her
    # user to her liking."
    #
    # A JSON blob rather than one column per layer, because the set of layers
    # grows - scanlines, glow, animations, typeface and density are five today
    # and were three in June - and a schema migration per look-and-feel option
    # would make adding one expensive enough that nobody would.
    #
    # '' means "has chosen nothing", which is NOT the same as "has chosen the
    # defaults": an empty override set follows the install's look as it
    # changes, while an explicit choice pins it. See people.appearance_of.
    "people": [("appearance", "TEXT NOT NULL DEFAULT ''")],
}


def init_db() -> None:
    """Create schema if missing and seed data on first creation."""
    is_new = not config.DB_PATH.exists()
    conn = get_conn()
    with _LOCK:
        conn.executescript(SCHEMA)
        added: set[tuple[str, str]] = set()
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    added.add((table, name))

        # The state-model migration: fold the old eight-value status enum into
        # stage + paused + build_requested + blocked_on. Runs exactly once -
        # renaming `status` to `status_old` is both the migration's last step
        # and its guard, and it makes any straggler still reading
        # project["status"] fail loudly rather than read a stale value.
        if "status" in {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}:
            _migrate_status_to_stage(conn)

        # Every journal entry that existed before delivery was tracked has been
        # seen by an agent (or is not a note at all), so it is delivered. This
        # runs ONLY on the migration that adds the column: as an unconditional
        # "WHERE delivered_at IS NULL" it would fire on every startup and mark
        # a note Wes wrote thirty seconds ago as already sent - silently taking
        # away the edit window the column exists to provide.
        if ("journal", "delivered_at") in added:
            conn.execute("UPDATE journal SET delivered_at = ts WHERE delivered_at IS NULL")

        # Back-fill the original idea for projects created before the column
        # existed. Until now `description` *was* the idea as Wes typed it and
        # nothing else ever wrote to it, so copying it across is exact rather
        # than a guess - and it has to happen before the first agent run gets
        # the chance to rewrite the description out from under it.
        conn.execute(
            "UPDATE projects SET initial_idea = COALESCE(description, '') "
            "WHERE initial_idea IS NULL OR initial_idea = ''"
        )

        # Scrub control characters out of titles stored before `clean_title`
        # existed. Cheap, idempotent, and it fixes the trailing `\r` a pasted
        # title left on at least one real project.
        for row in conn.execute("SELECT id, title FROM projects").fetchall():
            cleaned = clean_title(row["title"])
            if cleaned and cleaned != row["title"]:
                conn.execute("UPDATE projects SET title = ? WHERE id = ?", (cleaned, row["id"]))

        # Back-fill question slots for a database created before they existed,
        # oldest question first so the numbering matches the order they were
        # asked in. Anything already answered or dismissed stays NULL.
        unslotted = conn.execute(
            "SELECT id FROM questions WHERE status = 'open' AND slot IS NULL ORDER BY ts ASC, id ASC"
        ).fetchall()
        if unslotted:
            taken = {
                row["slot"]
                for row in conn.execute(
                    "SELECT slot FROM questions WHERE status = 'open' AND slot IS NOT NULL"
                )
            }
            for row in unslotted:
                slot = 1
                while slot in taken:
                    slot += 1
                taken.add(slot)
                conn.execute("UPDATE questions SET slot = ? WHERE id = ?", (slot, row["id"]))

        # Reconcile runs orphaned by a mid-run service stop: nothing can still
        # be running at startup, and a stuck 'running' row would block the
        # worker forever via is_run_running().
        conn.execute(
            "UPDATE runs SET status = 'error', ended_at = ?, "
            "summary = 'Orphaned: the service stopped while this run was in progress.' "
            "WHERE status = 'running'",
            (now(),),
        )
        conn.commit()

    for key, value in config.DEFAULT_SETTINGS.items():
        if get_setting(key) is None:
            set_setting(key, value)

    _backfill_build_approval()

    if is_new:
        _seed_data()

    _backfill_people()


def _backfill_people() -> None:
    """Everything that existed before people did belongs to the owner.

    Wes, 2026-07-28: "All current projects should be assigned to me, and they
    should be able to be reassigned if desired."

    Two halves, and the second is why this needs a flag rather than being a
    plain idempotent statement:

    - `ensure_owner` runs unconditionally, because a portal with no owner has
      nobody to attribute the next note to. It writes only when there is no
      owner, so it is free on every boot after the first.
    - The membership and journal backfills run ONCE. "Reassigned if desired"
      means somebody may take themselves off a project on purpose, and an
      unconditional `INSERT OR IGNORE ... SELECT id FROM projects` would put
      them back on the next restart - the portal overruling a decision a person
      made, which is the same bug class as the `delivered_at` backfill note
      above.

    Only `author = 'user'` journal rows are attributed. An agent's progress
    entry and a system status line have no person behind them, and stamping the
    owner on them would make the byline meaningless everywhere it appears.
    """
    from app import people  # local: people imports db, so this cannot be top-level

    people.ensure_owner()
    if get_setting(people.BACKFILL_KEY) == "1":
        return
    owner_id = int(people.owner()["id"])
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "INSERT OR IGNORE INTO project_people (project_id, person_id, added_at) "
            "SELECT id, ?, ? FROM projects",
            (owner_id, now()),
        )
        conn.execute(
            "UPDATE journal SET person_id = ? WHERE person_id IS NULL AND author = 'user'",
            (owner_id,),
        )
        conn.commit()
    set_setting(people.BACKFILL_KEY, "1")


def _migrate_status_to_stage(conn: sqlite3.Connection) -> None:
    """Fold the pre-redesign `status` column into the new model, one row at a
    time, then rename it to `status_old` (kept for one release as the hedge).

    The mechanical table from docs/state-model.md. The one judgement call is
    `waiting_user` without Wes's pause stamp: if the build gate parked it (not
    approved, and the gate's own journal line is on the project) it becomes a
    build request; otherwise it is an agent blocked on Wes, which the old model
    could only say by pointing at the journal - so the new column says exactly
    that.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    has_pause_stamp = "paused_by_user" in cols
    for row in conn.execute("SELECT * FROM projects").fetchall():
        status = row["status"]
        stage = config.LEGACY_STATUS_STAGE.get(status, "backlog")
        updates: dict[str, Any] = {}
        if status == "waiting_user":
            stage = "active"
            stamp = row["paused_by_user"] if has_pause_stamp else None
            if stamp:
                updates["paused"] = stamp
            elif not row["build_approved"] and conn.execute(
                "SELECT 1 FROM journal WHERE project_id = ? AND author = 'system' "
                "AND kind = 'status' AND content_md LIKE '%ready to build%' LIMIT 1",
                (row["id"],),
            ).fetchone():
                updates["build_requested"] = 1
            else:
                updates["blocked_on"] = "see the agent's last journal entry"
        updates["stage"] = stage
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE projects SET {sets} WHERE id = ?", (*updates.values(), row["id"]))
    conn.execute("ALTER TABLE projects RENAME COLUMN status TO status_old")


BUILD_APPROVAL_BACKFILL_KEY = "build_approval_backfilled"


def _backfill_build_approval() -> None:
    """Decide, once, which existing projects Wes had already okayed for building.

    The `build_approved` gate arrived after a run of agents had already promoted
    themselves out of triage and started building things Wes never asked for, so
    defaulting every project to "approved" would preserve exactly the behaviour
    the gate exists to stop. Instead the back-fill only trusts evidence that Wes
    himself asked for the build: a `user`-authored status entry moving the
    project into `building` (written by both the web UI and Telegram), plus the
    portal's own project, whose whole point is that it improves itself.

    Guarded by a settings flag rather than by "is the column empty", so
    un-approving a project stays un-approved across restarts.
    """
    if get_setting(BUILD_APPROVAL_BACKFILL_KEY) == "1":
        return
    conn = get_conn()
    with _LOCK:
        conn.execute(
            """UPDATE projects SET build_approved = 1
               WHERE slug = ?
                  OR id IN (SELECT project_id FROM journal
                            WHERE author = 'user' AND kind = 'status'
                              AND content_md LIKE '%-> `building`%')""",
            (config.META_PROJECT_SLUG,),
        )
        conn.commit()
    set_setting(BUILD_APPROVAL_BACKFILL_KEY, "1")


def approve_build(project_id: int) -> None:
    """Wes okays the build. Idempotent. Approval means "go": the request is
    consumed, the stage is active, and a pause (if any) lifts - approving a
    project and leaving it parked would look like the approval didn't take."""
    update_project(
        project_id, build_approved=1, build_requested=0, stage="active", paused=None
    )


def build_approved(project: sqlite3.Row) -> bool:
    try:
        return bool(project["build_approved"])
    except (IndexError, KeyError):  # row from a pre-migration query
        return False


def build_requested(project: sqlite3.Row) -> bool:
    return bool(_row_get(project, "build_requested", 0))


def blocked_on(project: sqlite3.Row) -> str:
    return str(_row_get(project, "blocked_on", "") or "")


def projects_awaiting_build_approval() -> list[sqlite3.Row]:
    """Projects whose agent has asked to start writing code, held for Wes's OK."""
    conn = get_conn()
    order = project_order("updated_at DESC, id DESC")
    with _LOCK:
        return conn.execute(
            "SELECT * FROM projects WHERE build_approved = 0 AND build_requested = 1 "
            f"ORDER BY {order}"
        ).fetchall()


def _row_get(row: sqlite3.Row, key: str, default=None):
    """`row["missing"]` raises on a sqlite3.Row, and test fixtures build rows
    that predate the newer columns, so read defensively."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


# What `slugify` can produce, and therefore the only shape a project folder can
# have. Anything else in a URL segment claiming to be a slug is not one - which
# is what lets the preview server reject a path before it reaches a filesystem
# mount rather than after.
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "idea"


# A folder name long enough to be descriptive and short enough to type after
# `cd`. Titles are already short; this only bites on the occasional subtitled
# one ("Manabase - fast offline MTG life counter (PWA)").
MAX_SLUG_LEN = 48


def slugify_title(title: str) -> str:
    """A slug for a *title*, truncated at a word boundary.

    `slugify` is used on free text Wes types into the rename box and keeps every
    word he wrote. This one is used on generated titles, where the tail is
    usually a parenthetical, so cutting it is a feature rather than data loss.
    """
    slug = slugify(title)
    if len(slug) <= MAX_SLUG_LEN:
        return slug
    head = slug[:MAX_SLUG_LEN + 1]
    cut = head.rsplit("-", 1)[0].strip("-") if "-" in head else ""
    # A title that is one enormous word has no boundary to cut at, so it just
    # gets chopped.
    return cut or slug[:MAX_SLUG_LEN].strip("-") or "idea"


# Words that carry no meaning in a directory name. They are dropped before the
# name is built, which is most of what turns a sentence into a folder:
# "Board games for my website" -> board-games.
SLUG_STOPWORDS = frozenset("""
a an the and or for to of my mine our your his her its their this that these
those with without on in at into from by as is are was be being been it there
here new some any all more most very just about over under using use via
""".split())

# What a directory name should cost you to type. Not a hard cut: the first
# significant word is always kept whole, however long it is, because a name
# chopped mid-word ("silhouett") is worse than a long one.
SHORT_SLUG_BUDGET = 20
SHORT_SLUG_MAX_WORDS = 3
# The longest a single leading word may be before it is cut anyway - past this
# it is not a word, it is a paragraph with the spaces removed.
SHORT_SLUG_HARD_LEN = 30

# Where a title stops being a name and starts being a subtitle. Everything from
# the first of these onwards is dropped: "Manabase - fast offline MTG life
# counter (PWA)" is a project called Manabase.
_SUBTITLE_RE = re.compile(r"\s+[—–-]\s+|[:(\[]")


def short_slug(title: str) -> str:
    """A brief *directory* name for a project, not a hyphenated copy of its title.

    Wes's ask, in his words: "I want the folder name to be a smaller, more brief
    directory name which is more like a directory name than a long project name
    with hyphens between the words."

    So this is deliberately lossy. It drops the subtitle, drops filler words,
    and then takes significant words only while they fit a small budget:

        "Board games for my website"               -> board-games
        "Self-hosted mail on the home server"      -> self-hosted-mail
        "Manabase - fast offline MTG life counter" -> manabase
        "Giftable Home NAS Box"                    -> giftable-home-nas

    A hyphenated compound ("self-hosted", "print-and-cut") counts as one word,
    because splitting it produces a name that reads as a truncation.

    The result is not guaranteed unique; `unique_slug` is what settles that.
    """
    head = _SUBTITLE_RE.split(title or "", 1)[0]
    # Split on whitespace first so hyphenated compounds survive as one token,
    # then clean each token individually. `slugify` is not used here: its empty
    # -> "idea" fallback is right for a whole slug and wrong for one word of one
    # ("Games & Things" would gain a word called "idea").
    words = []
    for word in head.split():
        cleaned = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", word.lower())).strip("-")
        if cleaned:
            words.append(cleaned)
    significant = [w for w in words if w not in SLUG_STOPWORDS]
    # A title made entirely of stopwords ("All About It") still needs a name.
    if not significant:
        significant = words
    if not significant:
        # A title with no letters or digits in it at all ("!!!", "", None).
        return "idea"

    chosen = [significant[0][:SHORT_SLUG_HARD_LEN].strip("-")]
    for word in significant[1:SHORT_SLUG_MAX_WORDS]:
        if len("-".join(chosen)) + 1 + len(word) > SHORT_SLUG_BUDGET:
            break
        chosen.append(word)
    return "-".join(chosen) or "idea"


# A slug this long, or this many words, is not a directory name someone chose -
# it is the raw sentence Wes typed when he had the idea, cut to fit. That, and
# nothing else, is what the portal offers to tidy: a title edit must not start
# proposing a folder move, because folder names no longer track titles.
#
# Tuned against the 17 live projects rather than guessed. At a 24-character
# threshold it also offered to rename `electric-car-cost-calc` and
# `portal-pipeline-smoke-test`, which are both already perfectly good folder
# names - and an offer to "tidy" a name that is already tidy is noise that
# teaches Wes to dismiss the ones that matter. 28 catches exactly the five that
# are still sentences.
UNTIDY_SLUG_LEN = 28
UNTIDY_SLUG_WORDS = 6


def slug_is_untidy(slug: str) -> bool:
    """True if this slug reads like raw idea text rather than a folder name."""
    slug = (slug or "").strip("-")
    if not slug:
        return False
    return len(slug) > UNTIDY_SLUG_LEN or slug.count("-") + 1 >= UNTIDY_SLUG_WORDS


def clean_title(title: str) -> str:
    """Normalise a title before it is stored.

    Titles arrive from three places that can all carry junk: a browser form (a
    stray `\\r` from a pasted line), a Telegram message, and an agent's JSON
    report. A control character in there is invisible in the UI but shows up in
    notification text and in `ssh` commands, so it is stripped at the one point
    every path goes through.
    """
    title = re.sub(r"[\x00-\x1f\x7f]+", " ", title or "")
    return re.sub(r"\s+", " ", title).strip()[:200]


def unique_slug(base: str) -> str:
    conn = get_conn()
    slug = slugify(base)
    candidate = slug
    n = 2
    with _LOCK:
        while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (candidate,)).fetchone():
            candidate = f"{slug}-{n}"
            n += 1
    return candidate


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------

def create_project(
    title: str,
    description: str = "",
    kind: str = "unknown",
    stage: str = "backlog",
    priority: int = 0,
    slug: Optional[str] = None,
    parent_id: Optional[int] = None,
    build_approved: Optional[bool] = None,
    person_id: Optional[int] = None,
) -> sqlite3.Row:
    conn = get_conn()
    ts = now()
    title = clean_title(title)
    slug = slug or unique_slug(title or "idea")
    # Creating a project already approved for building is a deliberate act by a
    # human (an agent can only ever ask later), passed explicitly - the old
    # implicit rule was `status == "building"`, which the legacy vocabulary
    # still honours. New ideas land in `backlog` (or `active` for the "add and
    # start planning" button) and stay unapproved until Wes says otherwise.
    if build_approved is None:
        build_approved = stage == "building"
    if stage not in config.PROJECT_STAGES:
        stage = config.LEGACY_STATUS_STAGE.get(stage, "backlog")
        if stage == "paused":  # legacy waiting_user; nothing creates this today
            stage = "active"
    with _LOCK:
        cur = conn.execute(
            """INSERT INTO projects (slug, title, description, initial_idea, kind,
                                      stage, priority, build_approved, parent_id,
                                      created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            # initial_idea starts as a copy of the description: at creation the
            # two are the same thing, and they diverge only once an agent
            # rewrites the description.
            (slug, title or slug, description, description, kind, stage, priority,
             1 if build_approved else 0, parent_id, ts, ts),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone()

    # A new project belongs to whoever made it, and to the owner if nobody said.
    # Done here rather than at each call site because a project with no members
    # is the one genuinely broken state - it would show on no dashboard and the
    # prompt would have nobody to address - and there are six places that create
    # one (the form, Telegram, the agent report, the sub-project split, ...).
    from app import people  # local: people imports db

    people.add_member(int(row["id"]), int(person_id) if person_id else int(people.owner()["id"]))
    return row


def get_project(project_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def get_project_by_slug(slug: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()


def list_projects(order_by: Optional[str] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    order_by = order_by or project_order("updated_at ASC")
    with _LOCK:
        return conn.execute(f"SELECT * FROM projects ORDER BY {order_by}").fetchall()


def show_priority() -> bool:
    """Whether the per-project priority number is part of this install.

    Read defensively: this is called from every project listing, including ones
    that run before the settings table has been seeded (and from tests holding
    a bare database), and a listing that raises is worse than one that keeps
    the default.
    """
    try:
        value = get_setting("show_priority")
    except Exception:
        return True
    if value is None:
        value = config.DEFAULT_SETTINGS.get("show_priority", "1")
    return value == "1"


def project_order(rest: str) -> str:
    """An ORDER BY for projects, with priority in front of it when it is on.

    One definition rather than five copies of the same string, so turning
    priority off cannot leave one listing still ranking by it. `rest` is the
    tiebreak that applies either way.

    CALL THIS BEFORE TAKING `_LOCK`, never inside the `with` block. It reads a
    setting, and `get_setting` takes `_LOCK` itself - which is a plain
    threading.Lock, not an RLock, so the same thread taking it twice hangs
    forever. The first cut of this interpolated the call straight into the
    f-string inside each `with _LOCK:` block, which deadlocked the dashboard,
    the scheduler and the sub-project list. See test_priority_toggle.py's
    watchdog test, which fails in a second rather than hanging the suite.
    """
    return f"priority DESC, {rest}" if show_priority() else rest


def project_sorts() -> dict[str, tuple[str, str]]:
    """The dashboard's sort menu for this install.

    With priority off, "priority, then recent" comes off the menu rather than
    staying there ranking by a number nothing on the page can set.
    """
    if show_priority():
        return dict(config.PROJECT_SORTS)
    return {k: v for k, v in config.PROJECT_SORTS.items() if k != "priority"}


def default_project_sort() -> str:
    """The sort a dashboard with no stored preference uses. Falls through to
    the first one still on the menu when the configured default is gone."""
    sorts = project_sorts()
    if config.DEFAULT_PROJECT_SORT in sorts:
        return config.DEFAULT_PROJECT_SORT
    return next(iter(sorts))


def list_projects_sorted(sort: Optional[str]) -> list[sqlite3.Row]:
    """Projects in one of the named orders from `project_sorts()`.

    The name is looked up rather than interpolated, so an unknown (or hostile)
    `?sort=` value falls back to the default instead of reaching SQLite.
    """
    sorts = project_sorts()
    label_sql = sorts.get(sort or "") or sorts[default_project_sort()]
    return list_projects(order_by=label_sql[1])


def list_projects_by_stage(stages: Iterable[str]) -> list[sqlite3.Row]:
    stages = list(stages)
    conn = get_conn()
    placeholders = ",".join("?" for _ in stages)
    order = project_order("updated_at ASC")
    with _LOCK:
        return conn.execute(
            f"SELECT * FROM projects WHERE stage IN ({placeholders}) "
            f"ORDER BY {order}",
            tuple(stages),
        ).fetchall()


def list_schedulable_projects() -> list[sqlite3.Row]:
    """What the worker may pick from on its own: active-stage, not paused.
    Finer filters (the build gate, blocked-with-nothing-workable) are the
    worker's, since they depend on settings and todos."""
    conn = get_conn()
    order = project_order("updated_at ASC")
    with _LOCK:
        return conn.execute(
            "SELECT * FROM projects WHERE stage = 'active' "
            "AND (paused IS NULL OR paused = '') "
            f"ORDER BY {order}",
        ).fetchall()


def parent_id_of(project: Optional[sqlite3.Row]) -> Optional[int]:
    """This project's parent id, or None.

    Tolerates a row read before the column existed (and `None`), because this is
    called from the dashboard, the prompt builder and the delete path - all of
    which would rather see "no parent" than raise.
    """
    if project is None:
        return None
    try:
        return project["parent_id"] or None
    except (IndexError, KeyError):
        return None


def child_projects(parent_id: int) -> list[sqlite3.Row]:
    """A parent's sub-projects, in the order they should be worked on.

    Same ordering as the dashboard's default (priority first, then least
    recently touched), so the list on the parent page reads as a queue.
    """
    conn = get_conn()
    order = project_order("updated_at ASC")
    with _LOCK:
        return conn.execute(
            "SELECT * FROM projects WHERE parent_id = ? "
            f"ORDER BY {order}",
            (parent_id,),
        ).fetchall()


def child_counts() -> dict[int, int]:
    """parent_id -> number of sub-projects, for every parent that has any."""
    conn = get_conn()
    with _LOCK:
        rows = conn.execute(
            "SELECT parent_id, COUNT(*) AS n FROM projects "
            "WHERE parent_id IS NOT NULL GROUP BY parent_id"
        ).fetchall()
    return {row["parent_id"]: row["n"] for row in rows}


def update_project(project_id: int, **fields: Any) -> None:
    if not fields:
        return
    if "title" in fields:
        # Every writer of a title goes through here, so this is the one place
        # the scrub has to happen. An all-junk title would blank the column, so
        # it is dropped rather than written.
        cleaned = clean_title(fields["title"])
        if cleaned:
            fields["title"] = cleaned
        else:
            fields.pop("title")
        if not fields:
            return
    # Deliberately NO automatic clearing of `paused` here: only Wes pauses, so
    # only Wes's own actions (`set_user_state`, `approve_build`) lift a pause.
    # An agent moving the stage to review must not quietly unpark a project he
    # put down.
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = get_conn()
    with _LOCK:
        conn.execute(f"UPDATE projects SET {cols} WHERE id = ?", (*fields.values(), project_id))
        conn.commit()


def pause_project(project_id: int) -> None:
    """Wes puts a project down. The stage stays what it was; the pause is the
    orthogonal flag on top of it."""
    update_project(project_id, paused=now())


def is_paused(project: sqlite3.Row) -> bool:
    """Did Wes pause this? Only he sets the flag - an agent blocked on him is
    `blocked_on`, which is a different fact."""
    return bool(_row_get(project, "paused", None))


def display_state(project: sqlite3.Row) -> str:
    """The one word a badge (or the picker) shows: `paused` when Wes has it
    down, otherwise the stage. Stored name = displayed name, no translation."""
    return "paused" if is_paused(project) else str(_row_get(project, "stage", "backlog"))


def set_user_state(project: sqlite3.Row, state: str, via: str = "") -> None:
    """Apply a state Wes chose - from the picker, a dashboard drag, the context
    menu or Telegram - and journal the move. The one shared writer, so the side
    effects can't drift between entry points.

    `paused` stamps the pause and leaves the stage alone; every real stage
    choice clears the pause (choosing a shelf IS the unpause). Choosing
    `active` is also the build approval - it would be perverse to put a project
    on the working shelf and have the worker refuse to write code there.
    Approval is sticky; a later pause or review doesn't revoke it.
    """
    was = display_state(project)
    if state == "paused":
        update_project(project["id"], paused=now())
    elif state == "active":
        update_project(
            project["id"], stage="active", paused=None,
            build_approved=1, build_requested=0,
        )
    else:
        # A stage move consumes any open build request: Wes has just said what
        # he wants instead.
        update_project(project["id"], stage=state, paused=None, build_requested=0)
    if state != was:
        add_journal(
            project["id"], "user", "status",
            f"Status changed{via}: `{was}` -> `{state}`",
        )


def queue_research(project_id: int) -> None:
    """Wes queues a project for a research burst in the next spend-down window."""
    update_project(project_id, research_queued_at=now())


def unqueue_research(project_id: int) -> None:
    update_project(project_id, research_queued_at=None)


def is_research_queued(project: sqlite3.Row) -> bool:
    try:
        return bool(project["research_queued_at"])
    except (IndexError, KeyError):  # row from a pre-migration query
        return False


def list_research_queued() -> list[sqlite3.Row]:
    """Queued projects, longest-waiting first, so a burst works through the
    queue in the order Wes filled it rather than by project id."""
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT * FROM projects WHERE research_queued_at IS NOT NULL "
            "AND research_queued_at != '' ORDER BY research_queued_at ASC, id ASC"
        ).fetchall()


def count_research_queued() -> int:
    return len(list_research_queued())


def project_shelf(project: sqlite3.Row, open_questions: int = 0) -> str:
    """Which dashboard section a project belongs on: `active`, `review`,
    `paused`, `backlog` or `done`. Arithmetic, not interpretation - the third
    rewrite of this mapping was the design's whole argument.

    An active project that is waiting on Wes for *anything* - his pause, an
    agent blocked on him, a build request he hasn't answered, an open question -
    folds to the Paused shelf ("even if they need user input or something, I
    don't care - I want them in the paused/backlog section"). Review stays
    Review even with questions open: that shelf already means "your turn".
    """
    stage = str(_row_get(project, "stage", "backlog"))
    if stage in config.DONE_STAGES:
        return "done"
    if is_paused(project):
        return "paused"
    if stage != "active":
        return stage
    gate_wait = (
        build_requested(project)
        and not build_approved(project)
        and (get_setting("require_build_approval") or "1") == "1"
    )
    if blocked_on(project) or gate_wait or open_questions:
        return "paused"
    return "active"


def shelved_project_ids() -> set[int]:
    """Projects whose open questions are real but not urgent - out of the nav
    badge and below the fold on the questions page. That is Wes-paused and
    backlog projects; an agent parked on him (blocked, build request, open
    question) stays loud, because that waiting is his to end."""
    conn = get_conn()
    with _LOCK:
        rows = conn.execute(
            "SELECT id FROM projects WHERE stage = 'backlog' "
            "OR (paused IS NOT NULL AND paused != '')"
        ).fetchall()
    return {int(row["id"]) for row in rows}


def suggested_slug(project: sqlite3.Row) -> Optional[str]:
    """The folder name this project's current title implies, or None.

    Projects are created before they have a name: the slug is cut from whatever
    Wes typed when he had the idea, so it reads like
    `make-the-silhouette-card-cutter-work-with-my-mtg-proxy-forge-maybe-modify-the-e`
    long after an agent has given the project the title "Silhouette print-and-cut
    for MTG proxies". This proposes the tidy version - a *short* one, via
    `short_slug`, not a hyphenated copy of the title.

    It fires only on a slug that is still raw idea text (`slug_is_untidy`), and
    deliberately not on any mismatch between title and folder. Folder names no
    longer track titles - that is the whole point of the short name - so a title
    edit must not start proposing a directory move underneath an agent.

    It only ever *proposes*. Renaming a workspace moves a directory an agent may
    have written absolute paths into, so it stays one deliberate click - and
    once Wes has had his say (a manual rename, or dismissing the suggestion)
    `slug_locked` stops the portal asking again.
    """
    slug = project["slug"]
    if _row_get(project, "slug_locked", 0):
        return None
    if slug == config.META_PROJECT_SLUG:
        # The self-update check is pinned to this slug.
        return None
    if not slug_is_untidy(slug):
        return None
    target = short_slug(project["title"] or "")
    if not target or target == "idea":
        return None
    # A sub-project's tidy name keeps the family prefix - proposing a bare
    # short name would quietly undo the child naming scheme.
    pid = parent_id_of(project)
    if pid:
        parent = get_project(pid)
        pslug = (parent["slug"] or "").strip("-") if parent else ""
        if pslug and target != pslug and not target.startswith(pslug + "-"):
            target = f"{pslug}-{target}"
    if target == slug:
        return None
    conn = get_conn()
    with _LOCK:
        taken = conn.execute(
            "SELECT 1 FROM projects WHERE slug = ? AND id != ?", (target, project["id"])
        ).fetchone()
    return None if taken else target


def projects_with_suggested_slugs() -> list[tuple[sqlite3.Row, str]]:
    """Every project whose folder name is still the raw idea text."""
    out = []
    for project in list_projects("id ASC"):
        target = suggested_slug(project)
        if target:
            out.append((project, target))
    return out


def delete_project(project_id: int) -> None:
    """Remove a project and everything written *about* it.

    Its runs are kept but detached (project_id -> NULL): they are the record of
    work the portal actually did, they carry the cost that was actually spent,
    and silently subtracting them from the usage history would make the totals
    on /activity disagree with reality. They show up there as unattributed runs
    instead. Journal entries and questions have no meaning without the project,
    so those do go."""
    conn = get_conn()
    with _LOCK:
        conn.execute("UPDATE runs SET project_id = NULL WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM questions WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM todos WHERE project_id = ?", (project_id,))
        # Attachments index rows go with the project. The files themselves live
        # in the workspace and share its fate - kept unless the workspace
        # checkbox was ticked - so an orphaned index row would be a lie either
        # way round.
        conn.execute("DELETE FROM attachments WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM journal WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()


# --------------------------------------------------------------------------
# Attachments (see app/attachments.py for the on-disk half)
# --------------------------------------------------------------------------

def add_attachment(
    project_id: int,
    orig_name: str,
    stored_name: str,
    mime: str,
    size: int,
    journal_id: Optional[int] = None,
    note: str = "",
) -> int:
    conn = get_conn()
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO attachments "
            "(project_id, journal_id, created_at, orig_name, stored_name, mime, size, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, journal_id, now(), orig_name, stored_name, mime, size, note),
        )
        conn.commit()
        return int(cur.lastrowid)


def set_attachment_stored_name(attachment_id: int, stored_name: str) -> None:
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "UPDATE attachments SET stored_name = ? WHERE id = ?", (stored_name, attachment_id)
        )
        conn.commit()


def set_attachment_journal(attachment_id: int, journal_id: int) -> None:
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "UPDATE attachments SET journal_id = ? WHERE id = ?", (journal_id, attachment_id)
        )
        conn.commit()


def get_attachment(attachment_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT attachments.*, projects.slug AS project_slug "
            "FROM attachments LEFT JOIN projects ON projects.id = attachments.project_id "
            "WHERE attachments.id = ?",
            (attachment_id,),
        ).fetchone()


def list_attachments(project_id: int) -> list[sqlite3.Row]:
    """Newest last, matching how they were uploaded. Rows with an empty
    stored_name are excluded: those are the brief window inside `store()`
    between the index row and the bytes landing on disk, and a row with no file
    behind it would render as a broken preview."""
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT * FROM attachments WHERE project_id = ? AND stored_name != '' "
            "ORDER BY id ASC",
            (project_id,),
        ).fetchall()


def attachments_by_journal(project_id: int) -> dict[int, list[sqlite3.Row]]:
    """Attachments grouped by the note they arrived with, so the journal can
    show each file under the sentence that explains it."""
    out: dict[int, list[sqlite3.Row]] = {}
    for row in list_attachments(project_id):
        if row["journal_id"] is not None:
            out.setdefault(int(row["journal_id"]), []).append(row)
    return out


def delete_attachment(attachment_id: int) -> None:
    conn = get_conn()
    with _LOCK:
        conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        conn.commit()


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------

def is_note(author: str, kind: str) -> bool:
    """The one kind of journal entry Wes wrote himself and may still take back.
    Status lines, agent reports and answered questions are records of something
    that already happened; a note is an instruction that has not been acted on
    yet."""
    return author == "user" and kind == "note"


def add_journal(
    project_id: Optional[int],
    author: str,
    kind: str,
    content_md: str,
    person_id: Optional[int] = None,
) -> int:
    """Returns the new entry's id, so a caller that has uploads in hand (see
    the note route) can attach them to the entry it just wrote.

    Anything that is not one of Wes's notes is stamped delivered immediately:
    only a note has an edit window, and a NULL on a status line would make the
    pending-notes query depend on its own WHERE clause being exactly right.

    `person_id` is who wrote it, once more than one person could have. It stays
    optional with a None default on purpose: every existing caller keeps
    working unchanged, and the ones that have no person behind them (an agent's
    report, a system status line) are supposed to pass nothing rather than be
    forced to invent an author."""
    ts = now()
    delivered = None if is_note(author, kind) else ts
    conn = get_conn()
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO journal (project_id, ts, author, kind, content_md, "
            "delivered_at, person_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, ts, author, kind, content_md, delivered,
             int(person_id) if person_id is not None else None),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_journal(entry_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute("SELECT * FROM journal WHERE id = ?", (entry_id,)).fetchone()


def pending_notes(project_id: int) -> list[sqlite3.Row]:
    """Wes's notes on this project that no agent has been given yet, oldest
    first - which is the order he wrote them in and so the order they should be
    read in."""
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT * FROM journal WHERE project_id = ? AND author = 'user' AND kind = 'note' "
            "AND delivered_at IS NULL ORDER BY id",
            (project_id,),
        ).fetchall()


def mark_notes_delivered(entry_ids: Sequence[int]) -> None:
    """Stamp exactly these entries as sent. Takes ids rather than a project so
    it can only ever close the edit window on the notes that actually went into
    the prompt - a note written while the prompt was being assembled stays
    pending for the next run rather than being lost."""
    ids = [int(i) for i in entry_ids]
    if not ids:
        return
    stamp = now()
    conn = get_conn()
    with _LOCK:
        conn.executemany(
            "UPDATE journal SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
            [(stamp, i) for i in ids],
        )
        conn.commit()


def update_journal_content(entry_id: int, content_md: str) -> bool:
    """Rewrite an undelivered note. Refuses anything already sent: the point of
    editing is to change what the agent will read, and rewriting history it has
    already acted on would make the journal disagree with the run."""
    conn = get_conn()
    with _LOCK:
        cur = conn.execute(
            "UPDATE journal SET content_md = ? WHERE id = ? AND delivered_at IS NULL "
            "AND author = 'user' AND kind = 'note'",
            (content_md, entry_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_journal_note(entry_id: int) -> bool:
    """Same window as editing. Attachments that rode along with the note keep
    their rows and their files - the note is Wes's text, and a file he uploaded
    is not undone by taking the sentence back."""
    conn = get_conn()
    with _LOCK:
        cur = conn.execute(
            "DELETE FROM journal WHERE id = ? AND delivered_at IS NULL "
            "AND author = 'user' AND kind = 'note'",
            (entry_id,),
        )
        conn.commit()
        return cur.rowcount > 0


# `ts` is second-resolution, so entries written in the same second (a note and
# the run it kicks off, an ask and its answer, a burst of status lines) sort
# arbitrarily under `ts` alone - and the arbitrary order changes between
# queries. `id` breaks the tie by insertion order, which is what a reader and a
# prompt both mean by "then what happened".
_JOURNAL_ORDER = "ORDER BY journal.ts DESC, journal.id DESC"


def list_journal(project_id: Optional[int] = None, limit: int = 100) -> list[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        if project_id is None:
            return conn.execute(
                "SELECT journal.*, projects.title AS project_title, projects.slug AS project_slug "
                "FROM journal LEFT JOIN projects ON projects.id = journal.project_id "
                f"{_JOURNAL_ORDER} LIMIT ?",
                (limit,),
            ).fetchall()
        return conn.execute(
            f"SELECT * FROM journal WHERE project_id = ? {_JOURNAL_ORDER} LIMIT ?",
            (project_id, limit),
        ).fetchall()


# The ask side thread: a question Wes asked about the project and the read-only
# answer it got back. Journalled and shown like anything else, but skipped when
# a RUN's prompt is built - Wes's 2026-07-25 note asked for an ask to be "asked
# in parallel and not factored into the rest of the journal context".
#
# Matched on the (author, kind) PAIR, not on the kind alone, because
# `user/answer` is a completely different thing: that is Wes answering one of
# the portal's own questions, which is an instruction a run must absolutely
# still see. See app/quoting.py.
SIDE_THREAD = (("user", "ask"), ("agent", "answer"))


def is_side_thread(author: str, kind: str) -> bool:
    """Whether an entry belongs to the ask side thread (used by the template to
    badge it, so the page and the prompt agree about what a run reads)."""
    return (author, kind) in SIDE_THREAD


def list_journal_asc(
    project_id: int, limit: int = 20, exclude: tuple = ()
) -> list[sqlite3.Row]:
    """Oldest-first slice of the most recent `limit` entries (for agent prompts).

    `exclude` is a tuple of (author, kind) pairs to leave out. The filtering
    happens in SQL, before the LIMIT, so excluded entries do not eat slots in
    the tail: a project with a chatty ask thread still hands a run its last
    `limit` real entries rather than a tail half-full of holes.
    """
    sql = "SELECT * FROM journal WHERE project_id = ?"
    params: list = [project_id]
    for author, kind in exclude:
        sql += " AND NOT (author = ? AND kind = ?)"
        params.extend([author, kind])
    sql += f" {_JOURNAL_ORDER} LIMIT ?"
    params.append(limit)
    conn = get_conn()
    with _LOCK:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return list(reversed(rows))


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------

def _next_slot(conn: sqlite3.Connection) -> int:
    """The smallest positive integer not currently held by an open question.

    Question numbers are what Wes types back at the bot ("Q7: ..."), so they
    have to stay small and typeable. Using the row id meant they climbed
    forever; a slot is released the moment the question is answered or
    dismissed and handed to the next question that comes along.
    """
    taken = {
        row["slot"]
        for row in conn.execute(
            "SELECT slot FROM questions WHERE status = 'open' AND slot IS NOT NULL"
        )
    }
    slot = 1
    while slot in taken:
        slot += 1
    return slot


# How long a question stays "dealt with" after Wes answers or dismisses it.
#
# Open questions are what he actually asked for ("the questions waiting to be
# answered"), but the worst case in the record slips straight past that: the
# spend-down offer was filed *eleven times in eighty minutes*, and he answered
# each one before the next arrived, so at no point were two of them open
# together. His reply to the eleventh was "You asked me way too many times
# here. I just want to be asked once."
#
# Six hours rather than forever, because an answer changes the world and the
# same words can be a real follow-up the next day - and because the answered
# Q&A is already in every prompt, so a run re-asking inside this window is
# ignoring an answer it can see.
QUESTION_SETTLED_HOURS = 6


class QuestionFiling(NamedTuple):
    """What happened when a question was filed.

    `created` is False when an open question on the same project already asked
    this - `row` is then that existing question, so a caller still has a real
    row to point a notification or a return value at, and can tell from
    `created` that it must NOT send one. That distinction is the whole point:
    re-notifying about a question Wes has already been shown is exactly the
    duplicate he complained about, only worse for arriving twice on his phone.
    """

    row: sqlite3.Row
    created: bool
    duplicate_of: Optional[sqlite3.Row] = None


def file_question(
    project_id: int, question: str, context: str = "", quick_options: str = ""
) -> QuestionFiling:
    """Ask Wes something, unless he is already being asked it.

    This is the choke point rather than a check at each of the three call sites,
    so a fourth caller added later inherits the guard instead of forgetting it -
    and so two runs racing on the same project cannot both slip a copy in (the
    lookup and the insert happen under the same `_LOCK` the writes use).
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=QUESTION_SETTLED_HOURS)
    ).isoformat(timespec="seconds")
    conn = get_conn()
    with _LOCK:
        existing = conn.execute(
            "SELECT * FROM questions WHERE project_id = ? AND ("
            "  status = 'open'"
            "  OR (status IN ('answered', 'dismissed') "
            "      AND COALESCE(answered_at, ts) >= ?)"
            ") ORDER BY ts ASC",
            (project_id, cutoff),
        ).fetchall()
        dupe = qdedupe.find_duplicate(question, existing, quick_options)
        if dupe is not None:
            log.info(
                "Question on project %s deduped against open question %s: %r",
                project_id, dupe["id"], (question or "")[:120],
            )
            return QuestionFiling(row=dupe, created=False, duplicate_of=dupe)
        cur = conn.execute(
            "INSERT INTO questions (project_id, ts, question, context, status, slot, quick_options) "
            "VALUES (?, ?, ?, ?, 'open', ?, ?)",
            (project_id, now(), question, context, _next_slot(conn), quick_options),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM questions WHERE id = ?", (cur.lastrowid,)).fetchone()
    return QuestionFiling(row=row, created=True)


def create_question(
    project_id: int, question: str, context: str = "", quick_options: str = ""
) -> sqlite3.Row:
    """Back-compatible shim: the row, whether it was inserted now or matched.

    Callers that send a notification want `file_question` so they can honour
    `created`; this stays for the ones that only want a row back.
    """
    return file_question(project_id, question, context, quick_options).row


def question_by_slot(slot: int) -> Optional[sqlite3.Row]:
    """Look up an *open* question by its short number. Slots are recycled, so
    this is only meaningful while the question is open."""
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT * FROM questions WHERE status = 'open' AND slot = ?", (slot,)
        ).fetchone()


def resolve_question(ref: int) -> Optional[sqlite3.Row]:
    """Resolve a number Wes typed at the bot.

    An open question's slot wins over a row id, because the slot is the number
    he was actually shown. Falling back to the id keeps older notification
    messages (and `/answer <id>`) working.
    """
    return question_by_slot(ref) or get_question(ref)


def get_question(question_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()


def set_question_telegram_msg_id(question_id: int, msg_id: int) -> None:
    conn = get_conn()
    with _LOCK:
        conn.execute("UPDATE questions SET telegram_msg_id = ? WHERE id = ?", (msg_id, question_id))
        conn.commit()


def open_questions(project_id: Optional[int] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        if project_id is None:
            return conn.execute(
                "SELECT questions.*, projects.title AS project_title, projects.slug AS project_slug "
                "FROM questions JOIN projects ON projects.id = questions.project_id "
                "WHERE questions.status = 'open' ORDER BY questions.ts ASC"
            ).fetchall()
        return conn.execute(
            "SELECT * FROM questions WHERE project_id = ? AND status = 'open' ORDER BY ts ASC",
            (project_id,),
        ).fetchall()


def answered_qa(project_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT * FROM questions WHERE project_id = ? AND status = 'answered' ORDER BY ts ASC",
            (project_id,),
        ).fetchall()


def dismissed_questions(project_id: int) -> list[sqlite3.Row]:
    """Questions Wes dismissed. Dismissing clears the notification and takes it
    off the questions tab, but the question stays on its own project - it is
    still a record of something the agent wanted to know."""
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT * FROM questions WHERE project_id = ? AND status = 'dismissed' ORDER BY ts ASC",
            (project_id,),
        ).fetchall()


def count_open_questions(project_id: int) -> int:
    conn = get_conn()
    with _LOCK:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM questions WHERE project_id = ? AND status = 'open'",
            (project_id,),
        ).fetchone()
    return int(row["c"])


def open_question_counts() -> dict[int, int]:
    """{project_id: open question count} for every project that has any.

    Used to draw the notification badge on the dashboard project cells without
    running one COUNT query per card.
    """
    conn = get_conn()
    with _LOCK:
        rows = conn.execute(
            "SELECT project_id, COUNT(*) AS c FROM questions "
            "WHERE status = 'open' GROUP BY project_id"
        ).fetchall()
    return {int(row["project_id"]): int(row["c"]) for row in rows}


def answer_question(question_id: int, answer: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "UPDATE questions SET status = 'answered', answer = ?, answered_at = ?, "
            "slot = NULL WHERE id = ?",
            (answer, now(), question_id),
        )
        conn.commit()
        return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()


def answer_question_and_resume(question_id: int, answer: str) -> Optional[sqlite3.Row]:
    """Answer a question and journal it. There is nothing to "resume" any more:
    an open question never moved the project, so answering the last one simply
    changes the count and every derived badge follows by itself."""
    question = get_question(question_id)
    if question is None:
        return None
    answer_question(question_id, answer)
    add_journal(question["project_id"], "user", "answer", f"**Q:** {question['question']}\n\n**A:** {answer}")
    return question


def dismiss_question(question_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "UPDATE questions SET status = 'dismissed', answered_at = ?, slot = NULL WHERE id = ?",
            (now(), question_id),
        )
        conn.commit()
        return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()


def reopen_question(question_id: int) -> Optional[sqlite3.Row]:
    """Put a dismissed question back on the questions tab.

    It gets a *fresh* slot rather than the one it had: that number was released
    on dismissal and something else is very likely holding it now.
    """
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "UPDATE questions SET status = 'open', answered_at = NULL, slot = ? "
            "WHERE id = ? AND status = 'dismissed'",
            (_next_slot(conn), question_id),
        )
        conn.commit()
        return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()


def dismiss_question_and_resume(question_id: int) -> Optional[sqlite3.Row]:
    question = get_question(question_id)
    if question is None:
        return None
    dismiss_question(question_id)
    return question


# --------------------------------------------------------------------------
# Todos
# --------------------------------------------------------------------------

TODO_OWNERS = ("agent", "user")

# The one tag with mechanical meaning: an open agent item wearing it is not
# workable, so it does not keep a project schedulable on its own. Everything
# else ("ready-to-build", "needs-parts", ...) is a label for Wes and the agent
# to read, nothing more.
BLOCKED_TAG = "blocked"
# Chips have to fit on a todo row; a paragraph belongs in the todo text.
MAX_TODO_TAGS = 6
TODO_TAG_MAXLEN = 24


def normalize_todo_tag(tag: Any) -> str:
    """One tag as stored: lowercase kebab ("Ready to Build" -> "ready-to-build").

    Kebab rather than free text because tags are stored comma-separated and
    rendered as bracketed chips, so commas and brackets cannot survive anyway -
    collapsing every run of other characters to a hyphen makes the same tag
    typed two ways compare equal instead of accumulating variants."""
    if not isinstance(tag, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-")[:TODO_TAG_MAXLEN].strip("-")


def _join_tags(tags: Any) -> str:
    """Normalise, dedupe (order kept) and cap a tag list into its stored form."""
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, (list, tuple)):
        return ""
    seen: list[str] = []
    for raw in tags:
        tag = normalize_todo_tag(raw)
        if tag and tag not in seen:
            seen.append(tag)
    return ",".join(seen[:MAX_TODO_TAGS])


def todo_tags(row: Any) -> list[str]:
    """The tags on a todo row, as a list. Tolerates pre-migration rows."""
    try:
        raw = row["tags"]
    except (IndexError, KeyError, TypeError):
        return []
    return [t for t in (raw or "").split(",") if t]


def set_todo_tags(todo_id: int, tags: Any) -> Optional[sqlite3.Row]:
    """Replace a todo's tags outright. The set-not-merge semantics are the
    point: reporting `[]` is how an agent says "no longer blocked"."""
    conn = get_conn()
    with _LOCK:
        if conn.execute("SELECT 1 FROM todos WHERE id = ?", (todo_id,)).fetchone() is None:
            return None
        conn.execute("UPDATE todos SET tags = ? WHERE id = ?", (_join_tags(tags), todo_id))
        conn.commit()
        return conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()


def add_todo_tag(todo_id: int, tag: str) -> Optional[sqlite3.Row]:
    row = get_todo(todo_id)
    if row is None:
        return None
    return set_todo_tags(todo_id, todo_tags(row) + [tag])


def remove_todo_tag(todo_id: int, tag: str) -> Optional[sqlite3.Row]:
    row = get_todo(todo_id)
    if row is None:
        return None
    gone = normalize_todo_tag(tag)
    return set_todo_tags(todo_id, [t for t in todo_tags(row) if t != gone])


def _todo_key(text: str) -> str:
    """Normalised form used to decide whether two todos are the same item.

    The agent sees its list in every prompt and re-states items in its report,
    so without this the same task accumulates a copy per run with slightly
    different capitalisation or trailing punctuation."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def add_todo(
    project_id: int, text: str, owner: str = "agent", tags: Any = None
) -> Optional[sqlite3.Row]:
    """Add a todo, or return the existing one if it is already on the list.

    Matching is on the normalised text within the project regardless of done
    state: re-adding something already ticked off should not resurrect it as a
    second open item. On a dedupe hit any new tags are merged into the existing
    row - restating "X, and it is blocked" should land the tag, not vanish."""
    # Same control-character scrub as titles, but a longer cap: a todo is a
    # sentence ("collapse the agent console by default"), not a name.
    text = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]+", " ", text or "")).strip()[:500]
    if not text:
        return None
    if owner not in TODO_OWNERS:
        owner = "agent"
    key = _todo_key(text)
    conn = get_conn()
    with _LOCK:
        existing = next(
            (
                row
                for row in conn.execute(
                    "SELECT * FROM todos WHERE project_id = ?", (project_id,)
                ).fetchall()
                if _todo_key(row["text"]) == key
            ),
            None,
        )
        if existing is None:
            cur = conn.execute(
                "INSERT INTO todos (project_id, owner, text, done, created_at, tags) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (project_id, owner, text, now(), _join_tags(tags)),
            )
            conn.commit()
            return conn.execute("SELECT * FROM todos WHERE id = ?", (cur.lastrowid,)).fetchone()
    if isinstance(tags, str):
        extra = tags.split(",")
    elif isinstance(tags, (list, tuple)):
        extra = list(tags)
    else:
        extra = []
    merged = _join_tags(todo_tags(existing) + extra)
    if merged != (existing["tags"] or ""):
        return set_todo_tags(existing["id"], merged)
    return existing


def get_todo(todo_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()


def list_todos(project_id: int, owner: Optional[str] = None) -> list[sqlite3.Row]:
    """Open items first, each half in the order they were added."""
    conn = get_conn()
    sql = "SELECT * FROM todos WHERE project_id = ?"
    params: list[Any] = [project_id]
    if owner:
        sql += " AND owner = ?"
        params.append(owner)
    sql += " ORDER BY done ASC, id ASC"
    with _LOCK:
        return conn.execute(sql, tuple(params)).fetchall()


def set_todo_done(todo_id: int, done: bool) -> Optional[sqlite3.Row]:
    """Tick or untick. Unticking also un-clears: an item Wes has pulled back
    onto the list should be on the list, not hidden because a previous
    completion of it was already cleared away."""
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "UPDATE todos SET done = ?, done_at = ?, cleared_at = ? WHERE id = ?",
            (1 if done else 0, now() if done else None, None, todo_id),
        )
        conn.commit()
        return conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()


# How long a completed item stays on the live list before it drops out on its
# own. Long enough that a run finishing overnight is still there in the
# morning; short enough that the list is a list of work, not an archive.
TODO_DONE_TTL_HOURS = 16


def _todo_expired(row, cutoff: datetime) -> bool:
    """Has this completed item aged off the live list?"""
    if not row["done"]:
        return False
    if row["cleared_at"]:
        return True
    stamp = row["done_at"]
    if not stamp:
        # Ticked off before done_at was recorded: no age to judge it by, so
        # treat it as old rather than pinning it to the list forever.
        return True
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= cutoff


def visible_todos(project_id: int, owner: Optional[str] = None) -> list[sqlite3.Row]:
    """What the project page shows: every open item, plus recently completed
    ones that haven't been cleared.

    Wes asked for completed items to stop occupying the list "after 16 hours or
    until they have been viewed". A page render is a poor proxy for "viewed" -
    it would vanish work he never looked at - so the explicit clear button is
    the "viewed" half, and the 16 hours is the backstop for when he doesn't
    press it."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=TODO_DONE_TTL_HOURS)
    return [r for r in list_todos(project_id, owner) if not _todo_expired(r, cutoff)]


def completed_todos(project_id: int, owner: Optional[str] = None) -> list[sqlite3.Row]:
    """Everything ever ticked off, newest first - the history view. Nothing is
    deleted by clearing, so this is the full record."""
    conn = get_conn()
    sql = "SELECT * FROM todos WHERE project_id = ? AND done = 1"
    params: list[Any] = [project_id]
    if owner:
        sql += " AND owner = ?"
        params.append(owner)
    sql += " ORDER BY COALESCE(done_at, created_at) DESC, id DESC"
    with _LOCK:
        return conn.execute(sql, tuple(params)).fetchall()


def clear_completed_todos(project_id: int) -> int:
    """Hide every completed item from the live list. Returns how many moved.

    An update rather than a delete: the history view is the promise that
    clearing is safe."""
    conn = get_conn()
    with _LOCK:
        cur = conn.execute(
            "UPDATE todos SET cleared_at = ? WHERE project_id = ? AND done = 1 AND cleared_at IS NULL",
            (now(), project_id),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def delete_todo(todo_id: int) -> None:
    conn = get_conn()
    with _LOCK:
        conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
        conn.commit()


def complete_todo_by_ref(project_id: int, ref: Any) -> Optional[sqlite3.Row]:
    """Tick off a todo named either by its id or by (roughly) its text.

    An agent report may carry either, because an id is precise but the text is
    what the model actually has in its head."""
    conn = get_conn()
    with _LOCK:
        rows = conn.execute("SELECT * FROM todos WHERE project_id = ?", (project_id,)).fetchall()
    match = None
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().lstrip("#").isdigit()):
        wanted = int(str(ref).strip().lstrip("#"))
        match = next((r for r in rows if r["id"] == wanted), None)
    if match is None and isinstance(ref, str):
        key = _todo_key(ref)
        if key:
            match = next((r for r in rows if _todo_key(r["text"]) == key), None)
    if match is None:
        return None
    return set_todo_done(match["id"], True)


def count_hidden_done_todos(project_id: int) -> int:
    """Completed items no longer on the live list - the size of the history."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=TODO_DONE_TTL_HOURS)
    return sum(1 for r in list_todos(project_id) if _todo_expired(r, cutoff))


def count_clearable_todos(project_id: int) -> int:
    """Completed items still on the live list - what "clear completed" would
    take away. Zero means the button has nothing to do and isn't shown."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=TODO_DONE_TTL_HOURS)
    return sum(1 for r in list_todos(project_id) if r["done"] and not _todo_expired(r, cutoff))


def count_open_todos(project_id: int, owner: Optional[str] = None) -> int:
    conn = get_conn()
    sql = "SELECT COUNT(*) AS c FROM todos WHERE project_id = ? AND done = 0"
    params: list[Any] = [project_id]
    if owner:
        sql += " AND owner = ?"
        params.append(owner)
    with _LOCK:
        return int(conn.execute(sql, tuple(params)).fetchone()["c"])


def count_workable_todos(project_id: int) -> int:
    """Open agent items a run could actually pick up - i.e. not tagged
    'blocked'. This is the number the scheduler's build-where-unblocked check
    reads; the plain open count would schedule a run onto a list where every
    item waits on Wes."""
    return sum(
        1
        for r in list_todos(project_id, owner="agent")
        if not r["done"] and BLOCKED_TAG not in todo_tags(r)
    )


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------

def create_run(
    project_id: Optional[int], task: str, model: str, oneoff_id: Optional[int] = None
) -> int:
    conn = get_conn()
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO runs (project_id, task, model, started_at, status, oneoff_id) "
            "VALUES (?, ?, ?, ?, 'running', ?)",
            (project_id, task, model, now(), oneoff_id),
        )
        conn.commit()
        return int(cur.lastrowid)


def finish_run(
    run_id: int,
    status: str,
    session_id: Optional[str] = None,
    cost_usd: Optional[float] = None,
    num_turns: Optional[int] = None,
    summary: Optional[str] = None,
) -> None:
    conn = get_conn()
    with _LOCK:
        conn.execute(
            """UPDATE runs SET ended_at = ?, status = ?, session_id = ?, cost_usd = ?,
                                num_turns = ?, summary = ? WHERE id = ?""",
            (now(), status, session_id, cost_usd, num_turns, summary, run_id),
        )
        conn.commit()


def add_hook_event(
    run_id: Optional[int],
    event: str,
    tool: str,
    decision: str,
    reason: Optional[str] = None,
    detail: Optional[str] = None,
) -> int:
    conn = get_conn()
    with _LOCK:
        cur = conn.execute(
            """INSERT INTO hook_events (run_id, ts, event, tool, decision, reason, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, now(), event, tool, decision, reason, detail),
        )
        conn.commit()
        return int(cur.lastrowid)


def hook_audit_for_run(run_id: int) -> list[sqlite3.Row]:
    """The run's PostToolUse audit trail, oldest first - one row per tool call
    it made (capped, see app/hookguard.py)."""
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT * FROM hook_events WHERE run_id = ? AND event = 'post_tool_use' ORDER BY id",
            (run_id,),
        ).fetchall()


# How long plain audit rows live. Long enough to answer "what did that run
# last week actually do" (transcripts only survive for the newest 200 runs),
# short enough that the table never becomes the unbounded log Wes distrusts.
AUDIT_RETENTION_DAYS = 30


def prune_hook_audit(days: int = AUDIT_RETENTION_DAYS) -> int:
    """Age the PostToolUse audit out of hook_events. Denials and Stop bounces
    are explicitly kept - those are the rare rows the table exists for."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = get_conn()
    with _LOCK:
        cur = conn.execute(
            "DELETE FROM hook_events WHERE event = 'post_tool_use' "
            "AND decision NOT IN ('deny', 'block') AND ts < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount


def hook_denials_for_run(run_id: int) -> list[sqlite3.Row]:
    """Everything the hooks stopped for this run: PreToolUse denies and the
    Stop-hook report nudge's blocks. Allows are not stored (see the schema
    comment)."""
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT * FROM hook_events WHERE run_id = ? AND decision IN ('deny', 'block') ORDER BY id",
            (run_id,),
        ).fetchall()


# --------------------------------------------------------------------------
# Web-push subscriptions (see app/webpush.py)
# --------------------------------------------------------------------------

def add_push_subscription(endpoint: str, p256dh: str, auth: str, ua: str = "") -> None:
    """Enrol a device, or refresh one that re-subscribed: the endpoint is the
    identity, and fresh keys replace stale ones in place (a browser is allowed
    to rotate a subscription's keys whenever it likes)."""
    conn = get_conn()
    with _LOCK:
        conn.execute(
            """INSERT INTO push_subscriptions (endpoint, p256dh, auth, ua, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET
                   p256dh = excluded.p256dh, auth = excluded.auth,
                   ua = excluded.ua, failures = 0""",
            (endpoint, p256dh, auth, ua, now()),
        )
        conn.commit()


def list_push_subscriptions() -> list[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute("SELECT * FROM push_subscriptions ORDER BY id").fetchall()


def delete_push_subscription(endpoint: str) -> None:
    conn = get_conn()
    with _LOCK:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        conn.commit()


def delete_push_subscription_by_id(sub_id: int) -> None:
    conn = get_conn()
    with _LOCK:
        conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))
        conn.commit()


def mark_push_result(endpoint: str, ok: bool) -> None:
    """Record how the push service answered. Consecutive failures are counted
    (and shown on /settings) but never auto-delete the row - only the push
    service saying 404/410 does that, in webpush.send_one."""
    conn = get_conn()
    with _LOCK:
        if ok:
            conn.execute(
                "UPDATE push_subscriptions SET last_ok_at = ?, failures = 0 WHERE endpoint = ?",
                (now(), endpoint),
            )
        else:
            conn.execute(
                "UPDATE push_subscriptions SET failures = failures + 1 WHERE endpoint = ?",
                (endpoint,),
            )
        conn.commit()


# How many run summaries the banner will show at once. Past this it stops being
# "here is what happened while you were away" and becomes a second journal.
MAX_UNACKED_SHOWN = 6


# How many bullets one run may contribute to the banner. The point of the
# banner is "what actually changed", which one line could not carry - but past
# a handful it stops being a summary and becomes the journal entry again.
MAX_SUMMARY_BULLETS = 4

# Leading noise a model puts on a summary line: a list marker it added itself,
# or the "Done." that every run's last printed line used to open with. Both
# carry no information, and the ✔ in front of the bullet already says it.
_BULLET_NOISE = re.compile(
    r"^(?:[-*•✓✔●·]+\s*|\d+[.)]\s+|done[.:!—-]+\s*|"
    r"summary[.:]\s*)+",
    re.IGNORECASE,
)


def _clean_bullet(text: str) -> str:
    """One banner bullet: scrubbed of control characters, markers and "Done."."""
    text = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]+", " ", text or "")).strip()
    text = _BULLET_NOISE.sub("", text).strip()
    # Wes: "in the summary text, after 3 lines, it is cut off - I'd like to see
    # the rest". 300 characters is about three lines on his screen, so a bullet
    # written as a full sentence was reliably losing its ending. The cap is now
    # only a guard against a model pasting its whole report into the field: it
    # is far past anything a real bullet reaches, and the banner wraps.
    if len(text) > 1200:
        text = text[:1197].rstrip() + "..."
    return text


# Which marker a banner bullet earns. The green tick means "this shipped";
# Wes: "Check marks should be for tasks that were completed or things that were
# implemented. Not notes to mention or things that didn't work." Agents are now
# told to open a status bullet with "note:" (stripped here); the opener
# patterns catch the runs from before that rule and the agents that forget it.
_NOTE_PREFIX = re.compile(r"^(?:note|nb|fyi|caveat|heads[ -]?up)\b[:,-]\s*", re.IGNORECASE)
_NOTE_OPENERS = re.compile(
    r"^(?:not\s|nothing\s|still\s|next\b|blocked\b|waiting\b|pending\b|"
    r"deferred\b|skipped\b|remaining\b|didn'?t\s|did\s+not\s|couldn'?t\s|"
    r"could\s+not\s|unchanged\b|known\s+issue|open\s+question|unfinished\b|"
    r"orphaned\b|failed\s+to\s|no\s+progress\b)",
    re.IGNORECASE,
)


def summary_bullet(text: str) -> dict:
    """One banner bullet as the template renders it: its text and whether it
    wears the tick (`done`) or the quiet note marker (`note`)."""
    text = (text or "").strip()
    m = _NOTE_PREFIX.match(text)
    if m:
        stripped = text[m.end():].strip()
        return {"text": stripped or text, "kind": "note"}
    if _NOTE_OPENERS.match(text):
        return {"text": text, "kind": "note"}
    return {"text": text, "kind": "done"}


def _to_bullets(summary) -> list[str]:
    """Normalise a report's `summary` field into banner bullets.

    The field is written by a language model, so it arrives as a list of
    strings, one string, or one string that is secretly a list (newlines, or
    "- " markers). All three mean the same thing to Wes, so all three are
    accepted rather than one of them silently recording nothing.
    """
    if summary is None:
        return []
    items = summary if isinstance(summary, (list, tuple)) else [summary]
    out: list[str] = []
    for item in items:
        if isinstance(item, (list, tuple, dict)) or item is None:
            continue
        raw = item if isinstance(item, str) else str(item)
        # A single string holding several bullets: split it back apart, but
        # only on real line breaks - a hyphen mid-sentence is not a marker.
        for line in re.split(r"[\r\n]+|(?<=[.;])\s+(?=[-*•]\s)", raw):
            bullet = _clean_bullet(line)
            if bullet and bullet not in out:
                out.append(bullet)
            if len(out) >= MAX_SUMMARY_BULLETS:
                return out
    return out


def set_run_report_summary(run_id: int, summary) -> None:
    """Record the `summary` bullets from a run's report.json against the run.

    Stored newline-separated in the one column: the banner is the only reader,
    and a column of JSON would make every existing single-line row a migration.
    """
    bullets = _to_bullets(summary)
    if not bullets:
        return
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "UPDATE runs SET report_summary = ? WHERE id = ?", ("\n".join(bullets), run_id)
        )
        conn.commit()


def _summary_bullets(row) -> list[str]:
    """The bullets to show for a run in the "since you last looked" banner.

    Prefers the run's report.json `summary`, and falls back to the first line
    of whatever the CLI printed last. The fallback exists because
    `report_summary` was only recorded from the run that added the feature
    onwards, which left the banner empty on every project's entire history -
    and the history is exactly what the banner is for. Those fallback lines are
    the ones that all opened with a contentless "Done."; _clean_bullet drops it.
    """
    stored = (row["report_summary"] or "").strip()
    if stored:
        return _to_bullets(stored)
    raw = (row["summary"] or "").strip()
    return _to_bullets(raw.split("\n", 1)[0]) if raw else []


def unacknowledged_work(project_id: int, limit: int = MAX_UNACKED_SHOWN) -> list[dict]:
    """Runs whose summary Wes has not acknowledged yet, newest first.

    Keyed on `ended_at` rather than the run id so that acknowledging can never
    swallow a run that finished *after* the button was pressed - with parallel
    runs, a higher id is not reliably a later finish. Timestamps are only
    second-resolution though, so the id breaks the tie within one second.
    """
    conn = get_conn()
    with _LOCK:
        project = conn.execute(
            "SELECT work_ack_at FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            return []
        rows = conn.execute(
            """SELECT * FROM runs
                 WHERE project_id = ? AND ended_at IS NOT NULL
                   AND (COALESCE(report_summary, '') != '' OR COALESCE(summary, '') != '')
                   AND (? IS NULL OR ended_at > ?)
                 ORDER BY ended_at DESC, id DESC LIMIT ?""",
            (project_id, project["work_ack_at"], project["work_ack_at"], limit),
        ).fetchall()
    out = []
    for row in rows:
        bullets = _summary_bullets(row)
        if bullets:
            out.append(
                {
                    "id": row["id"],
                    "ended_at": row["ended_at"],
                    "bullets": bullets,
                    # Kept for anything that wants one string (the Telegram
                    # digest, an API reader); the banner uses the bullets.
                    "report_summary": " ".join(bullets),
                }
            )
    return out


def acknowledge_work(project_id: int) -> None:
    """Clear the banner: everything finished up to now has been read."""
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "UPDATE projects SET work_ack_at = ? WHERE id = ?", (now(), project_id)
        )
        conn.commit()


def list_runs(project_id: int, limit: int = 50) -> list[sqlite3.Row]:
    """This project's runs, newest first.

    `started_at` has one-second resolution, so ordering by it alone leaves runs
    that started in the same second in an order SQLite is free to choose. That
    is invisible on the runs page and load-bearing for app/crashloop.py, which
    walks this list from the newest backwards and stops at the first healthy
    run - with a tie broken the wrong way, a recovery run sorts *below* the
    failures it ended and the project stays held. `id DESC` is the tiebreak
    because run ids are issued in creation order by definition.
    """
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC, id DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()


def get_run(run_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def get_run_with_project(run_id: int) -> Optional[sqlite3.Row]:
    """A run joined with its project, so the run detail page can link back
    without a second lookup. `project_slug` is NULL for memory/reflect runs."""
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT runs.*, projects.title AS project_title, projects.slug AS project_slug, "
            "oneoff_tasks.title AS oneoff_title "
            "FROM runs LEFT JOIN projects ON projects.id = runs.project_id "
            "LEFT JOIN oneoff_tasks ON oneoff_tasks.id = runs.oneoff_id "
            "WHERE runs.id = ?",
            (run_id,),
        ).fetchone()


def list_recent_runs(
    limit: int = 50,
    offset: int = 0,
    project_id: Optional[int] = None,
    status: Optional[str] = None,
) -> list[sqlite3.Row]:
    """Portal-wide run feed, newest first, optionally narrowed. Ordering is by
    id as well as start time because several runs can share a whole-second
    timestamp and paging needs a total order."""
    where, params = _run_filter(project_id, status)
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT runs.*, projects.title AS project_title, projects.slug AS project_slug, "
            "oneoff_tasks.title AS oneoff_title "
            "FROM runs LEFT JOIN projects ON projects.id = runs.project_id "
            "LEFT JOIN oneoff_tasks ON oneoff_tasks.id = runs.oneoff_id "
            f"{where} ORDER BY runs.started_at DESC, runs.id DESC LIMIT ? OFFSET ?",
            (*params, max(1, limit), max(0, offset)),
        ).fetchall()


def count_recent_runs(project_id: Optional[int] = None, status: Optional[str] = None) -> int:
    where, params = _run_filter(project_id, status)
    conn = get_conn()
    with _LOCK:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM runs {where}", params).fetchone()
    return int(row["c"])


def _run_filter(project_id: Optional[int], status: Optional[str]) -> tuple[str, tuple]:
    clauses, params = [], []
    if project_id is not None:
        clauses.append("runs.project_id = ?")
        params.append(project_id)
    if status:
        clauses.append("runs.status = ?")
        params.append(status)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", tuple(params)


def runs_since(date: str, project_id: Optional[int] = None) -> list[sqlite3.Row]:
    """Every run started on or after the UTC date `date` (YYYY-MM-DD). Used by
    `app.usage` to build the daily time series."""
    conn = get_conn()
    sql = "SELECT * FROM runs WHERE started_at >= ?"
    params: list[Any] = [date]
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    with _LOCK:
        return conn.execute(sql + " ORDER BY started_at ASC", params).fetchall()


def update_run_activity(run_id: int, last_activity: str, events: int) -> None:
    """Record the newest rendered log line so the UI can show what a run is
    doing without reading its whole transcript."""
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "UPDATE runs SET last_activity = ?, events = ?, last_event_at = ? WHERE id = ?",
            (last_activity, events, now(), run_id),
        )
        conn.commit()


def _day_start_iso() -> str:
    """Imported lazily: app.daycycle reads settings through this module, so a
    module-level import here would be circular."""
    from app import daycycle

    return daycycle.day_start_iso()


def count_runs_today(project_id: Optional[int] = None) -> int:
    """Runs since the current portal day began (05:00 local by default), not
    since UTC midnight - see app/daycycle.py."""
    conn = get_conn()
    since = _day_start_iso()
    with _LOCK:
        if project_id is None:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM runs WHERE started_at >= ?", (since,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM runs WHERE started_at >= ? AND project_id = ?",
                (since, project_id),
            ).fetchone()
    return int(row["c"])


def has_completed_project_run(project_id: int) -> bool:
    """Whether any project-task run has ever finished ok here. This is what
    separates a raw idea (whose first pass should be a triage: name it, scope
    it, check prior art) from a project mid-flight (whose unapproved passes are
    plan runs)."""
    conn = get_conn()
    with _LOCK:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE project_id = ? AND status = 'ok' "
            "AND task IN ('triage', 'plan', 'build') LIMIT 1",
            (project_id,),
        ).fetchone()
    return row is not None


def runs_today_by_project() -> dict[int, int]:
    conn = get_conn()
    since = _day_start_iso()
    with _LOCK:
        rows = conn.execute(
            "SELECT project_id, COUNT(*) AS c FROM runs "
            "WHERE started_at >= ? AND project_id IS NOT NULL GROUP BY project_id",
            (since,),
        ).fetchall()
    return {int(row["project_id"]): int(row["c"]) for row in rows}


def is_run_running() -> bool:
    conn = get_conn()
    with _LOCK:
        row = conn.execute("SELECT 1 FROM runs WHERE status = 'running' LIMIT 1").fetchone()
    return row is not None


_ACTIVE_RUNS_SQL = (
    "SELECT runs.*, projects.title AS project_title, projects.slug AS project_slug, "
    "oneoff_tasks.title AS oneoff_title "
    "FROM runs LEFT JOIN projects ON projects.id = runs.project_id "
    "LEFT JOIN oneoff_tasks ON oneoff_tasks.id = runs.oneoff_id "
    # `id` breaks the tie: parallel launches in the same tick share a
    # whole-second `started_at`, and "the newest run" must still be one answer.
    "WHERE runs.status = 'running' ORDER BY runs.started_at DESC, runs.id DESC"
)


def active_runs() -> list[sqlite3.Row]:
    """Every run currently in flight, newest first, joined with its project so
    the UI can label each one without a second query. More than one at a time
    is normal now that runs are parallel across projects."""
    conn = get_conn()
    with _LOCK:
        return conn.execute(_ACTIVE_RUNS_SQL).fetchall()


def active_run() -> Optional[sqlite3.Row]:
    """The most recently started in-flight run. Kept for the places that can
    only speak about one run - "stop it" over Telegram, the NL context - where
    the newest is the sensible referent."""
    conn = get_conn()
    with _LOCK:
        return conn.execute(_ACTIVE_RUNS_SQL + " LIMIT 1").fetchone()


def count_running() -> int:
    conn = get_conn()
    with _LOCK:
        row = conn.execute("SELECT COUNT(*) AS c FROM runs WHERE status = 'running'").fetchone()
    return int(row["c"])


def running_project_ids() -> set[int]:
    """Projects with a run in flight. A project never gets two concurrent runs:
    they would share one workspace and one git checkout."""
    conn = get_conn()
    with _LOCK:
        rows = conn.execute(
            "SELECT DISTINCT project_id FROM runs WHERE status = 'running' AND project_id IS NOT NULL"
        ).fetchall()
    return {int(row["project_id"]) for row in rows}


def is_project_running(project_id: int) -> bool:
    return project_id in running_project_ids()


# --------------------------------------------------------------------------
# Run budget
# --------------------------------------------------------------------------

def _current_day() -> str:
    from app import daycycle

    return daycycle.current_day()


def bonus_runs_today() -> int:
    """The temporary run boost, but only if it was granted for the current
    portal day. It is never cleared explicitly - a stale date simply stops
    counting."""
    if (get_setting("bonus_runs_date") or "") != _current_day():
        return 0
    try:
        return max(0, int(get_setting("bonus_runs_count") or "0"))
    except ValueError:
        return 0


def grant_bonus_runs(extra: int) -> int:
    """Add `extra` runs to today's budget (negative clears down to zero).
    Returns the new bonus total."""
    total = max(0, bonus_runs_today() + extra)
    set_setting("bonus_runs_count", str(total))
    set_setting("bonus_runs_date", _current_day())
    return total


def base_max_runs() -> int:
    try:
        return max(0, int(get_setting("max_runs_per_day") or "8"))
    except ValueError:
        return 8


def effective_max_runs() -> int:
    return base_max_runs() + bonus_runs_today()


def max_parallel_runs() -> int:
    """How many agent runs may be in flight at once. 1 restores the old
    strictly-serial behaviour."""
    try:
        return max(1, int(get_setting("max_parallel_runs") or "2"))
    except ValueError:
        return 2


def last_run_ended_at() -> Optional[str]:
    conn = get_conn()
    with _LOCK:
        row = conn.execute(
            "SELECT ended_at FROM runs WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1"
        ).fetchone()
    return row["ended_at"] if row else None


def last_run_started_at() -> Optional[str]:
    conn = get_conn()
    with _LOCK:
        row = conn.execute(
            "SELECT started_at FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return row["started_at"] if row else None


def runs_active_since(since_iso: str) -> list[sqlite3.Row]:
    """Runs whose activity touches the window starting at `since_iso`.

    A run that ended before the window is excluded; one still running
    (`ended_at IS NULL`) or that ended inside/after it is kept, even if it
    started before the window - `app.pacing.duty_cycle` clips the overhang. Used
    by the saturation guard to measure how continuously the portal has been
    running."""
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT started_at, ended_at FROM runs "
            "WHERE ended_at IS NULL OR ended_at >= ? ORDER BY started_at ASC",
            (since_iso,),
        ).fetchall()


# --------------------------------------------------------------------------
# One-off tasks
# --------------------------------------------------------------------------

ONEOFF_TITLE_MAX = 80


def _oneoff_title(text: str) -> str:
    """A list-page title cut from the first message. The first line usually IS
    the task ('fix the cron mail on the home server'), so take it and clip - the
    full text is the first message of the transcript either way."""
    first_line = ""
    for line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip().lstrip("#>-*• ").strip()
        if line:
            first_line = line
            break
    if not first_line:
        return "untitled task"
    if len(first_line) > ONEOFF_TITLE_MAX:
        first_line = first_line[: ONEOFF_TITLE_MAX - 3].rstrip() + "..."
    return first_line


def create_oneoff(text: str) -> sqlite3.Row:
    """A new one-off task, with `text` queued as its first message."""
    conn = get_conn()
    ts = now()
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO oneoff_tasks (title, status, created_at, updated_at) "
            "VALUES (?, 'open', ?, ?)",
            (_oneoff_title(text), ts, ts),
        )
        task_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO oneoff_messages (task_id, role, content_md, ts) VALUES (?, 'wes', ?, ?)",
            (task_id, text, ts),
        )
        conn.commit()
        return conn.execute("SELECT * FROM oneoff_tasks WHERE id = ?", (task_id,)).fetchone()


def get_oneoff(task_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute("SELECT * FROM oneoff_tasks WHERE id = ?", (task_id,)).fetchone()


def list_oneoffs(status: Optional[str] = None) -> list[sqlite3.Row]:
    """Tasks newest-activity first, with the last message and its author so the
    list page can show where each exchange stands without a query per row."""
    conn = get_conn()
    where = "WHERE t.status = ?" if status else ""
    args = (status,) if status else ()
    with _LOCK:
        return conn.execute(
            f"""SELECT t.*,
                       (SELECT content_md FROM oneoff_messages m
                         WHERE m.task_id = t.id ORDER BY m.id DESC LIMIT 1) AS last_message,
                       (SELECT role FROM oneoff_messages m
                         WHERE m.task_id = t.id ORDER BY m.id DESC LIMIT 1) AS last_role
                FROM oneoff_tasks t {where}
                ORDER BY t.updated_at DESC, t.id DESC""",
            args,
        ).fetchall()


def count_open_oneoffs() -> int:
    conn = get_conn()
    with _LOCK:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM oneoff_tasks WHERE status = 'open'"
        ).fetchone()
    return int(row["n"])


def set_oneoff_status(task_id: int, status: str) -> None:
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "UPDATE oneoff_tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now(), task_id),
        )
        conn.commit()


def set_oneoff_session(task_id: int, cli_session_id: Optional[str]) -> None:
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "UPDATE oneoff_tasks SET cli_session_id = ? WHERE id = ?",
            (cli_session_id, task_id),
        )
        conn.commit()


def add_oneoff_message(
    task_id: int, role: str, content_md: str, run_id: Optional[int] = None
) -> int:
    conn = get_conn()
    ts = now()
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO oneoff_messages (task_id, role, content_md, run_id, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, role, content_md, run_id, ts),
        )
        # Agent/system messages count as activity too - the list page sorts on
        # it, and "newest exchange first" is the order Wes actually wants.
        conn.execute("UPDATE oneoff_tasks SET updated_at = ? WHERE id = ?", (ts, task_id))
        conn.commit()
        return int(cur.lastrowid)


def list_oneoff_messages(task_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT * FROM oneoff_messages WHERE task_id = ? ORDER BY id ASC", (task_id,)
        ).fetchall()


def pending_oneoff_messages(task_id: int) -> list[sqlite3.Row]:
    """Wes's messages no agent has seen yet, oldest first. These are what the
    next run on this task is started for."""
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT * FROM oneoff_messages WHERE task_id = ? AND role = 'wes' "
            "AND delivered_at IS NULL ORDER BY id ASC",
            (task_id,),
        ).fetchall()


def mark_oneoff_delivered(message_ids: Sequence[int]) -> None:
    if not message_ids:
        return
    conn = get_conn()
    ts = now()
    with _LOCK:
        conn.executemany(
            "UPDATE oneoff_messages SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
            [(ts, int(mid)) for mid in message_ids],
        )
        conn.commit()


def oneoff_running(task_id: int) -> bool:
    conn = get_conn()
    with _LOCK:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE oneoff_id = ? AND status = 'running' LIMIT 1",
            (task_id,),
        ).fetchone()
    return row is not None


def latest_oneoff_run(task_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute(
            "SELECT * FROM runs WHERE oneoff_id = ? ORDER BY started_at DESC, id DESC LIMIT 1",
            (task_id,),
        ).fetchone()


# --------------------------------------------------------------------------
# Suggestions
# --------------------------------------------------------------------------

def add_suggestion(title: str, description: str = "") -> sqlite3.Row:
    conn = get_conn()
    with _LOCK:
        cur = conn.execute(
            "INSERT INTO suggestions (ts, title, description, status) VALUES (?, ?, ?, 'proposed')",
            (now(), title, description),
        )
        conn.commit()
        return conn.execute("SELECT * FROM suggestions WHERE id = ?", (cur.lastrowid,)).fetchone()


def list_suggestions() -> list[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute("SELECT * FROM suggestions ORDER BY ts DESC").fetchall()


def get_suggestion(suggestion_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    with _LOCK:
        return conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()


def set_suggestion_status(suggestion_id: int, status: str) -> None:
    conn = get_conn()
    with _LOCK:
        conn.execute("UPDATE suggestions SET status = ? WHERE id = ?", (status, suggestion_id))
        conn.commit()


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def get_setting(key: str) -> Optional[str]:
    conn = get_conn()
    with _LOCK:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    with _LOCK:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def get_all_settings() -> dict[str, str]:
    conn = get_conn()
    with _LOCK:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


# --------------------------------------------------------------------------
# Seed data (only called once, when the DB file is first created)
# --------------------------------------------------------------------------

def _seed_data() -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    project = create_project(
        title="Project Portal",
        description=(
            "This tool itself - the meta-project for self-improvement. "
            f"Workspace note: source code lives at {config.APP_ROOT} "
            "(outside the per-project data/projects workspace, since this project "
            "IS the portal application)."
        ),
        kind="software",
        # Parked until the owner engages: backlog is never scheduled, so a
        # fresh install does not start improving itself before anyone looks.
        stage="backlog",
        priority=5,
        slug="project-portal",
    )
    add_journal(
        project["id"],
        "system",
        "note",
        (
            "**Project Portal v1 seeded.**\n\n"
            "v1 includes: dashboard with quick-add ideas, per-project pages with "
            "status/priority controls, journal timeline, open questions with "
            "answer forms, workspace file browser, a background worker that "
            "invokes `claude -p` headlessly to triage/plan/build projects, "
            "Telegram + ntfy notifications, a memory system (profile/learnings/"
            "suggestions), and a settings page."
        ),
    )

    project_ws = config.PROJECTS_DIR / "project-portal"
    project_ws.mkdir(parents=True, exist_ok=True)
    (project_ws / "NOTE.md").write_text(
        f"This project's real source code lives at {config.APP_ROOT}, "
        "not in this workspace directory. This directory exists for consistency "
        "with the per-project workspace model but the agent should treat "
        f"{config.APP_ROOT} as the actual codebase when working on this project.\n"
    )

    create_question(
        project["id"],
        "You mentioned an existing Telegram bot - paste its bot token (from "
        "@BotFather) and your Telegram chat id into Settings "
        f"(http://{config.HOST_LABEL}:{config.PORT}/settings) to enable Telegram "
        "notifications and replies. Reply 'skip' to stick with ntfy.",
        context="Initial setup question seeded at first run.",
    )

    _seed_memory_files()


def _seed_memory_files() -> None:
    """Write the starter memory files - but never over ones that exist.

    The seed runs when the DATABASE is new, but the memory files live beside
    the database, not in it - and on 2026-07-21 a boot that found a fresh DB
    next to real memory re-seeded all three files and erased text Wes had
    typed about himself, unrecoverably. The DB being new says nothing about
    the files being disposable.
    """
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    if not config.PROFILE_MD.exists():
        # Deliberately near-empty, and deliberately *not* a description of
        # whoever wrote the portal. This file is the owner's own context, read
        # into every run's prompt; the reflect pass grows it from their real
        # journals. Seeding it with invented facts would put confident
        # falsehoods in front of every agent from the first run onwards.
        config.PROFILE_MD.write_text(
            f"# Profile: {config.OWNER}\n\n"
            "Who the owner is and what they care about: how agents should "
            "behave, the machines and services they run, and the way they like "
            "things built. Durable, big-picture facts only - run-by-run detail "
            "belongs in learnings.md.\n\n"
            "The daily reflect pass fills this in from real journals and notes. "
            "Edit it by hand any time (/memory in the web UI); nothing "
            "overwrites it.\n",
            encoding="utf-8",
        )
    if not config.LEARNINGS_MD.exists():
        config.LEARNINGS_MD.write_text(
            f"# Learnings about {config.OWNER}\n\n"
            "Timestamped bullets get appended below by the daily reflect job and "
            "after individual agent runs.\n\n",
            encoding="utf-8",
        )
    if not config.SUGGESTIONS_MD.exists():
        config.SUGGESTIONS_MD.write_text(
            "# Suggestions log\n\n"
            "This file mirrors the `suggestions` table for easy reading; the "
            "database is the source of truth (see /memory in the web UI).\n\n",
            encoding="utf-8",
        )

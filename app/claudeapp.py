"""Projects built in the Claude app on another machine, with the portal as their record.

Wes, 2026-10-02: some projects cannot be built on this server - a Mac or iOS
app needs Xcode - so they are built in the Claude app on whichever machine has
the tools. A plugin there links a local folder to a portal project and talks to
`/api/ext/v1/` (app/extapi.py). The portal keeps the board, journal, todos,
questions and memory, and hands each Claude app session the project's context.

The decisions he made, and where each one is enforced:

1. **Record and context only.** A project whose code lives only on another
   machine is never scheduled - `hosted_elsewhere` is the one predicate, and
   the worker's pickers, note-triggered reactivation and the "could a run
   start now" check all read it. A note or an answer on such a project goes
   into its next context instead (`context_md`'s "since" section).
2. **Stars live on the portal, per person**, so every machine's sidebar shows
   the same ones first.
3. **Search finds done and abandoned projects**, which the sidebar never shows.

What "hosted elsewhere" means: the project has at least one linked location and
no git checkout in its workspace here. A run is the only thing that creates
that checkout, and a hosted-elsewhere project never gets a run, so the answer is
stable; an existing portal project later linked to a Mac folder keeps its
workspace and keeps being scheduled, which is the "if it also has a workspace
here, normal scheduling applies" half of decision 1. Moving a project is adding
or removing locations.

Hosts and paths are whatever the plugin reported. They are runtime data and
never appear in this source (leakscan).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app import config, db

CLOSED_STAGES = ("done", "abandoned")

# A session nobody posted an end for (a crash, a closed lid) counts as ended
# after this long without an update.
SESSION_TIMEOUT_HOURS = 6

# The context goes into the first message of every Claude app session.
CONTEXT_CAP = 6000

SUMMARY_CHARS = 160


# ----------------------------------------------------------------- helpers


def _one_line(text: Optional[str], limit: int = SUMMARY_CHARS) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def iso(value: Any) -> Optional[str]:
    """An ISO-8601 time the plugin sent, normalized to the portal's own UTC
    format so string comparison is chronological. None for blank or junk."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    return db.get_conn()


# --------------------------------------------------------------- locations


def has_workspace_checkout(project: sqlite3.Row) -> bool:
    return (config.PROJECTS_DIR / str(project["slug"]) / ".git").exists()


def locations(project_id: int) -> list[sqlite3.Row]:
    with db._LOCK:
        return _conn().execute(
            "SELECT * FROM project_locations WHERE project_id = ? "
            "ORDER BY last_seen DESC, host, path",
            (int(project_id),),
        ).fetchall()


def hosted_elsewhere(project: Optional[sqlite3.Row]) -> bool:
    """Is this project's code only on other machines? See the module docstring.
    The worker never schedules one that is."""
    if project is None:
        return False
    return bool(locations(int(project["id"]))) and not has_workspace_checkout(project)


def hosted_elsewhere_ids() -> set[int]:
    """Every hosted-elsewhere project's id, in one query, for the pickers."""
    with db._LOCK:
        rows = _conn().execute(
            "SELECT DISTINCT p.id, p.slug FROM projects p "
            "JOIN project_locations l ON l.project_id = p.id"
        ).fetchall()
    return {int(r["id"]) for r in rows if not has_workspace_checkout(r)}


def link(project_id: int, host: str, path: str, remote: str = "", head: str = "") -> None:
    """Record (or refresh) a folder this project lives in."""
    with db._LOCK:
        conn = _conn()
        conn.execute(
            "INSERT INTO project_locations (project_id, host, path, remote, head, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id, host, path) DO UPDATE SET "
            "remote = CASE WHEN excluded.remote != '' THEN excluded.remote ELSE remote END, "
            "head = CASE WHEN excluded.head != '' THEN excluded.head ELSE head END, "
            "last_seen = excluded.last_seen",
            (int(project_id), host, path, remote or "", head or "", db.now()),
        )
        conn.commit()


def unlink(project_id: int, host: str, path: str) -> bool:
    with db._LOCK:
        conn = _conn()
        cur = conn.execute(
            "DELETE FROM project_locations WHERE project_id = ? AND host = ? AND path = ?",
            (int(project_id), host, path),
        )
        conn.commit()
    return cur.rowcount > 0


def elsewhere_line(project: sqlite3.Row) -> str:
    """One markdown line naming where a hosted-elsewhere project's files are,
    or '' for a project built here."""
    if not hosted_elsewhere(project):
        return ""
    places = ", ".join(f"`{r['host']}:{r['path']}`" for r in locations(int(project["id"])))
    return f"**Lives on:** {places} (built in the Claude app, not on this server)"


# ---------------------------------------------------------------- sessions


def close_stale_sessions() -> int:
    """End every session that has been silent for SESSION_TIMEOUT_HOURS.

    Called on read, so a session from a laptop that went to sleep stops
    reading as live without any background job."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=SESSION_TIMEOUT_HOURS)
    ).isoformat(timespec="seconds")
    with db._LOCK:
        conn = _conn()
        cur = conn.execute(
            "UPDATE app_sessions SET ended_at = updated_at, timed_out = 1 "
            "WHERE ended_at IS NULL AND updated_at < ?",
            (cutoff,),
        )
        conn.commit()
    return cur.rowcount


def sessions(project_id: int, limit: int = 20) -> list[sqlite3.Row]:
    close_stale_sessions()
    with db._LOCK:
        return _conn().execute(
            "SELECT * FROM app_sessions WHERE project_id = ? "
            "ORDER BY COALESCE(ended_at, started_at) DESC LIMIT ?",
            (int(project_id), int(limit)),
        ).fetchall()


def get_session(session_id: str) -> Optional[sqlite3.Row]:
    with db._LOCK:
        return _conn().execute(
            "SELECT * FROM app_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()


def last_session_at(project_id: int) -> Optional[str]:
    with db._LOCK:
        row = _conn().execute(
            "SELECT MAX(COALESCE(ended_at, started_at)) AS t FROM app_sessions "
            "WHERE project_id = ?",
            (int(project_id),),
        ).fetchone()
    return row["t"] if row else None


def _commits(raw: Any) -> list[dict]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and str(item.get("sha") or "").strip():
            out.append({"sha": str(item["sha"]).strip(),
                        "subject": " ".join(str(item.get("subject") or "").split())})
    return out


def session_entry(host: str, title: str, commits: list[dict], summary: str) -> str:
    """The journal entry an ended session writes, the way a run report would."""
    lines = [f"**Claude app session on {host or 'another machine'}:** {title or 'untitled'}"]
    if commits:
        lines.append("")
        lines += [f"- `{c['sha'][:7]}` {c['subject']}" for c in commits]
    if summary:
        lines += ["", summary.strip()]
    return "\n".join(lines)


def upsert_session(project: sqlite3.Row, body: dict, host: str,
                   person_id: Optional[int]) -> sqlite3.Row:
    """Record one Claude app session (posted at start and again at end).

    A field the plugin leaves out or sends as null keeps its stored value, so
    the end post does not have to repeat the start. When the session has ended
    with commits or a summary it is journaled, attributed to the person; a
    second end post rewrites that same entry rather than adding another.
    """
    sid = str(body.get("session_id") or "").strip()
    pid = int(project["id"])
    old = get_session(sid)
    if old is not None and int(old["project_id"]) != pid:
        raise ValueError("That session id belongs to a different project.")

    def pick(key: str, stored: str = "") -> str:
        value = body.get(key)
        if value is None:
            return stored
        return " ".join(str(value).split()) if key == "title" else str(value).strip()

    host = pick("host", old["host"] if old else "") or host
    path = pick("path", old["path"] if old else "")
    title = pick("title", old["title"] if old else "")
    started = iso(body.get("started_at")) or (old["started_at"] if old else db.now())
    ended = iso(body.get("ended_at")) or (old["ended_at"] if old and not old["timed_out"] else None)
    head_start = pick("head_start", old["head_start"] if old else "")
    head_end = pick("head_end", old["head_end"] if old else "")
    commits = _commits(body["commits"]) if "commits" in body and body["commits"] is not None \
        else (json.loads(old["commits_json"]) if old else [])
    summary = body.get("summary") if body.get("summary") is not None else (old["summary"] if old else None)
    summary = str(summary).strip() if summary else None
    who = person_id if person_id is not None else (old["person_id"] if old else None)

    with db._LOCK:
        conn = _conn()
        conn.execute(
            "INSERT INTO app_sessions (session_id, project_id, host, path, title, person_id, "
            "started_at, ended_at, timed_out, head_start, head_end, commits_json, summary, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET host = excluded.host, path = excluded.path, "
            "title = excluded.title, person_id = excluded.person_id, "
            "started_at = excluded.started_at, ended_at = excluded.ended_at, timed_out = 0, "
            "head_start = excluded.head_start, head_end = excluded.head_end, "
            "commits_json = excluded.commits_json, summary = excluded.summary, "
            "updated_at = excluded.updated_at",
            (sid, pid, host, path, title, who, started, ended, head_start, head_end,
             json.dumps(commits), summary, db.now()),
        )
        conn.commit()

    if host and path:
        with db._LOCK:
            conn = _conn()
            conn.execute(
                "UPDATE project_locations SET last_seen = ?, "
                "head = CASE WHEN ? != '' THEN ? ELSE head END "
                "WHERE project_id = ? AND host = ? AND path = ?",
                (db.now(), head_end or head_start, head_end or head_start, pid, host, path),
            )
            conn.commit()

    row = get_session(sid)
    if ended and (commits or summary):
        text = session_entry(host, title, commits, summary or "")
        if row["journal_id"]:
            with db._LOCK:
                conn = _conn()
                conn.execute("UPDATE journal SET content_md = ? WHERE id = ?",
                             (text, int(row["journal_id"])))
                conn.commit()
        else:
            entry = db.add_journal(pid, "user", "session", text, person_id=who)
            with db._LOCK:
                conn = _conn()
                conn.execute("UPDATE app_sessions SET journal_id = ? WHERE session_id = ?",
                             (entry, sid))
                conn.commit()
        db.update_project(pid, updated_at=db.now())
        row = get_session(sid)
    return row


def session_dict(row: sqlite3.Row) -> dict:
    return {
        "session_id": row["session_id"],
        "host": row["host"],
        "path": row["path"],
        "title": row["title"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "timed_out": bool(row["timed_out"]),
        "head_start": row["head_start"] or None,
        "head_end": row["head_end"] or None,
        "commits": json.loads(row["commits_json"] or "[]"),
        "summary": row["summary"],
    }


# ------------------------------------------------------------------- stars


def stars(person_id: int) -> list[sqlite3.Row]:
    """This person's starred projects, in star order."""
    with db._LOCK:
        return _conn().execute(
            "SELECT p.*, s.position AS star_position FROM stars s "
            "JOIN projects p ON p.id = s.project_id WHERE s.person_id = ? "
            "ORDER BY s.position, s.starred_at",
            (int(person_id),),
        ).fetchall()


def star_positions(person_id: int) -> dict[int, int]:
    """project id -> 0-based place in this person's star order."""
    return {int(r["id"]): i for i, r in enumerate(stars(person_id))}


def star(person_id: int, project_id: int) -> None:
    """Star at the end of the order. Starring a starred project is a no-op."""
    with db._LOCK:
        conn = _conn()
        conn.execute(
            "INSERT OR IGNORE INTO stars (person_id, project_id, position, starred_at) "
            "VALUES (?, ?, (SELECT COALESCE(MAX(position), -1) + 1 FROM stars "
            "WHERE person_id = ?), ?)",
            (int(person_id), int(project_id), int(person_id), db.now()),
        )
        conn.commit()


def unstar(person_id: int, project_id: int) -> None:
    with db._LOCK:
        conn = _conn()
        conn.execute("DELETE FROM stars WHERE person_id = ? AND project_id = ?",
                     (int(person_id), int(project_id)))
        conn.commit()


def set_star_order(person_id: int, project_ids: Iterable[int]) -> None:
    """Replace this person's stars with exactly these, in this order."""
    seen: list[int] = []
    for pid in project_ids:
        if int(pid) not in seen:
            seen.append(int(pid))
    ts = db.now()
    with db._LOCK:
        conn = _conn()
        kept = {int(r["project_id"]): r["starred_at"] for r in conn.execute(
            "SELECT project_id, starred_at FROM stars WHERE person_id = ?", (int(person_id),))}
        conn.execute("DELETE FROM stars WHERE person_id = ?", (int(person_id),))
        conn.executemany(
            "INSERT INTO stars (person_id, project_id, position, starred_at) VALUES (?, ?, ?, ?)",
            [(int(person_id), pid, i, kept.get(pid, ts)) for i, pid in enumerate(seen)],
        )
        conn.commit()


# ---------------------------------------------------------------- listings


def score(project: sqlite3.Row, query: str) -> int:
    """How well a project matches search text; 0 is no match.

    Title outweighs slug outweighs description outweighs the original idea, and
    a field that starts with the text counts triple. A multi-word query also
    matches a field holding every word in any order."""
    q = " ".join((query or "").lower().split())
    if not q:
        return 0
    words = q.split()
    total = 0
    for text, weight in ((project["title"], 8), (project["slug"], 6),
                         (project["description"], 2), (project["initial_idea"], 1)):
        text = (text or "").lower()
        if q in text:
            total += weight * (3 if text.startswith(q) else 1)
        elif all(w in text for w in words):
            total += weight
    return total


def search(rows: Iterable[sqlite3.Row], query: str) -> list[sqlite3.Row]:
    """Matching rows, best first; ties keep the order they came in."""
    scored = [(score(r, query), i, r) for i, r in enumerate(rows)]
    return [r for s, _, r in sorted((x for x in scored if x[0] > 0),
                                    key=lambda x: (-x[0], x[1]))]


def summary(project: sqlite3.Row, person_id: Optional[int],
            positions: Optional[dict[int, int]] = None) -> dict:
    """The project object every endpoint returns."""
    pid = int(project["id"])
    if positions is None:
        positions = star_positions(person_id) if person_id is not None else {}
    parent_id = db.parent_id_of(project)
    parent = db.get_project(parent_id) if parent_id else None
    open_q = db.count_open_questions(pid)
    place = positions.get(pid)
    return {
        "slug": project["slug"],
        "title": project["title"],
        "stage": project["stage"],
        "paused": db.is_paused(project),
        "shelf": db.project_shelf(project, open_q),
        "parent": parent["slug"] if parent is not None else None,
        "summary": _one_line(project["description"] or project["initial_idea"]),
        "starred": place is not None,
        "star_position": place,
        "hosted_elsewhere": hosted_elsewhere(project),
        "locations": [
            {"host": r["host"], "path": r["path"], "remote": r["remote"] or None,
             "head": r["head"] or None, "last_seen": r["last_seen"]}
            for r in locations(pid)
        ],
        "open_questions": open_q,
        "open_todos": db.count_open_todos(pid),
        "updated_at": project["updated_at"],
        "last_session_at": last_session_at(pid),
    }


# ----------------------------------------------------------------- context


def _since_lines(project: sqlite3.Row, since: Optional[str]) -> list[str]:
    """What arrived after `since`: notes and answers on this project, and runs
    by the portal's agent on related ones. Without `since`, only the notes no
    session or run has seen yet - a first session should not be handed the
    whole history twice."""
    from app import crossproject, notes  # local: crossproject imports this module

    pid = int(project["id"])
    out: list[str] = []
    if since:
        with db._LOCK:
            rows = _conn().execute(
                "SELECT * FROM journal WHERE project_id = ? AND ts > ? AND author = 'user' "
                "AND kind IN ('note', 'answer') ORDER BY ts ASC",
                (pid, since),
            ).fetchall()
    else:
        rows = list(notes.pending(pid))
    for row in rows:
        body = _one_line(row["content_md"], 600)
        who = _author(row)
        if row["kind"] == "answer":
            out.append(f"- {row['ts'][:10]} {who} answered: {body}")
        else:
            out.append(f"- {row['ts'][:10]} note from {who}: {body}")
    if since:
        try:
            near = crossproject.related(pid)
        except Exception:  # noqa: BLE001 - related projects are a nicety here
            near = []
        for other in near:
            with db._LOCK:
                runs = _conn().execute(
                    "SELECT * FROM journal WHERE project_id = ? AND ts > ? "
                    "AND author = 'agent' AND kind = 'progress' ORDER BY ts DESC LIMIT 2",
                    (int(other["id"]), since),
                ).fetchall()
            for row in runs:
                head = next((ln.lstrip("# ").strip() for ln in (row["content_md"] or "").splitlines()
                             if ln.strip()), "")
                out.append(f"- {row['ts'][:10]} the portal's agent on `{other['slug']}`: "
                           f"{_one_line(head, 200)}")
    return out


def _author(row: sqlite3.Row) -> str:
    from app import people

    try:
        pid = row["person_id"]
    except (IndexError, KeyError):
        pid = None
    return people.name_of(people.get(pid) if pid else None)


def _question_lines(project_id: int) -> list[str]:
    from app import quickreplies

    out = []
    for q in db.open_questions(project_id):
        options = quickreplies.decode(q["quick_options"])
        line = f"- (#{q['id']}) {_one_line(q['question'], 500)}"
        if options:
            line += f" (options: {', '.join(options)})"
        out.append(line)
    return out


def context_md(project: sqlite3.Row, since: Optional[str] = None) -> str:
    """What a Claude app session should know when it opens the folder.

    `crossproject.render` (the same digest other projects' runs read), with
    what arrived since the last session and the open questions put first, and
    the parent's one-paragraph summary added. Kept under CONTEXT_CAP: the
    journal is cut before anything else."""
    from app import crossproject

    head: list[str] = []
    fresh = _since_lines(project, since)
    if fresh:
        head += ["## New since your last session", *fresh, ""]
    questions = _question_lines(int(project["id"]))
    if questions:
        head += ["## Open questions waiting on a person",
                 "Answer them on the portal; the answer reaches the next session.",
                 *questions, ""]
    parent_id = db.parent_id_of(project)
    parent = db.get_project(parent_id) if parent_id else None
    tail: list[str] = []
    if parent is not None:
        tail = ["", f"## Its parent: {parent['title']} (`{parent['slug']}`)",
                _one_line(parent["description"] or parent["initial_idea"], 600)]

    def build(entries: int) -> str:
        body = crossproject.render(project, journal_entries=entries)
        title, _, rest = body.partition("\n")
        parts = [title, ""] + head + [rest.lstrip("\n")] + tail
        return "\n".join(parts).strip() + "\n"

    for entries in range(crossproject.JOURNAL_ENTRIES, -1, -1):
        text = build(entries)
        if len(text) <= CONTEXT_CAP:
            return text
    return text[: CONTEXT_CAP - 1] + "…"


# ------------------------------------------------------------- the portal page


def page_view(project: sqlite3.Row) -> Optional[dict]:
    """What the project page shows about the Claude app side, or None when
    there is nothing to show (no linked folder and no session ever)."""
    pid = int(project["id"])
    places = locations(pid)
    rows = sessions(pid, limit=8)
    if not places and not rows:
        return None
    return {
        "elsewhere": bool(places) and not has_workspace_checkout(project),
        "hosts": sorted({str(r["host"]) for r in places}),
        "locations": places,
        "sessions": [
            {**session_dict(r), "live": r["ended_at"] is None}
            for r in rows
        ],
    }


# ----------------------------------------------------------------- creating


def create(title: str, description: str, parent: Optional[sqlite3.Row], host: str,
           path: str, remote: str, head: str, person_id: Optional[int]) -> sqlite3.Row:
    """A new project for a folder the portal has never seen.

    Active, title locked (the plugin's title is one a person picked or
    confirmed), no workspace and no run - decision 1."""
    project = db.create_project(
        title, description=description, stage="active",
        parent_id=int(parent["id"]) if parent is not None else None,
        person_id=person_id, title_locked=True,
    )
    pid = int(project["id"])
    if parent is not None:
        from app import people

        for member in people.member_ids(int(parent["id"])):
            people.add_member(pid, member)
    link(pid, host, path, remote, head)
    db.add_journal(
        pid, "user", "status",
        f"Linked from the Claude app. Lives on `{host}` at `{path}`, and is built "
        f"there: the portal keeps its record and context but schedules no runs on it.",
        person_id=person_id,
    )
    return db.get_project(pid)

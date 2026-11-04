"""Part 2 of the Claude app plugin's API: starting work and talking to agents.

The wire contract is the plugin's SPEC-runs.md (Wes, 2026-11-04: "Kick off
builds there in my project portal tool ... without me having to be the middle
man"); the routes are in app/extapi.py and this is what they stand on. Every
operation here is a door the web UI already has, reached from a program:

- a run is "run agent now" (`worker.start_requested_run`, the same guards),
  attributed to the person and the machine that asked;
- instructions for a run, and a message to one, are project notes - the way a
  note or a diff comment reaches an agent today - so the portal decides how the
  agent hears them rather than the API replacing its prompt;
- a task is a one-off task (app/oneoff.py);
- an ask is the project page's read-only ask (app/ask.py).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from app import ask, config, db, midrun, people, revert, worker

# The run statuses the contract speaks, from the ones the runs table stores.
_STATUS = {"ok": "done", "error": "failed", "timeout": "failed", "cancelled": "cancelled"}


def run_url(run_id: int) -> str:
    return f"{config.SITE.base_url}/run/{int(run_id)}"


def task_url(task_id: int) -> str:
    return f"{config.SITE.base_url}/tasks/{int(task_id)}"


def run_status(row: sqlite3.Row) -> str:
    status = str(row["status"] or "")
    if status == "running":
        return "paused" if midrun.is_paused(int(row["id"])) else "running"
    if status == "paused":
        # Parked for the usage window (app/limitpause.py): it comes back on
        # its own, which to a caller is a pause, not an ending.
        return "paused"
    return _STATUS.get(status, status)


def _commits(row: sqlite3.Row) -> list[dict]:
    try:
        plan = revert.landed(row)
    except Exception:  # noqa: BLE001 - a git hiccup must not break a status read
        plan = None
    if plan is None:
        return []
    return [{"sha": c.sha, "subject": c.subject} for c in plan.commits]


def _started_by(row: sqlite3.Row) -> Optional[dict]:
    person_id = db._row_get(row, "started_by_person_id")  # noqa: SLF001
    host = db._row_get(row, "started_by_host")  # noqa: SLF001
    if not person_id and not host:
        return None
    person = people.get(int(person_id)) if person_id else None
    return {"person": people.name_of(person) if person else None, "host": host or None}


def run_dict(row: sqlite3.Row) -> dict:
    """The contract's RUN. `project` is the slug, null for a one-off task's run."""
    if "project_slug" not in row.keys():
        row = db.get_run_with_project(int(row["id"])) or row
    project_id = row["project_id"]
    summary = db._row_get(row, "report_summary") or row["summary"]  # noqa: SLF001
    return {
        "id": int(row["id"]),
        "project": db._row_get(row, "project_slug"),  # noqa: SLF001
        "task": int(row["oneoff_id"]) if row["oneoff_id"] else None,
        "kind": row["task"],
        "status": run_status(row),
        "started_at": row["started_at"],
        "finished_at": row["ended_at"],
        "summary": summary or None,
        "commits": _commits(row) if project_id else [],
        "questions": (db.questions_between(int(project_id), row["started_at"], row["ended_at"])
                      if project_id else []),
        "url": run_url(int(row["id"])),
        "started_by": _started_by(row),
    }


# ------------------------------------------------------------------- runs


def start(project: sqlite3.Row, person: sqlite3.Row, host: str,
          kind: str = "auto", instructions: str = "") -> "int | worker.StartRefused":
    """Start a run for the Claude app. Instructions are filed as a note just
    before the run row is made, so the run's prompt reads them like any other
    note - and a refused start files nothing."""
    where = f" `{host}`" if host else ""

    def file_instructions(fresh: sqlite3.Row) -> None:
        if instructions:
            db.add_journal(
                int(fresh["id"]), "user", "note",
                f"**From the Claude app on{where or ' another machine'}, for this run:**\n\n"
                f"{instructions}",
                person_id=int(person["id"]),
            )

    outcome = worker.start_requested_run(
        project, kind, via=f" from the Claude app on{where or ' another machine'}",
        before_spawn=file_instructions,
    )
    if isinstance(outcome, int):
        db.set_run_started_by(outcome, int(person["id"]), host)
    return outcome


async def message(row: sqlite3.Row, person: sqlite3.Row, host: str, text: str) -> str:
    """Steer or comment on a run, the way its page lets a person. Returns how
    the message travels: `now` (handed to the live agent at its next tool
    call), `next_run` (filed as a note the next run reads) or `task` (a reply
    on the one-off task the run belongs to)."""
    where = f" on `{host}`" if host else ""
    if not row["project_id"]:
        task_id = int(row["oneoff_id"])
        db.add_oneoff_message(task_id, "wes", text)
        worker.spawn_oneoff(task_id)
        return "task"
    project = db.get_project(int(row["project_id"]))
    live = row["status"] == "running" and midrun.can_hear(int(row["id"]))
    db.add_journal(
        int(project["id"]), "user", "note",
        f"**From the Claude app{where}, about run #{row['id']}:**\n\n{text}",
        person_id=int(person["id"]), hear_now=live,
    )
    if live:
        return "now"
    if row["status"] != "running":
        # The same thing a diff comment on a finished run does: a note on a
        # project put down wakes it.
        await worker.reactivate_on_note(project)
    return "next_run"


# ------------------------------------------------------------------ tasks


def task_dict(row: sqlite3.Row) -> dict:
    task_id = int(row["id"])
    project_id = db._row_get(row, "project_id")  # noqa: SLF001
    project = db.get_project(int(project_id)) if project_id else None
    if db.oneoff_busy(task_id):
        status = "running"
    elif row["status"] == "archived":
        status = "archived"
    else:
        status = "open"
    messages = [
        {"from": "person" if m["role"] == "wes" else "agent", "role": m["role"],
         "text": m["content_md"], "at": m["ts"]}
        for m in db.list_oneoff_messages(task_id)
    ]
    return {"id": task_id, "status": status, "title": row["title"],
            "project": project["slug"] if project is not None else None,
            "created_at": row["created_at"], "messages": messages, "url": task_url(task_id)}


# ------------------------------------------------------------------- asks


def ask_dict(entry_id: int) -> Optional[dict]:
    """An ask by the journal id of its question, or None if there is none."""
    asked = db.get_journal(int(entry_id))
    if asked is None or asked["author"] != "user" or asked["kind"] != "ask":
        return None
    answer = db.ask_answer_for(int(entry_id))
    if answer is not None:
        text = answer["content_md"] or ""
        status = "failed" if text == ask.FAILED_ANSWER else "answered"
        return {"id": int(entry_id), "status": status, "answer": text}
    # No answer on record. In flight if the project's ask is still thinking
    # and this is the question it is thinking about - the newest one; otherwise
    # the process that was answering it is gone (a restart), and "pending"
    # would be a promise nothing will keep.
    project_id = int(asked["project_id"])
    if ask.pending(project_id) and latest_ask_id(project_id) == int(entry_id):
        return {"id": int(entry_id), "status": "pending", "answer": None}
    return {"id": int(entry_id), "status": "failed", "answer": None}


def latest_ask_id(project_id: int) -> Optional[int]:
    rows = [r for r in db.ask_thread(int(project_id)) if r["author"] == "user"]
    return int(rows[-1]["id"]) if rows else None

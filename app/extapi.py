"""`/api/ext/v1/` - the API the Claude app plugin talks to.

The wire contract is fixed by the plugin's SPEC (Wes, 2026-10-02); the rules
behind it live in app/claudeapp.py. JSON in and out, errors as a non-2xx status
with `{"error": "<sentence>"}`, no token: the tailnet is the boundary, exactly
as for every form route. The caller is resolved the way the web UI resolves
one (cookie, then `tailscale whois`); the plugin's `X-Portal-Host` header names
the machine for locations and sessions and is never treated as identity.

The plugin reads `features` from the root endpoint and turns off whatever is
missing, so a feature is listed here only once every route behind it exists.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from app import ask, claudeapp, db, extrun, midrun, notify, people, quickreplies, scope, worker

API_VERSION = 1
FEATURES = ("projects", "context", "link", "sessions", "journal", "todos", "questions", "stars",
            "runs", "tasks", "ask", "mcp")

router = APIRouter(prefix="/api/ext/v1")


class _Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _person(request: Request) -> sqlite3.Row:
    from app import main  # local: main imports this module

    return main.resolve_person(request)


def _host(request: Request, body: Optional[dict] = None) -> str:
    """The machine the plugin says it is on: the body's `host`, else the
    header. Free text the plugin chose, used only to label locations."""
    value = (body or {}).get("host") or request.headers.get("x-portal-host") or ""
    return " ".join(str(value).split())[:200]


async def _body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise _Refused(400, "The request body is not JSON.") from None
    if not isinstance(data, dict):
        raise _Refused(400, "The request body must be a JSON object.")
    return data


def _project(slug: str) -> sqlite3.Row:
    row = db.get_project_by_slug(str(slug or "").strip().lower())
    if row is None:
        raise _Refused(404, f"No project with slug {slug!r}.")
    return row


def _text(body: dict, key: str, required: bool = True, limit: int = 20000) -> str:
    value = body.get(key)
    text = str(value).strip() if value is not None else ""
    if required and not text:
        raise _Refused(400, f"`{key}` is required.")
    return text[:limit]


def _summary(project: sqlite3.Row, person: sqlite3.Row) -> dict:
    return claudeapp.summary(project, int(person["id"]))


def _question(row: sqlite3.Row) -> dict:
    status = str(row["status"] or "open")
    return {
        "id": int(row["id"]),
        "status": status,
        "answer": row["answer"] if status == "answered" else None,
        "question": row["question"],
        "options": quickreplies.decode(row["quick_options"]),
    }


def _todo(row: sqlite3.Row) -> dict:
    return {"id": int(row["id"]), "text": row["text"], "done": bool(row["done"]),
            "owner": row["owner"]}


def _wrap(handler):
    """Turn a `_Refused` raised anywhere in a handler into the contract's
    error shape, so no route can leak FastAPI's `{"detail": ...}`."""
    import functools

    @functools.wraps(handler)
    async def wrapped(*args: Any, **kwargs: Any):
        try:
            return await handler(*args, **kwargs)
        except _Refused as refused:
            return _error(refused.status, refused.message)

    return wrapped


# ------------------------------------------------------------------- root


@router.get("")
@router.get("/")
@_wrap
async def root(request: Request):
    return {"api": API_VERSION, "features": list(FEATURES)}


# --------------------------------------------------------------- projects


@router.get("/projects")
@_wrap
async def list_projects(request: Request):
    person = _person(request)
    q = " ".join(str(request.query_params.get("q") or "").split())
    include_closed = request.query_params.get("include_closed") == "1"
    try:
        limit = max(1, min(1000, int(request.query_params.get("limit") or 200)))
    except ValueError:
        raise _Refused(400, "`limit` must be a number.") from None
    seen = scope.visible_ids(person)
    rows = [r for r in db.list_projects(order_by="updated_at DESC") if int(r["id"]) in seen]
    if not include_closed:
        rows = [r for r in rows if r["stage"] not in claudeapp.CLOSED_STAGES]
    if q:
        rows = claudeapp.search(rows, q)
    positions = claudeapp.star_positions(int(person["id"]))
    return {"projects": [claudeapp.summary(r, int(person["id"]), positions)
                         for r in rows[:limit]]}


@router.post("/projects")
@_wrap
async def create_project(request: Request):
    """Two shapes. With a `path`, a project for a folder on the caller's
    machine (SPEC.md): built there, never run here. Without one - only allowed
    alongside a `brief` (SPEC-runs.md) - a project the portal builds itself in
    its own workspace, exactly as one typed into /ideas, and `start` puts its
    first agent on it."""
    body = await _body(request)
    person = _person(request)
    title = _text(body, "title", limit=200)
    brief = _text(body, "brief", required=False)
    path = _text(body, "path", required=not brief, limit=1000)
    host = _host(request, body)
    if path and not host:
        raise _Refused(400, "`host` is required (or send an X-Portal-Host header).")
    parent = None
    if body.get("parent"):
        parent = _project(str(body["parent"]))
        if db.parent_id_of(parent):
            raise _Refused(400, "A sub-project cannot have sub-projects of its own; "
                                "pick a top-level parent.")
    description = _text(body, "description", required=False)
    if path:
        project = claudeapp.create(
            title, description or brief, parent, host, path,
            _text(body, "remote", required=False, limit=1000),
            _text(body, "head", required=False, limit=100),
            int(person["id"]),
        )
        if brief:
            db.update_project(int(project["id"]), initial_idea=brief)
    else:
        project = claudeapp.create_here(title, brief, description, parent, host,
                                        int(person["id"]), start=body.get("start") is True)
    out: dict = {"project": _summary(db.get_project(int(project["id"])), person)}
    if body.get("start") is True:
        outcome = extrun.start(project, person, host)
        if isinstance(outcome, int):
            out["run"] = extrun.run_dict(db.get_run_with_project(outcome))
            out["project"] = _summary(db.get_project(int(project["id"])), person)
        else:
            # Created either way; the start is reported, not the creation undone.
            out["run_refused"] = {"error": outcome.message, "reason": outcome.reason}
    return JSONResponse(out, status_code=201)


@router.get("/projects/{slug}/context")
@_wrap
async def context(slug: str, request: Request):
    project = _project(slug)
    person = _person(request)
    since = claudeapp.iso(request.query_params.get("since"))
    return {"project": _summary(project, person),
            "markdown": claudeapp.context_md(project, since)}


# -------------------------------------------------------------- locations


@router.post("/projects/{slug}/locations")
@_wrap
async def add_location(slug: str, request: Request):
    project = _project(slug)
    body = await _body(request)
    host = _host(request, body)
    if not host:
        raise _Refused(400, "`host` is required (or send an X-Portal-Host header).")
    path = _text(body, "path", limit=1000)
    claudeapp.link(int(project["id"]), host, path,
                   _text(body, "remote", required=False, limit=1000),
                   _text(body, "head", required=False, limit=100))
    return {"project": _summary(project, _person(request))}


@router.delete("/projects/{slug}/locations")
@_wrap
async def remove_location(slug: str, request: Request):
    project = _project(slug)
    host = _host(request, {"host": request.query_params.get("host")})
    path = str(request.query_params.get("path") or "").strip()
    if not host or not path:
        raise _Refused(400, "`host` and `path` are required.")
    claudeapp.unlink(int(project["id"]), host, path)
    return {"project": _summary(project, _person(request))}


# --------------------------------------------------------------- sessions


@router.post("/projects/{slug}/sessions")
@_wrap
async def post_session(slug: str, request: Request):
    project = _project(slug)
    body = await _body(request)
    if not str(body.get("session_id") or "").strip():
        raise _Refused(400, "`session_id` is required.")
    person = _person(request)
    try:
        row = claudeapp.upsert_session(project, body, _host(request), int(person["id"]))
    except ValueError as exc:
        raise _Refused(409, str(exc)) from None
    return {"session": claudeapp.session_dict(row)}


# ---------------------------------------------------------------- journal


@router.post("/projects/{slug}/journal")
@_wrap
async def post_journal(slug: str, request: Request):
    project = _project(slug)
    body = await _body(request)
    text = _text(body, "text")
    kind = str(body.get("kind") or "note").strip().lower()
    if kind not in ("note", "status", "progress"):
        kind = "note"
    person = _person(request)
    entry_id = db.add_journal(int(project["id"]), "user", kind, text,
                              person_id=int(person["id"]))
    db.update_project(int(project["id"]), updated_at=db.now())
    row = db.get_journal(entry_id)
    return JSONResponse({"entry": {"id": entry_id, "ts": row["ts"], "author": people.name_of(person),
                                   "kind": kind, "text": text}}, status_code=201)


# ------------------------------------------------------------------ todos


@router.get("/projects/{slug}/todos")
@_wrap
async def get_todos(slug: str, request: Request):
    project = _project(slug)
    return {"todos": [_todo(r) for r in db.visible_todos(int(project["id"]))]}


@router.post("/projects/{slug}/todos")
@_wrap
async def post_todo(slug: str, request: Request):
    project = _project(slug)
    body = await _body(request)
    owner = str(body.get("owner") or "agent").strip()
    if owner not in ("agent", "user"):
        raise _Refused(400, "`owner` must be \"agent\" or \"user\".")
    row = db.add_todo(int(project["id"]), _text(body, "text", limit=500), owner=owner)
    if row is None:
        raise _Refused(400, "`text` is required.")
    return JSONResponse({"todo": _todo(row)}, status_code=201)


@router.patch("/projects/{slug}/todos/{todo_id}")
@_wrap
async def patch_todo(slug: str, todo_id: int, request: Request):
    project = _project(slug)
    row = db.get_todo(int(todo_id))
    if row is None or int(row["project_id"]) != int(project["id"]):
        raise _Refused(404, "No such todo on this project.")
    body = await _body(request)
    if "text" in body:
        row = db.set_todo_text(int(todo_id), str(body.get("text") or ""))
        if row is None:
            raise _Refused(400, "`text` cannot be blank.")
    if "done" in body:
        row = db.set_todo_done(int(todo_id), bool(body["done"]))
    return {"todo": _todo(row)}


# -------------------------------------------------------------- questions


@router.post("/projects/{slug}/questions")
@_wrap
async def post_question(slug: str, request: Request):
    project = _project(slug)
    body = await _body(request)
    text = _text(body, "question", limit=2000)
    options = body.get("options") if isinstance(body.get("options"), list) else None
    options = [str(o).strip() for o in options or [] if str(o).strip()][:4] or None
    filing = db.file_question(
        int(project["id"]), text, _text(body, "context", required=False, limit=4000),
        quick_options=quickreplies.encode(quickreplies.derive(text, options)),
    )
    row = filing.row
    if not filing.created:
        # Already asked in some wording; an answer, if there is one, comes
        # straight back - the best outcome an ask can have.
        return {"question": _question(row)}
    task = BackgroundTask(
        notify.notify, "New question", text, question_id=int(row["id"]),
        project_title=project["title"], question_slot=row["slot"],
        project_id=int(project["id"]),
    )
    return JSONResponse({"question": _question(row)}, status_code=201, background=task)


@router.get("/questions/{question_id}")
@_wrap
async def get_question(question_id: int, request: Request):
    row = db.get_question(int(question_id))
    if row is None:
        raise _Refused(404, "No such question.")
    return {"question": _question(row)}


# ------------------------------------------------------------------ stars


def _star_slugs(person: sqlite3.Row) -> list[str]:
    return [str(r["slug"]) for r in claudeapp.stars(int(person["id"]))]


@router.get("/stars")
@_wrap
async def get_stars(request: Request):
    return {"stars": _star_slugs(_person(request))}


@router.put("/stars")
@_wrap
async def put_stars(request: Request):
    body = await _body(request)
    wanted = body.get("stars")
    if not isinstance(wanted, list):
        raise _Refused(400, "`stars` must be a list of slugs.")
    ids = []
    for slug in wanted:
        row = db.get_project_by_slug(str(slug or "").strip().lower())
        if row is not None:
            ids.append(int(row["id"]))
    person = _person(request)
    claudeapp.set_star_order(int(person["id"]), ids)
    return {"stars": _star_slugs(person)}


@router.put("/stars/{slug}")
@_wrap
async def put_star(slug: str, request: Request):
    project = _project(slug)
    person = _person(request)
    claudeapp.star(int(person["id"]), int(project["id"]))
    return {"stars": _star_slugs(person)}


@router.delete("/stars/{slug}")
@_wrap
async def delete_star(slug: str, request: Request):
    project = _project(slug)
    person = _person(request)
    claudeapp.unstar(int(person["id"]), int(project["id"]))
    return {"stars": _star_slugs(person)}


# ------------------------------------------------------------------- runs


def _run(run_id: int) -> sqlite3.Row:
    row = db.get_run_with_project(int(run_id))
    if row is None:
        raise _Refused(404, "No such run.")
    return row


def _limit(request: Request, default: int, top: int = 100) -> int:
    try:
        return max(1, min(top, int(request.query_params.get("limit") or default)))
    except ValueError:
        raise _Refused(400, "`limit` must be a number.") from None


@router.post("/projects/{slug}/runs")
@_wrap
async def start_run(slug: str, request: Request):
    project = _project(slug)
    body = await _body(request)
    person = _person(request)
    outcome = extrun.start(project, person, _host(request, body),
                           kind=str(body.get("kind") or "auto"),
                           instructions=_text(body, "instructions", required=False))
    if isinstance(outcome, int):
        return JSONResponse({"run": extrun.run_dict(_run(outcome))}, status_code=201)
    status = 400 if outcome.reason == "bad_kind" else 409
    out: dict = {"error": outcome.message, "reason": outcome.reason}
    if outcome.run_id:
        out["run"] = extrun.run_dict(_run(outcome.run_id))
    return JSONResponse(out, status_code=status)


@router.get("/projects/{slug}/runs")
@_wrap
async def list_runs(slug: str, request: Request):
    project = _project(slug)
    rows = db.list_runs(int(project["id"]), limit=_limit(request, 10))
    return {"runs": [extrun.run_dict(r) for r in rows]}


@router.get("/active-run")
@_wrap
async def active_run(request: Request):
    """The newest run in flight anywhere on the portal the caller can see, and
    all of them under `runs` - there can be several at once now."""
    seen = scope.visible_ids(_person(request))
    rows = [r for r in db.active_runs()
            if r["project_id"] is None or int(r["project_id"]) in seen]
    runs = [extrun.run_dict(r) for r in rows]
    return {"run": runs[0] if runs else None, "runs": runs}


@router.get("/runs/{run_id}")
@_wrap
async def get_run(run_id: int, request: Request):
    return {"run": extrun.run_dict(_run(run_id))}


@router.get("/runs/{run_id}/log")
@_wrap
async def run_log(run_id: int, request: Request):
    """The same stream `/api/run/{id}/log` serves, in the contract's shape."""
    from app import main  # local: main imports this module

    row = _run(run_id)
    try:
        offset = max(0, int(request.query_params.get("offset") or 0))
    except ValueError:
        raise _Refused(400, "`offset` must be a number.") from None
    tail = json.loads((await main.api_run_log(int(run_id), offset)).body)
    return {"text": tail["text"], "next_offset": tail["offset"],
            "done": row["status"] not in ("running", "paused")}


@router.post("/runs/{run_id}/messages")
@_wrap
async def run_message(run_id: int, request: Request):
    row = _run(run_id)
    body = await _body(request)
    text = _text(body, "text")
    delivery = await extrun.message(row, _person(request), _host(request, body), text)
    said = {
        "now": "Handed to the running agent; it reads this at its next tool call.",
        "next_run": ("This run is not listening, so it is filed as a note on the project "
                     "and the next run reads it."),
        "task": "Added to the run's one-off task; its agent picks it up next.",
    }[delivery]
    return {"run": extrun.run_dict(_run(run_id)), "delivery": delivery, "message": said}


@router.post("/runs/{run_id}/cancel")
@_wrap
async def cancel_run(run_id: int, request: Request):
    _run(run_id)
    outcome = worker.cancel_run(int(run_id))
    return {"run": extrun.run_dict(_run(run_id)), "outcome": outcome}


@router.post("/runs/{run_id}/pause")
@_wrap
async def pause_run(run_id: int, request: Request):
    _run(run_id)
    outcome = midrun.pause(int(run_id), by=people.name_of(_person(request)))
    return {"run": extrun.run_dict(_run(run_id)), "outcome": outcome}


@router.post("/runs/{run_id}/resume")
@_wrap
async def resume_run(run_id: int, request: Request):
    _run(run_id)
    outcome = midrun.resume(int(run_id), by=people.name_of(_person(request)))
    return {"run": extrun.run_dict(_run(run_id)), "outcome": outcome}


# ------------------------------------------------------------------ tasks


def _task(task_id: int) -> sqlite3.Row:
    row = db.get_oneoff(int(task_id))
    if row is None:
        raise _Refused(404, "No such task.")
    return row


@router.post("/tasks")
@_wrap
async def create_task(request: Request):
    body = await _body(request)
    text = _text(body, "text")
    project = _project(str(body["project"])) if body.get("project") else None
    task = db.create_oneoff(text)
    if project is not None:
        db.set_oneoff_project(int(task["id"]), int(project["id"]))
    worker.spawn_oneoff(int(task["id"]))
    return JSONResponse({"task": extrun.task_dict(_task(task["id"]))}, status_code=201)


@router.get("/tasks")
@_wrap
async def list_tasks(request: Request):
    rows = db.list_oneoffs()[:_limit(request, 20)]
    return {"tasks": [extrun.task_dict(r) for r in rows]}


@router.get("/tasks/{task_id}")
@_wrap
async def get_task(task_id: int, request: Request):
    return {"task": extrun.task_dict(_task(task_id))}


@router.post("/tasks/{task_id}/messages")
@_wrap
async def task_message(task_id: int, request: Request):
    task = _task(task_id)
    body = await _body(request)
    text = _text(body, "text")
    if task["status"] != "open":
        raise _Refused(409, "That task is archived; unarchive it on its page to carry on.")
    db.add_oneoff_message(int(task_id), "wes", text)
    # A no-op while an agent is already on it: the message waits as pending
    # and the finishing run starts the next one itself, as on the page.
    worker.spawn_oneoff(int(task_id))
    return {"task": extrun.task_dict(_task(task_id))}


# ------------------------------------------------------------------- asks


@router.post("/projects/{slug}/ask")
@_wrap
async def ask_project(slug: str, request: Request):
    """The project page's ask: read-only, off the budget, answered in the
    background and journaled (app/ask.py). One at a time per project."""
    project = _project(slug)
    body = await _body(request)
    question = _text(body, "question", limit=4000)
    if ask.pending(int(project["id"])):
        out: dict = {"error": "This project is already answering a question; poll that one "
                              "and ask again when it is done.", "reason": "busy"}
        pending_id = extrun.latest_ask_id(int(project["id"]))
        if pending_id is not None:
            out["ask"] = extrun.ask_dict(pending_id)
        return JSONResponse(out, status_code=409)
    entry_id = ask.start(int(project["id"]), question, quiet=True)
    return JSONResponse({"ask": {"id": entry_id, "status": "pending"}}, status_code=202)


@router.get("/asks/{ask_id}")
@_wrap
async def get_ask(ask_id: int, request: Request):
    found = extrun.ask_dict(int(ask_id))
    if found is None:
        raise _Refused(404, "No such ask.")
    return {"ask": found}


# Anything else under the prefix answers in the contract's error shape rather
# than FastAPI's `{"detail": "Not Found"}`. Registered last, so it only catches
# what no route above matched.
@router.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def unknown(rest: str, request: Request):
    return _error(404, f"No route {request.method} /api/ext/v1/{rest}.")

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

from app import claudeapp, db, notify, people, quickreplies, scope

API_VERSION = 1
FEATURES = ("projects", "context", "link", "sessions", "journal", "todos", "questions", "stars")

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
    body = await _body(request)
    person = _person(request)
    title = _text(body, "title", limit=200)
    path = _text(body, "path", limit=1000)
    host = _host(request, body)
    if not host:
        raise _Refused(400, "`host` is required (or send an X-Portal-Host header).")
    parent = None
    if body.get("parent"):
        parent = _project(str(body["parent"]))
        if db.parent_id_of(parent):
            raise _Refused(400, "A sub-project cannot have sub-projects of its own; "
                                "pick a top-level parent.")
    project = claudeapp.create(
        title, _text(body, "description", required=False), parent, host, path,
        _text(body, "remote", required=False, limit=1000),
        _text(body, "head", required=False, limit=100),
        int(person["id"]),
    )
    return JSONResponse({"project": _summary(project, person)}, status_code=201)


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


# Anything else under the prefix answers in the contract's error shape rather
# than FastAPI's `{"detail": "Not Found"}`. Registered last, so it only catches
# what no route above matched.
@router.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def unknown(rest: str, request: Request):
    return _error(404, f"No route {request.method} /api/ext/v1/{rest}.")

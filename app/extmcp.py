"""The portal as an MCP server for Claude Code on another machine.

Wes, 2026-11-10: "I often still use claude code on my macbook for developing
iOS and macOS apps. it often also has a component here on this machine where I
am handing markdown files back and forth. Could you set it up where claude code
can interface directly with the projects and send notes, view project context,
etc? Maybe its a thing where I give them some link to a project or the name of
it or something for it to know which one to interface with? Also should be
able to create a new project if the right one doesn't already exist."

`/api/ext/v1/` already did all of that for the Claude app plugin, but a plugin
is a thing to install and keep current on every machine. Claude Code speaks MCP
over plain HTTP, so the same capabilities are offered here as tools, and the
whole setup on a Mac is one command (Settings > access shows it):

    claude mcp add --transport http --scope user portal \\
        http://<portal>:8500/api/ext/v1/mcp --header "X-Portal-Host: $(hostname -s)"

## Naming a project

Every tool takes `project` as whatever Wes would type: the project page's URL,
its slug, its title or a few words of it (`resolve`). A match that is not
unique is refused with the candidates rather than guessed - a note filed on the
wrong project is a note an agent acts on. `find_projects` with `folder` also
answers "which project is this checkout?" once the folder is linked, so a
session in a linked repo never has to be told.

## What a tool does, and what it does not

Each tool is a thin call into the code the ext API routes already use, with the
same guards: a note goes through `worker.note_arrived` exactly like the green
"add note" button (and so never schedules a project hosted elsewhere), a run
through `extrun.start`, an ask through `ask.start`. Files go both ways because
that is the half of the job Wes was doing by hand: `send_note` carries text
files as attachments, `read_file` reads any file in a project's workspace.

## The transport

MCP "streamable HTTP" in its smallest legal form: every message is a POST, every
answer is one JSON body (no SSE stream, so GET is a 405), a notification gets a
bare 202, and no session id is issued because nothing here is per-session. A
tool that fails returns `isError` with a sentence, never a JSON-RPC error: the
model reads the first and can recover, the CLI swallows the second.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app import (
    ask, attachments, claudeapp, config, crossproject, db, extrun, scope, worker,
)

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "project-portal", "version": "1"}
PATH = "/api/ext/v1/mcp"

# An ask is usually answered in under a minute; past this the tool hands back
# the ask id so the session can check again instead of holding the call open.
ASK_WAIT_DEFAULT = 90
ASK_WAIT_MAX = 240
ASK_POLL_SEC = 2.0

# A file sent with a note is a markdown handoff, not a build artifact.
MAX_NOTE_FILES = 10

router = APIRouter()

INSTRUCTIONS = (
    "Project Portal keeps Wes's projects: each has a journal, a todo list, open questions and "
    "an agent that builds it on the portal's server. Name a project by its portal URL, slug or "
    "title. When you work in a folder that belongs to a project, call find_projects with that "
    "folder first; if it is not linked, find the project by name (or create it) and call "
    "link_folder so the next session knows. Use send_note for anything the portal's agent should "
    "act on, log_progress for a record of what you did here."
)


class ToolError(Exception):
    """A sentence for the model: the call failed and here is why."""


# ------------------------------------------------------------- resolving


_URL_SLUG = re.compile(r"/project/([a-z0-9][a-z0-9-]*)")


def resolve(ref: Any, person: sqlite3.Row) -> sqlite3.Row:
    """The project `ref` names: a portal URL, a slug, a title or part of one.

    A unique answer or a ToolError naming the candidates - never a guess."""
    text = " ".join(str(ref or "").split())
    if not text:
        raise ToolError("`project` is required: a portal URL, a slug or the project's name.")
    if "://" in text or "/project/" in text:
        found = _URL_SLUG.search(urlparse(text).path or text)
        if found:
            row = db.get_project_by_slug(found.group(1))
            if row is not None:
                return row
        raise ToolError(f"No project at {text!r}. Call find_projects to look one up.")
    row = db.get_project_by_slug(text.lower())
    if row is not None:
        return row
    seen = scope.visible_ids(person)
    rows = [r for r in db.list_projects(order_by="updated_at DESC") if int(r["id"]) in seen]
    exact = [r for r in rows if str(r["title"]).strip().lower() == text.lower()]
    if len(exact) == 1:
        return exact[0]
    hits = claudeapp.search(rows, text)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ToolError(f"No project matches {text!r}. find_projects lists them; "
                        f"create_project makes a new one.")
    names = ", ".join(f"`{r['slug']}` ({r['title']})" for r in hits[:8])
    raise ToolError(f"{text!r} matches more than one project: {names}. Name one by its slug.")


def by_folder(folder: str, host: str) -> Optional[sqlite3.Row]:
    """The project linked to `folder` or the nearest folder above it - this
    machine's link first, any machine's after, since a path is often the same
    on two Macs."""
    path = str(folder or "").rstrip("/")
    if not path:
        return None
    with db._LOCK:  # noqa: SLF001
        rows = claudeapp._conn().execute(  # noqa: SLF001
            "SELECT project_id, host, path FROM project_locations"
        ).fetchall()
    best: Optional[tuple] = None
    for r in rows:
        linked = str(r["path"]).rstrip("/")
        if not linked or not (path == linked or path.startswith(linked + "/")):
            continue
        rank = (str(r["host"]) == host, len(linked))
        if best is None or rank > best[0]:
            best = (rank, int(r["project_id"]))
    return db.get_project(best[1]) if best else None


def _url(project: sqlite3.Row) -> str:
    return f"{config.SITE.base_url}/project/{project['slug']}"


def _line(project: sqlite3.Row) -> str:
    where = " - built on another machine" if claudeapp.hosted_elsewhere(project) else ""
    return (f"- **{project['title']}** `{project['slug']}` [{project['stage']}]{where}\n"
            f"  {claudeapp._one_line(project['description'])}\n  {_url(project)}")  # noqa: SLF001


# ------------------------------------------------------------------ tools


def _str(args: dict, key: str, required: bool = False, limit: int = 20000) -> str:
    text = str(args.get(key) or "").strip()
    if required and not text:
        raise ToolError(f"`{key}` is required.")
    return text[:limit]


async def find_projects(args: dict, person: sqlite3.Row, host: str) -> str:
    folder = _str(args, "folder", limit=1000)
    if folder:
        linked = by_folder(folder, host)
        if linked is not None:
            return f"`{folder}` is linked to:\n\n{_line(linked)}"
        if not _str(args, "query"):
            return (f"No project is linked to `{folder}`. Find it by name with `query`, then "
                    f"link_folder - or create_project with this folder if it is new.")
    query = _str(args, "query", limit=200)
    seen = scope.visible_ids(person)
    rows = [r for r in db.list_projects(order_by="updated_at DESC") if int(r["id"]) in seen]
    if args.get("include_closed") is not True:
        rows = [r for r in rows if r["stage"] not in claudeapp.CLOSED_STAGES]
    if query:
        rows = claudeapp.search(rows, query)
    if not rows:
        return f"No project matches {query!r}." if query else "There are no projects yet."
    shown = rows[:25]
    more = f"\n\n… and {len(rows) - len(shown)} more; narrow with `query`." if len(rows) > 25 else ""
    return "\n".join(_line(r) for r in shown) + more


async def project_context(args: dict, person: sqlite3.Row, host: str) -> str:
    project = resolve(args.get("project"), person)
    since = claudeapp.iso(args.get("since")) if args.get("since") else None
    return f"{claudeapp.context_md(project, since)}\n\nPortal page: {_url(project)}"


def _files(args: dict) -> list[tuple[str, bytes]]:
    raw = args.get("files") or []
    if not isinstance(raw, list):
        raise ToolError("`files` must be a list of {name, content}.")
    if len(raw) > MAX_NOTE_FILES:
        raise ToolError(f"At most {MAX_NOTE_FILES} files per note.")
    out = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ToolError("Each file needs a `name` and its text `content`.")
        out.append((str(item["name"]).strip(), str(item.get("content") or "").encode("utf-8")))
    return out


async def send_note(args: dict, person: sqlite3.Row, host: str) -> str:
    """A note on the project, the way the web page's note box files one - files
    attached - and then the green button's wake unless `wake` is false."""
    project = resolve(args.get("project"), person)
    text = _str(args, "text")
    files = _files(args)
    if not text and not files:
        raise ToolError("Send `text`, `files` or both.")
    where = f" on `{host}`" if host else ""
    stored, errors = [], []
    for name, data in files:
        try:
            stored.append(attachments.store(project_id=project["id"], slug=project["slug"],
                                            orig_name=name, data=data,
                                            declared_mime="text/plain", note=text))
        except (ValueError, OSError) as exc:
            errors.append(f"{name}: {exc}")
    if files and not stored:
        raise ToolError("No file could be stored: " + "; ".join(errors))
    body = f"**From Claude Code{where}:**\n\n{text}".rstrip()
    if stored:
        body = f"{body}\n\n{attachments.listing_block(stored)}"
    entry = db.add_journal(int(project["id"]), "user", "note", body,
                           person_id=int(person["id"]))
    for a in stored:
        db.set_attachment_journal(a["id"], entry)
    said = [f"Filed on `{project['slug']}`"
            + (f" with {len(stored)} file(s)" if stored else "") + "."]
    if errors:
        said.append("Refused: " + "; ".join(errors) + ".")
    if args.get("wake") is False:
        said.append("Queued for whenever its agent next runs; nothing was started.")
    elif claudeapp.hosted_elsewhere(project):
        said.append("This project is built on another machine, so no run starts here; the note "
                    "is in its context for the next session.")
    elif await worker.note_arrived(project):
        said.append("Its agent was queued to act on it.")
    else:
        said.append("No run could start right now; the next run reads it.")
    return " ".join(said) + f"\n{_url(project)}"


async def log_progress(args: dict, person: sqlite3.Row, host: str) -> str:
    project = resolve(args.get("project"), person)
    text = _str(args, "text", required=True)
    where = f" on `{host}`" if host else ""
    db.add_journal(int(project["id"]), "user", "progress",
                   f"**From Claude Code{where}:**\n\n{text}", person_id=int(person["id"]))
    db.update_project(int(project["id"]), updated_at=db.now())
    return f"Journaled on `{project['slug']}`. Nothing was started."


async def read_file(args: dict, person: sqlite3.Row, host: str) -> str:
    """A directory listing or a file from the project's workspace here - the
    same containment `crossproject` applies to runs, minus the per-run
    permission, which is about runs reading each other and not about Wes."""
    project = resolve(args.get("project"), person)
    if claudeapp.hosted_elsewhere(project):
        raise ToolError(f"`{project['slug']}` is built on another machine and has no files here.")
    workspace = (config.PROJECTS_DIR / str(project["slug"])).resolve()
    if not workspace.is_dir():
        raise ToolError(f"`{project['slug']}` has no workspace on disk yet.")
    rel = _str(args, "path", limit=1000).lstrip("/")
    target = (workspace / rel).resolve() if rel else workspace
    try:
        target.relative_to(workspace)
    except ValueError:
        raise ToolError(f"`{rel}` is outside `{project['slug']}`'s workspace.") from None
    found = crossproject._Resolved(project=project, workspace=workspace, target=target)  # noqa: SLF001
    rel = "" if target == workspace else target.relative_to(workspace).as_posix()
    if not target.exists():
        raise ToolError(f"`{rel}` is not in `{project['slug']}`'s workspace; call with no path "
                        f"to list it.")
    if target.is_dir():
        return crossproject._render_listing(found, rel)  # noqa: SLF001
    return crossproject._render_file(found, rel)  # noqa: SLF001


async def todos(args: dict, person: sqlite3.Row, host: str) -> str:
    project = resolve(args.get("project"), person)
    pid = int(project["id"])
    said = []
    if _str(args, "add"):
        owner = _str(args, "owner") or "agent"
        if owner not in ("agent", "user"):
            raise ToolError("`owner` must be \"agent\" or \"user\".")
        row = db.add_todo(pid, _str(args, "add", limit=500), owner=owner)
        said.append(f"Added #{row['id']}.")
    for raw in args.get("complete") or []:
        row = db.get_todo(int(raw)) if str(raw).isdigit() else None
        if row is None or int(row["project_id"]) != pid:
            said.append(f"#{raw} is not a todo on this project.")
            continue
        db.set_todo_done(int(raw), True)
        said.append(f"Ticked #{raw}.")
    rows = db.visible_todos(pid)
    lines = [f"- [{'x' if r['done'] else ' '}] #{r['id']} ({r['owner']}) {r['text']}" for r in rows]
    return "\n".join(said + [f"`{project['slug']}` todos:"] + (lines or ["(none)"]))


async def create_project(args: dict, person: sqlite3.Row, host: str) -> str:
    """Two shapes, as `POST /projects`: with `folder`, a project built on the
    caller's machine (never run here); without, one the portal builds itself
    from `brief`, started at once when `start` is true."""
    title = _str(args, "title", required=True, limit=200)
    brief = _str(args, "brief")
    folder = _str(args, "folder", limit=1000)
    if not folder and not brief:
        raise ToolError("Give a `brief` (what the project is for), or a `folder` if it is built "
                        "on this machine.")
    pid = int(person["id"])
    if folder:
        if not host:
            raise ToolError(_NO_HOST)
        project = claudeapp.create(title, brief, None, host, folder, "", "", pid)
        if brief:
            db.update_project(int(project["id"]), initial_idea=brief)
    else:
        project = claudeapp.create_here(title, brief, "", None, host, pid,
                                        start=args.get("start") is True)
    project = db.get_project(int(project["id"]))
    said = [f"Created `{project['slug']}`: {_url(project)}"]
    if args.get("start") is True:
        outcome = extrun.start(project, person, host)
        if isinstance(outcome, int):
            said.append(f"Its first run is #{outcome}: {extrun.run_dict(db.get_run_with_project(outcome))['url']}")
        else:
            said.append(f"Its first run did not start: {outcome.message}")
    if folder:
        said.append(_CLAUDE_MD.format(url=_url(project)))
    return "\n\n".join(said)


async def link_folder(args: dict, person: sqlite3.Row, host: str) -> str:
    project = resolve(args.get("project"), person)
    folder = _str(args, "folder", required=True, limit=1000)
    if not host:
        raise ToolError(_NO_HOST)
    was_elsewhere = claudeapp.hosted_elsewhere(project)
    claudeapp.link(int(project["id"]), host, folder)
    said = [f"Linked `{folder}` on `{host}` to `{project['slug']}`."]
    if claudeapp.hosted_elsewhere(project) and not was_elsewhere:
        said.append("It has no checkout on the portal's server, so from now on it counts as "
                    "built on this machine and the portal will not schedule runs for it.")
    said.append(_CLAUDE_MD.format(url=_url(project)))
    return "\n\n".join(said)


async def start_run(args: dict, person: sqlite3.Row, host: str) -> str:
    project = resolve(args.get("project"), person)
    outcome = extrun.start(project, person, host, kind=_str(args, "kind") or "auto",
                           instructions=_str(args, "instructions"))
    if isinstance(outcome, int):
        run = extrun.run_dict(db.get_run_with_project(outcome))
        return f"Run #{outcome} started on `{project['slug']}` ({run['kind']}): {run['url']}"
    raise ToolError(f"Not started ({outcome.reason}): {outcome.message}")


async def run_status(args: dict, person: sqlite3.Row, host: str) -> str:
    """One run with the tail of its log, or a project's latest runs."""
    from app import main  # local: main imports this module

    if args.get("run_id") is None:
        project = resolve(args.get("project"), person)
        rows = db.list_runs(int(project["id"]), limit=5)
        if not rows:
            return f"`{project['slug']}` has no runs yet."
        runs = [extrun.run_dict(r) for r in rows]
        return "\n".join(f"- #{r['id']} {r['kind']} {r['status']} {r['started_at']}: "
                         f"{claudeapp._one_line(r['summary'])}" for r in runs)  # noqa: SLF001
    try:
        run_id = int(args["run_id"])
    except (TypeError, ValueError):
        raise ToolError("`run_id` must be a number.") from None
    row = db.get_run_with_project(run_id)
    if row is None:
        raise ToolError(f"No run #{run_id}.")
    run = extrun.run_dict(row)
    offset = max(0, int(args.get("log_offset") or 0))
    tail = json.loads((await main.api_run_log(run_id, offset)).body)
    text = tail["text"][-6000:]
    commits = ", ".join(f"{c['sha'][:7]} {c['subject']}" for c in run["commits"]) or "none"
    return (f"Run #{run_id} on `{run['project']}`: {run['status']} ({run['kind']}), started "
            f"{run['started_at']}, finished {run['finished_at'] or '-'}\n"
            f"Summary: {run['summary'] or '-'}\nCommits: {commits}\n{run['url']}\n\n"
            f"Log from offset {offset} (pass log_offset={tail['offset']} for what follows):\n"
            f"{text or '(nothing new)'}")


async def ask_project(args: dict, person: sqlite3.Row, host: str) -> str:
    """Ask the project's agent, read-only, and wait a while for the answer.
    With `ask_id`, check on an earlier ask instead of asking again."""
    if args.get("ask_id") is not None:
        entry_id = int(args["ask_id"])
        if extrun.ask_dict(entry_id) is None:
            raise ToolError(f"No ask #{entry_id}.")
    else:
        project = resolve(args.get("project"), person)
        question = _str(args, "question", required=True, limit=4000)
        pid = int(project["id"])
        if ask.pending(pid):
            raise ToolError(f"`{project['slug']}` is already answering ask "
                            f"#{extrun.latest_ask_id(pid)}; check it with ask_id.")
        entry_id = ask.start(pid, question, quiet=True)
    try:
        wait = float(args.get("wait_seconds", ASK_WAIT_DEFAULT))
    except (TypeError, ValueError):
        wait = ASK_WAIT_DEFAULT
    deadline = asyncio.get_running_loop().time() + max(0.0, min(ASK_WAIT_MAX, wait))
    while True:
        found = extrun.ask_dict(entry_id) or {"status": "failed"}
        if found["status"] == "answered":
            return found["answer"]
        if found["status"] == "failed":
            raise ToolError(f"Ask #{entry_id} failed without an answer.")
        if asyncio.get_running_loop().time() >= deadline:
            return (f"Ask #{entry_id} is still being answered. Call ask_project with "
                    f"ask_id={entry_id} to check again.")
        await asyncio.sleep(ASK_POLL_SEC)


_NO_HOST = ("This machine has not said its name. Re-add the server with "
            "`--header \"X-Portal-Host: $(hostname -s)\"`, or pass `host`.")

_CLAUDE_MD = ("Add this line to the folder's CLAUDE.md so every session here knows its "
              "project: `Project Portal: {url}`")


# ----------------------------------------------------------- tool table


def _schema(props: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


_PROJECT = {"type": "string",
            "description": "The project: its portal URL, its slug, or its name."}

TOOLS: dict[str, tuple[Any, str, dict]] = {
    "find_projects": (find_projects,
        "List or search the portal's projects. With `folder` (your working directory), say which "
        "project that folder is linked to.",
        _schema({"query": {"type": "string"}, "folder": {"type": "string"},
                 "include_closed": {"type": "boolean"}})),
    "project_context": (project_context,
        "A project's brief, recent journal, todos, open questions and notes - what you need to "
        "work on it. `since` (ISO time) adds what changed since then.",
        _schema({"project": _PROJECT, "since": {"type": "string"}}, ("project",))),
    "send_note": (send_note,
        "Send the project's portal agent a note - an instruction or information to act on, "
        "optionally with text files (markdown handoffs) attached. Like the web page's add-note "
        "button it starts the agent if it can run now; `wake: false` only queues it.",
        _schema({"project": _PROJECT, "text": {"type": "string"},
                 "files": {"type": "array", "items": _schema(
                     {"name": {"type": "string"}, "content": {"type": "string"}},
                     ("name", "content"))},
                 "wake": {"type": "boolean"}}, ("project",))),
    "log_progress": (log_progress,
        "Record on the project's journal what you did in this session. Starts nothing.",
        _schema({"project": _PROJECT, "text": {"type": "string"}}, ("project", "text"))),
    "read_file": (read_file,
        "Read a file from the project's workspace on the portal's server, or list a directory "
        "(no `path` lists the top).",
        _schema({"project": _PROJECT, "path": {"type": "string"}}, ("project",))),
    "todos": (todos,
        "The project's todo list; `add` a new item (owner agent|user) or `complete` ids.",
        _schema({"project": _PROJECT, "add": {"type": "string"},
                 "owner": {"type": "string", "enum": ["agent", "user"]},
                 "complete": {"type": "array", "items": {"type": "integer"}}}, ("project",))),
    "create_project": (create_project,
        "Create a project when none fits. With `folder`, it is built here on this machine and "
        "the portal keeps its record; without, the portal builds it from `brief`, and "
        "`start: true` puts its agent on it now.",
        _schema({"title": {"type": "string"}, "brief": {"type": "string"},
                 "folder": {"type": "string"}, "start": {"type": "boolean"}}, ("title",))),
    "link_folder": (link_folder,
        "Link a folder on this machine to a project, so find_projects recognizes it next time.",
        _schema({"project": _PROJECT, "folder": {"type": "string"}}, ("project", "folder"))),
    "start_run": (start_run,
        "Start the project's portal agent now, with optional instructions. kind: auto (default), "
        "build, plan or research.",
        _schema({"project": _PROJECT, "instructions": {"type": "string"},
                 "kind": {"type": "string", "enum": ["auto", "build", "plan", "research"]}},
                ("project",))),
    "run_status": (run_status,
        "A run's status, summary, commits and log tail by `run_id`; or a project's latest runs.",
        _schema({"run_id": {"type": "integer"}, "project": _PROJECT,
                 "log_offset": {"type": "integer"}})),
    "ask_project": (ask_project,
        "Ask the project's own agent a question about it. It answers read-only and changes "
        "nothing; waits up to `wait_seconds` (default 90). `ask_id` checks an earlier ask.",
        _schema({"project": _PROJECT, "question": {"type": "string"},
                 "wait_seconds": {"type": "integer"}, "ask_id": {"type": "integer"}})),
}


def tool_list() -> list[dict]:
    return [{"name": name, "description": desc, "inputSchema": schema}
            for name, (_, desc, schema) in TOOLS.items()]


async def call(name: str, args: Any, person: sqlite3.Row, host: str) -> dict:
    """One tools/call result. Every failure is a readable isError result."""
    entry = TOOLS.get(name)
    if entry is None:
        return _result(f"No tool named {name!r}.", error=True)
    if not isinstance(args, dict):
        args = {}
    host = host or " ".join(str(args.get("host") or "").split())[:200]
    try:
        return _result(await entry[0](args, person, host))
    except ToolError as exc:
        return _result(str(exc), error=True)


def _result(text: str, error: bool = False) -> dict:
    out: dict = {"content": [{"type": "text", "text": text}]}
    if error:
        out["isError"] = True
    return out


# -------------------------------------------------------------- transport


def _reply(msg_id: Any, result: Optional[dict] = None, error: Optional[dict] = None) -> dict:
    out: dict = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    return out


async def handle(message: Any, person: sqlite3.Row, host: str) -> Optional[dict]:
    """Answer one JSON-RPC message; None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _reply(None, error={"code": -32600, "message": "Not a JSON-RPC 2.0 message."})
    if "id" not in message:
        return None
    msg_id, method = message["id"], message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return _reply(msg_id, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _reply(msg_id, {})
    if method == "tools/list":
        return _reply(msg_id, {"tools": tool_list()})
    if method == "tools/call":
        return _reply(msg_id, await call(str(params.get("name") or ""),
                                         params.get("arguments"), person, host))
    return _reply(msg_id, error={"code": -32601, "message": f"Unknown method {method!r}."})


@router.post(PATH)
async def mcp_post(request: Request):
    from app import main  # local: main imports this module

    try:
        message = json.loads(await request.body())
    except ValueError:
        return JSONResponse(_reply(None, error={"code": -32700, "message": "Not JSON."}),
                            status_code=400)
    person = main.resolve_person(request)
    host = " ".join(str(request.headers.get("x-portal-host") or "").split())[:200]
    if isinstance(message, list):
        replies = [r for r in [await handle(m, person, host) for m in message] if r]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = await handle(message, person, host)
    return JSONResponse(reply) if reply is not None else Response(status_code=202)


@router.get(PATH)
@router.delete(PATH)
async def mcp_no_stream() -> Response:
    """No server-initiated stream and no sessions to end."""
    return Response(status_code=405, headers={"Allow": "POST"})


def setup_command(host_url: Optional[str] = None) -> str:
    """The one line that connects Claude Code on another machine."""
    base = host_url or config.SITE.base_url
    return (f"claude mcp add --transport http --scope user portal {base}{PATH} "
            f"--header \"X-Portal-Host: $(hostname -s)\"")


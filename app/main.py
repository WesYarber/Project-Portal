"""Project Portal - FastAPI app entrypoint."""
from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import markdown as md
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from app import (
    agent_runner,
    ask,
    attachments,
    climemory,
    config,
    daycycle,
    db,
    filetree,
    fileview,
    hookguard,
    launch,
    limits,
    live,
    mediamd,
    memory,
    modelwatch,
    netinfo,
    notes,
    notify,
    oneoff,
    pacing,
    preview,
    quoting,
    runlog,
    settings_form,
    subprojects,
    telegram_bot,
    usage,
    webpush,
    worker,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO, which would leak the Telegram bot
# token into the journal on every getUpdates poll.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("portal.main")

app = FastAPI(title="Project Portal")


# Registered BEFORE the /static mount, which would otherwise swallow the path
# and 404: the theme's original name. Anything built against it keeps working;
# the file itself is now terminal-theme.css - it is a dark terminal theme the
# portal happens to follow, not the portal's own.
@app.get("/static/portal-theme.css")
async def old_theme_name() -> RedirectResponse:
    return RedirectResponse(url="/static/terminal-theme.css", status_code=308)


# Served from the origin root, not /static: a service worker's scope is capped
# at the directory it was fetched from, and this one must control "/" to catch
# pushes for the whole portal.
@app.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(
        str(config.BASE_DIR / "app" / "static" / "sw.js"),
        media_type="application/javascript",
    )


app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")

templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))
# Self-modifying runs commit template + Python changes together, but only the
# Python side needs a process restart to take effect - Jinja's default
# auto_reload means a mid-run commit makes the live process serve a new
# template against still-old code immediately, well before that run (and its
# post-run restart) finishes. Pinning templates to what was on disk at
# startup keeps template and code versions in lockstep until an explicit
# restart brings both up to date atomically.
templates.env.auto_reload = False


def render_markdown(text: Optional[str]) -> str:
    if not text:
        return ""
    return md.markdown(text, extensions=["fenced_code", "tables", "nl2br"])


def timeago(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def markdown_media(
    text: Optional[str], slug: Optional[str] = None, raw_base: Optional[str] = None
) -> str:
    """Markdown, plus workspace media resolved to a servable URL.

    Journal entries and one-off replies are written by agents that only know
    their media as workspace-relative paths; this filter roots those at the
    inline-serving route so `![shot](shots/a.png)` actually renders. With
    neither a slug nor a base (a journal row with no project) it degrades to
    the plain markdown filter.
    """
    base = raw_base
    if base is None and slug:
        base = f"/raw/{quote(slug)}"
    return mediamd.resolve_media(render_markdown(text), base)


templates.env.filters["markdown"] = render_markdown
templates.env.filters["markdown_media"] = markdown_media
templates.env.filters["timeago"] = timeago
templates.env.globals["OPEN_STAGES"] = config.OPEN_STAGES
templates.env.globals["DONE_STAGES"] = config.DONE_STAGES
templates.env.globals["USER_STATES"] = config.USER_STATES
templates.env.globals["PROJECT_KINDS"] = config.PROJECT_KINDS
templates.env.globals["RUN_STATUSES"] = config.RUN_STATUSES
# This installation's identity (app/site.py), so no template has to name a
# machine: `SITE.handle` is the user@host-shaped label the chrome wears,
# `SITE.host` and `SITE.base_url` are what a link or a placeholder should use.
templates.env.globals["SITE"] = config.SITE

def _humanize_seconds(secs: int) -> str:
    return daycycle.humanize_seconds(secs)


def usage_snapshot() -> dict:
    """Everything the UI needs to answer "how much budget is left, and when
    does it come back?". The daily counters roll over at the portal day
    boundary (05:00 local by default, see app/daycycle.py) - reported here
    rather than left implicit."""
    now = datetime.now(timezone.utc)
    reset_at = daycycle.next_reset()
    resets_in = daycycle.seconds_until_reset()

    runs_today = db.count_runs_today()
    base = db.base_max_runs()
    bonus = db.bonus_runs_today()
    effective = base + bonus

    backoff_until = db.get_setting("backoff_until") or ""
    in_backoff = worker._in_backoff()  # noqa: SLF001 - internal reuse within app package
    backoff_in = 0
    if in_backoff:
        dt = worker._parse_iso(backoff_until)  # noqa: SLF001
        if dt is not None:
            backoff_in = max(0, int((dt - now).total_seconds()))

    return {
        "runs_today": runs_today,
        "base_max_runs": base,
        "bonus_runs": bonus,
        "max_runs": effective,
        "remaining": max(0, effective - runs_today),
        "resets_in_sec": resets_in,
        "resets_in": _humanize_seconds(resets_in),
        "resets_at": reset_at.isoformat(timespec="seconds"),
        "reset_hour": daycycle.reset_hour(),
        "day": daycycle.current_day(),
        # The account's real Claude windows, read from the cache the poller
        # keeps warm - never fetched here, so a page render can't hang on
        # api.anthropic.com being slow.
        "limits": limits.cached(),
        # When the default model's own window is exhausted, what runs actually
        # use instead (Fable -> Opus), so the dashboard says it out loud
        # rather than the run rows quietly wearing a different model name.
        "model_fallback": _model_fallback_status(),
        # Non-empty only while a spend-down window is open, which is a mode
        # rather than a reason - the idle line explains why nothing is running,
        # this explains why the budget on screen is not being enforced.
        "spend_down": pacing.status_line(now),
        # How many projects are waiting for a research burst. Shown as a count
        # rather than a warning: nothing is stuck, they are waiting for spare
        # allowance that may not come today.
        "research_queued": db.count_research_queued(),
        "backoff_until": backoff_until,
        "in_backoff": in_backoff,
        "backoff_in": _humanize_seconds(backoff_in) if backoff_in else "",
        "worker_enabled": (db.get_setting("worker_enabled") or "1") == "1",
    }


def _model_fallback_status() -> Optional[dict]:
    """{'from', 'to', 'why'} while the usage fallback is rerouting the global
    default model, else None."""
    configured = agent_runner.configured_model(None)
    used, why = limits.model_fallback(configured)
    if not why:
        return None
    return {"from": configured, "to": used, "why": why}


def _run_owner_label(row) -> str:
    """What a run row is *on*, for pages that list runs of every kind: a
    project's title, a one-off task's title, or the memory jobs."""
    if row["project_title"]:
        return row["project_title"]
    oneoff_title = db._row_get(row, "oneoff_title")  # noqa: SLF001 - shared helper
    if oneoff_title:
        return f"task: {oneoff_title}"
    return "memory / reflect"


def _run_snapshot(row) -> dict:
    started = worker._parse_iso(row["started_at"])  # noqa: SLF001
    elapsed = int((datetime.now(timezone.utc) - started).total_seconds()) if started else 0
    return {
        "active": True,
        "run_id": row["id"],
        "project_id": row["project_id"],
        "project_slug": row["project_slug"],
        "project_title": _run_owner_label(row),
        "oneoff_id": db._row_get(row, "oneoff_id"),  # noqa: SLF001
        "task": row["task"],
        "model": row["model"],
        "started_at": row["started_at"],
        "elapsed_sec": elapsed,
        "elapsed": _humanize_seconds(elapsed),
        "events": row["events"] or 0,
        "last_activity": row["last_activity"] or "starting up...",
    }


def active_run_snapshot() -> dict:
    """The in-flight runs, flattened for both templates and /api.

    Runs are parallel now, so `runs` is the real answer and the top-level
    fields mirror the newest of them - that keeps every existing caller
    (templates, the poller, /api) working while `runs` carries the rest.
    """
    rows = db.active_runs()
    if not rows:
        return {
            "active": False,
            "runs": [],
            "run_ids": "",
            "project_ids": [],
            "idle_reason": worker.idle_reason(),
        }
    snaps = [_run_snapshot(row) for row in rows]
    return {
        **snaps[0],
        "runs": snaps,
        # A stable identity for "which runs are on screen", so the poller can
        # tell a run starting or finishing from one merely progressing.
        "run_ids": ",".join(str(s["run_id"]) for s in sorted(snaps, key=lambda s: s["run_id"])),
        "project_ids": [s["project_id"] for s in snaps if s["project_id"]],
        "idle_reason": "",
    }


def open_question_total() -> int:
    """Nav badge count. A global rather than a per-route context value so every
    page (including error-adjacent ones) renders the same badge.

    Questions on projects Wes has paused himself, or that are still in the
    backlog, are left out: he asked for the number to mean "things waiting on
    me right now". They are still answerable - they sit in their own section
    below the fold on /questions - and the badge counts exactly what that page
    shows above it."""
    shelved = db.shelved_project_ids()
    return len([q for q in db.open_questions() if q["project_id"] not in shelved])


def appearance() -> dict[str, str]:
    """The CRT layer settings, each falling back to its default if unset or
    unrecognised. Rendered as classes on <body> so the whole page can respond
    without any per-element markup."""
    out = {}
    for key, choices in config.APPEARANCE_CHOICES.items():
        value = db.get_setting(key) or ""
        allowed = {v for v, _ in choices}
        out[key] = value if value in allowed else config.APPEARANCE_DEFAULTS[key]
    return out


def body_classes() -> str:
    look = appearance()
    return " ".join(
        f"{prefix}-{look[key]}"
        for key, prefix in config.APPEARANCE_CLASS_PREFIX.items()
        if key in look
    )


def static_url(name: str) -> str:
    """`/static/style.css?v=<mtime>` so a stylesheet change is never masked by
    a cached copy. This was not academic: the appearance settings landed with
    new CSS classes, and a browser holding the previous style.css kept painting
    scanlines no matter what the setting said - which reads exactly like the
    setting failing to save. Falls back to an unversioned URL if the file is
    missing rather than raising during a render."""
    path = config.BASE_DIR / "app" / "static" / name
    try:
        return f"/static/{name}?v={int(path.stat().st_mtime)}"
    except OSError:
        return f"/static/{name}"


def icon_url(name: str) -> str:
    """An icon URL that can never stay poisoned by a failed fetch.

    Chrome caches a *failed* icon fetch against the exact URL, and the mtime
    version in static_url only changes when the file changes - so one icon
    request that landed in a self-update restart gap left the dashboard tab
    blank until the next time the icon file itself was edited, which is
    roughly never. Salting with the boot id means every restart mints a fresh
    URL: the cached failure dies with the boot that caused it.
    """
    base = static_url(name)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}b={live.BOOT_ID}"


def secure_url() -> str:
    """The HTTPS address of this same portal, if one is being served.

    Every page carries it so that a page loaded over plain http can point at
    the secure copy of itself rather than silently dropping the features the
    browser only allows in a secure context (the microphone, and so voice
    memos). Reads the cache; never takes a reading during a render.
    """
    snap = netinfo.cached() or {}
    return snap.get("https_url") or ""


def open_oneoff_total() -> int:
    """Nav badge for /tasks: how many one-off sessions are open. Open, not
    'awaiting reply' - a session parked mid-exchange is exactly the thing the
    badge exists to stop Wes forgetting."""
    try:
        return db.count_open_oneoffs()
    except sqlite3.Error:
        return 0


templates.env.globals["secure_url"] = secure_url
templates.env.globals["todo_tags"] = db.todo_tags
templates.env.globals["open_question_total"] = open_question_total
templates.env.globals["open_oneoff_total"] = open_oneoff_total
templates.env.globals["restart_pending_runs"] = worker.restart_pending_runs
templates.env.globals["body_classes"] = body_classes
templates.env.globals["static_url"] = static_url
templates.env.globals["icon_url"] = icon_url
templates.env.globals["APPEARANCE_CHOICES"] = config.APPEARANCE_CHOICES
templates.env.globals["APPEARANCE_DEFAULTS"] = config.APPEARANCE_DEFAULTS
templates.env.globals["status_choices"] = config.status_choices
templates.env.globals["display_state"] = db.display_state
templates.env.globals["is_side_thread"] = db.is_side_thread
templates.env.globals["summary_bullet"] = db.summary_bullet
templates.env.filters["status_badge"] = config.status_badge
templates.env.globals["META_PROJECT_SLUG"] = config.META_PROJECT_SLUG
templates.env.globals["MODEL_CHOICES"] = config.MODEL_CHOICES
templates.env.globals["media_kind"] = attachments.media_kind
templates.env.globals["note_pending"] = notes.is_pending
templates.env.globals["max_upload"] = attachments.human_size(attachments.MAX_UPLOAD_BYTES)
templates.env.globals["max_upload_bytes"] = attachments.MAX_UPLOAD_BYTES
templates.env.filters["filesize"] = attachments.human_size
templates.env.globals["DEFAULT_MODEL"] = config.DEFAULT_MODEL
# Cost is rendered through these rather than an inline '$%.3f' so the
# weight-vs-dollars choice lives in exactly one place.
templates.env.filters["cost"] = usage.format_cost
templates.env.globals["cost_noun"] = usage.cost_noun
templates.env.globals["COST_UNIT_CHOICES"] = usage.COST_UNIT_CHOICES


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

_BACKGROUND_TASKS: list[asyncio.Task] = []


@app.on_event("startup")
async def on_startup() -> None:
    db.init_db()
    # The memory files on disk at boot are copied into the history if they are
    # not there already, so a version always survives whatever the next agent
    # does to them. Cheap and idempotent - identical content is not re-stored.
    try:
        memory.snapshot_all()
    except Exception:  # noqa: BLE001 - never let a backup stop the app booting
        log.exception("Could not snapshot the memory files at startup")
    _BACKGROUND_TASKS.clear()
    _BACKGROUND_TASKS.append(asyncio.create_task(worker.worker_loop()))
    _BACKGROUND_TASKS.append(asyncio.create_task(telegram_bot.telegram_poll_loop()))
    _BACKGROUND_TASKS.append(asyncio.create_task(limits.poll_loop()))
    _BACKGROUND_TASKS.append(asyncio.create_task(netinfo.poll_loop()))
    # The preview server shares this loop, so it starts and dies with the
    # portal and needs no unit of its own. See app/preview.py.
    _BACKGROUND_TASKS.append(asyncio.create_task(preview.serve_loop()))
    log.info("Project Portal started")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Cancel the background loops on SIGTERM.

    Correctness, not speed: measured, a stop takes ~0.2s either way, because
    uvicorn abandons orphaned tasks rather than awaiting them. What this buys
    is that the worker and the Telegram poller are torn down deliberately - no
    task killed mid-await, no "Task was destroyed but it is pending" noise, and
    a defined place to add real cleanup when a loop eventually needs it.

    Each task gets a short grace period; one that refuses to unwind is logged
    and abandoned rather than allowed to hold shutdown open, since the whole
    point of a fast restart is that nothing can stall it.
    """
    for task in _BACKGROUND_TASKS:
        task.cancel()
    if _BACKGROUND_TASKS:
        done, pending = await asyncio.wait(_BACKGROUND_TASKS, timeout=3)
        if pending:
            log.warning("%d background task(s) did not stop in time", len(pending))
    _BACKGROUND_TASKS.clear()
    log.info("Project Portal stopped")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, sort: str = "") -> HTMLResponse:
    # `?sort=` both applies and sticks, so the order survives the next visit
    # without needing a settings trip. An unknown name is ignored entirely.
    if sort in config.PROJECT_SORTS:
        db.set_setting("dashboard_sort", sort)
    active_sort = sort if sort in config.PROJECT_SORTS else (
        db.get_setting("dashboard_sort") or config.DEFAULT_PROJECT_SORT
    )
    if active_sort not in config.PROJECT_SORTS:
        active_sort = config.DEFAULT_PROJECT_SORT
    projects = db.list_projects_sorted(active_sort)
    done = [p for p in projects if p["stage"] in config.DONE_STAGES]
    question_counts = db.open_question_counts()

    # Four shelves rather than one wall of cards, in the order Wes asked for:
    # active work on top, review under it, then the put-down shelves. A project
    # with an agent actually working on it is always on the top shelf whatever
    # its stored stage says - work in flight outranks everything else.
    active_run = active_run_snapshot()
    running_ids = set(active_run["project_ids"])
    shelves: dict[str, list] = {"active": [], "review": [], "paused": [], "backlog": []}
    for p in projects:
        if p["stage"] in config.DONE_STAGES:
            continue
        if p["id"] in running_ids:
            shelf = "active"
        else:
            shelf = db.project_shelf(p, question_counts.get(p["id"], 0))
        shelves[shelf].append(p)
    # Each shelf is re-ordered so a sub-project sits directly under the project
    # it was split out of, indented and labelled. Children stay real cards -
    # hiding them inside the parent would make a game an agent is actively
    # building on invisible from the one page Wes checks.
    for name, rows in list(shelves.items()):
        shelves[name] = subprojects.group_for_shelf(rows)

    shelved = db.shelved_project_ids() - running_ids
    open_qs = [q for q in db.open_questions() if q["project_id"] not in shelved]
    # 25 rather than 10 now the feed scrolls inside its own window instead of
    # stretching the page.
    recent_journal = db.list_journal(limit=25)

    settings = db.get_all_settings()
    # Named apart from the `usage` module, which this function also calls.
    usage_now = usage_snapshot()

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "active_projects": shelves["active"],
            "review_projects": shelves["review"],
            "paused_projects": shelves["paused"],
            "backlog_projects": shelves["backlog"],
            "done_projects": done,
            "open_questions": open_qs,
            "recent_journal": recent_journal,
            "usage": usage_now,
            "worker_enabled": usage_now["worker_enabled"],
            "question_counts": question_counts,
            "run_counts": db.runs_today_by_project(),
            "heatmap": usage.heatmap(),
            "active_run": active_run,
            "worker_model": settings.get("worker_model") or config.DEFAULT_MODEL,
            "sorts": config.PROJECT_SORTS,
            "active_sort": active_sort,
            # Which cards carry the "needs your OK" badge. A set of ids rather
            # than a per-card call so the gate is evaluated once per render.
            "awaiting_approval": {
                p["id"] for p in projects if worker.build_gated(p)
            },
        },
    )


@app.post("/ideas")
async def create_idea(
    title: str = Form(""), idea: str = Form(...), then: str = Form("")
) -> RedirectResponse:
    """Two buttons on the idea form (Wes's ask): plain "add idea" parks it in
    the backlog and no model ever sees it until he says so; "add and start
    planning" makes it active (still unapproved for code) and puts an agent on
    it right now."""
    title = title.strip() or idea.strip().split("\n", 1)[0][:80] or "Untitled idea"
    stage = "active" if then == "plan" else "backlog"
    project = db.create_project(title=title, description=idea.strip(), kind="unknown", stage=stage)
    if idea.strip():
        db.add_journal(project["id"], "user", "note", idea.strip())
    if then == "plan":
        await worker.queue_manual_run(project["id"])
    return RedirectResponse(url=f"/project/{project['slug']}", status_code=303)


# --------------------------------------------------------------------------
# Project page
# --------------------------------------------------------------------------

def _get_project_or_404(slug: str) -> db.sqlite3.Row:
    project = db.get_project_by_slug(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.get("/project/{slug}", response_class=HTMLResponse)
async def project_page(request: Request, slug: str) -> HTMLResponse:
    project = _get_project_or_404(slug)
    journal = db.list_journal(project["id"], limit=200)
    questions = db.open_questions(project["id"])
    runs = db.list_runs(project["id"])
    # Only the workspace root is read here; folders are fetched from /tree as
    # they are opened, so this costs one directory read however big the project
    # is. The count is a separate capped walk, purely for the section header.
    workspace = config.PROJECTS_DIR / slug
    tree_entries = filetree.children(workspace)
    file_count, file_count_capped = filetree.count_files(workspace)

    # This project's own run, which with parallel runs is not necessarily the
    # newest one in flight - the page must not report someone else's agent.
    snapshot = active_run_snapshot()
    mine = [r for r in snapshot["runs"] if r["project_id"] == project["id"]]
    active = mine[0] if mine else {"active": False, "runs": [], "project_ids": []}
    # With no run in flight, show the last finished run's transcript so the
    # console box isn't an empty hole between runs.
    console_run_id = active["run_id"] if active["active"] else (runs[0]["id"] if runs else None)

    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "project": project,
            "journal": journal,
            "questions": questions,
            "dismissed_questions": db.dismissed_questions(project["id"]),
            "runs": runs,
            "tree_entries": tree_entries,
            "file_count": file_count,
            "file_count_capped": file_count_capped,
            "effective_model": agent_runner.resolve_model(project),
            "global_model": db.get_setting("worker_model") or config.DEFAULT_MODEL,
            "active_run": active,
            "console_run_id": console_run_id,
            "console_text": runlog.tail(console_run_id, 200) if console_run_id else "",
            "runs_today": db.count_runs_today(project["id"]),
            "usage": usage_snapshot(),
            "attachments": db.list_attachments(project["id"]),
            "journal_attachments": db.attachments_by_journal(project["id"]),
            "ssh_command": config.ssh_command(slug),
            "build_gated": worker.build_gated(project),
            "research_queued": db.is_research_queued(project),
            # The model a burst would actually use, setting override included.
            "research_model": agent_runner.resolve_model(None, "research"),
            "spending_down": pacing.spending_down(),
            "ask_pending": ask.pending(project["id"]),
            "agent_todos": db.visible_todos(project["id"], owner="agent"),
            "user_todos": db.visible_todos(project["id"], owner="user"),
            # How many ticked-off items have already dropped off the live list.
            # Only shown as a link to the history, so clearing never feels like
            # deleting.
            "hidden_done_todos": db.count_hidden_done_todos(project["id"]),
            "clearable_todos": db.count_clearable_todos(project["id"]),
            "suggested_slug": db.suggested_slug(project),
            "unacked_work": db.unacknowledged_work(project["id"]),
            "heatmap": usage.heatmap(project_id=project["id"]),
            "preview_link": preview.link_for(
                project, request.url.scheme, request.url.netloc
            ),
            "children": db.child_projects(project["id"]),
            "parent": db.get_project(db.parent_id_of(project)) if db.parent_id_of(project) else None,
            "can_split": subprojects.can_have_children(project),
            "can_become_child": subprojects.can_become_child(project),
            "adopt_parents": subprojects.adoptive_parents(project),
            "child_question_counts": db.open_question_counts(),
        },
    )


@app.post("/project/{slug}/run-cap")
async def update_run_cap(slug: str, max_runs_per_day: str = Form("")) -> RedirectResponse:
    """Per-project daily run cap. Empty or 0 means "no project-specific cap"."""
    project = _get_project_or_404(slug)
    raw = max_runs_per_day.strip()
    if not raw:
        cap: Optional[int] = None
    else:
        try:
            cap = max(0, int(raw)) or None
        except ValueError:
            raise HTTPException(status_code=400, detail="Run cap must be a number")
    db.update_project(project["id"], max_runs_per_day=cap)
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/model")
async def update_model(slug: str, model: str = Form("")) -> RedirectResponse:
    # Default rather than `Form(...)`: the "inherit global" option submits an
    # empty string, which FastAPI treats as a missing required field.
    """Set (or clear, with an empty value) this project's model override."""
    project = _get_project_or_404(slug)
    model = model.strip()
    if model and model not in config.MODEL_VALUES:
        raise HTTPException(status_code=400, detail="Unknown model")
    db.update_project(project["id"], model=model or None)
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/details")
async def update_details(
    slug: str,
    title: str = Form(""),
    description: str = Form(""),
    new_slug: str = Form(""),
    title_locked: str = Form(""),
    description_locked: str = Form(""),
    preview_url: str = Form(""),
) -> RedirectResponse:
    """Edit the title, the description, and their agent locks - and optionally
    rename the workspace folder.

    Renaming is the risky half. Every path in the portal is derived from the
    slug at call time (`PROJECTS_DIR / project['slug']`, and attachments
    underneath that), so moving the directory and updating the column really is
    the whole job - there are no stored absolute paths to rewrite. What it
    can't survive is doing it *under a running agent*, whose cwd would silently
    become a deleted inode, so that case is refused rather than raced.
    """
    project = _get_project_or_404(slug)
    updates: dict = {
        "title_locked": 1 if title_locked else 0,
        "description_locked": 1 if description_locked else 0,
        "description": description.strip(),
        # Emptying the box is how the button is taken away again, so this
        # is written unconditionally - unlike the agent-set path, which
        # only ever sets it.
        "preview_url": preview_url.strip()[:500],
    }
    title = title.strip()
    if title:
        updates["title"] = title

    target = db.slugify(new_slug.strip()) if new_slug.strip() else slug
    renamed = target != slug
    if renamed:
        _check_rename(project, target)
        _move_workspace(slug, target)
        updates["slug"] = target
        # Wes has named this folder himself, so stop proposing a tidier one.
        updates["slug_locked"] = 1

    db.update_project(project["id"], **updates)
    if renamed:
        db.add_journal(
            project["id"], "user", "status", f"Renamed workspace `{slug}` -> `{target}`."
        )
    return RedirectResponse(url=f"/project/{target}", status_code=303)


@app.post("/project/{slug}/rename")
async def rename_project(slug: str, title: str = Form("")) -> RedirectResponse:
    """Rename the project by clicking its name at the top of the page.

    Deliberately *only* the title. Folder names are short directory names now
    (`db.short_slug`) rather than hyphenated copies of the title, so the two are
    no longer meant to match and renaming one must not propose moving the other.
    That is why `suggested_slug` keys on the slug still being raw idea text
    rather than on any title/slug mismatch - without that, this button would
    hand Wes a "tidy the folder?" prompt every time he corrected a typo.

    It locks the title, because a rename Wes typed himself being silently
    replaced by the next agent's report is the obvious way for this to be
    annoying. The lock is a checkbox in the details editor if he wants it back.
    """
    project = _get_project_or_404(slug)
    title = db.clean_title(title)
    if not title or title == project["title"]:
        return RedirectResponse(url=f"/project/{slug}", status_code=303)
    was = project["title"]
    db.update_project(project["id"], title=title, title_locked=1)
    db.add_journal(project["id"], "user", "status", f"Renamed `{was}` -> `{title}`.")
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


def _check_rename(project, target: str) -> None:
    """The three things that make a workspace rename unsafe, in one place so the
    tidy-up button cannot end up with a weaker set of guards than the form."""
    if project["slug"] == config.META_PROJECT_SLUG:
        raise HTTPException(
            status_code=400,
            detail="The portal's own project keeps its slug - the self-update check is pinned to it.",
        )
    if project["id"] in db.running_project_ids():
        raise HTTPException(
            status_code=409,
            detail="An agent is running in this workspace. Stop the run, then rename.",
        )
    if db.get_project_by_slug(target) is not None:
        raise HTTPException(status_code=400, detail=f"The slug `{target}` is already taken.")


@app.post("/project/{slug}/tidy-slug")
async def tidy_slug(slug: str, action: str = Form("apply")) -> RedirectResponse:
    """Rename a workspace to match its title - or stop being asked about it.

    Projects are created before they have a name, so the folder keeps whatever
    Wes typed when he had the idea. Once an agent has given the project a real
    title this closes the gap, but it stays a button rather than something a run
    does on its own: it moves a directory agents have been working in, and the
    portal has already learned once that silent autonomy is expensive.
    """
    project = _get_project_or_404(slug)
    if action == "dismiss":
        db.update_project(project["id"], slug_locked=1)
        return RedirectResponse(url=f"/project/{slug}", status_code=303)

    target = db.suggested_slug(project)
    if not target:
        # Already tidy, dismissed, or the name was taken while the page sat open.
        return RedirectResponse(url=f"/project/{slug}", status_code=303)
    _check_rename(project, target)
    _move_workspace(slug, target)
    db.update_project(project["id"], slug=target, slug_locked=1)
    db.add_journal(
        project["id"], "user", "status", f"Renamed workspace `{slug}` -> `{target}` to match the title."
    )
    return RedirectResponse(url=f"/project/{target}", status_code=303)


def _move_workspace(slug: str, target: str) -> None:
    """Rename a workspace directory. The checks (direct child of the projects
    root on both ends, destination free) live in `subprojects.move_workspace`,
    shared with the convert-to-sub-project path."""
    try:
        subprojects.move_workspace(slug, target)
    except subprojects.SplitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/project/{slug}/status")
async def update_status(slug: str, status: str = Form(...)) -> RedirectResponse:
    """The state picker, the dashboard drag zones and the context menu all post
    here. Accepts the old vocabulary too (`inbox`, `building`, `waiting_user`),
    because it is baked into bookmarks and any page rendered before a deploy."""
    project = _get_project_or_404(slug)
    state = config.normalize_state(status)
    if state is None:
        raise HTTPException(status_code=400, detail="Invalid status")
    db.set_user_state(project, state)
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/approve-build")
async def approve_build(slug: str, run_now_too: str = Form("1")) -> RedirectResponse:
    """Wes okays the build: the gate opens, the project is active, and by
    default an agent starts on it immediately so the OK and the work are one
    click rather than two."""
    project = _get_project_or_404(slug)
    db.approve_build(project["id"])
    db.add_journal(
        project["id"], "user", "status",
        "Build approved. Agents may write code here now.",
    )
    if run_now_too:
        await worker.queue_manual_run(project["id"])
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/revoke-build")
async def revoke_build(slug: str) -> RedirectResponse:
    """Undo an approval - agents go back to planning only."""
    project = _get_project_or_404(slug)
    db.update_project(project["id"], build_approved=0, build_requested=0)
    db.add_journal(
        project["id"], "user", "status",
        "Build approval withdrawn. Agents will triage and plan here, but not write code.",
    )
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/research")
async def toggle_research(slug: str, queued: str = Form("1")) -> RedirectResponse:
    """Queue (or un-queue) this project for a research burst.

    Queueing starts nothing now: the burst runs inside a spend-down window,
    when there is weekly allowance about to expire anyway. That is the whole
    bargain - it costs nothing Wes would otherwise have used.
    """
    project = _get_project_or_404(slug)
    if queued == "1":
        db.queue_research(project["id"])
        db.add_journal(
            project["id"], "user", "note",
            "Queued for a research burst - it runs the next time there is spare "
            "weekly Claude allowance about to expire.",
        )
    else:
        db.unqueue_research(project["id"])
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/subproject")
async def add_subproject(
    slug: str, title: str = Form(""), description: str = Form("")
) -> RedirectResponse:
    """Split a deliverable out of this project by hand.

    Lands on the new child rather than back on the parent: Wes has just named a
    thing, and the next thing he wants is almost always to describe it or set
    its priority, both of which are on the child's own page.
    """
    project = _get_project_or_404(slug)
    if not title.strip():
        return RedirectResponse(url=f"/project/{slug}#subprojects", status_code=303)
    try:
        child = subprojects.create_child(project, title, description)
    except subprojects.SplitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=f"/project/{child['slug']}", status_code=303)


@app.post("/project/{slug}/make-subproject")
async def make_subproject(slug: str, parent_id: int = Form(...)) -> RedirectResponse:
    """Convert this whole project into a sub-project of another one.

    Wes's ask: "Be able to convert a project to a subproject of another project
    without losing stuff." Everything hangs off the project id, so nothing is
    copied and nothing can be lost - see `subprojects.adopt` for what actually
    moves (the parent pointer and the workspace folder name).
    """
    project = _get_project_or_404(slug)
    parent = db.get_project(parent_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="No such project to join")
    try:
        updated = subprojects.adopt(parent, project)
    except subprojects.SplitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=f"/project/{updated['slug']}", status_code=303)


@app.post("/project/{slug}/release")
async def release_subproject(slug: str) -> RedirectResponse:
    """Promote a sub-project back to a top-level project - the undo for a
    conversion, and the "re-home them first" step the delete guard asks for."""
    project = _get_project_or_404(slug)
    try:
        subprojects.release(project)
    except subprojects.SplitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/priority")
async def update_priority(slug: str, priority: int = Form(...)) -> RedirectResponse:
    project = _get_project_or_404(slug)
    db.update_project(project["id"], priority=priority)
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/note")
async def add_note(
    slug: str,
    note: str = Form(""),
    then: str = Form(""),
    quote: str = Form(""),
    files: list[UploadFile] = File(default=[]),
) -> RedirectResponse:
    """A note, some files, or both.

    Uploads ride along with the note rather than going to a separate endpoint,
    which is what makes drag-and-drop, paste, the mobile camera picker and a
    recorded voice memo all work with no JavaScript beyond putting the blob into
    the file input: the browser's own multipart submit does the rest. The note
    text is carried onto each attachment row so the agent sees what a file was
    dropped to illustrate.
    """
    project = _get_project_or_404(slug)
    note = note.strip()
    # A passage highlighted in the journal rides along as `quote` and is folded
    # into the note body here (app/quoting.py). `note` itself stays as typed:
    # it is what gets carried onto each attachment row, where the caption
    # wanted is "what this file illustrates", not the passage it answers.
    quoted = quoting.frame(quote, note)
    uploads = [f for f in files if f and f.filename]
    if not quoted and not uploads:
        # An empty box with "and run" pressed still means "go" - it is the same
        # gesture as the run button, and refusing it would look like the button
        # is broken rather than like the note was empty.
        if then == "run":
            if db.display_state(project) != "active":
                db.set_user_state(project, "active")
            await worker.queue_manual_run(project["id"])
        return RedirectResponse(url=f"/project/{slug}", status_code=303)

    stored: list[dict] = []
    errors: list[str] = []
    body = quoted
    if uploads:
        for upload in uploads:
            data = await upload.read()
            try:
                stored.append(
                    attachments.store(
                        project_id=project["id"],
                        slug=slug,
                        orig_name=upload.filename or "upload",
                        data=data,
                        declared_mime=upload.content_type or "",
                        note=note,
                    )
                )
            except (ValueError, OSError) as exc:
                errors.append(f"{upload.filename}: {exc}")
        # Named in the note body too, not only in the attachments table: the
        # journal is what a human reads back, and "see the screenshot" with no
        # trace of a screenshot is a worse record than a slightly noisier one.
        if stored:
            listing = "\n".join(
                f"- `{attachments.rel_path(a['stored_name'])}` ({a['mime']}, "
                f"{attachments.human_size(a['size'])})"
                for a in stored
            )
            body = f"{quoted}\n\n**Attached {len(stored)} file(s):**\n{listing}".strip()
    if errors:
        body = f"{body}\n\n*Rejected: {'; '.join(errors)}*".strip()

    journal_id = db.add_journal(project["id"], "user", "note", body)
    for a in stored:
        db.set_attachment_journal(a["id"], journal_id)

    # "add note and run" - the note, the switch to active and the run in one
    # press, because typing an instruction and then wanting it acted on now is
    # the common case and it was three controls in three different places.
    # Ordering matters: the note is already in the journal above, so the run
    # queued here cannot start without it.
    if then == "run":
        if db.display_state(project) != "active":
            db.set_user_state(project, "active")
        await worker.queue_manual_run(project["id"])
    elif then != "queue":
        # A note on a put-down project wakes it up (Wes's rule; see the helper).
        # "queue & don't run" is the explicit opt-out: the note is stored for
        # whenever the agent next runs, and nothing else is touched.
        await worker.reactivate_on_note(project)
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/note/{note_id}/edit")
async def edit_note(slug: str, note_id: int, note: str = Form("")) -> RedirectResponse:
    """Rewrite a note no agent has read yet.

    The window closes the instant the note goes into a prompt (app/notes.py), so
    this is deliberately a thin wrapper over a single guarded UPDATE rather than
    a check-then-write: a run starting between the two would otherwise edit a
    note an agent was already acting on. An empty box deletes it - the same
    gesture as clearing any other field here, and it saves a second button on a
    row that is already cramped on a phone.
    """
    project = _get_project_or_404(slug)
    entry = db.get_journal(note_id)
    if entry is None or entry["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="No such note on this project")
    body = note.strip()
    if body:
        db.update_journal_content(note_id, body)
    else:
        db.delete_journal_note(note_id)
    return RedirectResponse(url=f"/project/{slug}#journal", status_code=303)


@app.post("/project/{slug}/note/{note_id}/delete")
async def delete_note(slug: str, note_id: int) -> RedirectResponse:
    project = _get_project_or_404(slug)
    entry = db.get_journal(note_id)
    if entry is None or entry["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="No such note on this project")
    db.delete_journal_note(note_id)
    return RedirectResponse(url=f"/project/{slug}#journal", status_code=303)


@app.post("/project/{slug}/ask")
async def ask_project(
    slug: str, question: str = Form(""), quote: str = Form("")
) -> RedirectResponse:
    """Ask a question about a project without starting a run.

    Read-only and off the budget - see app/ask.py. Only one at a time per
    project: a second submission while one is thinking would answer against the
    same context and just cost twice.

    `quote` is a passage highlighted in the journal; it is folded into the
    question so both the journal entry and the model's prompt show what is
    being asked about (app/quoting.py). A quote with nothing typed is a valid
    question - "what is this?" - so it is not rejected as empty.
    """
    project = _get_project_or_404(slug)
    question = quoting.frame(quote, question)
    if question and not ask.pending(project["id"]):
        ask.start(project["id"], question)
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


@app.get("/attachment/{attachment_id}")
async def attachment_file(attachment_id: int) -> FileResponse:
    """Serve an uploaded file.

    Only a known-safe media type is served inline; everything else is forced to
    download under a neutral content type. Serving user-supplied HTML or SVG
    inline from this origin would be script execution on the portal's own
    domain, and a photo is not worth that.
    """
    row = db.get_attachment(attachment_id)
    if row is None or not row["stored_name"] or not row["project_slug"]:
        raise HTTPException(status_code=404, detail="Attachment not found")
    path = attachments.disk_path(row["project_slug"], row["stored_name"])
    if path is None:
        raise HTTPException(status_code=404, detail="Attachment file is missing")
    inline = row["mime"] in attachments.INLINE_TYPES
    return FileResponse(
        path,
        media_type=row["mime"] if inline else "application/octet-stream",
        filename=row["orig_name"],
        content_disposition_type="inline" if inline else "attachment",
    )


@app.post("/attachment/{attachment_id}/delete")
async def delete_attachment_route(attachment_id: int) -> RedirectResponse:
    row = db.get_attachment(attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    slug = row["project_slug"] or ""
    if row["stored_name"] and slug:
        attachments.remove_file(slug, row["stored_name"])
    db.delete_attachment(attachment_id)
    return RedirectResponse(url=f"/project/{slug}" if slug else "/", status_code=303)


@app.post("/project/{slug}/delete")
async def delete_project_route(
    slug: str, confirm: str = Form(""), delete_workspace: Optional[str] = Form(None)
) -> RedirectResponse:
    """Delete a project for good. Guarded three ways, because there is no undo:
    the meta-project can't be deleted (the worker and this very page depend on
    it), a run in flight on the project blocks it, and the form requires the
    slug to be typed back."""
    project = _get_project_or_404(slug)
    if slug == config.META_PROJECT_SLUG:
        raise HTTPException(status_code=400, detail="The portal's own project can't be deleted.")
    if confirm.strip() != slug:
        raise HTTPException(status_code=400, detail="Type the project slug to confirm deletion.")
    blocked = subprojects.blocks_delete(project)
    if blocked:
        raise HTTPException(status_code=409, detail=blocked)
    active = active_run_snapshot()
    if active["active"] and active["project_id"] == project["id"]:
        raise HTTPException(status_code=409, detail="Stop the running agent before deleting.")

    if delete_workspace:
        _remove_workspace(slug)
    db.delete_project(project["id"])
    db.add_journal(
        None, "user", "status", f"Deleted project **{project['title']}** (`{slug}`)."
    )
    log.info("Deleted project %s (workspace removed: %s)", slug, bool(delete_workspace))
    return RedirectResponse(url="/", status_code=303)


def _remove_workspace(slug: str) -> None:
    """Remove a project's workspace directory, but only after confirming the
    resolved path really is a direct child of PROJECTS_DIR - a slug is
    user-controlled and this is an rm -rf."""
    root = config.PROJECTS_DIR.resolve()
    target = (config.PROJECTS_DIR / slug).resolve()
    if target.parent != root or target == root or not target.is_dir():
        return
    shutil.rmtree(target, ignore_errors=True)


@app.post("/project/{slug}/run")
async def run_now(slug: str) -> RedirectResponse:
    """The run button is also a state gesture: asking for an agent puts the
    project back on the active shelf, whatever it was (see worker.run_now)."""
    project = _get_project_or_404(slug)
    await worker.run_now(project)
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Activity: the portal-wide run feed and per-run transcripts
# --------------------------------------------------------------------------

ACTIVITY_PAGE_SIZE = 40
HISTORY_WINDOWS = [7, 14, 30, 90]


def _decorate_runs(rows) -> list[dict]:
    """Flatten run rows for the templates, adding the derived fields (duration,
    whether a transcript still exists on disk) that SQL can't give us."""
    out = []
    for row in rows:
        secs = usage.run_duration(row)
        out.append(
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "project_slug": row["project_slug"],
                "project_title": _run_owner_label(row),
                "oneoff_id": db._row_get(row, "oneoff_id"),  # noqa: SLF001
                "task": row["task"],
                "model": row["model"] or "-",
                "status": row["status"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "duration": usage.humanize_seconds(secs),
                "cost_usd": row["cost_usd"],
                "num_turns": row["num_turns"],
                "events": row["events"] or 0,
                "summary": row["summary"] or "",
                "has_log": runlog.log_path(row["id"]).exists(),
            }
        )
    return out


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(
    request: Request, page: int = 1, days: int = 14, status: str = "", project: str = ""
) -> HTMLResponse:
    page = max(1, page)
    days = days if days in HISTORY_WINDOWS else 14
    status = status if status in config.RUN_STATUSES else ""

    project_row = db.get_project_by_slug(project) if project else None
    project_id = project_row["id"] if project_row else None

    total = db.count_recent_runs(project_id=project_id, status=status or None)
    rows = db.list_recent_runs(
        limit=ACTIVITY_PAGE_SIZE,
        offset=(page - 1) * ACTIVITY_PAGE_SIZE,
        project_id=project_id,
        status=status or None,
    )
    pages = max(1, (total + ACTIVITY_PAGE_SIZE - 1) // ACTIVITY_PAGE_SIZE)

    return templates.TemplateResponse(
        request,
        "activity.html",
        {
            "runs": _decorate_runs(rows),
            "history": usage.history(days, project_id=project_id),
            "usage": usage_snapshot(),
            "active_run": active_run_snapshot(),
            "page": page,
            "pages": pages,
            "total": total,
            "days": days,
            "windows": HISTORY_WINDOWS,
            "status_filter": status,
            "project_filter": project_row["slug"] if project_row else "",
            "projects": db.list_projects(),
        },
    )


@app.get("/run/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: int) -> HTMLResponse:
    row = db.get_run_with_project(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run = _decorate_runs([row])[0]
    text, _ = runlog.read_log(run_id, 0)
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "run": run,
            # A pruned transcript is a normal outcome, not an error - say so
            # rather than showing an empty terminal.
            "console_text": text or "",
            "log_pruned": not run["has_log"],
            "denials": db.hook_denials_for_run(run_id),
            "audit": db.hook_audit_for_run(run_id),
            "audit_retention_days": db.AUDIT_RETENTION_DAYS,
            "active_run": active_run_snapshot(),
        },
    )


@app.post("/run/{run_id}/cancel")
async def cancel_run_route(run_id: int, next: str = Form("/")) -> RedirectResponse:
    """Stop the agent mid-run. Reachable from the dashboard strip, the project
    console and the run page, so it redirects back to wherever it was pressed."""
    outcome = worker.cancel_run(run_id)
    log.info("Cancel run %s -> %s", run_id, outcome)
    return RedirectResponse(url=_safe_next(next), status_code=303)


def _safe_next(target: str) -> str:
    """Only ever redirect to a path on this app - never to an absolute URL a
    crafted form could smuggle in."""
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


# ---------------------------------------------------------------------------
# One-off tasks: scratch agent sessions without a project
# ---------------------------------------------------------------------------


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request) -> HTMLResponse:
    running_ids = {r["oneoff_id"] for r in db.active_runs() if r["oneoff_id"]}
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "open_tasks": db.list_oneoffs("open"),
            "archived_tasks": db.list_oneoffs("archived"),
            "running_ids": running_ids,
            "active_run": active_run_snapshot(),
        },
    )


@app.post("/tasks")
async def create_oneoff_route(text: str = Form(...)) -> RedirectResponse:
    text = text.strip()
    if not text:
        return RedirectResponse(url="/tasks", status_code=303)
    task = db.create_oneoff(text)
    worker.spawn_oneoff(task["id"])
    return RedirectResponse(url=f"/tasks/{task['id']}", status_code=303)


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def oneoff_page(request: Request, task_id: int) -> HTMLResponse:
    task = db.get_oneoff(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    running = db.oneoff_running(task_id)
    latest = db.latest_oneoff_run(task_id)
    return templates.TemplateResponse(
        request,
        "oneoff.html",
        {
            "task": task,
            "messages": db.list_oneoff_messages(task_id),
            "running": running,
            "latest_run": latest,
            "queued": len(db.pending_oneoff_messages(task_id)) if running else 0,
            "workspace": str(oneoff.workspace(task_id)),
            "active_run": active_run_snapshot(),
        },
    )


@app.post("/tasks/{task_id}/message")
async def oneoff_message(task_id: int, text: str = Form(...)) -> RedirectResponse:
    task = db.get_oneoff(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "open":
        # The page hides the reply box on an archived task; reaching this
        # means a stale tab. Do not quietly resurrect the session.
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)
    text = text.strip()
    if text:
        db.add_oneoff_message(task_id, "wes", text)
        # No-op if a run is already going - the message waits as pending and
        # the finishing run starts the next one itself.
        worker.spawn_oneoff(task_id)
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/archive")
async def oneoff_archive(task_id: int) -> RedirectResponse:
    if db.get_oneoff(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    db.set_oneoff_status(task_id, "archived")
    return RedirectResponse(url="/tasks", status_code=303)


@app.post("/tasks/{task_id}/unarchive")
async def oneoff_unarchive(task_id: int) -> RedirectResponse:
    if db.get_oneoff(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    db.set_oneoff_status(task_id, "open")
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.get("/questions", response_class=HTMLResponse)
async def questions_page(request: Request) -> HTMLResponse:
    # Questions on projects Wes has paused, or that are still in the backlog,
    # are answerable but not pressing - they go in their own section below the
    # live ones rather than competing with them.
    shelved = db.shelved_project_ids()
    open_qs = db.open_questions()
    return templates.TemplateResponse(
        request,
        "questions.html",
        {
            "questions": [q for q in open_qs if q["project_id"] not in shelved],
            "shelved_questions": [q for q in open_qs if q["project_id"] in shelved],
        },
    )


def _after_question(question: sqlite3.Row, next: str) -> str:
    """Where to land after answering or dismissing.

    Answering from the questions tab keeps you on the questions tab - working
    through a stack of them shouldn't bounce you into a project page and back
    for each one. Without an explicit `next` (the project page's own form) the
    old behaviour of returning to the project stands.
    """
    if next:
        return _safe_next(next)
    project = db.get_project(question["project_id"])
    return f"/project/{project['slug']}" if project else "/questions"


@app.post("/questions/{question_id}/answer")
async def answer_question(
    question_id: int, answer: str = Form(...), next: str = Form("")
) -> RedirectResponse:
    question = db.get_question(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    db.answer_question_and_resume(question_id, answer.strip())
    return RedirectResponse(url=_after_question(question, next), status_code=303)


@app.post("/questions/{question_id}/dismiss")
async def dismiss_question(question_id: int, next: str = Form("")) -> RedirectResponse:
    """Dismissing clears the notification and takes the question off the
    questions tab. The question itself stays on its project - it is still a
    record of something the agent wanted to know."""
    question = db.get_question(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    db.dismiss_question_and_resume(question_id)
    return RedirectResponse(url=_after_question(question, next), status_code=303)


@app.post("/questions/{question_id}/reopen")
async def reopen_question(question_id: int) -> RedirectResponse:
    """Undo a dismissal - the question goes back on the questions tab with a
    fresh number."""
    question = db.get_question(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    db.reopen_question(question_id)
    project = db.get_project(question["project_id"])
    return RedirectResponse(
        url=f"/project/{project['slug']}" if project else "/questions", status_code=303
    )


# --------------------------------------------------------------------------
# Todos
# --------------------------------------------------------------------------

def _todo_redirect(slug: str) -> RedirectResponse:
    """Back to the project page, with no anchor.

    It used to redirect to `#todos`, which was better than the top of the page
    but still not what Wes asked for: ticking an item halfway down a long list
    jumped him to the *heading* of the section. An explicit anchor also beats
    the saved-scroll restore in app.js by design, so the anchor was actively
    stopping the thing that puts you back exactly where you were."""
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/todo")
async def add_todo(
    slug: str, text: str = Form(...), owner: str = Form("agent")
) -> RedirectResponse:
    project = _get_project_or_404(slug)
    db.add_todo(project["id"], text, owner)
    return _todo_redirect(slug)


@app.post("/todo/{todo_id}/toggle")
async def toggle_todo(todo_id: int, done: str = Form("")) -> RedirectResponse:
    """Tick or untick. `done` is the state to move *to*, so the checkbox is
    idempotent under a double submit rather than flapping."""
    todo = db.get_todo(todo_id)
    if todo is None:
        raise HTTPException(status_code=404, detail="Todo not found")
    db.set_todo_done(todo_id, done == "1")
    project = db.get_project(todo["project_id"])
    return _todo_redirect(project["slug"]) if project else RedirectResponse(url="/", status_code=303)


@app.post("/todo/{todo_id}/tag")
async def tag_todo(todo_id: int, add: str = Form(""), remove: str = Form("")) -> RedirectResponse:
    """Put a tag on a row or take one off. Both fields normalise in db, so
    typing 'Ready to Build' lands as the chip `ready-to-build`."""
    todo = db.get_todo(todo_id)
    if todo is None:
        raise HTTPException(status_code=404, detail="Todo not found")
    if add.strip():
        db.add_todo_tag(todo_id, add)
    if remove.strip():
        db.remove_todo_tag(todo_id, remove)
    project = db.get_project(todo["project_id"])
    return _todo_redirect(project["slug"]) if project else RedirectResponse(url="/", status_code=303)


@app.post("/todo/{todo_id}/delete")
async def delete_todo(todo_id: int) -> RedirectResponse:
    todo = db.get_todo(todo_id)
    if todo is None:
        raise HTTPException(status_code=404, detail="Todo not found")
    project = db.get_project(todo["project_id"])
    db.delete_todo(todo_id)
    return _todo_redirect(project["slug"]) if project else RedirectResponse(url="/", status_code=303)


@app.post("/project/{slug}/todos/clear-completed")
async def clear_completed_todos(slug: str) -> RedirectResponse:
    """Take every ticked-off item off the live list in one go.

    Nothing is deleted - the history page below still has all of it - which is
    what makes a one-click sweep safe to offer."""
    project = _get_project_or_404(slug)
    db.clear_completed_todos(project["id"])
    return _todo_redirect(slug)


@app.get("/project/{slug}/todos/history", response_class=HTMLResponse)
async def todo_history(request: Request, slug: str) -> HTMLResponse:
    """Everything ever ticked off on this project, newest first."""
    project = _get_project_or_404(slug)
    return templates.TemplateResponse(
        request,
        "todo_history.html",
        {
            "project": project,
            "agent_done": db.completed_todos(project["id"], owner="agent"),
            "user_done": db.completed_todos(project["id"], owner="user"),
        },
    )


@app.post("/project/{slug}/acknowledge")
async def acknowledge_work(slug: str) -> RedirectResponse:
    """Clear the "since you last looked" banner and give the space back."""
    project = _get_project_or_404(slug)
    db.acknowledge_work(project["id"])
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------

@app.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request) -> HTMLResponse:
    profile = config.PROFILE_MD.read_text(encoding="utf-8") if config.PROFILE_MD.exists() else ""
    learnings = config.LEARNINGS_MD.read_text(encoding="utf-8") if config.LEARNINGS_MD.exists() else ""
    suggestions = db.list_suggestions()
    # The CLI's own, separate auto-memory - read-only, surfaced so it is not
    # a second memory system Wes cannot see (#223).
    try:
        known = climemory.build_known(db.list_projects(), db.list_oneoffs())
        cli_memory = climemory.scan(known)
    except Exception:  # noqa: BLE001 - a broken scan must never 500 /memory
        log.exception("Could not scan the CLI auto-memory")
        cli_memory = []
    return templates.TemplateResponse(
        request,
        "memory.html",
        {
            "profile": profile,
            "learnings": learnings,
            "suggestions": suggestions,
            "learnings_lines": len(learnings.splitlines()),
            "learnings_chars": len(learnings),
            "learnings_cap": worker.learnings_cap(),
            "learnings_over_cap": worker.learnings_over_cap(),
            "revisions": memory.revisions(),
            "compacting": worker.compaction_running(),
            "cli_memory": cli_memory,
            "cli_memory_files": sum(d.file_count for d in cli_memory),
            "archived_learnings": memory.archived_learnings(),
            "learnings_freshness": worker.learnings_freshness(),
            "promoted_skills": memory.promoted_skills(),
        },
    )


@app.post("/memory/profile")
async def save_profile(content: str = Form(...)) -> RedirectResponse:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memory.snapshot("profile")
    config.PROFILE_MD.write_text(content, encoding="utf-8")
    return RedirectResponse(url="/memory", status_code=303)


@app.post("/memory/learnings")
async def save_learnings(content: str = Form(...)) -> RedirectResponse:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memory.snapshot("learnings")
    config.LEARNINGS_MD.write_text(content, encoding="utf-8")
    return RedirectResponse(url="/memory", status_code=303)


@app.post("/memory/compact")
async def compact_learnings() -> RedirectResponse:
    """Send an agent through learnings.md to distil it.

    Deliberately a button rather than a schedule - see worker.start_compaction.
    """
    worker.start_compaction()
    return RedirectResponse(url="/memory", status_code=303)


@app.get("/memory/revision/{name}", response_class=HTMLResponse)
async def view_revision(request: Request, name: str) -> HTMLResponse:
    text = memory.read_revision(name)
    if text is None:
        raise HTTPException(status_code=404, detail="No such revision")
    return templates.TemplateResponse(
        request, "revision.html", {"name": name, "text": text}
    )


@app.post("/memory/revision/{name}/restore")
async def restore_revision(name: str) -> RedirectResponse:
    if not memory.restore(name):
        raise HTTPException(status_code=404, detail="No such revision")
    return RedirectResponse(url="/memory", status_code=303)


@app.get("/memory/skill/{name}", response_class=HTMLResponse)
async def view_promoted_skill(request: Request, name: str) -> HTMLResponse:
    """One promoted skill's SKILL.md, plain. The traversal guard lives in
    memory.read_promoted_skill - a bad name reads as absent."""
    text = memory.read_promoted_skill(name)
    if text is None:
        raise HTTPException(status_code=404, detail="No such skill")
    return templates.TemplateResponse(
        request, "skillview.html", {"name": name, "text": text}
    )


@app.post("/memory/skill/{name}/delete")
async def delete_promoted_skill(name: str) -> RedirectResponse:
    """Wes overrules a promotion. The stale sweep in worker._sync_skills then
    removes the skill from every workspace on its next run."""
    if not memory.delete_promoted_skill(name):
        raise HTTPException(status_code=404, detail="No such skill")
    return RedirectResponse(url="/memory", status_code=303)


@app.get("/memory/cli/{dir_name}/{filename}", response_class=HTMLResponse)
async def view_cli_memory(request: Request, dir_name: str, filename: str) -> HTMLResponse:
    """Read one of the CLI's own auto-memory files. Strictly read-only - the
    portal never edits the CLI's memory; this just makes it visible (#223)."""
    text = climemory.read_file(dir_name, filename)
    if text is None:
        raise HTTPException(status_code=404, detail="No such memory file")
    return templates.TemplateResponse(
        request, "revision.html", {"name": f"CLI memory: {filename}", "text": text}
    )


@app.post("/suggestions/{suggestion_id}/accept")
async def accept_suggestion(suggestion_id: int) -> RedirectResponse:
    suggestion = db.get_suggestion(suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    project = db.create_project(title=suggestion["title"], description=suggestion["description"], kind="unknown", status="inbox")
    db.set_suggestion_status(suggestion_id, "accepted")
    return RedirectResponse(url=f"/project/{project['slug']}", status_code=303)


@app.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(suggestion_id: int) -> RedirectResponse:
    suggestion = db.get_suggestion(suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    db.set_suggestion_status(suggestion_id, "dismissed")
    return RedirectResponse(url="/memory", status_code=303)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@app.get("/style", response_class=HTMLResponse)
async def style_guide(request: Request) -> HTMLResponse:
    """The dark terminal theme packaged to reuse (the portal follows it too):
    a gallery of every component, styled by terminal-theme.css ALONE - the
    page doubles as the proof that the template is self-contained.
    tests/test_style_template.py keeps the template's tokens in sync with
    style.css."""
    return templates.TemplateResponse(request, "style_guide.html", {})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    settings = db.get_all_settings()
    # `saved` is the comma-separated list of keys the previous POST actually
    # wrote. Echoing it back turns "did that stick?" into something the page
    # answers directly, instead of a silent redirect that looks identical
    # whether one setting changed or none did.
    saved = [
        key for key in (request.query_params.get("saved") or "").split(",")
        if key in settings_form.REGISTRY
    ]
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": settings,
            "usage": usage_snapshot(),
            "saved": saved,
            "sent": request.query_params.get("sent") == "1",
            "push_sent": request.query_params.get("push_sent"),
            "push_subs": db.list_push_subscriptions(),
            "max_parallel_limit": config.MAX_PARALLEL_LIMIT,
            "slug_suggestions": db.projects_with_suggested_slugs(),
            "tailnet": netinfo.cached(),
            "portal_port": config.PORT,
            "model_catalog": modelwatch.catalog(),
        },
    )


@app.post("/settings/bonus-runs")
async def bonus_runs(extra: int = Form(...)) -> RedirectResponse:
    """Temporarily raise today's run budget. Expires at UTC midnight on its
    own; `extra=0` clears the boost immediately."""
    if extra == 0:
        db.grant_bonus_runs(-db.bonus_runs_today())
    else:
        db.grant_bonus_runs(extra)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings")
async def save_settings(request: Request) -> RedirectResponse:
    """Save one section of the Settings page (or the whole thing).

    Fields are read off the raw form and validated by `settings_form` rather
    than being named as handler parameters. That is what makes the page safely
    splittable: each section declares the settings it owns, so saving
    Appearance can never blank out the Telegram token, and a field the running
    code does not recognise is dropped loudly at one choke point instead of
    being silently ignored by FastAPI's form binding.
    """
    form = await request.form()
    declared = form.get(settings_form.FIELDS_INPUT)
    values = settings_form.apply(
        {key: str(value) for key, value in form.multi_items()},
        declared if isinstance(declared, str) else None,
    )
    for key, value in values.items():
        db.set_setting(key, value)
    section = str(form.get("_section") or "").strip()
    saved = ",".join(sorted(values))
    url = f"/settings?saved={quote(saved)}"
    if section:
        url += f"#{quote(section)}"
    return RedirectResponse(url=url, status_code=303)


@app.post("/settings/test-notification")
async def test_notification() -> RedirectResponse:
    await notify.notify("Project Portal", "Test notification from Settings page.")
    return RedirectResponse(url="/settings?sent=1#notifications", status_code=303)


# --------------------------------------------------------------------------
# Web push (see app/webpush.py)
# --------------------------------------------------------------------------

@app.get("/push/pubkey")
async def push_pubkey() -> JSONResponse:
    return JSONResponse({"key": webpush.public_key_b64()})


@app.post("/push/subscribe")
async def push_subscribe(request: Request) -> JSONResponse:
    """Store the PushSubscription the browser just created. The body is the
    subscription's own toJSON() shape: {endpoint, keys: {p256dh, auth}}."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="not JSON")
    endpoint = data.get("endpoint") if isinstance(data, dict) else None
    keys = data.get("keys") if isinstance(data, dict) and isinstance(data.get("keys"), dict) else {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if (
        not isinstance(endpoint, str)
        or not endpoint.startswith("https://")
        or not isinstance(p256dh, str)
        or not p256dh
        or not isinstance(auth, str)
        or not auth
    ):
        raise HTTPException(status_code=400, detail="not a push subscription")
    db.add_push_subscription(
        endpoint, p256dh, auth, ua=(request.headers.get("user-agent") or "")[:200]
    )
    return JSONResponse({"ok": True})


@app.post("/push/unsubscribe")
async def push_unsubscribe(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="not JSON")
    endpoint = data.get("endpoint") if isinstance(data, dict) else None
    if isinstance(endpoint, str) and endpoint:
        db.delete_push_subscription(endpoint)
    return JSONResponse({"ok": True})


@app.post("/push/remove/{sub_id}")
async def push_remove(sub_id: int) -> RedirectResponse:
    """The settings-page remove button. The device keeps its OS-level
    subscription until it re-enrols or revokes it, but the portal stops
    sending to it immediately."""
    db.delete_push_subscription_by_id(sub_id)
    return RedirectResponse(url="/settings#notifications", status_code=303)


@app.post("/settings/test-push")
async def test_push() -> RedirectResponse:
    sent = await webpush.push_all("Project Portal", "Test push from Settings page.", urgency="high")
    return RedirectResponse(url=f"/settings?push_sent={sent}#notifications", status_code=303)


# --------------------------------------------------------------------------
# File viewer
# --------------------------------------------------------------------------

MAX_FILE_BYTES = 500 * 1024


def _workspace_file(slug: str, path: str) -> Path:
    """Resolve `path` inside a project's workspace, refusing anything that
    escapes it.

    resolve() collapses any symlinks (including ones inside the workspace that
    point outside it) before the containment check, so this also rejects
    symlink escapes without a separate is_symlink() check.
    """
    workspace = (config.PROJECTS_DIR / slug).resolve()
    if not workspace.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")
    candidate = (workspace / path).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return candidate


def _workspace_dir(slug: str, path: str) -> Path:
    """Resolve a *directory* inside a project's workspace, refusing escapes.

    The same containment rule as `_workspace_file`, and deliberately a separate
    function rather than a flag on it: this one must not 404 on a path that
    happens to be a file, it must reject it, or `/tree/x/README.md` would be a
    way to ask the tree endpoint questions about files.
    """
    workspace = (config.PROJECTS_DIR / slug).resolve()
    if not workspace.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")
    candidate = (workspace / path).resolve() if path else workspace
    try:
        candidate.relative_to(workspace)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")
    return candidate


@app.get("/tree/{slug}", response_class=HTMLResponse)
@app.get("/tree/{slug}/{path:path}", response_class=HTMLResponse)
async def workspace_tree(request: Request, slug: str, path: str = "") -> HTMLResponse:
    """The contents of one workspace directory, as an HTML fragment.

    The project page renders only the workspace root; every folder below it is
    fetched from here the first time it is opened. That is what keeps a page
    for a project with a `node_modules` in it the same size as a page for one
    without.
    """
    project = _get_project_or_404(slug)
    _workspace_dir(slug, path)
    workspace = config.PROJECTS_DIR / slug
    return templates.TemplateResponse(
        request,
        "_file_tree.html",
        {"entries": filetree.children(workspace, path), "project": project},
    )


@app.get("/download/{slug}/{path:path}")
async def download_file(slug: str, path: str) -> FileResponse:
    """Download a workspace file as-is.

    Unlike the viewer this has no size or text-only limit - the point is to get
    a binary, a big log or a whole built artifact off the server. It is always
    served as an attachment under a neutral content type: workspace files are
    written by agents, and serving one inline would be script execution on the
    portal's own origin.
    """
    _get_project_or_404(slug)
    candidate = _workspace_file(slug, path)
    return FileResponse(
        candidate,
        media_type="application/octet-stream",
        filename=candidate.name,
        content_disposition_type="attachment",
    )


@app.get("/raw/{slug}/{path:path}")
async def raw_file(slug: str, path: str) -> FileResponse:
    """Serve a workspace file *inline*, for the <img>/<audio>/<video>/<iframe>
    on the file page to fetch.

    Only the media types in fileview's whitelist are served this way; anything
    else 415s and stays on the attachment-only download route. Two headers do
    the rest of the work:

      * nosniff, so a browser cannot decide a mislabelled .png is really HTML
        and run it on the portal's origin;
      * a sandbox CSP, which is what actually contains a hostile PDF - PDF
        viewers run script, and this file was written by an agent.
    """
    _get_project_or_404(slug)
    candidate = _workspace_file(slug, path)
    media_type = fileview.inline_media_type(candidate)
    if media_type is None:
        raise HTTPException(status_code=415, detail="Not an inline-viewable type - use /download")
    return FileResponse(
        candidate,
        media_type=media_type,
        filename=candidate.name,
        content_disposition_type="inline",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; object-src 'none'",
        },
    )


@app.get("/tasks/{task_id}/raw/{path:path}")
async def oneoff_raw_file(task_id: int, path: str) -> FileResponse:
    """Serve a one-off task's workspace file inline, so an agent reply there
    can embed a screenshot the same way a journal entry does. Same whitelist
    and same containment rule as the project route above."""
    if db.get_oneoff(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    workspace = oneoff.workspace(task_id).resolve()
    if not workspace.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")
    candidate = (workspace / path).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    media_type = fileview.inline_media_type(candidate)
    if media_type is None:
        raise HTTPException(status_code=415, detail="Not an inline-viewable type")
    return FileResponse(
        candidate,
        media_type=media_type,
        filename=candidate.name,
        content_disposition_type="inline",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; object-src 'none'",
        },
    )


@app.get("/open/{slug}")
async def open_built(request: Request, slug: str):
    """The "open it" button. Guarantees, as far as possible, that what opens
    is a *working* page (Wes: "When I click it, it should surely open a
    working version of whatever page it is").

    A portal-served static page redirects immediately - the portal itself is
    what serves it. An explicit preview_url is probed first; when nothing
    answers, the project's `.portal/serve.json` recipe (if any) is started and
    a holding page polls until the server comes up, then takes Wes there.
    """
    project = _get_project_or_404(slug)
    link = preview.link_for(project, request.url.scheme, request.url.netloc)
    if link is None:
        raise HTTPException(status_code=404, detail="This project has nothing to open")
    explicit = (project["preview_url"] or "").strip()
    if not explicit:
        return RedirectResponse(url=link["url"])
    if await asyncio.to_thread(launch.probe, explicit):
        return RedirectResponse(url=explicit)

    cfg = launch.serve_config(slug)
    started, detail = (False, None)
    if cfg is not None:
        started, detail = await asyncio.to_thread(launch.start, slug, cfg)
    return templates.TemplateResponse(
        request,
        "launching.html",
        {
            "project": project,
            "target": explicit,
            "cfg": cfg,
            "started": started,
            "detail": detail,
        },
    )


@app.get("/open/{slug}/status")
async def open_built_status(slug: str) -> JSONResponse:
    """Polled by the holding page: is the project's server answering yet?"""
    project = _get_project_or_404(slug)
    explicit = (project["preview_url"] or "").strip()
    if not explicit:
        return JSONResponse({"up": True, "url": ""})
    up = await asyncio.to_thread(launch.probe, explicit)
    return JSONResponse({"up": up, "url": explicit})


@app.get("/file/{slug}/{path:path}", response_class=HTMLResponse)
async def file_view(request: Request, slug: str, path: str) -> HTMLResponse:
    """View one workspace file, rendered according to what it actually is.

    Nothing here 415s any more. A file the viewer cannot show still gets a page
    - with its size, its type and the download link - because arriving at a raw
    error detail after clicking a name in the file list is a dead end.
    """
    project = _get_project_or_404(slug)
    candidate = _workspace_file(slug, path)

    view = fileview.describe(candidate)
    body = view.html
    if view.kind == "markdown":
        body = render_markdown(view.text)

    return templates.TemplateResponse(
        request,
        "file_view.html",
        {
            "project": project,
            # Not "path": base.html does `{% set path = request.url.path %}` at
            # template level, which shadows a context variable of that name
            # inside every child block. That is why this page has always shown
            # the URL where the file name should be - and why the download link
            # added here came out as /download/<slug>//file/<slug>/<name>.
            "rel_path": path,
            "view": view,
            "body": body,
            "size": view.size,
        },
    )


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/api/status")
async def api_status() -> JSONResponse:
    settings = db.get_all_settings()
    return JSONResponse(
        {
            "ok": True,
            "worker_enabled": settings.get("worker_enabled") == "1",
            "runs_today": db.count_runs_today(),
            "open_questions": len(db.open_questions()),
            "backoff_until": settings.get("backoff_until") or "",
        }
    )


@app.get("/api/ping")
async def api_ping() -> PlainTextResponse:
    """Cheapest possible liveness check - no DB, no templates. The offline
    overlay polls this while the service is restarting itself, so it has to
    answer even when the rest of the app is still warming up."""
    return PlainTextResponse("pong")


@app.get("/api/version")
async def api_version() -> JSONResponse:
    """Polled by every page for live refresh. The token's boot half changes on
    restart (client does a full reload - its CSS/JS may be stale); the data
    half changes on any database commit (client patches the page in place).
    See app/live.py."""
    return JSONResponse({"v": live.version_token()})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    """Browsers probe the origin root for /favicon.ico whatever the <link> tags
    say, and a 404 there is enough to leave a tab with no icon - which is what
    Wes was seeing on the dashboard while project pages looked fine."""
    return FileResponse(
        config.BASE_DIR / "app" / "static" / "favicon.ico",
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/usage")
async def api_usage() -> JSONResponse:
    return JSONResponse(usage_snapshot())


@app.get("/api/limits")
async def api_limits(refresh: int = 0) -> JSONResponse:
    """The account's real Claude usage windows.

    Serves the cached snapshot by default. `?refresh=1` forces a live read -
    useful from a terminal and from the settings page, and deliberately opt-in
    so that ordinary polling can never turn into a request storm against the
    usage endpoint.
    """
    snapshot = await limits.refresh_async() if refresh else limits.cached()
    return JSONResponse(snapshot)


@app.get("/api/tailnet")
async def api_tailnet(refresh: int = 0) -> JSONResponse:
    """How the portal is reachable, and from which of Wes's machines.

    Cached by default for the same reason /api/limits is: taking the reading
    means three `tailscale` subprocesses, and no page render should wait on
    them. `?refresh=1` forces a live read.
    """
    if refresh:
        snap = await asyncio.to_thread(netinfo.snapshot)
        netinfo.store(snap)
        return JSONResponse(snap)
    return JSONResponse(netinfo.cached())


@app.get("/api/usage/history")
async def api_usage_history(days: int = 14, project: str = "") -> JSONResponse:
    project_row = db.get_project_by_slug(project) if project else None
    return JSONResponse(
        usage.history(days, project_id=project_row["id"] if project_row else None)
    )


@app.post("/hooks/pre-tool")
async def hooks_pre_tool(request: Request, run: int = 0, token: str = "") -> JSONResponse:
    """PreToolUse guardrail endpoint: every guarded run's tool calls arrive
    here (relayed by app/hookrelay.py) and get an allow/deny back. Loopback
    traffic from runs this portal spawned itself; anything unrecognisable
    fails open to allow - see app/hookguard.py."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - junk in, allow out
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    decision, reason = hookguard.decide(run, token, payload)
    return JSONResponse({"decision": decision, "reason": reason})


@app.post("/hooks/post-tool")
async def hooks_post_tool(request: Request, run: int = 0, token: str = "") -> JSONResponse:
    """PostToolUse audit endpoint: pure observation, one hook_events row per
    tool call a run makes. The answer carries no decision on purpose - the
    relay prints nothing and the CLI proceeds as if unhooked."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - junk in, one unrecorded row out
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    hookguard.record_tool_use(run, token, payload)
    return JSONResponse({"ok": True})


@app.post("/hooks/stop")
async def hooks_stop(request: Request, run: int = 0, token: str = "") -> JSONResponse:
    """Stop-hook report nudge: a run that tries to finish without having
    delivered its report is blocked once and told to submit it (RESEARCH.md
    §5's definition-of-done, todo #219). The relay prints `hook_output`
    verbatim; null means let the run finish."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - junk in, allow out
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    decision, reason = hookguard.decide_stop(run, token, payload)
    if decision == "block":
        return JSONResponse({"hook_output": {"decision": "block", "reason": reason}})
    return JSONResponse({"hook_output": None})


@app.get("/api/active-run")
async def api_active_run() -> JSONResponse:
    """Polled by every page so the header can show live agent activity."""
    return JSONResponse({**active_run_snapshot(), "usage": usage_snapshot()})


@app.get("/api/run/{run_id}/log")
async def api_run_log(run_id: int, offset: int = 0) -> JSONResponse:
    """Incremental tail of a run's transcript. The caller passes back the
    `offset` from the previous response to get only what is new."""
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    text, new_offset = runlog.read_log(run_id, max(0, offset))
    return JSONResponse(
        {
            "run_id": run_id,
            "status": run["status"],
            "running": run["status"] == "running",
            "text": text,
            "offset": new_offset,
            "events": run["events"] or 0,
        }
    )

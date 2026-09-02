"""Project Portal - FastAPI app entrypoint."""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
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
    claudelogin,
    climemory,
    config,
    crossproject,
    daycycle,
    db,
    filetree,
    fileview,
    headroom,
    hookguard,
    midrun,
    journalwindow,
    jumpkeys,
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
    people,
    portalmcp,
    preview,
    quickreplies,
    quiet,
    quoting,
    revert,
    rundiff,
    runlimit,
    runlog,
    scope,
    sections,
    settings_form,
    sidebar,
    site,
    spawnauth,
    strays,
    subprojects,
    telegram_bot,
    todos,
    transcribe,
    usage,
    verifydepth,
    webpush,
    worker,
)
# Under a name of its own, for the same reason the worker does it: a bare
# `parallel` reads as the portal-wide concurrency cap, not as two agents inside
# one project.
from app import parallel as parallel_runs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO, which would leak the Telegram bot
# token into the journal on every getUpdates poll.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("portal.main")

app = FastAPI(title="Project Portal")


# --------------------------------------------------------------------------
# Which of them is holding the phone
# --------------------------------------------------------------------------
#
# More than one person uses this portal now (see app/people.py). Every page has
# to know which one is reading it - to sign a note, to tick the right box, to
# show the right name in the corner - and every page means every template,
# which rules out a per-route context value: one route that forgot to pass it
# would show the wrong person's name in the masthead and there would be no
# obvious symptom.
#
# So it goes where `body_classes()` already lives: a zero-argument Jinja
# global. Those can read settings, but they cannot read a
# request - hence the ContextVar, set once per request by the middleware below
# and read by `me()` during the render.
_CURRENT_PERSON: contextvars.ContextVar = contextvars.ContextVar(
    "portal_current_person", default=None
)

# Whose LOOK this request is being rendered in, which is a different question
# from whose request it is.
#
# Wes, 2026-07-28: "Also allow users to switch themes and view whatever it
# would look like for another user. that would be a useful and neat feature."
#
# It is deliberately a second variable rather than a temporary swap of the
# first. Previewing her theme must not make a note you post get her name on
# it, must not change which projects count as yours, and must not change what
# the agent is told about who is asking. The only thing it may reach is
# `appearance()`. Keeping the two apart in the type system is what makes that
# true by construction instead of by remembering.
_CURRENT_LOOK: contextvars.ContextVar = contextvars.ContextVar(
    "portal_current_look", default=None
)

# The cookie holding a preview. Separate from people.COOKIE for the same
# reason: one says who you are and lasts ten years, the other says what you
# are looking at and should not outlive the browser.
LOOK_COOKIE = "portal_look"


def _client_ip(request: Request) -> str:
    """The address this request actually arrived from.

    Deliberately NOT X-Forwarded-For. That header is whatever the client typed
    unless a trusted proxy is verified to have set it, and the only thing this
    address is used for is a `tailscale whois` hint - so believing a spoofable
    header would let anyone claim to be anybody, in exchange for nothing.
    """
    client = request.client
    return client.host if client else ""


def resolve_person(request: Request):
    """Who is making this request. See `people.resolve` for the precedence."""
    slug = request.cookies.get(people.COOKIE, "")
    login = ""
    # The whois lookup is skipped entirely while there is one person, which is
    # every install until somebody adds a second: no subprocess, no cache, no
    # cost at all for a feature nobody is using. It is also skipped when the
    # cookie already answers, because the cookie outranks it anyway.
    if not slug:
        try:
            if len(people.everyone()) > 1:
                login = people.tailnet_login_cached(_client_ip(request))
        except Exception:  # pragma: no cover - defensive
            log.debug("Could not take a tailnet reading", exc_info=True)
    return people.resolve(slug, login)


@app.middleware("http")
async def _identify(request: Request, call_next):
    """Resolve the acting person once, for the whole request.

    Never fails a request: identity is a nicety on a home-LAN tool, not an
    authorization decision, and a portal that 500s because a whois call went
    wrong would be a strictly worse portal than the single-person one it
    replaced. The token is reset in `finally` so a worker thread cannot inherit
    a stale person from whichever request happened to run on it last.
    """
    token = look_token = None
    try:
        token = _CURRENT_PERSON.set(resolve_person(request))
    except Exception:  # pragma: no cover - defensive
        log.exception("Could not resolve who is making this request")
    try:
        look_token = _CURRENT_LOOK.set(_resolve_look(request))
    except Exception:  # pragma: no cover - defensive
        log.debug("Could not resolve a look preview; using your own", exc_info=True)
    try:
        return await call_next(request)
    finally:
        if token is not None:
            _CURRENT_PERSON.reset(token)
        if look_token is not None:
            _CURRENT_LOOK.reset(look_token)


def _resolve_look(request: Request):
    """The person whose look this page should be rendered in, or None for your
    own. Never raises and never falls back to anyone: an unreadable preview
    cookie means you see yourself, which is the safe answer."""
    slug = (request.cookies.get(LOOK_COOKIE) or "").strip()
    if not slug:
        return None
    return people.by_slug(slug)


def looking_as():
    """Whose look is on screen, when it is not your own. None the rest of the
    time, which is what the banner and the picker both key off."""
    person = _CURRENT_LOOK.get()
    if person is None:
        return None
    mine = me()
    if mine is not None and int(person["id"]) == int(mine["id"]):
        return None  # previewing yourself is not previewing
    return person


def _person_id(request: Request) -> Optional[int]:
    """The acting person's id, for a row about to be written.

    Resolves from the request rather than reading the ContextVar, because a
    route may be reached in ways the middleware did not wrap (a test client
    calling the function directly, a sub-application), and the id that gets
    stamped onto a note is not a thing to be approximate about.
    """
    try:
        return int(resolve_person(request)["id"])
    except Exception:  # pragma: no cover - defensive
        log.debug("Could not resolve the acting person for a write", exc_info=True)
        return None


def me():
    """The acting person, for a template. Falls back to the owner.

    The fallback matters on the paths with no request behind them at all - a
    template rendered from the worker, an error page - where the owner is both
    the only defensible answer and what the portal did before people existed.
    """
    person = _CURRENT_PERSON.get()
    if person is not None:
        return person
    try:
        return people.owner()
    except Exception:  # pragma: no cover - defensive
        return None


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


def _precompile_templates() -> int:
    """Load every template now, so this process serves the ones it booted with.

    `auto_reload = False` above was believed to give template/code lockstep. It
    does not, and the hole cost Wes a broken page on 2026-07-28: Jinja loads a
    template the first time it is RENDERED, not at import, so a template nobody
    had visited since the last restart was still read fresh off disk hours
    later. An agent editing settings.html at 14:45 therefore had its new markup
    picked up by a process still running the Python from 05:35, the page 500'd
    on a context variable the new template used and the old handler did not
    pass - and because auto_reload is off, that broken compile was then cached
    for good. Wes found it before the tests did, which is the wrong way round.

    Loading them all here closes the window properly: after this, every
    template is in `env.cache`, no later render touches the disk, and a
    template can only change together with the code that feeds it - at a
    restart, which is what the comment above always claimed.

    A template that will not compile is logged and skipped rather than raised.
    It would 500 on its own page either way, and taking the whole portal down
    at boot - including the restart that would fix it - is strictly worse.
    """
    loaded = 0
    for name in templates.env.list_templates():
        try:
            templates.env.get_template(name)
            loaded += 1
        except Exception:  # noqa: BLE001 - one bad template must not stop boot
            log.exception("Template %s could not be compiled at startup", name)
    return loaded


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
        # How runs are paid for. Carries no key - see app/spawnauth.status().
        # On a subscription install this is the ordinary case and the UI says
        # nothing; on an API-key one it is why the usage windows are blank,
        # which otherwise reads as the portal being broken.
        "auth": spawnauth.status(),
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
        # The hold state (app/midrun.py): `paused` is the request, `engaged`
        # whether the run has actually reached the tool call it holds at, and
        # `can_pause` whether this portal process can reach the run at all.
        **midrun.state(int(row["id"])),
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


def _paused_project_ids() -> set[int]:
    """Projects whose live run is on hold (app/midrun.py), so the rail can say
    "paused" instead of "working now" about an agent that is doing nothing."""
    paused = midrun.paused_run_ids()
    if not paused:
        return set()
    return {
        int(r["project_id"]) for r in db.active_runs()
        if r["project_id"] and int(r["id"]) in paused
    }


def side_rail(path: str = "") -> dict:
    """What the desktop side rail shows, for whoever is reading this page.

    A Jinja global rather than per-route context because the rail is in
    base.html and therefore on every page in the portal - threading it through
    forty route handlers would guarantee the one that forgot. It is the same
    shape as `open_question_total` and `restart_pending_runs` beside it.

    Scoped like the dashboard is (`scope.visible_ids`), because the rail
    carries project titles: an unscoped one would announce what everybody else
    is working on from the chrome of every page, which is exactly the leak
    `scope.only_runs` exists to stop.

    Every failure lands on an empty rail. This runs during the render of pages
    that are already reporting something going wrong, and chrome that can 500 a
    page is worse than no chrome.
    """
    try:
        person = me()
        mine = scope.visible_ids(person)
        admin = scope.is_admin(person)
        # The reader's own dashboard order, so the rail and the board rank the
        # same projects the same way. A rail sorted differently from the page it
        # links into is a second opinion nobody asked for.
        #
        # `activity` is read once and used twice: to rank these rows and, below,
        # to draw them. Both halves have to see the same map or the rail's order
        # and its "worked on 2h ago" would answer from different readings.
        activity = db.last_activity_at()
        order = db.get_setting("dashboard_sort") or config.DEFAULT_PROJECT_SORT
        projects = [
            p for p in db.list_projects_sorted(order, activity) if p["id"] in mine
        ]
        runs = sidebar.visible_runs(active_run_snapshot(), mine, admin)
        return sidebar.build(
            projects,
            question_counts=db.open_question_counts(),
            running_ids=db.running_project_ids() & mine,
            paused_ids=_paused_project_ids() & mine,
            gated_ids={p["id"] for p in projects if worker.build_gated(p)},
            # What "recent" actually means: the last run, note or journal entry
            # rather than the last write to the project row. One query for the
            # whole board, because this renders on every page.
            activity=activity,
            runs=runs,
            # Wes, 2026-08-16: usage against the 5-hour and weekly windows, and
            # how close each is to resetting, on the rail. Read from the cached
            # snapshot - `limits.cached` is one settings row and never a network
            # call, which is the rule that lets this run on every page render.
            usage=limits.compact_windows(),
            path=path,
            # The one appearance key that is not a class on <body>: what the
            # rail lists is decided here, on the server, because the rows have
            # to be sorted and cut before they are rendered.
            mode=appearance().get(
                "ui_rail_projects", config.APPEARANCE_DEFAULTS["ui_rail_projects"]
            ),
        )
    except Exception:  # noqa: BLE001
        log.debug("Could not build the side rail; drawing none", exc_info=True)
        return sidebar.empty()


def nav_links(path: str = "") -> list[dict]:
    """The portal's top-level sections, for whoever is reading this page.

    One call, two places: base.html draws the tabs across the top of the
    content AND the rail's copy of them from this list, so they can never come
    to disagree about which section is current or which sections exist. Wes,
    2026-08-15: "I want the dashboard, tasks, etc to show up on the nav bar on
    the side even when in project pages."

    Counts come from the same two functions the tabs have always used, so the
    rail's badge and the tab's badge are the same number by construction rather
    than by coincidence.

    The everyone tab is the admin door into other people's boards: only the
    owner has one, and only once there is somebody else to look at - on a
    one-person install it would open onto the same projects the dashboard
    already shows.
    """
    try:
        person = me()
        return sidebar.nav_rows(
            path,
            questions=open_question_total(),
            tasks=open_oneoff_total(),
            everyone=bool(person and person["is_owner"] and len(people.everyone()) > 1),
        )
    except Exception:  # noqa: BLE001
        log.debug("Could not count the nav badges; drawing bare links", exc_info=True)
        return sidebar.nav_rows(path)


def open_question_total() -> int:
    """Nav badge count. A global rather than a per-route context value so every
    page (including error-adjacent ones) renders the same badge.

    Questions on projects Wes has paused himself, or that are still in the
    backlog, are left out: he asked for the number to mean "things waiting on
    me right now". They are still answerable - they sit in their own section
    below the fold on /questions - and the badge counts exactly what that page
    shows above it.

    Scoped to the reader's own projects, like the page it counts: a badge
    saying 3 that opens onto a list of 1 is worse than no badge at all.

    The rule itself lives in `scope.pending_questions`, because the home-screen
    icon's `app_badge` has to count the same thing for the same person and a
    second copy of the filter would eventually disagree with this one."""
    return len(scope.pending_questions(me()))


def install_appearance() -> dict[str, str]:
    """The install's look: the settings rows, each falling back to its default
    if unset or unrecognized.

    This is what a person who has chosen nothing sees, and what a new person
    starts from.
    """
    out = {}
    for key, choices in config.APPEARANCE_CHOICES.items():
        value = db.get_setting(key) or ""
        allowed = {v for v, _ in choices}
        out[key] = value if value in allowed else config.APPEARANCE_DEFAULTS[key]
    return out


def appearance(person=None) -> dict[str, str]:
    """The CRT layers as the person in front of this request should see them.

    Three tiers, narrowest first: this person's own overrides, then the
    install's settings, then the shipped defaults. Rendered as classes on
    <body> so the whole page can respond without any per-element markup.

    `person` defaults to the acting person, which is what every template
    render wants. It is an argument at all so the settings page can render
    somebody's panel without pretending to be them.

    A person with no overrides gets exactly `install_appearance()`, byte for
    byte - so a one-person portal, and every page of it, is unchanged by this
    feature existing.
    """
    look = install_appearance()
    try:
        if person is None:
            # A preview, if one is running, and only here. `looking_as` is the
            # single place this override may be read - see _CURRENT_LOOK.
            person = looking_as() or me()
        look.update(people.appearance_of(person))
    except Exception:  # pragma: no cover - defensive
        log.debug("Could not read a personal appearance; using the install's", exc_info=True)
    return look


def body_classes() -> str:
    look = appearance()
    classes = [
        f"{prefix}-{look[key]}"
        for key, prefix in config.APPEARANCE_CLASS_PREFIX.items()
        if key in look
    ]
    # The stock rides along beside the theme name, so every light theme picks
    # up the same structural undoing (no scanlines, no glow, none of the
    # terminal's borrowed punctuation) without each one restating it. Only the
    # light stock gets a class: dark is the shipped look and has no rules.
    if theme_stock() == "light":
        classes.append("theme-stock-light")
    # And the type, for the same reason and independently of it: a theme whose
    # chrome is proportional re-faces the window title, the tabs, the badges,
    # the buttons and the form fields, and that list is written once rather
    # than per theme. Only `prose` gets a class - `mono` is the shipped voice.
    if theme_type() == "prose":
        classes.append("theme-type-prose")
    return " ".join(classes)


def section_order(person=None) -> list[str]:
    """The order the project page's movable blocks render in, for this person.

    Same three tiers as `appearance()` and read out of the same blob, so
    previewing somebody else's look also shows you their arrangement - which is
    the point of the preview: "see what you are asking them to look at".

    Always the full set of names, whatever is stored (see sections.order), so
    the template can dispatch on it without a guard and a truncated preference
    can never take a block off the page.
    """
    stored = ""
    try:
        if person is None:
            stored = appearance().get(sections.SETTING_KEY, "")
        else:
            stored = people.appearance_of(person).get(sections.SETTING_KEY, "")
    except Exception:  # pragma: no cover - defensive
        log.debug("Could not read a personal section order; using the default", exc_info=True)
    return sections.order(stored)


def theme() -> str:
    """The theme name on screen right now, for the two things that have to be
    decided before any CSS is read (see base.html)."""
    return appearance().get("ui_theme", config.APPEARANCE_DEFAULTS["ui_theme"])


def theme_stock() -> str:
    """"light" or "dark" for the theme on screen.

    Read by `body_classes` for the family class, and by base.html for the
    `color-scheme` meta - which is what paints the scrollbars, the checkbox
    glyphs and the native select popup, none of which any stylesheet reaches.
    Getting it from a table beats the `!= 'paper'` test this replaced: that
    test made every future light theme grow black scrollbars, silently, and
    the one place the bug would not show is a screenshot of the page body.
    """
    return config.THEME_STOCK.get(theme(), config.DEFAULT_THEME_STOCK)


def theme_type() -> str:
    """"mono" or "prose" for the theme on screen.

    Separate from the stock because the two questions genuinely are: workbench
    and blueprint are dark themes with nothing monospaced in them, which the
    stock alone could not express. A theme missing from the table falls to
    `mono`, which is the shipped look and therefore the safe default - the
    failure mode is a theme that renders in Fira Code, not one that renders
    with no font at all.
    """
    return config.THEME_TYPE.get(theme(), config.DEFAULT_THEME_TYPE)


def theme_chrome() -> str:
    """The browser-chrome color for the theme on screen: the iOS status bar
    tint and the overscroll fill."""
    return config.THEME_CHROME.get(theme(), config.THEME_CHROME["terminal"])


def jump_keys_json() -> str:
    """The configured `key -> section` map, for `<body data-jump-keys>`.

    Rendered into every page rather than fetched, because app.js has to know
    the bindings before the very first keystroke - a page that answered N only
    after a round trip would drop the keypress you made while it was reading
    the page. Falls back to the shipped defaults if the settings read fails, so
    a database hiccup costs the keys their configuration, not their existence.
    """
    try:
        return jumpkeys.bindings_json(db.get_all_settings())
    except Exception:  # noqa: BLE001
        return jumpkeys.bindings_json()


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


def themed_icon_name(name: str, theme_name: str) -> str:
    """`favicon-32.png` + `paper` -> `favicon-32-paper.png`, and `favicon.svg`
    -> `favicon-paper.svg`.

    The same rule deploy/make_icons.py names its output by, restated here
    rather than shared because the generator imports Pillow and must never be
    on the serving path. `tests/test_app_icons.py` pins the two against each
    other, which is the only thing making a restatement safe.
    """
    stem, _, ext = name.rpartition(".")
    return f"{stem}-{theme_name}.{ext}"


def favicon_url(name: str) -> str:
    """The tab icon in the reader's own theme, falling back to the shipped one.

    The mark is a tile and two rings and every theme names all three colors in
    its palette, so `deploy/make_icons.py` draws a set per theme - a paper tab
    gets the printed blue on cream, not the CRT's on near-black. The existence
    check is what keeps that honest in both directions: a theme added to
    config before the generator is re-run serves the terminal mark rather than
    a 404, and a 404 here is not a fallback but a blank tab, because a browser
    that has been offered an icon and failed to fetch it does not go looking
    for another one.

    (The home-screen icon cannot work this way and never will: iOS reads
    `apple-touch-icon` once, at Add-to-Home-Screen time, so it could only ever
    freeze whatever theme was on that day. This is the half of todo #664 that
    is actually reachable.)
    """
    return favicon_url_for(theme(), name)


def favicon_url_for(theme_name: str, name: str = "favicon-32.png") -> str:
    """`favicon_url` for a named theme rather than for the reader's own."""
    themed = themed_icon_name(name, theme_name)
    if (config.BASE_DIR / "app" / "static" / themed).exists():
        return icon_url(themed)
    return icon_url(name)


# The tab icons that follow the reader's theme, keyed the way base.html tags
# each <link data-icon-base>. favicon.ico is not among them - see favicon_url.
THEMED_ICONS = ("favicon-32.png", "favicon-16.png", "favicon.svg")


def theme_favicons() -> dict[str, dict[str, str]]:
    """theme name -> {shipped file name: that theme's URL}, for the settings
    page's live preview.

    Built server-side for the same reason the URL is: app.js must not be able
    to compose the name of a file that is not there, and the fallback lives in
    exactly one place.
    """
    return {
        name: {icon: favicon_url_for(name, icon) for icon in THEMED_ICONS}
        for name, _label in config.APPEARANCE_CHOICES["ui_theme"]
    }


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
templates.env.globals["side_rail"] = side_rail
# The point at which a Claude window stops being background information. One
# definition, so the dashboard's limit chips and the side rail's percentages
# turn the same color at the same moment.
templates.env.globals["limit_hot_at"] = limits.HOT_PERCENT
templates.env.globals["nav_links"] = nav_links
templates.env.globals["restart_pending_runs"] = worker.restart_pending_runs
templates.env.globals["body_classes"] = body_classes
templates.env.globals["theme"] = theme
templates.env.globals["theme_stock"] = theme_stock
# Read by _banner.html, which is the one place a theme's voice reaches CONTENT
# rather than styling: the block-drawing masthead is glyphs, and no stylesheet
# may empty those - so the branch lives in the template.
templates.env.globals["theme_type"] = theme_type
templates.env.globals["theme_chrome"] = theme_chrome
templates.env.globals["section_order"] = section_order
templates.env.globals["SECTIONS"] = sections.SECTIONS
templates.env.globals["SECTION_SETTING"] = sections.SETTING_KEY
templates.env.globals["looking_as"] = looking_as
templates.env.globals["static_url"] = static_url
templates.env.globals["icon_url"] = icon_url
templates.env.globals["favicon_url"] = favicon_url
templates.env.globals["theme_favicons"] = theme_favicons
templates.env.globals["APPEARANCE_CHOICES"] = config.APPEARANCE_CHOICES
templates.env.globals["APPEARANCE_DEFAULTS"] = config.APPEARANCE_DEFAULTS
# The body-class prefix per appearance key, so the settings page can tell app.js
# which class to swap for a live preview without a second copy of the table.
templates.env.globals["APPEARANCE_CLASS_PREFIX"] = config.APPEARANCE_CLASS_PREFIX
templates.env.globals["THEME_CHROME"] = config.THEME_CHROME
templates.env.globals["THEME_STOCK"] = config.THEME_STOCK
templates.env.globals["THEME_TYPE"] = config.THEME_TYPE
templates.env.globals["status_choices"] = config.status_choices
templates.env.globals["display_state"] = db.display_state
def byline(entry) -> str:
    """What to print on a journal row's author badge.

    The generic `user` is right on a one-person install and useless on a shared
    one: two people writing notes into the same journal both show up as "user",
    so the timeline cannot say who asked for what. When the portal recorded a
    person and there is more than one it could be, the badge carries the name.

    Falls back to the bare author rather than to the owner's name - see
    `people.known_name`. An old note written before person stamping existed is
    honestly anonymous, and printing the owner over it would be a guess that
    reads exactly like a fact.
    """
    try:
        author = entry["author"]
        if not people.more_than_one():
            return author
        return people.known_name(entry["person_id"]) or author
    except (IndexError, KeyError, TypeError):  # pragma: no cover - defensive
        return ""


templates.env.globals["jump_keys_json"] = jump_keys_json
# Who is reading this page, and everybody who could be. Globals rather than
# per-route context for the same reason `body_classes` is: the acting person
# appears in the masthead of every page and in the member boxes on two more,
# and a route that forgot to pass it would show somebody the wrong name.
def todo_head_for(person, style: str = "for") -> str:
    """The heading over one person's half of a todo list.

    "For you" when it is the person reading the page - which is what it has
    always said, and what it still says throughout a one-person install - and
    their name when it is somebody else. Second person for yourself and third
    for everyone else is how a shared list reads out loud.

    `style="possessive"` is the completed-history page's phrasing, which sits
    beside "The agent's" and would read oddly as "For you".
    """
    if person is None:
        return "Nobody in particular" if style == "possessive" else "For somebody"
    mine = me()
    if mine is not None and int(person["id"]) == int(mine["id"]):
        return "Yours" if style == "possessive" else "For you"
    name = people.name_of(person)
    return people.possessive(name) if style == "possessive" else f"For {name}"


templates.env.globals["me"] = me
templates.env.globals["todo_head_for"] = todo_head_for
templates.env.globals["everyone"] = people.everyone
templates.env.globals["project_members"] = people.members
templates.env.globals["refile_choices"] = todos.refile_choices
templates.env.globals["refile_value"] = todos.refile_value
templates.env.globals["person_pronouns"] = people.pronouns_of
templates.env.globals["byline"] = byline
# No is_side_thread global: no template asks any more. The side thread is drawn
# as its own conversation in the ask box (db.ask_thread) rather than badged
# where it sat in the journal, and the journal excludes it in SQL. The predicate
# itself stays - db.SIDE_THREAD is still what both exclusions are built from.
templates.env.globals["summary_bullet"] = db.summary_bullet
templates.env.filters["status_badge"] = config.status_badge
templates.env.globals["META_PROJECT_SLUG"] = config.META_PROJECT_SLUG
templates.env.globals["MODEL_CHOICES"] = config.MODEL_CHOICES
templates.env.globals["VERIFY_DEPTH_CHOICES"] = verifydepth.DEPTH_LABELS
templates.env.globals["media_kind"] = attachments.media_kind
templates.env.globals["note_pending"] = notes.is_pending
# Question numbering rides with the Telegram bot and nothing else - see
# db.telegram_enabled. Registered as the function, not its value, so ticking
# the box in Settings takes effect on the next render rather than at a restart.
templates.env.globals["question_numbers"] = db.telegram_enabled
# The one-tap answers an agent offered with a question ("merge it" / "keep
# both"). Stored at creation time on the question row, so what is shown is
# what was offered - see quickreplies.
templates.env.globals["question_options"] = lambda q: quickreplies.decode(q["quick_options"])
# Whether an agent is, right now, holding still for this question's answer
# (app/portalmcp.py). It changes what answering is worth: reply inside the next
# minute or two and the running agent acts on it; reply later and it is read by
# whatever run comes next. Live rather than stored, because so is the fact.
templates.env.globals["question_waiting"] = portalmcp.waiting_run
templates.env.globals["max_upload"] = attachments.human_size(attachments.MAX_UPLOAD_BYTES)
templates.env.globals["max_upload_bytes"] = attachments.MAX_UPLOAD_BYTES
templates.env.filters["filesize"] = attachments.human_size
templates.env.globals["DEFAULT_MODEL"] = config.DEFAULT_MODEL
# Cost is rendered through these rather than an inline '$%.3f' so the
# weight-vs-dollars choice lives in exactly one place.
templates.env.filters["cost"] = usage.format_cost
templates.env.globals["cost_noun"] = usage.cost_noun
templates.env.globals["COST_UNIT_CHOICES"] = usage.COST_UNIT_CHOICES

# Last, deliberately: a template may reference a filter or a global at compile
# time, so everything a template can name has to be registered above this line.
_TEMPLATES_LOADED = _precompile_templates()


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

_BACKGROUND_TASKS: list[asyncio.Task] = []


@app.on_event("startup")
async def on_startup() -> None:
    db.init_db()
    # `deploy/setup.py` and `deploy/update.py` boot this app on a scratch port
    # purely to prove it answers, and everything below is wrong for that. Three
    # separate ways wrong, each of which has actually happened or was one live
    # install away from happening:
    #
    # - The worker loop schedules runs. On the empty board of a fresh clone its
    #   first tick goes straight to the daily reflect, so `setup.py` on a new
    #   machine spawns a real, billed `claude -p` within seconds of finishing.
    #   That cost about ten seconds of Wes's allowance on 2026-08-30, from a
    #   throwaway server that was never meant to do anything but say `pong`.
    # - `reconcile_orphaned_runs_on_boot` settles every run it finds in flight
    #   as an orphan. Against a live data directory - which is exactly what
    #   `update.py` would be pointed at - that is the *running* service's runs,
    #   killed on the books by a health check.
    # - `preview.serve_loop` binds a fixed port, so the smoke test collides
    #   with the portal it is checking rather than staying on its scratch port.
    #
    # So the flag is not "no worker", it is "this process is not the service":
    # take no action that belongs to a real service start.
    smoke = os.environ.get("PORTAL_SMOKE_TEST", "") == "1"
    # Only here, and only because this really is the service starting: adopting
    # or burying a run that a previous process left behind is a conclusion only
    # a booting portal is entitled to draw. See db.reconcile_orphaned_runs_on_boot.
    if not smoke:
        db.reconcile_orphaned_runs_on_boot()
    # Say the configuration problems out loud. Both of these otherwise present
    # only as something quietly not working - every printed link dead on the
    # phone that reads it, or every run failing inside the CLI with an auth
    # error - and the log at boot is where somebody standing up a fresh clone
    # is actually looking.
    for warning in site.warnings() + spawnauth.problems():
        log.warning("%s", warning)
    # The memory files on disk at boot are copied into the history if they are
    # not there already, so a version always survives whatever the next agent
    # does to them. Cheap and idempotent - identical content is not re-stored.
    try:
        memory.snapshot_all()
    except Exception:  # noqa: BLE001 - never let a backup stop the app booting
        log.exception("Could not snapshot the memory files at startup")
    _BACKGROUND_TASKS.clear()
    if not smoke:
        _BACKGROUND_TASKS.append(asyncio.create_task(worker.worker_loop()))
        _BACKGROUND_TASKS.append(asyncio.create_task(telegram_bot.telegram_poll_loop()))
        _BACKGROUND_TASKS.append(asyncio.create_task(limits.poll_loop()))
        _BACKGROUND_TASKS.append(asyncio.create_task(netinfo.poll_loop()))
        # The preview server shares this loop, so it starts and dies with the
        # portal and needs no unit of its own. See app/preview.py.
        _BACKGROUND_TASKS.append(asyncio.create_task(preview.serve_loop()))
    # Voice memos uploaded before transcription existed (or while its Docker
    # image was missing) get their text now. Serial, on its own daemon thread,
    # off the event loop.
    try:
        transcribe.backfill()
    except Exception:  # noqa: BLE001 - a transcription sweep must not stop boot
        log.exception("Transcript backfill could not start")
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
    # without needing a settings trip. An unknown name is ignored entirely, and
    # a stored preference for a sort this install no longer offers - `priority`,
    # on any database seeded before 2026-08-16 - falls back to the default
    # rather than reaching SQLite or ranking by nothing.
    sorts = config.PROJECT_SORTS
    if sort in sorts:
        db.set_setting("dashboard_sort", sort)
    active_sort = sort if sort in sorts else (
        db.get_setting("dashboard_sort") or config.DEFAULT_PROJECT_SORT
    )
    if active_sort not in sorts:
        active_sort = config.DEFAULT_PROJECT_SORT
    # Your board, not the install's. Wes, 2026-07-28: "I only want users to see
    # projects they are included on." He is filtered like everybody else - the
    # admin view is /everyone, deliberately a page he goes to rather than
    # anything that reaches back into this feed. See app/scope.py.
    mine = scope.visible_ids(me())
    # Wes, 2026-08-16: "within project statuses on the dashboard, I want to sort
    # by most recently modified similar to how the left nav bar is done." The
    # shelves below keep the order this list arrives in, so ranking it once here
    # is what puts every shelf in recency order - one sort, four shelves, and no
    # way for one of them to be left ranking by something else.
    #
    # `activity` is what makes "modified" mean what he means by it: a run, a
    # note, an agent entry, a report. `projects.updated_at` alone moves only
    # when the project's own row is written, which is why the rail was wrong
    # about this until 2026-08-07. See `db.last_activity_at`.
    activity = db.last_activity_at()
    projects = [
        p for p in db.list_projects_sorted(active_sort, activity) if p["id"] in mine
    ]
    done = [p for p in projects if p["stage"] in config.DONE_STAGES]
    question_counts = db.open_question_counts()

    # Four shelves rather than one wall of cards, in the order Wes asked for:
    # active work on top, review under it, then the put-down shelves. A project
    # with an agent actually working on it is always on the top shelf whatever
    # its stored stage says - work in flight outranks everything else.
    active_run = scope.only_runs(active_run_snapshot(), mine, scope.is_admin(me()))
    running_ids = set(active_run["project_ids"])
    shelves: dict[str, list] = {"active": [], "review": [], "paused": [], "backlog": []}
    for p in projects:
        if p["stage"] in config.DONE_STAGES:
            continue
        # `shelf_of` rather than the rule spelled out here: the side rail lists
        # the same two shelves from the same rows, and the "a run in flight
        # outranks the stored stage" part used to live only in this loop.
        shelf = db.shelf_of(
            p, question_counts.get(p["id"], 0), p["id"] in running_ids
        )
        shelves[shelf].append(p)
    # Each shelf is re-ordered so a sub-project sits directly under the project
    # it was split out of, indented and labeled. Children stay real cards -
    # hiding them inside the parent would make a game an agent is actively
    # building on invisible from the one page Wes checks.
    for name, rows in list(shelves.items()):
        shelves[name] = subprojects.group_for_shelf(rows)

    shelved = db.shelved_project_ids() - running_ids
    open_qs = [
        q for q in db.open_questions()
        if q["project_id"] not in shelved and q["project_id"] in mine
    ]
    # Recent activity is NOT read here any more. The dashboard renders the fold
    # shut and empty and app.js fetches /activity/feed the first time it opens,
    # so the 35 KB of agent markdown that feed carries is off the critical path
    # of the page Wes opens most. See the fold in index.html.
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
            "usage": usage_now,
            "worker_enabled": usage_now["worker_enabled"],
            "question_counts": question_counts,
            "run_counts": db.runs_today_by_project(),
            "heatmap": usage.heatmap(),
            "active_run": active_run,
            "worker_model": settings.get("worker_model") or config.DEFAULT_MODEL,
            "sorts": sorts,
            "active_sort": active_sort,
            # The timestamp each card SAYS, so it agrees with the order the
            # cards are in. Showing `updated_at` under a recency sort would put
            # "5 days ago" at the top of the shelf above "2 hours ago" whenever
            # the newest thing was a run or a note rather than a write to the
            # project row - which is most of the time, and reads as the sort
            # being broken rather than as the label answering a different
            # question. Same value the sort ranked on; see `db.worked_on_at`.
            "worked_on": {
                p["id"]: db.worked_on_at(p, activity) for p in projects
            },
            # Which cards carry the "needs your OK" badge. A set of ids rather
            # than a per-card call so the gate is evaluated once per render.
            "awaiting_approval": {
                p["id"] for p in projects if worker.build_gated(p)
            },
        },
    )


@app.post("/ideas")
async def create_idea(
    request: Request,
    title: str = Form(""), idea: str = Form(...), then: str = Form("")
) -> RedirectResponse:
    """Two buttons on the idea form (Wes's ask): plain "add idea" parks it in
    the backlog and no model ever sees it until he says so; "add and start
    planning" makes it active (still unapproved for code) and puts an agent on
    it right now."""
    # A title Wes typed in the box is his, and locks. One he left blank is cut
    # from the first line of the idea, which is a placeholder rather than a
    # name, so that one stays open for an agent to improve. Wes, 2026-07-29:
    # "if a title is defined, do not change it. If you want to suggest
    # alternative titles, feel free to, but do not change a title that the user
    # set themselves." (Suggesting still works - see db.propose_title.)
    typed = bool(title.strip())
    title = title.strip() or idea.strip().split("\n", 1)[0][:80] or "Untitled idea"
    stage = "active" if then == "plan" else "backlog"
    # The project belongs to whoever is filling in the form - Karli's idea goes
    # on Karli's board, not the owner's (Wes, 2026-08-06). create_project only
    # falls back to the owner when nobody can be resolved at all.
    person_id = _person_id(request)
    project = db.create_project(
        title=title, description=idea.strip(), kind="unknown", stage=stage,
        title_locked=1 if typed else 0, person_id=person_id,
    )
    if idea.strip():
        db.add_journal(project["id"], "user", "note", idea.strip(), person_id=person_id)
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
    # The ask side thread is drawn in the ask box at the top of the page rather
    # than in this feed. Wes, 2026-08-16: "I also want the questions to be asked
    # and answered all up at the 'Ask' area instead of in line in the journal."
    # Excluded in SQL, so a chatty thread does not eat slots in the 200 entries
    # of journal this page holds.
    #
    # Fetched in full but rendered through a window (app/journalwindow.py): the
    # rows are cheap, and what actually cost this page four fifths of its 596 KB
    # was rendering two hundred entries of markdown into it on every load.
    # `?journal=all` asks for the rest, and survives a live patch by itself
    # because liveRefreshNow() re-fetches location.href, query string and all.
    journal_all = request.query_params.get("journal") == "all"
    journal_rows = db.list_journal(project["id"], limit=200, exclude=db.SIDE_THREAD)
    journal, journal_hidden = journalwindow.window(journal_rows, show_all=journal_all)
    ask_rows = db.ask_thread(project["id"])
    ask_pending = ask.pending(project["id"])
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
            "journal_hidden": journal_hidden,
            "journal_all": journal_all,
            "questions": questions,
            "dismissed_questions": db.dismissed_questions(project["id"]),
            "deleted_questions": db.deleted_questions(project["id"]),
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
            # What the runs/day control is actually enforcing right now: the
            # project's own number, or the board default it inherits, or 0 for
            # no cap. The control shows the choice; this shows its effect.
            "project_cap": worker.effective_project_cap(project),
            "default_project_cap": db.default_project_max_runs(),
            "usage": usage_snapshot(),
            "attachments": db.list_attachments(project["id"]),
            "journal_attachments": db.attachments_by_journal(project["id"]),
            "ssh_command": config.ssh_command(slug),
            "build_gated": worker.build_gated(project),
            # Whether the green "add note" button will start a run on its own,
            # which is also what decides if "add & run now" is worth rendering.
            "note_runs_now": worker.can_run_now(project),
            # How many agents are inside this project this second. Non-zero is
            # what puts the "parallel run" button on the note form, since a
            # second agent is only a meaningful request while a first one is
            # working. See app/parallel.py.
            "agents_running": len(db.running_runs_for_project(project["id"])),
            # Whether a note typed now reaches the agent already working, at
            # its next tool call (app/midrun.py) - only while one is, and only
            # if this portal process can reach it.
            "agent_hears": any(
                midrun.enabled() and midrun.can_hear(int(r["id"]))
                for r in db.running_runs_for_project(project["id"])
            ),
            "research_queued": db.is_research_queued(project),
            # The model a burst would actually use, setting override included.
            "research_model": agent_runner.resolve_model(None, "research"),
            "spending_down": pacing.spending_down(),
            "ask_pending": ask_pending,
            "ask_thread": ask_rows,
            "ask_open": ask.opens(ask_rows, ask_pending),
            "agent_todos": db.visible_todos(project["id"], owner="agent"),
            "user_todos": db.visible_todos(project["id"], owner="user"),
            # The same grouping the run prompt uses, so the page and the agent
            # never disagree about whose an item is. One group on a one-person
            # install, which renders exactly the single "For you" heading it
            # always did.
            "user_todo_groups": todos.by_person(
                db.visible_todos(project["id"], owner="user"), project["id"]
            ),
            # How many ticked-off items have already dropped off the live list.
            # Only shown as a link to the history, so clearing never feels like
            # deleting.
            "hidden_done_todos": db.count_hidden_done_todos(project["id"]),
            "clearable_todos": db.count_clearable_todos(project["id"]),
            "suggested_slug": db.suggested_slug(project),
            "title_suggestion": db.title_suggestion(project),
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
            **_related_context(project),
        },
    )


def _related_context(project) -> dict:
    """What the "Related projects" fold needs: the declared links, and the
    projects that could still become one.

    Candidates are `crossproject.readable()` minus what is already linked minus
    the family - the same list a run on this project can actually reach, so the
    dropdown cannot offer a link that would name nothing. Empty when
    cross-project reading is switched off, which is also when the whole fold
    stops being drawn.
    """
    pid = int(project["id"])
    if not crossproject.enabled():
        return {"linked_projects": [], "link_candidates": [], "cross_project_on": False}
    linked = db.linked_project_ids(pid)
    kin = crossproject.family_ids(project)
    readable = crossproject.readable(pid)
    return {
        # Not crossproject.declared(): this is the page where a link is
        # MANAGED, so one that has become family must still be listed with its
        # remove button rather than vanishing from the only place it can be
        # taken off again.
        #
        # Still readable-filtered, though, and that is the one case where the
        # link does vanish from this end: membership can change after a link is
        # made, and drawing a row for a project this page's viewer is no longer
        # on would name somebody else's work on a page they can open. It stays
        # removable from the other end, where they are a member.
        "linked_projects": [r for r in readable if int(r["id"]) in linked],
        "link_candidates": sorted(
            (
                r
                for r in readable
                if int(r["id"]) not in linked and int(r["id"]) not in kin
            ),
            key=lambda r: str(r["title"] or "").lower(),
        ),
        "cross_project_on": True,
    }


@app.post("/project/{slug}/run-cap")
async def update_run_cap(slug: str, max_runs_per_day: str = Form("")) -> RedirectResponse:
    """Per-project daily run cap.

    Empty means "inherit the board-wide default"; 0 means "no cap on this
    project at all". They used to be the same thing - 0 was folded to NULL -
    which left no way to say "let this one project run as much as it likes",
    and that is exactly what Wes asked for on 2026-08-13 ("I don't see where I
    can increase daily limits on runs of single projects"). See
    worker.effective_project_cap.
    """
    project = _get_project_or_404(slug)
    raw = max_runs_per_day.strip()
    if not raw:
        cap: Optional[int] = None
    else:
        try:
            cap = max(0, int(raw))
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
        if not db.same_title(title, project["title"]):
            db.reject_title_suggestion(project)

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
    # He has just decided the name himself, so an agent's pending offer of a
    # different one is answered - by being turned down, which also stops that
    # same wording coming back on the next run.
    db.reject_title_suggestion(project)
    db.update_project(project["id"], title=title, title_locked=1)
    db.add_journal(project["id"], "user", "status", f"Renamed `{was}` -> `{title}`.")
    return RedirectResponse(url=f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/title-suggestion")
async def answer_title_suggestion(slug: str, action: str = Form("apply")) -> RedirectResponse:
    """Take, or turn down, the alternative title an agent proposed.

    The offer only exists because the title is locked. Wes's rule is that a
    title he set himself is never overwritten - but he also said "if you want to
    suggest alternative titles, feel free to", so the proposal has to land
    somewhere he can act on in one press rather than being thrown away. Turning
    it down remembers the wording, so the next run cannot propose it again.
    """
    project = _get_project_or_404(slug)
    if action == "apply":
        db.accept_title_suggestion(project)
    else:
        db.reject_title_suggestion(project)
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
    thing, and the next thing he wants is almost always to describe it or hand
    it a note, both of which are on the child's own page.
    """
    project = _get_project_or_404(slug)
    if not title.strip():
        return RedirectResponse(url=f"/project/{slug}#subprojects", status_code=303)
    try:
        child = subprojects.create_child(project, title, description)
    except subprojects.SplitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Wes typed this name into the box himself, so it locks - the same rule as
    # a title typed on the idea form or into the heading. A child named by an
    # agent report (subprojects.add) is left open, because nobody has chosen it.
    db.update_project(child["id"], title_locked=1)
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


@app.post("/project/{slug}/link")
async def link_project(slug: str, other_id: int = Form(...)) -> RedirectResponse:
    """Declare another project related to this one.

    For the pairs `crossproject`'s slug heuristic cannot see - `commander-case`
    depends on `3d-vectorizer` and the two names share nothing. The link is
    unordered, so this also tells runs on the other project about this one.
    """
    project = _get_project_or_404(slug)
    other = db.get_project(other_id)
    if other is None:
        raise HTTPException(status_code=404, detail="No such project to link")
    # Only a project this one's runs may actually read: a link that named a
    # project the principal is not a member of would point at nothing, and the
    # dropdown that offers them is built from the same list.
    if other_id not in {int(r["id"]) for r in crossproject.readable(project["id"])}:
        raise HTTPException(status_code=400, detail="That project cannot be linked here")
    db.link_projects(project["id"], other_id)
    return RedirectResponse(url=f"/project/{slug}#related", status_code=303)


@app.post("/project/{slug}/unlink")
async def unlink_project(slug: str, other_id: int = Form(...)) -> RedirectResponse:
    """Undeclare a link, from either end of it."""
    project = _get_project_or_404(slug)
    db.unlink_projects(project["id"], other_id)
    return RedirectResponse(url=f"/project/{slug}#related", status_code=303)


async def _start_parallel(project) -> None:
    """Start a parallel run and, when it is refused, say so where Wes will see
    it. A press that produced nothing and explained nothing is the failure he
    reports most often - so the refusal goes in the journal, which is on the
    page he lands back on."""
    started, why = await worker.start_parallel_run(project)
    if not started and why:
        db.add_journal(project["id"], "system", "status", why)


@app.post("/project/{slug}/note")
async def add_note(
    request: Request,
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
        elif then == "parallel":
            await _start_parallel(project)
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
        # The block is built in attachments.py because that is also where it is
        # taken apart again when a file is removed before the prompt goes.
        if stored:
            body = f"{quoted}\n\n{attachments.listing_block(stored)}".strip()
    if errors:
        body = f"{body}\n\n*Rejected: {'; '.join(errors)}*".strip()

    # Every note waits for the agent's next run unless it was pressed as
    # "deliver mid-run", which hands it to the agent already working at its
    # next tool call (app/midrun.py). Wes, 2026-09-02: "don't deliver the
    # queued notes by default, but have an option on each one" - so the
    # channel is opt-in per note, and the journal entry offers the same switch
    # afterwards (the /hear route below).
    journal_id = db.add_journal(
        project["id"], "user", "note", body, person_id=_person_id(request),
        hear_now=(then == "hear"),
    )
    for a in stored:
        db.set_attachment_journal(a["id"], journal_id)

    # A voice memo gets transcribed before any run this note triggers, so the
    # run's prompt carries the words rather than "(transcription is still
    # running)" - the memo IS the instruction. transcribe.kick returns
    # immediately (the work happens on the loop after this response) and runs
    # the continuation whether or not transcription succeeds.
    audio_ids = [a["id"] for a in stored if transcribe.wants(a["mime"])]

    # "add note and run" - the note, the switch to active and the run in one
    # press, because typing an instruction and then wanting it acted on now is
    # the common case and it was three controls in three different places.
    # Ordering matters: the note is already in the journal above, so the run
    # queued here cannot start without it.
    if then == "run":
        if db.display_state(project) != "active":
            db.set_user_state(project, "active")
        if audio_ids:
            transcribe.kick(audio_ids, after=worker.queue_manual_run(project["id"]))
        else:
            await worker.queue_manual_run(project["id"])
    elif then == "parallel":
        # A second agent on this note NOW, beside the one already working, in a
        # worktree of its own. Unlike every other button here it starts the run
        # inline rather than queueing it: the manual queue exists to hold a
        # request until the project is free, which is the opposite of the ask.
        if audio_ids:
            transcribe.kick(audio_ids, after=_start_parallel(project))
        else:
            await _start_parallel(project)
    elif then != "queue":
        # The plain green "add note": wake a put-down project (Wes's rule) and
        # start a run whenever one could start at all - see worker.note_arrived.
        # "queue note" is the explicit opt-out: the note is stored for whenever
        # the agent next runs, and nothing else is touched. "deliver mid-run"
        # takes this same path on purpose: while the agent it was meant for is
        # working nothing here fires (the workspace is busy), and if that agent
        # finished in the meantime the note wakes the project exactly as a
        # plain one would, instead of sitting unread behind a review badge.
        if audio_ids:
            transcribe.kick(audio_ids, after=worker.note_arrived(project))
        else:
            await worker.note_arrived(project)
    elif audio_ids:
        transcribe.kick(audio_ids)
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


@app.post("/project/{slug}/note/{note_id}/hear")
async def hear_note(slug: str, note_id: int, hear: str = Form("1")) -> RedirectResponse:
    """Hand a note no agent has read yet to the agent already working - or
    take it back (`hear=0`) so it waits for the next run again.

    Wes, 2026-09-02: the switch "can be manually clicked from the journal
    entry". Same single guarded UPDATE as editing: a note that went into a
    prompt between the page render and this press is already read, and the
    route refuses to flag it rather than deliver it a second time mid-run.
    """
    project = _get_project_or_404(slug)
    entry = db.get_journal(note_id)
    if entry is None or entry["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="No such note on this project")
    on = hear.strip().lower() not in ("0", "off", "false", "no", "")
    if not db.set_note_hear_now(note_id, on):
        raise HTTPException(status_code=409, detail="An agent has already read this note")
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
    # Back to the ask box, not the top of the page: the question and its answer
    # both appear there now (Wes, 2026-08-16), and a redirect that landed above
    # them would be the same hunt his note is about.
    return RedirectResponse(url=f"/project/{slug}#ask", status_code=303)


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


def _unname_attachment(journal_id: int, stored_name: str) -> None:
    """Take a deleted file back out of the note that carried it.

    Wes, 2026-08-17: "Add a way of removing note file attachments before the
    prompt is sent." Deleting the row and the bytes is only half of that. The
    note's own markdown lists the file (see `add_note`), and that markdown is
    what an agent is handed as its instructions - so a note left naming a file
    that is no longer on disk tells the agent to go read a missing path, which
    is exactly the confusion the staging directory was built to prevent.

    Both writes are guarded in SQL on `delivered_at IS NULL`, so this cannot
    rewrite a note an agent has already acted on: after delivery the file may
    still be deleted from the Files shelf, but the sentence that was sent stays
    as it was sent. A body that strips to nothing was a files-only note, and it
    goes with its last file - the same rule as clearing the box in `edit_note`.

    There is deliberately no "did anything change?" early return here. It was
    written and the sweep proved nothing could observe it: `strip_from_note`
    hands back the body it was given when the file is not listed, so the write
    it would have skipped stores the text that is already there.
    """
    entry = db.get_journal(journal_id)
    if entry is None:
        return
    body = attachments.strip_from_note(entry["content_md"] or "", stored_name)
    if body:
        db.update_journal_content(journal_id, body)
    else:
        db.delete_journal_note(journal_id)


@app.post("/attachment/{attachment_id}/delete")
async def delete_attachment_route(attachment_id: int) -> RedirectResponse:
    row = db.get_attachment(attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    slug = row["project_slug"] or ""
    if row["stored_name"] and slug:
        attachments.remove_file(slug, row["stored_name"])
    db.delete_attachment(attachment_id)
    if row["journal_id"] and row["stored_name"]:
        _unname_attachment(int(row["journal_id"]), row["stored_name"])
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

    # Always, whichever box was ticked: a parallel checkout is not inside the
    # workspace, so deleting the workspace strands it, and keeping the
    # workspace strands it just as thoroughly - the drain tick lists these
    # directories and then skips any slug the database no longer knows, so
    # nothing would ever clear them.
    #
    # Before _remove_workspace only so that git gets to remove its own
    # worktree while the repo it belongs to is still there. It is tidiness,
    # not correctness: _remove_worktree falls back to rmtree, and a sweep
    # confirmed the checkout goes either way round.
    parallel_runs.discard_all(slug)
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
                # `_row_get`, because not every query that reaches here selects
                # the whole runs table. See app/promptbudget.py for what these
                # are for.
                "prompt_bytes": db._row_get(row, "prompt_bytes"),  # noqa: SLF001
                "output_tokens": db._row_get(row, "output_tokens"),  # noqa: SLF001
                "cache_read_tokens": db._row_get(row, "cache_read_tokens"),  # noqa: SLF001
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

    mine = scope.visible_ids(me())
    project_row = db.get_project_by_slug(project) if project else None
    # A slug you are not on is treated as no filter rather than as a filter
    # that matches nothing: the menu below only offers your own projects, so
    # the only way to get here is a stale link or a hand-typed URL, and an
    # empty table with a project name in the box reads as "this project has
    # never run" rather than "that is not yours".
    if project_row is not None and project_row["id"] not in mine:
        project_row = None
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
            "runs": _decorate_runs([r for r in rows if r["project_id"] in mine]),
            "history": usage.history(days, project_id=project_id, only_projects=mine),
            "usage": usage_snapshot(),
            "active_run": scope.only_runs(active_run_snapshot(), mine, scope.is_admin(me())),
            "page": page,
            "pages": pages,
            "total": total,
            "days": days,
            "windows": HISTORY_WINDOWS,
            "status_filter": status,
            "project_filter": project_row["slug"] if project_row else "",
            "projects": [p for p in db.list_projects() if p["id"] in mine],
        },
    )


@app.get("/run/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: int, filed: int = 0) -> HTMLResponse:
    # `filed` is set by the redirect a posted diff comment makes, so the
    # confirmation survives the POST-redirect-GET without the page being able
    # to re-post the comment on a refresh.
    return _render_run_page(request, run_id, comment_filed=bool(filed))


def _render_run_page(request: Request, run_id: int, **extra) -> HTMLResponse:
    """The run page, optionally carrying the outcome of an action taken on it."""
    row = db.get_run_with_project(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run = _decorate_runs([row])[0]
    text, _ = runlog.read_log(run_id, 0)
    landed = revert.landed(row)
    context = {
        "run": run,
        # A pruned transcript is a normal outcome, not an error - say so
        # rather than showing an empty terminal.
        "console_text": text or "",
        "log_pruned": not run["has_log"],
        "denials": db.hook_denials_for_run(run_id),
        "audit": db.hook_audit_for_run(run_id),
        "audit_retention_days": db.AUDIT_RETENTION_DAYS,
        "midrun": midrun.state(run_id),
        "midrun_events": db.midrun_events_for_run(run_id),
        "active_run": active_run_snapshot(),
        "landed": landed,
        # Built from the same Landed, so the diff and the undo button can never
        # disagree about which commits this run is responsible for.
        "diff": rundiff.for_run(landed),
        "undo_error": None,
        "comment_error": None,
        "comment_filed": False,
    }
    context.update(extra)
    return templates.TemplateResponse(request, "run.html", context)


@app.post("/run/{run_id}/comment", response_class=HTMLResponse)
async def comment_on_diff(
    request: Request,
    run_id: int,
    path: str = Form(""),
    index: str = Form(""),
    comment: str = Form(""),
):
    """Turn a line of this run's diff into a note on its project.

    RESEARCH.md §3: every comparable orchestrator steers its agent by comments
    on the diff, and the portal steered by prose written from memory. This is
    the smallest version of that which is actually useful - the comment becomes
    an ordinary project note, so it rides the path notes already have into the
    next run's prompt, and wakes a parked project the same way.

    The quoted line is read back out of the diff here rather than trusted from
    the form: see app/rundiff.py. Every refusal renders the page again with a
    sentence on it, because a phone user who taps `send` and gets a JSON error
    page has no idea whether their comment was filed.
    """
    row = db.get_run_with_project(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if not row["project_id"]:
        return _render_run_page(
            request, run_id,
            comment_error="This run has no project, so there is nobody to send a note to.",
        )

    hit = rundiff.line_at(rundiff.for_run(revert.landed(row)), path, _int_or(index, -1))
    if hit is None:
        return _render_run_page(
            request, run_id,
            comment_error=(
                "Pick a line first - tap one in the diff above, then press send. "
                "(If you did pick one, this run's diff has changed underneath the "
                "page; reload it and try again.)"
            ),
        )
    file, line = hit

    project = db.get_project(row["project_id"])
    if project is None:
        return _render_run_page(
            request, run_id,
            comment_error="This run's project no longer exists.",
        )
    db.add_journal(
        project["id"], "user", "note",
        rundiff.note_body(run_id, file, line, comment),
        person_id=_person_id(request),
    )
    await worker.reactivate_on_note(project)
    return RedirectResponse(url=f"/run/{run_id}?filed=1#rundiff", status_code=303)


def _int_or(raw: str, fallback: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback


@app.post("/run/{run_id}/revert", response_class=HTMLResponse)
async def revert_run_route(request: Request, run_id: int) -> HTMLResponse:
    """Undo the commits this run made to its project's workspace.

    Refusals render the page again with the reason on it rather than raising,
    because every one of them is a sentence a person has to read and act on, and
    FastAPI's default error page is a JSON blob - which is what Wes would be
    looking at on his phone at the exact moment something went wrong.

    The reason is recomputed inside `revert.undo` under the workspace lease, so
    what is shown here is what was actually true when the git ran, not what the
    page believed when it drew the button.
    """
    row = db.get_run_with_project(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")

    plan = revert.landed(row)
    who = people.name_of(me())
    outcome = revert.undo(row, who=who)
    if not outcome.ok:
        return _render_run_page(request, run_id, undo_error=outcome.message)

    db.mark_run_reverted(run_id)
    if plan is not None and row["project_id"]:
        # The next agent on this project has to be told, or it will find its
        # predecessor's feature missing, conclude the run died before committing,
        # and build the whole thing again. See revert.journal_note.
        db.add_journal(
            row["project_id"], "user", "status",
            revert.journal_note(run_id, plan, who, outcome.sha),
        )
    log.info("Run %s reverted by %s", run_id, who)
    if plan is not None and plan.is_source and outcome.sha:
        # Reverting the portal's own source only changes files on disk; imported
        # Python does not change until the process does. Without this the undo
        # would appear to work and change nothing about the running site, which
        # is the quiet-failure shape - and this is the one revert most likely to
        # be someone urgently backing out a broken self-update. The scheduler's
        # own restart path is reused so it still defers behind in-flight runs.
        worker.schedule_source_restart(row["project_id"], outcome.sha)
    return RedirectResponse(url=f"/run/{run_id}", status_code=303)


@app.post("/run/{run_id}/cancel")
async def cancel_run_route(run_id: int, next: str = Form("/")) -> RedirectResponse:
    """Stop the agent mid-run. Reachable from the dashboard strip, the project
    console and the run page, so it redirects back to wherever it was pressed."""
    outcome = worker.cancel_run(run_id)
    log.info("Cancel run %s -> %s", run_id, outcome)
    return RedirectResponse(url=_safe_next(next), status_code=303)


@app.post("/run/{run_id}/pause")
async def pause_run_route(request: Request, run_id: int, next: str = Form("/")) -> RedirectResponse:
    """Hold a live run at its next tool call, spending nothing until resumed.
    See app/midrun.py for what "next tool call" costs and why."""
    outcome = midrun.pause(run_id, by=_person_name(request))
    log.info("Pause run %s -> %s", run_id, outcome)
    return RedirectResponse(url=_safe_next(next), status_code=303)


@app.post("/run/{run_id}/resume")
async def resume_run_route(request: Request, run_id: int, next: str = Form("/")) -> RedirectResponse:
    outcome = midrun.resume(run_id, by=_person_name(request))
    log.info("Resume run %s -> %s", run_id, outcome)
    return RedirectResponse(url=_safe_next(next), status_code=303)


def _person_name(request: Request) -> str:
    """Who pressed a button, for a journal line - "" when nobody is known."""
    try:
        pid = _person_id(request)
        person = people.get(pid) if pid else None
        return people.name_of(person) if person else ""
    except Exception:  # noqa: BLE001 - a byline must never break the press
        return ""


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


@app.get("/everyone", response_class=HTMLResponse)
async def everyone_page(request: Request) -> HTMLResponse:
    """The whole board, grouped by whose it is. The owner's door into other
    people's projects, kept off his own feed - Wes, 2026-07-28: "I want a way
    where I can go view other users' projects, but dont want them in my main
    feed."

    404 rather than 403 for everybody else. Not because it is a secret - the
    portal has no passwords and app/scope.py is explicit that none of this is
    an authorization boundary - but because "you may not" invites a second
    attempt at a door that is not going to open, while a page that simply is
    not there for you is the honest description of a link you were never shown.
    """
    viewer = resolve_person(request)
    if not scope.is_admin(viewer):
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse(
        request,
        "everyone.html",
        {
            "groups": scope.by_person(viewer),
            "question_counts": db.open_question_counts(),
            "running_ids": set(active_run_snapshot()["project_ids"]),
        },
    )


@app.get("/questions", response_class=HTMLResponse)
async def questions_page(request: Request) -> HTMLResponse:
    # Questions on projects Wes has paused, or that are still in the backlog,
    # are answerable but not pressing - they go in their own section below the
    # live ones rather than competing with them.
    shelved = db.shelved_project_ids()
    mine = scope.visible_ids(me())
    open_qs = [q for q in db.open_questions() if q["project_id"] in mine]
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
    old behavior of returning to the project stands.
    """
    if next:
        return _safe_next(next)
    project = db.get_project(question["project_id"])
    return f"/project/{project['slug']}" if project else "/questions"


@app.get("/questions/{question_id}/tap", response_class=HTMLResponse)
async def question_tap(request: Request, question_id: int, opt: str = "") -> HTMLResponse:
    """Where an answer button on a lock-screen notification lands.

    A Declarative Web Push action navigates; it cannot post. So the button
    carries the option's INDEX here and this page submits the answer itself -
    see app/templates/question_tap.html for why it is not simply done in this
    handler.

    The index resolves against the options stored on the question, exactly as
    the Telegram callback does. Two things it deliberately refuses rather than
    guesses at: an index that no longer maps to an option (the question was
    re-asked with a different list), and a question that has already been
    settled by whoever got there first - a phone can hold a notification for a
    day, and a stale tap must not overwrite a real answer.
    """
    question = db.get_question(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")

    options = quickreplies.decode(question["quick_options"])
    choice, note = "", ""
    if question["status"] != "open":
        note = "That question has already been answered."
    else:
        try:
            index = int(opt)
        except ValueError:
            index = -1
        if 0 <= index < len(options):
            choice = options[index]
        else:
            note = "That button no longer maps to an answer - pick one here instead."

    return templates.TemplateResponse(
        request,
        "question_tap.html",
        {
            "question": question,
            "choice": choice,
            "note": note,
            "heading": "Answering" if choice else "Nothing to send",
        },
    )


@app.post("/questions/{question_id}/answer")
async def answer_question(
    request: Request,
    question_id: int,
    answer: str = Form(""),
    choice: str = Form(""),
    then: str = Form(""),
    next: str = Form(""),
) -> RedirectResponse:
    """Answer a question, by tapping an offered option, by typing, or both.

    Wes, 2026-07-28: "allow the prompt that asked the question to pre-fill some
    multiple-choice questions that can be clicked for ease of answering, but
    always allow the user to still be able to type in additional context if
    desired or just to skip clicking one of those answers altogether and answer
    by typing outright."

    So the option buttons are real submits carrying `choice`, which makes the
    common case one tap with no JavaScript at all, and anything already typed
    in the box rides along with the tap rather than being thrown away.

    `choice` is checked against the options actually stored on the question
    rather than trusted: the answer is journalled and read by the next agent,
    and an answer it never offered is a worse thing to record than a dropped
    tap. An unrecognized choice falls back to the typed text alone.

    `then=queue` is the "queue answer" button, and it is the same opt-out the
    note box's "queue note" is: the answer is recorded and journalled exactly as
    ever, and nothing else is touched - no run now, whatever else is or is not
    still open. Wes, 2026-08-30: "There should now be an option, when answering
    questions, to queue the answer rather than running it immediately."
    """
    question = db.get_question(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    typed = answer.strip()
    picked = choice.strip()
    if picked and picked not in quickreplies.decode(question["quick_options"]):
        log.warning("Ignoring unoffered choice %r on question %s", picked[:80], question_id)
        picked = ""
    text = f"{picked} - {typed}" if picked and typed else (picked or typed)
    if not text:
        # Nothing was chosen and nothing was typed. Sending an empty answer
        # would settle the question against a blank, which reads to the next
        # agent as a decision that was never made.
        return RedirectResponse(url=_after_question(question, next), status_code=303)
    # Who is answering, so the next agent can pitch its reply at them rather
    # than at whoever it assumed. Resolved from the request, not from the
    # ContextVar - see _person_id.
    db.answer_question_and_resume(question_id, text, _person_id(request))
    # A web answer settles the Telegram copies too - without this, whoever
    # got the question on Telegram keeps a message that looks open forever.
    await notify.settle_question_copies(question_id, f"answered: {text}")
    # An answer is an instruction, so it starts a run the way a note does -
    # unless this press was the explicit "queue answer". See
    # worker.answer_arrived for the three cases that deliberately do not, one of
    # which is "another question on this project is still open".
    if then != "queue":
        await worker.answer_arrived(question)
    return RedirectResponse(url=_after_question(question, next), status_code=303)


@app.post("/questions/{question_id}/dismiss")
async def dismiss_question(question_id: int, next: str = Form("")) -> RedirectResponse:
    """Save a question for later.

    It comes off the questions page and stops ringing the notification, but it
    stays on its own project, answerable in place, under "saved for later".
    Wes, 2026-07-28: "when dismissing a question, it should be saved for later
    ... it should still show the question in the relevant project page, but it
    should be removed from the questions page." The saying-no version of this
    is `delete` below."""
    question = db.get_question(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    db.dismiss_question_and_resume(question_id)
    # Telegram buttons on a dismissed question already answer "Already
    # handled." - settling the copies makes them say so instead of waiting
    # to be tapped. A typed reply to the message still answers by row id.
    await notify.settle_question_copies(question_id, "saved for later")
    return RedirectResponse(url=_after_question(question, next), status_code=303)


@app.post("/questions/{question_id}/delete")
async def delete_question(question_id: int, next: str = Form("")) -> RedirectResponse:
    """Throw a question away for good.

    The question is answered with `db.DELETED_ANSWER` so the agent reads a
    decision rather than a silence, no run is queued by it, and the same
    question can never be filed again - not even in a different wording (the
    dedupe pass in `db.file_question` treats a deleted question as permanent).
    """
    question = db.get_question(question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    db.delete_question(question_id)
    await notify.settle_question_copies(question_id, "deleted")
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
    request: Request, slug: str, text: str = Form(...), owner: str = Form("agent")
) -> RedirectResponse:
    """Add an item. `owner` is "agent", "user", or "person:<id>".

    The third form is what the picker posts once the install has more than one
    person in it, and it is the main way the human half of a list ever gets
    attributed: somebody choosing their own name from a dropdown is a decision,
    where stamping an existing row with it would have been a guess.
    """
    project = _get_project_or_404(slug)
    person_id = None
    if owner.startswith("person:"):
        raw = owner.split(":", 1)[1].strip()
        person = people.get(int(raw)) if raw.isdigit() else None
        # An id that names nobody falls back to an unattributed human item
        # rather than to the agent's list: the one thing the picker is certain
        # about is that a human was chosen.
        owner = "user"
        person_id = int(person["id"]) if person is not None else None
    elif owner == "user" and people.more_than_one():
        # "for me" from the picker. Whoever is holding the phone, resolved the
        # same way a note they post is - not the owner, who is only the right
        # answer on the install where he is the only answer.
        me = resolve_person(request)
        person_id = int(me["id"]) if me is not None else None
    db.add_todo(project["id"], text, owner, person_id=person_id)
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
    """Put a tag on a row or take one off. Both fields normalize in db, so
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


@app.post("/todo/{todo_id}/person")
async def refile_todo(todo_id: int, person: str = Form("")) -> RedirectResponse:
    """Hand an existing item to somebody, to nobody with an empty `person`, or
    back to the agent with `person=agent`.

    Until this existed the only way to change whose an item was, was to delete
    it and add it again - which threw away its tags and its age.

    Three destinations, not two, and "nobody" is not the same as "the agent":
    nobody says a person has to do this and we cannot yet say which, so the
    item stays on the human half; the agent says a person does not have to do
    it at all.

    A person who is not a member of this project files as nobody rather than
    being accepted: an item can only be somebody's if they can see the project
    it is on, and this matches the create route, which also prefers an
    unattributed human item to a wrong attribution. The UI never offers a
    non-member, so reaching this needs a hand-made request.
    """
    todo = db.get_todo(todo_id)
    if todo is None:
        raise HTTPException(status_code=404, detail="Todo not found")
    raw = person.strip()
    if raw == todos.AGENT_CHOICE:
        db.set_todo_agent(todo_id)
    else:
        person_id = None
        if raw.isdigit() and people.is_member(todo["project_id"], int(raw)):
            person_id = int(raw)
        db.set_todo_person(todo_id, person_id)
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
            "user_done_groups": todos.by_person(
                db.completed_todos(project["id"], owner="user"), project["id"]
            ),
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
            # Above how many characters a suggestion's description gets an
            # "open it" toggle instead of being silently cut off at two lines.
            #
            # A character count is a proxy for a line count, which CSS knows
            # and the server does not. It is deliberately set BELOW where the
            # clamp actually bites (a cell is ~34rem wide, so two lines of
            # 0.8rem text hold roughly 150 characters): erring low gives a few
            # short suggestions a toggle that reveals nothing much, while
            # erring high would leave exactly the description Wes complained
            # about with no way to read the rest of it.
            "SUGGESTION_EXPAND_CHARS": 110,
            "dismissal_days": db.SUGGESTION_DISMISSAL_DAYS,
            "learnings_lines": len(learnings.splitlines()),
            "learnings_chars": len(learnings),
            "learnings_cap": worker.learnings_cap_kb(),
            "learnings_over_cap": worker.learnings_over_cap(),
            # Not the file's size but how much of it a run actually reads. The
            # size alone told Wes nothing he could act on; "127 of 171 never
            # reach a prompt" is the number that says the file has a problem.
            "learnings_reach": worker.learnings_reach(),
            "profile_chars": len(profile),
            "profile_cap_kb": worker.profile_cap_bytes() // 1024,
            "profile_over_cap": worker.profile_over_cap(),
            "revisions": memory.revisions(),
            "compacting": worker.compaction_running(),
            # The other reason the compact button can do nothing: a daily
            # reflect (or a compaction started before the last restart) is
            # working in the same directory and holds its lease. Without this
            # the button would be live, press to no effect, and say nothing.
            "memory_busy": worker.memory_leased(),
            "cli_memory": cli_memory,
            "cli_memory_files": sum(d.file_count for d in cli_memory),
            "archived_learnings": memory.archived_learnings(),
            # Named on the page so the archive's "this does not age out" claim
            # is measured against the real depth of the history, not a number
            # written into the copy that would drift from `memory.KEEP`.
            "revision_keep": memory.KEEP,
            "learnings_freshness": worker.learnings_freshness(),
            "promoted_skills": memory.promoted_skills(),
            # Only once there are two people to tell apart, which is exactly
            # when the reflect starts maintaining these - a single-person
            # install would get a heading over one row that can never fill in.
            "people_learned": people.learned_overview() if len(people.everyone()) > 1 else [],
            "people_learned_max": people.LEARNED_MAX_LINES,
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
    """Send an agent through learnings.md to distill it.

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


@app.post("/memory/person/{slug}/clear")
async def clear_person_learned(slug: str) -> RedirectResponse:
    """Throw away what the reflect has concluded about one person.

    Deliberately not an edit box. What is in here is an inference from
    evidence, and a hand-edited inference is neither - if Wes wants to *state*
    something about somebody, the field for that is their background on the
    settings page, which no agent may write. This button says "you got this
    wrong, start again", and the next reflect does."""
    if not people.clear_learned(slug):
        raise HTTPException(status_code=404, detail="Nothing learned about that person")
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
async def accept_suggestion(request: Request, suggestion_id: int) -> RedirectResponse:
    suggestion = db.get_suggestion(suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    # `stage`, not `status`: the eight-value status enum was folded into the
    # stage model on 2026-07-22 and this call was never updated, so accepting a
    # suggestion raised TypeError and Wes got a 500 on every attempt. A new
    # idea lands in `backlog` unapproved, exactly like one typed in by hand -
    # and, like one typed in by hand, on the board of whoever pressed accept.
    project = db.create_project(
        title=suggestion["title"],
        description=suggestion["description"],
        kind="unknown",
        stage="backlog",
        person_id=_person_id(request),
    )
    db.set_suggestion_status(suggestion_id, "accepted")
    return RedirectResponse(url=f"/project/{project['slug']}", status_code=303)


@app.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(suggestion_id: int) -> RedirectResponse:
    suggestion = db.get_suggestion(suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    db.set_suggestion_status(suggestion_id, "dismissed")
    return RedirectResponse(url="/memory", status_code=303)


@app.post("/suggestions/{suggestion_id}/restore")
async def restore_suggestion(suggestion_id: int) -> RedirectResponse:
    """Undo a dismissal. Wes, 2026-07-28: "I also want to be able to undo where
    I've told it some projects that I don't want it to work on."

    Accepted suggestions are restorable too - accepting created a project, and
    changing your mind about that is deleting the project, not un-accepting the
    row - but the button is only offered on dismissed ones for that reason."""
    suggestion = db.get_suggestion(suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    db.set_suggestion_status(suggestion_id, "proposed")
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
    # The appearance dropdowns must show what THIS reader actually sees, not
    # the install's rows - otherwise the panel opens showing somebody else's
    # scanlines and the first save quietly adopts them. Overlaying the
    # resolved look onto `settings` keeps `select_field` unchanged: the macro
    # reads `settings[key]` and does not need to learn about people.
    viewer = None
    try:
        viewer = resolve_person(request)
    except Exception:  # pragma: no cover - defensive
        log.debug("Could not resolve who is reading Settings", exc_info=True)
    settings.update(appearance(viewer))
    # Which layers this person has pinned, so the panel can say so and offer
    # the way back to following the install.
    my_look = people.appearance_of(viewer)
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
            # How runs are paid for: this is the page that owns the per-run
            # ceiling, and what an unset ceiling *means* depends on the mode.
            "auth": spawnauth.status(),
            # The subscription login itself - whether one is on file, and the
            # in-portal /login flow (see app/claudelogin.py). Only rendered in
            # subscription mode; an API-key install has no login to keep fresh.
            "claude": {
                "status": claudelogin.status(),
                "pending": claudelogin.pending(),
                "result": claudelogin.last_result(),
            },
            # Same reasoning one field down: a blank memory cap is not "no cap",
            # it is a number derived from this machine, so the page has to say
            # which number rather than let someone guess.
            "runmem": {
                "available": runlimit.available(),
                "default_human": runlimit.human(runlimit.default_max_bytes()),
                "total_human": runlimit.human(runlimit.total_memory_bytes()),
                # The pool's own three answers: is it possible here, what does
                # blank mean, and is it actually in force. The third is not the
                # first two ANDed - systemd can refuse the property - and the
                # page reports the effective answer.
                "pool_available": runlimit.pool_available(),
                "pool_default_human": runlimit.human(runlimit.default_pool_bytes()),
                "pool_in_use": runlimit.pool_in_use(),
            },
            # A blank headroom reserve is not "no reserve" - it is the number
            # the portal measured from its own runs - so the field shows that
            # number as its placeholder and says how many runs it stands on.
            # Zero samples is worth showing too: it is the honest "this is
            # still the provisional default" the field would otherwise hide.
            "headroom_reserve": f"{headroom.reserve():g}",
            "headroom_samples": headroom.sample_size(),
            "headroom_max": f"{headroom.MAX_RESERVE:g}",
            # The zones quiet hours can be read in. A dropdown rather than a
            # free-text box because an IANA name is exactly the kind of string
            # a person mistypes ("America/Arkansas") and the mistake is silent:
            # a rejected zone falls back and the hours quietly move.
            "quiet_zones": quiet.zone_choices(),
            # One row per jumpable section: the field to render, and what the
            # key does now. Derived from jumpkeys.ACTIONS rather than listed in
            # the template, so a new jumpable section grows its settings field
            # without anyone remembering to add one.
            "jump_keys": jumpkeys.rows(settings),
            # Whose theme the appearance panel is editing, and which of its
            # layers they have pinned away from the install's look.
            "my_look": my_look,
            # The theme the panel is EDITING, which is your own even while you
            # are previewing somebody else's - the panel saves against you and
            # says so. Taken from `appearance(me())` rather than the bare
            # `appearance()` for exactly that reason: the plain call follows a
            # running preview, and a panel whose dropdown said "terminal"
            # while the line under it said "you are on the paper theme" is a
            # page arguing with itself.
            "my_theme": appearance(me()).get(
                "ui_theme", config.APPEARANCE_DEFAULTS["ui_theme"]
            ),
            # The stock, not the theme name: everything the printed structure
            # undoes is undone under EVERY light theme, so the panel that says
            # so has to ask the question that way round or the third light
            # theme silently starts claiming otherwise.
            "my_stock": config.THEME_STOCK.get(
                appearance(me()).get("ui_theme", config.APPEARANCE_DEFAULTS["ui_theme"]),
                config.DEFAULT_THEME_STOCK,
            ),
            # And the type, which is the one the CRT dials actually answer to.
            # It was the stock until workbench and blueprint existed - two DARK
            # themes with nothing terminal in them - and asking it the old way
            # round had the panel promising scanlines on a Linear-shaped app.
            "my_type": config.THEME_TYPE.get(
                appearance(me()).get("ui_theme", config.APPEARANCE_DEFAULTS["ui_theme"]),
                config.DEFAULT_THEME_TYPE,
            ),
            # How this person has arranged a project page, and the same thing
            # as one line of prose. `appearance(me())` for the same reason the
            # theme above uses it: the panel edits YOUR arrangement even while
            # you are previewing somebody else's page.
            "my_arrangement": appearance(me()).get(sections.SETTING_KEY, ""),
            "my_sections": sections.sections(appearance(me()).get(sections.SETTING_KEY, "")),
            "my_arrangement_desc": sections.describe(
                appearance(me()).get(sections.SETTING_KEY, "")
            ),
            "install_look": install_appearance(),
            "people_count": len(people.everyone()),
            # Archived people included on purpose: this is the one page where
            # bringing somebody back has to be possible, and a person who has
            # vanished from every screen is a person nobody can un-archive.
            "people_rows": people.everyone(include_archived=True),
            # Wes, 2026-07-28: "remove the pronouns from the actual gender
            # selection field and just have it say 'male,' 'female,' or 'rather
            # not say'" - and then, when the first pass missed the add-someone
            # form: "The pronouns still show up in the user creation drop-down."
            # Both forms read this list, so there is one place to get it right.
            #
            # The pronouns were there to show what the answer would make the
            # agent write, which is a thing the person answering does not need
            # to know: they are being asked about themselves, not about the
            # prose. The blank option stays FIRST despite reading last in the
            # note, because a <select> with no selected option shows its first
            # - so putting it anywhere else would make "male" the silent
            # default on the add form. It is not a third answer; it is the
            # state of a row nobody has answered. See site.UNSPECIFIED.
            "GENDER_CHOICES": [("", "rather not say")]
            + [(key, key) for key in site.GENDERS],
            "saved": saved,
            "sent": request.query_params.get("sent") == "1",
            "push_sent": request.query_params.get("push_sent"),
            "push_subs": db.list_push_subscriptions(),
            "max_parallel_limit": config.MAX_PARALLEL_LIMIT,
            "slug_suggestions": db.projects_with_suggested_slugs(),
            "tailnet": netinfo.cached(),
            "portal_port": config.PORT,
            "model_catalog": modelwatch.catalog(),
            "strays": _stray_view(),
        },
    )


def _stray_view() -> list[dict]:
    """Helpers that outlived the runs that started them, named for a person.

    A leftover is otherwise completely invisible: it is a live server holding a
    port, started by an agent that finished hours ago, and nothing in the UI has
    ever said so. Six of them were up on Wes's box when this was written.

    The project title comes from the run id baked into the scope name. It is
    best-effort on purpose - a run row can be pruned while its leftover is still
    running, and "some helper is still up" is worth showing even when the portal
    can no longer say which project asked for it.
    """
    view = []
    for scope in strays.listing():
        title = None
        if scope.run_id is not None:
            run = db.get_run_with_project(scope.run_id)
            if run is not None:
                title = run["project_title"]
        view.append({
            "unit": scope.unit,
            "run_id": scope.run_id,
            "project_title": title,
            "processes": [
                {"pid": p.pid, "command": p.command} for p in scope.processes
            ],
        })
    return view


@app.post("/settings/stray/stop")
async def stop_stray(unit: str = Form(...)) -> RedirectResponse:
    """Stop one leftover helper and everything under it.

    Deliberately a button rather than something the portal does by itself: these
    are usually the preview servers "open it" points at, and a portal that
    silently killed the thing a run was built to show would be a worse bug than
    the leak. `strays.stop` re-checks the unit name, because this takes one from
    a form and stopping an arbitrary user unit is a much larger power than
    stopping a leftover the portal created.
    """
    strays.stop(unit)
    return RedirectResponse(url="/settings#strays", status_code=303)


@app.post("/settings/appearance/reset")
async def reset_my_appearance(request: Request) -> RedirectResponse:
    """Follow the install's look again, dropping this person's overrides.

    Not the same as setting every layer back to the shipped default: this
    re-attaches them to the install, so a later change to the install's look
    reaches them. That distinction is why the column stores a subset rather
    than a full copy (see people.appearance_of).
    """
    person_id = _person_id(request)
    if person_id is not None:
        people.clear_appearance(person_id)
    return RedirectResponse(url="/settings#appearance", status_code=303)


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
    code does not recognize is dropped loudly at one choke point instead of
    being silently ignored by FastAPI's form binding.
    """
    form = await request.form()
    declared = form.get(settings_form.FIELDS_INPUT)
    values = settings_form.apply(
        {key: str(value) for key, value in form.multi_items()},
        declared if isinstance(declared, str) else None,
    )
    # The appearance layers belong to whoever is reading, not to the install -
    # see settings_form.PERSONAL_KEYS. Everything else is one portal-wide row
    # as it always was.
    install, personal = settings_form.split_personal(values)
    for key, value in install.items():
        db.set_setting(key, value)
    if personal:
        person_id = _person_id(request)
        if person_id is None:
            # No identifiable person - a script, or an identity lookup that
            # failed. Writing the theme to nobody would silently discard it, so
            # it lands on the install instead, which is exactly what this form
            # did before people existed.
            for key, value in personal.items():
                db.set_setting(key, value)
        else:
            people.set_appearance(person_id, personal)
    section = str(form.get("_section") or "").strip()
    saved = ",".join(sorted(values))
    url = f"/settings?saved={quote(saved)}"
    if section:
        url += f"#{quote(section)}"
    return RedirectResponse(url=url, status_code=303)


# --------------------------------------------------------------------------
# People (see app/people.py)
# --------------------------------------------------------------------------

def _safe_next(raw: str, fallback: str = "/") -> str:
    """A redirect target that cannot leave this portal.

    Every one of these routes takes a "come back to where I was" parameter, and
    an open redirect is the one thing a form field like that can turn into. A
    path starting with a single `/` is this site; `//evil.example` is not (the
    browser reads it as a protocol-relative URL), which is the case a naive
    `startswith("/")` misses.
    """
    raw = (raw or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return fallback


@app.post("/whoami")
async def set_whoami(
    person: str = Form(""),
    next: str = Form("/"),
) -> RedirectResponse:
    """Say which person is using this browser.

    The cookie is the top of `people.resolve`'s precedence: somebody who has
    said "I am Erin" on this device has made a statement about themselves, and
    nothing the network infers may overrule it.

    Ten years, and not httponly. Not a session - expiring it would silently
    start attributing her notes to him, which is the exact failure the whole
    feature exists to prevent - and not a secret, because it says a first name
    a browser is welcome to read. `samesite=lax` so it survives following a
    link in from Telegram.
    """
    target = _safe_next(next)
    response = RedirectResponse(url=target, status_code=303)
    row = people.by_slug(person)
    if row is None:
        return response
    response.set_cookie(
        people.COOKIE,
        row["slug"],
        max_age=people.COOKIE_MAX_AGE,
        httponly=False,
        samesite="lax",
    )
    return response


@app.post("/look")
async def set_look(person: str = Form(""), next: str = Form("/")) -> RedirectResponse:
    """Look at the portal the way somebody else does.

    Wes, 2026-07-28: "Also allow users to switch themes and view whatever it
    would look like for another user."

    A preview and nothing more. It reaches `appearance()` and stops there:
    you are still you for every note you post, every project that counts as
    yours, and everything the agent is told about who is asking. An empty
    `person` ends the preview.

    A session cookie, unlike the identity one, and that asymmetry is
    deliberate. Forgetting who you are silently misattributes your notes, so
    that cookie lasts ten years; forgetting that you were trying her theme on
    costs you one click. A preview you cannot remember starting is a portal
    that looks broken.
    """
    response = RedirectResponse(url=_safe_next(next), status_code=303)
    row = people.by_slug(person) if person else None
    if row is None:
        response.delete_cookie(LOOK_COOKIE)
        return response
    response.set_cookie(LOOK_COOKIE, row["slug"], httponly=False, samesite="lax")
    return response


@app.post("/people/add")
async def add_person(
    name: str = Form(""),
    gender: str = Form(""),
    background: str = Form(""),
) -> RedirectResponse:
    if (name or "").strip():
        people.add(name=name, gender=gender, background=background)
    return RedirectResponse(url="/settings?saved=people#people", status_code=303)


@app.post("/people/{person_id}/edit")
async def edit_person(
    person_id: int,
    name: str = Form(""),
    gender: str = Form(""),
    background: str = Form(""),
    tailnet_login: str = Form(""),
    ntfy_topic: str = Form(""),
    telegram_chat_id: str = Form(""),
) -> RedirectResponse:
    # `people.update` ignores name and gender for the owner - those belong to
    # portal.toml, because SITE.owner already names that person in the agent
    # contract and the todo headings. The panel renders them as read-only text
    # for the owner, so this is the belt to that braces.
    people.update(
        person_id,
        name=name,
        gender=gender,
        background=background,
        tailnet_login=tailnet_login,
        ntfy_topic=ntfy_topic,
        telegram_chat_id=telegram_chat_id,
    )
    return RedirectResponse(url="/settings?saved=people#people", status_code=303)


@app.post("/people/{person_id}/archive")
async def archive_person(person_id: int, restore: str = Form("")) -> RedirectResponse:
    if restore:
        people.restore(person_id)
    else:
        people.archive(person_id)
    return RedirectResponse(url="/settings?saved=people#people", status_code=303)


@app.post("/project/{slug}/members")
async def set_project_members(
    request: Request,
    slug: str,
    member: list[int] = Form(default=[]),
) -> RedirectResponse:
    """Whose project this is. Wes: "they should be able to be reassigned if
    desired."

    An empty selection is not refused here but in `people.set_members`, which
    falls back to the owner: it is reachable by unticking the last box, and a
    project with nobody on it shows on no dashboard.
    """
    project = _get_project_or_404(slug)
    people.set_members(project["id"], member)
    return RedirectResponse(url=f"/project/{slug}#project", status_code=303)


# --------------------------------------------------------------------------
# Claude login (see app/claudelogin.py) - the CLI's /login flow, from a phone.
# --------------------------------------------------------------------------

@app.post("/settings/claude-login/start")
async def claude_login_start() -> RedirectResponse:
    claudelogin.begin()
    return RedirectResponse(url="/settings#claude-account", status_code=303)


@app.post("/settings/claude-login/cancel")
async def claude_login_cancel() -> RedirectResponse:
    claudelogin.cancel()
    return RedirectResponse(url="/settings#claude-account", status_code=303)


@app.post("/settings/claude-login/finish")
async def claude_login_finish(code: str = Form("")) -> RedirectResponse:
    # The exchange is a network round trip; off the event loop with it.
    result = await asyncio.to_thread(claudelogin.finish, code)
    if result.get("ok"):
        # The usage cache was answering with the dead token's reading (or a
        # cached error). Refresh it now so the page reflects the new login
        # without waiting for the poller's next lap.
        await limits.refresh_async()
    return RedirectResponse(url="/settings#claude-account", status_code=303)


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
    # Whose phone this is. It is the same identity every other write on this
    # portal resolves (cookie first, `tailscale whois` second), and it is what
    # keeps a question about her project off his lock screen - see
    # app/routing.py.
    db.add_push_subscription(
        endpoint,
        p256dh,
        auth,
        ua=(request.headers.get("user-agent") or "")[:200],
        person_id=_person_id(request),
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
    subscription until it re-enrolls or revokes it, but the portal stops
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


# How many entries the dashboard's activity fold asks for the first time it is
# opened, and how much bigger each "show more" gets. Wes: "don't load everything
# possible but just load a certain range of entries."
ACTIVITY_PAGE = 15
ACTIVITY_MAX = 200


@app.get("/activity/feed", response_class=HTMLResponse)
async def activity_feed(request: Request, limit: int = ACTIVITY_PAGE) -> HTMLResponse:
    """The dashboard's recent-activity entries, as an HTML fragment.

    Under `/activity/feed` and not `/activity`, which is already the full
    activity PAGE (runs, heatmap, filters). Registering a second handler on that
    path does not fail - FastAPI keeps the first match - so the collision showed
    up only as this fragment quietly answering with an entire HTML document.

    The dashboard itself renders this fold shut and empty; app.js fetches it the
    first time it is opened, and again with a bigger `limit` for "show more".
    Nothing here is on the critical path of the dashboard - which is the point,
    because the feed is the heaviest thing that page used to carry and the one
    Wes says he almost never looks at.

    Scoped to the reader's own projects by the same rule the dashboard filters
    by, and in SQL rather than afterwards - see `db.list_journal`. Doing it here
    rather than trusting the caller matters: this is a real address, so it
    answers whoever asks it directly just as it answers the fold.
    """
    # Clamped rather than trusted. `limit` arrives in a query string, and an
    # unbounded one turns a fetch meant to keep the page light into a request
    # that renders every journal entry the install has ever written.
    limit = max(1, min(int(limit), ACTIVITY_MAX))
    mine = scope.visible_ids(me())
    rows = db.list_journal(limit=limit, only_projects=mine)
    # Only offer more when this answer actually filled the range asked for. A
    # short read means the feed is exhausted, and a "show more" that comes back
    # identical is a control that reads as broken.
    more = limit + ACTIVITY_PAGE
    return templates.TemplateResponse(
        request,
        "_activity_feed.html",
        {
            "recent_journal": rows,
            "more_limit": more if len(rows) >= limit and limit < ACTIVITY_MAX else 0,
        },
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

      * nosniff, so a browser cannot decide a mislabeled .png is really HTML
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
async def api_usage_history(request: Request, days: int = 14, project: str = "") -> JSONResponse:
    # by_project names every project it counts, so this is scoped like the
    # page it backs rather than left open behind it.
    mine = scope.visible_ids(resolve_person(request))
    project_row = db.get_project_by_slug(project) if project else None
    if project_row is not None and project_row["id"] not in mine:
        project_row = None
    return JSONResponse(
        usage.history(
            days,
            project_id=project_row["id"] if project_row else None,
            only_projects=mine,
        )
    )


@app.post("/hooks/pre-tool")
async def hooks_pre_tool(request: Request, run: int = 0, token: str = "") -> JSONResponse:
    """PreToolUse guardrail endpoint: every guarded run's tool calls arrive
    here (relayed by app/hookrelay.py) and get an allow/deny back. Loopback
    traffic from runs this portal spawned itself; anything unrecognizable
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
    # The mid-run channel rides the same post: a paused run is told to hold
    # here (the relay polls /hooks/hold), and a note typed while it worked is
    # handed back as the hook's additionalContext. See app/midrun.py.
    answer = midrun.after_tool_call(run, token, payload)
    return JSONResponse({"ok": True, **answer})


@app.post("/hooks/hold")
async def hooks_hold(run: int = 0, token: str = "") -> JSONResponse:
    """A held relay asking whether its run may go on. Answers `poll` again
    while the run is paused; once resumed, whatever notes arrived meanwhile
    ride back as the hook's additionalContext. An unknown run is released."""
    if not hookguard.authorized(run, token):
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": True, **midrun.hold_poll(run, token)})


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


@app.get("/mcp/tools")
async def mcp_tools(run: int = 0, token: str = "") -> JSONResponse:
    """The tool list one run's MCP relay sees (app/mcpstdio.py).

    Unlike the hook endpoints this fails *closed*: an unrecognized run gets an
    empty list rather than the benefit of the doubt, because the tool behind it
    files questions and pushes notifications to somebody's phone."""
    return JSONResponse({"tools": portalmcp.tools(run, token) or []})


@app.post("/mcp/call")
async def mcp_call(request: Request, run: int = 0, token: str = "") -> JSONResponse:
    """One `tools/call` from a run, relayed here. The response is the MCP tool
    result verbatim - the relay stays a dumb pipe, the portal owns the shape.

    This request can legitimately take minutes: `ask` blocks while it waits for
    a person to answer, which is the entire point of it."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    result = await portalmcp.call(
        run, token, str(payload.get("name") or ""), payload.get("arguments") or {}
    )
    return JSONResponse(result)


@app.get("/api/active-run")
async def api_active_run(request: Request) -> JSONResponse:
    """Polled by every page so the header can show live agent activity.

    Scoped like the strip it feeds. This one is easy to forget precisely
    because it does not look like a project listing - but it carries titles and
    slugs, and every page polls it every few seconds, so leaving it open would
    have announced other people's work in the header of the very pages the
    filtering above had just cleaned up.
    """
    viewer = resolve_person(request)
    mine = scope.visible_ids(viewer)
    return JSONResponse({
        **scope.only_runs(active_run_snapshot(), mine, scope.is_admin(viewer)),
        "usage": usage_snapshot(),
    })


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
            "paused": midrun.is_paused(run_id),
            "text": text,
            "offset": new_offset,
            "events": run["events"] or 0,
        }
    )

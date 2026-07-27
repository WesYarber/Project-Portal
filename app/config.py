"""Paths and default settings for Project Portal."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app import site

# This installation's identity - owner, hostname, ports, SSH user - resolved
# from portal.toml / the environment / the machine itself (see app/site.py).
# Bound first because the defaults below are seeded from it.
SITE = site.SITE

# Base directories -----------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
# The portal's own source checkout and its meta-project slug: runs on this
# project can modify the app itself, which triggers a service restart.
APP_ROOT = BASE_DIR
META_PROJECT_SLUG = "project-portal"
DB_PATH = DATA_DIR / "portal.db"
MEMORY_DIR = DATA_DIR / "memory"
PROJECTS_DIR = DATA_DIR / "projects"
# Live per-run transcripts, one text file per run id (see app/runlog.py).
RUNS_DIR = DATA_DIR / "runs"
# Scratch workspaces for one-off tasks, one directory per task id
# (see app/oneoff.py). Beside PROJECTS_DIR, never inside it - these are not
# projects and must not show up on the dashboard's folder scans.
TASKS_DIR = DATA_DIR / "tasks"

# Claude Code skills the portal ships to every project workspace. Copied into
# <workspace>/.claude/skills/ before each run (see worker._ensure_workspace),
# so an agent picks them up the same way it would a skill Wes wrote by hand.
SKILLS_DIR = BASE_DIR / "app" / "skills"

PROFILE_MD = MEMORY_DIR / "profile.md"
LEARNINGS_MD = MEMORY_DIR / "learnings.md"
SUGGESTIONS_MD = MEMORY_DIR / "suggestions.md"

# Models -----------------------------------------------------------------------
# (value, label) pairs for the agent dropdown. `value` is the portal's internal
# alias, woven through settings, spend ranks and usage windows; it is NOT handed
# to the CLI directly - `cli_model()` below translates it at the spawn boundary.
# Just the model's name (Wes: "remove the extra text like 'most capable' and
# whatnot. Just let it be Opus 4.8, etc.") - the blurbs made every picker as
# wide as its longest sales pitch.
MODEL_CHOICES: list[tuple[str, str]] = [
    ("opus", "Opus 5"),
    ("fable", "Fable 5"),
    ("sonnet", "Sonnet 5"),
    ("haiku", "Haiku 4.5"),
]
MODEL_VALUES = [value for value, _ in MODEL_CHOICES]
DEFAULT_MODEL = "opus"

# The string actually handed to `claude --model` for each internal alias. The
# CLI's own short aliases MOSTLY track the current model, but CLI 2.1.215's
# `opus` alias still resolves to claude-opus-4-8, not Opus 5 (verified live
# 2026-07-25: `--model opus` bills claude-opus-4-8, `--model claude-opus-5`
# bills claude-opus-5). Wes wants Opus 5 to replace 4.8 (same cost), so opus is
# pinned to the explicit id. sonnet/haiku/fable resolve correctly through their
# alias and pass through unchanged. Drop the opus entry once the CLI's alias
# catches up (then it auto-tracks the next Opus again).
CLI_MODEL_IDS: dict[str, str] = {
    "opus": "claude-opus-5",
}


def cli_model(alias: str) -> str:
    """Translate a portal model alias to the value handed to `claude --model`.

    Unknown aliases (and the ones whose CLI alias is already current) pass
    through unchanged, so this is safe to wrap every `--model` argument with.
    """
    return CLI_MODEL_IDS.get(alias, alias)
# Model for a research burst. Deliberately not the ordinary default: a burst
# only runs on allowance that is about to evaporate, so it is the one place
# where reaching for the newest, most expensive model costs nothing real.
RESEARCH_MODEL = "fable"

# Highest `max_parallel_runs` the settings form will accept. Each in-flight run
# is a `claude -p` subprocess against one Max subscription, so the real limit is
# rate limits rather than this box - it exists to stop a typo from launching
# sixty agents, not because sixteen is known to work.
MAX_PARALLEL_LIMIT = 16
# Default model for the Telegram natural-language intent router. Overridable in
# Settings (`telegram_model`) and from the chat itself with `/model`.
TELEGRAM_MODEL = "sonnet"
# Retained name for the router default; see `nl.router_model()` for the value
# actually used at call time.
ROUTER_MODEL = TELEGRAM_MODEL
# Default model for a read-only "ask a question about this project" (app/ask.py).
# An ask is judged on how fast a straight answer arrives, not on capability.
ASK_MODEL = "sonnet"
# Default model for the read-only self-review pass that critiques a run's own
# diff before the work surfaces for Wes to review (app/selfreview.py). Judging a
# diff against its own claims is a capability task more than a speed one, but it
# runs on the critical path of every review-bound run, so a mid-tier default.
SELF_REVIEW_MODEL = "sonnet"

# Default settings (used to seed the settings table) -------------------------
DEFAULT_SETTINGS: dict[str, str] = {
    "worker_enabled": "1",
    "worker_model": DEFAULT_MODEL,
    "worker_interval_min": "10",
    "max_runs_per_day": "8",
    # Agent runs allowed in flight at once, always on different projects. 1 is
    # the old strictly-serial behaviour; the ceiling is MAX_PARALLEL_LIMIT.
    "max_parallel_runs": "2",
    # A one-day-only boost on top of max_runs_per_day. `bonus_runs_date` is the
    # portal-day the boost applies to, so it expires by itself at the reset.
    "bonus_runs_count": "0",
    "bonus_runs_date": "",
    # Hour (local time) at which everything daily rolls over - see app/daycycle.py.
    "day_reset_hour": "5",
    "run_timeout_min": "30",
    # A hard per-run dollar ceiling passed to the CLI as --max-budget-usd.
    # Subscription runs bill $0 so this normally never fires; it is a backstop
    # for a leaked API key. Blank means no ceiling. See app/agent_runner.py.
    "run_max_budget_usd": "",
    # Memory ceiling for one run's cgroup ("6G", "512M", ...). Blank means a
    # fraction of this machine's RAM; "0" or "off" disables the cap. A runaway
    # tool inside a run used to OOM the whole box and take the portal with it.
    # See app/runlimit.py.
    "run_memory_max": "",
    # Percentage of a real Claude window at which scheduled runs stop, so the
    # portal stops just short of the wall instead of discovering it mid-run.
    # Manual runs ignore it. See app/pacing.py.
    "limit_hold_percent": "90",
    # Agents may triage and plan any project on their own, but moving one into
    # `building` - i.e. writing code - waits for Wes. Turn this off to let a
    # finished plan roll straight into a build.
    "require_build_approval": "1",
    "telegram_token": "",
    "telegram_chat_id": "",
    "telegram_natural_language": "1",
    # The model that reads Telegram messages and works out what Wes meant. It
    # is deliberately separate from worker_model: this runs on every message
    # and wants to be fast, while the agent model wants to be capable.
    "telegram_model": TELEGRAM_MODEL,
    # The model that answers a read-only ask (the "just asking" box on a
    # project page, and `/btw` over Telegram).
    "ask_model": ASK_MODEL,
    # Before a run's work surfaces for review, a read-only critic checks the
    # committed diff against the run's own claims and the project's todos, and
    # holds the work on the active shelf (with concrete gaps journalled) if it
    # is not actually done - so Wes's review queue only sees finished work. On
    # by default. See app/selfreview.py.
    "self_review": "1",
    "self_review_model": SELF_REVIEW_MODEL,
    # While a spend-down is running, a run pinned to a cheaper model is routed
    # up to this one, so an expiring weekly window is spent on quality rather
    # than only on more runs. See app/pacing.py:route_for_spend.
    "spend_down_model": DEFAULT_MODEL,
    # Dashboard project ordering; see PROJECT_SORTS.
    "dashboard_sort": "priority",
    # Whether the per-project priority number exists at all.
    #
    # Wes, 2026-07-27: "priority - I don't think I use it, and might want to
    # remove it. I think maybe it would be more useful in cases where someone
    # has less tokens to burn through and wants to keep less things running in
    # parallel... At very least, maybe let's add in an option in settings to
    # hide the priority." He is right about his own install: 28 of his 30
    # projects sit at 0.
    #
    # Off does NOT mean "hide a lever that is still pulling". Priority is the
    # first key in every ORDER BY the scheduler and the dashboard use, so a
    # hidden 6 would go on front-running the queue with nothing on screen to
    # explain it - the silent-behaviour failure this project keeps refusing to
    # ship. Off therefore drops it from the ordering too, and the queue falls
    # back to least-recently-touched. See db.project_order().
    "show_priority": "1",
    "glados_mode": "1",
    # Seeded from the site config, so an install whose ntfy lives elsewhere
    # starts correct instead of needing a visit to /settings. Once seeded these
    # are ordinary settings rows and the UI owns them.
    "ntfy_url": SITE.ntfy_url,
    "ntfy_topic": SITE.ntfy_topic,
    "backoff_until": "",
    "last_reflect_date": "",
    # learnings.md is injected into every run's prompt, so it must not grow
    # without bound. Past this many lines the portal auto-runs a compaction the
    # next quiet day-boundary (snapshotting first), and /memory shows a nag.
    # See app/worker.py _maybe_compact. 0 disables the automatic trigger.
    "learnings_cap_lines": "200",
    "last_auto_compact_date": "",
    # Appearance: how much of the CRT treatment to apply (see APPEARANCE below).
    "crt_scanlines": "all",
    "crt_glow": "prose",
    "crt_animations": "on",
    "ui_font": "mono",
    "ui_density": "comfortable",
}

# Appearance ------------------------------------------------------------------
# The retro-CRT styling is charming on chrome and tiring on things you have to
# read or type into, so each layer is independently adjustable rather than one
# all-or-nothing switch. Values become classes on <body>.
APPEARANCE_CHOICES: dict[str, list[tuple[str, str]]] = {
    "crt_scanlines": [
        ("all", "everywhere"),
        ("chrome", "frame + console only (not on text you read)"),
        ("off", "off"),
    ],
    "crt_glow": [
        ("all", "everywhere"),
        ("prose", "text only (crisp inputs, buttons and tables)"),
        ("off", "off"),
    ],
    "crt_animations": [
        ("on", "on"),
        ("off", "off (no pulsing, blinking or fades)"),
    ],
    # Typography is the other half of "how tiring is this to read". Full
    # monospace is the terminal look; hybrid keeps the chrome monospaced and
    # switches prose and form fields to a proportional face, which is easier
    # on long journal entries without losing the aesthetic.
    "ui_font": [
        ("mono", "monospace everywhere (terminal)"),
        ("hybrid", "hybrid - mono chrome, proportional prose + text fields"),
        ("sans", "proportional everywhere (chrome stays mono)"),
    ],
    "ui_density": [
        ("comfortable", "comfortable"),
        ("compact", "compact (tighter rows and cards)"),
    ],
}
# Class prefix on <body> for each appearance setting, e.g. crt_scanlines=off
# becomes `scan-off`. Keeping this beside the choices means adding an option
# never needs an edit in main.py.
APPEARANCE_CLASS_PREFIX: dict[str, str] = {
    "crt_scanlines": "scan",
    "crt_glow": "glow",
    "crt_animations": "anim",
    "ui_font": "font",
    "ui_density": "density",
}
APPEARANCE_DEFAULTS = {key: choices[0][0] for key, choices in APPEARANCE_CHOICES.items()}
# The shipped default for glow is the middle option, not the first one.
APPEARANCE_DEFAULTS["crt_glow"] = "prose"

# Dashboard sorting -----------------------------------------------------------
# name -> (label, SQL ORDER BY). The SQL is interpolated into the query, so this
# dict is the allowlist: a sort name that isn't a key here never reaches SQLite.
# Every order ends in `id DESC`: timestamps here have whole-second resolution,
# so two projects touched in the same second would otherwise come back in
# whatever order SQLite felt like, and the dashboard would reshuffle on reload.
PROJECT_SORTS: dict[str, tuple[str, str]] = {
    "priority": ("priority, then recent", "priority DESC, updated_at DESC, id DESC"),
    "recent": ("recently updated", "updated_at DESC, id DESC"),
    "created": ("newest first", "id DESC"),
    "title": ("title a-z", "title COLLATE NOCASE ASC, id DESC"),
}
DEFAULT_PROJECT_SORT = "priority"

# Stages / kinds ---------------------------------------------------------------
# The redesigned state model (docs/state-model.md, approved by Wes 2026-07-22):
# one user-owned `stage`, an orthogonal `paused` timestamp only Wes sets, and
# agent-reported facts (`build_requested`, `blocked_on`) stored beside it.
# Everything else - whose turn it is, which shelf a card sits on - is derived
# per render rather than stored. Stored name = displayed name.
PROJECT_STAGES = ["backlog", "active", "review", "done", "abandoned"]
DONE_STAGES = ["done", "abandoned"]
# Every stage that is not finished - what the Telegram router should offer as
# targets, and what counts as "the projects in play".
OPEN_STAGES = ["backlog", "active", "review"]

# What the picker, the drag zones and the context menu offer. `paused` is a
# pseudo-state: choosing it stamps the paused flag without touching the stage,
# and choosing anything else clears the flag.
USER_STATE_CHOICES: list[tuple[str, str]] = [
    ("backlog", "backlog"),
    ("active", "active"),
    ("paused", "paused"),
    ("review", "review"),
    ("done", "done"),
    ("abandoned", "abandoned"),
]
USER_STATES = [value for value, _ in USER_STATE_CHOICES]

# The pre-redesign vocabulary, accepted forever: it is baked into every agent
# report ever produced, old bookmarks, and any run that was in flight when the
# model changed. Maps each old status to the stage it means; the side facts
# (waiting_user's pause-or-blocked split, building's approval) are handled by
# the writers, not this table.
LEGACY_STATUS_STAGE = {
    "inbox": "backlog",
    "planning": "active",
    "building": "active",
    "needs_input": "active",
    "waiting_user": "paused",
    "review": "review",
    "done": "done",
    "abandoned": "abandoned",
}


def normalize_state(value: str) -> str | None:
    """A user-chosen state (picker, drag, Telegram, old URL) normalised to the
    new vocabulary, or None if it is not a state at all. `waiting_user` comes
    back as the `paused` pseudo-state because that is what choosing it meant."""
    value = (value or "").strip()
    if value in USER_STATES:
        return value
    return LEGACY_STATUS_STAGE.get(value)


def status_badge(state: str) -> str:
    """Badge text for a stage or display state. The stored names ARE the
    displayed names now; this survives only for underscores in legacy text."""
    return (state or "").replace("_", " ")


def status_choices(current: str) -> list[tuple[str, str]]:
    """The picker options for a project currently in `current` (a display
    state). Every display state is a choice, so nothing needs splicing in."""
    return list(USER_STATE_CHOICES)
PROJECT_KINDS = ["software", "hardware", "mixed", "unknown"]

JOURNAL_AUTHORS = ["user", "agent", "system"]
JOURNAL_KINDS = ["note", "progress", "plan", "question", "answer", "status", "reflect"]

QUESTION_STATUSES = ["open", "answered", "dismissed"]
RUN_STATUSES = ["running", "ok", "error", "timeout", "cancelled"]
SUGGESTION_STATUSES = ["proposed", "accepted", "dismissed"]

# The long-standing spellings the rest of the tree already imports, kept as
# aliases onto SITE so there is one place that knows the difference.
OWNER = SITE.owner
HOST_LABEL = SITE.host
SSH_USER = SITE.ssh_user
PORT = SITE.port
PREVIEW_PORT = SITE.preview_port
PREVIEW_HTTPS_PORT = SITE.preview_https_port


def ssh_command(slug: str) -> str:
    """The command that drops Wes into a shell already `cd`'d into a project.

    A web page cannot launch a terminal - that was the whole question behind
    this feature - so the portal hands over the exact line to paste instead
    (Wes picked that over an ssh:// handler or an embedded web shell). `-t`
    forces a TTY, and `exec bash -l` replaces the shell so the session is a
    normal login shell rather than a nested one that exits oddly.
    """
    workspace = PROJECTS_DIR / slug
    return f"ssh {SSH_USER}@{HOST_LABEL} -t 'cd \"{workspace}\" && exec bash -l'"


# The Claude CLI version, and a User-Agent that matches the real client.
# -----------------------------------------------------------------------------
# Anthropic's usage endpoint sorts requests carrying an unrecognised User-Agent
# into a punitive rate-limit bucket (hours-long 429s), so the portal's usage
# poller must present itself as the genuine CLI - `claude-cli/<version>
# (external, cli)` - rather than a made-up name. The version is discovered once
# from `claude --version` and cached; the constant below is only a fallback for
# when the CLI is somehow not on PATH.
DEFAULT_CLI_VERSION = "2.1.215"
_cli_version_cache: str | None = None


def cli_version() -> str:
    """The installed Claude CLI version, e.g. "2.1.215", discovered once.

    Never raises: any failure (no CLI on PATH, unparseable output, timeout)
    falls back to DEFAULT_CLI_VERSION. A stale-but-real version is still far
    better than a generic User-Agent, which is the failure this guards against.
    """
    global _cli_version_cache
    if _cli_version_cache is not None:
        return _cli_version_cache
    version = DEFAULT_CLI_VERSION
    try:
        out = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # "2.1.215 (Claude Code)" -> "2.1.215"
        token = (out.stdout or "").strip().split()[0] if out.stdout else ""
        if re.match(r"^\d+\.\d+", token):
            version = token
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    _cli_version_cache = version
    return version


def usage_user_agent() -> str:
    """The User-Agent the usage poller must send to look like the real client."""
    return f"claude-cli/{cli_version()} (external, cli)"


def cli_projects_dir() -> Path:
    """Where the Claude CLI keeps its per-workspace state, including the
    auto-memory files (`<encoded-cwd>/memory/*.md`) the portal surfaces on
    /memory. A function, not a constant, so a test can point it elsewhere by
    monkeypatching this module's `Path.home` isn't needed - callers pass an
    explicit root - but the default lives here in one place."""
    return Path.home() / ".claude" / "projects"

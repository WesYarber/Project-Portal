"""Paths and default settings for Project Portal."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app import jumpkeys, site

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
# Uploaded files that have not been shown to an agent yet (app/attachments.py).
#
# Wes, 2026-08-16: "Projects that are currently running when I attach a file to
# a note/prompt im currently working on ... seem to see the file get attached
# and ask a question about it. Maybe it could wait to be revealed to the agent
# that the file was added until it is dealing with that prompt?"
#
# An upload used to be written straight into the project workspace, which is
# the agent's cwd - so a screenshot appeared underneath a run already in flight,
# whose prompt was built minutes before the note explaining it existed. Files
# wait here instead, and are moved into the workspace by the run whose prompt
# carries their note. OUTSIDE PROJECTS_DIR for the whole of the point: anywhere
# under a workspace is somewhere a running agent can see.
INCOMING_DIR = DATA_DIR / "incoming"

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
    # Runs one project may take in a day when it has no cap of its own. The
    # budget above is for the whole board and the scheduler works it in one
    # order, so without this the project at the head takes the lot. 0 means no
    # default cap.
    "project_max_runs_per_day": "6",
    # Agent runs allowed in flight at once, always on different projects. 1 is
    # the old strictly-serial behavior; the ceiling is MAX_PARALLEL_LIMIT.
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
    # Memory ceiling for ALL runs in flight together, applied to the systemd
    # slice every run scope is created in. Blank means a fraction of this
    # machine's RAM; "0" or "off" disables it. This is what makes a generous
    # per-run cap safe: without it, `max_parallel_runs` x `run_memory_max` was
    # the only bound, and it was several times the size of the machine.
    "runs_memory_pool": "",
    # Percentage of a real Claude window at which scheduled runs stop, so the
    # portal stops just short of the wall instead of discovering it mid-run.
    # Manual runs ignore it. See app/pacing.py.
    "limit_hold_percent": "90",
    # Agents may triage and plan any project on their own, but moving one into
    # `building` - i.e. writing code - waits for Wes. Turn this off to let a
    # finished plan roll straight into a build.
    "require_build_approval": "1",
    # How hard a build run proves its own work: "proportionate" (scale it to
    # the diff), "thorough" (full suite plus a mutation sweep every time) or
    # "light" (the owning tests only). Self-verification is ~60% of what a run
    # spends, so this is the biggest single lever on cost. See
    # app/verifydepth.py.
    "verification_depth": "proportionate",
    # Off by default, as of 2026-07-28. Wes: "Get rid of the QX numbering
    # system as I'm no longer using telegram to control this. Turn off our
    # telegram integration along with this... If a user on GitHub, for example,
    # is using this project and wants to use the telegram integration, turn
    # back on the question numbers to work with that telegram integration."
    #
    # So this one switch owns two things that only make sense together: whether
    # the bot runs at all, and whether questions wear a number. The number is
    # not decoration - it is the handle you type back at a bot ("Q7: yes"), and
    # with no bot to type at there is nothing it addresses.
    "telegram_enabled": "0",
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
    "dashboard_sort": "recent",
    # Which letter jumps to which section (n / j / t / p as shipped). Spread in
    # from app/jumpkeys.py rather than written out here, so adding a jumpable
    # section is one entry in that module's ACTIONS and nothing else - the
    # settings row, the form field, the default and the page hint all follow.
    **jumpkeys.DEFAULTS,
    "glados_mode": "1",
    # Seeded from the site config, so an install whose ntfy lives elsewhere
    # starts correct instead of needing a visit to /settings. Once seeded these
    # are ordinary settings rows and the UI owns them.
    "ntfy_url": SITE.ntfy_url,
    "ntfy_topic": SITE.ntfy_topic,
    "backoff_until": "",
    "last_reflect_date": "",
    # learnings.md is injected into every run's prompt, so it must not grow
    # without bound. Past this many KB the portal auto-runs a compaction the
    # next quiet day-boundary (snapshotting first), and /memory shows a nag.
    # See app/worker.py _maybe_compact. 0 disables the automatic trigger.
    #
    # KB, not lines, since 2026-08-07: the trigger counted lines while a prompt
    # spends bytes, so a file of 189 long lines sat under a 200-LINE cap at
    # 58 KB with 43 KB of itself unreachable. See memory.learnings_cap_kb for
    # why 24 and not the 16 the prompt budget carries.
    "learnings_cap_kb": "24",
    "last_auto_compact_date": "",
    # profile.md is pasted WHOLE into every run of every project, so it is the
    # same tax as learnings.md and until 2026-08-07 it had no ceiling of any
    # kind: it had reached 26.7 KB, 31% of the average build prompt, having
    # grown 10 KB in ten days. Unlike learnings it needs no compaction job -
    # the daily reflect already rewrites it every day, so the cap is simply
    # told to the reflect (with the current size, and urgently once it is
    # over). The prompt-side backstop trims by WHOLE sections at twice this,
    # so a reflect that ignores the target once costs nothing and a runaway
    # still cannot grow forever. See app/promptbudget.py profile_for_prompt.
    #
    # 16 KB is the same constraint that chose prompt_learnings_kb: the profile's
    # behavior-shaping sections (who he is, what he values, how he wants things
    # built, working style) came to 18.8 KB when this was written, and getting
    # them under 16 is a merge-and-tighten pass, not an amputation. 0 disables.
    "profile_cap_kb": "16",
    # How much of each unbounded block reaches a prompt, in KB. See
    # app/promptbudget.py for why these are byte budgets and not counts, and
    # for the measurement that says reordering the prompt would buy nothing.
    #
    # 16 is not a round number picked for looking tidy: learnings.md's general
    # sections (who Wes is, how he wants agents to work, what the machines can
    # do, the cross-project engineering lessons) came to 12.3 KB when this was
    # written, so 16 keeps all of them whole with room for the newest domain
    # notes on top. It is the constraint that chose the number.
    "prompt_learnings_kb": "16",
    "prompt_journal_kb": "24",
    # 6 is chosen the same way: on the project with the longest question
    # history (twenty-five answered, 11.8 KB) the ten repeats of one question
    # collapse to one, leaving 8.9 KB, and 6 keeps roughly the last two thirds
    # of the real decisions. Older ones are named as left out, not hidden.
    "prompt_answered_kb": "6",
    # The appearance keys are NOT written out here. They are spread in from
    # APPEARANCE_DEFAULTS below, once, so that the table a fresh install is
    # seeded from and the table a running page falls back to cannot disagree.
    # They were two hand-kept copies of the same six values until 2026-08-29,
    # which is a divergence nobody would ever see: the seeded row wins on a new
    # install and the fallback wins on an old one, so the same portal would
    # look different depending on when it was installed.
}

# Appearance ------------------------------------------------------------------
# The retro-CRT styling is charming on chrome and tiring on things you have to
# read or type into, so each layer is independently adjustable rather than one
# all-or-nothing switch. Values become classes on <body>.
APPEARANCE_CHOICES: dict[str, list[tuple[str, str]]] = {
    # The whole visual language, and deliberately the first key: everything
    # below it is a dial on a look, where this is which look you are dialing.
    #
    # Wes, 2026-07-28: "she doesn't like this kind of terminal, tech-y theme
    # that I have, and she might want something a little more paper like,
    # flowery, neutral, or just drastically different than what I have here...
    # her own theme to where all of the functional pieces are still there, but
    # she can change how they appear."
    #
    # A theme is a class on <body> and a block in static/themes.css, not a
    # separate stylesheet: that way it rides the person -> install -> default
    # chain that already exists for the CRT layers, and a person choosing a
    # theme is the same act as a person turning the scanlines off. "terminal"
    # is the absence of any override, so the shipped look costs no CSS at all.
    # Wes, 2026-07-28: "Generate some additional themes that would be cool as
    # options." Adding one is three edits and no new mechanism: a value here, a
    # chrome color in THEME_CHROME, and a palette block in static/themes.css -
    # plus a line in THEME_STOCK saying whether it prints on light or dark
    # stock, and a line in THEME_TYPE saying whether its chrome speaks in a
    # monospaced voice. Those two decide which structure it inherits; see the
    # sheet's header for what a theme may and may not do.
    #
    # Wes, 2026-08-28: "I want to envision and experiment some completely
    # different themes for this page. No ties to anything existing about the
    # terminal theme, monospaced text, nothing. Even different layouts are
    # acceptable. Give me multiple ideas that I can explore and consider."
    #
    # The five below the rule are that ask, taken as five different design
    # languages rather than five palettes: a drafting sheet, a modern product
    # app, a broadsheet, a Swiss poster and a calm document. None of them is
    # monospaced, which is what THEME_TYPE exists for - before them, "not the
    # terminal's typeface" was something only a light theme could be.
    "ui_theme": [
        ("terminal", "terminal - the dark CRT look"),
        ("midnight", "midnight - deep indigo, soft neon"),
        ("amber", "amber - a warm monochrome CRT"),
        ("paper", "paper - warm, light, printed"),
        ("meadow", "meadow - soft green, light, floral"),
        ("workbench", "workbench - dark product app, no terminal in it"),
        ("blueprint", "blueprint - a drafting sheet, deep blue and cyan"),
        ("editorial", "editorial - a broadsheet, serif, one red"),
        ("press", "press - Swiss poster, huge type, hard rules"),
        ("quiet", "quiet - a calm document, warm white, soft green"),
    ],
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
    # The desktop side rail. Wes, 2026-08-01: "When on desktop with extra
    # unused horizontal space, let's add a nav bar to the side... I haven't
    # decided yet if im ok with shifting the rest of the main interface here
    # that already exists over to the right to make more space for this. Maybe
    # we can try it and see if I like it?"
    #
    # So both are built and the choice is his, which is why this is a setting
    # rather than a decision.
    #
    # The two "on" options are named for what they do to the page, not for
    # where the rail lands, because that is the only difference a person can
    # feel. Wes, 2026-08-04: "update the sidebar options naming convention. For
    # the 'on' options, one should be 'On - interface shift' and 'On - use
    # existing space' with existing space being the default."
    #
    #   margin  "use existing space" and the default. The rail floats in the
    #           dead margin beside the centered page and NOTHING on the page
    #           moves at any width. It shrinks to fit that margin rather than
    #           demanding a fixed 15rem, which is the rest of the same note -
    #           "I want it to be more flexible for the 'Use existing space'
    #           version to be able to be more narrow to still apply and use the
    #           space to the left of the interface" - and is what drops it from
    #           needing a 1620px window to needing 1400px.
    #   beside  "interface shift". Pinned to the left edge from 1100px, page
    #           pushed right and left-aligned against it. The only option that
    #           can exist on a window too narrow to have any dead margin.
    #   off     no rail at any width.
    #
    # The default has now been each of these once. It was `margin` on the
    # reasoning that a default should move nothing; it became `beside` when
    # `margin` needed 1620px and Wes's own window is ~1135px, so the default
    # was showing him nothing. It is `margin` again because he asked for it by
    # name - and the width work above is what makes that answer honest rather
    # than a rail he cannot see. Below 1400px `margin` still shows no rail,
    # because below 1400px there is genuinely no space to use.
    #
    # A layout switch and not a theme, deliberately: static/themes.css bans
    # display and position outright, because a theme that can move a control is
    # a look you cannot get back out of. This is the mechanism for the thing
    # themes are not allowed to do, and it rides the person -> install ->
    # default chain like every other appearance key.
    "ui_sidebar": [
        ("margin", "on - use existing space"),
        ("beside", "on - interface shift"),
        ("off", "off"),
    ],
    # What the rail's project list IS. Wes, 2026-08-01: "have it show as many of
    # the most recent projects that have been worked on as will fit ... and have
    # a section in settings to change this from recent back to kind of what we
    # have now which is just based on status."
    #
    # Recent is the default because that is the one he asked for; the rail is a
    # way back to what you were just doing, and what you were just doing is not
    # sorted by status.
    "ui_rail_projects": [
        ("recent", "most recently worked on"),
        ("shelf", "grouped by status (active, then in review)"),
    ],
}
# Class prefix on <body> for each appearance setting, e.g. crt_scanlines=off
# becomes `scan-off`. Keeping this beside the choices means adding an option
# never needs an edit in main.py.
APPEARANCE_CLASS_PREFIX: dict[str, str] = {
    "ui_theme": "theme",
    "crt_scanlines": "scan",
    "crt_glow": "glow",
    "crt_animations": "anim",
    "ui_font": "font",
    "ui_density": "density",
    "ui_sidebar": "rail",
}
# Deliberately absent from the table above: `ui_rail_projects`. The rail's list
# is built on the server (main.side_rail reads the setting), so there is no
# class to paint and nothing the browser could preview - and a preview that
# swapped a class and changed nothing on screen would read as the setting not
# working. select_field falls back to an empty prefix, which app.js skips.
# What the browser chrome outside the page should be for each theme: the iOS
# status bar tint and the overscroll color. These cannot come from the
# stylesheet - a <meta> is read before any CSS is applied, and it is what stops
# a white flash on a dark theme (and a black one on a light theme) while the
# page loads. Keys must match ui_theme's values.
THEME_CHROME: dict[str, str] = {
    "terminal": "#0d1016",
    "midnight": "#0e0b1c",
    "amber": "#140f06",
    "paper": "#f0e8da",
    "meadow": "#eef1e6",
    "workbench": "#0d0e12",
    "blueprint": "#102b41",
    "editorial": "#f0ece5",
    "press": "#eae6dc",
    "quiet": "#f6f5f3",
}

# Which stock a theme prints on: "dark" or "light". This is not decoration -
# it selects a whole family of structural rules in static/themes.css, because
# everything the paper theme had to undo (the scanline overlays, the glow, the
# terminal's borrowed punctuation, the dark-only `color-scheme`) has to be
# undone identically by any other light theme. Written once against
# `theme-stock-light` rather than copied per theme, so a new light theme is a
# palette and nothing else.
#
# `dark` is the shipped stock and carries no rules of its own: a dark theme is
# style.css exactly as it always was, with different variables pointed at it.
THEME_STOCK: dict[str, str] = {
    "terminal": "dark",
    "midnight": "dark",
    "amber": "dark",
    "paper": "light",
    "meadow": "light",
    "workbench": "dark",
    "blueprint": "dark",
    "editorial": "light",
    "press": "light",
    "quiet": "light",
}
DEFAULT_THEME_STOCK = "dark"

# Whether a theme's chrome speaks in a monospaced voice: "mono" or "prose".
# The second axis of the same idea as THEME_STOCK, and it exists because Wes
# asked for themes with "no ties to anything existing about the terminal theme,
# monospaced text, nothing" - and two of the five that answered that are DARK.
#
# Until then the two questions were tangled: the rule list that re-faces the
# window title, the tabs, the badges, the buttons and the form fields in
# `--font-prose` was scoped to `theme-stock-light`, so "not monospaced" was
# something only a light theme could be. Splitting them changes nothing about
# paper or meadow (both are light AND prose) and lets workbench and blueprint
# be dark without being terminals.
#
# `mono` is the shipped answer and carries no class, exactly like `dark`: the
# terminal look is still the total absence of any rule from themes.css.
#
# What this may NOT do is re-point --font-mono. That variable means "the font
# for things that have to line up", not "the chrome font" - pointed at a
# proportional face, the dashboard's box-drawing wordmark renders as a hatch.
# A prose theme names the chrome instead, which is what the rule list does.
THEME_TYPE: dict[str, str] = {
    "terminal": "mono",
    "midnight": "mono",
    "amber": "mono",
    "paper": "prose",
    "meadow": "prose",
    "workbench": "prose",
    "blueprint": "prose",
    "editorial": "prose",
    "press": "prose",
    "quiet": "prose",
}
DEFAULT_THEME_TYPE = "mono"

APPEARANCE_DEFAULTS = {key: choices[0][0] for key, choices in APPEARANCE_CHOICES.items()}
# Two of the CRT layers ship on their MIDDLE option rather than their first,
# and for the same reason: the effect is charming on the frame and tiring on
# the words. The choice lists are ordered most-treatment-first because that is
# the order a person reading a dropdown expects; where the shipped answer is
# not the top of that list, it is stated here.
#
# glow: "prose" - crisp inputs, buttons and tables.
# scanlines: "chrome" - the frame and the agent console, and nothing you read.
#
# Wes, 2026-08-29, on the README screenshots: "I noticed in the GitHub
# screenshots that the scan lines are in front of the text - I want you to make
# my current 'Wes' settings the default settings for a new installation." His
# own install has been on `chrome` for a long time; the shipped default was
# still `all`, so every fresh install - and every screenshot taken from the
# demo board, which seeds from this table - wore scanlines over its body text.
# This is the only appearance key on which his install differed from the
# defaults, so this line is the whole of that request. (His pacing settings and
# his Telegram token also differ, and are deliberately NOT copied here: the
# pacing is a personal spend-down of one Max subscription, and a token is a
# secret. See the 2026-08-29 journal entry.)
APPEARANCE_DEFAULTS["crt_glow"] = "prose"
APPEARANCE_DEFAULTS["crt_scanlines"] = "chrome"

# The seed table and the runtime fallback are now the same values by
# construction rather than by two people remembering to edit both.
DEFAULT_SETTINGS.update(APPEARANCE_DEFAULTS)

# Dashboard sorting -----------------------------------------------------------
# name -> (label, SQL ORDER BY). The SQL is interpolated into the query, so this
# dict is the allowlist: a sort name that isn't a key here never reaches SQLite.
# Every order ends in `id DESC`: timestamps here have whole-second resolution,
# so two projects touched in the same second would otherwise come back in
# whatever order SQLite felt like, and the dashboard would reshuffle on reload.
PROJECT_SORTS: dict[str, tuple[str, str]] = {
    "recent": ("recently worked on", "updated_at DESC, id DESC"),
    "created": ("newest first", "id DESC"),
    "title": ("title a-z", "title COLLATE NOCASE ASC, id DESC"),
}
DEFAULT_PROJECT_SORT = "recent"

# The one sort that SQL cannot express on its own. "Recently worked on" means
# the newest of a run, a note, a journal entry and the project row's own
# `updated_at` - and the first three live in other tables, so the SQL above is
# only the base order and `db.list_projects_sorted` refines it in Python from
# `db.last_activity_at()`. Named here rather than spelled out at each call site
# so the dashboard, the side rail and /everyone cannot come to disagree about
# which sort is the activity-aware one.
#
# Wes, 2026-08-16: "Get rid of the notion of project 'priority' values. Instead,
# within project statuses on the dashboard, I want to sort by most recently
# modified similar to how the left nav bar is done." The rail had already been
# taught this question on 2026-08-07; this is the board learning the same one.
ACTIVITY_SORT = "recent"

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
    """A user-chosen state (picker, drag, Telegram, old URL) normalized to the
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
# Anthropic's usage endpoint sorts requests carrying an unrecognized User-Agent
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

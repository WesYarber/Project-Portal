"""Builds prompts and runs the `claude -p` CLI headlessly for a single task."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Awaitable, Callable, Optional

from app import (
    attachments, config, db, limits, memory, notes, orphans, people, qdedupe, runlimit,
    runlog, spawnauth, subprojects, todos,
)

log = logging.getLogger("portal.agent_runner")

# `$HOST`, `$BASE_URL`, `$OWNER` and the pronouns below are filled in from this
# installation's site config (app/site.py) at import, so the contract names
# neither a machine nor a person. Substitution is `string.Template`-style on
# purpose: the contract is mostly a JSON shape, and `{}`-style formatting would
# fight every brace in it.
_AGENT_CONTRACT_TEMPLATE = """
# Agent contract

You are an autonomous agent working on behalf of $OWNER inside a per-project
workspace. You work ONLY inside your current working directory (this
workspace) - never touch files outside it.

Any URL or command you show $OWNER must use a hostname $THEY can reach from
another device - $THEY will never be on the machine your code runs on. Write
`$HOST` (e.g. `$BASE_URL`), never `localhost` or
`127.0.0.1`, in summaries, journal entries, questions and preview_url alike.

Write **American English**, everywhere: in what you say to $OWNER, in the UI you
build, and in code, comments and test names alike. "color" not "colour", "gray"
not "grey", "behavior", "recognize", "center", "labeled". $OWNER has asked for
this more than once, so treat a British spelling as a defect and fix it on any
line you are already touching.

Before you finish, you MUST deliver a report by calling the **StructuredOutput**
tool with EXACTLY this JSON shape - the harness provides that tool and
validates your report as you submit it. If no StructuredOutput tool is
available in your session, write the same JSON to a file at
`.portal/report.json` instead (creating the `.portal/` directory if needed):

{
  "summary": ["what changed, one concrete bullet each - see below"],
  "journal_entry_md": "markdown progress entry to append to the project journal",
  "new_stage": "'review' when the work is finished and ready for $OWNER to look at, 'active' from a triage pass promoting a backlog idea, else null",
  "request_build": false,
  "blocked_on": "one line naming what only $OWNER can do that blocks you, or null",
  "kind": "one of software|hardware|mixed, or null to keep the current kind",
  "title": "an improved short project title, or null to keep the current title",
  "description": "a rewritten description of what this project now IS (1-3 sentences), or null to keep the current one",
  "questions": [{"question": "...", "context": "...", "options": ["yes", "no"]}],
  "todo_updates": {"add": [{"text": "...", "owner": "agent|user", "tags": []}], "done": [12, 15], "tags": {"14": ["blocked"]}},
  "subprojects": {"add": [{"title": "...", "description": "...", "kind": "software"}]},
  "preview_url": "http://... where what you built can be viewed, or null",
  "learnings": ["short factual bullet(s) learned about $OWNER or $THEIR preferences"],
  "suggestion": null
}

- "summary": 1-4 bullets, shown to $OWNER at the top of the project page as the
  "since you last looked" banner. This is often the ONLY part of your report
  $THEY will read, so each bullet must say what actually changed, in $THEIR
  terms, well enough that $THEY should never have to open the journal to find
  out. Name the feature, the file or the behavior. Counting your own output
  is not a summary:
  "two commits", "two todo items shipped", "ten items cleared off the list" and
  anything opening with "Done." are all worthless to $THEM and are not acceptable.
  Write "completed todos now age out after 16 hours, with a clear button and a
  history page", not "two todo items shipped". A bullet may run to a sentence -
  fitting on one line matters far less than being specific. Do not start
  bullets with a tick, a dash or "Done."; the page adds its own marker.
  The page's marker is a green tick meaning "this shipped", so a bullet that
  is NOT a shipped change - something you did not get to, something that
  failed, a heads-up - must open with "note:" so the page marks it as a
  remark instead of a completed task. Never let a status remark wear the tick.
- "journal_entry_md": your progress entry, and it can SHOW things, not just
  say them. Markdown image syntax with a workspace-relative path renders
  inline in the journal: `![the new layout](shots/dashboard.png)`. Gifs
  animate, and the same syntax with a video or audio file
  (`![demo](demo.mp4)`) becomes a player. Use this whenever it helps -
  a screenshot of UI you built or changed, a photo, a plot, a short capture
  of a thing moving. A picture of the result is worth more to $OWNER than a
  paragraph describing it; if you took screenshots to verify your work,
  embed the best one rather than only describing what it showed. Keep the
  files in the workspace (committed, reasonably sized) or the embed breaks
  when the workspace is cleaned.
- "questions": only include items here if you are genuinely blocked on
  $OWNERS intent or a decision only $THEY can make. Otherwise use an empty list.
  Write each question so it stands alone: assume $OWNER is reading it on $THEIR
  phone without having read the rest of your report, so restate the context
  it needs even if you already explained it above.
  Never re-ask something already waiting for an answer. Any question still open
  on this project is listed above under "Questions already waiting for an
  answer"; asking one of them again in different words does not raise its
  priority, it just gives $OWNER two things to read where there was one. A
  question that matches an open one is dropped rather than filed, so a rewording
  costs you the slot without reaching $THEM.
  Asking does not mean stopping. Before you park a run on a question, check
  which todo items actually depend on the answer - work the ones that do not,
  and come back to the blocked item once $OWNER has replied.
  "options" is optional: when the useful answers are a short fixed set (yes/no,
  a choice between two designs), list them - each becomes a one-tap button
  under the question on $THEIR phone, so phrase each option as the answer itself
  ("merge it", "keep both"), at most 4, a few words each. Leave it out for
  open-ended questions; a "skip" button is always added for free.
- "todo_updates": the project's working checklist, which is shown to you in
  full every run. This is how a request survives a context window. Whenever
  $OWNER asks for something you are not finishing in this run, "add" it with
  owner "agent"; whenever something is blocked on $THEM (a purchase, a
  credential, a click), add it with owner "user". "done" takes the ids of
  items you actually completed and verified this run. All three default to
  empty. "tags" retags items: short kebab labels shown as chips on the row,
  and the given list REPLACES that item's tags, so `[]` untags. The tag
  "blocked" has teeth - a blocked item does not count as workable when the
  portal decides whether another run could make progress - so tag the items
  that truly wait on $OWNER, and clear the tag the moment one no longer does.
- "subprojects": split this project into children, each of which becomes a
  project in its own right with its own workspace, journal, todo list and runs.
  Use it when the project is really several independent deliverables that each
  need their own context - a "games for my site" project is one project per
  game. Do NOT use it for phases of a single build; those are todo items. Only
  a top-level project can be split, one level deep, and splitting moves
  nothing: the children start empty. Leave it out entirely if you are not
  splitting, which is most runs. See the Sub-projects section below.
- "preview_url": if this project serves something a browser can open and the
  portal cannot guess the address - a dev server on a port, a deployed site -
  put it here and the project page grows an "open it" button. You do NOT
  need this for a plain `index.html` in your workspace: the portal finds
  that by itself and serves it. Leave it out unless it changed.
  If that address is a server started from THIS workspace, also write
  `.portal/serve.json`: `{"cmd": "bun server.js"}` (optional "cwd", relative
  to the workspace). When $OWNER clicks "open it" and the server is down, the
  portal runs that command and waits for the address to answer - without the
  recipe the button dead-ends on a connection error. The command must start
  the server in the foreground and keep running.
- "description": keep this current. It should describe what the project is
  today, not what it was when $OWNER first typed the idea - that original text is
  preserved separately and shown to you every run. Leave it null if the current
  description is still accurate, or if the Project section below says the
  description is locked.
- "title": leave null if the Project section says the title is locked.
- "learnings": almost always an empty list. This file is read into the prompt of
  every run of every project, so a line here is a permanent tax on every future
  agent - and it is currently so full of run-by-run trivia that $OWNER says it
  buries the useful context. Only write one if it is about $OWNER (working
  style, preferences, hardware and accounts, location) or would change how an
  agent behaves on a DIFFERENT project. Never record what you did this run,
  what bug you fixed in this codebase, or how this project's code is laid out
  - that is
  what the journal and the code are for. At most 2 or 3, one plain sentence
  each, no timestamp: the portal stamps nothing, so if a date is part of the
  fact, put it in the sentence. A plain string is added, deduped, and if it is
  a fuller rephrasing of an existing line it REPLACES that line in place rather
  than piling up beside it. To retire a fact you have found to be no longer
  true, pass an object instead: {"op": "delete", "text": "the stale fact"}
  removes the closest matching line ({"op": "update"/"add", ...} force the
  other two). This is the only way to shrink the file from a normal run.
- "suggestion": null unless a genuinely good NEW project idea for $OWNER emerged
  while you worked (not the project you're working on). If so, set it to
  {"title": "...", "description": "..."}.
- "new_stage": the only stage moves you may make. "review" says the work is
  done and it is $OWNERS turn to look; "active" is how a triage pass promotes an
  idea out of the backlog. Never "done" or "abandoned" - only $OWNER finishes a
  project - and null is the right value on most runs.
- "request_build": true asks to start writing code. It is a REQUEST, not a
  decision: unless $OWNER has already approved this project for building, the
  portal records it, badges the project "needs your OK" and asks $THEM. Never
  treat writing code as pre-approved.
- "blocked_on": one short line naming the thing only $OWNER can do (a purchase, a
  credential, a click) that stops you. The portal wears it as a badge and
  clears it automatically when your next run reports, so restate it while it
  still holds. Being blocked does not stop future runs - they work whatever
  does not depend on $THEM - so also tag the specific todo items that wait on
  $THEM as "blocked", and keep an open question for anything that needs $THEIR
  answer rather than $THEIR hands.

## Knowing when to stop

Lean towards action: anything reversible inside your own workspace - writing
code, refactoring, adding tests, researching, rewriting the plan - just do,
without asking. Finish the chunk you started and commit it.

But do NOT keep going for the sake of going. Stop and hand back when:

- The next step depends on a decision only $OWNER can make (which of two designs,
  what to spend, what the thing should actually do). Ask it as a question -
  one clear question beats a guess you build on for three runs. The open
  question itself badges the project; no stage change needed.
- The next step needs something you do not have: hardware, a purchase, an
  account, a credential, physical access. Report it in "blocked_on" and say
  exactly what you need.
- You have finished what the plan called for. Set new_stage "review" rather
  than inventing more scope to fill the run.
- You are about to repeat work a previous run already did, or your last two
  runs made no real progress. Say so plainly and stop.

A run that ends early with an honest "here is what I need from you" is a good
run. A run that burns an hour guessing at $OWNERS intent is not.
"""

AGENT_CONTRACT = Template(_AGENT_CONTRACT_TEMPLATE).safe_substitute(
    **config.SITE.template_vars()
)

_TASK_GUIDANCE_TEMPLATES = {
    "triage": (
        "Task: TRIAGE. Understand the idea as written in the project description "
        "and journal. Classify its kind (software/hardware/mixed). Improve the "
        "project title if you can make it clearer/shorter. Write an initial "
        "assessment as your journal_entry_md: what the idea is, rough scope, and "
        "any obvious risks or missing info. Ask clarifying questions ONLY if you "
        "genuinely cannot proceed without $OWNERS input. Set new_stage to 'active' "
        "to move the idea out of the backlog (or leave it null if your "
        "questions must be answered first). Do NOT start building: triage names "
        "and scopes the idea, nothing more."
    ),
    "plan": (
        "Task: PLAN. Write a PLAN.md file in the workspace describing concrete "
        "steps to build this project. If the project is hardware (or mixed), "
        "also write BOM.md with specific orderable parts, estimated prices, and "
        "links (use WebSearch for this). Write the plan, do NOT start executing "
        "it. End with request_build true to ask $OWNER to start the build (the "
        "portal records the request and asks unless this project is already "
        "approved), blocked_on if it's hardware waiting on a parts order, "
        "or a question if you need a decision first. Your "
        "journal_entry_md should make the OK easy to give: what you'd build "
        "first, roughly how long, and anything $OWNER should decide now."
    ),
    "build": (
        "Task: BUILD. Execute the next concrete chunk of PLAN.md. Write real, "
        "working code with tests in the workspace. Commit your work to git with "
        "a sensible commit message. Make your journal_entry_md concrete: what "
        "you did, what you verified, and what's next. Set new_stage to "
        "'review' if you believe the project is complete and ready for $OWNER to "
        "look at; otherwise leave it null to keep building."
    ),
    "research": (
        "Task: RESEARCH BURST. This run exists because there is spare Claude "
        "allowance about to expire, so spend it on depth rather than on code. "
        "Do NOT write or change application code, and do not change the "
        "project's state - leave new_stage null and request_build false. "
        "Instead, research this "
        "project properly: use WebSearch/WebFetch heavily to find real "
        "products, prices, libraries, APIs, standards, prior art and the "
        "specific gotchas people hit doing this. Write or extend RESEARCH.md in "
        "the workspace with what you found, with links and dates, and say "
        "plainly where the evidence is thin. Commit it. Your journal_entry_md "
        "should be the findings that change what we would build, not a list of "
        "pages you read. Add todo items for anything the research says we "
        "should do differently."
    ),
    "reflect": (
        "Task: DAILY REFLECT. You are running with cwd set to the shared memory "
        "directory (not a project workspace). Read the recent cross-project "
        "journal entries and the current profile.md provided below. Rewrite "
        "profile.md (in your cwd) with an updated, well-organized picture of "
        "$OWNER: interests, skills, preferences, and patterns you notice across "
        "projects. Keep it concise and factual. You may also report (via the "
        "StructuredOutput tool, per the contract below) with just a "
        "'suggestion' field (and other fields null/empty) if a good new "
        "project idea emerged from the review."
    ),
    "compact": (
        "Task: COMPACT THE LEARNINGS. You are running with cwd set to the "
        "shared memory directory (not a project workspace). Read learnings.md "
        "in your cwd - all of it, not a tail - and REWRITE it in place, much "
        "shorter and much more useful.\n\n"
        "This file is injected into the prompt of every agent run the portal "
        "makes, so every line in it costs context on every future run. The failure "
        "mode is exact: a huge body of text that is mostly useless as learnings "
        "but adds a lot of unhelpful context to every agent, burying the lines "
        "that would actually have helped. Your job is to make what survives "
        "worth its place.\n\n"
        "KEEP, merged and generalised: durable facts about $OWNER (working style, "
        "preferences, turns of phrase, location, hardware and accounts); "
        "lessons that would change how a future agent "
        "behaves on a DIFFERENT project; hard-won environment facts that are "
        "expensive to rediscover (exact commands, addresses, quirks of "
        "$THEIR machines and tools).\n"
        "DROP: anything that is only about one project's internals and would "
        "mean nothing to an agent working elsewhere; narration of what some "
        "past run did; near-duplicates - and there are many, so merge them "
        "into one sharper line; anything already obvious from the code or the "
        "project's own journal; and self-congratulation.\n"
        "TIMESTAMPS: remove the leading [timestamp] from every line. Only keep "
        "a date inside a line when the line is about a moment in time (a price "
        "quoted, a credential rotated, a decision taken on a date).\n\n"
        "Group what remains under a handful of headings, write each as one "
        "plain sentence, and aim to end up at roughly a quarter of the length "
        "you started with - if you cannot get near that, you are keeping "
        "project trivia. Do not touch profile.md. Report (via the "
        "StructuredOutput tool, per the contract below) with a `summary` "
        "saying what you cut and what you kept, and `journal_entry_md` "
        "likewise; leave every other field null/empty."
    ),
}

# The guidance blocks name the owner too, so they take the same substitution as
# the contract. Done as a pass over the finished dict rather than by wrapping
# each literal, so the strings above stay readable prose - and the raw
# templates stay reachable, which is what lets a test render them under a
# different identity and prove no name is baked in.
TASK_GUIDANCE = {
    task: Template(text).safe_substitute(**config.SITE.template_vars())
    for task, text in _TASK_GUIDANCE_TEMPLATES.items()
}


def resolve_model(project: Optional[sqlite3.Row], task: str = "") -> str:
    """The model to run a task with: per-project override, else the global
    default from Settings, else `config.DEFAULT_MODEL` - then the usage
    fallback on top, so a model whose own weekly window is exhausted resolves
    to its stand-in (Fable -> Opus, Wes's rule) until the window resets.

    An empty/NULL `projects.model` means "inherit the global setting", which is
    what every project starts out as.

    A research burst is the exception and ignores both overrides: the whole
    point of a burst is to spend weekly allowance that is about to be lost on
    the best model available, so a project pinned to haiku for cost reasons
    still gets the research model. Cost is not what that pin is protecting
    here. The usage fallback still applies - a burst on a model with no
    window left would only buy backoffs.
    """
    # Three layers, in order: what Wes configured, then the spend-down upgrade
    # (a cheaper-pinned run routed up to burn an expiring window on quality),
    # then the usage fallback (a model whose own window is exhausted routed to
    # its stand-in). Both middle layers fail open, so the configured model runs
    # as asked whenever there is no spend-down and no usage pressure.
    from app import pacing  # local import: pacing imports config/db/limits, not this module

    routed, spend_why = pacing.route_for_spend(configured_model(project, task), task)
    model, fallback_why = limits.model_fallback(routed)
    reasons = [r for r in (spend_why, fallback_why) if r]
    if reasons:
        log.info("Model resolution -> %s (%s)", model, "; ".join(reasons))
    return model


def configured_model(project: Optional[sqlite3.Row], task: str = "") -> str:
    """`resolve_model` without the usage fallback: what Wes has asked for."""
    if task == "research":
        override = (db.get_setting("research_model") or "").strip()
        return override if override in config.MODEL_VALUES else config.RESEARCH_MODEL
    if project is not None:
        try:
            override = (project["model"] or "").strip()
        except (IndexError, KeyError):  # row from a pre-migration query
            override = ""
        if override in config.MODEL_VALUES:
            return override
    global_model = (db.get_setting("worker_model") or "").strip()
    if global_model in config.MODEL_VALUES:
        return global_model
    return config.DEFAULT_MODEL


@dataclass
class RunResult:
    ok: bool
    timed_out: bool = False
    cancelled: bool = False
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    result_text: str = ""
    raw_stdout: str = ""
    raw_stderr: str = ""
    report: Optional[dict] = field(default=None)
    # "structured" when the report came back schema-validated through the
    # CLI's StructuredOutput tool, "file" when it was read from the legacy
    # .portal/report.json, None when the run reported nothing.
    report_source: Optional[str] = None
    is_rate_limited: bool = False
    # True when the kernel OOM-killed something inside this run's memory-capped
    # scope (app/runlimit.py). The run itself usually survives - the cap kills
    # the greedy process only - so this can be set on a run that otherwise
    # succeeded, and is a fact worth reporting either way.
    oom_killed: bool = False
    peak_memory_bytes: Optional[int] = None
    # The result event's subtype ("success", "error_max_turns",
    # "error_during_execution"). On failures with an empty `result` string this
    # is the only thing the CLI says about *why* - dropping it is how eight
    # runs' deaths got journalled as literally "(no output)".
    subtype: Optional[str] = None

    @property
    def hit_max_turns(self) -> bool:
        return self.subtype == "error_max_turns"


def _row_get(row: sqlite3.Row, key: str, default=None):
    """`row["missing"]` raises on a sqlite3.Row. Tests build projects through
    older fixtures and the meta-project row predates these columns, so read
    defensively rather than making the prompt depend on a migration."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _project_section(project: sqlite3.Row) -> str:
    """The project header of the prompt.

    Both the original idea and the current description are shown, always and
    separately. The description is the agent's own summary of what the project
    has become and gets rewritten as work lands; the idea is the sentence Wes
    actually typed, and it is the only record of what he was originally after -
    so summarizing it away would quietly lose the brief.
    """
    idea = (_row_get(project, "initial_idea", "") or "").strip()
    description = (project["description"] or "").strip()
    lines = [
        "## Project",
        f"- Title: {project['title']}"
        + (" (LOCKED - do not propose a new title)" if _row_get(project, "title_locked", 0) else ""),
        f"- Slug: {project['slug']}",
        f"- Kind: {project['kind']}",
        f"- Status: {db.display_state(project)}",
        f"- Priority: {project['priority']}",
        "- Build approval: "
        + (
            f"{config.SITE.owner} has approved building this - write code."
            if _row_get(project, "build_approved", 0)
            else "NOT yet approved for building. Triage, plan and research only; "
            "ask for the OK rather than starting to write the project's code."
        ),
    ]
    if db.blocked_on(project):
        lines.append(
            f"- The previous run reported itself blocked on: {db.blocked_on(project)} "
            "(work anything that does not depend on it; restate it in your report "
            "if it still blocks you)"
        )
    if idea:
        lines.append(
            f"- {config.SITE.owners} original idea (never changes, this is the brief):\n{idea}"
        )
    locked = _row_get(project, "description_locked", 0)
    if description and description == idea:
        # Printing the same paragraph twice under two headings invites the
        # agent to treat them as two different requirements.
        lines.append(
            "- Description: same as the original idea above."
            + (" LOCKED - do not rewrite it." if locked else "")
        )
    else:
        suffix = " (LOCKED - do not rewrite it)" if locked else ""
        lines.append(f"- Current description{suffix}:\n{description or '(none)'}")
    return "\n".join(lines)


def _skills_section() -> str:
    """One line per skill the portal ships into the workspace.

    The skill files themselves are copied to .claude/skills/ (see
    worker._sync_skills), but a skill only gets used if the agent knows to go
    looking. The whole reason a capable skill sat unused for weeks is that
    nothing in the prompt ever mentioned it - so the names and one-liners go in
    the prompt, and the detail stays in the file.

    Two roots: the built-in skills in the repo, and the ones the compaction
    agent has promoted out of learnings.md (memory.promoted_skills_dir). Same
    listing either way - an agent does not care where a recipe came from. A
    name collision goes to the built-in copy, the curated one.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for root in (config.SKILLS_DIR, memory.promoted_skills_dir()):
        try:
            entries = sorted(root.iterdir()) if root.is_dir() else []
        except OSError:
            entries = []
        for path in entries:
            skill = path / "SKILL.md"
            if path.name in seen or not skill.is_file():
                continue
            name = path.name
            description = memory.skill_description(skill)
            seen.add(name)
            lines.append(f"- **{name}** (`.claude/skills/{name}/SKILL.md`) - {description}")
    if not lines:
        return ""
    return (
        "## Skills available to you\n"
        "These are in your working directory. Read the file before deciding you "
        "cannot do something.\n" + "\n".join(lines)
    )


def _promote_skills_section() -> str:
    """The compaction agent's license to lift a procedure out of learnings.md
    into a real skill (#226, the last piece of the memory overhaul).

    Dynamic rather than part of TASK_GUIDANCE because the destination is an
    absolute path under the data dir: the agent's cwd is MEMORY_DIR and the
    skills root sits beside it, so a relative path would invite ../ guessing.
    """
    dest = memory.promoted_skills_dir()
    return (
        "## Promote procedures to skills\n"
        "A learning that is really a PROCEDURE - a how-to whose value is its "
        "concrete steps or exact commands (\"to render X, run Y then Z\") - is "
        "worth more as a skill than as a bullet: skills are copied into every "
        "workspace and indexed by name in every run's prompt, so the recipe "
        "stays discoverable while its full text stops taxing every run's "
        "context. For each such learning (at most 3 this pass), write "
        f"`{dest}/<kebab-name>/SKILL.md` (an absolute path - create the "
        "directories), starting with YAML frontmatter:\n"
        "---\n"
        "name: <kebab-name>\n"
        "description: WHEN a future agent should reach for this, one line - "
        "it is shown in every prompt\n"
        "---\n"
        "followed by the full procedure, written to be followable without the "
        "original context. Then DROP the bullet from learnings.md - the skill "
        "replaces it. Only promote genuine procedures: a one-sentence fact "
        "stays a bullet, and never edit or delete skills already in that "
        "directory."
    )


def _entry_ages_section() -> str:
    """The freshness sidecar, digested for the compaction agent.

    The sidecar (memory.load_learnings_meta) knows when each live bullet was
    first recorded and how many later runs re-observed it - exactly the signal
    for choosing cuts, and invisible from inside learnings.md by design (the
    prompt-facing file stays date-free). Bullets are truncated: this is a
    matching hint against a file the agent reads in full from its cwd, not a
    second copy to rewrite.

    Fails open: no sidecar, no learnings, any error - no section, and the
    compaction runs exactly as it always has.
    """
    try:
        from app import worker  # local: worker imports this module at import time

        entries = worker.learnings_freshness()
        rows = []
        for e in entries:
            if not e.tracked:
                continue
            text = e.text if len(e.text) <= 110 else e.text[:110] + "..."
            rows.append(
                f"- [added {e.added} / confirmed {e.confirmed} / seen {e.count}x] {text}"
            )
        if not rows:
            return ""
        undated = len(entries) - len(rows)
        head = (
            "## Entry ages (context for choosing cuts)\n"
            f"Today is {memory.today()}. Each tracked live bullet below carries "
            "the date it was first recorded, the date a run last re-observed it, "
            "and how many runs have observed it - stalest first. A line "
            "confirmed often and recently is load-bearing: keep it, merged at "
            "most. A line added long ago, seen once and never re-confirmed is a "
            "prime candidate to drop or fold into a sharper line. This is a "
            "hint, not an order - judge the content too. Bullets are truncated "
            "here; the full text is in the file."
        )
        tail = (
            f"\n({undated} further live lines are undated - added before "
            "tracking began - judge those on content alone.)"
            if undated
            else ""
        )
        return head + "\n" + "\n".join(rows) + tail
    except Exception:  # noqa: BLE001 - a freshness bug must never block compaction
        return ""


def build_prompt(task: str, project: Optional[sqlite3.Row]) -> str:
    parts: list[str] = []

    # The compaction agent reads learnings.md itself, from its cwd. Pasting a
    # tail of it into the prompt as well would give it two versions of the file
    # to rewrite - and the tail is exactly the part it needs least, since the
    # duplication it is there to collapse is spread across the whole thing.
    if task == "compact":
        parts.append(TASK_GUIDANCE["compact"])
        try:
            cap = max(0, int(db.get_setting("learnings_cap_lines") or "200"))
        except (TypeError, ValueError):
            cap = 200
        if cap:
            parts.append(
                f"## Hard target\nThis file auto-compacts once it passes {cap} lines, "
                f"which is why you are running now. Finish comfortably under {cap} "
                "lines - if you cannot, you are keeping project trivia that should be "
                "dropped or merged."
            )
        parts.append(_promote_skills_section())
        ages = _entry_ages_section()
        if ages:
            parts.append(ages)
        parts.append(AGENT_CONTRACT)
        parts.append(
            "## Current profile.md (context only - do not rewrite it)\n"
            + (config.PROFILE_MD.read_text(encoding="utf-8") if config.PROFILE_MD.exists() else "")
        )
        return "\n\n".join(parts)

    if task == "reflect":
        parts.append(TASK_GUIDANCE["reflect"])
        parts.append(AGENT_CONTRACT)
        profile = config.PROFILE_MD.read_text(encoding="utf-8") if config.PROFILE_MD.exists() else ""
        learnings = ""
        if config.LEARNINGS_MD.exists():
            lines = config.LEARNINGS_MD.read_text(encoding="utf-8").splitlines()
            learnings = "\n".join(lines[-100:])
        journal = db.list_journal(project_id=None, limit=40)
        journal_txt = "\n".join(
            f"- [{row['ts']}] ({row['project_title'] or 'n/a'}) {row['author']}/{row['kind']}: "
            f"{(row['content_md'] or '')[:400]}"
            for row in reversed(journal)
        )
        parts.append(f"## Recent cross-project journal (last {len(journal)})\n{journal_txt}")
        parts.append(f"## Current profile.md\n{profile}")
        parts.append(f"## Recent learnings.md (tail)\n{learnings}")
        return "\n\n".join(parts)

    assert project is not None
    parts.append(TASK_GUIDANCE.get(task, f"Task: {task}."))
    parts.append(AGENT_CONTRACT)
    parts.append(_project_section(project))

    # Sits directly under the project section, above the journal: an agent that
    # reads no further than the first screen still finds out that a previous run
    # left a finished feature in the working tree. Empty on a clean repo, so an
    # ordinary run's prompt is byte-for-byte unchanged.
    orphan_txt = _orphan_section(project["slug"])
    if orphan_txt:
        parts.append(orphan_txt)

    # Above the journal too, because it is identity: a sub-project reading its
    # own journal needs to already know it is one, or it starts doing the
    # parent's job. Never fatal - a family listing is not worth losing a run
    # over.
    try:
        family_txt = subprojects.prompt_section(project)
    except Exception:  # pragma: no cover - defensive
        log.exception("Could not build the sub-project section for %s", project["slug"])
        family_txt = ""
    if family_txt:
        parts.append(family_txt)

    # Who uses this portal, when that is more than one person - above the notes
    # on purpose, because the very next section is signed and an agent has to
    # know whose signature it is reading before it reads it.
    #
    # Empty on a single-person install, which is every install until somebody
    # adds a second person, so an ordinary prompt is byte-for-byte unchanged and
    # none of the existing behavior shifts under a feature nobody is using.
    try:
        people_txt = people.prompt_section(project["id"])
    except Exception:  # pragma: no cover - defensive
        log.exception("Could not build the people section for %s", project["slug"])
        people_txt = ""
    if people_txt:
        parts.append(people_txt)

    # Everything Wes has written since the last run, as one block above the
    # journal rather than as scattered lines inside it. This also *spends* those
    # notes: they stop being editable from here on, because from here on an
    # agent has them. See app/notes.py.
    delivery = notes.deliver(project["id"])
    if delivery.text:
        parts.append(delivery.text)

    # The ask side thread is left out: an ask is a parallel question, not an
    # instruction, and its answer is not something a run should act on. See
    # db.SIDE_THREAD and app/quoting.py. `ask.build_prompt` keeps them, so the
    # side thread has continuity of its own.
    journal = db.list_journal_asc(project["id"], limit=20, exclude=db.SIDE_THREAD)
    # Dropped from the tail only if they made it into the block above, so a
    # failed delivery degrades to the old behavior instead of losing the note.
    delivered_now = set(delivery.ids)
    journal = [row for row in journal if int(row["id"]) not in delivered_now]
    journal_txt = "\n".join(
        f"- [{row['ts']}] {row['author']}/{row['kind']}: {row['content_md']}" for row in journal
    ) or "(no journal entries yet)"
    parts.append(f"## Recent journal (last {len(journal)})\n{journal_txt}")

    # Uploaded files sit in the workspace, so the agent only needs to be told
    # they exist and where - it can Read them itself. Omitted entirely when
    # there are none, rather than adding an empty heading to every prompt.
    attach_txt = attachments.prompt_section(project["id"])
    if attach_txt:
        parts.append(attach_txt)

    todo_txt = todos.prompt_section(project["id"])
    if todo_txt:
        parts.append(todo_txt)

    # Before the answered ones, because the open ones are the actionable half:
    # the prompt used to show only what Wes had answered, so an agent could not
    # see what it was already asking and re-asking was its only option. See
    # app/qdedupe.py.
    waiting_txt = qdedupe.prompt_section(project["id"])
    if waiting_txt:
        parts.append(waiting_txt)

    qa = db.answered_qa(project["id"])
    qa_txt = "\n".join(f"- Q: {row['question']}\n  A: {row['answer']}" for row in qa) or "(none)"
    parts.append(f"## Answered questions\n{qa_txt}")

    skills_txt = _skills_section()
    if skills_txt:
        parts.append(skills_txt)

    profile = config.PROFILE_MD.read_text(encoding="utf-8") if config.PROFILE_MD.exists() else "(none)"
    parts.append(f"## Memory: profile.md (full)\n{profile}")

    learnings = "(none)"
    if config.LEARNINGS_MD.exists():
        lines = config.LEARNINGS_MD.read_text(encoding="utf-8").splitlines()
        learnings = "\n".join(lines[-100:])
    parts.append(f"## Memory: learnings.md (tail)\n{learnings}")

    return "\n\n".join(parts)


def _orphan_section(slug: str) -> str:
    """Never let a git failure stop a run from being built. A prompt missing
    this block costs a rebuilt feature; a prompt that raises costs the run."""
    try:
        return orphans.prompt_section(slug)
    except Exception:  # noqa: BLE001 - see docstring
        log.exception("Orphan scan failed while building a prompt for %s", slug)
        return ""


# Environment variables that would route a spawned run onto pay-as-you-go API
# billing (or a third-party gateway) instead of the configured arrangement. A
# stray one of these is the single cause behind every large surprise-bill
# story, so they are stripped from every run in both modes - see
# `app/spawnauth.py`, which owns the list and puts the *configured* key back
# afterwards on an API-key install. On a subscription install nothing is put
# back and the CLI falls through to the OAuth credentials in
# ~/.claude/.credentials.json, which bill nothing.
#
# Kept as a module attribute because it has been the name this guard is known
# by since #218, and tests and hooks reference it.
_BILLING_ENV_VARS = spawnauth.BILLING_ENV_VARS


def _extra_env() -> dict[str, str]:
    env = os.environ.copy()
    local_bin = str(Path.home() / ".local" / "bin")
    env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
    return spawnauth.spawn_env(env)


def _configured_budget_usd() -> Optional[float]:
    """The per-run dollar ceiling: the setting, else the mode's default.

    On a subscription install runs bill $0, so an unset ceiling means no
    ceiling - this is only a backstop for the leaked-key case that pairs with
    `_extra_env` stripping the key in the first place.

    On an API-key install every run bills real money and the ceiling is doing
    actual work, so an unset one falls back to
    `spawnauth.DEFAULT_API_KEY_BUDGET_USD` rather than to "unlimited". An
    unattended scheduler with no per-run cap is how a runaway loop becomes an
    invoice, and defaulting that to infinity would be a trap laid for whoever
    installs this next.

    An explicit setting always wins, in both modes. Blank, zero, negative or
    unparseable mean "use the mode's default" so a bad value degrades to the
    safe answer rather than blocking every run.
    """
    raw = (db.get_setting("run_max_budget_usd") or "").strip()
    if not raw:
        return spawnauth.default_budget_usd()
    try:
        value = float(raw)
    except ValueError:
        return spawnauth.default_budget_usd()
    return value if value > 0 else spawnauth.default_budget_usd()


def _looks_rate_limited(text: str) -> bool:
    t = text.lower()
    if "limit" not in t:
        return False
    return any(word in t for word in ("reach", "exceed", "rate"))


# stream-json emits one JSON object per line, and a line carrying a big tool
# result can be far past asyncio's default 64 KiB StreamReader limit.
STREAM_LINE_LIMIT = 16 * 1024 * 1024
# Cap what we keep in memory / store as the run's raw output.
MAX_RAW_CHARS = 200_000

# Called with (event, rendered_lines) for every stream event, as it arrives.
EventCallback = Callable[[dict, list[str]], Optional[Awaitable[None]]]


async def _emit(on_event: Optional[EventCallback], event: dict, lines: list[str]) -> None:
    if on_event is None:
        return
    try:
        maybe = on_event(event, lines)
        if asyncio.iscoroutine(maybe):
            await maybe
    except Exception:  # noqa: BLE001 - a broken live view must not kill the run
        log.exception("run event callback failed")


# --------------------------------------------------------------------------
# Cancellation
#
# A run is a `claude` process (plus the tool children it spawns) owned by this
# process. To stop one on request we need a handle on it, so live runs register
# themselves here for the lifetime of the subprocess. The registry is in-memory
# on purpose: after a restart there is no process left to signal, and
# `init_db()` already reconciles the orphaned row.
# --------------------------------------------------------------------------

_ACTIVE_PROCS: dict[int, asyncio.subprocess.Process] = {}
_CANCEL_REQUESTED: set[int] = set()


def cancel_run(run_id: int) -> bool:
    """SIGKILL the process group of a live run.

    Returns True if a process was signaled. False means this process isn't
    supervising that run - it already finished, or it was started by a previous
    incarnation of the service - and the caller should settle the DB row itself.
    """
    _CANCEL_REQUESTED.add(run_id)
    proc = _ACTIVE_PROCS.get(run_id)
    if proc is None:
        _CANCEL_REQUESTED.discard(run_id)
        return False
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            return False
    log.info("Canceled run %s", run_id)
    return True


def cancel_requested(run_id: Optional[int]) -> bool:
    return run_id is not None and run_id in _CANCEL_REQUESTED


def _forget(run_id: Optional[int]) -> None:
    if run_id is None:
        return
    _ACTIVE_PROCS.pop(run_id, None)
    _CANCEL_REQUESTED.discard(run_id)


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the whole process group, then reap. Falls back to killing just
    the child if the group is already gone."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        log.warning("Timed-out run %s did not reap within 10s", proc.pid)


def build_cmd(
    model: str,
    max_turns: int,
    resume_session: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
    json_schema: Optional[str] = None,
    settings_json: Optional[str] = None,
) -> list[str]:
    """The argv for a run - deliberately without the prompt in it.

    The prompt used to sit here as `cmd[2]`, and on 2026-07-26 that quietly
    became a hard ceiling on how much context a project could carry. Linux caps
    a *single* argv string at MAX_ARG_STRLEN (32 pages = 131072 bytes), which is
    a separate limit from ARG_MAX and is not raiseable - so a project whose
    rendered prompt crossed 128 KiB could not be spawned at all. OpenJournal
    reached 146 KiB and failed 257 times in a row; ProxyTable was at 126 KiB,
    five kilobytes from the same wall, and every other active project was in
    the same band.

    `claude -p` reads the prompt from stdin when argv does not carry one, which
    has no such limit. Keeping the parameter out of this function's signature
    (rather than accepting and ignoring it) is what stops it being put back.
    """
    cmd = [
        "claude",
        "-p",
        "--model",
        config.cli_model(model),
        "--output-format",
        "stream-json",
        "--verbose",  # required by the CLI alongside stream-json in print mode
        "--dangerously-skip-permissions",
        "--max-turns",
        str(max_turns),
    ]
    # A hard dollar ceiling on the run. Redundant while runs bill $0 on the
    # subscription, but it caps the blast radius if an API key ever leaks into
    # the environment despite _extra_env stripping it. `:g` keeps whole numbers
    # tidy ("5" not "5.0") for the CLI.
    if max_budget_usd is not None and max_budget_usd > 0:
        cmd += ["--max-budget-usd", f"{max_budget_usd:g}"]
    # Schema-validated structured output (report_schema.py). The CLI exposes a
    # StructuredOutput tool to the agent, validates the submission (the model
    # sees a mismatch and retries), and returns the parsed object in the
    # result event's `structured_output` - composing fine with stream-json.
    if json_schema:
        cmd += ["--json-schema", json_schema]
    # Additional CLI settings for this run, passed as an inline JSON string.
    # Today this carries the PreToolUse guardrail hook (app/hookguard.py);
    # the CLI merges it with whatever the workspace's own settings say.
    if settings_json:
        cmd += ["--settings", settings_json]
    # Continue an existing CLI session instead of starting fresh - this is how
    # a one-off task's follow-up message reaches an agent that remembers the
    # whole exchange. The CLI issues a NEW session id for the resumed
    # conversation (it forks), so the caller must store the id from each
    # result, not just the first one.
    if resume_session:
        cmd += ["--resume", resume_session]
    return cmd


async def run_claude(
    prompt: str,
    cwd: Path,
    model: str,
    timeout_min: int,
    max_turns: int = 100,
    on_event: Optional[EventCallback] = None,
    run_id: Optional[int] = None,
    resume_session: Optional[str] = None,
    json_schema: Optional[str] = None,
    settings_json: Optional[str] = None,
) -> RunResult:
    """Run the agent, streaming its events to `on_event` as they happen.

    Uses `--output-format stream-json` rather than `json` so the portal can show
    what a run is doing while it is doing it; the final `result` event carries
    the same fields the single-shot `json` format returned.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    # Remove any stale report from a previous run so a failed run can't
    # accidentally re-apply the last run's status/questions.
    stale_report = cwd / ".portal" / "report.json"
    stale_report.unlink(missing_ok=True)
    cmd = build_cmd(
        model, max_turns, resume_session,
        max_budget_usd=_configured_budget_usd(), json_schema=json_schema,
        settings_json=settings_json,
    )
    # Each run gets its own memory-capped cgroup scope where the machine
    # supports it, so a runaway tool inside the run cannot OOM the box - and
    # therefore cannot take the portal (and every other run) down with it.
    # See app/runlimit.py.
    argv = runlimit.wrap(cmd, run_id)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=_extra_env(),
            # The prompt goes in here rather than in argv - see build_cmd. The
            # CLI reads it to EOF, so the pipe must be closed once written or
            # the run hangs before it starts.
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LINE_LIMIT,
            # Own process group: the CLI spawns children (shells, tools) that
            # inherit our pipes. Killing only the parent on timeout leaves them
            # alive holding stdout open, so the "timed out" path would block
            # until they finished anyway. See _kill_group.
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return RunResult(ok=False, result_text=f"claude CLI not found: {exc}")

    if run_id is not None:
        _ACTIVE_PROCS[run_id] = proc

    # Started, not awaited, and deliberately so: the prompt is far larger than
    # a pipe buffer (64 KiB on Linux; the busiest projects render past 120 KB),
    # so writing it to completion before reading stdout would deadlock the
    # first time the CLI emitted an event while we were still filling stdin.
    feeder = asyncio.create_task(_feed_prompt(proc, prompt))
    try:
        return await _supervise(proc, cwd, run_id, on_event, timeout_min)
    finally:
        feeder.cancel()
        _forget(run_id)
        runlimit.forget_scope(run_id)


async def _feed_prompt(proc: asyncio.subprocess.Process, prompt: str) -> None:
    """Write the prompt to the CLI's stdin and close it.

    Every failure here is swallowed on purpose. A broken pipe means the CLI
    exited before reading the prompt, and whatever made it exit is already on
    its way out of stderr - raising here would replace that real diagnosis with
    a write error, which is how a "run crashed, see the log" with nothing
    useful in the log gets made.
    """
    if proc.stdin is None:  # pragma: no cover - only when stdin was not piped
        return
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        log.info("Could not write the prompt to the CLI's stdin: %s", exc)
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


async def _supervise(
    proc: asyncio.subprocess.Process,
    cwd: Path,
    run_id: Optional[int],
    on_event: Optional[EventCallback],
    timeout_min: int,
) -> RunResult:
    parsed: dict = {}
    raw_parts: list[str] = []
    raw_len = 0
    mem = _MemoryWatch(proc.pid, run_id, on_event)
    watcher = asyncio.create_task(mem.poll_forever())

    async def pump_stdout() -> None:
        nonlocal parsed, raw_len
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # One oversized line; drop it rather than abandoning the run.
                log.warning("Dropping a stream-json line over %d bytes", STREAM_LINE_LIMIT)
                continue
            if not raw:
                return
            text = raw.decode(errors="replace")
            if raw_len < MAX_RAW_CHARS:
                raw_parts.append(text)
                raw_len += len(text)
            event = runlog.parse_line(text)
            if event is None:
                continue
            if event.get("type") == "result":
                parsed = event
            await _emit(on_event, event, runlog.render_event(event))

    async def read_stderr() -> bytes:
        assert proc.stderr is not None
        return await proc.stderr.read()

    try:
        _, stderr_b = await asyncio.wait_for(
            asyncio.gather(pump_stdout(), read_stderr()), timeout=timeout_min * 60
        )
    except asyncio.TimeoutError:
        watcher.cancel()
        await _kill_group(proc)
        return RunResult(
            ok=False, timed_out=True, result_text="Run timed out",
            oom_killed=mem.oom_killed, peak_memory_bytes=mem.peak_bytes,
        )

    # Read the scope one last time before anything is reaped. Without this the
    # answer depends on where the poll interval happened to fall: a run whose
    # last act was a memory kill would report a clean bill of health simply
    # because it ended between two polls.
    watcher.cancel()
    mem.read_once()
    await proc.wait()

    # A cancel SIGKILLs the group, which closes stdout and lands us here with a
    # non-zero return code. That is a deliberate stop, not a failure, so it gets
    # its own result flag rather than being reported as an error.
    if cancel_requested(run_id):
        return RunResult(ok=False, cancelled=True, result_text="Run canceled")

    stdout = "".join(raw_parts)
    stderr = stderr_b.decode(errors="replace")

    if not parsed:
        log.warning("No result event in claude stream output")

    is_error = bool(parsed.get("is_error"))
    result_text = str(parsed.get("result", "") or "")
    # Only treat as rate-limited on an actual failure; agent output legitimately
    # mentioning "rate limits" must not trigger a backoff.
    failed = is_error or proc.returncode != 0
    rate_limited = failed and (
        _looks_rate_limited(result_text) or _looks_rate_limited(stderr)
    )

    report, report_source = _pick_report(parsed, cwd)
    if report_source == "structured":
        # With structured output the CLI's `result` string is the raw JSON the
        # StructuredOutput call submitted; stored as a run summary it reads as
        # noise. The report's own summary bullets are the human line.
        bullets = report.get("summary")
        if isinstance(bullets, list) and bullets:
            result_text = "; ".join(str(b) for b in bullets)

    return RunResult(
        ok=not is_error and proc.returncode == 0,
        session_id=parsed.get("session_id"),
        cost_usd=parsed.get("total_cost_usd"),
        num_turns=parsed.get("num_turns"),
        result_text=result_text,
        raw_stdout=stdout,
        raw_stderr=stderr,
        report=report,
        report_source=report_source,
        is_rate_limited=rate_limited,
        oom_killed=mem.oom_killed,
        peak_memory_bytes=mem.peak_bytes,
        subtype=str(parsed.get("subtype")) if parsed.get("subtype") else None,
    )


class _MemoryWatch:
    """Polls a run's cgroup while it runs, so the portal can say "a command in
    this run was killed for using too much memory" instead of leaving the agent
    holding an unexplained `Killed`.

    Polled rather than read once at the end because the cgroup is gone the
    moment the last process in it exits - by the time `proc.wait()` returns
    there is nothing left to read. Two small file reads every 15s is a price
    worth paying to never lose the fact.

    The kill is announced into the run's live event stream the first time it is
    seen, not only in the final result: on a run that goes on to succeed the
    result never mentions it, and a swallowed OOM is exactly the silent failure
    that made this bug take five service restarts to notice.
    """

    INTERVAL_S = 5

    def __init__(
        self,
        pid: int,
        run_id: Optional[int],
        on_event: Optional[EventCallback] = None,
    ) -> None:
        self.pid = pid
        self.run_id = run_id
        self.on_event = on_event
        self.oom_killed = False
        self.peak_bytes: Optional[int] = None
        self._announced = False
        self._cgroup: Optional[Path] = None

    def read_once(self) -> Optional[runlimit.Sample]:
        # The path is resolved through /proc/<pid> once and then kept: /proc
        # stops answering the moment the process is reaped, and the last read -
        # the one that catches a kill in a run's final seconds - happens after
        # that.
        if self._cgroup is None:
            self._cgroup = runlimit.cgroup_for(self.pid, self.run_id)
        if self._cgroup is None:
            return None
        got = runlimit.read_sample(self._cgroup)
        if got is None:
            return None
        if got.peak_bytes is not None:
            self.peak_bytes = got.peak_bytes
        if got.oom_kills > 0:
            self.oom_killed = True
        return got

    async def poll_forever(self) -> None:
        while True:
            try:
                self.read_once()
            except Exception:  # noqa: BLE001 - diagnostics must never kill a run
                log.exception("Memory watch failed for run %s", self.run_id)
                return
            if self.oom_killed and not self._announced:
                self._announced = True
                note = runlimit.kill_note()
                log.warning("Run %s: %s", self.run_id, note)
                await _emit(self.on_event, {"type": "portal_oom"}, [f"! {note}"])
            await asyncio.sleep(self.INTERVAL_S)


def _pick_report(parsed: dict, cwd: Path) -> tuple[Optional[dict], Optional[str]]:
    """The run's report: schema-validated structured output when the CLI
    returned one, else the legacy .portal/report.json file.

    The file stays accepted forever, not just for one migration run - a run
    that dies short of calling StructuredOutput, an older prompt, or a spawn
    made without the schema flag can still report the old way. When both
    exist the structured one wins: it is the one the CLI validated."""
    structured = parsed.get("structured_output")
    if isinstance(structured, dict):
        return structured, "structured"
    file_report = _read_report(cwd)
    if file_report is not None:
        return file_report, "file"
    return None, None


def _read_report(cwd: Path) -> Optional[dict]:
    report_path = cwd / ".portal" / "report.json"
    if not report_path.exists():
        return None
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read report.json: %s", exc)
        return None

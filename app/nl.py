"""Natural-language intent router for the Telegram bot.

Slash commands are the Telegram convention, but they're a poor fit for a
personal assistant bot where most messages are either "here's a new idea" or an
answer to something the portal just asked. So: slash commands still work (see
`telegram_bot`), and anything else gets classified here.

Classification is a single short `claude -p --model haiku` call. It is given the
currently-open questions and active projects so it can resolve references like
"the portal one" or "yes, use sonnet" to concrete ids. Everything that talks to
the model is confined to `classify()`; the prompt building and response parsing
are pure functions so they can be tested without a subprocess.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from string import Template

from app import config, db

log = logging.getLogger("portal.nl")

# Intents the bot knows how to execute. `unknown` triggers the help text.
INTENTS = [
    "answer", "idea", "note", "ask", "status", "set_status", "run", "cancel", "help", "unknown",
]

_SYSTEM_PROMPT_TEMPLATE = """\
You classify a single Telegram message sent by $OWNER to $THEIR personal project
portal bot. Reply with ONLY a JSON object, no prose and no code fence.

Schema:
{"intent": one of ["answer","idea","note","ask","status","set_status","run","cancel","help","unknown"],
 "question_id": <int or null>,
 "project_slug": <string or null>,
 "text": <string or null>,
 "status": <one of the project statuses, or null>,
 "confidence": <float 0..1>}

Intent guide:
- "answer": the message replies to one of the OPEN QUESTIONS below. Set
  question_id to that question's id and text to the answer itself.
- "idea": a new project or thing to build. text = the idea.
- "note": a comment/instruction about an EXISTING project. Set project_slug and
  text.
- "ask": a QUESTION about one existing project, to be answered with no
  work done ("why did you pick that display?", "is the plan written yet?",
  "does that project use sqlite?"). Set project_slug and text. Prefer "note"
  when the message tells the agent to do or change something, and "ask"
  when it wants information back and nothing else to happen.
- "status": asking how things are going / what's in flight.
- "set_status": moving an existing project to a different status
  ("pause X", "mark X done", "put X back on the backlog", "start X" =
  active). Set project_slug and status.
- "run": the agent should work on an existing project now. Set project_slug.
- "cancel": the run currently in flight should STOP ("stop", "kill it",
  "abort", "that's enough", "cancel the portal run"). It targets whatever is
  under RUNNING NOW, so project_slug may be null; only set it if the message
  named a project explicitly.
- "help": asking what can be said to you.
- "unknown": you genuinely cannot tell. Use a low confidence.

Rules:
- project_slug MUST be one of the slugs listed under ACTIVE PROJECTS, or null.
- question_id MUST be one of the ids listed under OPEN QUESTIONS, or null.
- If there is exactly one open question and the message reads like a reply to
  it rather than a new idea, prefer "answer".
- Prefer "idea" over "note" when no existing project clearly matches.
"""

# Filled in from the site config, like both agent contracts.
SYSTEM_PROMPT = Template(_SYSTEM_PROMPT_TEMPLATE).safe_substitute(
    **config.SITE.template_vars()
)


def build_context(
    questions: list[sqlite3.Row],
    projects: list[sqlite3.Row],
    message: str,
    active_run: Optional[sqlite3.Row] = None,
) -> str:
    """The user-visible half of the router prompt. Pure; safe to unit test.

    `active_run` is included so "stop it" has a referent - without it the model
    has no way to tell a cancel from a note.
    """
    q_lines = "\n".join(
        f"- id={q['id']} project={q['project_slug']!r}: {q['question'][:300]}"
        for q in questions
    ) or "(none)"
    p_lines = "\n".join(
        f"- slug={p['slug']!r} title={p['title']!r} status={db.display_state(p)}"
        for p in projects
    ) or "(none)"
    if active_run is None:
        r_line = "(nothing is running right now - do NOT classify as 'cancel')"
    else:
        r_line = (
            f"run #{active_run['id']} task={active_run['task']} "
            f"project={active_run['project_slug'] or '(memory/reflect)'!r}"
        )
    return (
        f"{SYSTEM_PROMPT}\n"
        f"## OPEN QUESTIONS\n{q_lines}\n\n"
        f"## ACTIVE PROJECTS\n{p_lines}\n\n"
        f"## RUNNING NOW\n{r_line}\n\n"
        f"## PROJECT STATUSES\n{', '.join(config.USER_STATES)}\n\n"
        f"## MESSAGE\n{message}\n\n"
        "JSON:"
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_intent(
    raw: str,
    valid_question_ids: set[int],
    valid_slugs: set[str],
) -> dict[str, Any]:
    """Turn the model's reply into a validated intent dict.

    Anything malformed, or referencing an id/slug that doesn't exist, degrades
    to `unknown` rather than raising - the bot must always be able to reply.
    """
    fallback = {
        "intent": "unknown",
        "question_id": None,
        "project_slug": None,
        "text": None,
        "status": None,
        "confidence": 0.0,
    }
    if not raw:
        return fallback
    match = _JSON_RE.search(raw)
    if match is None:
        return fallback
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return fallback
    if not isinstance(data, dict):
        return fallback

    intent = data.get("intent")
    if intent not in INTENTS:
        return fallback

    qid = data.get("question_id")
    qid = int(qid) if isinstance(qid, (int, float)) and int(qid) in valid_question_ids else None

    slug = data.get("project_slug")
    slug = slug if isinstance(slug, str) and slug in valid_slugs else None

    # The old vocabulary ("inbox", "building", "waiting_user") is normalised
    # rather than rejected - the router model has seen it in old journals.
    status = config.normalize_state(str(data.get("status") or ""))

    text = data.get("text")
    text = text.strip() if isinstance(text, str) and text.strip() else None

    confidence = data.get("confidence")
    confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    # An intent that needs a referent it didn't resolve is not actionable.
    if intent == "answer" and qid is None:
        return fallback
    if intent in ("note", "ask", "set_status", "run") and slug is None:
        return fallback
    if intent == "set_status" and status is None:
        return fallback

    return {
        "intent": intent,
        "question_id": qid,
        "project_slug": slug,
        "text": text,
        "status": status,
        "confidence": confidence,
    }


ROUTER_TIMEOUT_SEC = 45


async def classify(message: str) -> dict[str, Any]:
    """Classify a free-text Telegram message. Never raises."""
    questions = db.open_questions()
    projects = db.list_projects_by_stage(config.OPEN_STAGES)
    prompt = build_context(questions, projects, message, db.active_run())
    raw = await _run_router(prompt)
    return parse_intent(
        raw,
        {int(q["id"]) for q in questions},
        {p["slug"] for p in projects},
    )


async def _run_router(prompt: str) -> str:
    env = os.environ.copy()
    env["PATH"] = f"{Path.home() / '.local' / 'bin'}:{env.get('PATH', '')}"
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        config.cli_model(router_model()),
        "--output-format",
        "json",
        "--max-turns",
        "1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(config.DATA_DIR),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=ROUTER_TIMEOUT_SEC)
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as exc:
        log.warning("NL router unavailable (%s); falling back to commands", exc)
        return ""
    try:
        return str(json.loads(stdout_b.decode(errors="replace")).get("result", "") or "")
    except json.JSONDecodeError:
        return ""


def is_enabled() -> bool:
    return (db.get_setting("telegram_natural_language") or "0") == "1"


def router_model() -> str:
    """The model that reads Telegram messages.

    Separate from `worker_model` on purpose: this runs on every single message
    and is judged on latency, while the agent model is judged on capability.
    An unrecognised stored value falls back to the default rather than being
    passed to `claude --model`, where it would fail every message.
    """
    value = (db.get_setting("telegram_model") or "").strip()
    return value if value in config.MODEL_VALUES else config.TELEGRAM_MODEL


def set_router_model(value: str) -> Optional[str]:
    """Set the Telegram router model. Returns the stored value, or None if the
    name isn't one of the known models."""
    value = (value or "").strip().lower()
    if value not in config.MODEL_VALUES:
        return None
    db.set_setting("telegram_model", value)
    return value

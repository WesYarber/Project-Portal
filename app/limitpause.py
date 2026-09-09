"""A run that runs out of allowance is paused, not stopped.

Wes, 2026-09-08: "Runs should be paused instead of stopped when they run out of
tokens during the run." That afternoon run 1485 had spent 71 turns and $11 on
ProxyTable, deployed, and was one tool call from filing its report when the CLI
refused with "You've hit your session limit · resets 7:30pm (UTC)". The portal
filed it as an error, its report never arrived, and the next run on the project
started the same feature over from the journal.

The CLI process is gone by the time the portal hears about the limit, so
"pause" here cannot be the between-turns hold of app/midrun.py. It is the next
best thing: the run's row goes to a `paused` status instead of `error`, keeps
the CLI session id the stream announced, and once the window reopens the
worker puts the same row back in flight with `claude --resume <session>` and a
short prompt saying what happened. The agent wakes with its whole conversation
- what it was doing, what it found, what it still owed - and the workspace
exactly as it left it. Nothing about the run's identity changes: same row,
same run page, same log (appended to, not replaced), and the cost of every
segment is summed onto it when the last one settles.

What decides when to resume, in order of how much the failure said: the reset
in Anthropic's own 429 headers when the CLI retried before giving up; the time
the refusal itself prints ("resets 7:30pm (UTC)", read by
`apiretry.parse_reset_hint`); and only then the usage endpoint's second
opinion. Whichever it is, the worker's global backoff is set to the same
moment, so nothing else spawns into the wall either.

The bounds, and why each exists:

* A run is paused only when it has a session to resume. A refusal on the very
  first call (run 1486, one turn, $0) has nothing to continue; it stays an
  error, and the project's next scheduled run - with a fresher prompt - is the
  better continuation.
* At most `MAX_RESUMES` resumes per run. A weekly window that is spent stays
  spent for days; the backoff is capped at six hours, so a run could otherwise
  be woken into the same wall every six hours indefinitely. Each wake costs
  one spawn and no tokens, but a run that has been resumed three times and is
  still not through is not going to be, and its row should say so.
* A paused run still owns its project. `db.busy_project_ids` counts it, so the
  scheduler will not put a fresh agent into the workspace the paused one is
  going to come back to.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from app import db

log = logging.getLogger("portal.limitpause")

STATUS = "paused"

# How many times one run may be woken after a usage-window pause before the
# portal stops trying and records the run as errored.
MAX_RESUMES = 3

# The hook_events rows this module writes: event 'midrun' (so the run page's
# "while it ran" list shows them beside pauses and resumes a person made),
# tool 'limit', and these decisions.
_TOOL = "limit"
PAUSED = "limit_paused"
RESUMED = "limit_resumed"
GAVE_UP = "limit_gave_up"


def _parse(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%H:%M UTC on %b %d")


def reset_moment(quota, hint: Optional[datetime], fallback: datetime) -> datetime:
    """When to wake a paused run: the reset in the 429 headers when the CLI
    saw one, else the time its refusal printed, else the scheduler's backoff
    - the first two uncapped, because they are the truth and the cap is for
    the scheduler's benefit, not this run's."""
    now = datetime.now(timezone.utc)
    if quota is not None and quota.resets_at and quota.resets_at > now:
        return quota.resets_at
    if hint is not None and hint > now:
        return hint
    return fallback


def resumes_so_far(run_id: int) -> int:
    return db.count_hook_events(run_id, "midrun", RESUMED)


def can_pause(result) -> bool:
    """Whether this failure is one the run can come back from: it hit the
    usage limit, and the CLI got far enough to have a session to resume."""
    return bool(result.is_rate_limited and result.session_id)


def pause(project, run_id: int, task: str, result, until: datetime, why: str) -> bool:
    """Park the run until `until`. Returns False - and records why - when the
    run has already been resumed `MAX_RESUMES` times, in which case the caller
    settles it as the error it has become."""
    resumed = resumes_so_far(run_id)
    if resumed >= MAX_RESUMES:
        db.add_hook_event(
            run_id, "midrun", _TOOL, GAVE_UP,
            f"Hit the usage limit again after {resumed} resumes; not resuming a "
            f"{ordinal(resumed + 1)} time.",
        )
        return False
    stamp = until.astimezone(timezone.utc).isoformat(timespec="seconds")
    db.finish_run(
        run_id, STATUS, result.session_id, result.cost_usd, result.num_turns,
        f"Paused for the usage window ({why}); resumes in the same session after {_fmt(until)}.",
    )
    db.set_run_scope_record(run_id, "hold_state", json.dumps({"limit_until": stamp, "why": why}))
    db.add_hook_event(
        run_id, "midrun", _TOOL, PAUSED,
        f"Ran out of allowance ({why}). Paused, not stopped: the run resumes in the "
        f"same session once the window reopens.",
        stamp,
    )
    db.add_journal(
        project["id"], "system", "status",
        f"Run ({task}) ran out of allowance mid-run ({why}) and is **paused, not stopped**: "
        f"it picks up in the same session, with the workspace as it left it, once the "
        f"window reopens at {_fmt(until)}."
        + (f" This is resume {resumed + 1} of at most {MAX_RESUMES}." if resumed else ""),
    )
    log.info("Run %s paused for the usage window until %s (%s)", run_id, stamp, why)
    return True


def resume_after(row) -> Optional[datetime]:
    """When a paused row may be woken, off its persisted hold record - or None
    for a paused row with no readable record, which is woken at once rather
    than left forever."""
    raw = db._row_get(row, "hold_state")  # noqa: SLF001
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return _parse(str(data.get("limit_until") or ""))


def describe(row) -> Optional[dict]:
    """What the run page says about a paused row: when it resumes, and why it
    stopped. None for any other row."""
    if row is None or row["status"] != STATUS:
        return None
    until = resume_after(row)
    raw = db._row_get(row, "hold_state")  # noqa: SLF001
    why = ""
    try:
        why = str((json.loads(raw) if raw else {}).get("why") or "")
    except ValueError:
        pass
    return {
        "until": until,
        "until_text": _fmt(until) if until else "the window reopens",
        "why": why,
        "resumes": resumes_so_far(int(row["id"])),
        "max_resumes": MAX_RESUMES,
    }


def due(now: Optional[datetime] = None) -> list:
    """The paused runs whose window has reopened, oldest first."""
    now = now or datetime.now(timezone.utc)
    ready = []
    for row in db.paused_runs():
        until = resume_after(row)
        if until is None or until <= now:
            ready.append(row)
    return ready


def mark_resumed(row, now: Optional[datetime] = None) -> str:
    """Record the wake on the run's timeline and journal, and return the
    prompt the resumed session is handed."""
    now = now or datetime.now(timezone.utc)
    paused_at = _parse(str(row["ended_at"] or "")) or now
    held = now - paused_at
    minutes = max(0, int(held.total_seconds() // 60))
    held_text = f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"
    count = resumes_so_far(int(row["id"])) + 1
    db.add_hook_event(
        int(row["id"]), "midrun", _TOOL, RESUMED,
        f"Resumed after {held_text} paused for the usage window (resume {count} of at most "
        f"{MAX_RESUMES}).",
    )
    if row["project_id"]:
        db.add_journal(
            int(row["project_id"]), "system", "status",
            f"Run #{row['id']} resumed after {held_text} paused for the usage window; it "
            "continues in the same session from where it stopped.",
        )
    return resume_prompt(paused_at, now, held_text)


def resume_prompt(paused_at: datetime, now: datetime, held_text: str) -> str:
    """What the agent reads when its session is resumed. Short on purpose:
    the whole conversation is already in front of it."""
    return (
        "## Resumed after the usage window reopened\n\n"
        f"This run was paused at {_fmt(paused_at)} because the account's usage "
        f"allowance ran out mid-run, and resumed now, {held_text} later, in the same "
        "session. Nothing else happened in between: no other agent touched the "
        "workspace, and every file is exactly as you left it (check `git status` if "
        "you had uncommitted work). Pick up where you stopped and finish the task - "
        "do not start over, and do not repeat work that is already committed. You "
        "still owe the StructuredOutput report at the end; if you had already "
        "gathered its contents, file it now."
    )


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

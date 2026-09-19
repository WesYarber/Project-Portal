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

* A run that never got going comes back too, by starting over rather than
  resuming. Wes, 2026-09-19: "whether a run gets cut off mid-run or never
  starts because it is waiting for usage limit to refresh, have them start
  working again after the 5h window rolls over." A refusal on the very first
  call (run 1486, one turn, $0) has no session to continue and no work to
  lose, so its row is paused in `RESTART` mode and re-run from the top - with
  a prompt built fresh at wake time - when the window reopens. That used to be
  an error on the floor, which was survivable for a *scheduled* run (the next
  tick would have picked the project up anyway) and a dead end for a manual
  one: Wes pressed the button, the run died on the launch pad, and nothing
  ever brought it back.
* At most `MAX_RESUMES` resumes per run. A weekly window that is spent stays
  spent for days; the backoff is capped at six hours, so a run could otherwise
  be woken into the same wall every six hours indefinitely. Each wake costs
  one spawn and no tokens, but a run that has been resumed three times and is
  still not through is not going to be, and its row should say so.
* A paused run still owns its project. `db.busy_project_ids` counts it, so the
  scheduler will not put a fresh agent into the workspace the paused one is
  going to come back to. The same holds for a one-off task
  (`db.oneoff_busy`), where the thing being held is the task's workspace and
  its CLI session.

One-off tasks (app/oneoff.py) get the same hold, which matters more there than
anywhere: a one-off is started by hand and nothing else ever picks one up, so
before this the person's message simply died on a spent allowance with "send
your message again" under it. The only difference is where the notice goes -
the chat thread rather than a project journal - and one extra rule: a restart
puts the messages that run took out of the queue back into it, because a run
refused before the CLI announced a session provably never read them.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

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
RESTARTED = "limit_restarted"
GAVE_UP = "limit_gave_up"
# The same two wakes, pressed by a person rather than reached by the tick.
# Separate decisions precisely so `resumes_so_far` does not count them: the
# cap exists to stop the portal waking a run into the same shut window
# forever, and somebody pressing a button is not that.
HAND_RESUMED = "limit_resumed_by_hand"
HAND_RESTARTED = "limit_restarted_by_hand"

# What waking a paused row means. RESUME continues the CLI session the run
# already had; RESTART re-runs the task from the top because the refusal came
# before the CLI had a session to continue. Stored on the row's hold record so
# the decision is made once, where the failure is still in hand.
RESUME = "resume"
RESTART = "restart"


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
    """Every *automatic* wake counts against `MAX_RESUMES`, whichever kind it
    was: a run woken three times into the same shut window is no more likely
    to get through by starting over than by resuming.

    A wake somebody pressed the button for does not count, and is written
    down under its own decision (`HAND_RESUMED`/`HAND_RESTARTED`) so it
    cannot. The cap is a guard against the portal looping, not a ration on
    what a person may ask for.
    """
    return (db.count_hook_events(run_id, "midrun", RESUMED)
            + db.count_hook_events(run_id, "midrun", RESTARTED))


def can_pause(result) -> bool:
    """Whether this failure is one the run can come back from. Every usage
    limit is: with a session it resumes, without one it starts over."""
    return bool(result.is_rate_limited)


def mode_for(result) -> str:
    """Resume the CLI session when the stream announced one, else start over.
    No session means the refusal landed before the model did any work, so
    there is nothing to continue and nothing to duplicate by re-running."""
    return RESUME if result.session_id else RESTART


def _park(run_id: int, result, until: datetime, why: str) -> Optional[dict]:
    """Settle the row as paused and write down how it is to come back.

    The half of a pause that is the same wherever the run came from. None -
    and a GAVE_UP event - when the run has already been woken `MAX_RESUMES`
    times, in which case the caller settles it as the error it has become.
    The caller announces the pause afterwards, because where that notice goes
    is the one thing a project run and a one-off task do not share: a project
    has a journal, a one-off has the chat thread the person is reading.
    """
    resumed = resumes_so_far(run_id)
    if resumed >= MAX_RESUMES:
        db.add_hook_event(
            run_id, "midrun", _TOOL, GAVE_UP,
            f"Hit the usage limit again after {resumed} resumes; not resuming a "
            f"{ordinal(resumed + 1)} time.",
        )
        return None
    mode = mode_for(result)
    stamp = until.astimezone(timezone.utc).isoformat(timespec="seconds")
    carry_on = "resumes in the same session" if mode == RESUME else "starts over"
    db.finish_run(
        run_id, STATUS, result.session_id, result.cost_usd, result.num_turns,
        f"Paused for the usage window ({why}); {carry_on} after {_fmt(until)}.",
    )
    db.set_run_scope_record(
        run_id, "hold_state",
        json.dumps({"limit_until": stamp, "why": why, "mode": mode}),
    )
    db.add_hook_event(
        run_id, "midrun", _TOOL, PAUSED,
        f"Ran out of allowance ({why}). Paused, not stopped: the run {carry_on} "
        f"once the window reopens.",
        stamp,
    )
    log.info("Run %s paused for the usage window until %s (%s, %s)", run_id, stamp, why, mode)
    return {"mode": mode, "resumed": resumed, "stamp": stamp}


def _wake_suffix(held: dict) -> str:
    if not held["resumed"]:
        return ""
    return f" This is wake {held['resumed'] + 1} of at most {MAX_RESUMES}."


def pause(project, run_id: int, task: str, result, until: datetime, why: str) -> bool:
    """Park a project run until `until`. Returns False - and records why - when
    the run has already been resumed `MAX_RESUMES` times."""
    held = _park(run_id, result, until, why)
    if held is None:
        return False
    if held["mode"] == RESUME:
        line = (
            f"Run ({task}) ran out of allowance mid-run ({why}) and is **paused, not stopped**: "
            f"it picks up in the same session, with the workspace as it left it, once the "
            f"window reopens at {_fmt(until)}."
        )
    else:
        line = (
            f"Run ({task}) could not start - the usage allowance was already spent ({why}) - "
            f"and is **paused, not dropped**: nothing had been done yet, so it starts over "
            f"on this same row once the window reopens at {_fmt(until)}."
        )
    db.add_journal(project["id"], "system", "status", line + _wake_suffix(held))
    return True


def pause_oneoff(
    task_id: int, run_id: int, result, until: datetime, why: str,
    delivered_ids: Sequence[int] = (),
) -> bool:
    """Park a one-off task's run until `until` (app/oneoff.py).

    Same hold as a project run, said in the chat thread instead of a journal,
    because that page is what the person who typed the message is looking at.

    `delivered_ids` are the messages this run took out of the queue on its way
    in. On a restart they go back: no session was ever announced, so the model
    never read them, and a run that starts over with an empty queue would
    answer a prompt with nobody's words in it. On a resume they stay spent -
    the conversation being resumed already has them in it, and delivering them
    twice would have the agent answer the same message again.
    """
    held = _park(run_id, result, until, why)
    if held is None:
        return False
    if held["mode"] == RESTART:
        db.undeliver_oneoff_messages(list(delivered_ids))
        line = (
            f"The usage allowance was already spent when this run asked ({why}), so nothing "
            f"was done and your message has not been read yet. It is **queued, not lost**: "
            f"the run starts over on its own once the window reopens at {_fmt(until)}. "
            f"Nothing to send again."
        )
    else:
        line = (
            f"This run ran out of allowance mid-run ({why}) and is **paused, not stopped**: "
            f"it picks up in the same session, with this task's workspace as it left it, "
            f"once the window reopens at {_fmt(until)}. Nothing to send again."
        )
    db.add_oneoff_message(task_id, "system", line + _wake_suffix(held), run_id=run_id)
    return True


def _hold(row) -> dict:
    """The row's persisted hold record, or {} when there is none to read."""
    raw = db._row_get(row, "hold_state")  # noqa: SLF001
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def hold_mode(row) -> str:
    """Whether waking this row resumes its session or starts it over.

    Read off the hold record, but never trusted over the row itself: a row
    with no session cannot be resumed whatever it says, and a row paused
    before this field existed is judged the way it was judged then.
    """
    if not db._row_get(row, "session_id"):  # noqa: SLF001
        return RESTART
    mode = str(_hold(row).get("mode") or "")
    return RESTART if mode == RESTART else RESUME


def resume_after(row) -> Optional[datetime]:
    """When a paused row may be woken, off its persisted hold record - or None
    for a paused row with no readable record, which is woken at once rather
    than left forever."""
    data = _hold(row)
    if not data:
        return None
    return _parse(str(data.get("limit_until") or ""))


def wake_blocked(row, now: Optional[datetime] = None) -> str:
    """Why this run cannot be woken by hand right now - "" when it can be.

    There is exactly one reason, and it is not a refusal to argue with: the
    usage window it is waiting for has not reopened. Waking a run into a
    spent allowance does not get it further; the CLI refuses on its first
    call and the run is parked again, a few cents and one wake later. So the
    button is drawn grayed out with this sentence under it rather than
    removed - never removed, and never silently disabled either.

    `MAX_RESUMES` is deliberately not a reason. A run that has used up its
    wakes is not paused any more (`_park` settles it as an error instead), so
    a paused row can never be at the cap.
    """
    if row is None or _row_status(row) != STATUS:
        return "this run is not paused for the usage window"
    until = resume_after(row)
    now = now or datetime.now(timezone.utc)
    if until is not None and until > now:
        return (
            f"the usage window is still shut - it reopens at {_fmt(until)}, and waking "
            "the run before then would spend one of its wakes on another refusal"
        )
    return ""


def _row_status(row) -> str:
    try:
        return str(row["status"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def describe(row) -> Optional[dict]:
    """What the run page says about a paused row: when it resumes, and why it
    stopped. None for any other row."""
    if row is None or row["status"] != STATUS:
        return None
    until = resume_after(row)
    mode = hold_mode(row)
    return {
        "until": until,
        "until_text": _fmt(until) if until else "the window reopens",
        "why": str(_hold(row).get("why") or ""),
        "resumes": resumes_so_far(int(row["id"])),
        "max_resumes": MAX_RESUMES,
        "mode": mode,
        "restarts": mode == RESTART,
        # "" when the run page's "resume now" button is live; otherwise the
        # sentence printed under the grayed-out button. See `wake_blocked`.
        "wake_blocked": wake_blocked(row),
        # What the run page says it will do. A run with a session picks its
        # conversation back up; one that never got a session never started,
        # so there is nothing to pick up and it runs from the top.
        "what_next": (
            "It resumes in the same session, with the workspace as it left it,"
            if mode == RESUME else
            "Nothing had been done yet, so it starts over on this same row,"
        ),
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


def held_for(row, now: Optional[datetime] = None) -> tuple[datetime, str]:
    """When the row was parked, and how long ago that reads as."""
    now = now or datetime.now(timezone.utc)
    paused_at = _parse(str(row["ended_at"] or "")) or now
    minutes = max(0, int((now - paused_at).total_seconds() // 60))
    return paused_at, (f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m")


def _say(row, line: str) -> None:
    """Tell whoever is watching this run that it woke. A project run's audience
    reads the journal; a one-off task's reads the chat thread it was typed in.
    A run with neither (there is no third kind today) is left to its own
    timeline, which every run gets."""
    if row["project_id"]:
        db.add_journal(int(row["project_id"]), "system", "status", line)
    elif db._row_get(row, "oneoff_id"):  # noqa: SLF001
        db.add_oneoff_message(
            int(row["oneoff_id"]), "system", line, run_id=int(row["id"])
        )


def mark_resumed(row, now: Optional[datetime] = None, by: str = "") -> str:
    """Record the wake on the run's timeline and journal, and return the
    prompt the resumed session is handed.

    `by` is the person who pressed "resume now" on the run page, "" when the
    tick got here by itself. A hand wake is written under `HAND_RESUMED` and
    so does not count against `MAX_RESUMES` - see `resumes_so_far`.
    """
    now = now or datetime.now(timezone.utc)
    paused_at, held_text = held_for(row, now)
    if by:
        db.add_hook_event(
            int(row["id"]), "midrun", _TOOL, HAND_RESUMED,
            f"Resumed by {by} after {held_text} paused for the usage window, without "
            f"waiting for the window; this does not count against the {MAX_RESUMES} "
            "automatic wakes.",
        )
        _say(row, f"Run #{row['id']} was resumed by {by} after {held_text} paused for the "
                  "usage window; it continues in the same session from where it stopped.")
    else:
        count = resumes_so_far(int(row["id"])) + 1
        db.add_hook_event(
            int(row["id"]), "midrun", _TOOL, RESUMED,
            f"Resumed after {held_text} paused for the usage window (resume {count} of at most "
            f"{MAX_RESUMES}).",
        )
        _say(row, f"Run #{row['id']} resumed after {held_text} paused for the usage window; it "
                  "continues in the same session from where it stopped.")
    if db._row_get(row, "oneoff_id"):  # noqa: SLF001
        return oneoff_resume_prompt(paused_at, now, held_text)
    return resume_prompt(paused_at, now, held_text)


def mark_restarted(row, now: Optional[datetime] = None, by: str = "") -> None:
    """Record the wake of a run that never got going. No prompt is returned:
    a restart is an ordinary run of the task, built fresh at wake time, which
    is the whole reason this case starts over rather than resuming.

    `by` is the person who pressed "resume now", as in `mark_resumed`, and
    the same exemption from `MAX_RESUMES` applies.
    """
    now = now or datetime.now(timezone.utc)
    _, held_text = held_for(row, now)
    if by:
        db.add_hook_event(
            int(row["id"]), "midrun", _TOOL, HAND_RESTARTED,
            f"Started over by {by} after {held_text} waiting for the usage window; this "
            f"does not count against the {MAX_RESUMES} automatic wakes.",
        )
        _say(row, f"Run #{row['id']}, which never started because the usage allowance was "
                  f"spent, was started by {by} after {held_text} waiting. It starts from "
                  "the top: nothing had been done to pick up.")
        return
    count = resumes_so_far(int(row["id"])) + 1
    db.add_hook_event(
        int(row["id"]), "midrun", _TOOL, RESTARTED,
        f"Started over after {held_text} waiting for the usage window (wake {count} of at "
        f"most {MAX_RESUMES}).",
    )
    _say(row, f"Run #{row['id']}, which never started because the usage allowance was spent, "
              f"is running now that the window has reopened ({held_text} later). It starts "
              "from the top: nothing had been done to pick up.")


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


def oneoff_resume_prompt(paused_at: datetime, now: datetime, held_text: str) -> str:
    """The same wake, for a one-off task (app/oneoff.py). Different in the two
    places a one-off differs: there is no report file to file, and the last
    thing the agent prints IS the reply the person reads, so a resumed run
    that says nothing leaves them looking at a page with no answer on it."""
    return (
        "## Resumed after the usage window reopened\n\n"
        f"This run was paused at {_fmt(paused_at)} because the account's usage "
        f"allowance ran out mid-run, and resumed now, {held_text} later, in the same "
        "session. Nothing else happened in between: nobody touched the workspace, and "
        "every file is exactly as you left it. Pick up where you stopped and finish "
        "the task - do not start over. Whatever you print at the end is still your "
        "reply on the task page, so finish with one: what you did, what you found, "
        "and anything you need. Say plainly that the run was held for the usage "
        "window if that changed what you managed to get done."
    )


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

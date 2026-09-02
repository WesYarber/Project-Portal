"""What a live run can still hear: a pause, and a note typed while it works.

Wes, 2026-09-02: "is it possible to pause runs? with no or minimal token
waste? or sort of add in additional context while the model is still running
as well?"

Both ride the same wire. Every hooked run posts each tool call to the portal
from its PostToolUse hook (app/hookrelay.py -> /hooks/post-tool), and the CLI
waits for that hook to answer before the tool's result goes back to the model.
That wait is the whole mechanism:

* **Pause.** While a run is paused, the portal answers the relay with a poll
  address instead of a verdict, and the relay keeps asking (/hooks/hold) until
  the answer changes. The CLI is between turns the whole time - the tool has
  run, the next API call has not started - so nothing is being generated and
  nothing is being spent. Resume lets the relay answer and the run carries on
  from exactly where it stood. A pause takes effect at the run's NEXT tool
  call, not instantly: a model part-way through a long reply could only be
  interrupted by discarding that reply and paying for it again on resume,
  which is the token waste he asked to avoid. The page says "pausing" until
  the hold has engaged and "paused" from then on, so the difference is never
  hidden.

* **A note mid-flight.** A note typed while a run is in the workspace used to
  wait for the next run (`worker._rerun_for_unseen_notes`). Now the relay's
  answer carries it as the hook's `additionalContext`, which the CLI injects
  into the model's context beside the tool result - the agent reads it before
  its next move, in the same session, with nothing restarted. The note is
  stamped delivered to that run, exactly as a prompt build would stamp it, so
  no second run is queued afterwards to say the same thing again. "queue note"
  keeps its meaning: a note pressed as queued is for the next run only.

The two compose: pause a run, type several notes, resume - and the run reads
all of them at once when it wakes.

What this cannot do, and says so: a run that predates a service restart has no
hook scope (the registry is in memory, like hookguard's), so it can neither
pause nor hear - `pause()` refuses rather than pretending. A run waiting inside
`mcp__portal__ask` makes no tool calls until answered, so a pause pressed then
engages only once the answer arrives. And a note that lands after the run has
filed its report is left for the next run: injecting it behind a StructuredOutput
call would hand an instruction to an agent that is already finishing.

Time: the run's wall-clock budget (`run_timeout_min`) counts running time only.
`paused_seconds` is added to the deadline by `agent_runner._supervise`, so an
hour on hold is not an hour closer to being killed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from app import config, db

log = logging.getLogger("portal.midrun")

# The PostToolUse hook's timeout while a run can be held. The CLI kills a hook
# that runs past its timeout and proceeds as if it had said nothing - which
# for a paused run means the pause silently ends. Six hours is longer than any
# hold Wes would leave a run on without deciding to stop it instead.
HOOK_TIMEOUT_SEC = 6 * 60 * 60

# How often a held relay asks whether it may go on. Seconds; the relay reads
# it from the portal's answer, so it is tunable here without touching the
# script that ships with every run.
POLL_INTERVAL_SEC = 3

# The tool whose completion means the run is finishing. A note injected after
# it would reach an agent that has already reported.
_REPORT_TOOL = "StructuredOutput"


@dataclass
class _Hold:
    run_id: int
    paused_at: Optional[float] = None  # monotonic; None while running
    paused_total: float = 0.0  # seconds spent in pauses already over
    engaged: bool = False  # the relay is actually holding, not just asked to
    engaged_at: str = ""
    heard: int = 0  # notes delivered to this run mid-flight


# In memory on purpose: "this run is on hold" is only ever true while a relay
# process is polling, and a restart ends both the process and the truth.
_HOLDS: dict[int, _Hold] = {}


def enabled() -> bool:
    return (db.get_setting("midrun") or "1") != "0"


def end(run_id: int) -> None:
    _HOLDS.pop(run_id, None)


# -- pause / resume ---------------------------------------------------------

PAUSE_RESULTS = ("paused", "already_paused", "not_running", "cannot_hear")
RESUME_RESULTS = ("resumed", "not_paused")


def can_hear(run_id: int) -> bool:
    """Whether this run's tool calls reach the portal at all - the precondition
    for both pausing it and handing it a note. False for a run started before
    the last service restart (its hook scope died with that process) and for
    any run spawned without the PostToolUse hook."""
    from app import hookguard

    return hookguard.hears_midrun(run_id)


def pause(run_id: int, by: str = "") -> str:
    """Ask a live run to hold at its next tool call. Returns one of
    PAUSE_RESULTS; only "paused" changed anything."""
    run = db.get_run(run_id)
    if run is None or run["status"] != "running":
        return "not_running"
    if not can_hear(run_id):
        return "cannot_hear"
    hold = _HOLDS.setdefault(run_id, _Hold(run_id=run_id))
    if hold.paused_at is not None:
        return "already_paused"
    hold.paused_at = time.monotonic()
    hold.engaged = False
    hold.engaged_at = ""
    who = f" by {by}" if by else ""
    _event(run_id, "pause", "hold", f"Pause requested{who}; the run holds at its next tool call.")
    _journal(run, f"Run #{run_id} paused{who}. It holds at its next tool call, spending nothing, "
                  "until resumed - and reads any note added meanwhile when it wakes.")
    log.info("Run %s paused%s", run_id, who)
    return "paused"


def resume(run_id: int, by: str = "") -> str:
    """Let a held run go on. Returns one of RESUME_RESULTS."""
    hold = _HOLDS.get(run_id)
    if hold is None or hold.paused_at is None:
        return "not_paused"
    held_for = time.monotonic() - hold.paused_at
    hold.paused_total += held_for
    hold.paused_at = None
    engaged = hold.engaged
    hold.engaged = False
    hold.engaged_at = ""
    who = f" by {by}" if by else ""
    how = "after holding" if engaged else "before the hold engaged,"
    _event(run_id, "resume", "resume", f"Resumed{who} {how} {_humanize(held_for)}.")
    run = db.get_run(run_id)
    if run is not None:
        _journal(run, f"Run #{run_id} resumed{who} after {_humanize(held_for)} on hold.")
    log.info("Run %s resumed%s after %.0fs", run_id, who, held_for)
    return "resumed"


def is_paused(run_id: Optional[int]) -> bool:
    hold = _HOLDS.get(run_id) if run_id is not None else None
    return hold is not None and hold.paused_at is not None


def paused_seconds(run_id: Optional[int]) -> float:
    """Every second this run has spent on hold, the current pause included.
    What the supervisor adds to the run's deadline."""
    if run_id is None:
        return 0.0
    hold = _HOLDS.get(run_id)
    if hold is None:
        return 0.0
    total = hold.paused_total
    if hold.paused_at is not None:
        total += time.monotonic() - hold.paused_at
    return total


def state(run_id: Optional[int]) -> dict:
    """What a page shows about a run's hold: whether it is paused, whether the
    hold has actually engaged yet, and whether it could be paused at all."""
    if run_id is None:
        return {"paused": False, "engaged": False, "can_pause": False, "heard": 0}
    hold = _HOLDS.get(run_id)
    paused = hold is not None and hold.paused_at is not None
    return {
        "paused": paused,
        "engaged": bool(paused and hold.engaged),
        "can_pause": enabled() and can_hear(run_id),
        "heard": hold.heard if hold else 0,
    }


def paused_run_ids() -> set[int]:
    return {rid for rid, h in _HOLDS.items() if h.paused_at is not None}


# -- the hook side ------------------------------------------------------------

def poll_url(run_id: int, token: str) -> str:
    return f"http://127.0.0.1:{config.PORT}/hooks/hold?run={run_id}&token={token}"


def after_tool_call(run_id: int, token: str, payload: dict) -> dict:
    """The post-tool endpoint's answer beyond the audit row.

    `{}` lets the run proceed untouched; `{"poll": url, "interval": s}` tells
    the relay to hold and ask again; `{"hook_output": {...}}` carries a note
    as the hook's additionalContext. Never raises - a bug here must cost at
    most one unheard note, never a stuck run."""
    from app import hookguard

    try:
        if not enabled() or not hookguard.authorized(run_id, token) or not can_hear(run_id):
            return {}
        if str(payload.get("tool_name") or "") == _REPORT_TOOL:
            # The run is finishing. Leave any note for the next run.
            return {}
        hold = _HOLDS.get(run_id)
        if hold is not None and hold.paused_at is not None:
            if not hold.engaged:
                hold.engaged = True
                hold.engaged_at = db.now()
                _event(run_id, "pause", "held", "Holding at this tool call. Nothing is being spent.")
            return {"poll": poll_url(run_id, token), "interval": POLL_INTERVAL_SEC}
        return inject_notes(run_id)
    except Exception:  # noqa: BLE001 - fail open, always
        log.exception("midrun.after_tool_call failed for run %s; proceeding", run_id)
        return {}


def hold_poll(run_id: int, token: str) -> dict:
    """A held relay asking whether it may go on. Same answers as
    `after_tool_call`, minus the report-tool rule (the held call already
    passed it)."""
    from app import hookguard

    try:
        if not hookguard.authorized(run_id, token) or not can_hear(run_id):
            return {}
        hold = _HOLDS.get(run_id)
        if hold is not None and hold.paused_at is not None:
            return {"poll": poll_url(run_id, token), "interval": POLL_INTERVAL_SEC}
        return inject_notes(run_id)
    except Exception:  # noqa: BLE001
        log.exception("midrun.hold_poll failed for run %s; releasing", run_id)
        return {}


def inject_notes(run_id: int) -> dict:
    """Hand this run whatever notes have arrived since its prompt was built,
    stamping them delivered so the run that follows does not get them again.
    `{}` when there is nothing to say."""
    run = db.get_run(run_id)
    if run is None or not run["project_id"]:
        return {}
    project = db.get_project(int(run["project_id"]))
    if project is None:
        return {}
    rows = hearable_notes(int(project["id"]))
    if not rows:
        return {}
    from app import attachments

    # The files that came with these notes go into the workspace first, so the
    # paths the block names exist by the time the agent reads it.
    attachments.reveal(int(project["id"]), project["slug"])
    text = render(rows, run_id)
    ids = [int(r["id"]) for r in rows]
    db.mark_notes_delivered(ids)
    hold = _HOLDS.setdefault(run_id, _Hold(run_id=run_id))
    hold.heard += len(rows)
    count = len(rows)
    noun = "note" if count == 1 else f"{count} notes"
    _event(run_id, "note", "heard", f"Read {noun} mid-run, typed while it was working.",
           _excerpt(rows[0]["content_md"] or ""))
    _journal(
        run,
        f"{'A note' if count == 1 else f'{count} notes'} delivered to the running agent "
        f"(run #{run_id}) at its next tool call - it reads {'it' if count == 1 else 'them'} "
        "now rather than a second run being queued for it afterwards.",
    )
    log.info("Run %s heard %d note(s) mid-run", run_id, count)
    return {
        "hook_output": {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": text,
            }
        }
    }


def hearable_notes(project_id: int) -> list[db.sqlite3.Row]:
    """The pending notes a live run may be handed: not the ones pressed as
    "queue note" (those are for the next run by definition), and not a voice
    memo whose transcript is still being written - the memo IS the note, and
    handing over "(transcription is still running)" would deliver nothing and
    stamp the note spent."""
    from app import attachments

    rows = [r for r in db.pending_notes(project_id) if not _for_next_run(r)]
    if not rows:
        return []
    transcribing: set[int] = set()
    try:
        for att in db.list_attachments(project_id):
            jid = db._row_get(att, "journal_id")  # noqa: SLF001
            if not jid:
                continue
            has_transcript = bool(db._row_get(att, "transcript", ""))  # noqa: SLF001
            if attachments.wants_transcript(att["mime"]) and not has_transcript:
                transcribing.add(int(jid))
    except Exception:  # noqa: BLE001 - a bad read here withholds nothing, on purpose
        log.exception("Could not check transcripts for project %s", project_id)
    return [r for r in rows if int(r["id"]) not in transcribing]


def _for_next_run(row) -> bool:
    return bool(db._row_get(row, "for_next_run", 0))  # noqa: SLF001


def render(rows: list[db.sqlite3.Row], run_id: int) -> str:
    """The block the agent reads. Framed as what it is - an instruction that
    arrived while it was working - and explicit that this run now owns it, so
    an agent that cannot act on it says so in its report instead of assuming
    the next run will pick it up."""
    from app import attachments, notes, people

    authors = [notes._author(row) for row in rows]  # noqa: SLF001
    named = [people.name_of(a) for a in authors]
    distinct = sorted(set(named))
    mixed = len(distinct) > 1
    count = len(rows)
    if mixed:
        who = ", ".join(distinct[:-1]) + " and " + distinct[-1]
        head = f"## {count} notes from {who}, typed while you were working"
    elif count == 1:
        head = f"## A note from {named[0]}, typed while you were working"
    else:
        head = f"## {count} notes from {named[0]}, typed while you were working"
    intro = (
        f"{'This arrived' if count == 1 else 'These arrived'} a moment ago, while this "
        "run was already under way, and reached you at your next tool call. "
        f"{'It is' if count == 1 else 'They are, oldest first,'} part of your instructions "
        "for THIS run: read before your next step and let it change what you do next. "
        f"{'It has' if count == 1 else 'They have'} been recorded as delivered to run "
        f"#{run_id}, so no other run will act on {'it' if count == 1 else 'them'} - if you "
        "cannot act on something here in this run, say so in your report and add a todo "
        "for it."
    )
    files_by_note: dict[int, list[str]] = {}
    try:
        project_id = int(rows[0]["project_id"])
        for att in db.list_attachments(project_id):
            jid = db._row_get(att, "journal_id")  # noqa: SLF001
            if jid and att["revealed_at"]:
                files_by_note.setdefault(int(jid), []).append(attachments.rel_path(att["stored_name"]))
    except Exception:  # noqa: BLE001 - the note still reads without its file list
        log.exception("Could not list files for a mid-run note")
    bodies = []
    for row, who in zip(rows, named):
        label = f"**[{row['ts']}]{f' {who}' if mixed else ''}**"
        body = (row["content_md"] or "").strip()
        files = files_by_note.get(int(row["id"]), [])
        if files:
            listed = ", ".join(f"`{f}`" for f in files)
            body += f"\n\nFile{'s' if len(files) > 1 else ''} that came with it, now in your workspace: {listed}"
        bodies.append(f"{label}\n{body}")
    return f"{head}\n{intro}\n\n" + "\n\n".join(bodies)


# -- bookkeeping ---------------------------------------------------------------

def _event(run_id: int, tool: str, decision: str, reason: str, detail: str = "") -> None:
    try:
        db.add_hook_event(run_id, "midrun", tool, decision, reason, detail or None)
    except Exception:  # noqa: BLE001 - the trail is best-effort
        log.exception("Could not record a midrun event for run %s", run_id)


def _journal(run, text: str) -> None:
    try:
        if run is not None and run["project_id"]:
            db.add_journal(int(run["project_id"]), "system", "status", text)
    except Exception:  # noqa: BLE001
        log.exception("Could not journal a midrun event for run %s", run["id"] if run else None)


def _humanize(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text[:200]

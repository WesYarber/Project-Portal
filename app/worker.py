"""Background worker: periodically advances projects via headless Claude runs."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Optional

from app import (
    agent_runner, config, crashloop, daycycle, db, hookguard, journalfile, limits, memory,
    modelwatch, notify, oneoff, orphans, pacing, people, preview, proof, quickreplies,
    report_schema, runlimit, runlog, selfreview, subprojects, todos,
)

log = logging.getLogger("portal.worker")

LOOP_INTERVAL_SEC = 60

# Manual "Run now" requests: a queue of project ids. The event wakes the
# worker loop out of its between-tick sleep, so a run Wes asked for starts
# immediately instead of at the next minute boundary.
manual_queue: "asyncio.Queue[int]" = asyncio.Queue()
_wake = asyncio.Event()


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _in_backoff() -> bool:
    backoff_until = db.get_setting("backoff_until") or ""
    dt = _parse_iso(backoff_until)
    if dt is None:
        return False
    return datetime.now(timezone.utc) < dt


async def _rate_limit_backoff() -> tuple[datetime, str]:
    """Set `backoff_until` after a run hit a usage limit, and say why.

    The old rule was a flat hour, which was a guess made without asking. The
    account knows exactly when the full window comes back, so ask it - a fresh
    reading, not the cached one, because the cache may predate the run that
    just failed and would therefore still show headroom. A failed fetch falls
    back to the flat hour, so this is strictly better-informed, never worse.
    """
    now = datetime.now(timezone.utc)
    try:
        snapshot = await limits.refresh_async()
    except Exception:  # noqa: BLE001 - never let the limit reader break a run's teardown
        log.exception("Could not read usage limits while backing off")
        snapshot = {}
    until = limits.backoff_until(snapshot, now=now)
    full = [w["label"] for w in (snapshot.get("windows") or []) if w["percent"] >= 95.0]
    if full:
        why = f"{', '.join(full)} window full"
    else:
        # Nothing account-wide is full - was it one model's own window (Fable
        # first)? Then a long global idle is pure waste: the very next spawn
        # resolves to the fallback model. Pause only long enough for teardown.
        scoped = [
            w for w in (snapshot.get("scoped") or [])
            if w["percent"] >= limits.FALLBACK_AT_PERCENT
        ]
        if scoped and any(
            limits.MODEL_WINDOW_NAMES.get(alias, "").lower() == str(w.get("model") or "").lower()
            for w in scoped for alias in limits.FALLBACK_MODELS
        ):
            until = now + timedelta(minutes=5)
            why = (
                f"{', '.join(w['label'] for w in scoped)} window full - "
                "runs fall back to another model until it resets"
            )
        else:
            why = f"{', '.join(w['label'] for w in scoped)} window full" if scoped \
                else "no live limit reading - flat 60 min"
    db.set_setting("backoff_until", until.isoformat(timespec="seconds"))
    return until, why


def require_build_approval() -> bool:
    """Whether writing code needs Wes's explicit OK first. On by default."""
    return (db.get_setting("require_build_approval") or "1") == "1"


def build_allowed(project: db.sqlite3.Row) -> bool:
    """May a run on this project write code? Approval, or the gate switched off."""
    return db.build_approved(project) or not require_build_approval()


def build_gated(project: db.sqlite3.Row) -> bool:
    """True when this project's agent has asked to start writing code and Wes
    has not answered - the "needs your OK" badge, the settings-page list, and
    a scheduling hold in one fact.

    The failure the gate exists to prevent actually happened: a pass over the
    backlog triaged every idea, each triage promoted itself to planning, each
    plan promoted itself to building, and the worker then built seventeen
    projects Wes had not decided to start. Triage and planning are cheap and
    reversible; writing code waits for him.
    """
    return (
        require_build_approval()
        and db.build_requested(project)
        and not db.build_approved(project)
    )


def task_for(project: db.sqlite3.Row, manual: bool = False) -> str:
    """The task a run on this project gets. Approved (or the gate off) means
    build - including a manual run on a review/done project, where continuing
    the work is the sensible reading of "run agent now". Unapproved means the
    agent may not write code whoever asked: a fresh idea with no completed run
    yet gets the triage pass (name it, scope it, check prior art), anything
    else a plan pass. Backlog projects are only ever reached manually - the
    scheduler no longer touches them ("just add it to the backlog and not feed
    it to a model yet")."""
    stage = project["stage"]
    if stage == "backlog":
        return "triage"
    if build_allowed(project):
        return "build"
    return "plan" if db.has_completed_project_run(project["id"]) else "triage"


def blocked_with_nothing_workable(project: db.sqlite3.Row) -> bool:
    """True when every remaining step waits on Wes. Being blocked does not stop
    scheduling by itself - the contract says work the todos that don't depend
    on the answer - but once none of those are left, another run could only
    repeat itself.

    "Workable" excludes items tagged 'blocked': a list where every open item
    wears the tag is the same fact as a blocked_on report, just stated per
    item, and it holds the project even when no question is open. An empty
    list with nothing else waiting keeps scheduling, as ever - on many
    projects the agent picks its own next chunk."""
    if db.count_workable_todos(project["id"]) > 0:
        return False
    waiting = bool(db.blocked_on(project)) or db.count_open_questions(project["id"]) > 0
    return waiting or db.count_open_todos(project["id"], owner="agent") > 0


def project_at_daily_cap(project: db.sqlite3.Row) -> bool:
    """True if this project has its own runs/day cap and has hit it today.
    A NULL/0 `projects.max_runs_per_day` means "no per-project limit"."""
    try:
        cap = project["max_runs_per_day"]
    except (IndexError, KeyError):  # row from a pre-migration query
        return False
    if not cap:
        return False
    return db.count_runs_today(project["id"]) >= int(cap)


def _pick_project(manual_project_id: Optional[int]) -> tuple[Optional[db.sqlite3.Row], bool]:
    """Returns (project_row, is_manual). Manual runs deliberately bypass the
    per-project cap - Wes asking for a run is the whole point - but never the
    one-run-per-project rule, since two agents in one workspace would fight
    over the same files and the same git checkout."""
    busy = db.running_project_ids()
    if manual_project_id is not None:
        proj = db.get_project(manual_project_id)
        if proj is not None and proj["id"] not in busy:
            return proj, True
    for candidate in db.list_schedulable_projects():
        if candidate["id"] in busy:
            continue
        if build_gated(candidate):
            continue
        if blocked_with_nothing_workable(candidate):
            continue
        # A project whose last runs died before the agent started gets spaced
        # out rather than retried every tick. Scheduled picks only - the manual
        # branch above is Wes explicitly asking, and is how he would test that
        # whatever broke is fixed. See app/crashloop.py.
        if crashloop.held(candidate["id"]):
            continue
        if not project_at_daily_cap(candidate):
            return candidate, False
    return None, False


def _pick_research() -> Optional[db.sqlite3.Row]:
    """The next project waiting for a research burst, or None.

    Only inside a spend-down window: queueing is a standing "when there is
    spare weekly allowance, go and read about this", not a request for a run
    now. Status and the build gate are both irrelevant here - a research pass
    writes RESEARCH.md and no code, so a backlog idea Wes has never approved is
    exactly the kind of thing worth spending expiring allowance on.
    """
    if not pacing.spending_down():
        return None
    busy = db.running_project_ids()
    for candidate in db.list_research_queued():
        if candidate["id"] not in busy:
            return candidate
    return None


def _pacing_reference() -> Optional[datetime]:
    """The instant the worker paces the next scheduled run from.

    With serial runs that was simply "when the last run ended". Runs are now
    parallel, so a long run that has not ended yet would otherwise leave the
    gate permanently open and the worker would fill every free slot instantly.
    Pacing from the most recent *start* while anything is in flight keeps the
    same one-run-per-interval rhythm, and parallelism happens when a run
    outlives the interval - which is exactly when it is worth having.
    """
    stamps = [_parse_iso(db.last_run_ended_at() or "")]
    if db.count_running():
        stamps.append(_parse_iso(db.last_run_started_at() or ""))
    known = [s for s in stamps if s is not None]
    return max(known) if known else None


def _seconds_until_scheduled() -> int:
    """0 when a scheduled run is due now."""
    interval_min = pacing.interval_min(int(db.get_setting("worker_interval_min") or "10"))
    reference = _pacing_reference()
    if reference is None:
        return 0
    due = reference + timedelta(minutes=interval_min)
    return max(0, int((due - datetime.now(timezone.utc)).total_seconds()))


async def _should_run_scheduled() -> bool:
    return _seconds_until_scheduled() == 0


def idle_reason() -> str:
    """Why no agent is running right now, phrased for the dashboard.

    Computed from the same predicates `_tick` decides with, so the answer can't
    drift from the real behavior. Empty string when a run is in flight.

    One deliberate difference from the tick's order: project eligibility is
    checked before the pacing interval. The tick checks pacing first because it
    is the cheaper test, but reporting "next run in 4 min" when there is
    nothing to run on would be a lie, and "nothing to work on" is the more
    useful answer.
    """
    if db.count_running():
        return ""
    if _pending_restart is not None:
        return (
            "holding new runs - a self-update is waiting to restart the "
            "service, which happens the moment the current runs finish"
        )
    if _in_backoff():
        left = _parse_iso(db.get_setting("backoff_until") or "")
        remaining = int((left - datetime.now(timezone.utc)).total_seconds()) if left else 0
        return f"backing off after a usage limit - retrying in {daycycle.humanize_seconds(remaining)}"
    if not manual_queue.empty():
        return "a manual run is queued and about to start"
    if (db.get_setting("worker_enabled") or "1") != "1":
        return "the worker is paused - switch it back on in settings"
    budget = pacing.run_budget(db.effective_max_runs())
    used = db.count_runs_today()
    if pacing.budget_applies() and used >= budget:
        resets_in = daycycle.humanize_seconds(daycycle.seconds_until_reset())
        return f"today's run budget is spent ({used}/{budget}) - it resets in {resets_in}"
    hold = pacing.scheduled_hold()
    if hold is not None:
        return pacing.hold_reason(hold)
    saturation = pacing.saturation_hold()
    if saturation is not None:
        return pacing.saturation_reason(saturation)
    project, _ = _pick_project(None)
    if project is None:
        actionable = db.list_schedulable_projects()
        if not actionable:
            return "no project is active and unpaused"
        waiting = [p for p in actionable if build_gated(p)]
        if waiting and len(waiting) == len(actionable):
            names = ", ".join(p["title"] for p in waiting[:3])
            more = f" and {len(waiting) - 3} more" if len(waiting) > 3 else ""
            return (
                f"waiting for your OK to start building - {names}{more} "
                f"(approve on the project page)"
            )
        if all(build_gated(p) or blocked_with_nothing_workable(p) for p in actionable):
            return "every active project is waiting on you (approval, an answer, or something only you can do)"
        return "every actionable project has hit its own per-project daily cap"
    wait = _seconds_until_scheduled()
    if wait:
        return (
            f"pacing the next run - {project['title']} starts in about "
            f"{daycycle.humanize_seconds(wait)}"
        )
    return f"about to start a run on {project['title']}"


async def worker_loop() -> None:
    log.info("Worker loop started")
    while True:
        try:
            await _tick()
        except Exception:  # noqa: BLE001 - the loop must never die
            log.exception("Worker tick failed")
        # Sleep until the next scheduled tick, or until `queue_manual_run`
        # wakes us - whichever comes first. The clear sits right before the
        # next tick, after the queue is already filled by whoever set the
        # event, so a wake can never be lost between the two.
        try:
            await asyncio.wait_for(_wake.wait(), timeout=LOOP_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass
        _wake.clear()


_audit_pruned_day: Optional[str] = None


def _daily_audit_prune() -> None:
    """Age the PostToolUse audit trail out of hook_events once a day. Denials
    and Stop bounces are kept; only the bulk 'what did this run do' rows age
    (db.AUDIT_RETENTION_DAYS). Best-effort - a failed prune waits a day."""
    global _audit_pruned_day
    today = datetime.now(timezone.utc).date().isoformat()
    if _audit_pruned_day == today:
        return
    _audit_pruned_day = today
    try:
        removed = db.prune_hook_audit()
        if removed:
            log.info("Aged %d hook-audit rows out of hook_events", removed)
    except Exception:  # noqa: BLE001
        log.exception("Hook-audit prune failed")


_model_checked_day: Optional[str] = None


async def _daily_model_check() -> None:
    """Ask Anthropic once a day whether it has shipped anything new, and tell
    Wes if it has (app/modelwatch.py). A model launch happens a few times a
    year, so daily is already generous; the day is stamped BEFORE the call so a
    failure waits until tomorrow rather than retrying every tick."""
    global _model_checked_day
    today = datetime.now(timezone.utc).date().isoformat()
    if _model_checked_day == today:
        return
    _model_checked_day = today
    try:
        result = await modelwatch.run_check()
        for model in result.get("new", []):
            log.info("New model spotted: %s", model.get("id"))
    except Exception:  # noqa: BLE001 - never let the watcher stop the worker
        log.exception("Model watch failed")


async def _tick() -> None:
    global _pending_restart
    _reap_inflight()
    _daily_audit_prune()
    await _daily_model_check()
    if _pending_restart is not None:
        # A self-update is waiting for the portal to go quiet. Start no
        # scheduled runs - one started now would be killed by the very restart
        # it is holding up - and fire the restart the moment the last run is
        # done. Manual runs are the exception: the queue is in-memory, so a
        # "run now" Wes pressed would be erased by the restart. Start those,
        # and let the restart wait for them like anything else.
        while not manual_queue.empty() and len(_inflight) < pacing.parallel_cap(db.max_parallel_runs()):
            if not await _start_one():
                break
        if not _inflight and manual_queue.empty():
            project_id, new_head = _pending_restart
            _pending_restart = None
            _fire_restart(project_id, new_head)
        return
    await _maybe_spend_down()
    if _in_backoff():
        await _maybe_reflect()
        await _maybe_compact()
        return

    # Keep filling free slots until something says no, rather than starting at
    # most one run per minute: queued manual runs should all go at once, and a
    # scheduled one still answers to the pacing interval inside `_start_one`.
    while len(_inflight) < pacing.parallel_cap(db.max_parallel_runs()):
        if not await _start_one():
            break

    await _maybe_reflect()
    await _maybe_compact()


def scheduled_work_enabled() -> bool:
    """The "run agents automatically" switch, as every scheduled job must read it.

    One definition, because there are three kinds of scheduled work now (project
    runs, the daily reflect, the learnings compaction) and two of them were
    reading nothing at all. A manual run from the UI deliberately does not
    consult this - pressing the button is the request.
    """
    return (db.get_setting("worker_enabled") or "1") == "1"


async def _start_one() -> bool:
    """Try to launch a single run. Returns True if one was started."""
    worker_enabled = scheduled_work_enabled()

    manual_project_id: Optional[int] = None
    if not manual_queue.empty():
        manual_project_id = await manual_queue.get()
        if manual_project_id in db.running_project_ids():
            # Already working on that project. Re-queue rather than drop the
            # request or put two agents in one workspace; the next tick that
            # finds it free will honor it.
            await manual_queue.put(manual_project_id)
            return False
    elif not worker_enabled:
        return False
    else:
        # The budget is counted against runs already *started*, which is why
        # the run row is created synchronously below before the next slot is
        # considered - otherwise two parallel launches in one tick would both
        # see the pre-launch count and overshoot the daily cap.
        if pacing.budget_applies() and db.count_runs_today() >= pacing.run_budget(db.effective_max_runs()):
            return False
        # Stop short of the real wall rather than discovering it mid-run. Only
        # scheduled runs: the manual branch above never reaches this.
        if pacing.scheduled_hold() is not None:
            return False
        # Leave deliberate idle gaps rather than saturating every window round
        # the clock. Scheduled only, and exempt during an explicit spend-down.
        if pacing.saturation_hold() is not None:
            return False
        # A queued research burst outranks the ordinary rotation *and* the
        # pacing interval: the whole point is to fill the free slots at once
        # while there is allowance about to expire. It is still under the hold
        # above and the parallel cap, which is what stops "burst" from meaning
        # "everything at once until something breaks".
        queued = _pick_research()
        if queued is not None:
            db.unqueue_research(queued["id"])
            db.add_journal(
                queued["id"], "system", "status",
                "Starting the queued research burst - spending weekly allowance that "
                "would otherwise expire.",
            )
            spawn_run(queued, "research")
            return True
        if not await _should_run_scheduled():
            return False

    project, is_manual = _pick_project(manual_project_id)
    if project is None:
        return False

    task = task_for(project, manual=is_manual)
    if task is None:
        return False
    spawn_run(project, task)
    return True


# Runs this process owns, keyed by run id. The runs table is the source of
# truth for *what* is running; this is how the loop knows when a slot frees up
# without waiting for the row to settle.
_inflight: dict[int, "asyncio.Task"] = {}


def _reap_inflight() -> None:
    for run_id, handle in list(_inflight.items()):
        if handle.done():
            _inflight.pop(run_id, None)
            exc = handle.exception() if not handle.cancelled() else None
            if exc is not None:
                log.error("Run %s crashed: %r", run_id, exc)


def spawn_run(project: db.sqlite3.Row, task: str) -> int:
    """Create the run row synchronously and execute it in the background.

    Splitting the row creation from the execution is what makes concurrency
    accounting honest: by the time this returns, both `count_runs_today()` and
    `running_project_ids()` already reflect the new run, so the next slot in
    the same tick cannot double-spend the budget or pick the same project.
    """
    model = agent_runner.resolve_model(project, task)
    run_id = db.create_run(project["id"], task, model)
    log.info("Running task=%s for project=%s (run_id=%s)", task, project["slug"], run_id)
    _inflight[run_id] = asyncio.create_task(_execute_run(project, task, run_id, model))
    return run_id


async def _execute_run(project: db.sqlite3.Row, task: str, run_id: int, model: str) -> None:
    try:
        await run_project_task(project, task, run_id=run_id, model=model)
    except Exception:  # noqa: BLE001 - a crashed run must not leave a 'running' row
        log.exception("Run %s failed", run_id)
        row = db.get_run(run_id)
        if row is not None and row["status"] == "running":
            db.finish_run(run_id, "error", summary="Run crashed; see the service log.")
    finally:
        # Whether it crashed or finished, this is the one place that sees every
        # completed run, so it is where "these keep dying on the launch pad"
        # gets noticed. Never allowed to break the caller: a failure to report
        # a crash loop must not itself become one.
        try:
            await _announce_crash_loop(project)
        except Exception:  # pragma: no cover - defensive
            log.exception("Could not check the crash-loop state for %s", project["slug"])


_CRASHLOOP_SETTING = "crashloop_announced_{}"


async def _announce_crash_loop(project: db.sqlite3.Row) -> None:
    """Tell Wes when a project's runs keep dying before the agent starts.

    The portal's failure on 2026-07-26 was not only that it retried 257 times -
    it was that it retried 257 times *silently*. Every one of those runs showed
    as a red row on a page nobody was looking at, and the first Wes knew of it
    was scrolling past a wall of them.
    """
    key = _CRASHLOOP_SETTING.format(project["id"])
    streak = crashloop.consecutive_dead_starts(project["id"])
    if streak <= 0:
        # A run got going, so the loop is over. Clearing the marker is what
        # lets a future recurrence announce itself instead of being silently
        # deduplicated against this one.
        if db.get_setting(key):
            db.set_setting(key, "0")
        return
    try:
        announced = int(db.get_setting(key) or "0")
    except ValueError:
        announced = 0
    if not crashloop.should_announce(streak, announced):
        return
    db.set_setting(key, str(streak))
    db.add_journal(project["id"], "system", "status", crashloop.note_for(streak))
    await notify.notify(
        f"{project['title']}: runs are failing to start",
        f"{streak} runs in a row died before the agent started. The portal is "
        f"backing off to every {crashloop.delay_min(streak)} minutes instead of "
        f"retrying every tick.",
        project_title=project["title"],
        project_id=project["id"],
    )


def _live_logger(run_id: int) -> agent_runner.EventCallback:
    """Stream a run's events to its log file and keep `runs.last_activity`
    current, so both the live console and the dashboard have something to show."""
    live = runlog.RunLog(run_id)
    state = {"events": 0, "last": ""}

    def on_event(event: dict, lines: list[str]) -> None:
        state["events"] += 1
        live.append(lines)
        if lines:
            state["last"] = lines[-1]
        db.update_run_activity(run_id, state["last"], state["events"])

    return on_event


def _guard_settings(run_id: int, project: Optional[db.sqlite3.Row]) -> Optional[str]:
    """The `--settings` JSON installing this run's hooks - the PreToolUse
    guardrail and/or the Stop-hook report nudge - or None to run unhooked.

    The meta-project is exempt from the write guardrail (editing the portal's
    source and data is its whole job) but still gets the report nudge: it must
    report like any other project run. `project` None means a one-off task,
    which is guarded to its own task workspace and never nudged - one-offs
    converse through their final text and file no report. Fails open - a bug
    here must never stop a run starting.
    """
    try:
        audit = hookguard.audit_enabled()
        if project is not None:
            is_meta = project["slug"] == config.META_PROJECT_SLUG
            pre_tool = hookguard.enabled() and not is_meta
            report_expected = hookguard.stop_nudge_enabled()
            if not pre_tool and not report_expected and not audit:
                return None
            if is_meta:
                allowed = [config.PROJECTS_DIR / project["slug"]]
            else:
                allowed = hookguard.family_workspaces(project)
            return hookguard.begin(
                run_id, allowed, report_expected=report_expected, pre_tool=pre_tool,
                audit=audit,
            )
        if not hookguard.enabled() and not audit:
            return None
        row = db.get_run(run_id)
        if row is None or not row["oneoff_id"]:
            return None
        return hookguard.begin(
            run_id, [oneoff.workspace(int(row["oneoff_id"]))],
            pre_tool=hookguard.enabled(), audit=audit,
        )
    except Exception:  # noqa: BLE001
        log.exception("Could not build guardrail settings for run %s", run_id)
        return None


async def run_project_task(
    project: db.sqlite3.Row,
    task: str,
    run_id: Optional[int] = None,
    model: Optional[str] = None,
) -> None:
    """Execute one task. `run_id` is passed in by `spawn_run`, which creates the
    row up front so the slot is accounted for before the coroutine starts; call
    without it to create the row here."""
    model = model or agent_runner.resolve_model(project, task)
    timeout_min = int(db.get_setting("run_timeout_min") or "30")
    max_turns = run_max_turns()
    workspace = config.PROJECTS_DIR / project["slug"]
    _ensure_workspace(workspace)

    if run_id is None:
        run_id = db.create_run(project["id"], task, model)
    # Strictly before the prompt is built. The prompt's journal section shortens
    # older entries and points at this file for their full text; writing it after
    # would let an entry created in between be shortened in the prompt and absent
    # from the file. Written first, the same race can only ever affect the newest
    # entry - which the prompt always shows whole. See app/journalfile.py.
    journalfile.write(project, workspace)
    prompt = agent_runner.build_prompt(task, project)
    # For the meta-project, remember the source HEAD so we can detect a
    # self-update and restart the service to load the new code.
    src_head_before = _src_head() if project["slug"] == config.META_PROJECT_SLUG else None
    # Remember the workspace repo's HEAD so a UI-touching run that showed no
    # screenshot can be noticed after it commits (see proof.py, RESEARCH.md §3).
    ws_head_before = proof.head_sha(orphans.repo_for(project["slug"]))

    try:
        result = await agent_runner.run_claude(
            prompt, workspace, model, timeout_min, max_turns=max_turns,
            on_event=_live_logger(run_id), run_id=run_id,
            json_schema=report_schema.schema_json(),
            settings_json=_guard_settings(run_id, project),
        )
    finally:
        hookguard.end(run_id)

    _note_memory_kill(project, task, result)

    if result.cancelled:
        db.finish_run(run_id, "cancelled", summary="Canceled from the portal.")
        db.add_journal(project["id"], "system", "status", f"Run ({task}) canceled from the portal.")
        _note_orphaned_work(project, task, "was canceled")
        return

    if result.is_rate_limited:
        until, why = await _rate_limit_backoff()
        db.finish_run(
            run_id, "error", result.session_id, result.cost_usd, result.num_turns,
            f"Rate limited; backing off until {until.isoformat(timespec='minutes')} ({why})",
        )
        db.add_journal(
            None, "system", "status",
            f"Usage/rate limit detected during **{task}** on `{project['slug']}`; "
            f"backing off until {until.isoformat(timespec='seconds')} ({why}).",
        )
        _note_orphaned_work(project, task, "hit a usage limit")
        return

    if result.timed_out:
        db.finish_run(run_id, "timeout", summary="Run timed out")
        db.add_journal(project["id"], "system", "status", f"Run ({task}) timed out after {timeout_min} min.")
        _note_orphaned_work(project, task, "timed out")
        return

    if not result.ok and result.report is None:
        detail = _failure_detail(result, max_turns)
        db.finish_run(
            run_id, "error", result.session_id, result.cost_usd, result.num_turns,
            (result.result_text or detail)[:500],
        )
        db.add_journal(
            project["id"], "system", "status",
            f"Run ({task}) errored: {result.result_text[:1000] or detail}",
        )
        _note_orphaned_work(project, task, "errored")
        return

    db.finish_run(run_id, "ok", result.session_id, result.cost_usd, result.num_turns, result.result_text[:500])
    _apply_report(project, result, run_id, task=task)
    await _maybe_self_review(project, result, task, ws_head_before)
    _note_missing_proof(project, result, task, ws_head_before)

    if src_head_before is not None:
        head_after = _src_head()
        if head_after and head_after != src_head_before:
            _schedule_self_restart(project["id"], head_after, current_run_id=run_id)


# The default turn ceiling for project runs. The old value here was 100, which
# one project's build runs - render a page, read the screenshot, fix,
# re-render - hit eight times in one day: each hit killed the run mid-work with
# an empty error, the work sat uncommitted, and the next run started the same
# feature again. The 30-minute timeout is the real backstop on a runaway run;
# the turn cap only exists to stop an agent that is looping without spending
# wall-clock time, and for that job 400 is as good a tripwire as 100.
DEFAULT_MAX_TURNS = 400


def run_max_turns() -> int:
    try:
        return max(1, int(db.get_setting("run_max_turns") or DEFAULT_MAX_TURNS))
    except ValueError:
        return DEFAULT_MAX_TURNS


def _failure_detail(result: agent_runner.RunResult, max_turns: int) -> str:
    """What to say about a failed run whose CLI printed no result text.

    "(no output)" told Wes nothing eight times in one day. When the CLI kills a
    run it says why in the result event's subtype even when the message is
    empty, so say that - and for the turn ceiling, say what to do about it.
    """
    if result.hit_max_turns:
        return (
            f"the run hit the {max_turns}-turn ceiling and was stopped mid-work "
            f"by the CLI. Whatever it had finished is in the working tree. If "
            f"this keeps happening, raise `run_max_turns` on the settings page."
        )
    if result.oom_killed:
        return runlimit.kill_note()
    if result.subtype:
        return f"the CLI reported `{result.subtype}` with no message"
    return "(no output)"


def _note_memory_kill(
    project: Optional[db.sqlite3.Row],
    task: str,
    result: agent_runner.RunResult,
) -> None:
    """Journal the fact that this run's memory cap fired.

    Deliberately on every path, including the successful one. The cap kills only
    the greedy process, so a run can hit it, watch its test command die with an
    unexplained `Killed`, work around it and report success - and Wes would
    never learn that a tool in his project wants more memory than the machine
    can give it. That was the shape of the bug this whole mechanism came from:
    the *effect* was loud (the portal restarting) and the *cause* was silent.
    """
    if not result.oom_killed or project is None:
        return
    peak = (
        f" Its peak was {runlimit.human(result.peak_memory_bytes)}."
        if result.peak_memory_bytes
        else ""
    )
    db.add_journal(
        project["id"], "system", "status",
        f"Run ({task}): {runlimit.kill_note()}{peak}",
    )


def _note_orphaned_work(project: db.sqlite3.Row, task: str, how: str) -> None:
    """Say so in the journal when a run dies leaving uncommitted changes.

    Wrapped in a bare except on purpose: this is diagnostics running on the path
    where something has already gone wrong, and a git call that raises must not
    be able to turn a recorded failure into an unrecorded one.
    """
    try:
        note = orphans.journal_note(project["slug"], task, how)
    except Exception:  # noqa: BLE001 - see docstring
        log.exception("Orphan scan failed for %s", project["slug"])
        return
    if note:
        db.add_journal(project["id"], "system", "status", note)


def _note_missing_proof(
    project: db.sqlite3.Row,
    result: agent_runner.RunResult,
    task: str,
    head_before: Optional[str],
) -> None:
    """Leave a visible note when a run committed a front-end change but showed no
    screenshot (see proof.py, RESEARCH.md §3).

    Only runs that produced a report are judged - a crashed or reportless run has
    already been journalled about, and piling a proof nag on top would bury the
    real message. A research burst writes RESEARCH.md, never UI, so it is exempt.
    Bare-excepted: this is a courtesy nudge, and a git failure computing it must
    never turn a good run into a recorded error.
    """
    if task == "research" or result.report is None:
        return
    try:
        if proof.report_has_proof(result.report):
            return
        files = proof.changed_ui_files(orphans.repo_for(project["slug"]), head_before)
        if not files:
            return
        db.add_journal(project["id"], "system", "status", proof.missing_proof_note(files))
    except Exception:  # noqa: BLE001 - see docstring
        log.exception("Proof-shot check failed for %s", project["slug"])


async def _maybe_self_review(
    project: db.sqlite3.Row,
    result: agent_runner.RunResult,
    task: str,
    head_before: Optional[str],
) -> None:
    """Critique a review-bound run's own diff before it surfaces to Wes.

    When a run proposed surfacing for review (and _apply_report granted it, so
    the project is now on the review shelf), a read-only critic checks the
    committed diff against the run's own claims and the open todos. If it finds
    concrete gaps, the project is pulled back to the active shelf and the gaps are
    journalled as the next run's marching orders - so Wes's review queue only sees
    finished work (RESEARCH.md §3).

    Fails open end to end: the whole thing is bare-excepted, and every uncertain
    verdict (no diff, timeout, junk answer, exception) leaves the project exactly
    where _apply_report put it - surfaced for review. A broken critic can never
    trap a project off Wes's radar, only fail to hold one back.
    """
    if not selfreview.enabled() or not selfreview.wants_review(result.report, task):
        return
    # _apply_report may have declined the review flip (e.g. a locked stage); only
    # gate work that actually reached the review shelf.
    fresh = db.get_project(project["id"])
    if fresh is None or fresh["stage"] != "review":
        return
    try:
        repo = orphans.repo_for(project["slug"])
        recent = db.list_journal_asc(project["id"], limit=12)
        recent_txt = "\n".join(
            f"- [{row['ts']}] {row['author']}/{row['kind']}: {row['content_md']}"
            for row in recent
        )
        prompt = selfreview.build_review(
            fresh, result.report, repo, head_before, recent_txt
        )
        if prompt is None:
            return  # nothing committed to judge - surfaced as normal
        workspace = config.PROJECTS_DIR / project["slug"]
        verdict = await selfreview.run_review(prompt, workspace, selfreview.review_model())
        if verdict.ready:
            return
        db.update_project(project["id"], stage="active")
        db.add_journal(project["id"], "system", "status", selfreview.hold_note(verdict))
    except Exception:  # noqa: BLE001 - see docstring: a broken critic must fail open
        log.exception("Self-review failed for %s; leaving it surfaced", project["slug"])


def _src_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(config.APP_ROOT), check=True, capture_output=True, text=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


# How long after a self-modifying run finishes before the restart fires. It
# only has to outlast this run's own final DB writes, and every second of it is
# time the site is running code that no longer matches its source, so it is
# short. The old value was 10s, which was pure downtime for no benefit.
RESTART_DELAY_SEC = 3


# A self-update waiting for the portal to go quiet: (project_id, new_head).
# Module state rather than a DB flag on purpose - if this process dies before
# firing, systemd starts a fresh process that loads the new code from disk,
# which is everything the restart was for.
_pending_restart: Optional[tuple[int, str]] = None


def restart_pending() -> bool:
    return _pending_restart is not None


def restart_pending_runs() -> Optional[int]:
    """None when no self-update is waiting; otherwise how many in-flight runs
    the restart is held behind. Feeds the site-wide notice - without it, a fix
    Wes asked for can be committed and invisible for half an hour while a slow
    run keeps the old code alive, which reads to him as the request being
    ignored (it did, twice, over the danger zone)."""
    if _pending_restart is None:
        return None
    return len(_inflight)


def _schedule_self_restart(project_id: int, new_head: str, current_run_id: Optional[int] = None) -> None:
    """A self-improvement run changed the portal's own source. Imported Python
    only changes at process start, so the running app no longer matches what is
    on disk - restart to apply the new code atomically.

    NOT unconditionally, though. Restarting the service SIGTERMs the whole
    cgroup, `claude` subprocesses included - and over one day that killed
    eleven of other projects' runs mid-work (four recorded as anonymous
    errors, seven orphaned), most of them SimpleClickTrack's. So if any other
    run is in flight, the restart waits: the flag is set here, the tick stops
    starting new runs, and `_tick` fires the restart once the last run
    finishes. The meta-run that made the change is excluded from the count -
    it is the one calling this.
    """
    global _pending_restart
    _reap_inflight()
    others = [rid for rid in _inflight if rid != current_run_id]
    if others:
        _pending_restart = (project_id, new_head)
        db.add_journal(
            project_id, "system", "status",
            f"Self-update detected (source now at `{new_head[:7]}`), but "
            f"{len(others)} other agent run(s) are still in flight and a restart "
            f"now would kill them mid-work. The restart happens as soon as they "
            f"finish; no new runs start until then.",
        )
        log.info("Self-update to %s; restart deferred behind %d in-flight run(s)", new_head[:7], len(others))
        return
    _fire_restart(project_id, new_head)


def _fire_restart(project_id: int, new_head: str) -> None:
    """Actually schedule the service restart, detached (systemd-run), because
    this process is the service being restarted."""
    try:
        subprocess.run(
            ["systemd-run", "--user", f"--on-active={RESTART_DELAY_SEC}",
             "systemctl", "--user", "restart", "project-portal.service"],
            check=True, capture_output=True, text=True,
        )
        db.add_journal(
            project_id, "system", "status",
            f"Self-update detected (source now at `{new_head[:7]}`); "
            f"restarting the service in {RESTART_DELAY_SEC}s to load the new code.",
        )
        log.info("Self-update to %s; service restart scheduled", new_head[:7])
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        db.add_journal(
            project_id, "system", "status",
            f"Self-update detected (source now at `{new_head[:7]}`) but scheduling "
            f"a service restart failed ({exc}). Restart manually with "
            "`systemctl --user restart project-portal` to load the new code.",
        )
        log.warning("Could not schedule self-restart: %s", exc)


def _ensure_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".portal").mkdir(parents=True, exist_ok=True)
    if not (workspace / ".git").exists():
        try:
            subprocess.run(["git", "init"], cwd=str(workspace), check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            log.warning("git init failed for %s: %s", workspace, exc)
    # .portal/ is the portal's own drop box in someone else's repository: a
    # report, a serve recipe, and now a journal mirror rewritten before every
    # run. Left visible it is a permanent dirty `git status` and a diff on every
    # single commit. (Twelve workspaces have already committed a report.json;
    # .git/info/exclude cannot untrack those, but it does keep the new file out.)
    _exclude_from_git(workspace, ".portal/")
    _sync_skills(workspace)


def _sync_skills(workspace: Path) -> None:
    """Copy the portal's skills into <workspace>/.claude/skills/.

    An agent has no way to discover a capability that is not in front of it -
    which is how every project up to now shipped a UI nobody had ever looked at,
    while a desktop with a browser sat idle on the same LAN. Shipping the
    knowledge as a Claude Code skill puts it where the agent already looks.

    Copied fresh every run rather than once at creation, so editing a skill in
    the portal repo reaches every existing workspace. Skills are the portal's to
    own: a directory here that no longer has a source is removed, so a renamed
    or deleted skill does not linger. Anything else under .claude/ is left
    alone - that is the agent's own scratch space.

    Two source roots: the built-in skills in the repo, and the ones the
    compaction agent promotes out of learnings.md (memory.promoted_skills_dir,
    #226). A promoted directory must contain a SKILL.md to ship - anything
    half-written stays home - and a name collision goes to the built-in copy,
    the curated one. Deleting a promoted skill on /memory therefore removes it
    from every workspace on its next sync, via the same stale sweep.
    """
    sources: dict[str, Path] = {}
    any_root = False
    try:
        promoted = memory.promoted_skills_dir()
        if promoted.is_dir():
            any_root = True
            for p in promoted.iterdir():
                if p.is_dir() and (p / "SKILL.md").is_file():
                    sources[p.name] = p
    except OSError:
        pass
    if config.SKILLS_DIR.is_dir():
        any_root = True
        for p in config.SKILLS_DIR.iterdir():
            if p.is_dir():
                sources[p.name] = p  # built-in wins a name collision
    if not any_root:
        return
    dest_root = workspace / ".claude" / "skills"
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        for name, src in sources.items():
            dest = dest_root / name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
            _localise_skill(dest / "SKILL.md")
        for stale in dest_root.iterdir():
            if stale.is_dir() and stale.name not in sources:
                shutil.rmtree(stale)
    except OSError as exc:
        # A workspace without its skills still runs; it just runs blinder.
        log.warning("could not sync skills into %s: %s", workspace, exc)
        return
    _exclude_from_git(workspace, ".claude/")


def _localise_skill(skill_md: Path) -> None:
    """Fill this installation's host and owner into a skill as it is shipped.

    A skill is only useful if it can say where things actually are - "the
    gallery is at http://$HOST:8500/style" is the whole point of the sentence.
    Hard-coding a hostname there would put a personal machine into the
    publishable tree, and stripping the URL out would make the skill useless.
    Substituting at sync time gets both: `app/skills/` ships with `$HOST` in
    it, and the copy in the workspace names this box.

    `safe_substitute`, so a `$VAR` that is not ours (a shell snippet's `$HOME`,
    say) is passed through untouched rather than blanked. Best-effort: a skill
    that cannot be read or rewritten is still shipped, just unsubstituted.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    filled = Template(text).safe_substitute(**config.SITE.template_vars())
    if filled != text:
        try:
            skill_md.write_text(filled, encoding="utf-8")
        except OSError as exc:  # pragma: no cover - unwritable fresh copy
            log.warning("could not localise %s: %s", skill_md, exc)


def _exclude_from_git(workspace: Path, pattern: str) -> None:
    """Ignore a path in this workspace without adding a tracked .gitignore.

    The skills are the portal's files, not the project's. Left alone they would
    turn up in every `git status` an agent runs and get committed into 17
    unrelated project repos. .git/info/exclude is the local, untracked place
    for exactly this.
    """
    exclude = workspace / ".git" / "info" / "exclude"
    try:
        if not exclude.parent.is_dir():
            return
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if pattern in existing.split():
            return
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(
            f"{existing}{prefix}# managed by the project portal\n{pattern}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("could not update git exclude in %s: %s", workspace, exc)


def _apply_report(
    project: db.sqlite3.Row,
    result: agent_runner.RunResult,
    run_id: Optional[int] = None,
    task: str = "",
) -> None:
    report = result.report
    project_id = project["id"]

    if report is None:
        entry = result.result_text or "(agent produced no report and no result text)"
        db.add_journal(project_id, "agent", "progress", entry)
        return

    # The one line the banner on the project page shows. Recorded before
    # anything below can fail, because "what did this run do" is worth keeping
    # even if some later field of the report is malformed.
    if run_id is not None:
        db.set_run_report_summary(run_id, report.get("summary"))

    updates: dict = {}
    # A run just finished and reported: whatever it said it was blocked on last
    # time is either resolved or about to be restated below, so the old value
    # must not outlive it (docs/state-model.md - "cannot go stale silently").
    if db.blocked_on(project):
        updates["blocked_on"] = None

    # The agent reports facts. New shape: `request_build`, `blocked_on`,
    # `new_stage` (review is the only stage move an agent may propose). Old
    # shape: the eight-value `new_status`, mapped forever - a contract change
    # is always executed by old-shape runs first, and every report ever
    # produced used this vocabulary.
    request_build = bool(report.get("request_build"))
    blocked = str(report.get("blocked_on") or "").strip()
    new_stage = report.get("new_stage")
    new_status = report.get("new_status")
    if new_status == "building":
        request_build = True
    elif new_status == "review":
        new_stage = "review"
    elif new_status == "waiting_user":
        blocked = blocked or "see the agent's last journal entry"
    elif new_status == "planning":
        # Triage's promotion out of the backlog. Any other stage keeps itself.
        new_stage = new_stage or ("active" if project["stage"] == "backlog" else None)
    elif new_status == "needs_input":
        pass  # the open questions below carry this by themselves now
    elif new_status == "done":
        log.warning("Agent tried to set stage=done for %s; ignoring (Wes-only)", project["slug"])

    if task == "research":
        # A burst reads and writes RESEARCH.md; it does not get to decide that
        # a backlog idea is now building. Wes queued reading, not a promotion.
        if new_stage or request_build or blocked:
            log.info("Ignoring state changes from a research burst on %s", project["slug"])
        new_stage, request_build, blocked = None, False, ""

    if new_stage == "review":
        updates["stage"] = "review"
        updates["build_requested"] = 0  # finished work moots an open request
    elif new_stage == "active" and project["stage"] == "backlog":
        updates["stage"] = "active"

    if request_build:
        if build_allowed(project):
            # Approved already (or the gate is off): asking to build IS
            # building, so make sure the project is on the working shelf.
            if project["stage"] != "active" and new_stage != "review":
                updates["stage"] = "active"
        elif not db.build_requested(project):
            # The plan is ready and the agent wants to start writing code.
            # That is Wes's call: record the request and say so loudly - once,
            # not on every subsequent plan pass while he thinks it over.
            updates["build_requested"] = 1
            db.add_journal(
                project_id, "system", "status",
                "The agent says this is ready to build. Waiting for your OK - press "
                "**approve build** on the project page (or set the status to active).",
            )
            asyncio.create_task(
                notify.notify(
                    "Ready to build",
                    f"{project['title']} has a plan and is waiting for your OK to start building.",
                    project_title=project["title"],
                    project_id=project_id,
                )
            )

    if blocked:
        updates["blocked_on"] = blocked[:200]

    kind = report.get("kind")
    if kind in config.PROJECT_KINDS:
        updates["kind"] = kind

    # Title and description are the two fields Wes may be holding a pen over
    # himself, so each has its own lock. A locked field is dropped silently
    # rather than journalled: an agent proposing a title every run would
    # otherwise fill the journal with rejections.
    title = report.get("title")
    if title and not project["title_locked"]:
        updates["title"] = title

    # Descriptions are meant to drift. What Wes typed at the start is the idea;
    # the description is what the project has actually turned into, and after a
    # few runs those are rarely the same sentence. `initial_idea` keeps the
    # original safe, so rewriting this loses nothing.
    description = (report.get("description") or "").strip()
    if description and not project["description_locked"]:
        updates["description"] = description

    if updates:
        db.update_project(project_id, **updates)

    journal_entry = report.get("journal_entry_md") or result.result_text or "(no journal entry provided)"
    db.add_journal(project_id, "agent", "progress", journal_entry)

    for q in report.get("questions") or []:
        question_text = q.get("question")
        if not question_text:
            continue
        # No stage move: an open question is a count, not a state. The badge
        # appears while the count is non-zero and disappears when it isn't.
        filing = db.file_question(
            project_id,
            question_text,
            q.get("context", ""),
            quick_options=quickreplies.encode(
                quickreplies.derive(question_text, q.get("options"))
            ),
        )
        # Already waiting on him in another wording (see app/qdedupe.py). The
        # notification is what makes a duplicate *cost* him something, so this
        # is the branch that matters, not the row that was not inserted.
        if not filing.created:
            log.info(
                "Skipped a duplicate question from %s: %r already open as #%s",
                project["slug"], question_text[:80], filing.row["id"],
            )
            continue
        row = filing.row
        asyncio.create_task(
            notify.notify(
                "New question",
                question_text,
                question_id=row["id"],
                project_title=project["title"],
                question_slot=row["slot"],
                project_id=project["id"],
            )
        )

    # Independent of everything above: a run that asked a question and parked
    # the project in needs_input still gets its checklist updated, because the
    # work it did do happened regardless of what it wants to know next.
    todos.apply_updates(project_id, report.get("todo_updates"))

    # A research burst reads and writes RESEARCH.md; spawning projects is a
    # change to the dashboard, which is exactly the kind of decision a burst on
    # an unapproved backlog idea does not get to make.
    if task != "research":
        try:
            for child in subprojects.apply_report(project, report):
                asyncio.create_task(
                    notify.notify(
                        "Sub-project created",
                        f"{child['title']} was split out of {project['title']}.",
                        project_title=project["title"],
                        project_id=project["id"],
                    )
                )
        except Exception:
            log.exception("Could not apply sub-projects from the report on %s", project["slug"])

    # Where to click to see what this project serves. Only ever set, never
    # cleared by a report - see preview.apply_report.
    try:
        preview.apply_report(project, report)
    except Exception:
        log.exception("Could not apply preview_url from the report on %s", project["slug"])

    learnings = report.get("learnings") or []
    if learnings:
        _append_learnings(learnings)

    suggestion = report.get("suggestion")
    if suggestion and suggestion.get("title"):
        srow = db.add_suggestion(suggestion["title"], suggestion.get("description", ""))
        asyncio.create_task(
            notify.notify("New suggestion", suggestion["title"], project_title=None)
        )


# How many learnings one run may add. Wes's complaint about this file was that
# it had become "a huge body of text that is mostly useless info", and the
# mechanism was simply that nothing ever said no: every run appended everything
# it felt like appending, and the file is fed to every subsequent run. The
# prompt asks for restraint; this is the backstop, because a prompt is advice
# and a limit is not. The first entries are kept, since a model puts what it
# thinks matters most at the top of a list.
MAX_LEARNINGS_PER_RUN = 3


def _learning_key(text: str) -> str:
    """A learning normalized for duplicate detection: case, punctuation and
    whitespace removed. Two runs noticing the same thing an hour apart phrase
    it slightly differently, so an exact-match check would catch almost none of
    the repetition that actually happens."""
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()


# At or above this word-overlap a new learning is treated as a refreshed
# rephrasing of an existing bullet (UPDATE, in place) rather than a new fact
# (ADD). Set conservatively: the failure to avoid is merging two genuinely
# distinct-but-related facts into one, which silently loses information. Below
# this, both survive and the semantic compaction job sorts out any real
# redundancy later. Above it, the near-duplicate pile-up Wes complained about
# is collapsed as it happens.
LEARNING_UPDATE_SIMILARITY = 0.6


def _learning_similarity(a: str, b: str) -> float:
    """Jaccard overlap of the two lines' significant word sets, 0..1.

    Symmetric and cheap - no model call on the write path - and good enough to
    catch a rephrasing of the same fact without flagging two facts that merely
    share a subject."""
    ta = set(_learning_key(a).split())
    tb = set(_learning_key(b).split())
    if not ta or not tb:
        return 0.0
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


def _clean_learning(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lstrip("-*• ").strip()


def _parse_learning(item) -> Optional[tuple[str, str]]:
    """Normalize one report learning into (op, text), or None to skip.

    A plain string is the common case and leaves the op to the gate ("auto").
    A run may also be explicit with {"op": "add|update|delete", "text": ...} -
    "delete" is the only way to retire a fact a run has found to be no longer
    true, since there is nothing new to add for it."""
    if isinstance(item, str):
        text = _clean_learning(item)
        return ("auto", text) if text else None
    if isinstance(item, dict):
        op = str(item.get("op") or "auto").strip().lower()
        text = _clean_learning(str(item.get("text") or ""))
        if op not in ("auto", "add", "update", "delete") or not text:
            return None
        return (op, text)
    return None


_BULLET_RE = re.compile(r"^(\s*[-*]\s+)(.*\S)\s*$")


def _append_learnings(learnings: list, *, when: Optional[datetime] = None) -> None:
    """Apply a run's learnings to the shared file through a write gate.

    Wes's standing complaint is that this file accumulated near-duplicates and
    stale lines, and because it is fed to every subsequent run of every project
    each dead line is a tax on every future run. Plain appending only ever grew
    it, so every incoming learning is now classified:

      NOOP   - an exact (normalized) duplicate of an existing bullet is dropped.
      UPDATE - a bullet highly similar to an existing one replaces it in place,
               so a refined rephrasing supersedes the old wording instead of
               piling up beside it. A run may force this with op "update".
      DELETE - op "delete" removes the existing bullet most similar to the given
               text, for a fact a run has found to be no longer true.
      ADD    - anything else is appended, up to MAX_LEARNINGS_PER_RUN net new
               bullets per run.

    No timestamp on added lines: Wes asked for these to read as durable facts,
    not a log; a learning whose date matters says so in its own text.

    Any operation that rewrites or removes an existing line snapshots
    learnings.md into the memory history first, so a wrong merge is one click
    from restore on /memory - the same safety the compaction job relies on, and
    the answer to "a silent rewrite once lost my profile text". A run that only
    appends touches nothing existing and needs no snapshot.
    """
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        lines = config.LEARNINGS_MD.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []

    # The freshness sidecar (added / last-confirmed / observation count per
    # entry). Kept beside the file, keyed by the same normalized text this gate
    # dedupes on - see memory.load_learnings_meta. Every classification below
    # feeds it: a NOOP is a confirmation, not a wasted re-observation.
    meta = memory.load_learnings_meta()
    day = memory.today(when)

    def bullet_text(idx: int) -> Optional[str]:
        m = _BULLET_RE.match(lines[idx])
        return m.group(2) if m else None

    def locate(text: str) -> tuple[int, int, float]:
        """(exact_index, best_index, best_sim) over the file's bullets. Each
        index is -1 when absent."""
        key = _learning_key(text)
        best_i, best_sim = -1, 0.0
        for i in range(len(lines)):
            bt = bullet_text(i)
            if bt is None:
                continue
            if _learning_key(bt) == key:
                return i, i, 1.0
            sim = _learning_similarity(text, bt)
            if sim > best_sim:
                best_sim, best_i = sim, i
        return -1, best_i, best_sim

    snapshotted = False
    mutated = False

    def snapshot_once() -> None:
        nonlocal snapshotted
        if not snapshotted:
            memory.snapshot("learnings")
            snapshotted = True

    added = 0  # net new bullets appended; caps at MAX_LEARNINGS_PER_RUN
    for item in learnings:
        parsed = _parse_learning(item)
        if parsed is None:
            continue
        op, text = parsed
        exact_i, best_i, best_sim = locate(text)

        if op == "delete":
            target = exact_i if exact_i >= 0 else (
                best_i if best_sim >= LEARNING_UPDATE_SIMILARITY else -1
            )
            if target >= 0:
                snapshot_once()
                gone = bullet_text(target)
                if gone:
                    memory.archive_learning(gone, "retired")
                    memory.meta_forget(meta, _learning_key(gone))
                del lines[target]
                mutated = True
            continue

        # A verbatim duplicate is a NOOP for add/auto (there is nothing to
        # refine). An explicit "update" of an exact match is equally a NOOP.
        # But a re-observation is evidence the fact still holds, so it confirms
        # the entry (bumping its last-confirmed date and count) even though the
        # file itself is untouched.
        if exact_i >= 0:
            memory.meta_touch(meta, _learning_key(text), day=day)
            continue

        update_i = best_i if best_sim >= LEARNING_UPDATE_SIMILARITY else -1
        if op == "add":
            update_i = -1  # forced ADD: never overwrite, even a close line

        if added >= MAX_LEARNINGS_PER_RUN:
            continue

        if update_i >= 0:
            m = _BULLET_RE.match(lines[update_i])
            prefix = m.group(1) if m else "- "
            snapshot_once()
            superseded = m.group(2) if m else None
            if superseded:
                memory.archive_learning(superseded, "superseded")
            memory.meta_supersede(
                meta, _learning_key(superseded or ""), _learning_key(text), day=day
            )
            lines[update_i] = f"{prefix.rstrip()} {text}"
            mutated = True
            added += 1
        else:
            memory.meta_touch(meta, _learning_key(text), day=day)
            lines.append(f"- {text}")
            added += 1

    if mutated:
        config.LEARNINGS_MD.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
    elif added:
        # No existing line changed - preserve the file's exact bytes and just
        # tack the new bullets on, exactly as the append-only path always did.
        # The appended entries are the last `added` items of `lines`, each
        # already carrying its "- " prefix.
        with open(config.LEARNINGS_MD, "a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines[len(lines) - added:]))

    # Garbage-collect the sidecar against the final file and persist it. Save
    # even on a pure-NOOP run: the confirmations recorded above are the point,
    # and they happen precisely when the file itself does not change.
    live_keys = [
        _learning_key(m.group(2)) for m in (_BULLET_RE.match(ln) for ln in lines) if m
    ]
    memory.meta_prune(meta, live_keys)
    memory.save_learnings_meta(meta)


@dataclass
class LearningFreshness:
    """One live learnings bullet paired with its sidecar age hints. `count` is 0
    and the dates None when the line predates tracking or was hand-edited."""
    text: str
    added: Optional[str]
    confirmed: Optional[str]
    count: int

    @property
    def tracked(self) -> bool:
        return self.confirmed is not None


def learnings_freshness() -> list[LearningFreshness]:
    """Live learnings bullets with their added / last-confirmed dates, stalest
    (oldest last-confirmed) first; undated lines sort last."""
    try:
        text = config.LEARNINGS_MD.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    meta = memory.load_learnings_meta()
    out: list[LearningFreshness] = []
    for ln in text.splitlines():
        m = _BULLET_RE.match(ln)
        if not m:
            continue
        bt = m.group(2).strip()
        ent = meta.get(_learning_key(bt))
        if isinstance(ent, dict) and ent.get("confirmed"):
            out.append(
                LearningFreshness(
                    text=bt,
                    added=ent.get("added"),
                    confirmed=ent.get("confirmed"),
                    count=int(ent.get("count", 1) or 1),
                )
            )
        else:
            out.append(LearningFreshness(text=bt, added=None, confirmed=None, count=0))
    out.sort(key=lambda e: e.confirmed or "9999-99-99")
    return out


async def queue_manual_run(project_id: int) -> None:
    await manual_queue.put(project_id)
    _wake.set()


async def run_now(project: db.sqlite3.Row, via: str = "") -> None:
    """Wes pressed "run agent now" (the web button, or "run X" over Telegram):
    the agent starts immediately and the project is active again from whatever
    shelf it was on - review, backlog, done, or under a pause. Deliberately
    NOT a build approval: on a gated project this same button reads "run
    planning pass", and running one must not open the gate - approving is its
    own click. The stage moves before the run is queued so the run's prompt
    already sees the project active."""
    was = db.display_state(project)
    if was != "active":
        db.update_project(project["id"], stage="active", paused=None)
        db.add_journal(
            project["id"], "user", "status",
            f"Status changed{via}: `{was}` -> `active` (run agent now)",
        )
    await queue_manual_run(project["id"])


async def reactivate_on_note(project: db.sqlite3.Row) -> bool:
    """Wes's rule: a new note on a project he had put down (paused, or parked
    in review) moves it back to active and puts an agent straight on it, from
    whichever door the note came through (web or Telegram).

    NOT a build approval - the agent may still only plan unless the build was
    already okayed. Backlog is deliberately excluded (backlog means "no model
    yet"; activating it is its own gesture), and done/abandoned stay finished
    unless he says otherwise. Returns True if it fired.
    """
    if not (db.is_paused(project) or project["stage"] == "review"):
        return False
    db.update_project(project["id"], stage="active", paused=None)
    db.add_journal(
        project["id"], "system", "status",
        "New note - moved back to **active** and queued an agent run to act on it.",
    )
    await queue_manual_run(project["id"])
    return True


CANCEL_RESULTS = {"cancelled", "orphaned", "not_running", "missing"}


def cancel_run(run_id: int) -> str:
    """Stop a run on request. Returns one of `CANCEL_RESULTS`.

    `orphaned` means the row said 'running' but no process in this service owns
    it - a leftover from before a restart. Killing nothing and leaving the row
    'running' would block the worker forever via `is_run_running()`, so the row
    is settled here instead.
    """
    run = db.get_run(run_id)
    if run is None:
        return "missing"
    if run["status"] != "running":
        return "not_running"
    if agent_runner.cancel_run(run_id):
        return "cancelled"
    db.finish_run(
        run_id, "cancelled",
        summary="Canceled: no live process owned this run (orphaned by a restart).",
    )
    log.warning("Cancel requested for run %s with no live process; row settled", run_id)
    return "orphaned"


# --------------------------------------------------------------------------
# One-off tasks
#
# Wes-initiated, like a manual run: they start immediately rather than through
# the scheduler, and they register in `_inflight` so a pending self-restart
# waits for them like it waits for everything else.
# --------------------------------------------------------------------------


def spawn_oneoff(task_id: int) -> Optional[int]:
    """Start a run on a one-off task now. None if it can't run: the task is
    gone, archived, or already has an agent on it (two agents resuming the
    same CLI session would fork it into two divergent conversations)."""
    _reap_inflight()
    task = db.get_oneoff(task_id)
    if task is None or task["status"] != "open":
        return None
    if db.oneoff_running(task_id):
        return None
    model = agent_runner.resolve_model(None)
    run_id = db.create_run(None, "oneoff", model, oneoff_id=task_id)
    log.info("Running one-off task %s (run_id=%s)", task_id, run_id)
    _inflight[run_id] = asyncio.create_task(_execute_oneoff(task_id, run_id, model))
    return run_id


async def _execute_oneoff(task_id: int, run_id: int, model: str) -> None:
    try:
        await run_oneoff_task(task_id, run_id, model)
    except Exception:  # noqa: BLE001 - a crashed run must not leave a 'running' row
        log.exception("One-off run %s failed", run_id)
        row = db.get_run(run_id)
        if row is not None and row["status"] == "running":
            db.finish_run(run_id, "error", summary="Run crashed; see the service log.")
            db.add_oneoff_message(
                task_id, "system", "The run crashed; see the service log.", run_id=run_id
            )


async def run_oneoff_task(task_id: int, run_id: int, model: str) -> None:
    task = db.get_oneoff(task_id)
    if task is None:
        db.finish_run(run_id, "error", summary="One-off task no longer exists.")
        return
    timeout_min = int(db.get_setting("run_timeout_min") or "30")
    max_turns = run_max_turns()
    ws = oneoff.workspace(task_id)
    ws.mkdir(parents=True, exist_ok=True)
    _sync_skills(ws)

    pending = db.pending_oneoff_messages(task_id)
    prompt = oneoff.build_prompt(task, pending)
    # Spent the moment they are in a prompt, exactly like project notes: from
    # here an agent has them, so they must not go into a second prompt too.
    db.mark_oneoff_delivered([m["id"] for m in pending])
    resume = task["cli_session_id"]

    try:
        result = await agent_runner.run_claude(
            prompt, ws, model, timeout_min, max_turns=max_turns,
            on_event=_live_logger(run_id), run_id=run_id, resume_session=resume,
            settings_json=_guard_settings(run_id, None),
        )
    finally:
        hookguard.end(run_id)

    if result.cancelled:
        db.finish_run(run_id, "cancelled", summary="Canceled from the portal.")
        db.add_oneoff_message(task_id, "system", "Run stopped from the portal.", run_id=run_id)
        return

    if result.is_rate_limited:
        until, why = await _rate_limit_backoff()
        db.finish_run(
            run_id, "error", result.session_id, result.cost_usd, result.num_turns,
            f"Rate limited; backing off until {until.isoformat(timespec='minutes')} ({why})",
        )
        db.add_oneoff_message(
            task_id, "system",
            f"The run hit a usage limit ({why}). Send your message again once the "
            f"window resets ({until.isoformat(timespec='minutes')}).",
            run_id=run_id,
        )
        return

    if result.timed_out:
        db.finish_run(run_id, "timeout", summary="Run timed out")
        db.add_oneoff_message(
            task_id, "system",
            f"The run timed out after {timeout_min} minutes. Anything it finished "
            f"is in the task's workspace; reply to continue where it left off.",
            run_id=run_id,
        )
        _continue_if_messages_waiting(task_id)
        return

    if oneoff.session_lost(result, resume):
        # Keeping a dead session id would fail every future message the same
        # way. Clear it: the next message starts a fresh CLI session in the
        # same workspace, which still holds all the files.
        db.set_oneoff_session(task_id, None)
        db.finish_run(
            run_id, "error", result.session_id, result.cost_usd, result.num_turns,
            "Saved CLI session could not be resumed; cleared it.",
        )
        db.add_oneoff_message(
            task_id, "system",
            "The saved conversation could not be resumed (the CLI no longer has "
            "it), so the agent's memory of this exchange is gone - the files in "
            "the workspace are still there. Send your message again and a fresh "
            "session picks it up.",
            run_id=run_id,
        )
        return

    if result.session_id:
        # The CLI issues a NEW id when it resumes a session (it forks), so the
        # id is re-recorded after every run, not just the first.
        db.set_oneoff_session(task_id, result.session_id)

    if result.ok:
        db.finish_run(
            run_id, "ok", result.session_id, result.cost_usd, result.num_turns,
            result.result_text[:500],
        )
        reply = result.result_text.strip() or "(the agent finished without printing a reply)"
        db.add_oneoff_message(task_id, "agent", reply, run_id=run_id)
    else:
        detail = _failure_detail(result, max_turns)
        db.finish_run(
            run_id, "error", result.session_id, result.cost_usd, result.num_turns,
            (result.result_text or detail)[:500],
        )
        db.add_oneoff_message(
            task_id, "system", f"The run failed: {result.result_text[:1000] or detail}",
            run_id=run_id,
        )
    _continue_if_messages_waiting(task_id)


def _continue_if_messages_waiting(task_id: int) -> None:
    """Messages Wes typed while the agent was mid-run start the next run as
    soon as this one settles - without this they would sit queued until he
    happened to send another one."""
    task = db.get_oneoff(task_id)
    if task is not None and task["status"] == "open" and db.pending_oneoff_messages(task_id):
        spawn_oneoff(task_id)


# --------------------------------------------------------------------------
# Daily reflect job
# --------------------------------------------------------------------------

async def _maybe_spend_down() -> None:
    """Settle an outstanding spend-down offer, and make a new one if it is due.

    Both halves are cheap reads of the cached snapshot and the questions table,
    so this runs every tick rather than on a schedule of its own. Nothing here
    is allowed to break a tick: an offer that fails to send is a missed
    opportunity, not a stopped worker.
    """
    try:
        if pacing.settle_offer():
            log.info("Spend-down window accepted")
        candidate = pacing.should_offer()
        if candidate is None:
            return
        question = pacing.create_offer_question(candidate)
        if question is None:
            return
        log.info("Offering to spend down the %s window", candidate["label"])
        await notify.notify(
            "Weekly Claude window is about to reset",
            pacing.offer_text(candidate),
            question_id=question["id"],
            project_title="Project Portal",
            question_slot=question["slot"],
        )
    except Exception:  # noqa: BLE001 - the tick must survive this
        log.exception("Spend-down check failed")


REFLECT_SLOT = -1


async def _maybe_reflect() -> None:
    # "Run agents automatically" off means off. This gate used to live only in
    # `_start_one`, which covers scheduled *project* runs - so the two
    # sleep-time jobs (this and `_maybe_compact`) still spawned a real `claude`
    # run with the switch off, spending window allowance and rewriting
    # profile.md. Found on 2026-07-26 by booting a throwaway instance with the
    # worker disabled and watching it start a reflect anyway.
    if not scheduled_work_enabled():
        return
    # The reflect summarizes the whole day across every project, so it waits
    # for a genuinely quiet moment rather than running alongside a build.
    if db.is_run_running() or REFLECT_SLOT in _inflight:
        return
    # Reflect once per portal day, and only once the day has actually turned
    # over - the reflect summarizes the day that just ended, so it runs just
    # after the boundary rather than just before it.
    today = daycycle.current_day()
    if (db.get_setting("last_reflect_date") or "") == today:
        return
    if daycycle.local_now().hour < daycycle.reset_hour():
        return
    # Spawned rather than awaited: the tick must keep returning promptly now
    # that it is responsible for topping up parallel run slots, and a reflect
    # can take as long as any other run. It holds a slot under a fixed key,
    # since its real run id only exists once the coroutine starts.
    _inflight[REFLECT_SLOT] = asyncio.create_task(run_reflect())


async def run_reflect() -> None:
    model = agent_runner.resolve_model(None)
    timeout_min = int(db.get_setting("run_timeout_min") or "30")
    cwd = config.MEMORY_DIR
    cwd.mkdir(parents=True, exist_ok=True)
    # The per-person files the reflect also maintains live in here. Made ahead
    # of the run rather than left to the agent's first Write so the directory
    # is visible in its cwd from the start - an `ls` that shows `people/`
    # answers "where do these go" without the agent having to trust the prompt.
    try:
        people.learned_dir().mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - defensive; the agent can still create it
        log.exception("Could not create the per-person memory directory")

    # The reflect job rewrites profile.md wholesale, which is how Wes's own
    # "about me" text was lost once already. Copy it first.
    try:
        memory.snapshot("profile")
    except Exception:  # noqa: BLE001 - a failed backup must not stop the run
        log.exception("Could not snapshot profile.md before the reflect")

    run_id = db.create_run(None, "reflect", model)
    prompt = agent_runner.build_prompt("reflect", None)
    log.info("Running daily reflect (run_id=%s)", run_id)
    result = await agent_runner.run_claude(
        prompt, cwd, model, timeout_min, max_turns=30, on_event=_live_logger(run_id),
        run_id=run_id, json_schema=report_schema.schema_json(),
    )

    if result.cancelled:
        db.finish_run(run_id, "cancelled", summary="Reflect canceled from the portal.")
        return

    if result.is_rate_limited:
        until, why = await _rate_limit_backoff()
        db.finish_run(run_id, "error", summary=f"Rate limited during reflect; back at {until.isoformat(timespec='minutes')} ({why})")
        return

    if result.timed_out:
        db.finish_run(run_id, "timeout", summary="Reflect timed out")
        db.add_journal(None, "system", "status", "Daily reflect timed out.")
        return

    status = "ok" if result.ok else "error"
    db.finish_run(run_id, status, result.session_id, result.cost_usd, result.num_turns, result.result_text[:500])
    db.set_setting("last_reflect_date", daycycle.current_day())

    summary = "profile.md reviewed and updated."
    if result.report:
        suggestion = result.report.get("suggestion")
        if suggestion and suggestion.get("title"):
            db.add_suggestion(suggestion["title"], suggestion.get("description", ""))
            summary += f" New suggestion: {suggestion['title']}."
    db.add_journal(None, "system", "reflect", f"Daily reflect ran. {summary}")


# --------------------------------------------------------------------------
# Learnings compaction
# --------------------------------------------------------------------------

COMPACT_SLOT = -2


def compaction_running() -> bool:
    handle = _inflight.get(COMPACT_SLOT)
    return handle is not None and not handle.done()


def start_compaction() -> bool:
    """Kick off a learnings-compaction run now. False if one is already going.

    Pressed from /memory, and now also fired automatically by `_maybe_compact`
    once the file crosses `learnings_cap_lines`. The original reason this was a
    button and not a timer - a background job silently rewriting Wes's memory is
    what lost his profile text once - is answered by `run_compaction` snapshotting
    the file into /memory revisions before the agent touches it, so nothing the
    auto-run cuts is unrecoverable, and the compaction lands loudly in the
    journal. The auto-trigger only touches learnings.md, never profile.md.
    """
    _reap_inflight()
    if compaction_running():
        return False
    _inflight[COMPACT_SLOT] = asyncio.create_task(run_compaction())
    return True


def learnings_cap() -> int:
    """The line count past which learnings.md auto-compacts. 0 disables it."""
    try:
        return max(0, int(db.get_setting("learnings_cap_lines") or "200"))
    except (TypeError, ValueError):
        return 200


def learnings_over_cap() -> bool:
    """True when learnings.md is past its cap and the cap is enabled."""
    cap = learnings_cap()
    return cap > 0 and _learnings_size()[0] > cap


async def _maybe_compact() -> None:
    """Auto-compact learnings.md when it has grown past its cap.

    This is the scheduled half of the memory overhaul (research §4): the file
    is injected into every run's prompt, so an ever-growing learnings.md taxes
    every future run, and leaving the fix to a button means it only happens when
    Wes happens to notice. Guards, in order of cheapness:

    - once per portal day, recorded whether or not the run succeeds, so a
      compaction that fails to get under the cap does not re-fire every tick;
    - only after the day boundary and only in a genuinely quiet moment, exactly
      like the daily reflect - it is a sleep-time job, not something to run
      alongside a build;
    - only when the file is actually over the cap.

    The date is stamped up front, at kick time, precisely because compaction is
    a real agent run that spends allowance: at most one attempt a day.
    """
    if not scheduled_work_enabled():  # see _maybe_reflect
        return
    if learnings_cap() == 0:
        return
    if db.is_run_running() or compaction_running():
        return
    today = daycycle.current_day()
    if (db.get_setting("last_auto_compact_date") or "") == today:
        return
    if daycycle.local_now().hour < daycycle.reset_hour():
        return
    if not learnings_over_cap():
        return
    lines = _learnings_size()[0]
    log.info("learnings.md is %d lines (cap %d); auto-compacting", lines, learnings_cap())
    db.set_setting("last_auto_compact_date", today)
    db.add_journal(
        None,
        "system",
        "status",
        f"learnings.md reached {lines} lines (cap {learnings_cap()}); auto-compacting. "
        f"The previous version is kept under /memory revisions.",
    )
    start_compaction()


async def run_compaction() -> None:
    model = agent_runner.resolve_model(None)
    timeout_min = int(db.get_setting("run_timeout_min") or "30")
    cwd = config.MEMORY_DIR
    cwd.mkdir(parents=True, exist_ok=True)

    # Before, not after: the agent rewrites learnings.md in place, so this copy
    # is the only way back if it cuts something Wes wanted kept. The /memory
    # page lists it with a restore button.
    try:
        memory.snapshot("learnings")
    except Exception:  # noqa: BLE001
        log.exception("Could not snapshot learnings.md before compaction")

    before = _learnings_size()
    run_id = db.create_run(None, "compact", model)
    prompt = agent_runner.build_prompt("compact", None)
    log.info("Compacting learnings.md (run_id=%s)", run_id)
    result = await agent_runner.run_claude(
        prompt, cwd, model, timeout_min, max_turns=30, on_event=_live_logger(run_id),
        run_id=run_id, json_schema=report_schema.schema_json(),
    )

    if result.cancelled:
        db.finish_run(run_id, "cancelled", summary="Learnings compaction canceled from the portal.")
        return
    if result.is_rate_limited:
        until, why = await _rate_limit_backoff()
        db.finish_run(run_id, "error", summary=f"Rate limited during compaction; back at {until.isoformat(timespec='minutes')} ({why})")
        return
    if result.timed_out:
        db.finish_run(run_id, "timeout", summary="Learnings compaction timed out")
        db.add_journal(None, "system", "status", "Learnings compaction timed out.")
        return

    status = "ok" if result.ok else "error"
    db.finish_run(run_id, status, result.session_id, result.cost_usd, result.num_turns, result.result_text[:500])
    after = _learnings_size()
    # Measured rather than taken from the report: the number that matters is
    # what the file on disk actually weighs now, and the agent describing its
    # own edit is the one source that cannot be checked.
    db.add_journal(
        None,
        "system",
        "reflect",
        f"Learnings compacted: {before[0]} lines / {before[1]} chars -> "
        f"{after[0]} lines / {after[1]} chars. The previous version is on /memory "
        f"under revisions if anything useful went missing.",
    )


def _learnings_size() -> tuple[int, int]:
    try:
        text = config.LEARNINGS_MD.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return (0, 0)
    return (len(text.splitlines()), len(text))

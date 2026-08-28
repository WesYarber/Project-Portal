"""Background worker: periodically advances projects via headless Claude runs."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Optional

from app import (
    agent_runner, apiretry, config, crashloop, daycycle, db, hookguard, journalfile,
    limits, memory, modelwatch, notes, notify, oneoff, orphans, pacing, people, portalmcp,
    preview, proof, quiet,
    quickreplies, report_schema, runlimit, runlog, selfreview, strays, subprojects,
    todos, worklock,
)
# Imported under a name of its own: `parallel` alone reads as the global
# concurrency cap everywhere else in this module (`pacing.parallel_cap`), and
# this is the per-project worktree mechanism, which is a different thing.
from app import parallel as parallel_runs

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


async def _rate_limit_backoff(
    quota: Optional[apiretry.Retry] = None,
) -> tuple[datetime, str]:
    """Set `backoff_until` after a run hit a usage limit, and say why.

    The old rule was a flat hour, which was a guess made without asking. The
    account knows exactly when the full window comes back, so ask it - a fresh
    reading, not the cached one, because the cache may predate the run that
    just failed and would therefore still show headroom. A failed fetch falls
    back to the flat hour, so this is strictly better-informed, never worse.

    `quota`, when the run's stream carried one (app/apiretry.py), beats both.
    Its `resets_at` came out of Anthropic's own 429 headers on the request that
    was actually refused, where the usage endpoint is a second opinion fetched
    afterwards over a network call that can itself fail - and does, on exactly
    the kind of bad afternoon that produces a rate limit in the first place.
    Same ceiling, because a full weekly window would otherwise idle for days.
    """
    now = datetime.now(timezone.utc)
    if quota is not None and quota.resets_at and quota.resets_at > now:
        until = min(quota.resets_at, now + limits.MAX_BACKOFF)
        why = f"{quota.limit_type} limit reached" if quota.limit_type \
            else "usage limit reported by the API"
        db.set_setting("backoff_until", until.isoformat(timespec="seconds"))
        return until, why
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


def effective_project_cap(project: db.sqlite3.Row) -> int:
    """How many scheduled runs this project may take today. 0 means no cap.

    Three states, and the whole point of this function is that the last two are
    not the same:

    - **A number on the project.** Wes typed it, so it binds in both directions
      and a spend-down is not a license to ignore it.
    - **0 on the project.** He lifted this one project off the board default by
      hand: no cap at all, however busy the rest of the board is.
    - **NULL.** Inherit the board-wide default (`project_max_runs_per_day`),
      which exists because the global budget alone cannot stop one project
      eating it: `list_schedulable_projects` hands over one order, so the
      project at its head is picked every tick until it has nothing workable,
      and Project Portal took 70 of 202 runs in the week to 2026-08-07 that way.

    The default is lifted during a spend-down and the project's own number is
    not. Wes has answered "yes, spend it" ten times to a weekly window about to
    expire, and this default is about spreading work across the board rather
    than about saving allowance - so with a window burning down it has nothing
    to say. Quiet hours still applies to a spend-down, because that one IS
    about him rather than about the money.
    """
    try:
        cap = project["max_runs_per_day"]
    except (IndexError, KeyError):  # row from a pre-migration query
        return 0
    if cap is not None:
        return max(0, int(cap))
    if pacing.spending_down():
        return 0
    return db.default_project_max_runs()


def project_at_daily_cap(project: db.sqlite3.Row) -> bool:
    """True if this project has taken all the runs it may take today."""
    cap = effective_project_cap(project)
    if not cap:
        return False
    return db.count_runs_today(project["id"]) >= cap


def workspace_leased(slug: str) -> bool:
    """Is another agent holding this project's workspace right now?

    Only a definite yes counts. `worklock.is_busy` answers None when it could
    not find out (no such directory yet, a filesystem with no BSD locks), and
    treating that as busy would stop the board on any machine where leasing does
    not work - the same fail-open rule the memory caps follow.

    This is a pre-flight check, not the mutual exclusion: it saves burning a run
    row and a parallel slot on a spawn that would refuse, while the lease the
    spawn itself takes is what actually makes a collision impossible. Anything
    that changes between here and the spawn is therefore harmless.
    """
    return worklock.is_busy(config.PROJECTS_DIR / slug) is True


def memory_leased() -> bool:
    """Is an agent holding the shared memory directory right now?

    The reflect and the compaction both work in `config.MEMORY_DIR` and both
    rewrite files in it, so they are one resource; their two separate
    `_inflight` slots have never expressed that, and neither slot survives the
    restart this box performs several times an hour to load its own new code.

    Same pre-flight rule as `workspace_leased`: only a definite yes counts, and
    the lease the spawn itself takes is what actually makes a collision
    impossible.
    """
    return worklock.is_busy(config.MEMORY_DIR) is True


def _pick_project(manual_project_id: Optional[int]) -> tuple[Optional[db.sqlite3.Row], bool]:
    """Returns (project_row, is_manual). Manual runs deliberately bypass the
    per-project cap - Wes asking for a run is the whole point - but never the
    one-run-per-project rule, since two agents in one workspace would fight
    over the same files and the same git checkout."""
    busy = db.running_project_ids()
    if manual_project_id is not None:
        proj = db.get_project(manual_project_id)
        if proj is not None and proj["id"] not in busy and not workspace_leased(proj["slug"]):
            return proj, True
    for candidate in db.list_schedulable_projects():
        if candidate["id"] in busy:
            continue
        # The runs table said this project is free. Ask the kernel too: a run
        # that outlived the portal process which started it holds the lease and
        # may hold no row at all. This is the check that would have caught the
        # 2026-07-29 double-run before it started.
        if workspace_leased(candidate["slug"]):
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
    quiet_hours = quiet.quiet_hold()
    if quiet_hours is not None:
        return quiet.quiet_reason(quiet_hours)
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
        # Not "its own" any more: the cap that stopped a project here is just
        # as likely to be the board-wide default as a number set on the
        # project. Worded to be true of both, and to say when it lifts, since
        # otherwise this reads as a fault rather than as pacing working.
        resets_in = daycycle.humanize_seconds(daycycle.seconds_until_reset())
        return (
            "every active project has taken all its runs for today - "
            f"they reset in {resets_in}"
        )
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
    """Daily aging, once per day: the PostToolUse audit trail out of
    hook_events (denials and Stop bounces are kept; only the bulk 'what did
    this run do' rows age, db.AUDIT_RETENTION_DAYS), and deleted questions out
    of questions entirely (Wes, 2026-08-06: "Deleted questions should fully
    delete after 7 days"). Best-effort - a failed prune waits a day, and each
    prune fails alone."""
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
    try:
        removed = db.prune_deleted_questions()
        if removed:
            log.info("Aged %d deleted question(s) out for good", removed)
    except Exception:  # noqa: BLE001
        log.exception("Deleted-question prune failed")


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
    if _restarting:
        # The restart timer is armed and this process is about to be killed.
        # Anything started now is started to be orphaned, so do nothing at all -
        # not even the reflect or the compaction, which are runs like any other.
        return
    _reap_adopted()
    await _sweep_strays()
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

    _drain_parallel_branches()
    await _maybe_reflect()
    await _maybe_compact()


def _drain_parallel_branches() -> None:
    """Retry every parallel branch that could not be merged when its run ended.

    Both reasons a merge is deferred - a run still in flight, a dirty tree -
    clear on their own with nothing to trigger them, so without a tick here a
    branch parked as `busy` would sit there until the next run on that project
    happened to finish. Defensive throughout: this is bookkeeping, and it runs
    inside the loop that starts every run on the board.
    """
    try:
        slugs = parallel_runs.projects_with_branches()
    except Exception:  # pragma: no cover - defensive
        log.exception("Could not list projects with parallel branches")
        return
    for slug in slugs:
        project = db.get_project_by_slug(slug)
        if project is None:
            continue
        try:
            merge_parallel_work(project)
        except Exception:  # pragma: no cover - defensive
            log.exception("Could not merge parallel work for %s", slug)


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
    if _restarting:
        # Belt and braces with the check in `_tick`: this is also reached from
        # the pending-restart branch there, which deliberately lets manual runs
        # through right up until the timer is armed.
        return False
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
        # Don't work through the night. Unlike every guard above this one, it
        # is not about how much allowance is left - it is about whether anyone
        # is awake to read the result. See app/quiet.py for the overnight that
        # taught us. Not exempt during a spend-down, deliberately.
        if quiet.quiet_hold() is not None:
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


ADOPTED_SUMMARY = (
    "Ended after a service restart: this run outlived the portal process that "
    "started it, so nothing was watching when it finished."
)

STRANDED_SUMMARY = (
    "Ended after a service restart, leaving something running: the agent itself "
    "had exited - its lease on the workspace was free - but a process it "
    "detached was still holding its container open. The leftover is listed "
    "under Settings > agent > Left running, where it can be stopped."
)

# How long a workspace lease must read free before that is taken as proof the
# agent has exited.
#
# The lease is recorded the instant the process is spawned, and `flock` takes it
# a moment later, so there is a brief window where the row claims a lease that
# nobody holds yet. A single free reading landing in that window would settle a
# run that has not started working. Two readings this far apart cannot both, and
# the cost of the wait is bounded: the run is already over, and its project is
# unlocked two minutes later instead of never.
LEASE_FREE_CONFIRM_S = 120.0

# run id -> the monotonic time its lease was FIRST seen definitely free. Not
# persisted on purpose: a portal restart should re-confirm from scratch rather
# than trust a timestamp written by a process that is no longer here.
_lease_free_since: dict[int, float] = {}


def _lease_says_finished(run: "db.RunningRun") -> bool:
    """Has this run's workspace lease read definitely free for long enough to
    prove the agent has exited?

    This is the signal that closes the one hole the scope signal cannot reach.
    `worklock.wrap` passes `--close`, so the lease descriptor is NOT inherited
    by anything the agent detaches - which is exactly what makes a free lease
    mean "the agent is gone" even while a preview server it left behind holds
    the run's scope active forever.

    Three ways to answer no, and all three are load-bearing:

    * **No lease recorded.** The run took none (a reflect, a compaction, a
      one-off) and its workspace would read free the whole time it ran.
    * **Busy.** The agent is alive and working. This is the normal answer.
    * **`None` - could not ask.** A workspace that has been deleted, a
      filesystem with no BSD locks. Unknown is not free; inventing "free" here
      unlocks a workspace on a failed probe, which is the original defect.
    """
    if not run.lock_dir:
        return False
    if worklock.is_busy(Path(run.lock_dir)) is not False:
        # Busy or unknowable: any earlier free reading is void, and a later one
        # has to start its confirmation window over.
        _lease_free_since.pop(run.run_id, None)
        return False
    first_seen = _lease_free_since.setdefault(run.run_id, time.monotonic())
    return time.monotonic() - first_seen >= LEASE_FREE_CONFIRM_S


def _reap_adopted() -> None:
    """Settle 'running' rows this process does not own once their scope is gone.

    The other half of `db._reconcile_orphaned_runs`. Adopting a run that
    survived a restart keeps its workspace locked, which is the point - but the
    adopting process has no `Popen` to await, so nothing would ever move the row
    off 'running' and the project would be locked out for good. Here the scope
    itself is the completion signal: while systemd still has the unit the agent
    is working, and the moment it does not, the run is over.

    A row with no scope unit is left alone. It cannot be one of these (a run
    that was never scoped really did die with its parent, and the boot sweep
    already settled it), and reaping on absence of evidence would settle live
    runs on any machine where scoping is switched off.

    "Adopted" means *started by a different portal process*, and the scope name
    is what says so - `_inflight` is not, which is the trap here. The daily
    reflect and the learnings compaction both create a real `runs` row and then
    register under a fixed slot key rather than under their run id, so they look
    exactly like orphans. Their scope also dies a few seconds before their row
    settles, in the window where the report is being parsed and journaled, which
    is precisely when this would have marked a healthy reflect as an error.

    **Two completion signals, because the first one can be held open forever.**
    The scope dying is the ordinary one. It fails in exactly one case, and that
    case is routine here: the agent detaches a preview server on its way out, the
    server keeps the scope active, and this run is never settled. The row stays
    'running', so `running_project_ids()` lists the project forever and it can
    never get another run - with nothing in the UI to say why. Worse, the stray
    sweep cannot rescue it either: `_protected_scopes` reads the same 'running'
    rows, so the leftover holding the scope open is the one thing protected from
    being moved out of it. The two mechanisms deadlock, permanently.

    The workspace lease breaks that. It is held by the agent alone and by
    nothing it spawns, so a definitely-free lease proves the agent has exited
    whatever its scope says. Settling the row here also releases the deadlock's
    other half: the unit stops being protected, and the next sweep rehouses the
    leftover rather than killing it.
    """
    global _last_stray_sweep
    seen: set[int] = set()
    for run in db.running_run_handles():
        seen.add(run.run_id)
        if (
            run.run_id in _inflight
            or not run.scope_unit
            or runlimit.minted_here(run.scope_unit)
        ):
            continue
        if runlimit.scope_is_active(run.scope_unit) is False:
            log.info(
                "Adopted run %s has ended (scope %s is gone)", run.run_id, run.scope_unit
            )
            db.finish_run(run.run_id, "error", summary=ADOPTED_SUMMARY)
        elif _lease_says_finished(run):
            log.warning(
                "Adopted run %s has ended but left something running: %s is still "
                "active with the workspace lease on %s already released",
                run.run_id, run.scope_unit, run.lock_dir,
            )
            db.finish_run(run.run_id, "error", summary=STRANDED_SUMMARY)
            # Now that the row is settled the scope is no longer protected, so
            # the leftover can finally be moved out of it. Sweep on the next
            # tick rather than up to ten minutes from now: we have just proved
            # there is something in there to rehouse.
            _last_stray_sweep = None
    # Runs that settled by some other route (a cancel, a finished supervisor)
    # must not leave a confirmation window behind for a future run to inherit -
    # run ids are not reused, but a leaked entry per run is still a leak.
    for stale in [run_id for run_id in _lease_free_since if run_id not in seen]:
        _lease_free_since.pop(stale, None)


# A stray sweep is not urgent and is not free - it shells out to systemd once
# per scope - so it runs on its own slow timer rather than every tick. The
# end-of-run sweep in `agent_runner.run_claude` is what actually keeps scopes
# clean; this exists for the two cases that path cannot reach: scopes left by an
# earlier portal process (including everything already leaked before this code
# shipped), and runs adopted across a restart, where the process that must sweep
# is not the one that spawned the agent.
STRAY_SWEEP_INTERVAL_S = 600
_last_stray_sweep: Optional[float] = None


def _protected_scopes() -> set[str]:
    """Scope units a sweep must not touch, from both sources on purpose.

    The database half covers runs this process did not start. `known_scopes`
    covers the window between spawning a run and recording its scope name, where
    the database half alone would call a live run finished and move its agent
    out of the cgroup containing it. Over-inclusive by design: a scope wrongly
    protected is swept ten minutes later, a scope wrongly swept is a live run
    losing its memory cap.
    """
    protected = set(runlimit.known_scopes())
    for run in db.running_run_handles():
        if run.scope_unit:
            protected.add(run.scope_unit)
    return protected


async def _sweep_strays() -> None:
    """Move helpers left behind by finished runs into scopes of their own.

    Off the event loop: the sweep is several `systemctl` calls per scope, and
    the tick has a whole board to get through. Never allowed to raise - a
    housekeeping job that can stop the worker is worse than the leak.
    """
    global _last_stray_sweep
    now = time.monotonic()
    if _last_stray_sweep is not None and now - _last_stray_sweep < STRAY_SWEEP_INTERVAL_S:
        return
    _last_stray_sweep = now
    try:
        evictions = await asyncio.to_thread(strays.sweep, _protected_scopes())
    except Exception:
        log.exception("Stray sweep failed")
        return
    for ev in evictions:
        if ev.moved:
            log.info(
                "Rehoused %s process(es) from %s into %s",
                len(ev.moved), ev.unit, ev.stray_unit,
            )


def spawn_run(project: db.sqlite3.Row, task: str, parallel: bool = False) -> int:
    """Create the run row synchronously and execute it in the background.

    Splitting the row creation from the execution is what makes concurrency
    accounting honest: by the time this returns, both `count_runs_today()` and
    `running_project_ids()` already reflect the new run, so the next slot in
    the same tick cannot double-spend the budget or pick the same project.

    `parallel` puts this run in its own git worktree beside a run that is
    already in flight on the same project - see app/parallel.py.
    """
    model = agent_runner.resolve_model(project, task)
    run_id = db.create_run(project["id"], task, model, parallel=parallel)
    log.info("Running task=%s for project=%s (run_id=%s, parallel=%s)",
             task, project["slug"], run_id, parallel)
    _inflight[run_id] = asyncio.create_task(
        _execute_run(project, task, run_id, model, parallel=parallel)
    )
    return run_id


async def _execute_run(
    project: db.sqlite3.Row, task: str, run_id: int, model: str, parallel: bool = False
) -> None:
    try:
        await run_project_task(project, task, run_id=run_id, model=model, parallel=parallel)
    except Exception:  # noqa: BLE001 - a crashed run must not leave a 'running' row
        log.exception("Run %s failed", run_id)
        row = db.get_run(run_id)
        if row is not None and row["status"] == "running":
            db.finish_run(run_id, "error", summary="Run crashed; see the service log.")
    finally:
        # Whether it crashed or finished, this is the one place that sees every
        # completed run, so it is where "these keep dying on the launch pad"
        # gets noticed, and where a parallel run's branch gets folded back in.
        # Never allowed to break the caller: a failure to report a crash loop
        # must not itself become one.
        try:
            await _announce_crash_loop(project)
        except Exception:  # pragma: no cover - defensive
            log.exception("Could not check the crash-loop state for %s", project["slug"])
        try:
            merge_parallel_work(project)
        except Exception:  # pragma: no cover - defensive
            log.exception("Could not merge parallel work for %s", project["slug"])


def merge_parallel_work(project: db.sqlite3.Row) -> list[parallel_runs.Merged]:
    """Fold every finished parallel branch back into the project's workspace.

    Called when any run on the project finishes and again on each tick, because
    the two things that stop a merge - a run still in flight and a dirty tree -
    both clear on their own. A branch that cannot be merged today is kept and
    retried rather than dropped: see app/parallel.py.

    A branch whose own run is *still going* is deliberately skipped: merging a
    live agent's half-written history is the same defect as merging into a
    dirty tree, just from the other end.
    """
    slug = str(project["slug"])
    live = {int(r["id"]) for r in db.running_runs_for_project(int(project["id"]))}
    settled = [run_id for run_id in parallel_runs.pending(slug) if run_id not in live]
    if not settled:
        return []
    results = parallel_runs.drain(slug, running=bool(live), run_ids=settled)
    for result in results:
        # A branch parked as busy has already said so once; repeating it on
        # every tick would fill the journal with the same line.
        if result.status == "busy" and _PARALLEL_SAID.get(result.branch) == "busy":
            continue
        note = parallel_runs.journal_note(slug, result)
        if note:
            db.add_journal(project["id"], "system", "status", note)
        if result.kept:
            _PARALLEL_SAID[result.branch] = result.status
        else:
            _PARALLEL_SAID.pop(result.branch, None)
    return results


# Which parallel branches have already had their "still waiting" line written,
# so the journal gets one of them rather than one per tick.
_PARALLEL_SAID: dict[str, str] = {}


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


def _mcp_config(run_id: int, project: db.sqlite3.Row, task: str) -> Optional[str]:
    """The `--mcp-config` that lets this run ask a question mid-flight.

    Wrapped the same way `_guard_settings` is: an MCP server the CLI cannot
    start is a warning in a log, never a reason a project stops getting runs."""
    try:
        return portalmcp.begin(run_id, int(project["id"]), task)
    except Exception:  # noqa: BLE001
        log.exception("Could not build the MCP config for run %s", run_id)
        return None


async def run_project_task(
    project: db.sqlite3.Row,
    task: str,
    run_id: Optional[int] = None,
    model: Optional[str] = None,
    parallel: bool = False,
) -> None:
    """Execute one task. `run_id` is passed in by `spawn_run`, which creates the
    row up front so the slot is accounted for before the coroutine starts; call
    without it to create the row here."""
    model = model or agent_runner.resolve_model(project, task)
    timeout_min = int(db.get_setting("run_timeout_min") or "30")
    max_turns = run_max_turns()
    slug = str(project["slug"])
    workspace = config.PROJECTS_DIR / slug
    _ensure_workspace(workspace)

    if run_id is None:
        run_id = db.create_run(project["id"], task, model, parallel=parallel)

    # A parallel run works in its own checkout of the same repo, so the lease
    # on the ordinary workspace is never contended and the two agents cannot
    # overwrite each other's files. If git will not make one, the run is
    # refused outright rather than falling back to the shared workspace - that
    # fallback IS the double-run this whole mechanism exists to avoid.
    if parallel:
        worktree = parallel_runs.open_worktree(slug, run_id)
        if worktree is None:
            note = (
                "The parallel run was refused: the portal could not open a git "
                f"worktree of `{workspace}`. Nothing was started, and the run "
                "already in flight is untouched."
            )
            db.finish_run(run_id, "error", summary=note)
            db.add_journal(project["id"], "system", "status", note)
            return
        workspace = worktree
        _ensure_workspace(workspace)
    # Strictly before the prompt is built. The prompt's journal section shortens
    # older entries and points at this file for their full text; writing it after
    # would let an entry created in between be shortened in the prompt and absent
    # from the file. Written first, the same race can only ever affect the newest
    # entry - which the prompt always shows whole. See app/journalfile.py.
    journalfile.write(project, workspace)
    prompt = agent_runner.build_prompt(
        task, project,
        parallel_note=(
            parallel_runs.prompt_section(
                slug, run_id, workspace,
                others=max(len(db.running_runs_for_project(int(project["id"]))) - 1, 1),
            )
            if parallel else ""
        ),
    )
    # For the meta-project, remember the source HEAD so we can detect a
    # self-update and restart the service to load the new code.
    src_head_before = _src_head() if project["slug"] == config.META_PROJECT_SLUG else None
    # Remember the repo's HEAD so a UI-touching run that showed no screenshot
    # can be noticed after it commits (see proof.py, RESEARCH.md §3). A
    # parallel run commits in its worktree, so that is the repo to watch - the
    # ordinary workspace's HEAD will not move until the merge.
    run_repo = _run_repo(slug, workspace, parallel)
    ws_head_before = proof.head_sha(run_repo)

    try:
        result = await agent_runner.run_claude(
            prompt, workspace, model, timeout_min, max_turns=max_turns,
            on_event=_live_logger(run_id), run_id=run_id,
            json_schema=report_schema.schema_json(),
            settings_json=_guard_settings(run_id, project),
            mcp_config=_mcp_config(run_id, project, task),
            # One agent per workspace, enforced by the kernel rather than by the
            # runs table. See app/worklock.py.
            lock_dir=workspace,
        )
    finally:
        hookguard.end(run_id)
        portalmcp.end(run_id)

    if result.lock_conflict:
        # Nothing ran, so there is nothing to salvage and nothing to warn about:
        # deliberately no `_note_orphaned_work` here, because an uncommitted-work
        # warning would point at the *other* agent's live edits and send the next
        # run to tidy up work that is still being written.
        log.warning("Run %s refused: %s is leased by another agent", run_id, project["slug"])
        note = worklock.refused_note(worklock.workspace_resource(project["slug"]))
        db.finish_run(run_id, "error", summary=note)
        db.add_journal(project["id"], "system", "status", note)
        return

    # Where the workspace repo ended up, recorded before any of the branches
    # below can return. A run that timed out, was canceled or errored may still
    # have committed something first - those are exactly the runs whose work
    # Wes is most likely to want undone - so this cannot live on the happy path.
    _record_workspace_heads(project, run_id, ws_head_before, repo=run_repo)

    _note_memory_kill(project, task, result)
    await _note_quota_wall(project, task, result)

    if result.cancelled:
        db.finish_run(run_id, "cancelled", summary="Canceled from the portal.")
        db.add_journal(project["id"], "system", "status", f"Run ({task}) canceled from the portal.")
        _note_orphaned_work(project, task, "was canceled", repo=run_repo)
        return

    if result.is_rate_limited:
        until, why = await _rate_limit_backoff(
            result.retries.quota if result.retries else None
        )
        db.finish_run(
            run_id, "error", result.session_id, result.cost_usd, result.num_turns,
            f"Rate limited; backing off until {until.isoformat(timespec='minutes')} ({why})",
        )
        db.add_journal(
            None, "system", "status",
            f"Usage/rate limit detected during **{task}** on `{project['slug']}`; "
            f"backing off until {until.isoformat(timespec='seconds')} ({why}).",
        )
        _note_orphaned_work(project, task, "hit a usage limit", repo=run_repo)
        return

    if result.timed_out:
        db.finish_run(run_id, "timeout", summary="Run timed out")
        db.add_journal(project["id"], "system", "status", f"Run ({task}) timed out after {timeout_min} min.")
        _note_orphaned_work(project, task, "timed out", repo=run_repo)
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
        _note_orphaned_work(project, task, "errored", repo=run_repo)
        return

    db.finish_run(run_id, "ok", result.session_id, result.cost_usd, result.num_turns, result.result_text[:500])
    _apply_report(project, result, run_id, task=task)
    _note_unreadable_report(project, result, task)
    # Before the critic, not after: the self-review asks "is this ready for Wes
    # to look at", and that question is already answered when a note he wrote
    # mid-run is still waiting to be read. Running it anyway would spend a model
    # call to decide a shelf this project is not going to be on either way.
    if not await _rerun_for_unseen_notes(project):
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

# The memory jobs (the daily reflect and the learnings compaction) get their own
# ceiling because they are a different shape of work: one agent, one directory,
# a handful of files it rewrites whole. It was hardcoded at 30 from when those
# files were a few kilobytes, and on 2026-08-07 both jobs hit it on the same day
# - the reflect part-way through profile.md, the compaction part-way through a
# 59 KB learnings.md - each burning about $5 of allowance to finish nothing. The
# files grow; a ceiling sized against their old size does not. 120 is roughly
# five times the largest run either job has ever needed (24 turns), and the
# 30-minute timeout remains the real backstop.
DEFAULT_MEMORY_MAX_TURNS = 120


def run_max_turns() -> int:
    try:
        return max(1, int(db.get_setting("run_max_turns") or DEFAULT_MAX_TURNS))
    except ValueError:
        return DEFAULT_MAX_TURNS


def memory_max_turns() -> int:
    try:
        return max(
            1, int(db.get_setting("memory_max_turns") or DEFAULT_MEMORY_MAX_TURNS)
        )
    except ValueError:
        return DEFAULT_MEMORY_MAX_TURNS


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
        return runlimit.kill_note(peak=result.peak_memory_bytes)
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
        f"Run ({task}): {runlimit.kill_note(peak=result.peak_memory_bytes)}{peak}",
    )


async def _note_quota_wall(
    project: Optional[db.sqlite3.Row],
    task: str,
    result: agent_runner.RunResult,
) -> None:
    """Back off when a run met the usage limit and got through it anyway.

    Deliberately on every path *except* the rate-limited one, which does its own
    backing off and would otherwise do it twice. The case this exists for is the
    run that succeeds: the CLI retries internally, so a run can sit against a
    quota wall for ten minutes, get through on the far side of it, and finish
    green. Nothing about that run's *outcome* records that the wall was there,
    so today the scheduler launches the next one straight into it - and the one
    after that - learning only from the bodies.

    The reset time decides. It arrived on the 429's own headers, so if it is
    still in the future the window is still shut and the next spawn is wasted;
    if it has passed, the wall is what the run already climbed and there is
    nothing to wait for. Only the first case holds anything back.
    """
    quota = result.retries.quota if result.retries else None
    if result.is_rate_limited or quota is None:
        return
    if not quota.resets_at or quota.resets_at <= datetime.now(timezone.utc):
        return
    until, why = await _rate_limit_backoff(quota)
    db.add_journal(
        project["id"] if project is not None else None, "system", "status",
        f"Run ({task}) hit a usage limit mid-run and retried through it "
        f"({why}); holding new runs until {until.isoformat(timespec='seconds')} "
        "rather than sending the next one into the same wall.",
    )


def _note_orphaned_work(
    project: db.sqlite3.Row, task: str, how: str, repo: Optional[Path] = None
) -> None:
    """Say so in the journal when a run dies leaving uncommitted changes.

    Wrapped in a bare except on purpose: this is diagnostics running on the path
    where something has already gone wrong, and a git call that raises must not
    be able to turn a recorded failure into an unrecorded one.
    """
    try:
        note = orphans.journal_note(project["slug"], task, how, repo=repo)
    except Exception:  # noqa: BLE001 - see docstring
        log.exception("Orphan scan failed for %s", project["slug"])
        return
    if note:
        db.add_journal(project["id"], "system", "status", note)


def _run_repo(slug: str, workspace: Path, parallel: bool) -> Optional[Path]:
    """Which repo this run's commits actually land in.

    For an ordinary run that is `orphans.repo_for` - the workspace, or the
    portal's own source root for the meta-project. A parallel run commits in
    its worktree instead, and the meta-project keeps its exception either way:
    a parallel run on the portal still edits the source tree at APP_ROOT, so
    that is still the repo whose HEAD says what it did.
    """
    if parallel and slug != config.META_PROJECT_SLUG and (workspace / ".git").exists():
        return workspace
    return orphans.repo_for(slug)


def _record_workspace_heads(
    project: db.sqlite3.Row,
    run_id: int,
    head_before: Optional[str],
    repo: Optional[Path] = None,
) -> None:
    """Persist the repo's HEAD either side of the run, so app/revert.py can name
    and undo exactly what this run committed.

    Bare-excepted for the same reason as the proof check: this is bookkeeping
    for a button, and a git call that fails must never turn a good run into a
    recorded error. The cost of missing it is one run without an undo button.
    """
    try:
        head_after = proof.head_sha(
            repo if repo is not None else orphans.repo_for(project["slug"])
        )
        if head_before == head_after:
            return  # committed nothing; leave both NULL rather than store a no-op
        db.set_run_workspace_heads(run_id, head_before, head_after)
    except Exception:  # noqa: BLE001 - see docstring
        log.exception("Recording workspace heads failed for run %s", run_id)


def _note_unreadable_report(
    project: db.sqlite3.Row,
    result: agent_runner.RunResult,
    task: str,
) -> None:
    """Say so when the CLI could not parse a run's StructuredOutput call.

    A run that loses its report otherwise looks like an ordinary success: the
    work is committed, the run row says `ok`, and only the blank-looking
    journal entry hints that anything went wrong. That is the silent failure
    this portal keeps promising not to have, so it gets its own line whether
    the report was rescued or lost outright."""
    if not result.report_unreadable:
        return
    if result.report is not None:
        note = (
            f"Run ({task}): the Claude CLI could not parse this run's report, so "
            "the portal read it back out of the raw tool call instead. The entry "
            "above is that recovered report and may be missing its later fields."
        )
    else:
        note = (
            f"Run ({task}): the Claude CLI could not parse this run's report and "
            "none of it could be recovered, so its summary, journal entry and any "
            "todo or stage changes are lost. The work itself is committed."
        )
    try:
        db.add_journal(project["id"], "system", "status", note)
    except Exception:  # noqa: BLE001 - a diagnostic must never fail a finished run
        log.exception("Could not journal an unreadable report on %s", project["slug"])


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


async def _rerun_for_unseen_notes(project: db.sqlite3.Row) -> bool:
    """A note that arrived while this run was working is not a note this run
    read - so hold the project off the review shelf and put another run on it.

    Wes, 2026-08-17: *"When a project finishes running if it has queued notes
    that haven't been seen by the model yet don't switch it to a review yet,
    run it again with a new queued notes."*

    The deferral this completes is `note_arrived`'s. Typing a note while an
    agent is in the workspace deliberately queues nothing - two agents in one
    workspace is the 2026-07-29 double-run - so the request is simply dropped
    on the floor, and until now the only thing that could pick it back up was
    the ordinary rotation, which never comes for a project the finishing run
    has just parked in review. The note then sat unread behind a green "ready
    for you to look at" badge, which is the last place anybody goes looking for
    unfinished business.

    "No model has seen it" is not a second copy of that idea: `notes.deliver`
    stamps `delivered_at` at the moment the text is rendered into a prompt, so
    the predicate here is exactly the one the edit window already uses. A note
    the finished run *did* read is stamped and does not count.

    Which shelves this acts on is `reactivate_on_note`'s rule, on purpose:
    review and a pause both wake, and backlog/done/abandoned do not - on those
    three a note is stored and waits for a person, and a run happening as a
    side effect of writing one down would resurrect a project Wes had filed.

    Returns True if another run was queued.
    """
    project_id = int(project["id"])
    try:
        pending = notes.pending(project_id)
    except Exception:  # noqa: BLE001 - a bad read must not eat the run's report
        log.exception("Could not check for unseen notes on %s", project["slug"])
        return False
    if not pending:
        return False
    fresh = db.get_project(project_id)
    if fresh is None:
        return False

    parked = db.is_paused(fresh) or fresh["stage"] == "review"
    if parked:
        db.update_project(project_id, stage="active", paused=None)
    elif fresh["stage"] not in RUNNABLE_STAGES:
        return False

    count = len(pending)
    what = "A note" if count == 1 else f"{count} notes"
    them = "it" if count == 1 else "them"
    shelf = "held on **active**" if parked else "kept **active**"
    db.add_journal(
        project_id, "system", "status",
        f"{what} arrived while this run was working, so no model has read {them} "
        f"yet - {shelf} rather than surfacing for review, and queued another run "
        f"to act on {them}.",
    )
    await queue_manual_run(project_id)
    return True


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

# Set once the restart timer is armed, and never cleared - the only thing that
# clears it is the process ending, which is what it is waiting for. Every path
# that could start a run consults it.
#
# This exists because RESTART_DELAY_SEC is a request, not a guarantee. A
# transient `systemd-run --on-active` timer inherits AccuracySec=1min, and on
# 2026-07-29 the 3-second timer fired 59 seconds late. `_pending_restart` had
# already been set to None by then (it is cleared *before* _fire_restart, so it
# cannot re-fire), so for that whole minute the worker scheduled runs normally
# into a process that was about to be killed - and two of them were still very
# much alive when it was. The delay was tightened as well, but the flag is the
# actual fix: a deadline can always be beaten, and a latch cannot.
_restarting = False


def restart_armed() -> bool:
    """True once the service restart is scheduled and nothing new may start."""
    return _restarting


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


def schedule_source_restart(project_id: int, new_head: str) -> None:
    """Public entry to the self-update restart, for a source change the portal
    made itself rather than one an agent committed.

    Today that is exactly one caller: reverting a meta-project run's commits from
    the run page (see main.revert_run_route). `current_run_id` is deliberately
    None - no run is making this change, so every in-flight run counts and the
    restart waits behind all of them.
    """
    _schedule_self_restart(project_id, new_head, current_run_id=None)


def _fire_restart(project_id: int, new_head: str) -> None:
    """Actually schedule the service restart, detached (systemd-run), because
    this process is the service being restarted.

    Two things guard the window between arming the timer and the process dying.
    `AccuracySec` is pinned, because systemd's one-minute default turned a
    3-second delay into a 59-second one; and `_restarting` is latched, because
    no delay short enough to be safe is a delay you can rely on.
    """
    global _restarting
    try:
        subprocess.run(
            ["systemd-run", "--user", f"--on-active={RESTART_DELAY_SEC}",
             "--timer-property=AccuracySec=1ms",
             "systemctl", "--user", "restart", "project-portal.service"],
            check=True, capture_output=True, text=True,
        )
        _restarting = True
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
            _localize_skill(dest / "SKILL.md")
        for stale in dest_root.iterdir():
            if stale.is_dir() and stale.name not in sources:
                shutil.rmtree(stale)
    except OSError as exc:
        # A workspace without its skills still runs; it just runs blinder.
        log.warning("could not sync skills into %s: %s", workspace, exc)
        return
    _exclude_from_git(workspace, ".claude/")


def _localize_skill(skill_md: Path) -> None:
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
            log.warning("could not localize %s: %s", skill_md, exc)


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
    # himself, so each has its own lock.
    #
    # A locked title is not a gag. Wes, 2026-07-29: "if a title is defined, do
    # not change it. If you want to suggest alternative titles, feel free to,
    # but do not change a title that the user set themselves." So the proposal
    # is parked on the project page as a one-click offer instead of being
    # dropped - and `db.propose_title` throws away the re-proposals (same as the
    # current title, same as the one he already turned down) so the offer stays
    # rare enough to be worth reading. Still not journalled: the suggestion is a
    # live offer, not an event.
    title = report.get("title")
    if title and not project["title_locked"]:
        updates["title"] = title
    elif title:
        db.propose_title(project, title)

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


def max_agents_per_project() -> int:
    """How many agents may be inside one project at once, counting the
    ordinary run. Never below 1: a zero here would silently disable runs."""
    raw = db.get_setting(parallel_runs.MAX_AGENTS_SETTING) or ""
    try:
        return max(1, int(raw))
    except ValueError:
        return parallel_runs.DEFAULT_MAX_AGENTS


async def start_parallel_run(project: db.sqlite3.Row) -> tuple[bool, str]:
    """Wes pressed "parallel run": put a *second* agent on this project now,
    beside the one already working, in a git worktree of its own.

    Started here and now rather than through `manual_queue`, because that queue
    exists precisely to hold a request back until the project is free - which
    is the opposite of what this button means.

    Returns (started, why-not). The refusals are deliberately different
    sentences: "the portal is full" clears by itself in minutes and "this
    project already has its agents" needs a setting changed, and a button that
    said the same thing for both would send Wes to the wrong place.
    """
    project_id = int(project["id"])
    live = db.running_runs_for_project(project_id)
    if not live:
        # Nothing to be parallel *to*. Rather than refuse - which would read as
        # the button being broken when a run happened to finish while he was
        # typing - do what he asked for, which is an agent on this note now.
        await run_now(project)
        return True, ""

    cap = max_agents_per_project()
    if len(live) >= cap:
        return False, (
            f"Not started: this project is already running {len(live)} agent(s), which "
            f"is its limit. Raise `{parallel_runs.MAX_AGENTS_SETTING}` in Settings to "
            f"allow more. The note is saved and the run in flight will read it."
        )

    slots = pacing.parallel_cap(db.max_parallel_runs())
    if len(_inflight) >= slots:
        return False, (
            f"Not started: the portal is already running {len(_inflight)} agent(s) "
            f"across all projects, which is its cap ({slots}). The note is saved and "
            f"will be picked up as soon as a slot frees."
        )

    # Asking for a second agent on a project that is paused or parked in review
    # is asking for the project back - the same rule a plain note follows. It is
    # deliberately NOT a build approval (unlike "add & run now"): a gated
    # project gets a second *planning* agent, and opening the gate stays its own
    # click.
    if db.display_state(project) != "active":
        db.update_project(project_id, stage="active", paused=None)
        db.add_journal(
            project_id, "system", "status",
            "Parallel run requested - moved back to **active**.",
        )
        project = db.get_project(project_id) or project

    task = task_for(project, manual=True)
    if task is None:  # pragma: no cover - task_for only returns None on a dead stage
        return False, "Not started: there is no task this project can be run on."
    spawn_run(project, task, parallel=True)
    return True, ""


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


# The shelves a run belongs on. Backlog means "no model yet" (his words), and
# done/abandoned are finished - on those three, a run happens only because
# somebody explicitly asked for one, never as a side effect of writing a note.
RUNNABLE_STAGES = {"active", "review"}


def can_run_now(project: db.sqlite3.Row) -> bool:
    """Would a run queued this second actually start on this project?

    Only the two things that genuinely stop a manual run: the shelf the project
    is on, and whether an agent is already in its workspace. Manual runs bypass
    the daily cap, quiet hours, the saturation gap and the crash-loop hold by
    design (`_pick_project`, `_start_one`), so consulting those here would
    answer a different, more pessimistic question than the one asked.

    Paused counts as runnable: a pause lives beside the stage rather than
    replacing it, and a note on a parked project has always woken it up.
    """
    if str(project["stage"]) not in RUNNABLE_STAGES:
        return False
    if db.is_project_running(int(project["id"])):
        return False
    return not workspace_leased(str(project["slug"]))


async def note_arrived(project: db.sqlite3.Row) -> bool:
    """The plain green "add note" button: store the note, then put an agent on
    it if one could start right now.

    Wes, 2026-08-10: "Make the 'Add note' button automatically run now if the
    project can be run now." Writing a note IS the request - the separate
    press for it was a second door onto the same room. It is deliberately NOT
    a build approval (unlike "add & run now", which is Wes putting the project
    on the working shelf): a gated project gets a planning pass, and opening
    the gate stays its own click.

    Returns True if a run was queued.
    """
    if await reactivate_on_note(project):
        return True
    if not can_run_now(project):
        return False
    await queue_manual_run(int(project["id"]))
    return True


CANCEL_RESULTS = {"cancelled", "orphaned", "not_running", "missing"}


def cancel_run(run_id: int) -> str:
    """Stop a run on request. Returns one of `CANCEL_RESULTS`.

    `orphaned` means the row said 'running' but nothing could be found to kill -
    a leftover from before a restart. Killing nothing and leaving the row
    'running' would block the worker forever via `is_run_running()`, so the row
    is settled here instead.

    "Nothing owns it" and "nothing is running" are different claims, and this
    used to conflate them: a run adopted across a restart has no entry in
    `agent_runner._ACTIVE_PROCS`, but its agent is alive in its own systemd
    scope and pressing cancel would settle the row while leaving the agent
    editing the workspace - which is the double-run failure with extra steps.
    So the scope is tried before giving up, and stopping the unit kills every
    process in it rather than only the one we happen to have a pid for.
    """
    run = db.get_run(run_id)
    if run is None:
        return "missing"
    if run["status"] != "running":
        return "not_running"
    if agent_runner.cancel_run(run_id):
        return "cancelled"
    unit = run["scope_unit"]
    if unit and runlimit.scope_is_active(unit) is True and runlimit.stop_scope(unit):
        db.finish_run(
            run_id, "cancelled",
            summary="Canceled: the run had outlived the portal process that "
                    "started it, and its systemd scope was stopped.",
        )
        log.warning("Cancel requested for adopted run %s; scope %s stopped", run_id, unit)
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

    # Before the messages are spent, and that ordering is the whole point of
    # checking here rather than only reading `result.lock_conflict` below.
    # `mark_oneoff_delivered` is one-way - there is no undeliver - so a refusal
    # discovered after it would burn the person's message on a run that never
    # read it. Left pending, the messages are picked up by whichever agent is
    # holding the workspace when it finishes: its own `_continue_if_messages_
    # waiting` is what re-spawns. Deliberately no re-spawn from here, which
    # would be a tight loop against a lease that is still held.
    if worklock.is_busy(ws) is True:
        note = worklock.refused_note(worklock.oneoff_resource(task_id))
        log.warning("One-off run %s refused: task %s workspace is leased", run_id, task_id)
        db.finish_run(run_id, "error", summary=note)
        return

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
            # One agent per task workspace. `db.oneoff_running` is a SELECT on
            # runs.status, which is the derived answer this module exists to
            # stop trusting alone; and two agents resuming one CLI session fork
            # the conversation as well as the checkout.
            lock_dir=ws,
        )
    finally:
        hookguard.end(run_id)

    if result.lock_conflict:
        # The backstop for the pre-flight above, for the window between the two.
        # The messages are already marked delivered by this point and cannot be
        # un-marked, so this says plainly that they need sending again rather
        # than leaving somebody waiting on an agent that never read them.
        db.finish_run(
            run_id, "error", summary=worklock.refused_note(worklock.oneoff_resource(task_id))
        )
        db.add_oneoff_message(
            task_id, "system",
            "Another agent is still working in this task's workspace, so this run "
            "stopped without reading your message rather than putting two agents in "
            "one directory. Send it again once the other run has finished.",
            run_id=run_id,
        )
        return

    if result.cancelled:
        db.finish_run(run_id, "cancelled", summary="Canceled from the portal.")
        db.add_oneoff_message(task_id, "system", "Run stopped from the portal.", run_id=run_id)
        return

    if result.is_rate_limited:
        until, why = await _rate_limit_backoff(
            result.retries.quota if result.retries else None
        )
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
    # The slot above dies with the portal process; the lease does not. A reflect
    # adopted across a restart is invisible to `_inflight` and would otherwise
    # get a second agent rewriting profile.md beside it.
    if memory_leased():
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


def _memory_failure_note(
    result: agent_runner.RunResult, job: str, max_turns: int
) -> str:
    """Why a memory run stopped without finishing, in one line fit for the journal.

    Both memory jobs rewrite Wes's files in place, so a run killed part-way
    through leaves a half-made edit on disk. Saying which failure it was is the
    difference between "restore the revision" and "leave it alone".
    """
    if result.hit_max_turns:
        return (
            f"The {job} hit the {max_turns}-turn ceiling and was stopped mid-work. "
            f"Whatever it had already written is on disk, and the version from "
            f"before it ran is on /memory under revisions. If this keeps "
            f"happening, raise `memory_max_turns` on the settings page."
        )
    if result.oom_killed:
        return f"The {job} was killed for memory. {runlimit.kill_note(peak=result.peak_memory_bytes)}"
    detail = (result.result_text or "").strip()
    if detail:
        return f"The {job} failed before it finished: {detail[:300]}"
    if result.subtype:
        return f"The {job} failed before it finished; the CLI reported `{result.subtype}` with no message."
    return f"The {job} failed before it finished, with no message from the CLI."


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

    before_profile = _profile_size()
    run_id = db.create_run(None, "reflect", model)
    prompt = agent_runner.build_prompt("reflect", None)
    log.info("Running daily reflect (run_id=%s)", run_id)
    max_turns = memory_max_turns()
    result = await agent_runner.run_claude(
        prompt, cwd, model, timeout_min, max_turns=max_turns,
        on_event=_live_logger(run_id),
        run_id=run_id, json_schema=report_schema.schema_json(),
        # One agent in the memory directory, enforced by the kernel. The
        # compaction leases the same path - see worklock's module docstring.
        lock_dir=cwd,
    )

    if result.lock_conflict:
        # Deliberately without stamping `last_reflect_date`: nothing ran, so the
        # day has not been reflected on and the next quiet tick should try again.
        note = worklock.refused_note(worklock.MEMORY_RESOURCE)
        db.finish_run(run_id, "error", summary=note)
        db.add_journal(None, "system", "status", note)
        return

    if result.cancelled:
        db.finish_run(run_id, "cancelled", summary="Reflect canceled from the portal.")
        return

    if result.is_rate_limited:
        until, why = await _rate_limit_backoff(
            result.retries.quota if result.retries else None
        )
        db.finish_run(run_id, "error", summary=f"Rate limited during reflect; back at {until.isoformat(timespec='minutes')} ({why})")
        return

    if result.timed_out:
        db.finish_run(run_id, "timeout", summary="Reflect timed out")
        db.add_journal(None, "system", "status", "Daily reflect timed out.")
        return

    if not result.ok:
        # A catch-all, deliberately guarding on `ok` rather than naming another
        # failure mode: every specific guard above returns, and anything that
        # reaches here used to fall straight through to the success path and
        # journal "Daily reflect ran" over a run that had been killed. That is
        # what happened on 2026-08-07 - run 892 died at the turn ceiling and the
        # journal recorded a clean reflect of a profile.md it had only half
        # rewritten. Enumerating failures leaves the next new one silent; asking
        # whether the run succeeded does not.
        note = _memory_failure_note(result, "daily reflect", max_turns)
        db.finish_run(
            run_id, "error", result.session_id, result.cost_usd,
            result.num_turns, note,
        )
        db.add_journal(None, "system", "status", note)
        # Stamped even though it failed, for the reason the compaction gives at
        # its own kick point: this is a real agent run that spends allowance, and
        # a reflect that ran out of turns will run out of them again on the same
        # input. At most one attempt a day. The transient failures above
        # (rate limit, lock conflict, cancel) deliberately do not stamp.
        db.set_setting("last_reflect_date", daycycle.current_day())
        return

    db.finish_run(run_id, "ok", result.session_id, result.cost_usd, result.num_turns, result.result_text[:500])
    db.set_setting("last_reflect_date", daycycle.current_day())

    # Measured off disk rather than taken from the report, for the reason the
    # compaction path states: the agent describing its own edit is the one
    # source that cannot be checked, and this file is 31% of every prompt.
    after_profile = _profile_size()
    summary = (
        f"profile.md reviewed and updated: {before_profile[1]} -> {after_profile[1]} "
        f"chars (cap {profile_cap_bytes() // 1024} KB)."
        if profile_cap_bytes() else
        f"profile.md reviewed and updated: {before_profile[1]} -> {after_profile[1]} chars."
    )
    if profile_over_cap():
        summary += (
            " Still over its cap - it is pasted whole into every run's prompt, so the "
            "next reflect is told again, and a prompt drops whole sections off the "
            "bottom past twice the cap."
        )
    if result.report:
        suggestion = result.report.get("suggestion")
        if suggestion and suggestion.get("title"):
            db.add_suggestion(suggestion["title"], suggestion.get("description", ""))
            summary += f" New suggestion: {suggestion['title']}."
    db.add_journal(None, "system", "reflect", f"Daily reflect ran. {summary}")


def _profile_size() -> tuple[int, int]:
    try:
        text = config.PROFILE_MD.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return (0, 0)
    return (len(text.splitlines()), len(text))


def profile_cap_bytes() -> int:
    """The size profile.md is meant to stay under. 0 disables it.

    Unlike learnings.md this has no compaction job of its own: the daily reflect
    already rewrites the whole file every day, so the cap is enforced by telling
    that run about it (`agent_runner._profile_target_section`). One knob, one
    mechanism, no second agent run to pay for.
    """
    return agent_runner.profile_cap_bytes()


def profile_over_cap() -> bool:
    cap = profile_cap_bytes()
    return cap > 0 and _profile_size()[1] > cap


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
    once the file crosses `learnings_cap_kb`. The original reason this was a
    button and not a timer - a background job silently rewriting Wes's memory is
    what lost his profile text once - is answered by `run_compaction` snapshotting
    the file into /memory revisions before the agent touches it, so nothing the
    auto-run cuts is unrecoverable, and the compaction lands loudly in the
    journal. The auto-trigger only touches learnings.md, never profile.md.
    """
    _reap_inflight()
    if compaction_running():
        return False
    # And the half `compaction_running` cannot see: a reflect working in the
    # same directory, or a compaction started by a portal process that has since
    # restarted. Both leave `_inflight` empty and the directory occupied.
    if memory_leased():
        return False
    _inflight[COMPACT_SLOT] = asyncio.create_task(run_compaction())
    return True


# The cap, its target and the reach measurement live in `app.memory` - the
# compaction prompt in agent_runner needs all three and cannot import this
# module (worker imports agent_runner). Re-exported here because the worker is
# where the trigger lives and every existing caller reads them from it.
learnings_cap_kb = memory.learnings_cap_kb
learnings_target_kb = memory.learnings_target_kb
learnings_reach = memory.learnings_reach


def learnings_over_cap() -> bool:
    """True when learnings.md is past its cap and the cap is enabled.

    "KB" here means `len(text)` against `kb * 1024`, which for a file with any
    non-ASCII in it is characters rather than bytes - and deliberately so,
    because that is exactly what `agent_runner._budget_bytes` and every prompt
    budget beside it measure. A cap has to be in the same unit as the thing it
    protects; making this one a true byte count would silently desync it from
    the budget it exists to keep the file inside.
    """
    cap = learnings_cap_kb()
    return cap > 0 and _learnings_size()[1] > cap * 1024


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
    if learnings_cap_kb() == 0:
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
    kb = _learnings_size()[1] / 1024
    reach = learnings_reach()
    log.info(
        "learnings.md is %.1f KB (cap %d KB); %d of %d entries never reach a prompt; "
        "auto-compacting", kb, learnings_cap_kb(), reach.entries_out, reach.entries_total,
    )
    db.set_setting("last_auto_compact_date", today)
    # The journal line names the reach, not just the size, because the size on
    # its own reads as housekeeping. "43 KB of it never reaches a prompt" is the
    # sentence that says why this run is worth its allowance.
    unread = (
        f" {reach.entries_out} of its {reach.entries_total} learnings never reach a "
        f"prompt at all."
        if reach.entries_out
        else ""
    )
    db.add_journal(
        None,
        "system",
        "status",
        f"learnings.md reached {kb:.1f} KB (cap {learnings_cap_kb()} KB); "
        f"auto-compacting.{unread} The previous version is kept under /memory revisions.",
    )
    start_compaction()


async def run_compaction() -> None:
    model = agent_runner.resolve_model(None)
    timeout_min = int(db.get_setting("run_timeout_min") or "30")
    cwd = config.MEMORY_DIR
    cwd.mkdir(parents=True, exist_ok=True)

    # Read before the agent runs, and kept for the whole call: the success path
    # diffs it against what the agent left to work out what was dropped, and a
    # revision on disk is no substitute because it ages out of the 60-deep
    # history while the archive is permanent.
    before_text = _learnings_text()

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
    max_turns = memory_max_turns()
    result = await agent_runner.run_claude(
        prompt, cwd, model, timeout_min, max_turns=max_turns,
        on_event=_live_logger(run_id),
        run_id=run_id, json_schema=report_schema.schema_json(),
        # The same directory the reflect leases, on purpose: both rewrite
        # learnings.md, and one of them rewrites profile.md too.
        lock_dir=cwd,
    )

    if result.lock_conflict:
        # `last_auto_compact_date` is deliberately left as it is. The automatic
        # path cannot reach here - `_maybe_compact` is gated on
        # `db.is_run_running()`, which a live reflect makes true - so a refusal
        # means the /memory button was pressed during a reflect, and that path
        # stamps no date to unwind.
        note = worklock.refused_note(worklock.MEMORY_RESOURCE)
        db.finish_run(run_id, "error", summary=note)
        db.add_journal(None, "system", "status", note)
        return

    if result.cancelled:
        db.finish_run(run_id, "cancelled", summary="Learnings compaction canceled from the portal.")
        return
    if result.is_rate_limited:
        until, why = await _rate_limit_backoff(
            result.retries.quota if result.retries else None
        )
        db.finish_run(run_id, "error", summary=f"Rate limited during compaction; back at {until.isoformat(timespec='minutes')} ({why})")
        return
    if result.timed_out:
        db.finish_run(run_id, "timeout", summary="Learnings compaction timed out")
        db.add_journal(None, "system", "status", "Learnings compaction timed out.")
        return

    if not result.ok:
        # The same catch-all as the reflect, and for the same reason: run 905
        # was killed at the turn ceiling part-way through rewriting a 59 KB
        # learnings.md, and the journal announced "Learnings compacted" with a
        # before/after that looked like a clean 71% cut. The sizes were real -
        # measured off disk - which is exactly what made the line convincing.
        # A half-finished edit reported as a finished one is worse than a loud
        # failure, because it is the version Wes would not think to restore.
        note = _memory_failure_note(result, "learnings compaction", max_turns)
        after = _learnings_size()
        db.finish_run(
            run_id, "error", result.session_id, result.cost_usd,
            result.num_turns, note,
        )
        db.add_journal(
            None,
            "system",
            "status",
            f"{note} learnings.md is {before[0]} lines / {before[1]} chars -> "
            f"{after[0]} lines / {after[1]} chars, which may be a part-made edit.",
        )
        return

    db.finish_run(run_id, "ok", result.session_id, result.cost_usd, result.num_turns, result.result_text[:500])
    after = _learnings_size()
    # Only on the success path. A killed compaction leaves a part-made edit, and
    # diffing against that would archive every line the agent had not reached
    # yet as though it had decided to drop them - writing a fiction into the one
    # record that is supposed to be permanent.
    archived = memory.archive_compaction_losses(before_text, _learnings_text())
    kept = (
        f" {archived} dropped learning{'s' if archived != 1 else ''} went to the "
        f"retired-learnings archive on /memory, which does not expire."
        if archived
        else ""
    )
    # Measured rather than taken from the report: the number that matters is
    # what the file on disk actually weighs now, and the agent describing its
    # own edit is the one source that cannot be checked.
    db.add_journal(
        None,
        "system",
        "reflect",
        f"Learnings compacted: {before[0]} lines / {before[1]} chars -> "
        f"{after[0]} lines / {after[1]} chars. The previous version is on /memory "
        f"under revisions if anything useful went missing.{kept}",
    )


def _learnings_text() -> str:
    try:
        return config.LEARNINGS_MD.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _learnings_size() -> tuple[int, int]:
    text = _learnings_text()
    return (len(text.splitlines()), len(text))

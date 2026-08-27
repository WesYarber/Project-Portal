"""Back off a project whose runs die before the agent ever starts.

The bug this exists for, seen 257 times between 2026-07-26T02:38 and
2026-07-27T05:51: OpenJournal's prompt grew past 128 KiB, the kernel refused
the spawn with `OSError: [Errno 7] Argument list too long`, and the run was
marked `error` about 70 ms after it was created. The scheduler then picked the
same project again on the next tick, because nothing anywhere remembered that
the last attempt had been pointless. Wes found out by noticing a wall of red on
the runs page - the portal itself never said a word.

`agent_runner` no longer passes the prompt in argv, so that particular spawn
failure is gone. This module exists because the *loop* was the part that made
it expensive, and the loop is not specific to argv length: a missing CLI, a
full disk, a bad `--settings` payload, a revoked login, any future crash
between "create the run row" and "the agent's first event" produces exactly the
same shape. Fixing only the cause of the day leaves the amplifier in place.

**What counts as a dead start:** a run that ended in `error` without ever
reaching the model. Two shapes qualify. A run with *zero* stream events never
even booted the CLI. And - learned the hard way on 2026-08-06, when an expired
OAuth session was retried once a minute for twenty runs straight - a run whose
CLI booted, printed one error and quit: that emits a *handful* of events
(session start, the error line, the failure result - exactly 3 that day) while
billing nothing at all. So a few-event error run counts as dead only when its
cost and output tokens are recorded as zero; an agent that reached the model
always billed something. An agent that started, worked and then failed has
events in the hundreds and real usage and is a normal bad run - it made
progress, it wrote a journal entry, retrying it promptly is right.

**Why backoff rather than a stop.** A project the scheduler refuses to touch
until somebody clears a flag is a project that stays broken while Wes is
asleep, and the failure is usually environmental - the thing that fixes it
(a restart, a deploy, a disk freed up) leaves no signal here. Doubling the wait
converges on "a few attempts a day" within an hour, which costs nothing and
recovers on its own the moment the cause goes away. A manual run ignores all of
this: pressing the button is Wes saying "try it now", and it is how he would
check whether a fix took.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import db

log = logging.getLogger("portal.crashloop")

# When this portal process started. Stamped at import, which is close enough:
# the worker loop begins moments later.
#
# This exists because of a trap the fix for the original incident walked
# straight into. OpenJournal had 257 dead starts behind it, so the moment the
# breaker shipped it would have held the project at the 6h ceiling - six hours
# of suppression for a project that had *just been fixed*, with the evidence
# for the hold being runs of code that no longer existed. A streak is evidence
# about an environment, and deploying new code, restarting the service or
# freeing up a disk all replace that environment while leaving no trace here.
#
# So a restart buys exactly one free attempt: if the last try predates this
# process, the hold lifts. If that attempt dies too, its timestamp is now
# after the boot and the full backoff applies again - so a genuinely broken
# project costs one run per restart rather than one per tick, and a fixed one
# recovers immediately.
BOOT_TIME = datetime.now(timezone.utc)

# The wait after a single dead start. Short enough that a one-off blip (a
# restart caught mid-spawn) costs one cycle rather than an evening.
BASE_DELAY_MIN = 5
# The ceiling on the doubling: four attempts a day, forever, without anybody
# having to clear anything.
MAX_DELAY_MIN = 6 * 60
# How many in a row before the portal says so out loud. One is noise - the
# spawn races a service restart often enough. Three in a row is a pattern, and
# it is reached in 15 minutes, so the notice still arrives while it is news.
NOTIFY_AFTER = 3
# How far back to look. A project that has genuinely failed this many times in
# a row is already at the delay ceiling, so counting further changes nothing.
SCAN_LIMIT = 40
# The most events a CLI can emit while booting and dying without the model:
# session start, an error line, the failure result. The 2026-08-06 auth outage
# produced exactly 3 per run. Above this, an error run is presumed to have
# been a real attempt regardless of what it billed - run 704 recorded 770
# events with zeroed token columns, so the count has to win over the billing.
DEAD_START_MAX_EVENTS = 4


def is_dead_start(run: db.sqlite3.Row) -> bool:
    """Did this run die before the agent produced anything at all?

    `events` is NULL on a row created by an older portal and on a run still in
    flight; neither is a dead start, and treating NULL as zero would have every
    in-progress run look like a crash the moment it was created.
    """
    if run["status"] != "error":
        return False
    try:
        events = run["events"]
    except (IndexError, KeyError):  # pragma: no cover - pre-migration row
        return False
    if events is None:
        return False
    if int(events) == 0:
        return True
    # A handful of events is ambiguous: the CLI's boot-and-fail emits 2-4, and
    # so could a very short real attempt. Billing settles it - a run that
    # reached the model billed output tokens or cost, and the CLI reports
    # explicit zeros (not NULL) when it dies at the starting line. NULL means
    # an old row or an unparsed result, which stays conservative.
    return int(events) <= DEAD_START_MAX_EVENTS and _billed_nothing(run)


def _billed_nothing(run: db.sqlite3.Row) -> bool:
    """Both usage columns present and zero - the model was never reached."""
    for col in ("cost_usd", "output_tokens"):
        try:
            value = run[col]
        except (IndexError, KeyError):
            return False
        if value is None or float(value) > 0:
            return False
    return True


def consecutive_dead_starts(project_id: int) -> int:
    """How many of this project's most recent runs died before starting.

    Counts backwards from the newest run and stops at the first one that did
    anything, so a single healthy run resets the whole thing - which is what
    makes the recovery automatic rather than something Wes has to clear.
    """
    streak = 0
    for run in db.list_runs(project_id, limit=SCAN_LIMIT):
        # A run still in flight is not evidence either way. Skipping rather
        # than stopping matters because the portal runs projects in parallel:
        # the newest row for this project can be the attempt being decided
        # right now.
        if run["status"] == "running":
            continue
        if not is_dead_start(run):
            break
        streak += 1
    return streak


def delay_min(streak: int) -> int:
    """The wait owed after `streak` dead starts in a row: 5, 10, 20, 40 ...

    Capped, and 0 for a project that is not in a loop at all.
    """
    if streak <= 0:
        return 0
    return min(MAX_DELAY_MIN, BASE_DELAY_MIN * (2 ** (streak - 1)))


def _parse_iso(text: str) -> Optional[datetime]:
    try:
        stamp = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def last_attempt_at(project_id: int) -> Optional[datetime]:
    """When this project's most recent finished run started."""
    for run in db.list_runs(project_id, limit=SCAN_LIMIT):
        if run["status"] == "running":
            continue
        return _parse_iso(run["started_at"] or "")
    return None


def held(project_id: int, now: Optional[datetime] = None) -> Optional[str]:
    """Why this project is being held back from a scheduled run, or None.

    Returning a sentence rather than a bool is deliberate: the same string is
    what the dashboard shows for "why is nothing running", and a hold nobody
    can see the reason for is the failure mode this whole module is about.
    """
    streak = consecutive_dead_starts(project_id)
    if streak <= 0:
        return None
    wait = delay_min(streak)
    started = last_attempt_at(project_id)
    if started is None:  # pragma: no cover - defensive
        return None
    # The portal has restarted since the last attempt, so the thing that was
    # failing may well be gone. Worth one run to find out. See BOOT_TIME.
    if started < BOOT_TIME:
        return None
    now = now or datetime.now(timezone.utc)
    ready_at = started + timedelta(minutes=wait)
    if now >= ready_at:
        return None
    left = ready_at - now
    minutes = max(1, int(left.total_seconds() // 60))
    return (
        f"{streak} run(s) in a row died before the agent started; "
        f"waiting {minutes} more minute(s) before trying again"
    )


def note_for(streak: int) -> str:
    """The journal entry written when the breaker first trips."""
    return (
        f"**{streak} runs in a row on this project died before the agent "
        f"started** - each run ended without the model ever being reached "
        f"(no events, or a bare CLI error that billed nothing). That is an "
        f"environment failure rather than anything "
        f"the agent did, so the portal is now spacing attempts out "
        f"({delay_min(streak)} minutes and doubling, up to "
        f"{MAX_DELAY_MIN // 60}h) instead of retrying every tick. It clears "
        f"itself as soon as one run gets going. The service log has the "
        f"traceback."
    )


def should_announce(streak: int, already_announced: int) -> bool:
    """Announce at the threshold, then only on each doubling of the streak.

    Without the doubling rule a stuck project would file the same notice every
    few minutes - which is precisely the "same thing over and over" that Wes
    has already told the portal off for once.
    """
    if streak < NOTIFY_AFTER:
        return False
    if already_announced <= 0:
        return True
    return streak >= already_announced * 2

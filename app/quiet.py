"""Quiet hours: the portal does not start scheduled runs while Wes is asleep.

Wes, 2026-08-07:

  "This project ran a ton overnight without me meaning for it to and used up a
  ton of my weekly limit. Please help tame that. I'm not certain that it was at
  all necessary."

He was right, and the runs table says exactly what happened. Between 18:00 on
the 6th and 09:00 on the 7th the portal started 62 runs. Nothing was
misbehaving: `worker_interval_min` is 0 and `max_runs_per_day` is 80 (both
lifted by him during a spend-down back in July and never put back), so the only
thing standing between the scheduler and a flat-out sprint was
`pacing.saturation_hold`, whose ceiling is 85%. Measured over rolling five-hour
spans, the overnight duty cycle came out at 76.7, 79.8, 83.3, 90.3, 84.7,
84.8%. The guard was working perfectly. It was holding the portal at exactly
the level it is designed to hold it at - all night, every night.

That is the gap this module fills, and it is a different kind of limit from
everything in `pacing.py`. Those all answer "how much allowance is left?".
This one answers "is he awake?", which no meter can report:

* A run finished at 03:00 is not read until 08:00, so five hours of allowance
  bought nothing that an 08:00 run would not have bought.
* Anything a night run gets wrong compounds for eight hours before he can say
  so - and this portal self-updates and restarts from its own runs.
* Overnight is where his weekly window went. The saturation guard's own
  premise is that continuous round-the-clock running is the shape that *looks*
  like a bot; an idle night is the cheapest way to not be one.

Two deliberate decisions, both the opposite of `saturation_hold`:

**A spend-down is not exempt.** The saturation guard exempts one, because a
spend-down is Wes saying "burn the allowance". But he said that about a weekly
window with hours left on it, not about a night - and this note is him telling
us the nights were the problem. If a spend-down and quiet hours ever collide,
the resolution is that the spend-down resumes in the morning, not that the
portal runs until dawn. He can turn quiet hours off in Settings, which is a
decision he makes once and can see.

**The zone is configured, not the host's.** This server runs on UTC, so "23:00
local" on the box is 18:00 in Central time - the middle of his evening, which
is when he is most likely to be watching the board. Getting this wrong does not
fail loudly; it just silently holds runs at the wrong end of the day. So the
zone is its own setting, defaulted to where he actually lives.

Runs already in flight are never touched. Quiet hours hold a run from
*starting*; a run that began at 22:50 finishes at its own pace, because killing
work half-done is the thing this portal keeps refusing to do. Manual runs never
hold either - `run now` at 2am is a decision, and the whole point of a manual
run is that it overrides the pacing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import db

log = logging.getLogger("portal.quiet")

START_SETTING = "quiet_hours_start"
END_SETTING = "quiet_hours_end"
ZONE_SETTING = "quiet_hours_zone"

# 23:00 to 07:00, which is the night the runs table shows him not reading.
# Defaults rather than opt-in, because the behavior he reported as wrong is the
# behavior a fresh install would also have.
DEFAULT_START = 23
DEFAULT_END = 7
# Central time. The host is on UTC and the difference is five hours, so
# defaulting to the host zone would put quiet hours across his evening.
DEFAULT_ZONE = "America/Chicago"


def zone() -> object:
    """The timezone quiet hours are expressed in.

    Falls back to the default and then to UTC rather than raising: a typo in a
    setting must not be able to stop the worker, and holding on the wrong hours
    is a far smaller failure than a scheduler that cannot decide anything.
    """
    name = (db.get_setting(ZONE_SETTING) or "").strip() or DEFAULT_ZONE
    for candidate in (name, DEFAULT_ZONE):
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("Unknown quiet-hours timezone %r", candidate)
    return timezone.utc


def zone_choices() -> list[str]:
    """The zone names the settings dropdown offers, current value included.

    Not the whole tz database - nearly six hundred names is a scroll nobody
    reads. The US zones he might plausibly be in, UTC because the host is on
    it, and whatever is configured today so a zone set by hand (or shipped by a
    future default) can never vanish from the list that is supposed to show it.
    """
    common = [
        "America/Chicago",
        "America/New_York",
        "America/Denver",
        "America/Phoenix",
        "America/Los_Angeles",
        "America/Anchorage",
        "Pacific/Honolulu",
        "UTC",
    ]
    current = (db.get_setting(ZONE_SETTING) or "").strip()
    if current and current not in common:
        common.append(current)
    return common


def _hour(setting: str, default: int) -> int:
    raw = db.get_setting(setting)
    try:
        value = default if raw in (None, "") else int(raw)
    except (TypeError, ValueError):
        return default
    return value if 0 <= value <= 23 else default


def window() -> Optional[tuple[int, int]]:
    """The (start, end) hours quiet runs from, or None when it is off.

    Equal hours mean off, which is the only sane reading: a window from 07:00
    to 07:00 is either nothing or the whole day, and "nothing" is the one a
    person setting both boxes to the same number wants. It is also how the
    setting is turned off without a third checkbox to keep in step with it.
    """
    start, end = _hour(START_SETTING, DEFAULT_START), _hour(END_SETTING, DEFAULT_END)
    return None if start == end else (start, end)


def _local(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(zone())


def is_quiet(now: Optional[datetime] = None) -> bool:
    """Whether the given moment falls inside quiet hours.

    The window wraps midnight in the ordinary case (23 -> 7), so the test is a
    union rather than a range; a non-wrapping window (1 -> 5) is the plain
    range. Half-open at the end: at exactly 07:00 the portal is working again,
    which is what "runs resume at 7" has to mean for the resume time below to
    be the truth.
    """
    span = window()
    if span is None:
        return False
    start, end = span
    hour = _local(now).hour
    return hour >= start or hour < end if start > end else start <= hour < end


def resumes_at(now: Optional[datetime] = None) -> Optional[datetime]:
    """When scheduled runs start again, or None if they are not held.

    Returned in the quiet zone so a caller can render it as the hour he set,
    rather than as an offset he has to convert.
    """
    span = window()
    if span is None or not is_quiet(now):
        return None
    local = _local(now)
    end = span[1]
    resume = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if resume <= local:
        resume += timedelta(days=1)
    return resume


def quiet_hold(now: Optional[datetime] = None) -> Optional[dict]:
    """The quiet-hours hold in force right now, or None.

    Shaped like `pacing.scheduled_hold` and `pacing.saturation_hold` - a dict
    the caller renders, or None - so the worker's guards all read the same way.
    Fails open on any error for the same reason they do: a bug in a guard may
    insert a gap it can explain, never idle the portal for good.
    """
    try:
        if not is_quiet(now):
            return None
        span = window()
        resume = resumes_at(now)
        if span is None or resume is None:  # pragma: no cover - is_quiet implies both
            return None
        local = _local(now)
        return {
            "start": span[0],
            "end": span[1],
            "resumes_at": resume.strftime("%H:%M"),
            "resumes_in": int((resume - local).total_seconds()),
            "zone": str(resume.tzinfo),
        }
    except Exception:  # noqa: BLE001 - fail open, never idle the portal on a bug
        log.exception("quiet hours guard failed")
        return None


def quiet_reason(hold: dict) -> str:
    from app import daycycle

    left = daycycle.humanize_seconds(hold.get("resumes_in") or 0)
    return (
        f"quiet hours - the portal does not start scheduled runs between "
        f"{hold['start']:02d}:00 and {hold['end']:02d}:00 {hold['zone']}, so the next "
        f"one starts at {hold['resumes_at']} in about {left} (run now still goes)"
    )

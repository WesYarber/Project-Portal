"""The portal's "day", which starts at 05:00 local time rather than midnight.

Everything that resets daily - the run budget, the temporary bonus, the reflect
job - is keyed on `current_day()` rather than on the calendar date, so a run at
02:00 still counts against the previous evening's budget. That matches how the
day actually feels from the outside: work done after midnight is still tonight's
work, and a fresh budget arriving at 5am is a fresh morning.

The boundary hour is a setting (`day_reset_hour`, 0-23) so it can move without a
code change; the timezone is the host's local zone.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app import db

DEFAULT_RESET_HOUR = 5


def reset_hour() -> int:
    """The configured boundary hour, clamped to a real hour of the day. A
    garbage or out-of-range setting falls back to the default rather than
    raising - the budget must keep working."""
    raw = db.get_setting("day_reset_hour")
    try:
        hour = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_RESET_HOUR
    return hour if 0 <= hour <= 23 else DEFAULT_RESET_HOUR


def local_now() -> datetime:
    """Now, as an aware datetime in the host's local timezone."""
    return datetime.now().astimezone()


def _as_local(moment: datetime | None) -> datetime:
    if moment is None:
        return local_now()
    if moment.tzinfo is None:
        return moment.astimezone()
    return moment.astimezone(local_now().tzinfo)


def current_day(moment: datetime | None = None, hour: int | None = None) -> str:
    """The portal-day label (YYYY-MM-DD) that `moment` belongs to.

    Before the boundary hour we are still in the previous day, which is the
    whole point: 02:00 belongs to the day that started at 05:00 yesterday.
    """
    local = _as_local(moment)
    boundary = reset_hour() if hour is None else hour
    day = local.date()
    if local.hour < boundary:
        day -= timedelta(days=1)
    return day.isoformat()


def day_start(moment: datetime | None = None) -> datetime:
    """The instant the current portal day began, in local time."""
    local = _as_local(moment)
    day = date.fromisoformat(current_day(local))
    return datetime.combine(day, time(hour=reset_hour()), tzinfo=local.tzinfo)


def next_reset(moment: datetime | None = None) -> datetime:
    """The instant the current portal day ends and the budget comes back."""
    return day_start(moment) + timedelta(days=1)


def seconds_until_reset(moment: datetime | None = None) -> int:
    local = _as_local(moment)
    return max(0, int((next_reset(local) - local).total_seconds()))


def day_start_iso(moment: datetime | None = None) -> str:
    """The day boundary as a UTC ISO timestamp, for comparing against the
    `started_at` strings in the runs table (which are UTC ISO with an offset,
    so a plain string `>=` is a correct chronological comparison)."""
    from datetime import timezone

    return day_start(moment).astimezone(timezone.utc).isoformat(timespec="seconds")


def humanize_seconds(secs: int) -> str:
    """A duration in the shortest form that is still unambiguous. Lives here
    rather than in the web layer because the worker phrases durations too (in
    the idle reason it hands the dashboard) and must not import `main`."""
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    hours, minutes = divmod(secs // 60, 60)
    return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"

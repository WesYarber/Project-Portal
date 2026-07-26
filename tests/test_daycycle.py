"""The portal day boundary: everything daily rolls over at 05:00 local."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import daycycle, db


def local(y, m, d, hh, mm=0):
    """A local-timezone datetime, whatever the host's zone happens to be."""
    return datetime(y, m, d, hh, mm).astimezone()


# --- current_day -----------------------------------------------------------

def test_after_boundary_is_todays_day():
    assert daycycle.current_day(local(2026, 7, 21, 5, 0)) == "2026-07-21"
    assert daycycle.current_day(local(2026, 7, 21, 23, 59)) == "2026-07-21"


def test_before_boundary_still_belongs_to_yesterday():
    # The whole point: work at 02:00 counts against last night's budget.
    assert daycycle.current_day(local(2026, 7, 21, 0, 1)) == "2026-07-20"
    assert daycycle.current_day(local(2026, 7, 21, 4, 59)) == "2026-07-20"


def test_boundary_hour_is_exclusive_at_the_bottom():
    assert daycycle.current_day(local(2026, 7, 21, 4, 59)) != daycycle.current_day(
        local(2026, 7, 21, 5, 0)
    )


def test_naive_datetime_is_treated_as_local():
    naive = datetime(2026, 7, 21, 3, 0)
    assert daycycle.current_day(naive) == "2026-07-20"


def test_utc_datetime_is_converted_before_bucketing():
    aware = datetime(2026, 7, 21, 3, 0, tzinfo=timezone.utc)
    expected = daycycle.current_day(aware.astimezone())
    assert daycycle.current_day(aware) == expected


# --- the configured hour ---------------------------------------------------

def test_reset_hour_defaults_to_five():
    assert daycycle.reset_hour() == 5


@pytest.mark.parametrize("bad", ["", "abc", "24", "-1", "5.5"])
def test_garbage_reset_hour_falls_back_to_default(bad):
    # A broken setting must never take the run budget down with it.
    db.set_setting("day_reset_hour", bad)
    assert daycycle.reset_hour() == daycycle.DEFAULT_RESET_HOUR


def test_reset_hour_is_configurable():
    db.set_setting("day_reset_hour", "0")
    assert daycycle.reset_hour() == 0
    # At hour 0 the boundary is midnight again, so nothing shifts back a day.
    assert daycycle.current_day(local(2026, 7, 21, 2, 0)) == "2026-07-21"


# --- day_start / next_reset ------------------------------------------------

def test_day_start_is_the_boundary_of_the_current_day():
    start = daycycle.day_start(local(2026, 7, 21, 14, 0))
    assert (start.hour, start.minute) == (5, 0)
    assert start.date().isoformat() == "2026-07-21"


def test_day_start_before_boundary_points_at_yesterday_morning():
    start = daycycle.day_start(local(2026, 7, 21, 2, 0))
    assert start.date().isoformat() == "2026-07-20"
    assert start.hour == 5


def test_next_reset_is_exactly_one_day_after_the_start():
    moment = local(2026, 7, 21, 14, 0)
    assert daycycle.next_reset(moment) - daycycle.day_start(moment) == timedelta(days=1)


def test_seconds_until_reset_counts_down_to_the_boundary():
    assert daycycle.seconds_until_reset(local(2026, 7, 21, 4, 0)) == 3600
    assert daycycle.seconds_until_reset(local(2026, 7, 21, 6, 0)) == 23 * 3600


def test_day_start_iso_is_utc_and_comparable_to_run_timestamps():
    iso = daycycle.day_start_iso(local(2026, 7, 21, 14, 0))
    parsed = datetime.fromisoformat(iso)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    # String ordering has to match chronological ordering, because that is how
    # the runs query compares it.
    assert iso < db.now()


# --- what the boundary actually governs ------------------------------------

def _run_at(moment: datetime, project_id=None) -> int:
    run_id = db.create_run(project_id, "build", "opus")
    conn = db.get_conn()
    conn.execute(
        "UPDATE runs SET started_at = ? WHERE id = ?",
        (moment.astimezone(timezone.utc).isoformat(timespec="seconds"), run_id),
    )
    conn.commit()
    return run_id


def test_runs_before_the_boundary_count_against_the_previous_day():
    now = daycycle.local_now()
    boundary = daycycle.day_start(now)
    _run_at(boundary + timedelta(minutes=5))  # this portal day
    _run_at(boundary - timedelta(minutes=5))  # the one before it
    assert db.count_runs_today() == 1


def test_runs_today_by_project_uses_the_same_boundary():
    project = db.create_project("Widget")
    boundary = daycycle.day_start()
    _run_at(boundary + timedelta(hours=1), project["id"])
    _run_at(boundary - timedelta(hours=1), project["id"])
    assert db.runs_today_by_project() == {project["id"]: 1}


def test_bonus_runs_expire_at_the_boundary_not_at_midnight():
    db.grant_bonus_runs(3)
    assert db.bonus_runs_today() == 3
    # A bonus granted for the calendar date is stale if that date is not the
    # current portal day.
    db.set_setting("bonus_runs_date", "2020-01-01")
    assert db.bonus_runs_today() == 0


def test_bonus_is_stamped_with_the_portal_day():
    db.grant_bonus_runs(2)
    assert db.get_setting("bonus_runs_date") == daycycle.current_day()

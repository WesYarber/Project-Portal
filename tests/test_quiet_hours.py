"""Quiet hours (app/quiet.py): the portal does not work through the night.

Wes, 2026-08-07:

  "This project ran a ton overnight without me meaning for it to and used up a
  ton of my weekly limit. Please help tame that."

The runs table backed him up - 62 runs between 18:00 and 09:00, at an 85% duty
cycle, which is exactly what the saturation guard is designed to allow. Nothing
in `pacing.py` could have stopped it, because every guard in there answers "how
much allowance is left" and this needed an answer to "is he awake".

What these pin, in the order they can go wrong quietly:

- The wrap across midnight. A 23 -> 7 window is a union of two ranges, and
  reading it as a plain `start <= hour < end` makes it always false, which
  turns the whole feature off without failing anywhere visible.
- The half-open end, which is what makes the resume time the truth.
- The zone. This server runs on UTC; reading these hours in the host's zone
  would hold runs across his evening instead of across his night, and would do
  it silently.
- Off means off. Equal hours are the switch, and a bad value falls back rather
  than raising - a guard must never be able to stop the worker deciding.
- The tick actually consults it, and `idle_reason` says the same thing the tick
  decided. Those two drifting apart is the portal lying about itself.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import db, quiet, settings_form

CHICAGO = ZoneInfo("America/Chicago")


def _at(hour: int, minute: int = 0, day: int = 7) -> datetime:
    """A moment given in Wes's own zone, converted to the UTC the code sees."""
    return datetime(2026, 8, day, hour, minute, tzinfo=CHICAGO).astimezone(timezone.utc)


# --- the window and its wrap ------------------------------------------------

def test_the_default_window_is_the_night_he_is_asleep(temp_data_dir):
    assert quiet.window() == (23, 7)


@pytest.mark.parametrize("hour", [23, 0, 3, 6])
def test_the_night_hours_are_quiet(temp_data_dir, hour):
    assert quiet.is_quiet(_at(hour)) is True


@pytest.mark.parametrize("hour", [7, 9, 12, 17, 22])
def test_the_waking_hours_are_not(temp_data_dir, hour):
    assert quiet.is_quiet(_at(hour)) is False


def test_a_window_that_does_not_wrap_midnight_is_a_plain_range(temp_data_dir):
    """1 -> 5 never crosses midnight, so the union reading would be wrong the
    other way round: it would call 06:00 and 23:00 quiet."""
    db.set_setting(quiet.START_SETTING, "1")
    db.set_setting(quiet.END_SETTING, "5")
    assert quiet.is_quiet(_at(2)) is True
    assert quiet.is_quiet(_at(6)) is False
    assert quiet.is_quiet(_at(23)) is False


def test_the_end_hour_is_already_working(temp_data_dir):
    """Half-open at the end. If 07:00 were still quiet, `resumes_at` would name
    an hour at which the portal is in fact still holding."""
    assert quiet.is_quiet(_at(6, 59)) is True
    assert quiet.is_quiet(_at(7, 0)) is False


# --- the zone ---------------------------------------------------------------

def test_the_hours_are_read_in_his_zone_not_the_host_s(temp_data_dir):
    """The regression this whole setting exists for.

    03:00 UTC is 22:00 in Chicago - his evening, and NOT quiet. 06:00 UTC is
    01:00 there, and IS. A host-zone reading inverts both, and does it without
    an error anywhere.
    """
    assert quiet.is_quiet(datetime(2026, 8, 7, 3, 0, tzinfo=timezone.utc)) is False
    assert quiet.is_quiet(datetime(2026, 8, 7, 6, 0, tzinfo=timezone.utc)) is True


@pytest.fixture
def host_in_the_pacific(monkeypatch):
    """Move the *host's* zone, which is the only way to see the bug below.

    This server runs on UTC, so on it `naive.replace(tzinfo=utc)` and
    `naive.astimezone()` give the same answer and a test written here cannot
    tell them apart - which is exactly how a host-local reading would ship
    unnoticed and then behave differently on somebody else's install. The
    mutation sweep found this: it was the one escape of twenty-one.
    """
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_a_naive_moment_is_read_as_utc(temp_data_dir, host_in_the_pacific):
    """Every timestamp in this database is UTC, so a caller handing one over
    without a tzinfo means UTC - not the host's local time.

    06:00 UTC is 01:00 Central, inside quiet hours. Read as host-local it would
    be 06:00 Pacific, which is 08:00 Central and awake - so the two readings
    give opposite answers here and identical ones on the box this ships to.
    """
    assert quiet.is_quiet(datetime(2026, 8, 7, 6, 0)) is True


def test_the_host_zone_never_enters_into_it(temp_data_dir, host_in_the_pacific):
    """The same for an aware timestamp: moving the host must not move the
    window, because the window is anchored to where he lives."""
    assert quiet.is_quiet(datetime(2026, 8, 7, 6, 0, tzinfo=timezone.utc)) is True
    assert quiet.is_quiet(datetime(2026, 8, 7, 3, 0, tzinfo=timezone.utc)) is False


def test_an_unknown_zone_falls_back_rather_than_raising(temp_data_dir):
    db.set_setting(quiet.ZONE_SETTING, "America/Arkansas")
    assert quiet.is_quiet(_at(2)) is True
    assert str(quiet.zone()) == quiet.DEFAULT_ZONE


def test_the_zone_dropdown_always_contains_what_is_configured(temp_data_dir):
    """A zone set by hand must not vanish from the list meant to show it -
    which would silently reset it to the default on the next save."""
    db.set_setting(quiet.ZONE_SETTING, "Europe/Berlin")
    choices = quiet.zone_choices()
    assert "Europe/Berlin" in choices
    assert quiet.DEFAULT_ZONE in choices


# --- turning it off, and bad values -----------------------------------------

def test_equal_hours_turn_it_off(temp_data_dir):
    db.set_setting(quiet.START_SETTING, "7")
    db.set_setting(quiet.END_SETTING, "7")
    assert quiet.window() is None
    assert quiet.is_quiet(_at(3)) is False
    assert quiet.quiet_hold(_at(3)) is None


@pytest.mark.parametrize("bad", ["", "  ", "abc", "99", "-1"])
def test_an_unusable_hour_falls_back_to_the_default(temp_data_dir, bad):
    db.set_setting(quiet.START_SETTING, bad)
    assert quiet.window() == (23, 7)


# --- the hold a caller renders ----------------------------------------------

def test_the_hold_says_when_runs_come_back(temp_data_dir):
    hold = quiet.quiet_hold(_at(2))
    assert hold is not None
    assert hold["resumes_at"] == "07:00"
    assert hold["resumes_in"] == 5 * 3600
    assert hold["zone"] == "America/Chicago"


def test_the_hold_before_midnight_resumes_the_next_morning(temp_data_dir):
    """The wrap again, this time in the arithmetic: at 23:30 the resume is
    07:00 *tomorrow*, and a same-day answer would report 7.5 hours ago."""
    hold = quiet.quiet_hold(_at(23, 30))
    assert hold is not None
    assert hold["resumes_at"] == "07:00"
    assert hold["resumes_in"] == 7 * 3600 + 1800


def test_there_is_no_hold_while_he_is_awake(temp_data_dir):
    assert quiet.quiet_hold(_at(14)) is None
    assert quiet.resumes_at(_at(14)) is None


def test_the_reason_names_the_hours_and_the_resume(temp_data_dir):
    reason = quiet.quiet_reason(quiet.quiet_hold(_at(2)))
    assert "23:00" in reason and "07:00" in reason
    assert "America/Chicago" in reason
    assert "run now still goes" in reason


def test_the_guard_fails_open_on_a_broken_clock(temp_data_dir, monkeypatch):
    """A bug in a guard may insert a gap it can explain; it may never idle the
    portal for good. Same rule as `pacing.saturation_hold`."""
    monkeypatch.setattr(quiet, "is_quiet", lambda now=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert quiet.quiet_hold(_at(2)) is None


# --- the settings page ------------------------------------------------------

def test_the_settings_form_owns_the_three_fields(temp_data_dir):
    cleaned = settings_form.apply(
        {
            quiet.START_SETTING: "22",
            quiet.END_SETTING: "8",
            quiet.ZONE_SETTING: "America/Denver",
        },
        f"{quiet.START_SETTING},{quiet.END_SETTING},{quiet.ZONE_SETTING}",
    )
    for key, value in cleaned.items():
        db.set_setting(key, value)
    assert quiet.window() == (22, 8)
    assert str(quiet.zone()) == "America/Denver"


def test_the_form_rejects_a_zone_that_is_not_real(temp_data_dir):
    """Validated on the way in, not just read defensively on the way out: a
    stored nonsense zone would keep working but would show as selected on a
    dropdown that cannot select it."""
    cleaned = settings_form.apply({quiet.ZONE_SETTING: "Mars/Olympus"}, quiet.ZONE_SETTING)
    assert cleaned[quiet.ZONE_SETTING] == quiet.DEFAULT_ZONE


def test_a_bad_quiet_hour_does_not_land_on_the_day_reset_hour(temp_data_dir):
    """`_hour` in settings_form pins daycycle's default (5). Reusing it here
    would silently make a mistyped quiet-hours start 05:00."""
    cleaned = settings_form.apply({quiet.START_SETTING: "nope"}, quiet.START_SETTING)
    assert cleaned[quiet.START_SETTING] == str(quiet.DEFAULT_START)


def test_the_settings_page_offers_the_fields(temp_data_dir):
    from starlette.testclient import TestClient

    from app import main

    body = TestClient(main.app).get("/settings").text
    for field in (quiet.START_SETTING, quiet.END_SETTING, quiet.ZONE_SETTING):
        assert f'name="{field}"' in body, field
        # Declared as owned by its form, or saving it is silently dropped.
        assert field in body.split('name="_fields"')[1].split(">")[0]


# --- the worker actually consults it ----------------------------------------

HOLDING = {
    "start": 23, "end": 7, "resumes_at": "07:00", "resumes_in": 3600,
    "zone": "America/Chicago",
}


def test_the_scheduler_holds_a_run_during_quiet_hours(temp_data_dir, monkeypatch):
    """The guard is only worth anything if `_start_one` asks it.

    Driven both ways in one test on purpose: with quiet hours on nothing
    spawns, and with them off the *same* project spawns immediately. A one-way
    assertion would pass just as happily if the project had been unschedulable
    for some unrelated reason.
    """
    import asyncio

    from app import worker

    project = db.create_project("A project", description="x", stage="active",
                                slug="a", build_approved=1)
    spawned = []
    monkeypatch.setattr(worker, "spawn_run", lambda p, t: spawned.append(p["id"]) or 1)

    monkeypatch.setattr(worker.quiet, "quiet_hold", lambda *a, **k: dict(HOLDING))
    assert asyncio.run(worker._start_one()) is False  # noqa: SLF001
    assert spawned == []

    monkeypatch.setattr(worker.quiet, "quiet_hold", lambda *a, **k: None)
    assert asyncio.run(worker._start_one()) is True  # noqa: SLF001
    assert spawned == [project["id"]]


def test_a_manual_run_goes_through_quiet_hours(temp_data_dir, monkeypatch):
    """'Run now' at 2am is a decision, not a guess. Same rule every other hold
    in the scheduler follows."""
    import asyncio

    from app import worker

    project = db.create_project("A project", description="x", stage="active",
                                slug="a", build_approved=1)
    started = []
    monkeypatch.setattr(worker, "spawn_run", lambda p, t: started.append(p["id"]) or 1)
    monkeypatch.setattr(worker.quiet, "quiet_hold", lambda *a, **k: dict(HOLDING))
    asyncio.run(worker.manual_queue.put(project["id"]))
    assert asyncio.run(worker._start_one()) is True  # noqa: SLF001
    assert started == [project["id"]]


def test_the_dashboard_says_quiet_hours_when_the_tick_is_holding_for_them(
    temp_data_dir, monkeypatch
):
    """`idle_reason` is computed from the same predicates the tick decides
    with, so it must name this one too - otherwise the board says "pacing the
    next run" all night while the real answer is "not until 7am"."""
    from app import worker

    db.create_project("A project", description="x", stage="active", slug="a",
                      build_approved=1)
    monkeypatch.setattr(worker.quiet, "quiet_hold", lambda *a, **k: dict(HOLDING))
    assert "quiet hours" in worker.idle_reason()


def test_a_spend_down_does_not_buy_a_night(temp_data_dir, monkeypatch):
    """Deliberately unlike the saturation guard, which exempts a spend-down.

    He said yes to spending a weekly window that was hours from resetting. He
    did not say yes to being run through at 3am, and this note is him saying
    so. If the two collide the spend-down resumes in the morning.
    """
    from app import pacing

    db.set_setting(pacing.ACTIVE_UNTIL,
                   datetime(2030, 1, 1, tzinfo=timezone.utc).isoformat())
    assert pacing.spending_down() is True
    assert quiet.quiet_hold(_at(2)) is not None

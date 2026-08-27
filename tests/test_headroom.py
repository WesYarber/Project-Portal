"""Keeping enough of the five-hour window free for the run about to start.

The bug: seven runs on 2026-08-06/07 did real work and were then killed by the
session limit, because the pacing guard was a fixed percentage of the meter and
nobody had ever measured what a run costs that meter. See app/headroom.py.

Nothing here touches the network. The pure functions take their pairs directly;
the rest goes through the database the way the real thing does.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import config, db, headroom, pacing

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)

# The weekly window's default here is not arbitrary: 5/7 of the week has
# elapsed in this fixture, and the front-loaded pace curve wants ~78% spent by
# then. Anything much lower reads as "miles ahead of pace" and switches on the
# ahead-of-pace boost, which holds the session window at its own 70% guard and
# quietly takes over every assertion below about the reserve.
ON_PACE_WEEKLY = 78.0


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def snapshot(five=2.0, seven=ON_PACE_WEEKLY, ok=True, stale=False, base=NOW):
    """A `limits.parse()`-shaped reading. Deliberately built without the
    `remaining_percent` key that the real parser adds: nothing in the hold path
    may depend on a field the tests' own fixtures happen to carry."""
    return {
        "ok": ok,
        "error": "",
        "stale": stale,
        "windows": [
            {
                "key": "five_hour", "label": "session", "percent": five,
                "resets_at": (base + timedelta(minutes=22)).isoformat(),
                "resets_in_sec": 22 * 60, "resets_in": "22m",
            },
            {
                "key": "seven_day", "label": "weekly", "percent": seven,
                "resets_at": (base + timedelta(days=2)).isoformat(),
                "resets_in_sec": 2 * 86400, "resets_in": "2d 0h",
            },
        ],
    }


def a_run(start, end, status="ok"):
    """One finished run with both meter readings stamped."""
    project = db.create_project("P", stage="active", build_approved=True)
    run_id = db.create_run(project["id"], "BUILD", "opus")
    if start is not None:
        db.record_run_session_meter(run_id, start=start)
    if end is not None:
        db.record_run_session_meter(run_id, end=end)
    if status != "running":
        db.finish_run(run_id, status)
    return run_id


# --------------------------------------------------------------------------
# What a pair of readings means
# --------------------------------------------------------------------------


def test_the_cost_of_a_run_is_how_far_the_meter_moved():
    assert headroom.observed_costs([(20.0, 33.5)]) == [13.5]


def test_a_pair_that_straddles_a_reset_is_dropped_not_counted_as_free():
    """The five-hour window resets under a long run, so its end reads lower
    than its start. That is missing data. Counting it - as a zero, or worse as
    a negative - would drag the reserve down using the very runs that are most
    at risk."""
    assert headroom.observed_costs([(95.0, 3.0)]) == []


def test_a_run_that_moved_the_meter_not_at_all_is_dropped():
    """Either the run died at the starting line or both stamps read the same
    cached snapshot. Neither says anything about what a working run spends."""
    assert headroom.observed_costs([(40.0, 40.0)]) == []


def test_the_usable_pairs_survive_the_unusable_ones_around_them():
    costs = headroom.observed_costs([(10.0, 22.0), (98.0, 4.0), (50.0, 50.0), (1.0, 4.5)])
    assert costs == [12.0, 3.5]


# --------------------------------------------------------------------------
# The statistic
# --------------------------------------------------------------------------


def test_the_percentile_interpolates_between_its_two_neighbors():
    assert headroom.percentile(list(range(1, 11)), 0.8) == pytest.approx(8.2)


def test_the_reserve_sits_high_in_the_costs_not_in_the_middle():
    """A median reserve is too small half the time by construction, and being
    too small is what kills a run - so this reads the configured PERCENTILE
    rather than passing its own, or lowering the constant would leave the whole
    file green.

    Six cheap runs and four expensive ones, which is roughly the real shape:
    the median says 2 points covers a run, and four runs in ten would then be
    started into a window they cannot finish in - exactly what happened to run
    841.
    """
    costs = [2.0] * 6 + [30.0] * 4
    for _ in range(6):
        a_run(10.0, 12.0)
    for _ in range(4):
        a_run(10.0, 40.0)
    measured = headroom.measured_reserve()
    assert headroom.percentile(costs, 0.5) == pytest.approx(2.0)
    assert measured == pytest.approx(headroom.percentile(costs, headroom.PERCENTILE))
    assert measured == pytest.approx(30.0)


def test_a_single_measurement_is_its_own_percentile():
    """statistics.quantiles raises on one data point; this is called with
    whatever history happens to exist."""
    assert headroom.percentile([7.0], 0.8) == 7.0


def test_no_measurements_at_all_is_none_rather_than_zero():
    assert headroom.percentile([], 0.8) is None


# --------------------------------------------------------------------------
# Reading it back out of the database
# --------------------------------------------------------------------------


def test_a_run_still_in_flight_is_not_a_zero_cost_run():
    """Its end reading has not happened yet. Reading the NULL as "cost nothing"
    would let a burst of in-flight runs talk the reserve down to nothing."""
    for _ in range(6):
        a_run(10.0, 25.0)
    a_run(80.0, None, status="running")
    pairs = db.recent_session_meter_pairs(40)
    assert len(pairs) == 6


def test_a_complete_pair_counts_even_before_its_row_is_finished():
    """The end is stamped when the agent process exits, which is a moment
    before `finish_run` marks the row. A status filter would throw away a
    finished measurement for being milliseconds early - the NULL is the rule,
    and it is the whole rule."""
    a_run(10.0, 25.0, status="running")
    assert db.recent_session_meter_pairs(40) == [(10.0, 25.0)]


def test_a_run_missing_one_reading_contributes_nothing():
    a_run(10.0, None)
    a_run(None, 25.0)
    assert db.recent_session_meter_pairs(40) == []


def test_the_measurement_takes_over_from_the_default_once_there_is_enough():
    for _ in range(headroom.MIN_SAMPLES):
        a_run(10.0, 30.0)
    assert headroom.measured_reserve() == pytest.approx(20.0)
    assert headroom.reserve() == pytest.approx(20.0)


def test_too_little_evidence_leaves_the_provisional_default_standing():
    for _ in range(headroom.MIN_SAMPLES - 1):
        a_run(10.0, 30.0)
    assert headroom.measured_reserve() is None
    assert headroom.reserve() == headroom.DEFAULT_RESERVE


def test_the_default_is_not_below_what_a_real_install_measures():
    """The seed a fresh install starts from must not be under the figure the
    same statistic produces from real runs, because the first `MIN_SAMPLES`
    runs are gated by the seed alone - and those are exactly the runs with no
    evidence to correct it.

    The costs below are the 9 measured runs on this install as of 2026-08-07,
    in points of the five-hour window. Their p80 is 21.8, and `DEFAULT_RESERVE`
    was 12.0 - so a fresh install used to reserve under half of what a run
    needs and killed its early runs part-way through. Written as a comparison
    rather than as `== 22.0` so that re-measuring is free: the invariant is
    that the guess errs high, not that it holds one particular value.
    """
    for cost in (11.0, 12.0, 17.0, 17.0, 17.0, 19.0, 19.0, 26.0, 35.0):
        a_run(10.0, 10.0 + cost)
    measured = headroom.measured_reserve()
    assert measured == pytest.approx(21.8)
    assert headroom.DEFAULT_RESERVE >= measured
    # ...and not so high that it would idle a fresh install by itself.
    assert headroom.DEFAULT_RESERVE < headroom.MAX_RESERVE


def test_a_wild_measurement_cannot_hold_every_scheduled_run_forever():
    """A run that somehow measured 79 points would push the hold below 21% of
    the window and leave it there. The cap is what stops a measurement bug from
    becoming an outage - and it is applied at `reserve()`, the value that
    actually gates a run, so removing it there is not covered by anything else.
    """
    for _ in range(headroom.MIN_SAMPLES):
        a_run(1.0, 80.0)
    assert headroom.reserve() == headroom.MAX_RESERVE
    # Still holds at a window the uncapped 79 would have held, and no longer
    # holds at one only the uncapped number could reach.
    assert pacing.scheduled_hold(snapshot(five=70.0), threshold=90.0) is not None
    assert pacing.scheduled_hold(snapshot(five=15.0), threshold=90.0) is None


def test_the_measurement_itself_is_reported_uncapped():
    """The cap is a safety rail on what the portal *does*, not a redaction of
    what it saw. A measured_reserve() that silently read MAX_RESERVE would hide
    the fact that the measurement had gone wrong."""
    for _ in range(headroom.MIN_SAMPLES):
        a_run(1.0, 80.0)
    assert headroom.measured_reserve() == pytest.approx(79.0)
    assert headroom.measured_reserve() > headroom.MAX_RESERVE


def test_only_the_recent_runs_are_measured():
    """Anthropic has doubled and reverted these limits inside a fortnight, so a
    long history describes a portal that no longer exists."""
    for _ in range(6):
        a_run(10.0, 11.0)      # older, cheap
    for _ in range(6):
        a_run(10.0, 30.0)      # newer, expensive
    assert headroom.measured_reserve(limit=6) == pytest.approx(20.0)


# --------------------------------------------------------------------------
# The setting
# --------------------------------------------------------------------------


def test_an_explicit_reserve_beats_the_measurement():
    for _ in range(headroom.MIN_SAMPLES):
        a_run(10.0, 30.0)
    db.set_setting(headroom.RESERVE_SETTING, "5")
    assert headroom.reserve() == pytest.approx(5.0)


def test_a_typo_in_the_field_falls_back_rather_than_holding_everything():
    db.set_setting(headroom.RESERVE_SETTING, "abc")
    assert headroom.configured_reserve() is None
    assert headroom.reserve() == headroom.DEFAULT_RESERVE


def test_an_out_of_range_reserve_is_ignored():
    db.set_setting(headroom.RESERVE_SETTING, "999")
    assert headroom.configured_reserve() is None
    assert headroom.reserve() == headroom.DEFAULT_RESERVE


def test_a_reserve_of_zero_is_a_real_answer_meaning_turn_it_off():
    """0 is not blank. Someone who has decided the reserve is doing more harm
    than good must be able to say so without the field reading as unset."""
    db.set_setting(headroom.RESERVE_SETTING, "0")
    assert headroom.configured_reserve() == 0.0
    assert headroom.reserve() == 0.0
    assert pacing.scheduled_hold(snapshot(five=89.0), threshold=90.0) is None


# --------------------------------------------------------------------------
# Reading the meter off a snapshot
# --------------------------------------------------------------------------


def test_the_session_percent_comes_off_the_five_hour_window():
    assert headroom.session_percent(snapshot(five=41.0, seven=88.0)) == 41.0


@pytest.mark.parametrize("kwargs", [{"ok": False}, {"stale": True}])
def test_a_reading_we_cannot_trust_is_none_not_a_plausible_zero(kwargs):
    """None and 0.0 mean opposite things here: one is "do not write this down",
    the other is "the window is empty"."""
    assert headroom.session_percent(snapshot(**kwargs)) is None


def test_a_snapshot_with_no_session_window_is_none():
    assert headroom.session_percent({"ok": True, "stale": False, "windows": []}) is None


# --------------------------------------------------------------------------
# The guard itself - the seven killed runs
# --------------------------------------------------------------------------


def test_a_window_too_full_for_a_run_holds_even_though_it_is_under_the_threshold():
    """Run 841's shape: the meter at 89%, the ordinary hold at 90% so it starts,
    and 11 points is not enough for a run that needs 12."""
    hold = pacing.scheduled_hold(snapshot(five=89.0), threshold=90.0)
    assert hold is not None
    assert hold["key"] == "five_hour"
    assert hold["reserved"] is True


def test_a_window_with_room_to_spare_still_does_not_hold():
    assert pacing.scheduled_hold(snapshot(five=40.0, seven=20.0), threshold=90.0) is None


def test_the_reserve_only_ever_tightens_the_hold():
    """A reserve of 2 would compute a floor of 98 - above the 90% threshold.
    The floor is applied as a minimum, never as a replacement, so the ordinary
    hold still fires at 90."""
    db.set_setting(headroom.RESERVE_SETTING, "2")
    hold = pacing.scheduled_hold(snapshot(five=91.0), threshold=90.0)
    assert hold is not None
    assert hold["hold_at"] == 90.0
    assert hold["reserved"] is False


def test_past_the_ordinary_threshold_is_a_plain_hold_not_a_reserved_one():
    """At 95% the window is over the wall by any rule; naming the reserve as the
    cause would misreport why nothing is running."""
    hold = pacing.scheduled_hold(snapshot(five=95.0), threshold=90.0)
    assert hold["reserved"] is False
    assert hold["guarded"] is False


def test_the_weekly_window_is_not_held_by_a_five_hour_measurement():
    """The reserve is measured in points of a five-hour window. 90% of a week
    still leaves many hours of runs, which is why none of the seven deaths were
    weekly - applying the number to the wrong denominator would idle the portal
    for two days at a stretch."""
    assert pacing.scheduled_hold(snapshot(five=5.0, seven=89.0), threshold=90.0) is None


def test_a_missing_reading_holds_nothing_at_all():
    """An outage at Anthropic's end must not be able to idle the portal, and
    that has to keep being true now that a second rule can hold the window."""
    assert pacing.scheduled_hold(snapshot(five=99.0, ok=False)) is None
    assert pacing.scheduled_hold(snapshot(five=99.0, stale=True)) is None


def test_the_burst_guard_still_wins_when_it_is_tighter():
    """Spend-down holds the session window at 70%, which is tighter than the
    88% the default reserve computes. The tightest rule has to win, whichever
    one it is."""
    # Anchored to the real clock, not to NOW. `pacing` reads this setting
    # against `datetime.now()`, so `NOW + 2h` was a time bomb: it stopped being
    # in the future at 14:00 UTC on 2026-08-07 and the burst guard then quietly
    # stopped applying, failing this test for good a few hours after it was
    # written. Everything else here can use the frozen NOW, because only this
    # one value is compared against the wall clock.
    db.set_setting(
        "spend_down_until",
        (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
    )
    hold = pacing.scheduled_hold(snapshot(five=75.0), threshold=90.0)
    assert hold is not None
    assert hold["hold_at"] == 70.0
    assert hold["guarded"] is True


def test_the_hold_reason_names_the_gap_and_when_it_clears():
    """The hold is the dashboard's answer to "why is nothing running", and a
    reason that only said "89%" would look like the threshold was wrong."""
    hold = pacing.scheduled_hold(snapshot(five=89.0), threshold=90.0)
    reason = pacing.hold_reason(hold)
    assert "11 points left" in reason
    # Derived, not written out: this asserts the reason quotes the reserve
    # actually in force, which is the point. Spelling the number here made it
    # fail the day DEFAULT_RESERVE was re-measured from 12 to 22, pointing at
    # the wording rather than at the constant that moved.
    assert f"{headroom.reserve():g} a run needs" in reason
    assert "22m" in reason
    assert "run now" in reason


# --------------------------------------------------------------------------
# Stamping, and never breaking a run to do it
# --------------------------------------------------------------------------


def test_stamping_writes_each_end_without_erasing_the_other():
    project = db.create_project("P", stage="active", build_approved=True)
    run_id = db.create_run(project["id"], "BUILD", "opus")
    db.record_run_session_meter(run_id, start=12.0)
    db.record_run_session_meter(run_id, end=30.0)
    row = db.get_run(run_id)
    assert (row["session_percent_start"], row["session_percent_end"]) == (12.0, 30.0)


def test_stamping_nothing_leaves_the_row_alone():
    project = db.create_project("P", stage="active", build_approved=True)
    run_id = db.create_run(project["id"], "BUILD", "opus")
    db.record_run_session_meter(run_id, start=12.0)
    db.record_run_session_meter(run_id)
    assert db.get_run(run_id)["session_percent_start"] == 12.0


def test_a_run_with_no_id_is_not_stamped(monkeypatch):
    """Ask, natural-language and self-review spawns pass run_id=None."""
    called = []
    monkeypatch.setattr(db, "record_run_session_meter", lambda *a, **k: called.append(a))
    headroom.stamp_start(None)
    headroom.stamp_end(None)
    assert called == []


def test_an_unreadable_meter_writes_nothing_rather_than_a_zero(monkeypatch):
    project = db.create_project("P", stage="active", build_approved=True)
    run_id = db.create_run(project["id"], "BUILD", "opus")
    monkeypatch.setattr(headroom, "session_percent", lambda *a, **k: None)
    headroom.stamp_start(run_id)
    assert db.get_run(run_id)["session_percent_start"] is None


def test_a_broken_database_cannot_kill_a_run(monkeypatch):
    """This is telemetry hanging off the side of a live agent. A locked
    database, or a column an un-migrated install does not have, must cost a log
    line rather than the run."""
    monkeypatch.setattr(headroom, "session_percent", lambda *a, **k: 40.0)

    def boom(*a, **k):
        raise db.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "record_run_session_meter", boom)
    headroom.stamp_start(1)  # must not raise
    headroom.stamp_end(1)


def test_a_broken_history_query_falls_back_to_the_default(monkeypatch):
    def boom(*a, **k):
        raise db.sqlite3.OperationalError("no such column")

    monkeypatch.setattr(db, "recent_session_meter_pairs", boom)
    assert headroom.measured_reserve() is None
    assert headroom.reserve() == headroom.DEFAULT_RESERVE
    assert headroom.sample_size() == 0


# --------------------------------------------------------------------------
# The knob on the Settings page
# --------------------------------------------------------------------------


def test_the_settings_page_offers_the_reserve_with_the_measured_number(client):
    for _ in range(headroom.MIN_SAMPLES):
        a_run(10.0, 30.0)
    body = client.get("/settings").text
    assert 'name="session_headroom_reserve"' in body
    # Blank means "measure it", so the measured number is the placeholder.
    assert 'placeholder="20"' in body
    assert f"{headroom.MIN_SAMPLES} measured so far" in body


def test_saving_the_reserve_sticks(client):
    client.post("/settings", data={"_fields": "session_headroom_reserve",
                                   "session_headroom_reserve": "7.5"})
    assert db.get_setting(headroom.RESERVE_SETTING) == "7.5"
    assert headroom.reserve() == pytest.approx(7.5)

"""Scheduling against the real Claude limits (app/pacing.py).

Two behaviours pulling opposite ways: stop scheduled runs short of a full
window, and spend a weekly window's leftovers before they evaporate. Nothing
here touches the network - every test hands `pacing` a snapshot in the shape
`limits.parse()` produces.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import config, db, limits, pacing, worker

NOW = datetime(2026, 7, 21, 22, 0, 0, tzinfo=timezone.utc)


def snapshot(five=2.0, seven=10.0, opus=None, seven_reset=None, ok=True, stale=False, base=NOW):
    # `base` is the moment the reset countdowns hang off. It defaults to the
    # fixed NOW for tests that also pass `now=NOW` into pacing, but a test that
    # stores the snapshot and reads it back through limits.cached() (which uses
    # the real clock) must pass base=datetime.now(...) so the windows have not
    # already reset relative to real time - a passed reset is treated as stale.
    windows = [
        {
            "key": "five_hour", "label": "session", "percent": five,
            "resets_at": (base + timedelta(hours=3)).isoformat(),
            "resets_in_sec": 3 * 3600, "resets_in": "3h 00m",
        },
        {
            "key": "seven_day", "label": "weekly", "percent": seven,
            "resets_at": (seven_reset or base + timedelta(days=2)).isoformat(),
            "resets_in_sec": 2 * 86400, "resets_in": "2d 0h",
        },
    ]
    if opus is not None:
        windows.append({
            "key": "seven_day_opus", "label": "weekly (Opus)", "percent": opus,
            "resets_at": (seven_reset or base + timedelta(days=2)).isoformat(),
            "resets_in_sec": 2 * 86400, "resets_in": "2d 0h",
        })
    return {"ok": ok, "error": "", "windows": windows, "stale": stale}


def meta_project():
    return db.create_project("Project Portal", slug=config.META_PROJECT_SLUG, stage="active", build_approved=True)


# --------------------------------------------------------------------------
# Holding scheduled runs short of the wall
# --------------------------------------------------------------------------


def test_quiet_windows_do_not_hold():
    assert pacing.scheduled_hold(snapshot(five=40.0, seven=20.0)) is None


def test_full_window_holds_and_names_itself():
    hold = pacing.scheduled_hold(snapshot(five=94.0))
    assert hold is not None and hold["label"] == "session"
    reason = pacing.hold_reason(hold)
    assert "session" in reason and "94%" in reason and "3h 00m" in reason


def test_fullest_window_wins():
    """The window a run would actually hit first is the one worth reporting."""
    hold = pacing.scheduled_hold(snapshot(five=91.0, seven=97.0))
    assert hold["label"] == "weekly"


def test_threshold_is_configurable():
    db.set_setting(pacing.HOLD_SETTING, "50")
    assert pacing.scheduled_hold(snapshot(five=60.0)) is not None
    db.set_setting(pacing.HOLD_SETTING, "99")
    assert pacing.scheduled_hold(snapshot(five=60.0)) is None


@pytest.mark.parametrize("value", ["", "junk", "0", "500", "-3"])
def test_nonsense_threshold_falls_back_to_the_default(value):
    db.set_setting(pacing.HOLD_SETTING, value)
    assert pacing.hold_percent() == pacing.DEFAULT_HOLD_PERCENT


def test_a_missing_or_stale_reading_never_holds():
    """An outage at Anthropic's end must not be able to idle the portal - the
    worst case has to degrade to the old behaviour (run, and back off if it
    fails), not to silence."""
    assert pacing.scheduled_hold({"ok": False, "error": "no snapshot", "windows": []}) is None
    assert pacing.scheduled_hold(snapshot(five=99.0, stale=True)) is None


def test_hold_reads_the_cache_when_given_nothing():
    db.set_setting(limits.CACHE_KEY, json.dumps({
        "ok": True,
        "windows": snapshot(five=98.0, base=datetime.now(timezone.utc))["windows"],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }))
    assert pacing.scheduled_hold() is not None


# --------------------------------------------------------------------------
# Spotting a weekly window about to expire unspent
# --------------------------------------------------------------------------


def test_weekly_window_near_reset_with_headroom_is_a_candidate():
    snap = snapshot(seven=30.0, seven_reset=NOW + timedelta(hours=5))
    candidate = pacing.spend_down_candidate(snap, now=NOW)
    assert candidate is not None
    assert candidate["key"] == "seven_day"
    assert candidate["unused"] == 70.0


def test_a_weekly_window_days_away_is_not_a_candidate():
    assert pacing.spend_down_candidate(snapshot(seven=30.0), now=NOW) is None


def test_a_nearly_full_weekly_window_has_nothing_to_spend():
    snap = snapshot(seven=90.0, seven_reset=NOW + timedelta(hours=5))
    assert pacing.spend_down_candidate(snap, now=NOW) is None


def test_the_session_window_is_never_a_spend_down_candidate():
    """It resets six times a day, so nothing is ever really wasted on it."""
    snap = snapshot(five=5.0, seven=95.0, seven_reset=NOW + timedelta(hours=5))
    assert pacing.spend_down_candidate(snap, now=NOW) is None


def test_the_tighter_weekly_window_is_the_one_offered():
    """Both weeklies qualify; the Opus one has less to give and is what will
    actually stop the burst, so it sets the size of the offer."""
    snap = snapshot(seven=20.0, opus=60.0, seven_reset=NOW + timedelta(hours=4))
    candidate = pacing.spend_down_candidate(snap, now=NOW)
    assert candidate["key"] == "seven_day_opus"
    assert candidate["unused"] == 40.0


def test_a_window_that_has_already_reset_is_not_a_candidate():
    snap = snapshot(seven=30.0, seven_reset=NOW - timedelta(minutes=1))
    assert pacing.spend_down_candidate(snap, now=NOW) is None


def test_a_stale_reading_makes_no_offer():
    snap = snapshot(seven=30.0, seven_reset=NOW + timedelta(hours=5), stale=True)
    assert pacing.spend_down_candidate(snap, now=NOW) is None


# --------------------------------------------------------------------------
# The offer itself
# --------------------------------------------------------------------------


def due(hours=5, seven=30.0):
    return snapshot(seven=seven, seven_reset=NOW + timedelta(hours=hours))


def test_offer_becomes_a_real_question_on_the_meta_project():
    project = meta_project()
    candidate = pacing.should_offer(due(), now=NOW)
    question = pacing.create_offer_question(candidate)
    assert question["project_id"] == project["id"]
    assert "70%" in question["question"]
    assert db.count_open_questions(project["id"]) == 1


def test_the_offer_is_made_once_per_window():
    meta_project()
    candidate = pacing.should_offer(due(), now=NOW)
    pacing.create_offer_question(candidate)
    assert pacing.should_offer(due(), now=NOW) is None


def test_the_next_week_gets_its_own_offer():
    meta_project()
    pacing.create_offer_question(pacing.should_offer(due(), now=NOW))
    later = NOW + timedelta(days=7)
    snap = snapshot(seven=30.0, seven_reset=later + timedelta(hours=5))
    assert pacing.should_offer(snap, now=later) is not None


def test_no_offer_without_a_meta_project():
    """A portal whose own project row has been deleted should decline quietly
    rather than raise inside the worker tick."""
    assert pacing.create_offer_question(pacing.should_offer(due(), now=NOW)) is None


# --------------------------------------------------------------------------
# Reading the answer back
# --------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["yes", "Yes please", "yeah go for it", "sure", "do it", "spend it"])
def test_affirmative(answer):
    assert pacing.is_affirmative(answer) is True


@pytest.mark.parametrize("answer", ["no", "nope", "not now", "leave it", "don't spend it", "", "maybe"])
def test_not_affirmative(answer):
    assert pacing.is_affirmative(answer) is False


def test_saying_yes_opens_the_window_until_the_reset():
    meta_project()
    question = pacing.create_offer_question(pacing.should_offer(due(), now=NOW))
    db.answer_question_and_resume(question["id"], "yes, go for it")

    assert pacing.settle_offer(now=NOW) is True
    until = pacing.active_until(now=NOW)
    assert until == NOW + timedelta(hours=5)
    assert pacing.spending_down(now=NOW) is True


def test_saying_no_changes_nothing():
    meta_project()
    question = pacing.create_offer_question(pacing.should_offer(due(), now=NOW))
    db.answer_question_and_resume(question["id"], "no, leave it")
    assert pacing.settle_offer(now=NOW) is False
    assert pacing.spending_down(now=NOW) is False


def test_an_unanswered_offer_settles_nothing():
    meta_project()
    pacing.create_offer_question(pacing.should_offer(due(), now=NOW))
    assert pacing.settle_offer(now=NOW) is None
    assert pacing.spending_down(now=NOW) is False


def test_settling_twice_does_not_reopen_the_window():
    meta_project()
    question = pacing.create_offer_question(pacing.should_offer(due(), now=NOW))
    db.answer_question_and_resume(question["id"], "yes")
    pacing.settle_offer(now=NOW)
    assert pacing.settle_offer(now=NOW) is None


def test_a_dismissed_offer_is_a_no():
    meta_project()
    question = pacing.create_offer_question(pacing.should_offer(due(), now=NOW))
    db.dismiss_question(question["id"])
    assert pacing.settle_offer(now=NOW) is False


def test_answering_yes_after_the_reset_opens_nothing():
    """The headroom he was offered no longer exists; opening a window that has
    already ended would either do nothing or, worse, never end."""
    meta_project()
    question = pacing.create_offer_question(pacing.should_offer(due(hours=1), now=NOW))
    db.answer_question_and_resume(question["id"], "yes")
    assert pacing.settle_offer(now=NOW + timedelta(hours=2)) is False
    assert pacing.spending_down(now=NOW + timedelta(hours=2)) is False


def test_no_offer_while_one_is_already_running():
    meta_project()
    pacing.start(NOW + timedelta(hours=4))
    assert pacing.should_offer(due(), now=NOW) is None


# --------------------------------------------------------------------------
# What the spend-down actually changes
# --------------------------------------------------------------------------


def test_budget_and_pacing_are_lifted_while_spending_down():
    assert pacing.budget_applies() is True
    assert pacing.interval_min(10) == 10
    pacing.start(datetime.now(timezone.utc) + timedelta(hours=3))
    assert pacing.budget_applies() is False
    assert pacing.interval_min(10) == pacing.SPEND_DOWN_INTERVAL_MIN
    # Never *lengthens* an already-short interval.
    assert pacing.interval_min(1) == 1


def test_the_window_expires_by_itself_and_says_so():
    pacing.start(datetime.now(timezone.utc) - timedelta(minutes=1))
    assert pacing.spending_down() is False
    assert db.get_setting(pacing.ACTIVE_UNTIL) == ""
    entries = db.list_journal(limit=10)
    assert any("Spend-down window over" in e["content_md"] for e in entries)


def test_status_line_only_speaks_while_the_window_is_open():
    assert pacing.status_line() == ""
    pacing.start(datetime.now(timezone.utc) + timedelta(hours=2))
    assert "spending down" in pacing.status_line()


# --------------------------------------------------------------------------
# The worker honouring all of it
# --------------------------------------------------------------------------


def store(snap):
    snap = dict(snap, fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    db.set_setting(limits.CACHE_KEY, json.dumps(snap))


def test_worker_holds_a_scheduled_run_when_a_window_is_full():
    db.create_project("Something", stage="active")
    store(snapshot(five=97.0, base=datetime.now(timezone.utc)))
    assert asyncio.run(worker._start_one()) is False  # noqa: SLF001
    assert db.count_runs_today() == 0


def test_a_manual_run_ignores_the_hold(monkeypatch):
    """Wes pressing 'run now' is a decision about his own allowance, not a
    guess the portal should overrule."""
    project = db.create_project("Something", stage="active")
    store(snapshot(five=97.0))
    started = []
    monkeypatch.setattr(worker, "spawn_run", lambda p, t: started.append((p["id"], t)) or 1)
    asyncio.run(worker.manual_queue.put(project["id"]))
    assert asyncio.run(worker._start_one()) is True  # noqa: SLF001
    assert started == [(project["id"], "triage")]


def test_the_hold_is_the_dashboard_reason():
    db.create_project("Something", stage="active")
    store(snapshot(five=97.0, base=datetime.now(timezone.utc)))
    assert "holding scheduled runs" in worker.idle_reason()


def test_a_spent_budget_stops_being_a_reason_while_spending_down(monkeypatch):
    db.create_project("Something", stage="active")
    db.set_setting("max_runs_per_day", "0")
    assert "run budget is spent" in worker.idle_reason()
    pacing.start(datetime.now(timezone.utc) + timedelta(hours=3))
    assert "run budget is spent" not in worker.idle_reason()


def test_worker_starts_a_run_past_the_daily_budget_while_spending_down(monkeypatch):
    project = db.create_project("Something", stage="active")
    db.set_setting("max_runs_per_day", "0")
    started = []
    monkeypatch.setattr(worker, "spawn_run", lambda p, t: started.append(p["id"]) or 1)
    assert asyncio.run(worker._start_one()) is False  # noqa: SLF001
    pacing.start(datetime.now(timezone.utc) + timedelta(hours=3))
    assert asyncio.run(worker._start_one()) is True  # noqa: SLF001
    assert started == [project["id"]]


def test_tick_makes_the_offer_and_notifies(monkeypatch):
    """End to end through the worker: a weekly window five hours from resetting
    with 70% unused becomes an open question and a Telegram message."""
    meta_project()
    real_now = datetime.now(timezone.utc)
    store(snapshot(seven=30.0, seven_reset=real_now + timedelta(hours=5), base=real_now))
    sent = {}

    async def fake_notify(title, message, **kw):
        sent.update({"title": title, "message": message, **kw})

    monkeypatch.setattr(worker.notify, "notify", fake_notify)
    asyncio.run(worker._maybe_spend_down())  # noqa: SLF001

    assert "70%" in sent["message"]
    assert sent["question_id"] is not None
    assert len(db.open_questions()) == 1
    # And not again on the next tick.
    sent.clear()
    asyncio.run(worker._maybe_spend_down())  # noqa: SLF001
    assert sent == {}


def test_a_broken_offer_never_breaks_the_tick(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.setattr(pacing, "settle_offer", boom)
    asyncio.run(worker._maybe_spend_down())  # noqa: SLF001


def test_the_hold_threshold_is_settable_from_the_settings_form():
    """The knob is only real if the agent tab can write it - the form drops any
    key it doesn't declare, so a field added to the template alone silently
    does nothing."""
    from app import settings_form

    assert settings_form.apply({"limit_hold_percent": "75"}, "limit_hold_percent") == {
        "limit_hold_percent": "75"
    }
    # Out of range falls back rather than writing a threshold that never fires.
    assert settings_form.apply({"limit_hold_percent": "0"}, "limit_hold_percent") == {
        "limit_hold_percent": "90"
    }


# --------------------------------------------------------------------------
# The jitter that asked Wes the same question twelve times
# --------------------------------------------------------------------------


def test_a_one_second_reset_jitter_is_the_same_window():
    """The endpoint reports the same weekly reset as 21:00:00 on one poll and
    20:59:59 on the next. On 2026-07-23 the exact-string comparison turned
    that into twelve copies of the spend-down question in one afternoon."""
    meta_project()
    reset = NOW + timedelta(hours=5)
    pacing.create_offer_question(pacing.should_offer(due(), now=NOW))
    jittered = snapshot(seven=30.0, seven_reset=reset - timedelta(seconds=1))
    assert pacing.should_offer(jittered, now=NOW) is None
    jittered = snapshot(seven=30.0, seven_reset=reset + timedelta(seconds=1))
    assert pacing.should_offer(jittered, now=NOW) is None


def test_an_open_offer_question_suppresses_any_reoffer():
    """Whatever the reset stamps say, an unanswered spend-down question means
    there is nothing new to ask."""
    meta_project()
    pacing.create_offer_question(pacing.should_offer(due(), now=NOW))
    # Simulate the stored stamp going bad entirely - the open question alone
    # must still hold the line.
    db.set_setting(pacing.OFFERED_FOR, "garbage")
    assert pacing.should_offer(due(), now=NOW) is None


def test_a_reset_a_week_apart_is_a_different_window():
    assert pacing._same_window(  # noqa: SLF001
        (NOW + timedelta(hours=5)).isoformat(), (NOW + timedelta(days=7)).isoformat()
    ) is False


def test_a_percent_in_the_yes_sets_the_session_guard():
    """'Yes, but don't go over my 5hr limit past 70%' is an instruction, not
    colour - the number becomes the burst session guard."""
    meta_project()
    question = pacing.create_offer_question(pacing.should_offer(due(), now=NOW))
    db.answer_question_and_resume(question["id"], "Yes, spend it down. Don't go past 65%, though.")
    assert pacing.settle_offer(now=NOW) is True
    assert pacing.session_guard() == 65.0


def test_a_silly_percent_in_the_yes_is_ignored():
    meta_project()
    question = pacing.create_offer_question(pacing.should_offer(due(), now=NOW))
    db.answer_question_and_resume(question["id"], "yes, 5% is fine")
    assert pacing.settle_offer(now=NOW) is True
    assert pacing.session_guard() == pacing.DEFAULT_SESSION_GUARD


# --------------------------------------------------------------------------
# Spending ahead of pace
# --------------------------------------------------------------------------


def paced(elapsed_days, seven, five=2.0, opus=None, base=NOW):
    """A snapshot whose weekly window has `elapsed_days` of the week behind it."""
    resets_in = int((7 - elapsed_days) * 86400)
    snap = snapshot(five=five, seven=seven, opus=opus, base=base)
    for w in snap["windows"]:
        if w["key"].startswith("seven_day"):
            w["resets_in_sec"] = resets_in
            w["resets_at"] = (base + timedelta(seconds=resets_in)).isoformat()
    return snap


def test_on_the_front_loaded_pace_means_no_boost():
    # 50% of the week gone; the front-loaded target there is ~59%, so 58% spent
    # is on pace and inside the dead band - no boost.
    assert pacing.boost_factor(paced(3.5, seven=58.0), now=NOW) == 1.0


def test_early_week_headroom_front_loads():
    # Tuesday with nothing spent: the front-loaded target is already ~23%, so
    # the portal is meaningfully behind pace and boosts - the whole point of
    # front-loading is to spend earlier rather than banking it for Sunday.
    factor = pacing.boost_factor(paced(1.0, seven=0.0), now=NOW)
    assert 1.5 < factor < 2.1


def test_front_load_boosts_where_linear_would_not():
    """The crux: 45% spent at mid-week is exactly on the *linear* pace (no
    boost), but behind the front-loaded curve (boost). Flipping the gamma
    setting to 1 turns front-loading off and restores the linear verdict."""
    snap = paced(3.5, seven=45.0)
    db.set_setting(pacing.FRONT_LOAD_SETTING, "1")
    assert pacing.boost_factor(snap, now=NOW) == 1.0
    db.set_setting(pacing.FRONT_LOAD_SETTING, "0.75")
    assert pacing.boost_factor(snap, now=NOW) > 1.0


def test_late_week_headroom_boosts_hard_but_is_capped():
    # Saturday with 5% spent: surplus ~81, which would be x5.6 uncapped.
    assert pacing.boost_factor(paced(6.5, seven=5.0), now=NOW) == pacing.BOOST_MAX


def test_the_tighter_weekly_window_sets_the_pace():
    """The all-model window is way ahead of pace but Opus is on pace - a boost
    would push Opus over, so there is none."""
    snap = paced(5.0, seven=10.0, opus=70.0)
    assert pacing.boost_factor(snap, now=NOW) == 1.0


def test_a_stale_reading_never_boosts():
    snap = paced(6.5, seven=5.0)
    snap["stale"] = True
    assert pacing.boost_factor(snap, now=NOW) == 1.0


def test_spending_down_supersedes_the_boost():
    pacing.start(datetime.now(timezone.utc) + timedelta(hours=3))
    assert pacing.boost_factor(paced(6.5, seven=5.0)) == 1.0


def test_boost_quickens_interval_and_raises_budget():
    store(paced(6.0, seven=10.0, base=datetime.now(timezone.utc)))  # surplus ~75.7 -> capped at x4
    assert pacing.interval_min(10) == 2
    assert pacing.run_budget(20) == 80
    # And never lengthens an interval already shorter than the boosted one.
    assert pacing.interval_min(1) == 1


def test_no_snapshot_means_ordinary_pacing():
    assert pacing.interval_min(10) == 10
    assert pacing.run_budget(20) == 20


def test_the_boost_holds_the_session_window_at_the_guard():
    """The five-hour guard is what spreads the burst out: at 75% the session
    window pauses the burst even though the ordinary hold is 90."""
    hold = pacing.scheduled_hold(paced(6.0, seven=10.0, five=75.0))
    assert hold is not None and hold["key"] == "five_hour"
    assert hold["guarded"] is True
    reason = pacing.hold_reason(hold)
    assert "spreads" in reason and "70%" in reason
    # Under the guard the burst runs.
    assert pacing.scheduled_hold(paced(6.0, seven=10.0, five=65.0)) is None
    # And with no boost in play the ordinary threshold still rules - 60% spent
    # at mid-week is on the front-loaded pace, so no burst and the 5h window's
    # 75% sits under the ordinary 90 hold.
    assert pacing.scheduled_hold(paced(3.5, seven=60.0, five=75.0)) is None


def test_the_guard_is_configurable():
    db.set_setting(pacing.SESSION_GUARD_SETTING, "50")
    assert pacing.scheduled_hold(paced(6.0, seven=10.0, five=60.0)) is not None
    db.set_setting(pacing.SESSION_GUARD_SETTING, "80")
    assert pacing.scheduled_hold(paced(6.0, seven=10.0, five=60.0)) is None


def test_the_guard_applies_during_a_spend_down_too():
    pacing.start(datetime.now(timezone.utc) + timedelta(hours=3))
    assert pacing.scheduled_hold(snapshot(five=75.0, seven=95.0)) is not None
    assert pacing.scheduled_hold(snapshot(five=65.0, seven=85.0)) is None


def test_status_line_speaks_while_ahead_of_pace():
    store(paced(6.0, seven=10.0, base=datetime.now(timezone.utc)))
    line = pacing.status_line()
    assert "ahead of pace" in line and "70%" in line


def test_the_session_guard_is_settable_from_the_settings_form():
    from app import settings_form

    assert settings_form.apply(
        {"spend_down_session_hold": "65"}, "spend_down_session_hold"
    ) == {"spend_down_session_hold": "65"}
    assert settings_form.apply(
        {"spend_down_session_hold": "0"}, "spend_down_session_hold"
    ) == {"spend_down_session_hold": "70"}


def test_pace_target_curve_is_front_loaded():
    # Concave: the target sits above the linear line everywhere inside (0, 1).
    assert pacing.pace_target(0.5, gamma=0.75) > 50.0
    assert pacing.pace_target(0.25, gamma=0.75) > 25.0
    # Endpoints are pinned whatever the gamma.
    assert pacing.pace_target(0.0, gamma=0.75) == 0.0
    assert pacing.pace_target(1.0, gamma=0.75) == 100.0
    # gamma 1 is exactly the linear line.
    assert pacing.pace_target(0.4, gamma=1.0) == 40.0
    # Clamped outside [0, 1] rather than extrapolating.
    assert pacing.pace_target(-0.5, gamma=0.75) == 0.0
    assert pacing.pace_target(1.5, gamma=0.75) == 100.0


def test_front_load_setting_reads_and_guards():
    assert pacing.front_load() == pacing.DEFAULT_FRONT_LOAD
    db.set_setting(pacing.FRONT_LOAD_SETTING, "0.5")
    assert pacing.front_load() == 0.5
    db.set_setting(pacing.FRONT_LOAD_SETTING, "1")
    assert pacing.front_load() == 1.0
    # A back-load (gamma > 1), zero, negative or junk all fall back to the
    # default rather than banking spend for a window that may reset early.
    for bad in ("0", "-0.2", "1.5", "abc", ""):
        db.set_setting(pacing.FRONT_LOAD_SETTING, bad)
        assert pacing.front_load() == pacing.DEFAULT_FRONT_LOAD


def test_the_front_load_is_settable_from_the_settings_form():
    from app import settings_form

    assert settings_form.apply(
        {"spend_front_load": "0.6"}, "spend_front_load"
    ) == {"spend_front_load": "0.6"}
    # Out of range or unparseable falls back to the default gamma.
    assert settings_form.apply(
        {"spend_front_load": "2"}, "spend_front_load"
    ) == {"spend_front_load": "0.75"}
    assert settings_form.apply(
        {"spend_front_load": "nope"}, "spend_front_load"
    ) == {"spend_front_load": "0.75"}


def test_status_line_names_the_front_loaded_target():
    store(paced(6.0, seven=10.0, base=datetime.now(timezone.utc)))
    line = pacing.status_line()
    assert "front-loaded target" in line


def test_a_yes_with_a_dont_condition_is_still_a_yes():
    """His literal 2026-07-23 answer - 'negatives always win' read it as a
    decline, which is half of why ten yeses never opened the window."""
    assert pacing.is_affirmative(
        "Yes, spend it down. Don't go over my 5hr limit by past 70%, though."
    ) is True


def test_a_stale_open_offer_is_retired_when_its_window_passes():
    meta_project()
    question = pacing.create_offer_question(pacing.should_offer(due(), now=NOW))
    later = NOW + timedelta(days=7)
    snap = snapshot(seven=30.0, seven_reset=later + timedelta(hours=5))
    assert pacing.should_offer(snap, now=later) is not None
    assert db.get_question(question["id"])["status"] == "dismissed"

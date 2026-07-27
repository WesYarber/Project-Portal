"""Learning the real reset cadence of the weekly window (app/cadence.py).

The endpoint's resets_at was seen lying - the weekly meter rolled over every
~72h while it claimed seven days - so the portal learns the true cadence from
the one signal that can't lie: utilization only falls at a reset. These tests
pin the detection, the averaging, the plausibility guards, and the integration
into pacing's ahead-of-pace boost. Nothing here touches the network.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import cadence, db, limits, pacing

NOW = datetime(2026, 7, 21, 22, 0, 0, tzinfo=timezone.utc)


def weekly_snapshot(percent, resets_in_sec=5 * 86400, key="seven_day", ok=True, stale=False):
    """A parsed-shape snapshot carrying one weekly window."""
    snap = {
        "ok": ok,
        "windows": [
            {
                "key": key,
                "label": "weekly",
                "percent": percent,
                "resets_at": (NOW + timedelta(seconds=resets_in_sec)).isoformat(),
                "resets_in_sec": resets_in_sec,
                "resets_in": "5d 0h",
            }
        ],
    }
    if stale:
        snap["stale"] = True
    return snap


def seed_resets(key, times, last_percent=40.0):
    """Write a learned-state row directly, for the pure-function tests."""
    db.set_setting(
        cadence.STATE_KEY,
        json.dumps(
            {
                key: {
                    "last_percent": last_percent,
                    "last_at": times[-1].isoformat(timespec="seconds") if times else "",
                    "resets": [t.isoformat(timespec="seconds") for t in times],
                }
            }
        ),
    )


# ---------------------------------------------------------------------------
# Detecting a reset from the readings
# ---------------------------------------------------------------------------


def test_utilization_drop_records_a_reset():
    cadence.record_reading(weekly_snapshot(50.0), now=NOW)
    cadence.record_reading(weekly_snapshot(5.0), now=NOW + timedelta(hours=1))
    state = json.loads(db.get_setting(cadence.STATE_KEY))
    assert len(state["seven_day"]["resets"]) == 1
    assert state["seven_day"]["last_percent"] == 5.0


def test_rising_utilization_is_not_a_reset():
    cadence.record_reading(weekly_snapshot(20.0), now=NOW)
    cadence.record_reading(weekly_snapshot(55.0), now=NOW + timedelta(hours=1))
    state = json.loads(db.get_setting(cadence.STATE_KEY))
    assert state["seven_day"]["resets"] == []


def test_small_drop_is_jitter_not_a_reset():
    # Below RESET_DROP_THRESHOLD: endpoint rounding, not a rollover.
    cadence.record_reading(weekly_snapshot(60.0), now=NOW)
    cadence.record_reading(weekly_snapshot(50.0), now=NOW + timedelta(hours=1))
    state = json.loads(db.get_setting(cadence.STATE_KEY))
    assert state["seven_day"]["resets"] == []


def test_first_reading_never_records_a_reset():
    cadence.record_reading(weekly_snapshot(3.0), now=NOW)
    state = json.loads(db.get_setting(cadence.STATE_KEY))
    assert state["seven_day"]["resets"] == []


def test_stale_and_failed_readings_are_ignored():
    cadence.record_reading(weekly_snapshot(50.0), now=NOW)
    cadence.record_reading(weekly_snapshot(5.0, stale=True), now=NOW + timedelta(hours=1))
    cadence.record_reading(weekly_snapshot(5.0, ok=False), now=NOW + timedelta(hours=2))
    state = json.loads(db.get_setting(cadence.STATE_KEY))
    # last_percent still 50 (neither dropped reading was folded in), no reset.
    assert state["seven_day"]["last_percent"] == 50.0
    assert state["seven_day"]["resets"] == []


def test_malformed_snapshot_never_raises():
    cadence.record_reading(None, now=NOW)
    cadence.record_reading({"ok": True}, now=NOW)
    cadence.record_reading({"ok": True, "windows": [{"key": "seven_day"}]}, now=NOW)
    # No crash, and a window with no percent reads as 0.0.
    state = json.loads(db.get_setting(cadence.STATE_KEY) or "{}")
    assert state.get("seven_day", {}).get("resets", []) == []


def test_reset_ring_is_capped():
    percent = 50.0
    t = NOW
    # Each high->low pair is one reset, so loop enough pairs to overflow the ring.
    for _ in range(2 * cadence.RESET_RING + 6):
        cadence.record_reading(weekly_snapshot(percent), now=t)
        percent = 5.0 if percent > 40 else 50.0  # alternate high/low -> a reset each dip
        t += timedelta(hours=30)
    state = json.loads(db.get_setting(cadence.STATE_KEY))
    assert len(state["seven_day"]["resets"]) == cadence.RESET_RING


def test_session_window_is_not_learned():
    # Only seven_day* windows carry a meaningful weekly cadence.
    snap = {
        "ok": True,
        "windows": [
            {"key": "five_hour", "label": "session", "percent": 80.0, "resets_in_sec": 3600},
        ],
    }
    cadence.record_reading(snap, now=NOW)
    cadence.record_reading(dict(snap, windows=[dict(snap["windows"][0], percent=5.0)]),
                           now=NOW + timedelta(hours=1))
    state = json.loads(db.get_setting(cadence.STATE_KEY) or "{}")
    assert "five_hour" not in state


# ---------------------------------------------------------------------------
# Turning recorded resets into an interval
# ---------------------------------------------------------------------------


def test_no_interval_until_enough_resets():
    seed_resets("seven_day", [NOW, NOW + timedelta(hours=72)])  # 2 resets, 1 gap
    assert cadence.observed_interval_sec("seven_day") is None


def test_median_of_plausible_gaps():
    resets = [NOW, NOW + timedelta(hours=72), NOW + timedelta(hours=144), NOW + timedelta(hours=216)]
    seed_resets("seven_day", resets)
    assert cadence.observed_interval_sec("seven_day") == 72 * 3600


def test_short_gaps_are_dropped_before_averaging():
    # A 6h gap (double-detection) sits between two real 72h gaps; it must not
    # drag the median down.
    resets = [NOW, NOW + timedelta(hours=6), NOW + timedelta(hours=78), NOW + timedelta(hours=150)]
    seed_resets("seven_day", resets)
    assert cadence.observed_interval_sec("seven_day") == 72 * 3600


def test_all_gaps_too_short_yields_no_interval():
    resets = [NOW, NOW + timedelta(hours=2), NOW + timedelta(hours=4), NOW + timedelta(hours=6)]
    seed_resets("seven_day", resets)
    assert cadence.observed_interval_sec("seven_day") is None


def test_implausibly_long_interval_is_ignored():
    resets = [NOW, NOW + timedelta(days=9), NOW + timedelta(days=18), NOW + timedelta(days=27)]
    seed_resets("seven_day", resets)
    assert cadence.observed_interval_sec("seven_day") is None


def test_last_reset_at():
    resets = [NOW, NOW + timedelta(hours=72)]
    seed_resets("seven_day", resets)
    assert cadence.last_reset_at("seven_day") == NOW + timedelta(hours=72)
    assert cadence.last_reset_at("seven_day_opus") is None


# ---------------------------------------------------------------------------
# Predicting the next reset and the effective horizon
# ---------------------------------------------------------------------------


def _seed_72h(key="seven_day", last=None):
    last = last or NOW
    resets = [last - timedelta(hours=144), last - timedelta(hours=72), last]
    seed_resets(key, resets)


def test_predicted_next_reset_is_last_plus_interval():
    _seed_72h(last=NOW)
    got = cadence.predicted_next_reset("seven_day", now=NOW + timedelta(hours=1))
    assert got == NOW + timedelta(hours=72)


def test_predicted_reset_rolls_forward_past_now():
    # Portal was down across a reset: last observed reset is 100h ago, interval
    # 72h, so the naive last+interval is in the past and must roll forward.
    _seed_72h(last=NOW - timedelta(hours=100))
    got = cadence.predicted_next_reset("seven_day", now=NOW)
    assert got is not None and got > NOW
    assert got == NOW - timedelta(hours=100) + timedelta(hours=144)


def test_predicted_reset_none_without_cadence():
    assert cadence.predicted_next_reset("seven_day", now=NOW) is None


def test_effective_resets_in_takes_the_sooner():
    _seed_72h(last=NOW)
    entry = {"key": "seven_day", "resets_in_sec": 5 * 86400}  # endpoint claims 5 days
    got = cadence.effective_resets_in_sec(entry, now=NOW + timedelta(hours=1))
    # learned predicts a reset in ~71h, far sooner than 5 days
    assert got == 71 * 3600


def test_effective_resets_in_falls_back_to_endpoint():
    entry = {"key": "seven_day", "resets_in_sec": 3 * 86400}
    assert cadence.effective_resets_in_sec(entry, now=NOW) == 3 * 86400


def test_effective_week_sec_learned_or_default():
    entry_key = "seven_day"
    assert cadence.effective_week_sec(entry_key) == float(cadence.WEEK_SEC)
    _seed_72h()
    assert cadence.effective_week_sec(entry_key) == 72 * 3600


def test_describe():
    assert cadence.describe("seven_day") == ""
    _seed_72h()
    assert cadence.describe("seven_day") == "learned reset cadence ~3.0d (endpoint claims 7d)"


# ---------------------------------------------------------------------------
# Integration: the boost paces on the learned horizon
# ---------------------------------------------------------------------------


def test_boost_ignores_learned_when_none_established():
    # No cadence learned: the boost falls back to the endpoint's resets_at and
    # the seven-day divisor. A window 6 days out has a front-loaded target of
    # ~23%, so 22% spent sits on that curve - no boost, and no cadence horizon
    # was consulted to reach that verdict.
    snap = weekly_snapshot(22.0, resets_in_sec=6 * 86400)
    pace = pacing.weekly_pace(snap, now=NOW)
    assert pace is not None and pace["factor"] == 1.0


def test_boost_fires_on_the_real_horizon():
    # Same window - 10% used, endpoint says 5 days left - but the learned 72h
    # cadence says the reset is ~24h away, so ~67% of the *real* window has
    # elapsed against only 10% spent: a big surplus that must boost.
    _seed_72h(last=NOW - timedelta(hours=48))  # next reset ~24h from NOW
    snap = weekly_snapshot(10.0, resets_in_sec=5 * 86400)
    pace = pacing.weekly_pace(snap, now=NOW)
    assert pace is not None
    assert pace["elapsed"] > 60.0
    assert pace["factor"] > 1.0


def test_status_line_names_the_learned_cadence(monkeypatch):
    """This test rotted, and how it rotted is worth keeping.

    It used to write the snapshot into the live cache and call `status_line()`
    with no clock. Both halves read the real time: `limits.cached()` marks a
    snapshot stale once its `resets_at` has passed, and `resets_at` here is
    five days after the frozen NOW of 2026-07-21. So from 2026-07-27 the
    cached reading was permanently "stale", `weekly_pace` refused it, and the
    line came back empty. It passed on the day it was written and failed every
    day after - the one test in this suite that could go red with nobody
    touching the code.

    Both the clock and the reading are pinned now. The subject is how the line
    is *worded*, so the freshness machinery has no business in it.
    """
    _seed_72h(last=NOW - timedelta(hours=48))
    snap = dict(weekly_snapshot(10.0, resets_in_sec=5 * 86400), ok=True, stale=False)
    monkeypatch.setattr(limits, "cached", lambda *a, **k: snap)
    line = pacing.status_line(now=NOW)
    assert "learned reset cadence" in line


# ---------------------------------------------------------------------------
# Wiring: a successful refresh feeds the learner
# ---------------------------------------------------------------------------


def test_refresh_records_into_cadence(monkeypatch):
    # Two successive good fetches, the second lower, must land a reset.
    payloads = iter([
        {"ok": True, "raw": {"seven_day": {"utilization": 50.0,
                                           "resets_at": (NOW + timedelta(days=5)).isoformat()}}},
        {"ok": True, "raw": {"seven_day": {"utilization": 4.0,
                                           "resets_at": (NOW + timedelta(days=5)).isoformat()}}},
    ])
    monkeypatch.setattr(limits, "fetch_raw", lambda *a, **k: next(payloads))
    limits.refresh()
    limits.refresh()
    state = json.loads(db.get_setting(cadence.STATE_KEY))
    assert len(state["seven_day"]["resets"]) == 1

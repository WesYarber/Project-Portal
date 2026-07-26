"""The don't-saturate compliance guard (app/pacing.py).

The portal deliberately leaves idle gaps rather than pinning every five-hour
window to the wall around the clock: 24/7 background running is the named reason
the weekly limits exist, so steady use should not *look* like a bot. These
tests pin the duty-cycle measurement, the ceiling reading, when the guard holds,
and the spend-down exemption. Nothing here touches the network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db, pacing, settings_form

NOW = datetime(2026, 7, 21, 22, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _run(started: datetime, ended=None, status="ok"):
    """Insert a run row with explicit start/end so a duty cycle is reproducible."""
    conn = db.get_conn()
    with db._LOCK:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (project_id, task, model, started_at, ended_at, status) "
            "VALUES (NULL, 'build', 'sonnet', ?, ?, ?)",
            (_iso(started), _iso(ended) if ended else None, status),
        )
        conn.commit()


# --- duty_cycle, the pure measurement ---------------------------------------

def test_duty_cycle_empty_is_zero():
    start = NOW - timedelta(hours=5)
    assert pacing.duty_cycle([], start, NOW) == 0.0


def test_duty_cycle_half_full():
    start = NOW - timedelta(hours=4)
    # One two-hour run inside a four-hour window is 50% duty.
    intervals = [(NOW - timedelta(hours=3), NOW - timedelta(hours=1))]
    assert pacing.duty_cycle(intervals, start, NOW) == 0.5


def test_duty_cycle_merges_overlapping_runs():
    start = NOW - timedelta(hours=4)
    # Two parallel runs overlapping the same two hours count once, not twice.
    intervals = [
        (NOW - timedelta(hours=3), NOW - timedelta(hours=1)),
        (NOW - timedelta(hours=2, minutes=30), NOW - timedelta(minutes=30)),
    ]
    # Union is 3h - 0h30 = 2.5h over a 4h window.
    assert pacing.duty_cycle(intervals, start, NOW) == 2.5 / 4.0


def test_duty_cycle_clips_overhang():
    start = NOW - timedelta(hours=2)
    # A run that began an hour before the window counts only the part inside.
    intervals = [(NOW - timedelta(hours=3), NOW - timedelta(hours=1))]
    assert pacing.duty_cycle(intervals, start, NOW) == 0.5


def test_duty_cycle_running_run_counts_to_now():
    start = NOW - timedelta(hours=4)
    # A still-running run (end None) is busy right up to now.
    intervals = [(NOW - timedelta(hours=1), None)]
    assert pacing.duty_cycle(intervals, start, NOW) == 0.25


def test_duty_cycle_never_exceeds_one():
    start = NOW - timedelta(hours=2)
    intervals = [(NOW - timedelta(hours=5), None)]  # spans the whole window and more
    assert pacing.duty_cycle(intervals, start, NOW) == 1.0


# --- the ceiling setting ----------------------------------------------------

def test_default_ceiling():
    assert pacing.saturation_max_duty() == pacing.DEFAULT_SATURATION


def test_custom_ceiling():
    db.set_setting(pacing.SATURATION_SETTING, "70")
    assert pacing.saturation_max_duty() == 70.0


def test_zero_disables_the_guard():
    db.set_setting(pacing.SATURATION_SETTING, "0")
    assert pacing.saturation_max_duty() is None


def test_hundred_disables_the_guard():
    db.set_setting(pacing.SATURATION_SETTING, "100")
    assert pacing.saturation_max_duty() is None


def test_junk_ceiling_falls_back_to_default():
    db.set_setting(pacing.SATURATION_SETTING, "not-a-number")
    assert pacing.saturation_max_duty() == pacing.DEFAULT_SATURATION


# --- saturation_hold, end to end against real run rows ----------------------

def test_quiet_portal_does_not_hold():
    # One short run in the last five hours: nowhere near saturating.
    _run(NOW - timedelta(hours=1), NOW - timedelta(minutes=50))
    assert pacing.saturation_hold(NOW) is None


def test_near_continuous_running_holds():
    # Busy for 4h30 of the last 5h = 90% duty, past the 85% default.
    _run(NOW - timedelta(hours=5), NOW - timedelta(minutes=30))
    hold = pacing.saturation_hold(NOW)
    assert hold is not None
    assert hold["duty"] >= 85.0
    assert hold["ceiling"] == pacing.DEFAULT_SATURATION


def test_paced_operation_stays_under_ceiling():
    # 20-minute runs with a 10-minute gap between them: 2/3 duty, under 85%.
    cursor = NOW - timedelta(hours=5)
    while cursor < NOW:
        _run(cursor, cursor + timedelta(minutes=20))
        cursor += timedelta(minutes=30)
    assert pacing.saturation_hold(NOW) is None


def test_spend_down_is_exempt(monkeypatch):
    _run(NOW - timedelta(hours=5), NOW - timedelta(minutes=30))  # would otherwise hold
    monkeypatch.setattr(pacing, "spending_down", lambda now=None: True)
    assert pacing.saturation_hold(NOW) is None


def test_disabled_guard_never_holds():
    db.set_setting(pacing.SATURATION_SETTING, "0")
    _run(NOW - timedelta(hours=5), NOW - timedelta(minutes=30))
    assert pacing.saturation_hold(NOW) is None


def test_fails_open_on_db_error(monkeypatch):
    def boom(_since):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(db, "runs_active_since", boom)
    # A bug reading runs must insert no gap, only ever fail open to "no hold".
    assert pacing.saturation_hold(NOW) is None


def test_reason_names_the_numbers():
    reason = pacing.saturation_reason({"duty": 92.0, "ceiling": 85.0, "window_hours": 5.0})
    assert "92%" in reason
    assert "85%" in reason
    assert "run now still goes" in reason


# --- the settings validator -------------------------------------------------

def test_validator_keeps_a_valid_ceiling():
    out = settings_form.apply(
        {"saturation_max_duty": "80"}, declared="saturation_max_duty"
    )
    assert out["saturation_max_duty"] == "80"


def test_validator_keeps_zero_as_the_real_off_value():
    out = settings_form.apply(
        {"saturation_max_duty": "0"}, declared="saturation_max_duty"
    )
    assert out["saturation_max_duty"] == "0"


def test_validator_rejects_junk_and_out_of_range():
    for bad in ("nonsense", "-5", "250"):
        out = settings_form.apply(
            {"saturation_max_duty": bad}, declared="saturation_max_duty"
        )
        assert out["saturation_max_duty"] == "85"

"""One project may not take the whole board's run budget.

Wes, 2026-08-07:

  "This project ran a ton overnight without me meaning for it to and used up a
  ton of my weekly limit. Please help tame that. I'm not certain that it was at
  all necessary."

Quiet hours (app/quiet.py) answered the "overnight" half. This is the other
half. `max_runs_per_day` is a budget for the WHOLE board, and
`db.list_schedulable_projects` sorts `priority DESC` - so the top project is
offered to every tick until it runs out of workable todos, and only then does
the scheduler reach the second one. Project Portal sits at priority 6, the
highest on the board, and took 70 of the last 202 runs. Nothing was
misbehaving; the board has simply never had a per-project limit that was on by
default. Every one of the 37 projects has a NULL `max_runs_per_day`, and the
control for setting it was deliberately deleted from the project page (see
tests/test_ui_notes_4.py), so the only reachable lever is a board-wide one.

What these pin, in the order they can go wrong quietly:

- The default applies without being opted into. A cap nobody sets is the same
  as no cap, which is the state this found the board in.
- A cap the project carries beats the default in BOTH directions - lower and
  higher - because a number Wes typed is a decision, not a suggestion.
- 0 means off, at either level, so the old behavior is still reachable.
- A spend-down lifts the default and does NOT lift a project's own cap. He has
  answered "yes, spend it" ten times over about a weekly window that would
  otherwise expire, and this default is about spreading work over the board
  rather than about saving allowance.
- The scheduler SKIPS a capped project rather than stopping at it. Getting that
  wrong idles the whole board behind its busiest project, which is worse than
  the problem being fixed.
- A manual run ignores it entirely, exactly as it ignores a project's own cap.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db, pacing, worker


@pytest.fixture
def alpha():
    return db.create_project("Alpha", stage="active", build_approved=True, slug="alpha")


def _runs(project_id: int, n: int) -> None:
    """`n` runs today, all FINISHED.

    Leaving them running would make every assertion below pass for the wrong
    reason: `_pick_project` skips a project with a run in flight under the
    one-run-per-project rule, which has nothing to do with the daily cap.
    """
    for _ in range(n):
        db.finish_run(db.create_run(project_id, "build", "sonnet"), "ok")


def _spend_down_on() -> None:
    """Anchored to the REAL clock, never to a frozen NOW.

    `pacing.active_until` compares this setting against `datetime.now`, so a
    value derived from a fixture's frozen instant passes until that timestamp
    goes by and then fails for good - with a message pointing at the feature
    rather than at the fixture. test_headroom.py shipped exactly that bug.
    """
    until = datetime.now(timezone.utc) + timedelta(hours=2)
    db.set_setting(pacing.ACTIVE_UNTIL, until.isoformat())


# --------------------------------------------------------------------------
# The default, and that it is on without being asked for
# --------------------------------------------------------------------------


def test_a_project_with_no_cap_of_its_own_stops_at_the_board_default(temp_data_dir, alpha):
    db.set_setting("project_max_runs_per_day", "3")
    _runs(alpha["id"], 2)
    assert worker.project_at_daily_cap(db.get_project(alpha["id"])) is False
    _runs(alpha["id"], 1)
    assert worker.project_at_daily_cap(db.get_project(alpha["id"])) is True


def test_the_default_applies_with_nothing_configured(temp_data_dir, alpha):
    """The bug was that a per-project limit existed and no project had one, so
    a default that only works once somebody sets it fixes nothing."""
    assert db.get_project(alpha["id"])["max_runs_per_day"] is None
    _runs(alpha["id"], db.default_project_max_runs())
    assert worker.project_at_daily_cap(db.get_project(alpha["id"])) is True


def test_the_default_is_a_real_number_out_of_the_box(temp_data_dir):
    assert db.default_project_max_runs() == 6


def test_a_junk_setting_falls_back_rather_than_raising(temp_data_dir):
    """A pacing guard must never be able to stop the worker deciding."""
    db.set_setting("project_max_runs_per_day", "six")
    assert db.default_project_max_runs() == 6


def test_the_fallback_holds_when_the_setting_is_blank(temp_data_dir, alpha):
    """The two escapes of the mutation sweep, and why they escaped.

    Breaking the `or "6"` literal in `db.default_project_max_runs` changed
    nothing, because `config.DEFAULT_SETTINGS` seeds a real row and every other
    test here reads that row - so the literal is only ever reached when the row
    is missing or blank, which nothing exercised. That is not a hypothetical
    state: blanking a number is what the settings form stores when the field is
    cleared, and a database seeded before this key existed has no row at all.
    Two independent copies of the number is the actual smell; this pins both to
    the same value so they cannot drift apart silently.
    """
    db.set_setting("project_max_runs_per_day", "")
    assert db.default_project_max_runs() == 6
    _runs(alpha["id"], 6)
    assert worker.project_at_daily_cap(db.get_project(alpha["id"])) is True


def test_zero_turns_the_default_off(temp_data_dir, alpha):
    """The behavior the board had before this existed is still reachable."""
    db.set_setting("project_max_runs_per_day", "0")
    _runs(alpha["id"], 50)
    assert worker.project_at_daily_cap(db.get_project(alpha["id"])) is False


# --------------------------------------------------------------------------
# A number Wes typed wins
# --------------------------------------------------------------------------


def test_the_projects_own_cap_wins_when_it_is_lower(temp_data_dir, alpha):
    db.set_setting("project_max_runs_per_day", "10")
    db.update_project(alpha["id"], max_runs_per_day=2)
    _runs(alpha["id"], 2)
    assert worker.project_at_daily_cap(db.get_project(alpha["id"])) is True


def test_the_projects_own_cap_wins_when_it_is_higher(temp_data_dir, alpha):
    """The escape hatch: the per-project number is how one project is let off
    the board default. This is the direction Wes asked for on 2026-08-13 - "I
    don't see where I can increase daily limits on runs of single projects" -
    and the reason the control is back on the project page."""
    db.set_setting("project_max_runs_per_day", "2")
    db.update_project(alpha["id"], max_runs_per_day=9)
    _runs(alpha["id"], 5)
    assert worker.project_at_daily_cap(db.get_project(alpha["id"])) is False


def test_zero_on_the_project_is_no_cap_at_all(temp_data_dir, alpha):
    """NULL and 0 are different answers, and this is the difference.

    NULL inherits the board default; 0 says this one project is exempt from it.
    They used to be the same value - the route folded 0 to NULL - which left
    no way at all to say "let this project run as much as it likes".
    """
    db.set_setting("project_max_runs_per_day", "2")
    db.update_project(alpha["id"], max_runs_per_day=0)
    _runs(alpha["id"], 50)
    assert worker.effective_project_cap(db.get_project(alpha["id"])) == 0
    assert worker.project_at_daily_cap(db.get_project(alpha["id"])) is False


def test_null_still_inherits_the_default_rather_than_meaning_no_cap(temp_data_dir, alpha):
    """The other side of the same coin: splitting 0 out must not turn every
    uncapped project loose."""
    db.set_setting("project_max_runs_per_day", "2")
    assert db.get_project(alpha["id"])["max_runs_per_day"] is None
    assert worker.effective_project_cap(db.get_project(alpha["id"])) == 2
    _runs(alpha["id"], 2)
    assert worker.project_at_daily_cap(db.get_project(alpha["id"])) is True


def test_the_effective_cap_reports_what_is_enforced(temp_data_dir, alpha):
    """What the project page's "3/8 runs today" denominator reads from, so the
    number shown and the number enforced cannot drift apart."""
    db.set_setting("project_max_runs_per_day", "6")
    assert worker.effective_project_cap(db.get_project(alpha["id"])) == 6
    db.update_project(alpha["id"], max_runs_per_day=20)
    assert worker.effective_project_cap(db.get_project(alpha["id"])) == 20


def test_a_pre_migration_row_has_no_cap(temp_data_dir, alpha):
    row = {k: alpha[k] for k in alpha.keys() if k != "max_runs_per_day"}
    assert worker.effective_project_cap(row) == 0


def test_a_row_from_before_the_column_existed_does_not_raise(temp_data_dir, alpha):
    row = {k: alpha[k] for k in alpha.keys() if k != "max_runs_per_day"}
    assert worker.project_at_daily_cap(row) is False


# --------------------------------------------------------------------------
# The spend-down carve-out
# --------------------------------------------------------------------------


def test_a_spend_down_lifts_the_board_default(temp_data_dir, alpha):
    db.set_setting("project_max_runs_per_day", "2")
    _runs(alpha["id"], 5)
    assert worker.project_at_daily_cap(db.get_project(alpha["id"])) is True
    _spend_down_on()
    assert worker.project_at_daily_cap(db.get_project(alpha["id"])) is False


def test_a_spend_down_does_not_lift_a_cap_he_set_himself(temp_data_dir, alpha):
    db.update_project(alpha["id"], max_runs_per_day=2)
    _runs(alpha["id"], 5)
    _spend_down_on()
    assert worker.project_at_daily_cap(db.get_project(alpha["id"])) is True


# --------------------------------------------------------------------------
# What the scheduler does with it - the part that matters
# --------------------------------------------------------------------------


def test_the_scheduler_moves_on_to_the_next_project(temp_data_dir):
    """The reported bug, end to end. Two active projects, the busy one at the
    head of the queue: the tick must reach the second.

    `top` is created first, so it is the least recently touched of the two and
    the scheduler picks it every tick until something stops it - which is the
    situation the cap exists for. (This used to say the same thing with
    `priority=6`, before priority was removed on 2026-08-16.)"""
    top = db.create_project("Top", stage="active", build_approved=True, slug="top")
    other = db.create_project("Other", stage="active", build_approved=True, slug="other")
    db.set_setting("project_max_runs_per_day", "2")

    picked, _ = worker._pick_project(None)
    assert picked["slug"] == "top"

    _runs(top["id"], 2)
    picked, _ = worker._pick_project(None)
    assert picked["slug"] == "other", "a capped project must not idle the board behind it"


def test_the_board_goes_quiet_only_when_every_project_is_capped(temp_data_dir):
    a = db.create_project("A", stage="active", build_approved=True, slug="a")
    b = db.create_project("B", stage="active", build_approved=True, slug="b")
    db.set_setting("project_max_runs_per_day", "1")
    _runs(a["id"], 1)
    _runs(b["id"], 1)
    picked, _ = worker._pick_project(None)
    assert picked is None


def test_the_dashboard_says_the_runs_come_back(temp_data_dir, alpha):
    """A board holding for this reason must not read as a fault. The old
    wording said "its own per-project daily cap", which is now wrong as often
    as it is right - the cap that stopped it is usually the board default."""
    db.set_setting("project_max_runs_per_day", "1")
    _runs(alpha["id"], 1)
    reason = worker.idle_reason()
    assert "taken all its runs for today" in reason
    assert "reset in" in reason
    assert "its own" not in reason


def test_a_manual_run_ignores_the_cap(temp_data_dir, alpha):
    """Wes asking for a run is the whole point - the same carve-out the
    per-project cap has always had."""
    db.set_setting("project_max_runs_per_day", "1")
    _runs(alpha["id"], 5)
    picked, manual = worker._pick_project(alpha["id"])
    assert picked is not None and picked["slug"] == "alpha"
    assert manual is True

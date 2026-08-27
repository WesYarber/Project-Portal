"""The Claude windows on the side rail (limits.compact_windows, base.html).

Wes, 2026-08-16:

  "It would be cool to see a very brief visualization or writeup of current
  usage vs 5hr limit and current usage vs week limit + when they reset / how
  close we are to when they reset on the side nav-bar. To save space, it might
  could be shown as a pie chart for how close we are to resets. For example, to
  show 25% of the 5hr limit has been used and it resets in 1 hr, it could show
  "5hr: 25%, o" where the o is a tiny pie chart progression meter that is 4/5
  full and 1/5 empty."

The two numbers in that sentence are answers to different questions and the
whole value of the row is reading them together, so the tests below keep them
apart on purpose: `percent` is how much of the allowance is gone, `elapsed` is
how far through the window we are. His worked example - 25% spent, resets in an
hour, pie four fifths full - is `test_his_worked_example` and is the one that
must never quietly become "both numbers are the same number".
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import db, limits, sidebar

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def snapshot(five=25.0, seven=10.0, five_reset_in=3600, seven_reset_in=2 * 86400):
    """A stored reading shaped like `limits.parse()` output.

    Reset times are written as real timestamps relative to the actual clock,
    because `limits.cached()` recomputes every countdown against now and would
    otherwise overwrite whatever `resets_in_sec` a fixture had baked in.
    """
    now = datetime.now(timezone.utc)
    return {
        "ok": True,
        "error": "",
        "windows": [
            {
                "key": "five_hour",
                "label": "session",
                "percent": five,
                "resets_at": (now + timedelta(seconds=five_reset_in)).isoformat(),
                "resets_in_sec": five_reset_in,
                "resets_in": limits.humanize_until(five_reset_in),
            },
            {
                "key": "seven_day",
                "label": "weekly",
                "percent": seven,
                "resets_at": (now + timedelta(seconds=seven_reset_in)).isoformat(),
                "resets_in_sec": seven_reset_in,
                "resets_in": limits.humanize_until(seven_reset_in),
            },
        ],
        "scoped": [],
        "fetched_at": now.isoformat(timespec="seconds"),
    }


def store(snap):
    db.set_setting(limits.CACHE_KEY, json.dumps(snap))


# --- the clock half of the row ---------------------------------------------


def test_his_worked_example():
    """"25% of the 5hr limit has been used and it resets in 1 hr" - the pie is
    4/5 full. The percentage spent has nothing to do with it."""
    rows = limits.compact_windows(snapshot(five=25.0, five_reset_in=3600))
    session = rows[0]

    assert session["used"] == 25
    assert session["elapsed"] == 80.0


def test_the_two_numbers_are_not_the_same_number():
    """The whole point of showing both. A window barely touched but nearly over
    is headroom about to expire; the reverse is a limit that will bite early."""
    rows = limits.compact_windows(snapshot(five=90.0, five_reset_in=4 * 3600))

    assert rows[0]["used"] == 90
    assert rows[0]["elapsed"] == 20.0


def test_a_week_is_measured_against_a_week():
    """A weekly window resetting in two days is five sevenths through, not
    two-fifths of the way through five hours."""
    rows = limits.compact_windows(snapshot(seven_reset_in=2 * 86400))
    weekly = [r for r in rows if r["key"] == "seven_day"][0]

    assert weekly["elapsed"] == round(5 / 7 * 100, 1)


def test_a_window_about_to_reset_is_a_full_pie():
    rows = limits.compact_windows(snapshot(five_reset_in=0))

    assert rows[0]["elapsed"] == 100.0


def test_a_reset_that_has_already_gone_by_does_not_overfill_the_pie():
    """A negative countdown would draw past full. `cached()` floors the number
    at zero, but the drawing must not depend on that being the only source."""
    assert limits.elapsed_percent("five_hour", -600) == 100.0


def test_a_countdown_longer_than_its_own_window_does_not_underfill_it():
    """Clock skew between this box and the endpoint, seen as a 5-hour window
    reporting six hours to go. Clamped rather than drawn as a negative arc."""
    assert limits.elapsed_percent("five_hour", 6 * 3600) == 0.0


def test_a_window_with_no_reported_reset_draws_no_pie():
    """None, not zero: an empty pie reads as "just reset", which is a claim the
    portal has no reading to make."""
    assert limits.elapsed_percent("five_hour", None) is None


def test_a_window_whose_length_is_not_known_draws_no_pie():
    """Only the windows in WINDOW_SECONDS have a length the portal can state.
    A new one the endpoint invents gets its percentage and its countdown, and
    no pie, rather than a pie measured against a guess."""
    assert limits.elapsed_percent("thirty_day", 3600) is None


# --- which windows the rail carries ----------------------------------------


def test_only_the_account_wide_windows_reach_the_rail():
    """Per-model scoped windows are real and are on the dashboard. Five rows of
    percentages down the side of every page is a status bar, not a glance."""
    snap = snapshot()
    snap["scoped"] = [
        {
            "key": "weekly_opus",
            "model": "Opus",
            "label": "weekly (Opus)",
            "percent": 40.0,
            "resets_in_sec": 86400,
            "resets_in": "1d 0h",
        }
    ]

    assert [r["key"] for r in limits.compact_windows(snap)] == ["five_hour", "seven_day"]


def test_a_fraction_of_a_percent_rounds_rather_than_disappearing():
    """The endpoint reports a tenth and the rail has room for a whole number.
    Truncating would report a window at 74.6% as 74 - always in the direction
    that makes the account look emptier than it is, which is the wrong way for
    a number that exists to warn."""
    rows = limits.compact_windows(snapshot(five=74.6, seven=12.4))

    assert rows[0]["used"] == 75
    assert rows[1]["used"] == 12
    # The tenth itself survives for anything that wants it.
    assert rows[0]["percent"] == 74.6


def test_the_labels_are_short_enough_for_a_15rem_rail():
    rows = limits.compact_windows(snapshot())

    assert [r["label"] for r in rows] == ["5h", "week"]
    # Every abbreviation stays short enough to survive the NARROW placement
    # too, which is where "week opus" ellipsized to "week op..." - all the
    # width spent and none of the meaning delivered.
    assert all(len(label) <= 5 for label in limits.SHORT_LABELS.values())


def test_a_row_keeps_the_full_name_of_its_window():
    """The abbreviation is for the eye; the row's title says which window it
    actually is, so shortening a label can never make one unidentifiable."""
    rows = limits.compact_windows(snapshot())

    assert [r["full"] for r in rows] == ["session", "weekly"]


def test_a_window_past_the_threshold_is_marked_hot():
    rows = limits.compact_windows(snapshot(five=limits.HOT_PERCENT))

    assert rows[0]["hot"] is True
    assert rows[1]["hot"] is False


def test_just_under_the_threshold_is_not_hot():
    """A boundary, so it is stated: HOT_PERCENT itself is hot, below it is
    not."""
    rows = limits.compact_windows(snapshot(five=limits.HOT_PERCENT - 0.1))

    assert rows[0]["hot"] is False


def test_no_trustworthy_reading_means_no_group_at_all():
    """A heading over an apology is worse than nothing. Covers a fresh install,
    a failed fetch, and an API-key install - all of which arrive here as an
    `ok: False` snapshot."""
    assert limits.compact_windows({"ok": False, "error": "no usage snapshot yet"}) == []
    assert limits.compact_windows({"ok": False, "not_applicable": True, "windows": []}) == []


def test_a_failed_reading_that_still_carries_windows_is_not_drawn():
    """The case the `ok` guard is actually for, and the one the two above miss:
    every no-reading path happens to hand over an empty `windows` list today, so
    deleting the guard changes nothing they can see. A snapshot that says it
    failed while still carrying figures is either a shape from an older version
    of the portal or a reading whose windows have rolled over - and drawing a
    percentage off it would put a stale number on every page in the portal
    with nothing marking it stale."""
    stale = {
        "ok": False,
        "error": "access token expired",
        "windows": [
            {"key": "five_hour", "percent": 96.0, "resets_in_sec": 60, "resets_in": "1m"}
        ],
    }

    assert limits.compact_windows(stale) == []


def test_an_ok_snapshot_with_no_windows_is_no_group_either():
    assert limits.compact_windows({"ok": True, "windows": []}) == []


# --- the rail carries it ----------------------------------------------------


def test_the_rail_passes_the_windows_through_untouched():
    """sidebar.build is a pure function of its arguments and does not read the
    snapshot itself - which is what keeps the rest of the rail testable with no
    database and no network behind it."""
    rail = sidebar.build([], usage=[{"key": "five_hour", "used": 25}])

    assert rail["usage"] == [{"key": "five_hour", "used": 25}]


def test_an_empty_rail_still_has_the_key():
    """base.html reads `rail.usage` on every page, including the ones rendering
    while something is already wrong."""
    assert sidebar.empty()["usage"] == []


def test_the_group_is_drawn_on_every_page(client, temp_data_dir):
    db.create_project("Working", description="x", stage="active", slug="working")
    store(snapshot(five=25.0, five_reset_in=3600))

    for path in ("/", "/project/working", "/settings"):
        body = client.get(path).text
        assert 'id="rail-usage"' in body, path
        group = body.split('id="rail-usage"')[1].split("</nav>")[0]
        assert "5h" in group and "25%" in group, path


def test_the_pie_carries_the_elapsed_fraction_and_not_the_spend(client, temp_data_dir):
    """The bug this would be: wiring `--pie` to the percentage used, which
    looks right on any window where the two happen to be close."""
    store(snapshot(five=25.0, five_reset_in=3600))

    group = client.get("/").text.split('id="rail-usage"')[1].split("</nav>")[0]

    assert "--pie: 80.0%" in group
    assert "--pie: 25" not in group


def test_a_window_with_no_reset_time_renders_its_row_without_a_pie(client, temp_data_dir):
    """The percentage and the countdown are still worth saying; the pie is not,
    because its number is unknown. Without the guard in the template the row
    would carry `--pie: None%`, which CSS drops on the floor and draws as an
    empty circle - "just reset", which is a claim nothing supports."""
    snap = snapshot()
    snap["windows"][0]["resets_at"] = ""
    snap["windows"][0]["resets_in_sec"] = None
    snap["windows"][0]["resets_in"] = ""
    store(snap)

    group = client.get("/").text.split('id="rail-usage"')[1].split("</nav>")[0]

    assert "None" not in group
    assert group.count("rail-pie") == 1
    assert "25%" in group


def test_a_reading_that_never_arrived_draws_no_group(client, temp_data_dir):
    """No snapshot stored at all, which is every install's first minute."""
    assert 'id="rail-usage"' not in client.get("/").text


def test_the_rows_lead_to_where_the_same_figures_are_drawn_at_length(client, temp_data_dir):
    """Every row in this rail is a way to get somewhere; these are not the
    exception."""
    store(snapshot())

    group = client.get("/").text.split('id="rail-usage"')[1].split("</nav>")[0]

    assert 'href="/activity"' in group


def test_the_group_is_announced_like_every_other_group_in_the_rail(client, temp_data_dir):
    """PORTAL, THIS PAGE, RECENT - and CLAUDE. One kind of heading, which is
    also what stops it needing a rule of its own (see test_rail_rhythm)."""
    store(snapshot())

    group = client.get("/").text.split('id="rail-usage"')[1].split("</nav>")[0]

    assert '<h3 class="rail-head">Claude</h3>' in group


def test_the_dashboard_chips_and_the_rail_go_hot_together(client, temp_data_dir):
    """One threshold, named once. The dashboard hardcoded 80 while the rail read
    the constant, which is exactly how two numbers for one judgment drift."""
    index = (limits.__file__.rsplit("/app/", 1)[0] + "/app/templates/index.html")
    with open(index, encoding="utf-8") as handle:
        assert "limit_hot_at" in handle.read()

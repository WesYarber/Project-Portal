"""Usage aggregated over time, the run feed, and per-run transcript pages."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import db, runlog, usage


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project("History Project", stage="active", build_approved=True, slug="history-project")


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _run(project_id, days_ago=0.0, status="ok", cost=0.1, turns=4, minutes=5, task="build"):
    """Insert a finished run directly - `create_run`/`finish_run` always stamp
    `now()`, and these tests need runs spread across past days."""
    conn = db.get_conn()
    started = _iso(days_ago)
    ended = (
        datetime.fromisoformat(started) + timedelta(minutes=minutes)
    ).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO runs (project_id, task, model, started_at, ended_at, status, "
        "cost_usd, num_turns, events) VALUES (?, ?, 'opus', ?, ?, ?, ?, ?, 7)",
        (project_id, task, started, None if status == "running" else ended, status, cost, turns),
    )
    conn.commit()
    return int(cur.lastrowid)


# --- sparkline ------------------------------------------------------------

def test_sparkline_is_one_glyph_per_value():
    assert len(usage.sparkline([1, 2, 3, 4])) == 4


def test_sparkline_of_empty_series():
    assert usage.sparkline([]) == ""


def test_sparkline_all_zero_is_blank_not_bars():
    assert usage.sparkline([0, 0, 0]) == "   "


def test_sparkline_peaks_at_the_full_block():
    assert usage.sparkline([0, 1, 10])[-1] == "█"


def test_sparkline_makes_any_nonzero_value_visible():
    """A day with one run against a peak of 500 must not render as a blank."""
    assert usage.sparkline([1, 500])[0] != " "


def test_sparkline_scales_relatively():
    low, high = usage.sparkline([2, 8])
    assert usage.BARS.index(low) < usage.BARS.index(high)


# --- bucketing ------------------------------------------------------------

def test_buckets_cover_every_day_including_idle_ones(project):
    _run(project["id"], days_ago=0)
    _run(project["id"], days_ago=3)
    buckets = usage.bucket_by_day(db.runs_since("2000-01-01"), days=7)
    assert len(buckets) == 7
    assert [b["date"] for b in buckets] == sorted(b["date"] for b in buckets)
    assert sum(b["runs"] for b in buckets) == 2
    assert any(b["runs"] == 0 for b in buckets)


def test_buckets_ignore_runs_outside_the_window(project):
    _run(project["id"], days_ago=0)
    _run(project["id"], days_ago=40)
    buckets = usage.bucket_by_day(db.runs_since("2000-01-01"), days=7)
    assert sum(b["runs"] for b in buckets) == 1


def test_buckets_split_ok_from_failed(project):
    _run(project["id"], status="ok")
    _run(project["id"], status="error")
    _run(project["id"], status="timeout")
    today = usage.bucket_by_day(db.runs_since("2000-01-01"), days=1)[0]
    assert (today["ok"], today["failed"], today["runs"]) == (1, 2, 3)


def test_running_run_counts_as_neither_ok_nor_failed(project):
    _run(project["id"], status="running")
    today = usage.bucket_by_day(db.runs_since("2000-01-01"), days=1)[0]
    assert (today["ok"], today["failed"], today["runs"]) == (0, 0, 1)


def test_duration_only_counts_finished_runs(project):
    _run(project["id"], minutes=10)
    _run(project["id"], status="running")
    today = usage.bucket_by_day(db.runs_since("2000-01-01"), days=1)[0]
    assert today["finished"] == 1
    assert today["duration_sec"] == 600
    # The unfinished run must not drag the mean toward zero.
    assert usage.summarize([today])["avg_duration_sec"] == 600


def test_run_duration_is_none_while_running(project):
    row = db.get_run(_run(project["id"], status="running"))
    assert usage.run_duration(row) is None


def test_summarize_of_an_empty_window_does_not_divide_by_zero():
    totals = usage.summarize(usage.bucket_by_day([], days=7))
    assert totals["runs"] == 0
    assert totals["avg_cost"] == 0.0
    assert totals["success_rate"] == 0.0
    assert totals["avg_duration_sec"] == 0


def test_summarize_costs_and_success_rate(project):
    _run(project["id"], cost=0.25, status="ok")
    _run(project["id"], cost=0.75, status="error")
    totals = usage.summarize(usage.bucket_by_day(db.runs_since("2000-01-01"), days=3))
    assert totals["cost"] == 1.0
    assert totals["avg_cost"] == 0.5
    assert totals["success_rate"] == 50.0


def test_history_is_clamped_to_a_sane_window(project):
    assert usage.history(0)["days"] == 1
    assert usage.history(9999)["days"] == 365


def test_history_can_be_scoped_to_one_project(project):
    other = db.create_project("Other", slug="other")
    _run(project["id"])
    _run(other["id"])
    _run(other["id"])
    assert usage.history(7)["totals"]["runs"] == 3
    assert usage.history(7, project_id=project["id"])["totals"]["runs"] == 1


def test_humanize_seconds():
    assert usage.humanize_seconds(None) == "-"
    assert usage.humanize_seconds(45) == "45s"
    assert usage.humanize_seconds(125) == "2m 05s"
    assert usage.humanize_seconds(3725) == "1h 02m"


# --- db queries -----------------------------------------------------------

def test_runs_since_excludes_older_days(project):
    _run(project["id"], days_ago=0)
    _run(project["id"], days_ago=10)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).date().isoformat()
    assert len(db.runs_since(cutoff)) == 1


def test_recent_runs_are_newest_first_and_pageable(project):
    for day in range(5):
        _run(project["id"], days_ago=day, task=f"task-{day}")
    page1 = db.list_recent_runs(limit=2, offset=0)
    page2 = db.list_recent_runs(limit=2, offset=2)
    assert [r["task"] for r in page1] == ["task-0", "task-1"]
    assert [r["task"] for r in page2] == ["task-2", "task-3"]
    assert db.count_recent_runs() == 5


def test_recent_runs_filter_by_status_and_project(project):
    other = db.create_project("Other", slug="other-2")
    _run(project["id"], status="ok")
    _run(project["id"], status="error")
    _run(other["id"], status="error")
    assert db.count_recent_runs(status="error") == 2
    assert db.count_recent_runs(project_id=project["id"]) == 2
    assert db.count_recent_runs(project_id=project["id"], status="error") == 1


def test_recent_runs_join_carries_the_project(project):
    _run(project["id"])
    row = db.list_recent_runs()[0]
    assert row["project_slug"] == "history-project"
    assert row["project_title"] == "History Project"


def test_recent_runs_include_projectless_reflect_runs():
    run_id = db.create_run(None, "reflect", "haiku")
    row = db.get_run_with_project(run_id)
    assert row["project_id"] is None
    assert row["project_slug"] is None


# --- routes ---------------------------------------------------------------

def test_activity_page_lists_runs(client, project):
    run_id = _run(project["id"], task="build the thing")
    body = client.get("/activity").text
    assert "build the thing" in body
    assert f"/run/{run_id}" in body


def test_activity_page_is_fine_with_no_runs(client):
    resp = client.get("/activity")
    assert resp.status_code == 200
    assert "No runs match" in resp.text


def test_activity_filters_by_project(client, project):
    other = db.create_project("Other", slug="other-3")
    _run(project["id"], task="mine")
    _run(other["id"], task="theirs")
    body = client.get("/activity?project=history-project").text
    assert "mine" in body and "theirs" not in body


def test_activity_rejects_a_bogus_window(client):
    """An unknown ?days= falls back to the default rather than 500ing."""
    resp = client.get("/activity?days=999999")
    assert resp.status_code == 200
    assert "Last 14 days" in resp.text


def test_activity_ignores_an_unknown_project_slug(client, project):
    _run(project["id"], task="still shown")
    assert "still shown" in client.get("/activity?project=nope").text


def test_activity_pages_beyond_the_end_render_empty(client, project):
    _run(project["id"])
    assert client.get("/activity?page=99").status_code == 200


def test_run_page_shows_the_transcript(client, project):
    run_id = _run(project["id"])
    log = runlog.RunLog(run_id)
    log.append(["> Bash(pytest -q)", "< ok (3 lines)"])
    body = client.get(f"/run/{run_id}").text
    assert "Bash(pytest -q)" in body
    assert "history-project" in body


def test_run_page_explains_a_pruned_transcript(client, project):
    run_id = _run(project["id"])
    body = client.get(f"/run/{run_id}").text
    assert "pruned" in body


def test_run_page_404s_on_an_unknown_run(client):
    assert client.get("/run/4242").status_code == 404


def test_api_usage_history_is_json(client, project):
    _run(project["id"], cost=0.5)
    payload = client.get("/api/usage/history?days=7").json()
    assert payload["days"] == 7
    assert len(payload["buckets"]) == 7
    assert payload["totals"]["cost"] == 0.5
    assert len(payload["runs_spark"]) == 7


def test_project_page_reaches_its_runs_through_scoped_activity(client, project):
    """The project page listed every run in a table of its own until Wes asked
    for that section to go. What is left is the count and a link, and the runs
    themselves are one click away on the activity page filtered to it."""
    run_id = _run(project["id"])
    page = client.get("/project/history-project").text
    assert "1 run on this project" in page
    assert 'href="/activity?project=history-project"' in page
    assert f"/run/{run_id}" in client.get("/activity?project=history-project").text


def test_activity_tab_is_in_the_nav(client):
    assert 'href="/activity"' in client.get("/").text

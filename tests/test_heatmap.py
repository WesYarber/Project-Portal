"""The GitHub-style activity grid.

From Wes's note: "Add a GitHub like activity tracker per project and for the
overall site, but make it relatively small and unobtrusive."

What matters here is that the grid tells the truth about *which* days were
busy - a chart whose columns silently drift a day would be worse than no chart -
and that a day that has not happened yet is distinguishable from an idle one.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import db, usage


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project("Fridge Board", stage="active", build_approved=True, slug="fridge")


def _run_on(project_id, date: str) -> None:
    run_id = db.create_run(project_id, "build", "opus")
    conn = db.get_conn()
    conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (f"{date}T12:00:00+00:00", run_id))
    conn.commit()


def _cell(grid: dict, date: str):
    return next(
        (c for week in grid["weeks"] for c in week if c and c["date"] == date), None
    )


# --- shape -----------------------------------------------------------------

def test_the_grid_is_weeks_of_seven_days(temp_data_dir):
    grid = usage.heatmap(weeks=13, today="2026-07-21")
    assert len(grid["weeks"]) == 13
    assert all(len(week) == 7 for week in grid["weeks"])


def test_every_row_is_the_same_weekday(temp_data_dir):
    from datetime import date

    grid = usage.heatmap(weeks=4, today="2026-07-21")  # a Tuesday
    for row in range(7):
        weekdays = {
            date.fromisoformat(week[row]["date"]).weekday()
            for week in grid["weeks"]
            if week[row]
        }
        assert len(weekdays) == 1


def test_days_after_today_are_holes_not_idle_days(temp_data_dir):
    grid = usage.heatmap(weeks=4, today="2026-07-21")  # Tuesday: Wed-Sat are unwritten
    last = grid["weeks"][-1]
    assert last[-1] is None
    assert _cell(grid, "2026-07-22") is None
    assert _cell(grid, "2026-07-21")["date"] == "2026-07-21"


def test_the_window_starts_on_a_sunday(temp_data_dir):
    from datetime import date

    grid = usage.heatmap(weeks=6, today="2026-07-21")
    first = date.fromisoformat(grid["weeks"][0][0]["date"])
    assert first.weekday() == 6  # Sunday


# --- counting --------------------------------------------------------------

def test_a_run_lands_on_the_day_it_started(project):
    _run_on(project["id"], "2026-07-15")
    grid = usage.heatmap(weeks=4, project_id=project["id"], today="2026-07-21")

    assert _cell(grid, "2026-07-15")["runs"] == 1
    assert _cell(grid, "2026-07-16")["runs"] == 0
    assert grid["total"] == 1


def test_a_project_grid_excludes_other_projects(project):
    other = db.create_project("Other", slug="other")
    _run_on(other["id"], "2026-07-15")

    mine = usage.heatmap(weeks=4, project_id=project["id"], today="2026-07-21")
    everything = usage.heatmap(weeks=4, today="2026-07-21")
    assert mine["total"] == 0
    assert everything["total"] == 1


def test_runs_outside_the_window_are_not_counted(project):
    _run_on(project["id"], "2020-01-01")
    grid = usage.heatmap(weeks=4, project_id=project["id"], today="2026-07-21")
    assert grid["total"] == 0


def test_busier_days_get_a_higher_level():
    assert usage.heatmap_level(0) == 0
    assert usage.heatmap_level(1) == 1
    assert usage.heatmap_level(20) == 4
    levels = [usage.heatmap_level(n) for n in range(0, 30)]
    assert levels == sorted(levels)  # monotonic: more runs never means a paler square


# --- on the pages ----------------------------------------------------------

def test_the_dashboard_shows_a_site_wide_grid(client, project):
    _run_on(project["id"], db.now()[:10])
    body = client.get("/").text
    assert "heatmap" in body and "heat-day" in body


def test_the_project_page_shows_its_own_grid(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert body.count("heat-week") == usage.HEATMAP_WEEKS

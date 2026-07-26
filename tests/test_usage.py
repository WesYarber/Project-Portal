"""Run budget (permanent + temporary + per-project) and live-run visibility."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import daycycle, db, runlog, worker


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project("Budget Project", stage="active", build_approved=True, slug="budget-project")


# --- budget ---------------------------------------------------------------

def test_bonus_defaults_to_zero():
    assert db.bonus_runs_today() == 0
    assert db.effective_max_runs() == db.base_max_runs()


def test_bonus_adds_to_todays_budget():
    base = db.base_max_runs()
    assert db.grant_bonus_runs(3) == 3
    assert db.grant_bonus_runs(2) == 5
    assert db.effective_max_runs() == base + 5


def test_bonus_expires_with_the_day():
    db.grant_bonus_runs(5)
    # Relative to the *portal* day, not the UTC calendar date. The portal day
    # rolls at 05:00 local, so between midnight and 05:00 "yesterday's UTC
    # date" IS the current portal day - this test used to pass 19 hours out of
    # 24 and assert nothing at all in the other five.
    today = daycycle.current_day()
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    db.set_setting("bonus_runs_date", yesterday)
    assert db.bonus_runs_today() == 0
    assert db.effective_max_runs() == db.base_max_runs()


def test_bonus_never_goes_negative():
    db.grant_bonus_runs(2)
    assert db.grant_bonus_runs(-10) == 0


def test_corrupt_budget_settings_fall_back_to_defaults():
    db.set_setting("max_runs_per_day", "lots")
    db.set_setting("bonus_runs_count", "many")
    db.set_setting("bonus_runs_date", datetime.now(timezone.utc).date().isoformat())
    assert db.base_max_runs() == 8
    assert db.bonus_runs_today() == 0


def test_bonus_endpoints_grant_and_clear(client):
    client.post("/settings/bonus-runs", data={"extra": "3"})
    assert db.bonus_runs_today() == 3
    client.post("/settings/bonus-runs", data={"extra": "0"})
    assert db.bonus_runs_today() == 0


# --- per-project cap ------------------------------------------------------

def test_no_cap_by_default(project):
    assert worker.project_at_daily_cap(project) is False


def test_project_cap_blocks_scheduled_pick(client, project):
    client.post(f"/project/{project['slug']}/run-cap", data={"max_runs_per_day": "1"})
    db.finish_run(db.create_run(project["id"], "build", "opus"), "ok")
    project = db.get_project(project["id"])
    assert worker.project_at_daily_cap(project) is True
    assert worker._pick_project(None) == (None, False)


def test_project_cap_does_not_block_a_manual_run(client, project):
    client.post(f"/project/{project['slug']}/run-cap", data={"max_runs_per_day": "1"})
    db.finish_run(db.create_run(project["id"], "build", "opus"), "ok")
    picked, is_manual = worker._pick_project(project["id"])
    assert picked["id"] == project["id"]
    assert is_manual is True


def test_a_capped_project_does_not_starve_the_others(client, project):
    other = db.create_project("Other", stage="active", build_approved=True, slug="other")
    client.post(f"/project/{project['slug']}/run-cap", data={"max_runs_per_day": "1"})
    db.update_project(project["id"], priority=9)  # would otherwise be picked first
    db.finish_run(db.create_run(project["id"], "build", "opus"), "ok")
    picked, _ = worker._pick_project(None)
    assert picked["id"] == other["id"]


def test_empty_run_cap_clears_the_override(client, project):
    client.post(f"/project/{project['slug']}/run-cap", data={"max_runs_per_day": "4"})
    assert db.get_project(project["id"])["max_runs_per_day"] == 4
    client.post(f"/project/{project['slug']}/run-cap", data={"max_runs_per_day": ""})
    assert db.get_project(project["id"])["max_runs_per_day"] is None


def test_zero_run_cap_means_no_limit(client, project):
    client.post(f"/project/{project['slug']}/run-cap", data={"max_runs_per_day": "0"})
    assert db.get_project(project["id"])["max_runs_per_day"] is None


def test_non_numeric_run_cap_is_rejected(client, project):
    assert client.post(
        f"/project/{project['slug']}/run-cap", data={"max_runs_per_day": "many"}
    ).status_code == 400


def test_runs_today_counts_per_project(project):
    other = db.create_project("Other", slug="other")
    db.create_run(project["id"], "build", "opus")
    db.create_run(project["id"], "build", "opus")
    db.create_run(other["id"], "build", "opus")
    assert db.count_runs_today(project["id"]) == 2
    assert db.count_runs_today() == 3
    assert db.runs_today_by_project() == {project["id"]: 2, other["id"]: 1}


# --- live run -------------------------------------------------------------

def test_active_run_is_none_when_idle(client):
    body = client.get("/api/active-run").json()
    assert body["active"] is False
    assert body["usage"]["runs_today"] == 0


def test_active_run_reports_the_running_run(client, project):
    run_id = db.create_run(project["id"], "build", "opus")
    db.update_run_activity(run_id, "> Bash(pytest -q)", 12)

    body = client.get("/api/active-run").json()
    assert body["active"] is True
    assert body["run_id"] == run_id
    assert body["project_slug"] == "budget-project"
    assert body["task"] == "build"
    assert body["last_activity"] == "> Bash(pytest -q)"
    assert body["events"] == 12


def test_finished_runs_are_not_active(client, project):
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok")
    assert client.get("/api/active-run").json()["active"] is False


def test_run_log_endpoint_streams_by_offset(client, project):
    run_id = db.create_run(project["id"], "build", "opus")
    log = runlog.RunLog(run_id)
    log.append(["> Bash(ls)"])

    first = client.get(f"/api/run/{run_id}/log").json()
    assert first["text"] == "> Bash(ls)\n"
    assert first["running"] is True

    log.append(["< ok (3 lines)"])
    second = client.get(f"/api/run/{run_id}/log?offset={first['offset']}").json()
    assert second["text"] == "< ok (3 lines)\n"

    db.finish_run(run_id, "ok")
    assert client.get(f"/api/run/{run_id}/log?offset={second['offset']}").json()["running"] is False


def test_run_log_endpoint_404s_for_an_unknown_run(client):
    assert client.get("/api/run/999/log").status_code == 404


def test_negative_offset_is_clamped(client, project):
    run_id = db.create_run(project["id"], "build", "opus")
    runlog.RunLog(run_id).append(["hello"])
    assert client.get(f"/api/run/{run_id}/log?offset=-5").json()["text"] == "hello\n"


def test_usage_endpoint_reports_budget_and_reset(client):
    db.grant_bonus_runs(2)
    usage = client.get("/api/usage").json()
    assert usage["base_max_runs"] == 8
    assert usage["bonus_runs"] == 2
    assert usage["max_runs"] == 10
    assert usage["remaining"] == 10
    assert 0 < usage["resets_in_sec"] <= 86400
    # The budget rolls over at the portal-day boundary (05:00 local), not at
    # midnight - see app/daycycle.py.
    assert usage["resets_at"].endswith(":00:00+00:00")
    assert usage["reset_hour"] == 5


def test_dashboard_marks_the_running_project(client, project):
    assert "cell-run" in client.get("/").text  # markup present, hidden by CSS
    assert "project-cell running" not in client.get("/").text
    db.create_run(project["id"], "build", "opus")
    assert "running" in client.get("/").text


def test_project_page_shows_the_console(client, project):
    run_id = db.create_run(project["id"], "build", "opus")
    runlog.RunLog(run_id).append(["> Bash(pytest -q)"])
    body = client.get(f"/project/{project['slug']}").text
    assert "agent-console" in body
    assert "&gt; Bash(pytest -q)" in body
    assert f'data-run-id="{run_id}"' in body


def test_project_console_falls_back_to_the_last_finished_run(client, project):
    run_id = db.create_run(project["id"], "build", "opus")
    runlog.RunLog(run_id).append(["* run complete"])
    db.finish_run(run_id, "ok")
    body = client.get(f"/project/{project['slug']}").text
    assert "last run transcript" in body
    assert "* run complete" in body


def test_project_page_without_runs_renders(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert "no runs yet" in body
    assert "run agent now" in body


def test_another_projects_run_is_not_shown_as_active_here(client, project):
    other = db.create_project("Other", stage="active", build_approved=True, slug="other")
    db.create_run(other["id"], "build", "opus")
    body = client.get(f"/project/{project['slug']}").text
    assert "agent running..." not in body


def test_settings_page_shows_the_budget_controls(client):
    body = client.get("/settings").text
    assert 'id="budget"' in body
    assert "/settings/bonus-runs" in body

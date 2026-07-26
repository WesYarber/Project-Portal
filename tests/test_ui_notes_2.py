"""A second batch of Wes's UI notes.

From his notes:

  "moving the project portal ASCII art title above the navigation bar on the
   dashboard"
  "Remove where it says '16 active projects' on the dashboard below the ascii
   art logo."
  "Allow the attachments section to be collapsed."
  "I also don't see the summary ... though you say you completed it."

The last one was not a rendering bug: `report_summary` is only recorded from
the run that added the feature onwards, so the banner was empty on every
project's whole history. The fallback to the run's own last output is what
makes the banner show something on runs that predate it.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import db


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project("Fridge Board", description="A thing.", stage="active", build_approved=True, slug="fridge")


# --- the masthead -----------------------------------------------------------

def test_the_ascii_banner_is_above_the_nav_tabs(client, project):
    html = client.get("/").text
    assert html.index('class="banner"') < html.index('class="nav-tabs"')


def test_the_banner_no_longer_counts_projects(client, project):
    html = client.get("/").text
    assert "active project" not in html


def test_the_banner_is_only_on_the_dashboard(client, project):
    assert 'class="banner"' not in client.get("/project/fridge").text


# --- attachments fold -------------------------------------------------------

def test_the_attachments_section_is_collapsed_by_default(client, project):
    db.add_attachment(project["id"], "shot.png", "0001-shot.png", "image/png", 1234)
    html = client.get("/project/fridge").text
    assert '<details class="fold-section attach-block" id="attachments">' in html
    # No `open` attribute: it starts folded.
    assert 'id="attachments" open' not in html


def test_the_attachment_count_is_readable_without_opening_it(client, project):
    db.add_attachment(project["id"], "shot.png", "0001-shot.png", "image/png", 1234)
    db.add_attachment(project["id"], "clip.mp4", "0002-clip.mp4", "video/mp4", 999)
    assert "2 files" in client.get("/project/fridge").text


# --- the work summary banner on historical runs -----------------------------

def _run_with_cli_summary(project_id: int, summary: str) -> int:
    run_id = db.create_run(project_id, "build", "opus")
    db.finish_run(run_id, "ok", summary=summary)
    return run_id


def test_a_run_that_predates_report_summaries_still_shows(project):
    _run_with_cli_summary(project["id"], "Shipped the todo list.\n\nAnd some detail.")
    rows = db.unacknowledged_work(project["id"])
    assert [r["report_summary"] for r in rows] == ["Shipped the todo list."]


def test_the_report_summary_wins_over_the_fallback(project):
    run_id = _run_with_cli_summary(project["id"], "whatever the CLI printed")
    db.set_run_report_summary(run_id, "the line the agent wrote")
    rows = db.unacknowledged_work(project["id"])
    assert [r["report_summary"] for r in rows] == ["the line the agent wrote"]


def test_the_fallback_is_capped(project):
    # The cap is a guard against a model pasting its whole report into the
    # field, not a display limit - Wes asked to see bullets that run past a few
    # lines - so it sits far beyond anything a real bullet reaches.
    _run_with_cli_summary(project["id"], "x" * 4000)
    assert len(db.unacknowledged_work(project["id"])[0]["report_summary"]) <= 1200


def test_a_run_still_in_flight_is_not_in_the_banner(project):
    db.create_run(project["id"], "build", "opus")
    assert db.unacknowledged_work(project["id"]) == []

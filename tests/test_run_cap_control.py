"""Wes's 2026-08-13 note: a single project's daily run limit needs a surface.

  "I don't see where I can increase daily limits on runs of single projects. I
  think it wasn't added to settings correctly."

He is right that there was nowhere. Settings carries `project_max_runs_per_day`,
which is the DEFAULT every project inherits - raising it raises the whole board,
not the one project he is looking at. The per-project control had been deleted
from the project page on his own 2026-07-23 note ("I don't use that on projects
at all"), leaving `projects.max_runs_per_day` reachable only from Telegram or
sqlite.

So the control is back in the project's control bar, beside status and agent,
and the number it enforces is spelled out in the hint line under it.

What these pin, in the order they can go wrong quietly:

- The control renders, submits to the route that already existed, and shows
  the number that is actually in force rather than an empty box.
- "default (N)" carries the board's number, so choosing between inheriting and
  overriding does not mean opening another page to find out what you inherit.
- 0 is a real choice ("no cap"), distinct from inheriting. Folding it back to
  NULL is what left him with no way to lift one project at all.
- A cap set from somewhere else, at a value that is not one of the presets,
  still shows - a control that silently reads "default" for a capped project
  is worse than no control.
- The hint's denominator is the ENFORCED cap, so what the page says and what
  the scheduler does cannot drift apart.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import db, main


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture
def project(temp_data_dir):
    return db.create_project("Fridge Board", slug="fridge", stage="active")


def control(html: str) -> str:
    """Just the runs/day <form>, so a match cannot come from elsewhere on a
    2,000-line page. Sliced from the opening tag, not from the class attribute:
    `action` is written before `class`, and cutting there would leave the route
    outside the window the assertions read."""
    assert 'class="control control-runs"' in html, "the runs/day control is not on the page"
    before, _, rest = html.partition('class="control control-runs"')
    return "<form" + before.rsplit("<form", 1)[1] + rest.split("</form>", 1)[0]


def page(client, project) -> str:
    return client.get(f"/project/{project['slug']}").text


# --------------------------------------------------------------------------
# It is on the page, and it is wired to the route
# --------------------------------------------------------------------------


def test_the_control_is_in_the_project_control_bar(client, project):
    """Beside status and agent - it is a property of this project, like those,
    and Wes calls that block "the project status/settings stuff"."""
    html = page(client, project)
    bar = html.split('class="control-bar"', 1)[1].split("</div>", 1)[0]
    assert "control-runs" in bar
    assert bar.index("control-status") < bar.index("control-runs")


def test_it_posts_to_the_run_cap_route(client, project):
    assert f'action="/project/{project["slug"]}/run-cap"' in control(page(client, project))


def test_it_submits_on_change_like_its_siblings(client, project):
    """Status and agent both save the moment you pick - a save button on the
    third control alone would read as a mistake."""
    assert "this.form.submit()" in control(page(client, project))


def test_choosing_a_number_takes_effect(client, project):
    client.post(f"/project/{project['slug']}/run-cap", data={"max_runs_per_day": "20"})
    assert db.get_project(project["id"])["max_runs_per_day"] == 20
    assert '<option value="20" selected>20</option>' in control(page(client, project))


# --------------------------------------------------------------------------
# The three states the control has to tell apart
# --------------------------------------------------------------------------


def test_the_default_option_names_the_boards_number(client, project):
    db.set_setting("project_max_runs_per_day", "6")
    assert "default (6)" in control(page(client, project))


def test_the_default_option_follows_the_setting(client, project):
    db.set_setting("project_max_runs_per_day", "12")
    assert "default (12)" in control(page(client, project))


def test_the_default_option_says_no_cap_when_the_board_has_none(client, project):
    db.set_setting("project_max_runs_per_day", "0")
    assert "default (no cap)" in control(page(client, project))


def test_an_uncapped_project_shows_default_selected(client, project):
    assert db.get_project(project["id"])["max_runs_per_day"] is None
    assert '<option value="" selected>' in control(page(client, project))


def test_no_cap_is_offered_and_sticks(client, project):
    """The state he asked for by name: this project, off the shared limit."""
    body = control(page(client, project))
    assert ">no cap</option>" in body
    client.post(f"/project/{project['slug']}/run-cap", data={"max_runs_per_day": "0"})
    assert db.get_project(project["id"])["max_runs_per_day"] == 0
    assert '<option value="0" selected>no cap</option>' in control(page(client, project))


def test_no_cap_and_default_are_different_selections(client, project):
    """0 and NULL both used to store NULL, so picking "no cap" put the control
    back on "default" and quietly kept the board limit."""
    client.post(f"/project/{project['slug']}/run-cap", data={"max_runs_per_day": "0"})
    body = control(page(client, project))
    assert '<option value="0" selected>no cap</option>' in body
    assert '<option value="" selected>' not in body


def test_a_cap_set_elsewhere_off_the_preset_list_still_shows(client, project):
    """Telegram and sqlite can write any number. A control that displayed
    "default" for a project capped at 7 would be lying about what is enforced."""
    db.update_project(project["id"], max_runs_per_day=7)
    body = control(page(client, project))
    assert '<option value="7" selected>7</option>' in body
    assert '<option value="" selected>' not in body


# --------------------------------------------------------------------------
# The hint line under the row
# --------------------------------------------------------------------------


def test_the_hint_counts_todays_runs_against_the_cap(client, project):
    db.set_setting("project_max_runs_per_day", "6")
    db.finish_run(db.create_run(project["id"], "build", "opus"), "ok")
    hint = page(client, project).split('class="hint control-hint"', 1)[1].split("</p>", 1)[0]
    assert "1/6 run" in hint


def test_the_hint_denominator_is_the_projects_own_cap(client, project):
    db.set_setting("project_max_runs_per_day", "6")
    db.update_project(project["id"], max_runs_per_day=20)
    db.finish_run(db.create_run(project["id"], "build", "opus"), "ok")
    hint = page(client, project).split('class="hint control-hint"', 1)[1].split("</p>", 1)[0]
    assert "1/20 run" in hint
    assert "1/6 run" not in hint


def test_the_hint_drops_the_denominator_when_nothing_caps_the_project(client, project):
    db.update_project(project["id"], max_runs_per_day=0)
    db.finish_run(db.create_run(project["id"], "build", "opus"), "ok")
    hint = page(client, project).split('class="hint control-hint"', 1)[1].split("</p>", 1)[0]
    assert "1 run today on this project" in hint
    # Only this project's own clause: the model name before it closes a
    # </strong> and the portal-wide count after it is an X/Y of its own, so
    # both carry a slash that has nothing to do with the cap.
    assert "1/" not in hint.split("today on this project", 1)[0]


# --------------------------------------------------------------------------
# Settings still points at the right thing
# --------------------------------------------------------------------------


def test_settings_calls_its_field_the_default_and_points_here(client):
    """His note was "I think it wasn't added to settings correctly" - the field
    in Settings is the board-wide default, so it has to say so and say where
    one project's own number lives."""
    html = client.get("/settings").text
    field = html.split('for="project_max_runs_per_day"', 1)[1].split("</label>", 1)[0]
    assert "Default per-project runs a day" in field
    assert "project page" in field

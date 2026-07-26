"""Editable, lockable titles and descriptions - and the original idea.

Three of Wes's notes converge here:

- the idea he first types should stay the prompt's brief, while the
  description is regenerated to describe what the project has become;
- descriptions should be editable, and lockable so an agent won't touch them;
- project names should be editable, including the workspace folder, if it can
  be done cleanly.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, settings_form, worker


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project(temp_data_dir):
    row = db.create_project("Dice Tower", description="A tower that rolls dice.", slug="dice-tower")
    (config.PROJECTS_DIR / row["slug"]).mkdir(parents=True, exist_ok=True)
    return row


def _report(**fields):
    return agent_runner.RunResult(ok=True, report={"journal_entry_md": "did a thing", **fields})


def _fresh(project):
    return db.get_project(project["id"])


# --- the original idea is preserved ----------------------------------------

def test_initial_idea_starts_as_a_copy_of_the_description(project):
    assert project["initial_idea"] == "A tower that rolls dice."


def test_agent_rewriting_the_description_leaves_the_idea_alone(project):
    worker._apply_report(project, _report(description="A 3D-printed dice tower with an ESP32."))
    row = _fresh(project)
    assert row["description"] == "A 3D-printed dice tower with an ESP32."
    assert row["initial_idea"] == "A tower that rolls dice."


def test_existing_projects_are_backfilled_on_migration(temp_data_dir):
    """A database written before the column existed: `description` *was* the
    idea, so the back-fill is exact rather than a guess."""
    conn = db.get_conn()
    conn.execute("UPDATE projects SET initial_idea = ''")
    conn.commit()
    row = db.create_project("Old", description="the original words")
    conn.execute("UPDATE projects SET initial_idea = '' WHERE id = ?", (row["id"],))
    conn.commit()
    db.init_db()
    assert db.get_project(row["id"])["initial_idea"] == "the original words"


def test_backfill_does_not_overwrite_a_diverged_idea(project):
    worker._apply_report(project, _report(description="something else entirely"))
    db.init_db()
    assert _fresh(project)["initial_idea"] == "A tower that rolls dice."


# --- the prompt ------------------------------------------------------------

def test_prompt_carries_both_the_idea_and_the_description(project):
    worker._apply_report(project, _report(description="Now an ESP32 build."))
    prompt = agent_runner.build_prompt("build", _fresh(project))
    assert "A tower that rolls dice." in prompt
    assert "Now an ESP32 build." in prompt
    assert "original idea" in prompt


def test_prompt_does_not_print_the_same_text_twice(project):
    """Before the first rewrite the two are the same paragraph, and printing it
    under two headings invites the agent to treat them as two requirements."""
    prompt = agent_runner.build_prompt("build", project)
    assert prompt.count("A tower that rolls dice.") == 1
    assert "same as the original idea" in prompt


def test_prompt_announces_a_locked_description(project):
    db.update_project(project["id"], description_locked=1, description="held by Wes")
    prompt = agent_runner.build_prompt("build", _fresh(project))
    assert "LOCKED" in prompt


def test_prompt_announces_a_locked_title(project):
    db.update_project(project["id"], title_locked=1)
    prompt = agent_runner.build_prompt("build", _fresh(project))
    assert "do not propose a new title" in prompt


def test_contract_asks_for_a_description(project):
    assert '"description"' in agent_runner.build_prompt("build", project)


def test_prompt_survives_a_row_without_the_new_columns(project):
    """The meta-project row and older fixtures predate these columns."""

    class OldRow(dict):
        def __getitem__(self, key):
            if key in ("initial_idea", "description_locked", "title_locked"):
                raise IndexError(key)
            return dict.__getitem__(self, key)

    row = OldRow({k: project[k] for k in project.keys()})
    assert "Dice Tower" in agent_runner.build_prompt("build", row)


# --- locks -----------------------------------------------------------------

def test_locked_description_is_not_rewritten(project):
    db.update_project(project["id"], description_locked=1)
    worker._apply_report(_fresh(project), _report(description="agent's version"))
    assert _fresh(project)["description"] == "A tower that rolls dice."


def test_locked_title_is_not_renamed(project):
    db.update_project(project["id"], title_locked=1)
    worker._apply_report(_fresh(project), _report(title="Agent's Better Name"))
    assert _fresh(project)["title"] == "Dice Tower"


def test_unlocked_title_still_gets_renamed(project):
    worker._apply_report(project, _report(title="Agent's Better Name"))
    assert _fresh(project)["title"] == "Agent's Better Name"


def test_a_null_description_keeps_the_current_one(project):
    worker._apply_report(project, _report(description=None))
    assert _fresh(project)["description"] == "A tower that rolls dice."


def test_a_blank_description_keeps_the_current_one(project):
    worker._apply_report(project, _report(description="   "))
    assert _fresh(project)["description"] == "A tower that rolls dice."


# --- the edit form ---------------------------------------------------------

def test_saving_details_updates_title_and_description(client, project):
    client.post(
        f"/project/{project['slug']}/details",
        data={"title": "Dice Tower Mk2", "description": "Rewritten by Wes."},
    )
    row = _fresh(project)
    assert (row["title"], row["description"]) == ("Dice Tower Mk2", "Rewritten by Wes.")


def test_saving_details_sets_the_locks(client, project):
    client.post(
        f"/project/{project['slug']}/details",
        data={
            "title": "Dice Tower",
            "description": "Mine.",
            "title_locked": "on",
            "description_locked": "on",
        },
    )
    row = _fresh(project)
    assert (row["title_locked"], row["description_locked"]) == (1, 1)


def test_unchecked_boxes_clear_the_locks(client, project):
    db.update_project(project["id"], title_locked=1, description_locked=1)
    client.post(
        f"/project/{project['slug']}/details", data={"title": "Dice Tower", "description": "x"}
    )
    row = _fresh(project)
    assert (row["title_locked"], row["description_locked"]) == (0, 0)


def test_an_empty_description_can_be_cleared(client, project):
    client.post(f"/project/{project['slug']}/details", data={"title": "Dice Tower", "description": ""})
    assert _fresh(project)["description"] == ""


def test_an_empty_title_is_ignored_rather_than_wiping_the_name(client, project):
    client.post(f"/project/{project['slug']}/details", data={"title": "  ", "description": "x"})
    assert _fresh(project)["title"] == "Dice Tower"


def test_project_page_shows_the_editor_and_the_original_idea(client, project):
    worker._apply_report(project, _report(description="Now an ESP32 build."))
    html = client.get(f"/project/{project['slug']}").text
    assert 'action="/project/dice-tower/details"' in html
    assert "the original idea" in html
    assert "A tower that rolls dice." in html


def test_original_idea_is_hidden_while_it_still_matches(client, project):
    assert "the original idea" not in client.get(f"/project/{project['slug']}").text


# --- renaming the workspace ------------------------------------------------

def test_renaming_moves_the_directory_and_the_row(client, project):
    (config.PROJECTS_DIR / "dice-tower" / "PLAN.md").write_text("plan", encoding="utf-8")
    resp = client.post(
        f"/project/{project['slug']}/details",
        data={"title": "Dice Tower", "description": "x", "new_slug": "dice-tower-mk2"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/project/dice-tower-mk2"
    assert _fresh(project)["slug"] == "dice-tower-mk2"
    assert not (config.PROJECTS_DIR / "dice-tower").exists()
    assert (config.PROJECTS_DIR / "dice-tower-mk2" / "PLAN.md").read_text(encoding="utf-8") == "plan"


def test_renaming_is_journalled(client, project):
    client.post(
        f"/project/{project['slug']}/details",
        data={"title": "Dice Tower", "description": "x", "new_slug": "dice-tower-mk2"},
    )
    entries = [row["content_md"] for row in db.list_journal_asc(project["id"], limit=20)]
    assert any("Renamed workspace" in e for e in entries)


def test_a_free_text_name_is_slugified_rather_than_rejected(client, project):
    client.post(
        f"/project/{project['slug']}/details",
        data={"title": "x", "description": "x", "new_slug": "Dice Tower Mk 2!!"},
    )
    assert _fresh(project)["slug"] == "dice-tower-mk-2"


def test_renaming_onto_a_taken_slug_is_refused(client, project):
    db.create_project("Other", slug="other")
    resp = client.post(
        f"/project/{project['slug']}/details",
        data={"title": "x", "description": "x", "new_slug": "other"},
    )
    assert resp.status_code == 400
    assert _fresh(project)["slug"] == "dice-tower"


def test_renaming_onto_an_existing_directory_is_refused(client, project):
    """A stale directory with no project row behind it: moving onto it would
    merge two workspaces silently."""
    (config.PROJECTS_DIR / "leftovers").mkdir(parents=True)
    resp = client.post(
        f"/project/{project['slug']}/details",
        data={"title": "x", "description": "x", "new_slug": "leftovers"},
    )
    assert resp.status_code == 400
    assert (config.PROJECTS_DIR / "dice-tower").is_dir()


def test_renaming_under_a_running_agent_is_refused(client, project, monkeypatch):
    monkeypatch.setattr(db, "running_project_ids", lambda: {project["id"]})
    resp = client.post(
        f"/project/{project['slug']}/details",
        data={"title": "x", "description": "x", "new_slug": "dice-tower-mk2"},
    )
    assert resp.status_code == 409
    assert _fresh(project)["slug"] == "dice-tower"
    assert (config.PROJECTS_DIR / "dice-tower").is_dir()


def test_the_portal_project_cannot_be_renamed(client, temp_data_dir):
    row = db.create_project("Project Portal", slug=config.META_PROJECT_SLUG)
    (config.PROJECTS_DIR / row["slug"]).mkdir(parents=True, exist_ok=True)
    resp = client.post(
        f"/project/{row['slug']}/details",
        data={"title": "x", "description": "x", "new_slug": "something-else"},
    )
    assert resp.status_code == 400
    assert db.get_project(row["id"])["slug"] == config.META_PROJECT_SLUG


def test_a_traversing_slug_cannot_escape_the_projects_dir(client, project):
    resp = client.post(
        f"/project/{project['slug']}/details",
        data={"title": "x", "description": "x", "new_slug": "../../etc/passwd"},
    )
    # slugify strips the dots and slashes, so this lands as a plain child name
    # rather than being rejected - and nothing is written outside PROJECTS_DIR.
    assert resp.status_code == 200
    slug = _fresh(project)["slug"]
    assert "/" not in slug and ".." not in slug
    assert (config.PROJECTS_DIR / slug).resolve().parent == config.PROJECTS_DIR.resolve()


def test_submitting_the_same_slug_is_not_a_rename(client, project):
    client.post(
        f"/project/{project['slug']}/details",
        data={"title": "x", "description": "y", "new_slug": "dice-tower"},
    )
    assert (config.PROJECTS_DIR / "dice-tower").is_dir()
    entries = [row["content_md"] for row in db.list_journal_asc(project["id"], limit=20)]
    assert not any("Renamed workspace" in e for e in entries)


# --- the 08:29 note: interval 0, and a higher parallel ceiling -------------

def test_zero_interval_is_accepted(temp_data_dir):
    assert settings_form.apply({"worker_interval_min": "0"})["worker_interval_min"] == "0"


def test_a_negative_interval_still_falls_back(temp_data_dir):
    assert settings_form.apply({"worker_interval_min": "-5"})["worker_interval_min"] == "10"


def test_zero_interval_means_a_run_is_always_due(temp_data_dir):
    db.set_setting("worker_interval_min", "0")
    db.create_run(None, "build", "opus")
    assert worker._seconds_until_scheduled() == 0


def test_parallel_runs_can_go_above_eight(temp_data_dir):
    assert settings_form.apply({"max_parallel_runs": "12"})["max_parallel_runs"] == "12"


def test_parallel_runs_above_the_ceiling_fall_back(temp_data_dir):
    over = str(config.MAX_PARALLEL_LIMIT + 1)
    assert settings_form.apply({"max_parallel_runs": over})["max_parallel_runs"] == "2"


def test_settings_page_offers_the_full_parallel_range(client):
    html = client.get("/settings").text
    assert f'max="{config.MAX_PARALLEL_LIMIT}"' in html
    assert 'name="worker_interval_min" value="10" min="0"' in html

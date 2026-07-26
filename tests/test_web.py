"""Route + template smoke tests: every page renders and the new controls work.

Startup is bypassed (`raise_server_exceptions` aside, TestClient would launch
the worker and Telegram pollers) by driving the ASGI app with a client that
doesn't run lifespan events - the DB is already initialised by the fixture.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db


@pytest.fixture
def client(temp_data_dir):
    from app import main

    # Deliberately not used as a context manager: that would run the lifespan
    # startup hook, which spawns the worker and Telegram poll loops. The DB is
    # already initialised against the temp dir by the autouse fixture.
    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project("Test Project", description="A thing.", stage="active", build_approved=True, slug="test-project")


def test_dashboard_renders_banner_and_wordmark(client):
    body = client.get("/").text
    assert "PR<span class=\"o-blue\">O</span>JECT" in body
    assert "portal-blue" in body
    assert "portal-orange" in body


def test_dashboard_shows_project_cell(client, project):
    body = client.get("/").text
    assert "project-cell" in body
    assert "Test Project" in body


def test_dashboard_shows_question_badge_only_when_questions_are_open(client, project):
    assert "cell-alert" not in client.get("/").text
    db.create_question(project["id"], "Which colour?")
    body = client.get("/").text
    # The card folds to the Paused shelf while it waits on Wes, but the count
    # rides along on the card and the nav badge stays loud.
    assert "cell-alert" in body
    assert 'class="nav-count">1<' in body


def test_nav_shows_open_question_count(client, project):
    assert "nav-count" not in client.get("/").text
    db.create_question(project["id"], "Which colour?")
    assert "nav-count" in client.get("/").text


@pytest.mark.parametrize("path", ["/", "/questions", "/memory", "/settings"])
def test_pages_render(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert "terminal-window" in resp.text


def test_project_page_renders(client, project):
    resp = client.get(f"/project/{project['slug']}")
    assert resp.status_code == 200
    assert "Test Project" in resp.text


def test_settings_page_uses_a_model_dropdown(client):
    body = client.get("/settings").text
    assert '<select id="worker_model" name="worker_model">' in body
    for value, _ in config.MODEL_CHOICES:
        assert f'value="{value}"' in body


def test_project_page_offers_inherit_plus_every_model(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert "inherit (" in body
    for value, _ in config.MODEL_CHOICES:
        assert f'value="{value}"' in body


def test_setting_a_project_model(client, project):
    resp = client.post(f"/project/{project['slug']}/model", data={"model": "haiku"}, follow_redirects=False)
    assert resp.status_code == 303
    assert db.get_project(project["id"])["model"] == "haiku"


def test_clearing_a_project_model(client, project):
    db.update_project(project["id"], model="haiku")
    client.post(f"/project/{project['slug']}/model", data={"model": ""}, follow_redirects=False)
    assert db.get_project(project["id"])["model"] is None


def test_rejecting_an_unknown_project_model(client, project):
    resp = client.post(f"/project/{project['slug']}/model", data={"model": "gpt-9"}, follow_redirects=False)
    assert resp.status_code == 400
    assert db.get_project(project["id"])["model"] is None


def test_saving_settings_persists_toggles(client):
    client.post(
        "/settings",
        data={
            "worker_enabled": "on",
            "worker_model": "sonnet",
            "worker_interval_min": "5",
            "max_runs_per_day": "4",
            "run_timeout_min": "20",
            "telegram_token": "",
            "telegram_chat_id": "",
            "glados_mode": "on",
            "ntfy_url": "",
            "ntfy_topic": "",
        },
        follow_redirects=False,
    )
    assert db.get_setting("worker_model") == "sonnet"
    assert db.get_setting("glados_mode") == "1"
    # Unchecked checkboxes are simply absent from the POST body.
    assert db.get_setting("telegram_natural_language") == "0"


def _settings_form(**overrides):
    form = {
        "worker_enabled": "on",
        "worker_model": "opus",
        "worker_interval_min": "5",
        "max_runs_per_day": "4",
        "run_timeout_min": "20",
        "telegram_token": "",
        "telegram_chat_id": "",
        "ntfy_url": "",
        "ntfy_topic": "",
    }
    form.update(overrides)
    return form


def test_cost_units_round_trip_through_settings(client):
    client.post("/settings", data=_settings_form(cost_units="usd"), follow_redirects=False)
    assert db.get_setting("cost_units") == "usd"
    client.post("/settings", data=_settings_form(cost_units="weight"), follow_redirects=False)
    assert db.get_setting("cost_units") == "weight"


def test_unknown_cost_units_fall_back_to_the_default(client):
    client.post("/settings", data=_settings_form(cost_units="doubloons"), follow_redirects=False)
    assert db.get_setting("cost_units") == "weight"


def test_settings_page_offers_the_cost_units_dropdown(client):
    body = client.get("/settings").text
    assert 'name="cost_units"' in body
    assert "relative weight" in body


def test_saving_an_unknown_model_falls_back_to_the_default(client):
    client.post(
        "/settings",
        data={
            "worker_enabled": "on",
            "worker_model": "definitely-not-a-model",
            "worker_interval_min": "5",
            "max_runs_per_day": "4",
            "run_timeout_min": "20",
            "ntfy_url": "",
            "ntfy_topic": "",
        },
        follow_redirects=False,
    )
    assert db.get_setting("worker_model") == config.DEFAULT_MODEL


def test_adding_an_idea_creates_a_project(client):
    resp = client.post("/ideas", data={"title": "", "idea": "A dice tower"}, follow_redirects=False)
    assert resp.status_code == 303
    assert db.get_project_by_slug("a-dice-tower") is not None


def test_memory_page_shows_cli_auto_memory(client, monkeypatch, tmp_path):
    """The /memory page surfaces the CLI's own auto-memory read-only (#223)."""
    from app import climemory, config as cfg

    mem = tmp_path / "-home-ada-tabletop-online" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("# Memory Index\n- [fact](fact.md)\n", encoding="utf-8")
    (mem / "fact.md").write_text(
        '---\ndescription: a self-hosted thing\n---\n\nbody\n', encoding="utf-8"
    )
    monkeypatch.setattr(cfg, "cli_projects_dir", lambda: tmp_path)

    body = client.get("/memory").text
    assert "CLI auto-memory" in body
    assert "fact.md" in body
    assert "a self-hosted thing" in body
    # The file opens read-only through the dedicated route.
    resp = client.get("/memory/cli/-home-ada-tabletop-online/fact.md")
    assert resp.status_code == 200
    assert "body" in resp.text


def test_cli_memory_route_rejects_traversal(client, monkeypatch, tmp_path):
    from app import config as cfg

    (tmp_path / "-d" / "memory").mkdir(parents=True)
    (tmp_path / "-d" / "memory" / "n.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "secret.md").write_text("secret\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "cli_projects_dir", lambda: tmp_path)

    assert client.get("/memory/cli/-d/n.md").status_code == 200
    assert client.get("/memory/cli/-d/..%2F..%2Fsecret.md").status_code == 404

"""Dashboard sorting, the Telegram router model, and the terminology purge.

These are small, separate notes from Wes that share one thing: each is a place
where the UI said something he didn't want it to say, or wouldn't let him
change something he wanted to change.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db, nl, persona, settings_form


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _project(title, slug, stage="active", build_approved=True):
    return db.create_project(title, slug=slug, stage=stage, build_approved=build_approved)


def _grid(client, url="/"):
    """Just the Active section's grid. The idle-reason line above it names a
    project too, and comparing positions in the whole page picks that up
    instead."""
    html = client.get(url).text
    start = html.index('data-status-zone="active"')
    return html[start:html.index("<h2>Review</h2>", start)]


# --- sorting ---------------------------------------------------------------

def test_recency_sort_puts_the_freshly_touched_one_first(client):
    _project("Older", "older")
    newer = _project("Newer", "newer")
    db.update_project(newer["id"], description="just edited")
    html = _grid(client, "/?sort=recent")
    assert html.index("Newer") < html.index("Older")


def test_title_sort_ignores_recency(client):
    zed = _project("Zed", "zed")
    _project("Abel", "abel")
    db.update_project(zed["id"], description="just edited")
    html = _grid(client, "/?sort=title")
    assert html.index("Abel") < html.index("Zed")


def test_sort_choice_sticks_for_the_next_visit(client):
    _project("Zed", "zed")
    _project("Abel", "abel")

    client.get("/?sort=title")
    assert db.get_setting("dashboard_sort") == "title"
    html = _grid(client)  # no ?sort this time
    assert html.index("Abel") < html.index("Zed")


def test_unknown_sort_is_ignored_rather_than_stored(client):
    _project("A", "a")
    client.get("/?sort=title")
    client.get("/?sort=; DROP TABLE projects")
    assert db.get_setting("dashboard_sort") == "title"
    # And the projects table is, reassuringly, still there.
    assert len(db.list_projects()) == 1


def test_every_sort_name_renders_and_is_valid_sql(client):
    _project("A", "a")
    _project("B", "b")
    for name in config.PROJECT_SORTS:
        assert client.get(f"/?sort={name}").status_code == 200


def test_dashboard_shows_the_sort_links(client):
    html = client.get("/").text
    for _, (label, _sql) in config.PROJECT_SORTS.items():
        assert label in html


# --- terminology -----------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/questions", "/activity", "/memory", "/settings"])
def test_no_aperture_terminology_anywhere(client, path):
    html = client.get(path).text.lower()
    for phrase in ("enrichment center", "test chamber", "aperture"):
        assert phrase not in html


def test_backlog_is_the_stored_name_now(client):
    _project("Idea", "idea", stage="backlog")
    html = client.get("/").text
    assert "backlog" in html
    # Stored name = displayed name under the state model.
    assert db.get_project_by_slug("idea")["stage"] == "backlog"


def test_status_badge_leaves_unknown_values_readable():
    assert config.status_badge("paused") == "paused"
    assert config.status_badge("something_new") == "something new"


def test_dashboard_has_a_favicon(client):
    assert "favicon.svg" in client.get("/").text
    assert client.get("/static/favicon.svg").status_code == 200


# --- Telegram router model -------------------------------------------------

def test_router_model_defaults_to_sonnet():
    assert nl.router_model() == "sonnet"
    assert config.DEFAULT_SETTINGS["telegram_model"] == "sonnet"


def test_router_model_is_independent_of_the_agent_model():
    db.set_setting("worker_model", "opus")
    db.set_setting("telegram_model", "haiku")
    assert nl.router_model() == "haiku"
    assert db.get_setting("worker_model") == "opus"


def test_unknown_stored_model_falls_back_rather_than_reaching_claude():
    # A junk value here would fail every single message, so it is not trusted.
    db.set_setting("telegram_model", "gpt-9")
    assert nl.router_model() == config.TELEGRAM_MODEL


def test_set_router_model_rejects_unknown_names():
    db.set_setting("telegram_model", "sonnet")
    assert nl.set_router_model("gpt-9") is None
    assert db.get_setting("telegram_model") == "sonnet"


def test_set_router_model_accepts_a_known_name():
    assert nl.set_router_model("Haiku") == "haiku"
    assert nl.router_model() == "haiku"


def test_settings_page_offers_the_telegram_model(client):
    html = client.get("/settings").text
    assert 'name="telegram_model"' in html
    assert "telegram_model" in html


def test_saving_notifications_does_not_touch_the_agent_model(client):
    db.set_setting("worker_model", "opus")
    client.post("/settings", data={
        "_fields": "telegram_natural_language,telegram_model,glados_mode,telegram_token,"
                   "telegram_chat_id,ntfy_url,ntfy_topic",
        "telegram_model": "haiku",
        "ntfy_url": "http://x",
        "ntfy_topic": "portal",
    })
    assert db.get_setting("telegram_model") == "haiku"
    assert db.get_setting("worker_model") == "opus"


def test_telegram_model_is_in_the_settings_registry():
    assert "telegram_model" in settings_form.REGISTRY
    cleaned = settings_form.apply({"telegram_model": "nonsense"}, "telegram_model")
    assert cleaned == {"telegram_model": config.TELEGRAM_MODEL}


def test_model_persona_lines_render():
    for key in ("model_current", "model_set", "model_unknown"):
        for voice in (persona.PLAIN, persona.GLADOS):
            assert persona.say(key, voice=voice, model="sonnet", options="opus, sonnet", value="x")


# --- installed-to-home-screen (Wes's 14:39 note) ---------------------------
# On a phone, added to the home screen, the portal runs chromeless: no reload
# button and no browser pull-to-refresh, which leaves a stale page with no way
# to update it. These pin the two halves of the fix.

def test_pages_link_a_web_manifest(client):
    html = client.get("/").text
    assert 'rel="manifest"' in html
    assert "manifest.webmanifest" in html


def test_the_manifest_is_served_and_is_standalone(client):
    resp = client.get("/static/manifest.webmanifest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["display"] == "standalone"
    assert data["start_url"] == "/"


def test_pull_to_refresh_is_standalone_only():
    """In an ordinary browser tab the platform already does this, and two
    refreshes fighting over one drag reads as broken."""
    js = (config.BASE_DIR / "app" / "static" / "app.js").read_text()
    assert "initPullToRefresh" in js
    assert "display-mode: standalone" in js
    assert "navigator.standalone" in js


def test_pull_to_refresh_only_triggers_from_the_top():
    js = (config.BASE_DIR / "app" / "static" / "app.js").read_text()
    assert "window.scrollY > 0" in js
    assert "location.reload()" in js

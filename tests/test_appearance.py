"""Appearance settings, the simplified status picker, and the model list."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project(temp_data_dir):
    return db.create_project("Test Project", stage="active", build_approved=True, slug="test-project")


SETTINGS_FORM = {
    "worker_model": "opus",
    "worker_interval_min": "10",
    "max_runs_per_day": "8",
    "run_timeout_min": "30",
}


# --- CRT layers ------------------------------------------------------------

def body_class_set(client) -> set[str]:
    """The <body> classes as a set. Asserting on the set rather than the exact
    string keeps these tests from breaking every time a new appearance layer is
    added - which is what happened when typeface and density landed."""
    from app import main

    return set(main.body_classes().split())


def test_defaults_render_as_body_classes(client):
    body = client.get("/").text
    for expected in ("scan-all", "glow-prose", "anim-on", "font-mono", "density-comfortable"):
        assert expected in body_class_set(client)
        assert expected in body


def test_each_layer_can_be_turned_off_independently(client):
    client.post("/settings", data={**SETTINGS_FORM, "crt_scanlines": "off", "crt_glow": "all",
                                   "crt_animations": "on"})
    classes = body_class_set(client)
    assert {"scan-off", "glow-all", "anim-on"} <= classes


def test_scanlines_can_be_limited_to_chrome(client):
    client.post("/settings", data={**SETTINGS_FORM, "crt_scanlines": "chrome",
                                   "crt_glow": "prose", "crt_animations": "off"})
    body = client.get("/").text
    assert "scan-chrome" in body
    assert "anim-off" in body


def test_unknown_appearance_value_falls_back_to_the_default(client):
    db.set_setting("crt_glow", "sparkles")
    from app import main

    assert main.appearance()["crt_glow"] == config.APPEARANCE_DEFAULTS["crt_glow"]
    assert "glow-prose" in client.get("/").text


def test_posting_a_bogus_value_is_not_stored(client):
    client.post("/settings", data={**SETTINGS_FORM, "crt_scanlines": "everywhere-plus"})
    assert db.get_setting("crt_scanlines") == config.APPEARANCE_DEFAULTS["crt_scanlines"]


def test_settings_page_offers_every_layer(client):
    body = client.get("/settings").text
    for key in config.APPEARANCE_CHOICES:
        assert f'name="{key}"' in body


def test_stylesheet_defines_each_layer_class():
    css = (config.BASE_DIR / "app" / "static" / "style.css").read_text()
    for selector in ["body.scan-all::before", "body.scan-chrome", "body.glow-off", "body.anim-off"]:
        assert selector in css
    # The unconditional glow moved onto the glow-* classes; a stray declaration
    # on `body` itself would make the "off" setting a lie.
    assert "  text-shadow: 0 0 5px var(--terminal-glow);\n  overflow-x" not in css


# --- Day reset hour --------------------------------------------------------

def test_reset_hour_round_trips_through_settings(client):
    client.post("/settings", data={**SETTINGS_FORM, "day_reset_hour": "3"})
    assert db.get_setting("day_reset_hour") == "3"


@pytest.mark.parametrize("bad", ["25", "-4", "five", ""])
def test_out_of_range_reset_hour_falls_back(client, bad):
    client.post("/settings", data={**SETTINGS_FORM, "day_reset_hour": bad})
    assert db.get_setting("day_reset_hour") == "5"


def test_usage_snapshot_reports_the_boundary(client):
    data = client.get("/api/usage").json()
    assert data["reset_hour"] == 5
    assert data["resets_at"].split("T")[1].startswith("05:00")
    assert 0 < data["resets_in_sec"] <= 24 * 3600


# --- Models ----------------------------------------------------------------

def test_fable_is_offered_and_opus_is_still_the_default():
    values = [value for value, _ in config.MODEL_CHOICES]
    assert values[0] == "opus"
    assert "fable" in values
    assert config.DEFAULT_MODEL == "opus"


def test_model_labels_name_a_version():
    labels = dict(config.MODEL_CHOICES)
    assert labels["opus"].startswith("Opus 5")
    assert labels["fable"].startswith("Fable 5")
    assert labels["sonnet"].startswith("Sonnet 5")
    assert labels["haiku"].startswith("Haiku 4.5")


def test_opus_alias_resolves_to_opus_5_at_the_cli():
    # CLI 2.1.215's own `opus` alias still bills claude-opus-4-8, so the portal
    # pins opus to the explicit Opus 5 id at the spawn boundary.
    assert config.cli_model("opus") == "claude-opus-5"
    assert "opus-4-8" not in config.cli_model("opus")


def test_cli_model_passes_through_current_aliases():
    # sonnet/haiku/fable resolve correctly through the CLI alias, so they are
    # handed to `claude --model` unchanged; unknown aliases pass through too.
    for alias in ("sonnet", "haiku", "fable"):
        assert config.cli_model(alias) == alias
    assert config.cli_model("whatever-new-model") == "whatever-new-model"


def test_project_can_be_switched_to_fable(client, project):
    client.post(f"/project/{project['slug']}/model", data={"model": "fable"})
    assert db.get_project_by_slug(project["slug"])["model"] == "fable"


def test_settings_accepts_fable_as_the_global_default(client):
    client.post("/settings", data={**SETTINGS_FORM, "worker_model": "fable"})
    assert db.get_setting("worker_model") == "fable"


# --- Status picker ---------------------------------------------------------

def test_picker_offers_every_state_and_nothing_else():
    values = [value for value, _ in config.status_choices("active")]
    assert "planning" not in values
    assert "needs_input" not in values
    assert values == config.USER_STATES


def test_every_offered_state_is_displayable():
    for current in config.USER_STATES:
        values = [value for value, _ in config.status_choices(current)]
        assert current in values


def test_labels_are_the_stored_names():
    """Stored name = displayed name now - no translation table to drift."""
    labels = dict(config.USER_STATE_CHOICES)
    assert labels == {v: v for v in config.USER_STATES}
    assert all(" - " not in label for label in labels.values())


def test_project_page_renders_the_shortlist_and_colorizes_it(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert "status-select status-active" in body
    assert ">paused</option>" in body
    assert "agent works on it" not in body
    # The old agent-driven statuses no longer exist to offer.
    assert 'data-opt-class="status-planning"' not in body


def test_control_bar_replaces_the_mismatched_inputs(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert "control-bar" in body
    # All four controls are now selects of the same shape, so no bare number
    # inputs should be left in the panel.
    assert 'type="number"' not in body.split("<h2>Agent console</h2>")[0]


def test_status_route_still_accepts_the_old_vocabulary(client, project):
    # Old bookmarks and pre-deploy pages post the old names; they normalize.
    client.post(f"/project/{project['slug']}/status", data={"status": "needs_input"})
    assert db.get_project_by_slug(project["slug"])["stage"] == "active"


# --- Offline overlay + ping ------------------------------------------------

def test_ping_is_cheap_and_public(client):
    resp = client.get("/api/ping")
    assert resp.status_code == 200
    assert resp.text == "pong"


def test_every_page_carries_the_offline_overlay(client, project):
    for path in ["/", "/questions", "/activity", "/memory", "/settings",
                 f"/project/{project['slug']}"]:
        assert 'id="offline-overlay"' in client.get(path).text

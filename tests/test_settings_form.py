"""Section-scoped settings saving, the save confirmation, and cache-busted
static URLs.

The bug these cover: Settings used to be one form bound to a handler with one
named parameter per setting. A form field the running handler didn't name was
silently dropped by FastAPI and the response was still 303, so a save that
wrote nothing was indistinguishable from one that worked.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db, settings_form


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


AGENT_FIELDS = "worker_enabled,worker_model,worker_interval_min,max_runs_per_day,run_timeout_min,day_reset_hour,cost_units"
APPEARANCE_FIELDS = "crt_scanlines,crt_glow,crt_animations,ui_font,ui_density"


# --- The registry ----------------------------------------------------------

def test_every_appearance_choice_is_a_known_field():
    """Adding an option to config must not require remembering to register it
    here too - the appearance fields are derived from config on purpose."""
    for key in config.APPEARANCE_CHOICES:
        assert key in settings_form.REGISTRY


def test_missing_declaration_means_every_known_key():
    """The legacy whole-page form (and older tests) post everything at once."""
    assert set(settings_form.declared_fields(None)) == set(settings_form.KNOWN_KEYS)


def test_unknown_declared_names_are_dropped():
    assert settings_form.declared_fields("crt_glow,not_a_setting") == ["crt_glow"]


def test_only_declared_fields_are_written():
    """The whole point: a form that doesn't own a field leaves it alone even
    when the browser happens to submit it."""
    values = settings_form.apply(
        {"crt_glow": "off", "telegram_token": "hunter2"}, "crt_glow"
    )
    assert values == {"crt_glow": "off"}


def test_absent_checkbox_in_a_declaring_form_means_off():
    assert settings_form.apply({}, "worker_enabled") == {"worker_enabled": "0"}


def test_self_review_checkbox_and_model_validate():
    assert settings_form.apply({"self_review": "on"}, "self_review") == {"self_review": "1"}
    assert settings_form.apply({}, "self_review") == {"self_review": "0"}
    assert settings_form.apply({"self_review_model": "opus"}, "self_review_model") == {
        "self_review_model": "opus"
    }
    assert settings_form.apply({"self_review_model": "junk"}, "self_review_model") == {
        "self_review_model": config.SELF_REVIEW_MODEL
    }


def test_absent_text_field_in_a_declaring_form_is_left_alone():
    """A checkbox's absence is meaningful; a text input's absence is not. If a
    declared text field simply isn't in the payload, writing "" would wipe a
    real value (e.g. a Telegram token) on an unrelated save."""
    assert settings_form.apply({}, "telegram_token") == {}


@pytest.mark.parametrize(
    "raw,expected",
    [("off", "off"), ("sparkles", config.APPEARANCE_DEFAULTS["crt_glow"]), ("", config.APPEARANCE_DEFAULTS["crt_glow"])],
)
def test_choice_values_are_validated(raw, expected):
    assert settings_form.apply({"crt_glow": raw}, "crt_glow")["crt_glow"] == expected


@pytest.mark.parametrize("raw,expected", [("25", "5"), ("-1", "5"), ("abc", "5"), ("0", "0"), ("23", "23")])
def test_reset_hour_is_clamped(raw, expected):
    assert settings_form.apply({"day_reset_hour": raw}, "day_reset_hour")["day_reset_hour"] == expected


@pytest.mark.parametrize("raw", ["", "-4", "nine"])
def test_interval_falls_back_rather_than_storing_junk(raw):
    assert settings_form.apply({"worker_interval_min": raw}, "worker_interval_min") == {
        "worker_interval_min": "10"
    }


def test_interval_zero_is_a_real_value_not_junk():
    """0 used to fall back to 10 with everything else non-numeric. Wes asked
    for it to mean "no pacing - start the next run as soon as a slot frees"."""
    assert settings_form.apply({"worker_interval_min": "0"}, "worker_interval_min") == {
        "worker_interval_min": "0"
    }


# --- End to end through the route ------------------------------------------

def test_saving_appearance_does_not_clobber_the_telegram_token(client):
    db.set_setting("telegram_token", "secret-token")
    client.post("/settings", data={
        "_fields": APPEARANCE_FIELDS,
        "crt_scanlines": "off", "crt_glow": "off", "crt_animations": "off",
        "ui_font": "hybrid", "ui_density": "compact",
    })
    assert db.get_setting("telegram_token") == "secret-token"
    assert db.get_setting("crt_scanlines") == "off"
    assert db.get_setting("ui_font") == "hybrid"


def test_saving_agent_settings_does_not_turn_off_glados(client):
    """glados_mode is a checkbox in a *different* section. Under the old
    whole-form handler its absence from this payload meant "unchecked"."""
    db.set_setting("glados_mode", "1")
    client.post("/settings", data={
        "_fields": AGENT_FIELDS,
        "worker_enabled": "on", "worker_model": "opus", "worker_interval_min": "10",
        "max_runs_per_day": "8", "run_timeout_min": "30", "day_reset_hour": "5",
        "cost_units": "weight",
    })
    assert db.get_setting("glados_mode") == "1"


def test_save_redirects_back_to_the_section_it_came_from(client):
    resp = client.post("/settings", data={
        "_section": "appearance", "_fields": APPEARANCE_FIELDS, "crt_glow": "off",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("#appearance")
    assert "saved=" in resp.headers["location"]


def flash_text(body: str) -> str:
    """The save-confirmation banner only. `crt_glow` appears all over the page
    as a field name, so asserting on the raw HTML would pass either way."""
    if 'class="flash ok"' not in body:
        return ""
    return body.split('class="flash ok"', 1)[1].split("</p>", 1)[0]


def test_the_page_says_which_settings_it_saved(client):
    flash = flash_text(client.get("/settings?saved=crt_glow,ui_font").text)
    assert "saved" in flash
    assert "crt_glow" in flash and "ui_font" in flash


def test_no_banner_when_nothing_was_saved(client):
    assert flash_text(client.get("/settings").text) == ""


def test_banner_lists_only_the_keys_that_were_written(client):
    resp = client.post("/settings", data={
        "_section": "appearance", "_fields": "crt_glow", "crt_glow": "off",
    }, follow_redirects=True)
    flash = flash_text(resp.text)
    assert "crt_glow" in flash
    assert "telegram_token" not in flash


def test_a_bogus_saved_key_is_not_echoed_back(client):
    """`saved` comes off the query string, so it is attacker-controlled text
    being rendered into the page - filter it to known keys rather than trusting
    escaping alone."""
    body = client.get("/settings?saved=<script>alert(1)</script>").text
    assert "<script>alert(1)</script>" not in body
    assert flash_text(body) == ""


def test_appearance_change_reaches_the_body_of_every_page(client):
    client.post("/settings", data={
        "_fields": APPEARANCE_FIELDS,
        "crt_scanlines": "off", "crt_glow": "off", "crt_animations": "off",
        "ui_font": "sans", "ui_density": "compact",
    })
    for path in ("/", "/settings", "/activity", "/questions"):
        body = client.get(path).text
        assert "scan-off" in body and "font-sans" in body and "density-compact" in body


# --- Cache busting ---------------------------------------------------------

def test_static_urls_carry_a_version(client):
    from app import main

    url = main.static_url("style.css")
    assert url.startswith("/static/style.css?v=")
    assert url.rsplit("=", 1)[1].isdigit()


def test_static_version_follows_the_file(client, tmp_path, monkeypatch):
    """A stylesheet edit has to change the URL, or a cached copy keeps painting
    the old look no matter what the settings say - which is exactly how the
    appearance settings first appeared to be broken. Uses a stand-in tree so
    the real style.css is never touched."""
    import os

    from app import main

    static = tmp_path / "fake-root" / "app" / "static"
    static.mkdir(parents=True)
    css = static / "style.css"
    css.write_text("body {}")
    monkeypatch.setattr(config, "BASE_DIR", tmp_path / "fake-root")

    os.utime(css, (1_000_000, 1_000_000))
    before = main.static_url("style.css")
    os.utime(css, (2_000_000, 2_000_000))
    after = main.static_url("style.css")

    assert before == "/static/style.css?v=1000000"
    assert after == "/static/style.css?v=2000000"


def test_missing_static_file_still_renders(client):
    from app import main

    assert main.static_url("nope.css") == "/static/nope.css"


def test_pages_reference_the_versioned_stylesheet(client):
    body = client.get("/").text
    assert "/static/style.css?v=" in body
    assert "/static/app.js?v=" in body


# --- Fast shutdown ---------------------------------------------------------
# Background loops used to be orphaned on SIGTERM: the worker sat in a sleep of
# up to its configured interval and the Telegram poller in a 30s long poll, so
# systemd's stop step waited on them. Since the portal restarts itself after
# every self-improving run, that wait was downtime on the live site.

def test_shutdown_cancels_the_background_loops(temp_data_dir):
    import asyncio

    from app import main

    async def scenario():
        async def forever():
            await asyncio.sleep(3600)

        main._BACKGROUND_TASKS.clear()
        tasks = [asyncio.create_task(forever()) for _ in range(2)]
        main._BACKGROUND_TASKS.extend(tasks)
        await main.on_shutdown()
        return tasks

    tasks = asyncio.run(scenario())
    assert all(task.cancelled() for task in tasks)
    assert main._BACKGROUND_TASKS == []


def test_shutdown_does_not_hang_on_a_task_that_ignores_cancellation(temp_data_dir):
    """A future loop that swallows CancelledError must not be able to hold the
    service's stop open - that is the failure mode being fixed, so it fails
    loudly in the log rather than silently waiting."""
    import asyncio
    import time

    from app import main

    async def scenario():
        async def stubborn():
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    pass  # deliberately uncooperative

        main._BACKGROUND_TASKS.clear()
        main._BACKGROUND_TASKS.append(asyncio.create_task(stubborn()))
        started = time.monotonic()
        await main.on_shutdown()
        return time.monotonic() - started

    # The grace period is 3s; assert it is bounded rather than unbounded.
    assert asyncio.run(scenario()) < 6


def test_lifecycle_through_the_test_client(temp_data_dir):
    """Startup registers exactly the five loops - worker, Telegram poller,
    usage-limit poller, tailnet poller and the preview server - and leaving
    the context stops them. The real path, not just the handler in isolation."""
    from app import main

    with TestClient(main.app) as running:
        running.get("/api/ping")
        assert len(main._BACKGROUND_TASKS) == 5
    assert main._BACKGROUND_TASKS == []


# --- Page structure --------------------------------------------------------

def test_settings_page_renders_all_panels(client):
    body = client.get("/settings").text
    for panel in ("agent", "appearance", "notifications", "people", "access"):
        assert f'id="panel-{panel}"' in body
        assert f'data-panel="{panel}"' in body


def test_every_declared_field_in_the_page_is_a_real_setting(client):
    """The `_fields` strings are hand-written in the template. A typo there
    would silently stop that setting saving - which is the exact class of bug
    this whole change exists to make impossible."""
    import re

    body = client.get("/settings").text
    declared = re.findall(r'name="_fields" value="([^"]+)"', body)
    assert declared, "no section declared any fields"
    for group in declared:
        for name in group.split(","):
            assert name in settings_form.REGISTRY, f"{name} is not a known setting"


def test_the_sections_between_them_cover_every_setting(client):
    """Nothing should be strandable: if a setting has a control on the page it
    must belong to exactly one section, and no section may claim it twice."""
    import re

    body = client.get("/settings").text
    names = [n for g in re.findall(r'name="_fields" value="([^"]+)"', body) for n in g.split(",")]
    assert len(names) == len(set(names)), "a setting is claimed by two sections"
    # Everything except the internal bookkeeping keys, which have no control.
    internal = {"backoff_until", "last_reflect_date", "bonus_runs_count", "bonus_runs_date"}
    assert set(settings_form.KNOWN_KEYS) - set(names) - internal == set()


def test_credentials_start_folded_once_configured(client):
    db.set_setting("telegram_token", "abc123")
    body = client.get("/settings").text
    fold = body.split("Telegram credentials", 1)[0]
    assert "<details class=\"fold\" >" in fold or "<details class=\"fold\">" in fold


def test_credentials_start_open_when_unset(client):
    db.set_setting("telegram_token", "")
    body = client.get("/settings").text
    assert 'class="fold" open' in body


def test_test_notification_is_its_own_form(client):
    """It used to be a formaction button inside the settings form, so clicking
    it submitted every setting on the page as a side effect."""
    body = client.get("/settings").text
    assert 'action="/settings/test-notification"' in body
    assert "formaction" not in body

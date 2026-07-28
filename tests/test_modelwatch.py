"""Noticing a new model the day it ships.

Wes, 2026-07-25: "it should also be able to detect new models being added under
API's or subscriptions it is using."

The source is `GET https://api.anthropic.com/v1/models` with the CLI's own
OAuth token (probed live on 2026-07-25: 200, with `id` / `display_name` /
`created_at` per model). These tests pin the parsing, the once-only
announcement, the deliberately silent first seed, the fail-open posture, and
the fact that the watcher never changes which model the portal actually spawns.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

from app import config, db, modelwatch, notify, settings_form, worker
from app.main import app

# A shape-faithful slice of the real payload, including the keys the parser is
# supposed to ignore.
PAYLOAD = {
    "data": [
        {
            "type": "model",
            "id": "claude-opus-5",
            "display_name": "Claude Opus 5",
            "created_at": "2026-07-24T00:00:00Z",
            "capabilities": {"thinking": {"supported": True}},
        },
        {
            "type": "model",
            "id": "claude-sonnet-5",
            "display_name": "Claude Sonnet 5",
            "created_at": "2026-06-29T00:00:00Z",
        },
    ],
    "has_more": False,
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _models(payload=PAYLOAD):
    return modelwatch.parse_models(payload)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def test_parse_keeps_the_three_fields_that_matter():
    models = _models()
    assert models[0] == {
        "id": "claude-opus-5",
        "display_name": "Claude Opus 5",
        "created_at": "2026-07-24T00:00:00Z",
    }
    assert [m["id"] for m in models] == ["claude-opus-5", "claude-sonnet-5"]


def test_parse_drops_entries_with_no_usable_id():
    # A blank id in the seen-set would swallow the next real model that
    # happened to parse badly.
    payload = {"data": [{"id": ""}, {"id": None}, {"nope": 1}, "junk", {"id": "  ok  "}]}
    assert [m["id"] for m in modelwatch.parse_models(payload)] == ["ok"]


def test_parse_falls_back_to_the_id_when_there_is_no_display_name():
    assert modelwatch.parse_models({"data": [{"id": "claude-x"}]})[0]["display_name"] == "claude-x"


def test_parse_survives_a_payload_of_the_wrong_shape():
    for junk in (None, [], "text", {}, {"data": "nope"}, {"data": {}}):
        assert modelwatch.parse_models(junk) == []


# --------------------------------------------------------------------------
# The catalog and what counts as new
# --------------------------------------------------------------------------

def test_the_first_check_seeds_silently():
    # Eleven notifications on the day the feature ships would be eleven reasons
    # to mute the twelfth, which is the real one.
    result = modelwatch.check(_models())
    assert result["ok"] and result["seeded"] is True
    assert result["new"] == []
    assert modelwatch.seen_ids() == {"claude-opus-5", "claude-sonnet-5"}


def test_a_model_that_appears_later_is_new_exactly_once():
    modelwatch.check(_models())  # seed
    later = _models() + [{
        "id": "claude-opus-6", "display_name": "Claude Opus 6",
        "created_at": "2026-11-01T00:00:00Z",
    }]
    first = modelwatch.check(later)
    assert first["seeded"] is False
    assert [m["id"] for m in first["new"]] == ["claude-opus-6"]
    # Checked again with the same list: nothing new, so nothing said.
    assert modelwatch.check(later)["new"] == []


def test_two_new_models_are_reported_newest_first():
    modelwatch.check(_models())
    later = _models() + [
        {"id": "a", "display_name": "A", "created_at": "2026-08-01T00:00:00Z"},
        {"id": "b", "display_name": "B", "created_at": "2026-09-01T00:00:00Z"},
    ]
    assert [m["id"] for m in modelwatch.check(later)["new"]] == ["b", "a"]


def test_a_model_disappearing_from_the_list_is_not_forgotten():
    # Retirement is not news, and re-listing a retired id must not announce it
    # a second time - the seen-set only ever grows.
    modelwatch.check(_models())
    modelwatch.check([m for m in _models() if m["id"] != "claude-sonnet-5"])
    assert "claude-sonnet-5" in modelwatch.seen_ids()
    assert modelwatch.check(_models())["new"] == []


def test_the_catalog_records_the_last_successful_fetch():
    modelwatch.check(_models())
    cat = modelwatch.catalog()
    assert [m["id"] for m in cat["models"]] == ["claude-opus-5", "claude-sonnet-5"]
    assert cat["fetched_at"]


def test_a_corrupt_stored_catalog_reads_as_empty_not_as_a_crash():
    db.set_setting(modelwatch.CATALOG_KEY, "{not json")
    db.set_setting(modelwatch.SEEN_KEY, "[]")
    assert modelwatch.catalog() == {"models": [], "fetched_at": ""}
    assert modelwatch.seen_ids() == set()


# --------------------------------------------------------------------------
# Fetching: fails open, every time
# --------------------------------------------------------------------------

def test_no_credentials_is_an_ordinary_state_not_an_error(monkeypatch):
    monkeypatch.setattr(
        modelwatch.limits, "read_token", lambda path=None: {"token": "", "error": "not logged in"}
    )
    out = modelwatch.fetch_models()
    assert out == {"ok": False, "error": "not logged in"}


def test_an_expired_token_is_never_refreshed_from_here(monkeypatch):
    # Refreshing rotates the token in the file every spawned `claude -p` reads.
    monkeypatch.setattr(
        modelwatch.limits, "read_token",
        lambda path=None: {"token": "t", "expired": True, "error": ""},
    )
    assert modelwatch.fetch_models()["ok"] is False


def test_an_http_error_reports_the_code_and_nothing_else(monkeypatch):
    monkeypatch.setattr(
        modelwatch.limits, "read_token", lambda path=None: {"token": "t", "error": ""}
    )

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 503, "nope", {}, None)

    monkeypatch.setattr(modelwatch.urllib.request, "urlopen", boom)
    out = modelwatch.fetch_models()
    assert out["ok"] is False and "503" in out["error"]


def test_a_request_carries_the_oauth_beta_header_and_the_real_user_agent(monkeypatch):
    # Without the beta header the endpoint rejects a bearer token; without the
    # real claude-code User-Agent, requests land in a punitive rate-limit
    # bucket (learned the hard way on the usage endpoint).
    monkeypatch.setattr(
        modelwatch.limits, "read_token", lambda path=None: {"token": "tok", "error": ""}
    )
    captured = {}

    class FakeResponse:
        def read(self):
            return json.dumps(PAYLOAD).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.headers)
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(modelwatch.urllib.request, "urlopen", fake_urlopen)
    out = modelwatch.fetch_models()
    assert out["ok"] and len(out["models"]) == 2
    lowered = {k.lower(): v for k, v in captured["headers"].items()}
    assert lowered["authorization"] == "Bearer tok"
    assert lowered["anthropic-beta"] == modelwatch.limits.OAUTH_BETA
    assert "claude-c" in lowered["user-agent"]  # claude-cli/claude-code, whatever the CLI calls itself
    assert captured["url"].startswith("https://api.anthropic.com/v1/models")


def test_an_empty_payload_is_a_failure_not_an_empty_catalog(monkeypatch):
    # Storing an empty list would wipe the seen-set's meaning on the next
    # check and re-announce every model that came back.
    monkeypatch.setattr(modelwatch, "parse_models", lambda payload: [])
    monkeypatch.setattr(
        modelwatch.limits, "read_token", lambda path=None: {"token": "t", "error": ""}
    )

    class FakeResponse:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(modelwatch.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert modelwatch.fetch_models()["ok"] is False


# --------------------------------------------------------------------------
# What it does with the news
# --------------------------------------------------------------------------

def test_the_announcement_names_the_id_and_the_release_date():
    title, body = modelwatch.announcement(
        {"id": "claude-opus-6", "display_name": "Claude Opus 6",
         "created_at": "2026-11-01T00:00:00Z"}
    )
    assert title == "New model available: Claude Opus 6"
    assert "`claude-opus-6`" in body
    assert "2026-11-01" in body


def test_a_new_model_files_a_one_tap_question_on_the_meta_project(monkeypatch):
    project = db.create_project(title="Project Portal", description="", kind="software")
    db.update_project(project["id"], slug=config.META_PROJECT_SLUG)
    monkeypatch.setattr(config, "META_PROJECT_SLUG", db.get_project(project["id"])["slug"])

    sent = []

    async def fake_notify(title, message, **kw):
        sent.append((title, message, kw))

    monkeypatch.setattr(notify, "notify", fake_notify)
    asyncio.run(modelwatch.announce(
        {"id": "claude-opus-6", "display_name": "Claude Opus 6", "created_at": ""}
    ))

    open_qs = db.open_questions()
    assert len(open_qs) == 1
    assert "Claude Opus 6" in open_qs[0]["question"]
    # One tap either way, rather than typing an answer at the bot.
    assert json.loads(open_qs[0]["quick_options"]) == modelwatch.QUESTION_OPTIONS
    # And it reached the phone, carrying the question so a reply can route back.
    assert sent and sent[0][2]["question_id"] == open_qs[0]["id"]


def test_a_failed_notification_still_leaves_the_question_filed(monkeypatch):
    project = db.create_project(title="Project Portal", description="", kind="software")
    db.update_project(project["id"], slug=config.META_PROJECT_SLUG)
    monkeypatch.setattr(config, "META_PROJECT_SLUG", db.get_project(project["id"])["slug"])

    async def boom(*a, **k):
        raise RuntimeError("ntfy is down")

    monkeypatch.setattr(notify, "notify", boom)
    asyncio.run(modelwatch.announce({"id": "x", "display_name": "X", "created_at": ""}))
    assert len(db.open_questions()) == 1


def test_announcing_with_no_meta_project_still_notifies(monkeypatch):
    sent = []

    async def fake_notify(title, message, **kw):
        sent.append(title)

    monkeypatch.setattr(notify, "notify", fake_notify)
    asyncio.run(modelwatch.announce({"id": "x", "display_name": "X", "created_at": ""}))
    assert sent == ["New model available: X"]
    assert db.open_questions() == []


def test_the_watcher_never_changes_which_model_runs_spawn(monkeypatch):
    # A new id on the list is NOT proof the CLI can spawn it - Opus 5 sat on
    # this list for a day while `claude --model opus` still billed 4.8. So
    # adoption is a decision Wes takes, never a side effect of the check.
    project = db.create_project(title="Project Portal", description="", kind="software")
    db.update_project(project["id"], slug=config.META_PROJECT_SLUG)
    monkeypatch.setattr(config, "META_PROJECT_SLUG", db.get_project(project["id"])["slug"])
    db.set_setting("worker_model", "haiku")

    async def fake_notify(*a, **k):
        return None

    monkeypatch.setattr(notify, "notify", fake_notify)
    modelwatch.check(_models())
    later = _models() + [{"id": "claude-opus-9", "display_name": "Claude Opus 9",
                          "created_at": "2027-01-01T00:00:00Z"}]
    asyncio.run(modelwatch.announce(modelwatch.check(later)["new"][0]))
    assert db.get_setting("worker_model") == "haiku"


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

def test_run_check_does_nothing_at_all_when_the_setting_is_off(monkeypatch):
    db.set_setting("model_watch", "0")
    called = []
    monkeypatch.setattr(modelwatch, "check", lambda *a, **k: called.append(1))
    out = asyncio.run(modelwatch.run_check())
    assert called == [] and out["ok"] is False


def test_run_check_is_on_by_default():
    assert modelwatch.enabled() is True
    db.set_setting("model_watch", "1")
    assert modelwatch.enabled() is True


def test_a_thrown_check_never_escapes_run_check(monkeypatch):
    def boom():
        raise RuntimeError("network on fire")

    monkeypatch.setattr(modelwatch, "check", boom)
    assert asyncio.run(modelwatch.run_check())["ok"] is False


def test_the_worker_checks_once_a_day(monkeypatch):
    calls = []

    async def fake_run_check():
        calls.append(1)
        return {"ok": True, "seeded": False, "new": [], "error": ""}

    monkeypatch.setattr(worker.modelwatch, "run_check", fake_run_check)
    monkeypatch.setattr(worker, "_model_checked_day", None, raising=False)
    asyncio.run(worker._daily_model_check())  # noqa: SLF001
    asyncio.run(worker._daily_model_check())  # noqa: SLF001
    assert calls == [1]


def test_a_failing_check_waits_for_tomorrow_rather_than_retrying_every_tick(monkeypatch):
    calls = []

    async def boom():
        calls.append(1)
        raise RuntimeError("down")

    monkeypatch.setattr(worker.modelwatch, "run_check", boom)
    monkeypatch.setattr(worker, "_model_checked_day", None, raising=False)
    asyncio.run(worker._daily_model_check())  # noqa: SLF001
    asyncio.run(worker._daily_model_check())  # noqa: SLF001
    assert calls == [1]


def test_the_setting_round_trips_through_the_settings_form():
    assert "model_watch" in settings_form.KNOWN_KEYS
    assert settings_form.REGISTRY["model_watch"].checkbox is True


def test_the_settings_page_shows_the_watch_toggle_and_the_catalog(client):
    modelwatch.check(_models())
    html = client.get("/settings").text
    assert 'name="model_watch"' in html
    assert "Watch for new models" in html
    assert "claude-opus-5" in html

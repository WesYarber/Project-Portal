"""Fable falling back to Opus when Fable's own weekly window runs out.

Wes (2026-07-23): "If individual Fable usage runs out, fall back to Opus until
Fable access returns." The usage endpoint reports Fable's weekly window as a
*scoped* limit in its `limits` array, separate from the account-wide windows;
`limits.parse` keeps those scoped windows out of `windows` (so pacing's holds
and boosts stay account-wide) and `limits.model_fallback` is the decision the
worker applies at every model resolution.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import agent_runner, db, limits, main

NOW = datetime(2026, 7, 23, 16, 0, 0, tzinfo=timezone.utc)


def payload(fable=85.0, five_hour=40.0, seven_day=50.0, now=NOW, fable_reset_hours=5):
    """A response shaped like the real 2026-07 /api/oauth/usage payload,
    including the `limits` array with a Fable-scoped weekly entry."""
    raw = {
        "five_hour": {
            "utilization": five_hour,
            "resets_at": (now + timedelta(hours=2)).isoformat(),
        },
        "seven_day": {
            "utilization": seven_day,
            "resets_at": (now + timedelta(hours=5)).isoformat(),
        },
        "seven_day_opus": None,
        "limits": [
            {"kind": "session", "group": "session", "percent": five_hour,
             "resets_at": (now + timedelta(hours=2)).isoformat(), "scope": None},
            {"kind": "weekly_all", "group": "weekly", "percent": seven_day,
             "resets_at": (now + timedelta(hours=5)).isoformat(), "scope": None},
            {"kind": "weekly_scoped", "group": "weekly", "percent": fable,
             "resets_at": (now + timedelta(hours=fable_reset_hours)).isoformat(),
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
             "is_active": True},
        ],
    }
    return {"ok": True, "raw": raw, "plan": "max", "tier": "default_claude_max_20x"}


def fresh_snapshot(**kwargs):
    """Parse against the real clock and store, so `cached()` reads it as fresh."""
    now = datetime.now(timezone.utc)
    return limits.store(limits.parse(payload(now=now, **kwargs), now=now))


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


# --------------------------------------------------------------------------
# Parsing the scoped windows
# --------------------------------------------------------------------------


def test_parse_reads_the_fable_scoped_window():
    snap = limits.parse(payload(fable=85.0), now=NOW)
    assert len(snap["scoped"]) == 1
    entry = snap["scoped"][0]
    assert entry["key"] == "weekly_fable"
    assert entry["model"] == "Fable"
    assert entry["label"] == "weekly (Fable)"
    assert entry["percent"] == 85.0
    assert entry["resets_in"] == "5h 00m"


def test_scoped_windows_stay_out_of_the_pacing_windows():
    """A single model's window filling up must not idle runs other models can
    still make - the holds/boost/spend-down layer only reads `windows`."""
    snap = limits.parse(payload(fable=100.0), now=NOW)
    assert {w["key"] for w in snap["windows"]} == {"five_hour", "seven_day"}
    assert limits.window(snap, "weekly_fable") is None
    # And the tightest (on-screen) window is still an account-wide one.
    assert snap["tightest"]["key"] in {"five_hour", "seven_day"}


def test_parse_survives_junk_in_the_limits_array():
    raw_payload = payload()
    raw_payload["raw"]["limits"] += [
        None, "what", {"kind": "weekly_scoped", "scope": {"model": {}}},
        {"kind": "weekly_scoped", "percent": "lots",
         "scope": {"model": {"display_name": "Opus"}}},
    ]
    snap = limits.parse(raw_payload, now=NOW)
    models = [w["model"] for w in snap["scoped"]]
    assert models == ["Fable", "Opus"]  # junk skipped, unparsable percent -> 0
    assert snap["scoped"][1]["percent"] == 0.0


def test_a_failed_fetch_still_carries_the_scoped_key():
    snap = limits.parse({"ok": False, "error": "down"}, now=NOW)
    assert snap["scoped"] == []


def test_cached_recomputes_scoped_countdowns():
    fresh_snapshot(fable=85.0, fable_reset_hours=5)
    entry = limits.scoped_window("fable")
    assert entry is not None
    assert 4.9 * 3600 <= entry["resets_in_sec"] <= 5 * 3600


# --------------------------------------------------------------------------
# The fallback decision
# --------------------------------------------------------------------------


def test_no_fallback_below_the_threshold():
    fresh_snapshot(fable=85.0)
    assert limits.model_fallback("fable") == ("fable", "")


def test_exhausted_fable_falls_back_to_opus_with_a_reason():
    fresh_snapshot(fable=98.0)
    model, why = limits.model_fallback("fable")
    assert model == "opus"
    assert "weekly (Fable)" in why and "98" in why and "back in" in why


def test_fallback_fails_open_on_a_stale_snapshot():
    """An outage at Anthropic's end must not reroute models on old data."""
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    snap = limits.parse(payload(fable=100.0, now=old), now=old)
    db.set_setting(limits.CACHE_KEY, __import__("json").dumps(snap))
    assert limits.cached()["stale"] is True
    assert limits.model_fallback("fable") == ("fable", "")


def test_fallback_fails_open_with_no_snapshot_at_all():
    assert limits.model_fallback("fable") == ("fable", "")


def test_models_without_a_fallback_are_untouched():
    fresh_snapshot(fable=100.0)
    assert limits.model_fallback("opus") == ("opus", "")
    assert limits.model_fallback("haiku") == ("haiku", "")


# --------------------------------------------------------------------------
# The worker actually using it
# --------------------------------------------------------------------------


def test_resolve_model_reroutes_an_exhausted_fable(temp_data_dir):
    db.set_setting("worker_model", "fable")
    fresh_snapshot(fable=99.0)
    assert agent_runner.configured_model(None) == "fable"
    assert agent_runner.resolve_model(None) == "opus"


def test_resolve_model_returns_fable_once_the_window_is_back(temp_data_dir):
    db.set_setting("worker_model", "fable")
    fresh_snapshot(fable=12.0)
    assert agent_runner.resolve_model(None) == "fable"


def test_research_bursts_fall_back_too(temp_data_dir):
    """A burst pinned to Fable with no Fable window left would only buy
    backoffs - it spends the weekly headroom on Opus instead."""
    fresh_snapshot(fable=99.0)
    assert agent_runner.resolve_model(None, "research") == "opus"


def test_a_per_project_pin_to_fable_is_rerouted_as_well(temp_data_dir):
    project = db.create_project("Thing", slug="thing", stage="active")
    db.update_project(project["id"], model="fable")
    fresh_snapshot(fable=99.0)
    assert agent_runner.resolve_model(db.get_project(project["id"])) == "opus"


@pytest.mark.asyncio
async def test_rate_limit_backoff_is_short_when_only_fable_is_full(monkeypatch):
    """A Fable-only limit mid-run must not idle the whole portal for an hour:
    the next spawn falls back to Opus, so the pause is minutes, not the flat
    60 - and the reason says what is happening."""
    from app import worker

    now = datetime.now(timezone.utc)
    snap = limits.parse(payload(fable=100.0, now=now), now=now)

    async def fake_refresh(*args, **kwargs):
        return limits.store(snap)

    monkeypatch.setattr(limits, "refresh_async", fake_refresh)
    until, why = await worker._rate_limit_backoff()
    assert (until - now) <= timedelta(minutes=6)
    assert "weekly (Fable)" in why and "fall back" in why


# --------------------------------------------------------------------------
# The dashboard saying it out loud
# --------------------------------------------------------------------------


def test_dashboard_shows_the_scoped_window_chip(client):
    fresh_snapshot(fable=85.0)
    html = client.get("/").text
    assert "weekly (Fable)" in html


def test_dashboard_announces_an_active_fallback(client):
    db.set_setting("worker_model", "fable")
    fresh_snapshot(fable=99.0)
    html = client.get("/").text
    assert "fallback-chip" in html
    assert "opus for now" in html


def test_dashboard_is_quiet_when_nothing_is_rerouted(client):
    db.set_setting("worker_model", "fable")
    fresh_snapshot(fable=40.0)
    assert "fallback-chip" not in client.get("/").text

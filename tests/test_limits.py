"""Reading Wes's real Claude usage windows (app/limits.py).

Nothing here touches the network: `fetch_raw` is the only function that does,
and everything downstream of it is a pure function over the payload, which is
the reason the module is split that way.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import db, limits

NOW = datetime(2026, 7, 21, 22, 0, 0, tzinfo=timezone.utc)


def payload(five_hour=2.0, seven_day=0.0, opus=None, five_reset=None, seven_reset=None, base=NOW):
    """A response shaped like the real one from /api/oauth/usage.

    `base` is the anchor for the reset times; pass the real now for tests that
    read the stored snapshot back through limits.cached(), whose reset-crossed
    staleness check compares against the real clock.
    """
    raw = {
        "five_hour": {
            "utilization": five_hour,
            "resets_at": (five_reset or base + timedelta(hours=3)).isoformat(),
        },
        "seven_day": {
            "utilization": seven_day,
            "resets_at": (seven_reset or base + timedelta(days=2)).isoformat(),
        },
        "seven_day_opus": None if opus is None else {
            "utilization": opus,
            "resets_at": (base + timedelta(days=2)).isoformat(),
        },
        "extra_usage": {"is_enabled": False},
    }
    return {"ok": True, "raw": raw, "plan": "max", "tier": "default_claude_max_20x"}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_parse_reads_both_windows():
    snap = limits.parse(payload(five_hour=41.0, seven_day=12.5), now=NOW)
    assert snap["ok"] is True
    session = limits.window(snap, "five_hour")
    weekly = limits.window(snap, "seven_day")
    assert session["percent"] == 41.0
    assert session["remaining_percent"] == 59.0
    assert session["resets_in_sec"] == 3 * 3600
    assert session["resets_in"] == "3h 00m"
    assert weekly["resets_in"] == "2d 0h"


def test_parse_skips_windows_the_account_does_not_report():
    """`seven_day_opus` is null for most plans; a null must not become a 0%
    window, which would read as "plenty of Opus left" on an account that has no
    separate Opus window at all."""
    snap = limits.parse(payload(), now=NOW)
    assert [w["key"] for w in snap["windows"]] == ["five_hour", "seven_day"]

    with_opus = limits.parse(payload(opus=88.0), now=NOW)
    assert limits.window(with_opus, "seven_day_opus")["percent"] == 88.0


def test_tightest_window_is_the_fullest_one():
    """Whichever window has least headroom is the one that will stop a run, so
    that is the headline figure - not the session window by default."""
    snap = limits.parse(payload(five_hour=10.0, seven_day=77.0), now=NOW)
    assert snap["tightest"]["key"] == "seven_day"
    assert snap["percent"] == 77.0


def test_parse_clamps_and_survives_junk():
    snap = limits.parse(
        {"ok": True, "raw": {"five_hour": {"utilization": 140.0, "resets_at": "not a date"},
                             "seven_day": {"utilization": "?", "resets_at": None}}},
        now=NOW,
    )
    session = limits.window(snap, "five_hour")
    assert session["percent"] == 100.0
    assert session["resets_at"] == "" and session["resets_in"] == ""
    assert limits.window(snap, "seven_day")["percent"] == 0.0


def test_parse_of_a_failure_still_returns_a_usable_shape():
    snap = limits.parse({"ok": False, "error": "boom"}, now=NOW)
    assert snap["ok"] is False and snap["error"] == "boom"
    assert snap["windows"] == []


@pytest.mark.parametrize(
    "seconds,expected",
    [(None, ""), (0, "now"), (30, "1m"), (90, "1m"), (600, "10m"),
     (3600, "1h 00m"), (3660, "1h 01m"), (86400, "1d 0h"), (200000, "2d 7h")],
)
def test_humanize_until(seconds, expected):
    assert limits.humanize_until(seconds) == expected


# --------------------------------------------------------------------------
# Backing off against the real reset
# --------------------------------------------------------------------------


def test_backoff_waits_for_the_window_that_is_actually_full():
    """The old rule was a flat hour whatever happened. A session window with 20
    minutes left on it should cost 20 minutes of idleness, not 60."""
    snap = limits.parse(
        payload(five_hour=100.0, five_reset=NOW + timedelta(minutes=20)), now=NOW
    )
    assert limits.backoff_until(snap, now=NOW) == NOW + timedelta(minutes=20)


def test_backoff_ignores_a_window_that_is_not_full():
    """A run can fail for reasons other than the window it happens to be
    nearest to; only a window at 95%+ is evidence about when to come back."""
    snap = limits.parse(payload(five_hour=40.0, seven_day=5.0), now=NOW)
    assert limits.backoff_until(snap, now=NOW) == NOW + timedelta(minutes=60)


def test_backoff_is_capped_when_the_weekly_window_is_the_full_one():
    """Waiting out a weekly reset would idle the portal for days. Come back in
    six hours and let the next attempt find out instead."""
    snap = limits.parse(
        payload(seven_day=100.0, seven_reset=NOW + timedelta(days=3)), now=NOW
    )
    assert limits.backoff_until(snap, now=NOW) == NOW + timedelta(hours=6)


def test_backoff_takes_the_earliest_full_window():
    snap = limits.parse(
        payload(five_hour=99.0, seven_day=100.0,
                five_reset=NOW + timedelta(minutes=45),
                seven_reset=NOW + timedelta(days=3)),
        now=NOW,
    )
    assert limits.backoff_until(snap, now=NOW) == NOW + timedelta(minutes=45)


def test_backoff_falls_back_to_the_flat_hour_with_no_snapshot():
    assert limits.backoff_until({}, now=NOW) == NOW + timedelta(minutes=60)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def test_read_token_missing_file_is_not_an_exception(tmp_path):
    creds = limits.read_token(tmp_path / "nope.json")
    assert creds["token"] == "" and "not logged in" in creds["error"]


def test_read_token_reads_the_cli_credentials(tmp_path):
    path = tmp_path / "creds.json"
    future_ms = (datetime.now(timezone.utc) + timedelta(hours=5)).timestamp() * 1000
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-x", "expiresAt": future_ms,
        "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x",
    }}))
    creds = limits.read_token(path)
    assert creds["token"] == "sk-ant-oat01-x"
    assert creds["expired"] is False
    assert creds["plan"] == "max"


def test_an_expired_token_is_reported_not_refreshed(tmp_path):
    """Refreshing rotates the refresh token, and this process shares the file
    with every `claude -p` the portal spawns. Never touch it."""
    path = tmp_path / "creds.json"
    past_ms = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp() * 1000
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": past_ms}}))
    assert limits.read_token(path)["expired"] is True

    result = limits.fetch_raw(path=path)
    assert result["ok"] is False and "expired" in result["error"]


def test_fetch_raw_does_not_call_out_without_credentials(tmp_path, monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("tried to reach the network with no token")

    monkeypatch.setattr(limits.urllib.request, "urlopen", explode)
    assert limits.fetch_raw(path=tmp_path / "nope.json")["ok"] is False


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------


def test_cached_with_nothing_stored_is_a_clean_miss():
    snap = limits.cached()
    assert snap["ok"] is False and snap["stale"] is True and snap["windows"] == []


def test_store_and_read_back():
    limits.store(limits.parse(payload(five_hour=33.0, base=datetime.now(timezone.utc))))
    snap = limits.cached()
    assert snap["ok"] is True
    assert limits.window(snap, "five_hour")["percent"] == 33.0
    assert snap["stale"] is False


def test_cached_recomputes_countdowns_against_now():
    """A stored snapshot's 'resets in 3h' is only true at the moment it was
    taken; served an hour later it must not still claim three hours."""
    old = limits.parse(payload(five_reset=NOW + timedelta(hours=3)), now=NOW)
    old["fetched_at"] = NOW.isoformat(timespec="seconds")
    db.set_setting(limits.CACHE_KEY, json.dumps(old))

    snap = limits.cached(max_age_sec=10**9)
    fresh = limits.window(snap, "five_hour")["resets_in_sec"]
    live = int((NOW + timedelta(hours=3) - datetime.now(timezone.utc)).total_seconds())
    assert fresh == max(0, live)


def test_the_poll_presents_itself_as_the_real_cli(monkeypatch):
    """An unrecognized User-Agent lands this endpoint's requests in a punitive
    rate-limit bucket, so the poll must look like the genuine Claude CLI."""
    import io
    import urllib.request as urlreq

    captured = {}

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.headers)
        return FakeResponse(json.dumps({"five_hour": {"utilization": 1.0}}).encode())

    monkeypatch.setattr(
        limits, "read_token", lambda path=None: {"token": "tok", "error": "", "expired": False}
    )
    monkeypatch.setattr(urlreq, "urlopen", fake_urlopen)

    result = limits.fetch_raw()
    assert result["ok"] is True
    # urllib title-cases header keys.
    ua = captured["headers"].get("User-agent", "")
    assert ua.startswith("claude-cli/")
    assert "(external, cli)" in ua


def test_the_user_agent_carries_the_installed_cli_version():
    from app import config

    ua = config.usage_user_agent()
    assert ua == f"claude-cli/{config.cli_version()} (external, cli)"
    # The version is a real dotted number, not the literal placeholder.
    assert config.cli_version()[0].isdigit()


def test_a_window_that_has_reset_makes_the_reading_stale(monkeypatch):
    """Once a resets_at has passed, the stored utilization describes a window
    that no longer exists (it rolled toward zero), so the reading must not be
    trusted no matter how young it is - a fresh fetch just took over."""
    now = datetime.now(timezone.utc)
    snap = limits.parse(payload(base=now))
    # Fetched one second ago, but its weekly window reset a minute back.
    snap["fetched_at"] = now.isoformat(timespec="seconds")
    for w in snap["windows"]:
        if w["key"] == "seven_day":
            w["resets_at"] = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    db.set_setting(limits.CACHE_KEY, json.dumps(snap))

    read = limits.cached()
    assert read["ok"] is True
    assert read["reset_crossed"] is True
    assert read["stale"] is True


def test_a_future_reset_reads_fresh():
    snap = limits.parse(payload(base=datetime.now(timezone.utc)))
    snap["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.set_setting(limits.CACHE_KEY, json.dumps(snap))
    read = limits.cached()
    assert read["reset_crossed"] is False
    assert read["stale"] is False


@pytest.mark.asyncio
async def test_the_poller_refetches_the_instant_a_window_rolls_over(monkeypatch):
    """A young snapshot would normally be left alone, but a passed reset makes
    its percentages wrong, so the poller must refresh without waiting out the
    rest of the interval."""
    import asyncio

    now = datetime.now(timezone.utc)
    snap = limits.parse(payload(base=now))
    snap["fetched_at"] = now.isoformat(timespec="seconds")  # brand new
    for w in snap["windows"]:
        w["resets_at"] = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    db.set_setting(limits.CACHE_KEY, json.dumps(snap))

    calls = []

    async def fake_refresh(*args, **kwargs):
        calls.append(1)
        # Replace with a fresh reading whose windows are back in the future.
        return limits.store(limits.parse(payload(base=datetime.now(timezone.utc))))

    monkeypatch.setattr(limits, "refresh_async", fake_refresh)
    task = asyncio.create_task(limits.poll_loop(interval_sec=0, startup_delay_sec=0))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls  # it refetched despite the snapshot being seconds old


def test_a_stale_snapshot_is_flagged_rather_than_dropped():
    old = limits.parse(payload())
    old["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    db.set_setting(limits.CACHE_KEY, json.dumps(old))
    snap = limits.cached()
    assert snap["ok"] is True and snap["stale"] is True


def test_a_failed_fetch_does_not_blank_a_good_snapshot():
    limits.store(limits.parse(payload(five_hour=50.0)))
    kept = limits.store(limits.parse({"ok": False, "error": "network down"}))
    assert kept["ok"] is True
    assert limits.window(kept, "five_hour")["percent"] == 50.0
    assert kept["last_error"] == "network down"


def test_a_failure_is_stored_when_there_is_nothing_better():
    stored = limits.store(limits.parse({"ok": False, "error": "not logged in"}))
    assert stored["ok"] is False
    assert limits.cached()["error"] == "not logged in"


def test_cached_survives_a_corrupt_blob():
    db.set_setting(limits.CACHE_KEY, "{not json")
    assert limits.cached()["ok"] is False


# --------------------------------------------------------------------------
# What the rest of the portal does with it
# --------------------------------------------------------------------------


@pytest.fixture
def client(temp_data_dir):
    from starlette.testclient import TestClient
    from app import main

    # No context manager: that would run the lifespan hook and start the
    # worker, the Telegram poller and the limits poller against a temp DB.
    return TestClient(main.app)


def test_dashboard_shows_the_real_windows(client):
    limits.store(limits.parse(payload(five_hour=41.0, seven_day=12.0), now=NOW))
    body = client.get("/").text
    assert "claude-limits" in body
    assert "session" in body and "weekly" in body
    assert "41%" in body


def test_dashboard_says_nothing_loud_when_there_is_no_reading(client):
    body = client.get("/").text
    assert "claude limits: unavailable" in body
    assert "limit-chip" not in body


def test_a_hot_window_is_marked(client):
    limits.store(limits.parse(payload(five_hour=91.0), now=NOW))
    assert "limit-chip hot" in client.get("/").text


def test_api_limits_serves_the_cache_without_fetching(client, monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("/api/limits fetched without ?refresh=1")

    monkeypatch.setattr(limits, "fetch_raw", explode)
    limits.store(limits.parse(payload(five_hour=7.0), now=NOW))
    body = client.get("/api/limits").json()
    assert body["ok"] is True
    assert body["windows"][0]["percent"] == 7.0


def test_api_limits_refresh_forces_a_read(client, monkeypatch):
    monkeypatch.setattr(limits, "fetch_raw", lambda *a, **k: payload(five_hour=64.0))
    body = client.get("/api/limits?refresh=1").json()
    assert body["windows"][0]["percent"] == 64.0
    assert limits.cached()["ok"] is True  # and it was stored


def test_usage_snapshot_carries_the_limits(client):
    from app import main

    limits.store(limits.parse(payload(five_hour=5.0), now=NOW))
    assert main.usage_snapshot()["limits"]["ok"] is True


@pytest.mark.asyncio
async def test_worker_backs_off_to_the_real_reset(monkeypatch):
    """The whole point of #25: a session window with 12 minutes left costs 12
    minutes of idleness, not the flat hour the worker used to guess."""
    from app import worker

    soon = datetime.now(timezone.utc) + timedelta(minutes=12)
    snap = limits.parse(payload(five_hour=100.0, five_reset=soon))

    async def fake_refresh(*args, **kwargs):
        return limits.store(snap)

    monkeypatch.setattr(limits, "refresh_async", fake_refresh)
    until, why = await worker._rate_limit_backoff()
    assert abs((until - soon).total_seconds()) < 2
    assert "session" in why
    assert db.get_setting("backoff_until")
    assert worker._in_backoff() is True


@pytest.mark.asyncio
async def test_worker_backoff_survives_an_unreadable_account(monkeypatch):
    from app import worker

    async def boom(*args, **kwargs):
        raise RuntimeError("no network")

    monkeypatch.setattr(limits, "refresh_async", boom)
    until, why = await worker._rate_limit_backoff()
    expected = datetime.now(timezone.utc) + timedelta(minutes=60)
    assert abs((until - expected).total_seconds()) < 5
    assert "flat 60 min" in why


@pytest.mark.asyncio
async def test_the_poller_fetches_when_the_cache_is_cold_and_then_leaves_it_alone(monkeypatch):
    """One fetch on a cold cache, none on the next tick - the fetch is gated on
    the snapshot's age, not on the tick."""
    import asyncio

    calls = []

    async def fake_refresh(*args, **kwargs):
        calls.append(1)
        return limits.store(limits.parse(payload(base=datetime.now(timezone.utc))))

    monkeypatch.setattr(limits, "refresh_async", fake_refresh)
    task = asyncio.create_task(limits.poll_loop(interval_sec=0, startup_delay_sec=0))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls == [1]


@pytest.mark.asyncio
async def test_the_poller_waits_before_its_first_fetch(monkeypatch):
    """The portal restarts itself on every self-update; a burst of restarts
    must not become a burst of requests to the usage endpoint."""
    import asyncio

    calls = []

    async def fake_refresh(*args, **kwargs):  # pragma: no cover - must not run
        calls.append(1)
        return {}

    monkeypatch.setattr(limits, "refresh_async", fake_refresh)
    task = asyncio.create_task(limits.poll_loop(interval_sec=0, startup_delay_sec=30))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls == []

"""The in-portal Claude login (app/claudelogin.py): the CLI's /login flow with
the Settings page standing in for the terminal.

The exchange itself is faked everywhere - only a real person at claude.ai can
mint a real code - so what these tests pin is everything around it: the PKCE
arithmetic, the state check that stops a stale paste completing a fresh
attempt, and above all the credentials file coming out in exactly the shape
the CLI writes, with nothing that was already in it lost.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import urllib.parse
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import claudelogin, main


@pytest.fixture(autouse=True)
def isolated_login(tmp_path, monkeypatch):
    """No test may read or write the machine's real ~/.claude/.credentials.json."""
    monkeypatch.setattr(claudelogin, "CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr(claudelogin, "_pending", None)
    monkeypatch.setattr(claudelogin, "_last_result", None)
    yield


@pytest.fixture
def client():
    return TestClient(main.app)


def token_response(**extra):
    out = {
        "access_token": "sk-ant-oat01-new",
        "refresh_token": "sk-ant-ort01-new",
        "expires_in": 28800,
        "scope": "user:profile user:inference",
    }
    out.update(extra)
    return out


# ---------------------------------------------------------------- begin/pending

def test_begin_builds_the_cli_login_url():
    live = claudelogin.begin(now=1000.0)
    parsed = urllib.parse.urlparse(live["url"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == claudelogin.AUTHORIZE_URL
    params = dict(urllib.parse.parse_qsl(parsed.query))
    assert params["client_id"] == claudelogin.CLIENT_ID
    assert params["response_type"] == "code"
    assert params["code"] == "true"
    assert params["redirect_uri"] == claudelogin.REDIRECT_URI
    assert params["scope"] == claudelogin.SCOPES
    assert params["state"] == live["state"]
    assert params["code_challenge_method"] == "S256"
    digest = hashlib.sha256(live["verifier"].encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert params["code_challenge"] == expected


def test_a_pending_login_ages_out():
    claudelogin.begin(now=1000.0)
    assert claudelogin.pending(now=1000.0 + claudelogin.PENDING_TTL_SEC - 1)
    assert claudelogin.pending(now=1000.0 + claudelogin.PENDING_TTL_SEC + 1) is None
    # And once aged out it stays gone, not flickering back.
    assert claudelogin.pending(now=1000.0) is None


def test_begin_again_replaces_the_first_attempt():
    first = claudelogin.begin(now=1000.0)
    second = claudelogin.begin(now=1001.0)
    assert claudelogin.pending(now=1002.0)["state"] == second["state"]
    assert first["state"] != second["state"]


# ---------------------------------------------------------------- parse_code

def test_parse_code_handles_what_people_actually_paste():
    assert claudelogin.parse_code("abc123#st_9") == ("abc123", "st_9")
    assert claudelogin.parse_code("abc123") == ("abc123", "")
    assert claudelogin.parse_code("  abc123#st_9\n") == ("abc123", "st_9")
    assert claudelogin.parse_code('"abc123#st_9"') == ("abc123", "st_9")
    assert claudelogin.parse_code("") == ("", "")


# ---------------------------------------------------------------- finish

def test_finish_exchanges_and_writes_the_credentials_file(tmp_path):
    path = tmp_path / "credentials.json"
    # What is already in the file (another login's leftovers + MCP tokens)
    # must survive the rewrite.
    path.write_text(json.dumps({
        "mcpOAuth": {"someServer": {"accessToken": "keep-me"}},
        "claudeAiOauth": {
            "accessToken": "old",
            "refreshToken": "old-refresh",
            "expiresAt": 1,
            "refreshTokenExpiresAt": 2,
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_20x",
            "scopes": ["user:inference"],
        },
    }))
    live = claudelogin.begin(now=1000.0)
    calls = {}

    def fake_post(url, payload, timeout):
        calls["url"], calls["payload"] = url, payload
        return 200, token_response()

    result = claudelogin.finish(
        f"thecode#{live['state']}", now=1000.0, path=path, post=fake_post
    )
    assert result["ok"] is True
    assert calls["url"] == claudelogin.TOKEN_URL
    assert calls["payload"]["grant_type"] == "authorization_code"
    assert calls["payload"]["code"] == "thecode"
    assert calls["payload"]["state"] == live["state"]
    assert calls["payload"]["code_verifier"] == live["verifier"]
    assert calls["payload"]["redirect_uri"] == claudelogin.REDIRECT_URI

    blob = json.loads(path.read_text())
    oauth = blob["claudeAiOauth"]
    assert oauth["accessToken"] == "sk-ant-oat01-new"
    assert oauth["refreshToken"] == "sk-ant-ort01-new"
    assert oauth["expiresAt"] == int((1000.0 + 28800) * 1000)
    assert oauth["scopes"] == ["user:profile", "user:inference"]
    # Fields the response did not re-state survive from the old blob...
    assert oauth["subscriptionType"] == "max"
    assert oauth["rateLimitTier"] == "default_claude_max_20x"
    # ...except the OLD refresh token's expiry, which would be a lie about
    # the NEW refresh token.
    assert "refreshTokenExpiresAt" not in oauth
    assert blob["mcpOAuth"] == {"someServer": {"accessToken": "keep-me"}}
    # Private to the account owner, like the CLI leaves it.
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # A completed login clears the pending slot.
    assert claudelogin.pending(now=1001.0) is None
    assert claudelogin.last_result()["ok"] is True


def test_finish_rejects_a_code_from_a_different_attempt(tmp_path):
    claudelogin.begin(now=1000.0)
    result = claudelogin.finish(
        "thecode#not-my-state", now=1000.0, path=tmp_path / "c.json",
        post=lambda *a: (_ for _ in ()).throw(AssertionError("must not exchange")),
    )
    assert result["ok"] is False
    assert "different login attempt" in result["error"]


def test_finish_without_a_pending_login_says_start_again(tmp_path):
    result = claudelogin.finish("thecode", now=1000.0, path=tmp_path / "c.json")
    assert result["ok"] is False
    assert "start again" in result["error"]


def test_finish_with_an_empty_paste_asks_for_the_code(tmp_path):
    claudelogin.begin(now=1000.0)
    result = claudelogin.finish("   ", now=1000.0, path=tmp_path / "c.json")
    assert result["ok"] is False
    assert "Paste the code" in result["error"]


def test_finish_surfaces_the_servers_own_words(tmp_path):
    live = claudelogin.begin(now=1000.0)
    result = claudelogin.finish(
        f"thecode#{live['state']}", now=1000.0, path=tmp_path / "c.json",
        post=lambda *a: (400, {"error": {"type": "invalid_grant",
                                         "message": "Authorization code expired"}}),
    )
    assert result["ok"] is False
    assert "400" in result["error"]
    assert "Authorization code expired" in result["error"]
    # A failed exchange keeps the pending login: the person can paste again
    # or reopen the same link without starting over.
    assert claudelogin.pending(now=1001.0) is not None


def test_finish_survives_the_network_being_down(tmp_path):
    live = claudelogin.begin(now=1000.0)

    def dead_post(url, payload, timeout):
        raise OSError("no route to host")

    result = claudelogin.finish(
        f"thecode#{live['state']}", now=1000.0, path=tmp_path / "c.json", post=dead_post
    )
    assert result["ok"] is False
    assert "no route to host" in result["error"]


def test_finish_writes_a_file_where_none_existed(tmp_path):
    live = claudelogin.begin(now=1000.0)
    path = tmp_path / "fresh" / "credentials.json"
    result = claudelogin.finish(
        f"thecode#{live['state']}", now=1000.0, path=path,
        post=lambda *a: (200, token_response(
            refresh_token_expires_in=2592000, subscriptionType="max")),
    )
    assert result["ok"] is True
    oauth = json.loads(path.read_text())["claudeAiOauth"]
    assert oauth["refreshTokenExpiresAt"] == int((1000.0 + 2592000) * 1000)
    assert oauth["subscriptionType"] == "max"


# ---------------------------------------------------------------- status

def test_status_missing_when_there_is_no_file(tmp_path):
    assert claudelogin.status(path=tmp_path / "nope.json")["state"] == "missing"


def write_creds(path: Path, *, expires_at_ms, refresh_at_ms=None, plan="max"):
    oauth = {"accessToken": "tok", "expiresAt": expires_at_ms, "subscriptionType": plan}
    if refresh_at_ms is not None:
        oauth["refreshTokenExpiresAt"] = refresh_at_ms
    path.write_text(json.dumps({"claudeAiOauth": oauth}))


def test_status_ok_while_the_access_token_is_fresh(tmp_path):
    path = tmp_path / "c.json"
    write_creds(path, expires_at_ms=2_000_000, refresh_at_ms=9_000_000)
    got = claudelogin.status(now=1000.0, path=path)
    assert got["state"] == "ok"
    assert got["plan"] == "max"
    assert got["refresh_expires_in"]  # humanized, non-empty


def test_status_stale_when_only_the_access_token_lapsed(tmp_path):
    path = tmp_path / "c.json"
    write_creds(path, expires_at_ms=500_000, refresh_at_ms=9_000_000)
    got = claudelogin.status(now=1000.0, path=path)
    assert got["state"] == "stale"
    assert "next run" in got["detail"]


def test_status_logged_out_when_the_refresh_token_lapsed_too(tmp_path):
    path = tmp_path / "c.json"
    write_creds(path, expires_at_ms=500_000, refresh_at_ms=600_000)
    got = claudelogin.status(now=1000.0, path=path)
    assert got["state"] == "logged_out"


# ---------------------------------------------------------------- the routes

def test_settings_page_shows_the_card_and_start_mints_a_link(client):
    page = client.get("/settings").text
    assert "Claude account" in page
    assert "no Claude login on file" in page

    resp = client.post("/settings/claude-login/start", follow_redirects=False)
    assert resp.status_code == 303
    live = claudelogin.pending()
    assert live is not None
    page = client.get("/settings").text
    assert "open the Claude login page" in page
    # The href is the real minted URL, not a template placeholder.
    assert live["url"].replace("&", "&amp;") in page

    client.post("/settings/claude-login/cancel", follow_redirects=False)
    assert claudelogin.pending() is None
    assert "open the Claude login page" not in client.get("/settings").text


def test_finish_route_reports_the_error_on_the_card(client, monkeypatch):
    client.post("/settings/claude-login/start", follow_redirects=False)
    monkeypatch.setattr(
        claudelogin, "_post_json",
        lambda *a: (400, {"error": {"type": "invalid_grant", "message": "nope"}}),
    )
    resp = client.post(
        "/settings/claude-login/finish", data={"code": "bad#wrong-state"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "different login attempt" in client.get("/settings").text

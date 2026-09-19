"""The ntfy publish credential: the `ntfy_token` setting and the header it becomes.

Why this exists. An ntfy server can be left open, where the topic name is the
whole of the authorization - anyone who learns it can read every notification
the portal has ever sent. Closing that door means the server demands a login to
publish, and the portal has to hold one.

The one non-obvious rule, and the reason most of this file: an EMPTY setting
must send no `Authorization` header at all, never a bare `Bearer `. ntfy reads a
malformed Authorization header as a failed login and answers 401, which turns
"this install has no credential" into "this install's credential was refused" -
the same failure with the wrong cause printed on it, in the one log line
anybody ever reads.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db, notify, settings_form, site


class _FakeResponse:
    def __init__(self, status: int = 200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Stands in for httpx.AsyncClient; records every POST `_send_ntfy` makes."""

    posts: list[dict] = []
    status: int = 200

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, content=None, headers=None, **k):
        _FakeClient.posts.append(
            {"url": url, "content": content, "headers": dict(headers or {})}
        )
        return _FakeResponse(_FakeClient.status)


@pytest.fixture
def posts(monkeypatch):
    _FakeClient.posts = []
    _FakeClient.status = 200
    monkeypatch.setattr(notify.httpx, "AsyncClient", _FakeClient)
    return _FakeClient.posts


# --- the header ------------------------------------------------------------

@pytest.mark.anyio
async def test_a_token_is_sent_as_a_bearer_header(posts):
    await notify._send_ntfy("http://ntfy.example:8095", "portal", "Title", "body", "tk_abc123")
    assert posts[0]["headers"]["Authorization"] == "Bearer tk_abc123"


@pytest.mark.anyio
async def test_no_token_means_no_authorization_header_at_all(posts):
    """Not a bare "Bearer ". See this module's docstring - ntfy answers 401 to a
    malformed header, so the empty case must be silent, not wrong."""
    await notify._send_ntfy("http://ntfy.example:8095", "portal", "Title", "body", "")
    assert "Authorization" not in posts[0]["headers"]


@pytest.mark.anyio
async def test_a_whitespace_only_token_is_no_token(posts):
    """A field cleared by selecting the value and hitting space, or a paste that
    brought a newline with it. Either way there is no credential here."""
    await notify._send_ntfy("http://ntfy.example:8095", "portal", "Title", "body", "   \n")
    assert "Authorization" not in posts[0]["headers"]


@pytest.mark.anyio
async def test_a_pasted_token_is_trimmed(posts):
    """A token copied out of a .env file usually arrives with a newline on it,
    and ntfy compares the header verbatim."""
    await notify._send_ntfy("http://ntfy.example:8095", "portal", "T", "body", " tk_abc123\n")
    assert posts[0]["headers"]["Authorization"] == "Bearer tk_abc123"


@pytest.mark.anyio
async def test_the_token_defaults_to_absent(posts):
    """Every caller in the tree passes one, but the parameter is optional so an
    older call site cannot start sending `Bearer None`."""
    await notify._send_ntfy("http://ntfy.example:8095", "portal", "T", "body")
    assert "Authorization" not in posts[0]["headers"]


@pytest.mark.anyio
async def test_the_title_header_still_rides_along_with_a_token(posts):
    await notify._send_ntfy("http://ntfy.example:8095", "portal", "Run finished", "body", "tk")
    headers = posts[0]["headers"]
    assert headers["Title"] == "Run finished"
    assert headers["Authorization"] == "Bearer tk"


@pytest.mark.anyio
async def test_the_message_and_url_are_unchanged_by_the_token(posts):
    await notify._send_ntfy("http://ntfy.example:8095/", "portal", "T", "body", "tk")
    assert posts[0]["url"] == "http://ntfy.example:8095/portal"
    assert posts[0]["content"] == b"body"


@pytest.mark.anyio
async def test_a_refused_credential_does_not_raise(posts):
    """Notifications are best effort. A 401 is logged and the rest of the send
    - web push, Telegram - still happens."""
    _FakeClient.status = 401
    await notify._send_ntfy("http://ntfy.example:8095", "portal", "T", "body", "wrong")
    assert posts  # it was attempted


@pytest.mark.anyio
async def test_no_url_or_topic_still_short_circuits(posts):
    await notify._send_ntfy("", "portal", "T", "body", "tk")
    await notify._send_ntfy("http://ntfy.example:8095", "", "T", "body", "tk")
    assert posts == []


# --- the wiring ------------------------------------------------------------

@pytest.mark.anyio
async def test_notify_hands_the_setting_to_the_sender(temp_data_dir, monkeypatch):
    """The header is useless if nothing reads the setting, and every other test
    here calls `_send_ntfy` directly - so this is the one that owns the line in
    `notify.notify` that looks the token up."""
    seen: list[str] = []

    async def fake_ntfy(url, topic, title, message, token=""):
        seen.append(token)

    monkeypatch.setattr(notify, "_send_ntfy", fake_ntfy)
    monkeypatch.setattr(notify.webpush, "push_to", _none_push)
    db.set_setting("ntfy_url", "http://ntfy.example:8095")
    db.set_setting("ntfy_topic", "portal")
    db.set_setting("ntfy_token", "tk_live")

    await notify.notify("Run finished", "all green")

    assert seen == ["tk_live"]


@pytest.mark.anyio
async def test_an_install_with_no_token_sends_an_empty_one(temp_data_dir, monkeypatch):
    seen: list[str] = []

    async def fake_ntfy(url, topic, title, message, token=""):
        seen.append(token)

    monkeypatch.setattr(notify, "_send_ntfy", fake_ntfy)
    monkeypatch.setattr(notify.webpush, "push_to", _none_push)
    db.set_setting("ntfy_url", "http://ntfy.example:8095")
    db.set_setting("ntfy_topic", "portal")

    await notify.notify("Run finished", "all green")

    assert seen == [""]


async def _none_push(*a, **k):
    return 0


# --- the setting -----------------------------------------------------------

def test_the_shipped_default_is_blank():
    """An open ntfy is still the common case, and a token nobody set must not
    become a header nobody meant."""
    assert config.DEFAULT_SETTINGS["ntfy_token"] == ""


def test_the_token_is_not_on_the_site_config():
    """`SITE` is a Jinja global on every page - see the comment on app/site.py's
    dataclass. `ntfy_url` and `ntfy_topic` are safe there; a credential is not,
    so this one lives only in the settings table."""
    assert not hasattr(site.SITE, "ntfy_token")
    assert "ntfy_token" not in site.defaults()


def test_the_form_accepts_and_trims_the_token():
    values = settings_form.apply({"ntfy_token": "  tk_abc  "}, "ntfy_token")
    assert values == {"ntfy_token": "tk_abc"}


def test_saving_another_section_leaves_the_token_alone():
    """The same guard that keeps Appearance from blanking the Telegram token."""
    values = settings_form.apply({"crt_glow": "off", "ntfy_token": "tk"}, "crt_glow")
    assert "ntfy_token" not in values


def test_the_notifications_section_declares_the_token(temp_data_dir):
    """The hidden `_fields` input is what makes a field savable at all: a name
    missing from it is dropped by `apply` and the page still answers 303."""
    from app import main

    with TestClient(main.app) as client:
        html = client.get("/settings").text
    declared = [line for line in html.splitlines() if 'name="_fields"' in line]
    assert any("ntfy_token" in line for line in declared)
    assert 'id="ntfy_token"' in html


def test_the_token_round_trips_through_the_settings_page(temp_data_dir):
    from app import main

    with TestClient(main.app) as client:
        resp = client.post(
            "/settings",
            data={
                "_fields": "ntfy_url,ntfy_topic,ntfy_token",
                "ntfy_url": "http://ntfy.example:8095",
                "ntfy_topic": "portal",
                "ntfy_token": "tk_roundtrip",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert db.get_setting("ntfy_token") == "tk_roundtrip"
        assert "tk_roundtrip" in client.get("/settings").text

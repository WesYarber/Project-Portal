"""Logging the portal's Claude subscription in from the portal itself.

The runs this portal schedules are paid for by the Claude Code subscription
login in `~/.claude/.credentials.json`. The CLI keeps that file fresh by
rotating the refresh token on its own - until the day it can't (the token is
revoked, or the rotation loses a race) and every run starts failing with
"OAuth session expired and could not be refreshed". The fix used to mean a
terminal: ssh in, run `claude /login`, copy the URL to a browser, paste the
code back. This module is that same flow with the portal as the terminal, so
it can be done from a phone.

It is the CLI's own OAuth dance, not a private one: the same client id, the
same scopes, the same PKCE exchange, and the result is written where the CLI
keeps it, in the shape the CLI writes - so a token minted here is
indistinguishable from one minted by `/login`, and the CLI happily refreshes
it from then on. The constants are read out of the installed CLI bundle, and
re-checked against it on 2026-09-02 when the CLI was updated to 2.1.258 for
Fable 5.1: the client id and both endpoints are byte-identical to 2.1.223's, so
a CLI update is not by itself a reason to re-derive them. If Anthropic moves an
endpoint, a login attempt fails with the server's own words on the card rather
than anything silent.

Flow, as the Settings card walks it:

1. `begin()` - mint a PKCE verifier and a state, remember them (in memory,
   single slot - this is a household portal, not a login farm), and hand back
   the authorize URL for the card to render as a link.
2. The person opens the link, signs in at claude.ai, and is handed a code to
   copy (`<code>#<state>` - the fragment IS the state, riding along).
3. `finish(pasted)` - check the state matches the one minted here (a paste
   from some other login attempt must not complete this one), exchange the
   code at the token endpoint with the verifier, and write the credentials
   file atomically, preserving everything in it that is not ours (`mcpOAuth`,
   and any claudeAiOauth fields the response did not re-state).

No secret ever renders: the card shows *when* tokens expire, never what they
are, and `finish` results carry the server's error text but no token.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from app.limits import humanize_until

log = logging.getLogger("portal.claudelogin")

# Read out of the installed CLI bundle - see the module docstring. claude.com's
# authorize URL is a 307 to this one, so the portal links straight here.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"

# Exactly the scopes the CLI's own subscription login produces (they are the
# scopes on file from Wes's last terminal `/login`). Asking for less would
# mint a token the CLI might refuse; asking for more would prompt for grants
# the runs never use.
SCOPES = (
    "user:profile user:inference user:sessions:claude_code "
    "user:mcp_servers user:file_upload"
)

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

# How long a minted login link stays usable. Long enough to walk to another
# device and sign in, short enough that a forgotten card does not hold a live
# verifier for days.
PENDING_TTL_SEC = 30 * 60

# One pending login at a time, module-level on purpose: the portal is one
# process, and a second `begin()` deliberately replaces the first (the old
# link simply stops being accepted, which is what you want after mis-clicking).
_pending: Optional[dict] = None

# The outcome of the last finish attempt, for the card to show once. Never
# holds a token - just ok/error and the server's words.
_last_result: Optional[dict] = None


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def begin(now: Optional[float] = None) -> dict:
    """Mint a fresh login link, replacing any pending one."""
    global _pending, _last_result
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    params = {
        # `code=true` is what makes the callback page SHOW the code for a
        # person to copy, instead of expecting a localhost listener the
        # portal's box does not have a browser to feed.
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
    }
    _pending = {
        "verifier": verifier,
        "state": state,
        "url": f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}",
        "created_at": time.time() if now is None else now,
    }
    _last_result = None
    return _pending


def pending(now: Optional[float] = None) -> Optional[dict]:
    """The live pending login, or None once it has aged out."""
    global _pending
    if _pending is None:
        return None
    now = time.time() if now is None else now
    if now - _pending["created_at"] > PENDING_TTL_SEC:
        _pending = None
        return None
    return _pending


def cancel() -> None:
    global _pending
    _pending = None


def last_result() -> Optional[dict]:
    return _last_result


def parse_code(pasted: str) -> tuple[str, str]:
    """`(code, state)` out of whatever was pasted.

    The callback page hands over `<code>#<state>`; people also paste the bare
    code, or the whole thing with whitespace from a phone's copy button.
    """
    text = (pasted or "").strip().strip('"').strip()
    if not text:
        return "", ""
    if "#" in text:
        code, _, state = text.partition("#")
        return code.strip(), state.strip()
    return text, ""


def _post_json(url: str, payload: dict, timeout: float) -> tuple[int, dict]:
    """POST JSON, return (status, parsed body). HTTP errors are read, not raised."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "project-portal"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        status = err.code
    try:
        parsed = json.loads(body) if body else {}
    except ValueError:
        parsed = {"raw": body[:500]}
    return status, parsed


def _server_words(body: dict) -> str:
    """The most human sentence a token-endpoint error body contains."""
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or "unknown error")
    if isinstance(err, str):
        detail = body.get("error_description")
        return f"{err}: {detail}" if detail else err
    return str(body.get("raw") or body or "unknown error")[:300]


def finish(
    pasted: str,
    *,
    now: Optional[float] = None,
    path: Optional[Path] = None,
    post: Optional[Callable[[str, dict, float], tuple[int, dict]]] = None,
    timeout: float = 30.0,
) -> dict:
    """Exchange a pasted code for tokens and write the credentials file.

    Returns `{"ok": True, ...}` or `{"ok": False, "error": "..."}`, and keeps
    the same dict in `last_result()` for the card. Never raises.
    """
    global _pending, _last_result
    live = pending(now)
    if live is None:
        _last_result = {
            "ok": False,
            "error": "This login link has expired - start again and use the fresh one.",
        }
        return _last_result
    code, state = parse_code(pasted)
    if not code:
        _last_result = {"ok": False, "error": "Paste the code the login page showed you."}
        return _last_result
    if state and state != live["state"]:
        _last_result = {
            "ok": False,
            "error": "That code belongs to a different login attempt - start again "
            "and use the newest link.",
        }
        return _last_result
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        # The endpoint wants the state even when it rode in on the fragment.
        "state": state or live["state"],
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": live["verifier"],
    }
    post = _post_json if post is None else post
    try:
        status_code, body = post(TOKEN_URL, payload, timeout)
    except Exception as err:  # network down, DNS, timeout
        _last_result = {"ok": False, "error": f"Could not reach {TOKEN_URL}: {err}"}
        return _last_result
    access = str(body.get("access_token") or "")
    if status_code != 200 or not access:
        log.warning("Claude login exchange failed (%s): %s", status_code, body)
        _last_result = {
            "ok": False,
            "error": f"The token exchange failed ({status_code}): {_server_words(body)}",
        }
        return _last_result
    try:
        write_credentials(body, now=now, path=path)
    except OSError as err:
        _last_result = {"ok": False, "error": f"Could not write the credentials file: {err}"}
        return _last_result
    _pending = None
    _last_result = {"ok": True, "plan": str(body.get("subscriptionType") or "")}
    return _last_result


def write_credentials(
    tokens: dict, *, now: Optional[float] = None, path: Optional[Path] = None
) -> None:
    """Write the token response into the CLI's credentials file, atomically.

    Everything in the file that is not ours survives (`mcpOAuth`, and any
    claudeAiOauth fields the response did not re-state, like
    `subscriptionType` - the CLI's own next refresh keeps those honest).
    """
    path = CREDENTIALS_PATH if path is None else path
    now = time.time() if now is None else now
    existing: dict[str, Any] = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    old = existing.get("claudeAiOauth")
    blob: dict[str, Any] = dict(old) if isinstance(old, dict) else {}
    blob["accessToken"] = tokens["access_token"]
    if tokens.get("refresh_token"):
        blob["refreshToken"] = tokens["refresh_token"]
    expires_in = float(tokens.get("expires_in") or 0)
    blob["expiresAt"] = int((now + expires_in) * 1000)
    scope = str(tokens.get("scope") or "")
    if scope:
        blob["scopes"] = scope.split()
    elif "scopes" not in blob:
        blob["scopes"] = SCOPES.split()
    # A fresh refresh token's own expiry is only known if the server said so.
    # A stale figure from the OLD token would read as "logged out" wrongly,
    # so absent an answer the field goes rather than lies.
    refresh_in = tokens.get("refresh_token_expires_in")
    if refresh_in:
        blob["refreshTokenExpiresAt"] = int((now + float(refresh_in)) * 1000)
    elif tokens.get("refresh_token"):
        blob.pop("refreshTokenExpiresAt", None)
    for key in ("subscriptionType", "rateLimitTier"):
        if tokens.get(key):
            blob[key] = tokens[key]
    existing["claudeAiOauth"] = blob
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".credentials-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(existing, handle, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def status(now: Optional[float] = None, path: Optional[Path] = None) -> dict:
    """What the Settings card says about the login on file. Carries no secret."""
    path = CREDENTIALS_PATH if path is None else path
    now = time.time() if now is None else now
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        oauth = blob.get("claudeAiOauth") or {}
    except (OSError, ValueError):
        oauth = {}
    token = oauth.get("accessToken") or ""
    if not token:
        return {"state": "missing", "detail": "no Claude login on file"}
    expires_at = float(oauth.get("expiresAt") or 0) / 1000.0
    refresh_at = float(oauth.get("refreshTokenExpiresAt") or 0) / 1000.0
    plan = str(oauth.get("subscriptionType") or "")
    out = {
        "plan": plan,
        "expires_in": humanize_until(max(0, int(expires_at - now))) if expires_at else "",
        "refresh_expires_in": (
            humanize_until(max(0, int(refresh_at - now))) if refresh_at else ""
        ),
    }
    if refresh_at and refresh_at < now:
        out.update(
            state="logged_out",
            detail="the login has fully expired - runs fail until someone logs in",
        )
    elif expires_at and expires_at < now:
        out.update(
            state="stale",
            detail="access token expired; the CLI refreshes it on the next run",
        )
    else:
        out.update(state="ok", detail="logged in")
    return out

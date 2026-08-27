"""Wes's real Claude usage limits, read from the account rather than guessed.

Until now the portal only knew about the budget it invents for itself - N runs
per portal-day - and it learned about the *real* limit the only way it could:
by running into it, seeing "You've hit your session limit" in a run's output,
and backing off a flat 60 minutes. That flat hour was a guess, and it was
usually wrong in the expensive direction (a session window that had eleven
minutes left cost an hour of idleness) or the useless one (a weekly limit that
resets on Thursday is not fixed by waiting an hour).

Claude Code's own `/usage` screen reads `GET /api/oauth/usage` with the OAuth
access token the CLI already stores in `~/.claude/.credentials.json`. That
endpoint reports, for each window, how much of it is spent and when it comes
back. This module reads exactly that, and nothing else.

Two deliberate non-behaviors:

* **It never refreshes the token.** The refresh flow rotates the refresh token,
  and this process shares the credentials file with the CLI that the portal
  spawns for every run - a rotation raced against a live `claude -p` would take
  out the thing the whole portal is built on. When the access token has expired
  we report it as stale and wait for the CLI to refresh it in the ordinary
  course of a run.
* **It never fetches from inside a request handler.** The snapshot the UI reads
  is a cache in the settings table, refreshed by a background poller. A page
  render is not allowed to depend on api.anthropic.com being up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app import config, db, spawnauth

log = logging.getLogger("portal.limits")

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The same beta header Claude Code sends; without it the endpoint rejects an
# OAuth bearer token outright.
OAUTH_BETA = "oauth-2025-04-20"

# Where the cached snapshot lives. Settings rather than a new table because
# there is exactly one current value and every reader wants only that one.
CACHE_KEY = "claude_limits_json"

# How stale a cached snapshot may be before the poller replaces it. Five
# minutes: the windows being tracked are five hours and seven days long, so
# minute-level freshness buys nothing, and this is a network call on someone
# else's server.
POLL_INTERVAL_SEC = 300

# The windows worth showing. `seven_day_opus` is reported separately by the API
# and is the one that usually bites first on a Max plan, so it is not folded in
# with the all-model weekly figure.
WINDOWS = (
    ("five_hour", "session"),
    ("seven_day", "weekly"),
    ("seven_day_opus", "weekly (Opus)"),
)


# ---------------------------------------------------------------------------
# Reading the account
# ---------------------------------------------------------------------------


def read_token(path: Optional[Path] = None) -> dict:
    """The stored OAuth credentials, as {token, expires_at, expired, error}.

    Never raises: a missing or unreadable credentials file is an ordinary state
    for a portal running somewhere the CLI has not been logged in.
    """
    path = path or CREDENTIALS_PATH
    try:
        blob = json.loads(path.read_text())
    except FileNotFoundError:
        return {"token": "", "error": "not logged in (no ~/.claude/.credentials.json)"}
    except (OSError, ValueError) as exc:
        return {"token": "", "error": f"unreadable credentials: {exc}"}

    oauth = blob.get("claudeAiOauth") or {}
    token = oauth.get("accessToken") or ""
    if not token:
        return {"token": "", "error": "credentials file has no access token"}

    # `expiresAt` is milliseconds since the epoch.
    try:
        expires_at = float(oauth.get("expiresAt") or 0) / 1000.0
    except (TypeError, ValueError):
        expires_at = 0.0
    return {
        "token": token,
        "expires_at": expires_at,
        # A minute of slack: a token about to expire is not worth a round trip.
        "expired": bool(expires_at and expires_at < time.time() + 60),
        "plan": oauth.get("subscriptionType") or "",
        "tier": oauth.get("rateLimitTier") or "",
        "error": "",
    }


def fetch_raw(timeout: float = 15.0, path: Optional[Path] = None) -> dict:
    """Hit the usage endpoint once. Blocking; call it off the event loop."""
    creds = read_token(path)
    if creds.get("error"):
        return {"ok": False, "error": creds["error"]}
    if creds.get("expired"):
        # Deliberately not refreshing - see the module docstring.
        return {"ok": False, "error": "access token expired; waiting for the CLI to refresh it"}

    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {creds['token']}",
            "anthropic-beta": OAUTH_BETA,
            "Accept": "application/json",
            # Must match the real Claude Code client; a made-up User-Agent lands
            # this endpoint's requests in a punitive rate-limit bucket.
            "User-Agent": config.usage_user_agent(),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"usage endpoint returned {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"could not reach the usage endpoint: {exc}"}

    return {"ok": True, "raw": payload, "plan": creds.get("plan", ""), "tier": creds.get("tier", "")}


# ---------------------------------------------------------------------------
# Turning it into something the portal can decide with
# ---------------------------------------------------------------------------


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def humanize_until(seconds: Optional[int]) -> str:
    """'3h 40m' / '2d 4h'. Coarse on purpose - these are windows, not timers."""
    if seconds is None:
        return ""
    if seconds <= 0:
        return "now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{max(1, minutes)}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _window(raw: dict, key: str, label: str, now: datetime) -> Optional[dict]:
    block = raw.get(key)
    if not isinstance(block, dict):
        return None
    try:
        percent = float(block.get("utilization") or 0.0)
    except (TypeError, ValueError):
        percent = 0.0
    percent = max(0.0, min(100.0, percent))
    resets_at = _parse_iso(block.get("resets_at"))
    resets_in = int((resets_at - now).total_seconds()) if resets_at else None
    if resets_in is not None:
        resets_in = max(0, resets_in)
    return {
        "key": key,
        "label": label,
        "percent": round(percent, 1),
        "remaining_percent": round(100.0 - percent, 1),
        "resets_at": resets_at.isoformat(timespec="seconds") if resets_at else "",
        "resets_in_sec": resets_in,
        "resets_in": humanize_until(resets_in),
    }


def _scoped_windows(raw: dict, now: datetime) -> list[dict]:
    """Per-model windows from the payload's `limits` array.

    The endpoint grew a second vocabulary: alongside the plain top-level blocks
    it now reports a list of limits, some of which are *scoped* to one model
    ("weekly_scoped" with scope.model.display_name "Fable"). Those are kept
    separate from `windows` on purpose - the pacing layer's holds, boosts and
    spend-downs reason about account-wide windows, and a single model's window
    filling up must not idle runs that other models could still make.
    """
    out: list[dict] = []
    for entry in raw.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        scope = entry.get("scope") or {}
        model = ((scope.get("model") or {}).get("display_name") or "").strip() if isinstance(scope, dict) else ""
        if not model:
            continue
        try:
            percent = float(entry.get("percent") or 0.0)
        except (TypeError, ValueError):
            percent = 0.0
        percent = max(0.0, min(100.0, percent))
        group = str(entry.get("group") or "weekly")
        resets_at = _parse_iso(entry.get("resets_at"))
        resets_in = max(0, int((resets_at - now).total_seconds())) if resets_at else None
        out.append({
            "key": f"{group}_{model.lower()}",
            "model": model,
            "label": f"{group} ({model})",
            "percent": round(percent, 1),
            "remaining_percent": round(100.0 - percent, 1),
            "resets_at": resets_at.isoformat(timespec="seconds") if resets_at else "",
            "resets_in_sec": resets_in,
            "resets_in": humanize_until(resets_in),
        })
    return out


def parse(payload: dict, now: Optional[datetime] = None) -> dict:
    """Normalize a raw endpoint response into the portal's own shape.

    A pure function over the payload so the whole decision layer is testable
    without a network or a token.
    """
    now = now or datetime.now(timezone.utc)
    if not payload.get("ok"):
        return {
            "ok": False,
            "error": payload.get("error") or "unknown error",
            "windows": [],
            "scoped": [],
            "fetched_at": now.isoformat(timespec="seconds"),
        }

    raw = payload.get("raw") or {}
    windows = [w for w in (_window(raw, key, label, now) for key, label in WINDOWS) if w]
    # The window with the least headroom is the one that will actually stop a
    # run, whichever window it is - that is the number worth putting on screen.
    tightest = max(windows, key=lambda w: w["percent"]) if windows else None
    return {
        "ok": True,
        "error": "",
        "windows": windows,
        "scoped": _scoped_windows(raw, now),
        "tightest": tightest,
        "percent": tightest["percent"] if tightest else 0.0,
        "plan": payload.get("plan", ""),
        "tier": payload.get("tier", ""),
        "fetched_at": now.isoformat(timespec="seconds"),
    }


def window(snapshot: dict, key: str) -> Optional[dict]:
    for entry in snapshot.get("windows") or []:
        if entry["key"] == key:
            return entry
    return None


# ---------------------------------------------------------------------------
# The compact reading the side rail draws
# ---------------------------------------------------------------------------

# How long each window actually runs. The endpoint says when a window comes
# back but never how long it is, and without the length "resets in 1h" cannot
# become "four fifths of the way through" - which is the number Wes asked to
# see. These are facts about the plan, so they are stated here rather than
# inferred from a reading: inferring would need two readings either side of a
# reset, and the first page load after a restart has one.
WINDOW_SECONDS: dict[str, int] = {
    "five_hour": 5 * 3600,
    "seven_day": 7 * 86400,
    "seven_day_opus": 7 * 86400,
}

# Short enough for a 15rem rail, and for the 13rem one the narrow placement
# leaves. "week opus" was the first try and it ellipsized to "week op..." at
# 1450px, which spends the width and delivers none of the meaning; "opus" is
# unambiguous under a CLAUDE heading beside a row called "week", and the row
# carries the endpoint's own full label as its title for anyone who wants it.
# The long labels stay on the dashboard chips, which have a whole line.
SHORT_LABELS: dict[str, str] = {
    "five_hour": "5h",
    "seven_day": "week",
    "seven_day_opus": "opus",
}

# Where a window stops being background information and starts being the thing
# that will end the day early. Published because the dashboard's limit chips
# mark the same threshold, and two numbers for one judgment is how they drift.
HOT_PERCENT = 80.0


def elapsed_percent(key: str, resets_in_sec: Optional[int]) -> Optional[float]:
    """How far through its window we are, 0-100, or None if unknowable.

    This is deliberately NOT the same question as `percent`, which is how much
    of the allowance is spent. Reading the two together is the whole point of
    putting them side by side: 25% spent with the window nearly over is
    headroom about to expire unused, and 90% spent with the window barely
    started is a limit that will bite long before it resets.

    Clamped, because a clock skew or a reset that has just gone by would
    otherwise draw a pie past full or before empty.
    """
    length = WINDOW_SECONDS.get(key)
    if resets_in_sec is None or not length:
        return None
    done = (length - resets_in_sec) / length
    return round(max(0.0, min(1.0, done)) * 100.0, 1)


def compact_windows(snapshot: Optional[dict] = None) -> list[dict]:
    """The account's own windows, small enough to live in the side rail.

    Wes, 2026-08-16: "It would be cool to see a very brief visualization or
    writeup of current usage vs 5hr limit and current usage vs week limit +
    when they reset / how close we are to when they reset on the side nav-bar."

    Account-wide windows only. The per-model scoped ones (`snapshot["scoped"]`)
    are real and are on the dashboard, but five rows of percentages down the
    side of every page is a status bar rather than a glance - and the two he
    named are the two that stop a run.

    Returns [] rather than a placeholder whenever there is nothing trustworthy
    to say - no snapshot yet, an API-key install, a failed fetch - so the rail
    draws no group at all instead of a heading over an apology.
    """
    snapshot = cached() if snapshot is None else snapshot
    if not snapshot.get("ok"):
        return []
    rows: list[dict] = []
    for entry in snapshot.get("windows") or []:
        key = str(entry.get("key") or "")
        percent = float(entry.get("percent") or 0.0)
        rows.append(
            {
                "key": key,
                "label": SHORT_LABELS.get(key, entry.get("label") or key),
                # What the account calls this window, for the row's title -
                # the abbreviation above is for the eye, this is for the
                # reader who wants to be sure which window they are looking at.
                "full": entry.get("label") or key,
                "percent": percent,
                # Rounded to a whole number for display: a rail row is not the
                # place for a decimal, and the tenth is still on the dashboard.
                "used": int(round(percent)),
                "elapsed": elapsed_percent(key, entry.get("resets_in_sec")),
                "resets_in": entry.get("resets_in") or "",
                "hot": percent >= HOT_PERCENT,
            }
        )
    return rows


def reset_for(snapshot: dict, key: str) -> Optional[datetime]:
    """When `key`'s window comes back, as a datetime, or None if unknown."""
    entry = window(snapshot, key)
    return _parse_iso((entry or {}).get("resets_at"))


# ---------------------------------------------------------------------------
# Falling back when one model's own window runs out
# ---------------------------------------------------------------------------

# Wes's rule: "If individual Fable usage runs out, fall back to Opus until
# Fable access returns." The account reports Fable's weekly window separately
# (a scoped limit), so when that one is exhausted the rest of the plan still
# has headroom - running Opus instead of idling is strictly better. Checked at
# every model resolution, so it reverses by itself as soon as a fresh reading
# shows the window open again.
FALLBACK_MODELS = {"fable": "opus"}

# The CLI alias -> the display_name the usage endpoint uses for that model's
# scoped window.
MODEL_WINDOW_NAMES = {"fable": "Fable", "opus": "Opus", "sonnet": "Sonnet"}

# Same threshold as backoff_until: the endpoint rounds, and a window it reports
# in the high nineties is already refusing runs.
FALLBACK_AT_PERCENT = 95.0


def scoped_window(model_alias: str, snapshot: Optional[dict] = None) -> Optional[dict]:
    """The per-model window for a CLI model alias, if the account reports one."""
    snapshot = cached() if snapshot is None else snapshot
    name = MODEL_WINDOW_NAMES.get(model_alias, model_alias).lower()
    for entry in snapshot.get("scoped") or []:
        if str(entry.get("model") or "").lower() == name:
            return entry
    return None


def model_fallback(model: str, snapshot: Optional[dict] = None) -> tuple[str, str]:
    """(model to actually run, reason) - reason is "" when nothing changed.

    Fails open: with no snapshot, a stale one, or no scoped reading for the
    model, the configured model runs as asked. Being wrong there costs one run
    that hits the limit and backs off briefly, which the fallback then covers
    on the next spawn.
    """
    fallback = FALLBACK_MODELS.get(model)
    if not fallback:
        return model, ""
    snapshot = cached() if snapshot is None else snapshot
    if not snapshot.get("ok") or snapshot.get("stale"):
        return model, ""
    entry = scoped_window(model, snapshot)
    if entry is None or float(entry.get("percent") or 0.0) < FALLBACK_AT_PERCENT:
        return model, ""
    resets = entry.get("resets_in") or ""
    tail = f", back in {resets}" if resets else ""
    return fallback, f"{entry['label']} window at {entry['percent']:g}%{tail}"


# The longest a usage limit is ever allowed to hold the scheduler. If the
# *weekly* window is what is full, waiting for its real reset would idle the
# portal for days; coming back periodically and letting the next attempt find
# out is strictly better. Published because `worker._rate_limit_backoff` applies
# the same ceiling to the reset it reads off a 429 header, and two different
# ceilings for the same decision is how they drift apart.
MAX_BACKOFF = timedelta(hours=6)


def backoff_until(snapshot: dict, now: Optional[datetime] = None, fallback_min: int = 60) -> datetime:
    """When to try again after a run reported a usage limit.

    Prefers the real reset of whichever window is actually full. Falls back to
    a flat hour only when there is no usable snapshot - which is the old
    behavior, kept as the floor rather than as the rule.

    `MAX_BACKOFF` caps the answer; see its comment.
    """
    now = now or datetime.now(timezone.utc)
    ceiling = now + MAX_BACKOFF
    candidates = []
    for entry in snapshot.get("windows") or []:
        # 95% rather than 100: the endpoint rounds, and a window reported at 99
        # has already refused a run if a run just failed on it.
        if entry["percent"] >= 95.0:
            moment = _parse_iso(entry.get("resets_at"))
            if moment and moment > now:
                candidates.append(moment)
    if not candidates:
        return now + timedelta(minutes=fallback_min)
    return min(min(candidates), ceiling)


# ---------------------------------------------------------------------------
# The cache the rest of the portal reads
# ---------------------------------------------------------------------------


def cached(max_age_sec: Optional[int] = None) -> dict:
    """The last stored snapshot, annotated with its age.

    Always returns a dict with an `ok` key, so callers never have to guard for
    "the portal has never managed a fetch". `stale` says whether to trust it.
    """
    # An API-key install has no subscription window, and this is the choke
    # point where that fact has to land - not the fetch. An API-key user very
    # likely *also* has the CLI logged in (they installed it), so the endpoint
    # answers happily with figures about a subscription their runs are not
    # spending. Everything downstream keys off `ok`, so answering here keeps
    # pacing from holding runs against a window that is not theirs, and keeps
    # a snapshot cached during some earlier subscription-mode life from being
    # served long after the mode changed.
    if not spawnauth.paces_on_subscription():
        return {
            "ok": False,
            "not_applicable": True,
            "error": (
                "runs are billed to an Anthropic API key, so there is no "
                "subscription window to read"
            ),
            "windows": [],
            "scoped": [],
            "age_sec": None,
            "stale": True,
        }

    now = datetime.now(timezone.utc)
    blob = db.get_setting(CACHE_KEY) or ""
    try:
        snapshot = json.loads(blob) if blob else None
    except ValueError:
        snapshot = None
    if not isinstance(snapshot, dict):
        return {
            "ok": False,
            "error": "no usage snapshot yet",
            "windows": [],
            "age_sec": None,
            "stale": True,
        }

    fetched = _parse_iso(snapshot.get("fetched_at"))
    age = int((now - fetched).total_seconds()) if fetched else None
    limit = POLL_INTERVAL_SEC * 3 if max_age_sec is None else max_age_sec
    snapshot["age_sec"] = age
    snapshot["age"] = humanize_until(age) if age else "just now"
    # Reset countdowns are relative to when the snapshot was taken, so they go
    # stale silently. Recompute them against now, and notice if any window has
    # rolled over since the reading was taken: once a resets_at has passed, the
    # utilization figure in the snapshot describes a window that no longer
    # exists (it reset toward zero), so the reading must not be trusted no
    # matter how young it is. Front-loaded spend-down decisions read these
    # percentages, and acting on a pre-reset 95% right after the window cleared
    # would wrongly hold work back.
    reset_crossed = False
    for entry in (snapshot.get("windows") or []) + (snapshot.get("scoped") or []):
        moment = _parse_iso(entry.get("resets_at"))
        if moment:
            entry["resets_in_sec"] = max(0, int((moment - now).total_seconds()))
            entry["resets_in"] = humanize_until(entry["resets_in_sec"])
            if now >= moment:
                reset_crossed = True
    snapshot["reset_crossed"] = reset_crossed
    snapshot["stale"] = age is None or age > limit or reset_crossed
    return snapshot


def store(snapshot: dict) -> dict:
    """Persist a snapshot, unless it is a failure and we already have a good one.

    A transient network blip should not blank out a perfectly serviceable
    reading from four minutes ago; the age field is what tells the UI how much
    to trust it. A failure is still recorded when there is nothing better, so
    the reason shows up on screen instead of a silent blank.
    """
    if not snapshot.get("ok"):
        previous = cached()
        if previous.get("ok"):
            previous["last_error"] = snapshot.get("error", "")
            db.set_setting(CACHE_KEY, json.dumps(previous))
            return previous
    db.set_setting(CACHE_KEY, json.dumps(snapshot))
    return snapshot


def refresh(timeout: float = 15.0) -> dict:
    """Fetch and store. Blocking - the poller calls it in a thread."""
    from app import cadence

    # Nothing to fetch on an API-key install, and fetching anyway would be
    # worse than useless: it would store a reading about somebody's untouched
    # subscription as though it described this portal's spending.
    if not spawnauth.paces_on_subscription():
        return cached()

    parsed = parse(fetch_raw(timeout=timeout))
    stored = store(parsed)
    # Learn the real reset cadence only from a reading that actually came back -
    # never from the previous snapshot `store` re-serves on a failed fetch, which
    # would re-observe an old reading and could mask a reset's true timing. Fails
    # open: a cadence bug must never turn a good poll into a lost snapshot.
    if parsed.get("ok"):
        try:
            cadence.record_reading(parsed)
        except Exception:  # noqa: BLE001
            log.exception("cadence.record_reading failed")
    return stored


async def refresh_async(timeout: float = 15.0) -> dict:
    """`refresh()` off the event loop, so nothing waits on the network."""
    return await asyncio.to_thread(refresh, timeout)


async def poll_loop(interval_sec: int = 60, startup_delay_sec: int = 5) -> None:
    """Keep the cached snapshot warm.

    Ticks more often than it fetches: the tick is cheap and the fetch is gated
    on the cache's age, so a restart picks up a fresh reading within a minute
    without a burst of requests when several things restart at once.

    The startup delay exists because this portal restarts itself whenever the
    meta-project commits, and a run of restarts should not become a run of
    requests to someone else's API before the process has even settled.
    """
    if not spawnauth.paces_on_subscription():
        # Returning rather than looping-and-skipping: there is no state that
        # could make this applicable later without a config change and a
        # restart, so a task that ticks forever to do nothing is just a line
        # of noise in every log.
        log.info(
            "Usage-limit poller not started: auth_mode is %s, so there is no "
            "subscription window to track",
            spawnauth.mode(),
        )
        return
    log.info("Usage-limit poller started")
    await asyncio.sleep(startup_delay_sec)
    while True:
        try:
            snapshot = cached()
            age = snapshot.get("age_sec")
            # Refresh on the usual cadence, but also the instant a window has
            # rolled over: a passed reset makes the cached percentages wrong, so
            # waiting out the rest of the poll interval would keep serving a
            # stale near-full reading straight through the moment fresh headroom
            # appears.
            if (
                not snapshot.get("ok")
                or age is None
                or age >= POLL_INTERVAL_SEC
                or snapshot.get("reset_crossed")
            ):
                await refresh_async()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must never die
            log.exception("Usage-limit poll failed")
        await asyncio.sleep(interval_sec)

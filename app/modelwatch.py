"""Notice when Anthropic ships a new model, without Wes having to.

Wes, 2026-07-25: *"it should also be able to detect new models being added
under API's or subscriptions it is using."*

He is right that this has been manual and that manual has been slow. Opus 5
shipped on 2026-07-24 and the portal only adopted it when he sent a note the
next day - and the adoption run found the portal had *still* been spawning
4.8, because the CLI's own `opus` alias had not caught up. A watcher would have
had that on his phone the morning it landed.

## Where the list comes from

`GET https://api.anthropic.com/v1/models`, authenticated with the OAuth access
token the Claude CLI already stores (`app/limits.read_token`) - no API key, no
billing, and it is scoped to what Wes's own subscription can reach, which is
exactly what "models under the APIs or subscriptions it is using" means. Probed
on 2026-07-25: the endpoint answers 200 to a Bearer token with the same
`anthropic-beta: oauth-2025-04-20` header the usage endpoint needs, and returns
`id`, `display_name` and `created_at` per model.

That is a better signal than the two alternatives. The usage endpoint's
`limits` array only names model *tiers* that already have a scoped window, so a
new model with no window is invisible there; and probing `claude --model <id>`
can only confirm an id someone already guessed.

## What it does about it

The first check after the feature ships is a **seed**, not an announcement: it
records the eleven models that exist today silently. Announcing them would be
eleven notifications about nothing, and would teach him to ignore the next one,
which is the only one that matters.

After that, an id that was not in the catalog is news, and the portal **adopts
it and then tells him**.

Wes, 2026-09-23, answering the adoption question this module used to file:
*"Always adopt the new models- no need to ask. Send me a notification maybe
still, though, as it's cool to know when they drop!"*

That retires the question and the one-tap options with it. It does NOT retire
the reason they existed - a new id in the catalog is still not proof the CLI
can spawn it, which `claude-opus-5-5` proved again on the very day he answered
by 400ing on CLI 2.1.258. "No need to ask" says who decides, not that the
check is optional, so the adoption itself runs through `app/modeladopt.py`:
family, version, and a real probe before anything is pinned. A model the CLI
cannot spawn yet is still adopted, behind its version gate, so runs keep using
the previous release until `claude update` catches up and then switch with no
further decision.

The notification says which of those two happened, and a third one fires later
when a gated adoption goes live - otherwise "waiting on claude update" would be
the last thing he ever heard about it.

Fails open and quiet throughout: no credentials, a 500 from Anthropic, a
garbled payload - the catalog is simply not updated that day. A watcher that
cannot see is never a reason for the portal to stop working.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from app import config, db, limits, modeladopt, notify

log = logging.getLogger("portal.modelwatch")

MODELS_URL = "https://api.anthropic.com/v1/models?limit=100"

# The catalog as last fetched (for the settings card), and the ids already
# announced. Two keys rather than one because they change for different
# reasons: the catalog is overwritten by every successful poll, the seen-set
# only ever grows and is what makes an announcement once-only.
CATALOG_KEY = "model_catalog_json"
SEEN_KEY = "model_ids_seen_json"
SETTING_ENABLED = "model_watch"

# Checked once a day. A model launch is a thing that happens a few times a
# year; polling it more often would be noise on someone else's server.


def enabled() -> bool:
    """On unless explicitly switched off - the same "absent means 1" convention
    every other checkbox setting uses. Opt-out rather than opt-in because the
    whole value of the feature is hearing about a release you did not know to
    look for."""
    return (db.get_setting(SETTING_ENABLED) or "1") == "1"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_models(timeout: float = 15.0, path=None) -> dict:
    """One call to the models endpoint. Blocking; call it off the event loop.

    Returns {"ok": True, "models": [...]} or {"ok": False, "error": "..."}.
    Never raises.
    """
    creds = limits.read_token(path)
    if creds.get("error"):
        return {"ok": False, "error": creds["error"]}
    if creds.get("expired"):
        # Same rule as app/limits: never refresh the token from a side process,
        # because the refresh rotates it in the file every `claude -p` reads.
        return {"ok": False, "error": "access token expired; waiting for the CLI to refresh it"}

    request = urllib.request.Request(
        MODELS_URL,
        headers={
            "Authorization": f"Bearer {creds['token']}",
            "anthropic-beta": limits.OAUTH_BETA,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
            "User-Agent": config.usage_user_agent(),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"models endpoint returned {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"could not reach the models endpoint: {exc}"}

    models = parse_models(payload)
    if not models:
        return {"ok": False, "error": "models endpoint returned nothing usable"}
    return {"ok": True, "models": models}


def parse_models(payload: Any) -> list[dict]:
    """The fields worth keeping, defensively.

    Anything without an `id` is dropped rather than stored as a blank, because
    a blank id in the seen-set would swallow the next real model that parsed
    badly. Extra keys the API grows are ignored, not carried.
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        out.append({
            "id": model_id.strip(),
            "display_name": str(item.get("display_name") or model_id).strip(),
            "created_at": str(item.get("created_at") or ""),
        })
    return out


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

def catalog() -> dict:
    """The last successful fetch: {"models": [...], "fetched_at": "..."}."""
    return _load(CATALOG_KEY, {"models": [], "fetched_at": ""})


def seen_ids() -> set[str]:
    blob = _load(SEEN_KEY, {})
    ids = blob.get("ids")
    return {str(i) for i in ids} if isinstance(ids, list) else set()


def _load(key: str, default: dict) -> dict:
    try:
        blob = json.loads(db.get_setting(key) or "")
    except (TypeError, ValueError):
        return dict(default)
    return blob if isinstance(blob, dict) else dict(default)


def _store_catalog(models: list[dict]) -> None:
    db.set_setting(CATALOG_KEY, json.dumps({
        "models": models,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }))


def _store_seen(ids: set[str]) -> None:
    db.set_setting(SEEN_KEY, json.dumps({"ids": sorted(ids)}))


def new_models(models: list[dict], seen: set[str]) -> list[dict]:
    """Which of these have never been recorded. Ordered newest-first by
    `created_at` so a double release announces the newer one first."""
    fresh = [m for m in models if m["id"] not in seen]
    return sorted(fresh, key=lambda m: m.get("created_at") or "", reverse=True)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def check(models: Optional[list[dict]] = None) -> dict:
    """Fold a fetch into the catalog and report what is new.

    Pure enough to test: pass `models` to skip the network. Returns
    {"ok", "seeded", "new": [...], "error"}. `seeded` is True on the very first
    successful check, when everything is "new" and none of it is news.
    """
    if models is None:
        result = fetch_models()
        if not result.get("ok"):
            return {"ok": False, "seeded": False, "new": [], "error": result.get("error", "")}
        models = result["models"]

    seen = seen_ids()
    first_time = not seen
    fresh = new_models(models, seen)

    _store_catalog(models)
    _store_seen(seen | {m["id"] for m in models})

    if first_time:
        # Seed silently. Eleven notifications on the day the feature ships
        # would be eleven reasons to mute the twelfth, which is the real one.
        log.info("Model watch seeded with %d models", len(models))
        return {"ok": True, "seeded": True, "new": [], "error": ""}
    return {"ok": True, "seeded": False, "new": fresh, "error": ""}


def announcement(model: dict, verdict: Optional[dict] = None) -> tuple[str, str]:
    """The (title, body) for one new model. Pure, so the wording is pinned.

    Three outcomes, three different things worth saying. The one that must not
    be fudged is the gated case: telling him a model is adopted while every run
    keeps spawning the previous one would be true and useless, so the body
    names both versions and the one command that closes the gap.
    """
    name = model.get("display_name") or model["id"]
    verdict = verdict or {}
    created = model.get("created_at") or ""
    released = f" Released {created[:10]}." if created else ""

    if verdict.get("adopted") and verdict.get("gated"):
        required = verdict.get("required_cli") or "a newer version"
        return (
            f"New model adopted: {name}",
            f"`{model['id']}` is out and the portal is pinned to it. This "
            f"machine's Claude CLI is {config.cli_version()} and it needs "
            f"{required}, so runs keep using the previous model until "
            f"`claude update` runs - then it switches by itself.{released}",
        )
    if verdict.get("adopted"):
        return (
            f"New model adopted: {name}",
            f"`{model['id']}` is out and the portal is now spawning it. "
            f"Nothing to do.{released}",
        )

    title = f"New model available: {name}"
    body = (
        f"`{model['id']}` ({name}) is now on the model list your Claude "
        f"subscription can reach."
    )
    reason = verdict.get("reason") or ""
    if reason:
        body += f" Not adopted: {reason}."
    return title, body + released


def live_announcement(cleared: dict) -> tuple[str, str]:
    """The (title, body) for a gated adoption that has just gone live."""
    label = cleared.get("label") or cleared["model_id"]
    return (
        f"{label} is live",
        f"The Claude CLI on this machine is new enough now, so runs are "
        f"spawning `{cleared['model_id']}`.",
    )


async def _send(title: str, body: str, model_id: str = "") -> None:
    """Notification plus journal line, each best effort and independent."""
    try:
        await notify.notify(title, body)
    except Exception:  # noqa: BLE001
        log.exception("Could not notify about %s", model_id)
    try:
        db.add_journal(None, "system", "status", f"{title}. {body}")
    except Exception:  # noqa: BLE001
        log.exception("Could not journal the new model %s", model_id)


async def announce(model: dict, verdict: Optional[dict] = None) -> None:
    """Tell Wes about one new model and what the portal did about it."""
    title, body = announcement(model, verdict)
    await _send(title, body, model.get("id", ""))


async def run_check() -> dict:
    """The whole daily job: fetch, fold in, adopt anything new, say so.

    The fetch and every probe are blocking subprocess/urllib calls, so they go
    to a thread. Never raises.
    """
    if not enabled():
        return {"ok": False, "seeded": False, "new": [], "adopted": [], "error": "model watch is off"}
    try:
        result = await asyncio.to_thread(check)
    except Exception:  # noqa: BLE001 - a broken watcher must not stop the worker
        log.exception("Model watch check failed")
        return {"ok": False, "seeded": False, "new": [], "adopted": [], "error": "check failed"}

    models = catalog().get("models") or []
    adopted: list[dict] = []

    for model in result.get("new", []):
        try:
            verdict = await asyncio.to_thread(modeladopt.adopt, model, models)
        except Exception:  # noqa: BLE001 - never let an adoption stop the news
            log.exception("Could not adopt %s", model.get("id"))
            verdict = {"adopted": False, "reason": "the adoption check failed"}
        if verdict.get("adopted"):
            adopted.append(verdict)
        await announce(model, verdict)

    # A model whose probe reached no verdict last time. Silent unless it lands:
    # he has already been told this model exists, so only the adoption is news.
    try:
        retried = await asyncio.to_thread(modeladopt.retry_pending, models)
    except Exception:  # noqa: BLE001
        log.exception("Could not retry pending model adoptions")
        retried = []
    for verdict in retried:
        if not verdict.get("adopted"):
            continue
        adopted.append(verdict)
        model = next((m for m in models if m["id"] == verdict["model_id"]), None)
        await announce(model or {"id": verdict["model_id"]}, verdict)

    # And an adoption that was waiting on `claude update`, now that it is not.
    try:
        for cleared in modeladopt.cleared_gates():
            title, body = live_announcement(cleared)
            await _send(title, body, cleared["model_id"])
    except Exception:  # noqa: BLE001
        log.exception("Could not announce a cleared model gate")

    result["adopted"] = adopted
    return result

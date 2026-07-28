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

After that, an id that was not in the catalog is news, and gets:

- a notification, so it reaches the phone; and
- an open question on the meta-project with one-tap options, because "Opus 6 is
  out - adopt it?" is a decision, and the portal already knows how to turn a
  tap into a woken project and a run (see app/quickreplies.py). Answering is
  what makes the adoption happen; the watcher itself changes no settings. It
  cannot: a new id in the catalog is not proof the CLI can spawn it, as Opus
  5 demonstrated, so switching the default model automatically would have
  pointed every run at a model that 404s.

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

from app import config, db, limits, notify, quickreplies

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
QUESTION_CONTEXT = (
    "The portal watches the model list your Claude subscription can reach and "
    "noticed this is new. Adopting means pointing the portal's default agent at "
    "it - which is a small code change, because the CLI's own short alias often "
    "lags the release by days and the explicit model id has to be pinned."
)


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


def announcement(model: dict) -> tuple[str, str]:
    """The (title, body) for one new model. Pure, so the wording is pinned."""
    name = model.get("display_name") or model["id"]
    title = f"New model available: {name}"
    body = (
        f"`{model['id']}` ({name}) is now on the model list your Claude "
        f"subscription can reach."
    )
    created = model.get("created_at") or ""
    if created:
        body += f" Released {created[:10]}."
    return title, body


def question_text(model: dict) -> str:
    name = model.get("display_name") or model["id"]
    return f"{name} (`{model['id']}`) is out. Want the portal to move onto it?"


QUESTION_OPTIONS = ["adopt it", "not yet"]


def meta_project() -> Optional[db.sqlite3.Row]:
    """The portal's own project - where a "should we adopt this?" question
    belongs, because acting on the answer is a change to the portal."""
    try:
        return db.get_project_by_slug(config.META_PROJECT_SLUG)
    except Exception:  # noqa: BLE001 - defensive; a missing meta project is fine
        return None


def file_question(model: dict) -> Optional[db.sqlite3.Row]:
    """Raise the adoption decision as a real question with one-tap options.

    A question rather than a bare notification because it needs an answer, and
    because every channel Wes already answers on - the project page, the
    questions page, a tap in Telegram - works on questions for free.
    """
    project = meta_project()
    if project is None:
        return None
    text = question_text(model)
    filing = db.file_question(
        project["id"],
        text,
        context=QUESTION_CONTEXT,
        quick_options=quickreplies.encode(QUESTION_OPTIONS),
    )
    # None, not the matched row: `announce` only sends a notification when it
    # gets a question back, so this is how a model whose adoption question is
    # already open stops re-pinging him. The seen-set means this should only
    # ever fire for two releases that read alike, which the mark check in
    # qdedupe is there to keep apart - so treat it as belt and braces.
    return filing.row if filing.created else None


async def announce(model: dict) -> None:
    """Tell Wes about one new model. Best effort in both halves: a failed
    notification must not stop the question being filed, and a failed question
    must not stop the notification going out."""
    title, body = announcement(model)
    question = None
    try:
        question = file_question(model)
    except Exception:  # noqa: BLE001
        log.exception("Could not file the adoption question for %s", model.get("id"))
    try:
        await notify.notify(
            title,
            body,
            question_id=question["id"] if question else None,
            question_slot=question["slot"] if question else None,
            project_title="Project Portal" if question else None,
        )
    except Exception:  # noqa: BLE001
        log.exception("Could not notify about %s", model.get("id"))
    try:
        db.add_journal(
            question["project_id"] if question else None,
            "system", "status", f"{title}. {body}",
        )
    except Exception:  # noqa: BLE001
        log.exception("Could not journal the new model %s", model.get("id"))


async def run_check() -> dict:
    """The whole daily job: fetch, fold in, announce anything new.

    The fetch is a blocking urllib call, so it goes to a thread; everything
    after it is cheap. Never raises.
    """
    if not enabled():
        return {"ok": False, "seeded": False, "new": [], "error": "model watch is off"}
    try:
        result = await asyncio.to_thread(check)
    except Exception:  # noqa: BLE001 - a broken watcher must not stop the worker
        log.exception("Model watch check failed")
        return {"ok": False, "seeded": False, "new": [], "error": "check failed"}
    for model in result.get("new", []):
        await announce(model)
    return result

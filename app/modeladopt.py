"""Move the portal onto a new model by itself, without breaking every run.

Wes, 2026-09-23, answering "Claude Opus 5.5 is out. Want the portal to move
onto it?": *"Always adopt the new models- no need to ask. Send me a
notification maybe still, though, as it's cool to know when they drop!"*

So the question `app/modelwatch.py` used to file is withdrawn. What is NOT
withdrawn is the reason it existed, which that module's own docstring states:
a new id in the catalog is not proof the CLI can spawn it. That is not a
historical worry - it is what the probe found on the very day Wes answered:

    $ claude -p "Reply with only the word: ok" --model claude-opus-5-5
    [claude-code:unrecognized_model] {"model":"claude-opus-5-5", ...}
    API Error: 400 Claude Code 2.1.258 does not support this model;
    version 2.1.280 or newer is required.

An unconditional adoption that morning would have pointed every run on the
board at a model that 400s. "No need to ask" is an instruction about *who
decides*, not permission to skip the check. So this module adopts
automatically and checks first.

## The three questions it answers

1. **Is this model one of ours?** `family_of` reads the family out of the id
   (`claude-opus-5-5` -> `opus`), and only the four families in
   `config.MODEL_VALUES` count. An id in a family the portal has no dropdown
   entry for is news, not an adoption.

2. **Is it actually newer?** `supersedes` compares the numeric parts after the
   family: `(5, 5) > (5,)`. Date-like parts are dropped rather than compared,
   because `claude-opus-5-20260101` would otherwise rank *above*
   `claude-opus-5-5` - 20260101 > 5 at the same position - and a dated
   snapshot would silently displace a newer release. Dropping them makes a
   snapshot merely equal to its floating id, and equal does not supersede.

   `candidate` adds the other half of that guard: a model is only adopted if
   it is the highest-ranked id of its family in the catalog *as well as*
   ahead of the current pin. That is what stops a family with no pin at all
   (sonnet, haiku) from adopting whichever of its ids happened to appear
   first.

3. **Can this CLI spawn it?** `probe` asks, by spawning it. Two things about
   reading that answer, both learned the hard way on this estate:

   - **Judge the output, not the return code.** The 400 above came back with
     `rc=0`. A probe that trusted the exit status would have adopted a model
     that cannot run.
   - **The error names the minimum version.** "version 2.1.280 or newer is
     required" is exactly the value `config.MODEL_MIN_CLI` wants, so the
     probe harvests it instead of making a human go and find it.

## Why a version failure still adopts

`config.cli_model()` already degrades a pin whose CLI requirement is not met
back to the bare alias, which every CLI understands. So when the probe comes
back "real model, CLI too old", the honest move is to **record the pin and its
minimum version anyway**. Runs keep spawning the bare `opus` alias today, and
the day `claude update` runs the portal is already pointed at 5.5 with nothing
further to do. Adoption completes without another decision, another probe or
another notification.

Any *other* probe failure - no credentials, a network blip, an error nobody
here has seen - does not adopt. It is recorded as pending and retried on the
next daily check, because "we could not tell" must never read as "yes".

## Where the answer lives

In settings rows, not in this file. The pin a running install has adopted is
local state: it was decided by probing *this* machine's CLI, and the office
portal at wes-wm has its own CLI and reaches its own conclusion. Overrides
layer over `config.CLI_MODEL_IDS` rather than replacing it, so the shipped
pins remain the floor for a fresh install.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Optional

from app import config, db

log = logging.getLogger("portal.modeladopt")

# Adopted pins, as {alias: model_id}. Layered OVER config.CLI_MODEL_IDS.
PINS_KEY = "model_pins_json"
# Minimum CLI version per alias, as {alias: "2.1.280"}. Layered OVER
# config.MODEL_MIN_CLI the same way.
MIN_CLI_KEY = "model_min_cli_json"
# Models that look adoptable but whose probe could not reach a verdict, as
# {model_id: {"family": ..., "display_name": ...}}. Retried daily.
PENDING_KEY = "model_adopt_pending_json"
# The dropdown label per alias, as {alias: "Opus 5.5"}. Without this an
# adoption would leave every model picker in the portal naming the model it
# used to spawn, which is the kind of quietly-wrong label Wes reads as a bug.
LABELS_KEY = "model_labels_json"
# Adoptions whose pin is recorded but whose CLI gate is not met yet, as
# {alias: model_id}. Self-clearing: `cleared_gates()` drops an entry the moment
# the installed CLI can spawn it, which is what lets the portal say "live now"
# after having said "waiting on claude update".
GATED_KEY = "model_gated_json"

# A part of a model id with this many digits is a date stamp, not a version
# component. `claude-haiku-4-5-20251001` is Haiku 4.5, not Haiku 4.5.20251001.
DATE_PART_DIGITS = 6

# What the probe asks for. Short enough that the spawn costs approximately
# nothing, and specific enough that a refusal or an error page does not
# accidentally look like a pass.
PROBE_PROMPT = "Reply with only the word: ok"

# Markers that mean the CLI did not actually run the model, whatever it exited
# with. The 400 that started all this exits 0.
ERROR_MARKERS = (
    "api error",
    "unrecognized_model",
    "isn't described by this version's model catalog",
    "does not support this model",
)

# "version 2.1.280 or newer is required"
MIN_CLI_RE = re.compile(r"version\s+(\d+(?:\.\d+)+)\s+or\s+newer", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Reading a model id
# ---------------------------------------------------------------------------

def family_of(model_id: str) -> Optional[str]:
    """`claude-opus-5-5` -> `opus`. None for anything the portal has no alias
    for, which includes a family it has never heard of and an id that is not
    shaped like one at all."""
    text = str(model_id or "").strip().lower()
    if not text.startswith("claude-"):
        return None
    rest = text[len("claude-"):]
    for alias in config.MODEL_VALUES:
        if rest == alias or rest.startswith(f"{alias}-"):
            return alias
    return None


def version_parts(model_id: str) -> tuple[int, ...]:
    """The numeric version after the family, with date stamps dropped.

    `claude-opus-5-5` -> (5, 5); `claude-haiku-4-5-20251001` -> (4, 5);
    an id with nothing numeric in it -> ().
    """
    family = family_of(model_id)
    if family is None:
        return ()
    rest = str(model_id).strip().lower()[len("claude-") + len(family):]
    parts: list[int] = []
    for chunk in rest.split("-"):
        if not chunk.isdigit():
            continue
        if len(chunk) >= DATE_PART_DIGITS:
            continue
        parts.append(int(chunk))
    return tuple(parts)


def supersedes(new_id: str, current_id: Optional[str]) -> bool:
    """Is `new_id` a later release than `current_id` of the same family?

    No current pin means yes - there is nothing it has to beat. A different
    family means no: `claude-fable-5-1` does not supersede `claude-opus-5`,
    they are different dropdown entries.
    """
    new_family = family_of(new_id)
    if new_family is None:
        return False
    if not version_parts(new_id):
        # An id with no readable version cannot be shown to be an improvement.
        return False
    if not current_id:
        return True
    if family_of(current_id) != new_family:
        return False
    return version_parts(new_id) > version_parts(current_id)


def best_in_family(models: list[dict], family: str) -> Optional[str]:
    """The highest-versioned id of one family in a catalog."""
    ranked = [
        (version_parts(m["id"]), m["id"])
        for m in models
        if family_of(m.get("id", "")) == family and version_parts(m.get("id", ""))
    ]
    if not ranked:
        return None
    return max(ranked)[1]


def candidate(model_id: str, models: list[dict]) -> bool:
    """Should this id be probed for adoption at all?

    Both halves have to hold: it must be ahead of what the portal spawns
    today, and it must be the newest of its family in the catalog. The second
    is what keeps a family with no pin from adopting an older sibling.
    """
    family = family_of(model_id)
    if family is None:
        return False
    if not supersedes(model_id, pins().get(family)):
        return False
    return best_in_family(models, family) == model_id


# ---------------------------------------------------------------------------
# Stored state
# ---------------------------------------------------------------------------

def _load(key: str) -> dict:
    try:
        blob = json.loads(db.get_setting(key) or "")
    except (TypeError, ValueError):
        return {}
    return blob if isinstance(blob, dict) else {}


def _store(key: str, blob: dict) -> None:
    db.set_setting(key, json.dumps(blob, sort_keys=True))


def pin_overrides() -> dict[str, str]:
    """Only what this install has adopted, without the shipped pins under it."""
    return {str(k): str(v) for k, v in _load(PINS_KEY).items() if isinstance(v, str) and v}


def pins() -> dict[str, str]:
    """The model id each alias spawns: shipped pins with adoptions over them."""
    merged = dict(config.CLI_MODEL_IDS)
    merged.update(pin_overrides())
    return merged


def min_cli_overrides() -> dict[str, str]:
    return {str(k): str(v) for k, v in _load(MIN_CLI_KEY).items() if isinstance(v, str) and v}


def min_cli() -> dict[str, str]:
    merged = dict(config.MODEL_MIN_CLI)
    merged.update(min_cli_overrides())
    return merged


def label_overrides() -> dict[str, str]:
    return {str(k): str(v) for k, v in _load(LABELS_KEY).items() if isinstance(v, str) and v}


def labels() -> dict[str, str]:
    merged = dict(config.MODEL_CHOICES)
    merged.update(label_overrides())
    return merged


def short_label(display_name: str, model_id: str) -> str:
    """"Claude Opus 5.5" -> "Opus 5.5".

    Wes on the picker: *"remove the extra text like 'most capable' and whatnot.
    Just let it be Opus 4.8, etc."* The vendor prefix is the same kind of
    padding, and every label already shipped is written without it.
    """
    name = str(display_name or "").strip()
    if not name:
        return str(model_id or "")
    if name.lower().startswith("claude "):
        name = name[len("claude "):].strip()
    return name or str(model_id or "")


def gated() -> dict[str, str]:
    return {str(k): str(v) for k, v in _load(GATED_KEY).items() if isinstance(v, str) and v}


def cleared_gates() -> list[dict]:
    """Adoptions whose CLI gate the installed CLI now meets.

    Removes them as it reports them, so each one is announced exactly once.
    An entry whose alias has since moved on to a newer model is dropped
    silently - the newer adoption is the news, not this one.
    """
    blob = gated()
    if not blob:
        return []
    live: list[dict] = []
    keep: dict[str, str] = {}
    current = pins()
    for alias, model_id in sorted(blob.items()):
        if current.get(alias) != model_id:
            continue  # superseded while it waited
        if config.cli_model(alias) == model_id:
            live.append({"alias": alias, "model_id": model_id,
                         "label": labels().get(alias, model_id)})
        else:
            keep[alias] = model_id
    if keep != blob:
        _store(GATED_KEY, keep)
    return live


def pending() -> dict[str, dict]:
    return {str(k): v for k, v in _load(PENDING_KEY).items() if isinstance(v, dict)}


def _remember_pending(model: dict, reason: str) -> None:
    blob = pending()
    blob[model["id"]] = {
        "family": family_of(model["id"]) or "",
        "display_name": model.get("display_name") or model["id"],
        "created_at": model.get("created_at") or "",
        "reason": reason,
    }
    _store(PENDING_KEY, blob)


def _forget_pending(model_id: str) -> None:
    blob = pending()
    if blob.pop(model_id, None) is not None:
        _store(PENDING_KEY, blob)


def record(alias: str, model_id: str, required_cli: str = "", label: str = "") -> None:
    """Point `alias` at `model_id`, with the CLI version it needs if it has one.

    `required_cli` is written as an override even when blank is tempting: an
    alias that used to need a version and now does not must stop being gated,
    and a merge that only ever adds keys could not express that.
    """
    blob = pin_overrides()
    blob[alias] = model_id
    _store(PINS_KEY, blob)

    gates = min_cli_overrides()
    if required_cli:
        gates[alias] = required_cli
    else:
        gates.pop(alias, None)
    _store(MIN_CLI_KEY, gates)

    if label:
        names = label_overrides()
        names[alias] = label
        _store(LABELS_KEY, names)

    waiting = gated()
    # Recorded only while the CLI genuinely cannot spawn it. Reading the gate
    # back through cli_model() rather than trusting `required_cli` is what
    # keeps this honest on an install whose CLI is already new enough.
    if required_cli and config.cli_model(alias) != model_id:
        waiting[alias] = model_id
    else:
        waiting.pop(alias, None)
    if waiting != gated():
        _store(GATED_KEY, waiting)


# ---------------------------------------------------------------------------
# Asking the CLI
# ---------------------------------------------------------------------------

def read_probe(output: str, returncode: int = 0) -> dict:
    """Turn a probe's output into a verdict. Pure, so the parsing is testable.

    Returns {"ok", "required_cli", "error"}. `ok` False with a `required_cli`
    means "real model, this CLI is too old" - which is an adoption, gated.
    `ok` False with neither means the probe reached no verdict.
    """
    text = str(output or "")
    lowered = text.lower()
    found = [marker for marker in ERROR_MARKERS if marker in lowered]
    if found:
        match = MIN_CLI_RE.search(text)
        return {
            "ok": False,
            "required_cli": match.group(1) if match else "",
            "error": found[0],
        }
    if returncode != 0:
        return {"ok": False, "required_cli": "", "error": f"claude exited {returncode}"}
    if not text.strip():
        # A silent success is indistinguishable from a silent failure, and the
        # expensive mistake is calling the second one the first.
        return {"ok": False, "required_cli": "", "error": "the probe returned nothing"}
    return {"ok": True, "required_cli": "", "error": ""}


def probe(model_id: str, timeout: float = 180.0) -> dict:
    """Spawn the model once and see what comes back. Never raises."""
    try:
        out = subprocess.run(
            ["claude", "-p", PROBE_PROMPT, "--model", model_id],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "required_cli": "", "error": "the probe timed out"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "required_cli": "", "error": f"could not run claude: {exc}"}
    return read_probe((out.stdout or "") + (out.stderr or ""), out.returncode)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def adopt(model: dict, models: list[dict], probe_fn=None) -> dict:
    """Adopt one model if it is ours, newer, and spawnable.

    Returns {"adopted", "alias", "model_id", "required_cli", "gated", "reason"}.
    `gated` True means the pin is recorded but this CLI is too old to use it
    yet, so runs keep spawning the bare alias until `claude update` runs.
    """
    model_id = str(model.get("id") or "")
    verdict = {
        "adopted": False,
        "alias": family_of(model_id) or "",
        "model_id": model_id,
        "required_cli": "",
        "gated": False,
        "reason": "",
    }
    if not candidate(model_id, models):
        verdict["reason"] = "not a newer model of a family the portal spawns"
        return verdict

    label = short_label(model.get("display_name") or "", model_id)
    # Resolved here, not as a default argument: a default binds `probe` at
    # import time, which silently pins the real subprocess even after the name
    # is replaced - so the seam would look overridable and not be.
    result = (probe_fn or probe)(model_id)
    if result.get("ok"):
        record(verdict["alias"], model_id, label=label)
        _forget_pending(model_id)
        verdict["adopted"] = True
        return verdict

    required = str(result.get("required_cli") or "")
    if required:
        # A real model behind a CLI gate. Record it: cli_model() degrades to
        # the bare alias until the CLI catches up, then switches by itself.
        record(verdict["alias"], model_id, required, label=label)
        _forget_pending(model_id)
        verdict["adopted"] = True
        verdict["gated"] = True
        verdict["required_cli"] = required
        return verdict

    verdict["reason"] = str(result.get("error") or "the probe reached no verdict")
    _remember_pending(model, verdict["reason"])
    return verdict


def retry_pending(models: list[dict], probe_fn=None) -> list[dict]:
    """Re-attempt every model whose probe previously reached no verdict.

    Silent by design: this is a retry of something Wes has already been told
    about, so only an adoption that actually lands is worth his lock screen,
    and `run_check` decides that from the verdicts returned here.
    """
    verdicts: list[dict] = []
    for model_id, saved in sorted(pending().items()):
        model = {
            "id": model_id,
            "display_name": saved.get("display_name") or model_id,
            "created_at": saved.get("created_at") or "",
        }
        if not candidate(model_id, models):
            # Superseded while it sat here, or no longer in the catalog.
            _forget_pending(model_id)
            continue
        verdicts.append(adopt(model, models, probe_fn=probe_fn or probe))
    return verdicts

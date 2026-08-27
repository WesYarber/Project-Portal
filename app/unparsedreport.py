"""Rescue a run's report when the CLI could not parse the StructuredOutput call.

`claude -p --json-schema` normally hands the portal a schema-validated object
in the result event's `structured_output`. When the model's tool input is not
valid JSON, the CLI does not fail the call - it substitutes a placeholder
keyed on `__unparsedToolInput` and carries on. That placeholder is a dict, so
the portal used to accept it as the report itself, with three consequences on
a real run on this install (run 897, 2026-08-07, 53 turns, $3.60):

- the whole report was dropped - bullets, journal entry, preview URL, the lot;
- the raw JSON blob became the run summary on the project page AND the
  project's journal entry for that run (journal id 1537);
- the run was recorded `ok`, so nothing anywhere said a report had been lost.

Two shapes are handled, because the CLI has emitted both. The current build
(2.1.223) constructs `{"__unparsedToolInput": {"raw": <text>, "len": <int>}}`
and truncates `raw` to 2048 characters; the two placeholders actually found in
this install's database held the text as a plain string, untruncated at 4472
and 12756 characters. Older builds minify differently and could not be grepped
to settle which produced them, so both are read and neither is assumed.

A third shape turned up on 2026-08-12 and is handled here too: the CLI can
hand back a perfectly valid dict that is not the report but an *envelope*
around it - `{"parameter": "<the whole report as a JSON string>"}`. Nothing
flags this one. `structured_output` is a dict, so it was accepted as the
report itself, and since it holds none of the contract's fields the run got no
summary bullets and no journal entry - the envelope's raw JSON became both, on
the project page and in the journal. Four runs on this install landed that way
(962, 973 and 976, journal entries 1709 and 1741) before Wes reported it as
"this messed up format".

Recovery is best-effort by design and never raises: a rescued report is a
bonus, and the caller's fallbacks (the legacy .portal/report.json, then an
honest failure note) must still run when nothing can be read.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app import report_schema

log = logging.getLogger(__name__)

UNPARSED_KEY = "__unparsedToolInput"

# The keys that make a decoded object recognizable as a run report. Derived
# from the schema rather than listed here, so a new contract field cannot make
# a report unrecognizable by being the only field a run sent. The two legacy
# names are the old status vocabulary the worker still maps.
REPORT_KEYS = frozenset(report_schema.REPORT_SCHEMA["properties"]) | {
    "new_status",
    "status",
}

# How many times to unwrap a `{"raw": ...}` shell before giving up. One is
# enough for every case seen; the bound exists so a self-referential payload
# cannot spin.
_MAX_UNWRAP = 3


def is_unparsed(structured: Any) -> bool:
    """True when the CLI handed back its could-not-parse placeholder rather
    than a report. This is the check that must gate acceptance: the
    placeholder is a perfectly ordinary dict and passes every isinstance test
    a report would."""
    return isinstance(structured, dict) and UNPARSED_KEY in structured


def looks_like_report(obj: Any) -> bool:
    """True when this object carries at least one field of the run contract.

    The test is deliberately "any contract field", not "all" or "summary": a
    reflect run legitimately reports two fields, and a build run that only
    answers a question reports one.
    """
    return isinstance(obj, dict) and bool(REPORT_KEYS & obj.keys())


def unwrap_envelope(structured: Any) -> Optional[dict]:
    """The report inside an argument envelope, or None if this is not one.

    A dict holding no contract field at all is not a report, whatever the CLI
    called it. When exactly one of its values decodes to something that IS a
    report, that is the thing the agent submitted and the outer dict is
    packaging - so hand back the inside. Both `{"parameter": "<json text>"}`
    and `{"parameter": {...}}` are read, because a wrapper that stringifies
    once may not stringify next time.

    Ambiguity is refused rather than guessed at: if two values both look like
    reports there is no way to tell which the agent meant, and picking one
    would silently drop the other.
    """
    if not isinstance(structured, dict) or looks_like_report(structured):
        return None
    found = []
    for value in structured.values():
        obj = decode(value) if isinstance(value, str) else value
        if looks_like_report(obj):
            found.append(obj)
    return found[0] if len(found) == 1 else None


def raw_text(structured: Any) -> Optional[str]:
    """The unparsed tool input as text, from either shape the CLI emits."""
    if not is_unparsed(structured):
        return None
    value = structured[UNPARSED_KEY]
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("raw"), str):
        return value["raw"]
    return None


def recover(structured: Any) -> Optional[dict]:
    """The report the agent meant to submit, or None if nothing usable is in
    there. Never raises."""
    try:
        return _recover(structured)
    except Exception:  # noqa: BLE001 - a rescue attempt must never kill a run
        log.exception("Could not recover an unparsed report")
        return None


def _recover(structured: Any) -> Optional[dict]:
    text = raw_text(structured)
    if not text:
        return None
    obj = decode(text)
    # A model that stringifies its tool argument submits {"raw": "<report>"} -
    # which is what run 897 did, and the whole report survives inside it.
    for _ in range(_MAX_UNWRAP):
        if not isinstance(obj, dict) or REPORT_KEYS & obj.keys():
            break
        if set(obj) - {"raw", "len"} or not isinstance(obj.get("raw"), str):
            break
        obj = decode(obj["raw"])
    if isinstance(obj, dict) and REPORT_KEYS & obj.keys():
        return obj
    return None


def decode(text: str) -> Any:
    """Parse the text, salvaging the complete leading fields if it was cut off
    mid-value (the CLI truncates its own placeholder at 2048 characters)."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _salvage(text)


def _salvage(text: str) -> Optional[dict]:
    """Every top-level field that arrived complete, from a truncated object.

    Scans for the last comma at nesting depth 1 outside a string - the point
    up to which every member is whole - and closes the object there. That
    deliberately keeps the FIRST fields, which is where the value is: `summary`
    leads the contract, and the summary bullets are the line Wes actually
    reads on the project page.
    """
    if not text.startswith("{"):
        return None
    depth = 0
    in_string = False
    escaped = False
    cut: Optional[int] = None
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == "," and depth == 1:
            cut = i
    if cut is None:
        return None
    try:
        got = json.loads(text[:cut] + "}")
    except json.JSONDecodeError:
        return None
    return got if isinstance(got, dict) else None


def failure_note(structured: Any) -> str:
    """The line to show where the report should have been. Says what happened
    and that the work itself is not in doubt, because it is not: the agent ran
    to completion and its commits are on disk - only the report was lost."""
    text = raw_text(structured) or ""
    size = f"{len(text)} characters" if text else "no text"
    return (
        "The agent finished its work, but the portal could not read its report: "
        f"the Claude CLI could not parse the StructuredOutput call ({size}), so "
        "the summary, journal entry and any todo or stage changes in it were "
        "lost. Whatever this run committed is still in the workspace."
    )

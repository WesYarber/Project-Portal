"""The JSON Schema handed to `claude -p --json-schema` for run reports.

Since CLI 2.1.2xx, `--json-schema` makes the CLI expose a StructuredOutput
tool to the agent and validate what it submits at the tool-call layer (the
model is shown the mismatch and retries), returning the parsed object in the
result event's `structured_output` field. That replaces the honor-system
".portal/report.json" convention as the primary report channel - the file
stays accepted forever as a fallback (see agent_runner._pick_report).

Design rules for this schema:

- Nothing is `required`. The worker already treats every report field as
  optional and validates semantics itself (stage vocabulary, todo ids,
  learning ops), and reflect/compact runs legitimately report only one or two
  fields. A schema that demands fields the run has no use for would burn
  turns on retries without making any report better.
- `additionalProperties` stays open (the JSON Schema default). Old vocabulary
  (`new_status`, `status`) and future fields must never make a report
  unsubmittable - the worker ignores what it does not know.
- Types are permissive unions rather than exact shapes for the same reason:
  the one hard enum is `new_stage`, where rejecting "done"/"abandoned" at
  submission time enforces a rule the contract already states (only Wes
  finishes a project) with immediate feedback instead of a silent drop.
"""
from __future__ import annotations

import json

REPORT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "array", "items": {"type": "string"}},
        "journal_entry_md": {"type": ["string", "null"]},
        "new_stage": {"enum": ["review", "active", None]},
        "request_build": {"type": ["boolean", "null"]},
        "blocked_on": {"type": ["string", "null"]},
        "kind": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "context": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question"],
            },
        },
        "todo_updates": {
            "type": "object",
            "properties": {
                "add": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "owner": {"type": "string"},
                            # Which human, when `owner` is "user" and the
                            # install has more than one person in it. A name;
                            # unresolvable values are dropped rather than
                            # guessed at. See todos._person_ref.
                            "person": {"type": "string"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["text"],
                    },
                },
                "done": {"type": "array", "items": {"type": "integer"}},
                "tags": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "subprojects": {
            "type": "object",
            "properties": {
                "add": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "kind": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                },
            },
        },
        "preview_url": {"type": ["string", "null"]},
        # Plain strings take the auto ADD/UPDATE/NOOP path; the object form
        # ({"op": "delete", "text": ...}) forces an op. Both are legal here.
        "learnings": {"type": "array", "items": {"type": ["string", "object"]}},
        "suggestion": {
            "type": ["object", "null"],
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
        },
    },
}


def schema_json() -> str:
    """The schema as the compact JSON string the CLI flag takes."""
    return json.dumps(REPORT_SCHEMA, separators=(",", ":"))

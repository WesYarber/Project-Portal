"""Natural-language intent parsing (no model call - `parse_intent` is pure)."""
from __future__ import annotations

import json

from app import db, nl

QIDS = {1, 2}
SLUGS = {"portal", "lamp"}


def parse(payload, qids=QIDS, slugs=SLUGS):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return nl.parse_intent(raw, qids, slugs)


def test_answer_intent():
    out = parse({"intent": "answer", "question_id": 2, "text": "use sonnet", "confidence": 0.9})
    assert out["intent"] == "answer"
    assert out["question_id"] == 2
    assert out["text"] == "use sonnet"


def test_idea_intent():
    out = parse({"intent": "idea", "text": "build a dice tower", "confidence": 0.8})
    assert out["intent"] == "idea"
    assert out["text"] == "build a dice tower"


def test_note_intent_resolves_slug():
    out = parse({"intent": "note", "project_slug": "lamp", "text": "make it blue", "confidence": 0.7})
    assert out["intent"] == "note"
    assert out["project_slug"] == "lamp"


def test_set_status_intent():
    out = parse({"intent": "set_status", "project_slug": "portal", "status": "review", "confidence": 0.9})
    assert out["intent"] == "set_status"
    assert out["status"] == "review"


def test_json_wrapped_in_prose_and_fences_is_recovered():
    raw = 'Sure! Here you go:\n```json\n{"intent": "status", "confidence": 0.95}\n```\nHope that helps.'
    assert parse(raw)["intent"] == "status"


def test_garbage_becomes_unknown():
    assert parse("not json at all")["intent"] == "unknown"
    assert parse("")["intent"] == "unknown"
    assert parse("[1, 2, 3]")["intent"] == "unknown"


def test_invented_question_id_is_rejected():
    # The model referencing a question that doesn't exist must not answer a
    # random other question - it degrades to unknown.
    out = parse({"intent": "answer", "question_id": 99, "text": "yes", "confidence": 0.9})
    assert out["intent"] == "unknown"


def test_invented_slug_is_rejected():
    out = parse({"intent": "note", "project_slug": "nope", "text": "hi", "confidence": 0.9})
    assert out["intent"] == "unknown"


def test_set_status_without_status_is_rejected():
    out = parse({"intent": "set_status", "project_slug": "portal", "confidence": 0.9})
    assert out["intent"] == "unknown"


def test_invalid_status_is_rejected():
    out = parse({"intent": "set_status", "project_slug": "portal", "status": "on fire", "confidence": 0.9})
    assert out["intent"] == "unknown"


def test_unknown_intent_name_is_rejected():
    assert parse({"intent": "launch_missiles", "confidence": 1.0})["intent"] == "unknown"


def test_confidence_is_clamped():
    assert parse({"intent": "status", "confidence": 5})["confidence"] == 1.0
    assert parse({"intent": "status", "confidence": -3})["confidence"] == 0.0
    assert parse({"intent": "status", "confidence": "high"})["confidence"] == 0.0


def test_blank_text_becomes_none():
    assert parse({"intent": "status", "text": "   ", "confidence": 0.9})["text"] is None


def test_build_context_lists_questions_and_projects():
    project = db.create_project("Portal", stage="active", build_approved=True, slug="portal")
    db.create_question(project["id"], "Which model?", "context here")
    prompt = nl.build_context(db.open_questions(), db.list_projects_by_stage(["active"]), "use opus")
    assert "Which model?" in prompt
    assert "'portal'" in prompt
    assert "use opus" in prompt


def test_build_context_handles_empty_state():
    prompt = nl.build_context([], [], "hello")
    assert "(none)" in prompt
    assert "hello" in prompt


def test_cancel_intent_needs_no_referent():
    # Unlike note/set_status/run, "stop it" targets whatever is live, so a null
    # project_slug must survive parsing rather than degrading to unknown.
    out = parse({"intent": "cancel", "project_slug": None, "confidence": 0.9})
    assert out["intent"] == "cancel"
    assert out["project_slug"] is None


def test_cancel_intent_keeps_a_valid_slug():
    out = parse({"intent": "cancel", "project_slug": "portal", "confidence": 0.9})
    assert out["intent"] == "cancel"
    assert out["project_slug"] == "portal"


def test_cancel_intent_drops_an_invented_slug():
    # An invented slug must not become a targeted cancel of the wrong project;
    # it degrades to "cancel whatever is running", which the bot then confirms.
    out = parse({"intent": "cancel", "project_slug": "nope", "confidence": 0.9})
    assert out["intent"] == "cancel"
    assert out["project_slug"] is None


def test_build_context_describes_the_live_run():
    project = db.create_project("Portal", stage="active", build_approved=True, slug="portal")
    run_id = db.create_run(project["id"], "build", "opus")
    prompt = nl.build_context([], [], "stop that", db.active_run())
    assert f"run #{run_id}" in prompt
    assert "task=build" in prompt


def test_build_context_says_when_nothing_is_running():
    prompt = nl.build_context([], [], "stop that")
    assert "nothing is running right now" in prompt


def test_is_enabled_follows_setting():
    db.set_setting("telegram_natural_language", "1")
    assert nl.is_enabled() is True
    db.set_setting("telegram_natural_language", "0")
    assert nl.is_enabled() is False

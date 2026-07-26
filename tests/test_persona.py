"""GLaDOS voice toggle."""
from __future__ import annotations

from app import db, persona


def test_plain_voice_is_literal():
    db.set_setting("glados_mode", "0")
    assert persona.say("answer_recorded", qid=7) == "Got it, recorded your answer to Q7."


def test_glados_voice_differs_but_keeps_the_facts():
    db.set_setting("glados_mode", "1")
    text = persona.say("answer_recorded", qid=7)
    assert text != "Got it, recorded your answer to Q7."
    assert "Q7" in text


def test_every_line_renders_in_both_voices():
    # Guards against a GLaDOS variant using a placeholder the caller doesn't pass.
    kwargs = {"qid": 3, "title": "Dice Tower", "url": "http://x/y", "status": "review",
              "run_id": 7, "count": 2, "listing": "#1 build on 'A'",
              "model": "sonnet", "options": "opus, sonnet", "value": "gpt",
              "slugs": "dice-tower, fridge"}
    for key in persona._LINES:  # noqa: SLF001 - exhaustiveness check
        for voice in (persona.PLAIN, persona.GLADOS):
            assert persona.say(key, voice=voice, **kwargs)


def test_variant_choice_is_stable():
    db.set_setting("glados_mode", "1")
    assert persona.say("idea_created", title="A", url="u") == persona.say("idea_created", title="A", url="u")


def test_different_events_can_get_different_variants():
    db.set_setting("glados_mode", "1")
    rendered = {persona.say("idea_created", title=f"Project {i}", url="u") for i in range(30)}
    assert len(rendered) > 1


def test_unknown_key_does_not_raise():
    assert persona.say("no_such_key", qid=1) == "no_such_key"


def test_current_voice_follows_setting():
    db.set_setting("glados_mode", "1")
    assert persona.current_voice() == persona.GLADOS
    db.set_setting("glados_mode", "0")
    assert persona.current_voice() == persona.PLAIN


def test_notification_body_is_never_rewritten():
    body = "The agent said something specific and important."
    db.set_setting("glados_mode", "1")
    assert body in persona.decorate_notification("New question", body)
    db.set_setting("glados_mode", "0")
    assert persona.decorate_notification("New question", body) == f"New question\n{body}"

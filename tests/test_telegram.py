"""Telegram-side control of a run: stopping one, and seeing what's live.

The bot is the interface Wes actually has on his phone, so "what is happening"
and "stop it" have to work there without opening the web UI. These tests drive
the handlers directly and capture outbound messages instead of calling Telegram.
"""
from __future__ import annotations

import pytest

from app import db, notify, persona, telegram_bot, worker

CHAT = "12345"


@pytest.fixture
def sent(monkeypatch):
    """Capture every outbound Telegram message instead of sending it."""
    messages: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str) -> None:
        messages.append((chat_id, text))

    monkeypatch.setattr(notify, "send_telegram_text", fake_send)
    # Plain voice keeps assertions about wording stable.
    db.set_setting("glados_mode", "0")
    return messages


@pytest.fixture
def cancels(monkeypatch):
    """Record cancel_run calls and control their outcome."""
    calls: list[int] = []
    outcome = {"value": "cancelled"}

    def fake_cancel(run_id: int) -> str:
        calls.append(run_id)
        return outcome["value"]

    monkeypatch.setattr(worker, "cancel_run", fake_cancel)
    return calls, outcome


def _running(task: str = "build") -> tuple[int, int]:
    project = db.create_project("Portal", "desc", stage="active", build_approved=True)
    run_id = db.create_run(project["id"], task, "opus")
    return project["id"], run_id


# --- cancelling -----------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_stops_the_live_run(sent, cancels):
    calls, _ = cancels
    _, run_id = _running()

    await telegram_bot._cancel_active_run(CHAT)

    assert calls == [run_id]
    assert f"#{run_id}" in sent[0][1]
    assert "Portal" in sent[0][1]


@pytest.mark.asyncio
async def test_cancel_with_nothing_running_says_so(sent, cancels):
    calls, _ = cancels
    await telegram_bot._cancel_active_run(CHAT)
    assert calls == []
    assert sent[0][1] == persona.say("run_none", voice=persona.PLAIN)


@pytest.mark.asyncio
async def test_orphaned_run_reports_differently(sent, cancels):
    _, outcome = cancels
    outcome["value"] = "orphaned"
    _, run_id = _running()

    await telegram_bot._cancel_active_run(CHAT)
    assert "already dead" in sent[0][1]


@pytest.mark.asyncio
async def test_run_that_finished_mid_request_reports_nothing_running(sent, cancels):
    # The row was 'running' at lookup time but had settled by the kill. We must
    # not claim to have stopped something that stopped on its own.
    _, outcome = cancels
    outcome["value"] = "not_running"
    _running()

    await telegram_bot._cancel_active_run(CHAT)
    assert sent[0][1] == persona.say("run_none", voice=persona.PLAIN)


@pytest.mark.asyncio
async def test_naming_a_project_that_is_not_running_does_not_kill_the_other_run(sent, cancels):
    calls, _ = cancels
    _, run_id = _running()
    db.create_project("Lamp", "desc", stage="active", build_approved=True)

    await telegram_bot._cancel_active_run(CHAT, project_slug="lamp")

    # The whole point: a mistargeted "stop the lamp one" must not kill Portal.
    assert calls == []
    assert f"#{run_id}" in sent[0][1] and "Portal" in sent[0][1]


@pytest.mark.asyncio
async def test_naming_the_running_project_does_cancel(sent, cancels):
    calls, _ = cancels
    _, run_id = _running()
    await telegram_bot._cancel_active_run(CHAT, project_slug="portal")
    assert calls == [run_id]


@pytest.mark.asyncio
async def test_reflect_run_has_no_project_slug_but_is_still_cancellable(sent, cancels):
    calls, _ = cancels
    run_id = db.create_run(None, "reflect", "opus")
    await telegram_bot._cancel_active_run(CHAT)
    assert calls == [run_id]
    assert "memory / reflect" in sent[0][1]


# --- routing --------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/stop", "/cancel", "/STOP"])
async def test_slash_stop_cancels_without_the_nl_router(sent, cancels, monkeypatch, text):
    calls, _ = cancels
    _, run_id = _running()

    def boom(*_a, **_kw):  # the router must not be consulted
        raise AssertionError("NL router should not be called for /stop")

    monkeypatch.setattr(telegram_bot.nl, "classify", boom)
    db.set_setting("telegram_chat_id", CHAT)

    await telegram_bot._handle_update(
        {"message": {"chat": {"id": CHAT}, "text": text, "message_id": 1}}
    )
    assert calls == [run_id]


@pytest.mark.asyncio
async def test_cancel_intent_is_dispatched(sent, cancels):
    calls, _ = cancels
    _, run_id = _running()
    await telegram_bot._dispatch_intent(
        {"intent": "cancel", "project_slug": None, "confidence": 0.9}, "stop it", CHAT
    )
    assert calls == [run_id]


@pytest.mark.asyncio
async def test_low_confidence_cancel_becomes_an_idea_not_a_kill(sent, cancels):
    calls, _ = cancels
    _running()
    await telegram_bot._dispatch_intent(
        {"intent": "cancel", "project_slug": None, "confidence": 0.1}, "stop??", CHAT
    )
    assert calls == []


# --- status ---------------------------------------------------------------

def test_status_summary_leads_with_the_live_run():
    project = db.create_project("Portal", "desc", stage="active", build_approved=True)
    run_id = db.create_run(project["id"], "build", "opus")
    db.update_run_activity(run_id, "> Bash(pytest -q)", 12)

    summary = telegram_bot._status_summary()
    assert summary.splitlines()[0] == f"Running: #{run_id} build on Portal"
    assert "> Bash(pytest -q)" in summary
    assert "Send 'stop' to end it." in summary


def test_status_summary_when_idle():
    summary = telegram_bot._status_summary()
    # Idle now says *why* - see worker.idle_reason().
    assert summary.splitlines()[0].startswith("Nothing running")


def test_status_summary_reports_the_remaining_budget():
    db.set_setting("max_runs_per_day", "8")
    db.create_run(None, "reflect", "opus")
    summary = telegram_bot._status_summary()
    assert "Runs today: 1/8 (7 left)" in summary


def test_status_summary_includes_the_bonus_budget():
    db.set_setting("max_runs_per_day", "8")
    db.grant_bonus_runs(3)
    assert "Runs today: 0/11 (11 left)" in telegram_bot._status_summary()


# --- /model ----------------------------------------------------------------

async def _say(text: str) -> None:
    db.set_setting("telegram_chat_id", CHAT)
    await telegram_bot._handle_update(
        {"message": {"chat": {"id": CHAT}, "text": text, "message_id": 1}}
    )


@pytest.mark.asyncio
async def test_slash_model_reports_the_current_router_model(sent, monkeypatch):
    monkeypatch.setattr(telegram_bot.nl, "classify", _never_called)
    await _say("/model")
    assert "sonnet" in sent[0][1]


@pytest.mark.asyncio
async def test_slash_model_switches_the_router_model(sent, monkeypatch):
    monkeypatch.setattr(telegram_bot.nl, "classify", _never_called)
    await _say("/model haiku")
    assert db.get_setting("telegram_model") == "haiku"
    assert "haiku" in sent[0][1]


@pytest.mark.asyncio
async def test_slash_model_rejects_an_unknown_name(sent, monkeypatch):
    monkeypatch.setattr(telegram_bot.nl, "classify", _never_called)
    db.set_setting("telegram_model", "sonnet")
    await _say("/model gpt-9")
    assert db.get_setting("telegram_model") == "sonnet"
    assert "gpt-9" in sent[0][1]


@pytest.mark.asyncio
async def test_slash_model_never_touches_the_agent_model(sent, monkeypatch):
    monkeypatch.setattr(telegram_bot.nl, "classify", _never_called)
    db.set_setting("worker_model", "opus")
    await _say("/model haiku")
    assert db.get_setting("worker_model") == "opus"


def _never_called(*_a, **_kw):
    raise AssertionError("the NL router should not be consulted for a slash command")


# --- answering by the number Wes was shown ---------------------------------

def _open_question(text: str = "Which one?"):
    project = db.create_project("Dice Tower", "desc", stage="active", build_approved=True)
    return db.create_question(project["id"], text)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["Q", "q", "#"])
async def test_answering_by_slot(sent, monkeypatch, prefix):
    monkeypatch.setattr(telegram_bot.nl, "classify", _never_called)
    q = _open_question()
    await _say(f"{prefix}{q['slot']} the left one")
    assert db.get_question(q["id"])["answer"] == "the left one"


@pytest.mark.asyncio
async def test_slot_wins_over_a_row_id_with_the_same_number(sent, monkeypatch):
    """The dangerous case: an old question's id collides with a live slot.

    Wes types the number he was just shown, so the live question must win -
    answering a closed question instead would silently swallow the reply.
    """
    monkeypatch.setattr(telegram_bot.nl, "classify", _never_called)
    project = db.create_project("Dice Tower", "desc", stage="active", build_approved=True)
    old = db.create_question(project["id"], "old?")
    db.answer_question_and_resume(old["id"], "already handled")
    live = db.create_question(project["id"], "live?")
    assert live["slot"] == 1 and old["id"] == 1

    await _say("Q1 the new answer")
    assert db.get_question(live["id"])["answer"] == "the new answer"
    assert db.get_question(old["id"])["answer"] == "already handled"


@pytest.mark.asyncio
async def test_slash_answer_accepts_a_q_prefix(sent, monkeypatch):
    monkeypatch.setattr(telegram_bot.nl, "classify", _never_called)
    q = _open_question()
    await _say(f"/answer Q{q['slot']} yes please")
    assert db.get_question(q["id"])["answer"] == "yes please"


@pytest.mark.asyncio
async def test_confirmation_echoes_the_slot_not_the_row_id(sent, monkeypatch):
    monkeypatch.setattr(telegram_bot.nl, "classify", _never_called)
    project = db.create_project("Dice Tower", "desc", stage="active", build_approved=True)
    for _ in range(3):
        old = db.create_question(project["id"], "old?")
        db.answer_question_and_resume(old["id"], "x")
    live = db.create_question(project["id"], "live?")

    await _say("Q1 done")
    assert "Q1" in sent[0][1]
    assert f"Q{live['id']}" not in sent[0][1]


@pytest.mark.asyncio
async def test_a_telegram_reply_still_resolves_by_row_id(sent, monkeypatch):
    """Replying to a notification carries the message id, which maps to a real
    row id - that must not be re-read as a slot."""
    monkeypatch.setattr(telegram_bot.nl, "classify", _never_called)
    project = db.create_project("Dice Tower", "desc", stage="active", build_approved=True)
    old = db.create_question(project["id"], "old?")
    db.answer_question_and_resume(old["id"], "x")
    live = db.create_question(project["id"], "live?")
    db.set_question_telegram_msg_id(old["id"], 99)
    db.set_setting("telegram_chat_id", CHAT)

    await telegram_bot._handle_update({"message": {
        "chat": {"id": CHAT}, "text": "revised", "message_id": 2,
        "reply_to_message": {"message_id": 99},
    }})
    assert db.get_question(old["id"])["answer"] == "revised"
    assert db.get_question(live["id"])["answer"] is None


@pytest.mark.asyncio
async def test_status_lists_questions_by_slot_and_project(sent, monkeypatch):
    monkeypatch.setattr(telegram_bot.nl, "classify", _never_called)
    _open_question("Which colour?")
    await _say("/status")
    assert "Q1: [Dice Tower]: Which colour?" in sent[0][1]

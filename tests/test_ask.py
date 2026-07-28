"""Asking a question about a project without setting anything in motion.

Wes's note: "add a way to just ask a question of a project without taking
actions (like a /btw on Claude code)". The properties that matter, and that
these tests pin down:

- an ask never becomes a run (no `runs` row, no budget spent, no status change);
- the subprocess is genuinely read-only, enforced by flags rather than by the
  prompt asking nicely;
- both halves land in the journal, so the next real run reads the exchange.

`ask.start` answers in a background task on purpose, so anything that goes
through it is driven here inside one `asyncio.run` and awaited explicitly -
see `_settle`.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from app import ask, config, db, nl, persona, telegram_bot


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project(temp_data_dir):
    return db.create_project("Fridge Board", slug="fridge", stage="active", build_approved=True)


@pytest.fixture(autouse=True)
def _clear_pending():
    ask._PENDING.clear()  # noqa: SLF001
    yield
    ask._PENDING.clear()  # noqa: SLF001


def _kinds(project_id):
    return [(row["author"], row["kind"]) for row in db.list_journal(project_id, limit=50)]


def _bodies(project_id):
    return [row["content_md"] or "" for row in db.list_journal(project_id, limit=50)]


# --- the read-only posture -------------------------------------------------

def test_command_allows_only_read_only_tools():
    cmd = ask.build_command("prompt", "sonnet")
    allowed = cmd[cmd.index("--allowedTools") + 1 : cmd.index("--disallowedTools")]
    assert set(allowed) == set(ask.ALLOWED_TOOLS)
    assert "Bash" not in allowed and "Edit" not in allowed and "Write" not in allowed


def test_command_denies_the_mutating_tools():
    cmd = ask.build_command("prompt", "sonnet")
    denied = cmd[cmd.index("--disallowedTools") + 1 :]
    for tool in ("Bash", "Edit", "Write"):
        assert tool in denied


def test_command_never_skips_permissions():
    """The whole point: an ask must not be able to grant itself anything."""
    cmd = ask.build_command("prompt", "sonnet")
    assert "--dangerously-skip-permissions" not in cmd
    assert "--allow-dangerously-skip-permissions" not in cmd


def test_command_carries_the_chosen_model():
    cmd = ask.build_command("prompt", "haiku")
    assert cmd[cmd.index("--model") + 1] == "haiku"


# --- the prompt ------------------------------------------------------------

def test_prompt_contains_the_question_and_the_project(project):
    db.add_journal(project["id"], "agent", "progress", "Picked a 7.5in panel.")
    prompt = ask.build_prompt(db.get_project(project["id"]), "why that panel?")
    assert "why that panel?" in prompt
    assert "Fridge Board" in prompt
    assert "Picked a 7.5in panel." in prompt


def test_prompt_forbids_a_report_and_forbids_doing_the_work(project):
    prompt = ask.build_prompt(project, "how's it going?")
    assert "report.json" in prompt
    assert "read-only" in prompt.lower()
    # It must not carry the agent contract, which is the thing that tells a run
    # to write code and move the status.
    assert "Task: BUILD" not in prompt


# --- model selection -------------------------------------------------------

def test_ask_model_defaults_to_the_configured_default(temp_data_dir):
    assert ask.ask_model() == config.ASK_MODEL


def test_ask_model_ignores_junk(temp_data_dir):
    db.set_setting("ask_model", "gpt-9")
    assert ask.ask_model() == config.ASK_MODEL


def test_ask_model_honors_a_valid_setting(temp_data_dir):
    db.set_setting("ask_model", "haiku")
    assert ask.ask_model() == "haiku"


def test_ask_model_is_savable_from_settings(client):
    resp = client.post(
        "/settings",
        data={"_fields": "worker_model,ask_model", "worker_model": "opus", "ask_model": "haiku"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.get_setting("ask_model") == "haiku"


def test_saving_another_section_leaves_ask_model_alone(client):
    db.set_setting("ask_model", "haiku")
    client.post("/settings", data={"_fields": "ntfy_topic", "ntfy_topic": "portal"})
    assert db.get_setting("ask_model") == "haiku"


# --- start / answer --------------------------------------------------------

def test_start_journals_the_question_before_the_answer(project, monkeypatch):
    """The page that triggered an ask must show the question on the very next
    request, minutes before the answer exists."""
    _capture(monkeypatch)
    monkeypatch.setattr(ask, "run_ask", _canned("Because it's readable at 2m."))

    async def scenario():
        ask.start(project["id"], "why that panel?")
        assert ("user", "ask") in _kinds(project["id"])
        assert ("agent", "answer") not in _kinds(project["id"])
        assert ask.pending(project["id"])
        await _settle()

    asyncio.run(scenario())
    assert ("agent", "answer") in _kinds(project["id"])
    assert any("readable at 2m" in body for body in _bodies(project["id"]))
    assert not ask.pending(project["id"])


def test_an_ask_is_not_a_run(project, monkeypatch):
    """No runs row, no budget spent, no status change - that's the deal."""
    _capture(monkeypatch)
    monkeypatch.setattr(ask, "run_ask", _canned("Fine."))
    before = db.get_project(project["id"])["stage"]
    asyncio.run(ask.answer(project["id"], "status?"))
    assert db.list_runs(project["id"]) == []
    assert db.count_runs_today(project["id"]) == 0
    assert db.get_project(project["id"])["stage"] == before


def test_a_failed_ask_still_answers(project, monkeypatch):
    """A silent non-answer is indistinguishable from the portal losing it."""
    _capture(monkeypatch)
    monkeypatch.setattr(ask, "run_ask", _canned(""))
    asyncio.run(ask.answer(project["id"], "why?"))
    latest = db.list_journal(project["id"], limit=1)[0]
    assert latest["kind"] == "answer"
    assert "couldn't answer" in latest["content_md"]


def test_a_crashing_ask_clears_the_pending_flag(project, monkeypatch):
    _capture(monkeypatch)

    async def boom(prompt, cwd, model):
        raise RuntimeError("claude fell over")

    monkeypatch.setattr(ask, "run_ask", boom)
    asyncio.run(ask.answer(project["id"], "why?"))
    assert not ask.pending(project["id"])
    assert db.list_journal(project["id"], limit=1)[0]["kind"] == "answer"


def test_an_ask_on_a_deleted_project_clears_pending(temp_data_dir, monkeypatch):
    _capture(monkeypatch)
    asyncio.run(ask.answer(9999, "anyone home?"))
    assert not ask.pending(9999)


def test_run_ask_returns_empty_when_claude_is_missing(project, monkeypatch, tmp_path):
    monkeypatch.setattr(ask, "build_command", lambda prompt, model: ["definitely-not-a-binary"])
    assert asyncio.run(ask.run_ask("hi", tmp_path, "sonnet")) == ""


# --- the web route ---------------------------------------------------------

def test_asking_from_the_web_starts_one(client, project, monkeypatch):
    started = []
    monkeypatch.setattr(ask, "start", lambda pid, q, **kw: started.append((pid, q)))
    resp = client.post(
        f"/project/{project['slug']}/ask",
        data={"question": "why that panel?"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert started == [(project["id"], "why that panel?")]


def test_empty_question_does_nothing(client, project, monkeypatch):
    started = []
    monkeypatch.setattr(ask, "start", lambda pid, q, **kw: started.append((pid, q)))
    client.post(f"/project/{project['slug']}/ask", data={"question": "   "})
    assert started == []


def test_a_second_ask_while_one_is_thinking_is_refused(client, project, monkeypatch):
    started = []
    monkeypatch.setattr(ask, "start", lambda pid, q, **kw: started.append((pid, q)))
    ask._PENDING.add(project["id"])  # noqa: SLF001
    client.post(f"/project/{project['slug']}/ask", data={"question": "again?"})
    assert started == []


def test_project_page_shows_the_ask_box(client, project):
    """Folded behind an "ask project" button at the top of the page since Wes's
    note of 2026-07-21 - still one click away, no longer a section."""
    html = client.get(f"/project/{project['slug']}").text
    assert f"/project/{project['slug']}/ask" in html
    assert ">ask project</summary>" in html


def test_project_page_says_it_is_thinking(client, project):
    ask._PENDING.add(project["id"])  # noqa: SLF001
    html = client.get(f"/project/{project['slug']}").text
    assert "data-ask-pending" in html


def test_ask_on_a_missing_project_404s(client):
    assert client.post("/project/nope/ask", data={"question": "hi"}).status_code == 404


# --- Telegram --------------------------------------------------------------

def test_btw_resolves_a_slug(project, monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(ask, "run_ask", _canned("An answer."))
    _telegram("/btw fridge is the plan written?")
    assert ("user", "ask") in _kinds(project["id"])
    assert any("is the plan written?" in body for body in _bodies(project["id"]))
    assert sent  # he gets told it started


def test_btw_resolves_a_title_prefix(project, monkeypatch):
    _capture(monkeypatch)
    monkeypatch.setattr(ask, "run_ask", _canned("An answer."))
    _telegram("/btw Fridge what panel?")
    assert ("user", "ask") in _kinds(project["id"])


def test_ask_is_an_alias_for_btw(project, monkeypatch):
    _capture(monkeypatch)
    monkeypatch.setattr(ask, "run_ask", _canned("An answer."))
    _telegram("/ask fridge what panel?")
    assert ("user", "ask") in _kinds(project["id"])


def test_btw_without_a_project_asks_which(project, monkeypatch):
    sent = _capture(monkeypatch)
    _telegram("/btw whats going on")
    assert db.list_journal(project["id"], limit=50) == []
    assert "project" in sent[0].lower()


def test_an_ambiguous_prefix_resolves_to_nothing(temp_data_dir):
    db.create_project("Fridge Board", slug="fridge-board")
    db.create_project("Fridge Magnet", slug="fridge-magnet")
    assert telegram_bot.resolve_project_token("fridge") is None


def test_an_exact_slug_beats_an_ambiguous_prefix(temp_data_dir):
    db.create_project("Fridge", slug="fridge")
    db.create_project("Fridge Magnet", slug="fridge-magnet")
    assert telegram_bot.resolve_project_token("fridge")["slug"] == "fridge"


def test_btw_with_no_question_says_so(project, monkeypatch):
    sent = _capture(monkeypatch)
    _telegram("/btw fridge")
    assert db.list_journal(project["id"], limit=50) == []
    assert "question" in sent[0].lower()


def test_btw_answer_goes_back_to_the_chat(project, monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(ask, "run_ask", _canned("Yes, PLAN.md exists."))
    _telegram("/btw fridge is the plan written?")
    assert any("PLAN.md exists" in message for message in sent)


def test_btw_never_queues_a_run(project, monkeypatch):
    """`/btw` must not be a back door into starting work."""
    _capture(monkeypatch)
    monkeypatch.setattr(ask, "run_ask", _canned("An answer."))
    queued = []
    monkeypatch.setattr(
        "app.worker.queue_manual_run", _async_recorder(queued)
    )
    _telegram("/btw fridge should we start?")
    assert queued == []
    assert db.list_runs(project["id"]) == []


def test_a_second_btw_while_thinking_is_refused(project, monkeypatch):
    sent = _capture(monkeypatch)
    ask._PENDING.add(project["id"])  # noqa: SLF001
    _telegram("/btw fridge and another thing")
    assert db.list_journal(project["id"], limit=50) == []
    assert "still" in sent[0].lower()


def test_ask_is_a_router_intent():
    parsed = nl.parse_intent(
        '{"intent":"ask","project_slug":"fridge","text":"why that panel?","confidence":0.9}',
        set(),
        {"fridge"},
    )
    assert parsed["intent"] == "ask"
    assert parsed["project_slug"] == "fridge"


def test_ask_without_a_project_is_not_actionable():
    parsed = nl.parse_intent('{"intent":"ask","text":"why?","confidence":0.9}', set(), {"fridge"})
    assert parsed["intent"] == "unknown"


def test_router_intent_ask_starts_one(project, monkeypatch):
    _capture(monkeypatch)
    monkeypatch.setattr(ask, "run_ask", _canned("An answer."))
    intent = {
        "intent": "ask",
        "project_slug": "fridge",
        "text": "why that panel?",
        "confidence": 0.9,
    }

    async def scenario():
        await telegram_bot._dispatch_intent(intent, "why that panel?", "42")  # noqa: SLF001
        await _settle()

    asyncio.run(scenario())
    assert ("user", "ask") in _kinds(project["id"])


def test_help_mentions_btw():
    for voice in (persona.PLAIN, persona.GLADOS):
        assert "/btw" in persona.say("help", voice=voice)


# --- helpers ---------------------------------------------------------------

def _canned(text):
    async def fake(prompt, cwd, model):
        return text

    return fake


def _async_recorder(sink):
    async def fake(*args, **kwargs):
        sink.append((args, kwargs))

    return fake


def _capture(monkeypatch):
    """Collect outbound Telegram/ntfy text instead of sending it."""
    sent: list[str] = []

    async def fake_send(chat_id, text):
        sent.append(text)

    async def fake_notify(title, message, **kwargs):
        sent.append(message)

    monkeypatch.setattr("app.notify.send_telegram_text", fake_send)
    monkeypatch.setattr("app.notify.notify", fake_notify)
    return sent


async def _settle():
    """Await the background answer tasks `ask.start` spawned."""
    for _ in range(10):
        tasks = [t for t in ask._TASKS if not t.done()]  # noqa: SLF001
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def _telegram(text: str) -> None:
    """Feed one message through the bot's handler and let the ask finish."""

    async def scenario():
        await telegram_bot._handle_update(  # noqa: SLF001
            {"message": {"text": text, "chat": {"id": 42}, "message_id": 1}}
        )
        await _settle()

    asyncio.run(scenario())

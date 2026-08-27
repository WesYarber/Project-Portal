"""Every Telegram copy of a question settles when it is answered (#416).

A question that reaches two chats used to remember only the first message id,
so whoever did NOT answer kept a copy with live-looking buttons forever. The
`question_messages` table records (question, chat, message id, sent text) for
every copy, and `notify.settle_question_copies` rewrites all of them - from a
button tap, a typed reply, or any of the web routes - so the outcome lands on
every phone the question did.
"""
from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from app import db, main, notify, quickreplies, telegram_bot


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def project():
    return db.create_project("Dice Tower", description="A thing.", stage="active",
                             build_approved=True, slug="dice-tower")


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def api_calls(monkeypatch):
    """Capture Telegram API methods instead of calling Telegram."""
    calls: list[tuple[str, dict]] = []

    async def fake_call(method: str, payload: dict) -> None:
        calls.append((method, payload))

    monkeypatch.setattr(notify, "telegram_call", fake_call)
    db.set_setting("telegram_chat_id", "42")
    return calls


def _question(project, options=("yes", "no")):
    return db.create_question(
        project["id"], "Merge?", quick_options=quickreplies.encode(list(options))
    )


def _two_copies(q):
    db.record_question_message(q["id"], "42", 5, "Q3: Merge?")
    db.record_question_message(q["id"], "77", 9, "Q3: Merge?")


def _edits(api_calls):
    return [p for m, p in api_calls if m == "editMessageText"]


def _tap(question_id, token, chat_id=42):
    return {
        "callback_query": {
            "id": "cb1",
            "data": f"q:{question_id}:{token}",
            "message": {
                "message_id": 5,
                "chat": {"id": chat_id},
                "text": "Q3: Merge?",
            },
        }
    }


# --- the table ---------------------------------------------------------------


def test_every_chats_copy_is_recorded(project):
    q = _question(project)
    _two_copies(q)
    rows = db.question_messages(q["id"])
    assert [(r["chat_id"], r["message_id"]) for r in rows] == [("42", 5), ("77", 9)]


def test_resending_to_the_same_chat_keeps_one_row(project):
    q = _question(project)
    db.record_question_message(q["id"], "42", 5, "first send")
    db.record_question_message(q["id"], "42", 8, "second send")
    rows = db.question_messages(q["id"])
    assert len(rows) == 1
    assert rows[0]["message_id"] == 8


def test_reply_lookup_knows_which_chat(project):
    # Telegram message ids are only unique per chat: id 5 in chat 42 and id 5
    # in chat 77 can be copies of different questions.
    q1, q2 = _question(project), _question(project)
    db.record_question_message(q1["id"], "42", 5, "Q3: Merge?")
    db.record_question_message(q2["id"], "77", 5, "Q4: Ship?")
    assert db.question_for_telegram_message("42", 5) == q1["id"]
    assert db.question_for_telegram_message("77", 5) == q2["id"]
    assert db.question_for_telegram_message("99", 5) is None


def test_reply_lookup_falls_back_to_the_legacy_column(project):
    # A question sent before the copies table existed has only telegram_msg_id,
    # which never recorded a chat - so it matches on message id alone, exactly
    # as the old lookup did.
    q = _question(project)
    db.set_question_telegram_msg_id(q["id"], 31)
    assert db.question_for_telegram_message("42", 31) == q["id"]
    assert db.question_for_telegram_message("42", None) is None


# --- settling ----------------------------------------------------------------


@pytest.mark.anyio
async def test_settle_edits_every_copy(project, api_calls):
    q = _question(project)
    _two_copies(q)

    settled = await notify.settle_question_copies(q["id"], "answered: no")

    assert settled == 2
    edits = _edits(api_calls)
    assert {(e["chat_id"], e["message_id"]) for e in edits} == {("42", 5), ("77", 9)}
    for e in edits:
        assert e["text"] == "Q3: Merge?\n\n[answered: no]"
        # No reply_markup at all is what removes the inline keyboard.
        assert "reply_markup" not in e


@pytest.mark.anyio
async def test_settle_with_no_copies_reports_zero(project, api_calls):
    q = _question(project)
    assert await notify.settle_question_copies(q["id"], "answered: no") == 0
    assert _edits(api_calls) == []


# --- every path that closes a question settles the copies --------------------


@pytest.mark.anyio
async def test_a_tap_settles_the_other_chats_copy_too(project, api_calls):
    q = _question(project)
    _two_copies(q)

    await telegram_bot._handle_update(_tap(q["id"], 1))

    assert db.get_question(q["id"])["answer"] == "no"
    edited = {e["chat_id"] for e in _edits(api_calls)}
    assert edited == {"42", "77"}


@pytest.mark.anyio
async def test_a_tap_on_a_pre_table_question_still_edits_the_tapped_message(project, api_calls):
    # Legacy: no rows in question_messages. The one copy that certainly
    # exists is the message the tap rode in on.
    q = _question(project)
    await telegram_bot._handle_update(_tap(q["id"], 1))
    edits = _edits(api_calls)
    assert len(edits) == 1
    assert edits[0]["message_id"] == 5
    assert "[answered: no]" in edits[0]["text"]


@pytest.mark.anyio
async def test_a_typed_reply_settles_every_copy(project, api_calls, monkeypatch):
    async def silent(chat_id, text):
        return None

    monkeypatch.setattr(notify, "send_telegram_text", silent)
    q = _question(project)
    _two_copies(q)

    await telegram_bot._answer_question(q["id"], "no", "42", by_id=True)

    assert {e["chat_id"] for e in _edits(api_calls)} == {"42", "77"}


def test_a_web_answer_settles_every_copy(project, client, api_calls):
    q = _question(project)
    _two_copies(q)

    resp = client.post(f"/questions/{q['id']}/answer", data={"choice": "yes"},
                       follow_redirects=False)

    assert resp.status_code == 303
    edits = _edits(api_calls)
    assert {e["chat_id"] for e in edits} == {"42", "77"}
    assert all("[answered: yes]" in e["text"] for e in edits)


def test_a_web_dismiss_marks_the_copies_saved_for_later(project, client, api_calls):
    q = _question(project)
    _two_copies(q)
    client.post(f"/questions/{q['id']}/dismiss", follow_redirects=False)
    assert all("[saved for later]" in e["text"] for e in _edits(api_calls))
    assert len(_edits(api_calls)) == 2


def test_a_web_delete_marks_the_copies_deleted(project, client, api_calls):
    q = _question(project)
    _two_copies(q)
    client.post(f"/questions/{q['id']}/delete", follow_redirects=False)
    assert all("[deleted]" in e["text"] for e in _edits(api_calls))


# --- housekeeping ------------------------------------------------------------


def test_pruning_a_deleted_question_removes_its_copies(project, monkeypatch):
    q = _question(project)
    _two_copies(q)
    db.delete_question(q["id"])
    # Age the deletion past the retention window.
    conn = db.get_conn()
    with db._LOCK:  # noqa: SLF001 - test reaches in to age the row
        conn.execute("UPDATE questions SET answered_at = '2020-01-01T00:00:00+00:00' "
                     "WHERE id = ?", (q["id"],))
        conn.commit()

    assert db.prune_deleted_questions() == 1
    assert db.question_messages(q["id"]) == []


# --- the send side records every chat ---------------------------------------


class _FakeResponse:
    def __init__(self, message_id):
        self._message_id = message_id

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "result": {"message_id": self._message_id}}


class _FakeClient:
    next_message_id = 100

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **k):
        _FakeClient.next_message_id += 1
        return _FakeResponse(_FakeClient.next_message_id)


@pytest.mark.anyio
async def test_sending_to_two_chats_records_both_copies(project, monkeypatch):
    monkeypatch.setattr(notify.httpx, "AsyncClient", _FakeClient)
    q = _question(project)

    await notify._send_telegram("tok", "42", "Q3: Merge?", q["id"], record_msg_id=True)
    await notify._send_telegram("tok", "77", "Q3: Merge?", q["id"], record_msg_id=False)

    rows = db.question_messages(q["id"])
    assert [r["chat_id"] for r in rows] == ["42", "77"]
    assert all(r["text"] == "Q3: Merge?" for r in rows)
    # The legacy column still carries the first copy's id for old code paths.
    assert db.get_question(q["id"])["telegram_msg_id"] == rows[0]["message_id"]

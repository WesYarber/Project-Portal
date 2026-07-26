"""One-tap Telegram answer buttons on questions (#217).

A question sent to Telegram carries an inline keyboard: the agent's explicit
`options` from its report, or yes/no when the question is visibly yes/no
shaped, plus an always-present [skip]. A tap routes through the same
answer/dismiss paths a typed reply uses, acks the tap, and edits the message
so the choice is on record and the buttons cannot double-fire.
"""
from __future__ import annotations

import json

import pytest

from app import agent_runner, db, notify, pacing, quickreplies, telegram_bot, worker


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- deriving options -------------------------------------------------------

def test_explicit_options_win_over_the_heuristic():
    assert quickreplies.derive("Should I merge?", ["merge it", "keep both"]) == [
        "merge it",
        "keep both",
    ]


def test_explicit_options_are_sanitized():
    raw = ["  merge it  ", "", 7, None, "Merge It", "skip", "x" * 200, "keep", "extra", "more"]
    out = quickreplies.derive("anything", raw)
    # Stripped, deduped case-insensitively, "skip" removed (it is always added
    # as a button anyway), long labels truncated, capped at MAX_OPTIONS.
    assert out[0] == "merge it"
    assert "Merge It" not in out
    assert "skip" not in [o.lower() for o in out]
    assert all(len(o) <= quickreplies.OPTION_MAXLEN for o in out)
    assert len(out) <= quickreplies.MAX_OPTIONS


def test_non_list_explicit_options_fall_back_to_the_heuristic():
    assert quickreplies.derive("Say yes and I will do it.", "yes,no") == ["yes", "no"]


@pytest.mark.parametrize(
    "question",
    [
        "Want me to spend it down? Say yes and I will lift the budget.",
        "Should I merge the two pages?",
        "The tunnel is ready. Shall I switch DNS over?",
        "Do you want the dark theme everywhere?",
        "Is it ok to delete the stale workspaces?",
        "Reply yes or no.",
    ],
)
def test_yes_no_shaped_questions_get_yes_no(question):
    assert quickreplies.derive(question) == ["yes", "no"]


@pytest.mark.parametrize(
    "question",
    [
        "Which of the two designs do you prefer?",
        "What port should the server use?",
        # "do I" mid-sentence is an open question, not a yes/no offer.
        "What do I need to buy for the e-ink build?",
        # "yesterday" must not read as "say yes".
        "You mentioned this yesterday - what was the password?",
        "",
    ],
)
def test_open_ended_questions_get_no_buttons(question):
    assert quickreplies.derive(question) == []


# --- encode/decode ----------------------------------------------------------

def test_encode_decode_round_trip():
    assert quickreplies.decode(quickreplies.encode(["yes", "no"])) == ["yes", "no"]


def test_empty_options_encode_to_the_column_default():
    assert quickreplies.encode([]) == ""


@pytest.mark.parametrize("raw", ["", None, "not json{", '"a string"', '{"a": 1}', "[1, 2]"])
def test_junk_stored_options_decode_to_none(raw):
    assert quickreplies.decode(raw) == []


# --- the keyboard -----------------------------------------------------------

def test_short_option_sets_share_one_row_with_skip_last():
    kb = quickreplies.keyboard(9, ["yes", "no"])
    (row,) = kb["inline_keyboard"]
    assert [b["text"] for b in row] == ["yes", "no", "skip"]
    assert [b["callback_data"] for b in row] == ["q:9:0", "q:9:1", "q:9:skip"]


def test_long_labels_get_a_row_per_button():
    kb = quickreplies.keyboard(9, ["restore the recovered revision", "keep the current one"])
    rows = kb["inline_keyboard"]
    assert len(rows) == 3
    assert all(len(row) == 1 for row in rows)
    assert rows[-1][0]["text"] == "skip"


def test_no_options_still_offers_skip():
    kb = quickreplies.keyboard(4, [])
    assert kb["inline_keyboard"] == [[{"text": "skip", "callback_data": "q:4:skip"}]]


@pytest.mark.parametrize(
    "data,expected",
    [
        ("q:7:0", (7, "0")),
        ("q:7:skip", (7, "skip")),
        ("q:7:2", (7, "2")),
        ("q:7:-1", None),
        ("q:x:0", None),
        ("q:7:maybe", None),
        ("other:7:0", None),
        ("q:7", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_callback(data, expected):
    assert quickreplies.parse_callback(data) == expected


# --- storage ----------------------------------------------------------------

@pytest.fixture
def project():
    return db.create_project("Dice Tower", description="A thing.", stage="active")


def test_create_question_stores_options(project):
    row = db.create_question(
        project["id"], "Merge?", quick_options=quickreplies.encode(["merge it", "keep both"])
    )
    assert quickreplies.decode(row["quick_options"]) == ["merge it", "keep both"]


def test_create_question_defaults_to_no_options(project):
    row = db.create_question(project["id"], "Open-ended?")
    assert row["quick_options"] == ""


# --- a tap ------------------------------------------------------------------

@pytest.fixture
def api_calls(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_call(method: str, payload: dict) -> None:
        calls.append((method, payload))

    monkeypatch.setattr(notify, "telegram_call", fake_call)
    db.set_setting("telegram_chat_id", "42")
    return calls


def _tap(question_id, token, chat_id=42, data=None):
    return {
        "callback_query": {
            "id": "cb1",
            "data": data if data is not None else f"q:{question_id}:{token}",
            "message": {
                "message_id": 5,
                "chat": {"id": chat_id},
                "text": "Q3: [Dice Tower]: Merge?",
            },
        }
    }


def _question(project, options=("yes", "no")):
    return db.create_question(
        project["id"], "Merge?", quick_options=quickreplies.encode(list(options))
    )


@pytest.mark.anyio
async def test_a_tap_answers_the_question(project, api_calls):
    q = _question(project)
    await telegram_bot._handle_update(_tap(q["id"], 1))

    row = db.get_question(q["id"])
    assert row["status"] == "answered"
    assert row["answer"] == "no"
    # The same journalling a typed answer gets.
    entries = [j for j in db.list_journal(project["id"]) if j["kind"] == "answer"]
    assert entries and "**A:** no" in entries[0]["content_md"]
    # The tap is acked and the message edited to carry the outcome, minus
    # the keyboard - so a second tap has nothing to press.
    methods = [m for m, _ in api_calls]
    assert "answerCallbackQuery" in methods
    edit = dict(api_calls)["editMessageText"]
    assert "[answered: no]" in edit["text"]
    assert "reply_markup" not in edit


@pytest.mark.anyio
async def test_a_skip_tap_dismisses(project, api_calls):
    q = _question(project)
    await telegram_bot._handle_update(_tap(q["id"], "skip"))
    assert db.get_question(q["id"])["status"] == "dismissed"
    assert "[skipped]" in dict(api_calls)["editMessageText"]["text"]


@pytest.mark.anyio
async def test_a_late_tap_on_an_answered_question_changes_nothing(project, api_calls):
    q = _question(project)
    db.answer_question_and_resume(q["id"], "typed first")
    await telegram_bot._handle_update(_tap(q["id"], 0))

    row = db.get_question(q["id"])
    assert row["answer"] == "typed first"
    # Acked (no spinner) and the stale buttons stripped, but never re-answered.
    assert dict(api_calls)["answerCallbackQuery"]["text"] == "Already handled."
    assert "editMessageReplyMarkup" in dict(api_calls)


@pytest.mark.anyio
async def test_a_tap_from_an_unknown_chat_is_ignored(project, api_calls):
    q = _question(project)
    await telegram_bot._handle_update(_tap(q["id"], 0, chat_id=666))
    assert db.get_question(q["id"])["status"] == "open"
    # Still acked - an ignored tap must not leave a spinner on their client.
    assert [m for m, _ in api_calls] == ["answerCallbackQuery"]


@pytest.mark.anyio
async def test_junk_callback_data_is_acked_and_ignored(project, api_calls):
    q = _question(project)
    await telegram_bot._handle_update(_tap(q["id"], 0, data="garbage"))
    assert db.get_question(q["id"])["status"] == "open"
    assert [m for m, _ in api_calls] == ["answerCallbackQuery"]


@pytest.mark.anyio
async def test_an_out_of_range_index_never_guesses(project, api_calls):
    q = _question(project)
    await telegram_bot._handle_update(_tap(q["id"], 9))
    assert db.get_question(q["id"])["status"] == "open"
    assert [m for m, _ in api_calls] == ["answerCallbackQuery"]


# --- wiring: where questions are born ---------------------------------------

@pytest.mark.anyio
async def test_report_questions_carry_their_options_onto_the_row(project, monkeypatch):
    async def silent(*a, **k):
        return None

    monkeypatch.setattr(notify, "notify", silent)
    result = agent_runner.RunResult(
        ok=True,
        report={
            "journal_entry_md": "x",
            "questions": [
                {"question": "Merge the pages?", "options": ["merge", "keep both"]},
                {"question": "Want me to keep going? Say yes and I will."},
                {"question": "Which font do you prefer?"},
            ],
        },
    )
    worker._apply_report(project, result)

    rows = db.open_questions(project["id"])
    by_text = {r["question"]: quickreplies.decode(r["quick_options"]) for r in rows}
    assert by_text["Merge the pages?"] == ["merge", "keep both"]
    assert by_text["Want me to keep going? Say yes and I will."] == ["yes", "no"]
    assert by_text["Which font do you prefer?"] == []


def test_the_spend_down_offer_is_one_tap(monkeypatch):
    meta = db.create_project("Project Portal", description="meta", stage="active")
    monkeypatch.setattr(pacing.config, "META_PROJECT_SLUG", meta["slug"])
    candidate = {
        "key": "seven_day",
        "label": "weekly",
        "percent": 53.0,
        "unused": 47.0,
        "resets_in": "7h 59m",
        "resets_at": "2026-07-24T06:00:00+00:00",
    }
    question = pacing.create_offer_question(candidate)
    assert quickreplies.decode(question["quick_options"]) == ["yes", "no"]


# --- the outbound message carries the keyboard ------------------------------

class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "result": {"message_id": 77}}


class _FakeClient:
    posts: list[tuple[str, dict]] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **k):
        _FakeClient.posts.append((url, json))
        return _FakeResponse()


@pytest.mark.anyio
async def test_question_message_includes_the_inline_keyboard(project, monkeypatch):
    _FakeClient.posts = []
    monkeypatch.setattr(notify.httpx, "AsyncClient", _FakeClient)
    q = _question(project, options=("yes", "no"))

    await notify._send_telegram("tok", "42", "Q3: Merge?", q["id"])

    (_, payload) = _FakeClient.posts[0]
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["yes", "no", "skip"]
    # The message id still gets recorded, so typed replies keep working too.
    assert db.get_question(q["id"])["telegram_msg_id"] == 77


@pytest.mark.anyio
async def test_plain_notifications_carry_no_keyboard(monkeypatch):
    _FakeClient.posts = []
    monkeypatch.setattr(notify.httpx, "AsyncClient", _FakeClient)
    await notify._send_telegram("tok", "42", "run finished", None)
    (_, payload) = _FakeClient.posts[0]
    assert "reply_markup" not in payload


# --- the contract documents it, and the schema accepts it -------------------

def test_contract_and_schema_both_know_about_options():
    assert '"options"' in agent_runner.AGENT_CONTRACT
    from app import report_schema

    props = report_schema.REPORT_SCHEMA["properties"]["questions"]["items"]["properties"]
    assert props["options"] == {"type": "array", "items": {"type": "string"}}

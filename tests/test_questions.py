"""Question slots, dismissal, and where you land after answering.

Three of Wes's notes converge here:

- questions should be labelled `Q7: [project]: <question>`, with the numbers
  recycled rather than climbing forever;
- the dismiss button on the questions tab did nothing at all;
- dismissing should clear the notification without erasing the question from
  its project.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db, notify, persona


@pytest.fixture
def client(temp_data_dir):
    from app import main

    # No context manager: that would run the lifespan hook and start the worker.
    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project("Dice Tower", description="A thing.", stage="active", build_approved=True, slug="dice-tower")


# --- slots -----------------------------------------------------------------

def test_slots_start_at_one_and_count_up(project):
    a = db.create_question(project["id"], "First?")
    b = db.create_question(project["id"], "Second?")
    assert (a["slot"], b["slot"]) == (1, 2)


def test_answering_releases_the_slot_for_the_next_question(project):
    a = db.create_question(project["id"], "First?")
    b = db.create_question(project["id"], "Second?")
    db.answer_question_and_resume(a["id"], "yes")

    # Slot 1 is free again, so the next question takes it rather than becoming 3.
    c = db.create_question(project["id"], "Third?")
    assert c["slot"] == 1
    assert db.get_question(a["id"])["slot"] is None
    assert db.get_question(b["id"])["slot"] == 2


def test_dismissing_also_releases_the_slot(project):
    a = db.create_question(project["id"], "First?")
    db.dismiss_question_and_resume(a["id"])
    assert db.get_question(a["id"])["slot"] is None
    assert db.create_question(project["id"], "Next?")["slot"] == 1


def test_slot_fills_the_lowest_gap_not_the_end(project):
    qs = [db.create_question(project["id"], f"Q{i}?") for i in range(3)]
    db.answer_question_and_resume(qs[1]["id"], "ok")  # frees slot 2
    assert db.create_question(project["id"], "New?")["slot"] == 2


def test_slots_are_global_not_per_project(project):
    other = db.create_project("Other", slug="other")
    a = db.create_question(project["id"], "A?")
    b = db.create_question(other["id"], "B?")
    # A number Wes types at the bot has to identify exactly one question.
    assert a["slot"] != b["slot"]


def test_question_by_slot_only_finds_open_questions(project):
    a = db.create_question(project["id"], "A?")
    assert db.question_by_slot(1)["id"] == a["id"]
    db.answer_question_and_resume(a["id"], "done")
    assert db.question_by_slot(1) is None


def test_resolve_prefers_the_slot_over_a_row_id(project):
    # Make the ids climb past the slots so the two can actually disagree.
    for i in range(4):
        q = db.create_question(project["id"], f"old {i}?")
        db.answer_question_and_resume(q["id"], "x")
    live = db.create_question(project["id"], "live?")
    assert live["slot"] == 1 and live["id"] > 1
    assert db.resolve_question(1)["id"] == live["id"]


def test_resolve_falls_back_to_the_row_id(project):
    a = db.create_question(project["id"], "A?")
    db.answer_question_and_resume(a["id"], "x")
    # Nothing holds a slot now, so an old notification's id still resolves.
    assert db.resolve_question(a["id"])["id"] == a["id"]


def test_existing_questions_get_slots_backfilled(temp_data_dir, project):
    # Simulate a database written before slots existed.
    conn = db.get_conn()
    ids = [db.create_question(project["id"], f"q{i}?")["id"] for i in range(3)]
    conn.execute("UPDATE questions SET slot = NULL")
    conn.commit()

    db.init_db()

    slots = sorted(db.get_question(i)["slot"] for i in ids)
    assert slots == [1, 2, 3]


# --- labelling -------------------------------------------------------------

def test_question_prefix_is_q_number_then_project():
    assert persona.question_prefix(7, "Dice Tower") == "Q7: [Dice Tower]"


def test_question_prefix_without_a_slot_is_project_only():
    assert persona.question_prefix(None, "Dice Tower") == "[Dice Tower]"


@pytest.mark.anyio
async def test_notification_text_is_q_number_project_question(project, monkeypatch):
    sent: list[str] = []

    async def fake_telegram(token, chat_id, text, question_id):
        sent.append(text)

    monkeypatch.setattr(notify, "_send_telegram", fake_telegram)
    monkeypatch.setattr(notify, "_send_ntfy", lambda *a, **k: _noop())
    db.set_setting("telegram_token", "t")
    db.set_setting("telegram_chat_id", "c")

    await notify.notify(
        "New question", "Which one?", question_id=5, project_title="Dice Tower", question_slot=3
    )
    assert "Q3: [Dice Tower]: Which one?" in sent[0]


async def _noop():
    return None


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_no_branded_prefix_on_notifications():
    db.set_setting("glados_mode", "1")
    assert "Aperture" not in persona.decorate_notification("New question", "body")


# --- the dismiss button ----------------------------------------------------

def test_questions_page_dismiss_button_skips_validation(client, project):
    db.create_question(project["id"], "Which one?")
    html = client.get("/questions").text
    # Without formnovalidate the required textarea blocks the submit entirely,
    # which is exactly why the button appeared dead.
    assert "formnovalidate" in html
    assert 'formaction="/questions/1/dismiss"' in html


def test_questions_page_shows_the_slot_not_the_row_id(client, project):
    for i in range(3):
        q = db.create_question(project["id"], f"old {i}?")
        db.answer_question_and_resume(q["id"], "x")
    db.create_question(project["id"], "live?")
    html = client.get("/questions").text
    assert "Q1" in html


def test_dismiss_from_the_questions_tab_stays_on_the_questions_tab(client, project):
    q = db.create_question(project["id"], "Which one?")
    resp = client.post(
        f"/questions/{q['id']}/dismiss", data={"next": "/questions"}, follow_redirects=False
    )
    assert resp.headers["location"] == "/questions"


def test_answer_from_the_questions_tab_stays_on_the_questions_tab(client, project):
    q = db.create_question(project["id"], "Which one?")
    resp = client.post(
        f"/questions/{q['id']}/answer",
        data={"answer": "this one", "next": "/questions"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/questions"


def test_answer_without_next_still_returns_to_the_project(client, project):
    q = db.create_question(project["id"], "Which one?")
    resp = client.post(
        f"/questions/{q['id']}/answer", data={"answer": "this one"}, follow_redirects=False
    )
    assert resp.headers["location"] == "/project/dice-tower"


def test_next_cannot_leave_the_site(client, project):
    q = db.create_question(project["id"], "Which one?")
    resp = client.post(
        f"/questions/{q['id']}/dismiss",
        data={"next": "//evil.example.com/"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/"


# --- dismissal semantics ---------------------------------------------------

def test_dismissed_question_leaves_the_questions_tab(client, project):
    q = db.create_question(project["id"], "Which one?")
    client.post(f"/questions/{q['id']}/dismiss")
    assert db.open_questions() == []
    assert "Which one?" not in client.get("/questions").text


def test_dismissed_question_stays_on_its_project(client, project):
    q = db.create_question(project["id"], "Which one?")
    client.post(f"/questions/{q['id']}/dismiss")
    assert [row["id"] for row in db.dismissed_questions(project["id"])] == [q["id"]]
    html = client.get("/project/dice-tower").text
    assert "Which one?" in html
    assert "dismissed questions (1)" in html


def test_reopening_gives_a_fresh_slot(client, project):
    a = db.create_question(project["id"], "A?")
    client.post(f"/questions/{a['id']}/dismiss")
    db.create_question(project["id"], "B?")  # takes the freed slot 1

    client.post(f"/questions/{a['id']}/reopen")
    reopened = db.get_question(a["id"])
    assert reopened["status"] == "open"
    # Not 1: that number belongs to B now.
    assert reopened["slot"] == 2


def test_reopen_ignores_an_answered_question(client, project):
    a = db.create_question(project["id"], "A?")
    db.answer_question_and_resume(a["id"], "yes")
    client.post(f"/questions/{a['id']}/reopen")
    assert db.get_question(a["id"])["status"] == "answered"

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
    db.set_setting("telegram_enabled", "1")
    db.set_setting("telegram_token", "t")
    db.set_setting("telegram_chat_id", "c")

    await notify.notify(
        "New question", "Which one?", question_id=5, project_title="Dice Tower", question_slot=3
    )
    assert "Q3: [Dice Tower]: Which one?" in sent[0]


@pytest.mark.anyio
async def test_no_q_number_in_the_notification_without_telegram(project, monkeypatch):
    # Wes, 2026-07-28: the QX numbering exists to be typed back at the bot, so
    # with the bot off it is spending characters from a one-line notification
    # preview to address something that cannot be addressed. The project name
    # stays - that is which board is asking, and it is useful either way.
    sent: list[str] = []
    monkeypatch.setattr(notify, "_send_ntfy", lambda url, topic, title, text: _record(sent, text))
    monkeypatch.setattr(notify.webpush, "push_all", lambda *a, **k: _noop())
    db.set_setting("telegram_enabled", "0")

    await notify.notify(
        "New question", "Which one?", question_id=5, project_title="Dice Tower", question_slot=3
    )
    assert sent == ["[Dice Tower]: Which one?"]


@pytest.mark.anyio
async def test_telegram_is_not_sent_to_while_it_is_switched_off(project, monkeypatch):
    # A token left in Settings from before the switch existed must not keep the
    # integration alive: the box is the intent, the token is only the means.
    sent: list[str] = []

    async def fake_telegram(token, chat_id, text, question_id):
        sent.append(text)

    monkeypatch.setattr(notify, "_send_telegram", fake_telegram)
    monkeypatch.setattr(notify, "_send_ntfy", lambda *a, **k: _noop())
    monkeypatch.setattr(notify.webpush, "push_all", lambda *a, **k: _noop())
    db.set_setting("telegram_enabled", "0")
    db.set_setting("telegram_token", "t")
    db.set_setting("telegram_chat_id", "c")

    await notify.notify("New question", "Which one?", question_id=5)
    assert sent == []


async def _record(into: list, text: str) -> None:
    into.append(text)


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
    # Only with Telegram on, now - see the pair of tests below. The slot is
    # still allocated either way, so turning the integration on brings the
    # numbers back on the questions that are already open rather than only on
    # the next one filed.
    db.set_setting("telegram_enabled", "1")
    db.set_setting("telegram_token", "t")
    for i in range(3):
        q = db.create_question(project["id"], f"old {i}?")
        db.answer_question_and_resume(q["id"], "x")
    db.create_question(project["id"], "live?")
    html = client.get("/questions").text
    assert "Q1" in html


def test_no_q_numbers_on_the_page_with_telegram_off(client, project):
    db.set_setting("telegram_enabled", "0")
    db.create_question(project["id"], "live?")
    html = client.get("/questions").text
    assert "live?" in html
    assert ">Q1<" not in html


def test_a_token_alone_does_not_bring_the_numbers_back(client, project):
    # The box is the intent. An install that has a leftover token from before
    # the switch existed is switched off, and must render as switched off.
    db.set_setting("telegram_enabled", "0")
    db.set_setting("telegram_token", "leftover")
    db.create_question(project["id"], "live?")
    assert ">Q1<" not in client.get("/questions").text


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
    # Wes, 2026-07-28: "when dismissing a question, it should be saved for
    # later. When a user does this, it should still show the question in the
    # relevant project page, but it should be removed from the questions
    # page." All three halves of that, in order.
    q = db.create_question(project["id"], "Which one?")
    client.post(f"/questions/{q['id']}/dismiss")
    assert [row["id"] for row in db.dismissed_questions(project["id"])] == [q["id"]]

    html = client.get("/project/dice-tower").text
    assert "Which one?" in html
    assert "Saved for later" in html
    # And answerable in place. This is the part that changed: it used to offer
    # only "reopen", which meant putting it back on the very page you took it
    # off before you could answer the thing you had come back to answer.
    assert f'action="/questions/{q["id"]}/answer"' in html

    assert "Which one?" not in client.get("/questions").text


def test_saving_a_question_for_later_then_answering_it_in_place(client, project):
    q = db.create_question(project["id"], "Which one?")
    client.post(f"/questions/{q['id']}/dismiss")
    client.post(f"/questions/{q['id']}/answer", data={"answer": "the left one"})
    row = db.get_question(q["id"])
    assert row["status"] == "answered"
    assert row["answer"] == "the left one"


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


# Deleting a question ---------------------------------------------------------
# Wes, 2026-07-28: "add an option for deleting a question in which case it
# should respond to the question with deleted will not answer and not prompt it
# to run due to the answer being submitted."

def test_deleting_answers_the_question_without_answering_it(client, project):
    q = db.create_question(project["id"], "Which one?")
    client.post(f"/questions/{q['id']}/delete")
    row = db.get_question(q["id"])
    assert row["status"] == "deleted"
    # An answer, in words, because the prompt's only record of a past question
    # is its answer and a blank there reads as "never got round to it".
    assert row["answer"] == db.DELETED_ANSWER
    assert "will not be answered" in row["answer"]
    # Off the questions page and off the open count, like any settled question.
    assert db.count_open_questions(project["id"]) == 0
    assert "Which one?" not in client.get("/questions").text


def test_a_deleted_question_is_not_in_the_answered_list(client, project):
    # The answered list is a record of decisions Wes actually made. A row that
    # only says "not answering" would sit in it wearing the same clothes.
    q = db.create_question(project["id"], "Which one?")
    client.post(f"/questions/{q['id']}/delete")
    assert db.answered_qa(project["id"]) == []
    assert [row["id"] for row in db.deleted_questions(project["id"])] == [q["id"]]


def test_a_deleted_question_can_never_be_asked_again(project):
    # The whole point of deleting rather than dismissing. An answered question
    # is only settled for QUESTION_SETTLED_HOURS; a deleted one is settled for
    # good, including against a rewording - which is the failure that would
    # make deleting worthless.
    q = db.create_question(project["id"], "Should I use Postgres or SQLite?")
    db.delete_question(q["id"])

    filing = db.file_question(project["id"], "Should I use Postgres or SQLite?")
    assert not filing.created
    assert filing.duplicate_of["id"] == q["id"]


def test_deleting_does_not_journal_an_answer(client, project):
    # "not prompt it to run due to the answer being submitted": a real answer
    # writes a `user/answer` journal entry, which is a note from Wes in every
    # sense the prompt cares about. Declining to answer is not that.
    q = db.create_question(project["id"], "Which one?")
    client.post(f"/questions/{q['id']}/delete")
    kinds = [row["kind"] for row in db.list_journal(project["id"])]
    assert "answer" not in kinds


def test_delete_is_offered_on_the_questions_page(client, project):
    q = db.create_question(project["id"], "Which one?")
    html = client.get("/questions").text
    assert f'formaction="/questions/{q["id"]}/delete"' in html
    # It asks first. Deleting is irreversible in a way dismissing is not.
    assert "data-confirm" in html


# One-tap answers -------------------------------------------------------------
# Wes, 2026-07-28: "allow the prompt that asked the question to pre-fill some
# multiple-choice questions that can be clicked for ease of answering, but
# always allow the user to still be able to type in additional context if
# desired or just to skip clicking one of those answers altogether and answer
# by typing outright."

def _with_options(project, question="Merge it?", options=("merge it", "keep both")):
    from app import quickreplies

    return db.create_question(
        project["id"], question, quick_options=quickreplies.encode(list(options))
    )


def test_offered_options_render_as_buttons_on_both_pages(client, project):
    _with_options(project)
    for url in ("/questions", "/project/dice-tower"):
        html = client.get(url).text
        assert 'name="choice" value="merge it"' in html, url
        assert 'name="choice" value="keep both"' in html, url
    # Real submit buttons, not script - one tap answers with scripting off.
    assert 'class="quick-option"' in client.get("/questions").text


def test_a_tapped_option_answers_the_question(client, project):
    q = _with_options(project)
    client.post(f"/questions/{q['id']}/answer", data={"choice": "merge it", "answer": ""})
    assert db.get_question(q["id"])["answer"] == "merge it"


def test_typing_alone_still_answers(client, project):
    q = _with_options(project)
    client.post(f"/questions/{q['id']}/answer", data={"answer": "neither, actually"})
    assert db.get_question(q["id"])["answer"] == "neither, actually"


def test_a_tap_carries_along_what_was_already_typed(client, project):
    # The one that would be easy to get wrong: tapping an option must not
    # throw away the context somebody had started writing beside it.
    q = _with_options(project)
    client.post(
        f"/questions/{q['id']}/answer",
        data={"choice": "merge it", "answer": "but run the tests first"},
    )
    assert db.get_question(q["id"])["answer"] == "merge it - but run the tests first"


def test_an_unoffered_choice_is_not_recorded(client, project):
    # The answer is journalled and read by the next agent. An answer it never
    # offered is a worse thing to record than a dropped tap.
    q = _with_options(project)
    client.post(
        f"/questions/{q['id']}/answer",
        data={"choice": "delete the repo", "answer": "go ahead"},
    )
    assert db.get_question(q["id"])["answer"] == "go ahead"


def test_an_empty_submit_leaves_the_question_open(client, project):
    # The textarea cannot be `required` any more (the answer may be a tapped
    # option), so the empty submit the browser used to block now reaches the
    # route. Settling a question against a blank would read to the next agent
    # as a decision that was never made.
    q = _with_options(project)
    client.post(f"/questions/{q['id']}/answer", data={"answer": "   "})
    assert db.get_question(q["id"])["status"] == "open"


def test_no_options_means_no_button_strip(client, project):
    db.create_question(project["id"], "What should this be called?")
    html = client.get("/questions").text
    assert "quick-options" not in html


# --------------------------------------------------------------------------
# Where "Saved for later" sits, and what happened to the ones from before
# --------------------------------------------------------------------------

def test_saved_for_later_is_folded_at_the_bottom_of_the_questions(client, project):
    """Wes, 2026-07-28: "Make the 'Saved for later' question tab be in a
    collapsed section that sits in the bottom of the questions section rather
    than having its own static headers."

    The previous pass gave it an <h2> level with "Open questions", which said
    the two were equals when the whole point of saving one is deciding it is
    not pressing.
    """
    saved = db.create_question(project["id"], "The saved one?")
    client.post(f"/questions/{saved['id']}/dismiss")
    live = db.create_question(project["id"], "The open one?")

    html = client.get("/project/dice-tower").text

    assert "<h2>Saved for later</h2>" not in html
    assert '<details class="fold-section saved-questions">' in html
    # Folded, and below the open ones - the order is the claim.
    assert html.index("The open one?") < html.index("The saved one?")
    assert html.index("Open questions") < html.index("saved-questions")
    assert str(live["id"]) in html


def test_the_fold_is_not_rendered_at_all_with_nothing_in_it(client, project):
    """An empty collapsed section is a row of chrome that never opens."""
    html = client.get("/project/dice-tower").text
    assert "saved-questions" not in html


def test_questions_dismissed_before_save_for_later_existed_are_deleted():
    """Wes, 2026-07-28: "all existing 'Saved for later' questions should be
    instantly deleted here since they were treated as deleted before."

    Until that morning the dismiss button was a throw-away with no way back, so
    every row already sitting in 'dismissed' was a refusal. Reusing the status
    for save-for-later retroactively turned a pile of noes into a pile of
    things Wes had supposedly meant to come back to.

    Dry-run against a copy of the real board before shipping: 34 dismissed
    questions settled, the 6 open ones untouched.
    """
    project = db.create_project("Legacy")
    q = db.create_question(project["id"], "An old one?")
    conn = db.get_conn()
    conn.execute("UPDATE questions SET status = 'dismissed' WHERE id = ?", (q["id"],))
    conn.commit()
    # As if the portal had never run the purge.
    db.set_setting(db.SHELVED_PURGE_KEY, "")

    db.init_db()

    row = db.get_question(q["id"])
    assert row["status"] == "deleted"
    assert row["answer"] == db.DELETED_ANSWER


def test_the_purge_runs_once_and_never_again():
    """Guarded by a settings key, not by a date: "dismissed before the feature
    shipped" is exactly what this means and no timestamp says it. Unguarded it
    would delete a question Wes saved for later that afternoon, at the next
    restart."""
    project = db.create_project("Legacy")
    db.init_db()  # the purge has now run

    q = db.create_question(project["id"], "Saved on purpose?")
    conn = db.get_conn()
    conn.execute("UPDATE questions SET status = 'dismissed' WHERE id = ?", (q["id"],))
    conn.commit()

    db.init_db()

    assert db.get_question(q["id"])["status"] == "dismissed"

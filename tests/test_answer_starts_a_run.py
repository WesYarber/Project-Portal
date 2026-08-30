"""Answering a question starts a run, exactly as adding a note does.

Wes, 2026-08-29: *"answering a question should prompt a run as if a new note was
just added."*

The asymmetry he is describing: an agent stops mid-task, asks the one thing only
a person can decide, and files the question. He answers it on his phone - and
nothing happened. The answer sat in the journal until the next scheduled run got
round to the project, which made replying the *slowest* way to tell an agent
something. Pasting the same words into the note box on the same page started a
run immediately.

So an answer now goes through `worker.answer_arrived`, which is `note_arrived`
plus the three refusals below. It is deliberately `note_arrived` and not
`queue_manual_run`: answering matches the plain green "add note" in both
directions, waking a put-down project but never forcing a run past a gate, a
pause, or a workspace that is already occupied.

Wes then put a bound on it, 2026-08-30: *"if there are other withstanding
questions (that are not deleted or saved for later), don't automatically run the
agent when a question is answered, but instead, only do this when the last
withstanding question is answered."* Plus an explicit opt-out on the card:
*"There should now be an option, when answering questions, to queue the answer
rather than running it immediately."*
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import db, notify, portalmcp, worker


@pytest.fixture
def client(temp_data_dir):
    from app import main

    # No context manager: that would run the lifespan hook and start the worker.
    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project(
        "Dice Tower", description="A thing.", stage="active",
        build_approved=True, slug="dice-tower",
    )


@pytest.fixture
def queued(monkeypatch):
    """Record what `answer_arrived` decides, without starting an agent."""
    calls: list[int] = []

    async def fake(project_row):
        calls.append(int(project_row["id"]))
        return True

    monkeypatch.setattr(worker, "note_arrived", fake)
    return calls


# --- the rule itself ----------------------------------------------------------


@pytest.mark.asyncio
async def test_answering_starts_a_run(project, queued):
    question = db.create_question(project["id"], "Which one?")

    assert await worker.answer_arrived(question) is True
    assert queued == [project["id"]]


@pytest.mark.asyncio
async def test_it_goes_through_note_arrived_not_straight_to_the_queue(project, monkeypatch):
    """The distinction that keeps a paused or gated project from being run by a
    reply. `queue_manual_run` is Wes putting a project on the working shelf;
    answering a question is not that, and using it here would mean a one-tap
    answer could start an agent on a project he had deliberately put down."""
    forced: list[int] = []

    async def must_not_run(project_id, *a, **k):
        forced.append(project_id)
        return True

    monkeypatch.setattr(worker, "queue_manual_run", must_not_run)
    monkeypatch.setattr(worker, "can_run_now", lambda p: False)
    question = db.create_question(project["id"], "Which one?")

    assert await worker.answer_arrived(question) is False
    assert forced == []


# --- the two refusals ---------------------------------------------------------


@pytest.mark.asyncio
async def test_no_run_when_an_agent_is_holding_still_for_this_answer(project, queued, monkeypatch):
    """An agent blocked inside `mcp__portal__ask` gets the answer within seconds,
    in the run it is already in. Starting a second agent would spawn a whole run
    to deliver a message that has already been delivered - and on this project it
    would land on a workspace the waiting run still holds."""
    question = db.create_question(project["id"], "Which one?")
    monkeypatch.setattr(portalmcp, "waiting_run", lambda qid: 4242)

    assert await worker.answer_arrived(question) is False
    assert queued == []


@pytest.mark.asyncio
async def test_an_answer_on_a_deleted_project_starts_nothing(project, queued):
    """`questions.project_id` is NOT NULL, so the row always names a project -
    but deleting a project does not delete its questions, and a notification
    sent days ago can still be tapped. There is no workspace left to run in, and
    handing `None` to `note_arrived` would be a 500 on the answer button."""
    question = db.create_question(project["id"], "Which one?")
    db.delete_project(project["id"])

    assert await worker.answer_arrived(question) is False
    assert queued == []


@pytest.mark.asyncio
async def test_the_waiting_check_asks_about_this_question(project, queued, monkeypatch):
    """A guard that ignored its argument would suppress the run for every
    question whenever any agent anywhere was waiting on some other one."""
    asked: list[int] = []

    def spy(qid):
        asked.append(int(qid))
        return None

    monkeypatch.setattr(portalmcp, "waiting_run", spy)
    question = db.create_question(project["id"], "Which one?")

    await worker.answer_arrived(question)

    assert asked == [int(question["id"])]


# --- through the real endpoints -----------------------------------------------


def test_the_web_answer_endpoint_starts_a_run(client, project, monkeypatch):
    """End to end, because the wiring is the part that breaks: `answer_arrived`
    could be perfect and never called."""
    started: list[int] = []

    async def fake(question):
        started.append(int(question["id"]))
        return True

    monkeypatch.setattr(worker, "answer_arrived", fake)
    question = db.create_question(project["id"], "Which one?")

    resp = client.post(
        f"/questions/{question['id']}/answer",
        data={"answer": "the second one"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert started == [int(question["id"])]
    assert db.get_question(question["id"])["status"] == "answered"


def test_an_empty_answer_starts_nothing(client, project, monkeypatch):
    """Nothing was chosen and nothing was typed, so the question is still open.
    Running an agent on a non-answer would burn a run to tell it nothing."""
    started: list[int] = []

    async def fake(question):
        started.append(int(question["id"]))
        return True

    monkeypatch.setattr(worker, "answer_arrived", fake)
    question = db.create_question(project["id"], "Which one?")

    client.post(
        f"/questions/{question['id']}/answer",
        data={"answer": "   "},
        follow_redirects=False,
    )

    assert started == []
    assert db.get_question(question["id"])["status"] == "open"


def test_dismissing_a_question_starts_nothing(client, project, monkeypatch):
    """"Saved for later" is the opposite of an instruction."""
    started: list[int] = []

    async def fake(question):
        started.append(int(question["id"]))
        return True

    monkeypatch.setattr(worker, "answer_arrived", fake)
    question = db.create_question(project["id"], "Which one?")

    client.post(f"/questions/{question['id']}/dismiss", follow_redirects=False)

    assert started == []


# --- only the last open question starts a run ---------------------------------


@pytest.mark.asyncio
async def test_an_answer_that_leaves_another_question_open_starts_nothing(project, queued):
    """The whole of Wes's 2026-08-30 rule. An agent that filed three questions
    needs three answers; running it on the first would send it back to work
    knowing one of the three things it said it could not proceed without."""
    first = db.create_question(project["id"], "Which one?")
    db.create_question(project["id"], "And what color?")

    assert await worker.answer_arrived(first) is False
    assert queued == []


@pytest.mark.asyncio
async def test_the_last_answer_starts_the_run(project, queued):
    """The other half: once nothing is left to answer, the run happens - and it
    carries every answer, because each was journalled as it was given."""
    first = db.create_question(project["id"], "Which one?")
    second = db.create_question(project["id"], "And what color?")
    db.answer_question_and_resume(first["id"], "the second one")
    await worker.answer_arrived(first)

    db.answer_question_and_resume(second["id"], "cobalt blue")
    assert await worker.answer_arrived(second) is True
    assert queued == [project["id"]]


@pytest.mark.asyncio
async def test_a_question_saved_for_later_does_not_hold_the_run_back(project, queued):
    """Wes named the exclusions himself: "not deleted or saved for later". A
    dismissed question is a deliberate "not now", so it is not something the run
    is still waiting on."""
    answered = db.create_question(project["id"], "Which one?")
    parked = db.create_question(project["id"], "And what color?")
    db.dismiss_question(parked["id"])

    assert await worker.answer_arrived(answered) is True
    assert queued == [project["id"]]


@pytest.mark.asyncio
async def test_a_deleted_question_does_not_hold_the_run_back(project, queued):
    """The other exclusion. A deleted question is answered with DELETED_ANSWER
    and can never be filed again, so nothing is waiting on it."""
    answered = db.create_question(project["id"], "Which one?")
    binned = db.create_question(project["id"], "And what color?")
    db.delete_question(binned["id"])

    assert await worker.answer_arrived(answered) is True
    assert queued == [project["id"]]


@pytest.mark.asyncio
async def test_an_open_question_on_another_project_is_none_of_this_projects_business(
    project, queued
):
    """The count is scoped to one project. "The agent" in Wes's note is this
    project's agent; a question open on some other project says nothing about
    whether this one is ready to be worked on, and a global count would leave
    a busy board unable to start any run at all."""
    other = db.create_project("Cork Engraver", stage="active", slug="cork-engraver")
    db.create_question(other["id"], "How dark does it go?")
    question = db.create_question(project["id"], "Which one?")

    assert await worker.answer_arrived(question) is True
    assert queued == [project["id"]]


@pytest.mark.asyncio
async def test_the_question_being_answered_does_not_block_itself(project, queued):
    """`answer_arrived` is called after the row is settled, but it must not
    depend on that: it excludes the question by id, so asking before the write
    gives the same answer as asking after it."""
    question = db.create_question(project["id"], "Which one?")

    assert db.get_question(question["id"])["status"] == "open"
    assert await worker.answer_arrived(question) is True
    assert queued == [project["id"]]


def test_the_endpoint_holds_the_run_back_while_another_question_is_open(
    client, project, monkeypatch
):
    """End to end: the rule has to survive the route, not just the helper."""
    calls: list[int] = []

    async def fake(project_row):
        calls.append(int(project_row["id"]))
        return True

    monkeypatch.setattr(worker, "note_arrived", fake)
    first = db.create_question(project["id"], "Which one?")
    second = db.create_question(project["id"], "And what color?")

    client.post(f"/questions/{first['id']}/answer",
                data={"answer": "the second one"}, follow_redirects=False)
    assert calls == []

    client.post(f"/questions/{second['id']}/answer",
                data={"answer": "cobalt blue"}, follow_redirects=False)
    assert calls == [project["id"]]


# --- queue answer -------------------------------------------------------------


def test_queue_answer_records_the_answer_and_starts_nothing(client, project, monkeypatch):
    """Wes, 2026-08-30: "an option, when answering questions, to queue the answer
    rather than running it immediately." The answer is real - journalled, the
    question settled - and no run comes of it even though it was the last one
    open."""
    started: list[int] = []

    async def fake(question):
        started.append(int(question["id"]))
        return True

    monkeypatch.setattr(worker, "answer_arrived", fake)
    question = db.create_question(project["id"], "Which one?")

    resp = client.post(
        f"/questions/{question['id']}/answer",
        data={"answer": "the second one", "then": "queue"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert started == []
    assert db.get_question(question["id"])["status"] == "answered"
    bodies = [e["content_md"] for e in db.list_journal(project["id"])]
    assert any("the second one" in b for b in bodies)


def test_anything_other_than_queue_still_runs(client, project, monkeypatch):
    """The opt-out is one exact word. A `then` the form never sends must not be
    read as "queue" - that would turn a plain answer into a silent no-op."""
    started: list[int] = []

    async def fake(question):
        started.append(int(question["id"]))
        return True

    monkeypatch.setattr(worker, "answer_arrived", fake)
    question = db.create_question(project["id"], "Which one?")

    client.post(
        f"/questions/{question['id']}/answer",
        data={"answer": "the second one", "then": "run"},
        follow_redirects=False,
    )

    assert started == [int(question["id"])]


def test_the_question_card_offers_the_queue_button(client, project):
    """The button has to exist and has to post `then=queue` from the same form,
    or the route's opt-out is unreachable from the page."""
    db.create_question(project["id"], "Which one?")

    body = client.get("/questions").text

    assert 'name="then" value="queue"' in body
    assert "queue answer" in body


def test_a_queued_answer_still_settles_the_telegram_copies(client, project, monkeypatch):
    """Queueing changes only whether a run starts. Whoever got the question on
    Telegram must still stop seeing it as open."""
    settled: list[int] = []

    async def fake(question_id, verdict):
        settled.append(int(question_id))

    monkeypatch.setattr(notify, "settle_question_copies", fake)
    question = db.create_question(project["id"], "Which one?")

    client.post(
        f"/questions/{question['id']}/answer",
        data={"answer": "the second one", "then": "queue"},
        follow_redirects=False,
    )

    assert settled == [int(question["id"])]


def test_the_answer_is_journaled_before_the_run_is_queued(client, project, monkeypatch):
    """Ordering, and it is load-bearing: the run this queues reads the project's
    journal to build its prompt, so an answer recorded afterwards would produce
    an agent woken BY the answer that cannot see it."""
    seen: list[bool] = []

    async def fake(question):
        bodies = [e["content_md"] for e in db.list_journal(project["id"])]
        seen.append(any("the second one" in b for b in bodies))
        return True

    monkeypatch.setattr(worker, "answer_arrived", fake)
    question = db.create_question(project["id"], "Which one?")

    client.post(
        f"/questions/{question['id']}/answer",
        data={"answer": "the second one"},
        follow_redirects=False,
    )

    assert seen == [True]

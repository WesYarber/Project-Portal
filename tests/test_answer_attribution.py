"""Who answered a question, and why guessing is worse than not knowing.

The portal became multi-person on 2026-07-28: a project has members, and the
agent contract now tells a run to "pitch each answer at the person you are
answering". Notes posted through the web form were stamped with a person from
that day. Answers were not - so the one channel where somebody states an
intention in their own words arrived at the next run anonymous, and an agent
following the contract had nothing to follow it with.

The rule these pin is that an unknown answerer stays unknown. `people.name_of`
falls back to the owner's name, which is right for a byline that must never
render blank and would be actively wrong here: it would turn "nobody recorded
who answered" into "Wes answered", and the agent reads that and pitches its next
reply at the wrong person. `people.known_name` is the strict version.
"""
from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from app import agent_runner, db, main, people


@pytest.fixture
def project():
    return db.create_project("Metronome", description="A thing.", stage="active",
                             build_approved=True, slug="metronome")


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def karli():
    return people.get(people.add(name="Karli", background="Newer to this."))


def _ask(project) -> int:
    return db.create_question(project["id"], "Acoustic or electronic?")["id"]


# --- the column --------------------------------------------------------------


def test_an_answer_records_who_gave_it(project, karli):
    qid = _ask(project)
    db.answer_question_and_resume(qid, "acoustic", karli["id"])
    assert db.get_question(qid)["answered_by"] == karli["id"]


def test_an_answer_from_a_channel_that_cannot_say_stays_unattributed(project):
    # Telegram, today: the bot knows a chat id and the portal has no map from
    # one to a person yet. NULL is the honest record.
    qid = _ask(project)
    db.answer_question_and_resume(qid, "acoustic")
    assert db.get_question(qid)["answered_by"] is None


def test_the_journal_entry_carries_the_same_person_as_the_question(project, karli):
    # Two places, read by different things - the journal entry by the prompt's
    # note machinery, the column by the answered-questions section - so an
    # answer attributed in one and anonymous in the other is worse than either.
    qid = _ask(project)
    db.answer_question_and_resume(qid, "acoustic", karli["id"])
    entry = [r for r in db.list_journal(project["id"]) if r["kind"] == "answer"][0]
    assert entry["person_id"] == karli["id"]


def test_answering_over_the_web_stamps_the_person_it_resolved(project, client):
    qid = _ask(project)
    client.post(f"/questions/{qid}/answer", data={"answer": "acoustic"},
                follow_redirects=False)
    # No cookie and no tailnet login, so `people.resolve` lands on the owner -
    # which is a resolution, not a guess, and is exactly who is at the keyboard.
    assert db.get_question(qid)["answered_by"] == people.owner()["id"]


# --- never inventing one -----------------------------------------------------


def test_known_name_refuses_to_fall_back_to_the_owner(karli):
    assert people.known_name(karli["id"]) == "Karli"
    assert people.known_name(None) == ""
    assert people.known_name(999999) == ""
    # ...whereas the byline helper does fall back, on purpose, and that is
    # precisely why it is the wrong tool for an attribution.
    assert people.name_of(None) == people.owner()["name"]


# --- what reaches the agent --------------------------------------------------


def _qa_section(project) -> str:
    prompt = agent_runner.build_prompt("build", project)
    return prompt.split("## Answered questions", 1)[1].split("\n##", 1)[0]


def test_the_prompt_names_who_answered_once_more_than_one_person_could_have(project,
                                                                            karli):
    qid = _ask(project)
    db.answer_question_and_resume(qid, "acoustic", karli["id"])
    assert "A (Karli): acoustic" in _qa_section(project)


def test_a_one_person_install_gets_the_section_it_always_got(project):
    qid = _ask(project)
    db.answer_question_and_resume(qid, "acoustic", people.owner()["id"])
    # Naming the only person there is would be noise in every prompt, and the
    # prompt is under a byte budget.
    section = _qa_section(project)
    assert "A: acoustic" in section
    assert people.owner()["name"] not in section


def test_an_unattributed_answer_is_not_credited_to_the_owner(project, karli):
    qid = _ask(project)
    db.answer_question_and_resume(qid, "acoustic")     # no person
    section = _qa_section(project)
    assert "A: acoustic" in section
    assert "Karli" not in section
    assert people.owner()["name"] not in section


# --- and what shows on the page ----------------------------------------------


def test_the_journal_row_carries_the_name_once_there_is_more_than_one_person(project,
                                                                             karli):
    db.add_journal(project["id"], "user", "note", "Make it louder.",
                   person_id=karli["id"])
    entry = db.list_journal(project["id"])[0]
    assert main.byline(entry) == "Karli"


def test_the_journal_row_says_user_on_a_one_person_install(project):
    db.add_journal(project["id"], "user", "note", "Make it louder.",
                   person_id=people.owner()["id"])
    assert main.byline(db.list_journal(project["id"])[0]) == "user"


def test_an_old_unstamped_note_stays_honestly_anonymous(project, karli):
    # Written before person stamping existed. Printing the owner over it would
    # be a guess that reads exactly like a fact.
    db.add_journal(project["id"], "user", "note", "From the before times.")
    assert main.byline(db.list_journal(project["id"])[0]) == "user"


def test_an_agent_entry_is_never_given_a_persons_name(project, karli):
    db.add_journal(project["id"], "agent", "progress", "## A run\n\nBody.")
    assert main.byline(db.list_journal(project["id"])[0]) == "agent"

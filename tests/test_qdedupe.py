"""The same question must not reach Wes twice in two wordings.

His note, 2026-07-26:

    I often get multiple questions from projects asking the same thing. I
    should not get the same question asked multiple times in multiple ways.
    Ensure when asking a question that the questions waiting to be answered do
    not ask the same thing.

The pairs below are not invented: the repeated spend-down offer is lifted
verbatim from his own answered-questions list, where the same sentence was
filed eleven times with only the countdown moving, and he eventually replied
"You asked me way too many times here. I just want to be asked once".

Half of these tests exist to prove the guard does NOT fire. A dedupe that
silently swallows a real question is a worse bug than the one it fixes: he
never learns there was something to decide, and the run that needed the answer
sits on it forever.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db, qdedupe
from fixtures_name_questions import NAME_QUESTIONS

# The real thing, twice, six minutes apart.
SPEND_A = (
    "Your weekly Claude window resets in 7h 59m with 47% of it unused - that headroom "
    "does not roll over, it just disappears at the reset. Want me to spend it down? Say "
    "yes and I will lift the portal's own run budget and pacing until then and work "
    "through the backlog; say no and I will leave it alone."
)
SPEND_B = SPEND_A.replace("7h 59m", "6h 51m")


@pytest.fixture
def project():
    return db.create_project("Dice Tower", stage="active", slug="dice-tower")


@pytest.fixture
def other():
    return db.create_project("Tak", stage="active", slug="tak")


# --- what counts as the same question --------------------------------------

def test_the_same_offer_with_a_different_countdown_is_the_same_question():
    assert qdedupe.similarity(SPEND_A, SPEND_B) >= qdedupe.THRESHOLD


def test_the_same_question_reworded_is_the_same_question():
    a = "Should I use design A or design B for the settings page?"
    b = "Which design do you want for the settings page, A or B?"
    assert qdedupe.similarity(a, b) >= qdedupe.THRESHOLD


def test_word_order_does_not_matter():
    a = "Do you want the leaderboard on the results screen?"
    b = "On the results screen, do you want a leaderboard?"
    assert qdedupe.similarity(a, b) >= qdedupe.THRESHOLD


# --- what does not ----------------------------------------------------------

def test_one_word_apart_but_a_different_decision():
    """The pair that rules out edit-distance similarity: these two read 85%
    alike as strings and are two different answers."""
    a = "Which colour do you want for the header?"
    b = "Which colour do you want for the footer?"
    assert qdedupe.similarity(a, b) < qdedupe.THRESHOLD


def test_two_model_releases_are_not_one_question():
    """The model watcher's sentence is mostly scaffolding, so plain overlap
    scores this pair 0.67 - above the threshold. The identifiers are what keep
    them apart; without that check one of two real releases is never asked."""
    a = "Opus 6 (`claude-opus-6`) is out. Want the portal to move onto it?"
    b = "Sonnet 6 (`claude-sonnet-6`) is out. Want the portal to move onto it?"
    assert qdedupe.similarity(a, b) < qdedupe.THRESHOLD


def test_a_rewording_may_drop_a_name_but_not_swap_one():
    a = "Adopt `claude-opus-6` as the default agent?"
    b = "Adopt the new opus as the default agent?"
    assert qdedupe.similarity(a, b) >= qdedupe.THRESHOLD


def test_different_hosts_are_different_questions():
    a = "Should I deploy the game to example.net or leave it local?"
    b = "Should I deploy the game to example.org or leave it local?"
    assert qdedupe.similarity(a, b) < qdedupe.THRESHOLD


def test_unrelated_questions_score_near_zero():
    a = "Which colour for the header?"
    b = "Should I buy the e-ink panel now or wait for the sale?"
    assert qdedupe.similarity(a, b) < qdedupe.THRESHOLD


# --- the guard at the choke point ------------------------------------------

def test_a_duplicate_is_not_inserted(project):
    first = db.file_question(project["id"], SPEND_A)
    second = db.file_question(project["id"], SPEND_B)
    assert first.created is True
    assert second.created is False
    assert second.row["id"] == first.row["id"]
    assert second.duplicate_of["id"] == first.row["id"]
    assert len(db.open_questions(project["id"])) == 1


def test_a_different_question_still_gets_through(project):
    db.file_question(project["id"], SPEND_A)
    other = db.file_question(project["id"], "Which colour for the header?")
    assert other.created is True
    assert len(db.open_questions(project["id"])) == 2


def test_the_original_wording_and_slot_survive(project):
    """Whatever he was already shown is what stays. The dupe does not rewrite
    the question under him, and it does not take a second slot number."""
    first = db.file_question(project["id"], SPEND_A, quick_options="yes|no")
    second = db.file_question(project["id"], SPEND_B, quick_options="")
    assert second.row["question"] == SPEND_A
    assert second.row["quick_options"] == "yes|no"
    assert second.row["slot"] == first.row["slot"]


def test_another_project_may_ask_the_same_thing(project, other):
    """Two projects asking "should I deploy this?" are two decisions with two
    answers. Merging them would lose one."""
    a = db.file_question(project["id"], "Should I deploy this to the live site?")
    b = db.file_question(other["id"], "Should I deploy this to the live site?")
    assert a.created and b.created
    assert a.row["id"] != b.row["id"]


def _backdate(question_id: int, hours: float) -> None:
    when = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn = db.get_conn()
    conn.execute("UPDATE questions SET ts = ?, answered_at = ? WHERE id = ?", (when, when, question_id))
    conn.commit()


def test_answering_does_not_reopen_the_door_immediately(project):
    """The eleven-times case. Every one of those was answered before the next
    was filed, so "open questions only" would have caught none of them."""
    first = db.file_question(project["id"], SPEND_A)
    db.answer_question(first.row["id"], "yes")
    again = db.file_question(project["id"], SPEND_B)
    assert again.created is False


def test_dismissing_does_not_reopen_the_door_immediately(project):
    first = db.file_question(project["id"], SPEND_A)
    db.dismiss_question(first.row["id"])
    again = db.file_question(project["id"], SPEND_B)
    assert again.created is False


def test_an_old_answer_does_not_block_a_real_follow_up(project):
    """An answer changes the world: the same words a day later can be a genuine
    next question, so the guard lets go rather than banning the topic."""
    first = db.file_question(project["id"], SPEND_A)
    db.answer_question(first.row["id"], "yes")
    _backdate(first.row["id"], db.QUESTION_SETTLED_HOURS + 1)
    again = db.file_question(project["id"], SPEND_B)
    assert again.created is True


def test_an_open_question_never_ages_out(project):
    """Unanswered is unanswered. Only *settled* questions expire from the
    check - one he has not replied to still blocks its own repeat forever."""
    first = db.file_question(project["id"], SPEND_A)
    _backdate(first.row["id"], db.QUESTION_SETTLED_HOURS * 10)
    again = db.file_question(project["id"], SPEND_B)
    assert again.created is False
    assert again.row["id"] == first.row["id"]


def test_create_question_shim_returns_the_matched_row(project):
    a = db.create_question(project["id"], SPEND_A)
    b = db.create_question(project["id"], SPEND_B)
    assert b["id"] == a["id"]


# --- the prevention half: the agent can see what it already asked ----------

def test_open_questions_reach_the_run_prompt(project):
    db.file_question(project["id"], "Which colour for the header?")
    section = qdedupe.prompt_section(project["id"])
    assert "Which colour for the header?" in section
    assert "Do NOT ask any of them again" in section


def test_no_heading_when_nothing_is_waiting(project):
    assert qdedupe.prompt_section(project["id"]) == ""


def test_answered_questions_are_not_listed_as_waiting(project):
    q = db.file_question(project["id"], "Which colour for the header?")
    db.answer_question(q.row["id"], "blue")
    assert qdedupe.prompt_section(project["id"]) == ""


def test_the_prompt_carries_the_waiting_block(project):
    from app import agent_runner

    db.file_question(project["id"], "Which colour for the header?")
    prompt = agent_runner.build_prompt("BUILD.", db.get_project(project["id"]))
    assert "Questions already waiting for an answer" in prompt
    assert "Which colour for the header?" in prompt


def test_the_contract_forbids_re_asking():
    from app import agent_runner

    assert "Never re-ask something already waiting" in agent_runner.AGENT_CONTRACT


# --- degradation ------------------------------------------------------------

def test_empty_and_junk_never_raise():
    assert qdedupe.similarity("", "") == 0.0
    assert qdedupe.similarity("Which colour?", "") == 0.0
    # No topic words at all on either side: identical scaffolding is still a
    # repeat, but anything less is left alone rather than guessed at.
    assert qdedupe.similarity("Should I?", "Should I?") == 1.0
    assert qdedupe.similarity("???", "???") == 0.0
    assert qdedupe.find_duplicate("anything", []) is None


# --- the answer space: the signal the live data forced -----------------------

def test_the_eight_real_name_questions_collapse_to_one(project):
    """The corpus this was built against. Filing all eight, in the order Wes
    actually received them, must leave exactly one question on his board."""
    created = [
        db.file_question(project["id"], text, quick_options=opts).created
        for text, opts in NAME_QUESTIONS
    ]
    assert created[0] is True
    assert not any(created[1:]), f"{sum(created)} of 8 got through"
    assert len(db.open_questions(project["id"])) == 1


def test_wording_alone_would_not_have_caught_them(project):
    """Why the answer space had to exist, stated so it can be falsified: file
    the same eight with their menus stripped off, and wording alone lets more
    than one through. If that ever stops being true the extra machinery is dead
    weight and this test says so."""
    got_through = sum(
        db.file_question(project["id"], text).created for text, _ in NAME_QUESTIONS
    )
    assert got_through > 1


def test_a_generic_menu_never_makes_two_questions_the_same():
    """Every yes/no question shares an answer space. If options alone were
    enough, the first yes/no question on a project would swallow all the rest."""
    a = "Should I deploy the game to the live site now?"
    b = "Do you want a leaderboard on the results screen?"
    assert qdedupe.similarity(a, b, ["yes", "no"], ["yes", "no"]) < qdedupe.THRESHOLD
    assert qdedupe.answer_space(["yes", "no"]) == frozenset()
    assert qdedupe.answer_space(["not yet", "later", "skip"]) == frozenset()


def test_a_different_menu_of_real_answers_blocks_a_wording_match():
    """Two questions can read almost identically and still offer different
    decisions - the menu is the tie-break, and it wins over the wording."""
    a = "The name is the last thing blocking the launch. Which do you want?"
    b = "The name is the last thing blocking the launch. Which do you want?"
    assert qdedupe.similarity(a, b, ["Kithlog", "Porchlog"], ["Ledger", "Almanac"]) == 0.0


def test_options_are_read_from_json_or_pipes_or_a_list():
    for form in ('["Kithlog", "Porchlog"]', "Kithlog|Porchlog", ["Kithlog", "Porchlog"]):
        assert qdedupe.answer_space(form) == frozenset({"kithlog", "porchlog"})
    # Never raises on rubbish; a broken menu is just a question without one.
    assert qdedupe.answer_space("[not json") == frozenset()
    assert qdedupe.answer_space(None) == frozenset()


def test_a_reusable_menu_of_verbs_does_not_merge_two_features(project):
    """Both verbatim from the live database, four hours apart on ProxyTable.
    Two different features, one boilerplate menu - an earlier cut of this
    matcher merged them, which would have meant Wes never being asked about
    the second. The menu describes the *shape* of the decision, not which one
    it is, and only a menu that names things is allowed to identify a
    question."""
    tour = (
        "The guided table tour is live at example.net/proxy-table-dev but NOT on "
        "production example.net/proxy-table. Once you've tried it: promote it to "
        "production, keep it dev-only for now, or turn it off?"
    )
    learn = (
        "The new Learn-to-play section (12 lessons, six teaching decks) is live at "
        "example.net/proxy-table-dev/learn but NOT on production "
        "example.net/proxy-table. Promote it to production, or keep it dev-only "
        "while you try it?"
    )
    menu = '["promote it to production", "keep it dev-only"]'
    assert qdedupe.answer_space(menu, learn) == frozenset()
    assert qdedupe.similarity(tour, learn, menu, menu) < qdedupe.THRESHOLD
    assert db.file_question(project["id"], tour, quick_options=menu).created
    assert db.file_question(project["id"], learn, quick_options=menu).created


def test_a_shared_address_is_not_a_shared_subject():
    """Every question a project asks names the same host. Left in, that address
    is half a dozen tokens voting that any two of them are the same question."""
    tokens = qdedupe._tokens("live at example.net/proxy-table-dev now")
    assert "hostexamplenet" in tokens
    # The path is gone: it is the same on every question the project asks.
    assert not {"proxy", "table", "dev"} & tokens
    assert "hosttesthost" in qdedupe._tokens("see http://testhost:8500/settings")
    # ...but the host itself is a name, so two hosts are two subjects.
    assert qdedupe._marks("publish to example.net") == frozenset({"hostexamplenet"})

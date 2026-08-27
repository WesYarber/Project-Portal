"""A title a person typed is never renamed - but an agent may still suggest.

Wes, 2026-07-29: "when adding a new note, if a title is defined, do not change
it. If you want to suggest alternative titles, feel free to, but do not change a
title that the user set themselves."

Two halves. A title typed into the idea form (or the heading, or the sub-project
box) locks on the spot, where before only a later rename locked it - so the very
first agent run could rename something he had just named. And a locked title
stops swallowing the agent's proposal: it lands on the project page as a one-tap
offer, and turning it down is remembered so the same wording cannot come back
every run.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import agent_runner, db, worker


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _report(**fields):
    return agent_runner.RunResult(ok=True, report={"journal_entry_md": "did a thing", **fields})


def _fresh(project):
    return db.get_project(project["id"])


# --- a title he typed locks itself ------------------------------------------

def test_a_typed_title_on_a_new_idea_locks(client):
    client.post("/ideas", data={"title": "Dice Tower", "idea": "A tower that rolls dice."})
    project = db.get_project_by_slug(db.list_projects("id DESC")[0]["slug"])
    assert project["title"] == "Dice Tower"
    assert project["title_locked"] == 1


def test_an_auto_generated_title_stays_open(client):
    """Blank title box: the name is cut from the first line of the idea, which
    is a placeholder rather than something he chose. An agent improving that is
    the feature, not the bug."""
    client.post("/ideas", data={"title": "  ", "idea": "A tower that rolls dice.\nmore"})
    project = db.list_projects("id DESC")[0]
    assert project["title"] == "A tower that rolls dice."
    assert project["title_locked"] == 0


def test_an_auto_named_project_is_still_renamed_by_an_agent(client):
    client.post("/ideas", data={"idea": "something about dice towers"})
    project = db.list_projects("id DESC")[0]
    worker._apply_report(project, _report(title="Dice Tower"))
    assert _fresh(project)["title"] == "Dice Tower"
    assert not db.title_suggestion(_fresh(project))


def test_a_hand_typed_subproject_title_locks(client):
    parent = db.create_project("Board Games", slug="board-games")
    client.post(f"/project/{parent['slug']}/subproject", data={"title": "Tak", "description": ""})
    child = db.get_project_by_slug(db.list_projects("id DESC")[0]["slug"])
    assert child["title"] == "Tak"
    assert child["title_locked"] == 1


# --- the suggestion channel -------------------------------------------------

@pytest.fixture
def named(temp_data_dir):
    """A project whose title Wes typed himself."""
    return db.create_project("Dice Tower", slug="dice-tower", title_locked=True)


def test_a_locked_title_is_not_renamed_but_is_recorded(named):
    worker._apply_report(named, _report(title="Cascading Dice Chute"))
    row = _fresh(named)
    assert row["title"] == "Dice Tower"
    assert db.title_suggestion(row) == "Cascading Dice Chute"


def test_an_unlocked_title_leaves_no_suggestion_behind(temp_data_dir):
    p = db.create_project("Dice Tower", slug="dice-tower")
    worker._apply_report(p, _report(title="Cascading Dice Chute"))
    row = _fresh(p)
    assert row["title"] == "Cascading Dice Chute"
    assert not db.title_suggestion(row)


def test_proposing_the_title_already_in_use_is_not_a_suggestion(named):
    worker._apply_report(named, _report(title="dice tower"))
    assert not db.title_suggestion(_fresh(named))
    # And it is refused at the door, not filtered on the way out. Reporting the
    # title a project already has is the commonest thing a run does with the
    # field, so it must not count as news.
    assert db.propose_title(_fresh(named), "dice tower") is False


def test_re_reporting_the_current_title_does_not_wipe_a_pending_offer(named):
    """The bite in the line above, and the reason it is a guard in
    `propose_title` rather than only in `title_suggestion`.

    A run that leaves the title alone still reports one, and one run's echo of
    the name in use must not throw away a different name the previous run
    proposed and Wes has not answered yet.
    """
    db.propose_title(named, "Cascading Dice Chute")
    stamped = _fresh(named)["updated_at"]

    assert db.propose_title(_fresh(named), "Dice Tower") is False
    assert db.title_suggestion(_fresh(named)) == "Cascading Dice Chute"
    assert _fresh(named)["updated_at"] == stamped


def test_a_suggestion_matching_the_title_stops_being_shown(named):
    """Belt and braces: accepting the name from any other door (the heading, the
    details form) takes the offer off the page without a second write."""
    db.propose_title(named, "Cascading Dice Chute")
    db.update_project(named["id"], title="Cascading Dice Chute")
    assert not db.title_suggestion(_fresh(named))


def test_re_proposing_the_same_wording_does_not_touch_the_row(named):
    db.propose_title(named, "Cascading Dice Chute")
    stamped = _fresh(named)["updated_at"]
    assert db.propose_title(_fresh(named), "Cascading Dice Chute") is False
    assert _fresh(named)["updated_at"] == stamped


def test_a_second_different_suggestion_replaces_the_first(named):
    db.propose_title(named, "Cascading Dice Chute")
    db.propose_title(_fresh(named), "The Dice Waterfall")
    assert db.title_suggestion(_fresh(named)) == "The Dice Waterfall"


# --- his two answers --------------------------------------------------------

def test_taking_the_suggestion_renames_and_keeps_the_lock(client, named):
    db.propose_title(named, "Cascading Dice Chute")
    client.post(f"/project/{named['slug']}/title-suggestion", data={"action": "apply"})

    row = _fresh(named)
    assert row["title"] == "Cascading Dice Chute"
    # Still his title: he has now chosen this one as deliberately as if he had
    # typed it, so the next agent must not rename it either.
    assert row["title_locked"] == 1
    assert not db.title_suggestion(row)


def test_taking_the_suggestion_is_journalled(client, named):
    db.propose_title(named, "Cascading Dice Chute")
    client.post(f"/project/{named['slug']}/title-suggestion", data={"action": "apply"})
    entries = db.list_journal(limit=10, only_projects={named["id"]})
    assert any("Cascading Dice Chute" in e["content_md"] for e in entries)


def test_turning_it_down_clears_the_offer(client, named):
    db.propose_title(named, "Cascading Dice Chute")
    client.post(f"/project/{named['slug']}/title-suggestion", data={"action": "dismiss"})
    row = _fresh(named)
    assert row["title"] == "Dice Tower"
    assert not db.title_suggestion(row)


def test_a_wording_he_turned_down_never_comes_back(client, named):
    """The whole point of remembering the rejected text. An agent that likes its
    own name for a project would otherwise re-propose it every single run, and a
    suggestion you have already declined is not a suggestion."""
    db.propose_title(named, "Cascading Dice Chute")
    client.post(f"/project/{named['slug']}/title-suggestion", data={"action": "dismiss"})

    worker._apply_report(_fresh(named), _report(title="Cascading Dice Chute"))
    assert not db.title_suggestion(_fresh(named))
    # Case and stray space are the same wording, not a new one.
    worker._apply_report(_fresh(named), _report(title="  cascading dice chute  "))
    assert not db.title_suggestion(_fresh(named))


def test_a_genuinely_different_wording_still_gets_through(client, named):
    db.propose_title(named, "Cascading Dice Chute")
    client.post(f"/project/{named['slug']}/title-suggestion", data={"action": "dismiss"})
    worker._apply_report(_fresh(named), _report(title="The Dice Waterfall"))
    assert db.title_suggestion(_fresh(named)) == "The Dice Waterfall"


def test_renaming_it_himself_answers_the_offer(client, named):
    """He has just decided the name. A stale offer of a third name sitting under
    the heading he just edited is noise."""
    db.propose_title(named, "Cascading Dice Chute")
    client.post(f"/project/{named['slug']}/rename", data={"title": "Dice Chute"})

    row = _fresh(named)
    assert row["title"] == "Dice Chute"
    assert not db.title_suggestion(row)
    # And it counts as a no, so the run that proposed it cannot repeat it.
    worker._apply_report(row, _report(title="Cascading Dice Chute"))
    assert not db.title_suggestion(_fresh(named))


def test_editing_the_title_in_the_details_form_answers_the_offer(client, named):
    db.propose_title(named, "Cascading Dice Chute")
    client.post(
        f"/project/{named['slug']}/details",
        data={"title": "Dice Chute", "description": "", "title_locked": "on"},
    )
    assert not db.title_suggestion(_fresh(named))


# --- the page ---------------------------------------------------------------

def test_the_project_page_offers_the_suggestion(client, named):
    db.propose_title(named, "Cascading Dice Chute")
    html = client.get(f"/project/{named['slug']}").text
    assert "Cascading Dice Chute" in html
    assert f'action="/project/{named["slug"]}/title-suggestion"' in html
    assert "use that title" in html
    assert "keep mine" in html


def test_a_project_with_no_suggestion_shows_no_box(client, named):
    html = client.get(f"/project/{named['slug']}").text
    assert "title-suggestion" not in html


def test_the_idea_form_says_a_typed_title_is_yours(client):
    html = client.get("/").text
    assert "agents can suggest a" in html

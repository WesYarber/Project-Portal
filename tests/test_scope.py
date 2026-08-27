"""You see the projects you are on, and the owner has a separate door.

Wes, 2026-07-28:

  "I only want users to see projects they are included on. with the exception
   of me as the admin - I want a way where I can go view other users' projects,
   but dont want them in my main feed."

The rule itself is four lines in app/scope.py. Everything hard about this is
that a project title leaks from more surfaces than the word "dashboard"
suggests, and the ones that bite are the ones that do not look like project
listings at all:

- the journal feed under the shelves, which carries other people's entries
- the live-run strip at the top of *every* page, and `/api/active-run` behind
  it, which every page polls every few seconds
- the nav question badge, which is a global rendered on every page
- the project menu on /activity, which enumerates every project by title

So most of this file is one test per leak. The section after them pins the two
rules that decide what "yours" means, and the last one pins what this feature
deliberately is NOT: an authorization boundary.
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from app import db, people, scope


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def board(client):
    """His project, her project, and a third they share.

    Returned as ids so the tests can talk about people rather than rows.
    """
    her_id = people.add("Erin", gender="female", background="Newer to all of this.")
    his_id = int(people.owner()["id"])

    his = int(db.create_project("His Thing", stage="active")["id"])
    hers = int(db.create_project("Her Thing", stage="active")["id"])
    both = int(db.create_project("Shared Thing", stage="active")["id"])

    people.set_members(hers, [her_id])
    people.set_members(both, [his_id, her_id])
    return {
        "his_id": his_id, "her_id": her_id,
        "his": his, "hers": hers, "both": both,
    }


def as_her(client, board):
    client.cookies.set(people.COOKIE, people.get(board["her_id"])["slug"])


def as_him(client, board):
    client.cookies.set(people.COOKIE, people.get(board["his_id"])["slug"])


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------

def test_you_see_the_projects_you_are_on(client, board):
    assert scope.visible_ids(people.get(board["her_id"])) == {board["hers"], board["both"]}


def test_the_owner_is_filtered_like_everybody_else(client, board):
    """The half that is easy to get backwards. He asked for other people's work
    NOT to be in his main feed, so being the owner buys him /everyone - not an
    exemption from the filter."""
    mine = scope.visible_ids(people.owner())
    assert board["hers"] not in mine
    assert mine == {board["his"], board["both"]}


def test_a_project_nobody_is_on_falls_to_the_owner(client, board):
    """Not reachable through the app - create_project always adds a member and
    set_members refuses to empty a project - so this forces the state with a
    direct DELETE. The branch stays because the alternative failure is silent:
    a project in no feed at all is live work that has vanished."""
    db.get_conn().execute("DELETE FROM project_people WHERE project_id = ?", (board["hers"],))
    db.get_conn().commit()
    assert board["hers"] in scope.visible_ids(people.owner())
    assert board["hers"] not in scope.visible_ids(people.get(board["her_id"]))


# --------------------------------------------------------------------------
# One test per leak
# --------------------------------------------------------------------------

def test_the_dashboard_shows_only_your_own_cards(client, board):
    as_her(client, board)
    html = client.get("/").text
    assert "Her Thing" in html and "Shared Thing" in html
    assert "His Thing" not in html


def test_the_journal_feed_does_not_carry_other_peoples_entries(client, board):
    db.add_journal(board["his"], "agent", "progress", "A secret about His Thing")
    db.add_journal(board["hers"], "agent", "progress", "Something about Her Thing")
    as_her(client, board)
    html = client.get("/").text
    assert "Something about Her Thing" in html
    assert "A secret about His Thing" not in html


def test_a_quiet_project_still_fills_its_own_feed(client, board):
    """The reason list_journal narrows in SQL rather than after the LIMIT.

    Filtering afterwards lets the other 25 entries eat every slot, so somebody
    whose one project is quiet opens the portal to an empty feed and concludes
    the thing is broken. Thirty of his against one of hers, and hers has to
    survive."""
    for i in range(30):
        db.add_journal(board["his"], "agent", "progress", f"his entry {i}")
    db.add_journal(board["hers"], "agent", "progress", "her only entry")
    as_her(client, board)
    assert "her only entry" in client.get("/").text


def test_install_wide_journal_entries_reach_everybody(client, board):
    """An entry with no project is the portal talking about itself - a restart,
    a new model. It belongs to nobody, so scoping must not swallow it."""
    db.add_journal(None, "system", "status", "The service is restarting")
    as_her(client, board)
    assert "The service is restarting" in client.get("/").text


def test_the_nav_badge_counts_only_your_own_questions(client, board):
    db.create_question(board["his"], "Something only he can answer")
    as_her(client, board)
    from app import main

    client.get("/")  # establishes the request context the global reads
    assert "Something only he can answer" not in client.get("/questions").text


def test_the_questions_page_shows_only_your_own(client, board):
    db.create_question(board["his"], "His question")
    db.create_question(board["hers"], "Her question")
    as_her(client, board)
    html = client.get("/questions").text
    assert "Her question" in html
    assert "His question" not in html


def test_the_activity_project_menu_lists_only_your_own(client, board):
    as_her(client, board)
    html = client.get("/activity").text
    assert "Her Thing" in html
    assert "His Thing" not in html


def test_an_activity_filter_for_a_project_you_are_not_on_is_ignored(client, board):
    """A hand-typed slug falls back to the unfiltered view rather than to an
    empty table with somebody else's project name sitting in the filter box."""
    as_her(client, board)
    r = client.get("/activity?project=his-thing")
    assert r.status_code == 200
    assert 'value="his-thing" selected' not in r.text


def test_the_live_run_strip_does_not_announce_other_peoples_work(client, board):
    """The strip is on every page and carries a title and a slug, which makes
    it the one surface that leaks without ever looking like a project list."""
    run_id = db.create_run(board["his"], "build", "opus")
    as_her(client, board)
    assert "His Thing" not in client.get("/").text
    assert db.get_run(run_id) is not None  # the run itself is untouched


def test_the_polled_api_is_scoped_like_the_strip_it_feeds(client, board):
    """Filtering the rendered strip and leaving the JSON open would put the
    title back on the page a few seconds later, which is worse than not having
    filtered at all - it would look fixed and not be."""
    db.create_run(board["his"], "build", "opus")
    as_her(client, board)
    body = json.dumps(client.get("/api/active-run").json())
    assert "His Thing" not in body and "his-thing" not in body


def test_a_oneoff_run_shows_on_the_owners_strip(client, board):
    """A one-off task run has no project_id, so a bare membership test drops
    it - which had the side rail saying "1 agent working" over a strip
    reading "no agent running" on the same page. The strip now follows the
    rail's rule: the owner sees his own task console's runs."""
    oneoff = db.create_oneoff("Sort out the garage inventory")
    db.create_run(None, "task", "opus", oneoff_id=int(oneoff["id"]))
    as_him(client, board)
    body = client.get("/api/active-run").json()
    assert body["active"] is True
    assert any(r.get("oneoff_id") for r in body["runs"])


def test_a_oneoff_run_is_not_shown_to_anybody_else(client, board):
    """The other half of the rule: a task title in the strip's chrome is still
    a leak, and a one-off belongs to the owner alone."""
    oneoff = db.create_oneoff("Sort out the garage inventory")
    db.create_run(None, "task", "opus", oneoff_id=int(oneoff["id"]))
    as_her(client, board)
    body = client.get("/api/active-run").json()
    assert body["active"] is False
    assert "garage" not in json.dumps(body)


def test_your_own_run_still_shows(client, board):
    """The filter has to leave the thing it is filtering for. A strip that
    showed nothing to anybody would pass every test above."""
    db.create_run(board["hers"], "build", "opus")
    as_her(client, board)
    assert client.get("/api/active-run").json()["active"] is True


def test_the_idle_line_does_not_name_the_project_it_is_pacing(client, board):
    """Found by running the tests, not by reading the code: the worker's status
    line says things like "about to start a run on His Thing" and "pacing the
    next run - His Thing starts in about four minutes". It sits in the same
    strip and had none of the strip's filtering.

    Scrubbing titles out of that sentence would be guesswork, so the line is
    simply not shown to anybody but the owner - it describes the install's
    scheduler queue, which a second person can neither see into nor act on."""
    snap = {"active": False, "runs": [], "idle_reason": "about to start a run on His Thing"}
    assert scope.only_runs(snap, set(), admin=False)["idle_reason"] == ""
    assert "His Thing" in scope.only_runs(snap, set(), admin=True)["idle_reason"]


def test_the_spend_breakdown_does_not_list_other_peoples_projects(client, board):
    """The other one running the tests turned up. /activity looks like a page
    about runs, and the by_project table under its chart is what spells out
    titles - so it is the last place you would look for a project listing."""
    from app import usage

    db.create_run(board["his"], "build", "opus")
    db.create_run(board["hers"], "build", "opus")
    titles = {
        g["title"] for g in
        usage.history(14, only_projects={board["hers"]})["by_project"]
    }
    assert "His Thing" not in titles


# --------------------------------------------------------------------------
# The owner's separate door
# --------------------------------------------------------------------------

def test_the_owner_can_see_everyones_board(client, board):
    as_him(client, board)
    html = client.get("/everyone").text
    assert "Her Thing" in html and "His Thing" in html


def test_other_people_have_no_such_page(client, board):
    as_her(client, board)
    assert client.get("/everyone").status_code == 404


def test_other_people_are_listed_before_you(client, board):
    """He goes there to look at somebody else's work; his own thirty-odd cards
    are already his whole front page and must not be what greets him."""
    as_him(client, board)
    groups = scope.by_person(people.owner())
    assert groups[0]["person"]["name"] == "Erin"
    assert groups[-1]["is_viewer"] is True


def test_the_everyone_tab_is_the_owners_alone(client, board):
    as_him(client, board)
    assert 'href="/everyone"' in client.get("/").text
    as_her(client, board)
    assert 'href="/everyone"' not in client.get("/").text


def test_a_one_person_install_is_offered_no_such_tab(client):
    """A page listing exactly the projects the dashboard already shows is a tab
    onto nothing. It appears when a second person does."""
    db.create_project("Only Thing", stage="active")
    assert 'href="/everyone"' not in client.get("/").text


def test_an_archived_persons_projects_are_still_listed(client, board):
    """Retiring somebody does not retire their work, and a group that quietly
    disappeared would read as projects going missing."""
    people.archive(board["her_id"])
    groups = scope.by_person(people.owner())
    assert any(g["person"] and g["person"]["name"] == "Erin" for g in groups)


# --------------------------------------------------------------------------
# What this is not
# --------------------------------------------------------------------------

def test_a_project_page_still_renders_for_anybody_who_asks(client, board):
    """Pinned deliberately, so nobody later mistakes this feature for security
    and builds on the assumption.

    The portal has no passwords: identity is a cookie you can change from the
    header dropdown. Filtering the feeds is what stops thirty cards that are
    not yours filling your board; it is not, and must not be described as,
    somebody being unable to read a project. If that is ever wanted it needs
    real authentication first, and this test is where the conversation starts.
    """
    as_her(client, board)
    assert client.get("/project/his-thing").status_code == 200


def test_a_one_person_install_sees_exactly_what_it_saw_before(client):
    """The whole feature has to be invisible until a second person exists."""
    a = int(db.create_project("Alpha", stage="active")["id"])
    b = int(db.create_project("Beta", stage="review")["id"])
    db.add_journal(a, "agent", "progress", "alpha moved")
    assert scope.visible_ids(people.owner()) == {a, b}
    html = client.get("/").text
    assert "Alpha" in html and "Beta" in html and "alpha moved" in html

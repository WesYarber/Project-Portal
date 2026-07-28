"""The per-project working checklist.

From Wes's ask: "Store a working todo list for each project with separate
sections for stuff the user needs to do and for stuff the agent needs to do.
When new requests are given to you, you need to add the relevant entries to the
todo list for yourself so that things don't get left behind in long context
windows."

The behaviors that make that true, and which these tests pin down:

- the list is split by owner, and both halves survive a run;
- the agent's half is written into every run prompt, so a request made ten runs
  ago is in front of the model that could act on it;
- a report can add to and tick off the list, and re-stating an item it already
  has does not accumulate duplicates;
- Wes can add, tick and untick items himself without an agent involved.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, agent_runner, db, todos


@pytest.fixture
def client(temp_data_dir):
    from app import main

    # No context manager: that would run the lifespan hook and start the worker.
    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project("Fridge Board", description="A thing.", stage="active", build_approved=True, slug="fridge")


# --- storage ---------------------------------------------------------------

def test_a_todo_belongs_to_one_owner(project):
    db.add_todo(project["id"], "Collapse the agent console", "agent")
    db.add_todo(project["id"], "Enable Tailscale HTTPS", "user")

    mine = db.list_todos(project["id"], owner="agent")
    theirs = db.list_todos(project["id"], owner="user")
    assert [r["text"] for r in mine] == ["Collapse the agent console"]
    assert [r["text"] for r in theirs] == ["Enable Tailscale HTTPS"]


def test_an_unknown_owner_falls_back_to_the_agent(project):
    row = db.add_todo(project["id"], "Something", "nobody")
    assert row["owner"] == "agent"


def test_blank_text_is_not_a_todo(project):
    assert db.add_todo(project["id"], "   ") is None
    assert db.list_todos(project["id"]) == []


def test_control_characters_are_scrubbed_from_the_text(project):
    row = db.add_todo(project["id"], "Fix the\r\nnote form")
    assert row["text"] == "Fix the note form"


def test_re_adding_the_same_item_does_not_duplicate_it(project):
    first = db.add_todo(project["id"], "Mute the paused-project border")
    again = db.add_todo(project["id"], "  mute the PAUSED-project border!  ")
    assert again["id"] == first["id"]
    assert len(db.list_todos(project["id"])) == 1


def test_re_adding_a_completed_item_does_not_resurrect_it(project):
    """An agent that re-states finished work in its report must not put it back
    on the list as an open item - that is how a list never converges."""
    row = db.add_todo(project["id"], "Ship the favicon")
    db.set_todo_done(row["id"], True)

    db.add_todo(project["id"], "Ship the favicon")
    rows = db.list_todos(project["id"])
    assert len(rows) == 1 and rows[0]["done"] == 1


def test_ticking_records_when_and_unticking_clears_it(project):
    row = db.add_todo(project["id"], "A thing")
    done = db.set_todo_done(row["id"], True)
    assert done["done"] == 1 and done["done_at"]

    reopened = db.set_todo_done(row["id"], False)
    assert reopened["done"] == 0 and reopened["done_at"] is None


def test_open_items_are_listed_before_completed_ones(project):
    first = db.add_todo(project["id"], "Older item")
    db.add_todo(project["id"], "Newer item")
    db.set_todo_done(first["id"], True)

    assert [r["text"] for r in db.list_todos(project["id"])] == ["Newer item", "Older item"]


def test_counts_only_count_open_items(project):
    a = db.add_todo(project["id"], "One")
    db.add_todo(project["id"], "Two", "user")
    db.set_todo_done(a["id"], True)

    assert db.count_open_todos(project["id"]) == 1
    assert db.count_open_todos(project["id"], owner="agent") == 0
    assert db.count_open_todos(project["id"], owner="user") == 1


def test_todos_are_per_project(project):
    other = db.create_project("Other", slug="other")
    db.add_todo(project["id"], "Mine")
    db.add_todo(other["id"], "Theirs")

    assert [r["text"] for r in db.list_todos(project["id"])] == ["Mine"]
    assert [r["text"] for r in db.list_todos(other["id"])] == ["Theirs"]


def test_deleting_a_project_takes_its_todos(project):
    db.add_todo(project["id"], "Doomed")
    db.delete_project(project["id"])
    assert db.list_todos(project["id"]) == []


# --- completing by reference ------------------------------------------------

def test_an_agent_can_tick_an_item_off_by_id(project):
    row = db.add_todo(project["id"], "Build the thing")
    assert db.complete_todo_by_ref(project["id"], row["id"])["done"] == 1


def test_an_agent_can_tick_an_item_off_by_its_text(project):
    """The id is precise, but the text is what the model actually has in mind,
    so both are accepted."""
    db.add_todo(project["id"], "Build the thing")
    assert db.complete_todo_by_ref(project["id"], "build the THING")["done"] == 1


def test_a_hash_prefixed_id_works_because_that_is_how_it_is_displayed(project):
    row = db.add_todo(project["id"], "Build the thing")
    assert db.complete_todo_by_ref(project["id"], f"#{row['id']}")["done"] == 1


def test_completing_an_unknown_reference_changes_nothing(project):
    db.add_todo(project["id"], "Build the thing")
    assert db.complete_todo_by_ref(project["id"], "something else entirely") is None
    assert db.count_open_todos(project["id"]) == 1


def test_one_project_cannot_tick_off_another_projects_todo(project):
    other = db.create_project("Other", slug="other")
    row = db.add_todo(other["id"], "Not yours")
    assert db.complete_todo_by_ref(project["id"], row["id"]) is None
    assert db.get_todo(row["id"])["done"] == 0


# --- the prompt section -----------------------------------------------------

def test_an_empty_list_adds_no_heading_to_the_prompt(project):
    assert todos.prompt_section(project["id"]) == ""


def test_the_prompt_shows_both_halves_with_ids_and_boxes(project):
    a = db.add_todo(project["id"], "Collapse the console")
    db.add_todo(project["id"], "Enable Tailscale HTTPS", "user")
    db.set_todo_done(a["id"], True)

    text = todos.prompt_section(project["id"])
    assert "### Yours (the agent's)" in text
    assert f"### {config.SITE.owners} (only {config.SITE.they} can do these)" in text
    assert f"- [x] #{a['id']} Collapse the console" in text
    assert "- [ ] #" in text and "Enable Tailscale HTTPS" in text


def test_an_empty_half_says_so_rather_than_vanishing(project):
    """Both headings always show once the list exists: an agent that sees only
    its own half can't tell whether Wes is blocked on something."""
    db.add_todo(project["id"], "Only an agent item")
    text = todos.prompt_section(project["id"])
    assert f"### {config.SITE.owners} (only {config.SITE.they} can do these)" in text
    assert "(nothing outstanding)" in text


def test_the_completed_tail_is_trimmed_but_open_items_never_are(project):
    for i in range(todos.MAX_DONE_SHOWN + 10):
        row = db.add_todo(project["id"], f"Finished item {i}")
        db.set_todo_done(row["id"], True)
    for i in range(30):
        db.add_todo(project["id"], f"Open item {i}")

    text = todos.prompt_section(project["id"])
    assert text.count("- [x]") == todos.MAX_DONE_SHOWN
    assert text.count("- [ ]") == 30


def test_the_list_reaches_the_run_prompt(project):
    db.add_todo(project["id"], "Survive the context window")
    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "## Todo list for this project" in prompt
    assert "Survive the context window" in prompt


def test_a_project_with_no_todos_gets_no_todo_section_in_its_prompt(project):
    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "## Todo list for this project" not in prompt


def test_the_contract_tells_the_agent_to_maintain_the_list():
    assert "todo_updates" in agent_runner.AGENT_CONTRACT


def test_the_contract_tells_the_agent_questions_must_stand_alone():
    """Wes: 'assume I have likely not read the rest of the response before
    them and am just coming in to see the question asked.'"""
    assert "stands alone" in agent_runner.AGENT_CONTRACT


# --- applying a report ------------------------------------------------------

def test_a_report_can_add_and_complete_in_one_go(project):
    existing = db.add_todo(project["id"], "Old work")
    counts = todos.apply_updates(
        project["id"],
        {"add": [{"text": "New request", "owner": "user"}], "done": [existing["id"]]},
    )
    assert counts == {"added": 1, "done": 1, "tagged": 0}
    assert db.get_todo(existing["id"])["done"] == 1
    assert db.list_todos(project["id"], owner="user")[0]["text"] == "New request"


def test_a_bare_string_in_add_is_the_agents_own_todo(project):
    todos.apply_updates(project["id"], {"add": ["Just a string"]})
    assert db.list_todos(project["id"], owner="agent")[0]["text"] == "Just a string"


def test_re_adding_an_item_the_agent_already_has_is_not_counted_as_new(project):
    db.add_todo(project["id"], "Already listed")
    counts = todos.apply_updates(project["id"], {"add": ["Already listed"]})
    assert counts["added"] == 0
    assert len(db.list_todos(project["id"])) == 1


def test_a_missing_or_malformed_block_is_ignored(project):
    """One field of a JSON blob written by a language model must not be able to
    take down the rest of the report handling."""
    for junk in (None, "nonsense", [], {"add": None, "done": None}, {"add": [7, None]}):
        assert todos.apply_updates(project["id"], junk) == {"added": 0, "done": 0, "tagged": 0}


def test_completing_something_that_is_not_on_the_list_is_survivable(project):
    assert todos.apply_updates(project["id"], {"done": [999, "ghost"]}) == {"added": 0, "done": 0, "tagged": 0}


def test_the_worker_applies_todo_updates_from_a_run(project):
    from app import agent_runner as ar
    from app import worker

    row = db.add_todo(project["id"], "Finish the gate")
    result = ar.RunResult(
        ok=True,
        report={
            "journal_entry_md": "did a thing",
            "todo_updates": {"add": ["Next chunk"], "done": [row["id"]]},
        },
    )
    worker._apply_report(db.get_project(project["id"]), result)

    assert db.get_todo(row["id"])["done"] == 1
    assert any(r["text"] == "Next chunk" for r in db.list_todos(project["id"]))


# --- the web UI -------------------------------------------------------------

def test_the_project_page_shows_both_halves(client, project):
    db.add_todo(project["id"], "Agent item here")
    db.add_todo(project["id"], "User item here", "user")

    body = client.get(f"/project/{project['slug']}").text
    assert "For the agent" in body and "Agent item here" in body
    assert "For you" in body and "User item here" in body


def test_wes_can_add_a_todo_for_himself(client, project):
    r = client.post(
        f"/project/{project['slug']}/todo",
        data={"text": "Buy the e-ink panel", "owner": "user"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.list_todos(project["id"], owner="user")[0]["text"] == "Buy the e-ink panel"


def test_adding_lands_you_back_at_the_list_not_the_top_of_the_page(client, project):
    """Wes: submitting shouldn't reset the page to the top - it's jarring when
    you're working through a batch.

    No `#todos` anchor: an anchor in the URL beats the saved-scroll restore in
    app.js, which is the thing that actually puts him back where he was."""
    r = client.post(
        f"/project/{project['slug']}/todo", data={"text": "A thing"}, follow_redirects=False
    )
    assert r.headers["location"] == f"/project/{project['slug']}"


def test_toggling_posts_the_state_to_move_to_so_a_double_submit_is_stable(client, project):
    row = db.add_todo(project["id"], "A thing")
    for _ in range(2):
        client.post(f"/todo/{row['id']}/toggle", data={"done": "1"}, follow_redirects=False)
    assert db.get_todo(row["id"])["done"] == 1

    client.post(f"/todo/{row['id']}/toggle", data={"done": "0"}, follow_redirects=False)
    assert db.get_todo(row["id"])["done"] == 0


def test_deleting_removes_it(client, project):
    row = db.add_todo(project["id"], "A thing")
    r = client.post(f"/todo/{row['id']}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert db.get_todo(row["id"]) is None


def test_acting_on_a_todo_that_does_not_exist_is_a_404(client):
    assert client.post("/todo/9999/toggle", data={"done": "1"}).status_code == 404
    assert client.post("/todo/9999/delete").status_code == 404

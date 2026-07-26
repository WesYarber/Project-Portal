"""Tags on todo items.

From Wes's 23:45 note: "individually set tags for todo items on the list like
blocked, ready to build, or anything else that could be useful for this while
continuing to build where we are unblocked approach".

What these pin down:

- tags are short kebab labels, normalised so the same tag typed two ways is
  one tag, capped in count and length so a chip stays a chip;
- 'blocked' is the one tag with teeth: an open agent item wearing it is not
  workable, and a project where nothing is workable is not scheduled;
- agents can tag through their report (add-with-tags and a retag block, in
  both shapes a model actually writes), and Wes can tag from the row itself;
- the chips reach both readers: the run prompt and the project page.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import agent_runner, db, todos, worker


@pytest.fixture
def client(temp_data_dir):
    from app import main

    # No context manager: that would run the lifespan hook and start the worker.
    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project(
        "Fridge Board", description="A thing.", stage="active", build_approved=True, slug="fridge"
    )


# --- normalisation ----------------------------------------------------------

def test_a_tag_is_normalised_to_kebab():
    assert db.normalize_todo_tag("Ready to Build ") == "ready-to-build"
    assert db.normalize_todo_tag("blocked") == "blocked"
    assert db.normalize_todo_tag("[weird,punct!]") == "weird-punct"


def test_junk_tags_normalise_to_nothing():
    assert db.normalize_todo_tag("") == ""
    assert db.normalize_todo_tag("!!!") == ""
    assert db.normalize_todo_tag(None) == ""
    assert db.normalize_todo_tag(["blocked"]) == ""


def test_a_tag_is_capped_in_length():
    assert len(db.normalize_todo_tag("x" * 100)) == db.TODO_TAG_MAXLEN


# --- storage ----------------------------------------------------------------

def test_set_and_read_tags(project):
    row = db.add_todo(project["id"], "Wire the sensor", "agent")
    db.set_todo_tags(row["id"], ["blocked", "Ready to Build"])
    assert db.todo_tags(db.get_todo(row["id"])) == ["blocked", "ready-to-build"]


def test_setting_an_empty_list_untags(project):
    row = db.add_todo(project["id"], "Wire the sensor", "agent", tags=["blocked"])
    db.set_todo_tags(row["id"], [])
    assert db.todo_tags(db.get_todo(row["id"])) == []


def test_add_and_remove_one_tag(project):
    row = db.add_todo(project["id"], "Wire the sensor", "agent")
    db.add_todo_tag(row["id"], "blocked")
    db.add_todo_tag(row["id"], "blocked")  # idempotent, not a duplicate chip
    assert db.todo_tags(db.get_todo(row["id"])) == ["blocked"]
    db.remove_todo_tag(row["id"], "Blocked")  # normalises before matching
    assert db.todo_tags(db.get_todo(row["id"])) == []


def test_tags_are_capped_in_count(project):
    row = db.add_todo(project["id"], "Wire the sensor", "agent")
    db.set_todo_tags(row["id"], [f"tag-{i}" for i in range(20)])
    assert len(db.todo_tags(db.get_todo(row["id"]))) == db.MAX_TODO_TAGS


def test_tagging_an_unknown_todo_is_a_none(project):
    assert db.set_todo_tags(9999, ["blocked"]) is None
    assert db.add_todo_tag(9999, "blocked") is None
    assert db.remove_todo_tag(9999, "blocked") is None


def test_readding_a_todo_merges_new_tags_into_the_existing_row(project):
    first = db.add_todo(project["id"], "Wire the sensor", "agent", tags=["ready"])
    again = db.add_todo(project["id"], "Wire the sensor!", "agent", tags=["blocked"])
    assert again["id"] == first["id"]
    assert db.todo_tags(again) == ["ready", "blocked"]
    assert db.count_open_todos(project["id"]) == 1


# --- what 'blocked' means to the scheduler ----------------------------------

def test_blocked_items_do_not_count_as_workable(project):
    db.add_todo(project["id"], "waiting on the part", "agent", tags=["blocked"])
    db.add_todo(project["id"], "doable now", "agent")
    db.add_todo(project["id"], "his half", "user")
    assert db.count_workable_todos(project["id"]) == 1


def test_a_done_item_is_not_workable_either(project):
    row = db.add_todo(project["id"], "shipped already", "agent")
    db.set_todo_done(row["id"], True)
    assert db.count_workable_todos(project["id"]) == 0


def test_a_list_that_is_all_blocked_parks_the_rotation(project):
    """No question, no blocked_on report - the tags alone say every remaining
    item waits on Wes, so a run could only repeat itself."""
    db.add_todo(project["id"], "needs the credential", "agent", tags=["blocked"])
    db.add_todo(project["id"], "needs the purchase", "agent", tags=["blocked"])
    assert worker._pick_project(None) == (None, False)


def test_one_workable_item_keeps_a_blocked_project_scheduling(project):
    db.update_project(project["id"], blocked_on="a part on order")
    db.add_todo(project["id"], "waiting on the part", "agent", tags=["blocked"])
    db.add_todo(project["id"], "the half that does not need it", "agent")
    picked, _ = worker._pick_project(None)
    assert picked is not None and picked["id"] == project["id"]


def test_a_question_with_only_blocked_todos_parks_the_rotation(project):
    db.create_question(project["id"], "Which colour?")
    db.add_todo(project["id"], "depends on the colour", "agent", tags=["blocked"])
    assert worker._pick_project(None) == (None, False)


def test_an_empty_list_with_nothing_waiting_still_schedules(project):
    """Unchanged behaviour, pinned: on many projects the agent picks its own
    next chunk, so no todos does not mean no work."""
    picked, _ = worker._pick_project(None)
    assert picked is not None and picked["id"] == project["id"]


def test_untagging_unparks_the_project(project):
    row = db.add_todo(project["id"], "was waiting", "agent", tags=["blocked"])
    assert worker._pick_project(None) == (None, False)
    db.set_todo_tags(row["id"], [])
    picked, _ = worker._pick_project(None)
    assert picked is not None


# --- the report contract ----------------------------------------------------

def test_a_report_can_add_a_tagged_item(project):
    todos.apply_updates(project["id"], {"add": [{"text": "Order the part", "owner": "user", "tags": ["blocked"]}]})
    rows = db.list_todos(project["id"])
    assert db.todo_tags(rows[0]) == ["blocked"]


def test_a_report_can_retag_by_mapping(project):
    row = db.add_todo(project["id"], "Wire the sensor", "agent")
    counts = todos.apply_updates(project["id"], {"tags": {str(row["id"]): ["blocked"]}})
    assert counts["tagged"] == 1
    assert db.todo_tags(db.get_todo(row["id"])) == ["blocked"]


def test_a_report_can_retag_by_list_and_hash_ref(project):
    row = db.add_todo(project["id"], "Wire the sensor", "agent", tags=["blocked"])
    counts = todos.apply_updates(
        project["id"], {"tags": [{"id": f"#{row['id']}", "tags": []}]}
    )
    assert counts["tagged"] == 1
    assert db.todo_tags(db.get_todo(row["id"])) == []


def test_a_retag_cannot_reach_across_projects(project):
    other = db.create_project("Other", slug="other", stage="active")
    theirs = db.add_todo(other["id"], "not yours", "agent")
    counts = todos.apply_updates(project["id"], {"tags": {str(theirs["id"]): ["blocked"]}})
    assert counts["tagged"] == 0
    assert db.todo_tags(db.get_todo(theirs["id"])) == []


def test_malformed_tag_blocks_are_shrugged_off(project):
    row = db.add_todo(project["id"], "Wire the sensor", "agent")
    counts = todos.apply_updates(
        project["id"],
        {"tags": "nonsense", "add": "also fine as a string"},
    )
    assert counts["tagged"] == 0
    counts = todos.apply_updates(project["id"], {"tags": {"not-a-number": ["x"], None: ["y"]}})
    assert counts["tagged"] == 0
    assert db.todo_tags(db.get_todo(row["id"])) == []


def test_the_prompt_shows_the_chips(project):
    db.add_todo(project["id"], "Wire the sensor", "agent", tags=["blocked", "ready"])
    section = todos.prompt_section(project["id"])
    assert "[blocked] [ready] Wire the sensor" in section
    # And tells the agent how to retag, and what 'blocked' does.
    assert '"tags"' in section
    assert "blocked" in section


def test_the_contract_carries_the_tag_vocabulary():
    assert '"tags"' in agent_runner.AGENT_CONTRACT
    assert "blocked" in agent_runner.AGENT_CONTRACT


def test_the_contract_speaks_the_new_state_vocabulary():
    """Todo #165: the contract text now asks for the new fields the worker
    already accepts. The old vocabulary stays accepted forever in the worker -
    that half is pinned in test_state_model."""
    contract = agent_runner.AGENT_CONTRACT
    assert '"new_stage"' in contract
    assert '"request_build"' in contract
    assert '"blocked_on"' in contract
    assert "new_status" not in contract


# --- the web half -----------------------------------------------------------

def test_wes_can_tag_from_the_row(client, project):
    row = db.add_todo(project["id"], "Wire the sensor", "agent")
    resp = client.post(f"/todo/{row['id']}/tag", data={"add": "Ready to Build"}, follow_redirects=False)
    assert resp.status_code == 303
    assert db.todo_tags(db.get_todo(row["id"])) == ["ready-to-build"]


def test_wes_can_untag_from_the_chip(client, project):
    row = db.add_todo(project["id"], "Wire the sensor", "agent", tags=["blocked"])
    client.post(f"/todo/{row['id']}/tag", data={"remove": "blocked"}, follow_redirects=False)
    assert db.todo_tags(db.get_todo(row["id"])) == []


def test_tagging_an_unknown_todo_is_a_404(client):
    resp = client.post("/todo/9999/tag", data={"add": "blocked"}, follow_redirects=False)
    assert resp.status_code == 404


def test_the_page_wears_the_chips(client, project):
    db.add_todo(project["id"], "Wire the sensor", "agent", tags=["blocked", "ready-to-build"])
    html = client.get("/project/fridge").text
    assert "todo-tag tag-blocked" in html
    assert "todo-tag tag-ready" in html
    assert 'data-act="tag-add"' in html


def test_a_done_item_keeps_its_chips_but_loses_the_add_button(client, project):
    row = db.add_todo(project["id"], "Wire the sensor", "agent", tags=["blocked"])
    db.set_todo_done(row["id"], True)
    html = client.get("/project/fridge").text
    assert "todo-tag tag-blocked" in html
    assert 'data-act="tag-add"' not in html

"""Completed todos leaving the live list, and the history that catches them.

From Wes's notes:

  "Hide completed todo tasks after they have been completed for 16 hours or
   until they have been viewed by the user. Allow the user to go back and
   review past todo-entries that have been completed."
  "Add a button to clear all completed TODO items from view (they still should
   be able to be viewed in the history)."

The behaviors that make that true, and which these tests pin down:

- a freshly ticked item stays on the list (you have to be able to see what you
  just did), and drops off on its own once it is 16 hours old;
- "clear completed" takes them all off at once, immediately;
- clearing hides, it never deletes - the history page has all of it, forever;
- unticking something puts it back on the live list even if it had been
  cleared away.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import db, todos


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project("Fridge Board", description="A thing.", stage="active", build_approved=True, slug="fridge")


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _tick(todo_id: int, hours_ago: float) -> None:
    """Complete an item and back-date the completion."""
    db.set_todo_done(todo_id, True)
    conn = db.get_conn()
    with db._LOCK:  # noqa: SLF001
        conn.execute("UPDATE todos SET done_at = ? WHERE id = ?", (_ago(hours_ago), todo_id))
        conn.commit()


# --- ageing off the live list ----------------------------------------------

def test_an_open_item_is_always_visible(project):
    db.add_todo(project["id"], "still to do")
    assert [t["text"] for t in db.visible_todos(project["id"])] == ["still to do"]


def test_a_freshly_completed_item_is_still_visible(project):
    todo = db.add_todo(project["id"], "just did this")
    db.set_todo_done(todo["id"], True)
    assert [t["text"] for t in db.visible_todos(project["id"])] == ["just did this"]


def test_a_completed_item_drops_off_after_sixteen_hours(project):
    todo = db.add_todo(project["id"], "yesterday's work")
    _tick(todo["id"], db.TODO_DONE_TTL_HOURS + 1)
    assert db.visible_todos(project["id"]) == []


def test_an_item_completed_just_inside_the_window_is_kept(project):
    todo = db.add_todo(project["id"], "overnight")
    _tick(todo["id"], db.TODO_DONE_TTL_HOURS - 1)
    assert len(db.visible_todos(project["id"])) == 1


def test_ageing_off_never_touches_open_items(project):
    todo = db.add_todo(project["id"], "old but open")
    conn = db.get_conn()
    with db._LOCK:  # noqa: SLF001
        conn.execute("UPDATE todos SET created_at = ? WHERE id = ?", (_ago(500), todo["id"]))
        conn.commit()
    assert len(db.visible_todos(project["id"])) == 1


# --- clearing ---------------------------------------------------------------

def test_clearing_hides_every_completed_item_at_once(project):
    a = db.add_todo(project["id"], "one")
    b = db.add_todo(project["id"], "two")
    open_item = db.add_todo(project["id"], "three")
    db.set_todo_done(a["id"], True)
    db.set_todo_done(b["id"], True)

    assert db.clear_completed_todos(project["id"]) == 2
    assert [t["id"] for t in db.visible_todos(project["id"])] == [open_item["id"]]


def test_clearing_deletes_nothing(project):
    todo = db.add_todo(project["id"], "kept forever")
    db.set_todo_done(todo["id"], True)
    db.clear_completed_todos(project["id"])

    assert db.get_todo(todo["id"]) is not None
    assert [t["text"] for t in db.completed_todos(project["id"])] == ["kept forever"]


def test_clearing_twice_is_a_no_op(project):
    todo = db.add_todo(project["id"], "one")
    db.set_todo_done(todo["id"], True)
    db.clear_completed_todos(project["id"])
    assert db.clear_completed_todos(project["id"]) == 0


def test_clearing_leaves_other_projects_alone(project):
    other = db.create_project("Other", slug="other")
    mine = db.add_todo(project["id"], "mine")
    theirs = db.add_todo(other["id"], "theirs")
    db.set_todo_done(mine["id"], True)
    db.set_todo_done(theirs["id"], True)

    db.clear_completed_todos(project["id"])
    assert len(db.visible_todos(other["id"])) == 1


def test_unticking_a_cleared_item_puts_it_back(project):
    """Otherwise pulling something back onto the list would be invisible."""
    todo = db.add_todo(project["id"], "not done after all")
    db.set_todo_done(todo["id"], True)
    db.clear_completed_todos(project["id"])
    db.set_todo_done(todo["id"], False)

    assert [t["text"] for t in db.visible_todos(project["id"])] == ["not done after all"]
    assert db.get_todo(todo["id"])["cleared_at"] is None


# --- history ----------------------------------------------------------------

def test_history_is_newest_first_and_holds_everything(project):
    old = db.add_todo(project["id"], "older")
    new = db.add_todo(project["id"], "newer")
    _tick(old["id"], 40)
    _tick(new["id"], 2)

    assert [t["text"] for t in db.completed_todos(project["id"])] == ["newer", "older"]


def test_history_excludes_open_items(project):
    db.add_todo(project["id"], "open")
    done = db.add_todo(project["id"], "closed")
    db.set_todo_done(done["id"], True)
    assert [t["text"] for t in db.completed_todos(project["id"])] == ["closed"]


def test_hidden_and_clearable_counts_split_the_completed_items(project):
    aged = db.add_todo(project["id"], "aged off")
    fresh = db.add_todo(project["id"], "fresh")
    _tick(aged["id"], db.TODO_DONE_TTL_HOURS + 2)
    db.set_todo_done(fresh["id"], True)

    assert db.count_hidden_done_todos(project["id"]) == 1
    assert db.count_clearable_todos(project["id"]) == 1


# --- the agent's prompt -----------------------------------------------------

def test_a_cleared_item_is_still_in_the_agents_prompt(project):
    """Hiding it is a UI decision. The agent must not re-add work it did."""
    todo = db.add_todo(project["id"], "already shipped")
    db.set_todo_done(todo["id"], True)
    db.clear_completed_todos(project["id"])

    assert "already shipped" in todos.prompt_section(project["id"])


# --- the web UI -------------------------------------------------------------

def test_the_clear_button_appears_only_when_there_is_something_to_clear(client, project):
    html = client.get("/project/fridge").text
    assert "clear 1 completed" not in html

    todo = db.add_todo(project["id"], "done thing")
    db.set_todo_done(todo["id"], True)
    assert "clear 1 completed" in client.get("/project/fridge").text


def test_posting_clear_completed_hides_them(client, project):
    todo = db.add_todo(project["id"], "done thing")
    db.set_todo_done(todo["id"], True)

    resp = client.post("/project/fridge/todos/clear-completed", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/project/fridge"
    assert "done thing" not in client.get("/project/fridge").text


def test_the_history_page_lists_completed_items(client, project):
    todo = db.add_todo(project["id"], "shipped the thing")
    db.set_todo_done(todo["id"], True)
    db.clear_completed_todos(project["id"])

    html = client.get("/project/fridge/todos/history").text
    assert "shipped the thing" in html


def test_the_history_page_404s_on_an_unknown_project(client):
    assert client.get("/project/nope/todos/history").status_code == 404


def test_the_project_page_always_links_to_the_history(client, project):
    assert "/project/fridge/todos/history" in client.get("/project/fridge").text


def test_the_delete_cross_is_not_hover_only():
    """A phone has no hover: hover-only meant mobile could never delete one,
    and a tap left the cross stuck lit afterwards."""
    from pathlib import Path

    css = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text()
    block = css[css.index(".todo-del {"):css.index(".todo-foot {")]
    assert "opacity: 0;" not in block  # never fully hidden
    assert ":focus-visible" in block   # a click does not leave it lit

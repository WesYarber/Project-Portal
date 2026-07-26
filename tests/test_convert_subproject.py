"""Converting an existing project into a sub-project of another, and back.

Wes's ask (23:45 note, 2026-07-22): "Be able to convert a project to a
subproject of another project without losing stuff."

The design makes "without losing stuff" nearly free: journal, todos, questions,
runs and the attachment index all hang off the project id, which a conversion
never touches. What actually changes is the parent pointer and the workspace
folder name (renamed to the family scheme), so the tests concentrate on those
two moves, the guards around them, and the reverse move (release), which is
also how the delete guard's "re-home them first" becomes possible.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db, subprojects


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _project(title, slug, **fields):
    row = db.create_project(title, slug=slug)
    if fields:
        db.update_project(row["id"], **fields)
        row = db.get_project(row["id"])
    return row


def _workspace(slug: str, marker: str = "hello") -> None:
    ws = config.PROJECTS_DIR / slug
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "keep.txt").write_text(marker)


# --------------------------------------------------------------------------
# The conversion itself
# --------------------------------------------------------------------------

def test_adopt_reparents_and_renames_to_the_family_scheme(temp_data_dir):
    parent = _project("Board games", "board-games")
    tak = _project("Tak", "tak")
    _workspace("tak", "the board")

    updated = subprojects.adopt(parent, tak)

    assert updated["parent_id"] == parent["id"]
    assert updated["slug"] == "board-games-tak"
    # The workspace moved in the same step, contents intact.
    assert not (config.PROJECTS_DIR / "tak").exists()
    assert (config.PROJECTS_DIR / "board-games-tak" / "keep.txt").read_text() == "the board"


def test_nothing_is_lost_in_a_conversion(temp_data_dir):
    """The whole point of the ask. Everything keyed by project id survives."""
    parent = _project("Board games", "board-games")
    tak = _project("Tak", "tak")
    todo = db.add_todo(tak["id"], "carve the capstones")
    question = db.create_question(tak["id"], "wood or plastic?")
    run_id = db.create_run(tak["id"], "build", "sonnet")
    # Finished, not running - a live run rightly blocks the conversion.
    db.finish_run(run_id, "done")
    journal_id = db.add_journal(tak["id"], "agent", "progress", "made a board")
    db.add_attachment(
        tak["id"], "rules.pdf", "0001-rules.pdf", "application/pdf", 123,
        journal_id=journal_id,
    )

    updated = subprojects.adopt(parent, tak)

    pid = updated["id"]
    assert pid == tak["id"]  # same row, new parent
    assert [t["id"] for t in db.visible_todos(pid)] == [todo["id"]]
    assert [q["id"] for q in db.open_questions(pid)] == [question["id"]]
    assert [r["id"] for r in db.list_runs(pid)] == [run_id]
    assert any(a["stored_name"] == "0001-rules.pdf" for a in db.list_attachments(pid))
    entries = " ".join(row["content_md"] for row in db.list_journal(pid))
    assert "made a board" in entries


def test_both_sides_get_a_journal_entry(temp_data_dir):
    parent = _project("Board games", "board-games")
    tak = _project("Tak", "tak")
    subprojects.adopt(parent, tak)

    child_journal = " ".join(r["content_md"] for r in db.list_journal(tak["id"]))
    parent_journal = " ".join(r["content_md"] for r in db.list_journal(parent["id"]))
    assert "sub-project of" in child_journal
    assert "board-games-tak" in child_journal  # the rename is recorded
    assert "board-games-tak" in parent_journal


def test_a_project_that_never_ran_has_no_folder_and_that_is_fine(temp_data_dir):
    parent = _project("Board games", "board-games")
    idea = _project("Azul", "azul")
    updated = subprojects.adopt(parent, idea)
    assert updated["slug"] == "board-games-azul"
    assert not (config.PROJECTS_DIR / "board-games-azul").exists()


def test_an_already_prefixed_slug_is_kept_not_uniquified(temp_data_dir):
    """A released child re-adopted by the same family keeps its folder - the
    uniquifier must not see its own name as taken and append -2."""
    parent = _project("Board games", "board-games")
    tak = _project("Tak", "board-games-tak")
    _workspace("board-games-tak")
    updated = subprojects.adopt(parent, tak)
    assert updated["slug"] == "board-games-tak"
    assert (config.PROJECTS_DIR / "board-games-tak").is_dir()


def test_a_slug_collision_is_uniquified(temp_data_dir):
    parent = _project("Board games", "board-games")
    _project("Tak the first", "board-games-tak")
    tak = _project("Tak", "tak")
    updated = subprojects.adopt(parent, tak)
    assert updated["slug"] == "board-games-tak-2"


def test_moving_between_families_swaps_the_prefix(temp_data_dir):
    """a-foo moved from family a to family b becomes b-foo, not b-a-foo."""
    a = _project("Alpha", "alpha")
    b = _project("Beta", "beta")
    foo = _project("Foo", "foo")
    _workspace("foo")
    subprojects.adopt(a, db.get_project(foo["id"]))
    assert db.get_project(foo["id"])["slug"] == "alpha-foo"

    updated = subprojects.adopt(b, db.get_project(foo["id"]))
    assert updated["parent_id"] == b["id"]
    assert updated["slug"] == "beta-foo"
    assert (config.PROJECTS_DIR / "beta-foo" / "keep.txt").exists()


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------

def test_cannot_adopt_itself(temp_data_dir):
    p = _project("Solo", "solo")
    with pytest.raises(subprojects.SplitError, match="own sub-project"):
        subprojects.adopt(p, p)


def test_the_meta_project_stays_top_level(temp_data_dir):
    parent = _project("Board games", "board-games")
    meta = _project("Project Portal", config.META_PROJECT_SLUG)
    with pytest.raises(subprojects.SplitError, match="stays top-level"):
        subprojects.adopt(parent, meta)


def test_cannot_adopt_into_a_sub_project(temp_data_dir):
    """One level deep, always: the target parent must itself be top-level."""
    grandparent = _project("Board games", "board-games")
    child = _project("Tak", "tak")
    subprojects.adopt(grandparent, child)
    other = _project("Chess clock", "chess-clock")
    with pytest.raises(subprojects.SplitError, match="one level deep"):
        subprojects.adopt(db.get_project(child["id"]), other)


def test_a_project_with_children_cannot_become_one(temp_data_dir):
    parent = _project("Board games", "board-games")
    subprojects.create_child(parent, "Tak")
    other = _project("Umbrella", "umbrella")
    with pytest.raises(subprojects.SplitError, match="Re-home or delete"):
        subprojects.adopt(other, db.get_project(parent["id"]))


def test_adopting_twice_is_refused(temp_data_dir):
    parent = _project("Board games", "board-games")
    tak = _project("Tak", "tak")
    subprojects.adopt(parent, tak)
    with pytest.raises(subprojects.SplitError, match="already a sub-project"):
        subprojects.adopt(parent, db.get_project(tak["id"]))


def test_refused_under_a_running_agent(temp_data_dir, monkeypatch):
    """The rename would pull the workspace out from under the agent's cwd."""
    parent = _project("Board games", "board-games")
    tak = _project("Tak", "tak")
    monkeypatch.setattr(db, "running_project_ids", lambda: {tak["id"]})
    with pytest.raises(subprojects.SplitError, match="Stop the run"):
        subprojects.adopt(parent, tak)


def test_move_workspace_refuses_path_traversal(temp_data_dir):
    with pytest.raises(subprojects.SplitError, match="Invalid workspace name"):
        subprojects.move_workspace("../outside", "target")
    with pytest.raises(subprojects.SplitError, match="Invalid workspace name"):
        subprojects.move_workspace("fine", "../../etc")


def test_move_workspace_refuses_an_occupied_destination(temp_data_dir):
    _workspace("a")
    _workspace("b")
    with pytest.raises(subprojects.SplitError, match="already exists on disk"):
        subprojects.move_workspace("a", "b")


# --------------------------------------------------------------------------
# Release (the reverse move)
# --------------------------------------------------------------------------

def test_release_promotes_and_keeps_the_folder_name(temp_data_dir):
    parent = _project("Board games", "board-games")
    tak = _project("Tak", "tak")
    _workspace("tak")
    subprojects.adopt(parent, tak)

    updated = subprojects.release(db.get_project(tak["id"]))
    assert updated["parent_id"] is None
    # No rename on the way out: a running agent cannot be disturbed.
    assert updated["slug"] == "board-games-tak"
    assert (config.PROJECTS_DIR / "board-games-tak").is_dir()
    child_journal = " ".join(r["content_md"] for r in db.list_journal(tak["id"]))
    assert "top-level" in child_journal


def test_release_of_a_top_level_project_is_refused(temp_data_dir):
    p = _project("Solo", "solo")
    with pytest.raises(subprojects.SplitError, match="already a top-level"):
        subprojects.release(p)


def test_release_unblocks_deleting_the_old_parent(temp_data_dir):
    """The delete guard says "re-home them first"; release is how."""
    parent = _project("Board games", "board-games")
    tak = _project("Tak", "tak")
    subprojects.adopt(parent, tak)
    assert subprojects.blocks_delete(db.get_project(parent["id"]))
    subprojects.release(db.get_project(tak["id"]))
    assert subprojects.blocks_delete(db.get_project(parent["id"])) is None


# --------------------------------------------------------------------------
# The eligible-parent list and the tidy-name proposal
# --------------------------------------------------------------------------

def test_adoptive_parents_excludes_self_children_and_current_parent(temp_data_dir):
    a = _project("Alpha", "alpha")
    b = _project("Beta", "beta")
    c = _project("Gamma", "gamma")
    subprojects.adopt(a, c)
    c = db.get_project(c["id"])
    titles = [p["title"] for p in subprojects.adoptive_parents(c)]
    # Not itself, not its current parent, and no sub-projects in the list.
    assert titles == ["Beta"]
    assert [p["title"] for p in subprojects.adoptive_parents(b)] == ["Alpha"]


def test_suggested_slug_keeps_the_family_prefix_on_a_child(temp_data_dir):
    """A tidy-name proposal on a converted child must not undo the scheme."""
    parent = _project("Board games", "board-games")
    child = _project(
        "Tak", "make-a-tak-board-game-with-online-play-and-maybe-an-ai-opponent-too"
    )
    subprojects.adopt(parent, child)
    child = db.get_project(child["id"])
    assert db.slug_is_untidy(child["slug"])  # still raw idea text, prefixed
    assert db.suggested_slug(child) == "board-games-tak"


# --------------------------------------------------------------------------
# Routes and the page
# --------------------------------------------------------------------------

def test_convert_route_end_to_end(client):
    parent = _project("Board games", "board-games")
    tak = _project("Tak", "tak")
    _workspace("tak")
    resp = client.post(
        f"/project/tak/make-subproject",
        data={"parent_id": str(parent["id"])},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/project/board-games-tak"
    assert db.get_project(tak["id"])["parent_id"] == parent["id"]


def test_convert_route_surfaces_the_guard_message(client):
    parent = _project("Board games", "board-games")
    subprojects.create_child(parent, "Tak")
    other = _project("Umbrella", "umbrella")
    resp = client.post(
        "/project/board-games/make-subproject",
        data={"parent_id": str(other["id"])},
    )
    assert resp.status_code == 400
    assert "Re-home or delete" in resp.text


def test_convert_route_404s_on_a_missing_parent(client):
    _project("Tak", "tak")
    resp = client.post("/project/tak/make-subproject", data={"parent_id": "9999"})
    assert resp.status_code == 404


def test_release_route(client):
    parent = _project("Board games", "board-games")
    tak = _project("Tak", "tak")
    subprojects.adopt(parent, tak)
    resp = client.post(
        "/project/board-games-tak/release", follow_redirects=False
    )
    assert resp.status_code == 303
    assert db.get_project(tak["id"])["parent_id"] is None


def test_release_route_refuses_a_top_level_project(client):
    _project("Solo", "solo")
    assert client.post("/project/solo/release").status_code == 400


def test_page_offers_convert_on_an_eligible_top_level_project(client):
    _project("Board games", "board-games")
    _project("Tak", "tak")
    html = client.get("/project/tak").text
    assert "/project/tak/make-subproject" in html
    assert "sub-project of" in html


def test_page_hides_convert_when_the_project_has_children(client):
    parent = _project("Board games", "board-games")
    subprojects.create_child(parent, "Tak")
    html = client.get("/project/board-games").text
    assert "/project/board-games/make-subproject" not in html


def test_page_hides_convert_when_there_is_nowhere_to_go(client):
    _project("Only one", "only-one")
    html = client.get("/project/only-one").text
    assert "make-subproject" not in html


def test_child_page_shows_the_family_fold(client):
    parent = _project("Board games", "board-games")
    _project("Beta", "beta")
    tak = _project("Tak", "tak")
    subprojects.adopt(parent, tak)
    html = client.get("/project/board-games-tak").text
    assert 'id="family"' in html
    assert "Sub-project of Board games" in html
    assert "/project/board-games-tak/release" in html
    # It can also move to the other family - but its own parent is not offered.
    assert "/project/board-games-tak/make-subproject" in html
    assert "<option" in html and "Beta" in html


def test_meta_project_page_offers_no_convert_controls(client):
    _project("Project Portal", config.META_PROJECT_SLUG)
    _project("Other", "other")
    html = client.get(f"/project/{config.META_PROJECT_SLUG}").text
    assert "make-subproject" not in html

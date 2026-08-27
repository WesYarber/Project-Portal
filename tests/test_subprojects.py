"""Splitting a project into sub-projects.

Wes, 2026-07-22 02:00, re-pasting a request from a day earlier:

    "Create a class/object for sub-projects where something can be split out
    like my project involving making different games to host on my site. It has
    many games in there, and the smart thing for it to do would be to split them
    into multiple subprojects that live under that original project that I
    spawned in. Some games won't be developed for now, but they will also all
    need their own context separate from one another."

The tests are grouped by the claim they defend: the shape of a child, the
one-level rule, what an agent's report may and may not do, what the prompt says,
and the two places the UI has to get it right.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, subprojects


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _parent(title="Board games for example.net/games", **fields):
    row = db.create_project(title, description="Games to host on my site", slug="board-games")
    if fields:
        db.update_project(row["id"], **fields)
        row = db.get_project(row["id"])
    return row


# --------------------------------------------------------------------------
# Folder names
# --------------------------------------------------------------------------

def test_child_slug_is_the_parent_slug_plus_a_short_name(temp_data_dir):
    parent = _parent()
    assert subprojects.child_slug(parent, "Catan") == "board-games-catan"
    # The child name gets the same lossy treatment a project name does, so a
    # title is not pasted whole onto the end of the parent's folder.
    assert subprojects.child_slug(parent, "Settlers of Catan") == "board-games-settlers-catan"


def test_child_slug_does_not_repeat_the_parent_name(temp_data_dir):
    """`board-games` + "Board games" must not become board-games-board-games.

    The parent already holds `board-games` itself, so the uniquifier has the
    last word - but what it uniquifies is the single name, not the doubled one.
    """
    parent = _parent()
    assert subprojects.child_slug(parent, "Board games") == "board-games-2"


def test_child_slug_is_unique(temp_data_dir):
    parent = _parent()
    first = subprojects.create_child(parent, "Catan")
    assert first["slug"] == "board-games-catan"
    second = subprojects.create_child(parent, "Catan the second")
    assert second["slug"] != first["slug"]


def test_child_workspace_is_beside_the_parent_not_inside_it(temp_data_dir):
    """Flat workspaces: a child's git repo must not land inside the parent's."""
    parent = _parent()
    child = subprojects.create_child(parent, "Catan")
    parent_ws = config.PROJECTS_DIR / parent["slug"]
    child_ws = config.PROJECTS_DIR / child["slug"]
    assert child_ws.parent == parent_ws.parent
    assert parent_ws not in child_ws.parents


# --------------------------------------------------------------------------
# What a child is
# --------------------------------------------------------------------------

def test_child_points_at_its_parent(temp_data_dir):
    parent = _parent()
    child = subprojects.create_child(parent, "Catan", "A tile-laying game.")
    assert db.parent_id_of(child) == parent["id"]
    assert child["description"] == "A tile-laying game."
    assert [row["id"] for row in db.child_projects(parent["id"])] == [child["id"]]


def test_child_starts_in_the_backlog(temp_data_dir):
    """"Some games won't be developed for now" - a split must not commit Wes to
    building every piece of it."""
    parent = _parent(stage="active", build_approved=True)
    child = subprojects.create_child(parent, "Catan")
    assert child["stage"] == "backlog"


def test_child_inherits_build_approval(temp_data_dir):
    """Approving "board games for my site" was approval to write the games."""
    parent = _parent(build_approved=1)
    assert subprojects.create_child(parent, "Catan")["build_approved"] == 1


def test_child_of_an_unapproved_parent_is_unapproved(temp_data_dir):
    parent = _parent(build_approved=0)
    assert subprojects.create_child(parent, "Catan")["build_approved"] == 0


def test_child_inherits_the_parents_members(temp_data_dir):
    # A child split out of Karli's project goes on Karli's board - the owner
    # fallback in create_project must not reassign her split (Wes, 2026-08-06,
    # about the idea form; same wrong default here).
    from app import people

    parent = _parent()
    karli = people.add(name="Karli", gender="female")
    people.set_members(parent["id"], {karli})

    child = subprojects.create_child(parent, "Catan")
    assert people.member_ids(child["id"]) == {karli}


def test_child_of_a_shared_parent_is_shared_too(temp_data_dir):
    from app import people

    parent = _parent()
    karli = people.add(name="Karli", gender="female")
    owner_id = int(people.owner()["id"])
    people.set_members(parent["id"], {owner_id, karli})

    child = subprojects.create_child(parent, "Catan")
    assert people.member_ids(child["id"]) == {owner_id, karli}


def test_child_inherits_the_parents_kind_when_none_is_given(temp_data_dir):
    parent = _parent(kind="hardware")
    assert subprojects.create_child(parent, "Catan")["kind"] == "hardware"


def test_an_unknown_kind_falls_back_rather_than_being_stored(temp_data_dir):
    parent = _parent(kind="software")
    child = subprojects.create_child(parent, "Catan", kind="boardgame")
    assert child["kind"] == "software"


def test_both_sides_are_journalled(temp_data_dir):
    parent = _parent()
    child = subprojects.create_child(parent, "Catan")
    parent_journal = " ".join(r["content_md"] for r in db.list_journal(parent["id"]))
    child_journal = " ".join(r["content_md"] for r in db.list_journal(child["id"]))
    assert "Catan" in parent_journal and child["slug"] in parent_journal
    assert parent["title"] in child_journal


def test_a_child_needs_a_title(temp_data_dir):
    parent = _parent()
    with pytest.raises(subprojects.SplitError):
        subprojects.create_child(parent, "   ")


def test_nothing_is_moved_out_of_the_parent(temp_data_dir):
    """A split creates empty projects; it never migrates the parent's todos."""
    parent = _parent()
    db.add_todo(parent["id"], "Ship Catan", owner="agent")
    subprojects.create_child(parent, "Catan")
    assert len(db.visible_todos(parent["id"], owner="agent")) == 1


# --------------------------------------------------------------------------
# One level deep
# --------------------------------------------------------------------------

def test_a_sub_project_cannot_be_split_again(temp_data_dir):
    parent = _parent()
    child = subprojects.create_child(parent, "Catan")
    assert not subprojects.can_have_children(child)
    with pytest.raises(subprojects.SplitError):
        subprojects.create_child(child, "Catan expansion")


def test_a_top_level_project_can_be_split(temp_data_dir):
    assert subprojects.can_have_children(_parent())


def test_parent_id_of_tolerates_a_row_without_the_column(temp_data_dir):
    """The dashboard, the prompt builder and the delete path all call this."""
    parent = _parent()
    row = db.get_conn().execute(
        "SELECT id, slug FROM projects WHERE id = ?", (parent["id"],)
    ).fetchone()
    assert db.parent_id_of(row) is None
    assert db.parent_id_of(None) is None


# --------------------------------------------------------------------------
# Applying an agent's report
# --------------------------------------------------------------------------

def test_report_creates_children(temp_data_dir):
    parent = _parent()
    created = subprojects.apply_report(parent, {"subprojects": {"add": [
        {"title": "Catan", "description": "Tiles."},
        {"title": "Wingspan"},
    ]}})
    assert [c["title"] for c in created] == ["Catan", "Wingspan"]
    assert len(db.child_projects(parent["id"])) == 2


def test_report_with_no_subprojects_field_does_nothing(temp_data_dir):
    parent = _parent()
    assert subprojects.apply_report(parent, {"summary": ["x"]}) == []
    assert db.child_projects(parent["id"]) == []


def test_report_tolerates_a_bare_list(temp_data_dir):
    """The shape a model reaches for first; rejecting it drops a real split."""
    parent = _parent()
    created = subprojects.apply_report(parent, {"subprojects": [{"title": "Catan"}]})
    assert [c["title"] for c in created] == ["Catan"]


def test_report_ignores_junk(temp_data_dir):
    parent = _parent()
    for value in (None, "Catan", 7, {"add": "Catan"}, {"add": []}, {"nope": [1]}):
        assert subprojects.apply_report(parent, {"subprojects": value}) == []
    assert db.child_projects(parent["id"]) == []


def test_report_skips_entries_that_are_not_objects(temp_data_dir):
    parent = _parent()
    created = subprojects.apply_report(
        parent, {"subprojects": {"add": ["Catan", None, {"title": "Wingspan"}]}}
    )
    assert [c["title"] for c in created] == ["Wingspan"]


def test_a_repeated_split_does_not_duplicate_children(temp_data_dir):
    """A run that restates last run's list must not create a second Catan."""
    parent = _parent()
    spec = {"subprojects": {"add": [{"title": "Catan"}, {"title": "Wingspan"}]}}
    subprojects.apply_report(parent, spec)
    created = subprojects.apply_report(parent, spec)
    assert created == []
    assert len(db.child_projects(parent["id"])) == 2


def test_duplicate_detection_ignores_case_and_spacing(temp_data_dir):
    parent = _parent()
    subprojects.apply_report(parent, {"subprojects": {"add": [{"title": "Catan"}]}})
    created = subprojects.apply_report(
        parent, {"subprojects": {"add": [{"title": "  catan  "}]}}
    )
    assert created == []


def test_duplicates_within_one_report_are_collapsed(temp_data_dir):
    parent = _parent()
    created = subprojects.apply_report(
        parent, {"subprojects": {"add": [{"title": "Catan"}, {"title": "Catan"}]}}
    )
    assert len(created) == 1


def test_a_report_is_capped(temp_data_dir):
    """Fifty new projects on the dashboard is not something one click undoes."""
    parent = _parent()
    created = subprojects.apply_report(parent, {"subprojects": {"add": [
        {"title": f"Game {n}"} for n in range(40)
    ]}})
    assert len(created) == subprojects.MAX_CHILDREN_PER_REPORT


def test_a_sub_project_report_asking_to_split_is_refused_and_says_so(temp_data_dir):
    parent = _parent()
    child = subprojects.create_child(parent, "Catan")
    created = subprojects.apply_report(child, {"subprojects": {"add": [{"title": "Expansion"}]}})
    assert created == []
    assert db.child_projects(child["id"]) == []
    assert "cannot be split again" in " ".join(
        r["content_md"] for r in db.list_journal(child["id"])
    )


# --------------------------------------------------------------------------
# Through the worker's report handler
# --------------------------------------------------------------------------

def _result(report):
    return agent_runner.RunResult(ok=True, report=report, result_text="")


def test_worker_applies_a_split(temp_data_dir):
    from app import worker

    parent = _parent()
    worker._apply_report(parent, _result({  # noqa: SLF001
        "journal_entry_md": "Split the games out.",
        "subprojects": {"add": [{"title": "Catan"}]},
    }), task="build")
    assert [c["title"] for c in db.child_projects(parent["id"])] == ["Catan"]


def test_a_research_burst_cannot_spawn_projects(temp_data_dir):
    """A burst runs on unapproved backlog ideas; changing the dashboard is not
    a decision it gets to make."""
    from app import worker

    parent = _parent()
    worker._apply_report(parent, _result({  # noqa: SLF001
        "journal_entry_md": "Read about games.",
        "subprojects": {"add": [{"title": "Catan"}]},
    }), task="research")
    assert db.child_projects(parent["id"]) == []


def test_a_failing_split_does_not_lose_the_rest_of_the_report(temp_data_dir, monkeypatch):
    from app import worker

    parent = _parent()
    monkeypatch.setattr(
        subprojects, "apply_report",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    worker._apply_report(parent, _result({  # noqa: SLF001
        "journal_entry_md": "Did the work.",
        "todo_updates": {"add": [{"text": "a leftover", "owner": "agent"}]},
        "subprojects": {"add": [{"title": "Catan"}]},
    }), task="build")
    assert [t["text"] for t in db.visible_todos(parent["id"], owner="agent")] == ["a leftover"]


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------

def test_an_unsplit_project_is_told_it_can_split(temp_data_dir):
    section = subprojects.prompt_section(_parent())
    assert "## Sub-projects" in section
    assert '"subprojects"' in section


def test_a_parent_is_told_who_its_children_are(temp_data_dir):
    parent = _parent()
    subprojects.create_child(parent, "Catan", "A tile-laying game.")
    section = subprojects.prompt_section(db.get_project(parent["id"]))
    assert "Catan" in section
    assert "board-games-catan" in section
    assert "do NOT do their work here" in section


def test_a_child_is_told_it_is_one_and_who_its_siblings_are(temp_data_dir):
    parent = _parent()
    child = subprojects.create_child(parent, "Catan")
    subprojects.create_child(parent, "Wingspan")
    section = subprojects.prompt_section(db.get_project(child["id"]))
    assert "## This is a sub-project" in section
    assert parent["title"] in section
    assert "Wingspan" in section
    # Its own workspace, not the parent's - the mistake a child would otherwise
    # make on its very first run.
    assert str(config.PROJECTS_DIR / child["slug"]) in section


def test_an_only_child_gets_no_empty_sibling_list(temp_data_dir):
    parent = _parent()
    child = subprojects.create_child(parent, "Catan")
    assert "Sibling sub-projects" not in subprojects.prompt_section(db.get_project(child["id"]))


def test_a_child_whose_parent_was_deleted_does_not_raise(temp_data_dir):
    parent = _parent()
    child = subprojects.create_child(parent, "Catan")
    db.delete_project(parent["id"])
    assert subprojects.prompt_section(db.get_project(child["id"])) == ""


def test_the_section_reaches_the_run_prompt_above_the_journal(temp_data_dir):
    parent = _parent()
    child = subprojects.create_child(parent, "Catan")
    prompt = agent_runner.build_prompt("build", db.get_project(child["id"]))
    assert "## This is a sub-project" in prompt
    assert prompt.index("## This is a sub-project") < prompt.index("## Recent journal")


def test_the_contract_documents_the_field(temp_data_dir):
    assert '"subprojects"' in agent_runner.AGENT_CONTRACT
    assert "phases of a single build" in agent_runner.AGENT_CONTRACT


# --------------------------------------------------------------------------
# The dashboard
# --------------------------------------------------------------------------

def _rows(*projects):
    return [db.get_project(p["id"]) for p in projects]


def test_a_child_follows_its_parent_on_the_shelf(temp_data_dir):
    parent = _parent()
    other = db.create_project("Something else", slug="other")
    child = subprojects.create_child(parent, "Catan")
    grouped = subprojects.group_for_shelf(_rows(parent, other, child))
    assert [e["project"]["slug"] for e in grouped] == [
        "board-games", "board-games-catan", "other",
    ]
    assert [e["child"] for e in grouped] == [False, True, False]
    assert grouped[1]["parent_title"] == parent["title"]


def test_a_child_on_a_shelf_without_its_parent_is_still_shown(temp_data_dir):
    """Not shown at all is the worst possible answer."""
    parent = _parent()
    child = subprojects.create_child(parent, "Catan")
    grouped = subprojects.group_for_shelf(_rows(child))
    assert len(grouped) == 1
    assert grouped[0]["child"] is True
    assert grouped[0]["parent_title"] == parent["title"]


def test_grouping_keeps_every_project_exactly_once(temp_data_dir):
    parent = _parent()
    kids = [subprojects.create_child(parent, name) for name in ("Catan", "Wingspan", "Azul")]
    rows = _rows(parent, *kids)
    grouped = subprojects.group_for_shelf(rows)
    assert sorted(e["project"]["id"] for e in grouped) == sorted(r["id"] for r in rows)


def test_grouping_an_empty_shelf(temp_data_dir):
    assert subprojects.group_for_shelf([]) == []


def test_dashboard_renders_a_family(client):
    parent = _parent(stage="active", build_approved=True)
    subprojects.create_child(parent, "Catan")
    db.update_project(db.get_project_by_slug("board-games-catan")["id"], stage="active")
    body = client.get("/").text
    assert "board-games-catan" in body
    assert "project-cell sub" in body or "sub\"" in body
    assert "Board games" in body


# --------------------------------------------------------------------------
# The project page
# --------------------------------------------------------------------------

def test_project_page_lists_children_and_offers_the_form(client):
    parent = _parent()
    subprojects.create_child(parent, "Catan")
    body = client.get("/project/board-games").text
    assert "Sub-projects" in body
    assert "/project/board-games-catan" in body
    assert "/project/board-games/subproject" in body


def test_a_child_page_links_home_and_hides_the_split_form(client):
    parent = _parent()
    child = subprojects.create_child(parent, "Catan")
    body = client.get(f"/project/{child['slug']}").text
    assert f'part of <a href="/project/{parent["slug"]}"' in body
    assert f"/project/{child['slug']}/subproject" not in body


def test_splitting_from_the_page_lands_on_the_new_child(client):
    _parent()
    resp = client.post(
        "/project/board-games/subproject",
        data={"title": "Catan", "description": "Tiles."},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/project/board-games-catan"
    assert db.get_project_by_slug("board-games-catan")["description"] == "Tiles."


def test_splitting_with_no_title_is_a_no_op(client):
    parent = _parent()
    resp = client.post(
        "/project/board-games/subproject", data={"title": "  "}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert db.child_projects(parent["id"]) == []


def test_splitting_a_sub_project_is_refused(client):
    parent = _parent()
    child = subprojects.create_child(parent, "Catan")
    resp = client.post(f"/project/{child['slug']}/subproject", data={"title": "Expansion"})
    assert resp.status_code == 400


def test_splitting_an_unknown_project_is_404(client):
    assert client.post("/project/nope/subproject", data={"title": "x"}).status_code == 404


# --------------------------------------------------------------------------
# Deletion
# --------------------------------------------------------------------------

def test_a_parent_with_children_cannot_be_deleted(client):
    parent = _parent()
    subprojects.create_child(parent, "Catan")
    resp = client.post("/project/board-games/delete", data={"confirm": "board-games"})
    assert resp.status_code == 409
    assert db.get_project(parent["id"]) is not None


def test_the_refusal_names_the_children(temp_data_dir):
    parent = _parent()
    subprojects.create_child(parent, "Catan")
    assert "Catan" in subprojects.blocks_delete(parent)


def test_a_childless_project_deletes_normally(client):
    parent = _parent()
    resp = client.post(
        "/project/board-games/delete", data={"confirm": "board-games"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert db.get_project(parent["id"]) is None


def test_a_child_deletes_normally(client):
    parent = _parent()
    child = subprojects.create_child(parent, "Catan")
    resp = client.post(
        f"/project/{child['slug']}/delete",
        data={"confirm": child["slug"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.child_projects(parent["id"]) == []


def test_child_counts(temp_data_dir):
    parent = _parent()
    subprojects.create_child(parent, "Catan")
    subprojects.create_child(parent, "Wingspan")
    db.create_project("Lonely", slug="lonely")
    assert db.child_counts() == {parent["id"]: 2}

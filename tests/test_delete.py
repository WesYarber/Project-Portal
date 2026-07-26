"""Deleting a project: the guards, and what survives the deletion."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project(temp_data_dir):
    row = db.create_project("Doomed", description="x", stage="active", build_approved=True, slug="doomed")
    db.add_journal(row["id"], "user", "note", "a note")
    db.create_question(row["id"], "a question?")
    workspace = config.PROJECTS_DIR / "doomed"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "NOTE.md").write_text("hello")
    return row


def test_delete_removes_project_journal_and_questions(client, project):
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok", cost_usd=0.5)
    resp = client.post(
        f"/project/{project['slug']}/delete", data={"confirm": "doomed"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert db.get_project_by_slug("doomed") is None
    assert db.list_journal(project["id"]) == []
    assert db.open_questions(project["id"]) == []
    # The run survives, detached: it is the record of work actually done, and
    # dropping it would make the usage totals on /activity disagree with what
    # the portal really spent.
    run = db.get_run(run_id)
    assert run is not None and run["project_id"] is None


def test_delete_keeps_the_workspace_by_default(client, project):
    client.post(f"/project/{project['slug']}/delete", data={"confirm": "doomed"})
    assert (config.PROJECTS_DIR / "doomed" / "NOTE.md").exists()


def test_delete_can_take_the_workspace_too(client, project):
    client.post(
        f"/project/{project['slug']}/delete",
        data={"confirm": "doomed", "delete_workspace": "on"},
    )
    assert not (config.PROJECTS_DIR / "doomed").exists()


def test_wrong_confirmation_deletes_nothing(client, project):
    resp = client.post(f"/project/{project['slug']}/delete", data={"confirm": "doome"})
    assert resp.status_code == 400
    assert db.get_project_by_slug("doomed") is not None


def test_missing_confirmation_deletes_nothing(client, project):
    resp = client.post(f"/project/{project['slug']}/delete", data={})
    assert resp.status_code == 400
    assert db.get_project_by_slug("doomed") is not None


def test_meta_project_cannot_be_deleted(client):
    db.create_project("Project Portal", slug=config.META_PROJECT_SLUG)
    resp = client.post(
        f"/project/{config.META_PROJECT_SLUG}/delete",
        data={"confirm": config.META_PROJECT_SLUG},
    )
    assert resp.status_code == 400
    assert db.get_project_by_slug(config.META_PROJECT_SLUG) is not None


def test_project_page_hides_the_danger_zone_for_the_meta_project(client):
    db.create_project("Project Portal", slug=config.META_PROJECT_SLUG)
    assert "danger-zone" not in client.get(f"/project/{config.META_PROJECT_SLUG}").text


def test_danger_zone_sits_below_the_journal(client, project):
    """His 04:52 note: the delete control belongs under everything he actually
    comes to the page to read, so it is the last section, after the journal."""
    db.add_journal(project["id"], "agent", "progress", "did a thing")
    body = client.get(f"/project/{project['slug']}").text
    assert body.index('id="journal"') < body.index("Danger zone")


def test_danger_zone_is_one_collapsed_fold_heading_included(client, project):
    """His 06:05 note, verbatim ask: collapsible and collapsed by default.
    The heading must live INSIDE the fold's summary - an always-visible
    <h2>Danger zone</h2> above a collapsed details is not a collapsed section,
    it is a heading with a hidden body."""
    body = client.get(f"/project/{project['slug']}").text
    assert "<h2>Danger zone</h2>" not in body
    zone = body[body.index('<details class="danger-zone"'):]
    zone = zone[:zone.index("</details>")]
    assert "<summary>Danger zone</summary>" in zone
    # No `open` attribute on the details tag: collapsed by default.
    assert "open" not in zone[:zone.index(">")]
    # The delete form is inside the fold, so nothing of it shows collapsed.
    assert 'name="confirm"' in zone


def test_running_agent_blocks_deletion(client, project):
    db.create_run(project["id"], "build", "opus")  # left 'running'
    resp = client.post(f"/project/{project['slug']}/delete", data={"confirm": "doomed"})
    assert resp.status_code == 409
    assert db.get_project_by_slug("doomed") is not None


def test_unknown_project_is_404(client):
    assert client.post("/project/nope/delete", data={"confirm": "nope"}).status_code == 404


def test_workspace_removal_refuses_paths_outside_the_projects_dir(temp_data_dir):
    from app import main

    outside = temp_data_dir / "secret"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    # A slug is user-controlled and _remove_workspace is an rm -rf, so a
    # traversal attempt must resolve out of range and be refused.
    main._remove_workspace("../secret")
    assert (outside / "keep.txt").exists()


def test_delete_is_journalled_portal_wide(client, project):
    client.post(f"/project/{project['slug']}/delete", data={"confirm": "doomed"})
    entries = db.list_journal(limit=10)
    assert any("Doomed" in row["content_md"] for row in entries)


def test_danger_zone_field_opts_out_of_draft_saving(client, project):
    # A remembered confirmation string would leave the delete form pre-armed
    # on the next visit.
    body = client.get(f"/project/{project['slug']}").text
    assert 'name="confirm"' in body
    assert "data-no-draft" in body

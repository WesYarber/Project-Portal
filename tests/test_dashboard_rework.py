"""The dashboard rework and banner-marker rules from Wes's 04:05/04:07 notes.

Two notes, one instruction: sections by status with drag-to-move and a themed
right-click menu; the summary tick reserved for things that actually shipped;
the control widths cut down to what their contents need; and the status widget
under the wordmark laid out so the heatmap's height cannot spread the text
lines apart.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project("Fridge Board", description="A thing.", stage="active", build_approved=True, slug="fridge")


def _finished_run(project_id: int, summary) -> int:
    run_id = db.create_run(project_id, "build", "opus")
    db.finish_run(run_id, "ok")
    db.set_run_report_summary(run_id, summary)
    return run_id


# --- the tick is only for things that shipped (04:07 note) ------------------

def test_a_shipped_bullet_wears_the_tick():
    assert db.summary_bullet("Completed todos now age out after 16 hours") == {
        "text": "Completed todos now age out after 16 hours",
        "kind": "done",
    }


def test_an_explicit_note_prefix_is_stripped_and_marked():
    assert db.summary_bullet("note: the dropdowns shipped without a screenshot pass") == {
        "text": "the dropdowns shipped without a screenshot pass",
        "kind": "note",
    }


def test_the_prefix_is_case_insensitive_and_tolerates_a_dash():
    assert db.summary_bullet("NOTE- check the phone layout")["kind"] == "note"
    assert db.summary_bullet("FYI: the restart is deferred")["kind"] == "note"


def test_wes_exact_complaint_is_classified_as_a_note():
    # The bullet he screenshotted wearing a tick it had not earned.
    b = db.summary_bullet(
        "Not started yet from your 03:05 note: one-off agent tasks without a whole project"
    )
    assert b["kind"] == "note"


def test_status_remark_openers_do_not_get_the_tick():
    for text in (
        "Next run: the dashboard rework",
        "Still to do: the live-updating pages",
        "Nothing needed fixing on the memory page",
        "Didn't get to the style template",
        "Blocked on the Cloudflare hostname",
        "Waiting on your answer about the ACL",
        "Skipped the screenshot pass this run",
        # Seen wearing a tick on the live page while building this - the
        # orphan detector's line is a status if anything is.
        "Orphaned: the service stopped while this run was in progress.",
        "Failed to reach the Claude usage API this run",
    ):
        assert db.summary_bullet(text)["kind"] == "note", text


def test_a_feature_about_notes_is_not_mistaken_for_a_note():
    # "Notes stay yours until an agent reads them" is a shipped change; only
    # "not " (the word) and the explicit "note:" prefix mark a remark.
    assert db.summary_bullet("Notes stay editable until an agent reads them")["kind"] == "done"


def test_a_bare_note_prefix_keeps_its_text():
    # "note:" followed by nothing must not render an empty line.
    assert db.summary_bullet("note:")["text"]


def test_the_page_renders_the_note_marker_class(client, project):
    _finished_run(project["id"], ["Shipped the thing", "note: one caveat to know"])
    body = client.get("/project/fridge").text
    assert "<li>Shipped the thing</li>" in body
    assert '<li class="is-note">one caveat to know</li>' in body


def test_the_note_marker_is_styled_without_the_tick():
    css = (STATIC / "style.css").read_text()
    assert ".work-summary-text li.is-note::before" in css
    # The tick glyph must not be the note marker.
    note_block = css.split(".work-summary-text li.is-note::before {")[1].split("}")[0]
    assert "2714" not in note_block


def test_the_contract_tells_agents_the_note_rule():
    from app import agent_runner

    assert 'must open with "note:"' in agent_runner.AGENT_CONTRACT


# --- sections, drag and the menu (04:05 note) -------------------------------

def test_done_and_abandoned_fold_together_at_the_end(client, project):
    db.update_project(project["id"], stage="done")
    other = db.create_project("Left Behind", slug="left", stage="abandoned")
    html = client.get("/").text
    assert 'id="done"' in html
    assert "Fridge Board" in html and "Left Behind" in html
    # Folded below the backlog shelf, above the new-idea form.
    assert html.index('id="backlog"') < html.index('id="done"') < html.index("New idea")


def test_done_cards_are_draggable_too(client, project):
    db.update_project(project["id"], stage="done")
    html = client.get("/").text
    done = html[html.index('id="done"'):]
    assert 'data-slug="fridge"' in done


def test_dropping_on_a_zone_posts_the_plain_status_route(client, project):
    """The drag posts to the same route as the picker, so the drop inherits
    every side effect: building approves the build, paused stamps the pause."""
    client.post("/project/fridge/status", data={"status": "paused"})
    row = db.get_project(project["id"])
    assert db.is_paused(row)
    client.post("/project/fridge/status", data={"status": "active"})
    row = db.get_project(project["id"])
    assert row["stage"] == "active" and row["build_approved"] == 1


def test_the_drag_and_menu_wiring_exists():
    js = (STATIC / "app.js").read_text()
    assert "initProjectDrag" in js
    assert "initProjectMenu" in js
    # The zone's status comes off the element, never a second mapping in JS.
    assert 'zone.getAttribute("data-status-zone")' in js
    # Dropping a card back onto its own section must not journal a fake change.
    assert 'dragged.getAttribute("data-status") === status' in js
    # The menu's delete still carries the slug the route demands.
    assert '{ confirm: slug }' in js


def test_the_menu_is_styled_by_the_page_not_the_platform():
    css = (STATIC / "style.css").read_text()
    assert ".ctx-menu {" in css
    assert ".ctx-item.status-building" in css  # move-to entries wear status colours
    assert ".ctx-item.ctx-danger" in css


def test_recent_activity_scrolls_in_the_journal_sized_window(client, project):
    db.add_journal(project["id"], "user", "note", "hello")
    html = client.get("/").text
    tail = html[html.index("Recent activity"):]
    assert "scroll-cap scroll-cap-journal" in tail


# --- control widths (04:05 note) --------------------------------------------

def test_model_labels_are_just_the_model_names():
    for _value, label in config.MODEL_CHOICES:
        assert "-" not in label.replace("4.5", "")
        assert "capable" not in label and "fastest" not in label

def test_status_line_separates_text_from_the_heatmap(client):
    """The tall heatmap in the same wrap container centred the text lines
    across its whole height - the "messed up spacing" screenshot."""
    html = client.get("/").text
    assert 'class="status-items"' in html
    # The heatmap link sits beside the items container, not inside it.
    items = html.split('class="status-items"')[1].split("</div>")[0]
    assert "heatmap-link" not in items

"""Wes's 2026-07-22 03:05 and 03:10 notes: the layout and control complaints.

    "in the summary text, after 3 lines, it is cut off. I'd like to see what
    the rest of the text is there even past 3 lines."
    "make the journal scrollable section another 5% taller."
    "the priority field in the project view is way too long for what it is. The
    status window isn't quite wide enough for the text it needs to display."
    "is it possible to theme the drop-downs as well? Or just implement custom
    ones? ... It would also be good if they could be colored options like they
    appear once selected."
    "I would also like to add an option to add a note, switch to building, and
    run an agent immediately."
    "Compress things like the learnings file viewer down into scrollable
    windows that is the same height of what the journal window size is
    currently. Consider this for any UI element that can grow without bound."

The CSS and JS assertions pin the rule that fixes each complaint, so a later
refactor that removes it fails here rather than silently reverting the fix.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db, worker


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project(temp_data_dir):
    return db.create_project("Project Portal", description="the portal", slug="portal")


def css() -> str:
    return (config.APP_ROOT / "app" / "static" / "style.css").read_text()


def js() -> str:
    return (config.APP_ROOT / "app" / "static" / "app.js").read_text()


# --------------------------------------------------------------------------
# The summary that was cut off
# --------------------------------------------------------------------------

def test_a_long_summary_bullet_is_kept_whole(project):
    sentence = (
        "Completed todos now age out after 16 hours, with a clear button and a "
        "history page, and the banner keeps the whole sentence rather than "
        "stopping at about three lines the way it used to when the cap was 300 "
        "characters and every bullet written as a real sentence lost its end - "
        "which is exactly the complaint, and this sentence is deliberately "
        "longer than the old cap so that it would have been truncated."
    )
    assert len(sentence) > 300
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok")
    db.set_run_report_summary(run_id, [sentence])
    assert db.get_run(run_id)["report_summary"] == sentence


# --------------------------------------------------------------------------
# Heights
# --------------------------------------------------------------------------

def test_the_journal_box_grew_by_another_five_percent():
    assert "--journal-max-h: 95vh;" in css()


def test_anything_that_can_grow_without_bound_has_a_cap():
    sheet = css()
    assert "--panel-max-h:" in sheet
    assert ".scroll-cap-panel { max-height: var(--panel-max-h); }" in sheet
    # The memory editors are textareas that app.js grows to fit their content,
    # which is what made the learnings file several screens tall.
    assert "max-height: var(--panel-max-h);" in sheet.split("textarea { resize: vertical")[1][:120]


def test_the_memory_editors_are_still_editable(client, temp_data_dir):
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.LEARNINGS_MD.write_text("- one\n", encoding="utf-8")
    client.post("/memory/learnings", data={"content": "- two\n"})
    assert config.LEARNINGS_MD.read_text() == "- two\n"


# --------------------------------------------------------------------------
# The control bar
# --------------------------------------------------------------------------

def test_controls_are_content_sized_not_row_filling():
    """Wes twice over: priority (one digit) had a quarter of the row, and the
    status box does not need a sentence's width. Fixed widths, no stretching."""
    sheet = css()
    assert ".control-bar > .control-priority { width: 5rem; }" in sheet
    assert ".control-bar > .control-status { width: 9.5rem; }" in sheet
    assert "grid-template-columns" not in sheet.split(".control-bar {")[1].split("}")[0]


def test_the_phone_layout_lets_the_controls_share_rows():
    # Inside the 720px media block the fixed widths are released so three
    # controls can split a 390px row instead of overflowing it.
    phone = css().split("@media (max-width: 720px)")[1].split("@media")[0]
    assert ".control-bar { flex-wrap: wrap; }" in phone
    assert "width: auto" in phone


def test_priority_options_are_bare_digits(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert "- highest" not in body
    assert "- lowest" not in body


def test_the_todo_owner_dropdown_shares_the_line_and_sits_right():
    """The themed dropdown's .sel wrapper is width:100% everywhere else, which
    shoved the owner picker onto its own row under the add-a-todo field."""
    sheet = css()
    block = sheet.split(".todo-add .sel {")[1].split("}")[0]
    assert "width: auto" in block
    assert "margin-left: auto" in block
    assert "flex-wrap: nowrap" in sheet.split(".todo-add {")[1].split("}")[0]


# --------------------------------------------------------------------------
# Themed dropdowns
# --------------------------------------------------------------------------

def test_the_native_select_is_kept_and_submitted():
    # The enhancement must not replace the control the form actually posts.
    source = js()
    assert "wrap.appendChild(sel);" in source
    assert 'sel.dispatchEvent(new Event("change", { bubbles: true }))' in source
    assert ".sel select {" in css()
    assert "display: none" not in css().split(".sel select {")[1].split("}")[0]


def test_a_disabled_or_multiple_select_is_left_alone():
    assert "if (sel.multiple || sel.size > 1 || sel.dataset.enhanced) return;" in js()


def test_the_status_options_carry_their_color(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert 'data-opt-class="status-active"' in body
    assert 'data-opt-class="status-review"' in body


def test_the_priority_options_carry_a_color_band(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert 'data-opt-class="prio-high"' in body
    assert 'data-opt-class="prio-low"' in body


def test_the_option_rows_are_colored_like_the_closed_control():
    sheet = css()
    for state in ["active", "review", "paused", "abandoned"]:
        assert f".sel-opt.status-{state}" in sheet


# --------------------------------------------------------------------------
# Add note, build, run - in one press
# --------------------------------------------------------------------------

def test_the_note_form_offers_the_combined_button(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert 'name="then" value="run"' in body


def test_a_note_with_run_switches_to_active_and_queues_a_run(client, project, monkeypatch):
    queued = []

    async def fake_queue(project_id):
        queued.append(project_id)

    monkeypatch.setattr(worker, "queue_manual_run", fake_queue)
    client.post(
        f"/project/{project['slug']}/note",
        data={"note": "do the thing", "then": "run"},
        files=[],
    )
    row = db.get_project(project["id"])
    assert row["stage"] == "active"
    # Choosing active here is the approval, exactly as it is in the picker -
    # otherwise the project would say active and the worker would refuse.
    assert row["build_approved"] == 1
    assert queued == [project["id"]]
    assert any("do the thing" in j["content_md"] for j in db.list_journal(project["id"], limit=5))


def test_the_note_lands_before_the_run_is_queued(client, project, monkeypatch):
    seen = {}

    async def fake_queue(project_id):
        seen["notes"] = [j["content_md"] for j in db.list_journal(project_id, limit=5)]

    monkeypatch.setattr(worker, "queue_manual_run", fake_queue)
    client.post(f"/project/{project['slug']}/note", data={"note": "read me", "then": "run"})
    assert any("read me" in n for n in seen["notes"])


def test_a_plain_note_does_not_start_anything(client, project, monkeypatch):
    queued = []

    async def fake_queue(project_id):
        queued.append(project_id)

    monkeypatch.setattr(worker, "queue_manual_run", fake_queue)
    client.post(f"/project/{project['slug']}/note", data={"note": "just a note"})
    # A backlog project stays parked: backlog means "no model yet", so a plain
    # note must not activate it or start a run.
    assert db.get_project(project["id"])["stage"] == "backlog"
    assert queued == []


def test_an_empty_box_with_run_still_runs(client, project, monkeypatch):
    queued = []

    async def fake_queue(project_id):
        queued.append(project_id)

    monkeypatch.setattr(worker, "queue_manual_run", fake_queue)
    client.post(f"/project/{project['slug']}/note", data={"note": "  ", "then": "run"})
    assert queued == [project["id"]]


def test_a_project_already_building_is_not_journalled_as_changing(client, project, monkeypatch):
    async def fake_queue(project_id):
        return None

    monkeypatch.setattr(worker, "queue_manual_run", fake_queue)
    db.update_project(project["id"], stage="active", build_approved=True)
    client.post(f"/project/{project['slug']}/note", data={"note": "carry on", "then": "run"})
    statuses = [j for j in db.list_journal(project["id"], limit=10) if j["kind"] == "status"]
    assert statuses == []

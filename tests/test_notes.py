"""Notes that are still Wes's, and notes grouped into one for the next agent.

Wes, 2026-07-22 02:00, re-pasting a request from a day earlier:

    "previous notes that haven't yet been sent to a model should be able to be
    edited. And each note sent between model runs should be grouped together and
    sent to the model as one single note the next time the model is called up
    for a project."

Grouped by the claim they defend: what "pending" means and when it stops being
true, the block that goes into the prompt, the two routes, the migration (which
is the one place a mistake would silently destroy the edit window on every
restart), and the page.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import agent_runner, db, notes


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _project(title="Project Portal", slug="portal"):
    return db.create_project(title, description="the portal", slug=slug)


def _note(project_id, text):
    return db.add_journal(project_id, "user", "note", text)


# --------------------------------------------------------------------------
# What counts as pending
# --------------------------------------------------------------------------

def test_a_fresh_note_is_pending(temp_data_dir):
    p = _project()
    _note(p["id"], "make the buttons the same height")
    assert [r["content_md"] for r in db.pending_notes(p["id"])] == [
        "make the buttons the same height"
    ]


def test_a_note_is_written_with_no_delivery_stamp(temp_data_dir):
    p = _project()
    row = db.get_journal(_note(p["id"], "hello"))
    assert row["delivered_at"] is None


def test_everything_that_is_not_a_note_is_stamped_at_once(temp_data_dir):
    """A status line or an agent report has no edit window, so it must never be
    able to turn up in the pending list - not even if the query grows a bug."""
    p = _project()
    for author, kind in [
        ("user", "status"),
        ("agent", "progress"),
        ("system", "status"),
        ("user", "answer"),
    ]:
        row = db.get_journal(db.add_journal(p["id"], author, kind, "x"))
        assert row["delivered_at"] is not None, (author, kind)
    assert db.pending_notes(p["id"]) == []


def test_pending_notes_are_oldest_first(temp_data_dir):
    """He writes a correction after the thing it corrects, so order is meaning."""
    p = _project()
    for text in ["do the thing", "actually do it in blue", "no, green"]:
        _note(p["id"], text)
    assert [r["content_md"] for r in db.pending_notes(p["id"])] == [
        "do the thing",
        "actually do it in blue",
        "no, green",
    ]


def test_another_projects_notes_are_not_pending_here(temp_data_dir):
    a, b = _project(slug="a"), _project(title="Other", slug="b")
    _note(b["id"], "for b only")
    assert db.pending_notes(a["id"]) == []


def test_delivery_makes_a_note_no_longer_pending(temp_data_dir):
    p = _project()
    note_id = _note(p["id"], "x")
    db.mark_notes_delivered([note_id])
    assert db.pending_notes(p["id"]) == []
    assert db.get_journal(note_id)["delivered_at"] is not None


def test_delivery_only_touches_the_ids_it_is_given(temp_data_dir):
    """The prompt spends the notes it actually rendered. One written while the
    prompt was being assembled has to survive to the next run."""
    p = _project()
    first = _note(p["id"], "first")
    second = _note(p["id"], "written mid-build")
    db.mark_notes_delivered([first])
    assert [r["id"] for r in db.pending_notes(p["id"])] == [second]


def test_delivering_nothing_is_a_no_op(temp_data_dir):
    p = _project()
    _note(p["id"], "x")
    db.mark_notes_delivered([])
    assert len(db.pending_notes(p["id"])) == 1


def test_redelivery_does_not_move_the_original_stamp(temp_data_dir):
    p = _project()
    note_id = _note(p["id"], "x")
    db.mark_notes_delivered([note_id])
    was = db.get_journal(note_id)["delivered_at"]
    db.mark_notes_delivered([note_id])
    assert db.get_journal(note_id)["delivered_at"] == was


def test_is_pending_reads_the_row_the_template_already_has(temp_data_dir):
    p = _project()
    note_id = _note(p["id"], "x")
    status_id = db.add_journal(p["id"], "user", "status", "y")
    assert notes.is_pending(db.get_journal(note_id)) is True
    assert notes.is_pending(db.get_journal(status_id)) is False
    db.mark_notes_delivered([note_id])
    assert notes.is_pending(db.get_journal(note_id)) is False


# --------------------------------------------------------------------------
# The block that goes into the prompt
# --------------------------------------------------------------------------

def test_no_notes_renders_nothing(temp_data_dir):
    assert notes.render([]) == ""


def test_one_note_is_announced_as_one(temp_data_dir):
    p = _project()
    _note(p["id"], "make the journal taller")
    text = notes.render(db.pending_notes(p["id"]))
    assert "## A note from Wes since your last run" in text
    assert "make the journal taller" in text


def test_several_notes_arrive_as_one_block_in_order(temp_data_dir):
    """The ask: 'grouped together and sent to the model as one single note'."""
    p = _project()
    for text in ["first thing", "second thing", "third thing"]:
        _note(p["id"], text)
    text = notes.render(db.pending_notes(p["id"]))
    assert text.count("## ") == 1
    assert "3 notes from Wes" in text
    assert text.index("first thing") < text.index("second thing") < text.index("third thing")


def test_the_block_says_a_later_note_may_correct_an_earlier_one(temp_data_dir):
    p = _project()
    _note(p["id"], "blue")
    _note(p["id"], "no, green")
    assert "correct an earlier one" in notes.render(db.pending_notes(p["id"]))


def test_render_does_not_spend_the_notes(temp_data_dir):
    """So a preview of what an agent would read costs nothing."""
    p = _project()
    _note(p["id"], "x")
    notes.render(db.pending_notes(p["id"]))
    assert len(db.pending_notes(p["id"])) == 1


def test_deliver_returns_the_text_and_the_ids_and_spends_them(temp_data_dir):
    p = _project()
    ids = [_note(p["id"], "a"), _note(p["id"], "b")]
    got = notes.deliver(p["id"])
    assert got.ids == ids
    assert "a" in got.text and "b" in got.text
    assert db.pending_notes(p["id"]) == []


def test_deliver_with_nothing_pending_is_empty(temp_data_dir):
    p = _project()
    got = notes.deliver(p["id"])
    assert got.text == "" and got.ids == []


def test_deliver_never_raises(temp_data_dir, monkeypatch):
    """This is on the path that starts a run. A broken notes block must cost a
    block, not a run."""
    p = _project()
    _note(p["id"], "x")
    monkeypatch.setattr(db, "pending_notes", lambda _pid: (_ for _ in ()).throw(RuntimeError("boom")))
    got = notes.deliver(p["id"])
    assert got.text == "" and got.ids == []


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------

def test_the_prompt_carries_the_block_above_the_journal(temp_data_dir):
    p = _project()
    _note(p["id"], "the buttons are still not level")
    prompt = agent_runner.build_prompt("build", p)
    assert "notes from Wes" in prompt or "A note from Wes" in prompt
    assert prompt.index("from Wes since your last run") < prompt.index("## Recent journal")


def test_a_delivered_note_is_not_repeated_in_the_journal_tail(temp_data_dir):
    """Twice in one prompt, in two different framings, is how an agent ends up
    answering the same note twice."""
    p = _project()
    _note(p["id"], "unmistakable-note-text")
    prompt = agent_runner.build_prompt("build", p)
    assert prompt.count("unmistakable-note-text") == 1


def test_the_note_is_in_the_journal_tail_on_the_following_run(temp_data_dir):
    p = _project()
    _note(p["id"], "unmistakable-note-text")
    agent_runner.build_prompt("build", p)
    second = agent_runner.build_prompt("build", p)
    assert "from Wes since your last run" not in second
    tail = second.split("## Recent journal")[1]
    assert "unmistakable-note-text" in tail


def test_building_a_prompt_spends_the_notes(temp_data_dir):
    p = _project()
    _note(p["id"], "x")
    agent_runner.build_prompt("build", p)
    assert db.pending_notes(p["id"]) == []


def test_a_failed_delivery_leaves_the_note_in_the_journal_tail(temp_data_dir, monkeypatch):
    p = _project()
    _note(p["id"], "unmistakable-note-text")
    monkeypatch.setattr(
        db, "pending_notes", lambda _pid: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    prompt = agent_runner.build_prompt("build", p)
    assert "unmistakable-note-text" in prompt


def test_a_prompt_with_no_pending_notes_has_no_heading(temp_data_dir):
    p = _project()
    db.add_journal(p["id"], "agent", "progress", "did a thing")
    prompt = agent_runner.build_prompt("build", p)
    assert "from Wes since your last run" not in prompt


# --------------------------------------------------------------------------
# Editing and removing
# --------------------------------------------------------------------------

def test_editing_an_unsent_note_rewrites_it(client, temp_data_dir):
    p = _project()
    note_id = _note(p["id"], "make it blue")
    client.post(f"/project/{p['slug']}/note/{note_id}/edit", data={"note": "make it green"})
    assert db.get_journal(note_id)["content_md"] == "make it green"


def test_an_edited_note_is_still_pending(client, temp_data_dir):
    p = _project()
    note_id = _note(p["id"], "make it blue")
    client.post(f"/project/{p['slug']}/note/{note_id}/edit", data={"note": "make it green"})
    assert [r["id"] for r in db.pending_notes(p["id"])] == [note_id]


def test_a_sent_note_cannot_be_edited(client, temp_data_dir):
    """The agent has already acted on these words; rewriting them would make the
    journal disagree with the run that used them."""
    p = _project()
    note_id = _note(p["id"], "the original")
    db.mark_notes_delivered([note_id])
    client.post(f"/project/{p['slug']}/note/{note_id}/edit", data={"note": "rewritten"})
    assert db.get_journal(note_id)["content_md"] == "the original"


def test_a_sent_note_cannot_be_deleted(client, temp_data_dir):
    p = _project()
    note_id = _note(p["id"], "the original")
    db.mark_notes_delivered([note_id])
    client.post(f"/project/{p['slug']}/note/{note_id}/delete")
    assert db.get_journal(note_id) is not None


def test_a_status_entry_cannot_be_edited_through_the_note_route(client, temp_data_dir):
    """Nothing on the page offers this, so reaching it means something is wrong;
    it must still refuse rather than let the journal be rewritten by hand."""
    p = _project()
    entry = db.add_journal(p["id"], "user", "status", "Status changed")
    client.post(f"/project/{p['slug']}/note/{entry}/edit", data={"note": "no"})
    assert db.get_journal(entry)["content_md"] == "Status changed"


def test_an_agent_report_cannot_be_deleted_through_the_note_route(client, temp_data_dir):
    p = _project()
    entry = db.add_journal(p["id"], "agent", "progress", "a whole run report")
    client.post(f"/project/{p['slug']}/note/{entry}/delete")
    assert db.get_journal(entry) is not None


def test_deleting_an_unsent_note_removes_it(client, temp_data_dir):
    p = _project()
    note_id = _note(p["id"], "forget this")
    client.post(f"/project/{p['slug']}/note/{note_id}/delete")
    assert db.get_journal(note_id) is None


def test_emptying_the_box_deletes_the_note(client, temp_data_dir):
    p = _project()
    note_id = _note(p["id"], "forget this")
    client.post(f"/project/{p['slug']}/note/{note_id}/edit", data={"note": "   "})
    assert db.get_journal(note_id) is None


def test_a_note_belonging_to_another_project_is_404(client, temp_data_dir):
    a = _project(slug="a")
    b = _project(title="Other", slug="b")
    note_id = _note(b["id"], "b's note")
    assert client.post(f"/project/a/note/{note_id}/edit", data={"note": "x"}).status_code == 404
    assert client.post(f"/project/a/note/{note_id}/delete").status_code == 404
    assert db.get_journal(note_id)["content_md"] == "b's note"
    assert a is not None


def test_an_unknown_note_is_404(client, temp_data_dir):
    _project(slug="a")
    assert client.post("/project/a/note/9999/edit", data={"note": "x"}).status_code == 404


def test_editing_lands_back_on_the_journal(client, temp_data_dir):
    p = _project()
    note_id = _note(p["id"], "x")
    r = client.post(
        f"/project/{p['slug']}/note/{note_id}/edit",
        data={"note": "y"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("#journal")


def test_a_deleted_note_never_reaches_a_prompt(client, temp_data_dir):
    """The whole point of the window."""
    p = _project()
    note_id = _note(p["id"], "ignore-me-entirely")
    client.post(f"/project/{p['slug']}/note/{note_id}/delete")
    assert "ignore-me-entirely" not in agent_runner.build_prompt("build", p)


def test_an_edit_is_what_the_agent_reads(client, temp_data_dir):
    p = _project()
    note_id = _note(p["id"], "make it blue")
    client.post(f"/project/{p['slug']}/note/{note_id}/edit", data={"note": "make it green"})
    prompt = agent_runner.build_prompt("build", p)
    assert "make it green" in prompt and "make it blue" not in prompt


# --------------------------------------------------------------------------
# The migration
# --------------------------------------------------------------------------

def test_init_db_does_not_stamp_a_pending_note_on_restart(temp_data_dir):
    """The back-fill runs once, on the migration that adds the column. Run
    unconditionally it would mark every note Wes had just written as already
    sent on the next service restart - which for this project is every run."""
    p = _project()
    note_id = _note(p["id"], "written just before a restart")
    db.init_db()
    db.init_db()
    assert db.get_journal(note_id)["delivered_at"] is None
    assert [r["id"] for r in db.pending_notes(p["id"])] == [note_id]


def test_a_database_from_before_the_column_has_no_pending_notes(temp_data_dir, monkeypatch):
    """Every note in Wes's live journal has been read by an agent already, so
    the migration must not resurrect months of them into one giant block."""
    p = _project()
    note_id = _note(p["id"], "an old note")
    conn = db.get_conn()
    conn.execute("UPDATE journal SET delivered_at = NULL")
    conn.commit()
    # Simulate the column not existing yet, so init_db takes the migration path.
    monkeypatch.setattr(
        db, "_ADDED_COLUMNS", {**db._ADDED_COLUMNS, "journal": [("delivered_at", "TEXT")]}
    )
    conn.execute("ALTER TABLE journal RENAME COLUMN delivered_at TO delivered_at_old")
    conn.commit()
    db.init_db()
    assert db.pending_notes(p["id"]) == []
    assert db.get_journal(note_id)["delivered_at"] is not None


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

def test_an_unsent_note_offers_an_edit_box(client, temp_data_dir):
    p = _project()
    note_id = _note(p["id"], "make it green")
    html = client.get(f"/project/{p['slug']}").text
    assert f"/note/{note_id}/edit" in html
    assert f"/note/{note_id}/delete" in html
    assert "not sent yet" in html


def test_a_sent_note_offers_nothing(client, temp_data_dir):
    p = _project()
    note_id = _note(p["id"], "make it green")
    db.mark_notes_delivered([note_id])
    html = client.get(f"/project/{p['slug']}").text
    assert f"/note/{note_id}/edit" not in html
    assert "not sent yet" not in html


def test_the_edit_box_is_prefilled_with_the_note(client, temp_data_dir):
    p = _project()
    _note(p["id"], "the exact text he typed")
    html = client.get(f"/project/{p['slug']}").text
    assert "<textarea name=\"note\" rows=\"4\">the exact text he typed</textarea>" in html


def test_a_status_line_is_never_editable_on_the_page(client, temp_data_dir):
    p = _project()
    entry = db.add_journal(p["id"], "user", "status", "Status changed")
    html = client.get(f"/project/{p['slug']}").text
    assert f"/note/{entry}/edit" not in html


def test_a_note_posted_through_the_form_is_editable(client, temp_data_dir):
    """End to end: the way he actually writes one."""
    p = _project()
    client.post(f"/project/{p['slug']}/note", data={"note": "typed into the box"})
    html = client.get(f"/project/{p['slug']}").text
    assert "not sent yet" in html
    assert [r["content_md"] for r in db.pending_notes(p["id"])] == ["typed into the box"]


def test_the_journal_anchor_exists_for_the_redirect(client, temp_data_dir):
    p = _project()
    _note(p["id"], "x")
    assert 'id="journal"' in client.get(f"/project/{p['slug']}").text

"""Taking a file back off a note before any agent has read it.

Wes, 2026-08-17: "Add a way of removing note file attachments before the prompt
is sent."

The delete route and the bytes on disk already existed; what did not was a
control anywhere near the note, and - the part with actual logic in it - any
repair of the note's own text. A note lists the files that rode along with it
(`main.add_note`), that markdown is handed to the agent verbatim as its
instructions (`notes.render`), so a removed file left named in the note is an
instruction to go read a path that is not in the workspace.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import attachments, db, notes

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project(
        "Test Project", stage="active", build_approved=True, slug="test-project"
    )


def _note_with_files(client, project, text, count=2):
    """Post a note carrying `count` PNGs and hand back (journal_id, rows)."""
    files = [("files", (f"shot{i}.png", PNG, "image/png")) for i in range(count)]
    resp = client.post(
        f"/project/{project['slug']}/note",
        data={"note": text, "then": "queue"},
        files=files,
    )
    assert resp.status_code == 200
    rows = db.list_attachments(project["id"])
    assert len(rows) == count
    journal_id = int(rows[0]["journal_id"])
    return journal_id, rows


# --- the format, both ways -------------------------------------------------

def test_a_listing_round_trips_back_to_the_note_it_was_added_to():
    rows = [
        {"stored_name": "0001-a.png", "mime": "image/png", "size": 1024},
        {"stored_name": "0002-b.png", "mime": "image/png", "size": 2048},
    ]
    body = f"look at these\n\n{attachments.listing_block(rows)}"
    once = attachments.strip_from_note(body, "0001-a.png")
    assert "0001-a.png" not in once
    assert "0002-b.png" in once
    assert "**Attached 1 file(s):**" in once
    twice = attachments.strip_from_note(once, "0002-b.png")
    assert twice == "look at these"


def test_the_header_count_is_recounted_not_decremented():
    """A body is editable by hand between the upload and the removal, so a
    header that was already wrong must come out right rather than one less
    wrong."""
    body = (
        "hi\n\n**Attached 9 file(s):**\n"
        "- `attachments/0001-a.png` (image/png, 1.0 KB)\n"
        "- `attachments/0002-b.png` (image/png, 2.0 KB)"
    )
    after = attachments.strip_from_note(body, "0001-a.png")
    assert "**Attached 1 file(s):**" in after
    assert "9 file(s)" not in after


def test_a_file_the_note_never_named_leaves_the_body_identical():
    """Not just "comes back equal" - comes back *untouched*.

    The header below says 2 over one line, which is wrong, and the point is
    that this function does not fix it: the caller decides whether a note is
    worth an UPDATE by comparing the result against what went in, so a body
    that quietly rebuilds itself would rewrite notes nobody asked to change.
    """
    body = (
        "hi\n\n**Attached 2 file(s):**\n"
        "- `attachments/0001-a.png` (image/png, 1.0 KB)"
    )
    assert attachments.strip_from_note(body, "0009-never-here.png") == body


def test_a_bullet_of_the_writers_own_is_not_mistaken_for_the_listing():
    """The note's text may itself be a list of backticked paths, and one can
    sit directly under the block - so only the attachments/ prefix marks a
    line as part of it. Counting a sentence as a file leaves the header saying
    two over one file."""
    body = (
        "look at these\n\n"
        "**Attached 2 file(s):**\n"
        "- `attachments/0001-a.png` (image/png, 1.0 KB)\n"
        "- `attachments/0002-b.png` (image/png, 2.0 KB)\n"
        "- `app/db.py` is the one to fix"
    )
    after = attachments.strip_from_note(body, "0001-a.png")
    assert "**Attached 1 file(s):**" in after
    assert "- `app/db.py` is the one to fix" in after


def test_the_block_ends_at_the_first_line_that_is_not_part_of_it():
    """A path the writer pasted further down the note is not a third file.
    The block is the run of lines directly under its header and stops at the
    first line that is not one, blank line included."""
    body = (
        "look at these\n\n"
        "**Attached 2 file(s):**\n"
        "- `attachments/0001-a.png` (image/png, 1.0 KB)\n"
        "- `attachments/0002-b.png` (image/png, 2.0 KB)\n\n"
        "the older one is still here too:\n"
        "- `attachments/0000-old.png`"
    )
    after = attachments.strip_from_note(body, "0001-a.png")
    assert "**Attached 1 file(s):**" in after
    assert "- `attachments/0000-old.png`" in after


def test_removing_the_last_file_takes_the_header_with_it():
    rows = [{"stored_name": "0001-a.png", "mime": "image/png", "size": 1024}]
    body = f"see this\n\n{attachments.listing_block(rows)}"
    after = attachments.strip_from_note(body, "0001-a.png")
    assert after == "see this"
    assert "Attached" not in after


def test_text_after_the_block_survives_with_one_blank_line():
    rows = [{"stored_name": "0001-a.png", "mime": "image/png", "size": 1024}]
    body = f"see this\n\n{attachments.listing_block(rows)}\n\n*Rejected: big.mov: too big*"
    after = attachments.strip_from_note(body, "0001-a.png")
    assert after == "see this\n\n*Rejected: big.mov: too big*"


# --- the route -------------------------------------------------------------

def test_removing_one_file_updates_the_note_the_agent_will_read(client, project):
    journal_id, rows = _note_with_files(client, project, "two shots")
    victim, survivor = rows[0], rows[1]

    client.post(f"/attachment/{victim['id']}/delete")

    body = db.get_journal(journal_id)["content_md"]
    assert victim["stored_name"] not in body
    assert survivor["stored_name"] in body
    assert "**Attached 1 file(s):**" in body
    # And the prompt block the agent actually gets no longer names it either.
    prompt = notes.render(notes.pending(project["id"]))
    assert victim["stored_name"] not in prompt
    assert survivor["stored_name"] in prompt


def test_the_bytes_go_too_so_no_run_can_find_the_file(client, project):
    _journal_id, rows = _note_with_files(client, project, "one shot", count=1)
    row = rows[0]
    assert attachments.disk_path(project["slug"], row["stored_name"]) is not None

    client.post(f"/attachment/{row['id']}/delete")

    assert attachments.disk_path(project["slug"], row["stored_name"]) is None
    assert db.get_attachment(row["id"]) is None


def test_a_files_only_note_is_deleted_with_its_last_file(client, project):
    """Nothing was typed, so once the file is gone the note is an empty entry
    in the journal saying nothing - the same rule as clearing the edit box."""
    _journal_id, rows = _note_with_files(client, project, "", count=1)
    journal_id = int(rows[0]["journal_id"])

    client.post(f"/attachment/{rows[0]['id']}/delete")

    assert db.get_journal(journal_id) is None
    assert notes.pending(project["id"]) == []


def test_a_files_only_note_survives_while_it_still_has_a_file(client, project):
    _journal_id, rows = _note_with_files(client, project, "", count=2)
    journal_id = int(rows[0]["journal_id"])

    client.post(f"/attachment/{rows[0]['id']}/delete")

    entry = db.get_journal(journal_id)
    assert entry is not None
    assert rows[1]["stored_name"] in entry["content_md"]


def test_a_delivered_note_keeps_the_words_that_were_sent(client, project):
    """After delivery the file may still be deleted from the Files shelf, but
    the sentence an agent was given is history and stays as it was given."""
    journal_id, rows = _note_with_files(client, project, "two shots")
    notes.deliver(project["id"])
    before = db.get_journal(journal_id)["content_md"]

    client.post(f"/attachment/{rows[0]['id']}/delete")

    assert db.get_journal(journal_id)["content_md"] == before
    # The row and the bytes are gone even so - deleting an old upload is still
    # allowed, it just does not rewrite the record.
    assert db.get_attachment(rows[0]["id"]) is None


def test_deleting_a_loose_upload_touches_no_journal_entry(client, project):
    """An attachment with no note behind it (none of the upload paths make one
    today, but a row's journal_id is nullable) must not crash the route."""
    stored = attachments.store(
        project_id=project["id"],
        slug=project["slug"],
        orig_name="loose.png",
        data=PNG,
        declared_mime="image/png",
    )
    resp = client.post(f"/attachment/{stored['id']}/delete")
    assert resp.status_code == 200
    assert db.get_attachment(stored["id"]) is None


# --- taking the whole note back, files and all -----------------------------
#
# `attachments.journal_id` is a real foreign key, so a note carrying a file
# could not be deleted at all until 2026-08-17: the DELETE raised
# IntegrityError and the route 500ed. Both routes below reach the same
# `db.delete_journal_note`, and both were broken.

def test_deleting_a_note_that_carried_a_file_does_not_error(client, project):
    journal_id, rows = _note_with_files(client, project, "two shots")
    resp = client.post(f"/project/{project['slug']}/note/{journal_id}/delete")
    assert resp.status_code == 200
    assert db.get_journal(journal_id) is None
    # The files are Wes's, not the sentence's: still uploaded, still on disk.
    for row in rows:
        assert db.get_attachment(row["id"]) is not None
        assert attachments.disk_path(project["slug"], row["stored_name"]) is not None


def test_clearing_the_edit_box_on_a_note_with_a_file_does_not_error(client, project):
    journal_id, _rows = _note_with_files(client, project, "a shot", count=1)
    resp = client.post(
        f"/project/{project['slug']}/note/{journal_id}/edit", data={"note": "   "}
    )
    assert resp.status_code == 200
    assert db.get_journal(journal_id) is None


def test_a_delivered_note_keeps_its_files_provenance(client, project):
    """The detach must carry the same window as the delete, or a refused
    delete would still have cut the files loose from the note that explains
    them."""
    journal_id, rows = _note_with_files(client, project, "two shots")
    notes.deliver(project["id"])

    client.post(f"/project/{project['slug']}/note/{journal_id}/delete")

    assert db.get_journal(journal_id) is not None
    assert db.get_attachment(rows[0]["id"])["journal_id"] == journal_id


# --- the control on the page ----------------------------------------------

def test_the_note_offers_remove_on_each_file_while_it_is_unsent(client, project):
    _journal_id, rows = _note_with_files(client, project, "two shots")
    html = client.get(f"/project/{project['slug']}").text
    for row in rows:
        assert f'action="/attachment/{row["id"]}/delete"' in html
    assert html.count(">remove</button>") == 2


def test_the_remove_control_disappears_once_the_note_has_gone_to_a_run(
    client, project
):
    _journal_id, rows = _note_with_files(client, project, "two shots")
    notes.deliver(project["id"])
    html = client.get(f"/project/{project['slug']}").text
    assert ">remove</button>" not in html
    # The Files shelf still offers its own delete, so the file is not stranded.
    assert f'action="/attachment/{rows[0]["id"]}/delete"' in html

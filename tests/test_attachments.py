"""Dropping files onto a project: storage, serving, and what the agent sees."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import agent_runner, attachments, config, db

# A one-pixel PNG - real bytes, so the mime sniffing and size maths are honest.
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
    return db.create_project("Test Project", stage="active", build_approved=True, slug="test-project")


# --- filename safety -------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("photo.png", "photo.png"),
        ("../../etc/passwd", "passwd"),
        (r"C:\Users\wes\shot.PNG", "shot.PNG"),
        ("my photo (1).jpg", "my-photo-1-.jpg"),
        ("....", "upload"),
        ("", "upload"),
        ("/", "upload"),
        (".hidden", "hidden"),
    ],
)
def test_safe_name(raw, expected):
    assert attachments.safe_name(raw) == expected


def test_safe_name_never_escapes_its_directory(tmp_path):
    for raw in ["../../evil", "..", "../x.png", "a/b/c.png"]:
        joined = (tmp_path / attachments.safe_name(raw)).resolve()
        assert joined.parent == tmp_path.resolve()


def test_safe_name_truncates_stem_but_keeps_extension():
    name = attachments.safe_name("x" * 200 + ".png")
    assert name.endswith(".png")
    assert len(name) <= attachments.MAX_NAME_LEN


def test_guess_mime_prefers_browser_but_falls_back_to_extension():
    assert attachments.guess_mime("a.bin", "image/png") == "image/png"
    # MediaRecorder sends codec parameters; they must not survive into the
    # value compared against INLINE_TYPES.
    assert attachments.guess_mime("m.webm", "audio/webm;codecs=opus") == "audio/webm"
    assert attachments.guess_mime("photo.png", "") == "image/png"
    assert attachments.guess_mime("photo.png", "application/octet-stream") == "image/png"
    assert attachments.guess_mime("mystery.zzz", "") == "application/octet-stream"


def test_media_kind():
    assert attachments.media_kind("image/png") == "image"
    assert attachments.media_kind("audio/webm") == "audio"
    assert attachments.media_kind("video/mp4") == "video"
    assert attachments.media_kind("application/pdf") == "file"
    assert attachments.media_kind("") == "file"


# --- storage ---------------------------------------------------------------

def test_store_writes_into_the_workspace(project):
    row = attachments.store(project["id"], project["slug"], "shot.png", PNG, "image/png")
    path = config.PROJECTS_DIR / project["slug"] / "attachments" / row["stored_name"]
    assert path.read_bytes() == PNG
    assert row["mime"] == "image/png"
    assert row["size"] == len(PNG)
    # The id is baked into the stored name, which is what makes collisions
    # impossible without a second uniqueness scheme.
    assert row["stored_name"].endswith("-shot.png")
    assert str(row["id"]) in row["stored_name"]


def test_same_filename_twice_does_not_overwrite(project):
    a = attachments.store(project["id"], project["slug"], "shot.png", PNG, "image/png")
    b = attachments.store(project["id"], project["slug"], "shot.png", b"different", "image/png")
    assert a["stored_name"] != b["stored_name"]
    assert attachments.disk_path(project["slug"], a["stored_name"]).read_bytes() == PNG
    assert attachments.disk_path(project["slug"], b["stored_name"]).read_bytes() == b"different"


def test_store_rejects_empty_and_oversized(project, monkeypatch):
    with pytest.raises(ValueError):
        attachments.store(project["id"], project["slug"], "empty.png", b"", "image/png")
    monkeypatch.setattr(attachments, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(ValueError):
        attachments.store(project["id"], project["slug"], "big.png", b"12345", "image/png")
    # A rejected upload leaves no index row behind.
    assert db.list_attachments(project["id"]) == []


def test_disk_path_rejects_traversal(project):
    attachments.store(project["id"], project["slug"], "shot.png", PNG, "image/png")
    assert attachments.disk_path(project["slug"], "../../portal.db") is None
    assert attachments.disk_path(project["slug"], "nope.png") is None


def test_list_attachments_hides_rows_with_no_file_yet(project):
    db.add_attachment(project["id"], "pending.png", "", "image/png", 10)
    assert db.list_attachments(project["id"]) == []


def test_human_size():
    assert attachments.human_size(0) == "0 B"
    assert attachments.human_size(None) == "0 B"
    assert attachments.human_size(512) == "512 B"
    assert attachments.human_size(2048) == "2.0 KB"
    assert attachments.human_size(5 * 1024 * 1024) == "5.0 MB"


# --- the note form ---------------------------------------------------------

def test_note_with_a_file_stores_it_and_names_it_in_the_journal(client, project):
    resp = client.post(
        f"/project/{project['slug']}/note",
        data={"note": "look at this"},
        files={"files": ("shot.png", PNG, "image/png")},
    )
    assert resp.status_code == 200  # followed the redirect

    rows = db.list_attachments(project["id"])
    assert len(rows) == 1
    assert rows[0]["note"] == "look at this"
    # Linked to the journal entry it arrived with, so the UI can show it there.
    assert rows[0]["journal_id"] is not None

    entry = db.list_journal(project["id"])[0]
    assert "look at this" in entry["content_md"]
    assert f"attachments/{rows[0]['stored_name']}" in entry["content_md"]


def test_note_with_multiple_files(client, project):
    resp = client.post(
        f"/project/{project['slug']}/note",
        data={"note": "two things"},
        files=[
            ("files", ("a.png", PNG, "image/png")),
            ("files", ("b.txt", b"hello", "text/plain")),
        ],
    )
    assert resp.status_code == 200
    rows = db.list_attachments(project["id"])
    assert [r["orig_name"] for r in rows] == ["a.png", "b.txt"]


def test_file_with_no_note_text_still_lands(client, project):
    client.post(
        f"/project/{project['slug']}/note",
        data={"note": ""},
        files={"files": ("voice-memo.webm", b"OggS-ish", "audio/webm;codecs=opus")},
    )
    rows = db.list_attachments(project["id"])
    assert len(rows) == 1
    assert rows[0]["mime"] == "audio/webm"


def test_empty_submission_writes_nothing(client, project):
    client.post(f"/project/{project['slug']}/note", data={"note": "   "})
    assert db.list_journal(project["id"]) == []
    assert db.list_attachments(project["id"]) == []


def test_plain_note_without_files_still_works(client, project):
    """The form is multipart now; the old text-only path must be untouched."""
    client.post(f"/project/{project['slug']}/note", data={"note": "just words"})
    entries = db.list_journal(project["id"])
    assert len(entries) == 1
    assert entries[0]["content_md"] == "just words"
    assert "Attached" not in entries[0]["content_md"]


def test_oversized_upload_is_reported_in_the_note_not_swallowed(client, project, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_UPLOAD_BYTES", 4)
    client.post(
        f"/project/{project['slug']}/note",
        data={"note": "here"},
        files={"files": ("big.png", PNG, "image/png")},
    )
    entry = db.list_journal(project["id"])[0]
    assert "Rejected" in entry["content_md"]
    assert db.list_attachments(project["id"]) == []


def test_note_route_404s_for_unknown_project(client):
    resp = client.post("/project/nope/note", data={"note": "hi"})
    assert resp.status_code == 404


# --- serving ---------------------------------------------------------------

def test_serving_an_image_inline(client, project):
    row = attachments.store(project["id"], project["slug"], "shot.png", PNG, "image/png")
    resp = client.get(f"/attachment/{row['id']}")
    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.headers["content-disposition"].startswith("inline")


def test_unsafe_types_are_forced_to_download(client, project):
    """Serving user-supplied HTML or SVG inline from this origin would be script
    execution on the portal's own domain."""
    for name, mime in [("evil.html", "text/html"), ("evil.svg", "image/svg+xml")]:
        row = attachments.store(project["id"], project["slug"], name, b"<script>x</script>", mime)
        resp = client.get(f"/attachment/{row['id']}")
        assert resp.headers["content-type"].startswith("application/octet-stream")
        assert resp.headers["content-disposition"].startswith("attachment")


def test_serving_a_missing_file_404s_rather_than_500s(client, project):
    row = attachments.store(project["id"], project["slug"], "shot.png", PNG, "image/png")
    attachments.disk_path(project["slug"], row["stored_name"]).unlink()
    assert client.get(f"/attachment/{row['id']}").status_code == 404


def test_serving_an_unknown_attachment_404s(client):
    assert client.get("/attachment/9999").status_code == 404


def test_delete_attachment_removes_row_and_file(client, project):
    row = attachments.store(project["id"], project["slug"], "shot.png", PNG, "image/png")
    path = attachments.disk_path(project["slug"], row["stored_name"])
    resp = client.post(f"/attachment/{row['id']}/delete")
    assert resp.status_code == 200
    assert not path.exists()
    assert db.list_attachments(project["id"]) == []


# --- page + prompt ---------------------------------------------------------

def test_project_page_shows_previews_and_the_relative_path(client, project):
    attachments.store(project["id"], project["slug"], "shot.png", PNG, "image/png")
    attachments.store(project["id"], project["slug"], "memo.webm", b"aud", "audio/webm")
    body = client.get(f"/project/{project['slug']}").text
    assert "<img" in body and "<audio" in body
    # The path the agent will use is shown, so what Wes sees and what the agent
    # is told are visibly the same string.
    assert "attachments/0001-shot.png" in body
    assert 'enctype="multipart/form-data"' in body
    assert "data-dropzone" in body


def test_project_page_without_attachments_hides_the_section(client, project):
    """It used to say "Nothing attached yet". Wes asked for the section to be
    absent entirely when there is nothing in it - the note box above is still
    where an attachment comes from."""
    body = client.get(f"/project/{project['slug']}").text
    assert "<h2>Attachments</h2>" not in body
    assert "Nothing attached yet" not in body
    assert "attach files" in body


def test_workspace_listing_excludes_the_attachments_dir(client, project):
    from app import filetree

    attachments.store(project["id"], project["slug"], "shot.png", PNG, "image/png")
    workspace = config.PROJECTS_DIR / project["slug"]
    (workspace / "README.md").write_text("hi", encoding="utf-8")
    (workspace / "sub" / "attachments").mkdir(parents=True)
    (workspace / "sub" / "attachments" / "keep.txt").write_text("x", encoding="utf-8")

    root = {e.name for e in filetree.children(workspace)}
    assert "README.md" in root
    assert "attachments" not in root
    # Only the top-level one is special-cased: the same name deeper in the
    # tree is the project's own directory and stays visible.
    assert "attachments" in {e.name for e in filetree.children(workspace, "sub")}
    assert "keep.txt" in {
        e.name for e in filetree.children(workspace, "sub/attachments")
    }


def test_prompt_tells_the_agent_where_the_files_are(project):
    attachments.store(
        project["id"], project["slug"], "shot.png", PNG, "image/png", note="the broken layout"
    )
    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "## Attachments" in prompt
    assert "`attachments/0001-shot.png`" in prompt
    assert "the broken layout" in prompt


def test_prompt_has_no_attachments_heading_when_there_are_none(project):
    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "## Attachments" not in prompt


def test_prompt_truncates_a_long_note(project):
    attachments.store(
        project["id"], project["slug"], "a.png", PNG, "image/png", note="x" * 500
    )
    section = attachments.prompt_section(project["id"])
    assert "..." in section
    assert "x" * 200 not in section


# --- interaction with project deletion -------------------------------------

def test_deleting_a_project_drops_its_attachment_rows(client, project):
    attachments.store(project["id"], project["slug"], "shot.png", PNG, "image/png")
    client.post(
        f"/project/{project['slug']}/delete",
        data={"confirm": project["slug"], "delete_workspace": "on"},
    )
    assert db.get_project(project["id"]) is None
    assert db.list_attachments(project["id"]) == []
    assert not (config.PROJECTS_DIR / project["slug"]).exists()

"""Files attached to a new idea on the dashboard.

Wes, 2026-10-15: "Allow me to attach files when adding a new idea." They become
the new project's first attachments, named in its first journal note, exactly
as if they had been dropped on its note form afterwards.
"""
from __future__ import annotations

import io

import pytest
from starlette.testclient import TestClient

from app import db, worker

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
M4A = b"\x00\x00\x00\x20ftypM4A " + b"0" * 64


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def queued(monkeypatch):
    """Record manual runs instead of queueing them."""
    calls: list[int] = []

    async def fake_queue(project_id):
        calls.append(project_id)

    monkeypatch.setattr(worker, "queue_manual_run", fake_queue)
    return calls


@pytest.fixture
def kicked(monkeypatch):
    from app import main

    calls: list[tuple[list[int], bool]] = []

    def fake_kick(ids, after=None):
        calls.append((list(ids), after is not None))
        if after is not None:
            after.close()

    monkeypatch.setattr(main.transcribe, "kick", fake_kick)
    return calls


def _post(client, files, **data):
    return client.post("/ideas", data=data, files=files, follow_redirects=False)


def test_the_idea_form_can_carry_files(client):
    body = client.get("/").text
    form = body[body.index('action="/ideas"') - 200:]
    form = form[: form.index("</form>")]
    assert 'enctype="multipart/form-data"' in form
    assert "data-dropzone" in form
    assert 'type="file" name="files" multiple' in form
    assert "data-attach-shelf" in form
    # A file alone is an idea, so the browser must not refuse the press.
    textarea = form[form.index('<textarea id="idea"'):]
    assert "required" not in textarea[: textarea.index(">")]


def test_files_become_the_new_projects_attachments(client):
    r = _post(
        client,
        [("files", ("sketch.png", io.BytesIO(PNG), "image/png")),
         ("files", ("notes.txt", io.BytesIO(b"hello"), "text/plain"))],
        title="", idea="A dice tower",
    )
    assert r.status_code == 303
    p = db.get_project_by_slug("a-dice-tower")
    assert r.headers["location"] == f"/project/{p['slug']}"
    rows = db.list_attachments(p["id"])
    assert sorted(a["orig_name"] for a in rows) == ["notes.txt", "sketch.png"]
    # Captioned with the idea, and tied to the journal note that names them.
    notes = [j for j in db.list_journal(p["id"]) if j["kind"] == "note"]
    assert len(notes) == 1
    assert notes[0]["content_md"].startswith("A dice tower")
    assert "sketch.png" in notes[0]["content_md"]
    assert {a["journal_id"] for a in rows} == {notes[0]["id"]}
    assert {a["note"] for a in rows} == {"A dice tower"}


def test_a_file_alone_is_enough(client):
    r = _post(client, [("files", ("sketch.png", io.BytesIO(PNG), "image/png"))], title="", idea="")
    assert r.status_code == 303
    p = db.get_project_by_slug("untitled-idea")
    assert p is not None and p["stage"] == "backlog"
    assert len(db.list_attachments(p["id"])) == 1
    # The title is a placeholder, so an agent may still rename it.
    assert not p["title_locked"]


def test_nothing_at_all_files_nothing(client):
    before = len(db.list_projects())
    r = client.post("/ideas", data={"title": "t", "idea": "  "}, follow_redirects=False)
    assert r.status_code == 303
    assert not r.headers["location"].startswith("/project/")
    assert len(db.list_projects()) == before


def test_a_refused_file_does_not_cost_the_idea(client):
    r = _post(
        client,
        [("files", ("empty.txt", io.BytesIO(b""), "text/plain"))],
        title="", idea="Keep me",
    )
    assert r.status_code == 303
    p = db.get_project_by_slug("keep-me")
    assert db.list_attachments(p["id"]) == []
    note = [j for j in db.list_journal(p["id"]) if j["kind"] == "note"][0]
    assert "Rejected: empty.txt" in note["content_md"]


def test_start_building_with_files_queues_the_run(client, queued, kicked):
    _post(client, [("files", ("sketch.png", io.BytesIO(PNG), "image/png"))],
          title="", idea="Build it", then="plan")
    p = db.get_project_by_slug("build-it")
    assert queued == [p["id"]]
    assert kicked == []


def test_a_voice_memo_is_transcribed_before_the_first_run(client, queued, kicked):
    # The run rides on the transcription, so nothing is queued directly - the
    # memo may be the whole brief and the run must not start without its words.
    _post(client, [("files", ("memo.m4a", io.BytesIO(M4A), "audio/mp4"))],
          title="", idea="", then="plan")
    assert queued == []
    assert len(kicked) == 1 and kicked[0][1] is True


def test_a_voice_memo_on_a_backlog_idea_is_still_transcribed(client, queued, kicked):
    _post(client, [("files", ("memo.m4a", io.BytesIO(M4A), "audio/mp4"))], title="", idea="later")
    assert queued == []
    assert len(kicked) == 1 and kicked[0][1] is False

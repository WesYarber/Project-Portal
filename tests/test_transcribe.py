"""Voice memos become text: the whisper container wrapper, the storage, the
prompt, and the route that must not start a run before the words are in.

Wes, 2026-08-04: "It appears voice transcription for recorded audio files is
not set up, or maybe projects just don't know about it. Set that up."

The real engine is Docker + whisper.cpp (deploy/whisper/); no test here runs
it. Everything around it is what can quietly break: the transcript landing in
the DB and the sidecar, the prompt quoting it, failure being stored as loud
text rather than silence, and "add & run now" waiting for the transcript so
the run's prompt carries the memo's words.
"""
from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app import attachments, db, transcribe


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project(
        "Voice Project", stage="active", build_approved=True, slug="voice-project"
    )


def store_memo(project, name="memo.webm", mime="audio/webm", data=b"not-really-audio"):
    return attachments.store(project["id"], project["slug"], name, data, mime)


def prompt_section(project):
    """What a real prompt build would produce: reveal, then read.

    `attachments.prompt_section` names only files that are in the workspace, and
    `agent_runner.build_prompt` puts them there on the line above it. A test
    that skipped the reveal would be asserting against an empty string and
    passing on every negative it made - see
    `test_prompt_never_marks_non_audio_as_pending`, which did exactly that.
    """
    attachments.reveal(project["id"], project["slug"])
    return attachments.prompt_section(project["id"])


# --- what gets transcribed --------------------------------------------------

def test_only_audio_wants_a_transcript():
    assert transcribe.wants("audio/webm")
    assert transcribe.wants("audio/mp4")
    assert not transcribe.wants("video/mp4")
    assert not transcribe.wants("image/png")
    assert not transcribe.wants("")


# --- the container wrapper --------------------------------------------------

def fake_run(stdout=b"", stderr=b"", returncode=0):
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    return runner, calls


def test_transcribe_path_returns_the_text_and_contains_the_container(tmp_path, monkeypatch):
    audio = tmp_path / "memo.webm"
    audio.write_bytes(b"x")
    runner, calls = fake_run(stdout=b"  Hello there.\n")
    monkeypatch.setattr(transcribe.subprocess, "run", runner)
    assert transcribe.transcribe_path(audio) == "Hello there."
    cmd = calls[0]
    # The sandbox promises: no network, a memory cap, and the file read-only.
    assert "--network" in cmd and "none" in cmd
    assert "--memory" in cmd
    assert any(str(audio) in part and ":ro" in part for part in cmd)


def test_blank_audio_says_so_rather_than_returning_nothing(tmp_path, monkeypatch):
    audio = tmp_path / "memo.webm"
    audio.write_bytes(b"x")
    runner, _ = fake_run(stdout=b"[BLANK_AUDIO]\n")
    monkeypatch.setattr(transcribe.subprocess, "run", runner)
    assert transcribe.transcribe_path(audio) == "[no speech detected]"


def test_a_failed_container_raises_with_the_last_stderr_line(tmp_path, monkeypatch):
    audio = tmp_path / "memo.webm"
    audio.write_bytes(b"x")
    runner, _ = fake_run(stderr=b"noise\nmemo.webm: Invalid data found\n", returncode=1)
    monkeypatch.setattr(transcribe.subprocess, "run", runner)
    with pytest.raises(transcribe.TranscribeError, match="Invalid data found"):
        transcribe.transcribe_path(audio)


def test_a_hung_container_raises_rather_than_hanging_the_portal(tmp_path, monkeypatch):
    audio = tmp_path / "memo.webm"
    audio.write_bytes(b"x")

    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, transcribe.TIMEOUT_S)

    monkeypatch.setattr(transcribe.subprocess, "run", hang)
    with pytest.raises(transcribe.TranscribeError, match="gave up"):
        transcribe.transcribe_path(audio)


# --- one attachment, end to end ---------------------------------------------

def test_transcribe_attachment_writes_db_and_sidecar(project, monkeypatch):
    row = store_memo(project)
    monkeypatch.setattr(transcribe, "available", lambda: True)
    monkeypatch.setattr(transcribe, "transcribe_path", lambda p: "Buy more filament.")
    transcribe.transcribe_attachment(dict(db.get_attachment(row["id"])))
    assert db.get_attachment(row["id"])["transcript"] == "Buy more filament."
    # Beside the audio, which until a run reveals it is still in staging - a
    # stray .txt in a running agent's workspace is the same unexplained file
    # the audio itself is being held back to avoid.
    audio = attachments.disk_path(project["slug"], row["stored_name"])
    sidecar = transcribe.sidecar_path(project["slug"], row["stored_name"], audio)
    assert sidecar.parent == audio.parent
    assert sidecar.read_text() == "Buy more filament.\n"


def test_failure_is_stored_as_loud_text_not_silence(project, monkeypatch):
    row = store_memo(project)
    monkeypatch.setattr(transcribe, "available", lambda: True)

    def boom(path):
        raise transcribe.TranscribeError("ffmpeg choked")

    monkeypatch.setattr(transcribe, "transcribe_path", boom)
    transcribe.transcribe_attachment(dict(db.get_attachment(row["id"])))
    stored = db.get_attachment(row["id"])["transcript"]
    assert stored == "[transcription failed: ffmpeg choked]"
    # No sidecar for a failure: the agent should not Read an error message
    # where it expects the memo's words.
    assert not transcribe.sidecar_path(project["slug"], row["stored_name"]).exists()


def test_missing_engine_is_named_in_the_transcript_field(project):
    # conftest pins available() to False, which IS the missing-engine case.
    row = store_memo(project)
    transcribe.transcribe_attachment(dict(db.get_attachment(row["id"])))
    stored = db.get_attachment(row["id"])["transcript"]
    assert "transcription failed" in stored and "portal-whisper" in stored


# --- the backfill worklist --------------------------------------------------

def test_backfill_worklist_is_audio_without_transcript_only(project, monkeypatch):
    memo = store_memo(project)
    attachments.store(project["id"], project["slug"], "shot.png", b"png", "image/png")
    done = store_memo(project, name="done.m4a", mime="audio/mp4")
    db.set_attachment_transcript(done["id"], "already have it")
    failed = store_memo(project, name="failed.m4a", mime="audio/mp4")
    db.set_attachment_transcript(failed["id"], "[transcription failed: x]")
    rows = db.attachments_needing_transcript()
    # A row that failed is not retried forever at every boot; NULL means never
    # attempted, and that is the whole worklist.
    assert [r["id"] for r in rows] == [memo["id"]]
    assert rows[0]["project_slug"] == project["slug"]


def test_backfill_runs_the_worklist(project, monkeypatch):
    memo = store_memo(project)
    monkeypatch.setattr(transcribe, "available", lambda: True)
    seen = []
    monkeypatch.setattr(transcribe, "transcribe_attachment", lambda row: seen.append(row["id"]))

    class InlineThread:
        def __init__(self, target=None, **kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(transcribe.threading, "Thread", InlineThread)
    transcribe.backfill()
    assert seen == [memo["id"]]


# --- kick: transcribe first, then the continuation ---------------------------

def test_kick_finishes_transcription_before_the_continuation(project, monkeypatch):
    memo = store_memo(project)
    order = []
    monkeypatch.setattr(
        transcribe, "transcribe_attachment", lambda row: order.append(("transcribed", row["id"]))
    )

    async def continuation():
        order.append(("ran",))

    async def scenario():
        transcribe.kick([memo["id"]], after=continuation())
        for task in list(transcribe._TASKS):
            await task

    asyncio.run(scenario())
    assert order == [("transcribed", memo["id"]), ("ran",)]


def test_kick_runs_the_continuation_even_when_transcription_blows_up(project, monkeypatch):
    """transcribe_attachment is contractually no-raise, but if that contract
    ever breaks, losing the run the note asked for would be strictly worse
    than losing the transcript - so kick guards the continuation itself."""
    memo = store_memo(project)

    def boom(row):
        raise RuntimeError("contract broken")

    monkeypatch.setattr(transcribe, "transcribe_attachment", boom)
    ran = []

    async def continuation():
        ran.append(True)

    async def scenario():
        transcribe.kick([memo["id"]], after=continuation())
        for task in list(transcribe._TASKS):
            await task

    asyncio.run(scenario())
    assert ran == [True]


# --- the prompt --------------------------------------------------------------

def test_prompt_quotes_the_transcript(project):
    row = store_memo(project)
    db.set_attachment_transcript(row["id"], "Center the buttons.\nAnd make them green.")
    section = prompt_section(project)
    assert "  > Center the buttons." in section
    assert "  > And make them green." in section


def test_prompt_marks_a_transcription_still_running(project):
    store_memo(project)
    section = prompt_section(project)
    assert "transcription is still running" in section


def test_prompt_never_marks_non_audio_as_pending(project):
    attachments.store(project["id"], project["slug"], "shot.png", b"png", "image/png")
    section = prompt_section(project)
    assert "transcription" not in section


def test_a_long_transcript_is_clipped_and_points_at_the_sidecar(project):
    row = store_memo(project)
    db.set_attachment_transcript(row["id"], "word " * 800)
    section = prompt_section(project)
    assert "word " * 800 not in section
    assert f"attachments/{row['stored_name']}.txt" in section


# --- the route: a memo's words go into the run it triggers -------------------

def test_add_note_with_audio_defers_the_run_until_transcribed(client, project, monkeypatch):
    from app import main, worker

    kicked = {}

    def fake_kick(ids, after=None):
        kicked["ids"] = ids
        kicked["after"] = after

    monkeypatch.setattr(main.transcribe, "kick", fake_kick)
    queued = []

    async def fake_queue(pid):
        queued.append(pid)

    monkeypatch.setattr(worker, "queue_manual_run", fake_queue)
    client.post(
        f"/project/{project['slug']}/note",
        data={"note": "listen to this", "then": "run"},
        files={"files": ("memo.webm", b"blob", "audio/webm;codecs=opus")},
    )
    row = db.list_attachments(project["id"])[0]
    assert kicked["ids"] == [row["id"]]
    # The run was NOT queued on the request path - it is the continuation, and
    # runs only after the transcript is stored.
    assert queued == []
    asyncio.run(kicked["after"])
    assert queued == [project["id"]]


def test_add_note_without_audio_queues_the_run_directly(client, project, monkeypatch):
    from app import main, worker

    def no_kick(ids, after=None):  # pragma: no cover - the assertion is the call
        raise AssertionError("kick has no business in a text-only note")

    monkeypatch.setattr(main.transcribe, "kick", no_kick)
    queued = []

    async def fake_queue(pid):
        queued.append(pid)

    monkeypatch.setattr(worker, "queue_manual_run", fake_queue)
    client.post(
        f"/project/{project['slug']}/note",
        data={"note": "plain words", "then": "run"},
    )
    assert queued == [project["id"]]


# --- the page ----------------------------------------------------------------

def test_project_page_shows_the_transcript_under_the_player(client, project):
    row = store_memo(project)
    db.set_attachment_transcript(row["id"], "The words of the memo.")
    body = client.get(f"/project/{project['slug']}").text
    assert "The words of the memo." in body
    assert "attach-transcript" in body


def test_project_page_says_transcribing_while_the_text_is_pending(client, project):
    store_memo(project)
    body = client.get(f"/project/{project['slug']}").text
    assert "transcribing" in body


def test_project_page_wears_the_failure_in_red(client, project):
    row = store_memo(project)
    db.set_attachment_transcript(row["id"], "[transcription failed: no container]")
    body = client.get(f"/project/{project['slug']}").text
    assert "[transcription failed: no container]" in body
    assert "attach-transcript small error" in body


def test_note_form_has_the_recorder_panel_and_the_mic_glyph(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert "data-rec-panel" in body and "data-rec-shelf" in body
    assert "data-rec-time" in body and "data-rec-pause" in body
    # The mic is an SVG inside the record button, not an emoji.
    assert "data-record" in body
    assert body.index("<svg", body.index("data-record")) < body.index("</button>", body.index("data-record"))

"""A run that outlives the portal process still gets its report filed.

Runs 1441 and 1442 on 2026-09-02 both committed real work and both filed a full
report through StructuredOutput - and both were settled as "error: nothing was
watching when it finished", because the service had restarted under them and
their stdout went down a dead pipe. The CLI's transcript on disk carries the
same report, so `worker._reap_adopted` now reads it back (app/transcript.py)
and settles the run the way a watched one settles.

Every claim below is checked by deleting the fix and watching this file fail.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import climemory, config, db, oneoff, runlimit, transcript, worker


@pytest.fixture(autouse=True)
def _clean_worker_state():
    worker._inflight.clear()
    worker._lease_free_since.clear()
    yield
    worker._inflight.clear()
    worker._lease_free_since.clear()


@pytest.fixture
def project():
    return db.create_project("Alpha", stage="active", build_approved=True, slug="alpha")


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "projects" / "alpha"
    ws.mkdir(parents=True)
    return ws


REPORT = {
    "summary": ["the strip shows what the agent said", "note: live at the next restart"],
    "journal_entry_md": "## What the agent said\n\nBuilt and committed.",
    "new_stage": None,
    "request_build": False,
    "blocked_on": None,
    "kind": None,
    "title": None,
    "description": None,
    "questions": [],
    "todo_updates": {"add": [], "done": [], "tags": {}},
    "preview_url": None,
    "learnings": [],
    "suggestion": None,
}


def _stamp(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _lines(session: str, base: datetime, report=REPORT, reply="All done.", extra=()):
    """A transcript the shape the CLI writes: an enqueue line, a user prompt,
    assistant turns split one content block per line, and the odd bookkeeping
    record the reader has no use for."""
    yield {"type": "queue-operation", "operation": "enqueue", "timestamp": _stamp(base, 1),
           "sessionId": session, "content": "Task: BUILD."}
    yield {"type": "user", "sessionId": session, "timestamp": _stamp(base, 2),
           "message": {"role": "user", "content": "Task: BUILD."}}
    yield {"type": "assistant", "sessionId": session, "timestamp": _stamp(base, 30),
           "message": {"id": "msg_1", "role": "assistant",
                       "content": [{"type": "text", "text": "Starting."}]}}
    yield {"type": "assistant", "sessionId": session, "timestamp": _stamp(base, 31),
           "message": {"id": "msg_1", "role": "assistant",
                       "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                    "input": {"command": "ls"}}]}}
    yield {"type": "atis-latch", "atis": "", "sessionId": session}
    for event in extra:
        yield event
    yield {"type": "assistant", "sessionId": session, "timestamp": _stamp(base, 500),
           "message": {"id": "msg_2", "role": "assistant",
                       "content": [{"type": "text", "text": reply}]}}
    if report is not None:
        yield {"type": "assistant", "sessionId": session, "timestamp": _stamp(base, 501),
               "message": {"id": "msg_2", "role": "assistant",
                           "content": [{"type": "tool_use", "id": "t2",
                                        "name": "StructuredOutput", "input": report}]}}


def _write(cwd: Path, session: str, base: datetime, **kw) -> Path:
    directory = config.cli_projects_dir() / climemory.encode_cwd(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for event in _lines(session, base, **kw):
            fh.write(json.dumps(event) + "\n")
    return path


def _adopted_run(project_id, workspace, session=None) -> int:
    """A run some other portal process started: a scope name minted by a pid
    that is not ours, the workspace leased, and - as every run before the fix -
    no session id on the row unless the test says so."""
    run_id = db.create_run(project_id, "build", "claude-fable-5-1")
    db.set_run_scope(run_id, f"portal-run-{run_id}-{os.getpid() + 1}-1.scope")
    if workspace is not None:
        db.set_run_lease(run_id, str(workspace))
    if session:
        db.set_run_session(run_id, session)
    return run_id


def _started(run_id) -> datetime:
    return datetime.fromisoformat(db.get_run(run_id)["started_at"])


def _scope_gone(monkeypatch):
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: False)


def _journal(project_id):
    return [
        (r["author"], r["kind"], r["content_md"])
        for r in db.get_conn().execute(
            "SELECT author, kind, content_md FROM journal WHERE project_id = ? ORDER BY id",
            (project_id,),
        )
    ]


# --- the reader --------------------------------------------------------------


def test_the_report_is_read_out_of_the_structured_output_call(tmp_path):
    base = datetime(2026, 9, 2, 17, 28, tzinfo=timezone.utc)
    path = _write(tmp_path / "ws", "sess-1", base)
    rec = transcript.read(path)
    assert rec.report == REPORT
    assert rec.session_id == "sess-1"
    assert rec.reply == "All done."
    assert rec.turns == 2  # msg_1 and msg_2, however many lines each was split over
    assert rec.ended_at == _stamp(base, 501)


def test_a_transcript_with_no_report_says_so(tmp_path):
    base = datetime(2026, 9, 2, tzinfo=timezone.utc)
    rec = transcript.read(_write(tmp_path / "ws", "s", base, report=None))
    assert rec.report is None
    assert rec.reply == "All done."


def test_the_last_report_filed_wins(tmp_path):
    base = datetime(2026, 9, 2, tzinfo=timezone.utc)
    first = dict(REPORT, summary=["the first attempt"])
    extra = [{"type": "assistant", "sessionId": "s", "timestamp": _stamp(base, 100),
              "message": {"id": "msg_x", "role": "assistant",
                          "content": [{"type": "tool_use", "id": "t0",
                                       "name": "StructuredOutput", "input": first}]}}]
    rec = transcript.read(_write(tmp_path / "ws", "s", base, extra=extra))
    assert rec.report == REPORT


def test_a_subagents_turns_are_not_the_runs(tmp_path):
    base = datetime(2026, 9, 2, tzinfo=timezone.utc)
    side = [{"type": "assistant", "isSidechain": True, "sessionId": "s",
             "timestamp": _stamp(base, 400),
             "message": {"id": "msg_side", "role": "assistant",
                         "content": [{"type": "text", "text": "subagent words"},
                                     {"type": "tool_use", "id": "t9", "name": "StructuredOutput",
                                      "input": {"summary": ["a subagent's report"]}}]}}]
    rec = transcript.read(_write(tmp_path / "ws", "s", base, report=None, extra=side))
    assert rec.report is None
    assert rec.reply == "All done."
    assert rec.turns == 2


def test_unreadable_lines_are_skipped_not_fatal(tmp_path):
    base = datetime(2026, 9, 2, tzinfo=timezone.utc)
    path = _write(tmp_path / "ws", "s", base)
    text = path.read_text()
    lines = text.splitlines()
    lines.insert(2, '{"type": "assistant", "message": {"content": [')  # a torn write
    lines.append('{"half')
    path.write_text("\n".join(lines) + "\n")
    rec = transcript.read(path)
    assert rec.report == REPORT
    assert rec.unreadable_lines == 2


# --- locating the file -------------------------------------------------------


def test_a_session_id_names_the_file_outright(tmp_path):
    base = datetime(2026, 9, 2, tzinfo=timezone.utc)
    path = _write(tmp_path / "ws", "sess-a", base)
    _write(tmp_path / "ws", "sess-b", base)  # same window, would be ambiguous by time
    assert transcript.locate(tmp_path / "ws", "sess-a", base.isoformat()) == path


def test_a_session_id_with_no_file_finds_nothing_rather_than_guessing(tmp_path):
    base = datetime(2026, 9, 2, tzinfo=timezone.utc)
    _write(tmp_path / "ws", "sess-a", base)
    assert transcript.locate(tmp_path / "ws", "sess-missing", base.isoformat()) is None


def test_without_a_session_id_the_one_transcript_in_the_start_window_is_it(tmp_path):
    base = datetime(2026, 9, 2, 17, 28, 9, tzinfo=timezone.utc)
    path = _write(tmp_path / "ws", "sess-1442", base)  # first line stamped base+1s
    _write(tmp_path / "ws", "sess-old", base - timedelta(hours=1))
    _write(tmp_path / "ws", "sess-later", base + timedelta(hours=1))
    assert transcript.locate(tmp_path / "ws", None, base.isoformat()) == path


def test_the_window_is_narrow_on_both_sides(tmp_path):
    base = datetime(2026, 9, 2, 17, 28, 9, tzinfo=timezone.utc)
    _write(tmp_path / "ws", "just-before", base - transcript.START_WINDOW_BEFORE - timedelta(seconds=2))
    _write(tmp_path / "ws", "just-after", base + transcript.START_WINDOW_AFTER)
    assert transcript.locate(tmp_path / "ws", None, base.isoformat()) is None


def test_two_transcripts_in_the_window_is_a_refusal(tmp_path):
    base = datetime(2026, 9, 2, tzinfo=timezone.utc)
    _write(tmp_path / "ws", "a", base)
    _write(tmp_path / "ws", "b", base + timedelta(seconds=5))
    assert transcript.locate(tmp_path / "ws", None, base.isoformat()) is None


def test_the_directory_is_the_clis_encoding_of_the_cwd(tmp_path):
    cwd = Path("/home/ada/project-portal/data/projects/project-portal")
    assert transcript.transcript_dir(cwd, tmp_path) == (
        tmp_path / "-home-ada-project-portal-data-projects-project-portal"
    )


def test_a_transcript_directory_that_does_not_exist_is_simply_nothing(tmp_path):
    assert transcript.recover(tmp_path / "nowhere", None, "2026-09-02T00:00:00+00:00") is None


# --- the session id is recorded at the first event ---------------------------


def test_the_init_event_records_the_session_id(project):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    on_event = worker._live_logger(run_id)
    on_event({"type": "system", "subtype": "init", "session_id": "sess-init"}, ["* session start"])
    assert db.get_run(run_id)["session_id"] == "sess-init"


def test_other_events_record_no_session(project):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    on_event = worker._live_logger(run_id)
    on_event({"type": "assistant", "session_id": "sess-x"}, ["words"])
    on_event({"type": "system", "subtype": "api_retry", "session_id": "sess-x"}, [])
    assert db.get_run(run_id)["session_id"] is None


def test_a_settle_without_a_session_keeps_the_recorded_one(project):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    db.set_run_session(run_id, "sess-kept")
    db.finish_run(run_id, "error", summary="died")
    assert db.get_run(run_id)["session_id"] == "sess-kept"


def test_a_settle_with_a_session_still_writes_it(project):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    db.finish_run(run_id, "ok", "sess-final", 1.0, 3, "fine")
    assert db.get_run(run_id)["session_id"] == "sess-final"


def test_the_first_recorded_session_id_is_not_overwritten(project):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    db.set_run_session(run_id, "sess-first")
    db.set_run_session(run_id, "sess-second")
    assert db.get_run(run_id)["session_id"] == "sess-first"


# --- the settle ----------------------------------------------------------------


def test_an_adopted_run_whose_scope_died_is_filed_from_its_transcript(
    project, workspace, monkeypatch
):
    """The fix, on the exact shape of run 1442: no session id on the row, the
    scope gone, the transcript in the workspace's directory."""
    run_id = _adopted_run(project["id"], workspace)
    _write(workspace, "sess-1442", _started(run_id))
    _scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "ok"
    assert row["summary"] == "; ".join(REPORT["summary"])
    assert row["session_id"] == "sess-1442"
    assert row["num_turns"] == 2
    assert row["report_summary"].split("\n") == REPORT["summary"]
    entries = _journal(project["id"])
    assert ("agent", "progress", REPORT["journal_entry_md"]) in entries
    assert entries[-1][0:2] == ("system", "status")
    assert entries[-1][2] == worker.RECOVERED_NOTE
    assert project["id"] not in db.running_project_ids()


def test_a_recorded_session_id_finds_the_transcript_by_name(project, workspace, monkeypatch):
    run_id = _adopted_run(project["id"], workspace, session="sess-named")
    started = _started(run_id)
    _write(workspace, "sess-named", started, report=dict(REPORT, summary=["the named one"]))
    _write(workspace, "sess-other", started + timedelta(seconds=3))  # by time it would be ambiguous
    _scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "ok"
    assert row["summary"] == "the named one"


def test_no_transcript_settles_as_before(project, workspace, monkeypatch):
    run_id = _adopted_run(project["id"], workspace)
    _scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "error"
    assert row["summary"] == worker.ADOPTED_SUMMARY
    assert _journal(project["id"]) == []


def test_a_transcript_without_a_report_is_still_a_failed_run(project, workspace, monkeypatch):
    """An agent killed mid-work has a transcript and last words but no report.
    Its last words are not a success."""
    run_id = _adopted_run(project["id"], workspace)
    _write(workspace, "sess-cut", _started(run_id), report=None, reply="Now running the tests.")
    _scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "error"
    assert row["summary"] == worker.ADOPTED_SUMMARY
    assert row["session_id"] == "sess-cut"  # the handle is kept even so
    assert not any(kind == "progress" for _, kind, _ in _journal(project["id"]))


def test_a_stranded_run_is_filed_too_and_says_what_it_left(project, workspace, monkeypatch):
    run_id = _adopted_run(project["id"], workspace)
    _write(workspace, "sess-str", _started(run_id))
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: True)
    monkeypatch.setattr(worker.worklock, "is_busy", lambda lock_dir: False)
    monkeypatch.setattr(worker, "LEASE_FREE_CONFIRM_S", 0.0)

    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "ok"
    note = _journal(project["id"])[-1][2]
    assert note.startswith(worker.RECOVERED_NOTE)
    assert worker.STRANDED_NOTE in note


def test_a_stranded_run_with_no_transcript_keeps_the_stranded_summary(
    project, workspace, monkeypatch
):
    run_id = _adopted_run(project["id"], workspace)
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: True)
    monkeypatch.setattr(worker.worklock, "is_busy", lambda lock_dir: False)
    monkeypatch.setattr(worker, "LEASE_FREE_CONFIRM_S", 0.0)

    worker._reap_adopted()

    assert db.get_run(run_id)["summary"] == worker.STRANDED_SUMMARY


def test_the_report_reaches_the_project_the_way_a_watched_one_does(
    project, workspace, monkeypatch
):
    """`_apply_report` is the same door: a recovered report's stage move and
    todo land on the project, not only its journal entry."""
    run_id = _adopted_run(project["id"], workspace)
    report = dict(
        REPORT,
        new_stage="review",
        todo_updates={"add": [{"text": "Check the strip on the phone", "owner": "user"}],
                      "done": [], "tags": {}},
    )
    _write(workspace, "sess-r", _started(run_id), report=report)
    _scope_gone(monkeypatch)

    worker._reap_adopted()

    assert db.get_project(project["id"])["stage"] == "review"
    texts = [t["text"] for t in db.list_todos(project["id"])]
    assert "Check the strip on the phone" in texts


def test_a_run_without_a_lease_is_looked_up_in_its_project_workspace(
    project, monkeypatch
):
    run_id = _adopted_run(project["id"], None)
    _write(config.PROJECTS_DIR / "alpha", "sess-nolease", _started(run_id))
    _scope_gone(monkeypatch)

    worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "ok"


def test_a_broken_transcript_read_still_settles_the_row(project, workspace, monkeypatch):
    run_id = _adopted_run(project["id"], workspace)
    _scope_gone(monkeypatch)

    def boom(*a, **k):
        raise OSError("disk went away")

    monkeypatch.setattr(worker.transcript, "recover", boom)
    worker._reap_adopted()
    assert db.get_run(run_id)["status"] == "error"


def test_this_process_own_runs_are_never_recovered_from_disk(project, workspace, monkeypatch):
    """A run we are watching settles through its own stream. Reading its
    transcript mid-flight would file a half-written report."""
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    db.set_run_scope(run_id, f"portal-run-{run_id}-{os.getpid()}-1.scope")
    db.set_run_lease(run_id, str(workspace))
    worker._inflight[run_id] = object()
    _write(workspace, "sess-mine", _started(run_id))
    _scope_gone(monkeypatch)

    worker._reap_adopted()

    assert db.get_run(run_id)["status"] == "running"


# --- one-off tasks ---------------------------------------------------------------


def test_a_one_off_tasks_reply_is_recovered_onto_its_thread(monkeypatch):
    task = db.create_oneoff("What is on the office machine?")
    ws = oneoff.workspace(int(task["id"]))
    ws.mkdir(parents=True)
    run_id = db.create_run(None, "oneoff", "claude-fable-5-1", oneoff_id=int(task["id"]))
    db.set_run_scope(run_id, f"portal-run-{run_id}-{os.getpid() + 1}-2.scope")
    db.set_run_lease(run_id, str(ws))
    _write(ws, "sess-task", _started(run_id), report=None, reply="Merged office-node and restarted.")
    _scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "ok"
    assert row["summary"] == "Merged office-node and restarted."
    messages = [(m["role"], m["content_md"]) for m in db.list_oneoff_messages(int(task["id"]))]
    assert ("agent", "Merged office-node and restarted.") in messages
    assert messages[-1][0] == "system" and messages[-1][1] == worker.RECOVERED_NOTE
    assert db.get_oneoff(int(task["id"]))["cli_session_id"] == "sess-task"


def test_a_one_off_with_no_transcript_settles_as_before(monkeypatch):
    task = db.create_oneoff("hello")
    run_id = db.create_run(None, "oneoff", "claude-fable-5-1", oneoff_id=int(task["id"]))
    db.set_run_scope(run_id, f"portal-run-{run_id}-{os.getpid() + 1}-2.scope")
    _scope_gone(monkeypatch)

    worker._reap_adopted()

    row = db.get_run(run_id)
    assert row["status"] == "error"
    assert row["summary"] == worker.ADOPTED_SUMMARY
    assert all(
        worker.RECOVERED_NOTE not in m["content_md"]
        for m in db.list_oneoff_messages(int(task["id"]))
    )

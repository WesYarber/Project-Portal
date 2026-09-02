"""A recovered run's page shows the whole run, not the half the portal saw.

The portal's log of a run (`data/runs/<id>.log`) is written from the run's
stdout by the process that started it. When the service restarts under a run,
the log stops at the restart even though the run kept going and (since
2026-09-02) had its report recovered from the CLI's transcript. The run page
showed the first half of the run and nothing after. The CLI's session file has
every turn in the same event shape, so `transcript.render` draws it through
`runlog.render_event`, and `main._render_run_page` uses it whenever a finished
run's own log never reached its closing line.

Every claim below is checked by deleting the fix and watching this file fail.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import climemory, config, db, oneoff, parallel, runlog, transcript


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project("Alpha", stage="active", build_approved=True, slug="alpha")


BASE = datetime(2026, 9, 2, 17, 28, tzinfo=timezone.utc)

REPORT = {"summary": ["built the thing"], "journal_entry_md": "## Built\n\nDone."}


def _stamp(seconds: float, base: datetime = BASE) -> str:
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _turn(session, msg_id, content, seconds, sidechain=False, base: datetime = BASE):
    return {
        "type": "assistant", "sessionId": session, "timestamp": _stamp(seconds, base),
        "isSidechain": sidechain,
        "message": {"id": msg_id, "role": "assistant", "content": content},
    }


def _events(session, report=REPORT, extra=(), base: datetime = BASE):
    yield {"type": "queue-operation", "operation": "enqueue", "timestamp": _stamp(1, base),
           "sessionId": session, "content": "Task: BUILD."}
    yield {"type": "user", "sessionId": session, "timestamp": _stamp(2, base),
           "message": {"role": "user", "content": "Task: BUILD."}}
    yield _turn(session, "msg_1", [{"type": "text", "text": "Looking at the log first."}], 30)
    yield _turn(session, "msg_1", [{"type": "tool_use", "id": "t1", "name": "Bash",
                                    "input": {"command": "ls data/runs"}}], 31)
    yield {"type": "user", "sessionId": session, "timestamp": _stamp(32),
           "message": {"role": "user", "content": [
               {"type": "tool_result", "tool_use_id": "t1", "content": "1.log\n2.log"}]}}
    yield {"type": "ai-title", "aiTitle": "Execute next chunk", "sessionId": session}
    for event in extra:
        yield event
    yield _turn(session, "msg_2", [{"type": "text", "text": "AFTER THE RESTART: still working."}], 900)
    if report is not None:
        yield _turn(session, "msg_2", [{"type": "tool_use", "id": "t2",
                                        "name": "StructuredOutput", "input": report}], 901)


def _write(cwd: Path, session: str, **kw) -> Path:
    directory = config.cli_projects_dir() / climemory.encode_cwd(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for event in _events(session, **kw):
            fh.write(json.dumps(event) + "\n")
    return path


def _finished_run(project, session="sess-1", status="ok", log_lines=None, log=True):
    """A run whose portal log stops mid-run: the shape a restart leaves."""
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    if log:
        live = runlog.RunLog(run_id)
        live.append(log_lines if log_lines is not None else [
            "* session start  model=claude-fable-5-1  tools=20",
            "Looking at the log first.",
            "> Bash(ls data/runs)",
            "< ok (2 lines)",
        ])
    db.finish_run(run_id, status, session, None, 2, "built the thing")
    return run_id


# --- is the log the whole story? ---------------------------------------------


@pytest.mark.parametrize("closing", ["* run complete  (41 turns)", "! run failed - hit the turn limit"])
def test_a_log_ending_on_the_result_line_is_complete(closing):
    assert runlog.is_complete("* session start\n> Bash(ls)\n< ok (1 line)\n" + closing + "\n")


def test_trailing_blank_lines_do_not_hide_the_closing_line():
    assert runlog.is_complete("> Bash(ls)\n* run complete\n\n\n")


def test_a_log_that_stops_on_a_tool_call_is_not_complete():
    assert not runlog.is_complete("* session start\n> Bash(ls)\n< ok (1 line)\n")


def test_prose_mentioning_completion_is_not_the_closing_line():
    # The agent's own words are unmarked; only the STATUS line the CLI's
    # result event renders to counts.
    assert not runlog.is_complete("> Bash(ls)\n run complete, I think\n")


def test_an_empty_log_is_not_complete():
    assert not runlog.is_complete("")


# --- drawing a transcript ------------------------------------------------------


def test_the_transcript_is_drawn_the_way_the_live_log_is_drawn(tmp_path):
    path = _write(tmp_path / "ws", "sess-1")
    out = transcript.render(path)
    lines = out.text.splitlines()
    assert "Looking at the log first." in lines, "prose unmarked and unindented"
    assert "> Bash(ls data/runs)" in lines, "tool calls with the tool marker"
    assert "< ok (2 lines)" in lines, "results counted as the live logger counts them"
    assert "AFTER THE RESTART: still working." in lines, "the half the portal never saw"
    assert "Task: BUILD." not in out.text, "the prompt echo is not a transcript line"


def test_the_first_line_says_where_the_transcript_came_from(tmp_path):
    path = _write(tmp_path / "ws", "sess-1")
    first = transcript.render(path).text.splitlines()[0]
    assert first.startswith("* ")
    assert "session file" in first


def test_a_filed_report_closes_the_transcript_as_complete(tmp_path):
    path = _write(tmp_path / "ws", "sess-1")
    out = transcript.render(path)
    assert out.has_report is True
    assert out.turns == 2
    assert out.text.splitlines()[-1] == "* run complete  (2 turns, report filed)"


def test_a_transcript_without_a_report_says_so_rather_than_complete(tmp_path):
    path = _write(tmp_path / "ws", "sess-1", report=None)
    out = transcript.render(path)
    assert out.has_report is False
    last = out.text.splitlines()[-1]
    assert "without a report" in last
    assert "run complete" not in last


def test_a_subagents_turns_are_not_drawn(tmp_path):
    side = _turn("sess-1", "msg_side", [{"type": "text", "text": "SUBAGENT SAYS HI"}], 100,
                 sidechain=True)
    path = _write(tmp_path / "ws", "sess-1", extra=[side])
    out = transcript.render(path)
    assert "SUBAGENT SAYS HI" not in out.text
    assert out.turns == 2


def test_a_torn_line_is_skipped_not_fatal(tmp_path):
    # Torn in the MIDDLE: a line at the end of the file could be one the CLI is
    # still writing, and stopping there would look the same as skipping it.
    path = _write(tmp_path / "ws", "sess-1")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines.insert(3, '{"type": "assistant", "message": {"content": [{"type": "te\n')
    path.write_text("".join(lines), encoding="utf-8")
    out = transcript.render(path)
    assert "Looking at the log first." in out.text
    assert "AFTER THE RESTART" in out.text


def test_a_missing_file_renders_nothing(tmp_path):
    out = transcript.render(tmp_path / "nope.jsonl")
    assert out.text == ""


# --- where a finished run's transcript lives ----------------------------------


def test_a_project_run_ran_in_its_workspace(project):
    run_id = _finished_run(project)
    row = db.get_run_with_project(run_id)
    assert transcript.run_cwd(row) == config.PROJECTS_DIR / "alpha"


def test_a_project_run_is_found_without_the_joined_slug(project):
    run_id = _finished_run(project)
    assert transcript.run_cwd(db.get_run(run_id)) == config.PROJECTS_DIR / "alpha"


def test_a_parallel_run_ran_in_its_worktree(project):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1", parallel=True)
    db.finish_run(run_id, "ok", "sess-p", None, 1, "x")
    row = db.get_run_with_project(run_id)
    assert transcript.run_cwd(row) == parallel.worktree_for("alpha", run_id)


def test_a_one_off_task_ran_in_its_own_directory():
    task_id = db.create_oneoff("Know the other portals: probe them")["id"]
    run_id = db.create_run(None, "oneoff", "claude-fable-5-1", oneoff_id=task_id)
    row = db.get_run_with_project(run_id)
    assert transcript.run_cwd(row) == oneoff.workspace(task_id)


def test_a_run_with_neither_has_nowhere_to_look():
    run_id = db.create_run(None, "reflect", "claude-fable-5-1")
    assert transcript.run_cwd(db.get_run_with_project(run_id)) is None
    assert transcript.render_for_run(db.get_run_with_project(run_id)) is None


def test_no_transcript_means_nothing_to_draw(project):
    run_id = _finished_run(project)
    assert transcript.render_for_run(db.get_run_with_project(run_id)) is None


# --- the page ------------------------------------------------------------------


def test_a_recovered_runs_page_shows_the_whole_run(project, client):
    run_id = _finished_run(project)
    _write(config.PROJECTS_DIR / "alpha", "sess-1")

    body = client.get(f"/run/{run_id}").text
    assert "AFTER THE RESTART: still working." in body
    assert "recovered transcript" in body
    assert 'id="console-source"' in body, "the page says where the text came from"
    assert "no longer on disk" not in body


def test_a_run_whose_log_is_whole_keeps_its_own_log(project, client):
    run_id = _finished_run(project, log_lines=[
        "* session start  model=claude-fable-5-1  tools=20",
        "Looking at the log first.",
        "* run complete  (2 turns, 0.100w)",
    ])
    _write(config.PROJECTS_DIR / "alpha", "sess-1")

    body = client.get(f"/run/{run_id}").text
    assert "AFTER THE RESTART" not in body
    assert "finished transcript" in body
    assert 'id="console-source"' not in body


def test_a_running_run_keeps_its_live_log_even_with_a_transcript_beside_it(project, client):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    runlog.RunLog(run_id).append(["* session start", "> Bash(ls)"])
    db.set_run_session(run_id, "sess-1")
    _write(config.PROJECTS_DIR / "alpha", "sess-1")

    body = client.get(f"/run/{run_id}").text
    assert "AFTER THE RESTART" not in body, "the live poller tails the log, so the page must too"
    assert "live transcript" in body
    assert 'data-live="1"' in body


def test_a_pruned_log_with_a_transcript_shows_the_transcript(project, client):
    run_id = _finished_run(project, log=False)
    _write(config.PROJECTS_DIR / "alpha", "sess-1")

    body = client.get(f"/run/{run_id}").text
    assert "AFTER THE RESTART" in body
    assert "no longer on disk" not in body


def test_a_pruned_log_with_no_transcript_still_says_pruned(project, client):
    run_id = _finished_run(project, log=False)
    body = client.get(f"/run/{run_id}").text
    assert "no longer on disk" in body
    assert 'id="console-source"' not in body


def test_a_run_without_a_session_id_is_found_by_its_start_window(project, client):
    run_id = _finished_run(project, session=None)
    started = datetime.fromisoformat(db.get_run(run_id)["started_at"])
    _write(config.PROJECTS_DIR / "alpha", "sess-window", base=started.replace(tzinfo=timezone.utc))

    body = client.get(f"/run/{run_id}").text
    assert "AFTER THE RESTART" in body


def test_a_failing_render_leaves_the_page_standing(project, client, monkeypatch):
    run_id = _finished_run(project)
    _write(config.PROJECTS_DIR / "alpha", "sess-1")

    def boom(row):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(transcript, "render_for_run", boom)
    response = client.get(f"/run/{run_id}")
    assert response.status_code == 200
    assert "Looking at the log first." in response.text
    assert 'id="console-source"' not in response.text

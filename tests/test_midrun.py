"""Pausing a live run, and handing it a note while it works (app/midrun.py).

Wes, 2026-09-02: "is it possible to pause runs? with no or minimal token
waste? or sort of add in additional context while the model is still running
as well?"

Both ride the PostToolUse relay: a paused run's relay is told to poll instead
of answering, so the CLI sits between turns spending nothing; a note typed
mid-run rides back as the hook's additionalContext and is stamped delivered to
that run so no second run is queued for it. These tests pin the hold state
machine, the answers the endpoints give the relay, the relay's poll loop, the
supervisor's pause-aware deadline, the "queue note" exemption, the report-tool
rule, and the pages.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import agent_runner, attachments, config, db, hookguard, hookrelay, midrun, notes, sidebar, worker

from tests.test_stream import fake_claude  # noqa: F401 - fixture reuse


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _project(slug="game"):
    return db.get_project_by_slug(slug) or db.create_project("Game", stage="active", slug=slug)


def _live_run(project=None, *, midrun_on=True, tmp_path=None):
    """A run row in flight, with a hook scope registered the way the worker
    registers one. Returns (run_id, token)."""
    project = project or _project()
    run_id = db.create_run(project["id"], "build", "opus")
    ws = (tmp_path or Path(config.PROJECTS_DIR)) / project["slug"]
    ws.mkdir(parents=True, exist_ok=True)
    hookguard.begin(run_id, [ws], pre_tool=False, audit=True, midrun=midrun_on)
    return run_id, hookguard._SCOPES[run_id].token  # noqa: SLF001


def _payload(tool="Bash"):
    return {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {"command": "ls"}}


def _events(run_id):
    return [(r["tool"], r["decision"]) for r in db.midrun_events_for_run(run_id)]


def _status_lines(project_id):
    return [r["content_md"] for r in db.list_journal_asc(project_id, limit=50)
            if r["author"] == "system" and r["kind"] == "status"]


# --- settings JSON ------------------------------------------------------------

def test_begin_with_midrun_installs_a_long_lived_post_tool_hook(tmp_path):
    settings = hookguard.begin(30, [tmp_path], audit=False, pre_tool=False, midrun=True)
    try:
        entry = json.loads(settings)["hooks"]["PostToolUse"][0]["hooks"][0]
        assert "/hooks/post-tool?run=30" in entry["command"]
        assert entry["timeout"] == midrun.HOOK_TIMEOUT_SEC
        assert hookguard.hears_midrun(30) is True
    finally:
        hookguard.end(30)


def test_begin_without_midrun_keeps_the_short_timeout(tmp_path):
    settings = hookguard.begin(31, [tmp_path], audit=True, pre_tool=False, midrun=False)
    try:
        entry = json.loads(settings)["hooks"]["PostToolUse"][0]["hooks"][0]
        assert entry["timeout"] == 15
        assert hookguard.hears_midrun(31) is False
    finally:
        hookguard.end(31)


def test_hook_timeout_outlasts_any_hold_wes_would_leave():
    # Six hours: a hold longer than that is a run he meant to stop. The CLI
    # kills a hook past its timeout and carries on, which would silently end
    # the pause - so the number is the pause's real upper bound.
    assert midrun.HOOK_TIMEOUT_SEC >= 6 * 3600


def test_worker_passes_the_midrun_flag_and_the_setting_turns_it_off(temp_data_dir):
    project = _project()
    settings = worker._guard_settings(40, project)  # noqa: SLF001
    try:
        assert hookguard._SCOPES[40].midrun is True  # noqa: SLF001
        assert "PostToolUse" in json.loads(settings)["hooks"]
    finally:
        hookguard.end(40)
    db.set_setting("midrun", "0")
    worker._guard_settings(41, project)  # noqa: SLF001
    try:
        assert hookguard._SCOPES[41].midrun is False  # noqa: SLF001
    finally:
        hookguard.end(41)


def test_a_oneoff_run_can_be_held_too(temp_data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    task_id = int(db.create_oneoff("Try a thing")["id"])
    run_id = db.create_run(None, "oneoff", "sonnet", oneoff_id=task_id)
    worker._guard_settings(run_id, None)  # noqa: SLF001
    try:
        assert hookguard.hears_midrun(run_id) is True
    finally:
        hookguard.end(run_id)


def test_end_forgets_the_hold_with_the_scope(temp_data_dir):
    run_id, _ = _live_run()
    assert midrun.pause(run_id) == "paused"
    hookguard.end(run_id)
    assert midrun.is_paused(run_id) is False
    assert run_id not in midrun._HOLDS  # noqa: SLF001


# --- pause / resume -------------------------------------------------------------

def test_pause_refuses_a_run_that_is_not_running(temp_data_dir):
    project = _project()
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "success")
    assert midrun.pause(run_id) == "not_running"
    assert midrun.pause(999) == "not_running"


def test_pause_refuses_a_run_the_portal_cannot_reach(temp_data_dir):
    # No hook scope - the shape of a run adopted across a restart.
    project = _project()
    run_id = db.create_run(project["id"], "build", "opus")
    assert midrun.pause(run_id) == "cannot_hear"
    assert midrun.state(run_id)["can_pause"] is False


def test_pause_refuses_a_run_whose_scope_has_no_midrun(temp_data_dir):
    run_id, _ = _live_run(midrun_on=False)
    try:
        assert midrun.pause(run_id) == "cannot_hear"
    finally:
        hookguard.end(run_id)


def test_pause_then_resume_records_the_trail_and_the_journal(temp_data_dir):
    project = _project()
    run_id, _ = _live_run(project)
    try:
        assert midrun.pause(run_id, by="Wes") == "paused"
        assert midrun.pause(run_id) == "already_paused"
        assert midrun.is_paused(run_id) is True
        assert midrun.state(run_id) == {"paused": True, "engaged": False, "can_pause": True, "heard": 0}
        assert midrun.paused_run_ids() == {run_id}
        assert midrun.resume(run_id, by="Wes") == "resumed"
        assert midrun.resume(run_id) == "not_paused"
        assert midrun.is_paused(run_id) is False
        assert _events(run_id) == [("pause", "hold"), ("resume", "resume")]
        lines = _status_lines(project["id"])
        assert any("paused by Wes" in line for line in lines)
        assert any("resumed by Wes" in line for line in lines)
    finally:
        hookguard.end(run_id)


def test_paused_seconds_counts_the_current_hold_and_every_earlier_one(temp_data_dir, monkeypatch):
    run_id, _ = _live_run()
    try:
        clock = [1000.0]
        monkeypatch.setattr(midrun.time, "monotonic", lambda: clock[0])
        assert midrun.paused_seconds(run_id) == 0.0
        midrun.pause(run_id)
        clock[0] += 30
        assert midrun.paused_seconds(run_id) == 30.0
        midrun.resume(run_id)
        clock[0] += 100  # running time does not count
        assert midrun.paused_seconds(run_id) == 30.0
        midrun.pause(run_id)
        clock[0] += 12
        assert midrun.paused_seconds(run_id) == 42.0
        assert midrun.paused_seconds(None) == 0.0
    finally:
        hookguard.end(run_id)


# --- the hook side ----------------------------------------------------------------

def test_after_tool_call_proceeds_when_nothing_is_going_on(temp_data_dir):
    run_id, token = _live_run()
    try:
        assert midrun.after_tool_call(run_id, token, _payload()) == {}
    finally:
        hookguard.end(run_id)


def test_after_tool_call_ignores_a_bad_token_or_unknown_run(temp_data_dir):
    run_id, _ = _live_run()
    try:
        midrun.pause(run_id)
        assert midrun.after_tool_call(run_id, "wrong", _payload()) == {}
        assert midrun.hold_poll(run_id, "wrong") == {}
    finally:
        hookguard.end(run_id)
    assert midrun.after_tool_call(999, "x", _payload()) == {}


def test_a_paused_run_is_told_to_poll_and_the_hold_engages(temp_data_dir):
    run_id, token = _live_run()
    try:
        midrun.pause(run_id)
        answer = midrun.after_tool_call(run_id, token, _payload())
        assert answer["poll"] == midrun.poll_url(run_id, token)
        assert answer["interval"] == midrun.POLL_INTERVAL_SEC
        assert f"/hooks/hold?run={run_id}&token={token}" in answer["poll"]
        assert midrun.state(run_id)["engaged"] is True
        assert ("pause", "held") in _events(run_id)
        # Still held on the next ask - from either door - and "held" is
        # recorded once, not per poll.
        assert midrun.hold_poll(run_id, token)["poll"] == answer["poll"]
        assert midrun.after_tool_call(run_id, token, _payload())["poll"] == answer["poll"]
        assert _events(run_id).count(("pause", "held")) == 1
        midrun.resume(run_id)
        assert midrun.hold_poll(run_id, token) == {}
    finally:
        hookguard.end(run_id)


def test_a_note_typed_mid_run_rides_back_as_additional_context(temp_data_dir):
    project = _project()
    run_id, token = _live_run(project)
    try:
        note_id = db.add_journal(project["id"], "user", "note", "Actually, make the button green.")
        answer = midrun.after_tool_call(run_id, token, _payload())
        out = answer["hook_output"]["hookSpecificOutput"]
        assert out["hookEventName"] == "PostToolUse"
        assert "make the button green" in out["additionalContext"]
        assert "typed while you were working" in out["additionalContext"]
        assert f"run #{run_id}" in out["additionalContext"]
        # Spent: the next run's prompt will not carry it, and the finishing
        # run will not queue a rerun for it (worker._rerun_for_unseen_notes).
        assert notes.pending(project["id"]) == []
        assert db.get_journal(note_id)["delivered_at"]
        assert midrun.state(run_id)["heard"] == 1
        assert ("note", "heard") in _events(run_id)
        assert any("delivered to the running agent" in line for line in _status_lines(project["id"]))
        # Nothing left to say on the next call.
        assert midrun.after_tool_call(run_id, token, _payload()) == {}
    finally:
        hookguard.end(run_id)


def test_notes_typed_during_a_hold_arrive_together_on_resume(temp_data_dir):
    project = _project()
    run_id, token = _live_run(project)
    try:
        midrun.pause(run_id)
        assert "poll" in midrun.after_tool_call(run_id, token, _payload())
        db.add_journal(project["id"], "user", "note", "First thought.")
        db.add_journal(project["id"], "user", "note", "Second thought, which corrects the first.")
        # Still held: the notes wait with the run.
        assert "poll" in midrun.hold_poll(run_id, token)
        assert len(notes.pending(project["id"])) == 2
        midrun.resume(run_id)
        answer = midrun.hold_poll(run_id, token)
        text = answer["hook_output"]["hookSpecificOutput"]["additionalContext"]
        assert "2 notes" in text
        assert text.index("First thought") < text.index("Second thought")
        assert notes.pending(project["id"]) == []
    finally:
        hookguard.end(run_id)


def test_a_queued_note_waits_for_the_next_run(temp_data_dir):
    project = _project()
    run_id, token = _live_run(project)
    try:
        db.add_journal(project["id"], "user", "note", "For next time.", for_next_run=True)
        assert midrun.after_tool_call(run_id, token, _payload()) == {}
        assert len(notes.pending(project["id"])) == 1
    finally:
        hookguard.end(run_id)


def test_a_note_after_the_report_is_filed_waits_for_the_next_run(temp_data_dir):
    project = _project()
    run_id, token = _live_run(project)
    try:
        db.add_journal(project["id"], "user", "note", "One more thing.")
        assert midrun.after_tool_call(run_id, token, _payload("StructuredOutput")) == {}
        assert len(notes.pending(project["id"])) == 1
        # An ordinary tool call after that still hands it over.
        assert "hook_output" in midrun.after_tool_call(run_id, token, _payload("Bash"))
    finally:
        hookguard.end(run_id)


def test_the_setting_off_hands_nothing_over(temp_data_dir):
    project = _project()
    run_id, token = _live_run(project)
    try:
        db.set_setting("midrun", "0")
        db.add_journal(project["id"], "user", "note", "Hello?")
        assert midrun.after_tool_call(run_id, token, _payload()) == {}
        assert midrun.state(run_id)["can_pause"] is False
    finally:
        hookguard.end(run_id)


def test_a_voice_memo_still_transcribing_is_held_back(temp_data_dir):
    project = _project()
    run_id, token = _live_run(project)
    try:
        note_id = db.add_journal(project["id"], "user", "note", "(voice memo)")
        stored = attachments.store(
            project_id=project["id"], slug=project["slug"], orig_name="memo.m4a",
            data=b"\x00" * 16, declared_mime="audio/mp4", journal_id=note_id,
        )
        assert midrun.after_tool_call(run_id, token, _payload()) == {}
        assert len(notes.pending(project["id"])) == 1
        # The words land: now it goes.
        db.set_attachment_transcript(stored["id"], "make it blue")
        answer = midrun.after_tool_call(run_id, token, _payload())
        assert "hook_output" in answer
    finally:
        hookguard.end(run_id)


def test_files_with_a_note_are_revealed_and_named(temp_data_dir):
    project = _project()
    run_id, token = _live_run(project)
    try:
        note_id = db.add_journal(project["id"], "user", "note", "Look at this shot.")
        stored = attachments.store(
            project_id=project["id"], slug=project["slug"], orig_name="shot.png",
            data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, declared_mime="image/png", journal_id=note_id,
        )
        staged = attachments.incoming_dir(project["slug"]) / stored["stored_name"]
        assert staged.exists()
        text = midrun.after_tool_call(run_id, token, _payload())["hook_output"]["hookSpecificOutput"]["additionalContext"]
        assert attachments.rel_path(stored["stored_name"]) in text
        assert "now in your workspace" in text
        assert not staged.exists()
        assert (attachments.attachments_dir(project["slug"]) / stored["stored_name"]).exists()
    finally:
        hookguard.end(run_id)


def test_render_signs_each_note_when_more_than_one_person_wrote(temp_data_dir):
    from app import people

    project = _project()
    wes = people.owner()
    karli = people.get(people.add("Karli", gender="female"))
    rows = [
        db.get_journal(db.add_journal(project["id"], "user", "note", "Mine.", person_id=wes["id"])),
        db.get_journal(db.add_journal(project["id"], "user", "note", "Hers.", person_id=karli["id"])),
    ]
    text = midrun.render(rows, 7)
    assert "2 notes from" in text
    assert "Karli" in text and "Mine." in text and "Hers." in text


# --- the relay ------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _run_relay(monkeypatch, capsys, answers, *, sleeps=None):
    """Feed the relay one answer per request, in order."""
    calls = []
    queue = list(answers)

    def fake_urlopen(req, timeout=5):
        calls.append(req.full_url)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _FakeResponse(json.dumps(answer).encode("utf-8"))

    monkeypatch.setattr(sys, "argv", ["hookrelay.py", "http://127.0.0.1:1/hooks/post-tool?run=1&token=t"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_name": "Bash"}'))
    monkeypatch.setattr(hookrelay.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(hookrelay.time, "sleep", lambda s: (sleeps if sleeps is not None else []).append(s))
    assert hookrelay.main() == 0
    return calls, capsys.readouterr().out.strip()


def test_relay_polls_while_held_then_prints_the_final_answer(monkeypatch, capsys):
    sleeps: list = []
    context = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "hi"}}
    calls, out = _run_relay(
        monkeypatch, capsys,
        [
            {"ok": True, "poll": "http://127.0.0.1:1/hooks/hold?run=1&token=t", "interval": 2},
            {"ok": True, "poll": "http://127.0.0.1:1/hooks/hold?run=1&token=t", "interval": 2},
            {"ok": True, "hook_output": context},
        ],
        sleeps=sleeps,
    )
    assert calls == [
        "http://127.0.0.1:1/hooks/post-tool?run=1&token=t",
        "http://127.0.0.1:1/hooks/hold?run=1&token=t",
        "http://127.0.0.1:1/hooks/hold?run=1&token=t",
    ]
    assert sleeps == [2, 2]
    assert json.loads(out) == context


def test_relay_releases_silently_when_a_poll_fails(monkeypatch, capsys):
    calls, out = _run_relay(
        monkeypatch, capsys,
        [{"ok": True, "poll": "http://127.0.0.1:1/hooks/hold?run=1&token=t"}, OSError("portal restarted")],
    )
    assert len(calls) == 2
    assert out == ""


def test_relay_uses_its_default_interval_when_none_is_given(monkeypatch, capsys):
    sleeps: list = []
    _run_relay(
        monkeypatch, capsys,
        [{"poll": "http://127.0.0.1:1/hooks/hold?run=1&token=t"}, {"ok": True}],
        sleeps=sleeps,
    )
    assert sleeps == [hookrelay.DEFAULT_POLL_SEC]


def test_relay_prints_nothing_for_a_plain_proceed(monkeypatch, capsys):
    _, out = _run_relay(monkeypatch, capsys, [{"ok": True}])
    assert out == ""


# --- the endpoints ----------------------------------------------------------------------

def test_post_tool_endpoint_carries_the_hold_and_the_note(client):
    project = _project()
    run_id, token = _live_run(project)
    try:
        midrun.pause(run_id)
        resp = client.post(f"/hooks/post-tool?run={run_id}&token={token}", json=_payload())
        assert resp.status_code == 200
        assert resp.json()["poll"].endswith(f"/hooks/hold?run={run_id}&token={token}")
        # The audit row still landed underneath.
        assert len(db.hook_audit_for_run(run_id)) == 1
        db.add_journal(project["id"], "user", "note", "Use tabs.")
        assert "poll" in client.post(f"/hooks/hold?run={run_id}&token={token}").json()
        midrun.resume(run_id)
        held = client.post(f"/hooks/hold?run={run_id}&token={token}").json()
        assert "Use tabs." in held["hook_output"]["hookSpecificOutput"]["additionalContext"]
        assert "poll" not in held
    finally:
        hookguard.end(run_id)


def test_hold_endpoint_releases_an_unknown_run(client):
    assert client.post("/hooks/hold?run=999&token=nope").json() == {"ok": True}


def test_pause_and_resume_routes(client):
    project = _project()
    run_id, _ = _live_run(project)
    try:
        resp = client.post(f"/run/{run_id}/pause", data={"next": f"/project/{project['slug']}"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/project/{project['slug']}"
        assert midrun.is_paused(run_id) is True
        resp = client.post(f"/run/{run_id}/resume", data={"next": "//evil"}, follow_redirects=False)
        assert resp.headers["location"] == "/"
        assert midrun.is_paused(run_id) is False
    finally:
        hookguard.end(run_id)


def test_log_api_reports_the_hold(client):
    run_id, _ = _live_run()
    try:
        assert client.get(f"/api/run/{run_id}/log").json()["paused"] is False
        midrun.pause(run_id)
        assert client.get(f"/api/run/{run_id}/log").json()["paused"] is True
    finally:
        hookguard.end(run_id)


def test_queue_note_is_marked_for_the_next_run_and_a_plain_note_is_not(client):
    project = _project()
    client.post(f"/project/{project['slug']}/note", data={"note": "later", "then": "queue"})
    client.post(f"/project/{project['slug']}/note", data={"note": "now"})
    rows = {r["content_md"]: r for r in notes.pending(project["id"])}
    assert rows["later"]["for_next_run"] == 1
    assert rows["now"]["for_next_run"] == 0


# --- the pages ----------------------------------------------------------------------------

def test_project_page_offers_pause_only_while_the_run_can_be_reached(client):
    project = _project()
    run_id = db.create_run(project["id"], "build", "opus")
    page = client.get(f"/project/{project['slug']}").text
    assert "pause this run" not in page
    assert "stop this run" in page
    db.finish_run(run_id, "success")
    run_id, _ = _live_run(project)
    try:
        page = client.get(f"/project/{project['slug']}").text
        assert f'action="/run/{run_id}/pause"' in page
        assert "pause this run" in page
        assert "reaches the running agent at its next tool call" in page
        midrun.pause(run_id)
        page = client.get(f"/project/{project['slug']}").text
        assert f'action="/run/{run_id}/resume"' in page
        assert "resume this run" in page
        assert "agent paused" in page
        assert "Pausing - it holds at its next tool call" in page
        hookguard._SCOPES[run_id]  # noqa: SLF001 - still registered
        midrun._HOLDS[run_id].engaged = True  # noqa: SLF001
        page = client.get(f"/project/{project['slug']}").text
        assert "Paused at its last tool call" in page
    finally:
        hookguard.end(run_id)


def test_run_page_shows_the_controls_and_what_happened_while_it_ran(client):
    project = _project()
    run_id, token = _live_run(project)
    try:
        page = client.get(f"/run/{run_id}").text
        assert "pause this run" in page
        midrun.pause(run_id, by="Wes")
        midrun.after_tool_call(run_id, token, _payload())
        db.add_journal(project["id"], "user", "note", "Try the other font.")
        midrun.resume(run_id)
        midrun.hold_poll(run_id, token)
        page = client.get(f"/run/{run_id}").text
        assert "while it ran" in page
        assert "Pause requested by Wes" in page
        assert "Holding at this tool call" in page
        assert "Read note mid-run" in page
        assert "Try the other font." in page
    finally:
        hookguard.end(run_id)


def test_rail_says_paused_rather_than_working_now():
    assert sidebar.project_status({"blocked_on": ""}, running=True) == ("working now", "working")
    assert sidebar.project_status({"blocked_on": ""}, running=True, paused_run=True) == ("run paused", "working")


def test_settings_page_lists_the_switch(client):
    page = client.get("/settings").text
    assert 'name="midrun"' in page
    assert "Pause a run, and hand it notes while it works" in page


# --- the supervisor's deadline -------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_hold_pushes_the_deadline_out(tmp_path, fake_claude, monkeypatch):  # noqa: F811
    fake_claude("sleep 30\n")
    run_id = db.create_run(None, "build", "opus")
    monkeypatch.setattr(agent_runner, "_DEADLINE_TICK_SEC", 0.2)
    # A hold of 1.5s, seen by the supervisor from the start: the 1s budget is
    # spent at 2.5s, not at 1s.
    monkeypatch.setattr(midrun, "paused_seconds", lambda rid: 1.5 if rid == run_id else 0.0)
    started = time.monotonic()
    result = await agent_runner.run_claude(
        "prompt", tmp_path / "ws", "opus", timeout_min=1 / 60, run_id=run_id
    )
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert elapsed >= 2.4, elapsed
    assert elapsed < 8, elapsed


@pytest.mark.asyncio
async def test_without_a_hold_the_deadline_is_unchanged(tmp_path, fake_claude, monkeypatch):  # noqa: F811
    fake_claude("sleep 30\n")
    run_id = db.create_run(None, "build", "opus")
    monkeypatch.setattr(agent_runner, "_DEADLINE_TICK_SEC", 0.2)
    started = time.monotonic()
    result = await agent_runner.run_claude(
        "prompt", tmp_path / "ws", "opus", timeout_min=1 / 60, run_id=run_id
    )
    assert result.timed_out is True
    assert time.monotonic() - started < 2.2


@pytest.mark.asyncio
async def test_a_run_that_finishes_is_not_mistaken_for_a_timeout(tmp_path, fake_claude, monkeypatch):  # noqa: F811
    from tests.test_stream import EVENTS, _emit

    fake_claude(_emit(EVENTS))
    monkeypatch.setattr(agent_runner, "_DEADLINE_TICK_SEC", 0.05)
    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.timed_out is False
    assert result.ok is True


# --- the rerun rule this replaces -------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_note_heard_mid_run_queues_no_rerun_afterwards(temp_data_dir, monkeypatch):
    project = _project()
    run_id, token = _live_run(project)
    queued = []

    async def fake_queue(pid):
        queued.append(pid)

    monkeypatch.setattr(worker, "queue_manual_run", fake_queue)
    try:
        db.add_journal(project["id"], "user", "note", "Heard live.")
        assert "hook_output" in midrun.after_tool_call(run_id, token, _payload())
        assert await worker._rerun_for_unseen_notes(project) is False  # noqa: SLF001
        assert queued == []
        # A note the run never got to (it arrived after the report) still does.
        db.add_journal(project["id"], "user", "note", "Too late for that run.")
        assert await worker._rerun_for_unseen_notes(project) is True  # noqa: SLF001
    finally:
        hookguard.end(run_id)

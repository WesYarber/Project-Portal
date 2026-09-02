"""The PostToolUse audit trail (app/hookguard.py, todo #219's last hook piece).

Every tool call a run makes lands as one structured hook_events row, so a
run's page can answer "what did this run actually do" after its transcript is
pruned. Bounded on purpose: at most AUDIT_CAP rows per run (then one capped
marker), and plain rows age out after AUDIT_RETENTION_DAYS while denials and
Stop bounces are kept forever. These tests pin the recorder, the bounds, the
settings JSON, the retention sweep, the worker wiring, the endpoint and the
run page.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db, hookguard, settings_form, worker


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "projects" / "my-game"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _run():
    project = db.get_project_by_slug("game") or db.create_project("Game", stage="active", slug="game")
    return db.create_run(project["id"], "build", "opus")


def _register(ws, run_id=1):
    hookguard.begin(run_id, [ws], audit=True, pre_tool=False)
    scope = hookguard._SCOPES[run_id]  # noqa: SLF001
    return scope.token


def _payload(tool="Bash", tool_input=None, response=None):
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": tool_input if tool_input is not None else {"command": "ls -la"},
        "tool_response": response,
    }


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


# --- settings JSON -----------------------------------------------------------

def test_begin_with_audit_installs_post_tool_hook(workspace):
    settings = hookguard.begin(30, [workspace], audit=True)
    try:
        hooks = json.loads(settings)["hooks"]
        assert "PostToolUse" in hooks
        command = hooks["PostToolUse"][0]["hooks"][0]["command"]
        assert "/hooks/post-tool" in command and "run=30" in command
        # Every tool call, not just the guarded subset: no matcher.
        assert "matcher" not in hooks["PostToolUse"][0]
    finally:
        hookguard.end(30)


def test_begin_without_audit_installs_no_post_tool_hook(workspace):
    settings = hookguard.begin(31, [workspace], audit=False)
    try:
        assert "PostToolUse" not in json.loads(settings)["hooks"]
    finally:
        hookguard.end(31)


def test_audit_alone_is_enough_to_hook_a_run(workspace):
    settings = hookguard.begin(32, [workspace], pre_tool=False, audit=True)
    try:
        assert list(json.loads(settings)["hooks"]) == ["PostToolUse"]
    finally:
        hookguard.end(32)


def test_audit_enabled_reads_setting_default_on():
    assert hookguard.audit_enabled() is True
    db.set_setting("hook_audit", "0")
    assert hookguard.audit_enabled() is False


# --- the recorder ------------------------------------------------------------

def test_records_one_row_per_tool_call(workspace):
    run_id = _run()
    token = _register(workspace, run_id)
    try:
        hookguard.record_tool_use(run_id, token, _payload("Bash", {"command": "git status"}))
        rows = db.hook_audit_for_run(run_id)
        assert len(rows) == 1
        assert rows[0]["event"] == "post_tool_use"
        assert rows[0]["tool"] == "Bash"
        assert rows[0]["decision"] == "ok"
        assert rows[0]["detail"] == "git status"
    finally:
        hookguard.end(run_id)


def test_detail_prefers_the_tool_shaped_key(workspace):
    run_id = _run()
    token = _register(workspace, run_id)
    try:
        hookguard.record_tool_use(run_id, token, _payload("Edit", {"file_path": "/tmp/a.py", "old_string": "x"}))
        hookguard.record_tool_use(run_id, token, _payload("WebFetch", {"url": "https://example.com"}))
        hookguard.record_tool_use(run_id, token, _payload("Weird", {"count": 3}))
        details = [r["detail"] for r in db.hook_audit_for_run(run_id)]
        assert details == ["/tmp/a.py", "https://example.com", ""]
    finally:
        hookguard.end(run_id)


def test_error_response_marks_the_row(workspace):
    run_id = _run()
    token = _register(workspace, run_id)
    try:
        hookguard.record_tool_use(run_id, token, _payload(response={"is_error": True}))
        hookguard.record_tool_use(run_id, token, _payload(response=[{"is_error": True}]))
        hookguard.record_tool_use(run_id, token, _payload(response="fine"))
        decisions = [r["decision"] for r in db.hook_audit_for_run(run_id)]
        assert decisions == ["error", "error", "ok"]
    finally:
        hookguard.end(run_id)


def test_bad_token_and_unknown_run_record_nothing(workspace):
    token = _register(workspace, 43)
    try:
        hookguard.record_tool_use(43, "wrong-token", _payload())
        hookguard.record_tool_use(999, token, _payload())
        assert db.hook_audit_for_run(43) == []
        assert db.hook_audit_for_run(999) == []
    finally:
        hookguard.end(43)


def test_unaudited_scope_records_nothing(workspace):
    hookguard.begin(44, [workspace], audit=False)
    token = hookguard._SCOPES[44].token  # noqa: SLF001
    try:
        hookguard.record_tool_use(44, token, _payload())
        assert db.hook_audit_for_run(44) == []
    finally:
        hookguard.end(44)


def test_trail_caps_with_one_marker_row(workspace, monkeypatch):
    monkeypatch.setattr(hookguard, "AUDIT_CAP", 3)
    run_id = _run()
    token = _register(workspace, run_id)
    try:
        for i in range(6):
            hookguard.record_tool_use(run_id, token, _payload("Bash", {"command": f"step {i}"}))
        rows = db.hook_audit_for_run(run_id)
        assert len(rows) == 3  # 2 real rows + the marker, then silence
        assert rows[-1]["tool"] == "audit"
        assert "capped" in rows[-1]["reason"]
    finally:
        hookguard.end(run_id)


def test_recorder_swallows_db_failure(workspace, monkeypatch):
    token = _register(workspace, 46)
    try:
        monkeypatch.setattr(db, "add_hook_event", lambda *a, **k: 1 / 0)
        hookguard.record_tool_use(46, token, _payload())  # must not raise
    finally:
        hookguard.end(46)


# --- retention ---------------------------------------------------------------

def _age_row(row_id: int, days: int) -> None:
    conn = db.get_conn()
    old = f"-{days} days"
    conn.execute(
        "UPDATE hook_events SET ts = datetime('now', ?) || '+00:00' WHERE id = ?",
        (old, row_id),
    )
    conn.commit()


def test_prune_ages_plain_rows_but_keeps_denials():
    run_id = _run()
    old_ok = db.add_hook_event(run_id, "post_tool_use", "Bash", "ok", None, "ls")
    old_deny = db.add_hook_event(run_id, "pre_tool_use", "Bash", "deny", "no", "rm portal.db")
    fresh_ok = db.add_hook_event(run_id, "post_tool_use", "Edit", "ok", None, "/tmp/a")
    _age_row(old_ok, db.AUDIT_RETENTION_DAYS + 5)
    _age_row(old_deny, db.AUDIT_RETENTION_DAYS + 5)
    assert db.prune_hook_audit() == 1
    ids = {r["id"] for r in db.get_conn().execute("SELECT id FROM hook_events")}
    assert ids == {old_deny, fresh_ok}


def test_daily_prune_runs_once_per_day(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "prune_hook_audit", lambda: calls.append(1) or 0)
    monkeypatch.setattr(worker, "_audit_pruned_day", None)
    worker._daily_audit_prune()  # noqa: SLF001
    worker._daily_audit_prune()  # noqa: SLF001
    assert len(calls) == 1


def test_daily_prune_survives_a_failing_prune(monkeypatch):
    monkeypatch.setattr(db, "prune_hook_audit", lambda: 1 / 0)
    monkeypatch.setattr(worker, "_audit_pruned_day", None)
    worker._daily_audit_prune()  # must not raise  # noqa: SLF001


# --- worker wiring -----------------------------------------------------------

def test_guard_settings_audits_normal_and_meta_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    project = db.create_project("Game", stage="active", slug="game")
    meta = db.create_project("Portal", stage="active", slug=config.META_PROJECT_SLUG)
    for run_id, row in ((50, project), (51, meta)):
        settings = worker._guard_settings(run_id, row)  # noqa: SLF001
        try:
            assert "PostToolUse" in json.loads(settings)["hooks"]
        finally:
            hookguard.end(run_id)


def test_guard_settings_audits_oneoff_even_with_guardrails_off(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    db.set_setting("hook_guardrails", "0")
    task_id = int(db.create_oneoff("Try a thing")["id"])
    run_id = db.create_run(None, "oneoff", "sonnet", oneoff_id=task_id)
    settings = worker._guard_settings(run_id, None)  # noqa: SLF001
    try:
        assert list(json.loads(settings)["hooks"]) == ["PostToolUse"]
    finally:
        hookguard.end(run_id)


def test_guard_settings_audit_off_removes_the_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.set_setting("hook_audit", "0")
    # The mid-run channel (app/midrun.py) rides the same hook, so it has to
    # be off too for the hook to go - see test_midrun for the other half.
    db.set_setting("midrun", "0")
    project = db.create_project("Game", stage="active", slug="game")
    settings = worker._guard_settings(52, project)  # noqa: SLF001
    try:
        assert "PostToolUse" not in json.loads(settings)["hooks"]
    finally:
        hookguard.end(52)


def test_guard_settings_audit_off_keeps_the_hook_for_the_midrun_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.set_setting("hook_audit", "0")
    project = db.create_project("Game", stage="active", slug="game")
    settings = worker._guard_settings(53, project)  # noqa: SLF001
    try:
        hooks = json.loads(settings)["hooks"]
        assert "PostToolUse" in hooks
        assert hookguard._SCOPES[53].audit is False  # noqa: SLF001
        assert hookguard._SCOPES[53].midrun is True  # noqa: SLF001
    finally:
        hookguard.end(53)


# --- endpoint and UI ---------------------------------------------------------

def test_endpoint_records_and_answers_no_decision(workspace, client):
    run_id = _run()
    token = _register(workspace, run_id)
    try:
        resp = client.post(
            f"/hooks/post-tool?run={run_id}&token={token}",
            json=_payload("Read", {"file_path": "/tmp/notes.md"}),
        )
        assert resp.status_code == 200
        assert "decision" not in resp.json() and "hook_output" not in resp.json()
        rows = db.hook_audit_for_run(run_id)
        assert len(rows) == 1 and rows[0]["tool"] == "Read"
    finally:
        hookguard.end(run_id)


def test_endpoint_swallows_junk_body(client):
    resp = client.post("/hooks/post-tool?run=1&token=x", content=b"not json")
    assert resp.status_code == 200


def test_run_page_shows_the_tool_call_trail(client):
    project = db.create_project("Game", stage="active", slug="game")
    run_id = db.create_run(project["id"], "build", "opus")
    db.add_hook_event(run_id, "post_tool_use", "Bash", "ok", None, "bun test")
    db.add_hook_event(run_id, "post_tool_use", "Edit", "error", None, "/tmp/a.py")
    page = client.get(f"/run/{run_id}").text
    assert "tool calls" in page
    assert "bun test" in page
    assert "error" in page


def test_run_page_without_audit_shows_no_trail_card(client):
    project = db.create_project("Game", stage="active", slug="game")
    run_id = db.create_run(project["id"], "build", "opus")
    page = client.get(f"/run/{run_id}").text
    assert "tool calls" not in page


def test_settings_form_checkbox_round_trip():
    assert settings_form.apply({"hook_audit": "on"}, "hook_audit") == {"hook_audit": "1"}
    assert settings_form.apply({}, "hook_audit") == {"hook_audit": "0"}

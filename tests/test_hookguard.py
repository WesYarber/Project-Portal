"""The PreToolUse guardrail (app/hookguard.py, RESEARCH.md §5, todo #219).

Runs post every risky tool call back to the portal, which denies anything
touching the portal's own source/data outside the run's workspace family.
These tests pin the policy (including the documented flows that must stay
allowed: parent-workspace writes, deploy/screenshot.sh, secrets reads), the
fail-open registry, the audit trail, the worker wiring and the endpoint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, hookguard, settings_form, worker


@pytest.fixture
def portal_layout(tmp_path, monkeypatch):
    """A fake portal layout: APP_ROOT with a data dir, two project workspaces
    and a credentials file, all resolved real paths."""
    root = tmp_path / "portal"
    data = root / "data"
    projects = data / "projects"
    own = projects / "my-game"
    other = projects / "other-project"
    for d in (own, other, root / "secrets", root / "deploy"):
        d.mkdir(parents=True)
    (root / "secrets" / "cloudflare.txt").write_text("token\n")
    (root / "deploy" / "screenshot.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(config, "APP_ROOT", root)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    creds = fake_home / ".claude" / ".credentials.json"
    creds.write_text("{}")
    monkeypatch.setattr(hookguard, "_credentials_path", lambda: creds.resolve())
    return {"root": root, "data": data, "own": own, "other": other, "creds": creds}


def _payload(tool, tool_input, cwd):
    return {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)}


def _eval(layout, tool, tool_input, allowed=None):
    allowed = [Path(p).resolve() for p in (allowed or [layout["own"]])]
    return hookguard.evaluate(_payload(tool, tool_input, layout["own"]), allowed)


# --- file-tool policy --------------------------------------------------------

def test_write_inside_own_workspace_allowed(portal_layout):
    target = portal_layout["own"] / "src" / "app.js"
    assert _eval(portal_layout, "Write", {"file_path": str(target)}) is None


def test_write_into_another_projects_workspace_denied(portal_layout):
    target = portal_layout["other"] / "index.html"
    verdict = _eval(portal_layout, "Write", {"file_path": str(target)})
    assert verdict is not None and "workspace" in verdict[0]


def test_write_into_portal_source_denied(portal_layout):
    target = portal_layout["root"] / "app" / "worker.py"
    assert _eval(portal_layout, "Write", {"file_path": str(target)}) is not None


def test_write_into_parent_workspace_in_family_allowed(portal_layout):
    """The board-games flow: a child project writes into its parent's
    workspace and deploys from there. The family allowance must cover it."""
    target = portal_layout["other"] / "server" / "games.js"
    verdict = _eval(
        portal_layout, "Write", {"file_path": str(target)},
        allowed=[portal_layout["own"], portal_layout["other"]],
    )
    assert verdict is None


def test_write_outside_the_portal_entirely_allowed(portal_layout, tmp_path):
    target = tmp_path / "elsewhere" / "notes.md"
    assert _eval(portal_layout, "Write", {"file_path": str(target)}) is None


def test_relative_traversal_out_of_workspace_denied(portal_layout):
    """file_path is resolved against the hook payload's cwd, so `../other`
    lands where it lands, not where it is spelled."""
    verdict = _eval(portal_layout, "Edit", {"file_path": "../other-project/x.txt"})
    assert verdict is not None


def test_relative_write_inside_workspace_allowed(portal_layout):
    assert _eval(portal_layout, "Edit", {"file_path": "src/main.py"}) is None


def test_notebook_edit_uses_notebook_path(portal_layout):
    target = portal_layout["other"] / "nb.ipynb"
    assert _eval(portal_layout, "NotebookEdit", {"notebook_path": str(target)}) is not None


def test_symlink_pointing_at_protected_file_denied(portal_layout):
    """A link inside the workspace is judged by where it lands."""
    real = portal_layout["data"] / "portal.db"
    real.write_text("sqlite")
    link = portal_layout["own"] / "innocent.txt"
    os.symlink(real, link)
    assert _eval(portal_layout, "Write", {"file_path": str(link)}) is not None


def test_read_of_credentials_denied_but_other_reads_allowed(portal_layout):
    assert _eval(portal_layout, "Read", {"file_path": str(portal_layout["creds"])}) is not None
    # Reads elsewhere stay open: secrets/cloudflare.txt is a documented
    # cross-project workflow, and reading another workspace is not a threat.
    secrets = portal_layout["root"] / "secrets" / "cloudflare.txt"
    assert _eval(portal_layout, "Read", {"file_path": str(secrets)}) is None
    assert _eval(portal_layout, "Read", {"file_path": str(portal_layout["other"] / "x")}) is None


def test_grep_path_into_credentials_denied(portal_layout):
    assert _eval(portal_layout, "Grep", {"pattern": "x", "path": str(portal_layout["creds"])}) is not None


def test_junk_tool_input_allows(portal_layout):
    assert hookguard.evaluate({"tool_name": "Write", "tool_input": "junk"}, []) is None
    assert hookguard.evaluate({"tool_name": "Write", "tool_input": {}}, []) is None
    assert hookguard.evaluate({}, []) is None


# --- bash policy -------------------------------------------------------------

def test_bash_mentioning_portal_db_denied(portal_layout):
    verdict = _eval(portal_layout, "Bash", {"command": "sqlite3 ../../portal.db 'DROP TABLE runs'"})
    assert verdict is not None and "portal.db" in verdict[0]


def test_bash_mentioning_credentials_denied(portal_layout):
    verdict = _eval(portal_layout, "Bash", {"command": "cat ~/.claude/.credentials.json"})
    assert verdict is not None and "credentials" in verdict[0]


def test_bash_absolute_path_into_other_workspace_denied(portal_layout):
    cmd = f"rm -rf {portal_layout['other']}"
    assert _eval(portal_layout, "Bash", {"command": cmd}) is not None


def test_bash_own_workspace_and_family_allowed(portal_layout):
    cmd = f"git -C {portal_layout['own']} status"
    assert _eval(portal_layout, "Bash", {"command": cmd}) is None
    cmd = f"cp -r game {portal_layout['other']}/games/"
    assert _eval(
        portal_layout, "Bash", {"command": cmd},
        allowed=[portal_layout["own"], portal_layout["other"]],
    ) is None


def test_bash_portal_repo_tools_stay_allowed(portal_layout):
    """deploy/screenshot.sh and secrets reads are documented cross-project
    workflows - the bash screen only covers the data dir, not the repo."""
    root = portal_layout["root"]
    assert _eval(portal_layout, "Bash", {"command": f"bash {root}/deploy/screenshot.sh http://x"}) is None
    assert _eval(portal_layout, "Bash", {"command": f"cat {root}/secrets/cloudflare.txt"}) is None


def test_bash_plain_command_allowed(portal_layout):
    assert _eval(portal_layout, "Bash", {"command": "bun test && git commit -m 'x'"}) is None


def test_bash_normalises_dotdot_in_absolute_paths(portal_layout):
    sneaky = f"{portal_layout['own']}/../other-project/secret.txt"
    assert _eval(portal_layout, "Bash", {"command": f"cat {sneaky}"}) is not None


# --- registry: begin / decide / end ------------------------------------------

def _register(layout, run_id=1):
    settings = hookguard.begin(run_id, [layout["own"]])
    scope = hookguard._SCOPES[run_id]  # noqa: SLF001
    return settings, scope.token


def test_begin_returns_relay_hook_settings(portal_layout):
    settings, token = _register(portal_layout, run_id=7)
    try:
        parsed = json.loads(settings)
        entry = parsed["hooks"]["PreToolUse"][0]
        assert entry["matcher"] == hookguard.HOOK_MATCHER
        command = entry["hooks"][0]["command"]
        assert "hookrelay.py" in command
        assert f"run=7&token={token}" in command
        assert f"127.0.0.1:{config.PORT}" in command
    finally:
        hookguard.end(7)


def test_decide_denies_and_records_audit_row(portal_layout, monkeypatch):
    project = db.create_project("Game", stage="active", slug="my-game")
    run_id = db.create_run(project["id"], "build", "opus")
    _, token = _register(portal_layout, run_id=run_id)
    try:
        payload = _payload("Write", {"file_path": str(portal_layout["other"] / "x")}, portal_layout["own"])
        decision, reason = hookguard.decide(run_id, token, payload)
        assert decision == "deny" and "workspace" in reason
        rows = db.hook_denials_for_run(run_id)
        assert len(rows) == 1
        assert rows[0]["tool"] == "Write"
        assert rows[0]["decision"] == "deny"
    finally:
        hookguard.end(run_id)


def test_decide_allows_clean_call_without_audit_row(portal_layout):
    _, token = _register(portal_layout, run_id=12)
    try:
        payload = _payload("Write", {"file_path": str(portal_layout["own"] / "x")}, portal_layout["own"])
        assert hookguard.decide(12, token, payload) == ("allow", "")
        assert db.hook_denials_for_run(12) == []
    finally:
        hookguard.end(12)


def test_decide_fails_open_on_unknown_run_and_bad_token(portal_layout):
    payload = _payload("Write", {"file_path": str(portal_layout["other"] / "x")}, portal_layout["own"])
    assert hookguard.decide(999, "whatever", payload)[0] == "allow"
    _, _token = _register(portal_layout, run_id=13)
    try:
        assert hookguard.decide(13, "wrong-token", payload)[0] == "allow"
    finally:
        hookguard.end(13)


def test_end_forgets_the_scope(portal_layout):
    _, token = _register(portal_layout, run_id=14)
    hookguard.end(14)
    payload = _payload("Write", {"file_path": str(portal_layout["other"] / "x")}, portal_layout["own"])
    assert hookguard.decide(14, token, payload)[0] == "allow"


def test_enabled_reads_setting_default_on():
    assert hookguard.enabled() is True
    db.set_setting("hook_guardrails", "0")
    assert hookguard.enabled() is False
    db.set_setting("hook_guardrails", "1")
    assert hookguard.enabled() is True


def test_settings_form_checkbox():
    assert settings_form.apply({"hook_guardrails": "on"}, "hook_guardrails") == {"hook_guardrails": "1"}
    assert settings_form.apply({}, "hook_guardrails") == {"hook_guardrails": "0"}


# --- worker wiring -----------------------------------------------------------

def test_guard_settings_exempts_meta_project_from_write_guard(portal_layout):
    """The meta-project edits the portal by design, so it gets no PreToolUse
    guardrail - but it must still report, so the Stop nudge installs."""
    meta = db.create_project("Portal", stage="active", slug=config.META_PROJECT_SLUG)
    settings = worker._guard_settings(1, meta)  # noqa: SLF001
    try:
        assert settings is not None
        parsed = json.loads(settings)
        assert "PreToolUse" not in parsed["hooks"]
        assert "Stop" in parsed["hooks"]
    finally:
        hookguard.end(1)


def test_guard_settings_registers_normal_project(portal_layout):
    project = db.create_project("Game", stage="active", slug="my-game")
    settings = worker._guard_settings(21, project)  # noqa: SLF001
    try:
        assert settings is not None and "PreToolUse" in settings
        scope = hookguard._SCOPES[21]  # noqa: SLF001
        assert Path(portal_layout["own"]).resolve() in scope.allowed
    finally:
        hookguard.end(21)


def test_guard_settings_family_includes_parent(portal_layout):
    parent = db.create_project("Board games", stage="active", slug="other-project")
    child = db.create_project("Tak", stage="active", slug="my-game", parent_id=parent["id"])
    settings = worker._guard_settings(22, child)  # noqa: SLF001
    try:
        assert settings is not None
        allowed = hookguard._SCOPES[22].allowed  # noqa: SLF001
        assert Path(portal_layout["other"]).resolve() in allowed
        assert Path(portal_layout["own"]).resolve() in allowed
    finally:
        hookguard.end(22)


def test_guard_settings_off_when_disabled(portal_layout):
    """Guardrails off leaves the report nudge and the audit; all three off
    leaves nothing."""
    db.set_setting("hook_guardrails", "0")
    project = db.create_project("Game", stage="active", slug="my-game")
    settings = worker._guard_settings(23, project)  # noqa: SLF001
    try:
        parsed = json.loads(settings)
        assert "PreToolUse" not in parsed["hooks"]
        assert "Stop" in parsed["hooks"]
    finally:
        hookguard.end(23)
    db.set_setting("stop_report_nudge", "0")
    settings = worker._guard_settings(24, project)  # noqa: SLF001
    try:
        assert list(json.loads(settings)["hooks"]) == ["PostToolUse"]
    finally:
        hookguard.end(24)
    db.set_setting("hook_audit", "0")
    assert worker._guard_settings(25, project) is None  # noqa: SLF001


def test_guard_settings_oneoff_uses_task_workspace(portal_layout, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", portal_layout["data"] / "tasks")
    task_id = int(db.create_oneoff("Try a thing")["id"])
    run_id = db.create_run(None, "oneoff", "sonnet", oneoff_id=task_id)
    settings = worker._guard_settings(run_id, None)  # noqa: SLF001
    try:
        assert settings is not None
        allowed = hookguard._SCOPES[run_id].allowed  # noqa: SLF001
        assert any(str(task_id) in str(p) for p in allowed)
    finally:
        hookguard.end(run_id)


# --- spawn flag --------------------------------------------------------------

def test_build_cmd_carries_settings_json():
    cmd = agent_runner.build_cmd("p", "opus", 50, settings_json='{"hooks":{}}')
    idx = cmd.index("--settings")
    assert cmd[idx + 1] == '{"hooks":{}}'
    assert "--settings" not in agent_runner.build_cmd("p", "opus", 50)


# --- endpoint ----------------------------------------------------------------

@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def test_endpoint_denies_with_registered_scope(portal_layout, client):
    _, token = _register(portal_layout, run_id=31)
    try:
        payload = _payload("Write", {"file_path": str(portal_layout["other"] / "x")}, portal_layout["own"])
        resp = client.post(f"/hooks/pre-tool?run=31&token={token}", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "deny" and body["reason"]
    finally:
        hookguard.end(31)


def test_endpoint_fails_open_on_junk_body(client):
    resp = client.post("/hooks/pre-tool?run=1&token=x", content=b"not json")
    assert resp.status_code == 200
    assert resp.json()["decision"] == "allow"


def test_run_page_lists_denials(portal_layout, client):
    project = db.create_project("Game", stage="active", slug="my-game")
    run_id = db.create_run(project["id"], "build", "opus")
    db.add_hook_event(run_id, "pre_tool_use", "Bash", "deny", "This run may not touch portal.db.", "sqlite3 portal.db")
    page = client.get(f"/run/{run_id}").text
    assert "guardrail events" in page
    assert "may not touch portal.db" in page

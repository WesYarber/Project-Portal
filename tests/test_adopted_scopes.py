"""A run that outlives a service restart keeps its hooks and its MCP tools.

Every run of the meta-project restarts the service under itself, and until
2026-09-02 the adopting process knew nothing about the survivor's hook scope:
the registry was in memory. Its PreToolUse posts met an unknown run id and
failed open (no write guard), its PostToolUse posts wrote no audit rows, and
the mid-run channel - pause, resume, "deliver mid-run" - refused it with
`cannot_hear`. Its MCP calls were refused outright. Now `hookguard.begin` and
`portalmcp.begin` write the scope onto the run's row (`runs.hook_scope`,
`runs.mcp_scope`), and a lookup that misses the registry rebuilds it from the
row for a run still 'running'. These tests simulate the restart by clearing
the registries between `begin` and the hook post.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, hookguard, midrun, portalmcp, runlimit, worker


@pytest.fixture(autouse=True)
def _clean_state():
    hookguard._SCOPES.clear()  # noqa: SLF001
    midrun._HOLDS.clear()  # noqa: SLF001
    portalmcp._SCOPES.clear()  # noqa: SLF001
    worker._inflight.clear()  # noqa: SLF001
    yield
    hookguard._SCOPES.clear()  # noqa: SLF001
    midrun._HOLDS.clear()  # noqa: SLF001
    portalmcp._SCOPES.clear()  # noqa: SLF001
    worker._inflight.clear()  # noqa: SLF001


@pytest.fixture
def project():
    return db.create_project("Game", stage="active", build_approved=True, slug="game")


@pytest.fixture
def workspace(project, tmp_path):
    ws = Path(config.PROJECTS_DIR) / project["slug"]
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def client():
    from app import main

    return TestClient(main.app)


def _spawned(project, workspace, **kw) -> tuple[int, str]:
    """A run row in flight with a hook scope registered the way the worker
    registers one. Returns (run_id, token)."""
    kw.setdefault("pre_tool", True)
    kw.setdefault("audit", True)
    kw.setdefault("midrun", True)
    kw.setdefault("report_expected", True)
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    settings = hookguard.begin(run_id, [workspace], **kw)
    assert settings is not None
    return run_id, hookguard._SCOPES[run_id].token  # noqa: SLF001


def _restart() -> None:
    """What a service restart does to the registries: empties them. The rows
    are untouched, exactly as the database is."""
    hookguard._SCOPES.clear()  # noqa: SLF001
    midrun._HOLDS.clear()  # noqa: SLF001
    portalmcp._SCOPES.clear()  # noqa: SLF001


def _write(path: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Write",
            "tool_input": {"file_path": path, "content": "x"}}


def _post(tool="Bash", command="ls") -> dict:
    return {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {"command": command}}


def _scope_json(run_id: int) -> dict:
    raw = db.get_run(run_id)["hook_scope"]
    assert raw
    return json.loads(raw)


# --- the record ----------------------------------------------------------------

def test_begin_writes_the_scope_onto_the_run_row(project, workspace):
    run_id, token = _spawned(project, workspace)
    data = _scope_json(run_id)
    assert data["token"] == token
    assert data["allowed"] == [str(workspace.resolve())]
    assert data["workspace"] == str(workspace.resolve())
    assert data["report_expected"] is True
    assert data["audit"] is True
    assert data["midrun"] is True
    assert abs(data["started"] - time.time()) < 5


def test_begin_records_the_flags_as_given(project, workspace):
    run_id, _ = _spawned(project, workspace, audit=False, midrun=False, report_expected=False)
    data = _scope_json(run_id)
    assert (data["audit"], data["midrun"], data["report_expected"]) == (False, False, False)


def test_a_run_spawned_with_no_hooks_has_no_record(project, workspace):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    assert hookguard.begin(run_id, [workspace], pre_tool=False, audit=False, midrun=False) is None
    assert db.get_run(run_id)["hook_scope"] is None


def test_end_clears_the_record(project, workspace):
    run_id, _ = _spawned(project, workspace)
    hookguard.end(run_id)
    assert db.get_run(run_id)["hook_scope"] is None
    assert run_id not in hookguard._SCOPES  # noqa: SLF001


def test_a_failed_write_of_the_record_does_not_stop_the_run_starting(project, workspace, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "set_run_scope_record", boom)
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    settings = hookguard.begin(run_id, [workspace], pre_tool=True)
    assert settings is not None
    assert run_id in hookguard._SCOPES  # noqa: SLF001


# --- revival after a restart -----------------------------------------------------

def test_the_adopting_process_recognizes_the_survivor_token(project, workspace):
    run_id, token = _spawned(project, workspace)
    _restart()
    assert hookguard.authorized(run_id, token) is True
    assert run_id in hookguard._SCOPES  # noqa: SLF001 - cached once revived


def test_a_wrong_token_is_still_refused_after_revival(project, workspace):
    run_id, token = _spawned(project, workspace)
    _restart()
    assert hookguard.authorized(run_id, token + "x") is False


def test_a_finished_run_is_not_revived(project, workspace):
    run_id, token = _spawned(project, workspace)
    db.finish_run(run_id, "ok")
    _restart()
    assert hookguard.authorized(run_id, token) is False
    assert run_id not in hookguard._SCOPES  # noqa: SLF001


def test_a_run_without_a_record_is_not_revived(project, workspace):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    assert hookguard.authorized(run_id, "anything") is False
    assert hookguard.hears_midrun(run_id) is False


def test_an_unreadable_record_is_no_scope(project, workspace):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    db.set_run_scope_record(run_id, "hook_scope", "{not json")
    assert hookguard.authorized(run_id, "t") is False
    db.set_run_scope_record(run_id, "hook_scope", json.dumps({"token": 5}))
    assert hookguard.authorized(run_id, "5") is False
    db.set_run_scope_record(run_id, "hook_scope", json.dumps(["token"]))
    assert hookguard.authorized(run_id, "token") is False


def test_a_revived_scope_restores_every_field(project, workspace):
    run_id, token = _spawned(project, workspace)
    before = hookguard._SCOPES[run_id]  # noqa: SLF001
    _restart()
    assert hookguard.authorized(run_id, token)
    after = hookguard._SCOPES[run_id]  # noqa: SLF001
    assert after.allowed == before.allowed
    assert after.workspace == before.workspace
    assert after.started == pytest.approx(before.started)
    assert (after.report_expected, after.audit, after.midrun) == (True, True, True)


# --- what the revived scope does -------------------------------------------------

def test_the_write_guard_holds_across_a_restart(project, workspace, tmp_path):
    """The point of the guard is a foreign write; before the fix a survivor's
    writes anywhere were allowed."""
    run_id, token = _spawned(project, workspace)
    _restart()
    outside = str(Path(config.DATA_DIR) / "projects" / "other" / "x.txt")
    decision, reason = hookguard.decide(run_id, token, _write(outside))
    assert decision == "deny"
    decision, _ = hookguard.decide(run_id, token, _write(str(workspace / "ok.txt")))
    assert decision == "allow"


def test_the_audit_trail_carries_on_across_a_restart(project, workspace):
    run_id, token = _spawned(project, workspace)
    hookguard.record_tool_use(run_id, token, _post(command="before"))
    _restart()
    hookguard.record_tool_use(run_id, token, _post(command="after"))
    details = [r["detail"] for r in db.hook_audit_for_run(run_id)]
    assert details == ["before", "after"]


def test_the_audit_cap_resumes_from_the_rows_already_written(project, workspace):
    run_id, token = _spawned(project, workspace)
    for i in range(3):
        hookguard.record_tool_use(run_id, token, _post(command=f"c{i}"))
    _restart()
    assert hookguard.authorized(run_id, token)
    assert hookguard._SCOPES[run_id].audited == 3  # noqa: SLF001


def test_a_capped_trail_stays_capped_across_a_restart(project, workspace):
    run_id, token = _spawned(project, workspace)
    for _ in range(hookguard.AUDIT_CAP):
        db.add_hook_event(run_id, "post_tool_use", "Bash", "ok", None, "x")
    _restart()
    hookguard.record_tool_use(run_id, token, _post(command="one more"))
    assert len(db.hook_audit_for_run(run_id)) == hookguard.AUDIT_CAP


def test_the_stop_nudge_fires_once_across_a_restart(project, workspace, tmp_path):
    run_id, token = _spawned(project, workspace)
    payload = {"hook_event_name": "Stop", "transcript_path": str(tmp_path / "none.jsonl")}
    _restart()
    assert hookguard.decide_stop(run_id, token, payload)[0] == "block"
    _restart()
    # The block is on record in hook_events, so the revived scope does not
    # bounce the run a second time.
    assert hookguard.decide_stop(run_id, token, payload)[0] == "allow"


def test_a_report_written_after_the_spawn_counts_after_a_restart(project, workspace, tmp_path):
    """The freshness check reads `started` from the record, not the clock at
    revival: a report.json written after the run began - long before the
    restart - means the report was delivered. Staged as a run spawned 100 s
    ago whose report landed 50 s ago; a scope that took "now" as its start
    would call that report stale and bounce a run that has already reported."""
    run_id, token = _spawned(project, workspace)
    record = _scope_json(run_id)
    record["started"] = time.time() - 100
    db.set_run_scope_record(run_id, "hook_scope", json.dumps(record))
    (workspace / ".portal").mkdir()
    report = workspace / ".portal" / "report.json"
    report.write_text("{}")
    os.utime(report, (time.time() - 50, time.time() - 50))
    _restart()
    payload = {"hook_event_name": "Stop", "transcript_path": str(tmp_path / "none.jsonl")}
    assert hookguard.decide_stop(run_id, token, payload)[0] == "allow"
    assert db.count_hook_events(run_id, "stop", "block") == 0


def test_a_survivor_can_be_paused_and_holds_at_its_next_tool_call(project, workspace):
    run_id, token = _spawned(project, workspace)
    _restart()
    assert midrun.state(run_id)["can_pause"] is True
    assert midrun.pause(run_id, by="Wes") == "paused"
    answer = midrun.after_tool_call(run_id, token, _post())
    assert answer["poll"].endswith(f"/hooks/hold?run={run_id}&token={token}")
    assert midrun.state(run_id)["engaged"] is True
    assert midrun.resume(run_id) == "resumed"
    assert midrun.hold_poll(run_id, token) == {}


def test_a_survivor_spawned_without_the_channel_still_cannot_hear(project, workspace):
    run_id, token = _spawned(project, workspace, midrun=False)
    _restart()
    assert hookguard.authorized(run_id, token) is True
    assert midrun.pause(run_id) == "cannot_hear"


def test_a_note_handed_mid_run_reaches_a_survivor(project, workspace):
    run_id, token = _spawned(project, workspace)
    _restart()
    note_id = db.add_journal(project["id"], "user", "note", "Make the button green.", hear_now=True)
    answer = midrun.after_tool_call(run_id, token, _post())
    text = answer["hook_output"]["hookSpecificOutput"]["additionalContext"]
    assert "Make the button green." in text
    assert db.get_journal(note_id)["delivered_at"]


def test_the_hold_endpoint_knows_a_survivor(client, project, workspace):
    run_id, token = _spawned(project, workspace)
    _restart()
    midrun.pause(run_id)
    r = client.post(f"/hooks/hold?run={run_id}&token={token}")
    assert r.status_code == 200
    assert r.json()["poll"].endswith(f"run={run_id}&token={token}")
    midrun.resume(run_id)
    assert client.post(f"/hooks/hold?run={run_id}&token={token}").json() == {"ok": True}


def test_the_pre_tool_endpoint_denies_a_survivor_foreign_write(client, project, workspace):
    run_id, token = _spawned(project, workspace)
    _restart()
    outside = str(Path(config.DATA_DIR) / "projects" / "other" / "x.txt")
    r = client.post(f"/hooks/pre-tool?run={run_id}&token={token}", json=_write(outside))
    assert r.json()["decision"] == "deny"


# --- the scope ends with the adopted run --------------------------------------------

def _adopted(project, workspace) -> tuple[int, str]:
    """A survivor as the reaper sees it: scoped under another pid, leased."""
    run_id, token = _spawned(project, workspace)
    db.set_run_scope(run_id, f"portal-run-{run_id}-{os.getpid() + 1}-1.scope")
    db.set_run_lease(run_id, str(workspace))
    _restart()
    return run_id, token


def test_settling_an_adopted_run_ends_its_revived_scopes(project, workspace, monkeypatch):
    run_id, token = _adopted(project, workspace)
    portalmcp.begin(run_id, project["id"])
    assert hookguard.authorized(run_id, token)  # revived
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: False)

    worker._reap_adopted()  # noqa: SLF001

    assert db.get_run(run_id)["status"] == "error"
    assert run_id not in hookguard._SCOPES  # noqa: SLF001
    assert run_id not in portalmcp._SCOPES  # noqa: SLF001
    assert db.get_run(run_id)["hook_scope"] is None
    assert db.get_run(run_id)["mcp_scope"] is None
    assert hookguard.authorized(run_id, token) is False


def test_canceling_an_adopted_run_ends_its_revived_scope(project, workspace, monkeypatch):
    run_id, token = _adopted(project, workspace)
    assert hookguard.authorized(run_id, token)
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: True)
    monkeypatch.setattr(runlimit, "stop_scope", lambda unit: True)
    assert run_id not in agent_runner._ACTIVE_PROCS  # noqa: SLF001

    assert worker.cancel_run(run_id) not in ("orphaned", "not_running", "missing")

    assert db.get_run(run_id)["status"] != "running"
    assert run_id not in hookguard._SCOPES  # noqa: SLF001
    assert db.get_run(run_id)["hook_scope"] is None
    assert hookguard.authorized(run_id, token) is False


def test_canceling_an_orphaned_row_ends_its_scope_too(project, workspace):
    run_id, token = _spawned(project, workspace)
    _restart()
    assert worker.cancel_run(run_id) == "orphaned"
    assert db.get_run(run_id)["hook_scope"] is None
    assert hookguard.authorized(run_id, token) is False


# --- the boot message says whether the survivor can still be reached -----------------

def test_the_boot_journal_says_a_recorded_survivor_can_still_be_paused(project, workspace, monkeypatch):
    run_id, _ = _spawned(project, workspace)
    db.set_run_scope(run_id, "portal-run-1-1-1.scope")
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: True)
    db.reconcile_orphaned_runs_on_boot()
    lines = [r["content_md"] for r in db.list_journal_asc(project["id"], limit=10)]
    assert any("can be paused and handed a note" in line for line in lines)


def test_the_boot_journal_says_when_a_survivor_is_out_of_reach(project, monkeypatch):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    db.set_run_scope(run_id, "portal-run-1-1-1.scope")
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: True)
    db.reconcile_orphaned_runs_on_boot()
    lines = [r["content_md"] for r in db.list_journal_asc(project["id"], limit=10)]
    assert any("cannot be paused or handed a note" in line for line in lines)


# --- the MCP scope --------------------------------------------------------------------

def _mcp_begin(project, run_id) -> str:
    raw = portalmcp.begin(run_id, int(project["id"]), "build")
    assert raw is not None
    return json.loads(raw)["mcpServers"]["portal"]["args"][3]


def test_mcp_begin_writes_the_scope_onto_the_run_row(project):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    token = _mcp_begin(project, run_id)
    data = json.loads(db.get_run(run_id)["mcp_scope"])
    assert data == {"token": token, "project_id": project["id"]}


def test_mcp_end_clears_the_record(project):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    _mcp_begin(project, run_id)
    portalmcp.end(run_id)
    assert db.get_run(run_id)["mcp_scope"] is None


def test_mcp_tools_are_served_to_a_survivor(project):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    token = _mcp_begin(project, run_id)
    _restart()
    tools = portalmcp.tools(run_id, token)
    assert tools is not None and any(t["name"] == "ask" for t in tools)
    scope = portalmcp._SCOPES[run_id]  # noqa: SLF001
    assert (scope.project_id, scope.run_id) == (project["id"], run_id)


def test_mcp_refuses_a_wrong_token_and_a_finished_run(project):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    token = _mcp_begin(project, run_id)
    _restart()
    assert portalmcp.tools(run_id, token + "x") is None
    db.finish_run(run_id, "ok")
    _restart()
    assert portalmcp.tools(run_id, token) is None


def test_mcp_ignores_an_unreadable_record(project):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    db.set_run_scope_record(run_id, "mcp_scope", "{nope")
    assert portalmcp.tools(run_id, "t") is None
    db.set_run_scope_record(run_id, "mcp_scope", json.dumps({"token": "t"}))
    assert portalmcp.tools(run_id, "t") is None


def test_a_survivor_can_still_ask(project, monkeypatch):
    sent = []

    async def fake_notify(title, body, **kw):
        sent.append(title)

    monkeypatch.setattr(portalmcp.notify, "notify", fake_notify)
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    token = _mcp_begin(project, run_id)
    _restart()
    result = asyncio.run(portalmcp.call(run_id, token, "ask",
                                        {"question": "Blue or green?", "wait_seconds": 0}))
    assert not result.get("isError"), result
    assert db.count_open_questions(project["id"]) == 1


def test_a_failed_mcp_record_write_does_not_stop_the_run(project, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "set_run_scope_record", boom)
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    assert portalmcp.begin(run_id, project["id"], "build") is not None
    assert run_id in portalmcp._SCOPES  # noqa: SLF001


def test_the_scope_column_name_is_checked(project):
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    with pytest.raises(ValueError):
        db.set_run_scope_record(run_id, "summary", "x")


def test_a_report_left_by_an_earlier_run_does_not_count_after_a_restart(project, workspace, tmp_path):
    """`started` is read from the record, not reset at revival: a report.json
    older than the run is a previous run's, and the nudge still fires."""
    run_id, token = _spawned(project, workspace)
    (workspace / ".portal").mkdir()
    report = workspace / ".portal" / "report.json"
    report.write_text("{}")
    stale = hookguard._SCOPES[run_id].started - 100  # noqa: SLF001
    os.utime(report, (stale, stale))
    _restart()
    payload = {"hook_event_name": "Stop", "transcript_path": str(tmp_path / "none.jsonl")}
    assert hookguard.decide_stop(run_id, token, payload)[0] == "block"


def test_only_a_block_on_record_counts_as_a_spent_nudge(project, workspace, tmp_path):
    run_id, token = _spawned(project, workspace)
    db.add_hook_event(run_id, "stop", "Stop", "allow", "let through")
    _restart()
    payload = {"hook_event_name": "Stop", "transcript_path": str(tmp_path / "none.jsonl")}
    assert hookguard.decide_stop(run_id, token, payload)[0] == "block"


# --- a pause outlives the restart ------------------------------------------------------
#
# The hold used to live only in memory, so every restart of the service woke
# the run paused under it. Now `midrun._persist` writes the hold onto the run's
# row on every change, the relay keeps asking through the restart
# (tests/test_midrun.py), and `midrun._hold` rebuilds the hold for a run this
# process did not start.


def _hold_json(run_id: int) -> dict:
    raw = db.get_run(run_id)["hold_state"]
    assert raw
    return json.loads(raw)


def _midrun_events(run_id):
    return [(r["tool"], r["decision"]) for r in db.midrun_events_for_run(run_id)]


def test_an_engaged_pause_outlives_a_restart(project, workspace):
    run_id, token = _spawned(project, workspace)
    assert midrun.pause(run_id, by="Wes") == "paused"
    assert "poll" in midrun.after_tool_call(run_id, token, _post())
    assert _hold_json(run_id)["engaged"] is True
    _restart()
    # The relay's next poll, met by a process that has never heard of the hold.
    answer = midrun.hold_poll(run_id, token)
    assert answer["poll"].endswith(f"/hooks/hold?run={run_id}&token={token}")
    st = midrun.state(run_id)
    assert st["paused"] is True and st["engaged"] is True and st["can_pause"] is True
    assert midrun.is_paused(run_id) is True


def test_the_revival_is_recorded_once_on_the_run_s_timeline(project, workspace):
    run_id, token = _spawned(project, workspace)
    midrun.pause(run_id)
    midrun.after_tool_call(run_id, token, _post())
    _restart()
    midrun.hold_poll(run_id, token)
    midrun.hold_poll(run_id, token)
    midrun.state(run_id)
    assert _midrun_events(run_id).count(("pause", "kept")) == 1
    # A revived hold that is already engaged does not announce a second engagement.
    assert _midrun_events(run_id).count(("pause", "held")) == 1


def test_a_pause_requested_before_the_restart_engages_after_it(project, workspace):
    run_id, token = _spawned(project, workspace)
    midrun.pause(run_id)
    _restart()
    answer = midrun.after_tool_call(run_id, token, _post())
    assert "poll" in answer
    assert midrun.state(run_id)["engaged"] is True
    assert _hold_json(run_id)["engaged"] is True
    assert ("pause", "held") in _midrun_events(run_id)


def test_time_on_hold_counts_across_the_restart(project, workspace):
    run_id, token = _spawned(project, workspace)
    midrun.pause(run_id)
    midrun.after_tool_call(run_id, token, _post())
    data = _hold_json(run_id)
    data["paused_since"] -= 100.0  # the pause began 100 s before the restart
    data["paused_total"] = 40.0  # and an earlier pause had already cost 40 s
    db.set_run_scope_record(run_id, "hold_state", json.dumps(data))
    _restart()
    assert 140.0 <= midrun.paused_seconds(run_id) < 145.0
    assert midrun.resume(run_id, by="Wes") == "resumed"
    resumed = [r for r in db.midrun_events_for_run(run_id) if r["decision"] == "resume"]
    assert "after holding 1m 4" in resumed[-1]["reason"]  # 1m 40s, held (engaged), not "before the hold engaged"
    assert 140.0 <= midrun.paused_seconds(run_id) < 145.0


def test_resuming_a_survivor_lets_its_relay_go_and_clears_the_pause_from_the_row(project, workspace):
    run_id, token = _spawned(project, workspace)
    midrun.pause(run_id)
    midrun.after_tool_call(run_id, token, _post())
    _restart()
    assert midrun.resume(run_id) == "resumed"
    assert midrun.hold_poll(run_id, token) == {}
    assert db.hold_record_says_paused(db.get_run(run_id)) is False
    assert _hold_json(run_id)["engaged"] is False


def test_a_resumed_hold_keeps_its_total_across_a_restart_without_being_paused(project, workspace):
    run_id, token = _spawned(project, workspace)
    db.set_run_scope_record(run_id, "hold_state", json.dumps(
        {"paused_since": None, "paused_total": 42.0, "engaged": False, "engaged_at": "", "heard": 2}))
    _restart()
    assert midrun.is_paused(run_id) is False
    assert midrun.paused_seconds(run_id) == 42.0
    assert midrun.state(run_id)["heard"] == 2
    assert midrun.hold_poll(run_id, token) == {}
    assert ("pause", "kept") not in _midrun_events(run_id)


def test_a_finished_run_s_hold_is_not_revived(project, workspace):
    run_id, token = _spawned(project, workspace)
    midrun.pause(run_id)
    conn = db.get_conn()
    conn.execute("UPDATE runs SET status = 'ok' WHERE id = ?", (run_id,))
    conn.commit()
    _restart()
    assert midrun.is_paused(run_id) is False
    assert midrun.paused_run_ids() == set()


def test_an_unreadable_hold_record_is_no_hold(project, workspace):
    run_id, token = _spawned(project, workspace)
    db.set_run_scope_record(run_id, "hold_state", "{not json")
    _restart()
    assert midrun.is_paused(run_id) is False
    assert midrun.hold_poll(run_id, token) == {}
    # The run can still be paused afresh: the scope is intact, only the hold was unreadable.
    assert midrun.pause(run_id) == "paused"
    db.set_run_scope_record(run_id, "hold_state", json.dumps(["a", "list"]))
    _restart()
    assert midrun.is_paused(run_id) is False


def test_the_rail_sees_a_survivor_s_hold_before_any_page_asks_about_it(project, workspace):
    from app import main

    run_id, token = _spawned(project, workspace)
    db.set_run_scope(run_id, "portal-run-1-1-1.scope")
    midrun.pause(run_id)
    _restart()
    assert run_id in midrun.paused_run_ids()
    assert project["id"] in main._paused_project_ids()  # noqa: SLF001


def test_the_hold_record_is_cleared_when_the_run_ends(project, workspace):
    run_id, token = _spawned(project, workspace)
    midrun.pause(run_id)
    assert db.get_run(run_id)["hold_state"]
    hookguard.end(run_id)
    assert db.get_run(run_id)["hold_state"] is None
    assert db.running_run_ids_with_record("hold_state") == []


def test_a_failed_hold_write_does_not_stop_the_pause(project, workspace, monkeypatch):
    run_id, token = _spawned(project, workspace)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "set_run_scope_record", boom)
    assert midrun.pause(run_id) == "paused"
    assert "poll" in midrun.after_tool_call(run_id, token, _post())
    assert midrun.resume(run_id) == "resumed"
    hookguard.end(run_id)  # the clear fails the same way, and the run still ends


def test_a_note_heard_by_a_survivor_is_counted_on_the_row(project, workspace):
    run_id, token = _spawned(project, workspace)
    _restart()
    db.add_journal(project["id"], "user", "note", "Make it blue.", hear_now=True)
    assert "hook_output" in midrun.after_tool_call(run_id, token, _post())
    assert _hold_json(run_id)["heard"] == 1
    _restart()
    assert midrun.state(run_id)["heard"] == 1


def test_hold_record_says_paused_reads_only_a_real_record():
    assert db.hold_record_says_paused(None) is False
    project = db.create_project("P", stage="active", slug="p")
    run_id = db.create_run(project["id"], "build", "claude-fable-5-1")
    assert db.hold_record_says_paused(db.get_run(run_id)) is False
    db.set_run_scope_record(run_id, "hold_state", "{junk")
    assert db.hold_record_says_paused(db.get_run(run_id)) is False
    db.set_run_scope_record(run_id, "hold_state", json.dumps({"paused_since": None}))
    assert db.hold_record_says_paused(db.get_run(run_id)) is False
    db.set_run_scope_record(run_id, "hold_state", json.dumps({"paused_since": 1.0}))
    assert db.hold_record_says_paused(db.get_run(run_id)) is True


def test_running_run_ids_with_record_lists_only_live_rows_of_that_column():
    project = db.create_project("P", stage="active", slug="p")
    held = db.create_run(project["id"], "build", "claude-fable-5-1")
    hooked = db.create_run(project["id"], "build", "claude-fable-5-1")
    done = db.create_run(project["id"], "build", "claude-fable-5-1")
    db.set_run_scope_record(held, "hold_state", "{}")
    db.set_run_scope_record(hooked, "hook_scope", "{}")
    db.set_run_scope_record(done, "hold_state", "{}")
    conn = db.get_conn()
    conn.execute("UPDATE runs SET status = 'ok' WHERE id = ?", (done,))
    conn.commit()
    assert db.running_run_ids_with_record("hold_state") == [held]
    assert db.running_run_ids_with_record("hook_scope") == [hooked]
    with pytest.raises(ValueError):
        db.running_run_ids_with_record("status")


def test_the_boot_journal_says_a_survivor_is_still_held(project, workspace, monkeypatch):
    run_id, _ = _spawned(project, workspace)
    db.set_run_scope(run_id, "portal-run-1-1-1.scope")
    midrun.pause(run_id)
    monkeypatch.setattr(runlimit, "scope_is_active", lambda unit: True)
    _restart()
    db.reconcile_orphaned_runs_on_boot()
    lines = [r["content_md"] for r in db.list_journal_asc(project["id"], limit=10)]
    assert any("is still held" in line and "Resume it when ready" in line for line in lines)
    assert not any("can be paused and handed a note" in line for line in lines)

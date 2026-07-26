"""The Stop-hook report nudge (app/hookguard.py, RESEARCH.md §5, todo #219).

A project run that tries to finish without having delivered its report - no
StructuredOutput tool call in the transcript, no fresh .portal/report.json in
its workspace - is blocked once at the CLI's Stop hook and told to submit it.
Verified live against CLI 2.1.215 before building: the Stop hook fires in -p
print mode, a {"decision":"block","reason":...} answer makes the model
actually continue and comply, and the re-stop arrives with stop_hook_active
true. These tests pin the detection (structural, not substring), the
once-only bound, the fail-open contract, the endpoint, the relay passthrough
and the worker wiring.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db, hookguard, hookrelay, settings_form, worker


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "projects" / "my-game"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _register(ws, run_id=1, report_expected=True, pre_tool=False):
    settings = hookguard.begin(
        run_id, [ws], report_expected=report_expected, pre_tool=pre_tool
    )
    scope = hookguard._SCOPES.get(run_id)  # noqa: SLF001
    return settings, (scope.token if scope else "")


def _transcript(tmp_path, lines):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _payload(transcript_path="", stop_hook_active=False):
    return {
        "hook_event_name": "Stop",
        "transcript_path": transcript_path,
        "stop_hook_active": stop_hook_active,
        "session_id": "abc",
    }


_REPORT_LINE = json.dumps(
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "StructuredOutput", "input": {}}
            ],
        },
    }
)
# The contract text mentions the tool by name in the prompt, so the word shows
# up in user lines of every transcript - a substring match would never block.
_PROSE_MENTION_LINE = json.dumps(
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": "You MUST deliver a report by calling the StructuredOutput tool.",
        },
    }
)


# --- settings JSON from begin() ---------------------------------------------

def test_begin_installs_stop_hook_when_report_expected(workspace):
    settings, token = _register(workspace, run_id=7)
    try:
        parsed = json.loads(settings)
        assert "PreToolUse" not in parsed["hooks"]
        entry = parsed["hooks"]["Stop"][0]["hooks"][0]
        assert "hookrelay.py" in entry["command"]
        assert f"/hooks/stop?run=7&token={token}" in entry["command"]
    finally:
        hookguard.end(7)


def test_begin_installs_both_hooks_for_a_guarded_reporting_run(workspace):
    settings, _ = _register(workspace, run_id=8, pre_tool=True)
    try:
        parsed = json.loads(settings)
        assert "PreToolUse" in parsed["hooks"]
        assert "Stop" in parsed["hooks"]
    finally:
        hookguard.end(8)


def test_begin_default_has_no_stop_hook(workspace):
    settings, _ = _register(workspace, run_id=9, report_expected=False, pre_tool=True)
    try:
        assert "Stop" not in json.loads(settings)["hooks"]
    finally:
        hookguard.end(9)


def test_begin_with_no_hooks_returns_none_and_leaves_no_scope(workspace):
    settings = hookguard.begin(10, [workspace], report_expected=False, pre_tool=False)
    assert settings is None
    assert 10 not in hookguard._SCOPES  # noqa: SLF001


# --- decide_stop: when it blocks --------------------------------------------

def test_blocks_a_reportless_stop_and_records_it(workspace, tmp_path):
    run_id = db.create_run(None, "build", "opus")
    _, token = _register(workspace, run_id=run_id)
    try:
        transcript = _transcript(tmp_path, [_PROSE_MENTION_LINE])
        decision, reason = hookguard.decide_stop(run_id, token, _payload(transcript))
        assert decision == "block"
        assert "StructuredOutput" in reason and "report.json" in reason
        rows = db.hook_denials_for_run(run_id)
        assert len(rows) == 1
        assert rows[0]["event"] == "stop" and rows[0]["decision"] == "block"
    finally:
        hookguard.end(run_id)


def test_blocks_at_most_once_per_run(workspace, tmp_path):
    _, token = _register(workspace, run_id=11)
    try:
        transcript = _transcript(tmp_path, [_PROSE_MENTION_LINE])
        assert hookguard.decide_stop(11, token, _payload(transcript))[0] == "block"
        assert hookguard.decide_stop(11, token, _payload(transcript))[0] == "allow"
    finally:
        hookguard.end(11)


def test_stop_hook_active_never_blocks(workspace, tmp_path):
    """The CLI's own loop guard: after one block the re-stop arrives with
    stop_hook_active true and must pass, whatever we remember."""
    _, token = _register(workspace, run_id=12)
    try:
        transcript = _transcript(tmp_path, [_PROSE_MENTION_LINE])
        payload = _payload(transcript, stop_hook_active=True)
        assert hookguard.decide_stop(12, token, payload)[0] == "allow"
        assert db.hook_denials_for_run(12) == []
    finally:
        hookguard.end(12)


def test_missing_transcript_still_blocks_once(workspace):
    """An unreadable transcript reads as "not delivered": one bounded bounce
    beats silently losing the nudge the day the payload shape changes."""
    _, token = _register(workspace, run_id=13)
    try:
        payload = _payload(str(workspace / "no-such-transcript.jsonl"))
        assert hookguard.decide_stop(13, token, payload)[0] == "block"
    finally:
        hookguard.end(13)


# --- decide_stop: when it allows --------------------------------------------

def test_structured_output_in_transcript_allows(workspace, tmp_path):
    _, token = _register(workspace, run_id=14)
    try:
        transcript = _transcript(tmp_path, [_PROSE_MENTION_LINE, _REPORT_LINE])
        assert hookguard.decide_stop(14, token, _payload(transcript))[0] == "allow"
        assert db.hook_denials_for_run(14) == []
    finally:
        hookguard.end(14)


def test_prose_mention_is_not_a_report(workspace, tmp_path):
    """Pins the structural parse: the tool name as plain prompt text (which
    every run's transcript contains) must not count as a submission."""
    _, token = _register(workspace, run_id=15)
    try:
        transcript = _transcript(tmp_path, [_PROSE_MENTION_LINE])
        assert hookguard.decide_stop(15, token, _payload(transcript))[0] == "block"
    finally:
        hookguard.end(15)


def test_malformed_transcript_lines_are_skipped(workspace, tmp_path):
    _, token = _register(workspace, run_id=16)
    try:
        transcript = _transcript(
            tmp_path, ["{not json StructuredOutput", "", _REPORT_LINE]
        )
        assert hookguard.decide_stop(16, token, _payload(transcript))[0] == "allow"
    finally:
        hookguard.end(16)


def test_fresh_report_file_allows(workspace, tmp_path):
    _, token = _register(workspace, run_id=17)
    try:
        portal_dir = workspace / ".portal"
        portal_dir.mkdir()
        (portal_dir / "report.json").write_text("{}")
        transcript = _transcript(tmp_path, [_PROSE_MENTION_LINE])
        assert hookguard.decide_stop(17, token, _payload(transcript))[0] == "allow"
    finally:
        hookguard.end(17)


def test_stale_report_file_from_a_previous_run_blocks(workspace, tmp_path):
    _, token = _register(workspace, run_id=18)
    try:
        portal_dir = workspace / ".portal"
        portal_dir.mkdir()
        report = portal_dir / "report.json"
        report.write_text("{}")
        old = time.time() - 3600
        os.utime(report, (old, old))
        transcript = _transcript(tmp_path, [_PROSE_MENTION_LINE])
        assert hookguard.decide_stop(18, token, _payload(transcript))[0] == "block"
    finally:
        hookguard.end(18)


def test_unknown_run_and_bad_token_allow(workspace, tmp_path):
    transcript = _transcript(tmp_path, [_PROSE_MENTION_LINE])
    assert hookguard.decide_stop(999, "tok", _payload(transcript))[0] == "allow"
    _, _token = _register(workspace, run_id=19)
    try:
        assert hookguard.decide_stop(19, "wrong", _payload(transcript))[0] == "allow"
    finally:
        hookguard.end(19)


def test_report_not_expected_allows(workspace, tmp_path):
    _, token = _register(workspace, run_id=20, report_expected=False, pre_tool=True)
    try:
        transcript = _transcript(tmp_path, [_PROSE_MENTION_LINE])
        assert hookguard.decide_stop(20, token, _payload(transcript))[0] == "allow"
    finally:
        hookguard.end(20)


def test_setting_off_allows(workspace, tmp_path):
    db.set_setting("stop_report_nudge", "0")
    _, token = _register(workspace, run_id=21)
    try:
        transcript = _transcript(tmp_path, [_PROSE_MENTION_LINE])
        assert hookguard.decide_stop(21, token, _payload(transcript))[0] == "allow"
    finally:
        hookguard.end(21)


def test_decide_stop_fails_open_on_exception(workspace, monkeypatch):
    _, token = _register(workspace, run_id=22)
    try:
        def boom(payload, scope):
            raise RuntimeError("bug")

        monkeypatch.setattr(hookguard, "_report_delivered", boom)
        assert hookguard.decide_stop(22, token, _payload("x"))[0] == "allow"
    finally:
        hookguard.end(22)


# --- endpoint and relay ------------------------------------------------------

@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def test_endpoint_blocks_reportless_stop(workspace, tmp_path, client):
    _, token = _register(workspace, run_id=31)
    try:
        transcript = _transcript(tmp_path, [_PROSE_MENTION_LINE])
        resp = client.post(f"/hooks/stop?run=31&token={token}", json=_payload(transcript))
        assert resp.status_code == 200
        out = resp.json()["hook_output"]
        assert out["decision"] == "block"
        assert "StructuredOutput" in out["reason"]
    finally:
        hookguard.end(31)


def test_endpoint_allows_after_report(workspace, tmp_path, client):
    _, token = _register(workspace, run_id=32)
    try:
        transcript = _transcript(tmp_path, [_REPORT_LINE])
        resp = client.post(f"/hooks/stop?run=32&token={token}", json=_payload(transcript))
        assert resp.json()["hook_output"] is None
    finally:
        hookguard.end(32)


def test_endpoint_fails_open_on_junk_body(client):
    resp = client.post("/hooks/stop?run=1&token=x", content=b"not json")
    assert resp.status_code == 200
    assert resp.json()["hook_output"] is None


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _run_relay(monkeypatch, capsys, answer):
    monkeypatch.setattr(sys, "argv", ["hookrelay.py", "http://127.0.0.1:1/hooks/stop?run=1&token=t"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(
        hookrelay.urllib.request,
        "urlopen",
        lambda req, timeout=5: _FakeResponse(json.dumps(answer).encode("utf-8")),
    )
    assert hookrelay.main() == 0
    return capsys.readouterr().out.strip()


def test_relay_prints_hook_output_verbatim(monkeypatch, capsys):
    block = {"decision": "block", "reason": "submit your report"}
    out = _run_relay(monkeypatch, capsys, {"hook_output": block})
    assert json.loads(out) == block


def test_relay_stays_silent_when_hook_output_is_null(monkeypatch, capsys):
    assert _run_relay(monkeypatch, capsys, {"hook_output": None}) == ""


def test_relay_legacy_pre_tool_deny_still_works(monkeypatch, capsys):
    out = _run_relay(monkeypatch, capsys, {"decision": "deny", "reason": "no"})
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- worker wiring and UI ----------------------------------------------------

def test_oneoff_runs_are_never_nudged(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    task_id = int(db.create_oneoff("Try a thing")["id"])
    run_id = db.create_run(None, "oneoff", "sonnet", oneoff_id=task_id)
    settings = worker._guard_settings(run_id, None)  # noqa: SLF001
    try:
        assert "Stop" not in json.loads(settings)["hooks"]
    finally:
        hookguard.end(run_id)


def test_run_page_shows_the_stop_bounce(client):
    project = db.create_project("Game", stage="active", slug="game")
    run_id = db.create_run(project["id"], "build", "opus")
    db.add_hook_event(
        run_id, "stop", "Stop", "block",
        "Tried to finish without delivering its report; bounced once and told to submit it.",
        "/tmp/t.jsonl",
    )
    resp = client.get(f"/run/{run_id}")
    assert resp.status_code == 200
    assert "guardrail events" in resp.text
    assert "without delivering its report" in resp.text


def test_settings_form_checkbox_round_trip():
    assert settings_form.apply({"stop_report_nudge": "on"}, "stop_report_nudge") == {
        "stop_report_nudge": "1"
    }
    assert settings_form.apply({}, "stop_report_nudge") == {"stop_report_nudge": "0"}

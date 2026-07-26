"""The streaming agent runner, driven by a fake `claude` on PATH.

These exercise the real subprocess path (stream-json parsing, event callback,
timeout, report pickup) without ever invoking a model.
"""
from __future__ import annotations

import json
import os
import stat
import textwrap

import pytest

from app import agent_runner, worker


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Install a stub `claude` and make the runner see only it.

    `_extra_env` prepends ~/.local/bin, where the real CLI lives, so merely
    editing PATH here would let a test invoke the real agent - hence the
    _extra_env override rather than an os.environ tweak.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)

    def install(body: str) -> None:
        script = bindir / "claude"
        script.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    # Real PATH still follows, so the stub can use cat/sleep/printf.
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    monkeypatch.setattr(agent_runner, "_extra_env", lambda: dict(env))
    return install


EVENTS = [
    {"type": "system", "subtype": "init", "model": "opus", "tools": ["Bash"]},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "a\nb"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
     "total_cost_usd": 0.25, "session_id": "sess-1", "result": "all done"},
]


def _emit(events: list[dict]) -> str:
    """Emit these events as NDJSON. A quoted heredoc rather than `echo`, whose
    /bin/sh implementation expands the \\n inside a JSON string and would split
    one event across two lines."""
    body = "\n".join(json.dumps(e) for e in events)
    return f"cat <<'PORTAL_EOF'\n{body}\nPORTAL_EOF\n"


def _emit_script() -> str:
    return _emit(EVENTS)


@pytest.mark.asyncio
async def test_stream_events_reach_the_callback(tmp_path, fake_claude):
    fake_claude(_emit_script())
    seen: list[dict] = []
    result = await agent_runner.run_claude(
        "prompt", tmp_path / "ws", "opus", timeout_min=1,
        on_event=lambda event, lines: seen.append(event),
    )
    assert result.ok is True
    assert result.session_id == "sess-1"
    assert result.cost_usd == 0.25
    assert result.num_turns == 3
    assert result.result_text == "all done"
    assert [e["type"] for e in seen] == ["system", "assistant", "user", "result"]


@pytest.mark.asyncio
async def test_worker_live_logger_writes_the_transcript(tmp_path, fake_claude):
    from app import db, runlog

    fake_claude(_emit_script())
    run_id = db.create_run(None, "build", "opus")
    await agent_runner.run_claude(
        "prompt", tmp_path / "ws", "opus", timeout_min=1,
        on_event=worker._live_logger(run_id),
    )
    text, _ = runlog.read_log(run_id, 0)
    assert "* session start  model=opus  tools=1" in text
    assert "> Bash(ls)" in text
    assert "< ok (2 lines)" in text

    run = db.get_run(run_id)
    assert run["events"] == 4
    assert run["last_activity"] == "* run complete  (3 turns, 0.250w)"


@pytest.mark.asyncio
async def test_a_failing_callback_does_not_kill_the_run(tmp_path, fake_claude):
    fake_claude(_emit_script())

    def boom(event, lines):
        raise RuntimeError("live view exploded")

    result = await agent_runner.run_claude(
        "prompt", tmp_path / "ws", "opus", timeout_min=1, on_event=boom
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_non_json_noise_on_stdout_is_ignored(tmp_path, fake_claude):
    fake_claude("echo 'warning: something'\n" + _emit_script())
    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.ok is True
    assert result.num_turns == 3


@pytest.mark.asyncio
async def test_error_result_event_marks_the_run_failed(tmp_path, fake_claude):
    event = {"type": "result", "is_error": True, "result": "usage limit reached"}
    fake_claude(_emit([event]))
    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.ok is False
    assert result.is_rate_limited is True


@pytest.mark.asyncio
async def test_timeout_kills_the_process(tmp_path, fake_claude):
    fake_claude("sleep 30\n")
    result = await agent_runner.run_claude(
        "prompt", tmp_path / "ws", "opus", timeout_min=1 / 60  # 1 second
    )
    assert result.timed_out is True
    assert result.ok is False


@pytest.mark.asyncio
async def test_missing_claude_binary_is_reported_not_raised(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(agent_runner, "_extra_env", lambda: {"PATH": str(empty)})
    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.ok is False
    assert "not found" in result.result_text


@pytest.mark.asyncio
async def test_report_json_is_picked_up_and_stale_ones_cleared(tmp_path, fake_claude):
    workspace = tmp_path / "ws"
    (workspace / ".portal").mkdir(parents=True)
    (workspace / ".portal" / "report.json").write_text('{"summary": "stale"}')

    # A run that writes no report must not inherit the stale one.
    fake_claude(_emit_script())
    result = await agent_runner.run_claude("prompt", workspace, "opus", timeout_min=1)
    assert result.report is None

    report = {"summary": "fresh", "new_status": None}
    fake_claude(
        f"mkdir -p .portal\nprintf '%s' '{json.dumps(report)}' > .portal/report.json\n"
        + _emit_script(),
    )
    result = await agent_runner.run_claude("prompt", workspace, "opus", timeout_min=1)
    assert result.report == report

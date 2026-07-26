"""#224: schema-validated structured output replaces .portal/report.json.

`claude -p --json-schema` exposes a StructuredOutput tool to the agent,
validates what it submits at the tool-call layer, and returns the parsed
object in the result event's `structured_output` field - composing with the
`stream-json` output the portal needs for live progress. These pin:

- the schema itself (round-trips, covers every contract field, requires
  nothing, rejects the stages only Wes may set);
- the spawn flag (present when asked for, absent otherwise);
- report pickup (structured wins over the file, file stays a working
  fallback forever, junk structured output falls through, neither -> None);
- the run summary preferring the report's bullets over the raw JSON the CLI
  echoes as `result` when structured output is used;
- the worker passing the schema on project, reflect and compaction spawns
  but NOT on one-off tasks (which report nothing).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import textwrap

import pytest

from app import agent_runner, db, report_schema, worker


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)

    def install(body: str) -> None:
        script = bindir / "claude"
        script.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    monkeypatch.setattr(agent_runner, "_extra_env", lambda: dict(env))
    return install


def _emit(events: list[dict]) -> str:
    body = "\n".join(json.dumps(e) for e in events)
    return f"cat <<'PORTAL_EOF'\n{body}\nPORTAL_EOF\n"


def _write_file_report(payload: dict) -> str:
    """Shell that writes a legacy .portal/report.json in the run's cwd."""
    return (
        "mkdir -p .portal\n"
        f"cat > .portal/report.json <<'REPORT_EOF'\n{json.dumps(payload)}\nREPORT_EOF\n"
    )


def _result_event(**extra) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False,
            "num_turns": 2, "session_id": "s-1", "result": "raw text", **extra}


# --- the schema itself ------------------------------------------------------


def test_schema_json_round_trips_to_the_schema():
    assert json.loads(report_schema.schema_json()) == report_schema.REPORT_SCHEMA
    assert report_schema.REPORT_SCHEMA["type"] == "object"


def test_schema_covers_every_field_the_contract_documents():
    # The contract's example report block is real JSON; every key it shows the
    # agent must exist in the schema, or the CLI would let a documented field
    # drift out of validation silently.
    block = re.search(r"^\{\n.*?^\}", agent_runner.AGENT_CONTRACT, re.S | re.M)
    assert block is not None
    example = json.loads(block.group(0))
    assert set(example) == set(report_schema.REPORT_SCHEMA["properties"])


def test_schema_requires_nothing():
    # Reflect/compact runs legitimately report one or two fields; a required
    # list would burn turns on retries without improving any report.
    assert "required" not in report_schema.REPORT_SCHEMA


def test_schema_rejects_the_stages_only_wes_may_set():
    # "done"/"abandoned" are Wes's moves; the enum turns the contract's rule
    # into submission-time feedback instead of a silent worker-side drop.
    assert report_schema.REPORT_SCHEMA["properties"]["new_stage"]["enum"] == [
        "review", "active", None,
    ]


# --- the spawn flag ---------------------------------------------------------


def test_build_cmd_passes_the_schema_flag():
    cmd = agent_runner.build_cmd("p", "opus", 400, json_schema='{"type":"object"}')
    idx = cmd.index("--json-schema")
    assert cmd[idx + 1] == '{"type":"object"}'


def test_build_cmd_omits_the_flag_without_a_schema():
    assert "--json-schema" not in agent_runner.build_cmd("p", "opus", 400)


def test_contract_names_structured_output_and_keeps_the_file_fallback():
    assert "StructuredOutput" in agent_runner.AGENT_CONTRACT
    assert ".portal/report.json" in agent_runner.AGENT_CONTRACT


# --- report pickup ----------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_output_wins_over_the_file(tmp_path, fake_claude):
    fake_claude(
        _write_file_report({"summary": ["from the file"]})
        + _emit([_result_event(structured_output={"summary": ["from the tool"]})])
    )
    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.report == {"summary": ["from the tool"]}
    assert result.report_source == "structured"


@pytest.mark.asyncio
async def test_the_file_stays_a_working_fallback(tmp_path, fake_claude):
    fake_claude(
        _write_file_report({"summary": ["from the file"]})
        + _emit([_result_event()])
    )
    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.report == {"summary": ["from the file"]}
    assert result.report_source == "file"


@pytest.mark.asyncio
async def test_non_dict_structured_output_falls_through_to_the_file(tmp_path, fake_claude):
    fake_claude(
        _write_file_report({"summary": ["from the file"]})
        + _emit([_result_event(structured_output="not a report")])
    )
    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.report == {"summary": ["from the file"]}
    assert result.report_source == "file"


@pytest.mark.asyncio
async def test_no_report_anywhere_is_none(tmp_path, fake_claude):
    fake_claude(_emit([_result_event()]))
    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.report is None
    assert result.report_source is None


# --- the run summary line ---------------------------------------------------


@pytest.mark.asyncio
async def test_summary_bullets_replace_the_raw_json_result_text(tmp_path, fake_claude):
    report = {"summary": ["shipped the thing", "note: one more"]}
    fake_claude(_emit([_result_event(
        result=json.dumps(report), structured_output=report,
    )]))
    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.result_text == "shipped the thing; note: one more"


@pytest.mark.asyncio
async def test_a_structured_report_without_bullets_keeps_the_raw_text(tmp_path, fake_claude):
    fake_claude(_emit([_result_event(structured_output={"summary": []})]))
    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.result_text == "raw text"


# --- worker wiring ----------------------------------------------------------


def _capture_run(monkeypatch) -> dict:
    seen: dict = {}

    async def fake(prompt, cwd, model, timeout_min, **kwargs):
        seen.update(kwargs, prompt=prompt)
        return agent_runner.RunResult(ok=True, result_text="done")

    monkeypatch.setattr(agent_runner, "run_claude", fake)
    return seen


@pytest.mark.asyncio
async def test_project_runs_pass_the_report_schema(monkeypatch):
    project = db.create_project(
        "Thing", description="d", stage="active", build_approved=True, slug="thing"
    )
    seen = _capture_run(monkeypatch)
    await worker.run_project_task(project, "build")
    assert json.loads(seen["json_schema"]) == report_schema.REPORT_SCHEMA


def test_reflect_and_compaction_pass_the_report_schema(monkeypatch):
    seen = _capture_run(monkeypatch)
    asyncio.run(worker.run_reflect())
    assert json.loads(seen["json_schema"]) == report_schema.REPORT_SCHEMA
    seen.clear()
    asyncio.run(worker.run_compaction())
    assert json.loads(seen["json_schema"]) == report_schema.REPORT_SCHEMA


def test_oneoff_tasks_do_not_get_the_schema(monkeypatch):
    # One-off agents converse through their final text; their prompt says
    # report.json is not read, so forcing a StructuredOutput tool on them
    # would only invite reports nothing consumes.
    task = db.create_oneoff("scratch task")
    run_id = db.create_run(None, "oneoff", "opus")
    seen = _capture_run(monkeypatch)
    asyncio.run(worker.run_oneoff_task(task["id"], run_id, "opus"))
    assert "json_schema" not in seen or not seen["json_schema"]

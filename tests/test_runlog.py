"""Rendering stream-json events into the live console log, and tailing it."""
from __future__ import annotations

from app import runlog


def test_init_event_reports_model_and_tool_count():
    lines = runlog.render_event(
        {"type": "system", "subtype": "init", "model": "opus", "tools": ["Bash", "Read"]}
    )
    assert lines == ["* session start  model=opus  tools=2"]


def test_assistant_text_and_tool_use_render_separately():
    lines = runlog.render_event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Looking at the tests.\nThen fixing."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
                ]
            },
        }
    )
    # The agent's own words are the unmarked, unindented case as of 2026-07-28;
    # it is the machinery that is set back now. See tests/test_console.py.
    assert lines == ["Looking at the tests.", "Then fixing.", "> Bash(pytest -q)"]


def test_tool_use_uses_the_identifying_field_not_just_the_first():
    # Read's input has file_path plus other keys; the path is what identifies it.
    lines = runlog.render_event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"offset": 3, "file_path": "app/db.py"}}
                ]
            },
        }
    )
    assert lines == ["> Read(app/db.py)"]


def test_unknown_tool_falls_back_to_first_string_input():
    assert runlog.summarize_tool_input("Mystery", {"n": 1, "thing": "hello"}) == "hello"
    assert runlog.summarize_tool_input("Mystery", {"n": 1}) == ""


def test_long_tool_input_is_clipped():
    summary = runlog.summarize_tool_input("Bash", {"command": "x" * 500})
    assert len(summary) == runlog.MAX_SUMMARY_CHARS
    assert summary.endswith("…")


def test_tool_result_ok_and_error():
    ok = runlog.render_event(
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "a\nb\n"}]}}
    )
    assert ok == ["< ok (2 lines)"]
    err = runlog.render_event(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "is_error": True, "content": "boom"}]},
        }
    )
    assert err == ["! error: boom"]


def test_result_event_summarizes_cost_and_turns():
    assert runlog.render_event(
        {"type": "result", "is_error": False, "num_turns": 12, "total_cost_usd": 0.4211}
    ) == ["* run complete  (12 turns, 0.421w)"]
    assert runlog.render_event({"type": "result", "is_error": True, "num_turns": 2}) == [
        "! run failed  (2 turns)"
    ]


def test_unknown_event_types_render_nothing():
    assert runlog.render_event({"type": "stream_event"}) == []
    assert runlog.render_event({}) == []


def test_parse_line_ignores_noise():
    assert runlog.parse_line('{"type": "result"}') == {"type": "result"}
    assert runlog.parse_line("not json") is None
    assert runlog.parse_line("  ") is None
    assert runlog.parse_line("[1, 2]") is None  # valid JSON, but not an event


def test_read_log_is_incremental_by_offset():
    log = runlog.RunLog(7)
    log.append(["first"])
    text, offset = runlog.read_log(7, 0)
    assert text == "first\n"

    log.append(["second", "third"])
    text2, offset2 = runlog.read_log(7, offset)
    assert text2 == "second\nthird\n"
    assert offset2 > offset

    # Nothing new -> empty delta, same offset.
    assert runlog.read_log(7, offset2) == ("", offset2)


def test_read_log_recovers_from_an_offset_past_the_end():
    log = runlog.RunLog(9)
    log.append(["short"])
    text, offset = runlog.read_log(9, 10_000)
    assert text == "short\n"
    assert offset == len("short\n")


def test_read_log_missing_file():
    assert runlog.read_log(404, 0) == ("", 0)


def test_new_runlog_truncates_a_reused_id():
    runlog.RunLog(3).append(["old"])
    runlog.RunLog(3)
    assert runlog.read_log(3, 0) == ("", 0)


def test_prune_keeps_only_the_newest_logs():
    for run_id in range(10):
        runlog.RunLog(run_id).append(["x"])
    assert runlog.prune(keep=3) == 7
    remaining = sorted(p.name for p in runlog.config.RUNS_DIR.glob("*.log"))
    assert len(remaining) == 3
    # The newest ids survive; a fresh run's own log is never the one dropped.
    assert "9.log" in remaining


def test_prune_on_an_empty_dir_is_a_no_op():
    assert runlog.prune() == 0


def test_tail_returns_only_the_last_lines():
    log = runlog.RunLog(4)
    log.append([f"line {i}" for i in range(100)])
    assert runlog.tail(4, max_lines=3) == "line 97\nline 98\nline 99"

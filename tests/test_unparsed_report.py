"""A report the CLI could not parse is not a report.

When the model's StructuredOutput argument is not valid JSON, the CLI does not
fail the call - it substitutes `{"__unparsedToolInput": ...}` and carries on.
That placeholder is a dict, so the portal accepted it as the report itself and
run 897 (Commander Case, 2026-08-07) lost its entire report while being
recorded a success, with the raw JSON blob standing in as both the run summary
on the project page and the project's journal entry (journal id 1537).

These pin the three halves of the fix: the placeholder is refused, as much of
the real report as survives inside it is rescued, and what cannot be rescued
is said out loud instead of passing quietly.
"""
from __future__ import annotations

import json
import os
import stat
import textwrap

import pytest

from app import agent_runner, report_schema, unparsedreport, worker

REPORT = {
    "summary": ["the lid slides on from the front now", "note: still waiting on keys"],
    "journal_entry_md": "## The lid slides\n\nA long entry.",
    "preview_url": "https://example.com/case/",
    "suggestion": None,
}


def _string_shape(text: str) -> dict:
    """The placeholder as it was actually stored on run 897: the tool input as
    a plain string."""
    return {unparsedreport.UNPARSED_KEY: text}


def _object_shape(text: str) -> dict:
    """The placeholder CLI 2.1.223 constructs: `{raw, len}`, with `raw`
    truncated to 2048 characters."""
    return {unparsedreport.UNPARSED_KEY: {"raw": text[:2048], "len": len(text)}}


def _stringified(report: dict) -> str:
    """What a model that stringifies its tool argument submits - the shape run
    897 hit, where the whole report survives one level down."""
    return json.dumps({"raw": json.dumps(report)})


# --- refusing the placeholder ----------------------------------------------


def test_the_placeholder_is_recognized_as_not_a_report():
    assert unparsedreport.is_unparsed(_string_shape("{"))
    assert unparsedreport.is_unparsed(_object_shape("{"))


def test_a_real_report_is_not_mistaken_for_the_placeholder():
    assert not unparsedreport.is_unparsed(REPORT)
    assert not unparsedreport.is_unparsed({"summary": ["fine"]})


def test_a_non_dict_structured_output_is_not_the_placeholder():
    for value in ("a string", None, 7, ["a", "list"]):
        assert not unparsedreport.is_unparsed(value)


# --- reading the raw text out of either shape ------------------------------


def test_the_raw_text_is_read_from_the_string_shape():
    assert unparsedreport.raw_text(_string_shape('{"summary": []}')) == '{"summary": []}'


def test_the_raw_text_is_read_from_the_object_shape():
    assert unparsedreport.raw_text(_object_shape('{"summary": []}')) == '{"summary": []}'


def test_a_placeholder_with_no_readable_text_gives_none():
    assert unparsedreport.raw_text({unparsedreport.UNPARSED_KEY: {"len": 12}}) is None
    assert unparsedreport.raw_text({unparsedreport.UNPARSED_KEY: 12}) is None


# --- rescuing the report ----------------------------------------------------


def test_a_report_the_model_stringified_is_recovered_whole():
    # Run 897's exact shape: the tool input decoded to {"raw": "<report>"} and
    # every field of the report was intact inside it.
    assert unparsedreport.recover(_string_shape(_stringified(REPORT))) == REPORT


def test_the_same_rescue_works_through_the_object_shape():
    assert unparsedreport.recover(_object_shape(_stringified(REPORT))) == REPORT


def test_a_report_submitted_as_plain_text_json_is_recovered():
    assert unparsedreport.recover(_string_shape(json.dumps(REPORT))) == REPORT


def test_a_truncated_report_keeps_the_fields_that_arrived_whole():
    # The CLI truncates its own placeholder at 2048 characters, so a big report
    # arrives cut off mid-value. The leading fields are still complete, and
    # `summary` leads the contract - which is the line Wes actually reads.
    big = dict(REPORT, journal_entry_md="x" * 4000)
    got = unparsedreport.recover(_object_shape(json.dumps(big)))
    assert got is not None
    assert got["summary"] == REPORT["summary"]


def test_the_rescue_keeps_the_leading_fields_not_the_trailing_ones():
    # A salvage that closed the object from the END would return the tail and
    # drop the summary, which is the one field worth having. Cut part way into
    # the journal entry, so `summary` is complete and nothing after it is.
    text = json.dumps(REPORT)
    cut = text[: text.index('"journal_entry_md"') + 30]
    got = unparsedreport.recover(_string_shape(cut))
    assert got is not None
    assert got["summary"] == REPORT["summary"]
    assert "journal_entry_md" not in got
    assert "preview_url" not in got


def test_a_bullet_containing_a_brace_or_quote_does_not_confuse_the_salvage():
    # Depth is counted outside strings only; a bullet full of JSON-looking text
    # would otherwise unbalance the scan and lose the whole report.
    tricky = {
        "summary": ['he asked for {"a": [1,2]} in a bullet, "quoted" and all'],
        "journal_entry_md": "y" * 4000,
    }
    got = unparsedreport.recover(_object_shape(json.dumps(tricky)))
    assert got is not None
    assert got["summary"] == tricky["summary"]


def test_the_cut_is_never_taken_inside_a_nested_structure():
    # Counting commas at any depth instead of only the top level puts the cut
    # inside `todo_updates`, and closing the object there produces JSON that
    # does not parse - so a report that could have kept its summary keeps
    # nothing at all.
    text = json.dumps({
        "summary": ["one"],
        "todo_updates": {"add": [{"text": "x"}], "done": [1, 2, 3]},
    })
    got = unparsedreport.recover(_string_shape(text[: text.index('"done"') + 12]))
    assert got == {"summary": ["one"]}


def test_an_odd_escaped_quote_does_not_desynchronize_the_scan():
    # A field holding one escaped quote flips the scanner's idea of where
    # strings start if escapes are not honored: everything after it reads as
    # one long string, the top-level comma that follows is never seen, and the
    # field is dropped even though it arrived complete. An EVEN number of
    # escaped quotes re-synchronizes by accident, which is why this test uses
    # one and not two.
    text = json.dumps({
        "summary": ["ok"],
        "description": 'he said "go',
        "journal_entry_md": "y" * 50,
    })
    got = unparsedreport.recover(
        _string_shape(text[: text.index('"journal_entry_md"') + 40])
    )
    assert got == {"summary": ["ok"], "description": 'he said "go'}


def test_an_escaped_backslash_before_the_cut_does_not_shift_the_scan():
    tricky = {"summary": ["a path ending in a backslash \\\\"], "blocked_on": "z" * 4000}
    got = unparsedreport.recover(_object_shape(json.dumps(tricky)))
    assert got is not None
    assert got["summary"] == tricky["summary"]


# --- refusing to invent one -------------------------------------------------


def test_text_that_is_not_a_report_at_all_recovers_nothing():
    assert unparsedreport.recover(_string_shape("I'm sorry, I can't do that")) is None
    assert unparsedreport.recover(_string_shape("")) is None


def test_a_decoded_object_with_no_report_field_is_not_a_report():
    # An object is not evidence of a report. Returning one here would put a
    # made-up report where the worker expects the agent's own words.
    assert unparsedreport.recover(_string_shape('{"mood": "red", "x": 1}')) is None


def test_a_truncated_payload_with_nothing_complete_recovers_nothing():
    assert unparsedreport.recover(_string_shape('{"summary": ["half a bul')) is None


def test_the_rescue_never_raises():
    for junk in (None, 7, {"__unparsedToolInput": {"raw": {"nested": "dict"}}},
                 _string_shape("{" * 500)):
        assert unparsedreport.recover(junk) is None


def test_the_recognized_fields_come_from_the_schema_not_a_hand_list():
    # Derived, so a new contract field cannot make a report unrecognizable by
    # being the only field a run sent.
    assert set(report_schema.REPORT_SCHEMA["properties"]) <= unparsedreport.REPORT_KEYS
    assert "new_status" in unparsedreport.REPORT_KEYS


def test_the_failure_note_says_the_work_survived():
    note = unparsedreport.failure_note(_string_shape("x" * 30))
    assert "could not" in note and "30 characters" in note
    assert "committed" in note


# --- the argument envelope --------------------------------------------------
# `{"parameter": "<the whole report as a JSON string>"}`. No placeholder key,
# no error, nothing to give it away: a valid dict holding no report at all.


def _enveloped(report: dict) -> dict:
    """The envelope as it actually arrived on run 976: the whole report, as a
    JSON string, under a key that is not a contract field."""
    return {"parameter": json.dumps(report)}


def test_the_envelope_shape_found_on_run_976_is_unwrapped():
    envelope = _enveloped(REPORT)
    assert unparsedreport.unwrap_envelope(envelope) == REPORT


def test_an_envelope_holding_the_object_rather_than_its_text_is_unwrapped():
    assert unparsedreport.unwrap_envelope({"parameter": REPORT}) == REPORT


def test_a_real_report_is_never_unwrapped():
    assert unparsedreport.unwrap_envelope(REPORT) is None


def test_a_report_that_quotes_another_report_is_returned_as_itself():
    # The guard that matters, and not a hypothetical one: a run reporting on
    # THIS bug pastes an envelope's JSON into its journal entry. Without the
    # "already a report" check the outer report is thrown away and the thing
    # it was writing about takes its place.
    quoting = {
        "summary": ["fixed the envelope bug"],
        "journal_entry_md": '{"summary": ["what the envelope held"]}',
    }
    assert unparsedreport.unwrap_envelope(quoting) is None


def test_an_envelope_of_junk_unwraps_to_nothing():
    assert unparsedreport.unwrap_envelope({"parameter": "not json at all"}) is None
    assert unparsedreport.unwrap_envelope({"parameter": {"nope": 1}}) is None


def test_an_ambiguous_envelope_is_refused_rather_than_guessed_at():
    two = {"a": json.dumps(REPORT), "b": json.dumps({"summary": ["other"]})}
    assert unparsedreport.unwrap_envelope(two) is None


def test_a_non_dict_is_not_an_envelope():
    for junk in ("text", ["a"], None, 7):
        assert unparsedreport.unwrap_envelope(junk) is None


def test_looks_like_a_report_needs_only_one_contract_field():
    # A reflect run reports two fields; a run that only answers a question
    # reports one.
    assert unparsedreport.looks_like_report({"summary": []})
    assert not unparsedreport.looks_like_report({"parameter": "x"})


# --- the runner -------------------------------------------------------------


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
    return (
        "mkdir -p .portal\n"
        f"cat > .portal/report.json <<'REPORT_EOF'\n{json.dumps(payload)}\nREPORT_EOF\n"
    )


def _result_event(structured, result_text: str) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
            "session_id": "s-1", "result": result_text,
            "structured_output": structured}


@pytest.mark.asyncio
async def test_a_rescued_report_is_used_and_marked_recovered(tmp_path, fake_claude):
    placeholder = _string_shape(_stringified(REPORT))
    fake_claude(_emit([_result_event(placeholder, json.dumps(placeholder))]))
    result = await agent_runner.run_claude("p", tmp_path / "ws", "opus", timeout_min=1)
    assert result.report == REPORT
    assert result.report_source == "recovered"
    assert result.report_unreadable is True


@pytest.mark.asyncio
async def test_a_rescued_report_puts_its_bullets_in_the_run_summary(tmp_path, fake_claude):
    placeholder = _string_shape(_stringified(REPORT))
    fake_claude(_emit([_result_event(placeholder, json.dumps(placeholder))]))
    result = await agent_runner.run_claude("p", tmp_path / "ws", "opus", timeout_min=1)
    assert result.result_text.startswith("the lid slides on from the front now")
    assert unparsedreport.UNPARSED_KEY not in result.result_text


@pytest.mark.asyncio
async def test_an_unrecoverable_placeholder_is_never_the_report(tmp_path, fake_claude):
    placeholder = _string_shape("not json at all")
    fake_claude(_emit([_result_event(placeholder, json.dumps(placeholder))]))
    result = await agent_runner.run_claude("p", tmp_path / "ws", "opus", timeout_min=1)
    assert result.report is None
    assert result.report_source is None
    assert result.report_unreadable is True


@pytest.mark.asyncio
async def test_the_blob_never_reaches_the_run_summary(tmp_path, fake_claude):
    # The whole visible symptom on run 897: this JSON was the summary banner on
    # the project page and the project's journal entry for the run.
    placeholder = _string_shape("not json at all")
    fake_claude(_emit([_result_event(placeholder, json.dumps(placeholder))]))
    result = await agent_runner.run_claude("p", tmp_path / "ws", "opus", timeout_min=1)
    assert unparsedreport.UNPARSED_KEY not in result.result_text
    assert "could not read its report" in result.result_text


@pytest.mark.asyncio
async def test_an_unparseable_call_falls_through_to_the_report_file(tmp_path, fake_claude):
    # The legacy file is the fallback that already existed; the placeholder used
    # to shadow it, so a run that wrote both still lost its report.
    placeholder = _string_shape("not json at all")
    fake_claude(
        _write_file_report({"summary": ["from the file"]})
        + _emit([_result_event(placeholder, json.dumps(placeholder))])
    )
    result = await agent_runner.run_claude("p", tmp_path / "ws", "opus", timeout_min=1)
    assert result.report == {"summary": ["from the file"]}
    assert result.report_source == "file"
    assert result.result_text == "from the file"
    assert result.report_unreadable is True


@pytest.mark.asyncio
async def test_an_enveloped_report_is_unwrapped_by_the_runner(tmp_path, fake_claude):
    envelope = _enveloped(REPORT)
    fake_claude(_emit([_result_event(envelope, json.dumps(envelope))]))
    result = await agent_runner.run_claude("p", tmp_path / "ws", "opus", timeout_min=1)
    assert result.report == REPORT
    assert result.report_source == "recovered"
    # Not the CLI's could-not-parse placeholder: the call parsed fine, it was
    # just wrapped. Saying "unreadable" here would file a false alarm.
    assert result.report_unreadable is False


@pytest.mark.asyncio
async def test_an_envelope_never_reaches_the_run_summary(tmp_path, fake_claude):
    # What Wes saw on the KvK project: this JSON was the summary banner and the
    # journal entry for runs 962, 973 and 976.
    envelope = _enveloped(REPORT)
    fake_claude(_emit([_result_event(envelope, json.dumps(envelope))]))
    result = await agent_runner.run_claude("p", tmp_path / "ws", "opus", timeout_min=1)
    assert "parameter" not in result.result_text
    assert result.result_text.startswith("the lid slides on from the front now")


@pytest.mark.asyncio
async def test_an_envelope_the_runner_cannot_open_still_yields_no_report(tmp_path, fake_claude):
    envelope = {"parameter": "not json at all"}
    fake_claude(_emit([_result_event(envelope, json.dumps(envelope))]))
    result = await agent_runner.run_claude("p", tmp_path / "ws", "opus", timeout_min=1)
    # Nothing to unwrap, so the dict stands as the report it claims to be -
    # but it carries no bullets and no journal entry, so the summary is blank
    # rather than the envelope's JSON.
    assert result.result_text == ""


@pytest.mark.asyncio
async def test_a_normal_run_is_not_marked_unreadable(tmp_path, fake_claude):
    fake_claude(_emit([_result_event({"summary": ["all fine"]}, "raw")]))
    result = await agent_runner.run_claude("p", tmp_path / "ws", "opus", timeout_min=1)
    assert result.report_source == "structured"
    assert result.report_unreadable is False


# --- the worker's note ------------------------------------------------------


def _entries(monkeypatch) -> list:
    got: list = []
    monkeypatch.setattr(worker.db, "add_journal",
                        lambda pid, a, k, t: got.append((pid, a, k, t)))
    return got


def test_a_lost_report_gets_its_own_journal_line(monkeypatch):
    got = _entries(monkeypatch)
    worker._note_unreadable_report(
        {"id": 7, "slug": "demo"},
        agent_runner.RunResult(ok=True, report=None, report_unreadable=True),
        "build",
    )
    assert len(got) == 1
    pid, author, kind, text = got[0]
    assert (pid, author, kind) == (7, "system", "status")
    assert "could not parse" in text and "lost" in text


def test_a_rescued_report_says_it_was_rescued(monkeypatch):
    got = _entries(monkeypatch)
    worker._note_unreadable_report(
        {"id": 7, "slug": "demo"},
        agent_runner.RunResult(ok=True, report=REPORT, report_unreadable=True),
        "build",
    )
    assert len(got) == 1
    assert "recovered" in got[0][3]
    assert "lost" not in got[0][3]


def test_an_ordinary_run_gets_no_note(monkeypatch):
    got = _entries(monkeypatch)
    worker._note_unreadable_report(
        {"id": 7, "slug": "demo"},
        agent_runner.RunResult(ok=True, report=REPORT),
        "build",
    )
    assert got == []


def test_the_note_never_breaks_a_finished_run(monkeypatch):
    def boom(*a):
        raise RuntimeError("journal is down")

    monkeypatch.setattr(worker.db, "add_journal", boom)
    worker._note_unreadable_report(
        {"id": 7, "slug": "demo"},
        agent_runner.RunResult(ok=True, report=None, report_unreadable=True),
        "build",
    )

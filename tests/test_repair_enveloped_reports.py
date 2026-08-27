"""The one-shot repair for rows that hold a serialized report instead of prose.

Two bugs wrote them (an argument envelope the portal mistook for a report, and
a bullet-less structured report leaving the CLI's JSON `result` in place); both
are fixed in app/agent_runner.py, and this script reaches the rows already on
disk. These pin the parts that could quietly do damage: prose is never
touched, a journal entry is never blanked, a truncated run summary is repaired
from the journal row it is a prefix of, and re-running changes nothing.
"""
from __future__ import annotations

import importlib.util
import os
import json
import sqlite3
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "repair_enveloped_reports",
    Path(__file__).resolve().parent.parent / "deploy" / "repair_enveloped_reports.py",
)
repair = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(repair)

REPORT = {
    "summary": ["the popup was off for all 40 players", "note: 26 still owe an answer"],
    "journal_entry_md": "## The confirm popup was not flaky\n\nIt was off.",
    "new_stage": "review",
}
ENVELOPE_TEXT = json.dumps({"parameter": json.dumps(REPORT)})

# A report whose first bullet is longer than the 500 characters a run summary
# is stored in, so the truncated row genuinely cannot be read on its own and
# the journal is the only place the bullets survive.
LONG_REPORT = {
    "summary": ["y" * 600, "the second bullet"],
    "journal_entry_md": "## What the run did\n\nbody",
}
LONG_ENVELOPE = json.dumps({"parameter": json.dumps(LONG_REPORT)})


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, summary TEXT, report_summary TEXT)"
    )
    conn.execute("CREATE TABLE journal (id INTEGER PRIMARY KEY, content_md TEXT)")
    return conn


def _add_run(conn, rid, summary, report_summary=None):
    conn.execute("INSERT INTO runs VALUES (?, ?, ?)", (rid, summary, report_summary))


def _rows(conn):
    return (
        {r[0]: (r[1], r[2]) for r in conn.execute("SELECT * FROM runs")},
        dict(conn.execute("SELECT id, content_md FROM journal").fetchall()),
    )


# --- recognizing what is really stored --------------------------------------


def test_an_envelope_is_recognized_and_opened():
    assert repair.report_in(ENVELOPE_TEXT) == REPORT


def test_a_bare_serialized_report_is_recognized():
    assert repair.report_in(json.dumps(REPORT)) == REPORT


def test_an_envelope_cut_off_mid_string_still_gives_up_its_leading_fields():
    # A run summary is stored truncated at 500 characters, so the closing
    # quote and brace are simply not there and no decoder will touch it.
    big = dict(REPORT, journal_entry_md="x" * 4000)
    got = repair.report_in(json.dumps({"parameter": json.dumps(big)})[:600])
    assert got is not None
    assert got["summary"] == REPORT["summary"]


def test_a_cut_landing_mid_escape_still_opens_the_envelope():
    # Character 40 of this payload lands on an odd run of backslashes, so
    # closing the string without dropping the dangling escape re-opens the
    # very string it means to close and the whole row is given up on.
    whole = json.dumps({"parameter": json.dumps(
        {"summary": ["ok"], "description": 'he said "go"'}
    )})
    assert whole[:40].endswith("\\")
    assert repair.report_in(whole[:40]) == {"summary": ["ok"]}


def test_no_cut_position_raises_or_invents_a_summary():
    # Cutting one character at a time walks the truncation through every
    # escape sequence in the payload; none of them may raise or return junk.
    whole = json.dumps({"parameter": json.dumps(REPORT)})
    for cut in range(20, len(whole)):
        got = repair.report_in(whole[:cut])
        assert got is None or got["summary"][0] == REPORT["summary"][0]


def test_a_truncated_envelope_holding_something_else_is_refused():
    # Salvaging a cut-off object always yields *something*; only a contract
    # field makes it a report. Without that check this row would be "repaired"
    # by blanking a summary that was never a report at all.
    assert repair.report_in('{"parameter": "{\\"mood\\": \\"red\\", \\"x\\": 1') is None


def test_prose_is_never_mistaken_for_a_report():
    for text in (
        "Shipped the thing; note: one more",
        "{not json}",
        '{"mood": "red"}',
        "",
        None,
    ):
        assert repair.report_in(text) is None
        assert not repair.looks_serialized(text)


def test_an_unrecoverable_serialization_is_still_recognized_as_one():
    # Nothing can be read back out of this, but it is plainly not prose - and
    # leaving JSON in the summary column is the whole complaint.
    assert repair.looks_serialized('{"summary": ["a bullet cut off right he')


# --- what a repaired row says -----------------------------------------------


def test_a_repaired_summary_is_the_bullets():
    assert repair.summary_line(REPORT).startswith("the popup was off for all 40")
    assert "note: 26 still owe an answer" in repair.summary_line(REPORT)


def test_a_bulletless_report_falls_back_to_the_journal_heading():
    got = repair.summary_line({"summary": [], "journal_entry_md": "## Reflected\n\nx"})
    assert got == "Reflected"


def test_a_report_with_nothing_human_in_it_summarizes_to_nothing():
    # Every reflect run: `summary: []`, no journal entry. A blank cell is
    # honest; a serialized object is not.
    assert repair.summary_line({"summary": [], "journal_entry_md": None}) == ""


def test_a_repaired_summary_is_capped_like_the_column():
    assert len(repair.summary_line({"summary": ["x" * 900]})) == repair.SUMMARY_CHARS


def test_the_bullet_block_is_one_bullet_a_line():
    assert repair.bullet_block(REPORT) == "\n".join(REPORT["summary"])


def test_a_journal_entry_is_never_blanked():
    # Losing the run's account of itself is worse than showing the JSON of it,
    # so a report with no entry leaves the row exactly as it was.
    assert repair.journal_body({"summary": ["a"]}) is None
    assert repair.journal_body({"journal_entry_md": "   "}) is None


# --- planning and applying --------------------------------------------------


def test_a_truncated_run_summary_is_repaired_from_its_journal_row(db):
    truncated = LONG_ENVELOPE[:500]
    assert repair.report_in(truncated) is None, "the row must be beyond self-repair"
    _add_run(db, 976, truncated)
    db.execute("INSERT INTO journal VALUES (1741, ?)", (LONG_ENVELOPE,))
    repair.apply(db, *repair.plan(db))
    runs, entries = _rows(db)
    # The whole report, not the 500 characters that survived in the run row.
    assert runs[976][0] == "; ".join(LONG_REPORT["summary"])[:500]
    assert runs[976][1] == "\n".join(LONG_REPORT["summary"])
    assert entries[1741] == LONG_REPORT["journal_entry_md"]


def test_a_whole_run_summary_is_repaired_without_needing_a_journal_row(db):
    _add_run(db, 5, ENVELOPE_TEXT)
    repair.apply(db, *repair.plan(db))
    assert _rows(db)[0][5][0] == "; ".join(REPORT["summary"])


def test_an_unrecoverable_summary_is_emptied_rather_than_left_as_json(db):
    _add_run(db, 973, '{"summary": ["a bullet cut off right he')
    repair.apply(db, *repair.plan(db))
    assert _rows(db)[0][973] == ("", None)


def test_prose_rows_are_left_out_of_the_plan(db):
    _add_run(db, 1, "shipped the thing")
    db.execute("INSERT INTO journal VALUES (1, '## A heading\n\nbody')")
    assert repair.plan(db) == ([], [])


def test_running_it_twice_changes_nothing_the_second_time(db):
    _add_run(db, 976, LONG_ENVELOPE[:500])
    db.execute("INSERT INTO journal VALUES (1741, ?)", (LONG_ENVELOPE,))
    repair.apply(db, *repair.plan(db))
    after_once = _rows(db)
    assert repair.plan(db) == ([], [])
    repair.apply(db, *repair.plan(db))
    assert _rows(db) == after_once


def test_a_journal_row_with_no_entry_in_its_report_survives_untouched(db):
    stored = json.dumps({"parameter": json.dumps({"summary": ["only bullets"]})})
    db.execute("INSERT INTO journal VALUES (9, ?)", (stored,))
    repair.apply(db, *repair.plan(db))
    assert _rows(db)[1][9] == stored


def test_a_summary_matching_two_different_reports_is_paired_with_neither(db):
    # Ambiguity is refused: taking the first match would put another run's
    # words on this run's page, which is worse than showing nothing.
    def envelope(tail):
        return json.dumps({"parameter": json.dumps({"summary": ["a shared opening line, then " + tail]})})

    shared = os.path.commonprefix([envelope("tail A"), envelope("tail B")])
    _add_run(db, 5, shared)
    db.execute("INSERT INTO journal VALUES (1, ?)", (envelope("tail A"),))
    db.execute("INSERT INTO journal VALUES (2, ?)", (envelope("tail B"),))
    repair.apply(db, *repair.plan(db))
    # Nothing survives the cut on its own either, so the row is emptied rather
    # than filled in with one of the two candidates.
    assert _rows(db)[0][5][0] == ""

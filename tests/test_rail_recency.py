"""What "recent" means on the side rail - the last real event, not the last write.

Wes, 2026-08-07:

  "Also, the recents in the bar on the left side doesn't seem to actually
  reflect projects that were used/run/notes added to most recently sorted to
  the top as I think it should."

It didn't. The rail sorted on `projects.updated_at`, which `db.update_project`
is the only writer of - so it moves when a stage, title, description or preview
URL changes and stays put when an agent runs for half an hour, journals four
paragraphs and reports back. On the morning he wrote that, the live board had
Commander Case with a run in flight sitting fifth (updated_at 08:33, run
started 15:32), and OpenJournal with a journal entry from that afternoon
sitting sixteenth on a July timestamp.

The three named in his sentence - used, run, notes added - plus everything else
that lands in the journal, are now what the sort reads, via
`db.last_activity_at`. These tests pin each of the three separately, because
they come from two different tables and a fix that covered only one would look
right on the day it shipped.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import db, sidebar


def _project(title, slug, stage="active", **kw):
    return db.create_project(title, description="x", stage=stage, slug=slug, **kw)


def _stamp_project(row, when):
    """Force `projects.updated_at`, the column the rail used to sort on."""
    conn = db.get_conn()
    with db._LOCK:  # noqa: SLF001
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (when, row["id"]))
        conn.commit()
    return db.get_project(row["id"])


def _stamp_journal(project_id, when, author="agent", kind="progress"):
    conn = db.get_conn()
    with db._LOCK:  # noqa: SLF001
        conn.execute(
            "INSERT INTO journal (project_id, ts, author, kind, content_md) "
            "VALUES (?, ?, ?, ?, 'x')",
            (project_id, when, author, kind),
        )
        conn.commit()


def _stamp_run(project_id, started, ended=None):
    conn = db.get_conn()
    with db._LOCK:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (project_id, task, model, started_at, ended_at, status) "
            "VALUES (?, 'build', 'sonnet', ?, ?, 'ok')",
            (project_id, started, ended),
        )
        conn.commit()


OLD = "2026-07-01T00:00:00+00:00"
NEW = "2026-08-07T15:32:00+00:00"


# --- db.last_activity_at, the measurement -----------------------------------

def test_a_project_with_nothing_on_it_has_no_activity(temp_data_dir):
    _project("Fresh", "fresh")
    assert db.last_activity_at() == {}


def test_a_run_starting_counts(temp_data_dir):
    """The case from his morning: an agent working on it right now, and no end
    timestamp to read yet. A measurement that only looked at `ended_at` would
    rank the one project visibly in motion as the stalest on the board."""
    project = _project("Running", "running")
    _stamp_run(project["id"], NEW, ended=None)
    assert db.last_activity_at()[project["id"]] == NEW


def test_a_run_ending_counts(temp_data_dir):
    project = _project("Finished", "finished")
    _stamp_run(project["id"], OLD, ended=NEW)
    assert db.last_activity_at()[project["id"]] == NEW


def test_a_journal_entry_counts(temp_data_dir):
    """Which is how notes, agent reports and status changes all count: every
    one of them lands in this table, so one union arm covers all three."""
    project = _project("Journalled", "journalled")
    _stamp_journal(project["id"], NEW)
    assert db.last_activity_at()[project["id"]] == NEW


def test_a_note_counts(temp_data_dir):
    project = _project("Noted", "noted")
    db.add_journal(project["id"], "user", "note", "do the thing")
    assert project["id"] in db.last_activity_at()


def test_the_newest_of_all_three_wins(temp_data_dir):
    project = _project("Busy", "busy")
    _stamp_journal(project["id"], OLD)
    _stamp_run(project["id"], "2026-07-15T00:00:00+00:00", ended=NEW)
    assert db.last_activity_at()[project["id"]] == NEW


def test_runs_and_journal_rows_with_no_project_are_left_out(temp_data_dir):
    """The reflect and compaction jobs write runs with a NULL project_id, and
    so do one-off tasks. A GROUP BY that kept them would put a `None` key in a
    map every row is looked up in by integer id."""
    conn = db.get_conn()
    with db._LOCK:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (project_id, task, model, started_at, status) "
            "VALUES (NULL, 'reflect', 'sonnet', ?, 'ok')", (NEW,))
        conn.execute(
            "INSERT INTO journal (project_id, ts, author, kind, content_md) "
            "VALUES (NULL, ?, 'system', 'status', 'x')", (NEW,))
        conn.commit()
    assert db.last_activity_at() == {}


# --- the rail using it ------------------------------------------------------

def test_a_running_project_outranks_one_whose_row_was_written_later(temp_data_dir):
    """The exact inversion he reported, reduced to two rows.

    `stale` has the newer `updated_at`; `worked` has the newer run. Sorting on
    the column alone puts them the wrong way round, and no amount of staring at
    the rail explains why - both look like ordinary active projects.
    """
    worked = _stamp_project(_project("Worked On", "worked"), OLD)
    stale = _stamp_project(_project("Row Rewritten", "stale"), "2026-08-07T08:33:00+00:00")
    _stamp_run(worked["id"], NEW, ended=None)

    rail = sidebar.build([worked, stale], activity=db.last_activity_at(), mode="recent")

    assert [r["slug"] for r in rail["shelves"][0]["rows"]] == ["worked", "stale"]


def test_a_note_moves_a_project_up_the_rail(temp_data_dir):
    """"projects that were ... notes added to most recently sorted to the top"."""
    quiet_one = _stamp_project(_project("Untouched", "untouched"),
                               "2026-08-07T12:00:00+00:00")
    noted = _stamp_project(_project("Noted", "noted"), OLD)
    _stamp_journal(noted["id"], NEW, author="user", kind="note")

    rail = sidebar.build([quiet_one, noted], activity=db.last_activity_at(), mode="recent")

    assert rail["shelves"][0]["rows"][0]["slug"] == "noted"


def test_the_more_tail_is_ordered_the_same_way(temp_data_dir):
    """Backlog and paused projects tail behind the working shelves, and that
    group exists to answer "where did X go" - so it has to read the same clock
    as the list above it, not the old column."""
    old = _stamp_project(_project("Idea", "idea", stage="backlog"),
                         "2026-08-07T12:00:00+00:00")
    touched = _stamp_project(_project("Touched Idea", "touched", stage="backlog"), OLD)
    _stamp_run(touched["id"], NEW, ended=NEW)

    rail = sidebar.build([old, touched], activity=db.last_activity_at(), mode="recent")

    assert [r["slug"] for r in rail["shelves"][-1]["rows"]] == ["touched", "idea"]


def test_a_project_with_no_events_still_sorts_on_its_own_row(temp_data_dir):
    """A brand-new project has no runs and no journal, and must not fall to the
    bottom of the rail under a blank timestamp - `updated_at` is the floor."""
    fresh = _stamp_project(_project("Fresh", "fresh"), NEW)
    old = _stamp_project(_project("Old", "old"), OLD)
    _stamp_run(old["id"], "2026-08-01T00:00:00+00:00", ended=None)

    rail = sidebar.build([fresh, old], activity=db.last_activity_at(), mode="recent")

    assert [r["slug"] for r in rail["shelves"][0]["rows"]] == ["fresh", "old"]


def test_the_row_note_reads_from_the_same_clock(temp_data_dir):
    """The rail's fallback caption is "how long it has been since anything
    happened" - base.html says so in as many words. Left on `updated_at` it
    would have told him a project an agent finished an hour ago had been
    sitting for five weeks."""
    project = _stamp_project(_project("Worked", "worked", stage="review"), OLD)
    _stamp_run(project["id"], NEW, ended=NEW)

    rail = sidebar.build([project], activity=db.last_activity_at(), mode="recent")

    assert rail["shelves"][0]["rows"][0]["since"] == NEW


def test_the_rail_is_unchanged_when_no_activity_is_supplied(temp_data_dir):
    """`activity` is optional, because `sidebar.build` is a pure function every
    other test in the suite calls without a database behind it."""
    old = _stamp_project(_project("Old", "old"), OLD)
    new = _stamp_project(_project("New", "new"), NEW)

    rail = sidebar.build([old, new], mode="recent")

    assert [r["slug"] for r in rail["shelves"][0]["rows"]] == ["new", "old"]


# --- and the live page ------------------------------------------------------

def test_the_rendered_rail_puts_the_worked_on_project_first(temp_data_dir):
    """End to end, because `sidebar.build` being right is worth nothing if the
    route never hands it the map."""
    from app import main

    worked = _stamp_project(_project("Worked On", "worked"), OLD)
    _stamp_project(_project("Row Rewritten", "stale"), "2026-08-07T08:33:00+00:00")
    _stamp_run(worked["id"], NEW, ended=None)

    body = TestClient(main.app).get("/").text
    rail = body.split('id="rail-projects"')[1]
    assert rail.index("Worked On") < rail.index("Row Rewritten")

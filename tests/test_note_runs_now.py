"""Wes, 2026-08-10: "Make the 'Add note' button automatically run now if the
project can be run now. Change the 'Queue and don't run' button to just say
'queue note'".

Writing the note IS the request, so the plain green button now queues a run
whenever one could actually start - and "add & run now" only survives on the
page in the cases where it would NOT (a project on a shelf a run does not
belong on, or one an agent is already working in).

The load-bearing question is what "can be run now" means, so most of this file
is `worker.can_run_now` at its boundaries: the two things that genuinely stop a
manual run are the shelf and a busy workspace, and *nothing else must creep in*
- a manual run deliberately bypasses the daily cap, quiet hours, the saturation
gap and the crash-loop hold, so consulting those would answer a more
pessimistic question than Wes asked.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from app import db, main, worker


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clean_worker_state():
    def reset():
        worker._inflight.clear()
        worker._wake = asyncio.Event()
        while not worker.manual_queue.empty():
            worker.manual_queue.get_nowait()

    reset()
    yield
    reset()


def queued() -> list[int]:
    return list(worker.manual_queue._queue)  # type: ignore[attr-defined]


def busy(project_id: int) -> int:
    """A run in flight on this project, as the runs table records one."""
    return db.create_run(project_id, "build", "opus")


# --------------------------------------------------------------------------
# can_run_now
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["active", "review"])
def test_the_working_shelves_can_run_now(temp_data_dir, stage):
    p = db.create_project("Board", slug=f"board-{stage}", stage=stage)
    assert worker.can_run_now(db.get_project(p["id"])) is True


@pytest.mark.parametrize("stage", ["backlog", "done", "abandoned"])
def test_the_finished_and_unstarted_shelves_cannot(temp_data_dir, stage):
    """Backlog means "no model yet" and done/abandoned are finished: on those a
    run happens because somebody asked for one, never as a side effect of
    writing a note down."""
    p = db.create_project("Board", slug=f"board-{stage}", stage=stage)
    assert worker.can_run_now(db.get_project(p["id"])) is False


def test_a_paused_project_can_still_run_now(temp_data_dir):
    """A pause lives beside the stage rather than replacing it, and a note on a
    parked project has always woken it up - so the pause must not read as "not
    runnable" and quietly take that behavior away."""
    p = db.create_project("Napping", slug="napping", stage="active")
    db.pause_project(p["id"])
    row = db.get_project(p["id"])
    assert db.is_paused(row)
    assert worker.can_run_now(row) is True


def test_a_project_with_an_agent_already_in_it_cannot(temp_data_dir):
    p = db.create_project("Busy", slug="busy", stage="active")
    assert worker.can_run_now(db.get_project(p["id"])) is True
    run_id = busy(p["id"])
    assert worker.can_run_now(db.get_project(p["id"])) is False
    db.finish_run(run_id, "ok")
    assert worker.can_run_now(db.get_project(p["id"])) is True


def test_a_leased_workspace_holds_it_even_with_no_run_row(temp_data_dir, monkeypatch):
    """The runs table can be wrong - a run that outlived the portal process that
    started it holds the kernel lease and may hold no row at all. That is the
    check that would have caught the 2026-07-29 double-run."""
    p = db.create_project("Leased", slug="leased", stage="active")
    monkeypatch.setattr(worker, "workspace_leased", lambda slug: slug == "leased")
    assert worker.can_run_now(db.get_project(p["id"])) is False


def test_someone_elses_run_does_not_hold_this_project(temp_data_dir):
    """Runs go in parallel. Only THIS project's workspace matters."""
    mine = db.create_project("Mine", slug="mine", stage="active")
    theirs = db.create_project("Theirs", slug="theirs", stage="active")
    busy(theirs["id"])
    assert worker.can_run_now(db.get_project(mine["id"])) is True


def test_the_daily_cap_does_not_make_it_unrunnable(temp_data_dir):
    """A manual run bypasses the per-project cap by design (`_pick_project`:
    "Wes asking for a run is the whole point"). Reading the cap here would
    refuse a note-triggered run the worker would happily have started."""
    p = db.create_project("Capped", slug="capped", stage="active")
    db.update_project(p["id"], max_runs_per_day=1)
    db.finish_run(db.create_run(p["id"], "build", "opus"), "ok")
    row = db.get_project(p["id"])
    assert worker.project_at_daily_cap(row) is True
    assert worker.can_run_now(row) is True


def test_the_worker_switch_being_off_does_not_make_it_unrunnable(temp_data_dir):
    """`scheduled_work_enabled` is about SCHEDULED work: "pressing the button is
    the request", and so is typing a note."""
    p = db.create_project("Manual", slug="manual", stage="active")
    db.set_setting("worker_enabled", "0")
    assert worker.scheduled_work_enabled() is False
    assert worker.can_run_now(db.get_project(p["id"])) is True


# --------------------------------------------------------------------------
# The note route
# --------------------------------------------------------------------------


def test_a_note_on_an_active_project_starts_a_run(client):
    p = db.create_project("Fridge", slug="fridge", stage="active")
    client.post("/project/fridge/note", data={"note": "make the button run"})
    assert queued() == [p["id"]]
    assert any("make the button run" in j["content_md"] for j in db.list_journal(p["id"]))


def test_a_note_on_a_parked_project_still_wakes_it_and_runs(client):
    p = db.create_project("Parked", slug="parked", stage="review")
    client.post("/project/parked/note", data={"note": "one more thing"})
    assert db.get_project(p["id"])["stage"] == "active"
    assert queued() == [p["id"]]


@pytest.mark.parametrize("stage", ["backlog", "done"])
def test_a_note_on_a_shelved_project_is_stored_and_nothing_else(client, stage):
    p = db.create_project("Shelved", slug=f"shelved-{stage}", stage=stage)
    client.post(f"/project/shelved-{stage}/note", data={"note": "someday"})
    assert db.get_project(p["id"])["stage"] == stage
    assert queued() == []
    assert any("someday" in j["content_md"] for j in db.list_journal(p["id"]))


def test_a_note_while_an_agent_is_working_does_not_queue_a_second_run(client):
    """It would only be re-queued until the workspace freed up, and the point of
    the change is "run now" - not "run eventually". The note is still stored,
    and the project is active, so the scheduler picks it up as it always did."""
    p = db.create_project("Busy", slug="busy", stage="active")
    busy(p["id"])
    client.post("/project/busy/note", data={"note": "while you are in there"})
    assert queued() == []
    assert any("while you are in there" in j["content_md"] for j in db.list_journal(p["id"]))


def test_a_voice_memo_runs_too_once_it_has_been_transcribed(client, monkeypatch):
    """Most of Wes's notes arrive as a dictated memo with no text at all, so the
    audio path is the common one, not the exception - and it is a different
    branch: the run is the CONTINUATION of transcription, so that the prompt
    carries his words rather than "(transcription is still running)".

    Found by the sweep: deleting the fix here broke nothing, because every test
    of the audio path pressed "add & run now" explicitly."""
    from app import main

    p = db.create_project("Fridge", slug="fridge", stage="active")
    kicked: dict = {}
    monkeypatch.setattr(
        main.transcribe, "kick",
        lambda ids, after=None: kicked.update(ids=ids, after=after),
    )
    client.post(
        "/project/fridge/note",
        files={"files": ("memo.webm", b"blob", "audio/webm;codecs=opus")},
    )
    assert kicked["ids"] == [db.list_attachments(p["id"])[0]["id"]]
    # Not on the request path: the words have to land first.
    assert queued() == []
    asyncio.run(kicked["after"])
    assert queued() == [p["id"]]


def test_a_voice_memo_on_a_shelved_project_still_starts_nothing(client, monkeypatch):
    """The audio branch has to consult the same predicate the text one does, or
    a memo dropped on a done project resurrects it."""
    from app import main

    db.create_project("Finished", slug="finished", stage="done")
    kicked: dict = {}
    monkeypatch.setattr(
        main.transcribe, "kick",
        lambda ids, after=None: kicked.update(ids=ids, after=after),
    )
    client.post(
        "/project/finished/note",
        files={"files": ("memo.webm", b"blob", "audio/webm;codecs=opus")},
    )
    asyncio.run(kicked["after"])
    assert queued() == []


def test_queue_note_still_starts_nothing_on_a_runnable_project(client):
    """The opt-out has to keep working, or there is no way to stack a thought."""
    p = db.create_project("Fridge", slug="fridge", stage="active")
    client.post("/project/fridge/note", data={"note": "for later", "then": "queue"})
    assert queued() == []
    assert any("for later" in j["content_md"] for j in db.list_journal(p["id"]))


def test_an_empty_box_starts_nothing(client):
    """Pressing the green button with nothing typed is a mis-tap, not a request
    - only the explicit "add & run now" reads an empty box as "go"."""
    db.create_project("Fridge", slug="fridge", stage="active")
    client.post("/project/fridge/note", data={"note": "   "})
    assert queued() == []


def test_the_auto_run_is_not_a_build_approval(client):
    """"add & run now" puts the project on the working shelf, which IS the build
    approval. The plain button must not: writing code waits for his click, and
    the run this queues gets a planning pass."""
    p = db.create_project("Gated", slug="gated", stage="active")
    db.update_project(p["id"], build_requested=1, build_approved=0)
    row = db.get_project(p["id"])
    assert worker.build_gated(row) is True

    client.post("/project/gated/note", data={"note": "keep planning"})
    row = db.get_project(p["id"])
    assert queued() == [p["id"]]
    assert worker.build_gated(row) is True
    assert worker.task_for(row, manual=True) != "build"


# --------------------------------------------------------------------------
# The button row
# --------------------------------------------------------------------------


def test_add_and_run_now_is_gone_when_the_green_button_runs(client):
    """Two buttons doing the same thing side by side is the duplicate control
    Wes deletes."""
    db.create_project("Fridge", slug="fridge", stage="active")
    html = client.get("/project/fridge").text
    assert 'name="then" value="run"' not in html
    assert 'name="then" value="queue"' in html
    assert "start an agent run on it now" in html


def test_add_and_run_now_survives_where_it_is_the_only_way_to_run(client):
    """On a shelf a note does not wake, it is still the override."""
    db.create_project("Someday", slug="someday", stage="backlog")
    html = client.get("/project/someday").text
    assert 'name="then" value="run"' in html
    assert "start an agent run on it now" not in html


def test_the_group_stays_right_justified_without_the_run_button(client):
    """.note-actions carries the margin-left:auto that pushes the group right,
    and it was on the button that is now conditional."""
    db.create_project("Fridge", slug="fridge", stage="active")
    html = client.get("/project/fridge").text
    i_queue = html.index('name="then" value="queue"')
    assert "note-actions" in html[i_queue - 200 : i_queue + 200]

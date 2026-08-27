"""Wes, 2026-08-17: "When a project finishes running if it has queued notes
that haven't been seen by the model yet don't switch it to a review yet run it
again with a new queued notes".

The hole he is describing: `note_arrived` deliberately queues nothing while an
agent is in the workspace (two agents in one workspace is the 2026-07-29
double-run), so a note typed mid-run is stored and its run request is dropped.
If that run then reports `new_stage: review`, the project lands on the review
shelf carrying a note no model has ever read, and the ordinary rotation never
comes back for a review-shelf project. `worker._rerun_for_unseen_notes` is the
deferral being completed at the end of the run instead.

The load-bearing boundary is "unseen": a note the finished run DID read is
stamped `delivered_at` by `notes.deliver` at prompt-build time and must not
hold anything back, or every run on a project with any note history would
re-run itself forever.
"""
from __future__ import annotations

import asyncio

import pytest

from app import agent_runner, db, notes, selfreview, worker


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


def note(project_id: int, text: str = "one more thing") -> int:
    """A note as the note route writes one: pending until a prompt spends it."""
    return db.add_journal(project_id, "user", "note", text)


def status_lines(project_id: int) -> list[str]:
    return [
        row["content_md"]
        for row in db.list_journal_asc(project_id, limit=50)
        if row["author"] == "system" and row["kind"] == "status"
    ]


# --------------------------------------------------------------------------
# The rule Wes stated
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unseen_note_holds_the_project_off_the_review_shelf(temp_data_dir):
    p = db.create_project("Portal", slug="portal", stage="active")
    note(p["id"])
    # _apply_report granting the run's `new_stage: review`.
    db.update_project(p["id"], stage="review")

    assert await worker._rerun_for_unseen_notes(db.get_project(p["id"])) is True

    assert db.get_project(p["id"])["stage"] == "active"
    assert queued() == [p["id"]]


@pytest.mark.asyncio
async def test_it_says_why_in_the_journal(temp_data_dir):
    """A project that asked to surface and did not has to explain itself on the
    page, or the next thing Wes reads is an agent report claiming it finished
    beside a project still sitting on the active shelf."""
    p = db.create_project("Portal", slug="portal", stage="review")
    note(p["id"])

    await worker._rerun_for_unseen_notes(db.get_project(p["id"]))

    line = status_lines(p["id"])[-1]
    assert "A note arrived while this run was working" in line
    assert "queued another run" in line


@pytest.mark.asyncio
async def test_the_line_counts_the_notes_when_there_is_more_than_one(temp_data_dir):
    p = db.create_project("Portal", slug="portal", stage="review")
    note(p["id"], "first")
    note(p["id"], "second")
    note(p["id"], "third")

    await worker._rerun_for_unseen_notes(db.get_project(p["id"]))

    line = status_lines(p["id"])[-1]
    assert "3 notes arrived while this run was working" in line
    assert "read them yet" in line


# --------------------------------------------------------------------------
# "Unseen" is delivered_at, and nothing else
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_note_this_run_actually_read_surfaces_for_review_as_normal(temp_data_dir):
    """The one that would turn every run into an infinite loop. `notes.deliver`
    stamps the note when it goes into the prompt, so by the end of the run that
    note has been seen and holds nothing back."""
    p = db.create_project("Portal", slug="portal", stage="active")
    note(p["id"])
    delivery = notes.deliver(p["id"])
    assert delivery.ids  # it really went into a prompt
    db.update_project(p["id"], stage="review")

    assert await worker._rerun_for_unseen_notes(db.get_project(p["id"])) is False

    assert db.get_project(p["id"])["stage"] == "review"
    assert queued() == []


@pytest.mark.asyncio
async def test_a_project_with_no_notes_at_all_surfaces_for_review(temp_data_dir):
    p = db.create_project("Portal", slug="portal", stage="review")

    assert await worker._rerun_for_unseen_notes(db.get_project(p["id"])) is False

    assert db.get_project(p["id"])["stage"] == "review"
    assert queued() == []


@pytest.mark.asyncio
async def test_an_agents_own_journal_entry_is_not_an_unseen_note(temp_data_dir):
    """Everything that is not one of his notes is stamped delivered the moment
    it is written (db.add_journal), precisely so it can never masquerade as
    pending - assert that here rather than trusting it from two files away."""
    p = db.create_project("Portal", slug="portal", stage="review")
    db.add_journal(p["id"], "agent", "progress", "## I did a thing")
    db.add_journal(p["id"], "system", "status", "Something happened")

    assert await worker._rerun_for_unseen_notes(db.get_project(p["id"])) is False

    assert db.get_project(p["id"])["stage"] == "review"


# --------------------------------------------------------------------------
# Which shelves it acts on - `reactivate_on_note`'s rule
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_project_deleted_while_the_run_worked_is_not_resurrected(
    temp_data_dir, monkeypatch
):
    """The row is re-read rather than trusted from the start of the run, because
    a project can be deleted from the phone while its agent is still working.
    Queueing a run for an id that no longer exists would spin the worker."""
    p = db.create_project("Portal", slug="portal", stage="review")
    note(p["id"])
    row = db.get_project(p["id"])
    monkeypatch.setattr(db, "get_project", lambda project_id: None)

    assert await worker._rerun_for_unseen_notes(row) is False

    assert queued() == []


@pytest.mark.asyncio
async def test_an_active_project_stays_active_and_gets_another_run(temp_data_dir):
    """The run did not ask to surface, so there is no shelf to hold - but the
    note is still unread, and the rotation may not come round for hours."""
    p = db.create_project("Portal", slug="portal", stage="active")
    note(p["id"])

    assert await worker._rerun_for_unseen_notes(db.get_project(p["id"])) is True

    assert db.get_project(p["id"])["stage"] == "active"
    assert queued() == [p["id"]]
    assert "kept **active**" in status_lines(p["id"])[-1]


@pytest.mark.asyncio
async def test_a_paused_project_is_woken_by_its_unseen_note(temp_data_dir):
    """A pause lives beside the stage, and a note on a parked project has always
    woken it up. A run finishing under a pause must not leave the note asleep."""
    p = db.create_project("Portal", slug="portal", stage="active")
    db.pause_project(p["id"])
    note(p["id"])

    assert await worker._rerun_for_unseen_notes(db.get_project(p["id"])) is True

    row = db.get_project(p["id"])
    assert db.is_paused(row) is False
    assert row["stage"] == "active"
    assert queued() == [p["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["backlog", "done", "abandoned"])
async def test_the_shelved_stages_are_left_exactly_where_they_are(temp_data_dir, stage):
    """Backlog means "no model yet" and done/abandoned are finished. A note on
    one of those waits for a person, so an unseen note must not resurrect it -
    the same rule `reactivate_on_note` follows from the other direction."""
    p = db.create_project("Portal", slug=f"portal-{stage}", stage=stage)
    note(p["id"])

    assert await worker._rerun_for_unseen_notes(db.get_project(p["id"])) is False

    assert db.get_project(p["id"])["stage"] == stage
    assert queued() == []
    assert status_lines(p["id"]) == []


# --------------------------------------------------------------------------
# Wiring: the critic is skipped, and a bad read cannot eat the report
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_self_review_critic_is_skipped_when_a_rerun_is_queued(
    temp_data_dir, monkeypatch
):
    """The critic answers "is this ready for Wes to look at", which is already
    answered when a note he wrote mid-run is unread. Spending a model call on it
    is waste, and its hold note would contradict the line just journalled."""
    p = db.create_project("Portal", slug="portal", stage="active")
    note(p["id"])
    db.update_project(p["id"], stage="review")

    called: list[int] = []

    async def fake_review(*args, **kwargs):
        called.append(1)

    monkeypatch.setattr(worker, "_maybe_self_review", fake_review)

    if not await worker._rerun_for_unseen_notes(db.get_project(p["id"])):
        await worker._maybe_self_review(
            db.get_project(p["id"]), agent_runner.RunResult(ok=True, report={}), "build", None
        )

    assert called == []


@pytest.mark.asyncio
async def test_the_critic_still_runs_when_nothing_is_waiting(temp_data_dir, monkeypatch):
    """The other half of the branch above: with no unseen note, the review-bound
    run is judged exactly as it was before any of this existed."""
    p = db.create_project("Portal", slug="portal", stage="review")

    called: list[int] = []

    async def fake_review(*args, **kwargs):
        called.append(1)

    monkeypatch.setattr(worker, "_maybe_self_review", fake_review)

    if not await worker._rerun_for_unseen_notes(db.get_project(p["id"])):
        await worker._maybe_self_review(
            db.get_project(p["id"]), agent_runner.RunResult(ok=True, report={}), "build", None
        )

    assert called == [1]


@pytest.mark.asyncio
async def test_a_failed_note_read_leaves_the_run_alone(temp_data_dir, monkeypatch):
    """Same fail-open rule the critic follows: this check runs after the report
    has already been applied, so an exception here must not take out the tail of
    the run. It declines to hold rather than raising."""
    p = db.create_project("Portal", slug="portal", stage="review")

    def boom(_project_id):
        raise RuntimeError("no")

    monkeypatch.setattr(notes, "pending", boom)

    assert await worker._rerun_for_unseen_notes(db.get_project(p["id"])) is False

    assert db.get_project(p["id"])["stage"] == "review"
    assert queued() == []


@pytest.mark.asyncio
async def test_the_queued_rerun_delivers_the_note_and_the_next_end_is_quiet(temp_data_dir):
    """End to end on the loop's own termination: the run this queues renders the
    note into its prompt, which stamps it, so the finish after that one holds
    nothing back. Without this the project would re-run itself forever."""
    p = db.create_project("Portal", slug="portal", stage="active")
    note(p["id"])
    db.update_project(p["id"], stage="review")

    assert await worker._rerun_for_unseen_notes(db.get_project(p["id"])) is True
    # The run that request starts builds a prompt, which spends the note.
    assert notes.deliver(p["id"]).ids
    db.update_project(p["id"], stage="review")

    assert await worker._rerun_for_unseen_notes(db.get_project(p["id"])) is False
    assert db.get_project(p["id"])["stage"] == "review"


# --------------------------------------------------------------------------
# The end-of-run path really calls it
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_note_typed_mid_run_holds_the_whole_real_run_back(temp_data_dir, monkeypatch):
    """The scenario end to end, through `run_project_task` rather than through
    the helper: the note is written while the agent is working (inside the fake
    `run_claude`, i.e. strictly after `build_prompt` has spent whatever was
    pending), and the run then reports itself finished."""
    p = db.create_project("Portal", slug="portal", stage="active", build_approved=True)
    monkeypatch.setattr(selfreview, "enabled", lambda: False)

    async def fake_claude(prompt, cwd, model, timeout_min, **kwargs):
        # Wes types a note from his phone while the agent is mid-run. The note
        # route stores it and queues nothing, because this run holds the
        # workspace - that dropped request is what the end of the run honors.
        note(p["id"], "one more thing before you finish")
        return agent_runner.RunResult(
            ok=True,
            report={"summary": ["did the thing"], "new_stage": "review"},
            result_text="ok",
        )

    monkeypatch.setattr(agent_runner, "run_claude", fake_claude)
    await worker.run_project_task(db.get_project(p["id"]), "build")

    assert db.get_project(p["id"])["stage"] == "active"
    assert queued() == [p["id"]]
    assert any("no model has read it yet" in line for line in status_lines(p["id"]))


@pytest.mark.asyncio
async def test_a_real_run_with_nothing_typed_during_it_surfaces_for_review(
    temp_data_dir, monkeypatch
):
    """The control for the test above, through the same path: without a note
    arriving mid-run, `new_stage: review` still puts the project on the review
    shelf and queues nothing."""
    p = db.create_project("Portal", slug="portal", stage="active", build_approved=True)
    monkeypatch.setattr(selfreview, "enabled", lambda: False)
    # A note from BEFORE the run: build_prompt spends it, so it is seen.
    note(p["id"], "the thing this run was started for")

    async def fake_claude(prompt, cwd, model, timeout_min, **kwargs):
        assert "the thing this run was started for" in prompt
        return agent_runner.RunResult(
            ok=True,
            report={"summary": ["did the thing"], "new_stage": "review"},
            result_text="ok",
        )

    monkeypatch.setattr(agent_runner, "run_claude", fake_claude)
    await worker.run_project_task(db.get_project(p["id"]), "build")

    assert db.get_project(p["id"])["stage"] == "review"
    assert queued() == []

"""Research bursts: spending expiring weekly allowance on depth (todo #27).

Queueing a project is a standing request, not a run - it fires only inside a
spend-down window, on the research model, and it writes RESEARCH.md rather than
code. These tests pin the three things that make that safe: it never starts
outside a window, one queueing buys exactly one burst, and a burst cannot move
a project's status.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, pacing, worker


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def open_window(hours: float = 3) -> None:
    pacing.start(datetime.now(timezone.utc) + timedelta(hours=hours))


# --------------------------------------------------------------------------
# The queue itself
# --------------------------------------------------------------------------


def test_queueing_and_unqueueing_a_project():
    project = db.create_project("Something", stage="backlog")
    assert db.is_research_queued(db.get_project(project["id"])) is False
    db.queue_research(project["id"])
    assert db.is_research_queued(db.get_project(project["id"])) is True
    assert [p["id"] for p in db.list_research_queued()] == [project["id"]]
    db.unqueue_research(project["id"])
    assert db.list_research_queued() == []
    assert db.count_research_queued() == 0


def test_the_queue_runs_in_the_order_it_was_filled():
    first = db.create_project("First", stage="backlog")
    second = db.create_project("Second", stage="backlog")
    db.update_project(second["id"], research_queued_at="2026-07-20T00:00:00+00:00")
    db.update_project(first["id"], research_queued_at="2026-07-21T00:00:00+00:00")
    assert [p["id"] for p in db.list_research_queued()] == [second["id"], first["id"]]


def test_queueing_does_not_touch_the_status():
    """Deliberate: reading about a backlog idea is not a decision to start it."""
    project = db.create_project("Something", stage="backlog")
    db.queue_research(project["id"])
    assert db.get_project(project["id"])["stage"] == "backlog"


# --------------------------------------------------------------------------
# Picking one
# --------------------------------------------------------------------------


def test_nothing_is_picked_outside_a_spend_down_window():
    project = db.create_project("Something", stage="backlog")
    db.queue_research(project["id"])
    assert worker._pick_research() is None  # noqa: SLF001


def test_a_queued_project_is_picked_inside_the_window():
    project = db.create_project("Something", stage="backlog")
    db.queue_research(project["id"])
    open_window()
    picked = worker._pick_research()  # noqa: SLF001
    assert picked is not None and picked["id"] == project["id"]


def test_an_unqueued_project_is_never_picked():
    db.create_project("Something", stage="active", build_approved=True)
    open_window()
    assert worker._pick_research() is None  # noqa: SLF001


def test_a_busy_project_is_skipped_for_the_next_in_the_queue():
    """One agent per workspace still holds: two in one checkout would fight."""
    busy = db.create_project("Busy", stage="active", build_approved=True)
    free = db.create_project("Free", stage="backlog")
    db.update_project(busy["id"], research_queued_at="2026-07-20T00:00:00+00:00")
    db.update_project(free["id"], research_queued_at="2026-07-21T00:00:00+00:00")
    db.create_run(busy["id"], "build", "opus")
    open_window()
    picked = worker._pick_research()  # noqa: SLF001
    assert picked is not None and picked["id"] == free["id"]


def test_the_build_gate_does_not_block_a_burst():
    """A burst writes RESEARCH.md and no code, so an unapproved project - the
    kind most worth reading about - is fair game."""
    project = db.create_project("Unapproved", stage="active")
    db.update_project(project["id"], build_approved=0, build_requested=1)
    assert worker.build_gated(db.get_project(project["id"])) is True
    db.queue_research(project["id"])
    open_window()
    assert worker._pick_research() is not None  # noqa: SLF001


# --------------------------------------------------------------------------
# Through the worker
# --------------------------------------------------------------------------


def test_the_worker_starts_a_burst_and_clears_the_queue_flag(monkeypatch):
    project = db.create_project("Something", stage="active", build_approved=True)
    db.queue_research(project["id"])
    open_window()
    started = []
    monkeypatch.setattr(worker, "spawn_run", lambda p, t: started.append((p["id"], t)) or 1)

    assert asyncio.run(worker._start_one()) is True  # noqa: SLF001
    assert started == [(project["id"], "research")]
    # One queueing, one burst: a flag left set would relaunch the same project
    # every couple of minutes for the rest of the window.
    assert db.list_research_queued() == []
    assert asyncio.run(worker._start_one()) is True  # noqa: SLF001
    assert started[1][1] != "research"


def test_a_burst_beats_the_ordinary_rotation_and_the_pacing_interval(monkeypatch):
    """The queue is the point of the window; the rotation can wait its turn."""
    ordinary = db.create_project("Ordinary", stage="active", build_approved=True)
    db.approve_build(ordinary["id"])
    queued = db.create_project("Queued", stage="backlog")
    db.queue_research(queued["id"])
    db.set_setting("worker_interval_min", "60")
    db.create_run(ordinary["id"], "build", "opus")  # a recent start to pace from
    db.finish_run(1, "ok")
    open_window()
    started = []
    monkeypatch.setattr(worker, "spawn_run", lambda p, t: started.append((p["id"], t)) or 1)

    assert asyncio.run(worker._start_one()) is True  # noqa: SLF001
    assert started == [(queued["id"], "research")]


def test_a_full_window_still_holds_a_burst(monkeypatch):
    """A spend-down aims at the wall; it does not go through it."""
    project = db.create_project("Something", stage="backlog")
    db.queue_research(project["id"])
    open_window()
    monkeypatch.setattr(pacing, "scheduled_hold", lambda *a, **k: {"label": "session", "percent": 97.0})
    started = []
    monkeypatch.setattr(worker, "spawn_run", lambda p, t: started.append(p["id"]) or 1)
    assert asyncio.run(worker._start_one()) is False  # noqa: SLF001
    assert started == []
    assert db.count_research_queued() == 1


def test_a_burst_journals_why_it_started(monkeypatch):
    project = db.create_project("Something", stage="backlog")
    db.queue_research(project["id"])
    open_window()
    monkeypatch.setattr(worker, "spawn_run", lambda p, t: 1)
    asyncio.run(worker._start_one())  # noqa: SLF001
    entries = [e["content_md"] for e in db.list_journal(project["id"])]
    assert any("research burst" in e for e in entries)


# --------------------------------------------------------------------------
# The parallel cap
# --------------------------------------------------------------------------


def test_the_parallel_cap_widens_while_spending_down():
    assert pacing.parallel_cap(2) == 2
    open_window()
    assert pacing.parallel_cap(2) == pacing.SPEND_DOWN_PARALLEL


def test_a_spend_down_never_narrows_a_wider_cap():
    """If Wes has set 8 he means 8 - a burst must not slow the portal down."""
    open_window()
    assert pacing.parallel_cap(8) == 8


def test_the_tick_fills_the_wider_cap(monkeypatch):
    for i in range(6):
        project = db.create_project(f"P{i}", stage="backlog")
        db.queue_research(project["id"])
    open_window()
    started = []

    def fake_spawn(project, task):
        run_id = db.create_run(project["id"], task, "fable")
        started.append(project["id"])
        worker._inflight[run_id] = asyncio.get_event_loop().create_future()  # noqa: SLF001
        return run_id

    monkeypatch.setattr(worker, "spawn_run", fake_spawn)
    monkeypatch.setattr(worker, "_maybe_spend_down", _noop)
    monkeypatch.setattr(worker, "_maybe_reflect", _noop)
    asyncio.run(worker._tick())  # noqa: SLF001
    worker._inflight.clear()  # noqa: SLF001
    assert len(started) == pacing.SPEND_DOWN_PARALLEL


async def _noop() -> None:
    return None


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


def test_a_burst_uses_the_research_model_over_the_project_override():
    project = db.create_project("Something", stage="backlog")
    db.update_project(project["id"], model="haiku")
    row = db.get_project(project["id"])
    assert agent_runner.resolve_model(row) == "haiku"
    assert agent_runner.resolve_model(row, "research") == config.RESEARCH_MODEL


def test_the_research_model_is_configurable():
    db.set_setting("research_model", "sonnet")
    assert agent_runner.resolve_model(None, "research") == "sonnet"


def test_junk_in_the_research_model_setting_falls_back():
    db.set_setting("research_model", "gpt-9")
    assert agent_runner.resolve_model(None, "research") == config.RESEARCH_MODEL


def test_ordinary_tasks_are_unaffected_by_the_research_model():
    db.set_setting("research_model", "sonnet")
    db.set_setting("worker_model", "opus")
    assert agent_runner.resolve_model(None, "build") == "opus"


# --------------------------------------------------------------------------
# What a burst is allowed to change
# --------------------------------------------------------------------------


def test_the_research_prompt_says_no_code_and_no_status_change():
    guidance = agent_runner.TASK_GUIDANCE["research"]
    assert "RESEARCH.md" in guidance
    assert "Do NOT write or change application code" in guidance
    assert "new_stage null" in guidance


def test_the_research_prompt_reaches_the_agent():
    project = db.create_project("Something", stage="backlog")
    prompt = agent_runner.build_prompt("research", db.get_project(project["id"]))
    assert "RESEARCH BURST" in prompt


def test_a_burst_report_cannot_move_the_status():
    project = db.create_project("Something", stage="backlog")
    result = agent_runner.RunResult(ok=True, report={"new_status": "building", "journal_entry_md": "read a lot"})
    worker._apply_report(db.get_project(project["id"]), result, task="research")  # noqa: SLF001
    after = db.get_project(project["id"])
    assert after["stage"] == "backlog"
    assert after["build_requested"] == 0


def test_an_ordinary_report_still_moves_the_status():
    project = db.create_project("Something", stage="backlog")
    result = agent_runner.RunResult(ok=True, report={"new_status": "planning", "journal_entry_md": "triaged"})
    worker._apply_report(db.get_project(project["id"]), result, task="triage")  # noqa: SLF001
    assert db.get_project(project["id"])["stage"] == "active"


def test_a_burst_report_still_records_its_journal_and_todos():
    project = db.create_project("Something", stage="backlog")
    result = agent_runner.RunResult(
        ok=True,
        report={
            "new_status": "building",
            "journal_entry_md": "The good hinge is the Sugatsune one.",
            "todo_updates": {"add": [{"text": "price the Sugatsune hinge", "owner": "agent"}]},
        },
    )
    worker._apply_report(db.get_project(project["id"]), result, task="research")  # noqa: SLF001
    assert any("Sugatsune" in e["content_md"] for e in db.list_journal(project["id"]))
    assert [t["text"] for t in db.visible_todos(project["id"], owner="agent")] == [
        "price the Sugatsune hinge"
    ]


# --------------------------------------------------------------------------
# The UI
# --------------------------------------------------------------------------


def test_the_project_page_offers_a_burst_and_takes_the_queueing(client):
    db.create_project("Something", stage="backlog", slug="something")
    page = client.get("/project/something").text
    assert "queue research burst" in page

    client.post("/project/something/research", data={"queued": "1"})
    project = db.get_project_by_slug("something")
    assert db.is_research_queued(project) is True

    page = client.get("/project/something").text
    assert "cancel research burst" in page
    assert config.RESEARCH_MODEL in page

    client.post("/project/something/research", data={"queued": "0"})
    assert db.is_research_queued(db.get_project_by_slug("something")) is False


def test_the_page_says_a_burst_is_imminent_only_inside_a_window(client):
    project = db.create_project("Something", stage="backlog", slug="something")
    db.queue_research(project["id"])
    assert "starts within a minute" not in client.get("/project/something").text
    open_window()
    assert "starts within a minute" in client.get("/project/something").text


def test_the_dashboard_counts_the_queue_only_when_there_is_one(client):
    assert "queued for research" not in client.get("/").text
    project = db.create_project("Something", stage="backlog", slug="something")
    db.queue_research(project["id"])
    assert "1 queued for research" in client.get("/").text


def test_queueing_an_unknown_project_404s(client):
    assert client.post("/project/nope/research", data={"queued": "1"}).status_code == 404

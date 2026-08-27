"""The build approval gate, under the redesigned state model.

Wes's 14:33 note, verbatim in effect: a pass over the backlog triaged every
idea, each triage promoted itself to `planning`, each plan promoted itself to
`building`, and seventeen projects he had not decided to start began building
themselves until the usage limit stopped them.

So triage and planning stay unasked - they are cheap, reversible and produce a
title, an assessment and a PLAN.md - and writing code waits for his OK. The
agent's "may I build?" is now a stored fact (`build_requested`) rather than a
hijack of `waiting_user`; the project stays `active` while it waits.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, daycycle, db, main, worker


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clean_worker_state(temp_data_dir):
    # A tick also considers the daily reflect, which would shell out to the real
    # `claude` CLI. Marking today's reflect as already done keeps these tests
    # about the gate (and offline).
    db.set_setting("last_reflect_date", daycycle.current_day())

    def reset():
        worker._inflight.clear()
        while not worker.manual_queue.empty():
            worker.manual_queue.get_nowait()

    reset()
    yield
    reset()


@pytest.fixture
def spawned(monkeypatch):
    started: list[tuple[str, str]] = []

    async def fake_execute(project, task, run_id, model):
        started.append((project["slug"], task))
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "_execute_run", fake_execute)
    return started


async def tick() -> None:
    await worker._tick()
    await asyncio.sleep(0)


def idea(title="Fridge Board", slug="fridge-board", stage="backlog", requested=False):
    """A project as an idea actually arrives - unapproved, whatever its stage.
    `requested=True` is the shape a finished plan leaves behind: the agent has
    asked to build and Wes has not answered."""
    project = db.create_project(title, "An idea.", slug=slug, stage=stage)
    if requested:
        db.update_project(project["id"], build_requested=1)
    return db.get_project(project["id"])


# --- what the worker will and won't pick up --------------------------------

def test_an_unanswered_build_request_is_not_picked(temp_data_dir):
    project = idea(stage="active", requested=True)
    assert worker.build_gated(project) is True
    assert worker._pick_project(None) == (None, False)


def test_planning_still_happens_unasked(temp_data_dir):
    """The gate is on `build`, not on thinking about the idea. An unapproved
    active project that has not asked to build yet keeps getting planned."""
    project = idea(stage="active")
    assert worker.build_gated(project) is False
    assert worker.task_for(project) in ("triage", "plan")
    picked, _ = worker._pick_project(None)
    assert picked["slug"] == project["slug"]


def test_the_backlog_is_never_scheduled(temp_data_dir):
    """Wes's ask, verbatim: "just add it to the backlog and not feed it to a
    model yet". Backlog projects wait for him - only a deliberate act (the
    plan button, a manual run, activation) puts a model on one."""
    idea(stage="backlog")
    assert worker._pick_project(None) == (None, False)


def test_a_manual_run_on_a_backlog_idea_is_a_triage(temp_data_dir):
    project = idea(stage="backlog")
    assert worker.task_for(project, manual=True) == "triage"


def test_approval_lets_the_build_through(temp_data_dir):
    project = idea(stage="active", requested=True)
    db.approve_build(project["id"])
    picked, _ = worker._pick_project(None)
    assert picked["slug"] == "fridge-board"
    assert worker.task_for(picked) == "build"


def test_a_manual_run_on_a_gated_project_plans_rather_than_builds(temp_data_dir):
    """Wes asking for a run is a real request, so something happens - but the
    thing the gate exists to stop still doesn't."""
    project = idea(stage="active", requested=True)
    db.finish_run(db.create_run(project["id"], "plan", "opus"), "ok")
    assert worker.task_for(db.get_project(project["id"]), manual=True) == "plan"


@pytest.mark.anyio
async def test_the_worker_starts_nothing_for_a_backlog_of_unanswered_requests(
    temp_data_dir, spawned
):
    for i in range(3):
        idea(f"Idea {i}", slug=f"idea-{i}", stage="active", requested=True)
    await tick()
    assert spawned == []
    assert db.count_runs_today() == 0


@pytest.mark.anyio
async def test_the_setting_can_be_turned_off(temp_data_dir, spawned):
    project = idea(stage="active", requested=True)
    db.set_setting("require_build_approval", "0")
    assert worker.build_gated(project) is False
    await tick()
    assert spawned == [("fridge-board", "build")]


# --- what happens to an agent that asks for `building` ---------------------

def _report(**over):
    report = {
        "summary": "planned it",
        "journal_entry_md": "Here is the plan.",
        "new_status": "building",
        "questions": [],
        "learnings": [],
    }
    report.update(over)
    return report


def _apply(project, **over):
    result = agent_runner.RunResult(ok=True, report=_report(**over))
    worker._apply_report(project, result)
    return db.get_project(project["id"])


@pytest.mark.anyio
async def test_an_agent_asking_to_build_records_the_request(temp_data_dir):
    project = idea(stage="active")
    after = _apply(project)
    assert after["stage"] == "active"  # no state hijack - it stays active
    assert after["build_requested"] == 1
    assert after["build_approved"] == 0
    assert db.project_shelf(after) == "paused"  # but folded away, waiting on Wes
    journal = [row["content_md"] for row in db.list_journal(project["id"])]
    assert any("Waiting for your OK" in text for text in journal)


@pytest.mark.anyio
async def test_the_request_plea_is_journalled_once_not_every_run(temp_data_dir):
    project = idea(stage="active")
    _apply(project)
    _apply(db.get_project(project["id"]))
    journal = [row["content_md"] for row in db.list_journal(project["id"])]
    assert sum("Waiting for your OK" in text for text in journal) == 1


@pytest.mark.anyio
async def test_an_approved_project_keeps_building_without_re_asking(temp_data_dir):
    project = idea(stage="active")
    db.approve_build(project["id"])
    after = _apply(db.get_project(project["id"]))
    assert after["stage"] == "active"
    assert after["build_requested"] == 0
    assert worker.build_gated(after) is False


@pytest.mark.anyio
async def test_the_old_waiting_user_report_becomes_blocked_on(temp_data_dir):
    project = idea(stage="active")
    after = _apply(project, new_status="waiting_user")
    assert after["stage"] == "active"
    assert after["blocked_on"]
    # And since Wes's 2026-07-30 note it keeps its place on the Active shelf,
    # wearing the `blocked` badge rather than being folded away.
    assert db.project_shelf(after) == "active"


@pytest.mark.anyio
async def test_a_review_report_moves_the_stage(temp_data_dir):
    project = idea(stage="active")
    db.approve_build(project["id"])
    after = _apply(db.get_project(project["id"]), new_status="review")
    assert after["stage"] == "review"


@pytest.mark.anyio
async def test_a_needs_input_report_no_longer_moves_anything(temp_data_dir):
    project = idea(stage="active")
    after = _apply(project, new_status="needs_input")
    assert after["stage"] == "active"


@pytest.mark.anyio
async def test_a_triage_promoting_planning_activates_a_backlog_idea(temp_data_dir):
    project = idea(stage="backlog")
    after = _apply(project, new_status="planning")
    assert after["stage"] == "active"
    assert after["build_approved"] == 0  # promotion is not approval


@pytest.mark.anyio
async def test_blocked_on_clears_when_the_next_run_reports(temp_data_dir):
    project = idea(stage="active")
    db.update_project(project["id"], blocked_on="a part on order")
    after = _apply(db.get_project(project["id"]), new_status=None)
    assert not after["blocked_on"]


# --- how Wes approves ------------------------------------------------------

def test_choosing_active_in_the_picker_is_the_approval(client):
    project = idea(stage="backlog")
    client.post(f"/project/{project['slug']}/status", data={"status": "active"})
    after = db.get_project(project["id"])
    assert after["stage"] == "active"
    assert after["build_approved"] == 1


def test_the_legacy_building_choice_still_approves(client):
    project = idea(stage="backlog")
    client.post(f"/project/{project['slug']}/status", data={"status": "building"})
    after = db.get_project(project["id"])
    assert after["stage"] == "active"
    assert after["build_approved"] == 1


def test_the_approve_button_approves_and_queues_a_run(client):
    project = idea(stage="active", requested=True)
    resp = client.post(f"/project/{project['slug']}/approve-build", data={})
    assert resp.status_code == 200  # followed the redirect
    after = db.get_project(project["id"])
    assert after["stage"] == "active"
    assert after["build_approved"] == 1
    assert after["build_requested"] == 0
    assert worker.manual_queue.qsize() == 1


def test_approval_can_be_withdrawn(client):
    project = idea(stage="active", requested=True)
    db.approve_build(project["id"])
    db.finish_run(db.create_run(project["id"], "plan", "opus"), "ok")
    client.post(f"/project/{project['slug']}/revoke-build", data={})
    after = db.get_project(project["id"])
    assert after["build_approved"] == 0
    assert after["stage"] == "active"  # still planned, just not built
    assert worker.task_for(after) == "plan"


def test_pausing_after_approval_does_not_revoke_it(client):
    """Approval is about permission, not about the current state - pausing a
    project shouldn't mean re-approving it to resume."""
    project = idea(stage="active", requested=True)
    db.approve_build(project["id"])
    client.post(f"/project/{project['slug']}/status", data={"status": "paused"})
    assert db.get_project(project["id"])["build_approved"] == 1


# --- the page and the reason ----------------------------------------------

def test_the_project_page_offers_approval_when_gated(client):
    project = idea(stage="active", requested=True)
    body = client.get(f"/project/{project['slug']}").text
    assert "approve build" in body
    assert "run planning pass" in body


def test_the_project_page_offers_a_normal_run_once_approved(client):
    project = idea(stage="active", requested=True)
    db.approve_build(project["id"])
    body = client.get(f"/project/{project['slug']}").text
    assert "run agent now" in body
    assert "approve build &amp; run agent" not in body


def test_the_idle_reason_names_the_projects_waiting_for_an_ok(temp_data_dir):
    idea("Fridge Board", "fridge-board", stage="active", requested=True)
    idea("Dice Tower", "dice-tower", stage="active", requested=True)
    reason = worker.idle_reason()
    assert "waiting for your OK to start building" in reason
    assert "Fridge Board" in reason


def test_the_idle_reason_prefers_the_cap_answer_when_only_some_are_gated(temp_data_dir):
    """A gated project plus a capped one isn't "waiting for your OK" - one of
    them is a budget problem, and saying otherwise would send Wes to the wrong
    button."""
    idea("Gated", "gated", stage="active", requested=True)
    capped = db.create_project("Capped", stage="active", build_approved=True, slug="capped")
    db.update_project(capped["id"], max_runs_per_day=1)
    db.finish_run(db.create_run(capped["id"], "build", "opus"), "ok")
    assert "waiting for your OK" not in worker.idle_reason()


# --- the prompt ------------------------------------------------------------

def test_the_prompt_tells_the_agent_where_it_stands(temp_data_dir):
    project = idea(stage="active")
    prompt = agent_runner.build_prompt("plan", project)
    assert "NOT yet approved for building" in prompt

    db.approve_build(project["id"])
    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "Wes has approved building this" in prompt


def test_the_contract_says_when_to_stop(temp_data_dir):
    project = idea()
    prompt = agent_runner.build_prompt("triage", project)
    assert "Knowing when to stop" in prompt
    assert "Lean towards action" in prompt


# --- the migration ---------------------------------------------------------

def test_the_backfill_only_trusts_wes_asking_for_the_build(temp_data_dir):
    """Existing projects can't all be grandfathered in - that would preserve
    exactly the runaway this gate exists to stop. Only an explicit user status
    change (and the portal's own project) counts."""
    asked = idea("Asked For", "asked-for", stage="active")
    db.add_journal(asked["id"], "user", "status", "Status changed: `planning` -> `building`")
    agent_moved = idea("Agent Moved", "agent-moved", stage="active")
    db.add_journal(agent_moved["id"], "agent", "progress", "moved myself to building")
    meta = idea("Portal", config.META_PROJECT_SLUG, stage="active")

    db.set_setting(db.BUILD_APPROVAL_BACKFILL_KEY, "")
    db._backfill_build_approval()

    assert db.get_project(asked["id"])["build_approved"] == 1
    assert db.get_project(meta["id"])["build_approved"] == 1
    assert db.get_project(agent_moved["id"])["build_approved"] == 0


def test_the_backfill_does_not_undo_a_withdrawal(temp_data_dir):
    project = idea("Portal", config.META_PROJECT_SLUG, stage="active")
    db.update_project(project["id"], build_approved=0)
    db._backfill_build_approval()  # runs on every startup
    assert db.get_project(project["id"])["build_approved"] == 0


def test_the_dashboard_flags_projects_waiting_for_an_ok(client):
    gated = idea("Fridge Board", "fridge-board", stage="active", requested=True)
    db.create_project("Approved", stage="active", build_approved=True, slug="approved")
    body = client.get("/").text
    # The side rail's More tail lists the gated project with its own
    # "needs your OK" status line; split it off so the card count is the
    # card count.
    rail, page = body.split("</aside>", 1)  # the rail renders before the content
    assert page.count("needs your OK") == 1  # one badge, on the gated card only
    assert rail.count("needs your OK") == 1  # and the rail row agrees
    db.approve_build(gated["id"])
    assert "needs your OK" not in client.get("/").text

"""One-off agent tasks: scratch chat sessions with an agent, no project.

Covers the data model (tasks, messages, the pending/delivered lifecycle), the
prompt (contract on the first run, bare reply on a resumed one), the run
driver in worker.py (reply recorded, session id kept current, every failure
mode leaving an honest system message), and the /tasks routes.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, limits, main, oneoff, worker


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def task():
    return db.create_oneoff("Fix the cron mail on testhost\n\nIt stopped arriving on Sunday.")


def _fake_run(monkeypatch, result: agent_runner.RunResult) -> dict:
    seen: dict = {}

    async def fake(prompt, cwd, model, timeout_min, **kwargs):
        seen.update(kwargs, prompt=prompt, cwd=cwd, model=model, timeout_min=timeout_min)
        return result

    monkeypatch.setattr(agent_runner, "run_claude", fake)
    return seen


def _messages(task_id: int) -> list:
    return [(m["role"], m["content_md"]) for m in db.list_oneoff_messages(task_id)]


# --- the data model ----------------------------------------------------------


def test_a_new_task_takes_its_title_from_the_first_line(task):
    assert task["title"] == "Fix the cron mail on testhost"
    assert task["status"] == "open"
    assert task["cli_session_id"] is None


def test_the_title_skips_markdown_noise_and_blank_lines():
    task = db.create_oneoff("\n\n## - check the NAS fans\nplease")
    assert task["title"] == "check the NAS fans"


def test_a_long_first_line_is_clipped_not_kept_whole():
    task = db.create_oneoff("x" * 300)
    assert len(task["title"]) <= db.ONEOFF_TITLE_MAX
    assert task["title"].endswith("...")


def test_an_unusable_text_still_gets_a_title():
    assert db.create_oneoff("###")["title"] == "untitled task"


def test_the_first_message_is_queued_for_the_agent(task):
    pending = db.pending_oneoff_messages(task["id"])
    assert len(pending) == 1
    assert "cron mail" in pending[0]["content_md"]
    assert pending[0]["delivered_at"] is None


def test_delivery_spends_a_message_exactly_once(task):
    (msg,) = db.pending_oneoff_messages(task["id"])
    db.mark_oneoff_delivered([msg["id"]])
    assert db.pending_oneoff_messages(task["id"]) == []
    # Re-marking must not move the original stamp.
    stamp = db.list_oneoff_messages(task["id"])[0]["delivered_at"]
    db.mark_oneoff_delivered([msg["id"]])
    assert db.list_oneoff_messages(task["id"])[0]["delivered_at"] == stamp


def test_agent_and_system_messages_are_never_pending(task):
    db.add_oneoff_message(task["id"], "agent", "done, see /tmp")
    db.add_oneoff_message(task["id"], "system", "the run failed")
    pending = db.pending_oneoff_messages(task["id"])
    assert [m["role"] for m in pending] == ["wes"]


def test_the_list_page_query_carries_the_last_message(task):
    db.mark_oneoff_delivered([m["id"] for m in db.pending_oneoff_messages(task["id"])])
    db.add_oneoff_message(task["id"], "agent", "It was a full /var partition.")
    rows = db.list_oneoffs("open")
    assert rows[0]["id"] == task["id"]
    assert rows[0]["last_role"] == "agent"
    assert "partition" in rows[0]["last_message"]


def test_archiving_moves_a_task_between_the_lists(task):
    db.set_oneoff_status(task["id"], "archived")
    assert db.list_oneoffs("open") == []
    assert [r["id"] for r in db.list_oneoffs("archived")] == [task["id"]]
    assert db.count_open_oneoffs() == 0


def test_a_tasks_run_is_found_through_its_run_row(task):
    assert db.oneoff_running(task["id"]) is False
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    assert db.oneoff_running(task["id"]) is True
    assert db.latest_oneoff_run(task["id"])["id"] == run_id
    db.finish_run(run_id, "ok")
    assert db.oneoff_running(task["id"]) is False


def test_active_runs_carry_the_task_title_for_the_dashboard(task):
    db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    (row,) = db.active_runs()
    assert row["oneoff_title"] == task["title"]
    assert main._run_owner_label(row) == f"task: {task['title']}"
    snap = main.active_run_snapshot()
    assert snap["runs"][0]["oneoff_id"] == task["id"]


# --- the CLI command ---------------------------------------------------------


def test_a_fresh_run_does_not_resume_anything():
    cmd = agent_runner.build_cmd("opus", 400)
    assert "--resume" not in cmd


def test_a_follow_up_resumes_the_saved_session():
    cmd = agent_runner.build_cmd("opus", 400, resume_session="sess-1")
    assert cmd[cmd.index("--resume") + 1] == "sess-1"


# --- the prompt --------------------------------------------------------------


def test_the_first_prompt_carries_the_contract_and_the_memory(task):
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.PROFILE_MD.write_text("# Profile: Ada\n- has a render-box", encoding="utf-8")
    prompt = oneoff.build_prompt(task, db.pending_oneoff_messages(task["id"]))
    assert "ONE-OFF TASK" in prompt
    assert "cron mail" in prompt
    assert "Do NOT write" in prompt and ".portal/report.json" in prompt
    assert "has a render-box" in prompt


def test_a_resumed_prompt_is_only_the_reply(task):
    db.set_oneoff_session(task["id"], "sess-1")
    db.mark_oneoff_delivered([m["id"] for m in db.pending_oneoff_messages(task["id"])])
    db.add_oneoff_message(task["id"], "wes", "It is postfix, not cron.")
    prompt = oneoff.build_prompt(db.get_oneoff(task["id"]), db.pending_oneoff_messages(task["id"]))
    assert prompt.startswith(f"{config.SITE.owner} replies:")
    assert "postfix" in prompt
    # The resumed session already holds the contract - do not re-brief.
    assert "ONE-OFF TASK" not in prompt
    assert "profile.md" not in prompt


def test_messages_typed_in_a_row_arrive_as_one_batch_oldest_first(task):
    db.add_oneoff_message(task["id"], "wes", "also check the disk")
    prompt = oneoff.build_prompt(task, db.pending_oneoff_messages(task["id"]))
    assert "2 messages" in prompt
    assert "may correct an earlier one" in prompt
    assert prompt.index("cron mail") < prompt.index("check the disk")


# --- the run driver ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_good_run_replies_and_keeps_the_session(task, monkeypatch):
    seen = _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="sess-9", result_text="Found it: /var was full.",
    ))
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    await worker.run_oneoff_task(task["id"], run_id, "opus")

    assert seen["resume_session"] is None
    assert ("agent", "Found it: /var was full.") in _messages(task["id"])
    assert db.get_oneoff(task["id"])["cli_session_id"] == "sess-9"
    assert db.pending_oneoff_messages(task["id"]) == []
    assert db.get_run(run_id)["status"] == "ok"
    assert oneoff.workspace(task["id"]).is_dir()


@pytest.mark.asyncio
async def test_a_resumed_run_passes_the_session_and_stores_the_new_fork(task, monkeypatch):
    db.set_oneoff_session(task["id"], "sess-old")
    seen = _fake_run(monkeypatch, agent_runner.RunResult(
        ok=True, session_id="sess-new", result_text="ok",
    ))
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    await worker.run_oneoff_task(task["id"], run_id, "opus")
    assert seen["resume_session"] == "sess-old"
    assert db.get_oneoff(task["id"])["cli_session_id"] == "sess-new"


@pytest.mark.asyncio
async def test_a_failed_run_leaves_an_honest_system_message(task, monkeypatch):
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=False, subtype="error_max_turns", result_text="",
    ))
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    await worker.run_oneoff_task(task["id"], run_id, "opus")
    roles = [m["role"] for m in db.list_oneoff_messages(task["id"])]
    assert "system" in roles and "agent" not in roles
    body = db.list_oneoff_messages(task["id"])[-1]["content_md"]
    assert "turn ceiling" in body
    assert db.get_run(run_id)["status"] == "error"


@pytest.mark.asyncio
async def test_a_lost_session_is_cleared_so_the_task_is_not_bricked(task, monkeypatch):
    db.set_oneoff_session(task["id"], "sess-gone")
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=False, result_text="No conversation found with session ID: sess-gone",
    ))
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    await worker.run_oneoff_task(task["id"], run_id, "opus")
    assert db.get_oneoff(task["id"])["cli_session_id"] is None
    body = db.list_oneoff_messages(task["id"])[-1]["content_md"]
    assert "could not be resumed" in body


@pytest.mark.asyncio
async def test_a_mere_failure_never_throws_away_a_live_session(task, monkeypatch):
    db.set_oneoff_session(task["id"], "sess-live")
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=False, subtype="error_during_execution", result_text="something broke",
    ))
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    await worker.run_oneoff_task(task["id"], run_id, "opus")
    assert db.get_oneoff(task["id"])["cli_session_id"] == "sess-live"


@pytest.mark.asyncio
async def test_a_canceled_run_says_so_and_does_not_continue(task, monkeypatch):
    _fake_run(monkeypatch, agent_runner.RunResult(ok=False, cancelled=True))
    db.add_oneoff_message(task["id"], "wes", "queued while running")
    called = []
    monkeypatch.setattr(worker, "spawn_oneoff", lambda tid: called.append(tid))
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    await worker.run_oneoff_task(task["id"], run_id, "opus")
    assert db.get_run(run_id)["status"] == "cancelled"
    assert "stopped" in db.list_oneoff_messages(task["id"])[-1]["content_md"]
    # A cancel is Wes saying stop - respawning would override him.
    assert called == []


@pytest.mark.asyncio
async def test_a_timeout_says_the_workspace_survived(task, monkeypatch):
    _fake_run(monkeypatch, agent_runner.RunResult(ok=False, timed_out=True))
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    await worker.run_oneoff_task(task["id"], run_id, "opus")
    assert db.get_run(run_id)["status"] == "timeout"
    assert "timed out" in db.list_oneoff_messages(task["id"])[-1]["content_md"]


@pytest.mark.asyncio
async def test_a_rate_limited_run_backs_off_and_tells_wes(task, monkeypatch):
    async def no_network(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(limits, "refresh_async", no_network)
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=False, is_rate_limited=True, result_text="usage limit reached",
    ))
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    await worker.run_oneoff_task(task["id"], run_id, "opus")
    assert "usage limit" in db.list_oneoff_messages(task["id"])[-1]["content_md"]
    assert db.get_setting("backoff_until")


@pytest.mark.asyncio
async def test_replies_typed_mid_run_start_the_next_run_themselves(task, monkeypatch):
    _fake_run(monkeypatch, agent_runner.RunResult(ok=True, session_id="s", result_text="first"))
    called = []
    monkeypatch.setattr(worker, "spawn_oneoff", lambda tid: called.append(tid))
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])

    async def fake_with_interruption(prompt, cwd, model, timeout_min, **kwargs):
        # Wes types while the agent is mid-run.
        db.add_oneoff_message(task["id"], "wes", "one more thing")
        return agent_runner.RunResult(ok=True, session_id="s", result_text="first")

    monkeypatch.setattr(agent_runner, "run_claude", fake_with_interruption)
    await worker.run_oneoff_task(task["id"], run_id, "opus")
    assert called == [task["id"]]


@pytest.mark.asyncio
async def test_no_continuation_on_an_archived_task(task, monkeypatch):
    db.add_oneoff_message(task["id"], "wes", "late reply")
    db.set_oneoff_status(task["id"], "archived")
    called = []
    monkeypatch.setattr(worker, "spawn_oneoff", lambda tid: called.append(tid))
    worker._continue_if_messages_waiting(task["id"])
    assert called == []


# --- spawning ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_refuses_archived_and_busy_tasks(task, monkeypatch):
    db.set_oneoff_status(task["id"], "archived")
    assert worker.spawn_oneoff(task["id"]) is None

    db.set_oneoff_status(task["id"], "open")
    run_id = db.create_run(None, "oneoff", "opus", oneoff_id=task["id"])
    # Two agents resuming one CLI session would fork the conversation.
    assert worker.spawn_oneoff(task["id"]) is None
    db.finish_run(run_id, "ok")

    assert worker.spawn_oneoff(9999) is None


@pytest.mark.asyncio
async def test_spawn_creates_the_run_row_and_registers_in_flight(task, monkeypatch):
    _fake_run(monkeypatch, agent_runner.RunResult(ok=True, session_id="s", result_text="hi"))
    run_id = worker.spawn_oneoff(task["id"])
    assert run_id is not None
    assert db.get_run(run_id)["oneoff_id"] == task["id"]
    assert run_id in worker._inflight
    # The pending restart logic waits on _inflight, so the run counting there
    # is what keeps a self-update from killing a one-off mid-conversation.
    await worker._inflight.pop(run_id)
    assert ("agent", "hi") in _messages(task["id"])


@pytest.mark.asyncio
async def test_a_crashing_run_still_settles_the_row_and_tells_wes(task, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(agent_runner, "run_claude", boom)
    run_id = worker.spawn_oneoff(task["id"])
    await worker._inflight.pop(run_id)
    assert db.get_run(run_id)["status"] == "error"
    assert "crashed" in db.list_oneoff_messages(task["id"])[-1]["content_md"]


# --- the routes --------------------------------------------------------------


def test_creating_a_task_spawns_and_lands_on_its_page(client, monkeypatch):
    called = []
    monkeypatch.setattr(worker, "spawn_oneoff", lambda tid: called.append(tid))
    resp = client.post("/tasks", data={"text": "resize the jellyfin swap"}, follow_redirects=False)
    assert resp.status_code == 303
    (row,) = db.list_oneoffs("open")
    assert resp.headers["location"] == f"/tasks/{row['id']}"
    assert called == [row["id"]]


def test_an_empty_task_creates_nothing(client, monkeypatch):
    monkeypatch.setattr(worker, "spawn_oneoff", lambda tid: None)
    resp = client.post("/tasks", data={"text": "   "}, follow_redirects=False)
    assert resp.headers["location"] == "/tasks"
    assert db.list_oneoffs() == []


def test_the_list_page_shows_open_and_archived(client, task):
    other = db.create_oneoff("an archived errand")
    db.set_oneoff_status(other["id"], "archived")
    html = client.get("/tasks").text
    assert "Fix the cron mail on testhost" in html
    assert "an archived errand" in html
    assert "archived (1)" in html


def test_the_session_page_renders_the_exchange(client, task):
    db.add_oneoff_message(task["id"], "agent", "It was **postfix**.")
    html = client.get(f"/tasks/{task['id']}").text
    assert "cron mail" in html
    assert "<strong>postfix</strong>" in html
    assert f"/tasks/{task['id']}/message" in html  # the reply box


def test_replying_queues_and_spawns(client, task, monkeypatch):
    called = []
    monkeypatch.setattr(worker, "spawn_oneoff", lambda tid: called.append(tid))
    client.post(f"/tasks/{task['id']}/message", data={"text": "try again"}, follow_redirects=False)
    assert ("wes", "try again") in _messages(task["id"])
    assert called == [task["id"]]


def test_an_archived_session_refuses_new_messages(client, task, monkeypatch):
    db.set_oneoff_status(task["id"], "archived")
    monkeypatch.setattr(worker, "spawn_oneoff", lambda tid: pytest.fail("must not spawn"))
    before = len(db.list_oneoff_messages(task["id"]))
    resp = client.post(
        f"/tasks/{task['id']}/message", data={"text": "hello?"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert len(db.list_oneoff_messages(task["id"])) == before
    # And the page offers no reply box, only the unarchive button.
    html = client.get(f"/tasks/{task['id']}").text
    assert f"/tasks/{task['id']}/message" not in html
    assert f"/tasks/{task['id']}/unarchive" in html


def test_archive_and_unarchive_round_trip(client, task):
    client.post(f"/tasks/{task['id']}/archive", follow_redirects=False)
    assert db.get_oneoff(task["id"])["status"] == "archived"
    client.post(f"/tasks/{task['id']}/unarchive", follow_redirects=False)
    assert db.get_oneoff(task["id"])["status"] == "open"


def test_unknown_task_ids_404(client):
    assert client.get("/tasks/999").status_code == 404
    assert client.post("/tasks/999/message", data={"text": "x"}).status_code == 404
    assert client.post("/tasks/999/archive").status_code == 404


def test_the_nav_carries_the_open_task_count(client, task):
    html = client.get("/tasks").text
    assert 'href="/tasks">tasks<span class="nav-count">1</span>' in html

"""Part 2 of the Claude app plugin's API (app/extrun.py, routes in
app/extapi.py): starting runs, steering them, one-off tasks and asks.

Wes, 2026-11-04: "Kick off builds there in my project portal tool. You could
directly prompt/create a project or talk to existing projects there for their
context and whatnot without me having to be the middle man." The contract is
the plugin's SPEC-runs.md; its rules pinned here:

- an API start is "run agent now" - the same approval setting, and every guard
  a manual run meets (hosted elsewhere, usage limit, restart, one agent per
  workspace), refused with a reason rather than a 500;
- a run started from the Claude app is attributed to the person and host;
- instructions and messages reach a run the way a note does;
- an ask cannot change anything.

Hosts and paths are invented.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import ask, claudeapp, config, db, extrun, midrun, notify, oneoff, runlog, worker

HOST = "test-laptop"
API = "/api/ext/v1"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app, headers={"X-Portal-Host": HOST,
                                         "X-Portal-Client": "claude-app-plugin/0.2"})


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, temp_data_dir):
    sent: list[tuple] = []

    async def fake_notify(title, message, **kw):
        sent.append((title, message, kw))

    monkeypatch.setattr(notify, "notify", fake_notify)
    return sent


@pytest.fixture(autouse=True)
def spawned(monkeypatch):
    """No agent is ever really started: the run row is made, the body of the
    run is a no-op. What was started, with which task, is recorded."""
    started: list[tuple[str, str]] = []

    async def fake_execute(project, task, run_id, model, parallel=False):
        started.append((project["slug"], task))

    monkeypatch.setattr(worker, "_execute_run", fake_execute)
    return started


@pytest.fixture
def oneoffs(monkeypatch):
    started: list[int] = []
    monkeypatch.setattr(worker, "spawn_oneoff", lambda task_id: started.append(task_id))
    return started


def here(slug="fridge-board", stage="active", **extra):
    return db.create_project("Fridge Board", "A board.", slug=slug, stage=stage, **extra)


def elsewhere(slug="metronome"):
    project = db.create_project("Metronome", "On a laptop.", slug=slug, stage="active")
    claudeapp.link(int(project["id"]), HOST, "/Users/someone/Metronome")
    return project


def runs_of(project) -> list:
    return db.list_runs(int(project["id"]))


def start(client, slug, **body):
    return client.post(f"{API}/projects/{slug}/runs", json=body)


# ------------------------------------------------------------- starting


def test_a_start_returns_the_run_attributed_to_the_caller(client, spawned):
    here()
    r = start(client, "fridge-board")
    assert r.status_code == 201, r.text
    run = r.json()["run"]
    assert run["project"] == "fridge-board"
    assert run["kind"] == "build"  # the gate is off by default
    assert run["status"] == "running"
    assert run["finished_at"] is None
    assert run["url"].endswith(f"/run/{run['id']}")
    assert run["started_by"]["host"] == HOST
    assert run["started_by"]["person"]
    assert set(run) >= {"id", "project", "kind", "status", "started_at", "finished_at",
                        "summary", "commits", "questions", "url", "started_by"}
    assert spawned == [("fridge-board", "build")]
    row = db.get_run(run["id"])
    assert row["started_by_host"] == HOST


def test_a_run_started_here_is_not_attributed(client):
    project = here()
    run_id = db.create_run(int(project["id"]), "build", "m")
    assert extrun.run_dict(db.get_run(run_id))["started_by"] is None


def test_instructions_are_a_note_written_before_the_run(client):
    project = here()
    r = start(client, "fridge-board", instructions="Make the header sticky.")
    assert r.status_code == 201
    notes = [j for j in db.list_journal(int(project["id"])) if j["kind"] == "note"]
    assert len(notes) == 1
    assert "Make the header sticky." in notes[0]["content_md"]
    assert HOST in notes[0]["content_md"]
    assert notes[0]["person_id"]
    assert notes[0]["delivered_at"] is None  # the run's prompt reads it


def test_a_start_on_a_project_hosted_elsewhere_is_refused(client, spawned):
    project = elsewhere()
    r = start(client, "metronome", instructions="Go.")
    assert r.status_code == 409
    assert r.json()["reason"] == "hosted_elsewhere"
    assert r.json()["error"]
    assert runs_of(project) == []
    assert spawned == []
    assert not [j for j in db.list_journal(int(project["id"])) if j["kind"] == "note"]


def test_a_second_start_while_one_runs_is_busy_and_names_the_run(client, spawned):
    project = here()
    first = start(client, "fridge-board").json()["run"]
    r = start(client, "fridge-board", instructions="And this.")
    assert r.status_code == 409
    assert r.json()["reason"] == "busy"
    assert r.json()["run"]["id"] == first["id"]
    assert len(runs_of(project)) == 1
    assert not [j for j in db.list_journal(int(project["id"])) if j["kind"] == "note"]


def test_a_run_parked_for_the_usage_window_holds_the_project(client):
    project = here()
    run_id = db.create_run(int(project["id"]), "build", "m")
    with db._LOCK:  # noqa: SLF001
        db.get_conn().execute("UPDATE runs SET status = 'paused' WHERE id = ?", (run_id,))
        db.get_conn().commit()
    r = start(client, "fridge-board")
    assert r.status_code == 409
    assert r.json()["reason"] == "paused"
    assert r.json()["run"]["id"] == run_id
    assert len(runs_of(project)) == 1


def test_a_workspace_held_by_a_leftover_agent_is_busy(client, monkeypatch):
    project = here()
    monkeypatch.setattr(worker, "workspace_leased", lambda slug: True)
    r = start(client, "fridge-board")
    assert r.status_code == 409
    assert r.json()["reason"] == "busy"
    assert runs_of(project) == []


def test_the_usage_limit_refuses_a_start(client):
    project = here()
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    db.set_setting("backoff_until", later)
    r = start(client, "fridge-board")
    assert r.status_code == 409
    assert r.json()["reason"] == "usage_limit"
    assert runs_of(project) == []


def test_a_restart_in_progress_refuses_a_start(client, monkeypatch):
    project = here()
    monkeypatch.setattr(worker, "_restarting", True)
    r = start(client, "fridge-board")
    assert r.status_code == 409
    assert r.json()["reason"] == "restarting"
    assert runs_of(project) == []


def test_the_parallel_cap_does_not_hold_a_start_any_more_than_run_now(client, monkeypatch):
    here()
    monkeypatch.setattr(worker.pacing, "parallel_cap", lambda n: 0)
    assert start(client, "fridge-board").status_code == 201


def test_a_bad_kind_is_a_400(client):
    here()
    r = start(client, "fridge-board", kind="deploy")
    assert r.status_code == 400
    assert "kind" in r.json()["error"]


def test_a_start_puts_a_shelved_project_back_on_active(client):
    project = here(stage="review")
    assert start(client, "fridge-board").status_code == 201
    assert db.get_project(int(project["id"]))["stage"] == "active"
    lines = [j["content_md"] for j in db.list_journal(int(project["id"])) if j["kind"] == "status"]
    assert any("Claude app" in line and "`review` -> `active`" in line for line in lines)


def test_unknown_project_is_a_404(client):
    assert start(client, "nope").status_code == 404


# ------------------------------------------------- the approval setting


@pytest.fixture
def gate_on():
    db.set_setting("require_build_approval", "1")


def test_under_the_gate_a_build_is_refused_as_needing_approval(client, gate_on, spawned):
    project = here()
    r = start(client, "fridge-board", kind="build")
    assert r.status_code == 409
    assert r.json()["reason"] == "needs_approval"
    assert runs_of(project) == []
    assert spawned == []


def test_under_the_gate_auto_gets_what_run_now_would(client, gate_on, spawned):
    project = here()
    expected = worker.task_for(db.get_project(int(project["id"])), manual=True)
    assert expected != "build"
    r = start(client, "fridge-board")
    assert r.status_code == 201
    assert r.json()["run"]["kind"] == expected


def test_under_the_gate_an_approved_project_builds(client, gate_on):
    project = here()
    db.update_project(int(project["id"]), build_approved=1)
    r = start(client, "fridge-board", kind="build")
    assert r.status_code == 201
    assert r.json()["run"]["kind"] == "build"


def test_with_the_gate_off_a_build_needs_nobody(client):
    here()
    r = start(client, "fridge-board", kind="build")
    assert r.status_code == 201


def test_a_plan_kind_is_honored(client):
    here()
    assert start(client, "fridge-board", kind="plan").json()["run"]["kind"] == "plan"


# ------------------------------------------------------------- reading


def test_status_words_follow_the_contract(client):
    project = here()
    ids = {}
    for status in ("ok", "error", "timeout", "cancelled"):
        ids[status] = db.create_run(int(project["id"]), "build", "m")
        db.finish_run(ids[status], status)
    got = {s: client.get(f"{API}/runs/{i}").json()["run"]["status"] for s, i in ids.items()}
    assert got == {"ok": "done", "error": "failed", "timeout": "failed", "cancelled": "cancelled"}


def test_a_missing_run_is_a_404(client):
    r = client.get(f"{API}/runs/999")
    assert r.status_code == 404
    assert r.json()["error"]


def test_a_run_lists_its_commits_and_questions(client):
    project = here()
    repo = config.PROJECTS_DIR / "fridge-board"
    repo.mkdir(parents=True)

    def git(*args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                               *args], cwd=repo, capture_output=True, text=True, check=True
                              ).stdout.strip()

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "before")
    before = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "Add the sticky header")
    after = git("rev-parse", "HEAD")
    run_id = db.create_run(int(project["id"]), "build", "m")
    question = db.file_question(int(project["id"]), "Blue or green?").row
    db.finish_run(run_id, "ok")
    db.set_run_workspace_heads(run_id, before, after)
    run = client.get(f"{API}/runs/{run_id}").json()["run"]
    assert run["commits"] == [{"sha": after, "subject": "Add the sticky header"}]
    assert run["questions"] == [int(question["id"])]


def test_project_runs_are_newest_first_and_limited(client):
    project = here()
    ids = [db.create_run(int(project["id"]), "build", "m") for _ in range(3)]
    for i in ids:
        db.finish_run(i, "ok")
    got = client.get(f"{API}/projects/fridge-board/runs?limit=2").json()["runs"]
    assert [r["id"] for r in got] == [ids[2], ids[1]]


def test_active_run_is_null_when_idle_and_the_run_when_not(client):
    assert client.get(f"{API}/active-run").json()["run"] is None
    here()
    run = start(client, "fridge-board").json()["run"]
    assert client.get(f"{API}/active-run").json()["run"]["id"] == run["id"]


def test_the_log_tails_from_an_offset(client):
    project = here()
    run_id = db.create_run(int(project["id"]), "build", "m")
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    runlog.log_path(run_id).write_text("hello ", encoding="utf-8")
    first = client.get(f"{API}/runs/{run_id}/log").json()
    assert first == {"text": "hello ", "next_offset": 6, "done": False}
    with runlog.log_path(run_id).open("a", encoding="utf-8") as fh:
        fh.write("world")
    db.finish_run(run_id, "ok")
    second = client.get(f"{API}/runs/{run_id}/log?offset=6").json()
    assert second["text"] == "world"
    assert second["done"] is True


# ------------------------------------------------------------ steering


def _note_rows(project):
    return [j for j in db.list_journal(int(project["id"])) if j["kind"] == "note"]


def test_a_message_to_a_listening_run_is_handed_over_now(client, monkeypatch):
    project = here()
    run = start(client, "fridge-board").json()["run"]
    monkeypatch.setattr(midrun, "can_hear", lambda run_id: True)
    r = client.post(f"{API}/runs/{run['id']}/messages", json={"text": "Use blue."})
    assert r.status_code == 200
    assert r.json()["delivery"] == "now"
    note = _note_rows(project)[-1]
    assert "Use blue." in note["content_md"]
    assert note["hear_now"] == 1


def test_a_message_to_a_run_that_cannot_hear_waits_for_the_next(client, monkeypatch):
    project = here()
    run = start(client, "fridge-board").json()["run"]
    monkeypatch.setattr(midrun, "can_hear", lambda run_id: False)
    r = client.post(f"{API}/runs/{run['id']}/messages", json={"text": "Use blue."})
    assert r.json()["delivery"] == "next_run"
    assert not _note_rows(project)[-1]["hear_now"]


def test_a_message_to_a_finished_run_is_filed_and_wakes_a_shelved_project(client, monkeypatch):
    project = here(stage="review")
    run_id = db.create_run(int(project["id"]), "build", "m")
    db.finish_run(run_id, "ok")
    woke = []

    async def fake_reactivate(p):
        woke.append(p["id"])
        return True

    monkeypatch.setattr(worker, "reactivate_on_note", fake_reactivate)
    r = client.post(f"{API}/runs/{run_id}/messages", json={"text": "One more thing."})
    assert r.status_code == 200
    assert r.json()["delivery"] == "next_run"
    assert "next run" in r.json()["message"]
    assert woke == [project["id"]]
    assert not _note_rows(project)[-1]["hear_now"]


def test_a_message_to_a_task_run_goes_to_its_task(client, oneoffs):
    task = db.create_oneoff("Look into it.")
    run_id = db.create_run(None, "oneoff", "m", oneoff_id=int(task["id"]))
    r = client.post(f"{API}/runs/{run_id}/messages", json={"text": "And this."})
    assert r.json()["delivery"] == "task"
    assert [m["content_md"] for m in db.list_oneoff_messages(int(task["id"]))][-1] == "And this."
    assert oneoffs == [int(task["id"])]


def test_a_blank_message_is_a_400(client):
    project = here()
    run_id = db.create_run(int(project["id"]), "build", "m")
    assert client.post(f"{API}/runs/{run_id}/messages", json={"text": " "}).status_code == 400


def test_cancel_settles_a_run_with_nothing_to_kill(client):
    project = here()
    run_id = db.create_run(int(project["id"]), "build", "m")
    r = client.post(f"{API}/runs/{run_id}/cancel")
    assert r.status_code == 200
    assert r.json()["run"]["status"] in ("cancelled", "failed")
    assert db.get_run(run_id)["status"] != "running"


def test_pause_and_resume_mirror_the_buttons(client, monkeypatch):
    project = here()
    run_id = db.create_run(int(project["id"]), "build", "m")
    monkeypatch.setattr(midrun, "can_hear", lambda run_id: True)
    r = client.post(f"{API}/runs/{run_id}/pause")
    assert r.json()["outcome"] == "paused"
    assert r.json()["run"]["status"] == "paused"
    r = client.post(f"{API}/runs/{run_id}/resume")
    assert r.json()["outcome"] == "resumed"
    assert r.json()["run"]["status"] == "running"


# ------------------------------------------------------------------ tasks


def test_a_task_is_created_started_and_read_back(client, oneoffs):
    here()
    r = client.post(f"{API}/tasks", json={"text": "Compare two charting libraries.",
                                          "project": "fridge-board"})
    assert r.status_code == 201
    task = r.json()["task"]
    assert task["project"] == "fridge-board"
    assert task["status"] == "open"
    assert task["messages"][0]["from"] == "person"
    assert task["messages"][0]["text"] == "Compare two charting libraries."
    assert task["url"].endswith(f"/tasks/{task['id']}")
    assert oneoffs == [task["id"]]
    assert client.get(f"{API}/tasks/{task['id']}").json()["task"]["id"] == task["id"]


def test_a_task_about_a_project_is_told_where_it_is(client, oneoffs):
    here()
    task = client.post(f"{API}/tasks", json={"text": "Look.", "project": "fridge-board"}
                       ).json()["task"]
    row = db.get_oneoff(task["id"])
    prompt = oneoff.build_prompt(row, db.pending_oneoff_messages(task["id"]))
    assert "Fridge Board" in prompt
    assert str(config.PROJECTS_DIR / "fridge-board") in prompt


def test_a_task_about_nothing_has_no_project_section(client, oneoffs):
    task = client.post(f"{API}/tasks", json={"text": "Look."}).json()["task"]
    assert task["project"] is None
    row = db.get_oneoff(task["id"])
    assert "The project this is about" not in oneoff.build_prompt(row, [])


def test_a_task_naming_an_unknown_project_is_a_404(client, oneoffs):
    r = client.post(f"{API}/tasks", json={"text": "Look.", "project": "nope"})
    assert r.status_code == 404
    assert oneoffs == []


def test_a_task_message_is_appended_and_starts_the_agent(client, oneoffs):
    task = client.post(f"{API}/tasks", json={"text": "Look."}).json()["task"]
    r = client.post(f"{API}/tasks/{task['id']}/messages", json={"text": "Deeper."})
    assert [m["text"] for m in r.json()["task"]["messages"]] == ["Look.", "Deeper."]
    assert oneoffs == [task["id"], task["id"]]


def test_an_archived_task_refuses_a_message(client, oneoffs):
    task = client.post(f"{API}/tasks", json={"text": "Look."}).json()["task"]
    db.set_oneoff_status(task["id"], "archived")
    r = client.post(f"{API}/tasks/{task['id']}/messages", json={"text": "Deeper."})
    assert r.status_code == 409
    assert len(db.list_oneoff_messages(task["id"])) == 1


def test_tasks_list_is_limited(client, oneoffs):
    for i in range(3):
        client.post(f"{API}/tasks", json={"text": f"Task {i}."})
    assert len(client.get(f"{API}/tasks?limit=2").json()["tasks"]) == 2


# ------------------------------------------------------------------- asks


@pytest.fixture
def no_background(monkeypatch):
    """`ask.start` answers on a background task; the test drives the answer
    itself with `ask.answer`, so nothing runs behind its back."""
    calls = []

    async def fake_answer(project_id, question, reply_chat_id=None, quiet=False):
        calls.append((project_id, question, quiet))

    monkeypatch.setattr(ask, "answer", fake_answer)
    return calls


def test_an_ask_is_pending_then_answered(client, no_background, monkeypatch):
    project = here()
    r = client.post(f"{API}/projects/fridge-board/ask", json={"question": "What color?"})
    assert r.status_code == 202
    ask_id = r.json()["ask"]["id"]
    assert r.json()["ask"]["status"] == "pending"
    assert no_background == [(project["id"], "What color?", True)]
    assert client.get(f"{API}/asks/{ask_id}").json()["ask"]["status"] == "pending"
    db.add_journal(int(project["id"]), "agent", "answer", "Blue.")
    ask._PENDING.discard(int(project["id"]))  # noqa: SLF001
    got = client.get(f"{API}/asks/{ask_id}").json()["ask"]
    assert got == {"id": ask_id, "status": "answered", "answer": "Blue."}


def test_an_ask_whose_answer_failed_says_so(client, no_background):
    project = here()
    ask_id = client.post(f"{API}/projects/fridge-board/ask", json={"question": "?"}
                         ).json()["ask"]["id"]
    db.add_journal(int(project["id"]), "agent", "answer", ask.FAILED_ANSWER)
    ask._PENDING.discard(int(project["id"]))  # noqa: SLF001
    assert client.get(f"{API}/asks/{ask_id}").json()["ask"]["status"] == "failed"


def test_an_ask_nobody_is_answering_any_more_is_failed_not_pending(client, no_background):
    project = here()
    ask_id = client.post(f"{API}/projects/fridge-board/ask", json={"question": "?"}
                         ).json()["ask"]["id"]
    ask._PENDING.discard(int(project["id"]))  # noqa: SLF001 - as after a restart
    assert client.get(f"{API}/asks/{ask_id}").json()["ask"]["status"] == "failed"


def test_an_answer_belongs_to_its_own_question_only(client, no_background):
    project = here()
    first = client.post(f"{API}/projects/fridge-board/ask", json={"question": "1?"}
                        ).json()["ask"]["id"]
    db.add_journal(int(project["id"]), "agent", "answer", "One.")
    ask._PENDING.discard(int(project["id"]))  # noqa: SLF001
    second = client.post(f"{API}/projects/fridge-board/ask", json={"question": "2?"}
                         ).json()["ask"]["id"]
    assert client.get(f"{API}/asks/{first}").json()["ask"]["answer"] == "One."
    assert client.get(f"{API}/asks/{second}").json()["ask"]["status"] == "pending"


def test_a_second_ask_while_one_thinks_is_busy(client, no_background):
    here()
    first = client.post(f"{API}/projects/fridge-board/ask", json={"question": "1?"}
                        ).json()["ask"]["id"]
    r = client.post(f"{API}/projects/fridge-board/ask", json={"question": "2?"})
    assert r.status_code == 409
    assert r.json()["reason"] == "busy"
    assert r.json()["ask"]["id"] == first


def test_an_unknown_ask_is_a_404(client):
    assert client.get(f"{API}/asks/999").status_code == 404
    project = here()
    note = db.add_journal(int(project["id"]), "user", "note", "Not an ask.")
    assert client.get(f"{API}/asks/{note}").status_code == 404


def test_an_asking_agent_is_given_no_tool_that_writes():
    argv = ask.build_command("q", "m")
    assert "--dangerously-skip-permissions" not in argv
    assert "--strict-mcp-config" in argv
    denied = argv[argv.index("--disallowedTools") + 1:]
    for tool in ("Bash", "Edit", "Write", "NotebookEdit"):
        assert tool in denied
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
    assert set(allowed) <= {"Read", "Glob", "Grep", "WebSearch", "WebFetch"}


def test_an_answer_that_tries_to_change_the_project_changes_nothing(monkeypatch, _quiet):
    """The answering agent emits everything a run's report could carry - a
    stage move, todos, a question, a commit claim. None of it is applied."""
    project = here()
    pid = int(project["id"])
    attempt = json.dumps({
        "summary": ["committed abc123"], "new_stage": "review", "blocked_on": "you",
        "questions": [{"question": "Ship it?"}],
        "todo_updates": {"add": [{"text": "Rewrite everything", "owner": "agent"}]},
        "title": "Hijacked",
    })

    async def fake_run_ask(prompt, cwd, model):
        return attempt

    monkeypatch.setattr(ask, "run_ask", fake_run_ask)
    before = (dict(db.get_project(pid)), len(db.list_todos(pid)),
              db.count_open_questions(pid), len(db.list_runs(pid)))
    asyncio.run(ask.answer(pid, "What now?", quiet=True))
    after = (dict(db.get_project(pid)), len(db.list_todos(pid)),
             db.count_open_questions(pid), len(db.list_runs(pid)))
    assert after == before
    assert not (config.PROJECTS_DIR / "fridge-board" / ".portal" / "report.json").exists()
    assert _quiet == []  # quiet: a program is polling, nobody's phone buzzes


def test_an_ask_from_the_page_still_notifies(monkeypatch, _quiet):
    project = here()

    async def fake_run_ask(prompt, cwd, model):
        return "Blue."

    monkeypatch.setattr(ask, "run_ask", fake_run_ask)
    asyncio.run(ask.answer(int(project["id"]), "What color?"))
    assert len(_quiet) == 1


# ------------------------------------------------------ creating projects


def test_a_brief_without_a_path_makes_a_project_built_here(client, spawned):
    r = client.post(f"{API}/projects", json={"title": "Chore Chart",
                                             "brief": "A chore chart for the fridge."})
    assert r.status_code == 201, r.text
    slug = r.json()["project"]["slug"]
    project = db.get_project_by_slug(slug)
    assert project["stage"] == "backlog"
    assert not claudeapp.hosted_elsewhere(project)
    assert claudeapp.locations(int(project["id"])) == []
    notes = [j["content_md"] for j in db.list_journal(int(project["id"])) if j["kind"] == "note"]
    assert any("A chore chart for the fridge." in n for n in notes)
    assert "run" not in r.json()
    assert spawned == []


def test_start_true_starts_the_first_run(client, spawned):
    r = client.post(f"{API}/projects", json={"title": "Chore Chart",
                                             "brief": "A chore chart.", "start": True})
    assert r.status_code == 201
    body = r.json()
    assert body["run"]["status"] == "running"
    assert body["run"]["started_by"]["host"] == HOST
    project = db.get_project_by_slug(body["project"]["slug"])
    assert project["stage"] == "active"
    assert spawned == [(project["slug"], body["run"]["kind"])]


def test_start_on_a_folder_elsewhere_creates_but_does_not_run(client, spawned):
    r = client.post(f"{API}/projects", json={
        "title": "Metronome", "host": HOST, "path": "/Users/someone/Metronome",
        "brief": "A click track.", "start": True})
    assert r.status_code == 201
    assert "run" not in r.json()
    assert r.json()["run_refused"]["reason"] == "hosted_elsewhere"
    assert spawned == []
    project = db.get_project_by_slug(r.json()["project"]["slug"])
    assert project["initial_idea"] == "A click track."


def test_neither_path_nor_brief_is_still_a_400(client):
    r = client.post(f"{API}/projects", json={"title": "Nothing"})
    assert r.status_code == 400
    assert "path" in r.json()["error"]


# ------------------------------------------------------------- the page


def test_the_run_page_says_where_an_api_run_came_from(client):
    here()
    run = start(client, "fridge-board").json()["run"]
    page = client.get(f"/run/{run['id']}").text
    assert "from the Claude app on" in page
    assert HOST in page


def test_a_run_started_here_says_nothing_about_the_claude_app(client):
    project = here()
    run_id = db.create_run(int(project["id"]), "build", "m")
    assert "from the Claude app" not in client.get(f"/run/{run_id}").text

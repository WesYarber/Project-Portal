"""Claude Code on another machine, talking to the portal over MCP (app/extmcp.py).

Wes, 2026-11-10: Claude Code on his Mac should "interface directly with the
projects and send notes, view project context, etc", find the project from "some
link to a project or the name of it", and "create a new project if the right one
doesn't already exist". Pinned here:

- the transport is legal streamable-HTTP MCP: initialize, tools/list, tools/call,
  a notification gets a bare 202, and GET is a 405;
- a project is named by URL, slug or name, and an ambiguous name is refused
  with the candidates, never guessed;
- a note behaves like the page's green "add note": it wakes the agent, unless
  asked only to queue, and never schedules a project built on another machine;
- a linked folder is recognized again, the nearest one above the cwd winning;
- a tool that fails says why as an isError result.

Hosts and paths are invented.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import ask, claudeapp, config, db, extmcp, notify, worker

HOST = "test-laptop"
MCP = "/api/ext/v1/mcp"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app, headers={"X-Portal-Host": HOST})


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, temp_data_dir):
    async def fake_notify(title, message, **kw):
        return None

    monkeypatch.setattr(notify, "notify", fake_notify)

    def reset():
        while not worker.manual_queue.empty():
            worker.manual_queue.get_nowait()

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def spawned(monkeypatch):
    started: list[tuple[str, str]] = []

    async def fake_execute(project, task, run_id, model, parallel=False):
        started.append((project["slug"], task))

    monkeypatch.setattr(worker, "_execute_run", fake_execute)
    return started


def queued() -> list[int]:
    return list(worker.manual_queue._queue)  # type: ignore[attr-defined]


def here(title="Fridge Board", slug="fridge-board", stage="active"):
    return db.create_project(title, "A board for the fridge.", slug=slug, stage=stage)


def last_by_user(project):
    return [e for e in db.list_journal(int(project["id"])) if e["author"] == "user"][0]


def checkout(slug: str) -> None:
    (config.PROJECTS_DIR / slug / ".git").mkdir(parents=True, exist_ok=True)


def rpc(client, method, params=None, msg_id=1):
    r = client.post(MCP, json={"jsonrpc": "2.0", "id": msg_id, "method": method,
                               "params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


def call(client, name, **args):
    result = rpc(client, "tools/call", {"name": name, "arguments": args})["result"]
    return result["content"][0]["text"], bool(result.get("isError"))


# ------------------------------------------------------------ transport


def test_the_handshake_lists_the_tools(client):
    init = rpc(client, "initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert init["protocolVersion"] == "2025-03-26"
    assert "tools" in init["capabilities"]
    assert init["instructions"]
    names = {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}
    assert names == {"find_projects", "project_context", "send_note", "log_progress",
                     "read_file", "todos", "create_project", "link_folder", "start_run",
                     "run_status", "ask_project"}


def test_a_notification_gets_a_bare_202_and_get_is_405(client):
    r = client.post(MCP, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""
    assert client.get(MCP).status_code == 405


def test_an_unknown_method_is_a_jsonrpc_error_and_bad_json_a_400(client):
    assert rpc(client, "resources/list")["error"]["code"] == -32601
    assert client.post(MCP, content=b"{nope").status_code == 400


def test_the_feature_is_advertised(client):
    assert "mcp" in client.get("/api/ext/v1/").json()["features"]


# ------------------------------------------------------------- resolving


def test_a_project_is_named_by_url_slug_or_name(client):
    here()
    for ref in ("http://somewhere:8500/project/fridge-board", "fridge-board", "Fridge Board",
                "fridge"):
        text, err = call(client, "project_context", project=ref)
        assert not err, (ref, text)
        assert "Fridge Board" in text


def test_an_ambiguous_name_is_refused_with_the_candidates(client):
    here()
    here("Fridge Magnets", "fridge-magnets")
    text, err = call(client, "project_context", project="fridge")
    assert err
    assert "fridge-board" in text and "fridge-magnets" in text


def test_an_exact_title_wins_over_a_broader_match(client):
    here()
    here("Fridge Board Two", "fridge-board-two")
    text, err = call(client, "project_context", project="fridge board")
    assert not err and "fridge-board" in text


def test_an_unknown_project_or_url_is_an_error_result(client):
    assert call(client, "project_context", project="nothing-here")[1]
    assert call(client, "project_context", project="http://x:8500/project/gone")[1]


# ----------------------------------------------------------------- notes


def test_a_note_wakes_the_project_like_the_green_button(client):
    project = here()
    checkout("fridge-board")
    text, err = call(client, "send_note", project="fridge-board", text="Make it blue.")
    assert not err, text
    assert "queued" in text
    assert queued() == [project["id"]]
    entry = last_by_user(project)
    assert entry["kind"] == "note" and "Make it blue." in entry["content_md"]
    assert HOST in entry["content_md"]


def test_a_note_asked_only_to_queue_starts_nothing(client):
    here()
    checkout("fridge-board")
    text, err = call(client, "send_note", project="fridge-board", text="Later.", wake=False)
    assert not err and queued() == []


def test_a_note_never_schedules_a_project_built_elsewhere(client):
    project = here()
    claudeapp.link(int(project["id"]), HOST, "/Users/someone/Fridge")
    text, err = call(client, "send_note", project="fridge-board", text="Hi.")
    assert not err and "another machine" in text
    assert queued() == []


def test_a_note_carries_markdown_files_as_attachments(client):
    project = here()
    text, err = call(client, "send_note", project="fridge-board", text="The spec.",
                     files=[{"name": "SPEC.md", "content": "# Spec\n"}], wake=False)
    assert not err and "1 file" in text
    rows = db.list_attachments(int(project["id"]))
    assert [r["orig_name"] for r in rows] == ["SPEC.md"]
    assert rows[0]["journal_id"]


def test_progress_is_journaled_and_starts_nothing(client):
    project = here()
    checkout("fridge-board")
    text, err = call(client, "log_progress", project="fridge-board", text="Built the view.")
    assert not err and queued() == []
    assert last_by_user(project)["kind"] == "progress"


# ----------------------------------------------------------------- files


def test_a_workspace_file_is_read_and_escapes_are_refused(client):
    here()
    workspace = config.PROJECTS_DIR / "fridge-board"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "HANDOFF.md").write_text("# Over to you\n")
    text, err = call(client, "read_file", project="fridge-board")
    assert not err and "HANDOFF.md" in text
    text, err = call(client, "read_file", project="fridge-board", path="HANDOFF.md")
    assert not err and "# Over to you" in text
    assert call(client, "read_file", project="fridge-board", path="../../portal.db")[1]


# ----------------------------------------------------- folders and create


def test_a_linked_folder_is_recognized_from_a_subfolder(client):
    here()
    here("Other", "other")
    assert not call(client, "link_folder", project="fridge-board", folder="/Users/s/Fridge")[1]
    call(client, "link_folder", project="other", folder="/Users/s/Fridge/Vendor/Other")
    text, _ = call(client, "find_projects", folder="/Users/s/Fridge/Sources")
    assert "fridge-board" in text and "`other`" not in text
    text, _ = call(client, "find_projects", folder="/Users/s/Fridge/Vendor/Other/x")
    assert "`other`" in text
    text, _ = call(client, "find_projects", folder="/Users/s/Fridgeless")
    assert "No project is linked" in text


def test_this_machines_link_beats_another_machines(client):
    a, b = here(), here("Other", "other")
    claudeapp.link(int(a["id"]), "someone-else", "/Users/s/App")
    claudeapp.link(int(b["id"]), HOST, "/Users/s/App")
    text = call(client, "find_projects", folder="/Users/s/App")[0]
    assert "`other`" in text and "fridge-board" not in text


def test_linking_needs_the_machine_to_say_its_name(temp_data_dir):
    from app import main

    here()
    bare = TestClient(main.app)
    r = bare.post(MCP, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "link_folder", "arguments": {"project": "fridge-board", "folder": "/x"}}})
    assert r.json()["result"]["isError"]


def test_create_with_a_folder_is_built_here_and_never_run(client):
    text, err = call(client, "create_project", title="Mac Widget", folder="/Users/s/Widget",
                     brief="A menu-bar widget.", start=True)
    assert not err, text
    project = db.get_project_by_slug("mac-widget")
    assert claudeapp.hosted_elsewhere(project)
    assert "did not start" in text
    assert "CLAUDE.md" in text
    assert queued() == []


def test_create_with_a_brief_and_start_puts_an_agent_on_it(client, spawned):
    text, err = call(client, "create_project", title="Tide Clock",
                     brief="A clock that shows the tide.", start=True)
    assert not err, text
    assert "first run is #" in text
    assert spawned and spawned[0][0] == "tide-clock"


def test_create_needs_a_brief_or_a_folder(client):
    assert call(client, "create_project", title="Nothing")[1]


# ------------------------------------------------------------ runs, asks


def test_a_refused_start_says_why(client):
    project = here()
    claudeapp.link(int(project["id"]), HOST, "/Users/s/Fridge")
    text, err = call(client, "start_run", project="fridge-board")
    assert err and "hosted_elsewhere" in text


def test_a_run_started_is_reported_and_its_status_read(client, spawned):
    here()
    text, err = call(client, "start_run", project="fridge-board", instructions="Go.")
    assert not err and spawned
    run_id = int(text.split("#")[1].split()[0])
    text, err = call(client, "run_status", run_id=run_id)
    assert not err and f"Run #{run_id}" in text
    text, err = call(client, "run_status", project="fridge-board")
    assert not err and f"#{run_id}" in text


def test_todos_add_and_complete(client):
    here()
    text, _ = call(client, "todos", project="fridge-board", add="Paint it.")
    todo_id = int(text.split("#")[1].split(".")[0])
    text, _ = call(client, "todos", project="fridge-board", complete=[todo_id, 9999])
    assert f"Ticked #{todo_id}" in text and "#9999 is not" in text
    theirs = db.add_todo(int(here("Other", "other")["id"]), "Not yours.", owner="agent")
    text, _ = call(client, "todos", project="fridge-board", complete=[theirs["id"]])
    assert "is not a todo on this project" in text
    assert not db.get_todo(int(theirs["id"]))["done"]


@pytest.fixture
def no_background(monkeypatch):
    async def fake_answer(project_id, question, reply_chat_id=None, quiet=False):
        return None

    monkeypatch.setattr(ask, "answer", fake_answer)
    monkeypatch.setattr(extmcp, "ASK_POLL_SEC", 0.01)


def test_an_ask_still_thinking_hands_back_its_id_then_answers(client, no_background):
    project = here()
    text, err = call(client, "ask_project", project="fridge-board", question="Color?",
                     wait_seconds=0)
    assert not err and "still being answered" in text
    ask_id = int(text.split("#")[1].split()[0])
    assert call(client, "ask_project", project="fridge-board", question="Again?")[1]
    db.add_journal(int(project["id"]), "agent", "answer", "Blue.")
    ask._PENDING.discard(int(project["id"]))  # noqa: SLF001
    assert call(client, "ask_project", ask_id=ask_id, wait_seconds=0) == ("Blue.", False)


def test_an_unknown_tool_is_an_error_result(client):
    assert call(client, "rm_rf")[1]


def test_settings_shows_the_one_line_setup(client):
    page = client.get("/settings").text
    assert "claude mcp add --transport http" in page
    assert f"{config.SITE.base_url}{MCP}" in page

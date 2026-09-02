"""The portal as an MCP server for its own runs (app/portalmcp.py, RESEARCH.md §2/§7).

One tool, `ask`: file a question mid-run, notify immediately, and wait a couple
of minutes for the answer instead of parking it until the report.

These tests pin the decisions - the per-run scope and its fail-*closed* posture
(the opposite of the hook relay's), the ask cap, what a dedupe against an
already-answered question returns, the wait and every way it can end, whose name
the tool description addresses, and the argv wiring. The JSON-RPC half is driven
through `app/mcpstdio.py`'s own `serve()` with real pipes.

What no test here can prove is whether the installed CLI starts the relay at all
and shows the model the tool - that is a property of the binary, so it is
checked live by the workspace's `scripts/mcp_live.py`, which drives a real `claude -p`.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, mcpstdio, people, portalmcp, quickreplies, settings_form


@pytest.fixture
def project():
    return db.create_project(title="A project", description="d", stage="active")


@pytest.fixture(autouse=True)
def no_leftover_scopes():
    """`_SCOPES` is a module global, so a run id another test registered would
    still be live here - and the ask cap is per-run state living in it."""
    portalmcp._SCOPES.clear()
    portalmcp._WAITING.clear()
    yield
    portalmcp._SCOPES.clear()
    portalmcp._WAITING.clear()


@pytest.fixture(autouse=True)
def no_real_notifications(monkeypatch):
    """Every `ask` that files a new question pushes one. Collected, never sent."""
    sent: list[dict] = []

    async def fake_notify(title, body, **kw):
        sent.append({"title": title, "body": body, **kw})

    monkeypatch.setattr(portalmcp.notify, "notify", fake_notify)
    return sent


def _begin(project, task="build"):
    """Register run 1 on this project and return its token."""
    raw = portalmcp.begin(1, int(project["id"]), task)
    assert raw is not None
    cfg = json.loads(raw)
    return cfg["mcpServers"]["portal"]["args"][3]


def _ask(project, token=None, run=1, **args):
    token = token if token is not None else _begin(project)
    args.setdefault("wait_seconds", 0)
    return asyncio.run(portalmcp.call(run, token, "ask", args))


def _text(result: dict) -> str:
    return result["content"][0]["text"]


# --------------------------------------------------------------------------
# The scope
# --------------------------------------------------------------------------

def test_begin_points_the_cli_at_this_portal_and_this_run(project, monkeypatch):
    monkeypatch.setattr(config, "PORT", 8511)
    raw = portalmcp.begin(7, int(project["id"]))
    server = json.loads(raw)["mcpServers"]["portal"]
    assert server["type"] == "stdio"
    assert server["command"] == sys.executable
    assert server["args"][0].endswith("app/mcpstdio.py")
    assert server["args"][1] == "http://127.0.0.1:8511"
    assert server["args"][2] == "7"
    # The token is the run's, not a shared secret: two runs get two.
    other = json.loads(portalmcp.begin(8, int(project["id"])))
    assert other["mcpServers"]["portal"]["args"][3] != server["args"][3]


def test_end_retires_the_scope(project):
    token = _begin(project)
    assert portalmcp.tools(1, token) is not None
    portalmcp.end(1)
    assert portalmcp.tools(1, token) is None


def test_the_tool_is_off_when_the_setting_is(project):
    db.set_setting("mcp_tools", "0")
    assert portalmcp.begin(1, int(project["id"])) is None


@pytest.mark.parametrize("task", ["reflect", "compact"])
def test_memory_maintenance_runs_carry_no_tool(project, task):
    # Nobody is waiting to answer a question from a run that is rewriting
    # learnings.md, so blocking one for four minutes would be pure wall clock.
    assert portalmcp.begin(1, int(project["id"]), task) is None


@pytest.mark.parametrize("task", ["triage", "plan", "build", "research"])
def test_project_work_carries_it(project, task):
    assert portalmcp.begin(1, int(project["id"]), task) is not None


# --------------------------------------------------------------------------
# Fails closed - the whole way this differs from the hook relay
# --------------------------------------------------------------------------

def test_an_unknown_run_is_offered_no_tools(project):
    _begin(project)
    assert portalmcp.tools(999, "anything") is None


def test_a_wrong_token_is_offered_no_tools(project):
    _begin(project)
    assert portalmcp.tools(1, "not-the-token") is None


def test_a_blank_token_does_not_match_a_scope(project):
    # A scope whose token compared equal to "" would be reachable by any caller
    # that simply omitted the parameter.
    _begin(project)
    assert portalmcp.tools(1, "") is None


def test_a_call_from_an_unknown_run_files_nothing(project):
    _begin(project)
    result = _ask(project, token="wrong-token", question="Should I ship it?")
    assert result["isError"] is True
    assert "does not recognize" in _text(result)
    # The point of failing closed: no question, and therefore no notification.
    assert db.open_questions(int(project["id"])) == []


def test_an_unknown_tool_name_is_refused(project):
    token = _begin(project)
    result = asyncio.run(portalmcp.call(1, token, "delete_everything", {}))
    assert result["isError"] is True
    assert "No such tool" in _text(result)


def test_a_question_with_no_text_is_refused(project):
    result = _ask(project, question="   ")
    assert result["isError"] is True
    assert db.open_questions(int(project["id"])) == []


# --------------------------------------------------------------------------
# The tool description
# --------------------------------------------------------------------------

def test_the_description_addresses_the_projects_own_principal(project):
    """A run on a project the install owner is not on works for somebody else,
    and a tool saying "ask Wes" would point every mid-run decision at the wrong
    person - the bug `people.principal` exists to fix in the contract."""
    karli = people.add(name="Karli", gender="female")
    people.set_members(int(project["id"]), [int(karli)])
    token = _begin(project)
    description = portalmcp.tools(1, token)[0]["description"]
    assert "Karli" in description
    assert config.SITE.owner not in description
    assert "her phone" in description


def test_the_description_addresses_the_owner_on_his_own_project(project):
    token = _begin(project)
    description = portalmcp.tools(1, token)[0]["description"]
    assert config.SITE.owner in description


def test_the_tool_takes_a_question_and_nothing_is_required_but_that(project):
    token = _begin(project)
    schema = portalmcp.tools(1, token)[0]["inputSchema"]
    assert schema["required"] == ["question"]
    assert set(schema["properties"]) == {"question", "context", "options", "wait_seconds"}


# --------------------------------------------------------------------------
# Asking
# --------------------------------------------------------------------------

def test_asking_files_the_question_and_pushes_it(project, no_real_notifications):
    _ask(project, question="Which color should the button be?",
         context="Both are in the palette.", options=["green", "blue"])
    rows = db.open_questions(int(project["id"]))
    assert len(rows) == 1
    assert rows[0]["question"] == "Which color should the button be?"
    assert rows[0]["context"] == "Both are in the palette."
    assert quickreplies.decode(rows[0]["quick_options"]) == ["green", "blue"]
    # The push is what makes this different from a question in a report: it
    # reaches a phone now, with the answer buttons on it.
    assert len(no_real_notifications) == 1
    assert no_real_notifications[0]["question_id"] == rows[0]["id"]
    assert no_real_notifications[0]["question_slot"] == rows[0]["slot"]


def test_an_unanswered_question_ends_the_call_rather_than_the_run(project):
    result = _ask(project, question="Should I ship it?")
    assert result["isError"] is False
    text = _text(result)
    assert "No answer yet" in text
    assert "carry on without it" in text
    # And it must not be asked twice - the report is the other channel.
    assert "leave it out of your report" in text


def test_an_answer_that_arrives_during_the_wait_comes_back_to_the_run(project):
    """The whole point: the round trip closes inside the run, not a run later."""
    token = _begin(project)

    async def drive():
        task = asyncio.create_task(
            portalmcp.call(1, token, "ask",
                           {"question": "Which color?", "wait_seconds": 30})
        )
        # Let the ask file its row, then answer it the way a phone tap would.
        for _ in range(100):
            await asyncio.sleep(0.01)
            rows = db.open_questions(int(project["id"]))
            if rows:
                db.answer_question(int(rows[0]["id"]), "the green one")
                break
        return await asyncio.wait_for(task, timeout=10)

    result = asyncio.run(drive())
    assert result["isError"] is False
    assert "the green one" in _text(result)
    assert f"{config.SITE.owner} answered" in _text(result)


def test_a_question_put_aside_is_reported_as_put_aside(project):
    """"Save for later" is an answer of a kind - the run should stop waiting and
    carry on, not sit out the full wait for a decision that is not coming."""
    token = _begin(project)

    async def drive():
        task = asyncio.create_task(
            portalmcp.call(1, token, "ask", {"question": "Which color?", "wait_seconds": 30})
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            rows = db.open_questions(int(project["id"]))
            if rows:
                db.dismiss_question(int(rows[0]["id"]))
                break
        return await asyncio.wait_for(task, timeout=10)

    result = asyncio.run(drive())
    assert "put the question aside" in _text(result)


def test_a_deleted_question_stops_the_wait(project):
    token = _begin(project)

    async def drive():
        task = asyncio.create_task(
            portalmcp.call(1, token, "ask", {"question": "Which color?", "wait_seconds": 30})
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            rows = db.open_questions(int(project["id"]))
            if rows:
                conn = db.get_conn()
                conn.execute("DELETE FROM questions WHERE id = ?", (int(rows[0]["id"]),))
                conn.commit()
                break
        return await asyncio.wait_for(task, timeout=10)

    assert "put the question aside" in _text(asyncio.run(drive()))


# --------------------------------------------------------------------------
# "A run is waiting on this"
# --------------------------------------------------------------------------

def test_a_question_being_waited_on_names_the_run(project):
    """What the marker is for: an answer given now changes what a running agent
    does next, and the page could not tell that apart from any other question."""
    token = _begin(project)
    seen: list = []

    async def drive():
        task = asyncio.create_task(
            portalmcp.call(1, token, "ask", {"question": "Which color?", "wait_seconds": 30})
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            rows = db.open_questions(int(project["id"]))
            if rows:
                seen.append(portalmcp.waiting_run(int(rows[0]["id"])))
                db.answer_question(int(rows[0]["id"]), "green")
                break
        await asyncio.wait_for(task, timeout=10)
        return int(rows[0]["id"])

    question_id = asyncio.run(drive())
    assert seen == [1]
    # And the moment the wait ends the claim goes with it.
    assert portalmcp.waiting_run(question_id) is None


def test_nothing_is_waiting_on_an_ordinary_question(project):
    row = db.create_question(int(project["id"]), "Filed by a report, like every other one")
    assert portalmcp.waiting_run(int(row["id"])) is None


def test_a_wait_that_times_out_stops_claiming_a_run(project):
    _ask(project, question="Should I ship it?", wait_seconds=0)
    row = db.open_questions(int(project["id"]))[0]
    assert portalmcp.waiting_run(int(row["id"])) is None


def test_a_wait_that_blows_up_stops_claiming_a_run(project, monkeypatch):
    """The `finally` earns its keep here: a run killed mid-wait must not leave
    the questions page promising an agent that is no longer running."""
    token = _begin(project)

    async def boom(question_id, wait):
        raise RuntimeError("the run died")

    monkeypatch.setattr(portalmcp, "_await_answer", boom)
    result = asyncio.run(portalmcp.call(1, token, "ask", {"question": "Which color?"}))
    assert result["isError"] is True
    row = db.open_questions(int(project["id"]))[0]
    assert portalmcp.waiting_run(int(row["id"])) is None


def test_the_card_says_so_and_links_to_the_run(client, project):
    """Driven through the module's own `_WAITING` rather than a stubbed template
    global, so what is exercised is the real `waiting_run` the page calls."""
    token = _begin(project)
    _ask(project, token=token, question="Which color should the button be?")
    row = db.open_questions(int(project["id"]))[0]

    page = client.get("/questions").text
    assert "Which color should the button be?" in page
    assert "a run is waiting on this" not in page

    portalmcp._WAITING[int(row["id"])] = 1
    try:
        page = client.get("/questions").text
    finally:
        portalmcp._WAITING.clear()
    assert "a run is waiting on this" in page
    assert 'href="/run/1"' in page

    # And it comes off the card by itself when the run stops waiting - the
    # live-refresh morph is what makes that visible without a reload.
    assert "a run is waiting on this" not in client.get("/questions").text


# --------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------

def test_a_question_already_answered_comes_straight_back(project, no_real_notifications):
    """The best outcome an ask can have: no wait at all, because he has already
    said. `db.file_question`'s dedupe window covers recently answered rows, so
    the ask lands on that row and reads its answer instead of asking again."""
    row = db.create_question(int(project["id"]), "Which color should the button be?")
    db.answer_question(int(row["id"]), "the green one")
    result = _ask(project, question="Which color should the button be?", wait_seconds=30)
    assert "already answered this" in _text(result)
    assert "the green one" in _text(result)
    # Nothing new was filed and nobody's phone went off.
    assert no_real_notifications == []


def test_a_reworded_duplicate_does_not_notify_twice(project, no_real_notifications):
    _ask(project, question="Should the button be green or blue?")
    assert len(no_real_notifications) == 1
    _ask(project, question="Should the button be green or blue?")
    assert len(no_real_notifications) == 1
    assert len(db.open_questions(int(project["id"]))) == 1


# --------------------------------------------------------------------------
# The cap
# --------------------------------------------------------------------------

DISTINCT = [
    "Which color should the button be?",
    "Should the export be CSV or JSON?",
    "Do you want the old photographs kept after an import?",
    "Is the church laptop allowed to reach this over the tunnel?",
]


def test_a_run_may_ask_three_times_and_no_more(project):
    token = _begin(project)
    for i in range(portalmcp.ASK_CAP):
        result = _ask(project, token=token, question=DISTINCT[i])
        assert result["isError"] is False
    result = _ask(project, token=token, question=DISTINCT[portalmcp.ASK_CAP])
    assert result["isError"] is True
    assert "cap" in _text(result)
    assert len(db.open_questions(int(project["id"]))) == portalmcp.ASK_CAP


def test_the_cap_counts_asks_not_questions_filed(project):
    """A run that keeps rewording one question is still interrupting somebody
    three times. If the cap only counted insertions, the dedupe would hand it an
    unlimited retry loop."""
    token = _begin(project)
    for _ in range(portalmcp.ASK_CAP):
        _ask(project, token=token, question="Should the button be green or blue?")
    result = _ask(project, token=token, question="Should the button be green or blue?")
    assert result["isError"] is True
    assert "cap" in _text(result)


def test_the_cap_is_per_run(project):
    first = _begin(project)
    for i in range(portalmcp.ASK_CAP):
        _ask(project, token=first, question=DISTINCT[i])
    second_raw = portalmcp.begin(2, int(project["id"]))
    second = json.loads(second_raw)["mcpServers"]["portal"]["args"][3]
    result = _ask(project, token=second, run=2,
                  question="A wholly separate matter about the deployment target?")
    assert result["isError"] is False


# --------------------------------------------------------------------------
# The wait
# --------------------------------------------------------------------------

@pytest.mark.parametrize("given,expected", [
    (None, portalmcp.DEFAULT_WAIT),
    ("nonsense", portalmcp.DEFAULT_WAIT),
    (-30, 0),
    (10, 10),
    (99999, portalmcp.MAX_WAIT),
])
def test_the_wait_is_clamped(given, expected):
    assert portalmcp._wait_seconds(given) == expected


def test_the_cap_stays_under_the_relays_own_timeout():
    """A wait longer than the relay's HTTP timeout would be killed from the
    wrong end, losing an answer that was about to arrive."""
    assert portalmcp.MAX_WAIT < mcpstdio.CALL_TIMEOUT


# --------------------------------------------------------------------------
# The JSON-RPC relay
# --------------------------------------------------------------------------

def _serve(lines, relay):
    out = io.StringIO()
    mcpstdio.serve(relay, io.StringIO("".join(l + "\n" for l in lines)), out)
    return [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]


class _StubRelay(mcpstdio.Relay):
    def __init__(self, tools=None, result=None, boom=False):
        super().__init__("http://127.0.0.1:1", "1", "t")
        self._tools = tools if tools is not None else [{"name": "ask"}]
        self._result = result or {"content": [{"type": "text", "text": "ok"}]}
        self.calls: list[tuple] = []
        self._boom = boom

    def tools(self):
        return self._tools

    def call(self, name, arguments):
        if self._boom:
            raise RuntimeError("boom")
        self.calls.append((name, arguments))
        return self._result


def _rpc(method, msg_id=1, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        msg["id"] = msg_id
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def test_the_handshake_is_answered_without_touching_the_network():
    """The CLI blocks its own startup on `initialize`, so a portal that is slow
    or down must not be able to hold up the first second of every run."""
    class Exploding(_StubRelay):
        def tools(self):
            raise AssertionError("initialize must not reach the portal")

    out = _serve([_rpc("initialize", params={"protocolVersion": "2025-06-18"})], Exploding())
    assert out[0]["result"]["capabilities"] == {"tools": {}}
    assert out[0]["result"]["serverInfo"]["name"] == "portal"


def test_the_handshake_echoes_the_clients_protocol_version():
    out = _serve([_rpc("initialize", params={"protocolVersion": "2099-01-01"})], _StubRelay())
    assert out[0]["result"]["protocolVersion"] == "2099-01-01"


def test_a_client_that_names_no_version_gets_ours():
    out = _serve([_rpc("initialize", params={})], _StubRelay())
    assert out[0]["result"]["protocolVersion"] == mcpstdio.PROTOCOL_VERSION


def test_a_notification_is_answered_with_silence():
    """Replying to a message with no id is a protocol violation that gets the
    server dropped - and `notifications/initialized` arrives on every startup."""
    assert _serve([_rpc("notifications/initialized", msg_id=None)], _StubRelay()) == []


def test_tools_list_relays_what_the_portal_serves():
    out = _serve([_rpc("tools/list")], _StubRelay(tools=[{"name": "ask"}]))
    assert out[0]["result"]["tools"] == [{"name": "ask"}]


def test_a_call_relays_the_name_and_arguments_through():
    relay = _StubRelay()
    _serve([_rpc("tools/call", params={"name": "ask", "arguments": {"question": "q"}})], relay)
    assert relay.calls == [("ask", {"question": "q"})]


def test_an_unknown_method_is_a_json_rpc_error_not_a_crash():
    out = _serve([_rpc("resources/list")], _StubRelay())
    assert out[0]["error"]["code"] == -32601


def test_ping_is_answered():
    out = _serve([_rpc("ping")], _StubRelay())
    assert out[0]["result"] == {}


def test_a_relay_that_raises_answers_with_an_error_and_keeps_serving():
    out = _serve(
        [_rpc("tools/call", msg_id=1, params={"name": "ask"}), _rpc("ping", msg_id=2)],
        _StubRelay(boom=True),
    )
    assert out[0]["error"]["code"] == -32603
    assert out[1]["id"] == 2  # the next message is still served


def test_junk_on_the_pipe_is_skipped_rather_than_answered():
    out = _serve(["not json at all", "", _rpc("ping", msg_id=5)], _StubRelay())
    assert len(out) == 1 and out[0]["id"] == 5


def test_an_unreachable_portal_means_no_tools_rather_than_a_broken_server():
    """The correct degradation: the run proceeds exactly as runs did before
    this existed, instead of the CLI dropping a server that errors."""
    relay = mcpstdio.Relay("http://127.0.0.1:1", "1", "t")  # nothing listens there
    assert relay.tools() == []


def test_an_unreachable_portal_tells_the_run_to_use_its_report():
    relay = mcpstdio.Relay("http://127.0.0.1:1", "1", "t")
    result = relay.call("ask", {"question": "q"})
    assert result["isError"] is True
    assert "report" in result["content"][0]["text"]


# --------------------------------------------------------------------------
# The wiring
# --------------------------------------------------------------------------

def test_the_config_reaches_the_argv():
    cmd = agent_runner.build_cmd("opus", 100, mcp_config='{"mcpServers":{}}')
    assert "--mcp-config" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}'


def test_a_run_without_one_is_spawned_exactly_as_before():
    assert "--mcp-config" not in agent_runner.build_cmd("opus", 100)


def test_the_projects_own_mcp_servers_are_not_switched_off():
    """`--strict-mcp-config` would also disable any MCP server a project's
    workspace configures, which is the project's business and not this flag's."""
    cmd = agent_runner.build_cmd("opus", 100, mcp_config="{}")
    assert "--strict-mcp-config" not in cmd


def test_the_worker_builds_one_and_survives_a_failure(project, monkeypatch):
    from app import worker

    assert worker._mcp_config(1, project, "build") is not None

    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.setattr(portalmcp, "begin", boom)
    # A broken MCP config is a log line, never a reason a project stops running.
    assert worker._mcp_config(1, project, "build") is None


def test_the_setting_is_on_the_settings_form():
    assert "mcp_tools" in settings_form.KNOWN_KEYS


def test_the_setting_is_on_the_settings_page():
    page = (Path(config.APP_ROOT) / "app" / "templates" / "settings.html").read_text()
    assert 'name="mcp_tools"' in page
    assert "mcp_tools" in page.split('name="_fields" value="')[1].split('"')[0]


# --------------------------------------------------------------------------
# The endpoints
# --------------------------------------------------------------------------

@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_the_tools_endpoint_serves_a_known_run(client, project):
    token = _begin(project)
    body = client.get(f"/mcp/tools?run=1&token={token}").json()
    assert [t["name"] for t in body["tools"]] == ["ask"]


def test_the_tools_endpoint_serves_a_stranger_nothing(client, project):
    _begin(project)
    assert client.get("/mcp/tools?run=1&token=guess").json() == {"tools": []}


def test_the_call_endpoint_files_a_question(client, project, no_real_notifications):
    token = _begin(project)
    body = client.post(
        f"/mcp/call?run=1&token={token}",
        json={"name": "ask", "arguments": {"question": "Ship it now?", "wait_seconds": 0}},
    ).json()
    assert body["isError"] is False
    assert len(db.open_questions(int(project["id"]))) == 1


def test_the_call_endpoint_files_nothing_for_a_stranger(client, project):
    _begin(project)
    body = client.post(
        "/mcp/call?run=1&token=guess",
        json={"name": "ask", "arguments": {"question": "Ship it now?", "wait_seconds": 0}},
    ).json()
    assert body["isError"] is True
    assert db.open_questions(int(project["id"])) == []


def test_junk_posted_to_the_call_endpoint_files_nothing(client, project):
    token = _begin(project)
    body = client.post(f"/mcp/call?run=1&token={token}", content=b"not json").json()
    assert body["isError"] is True
    assert db.open_questions(int(project["id"])) == []


# --------------------------------------------------------------------------
# The relay through a restart
# --------------------------------------------------------------------------
# The portal restarts itself to load an update, and a call that lands in that
# window - or an `ask` holding its connection open when the window opens - used
# to be reported as "the portal could not be reached, so nothing was filed".
# Now the relay keeps posting for `POST_RETRY_SEC` after the first transport
# failure, and a retried ask carries the wait it has left.

import http.client
import urllib.error


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(mcpstdio, "_clock", c)
    monkeypatch.setattr(mcpstdio, "_sleep", c.sleep)
    return c


def _posting(monkeypatch, script, clock=None):
    """`_post` replaced by a script: each entry is an exception to raise, a
    number of seconds the attempt takes before it dies (then it raises a
    reset), or a dict to answer with. Records every payload posted."""
    posted = []
    steps = list(script)

    def fake_post(url, payload, timeout):
        posted.append(payload["arguments"])
        step = steps.pop(0) if steps else {"content": [{"type": "text", "text": "late"}]}
        if isinstance(step, (int, float)):
            clock.now += step
            raise ConnectionResetError("reset")
        if isinstance(step, BaseException):
            raise step
        return step

    monkeypatch.setattr(mcpstdio, "_post", fake_post)
    return posted


def test_a_call_is_retried_through_a_portal_that_is_not_answering(clock, monkeypatch):
    answer = {"content": [{"type": "text", "text": "filed"}]}
    posted = _posting(monkeypatch, [ConnectionRefusedError(), ConnectionRefusedError(), answer])
    relay = mcpstdio.Relay("http://127.0.0.1:1", "7", "t")
    assert relay.call("ask", {"question": "Blue?", "wait_seconds": 30}) == answer
    assert len(posted) == 3
    assert "retry" not in posted[0]
    assert posted[1]["retry"] is True and posted[2]["retry"] is True


def test_a_call_gives_up_after_the_retry_budget(clock, monkeypatch):
    posted = _posting(monkeypatch, [ConnectionRefusedError()] * 100)
    relay = mcpstdio.Relay("http://127.0.0.1:1", "7", "t")
    started = clock.now
    result = relay.call("ask", {"question": "Blue?"})
    assert result["isError"]
    assert "could not be reached" in result["content"][0]["text"]
    # The retry interval divides the budget, so the last attempt lands exactly
    # on the deadline and an extra tick is a defect, not rounding.
    assert clock.now - started == pytest.approx(mcpstdio.POST_RETRY_SEC, abs=0.01)
    assert len(posted) > 2


def test_the_retry_budget_counts_from_the_failure_not_the_start(clock, monkeypatch):
    """An ask holds its connection open for minutes before a restart cuts it;
    that time is the person's, not the budget's."""
    answer = {"content": [{"type": "text", "text": "green"}]}
    posted = _posting(monkeypatch, [200, ConnectionRefusedError(), answer], clock)
    relay = mcpstdio.Relay("http://127.0.0.1:1", "7", "t")
    assert relay.call("ask", {"question": "Blue?", "wait_seconds": 240}) == answer
    assert len(posted) == 3


def test_a_retried_ask_carries_the_wait_it_has_left(clock, monkeypatch):
    answer = {"content": [{"type": "text", "text": "ok"}]}
    posted = _posting(monkeypatch, [60, answer], clock)
    relay = mcpstdio.Relay("http://127.0.0.1:1", "7", "t")
    relay.call("ask", {"question": "Blue?", "wait_seconds": 100})
    assert posted[0] == {"question": "Blue?", "wait_seconds": 100}
    assert posted[1]["wait_seconds"] == pytest.approx(40, abs=1) and posted[1]["retry"] is True


def test_a_retried_ask_with_no_wait_given_counts_down_from_the_default(clock, monkeypatch):
    posted = _posting(monkeypatch, [30, {"content": []}], clock)
    mcpstdio.Relay("http://127.0.0.1:1", "7", "t").call("ask", {"question": "Blue?"})
    assert posted[1]["wait_seconds"] == pytest.approx(mcpstdio.DEFAULT_WAIT - 30, abs=1)


def test_a_retried_ask_never_asks_for_a_negative_wait(clock, monkeypatch):
    posted = _posting(monkeypatch, [500, {"content": []}], clock)
    mcpstdio.Relay("http://127.0.0.1:1", "7", "t").call(
        "ask", {"question": "Blue?", "wait_seconds": "nonsense"}
    )
    assert posted[1]["wait_seconds"] == 0


def test_a_retried_call_to_another_tool_keeps_its_arguments(clock, monkeypatch):
    posted = _posting(monkeypatch, [ConnectionRefusedError(), {"content": []}])
    mcpstdio.Relay("http://127.0.0.1:1", "7", "t").call("project_context", {"slug": "x"})
    assert posted[1] == {"slug": "x", "retry": True}


def test_an_http_error_is_the_portal_answering_and_is_not_retried(clock, monkeypatch):
    err = urllib.error.HTTPError("http://x", 500, "boom", {}, None)
    posted = _posting(monkeypatch, [err, {"content": []}])
    result = mcpstdio.Relay("http://127.0.0.1:1", "7", "t").call("ask", {"question": "Blue?"})
    assert result["isError"] and len(posted) == 1


def test_a_connection_the_server_closed_mid_response_is_retried(clock, monkeypatch):
    answer = {"content": [{"type": "text", "text": "ok"}]}
    # IncompleteRead is an HTTPException and not an OSError, unlike
    # RemoteDisconnected, which is both - so this is the case that proves the
    # second entry in TRANSPORT_ERRORS earns its place.
    posted = _posting(monkeypatch, [http.client.IncompleteRead(b""), answer])
    assert mcpstdio.Relay("http://127.0.0.1:1", "7", "t").call("ask", {"question": "Blue?"}) == answer
    assert len(posted) == 2


def test_the_tool_list_is_retried_too(clock, monkeypatch):
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise ConnectionRefusedError()
        return {"tools": [{"name": "ask"}]}

    monkeypatch.setattr(mcpstdio, "_get", fake_get)
    assert mcpstdio.Relay("http://127.0.0.1:1", "7", "t").tools() == [{"name": "ask"}]
    assert len(calls) == 3


def test_the_relays_default_wait_is_the_portals():
    assert mcpstdio.DEFAULT_WAIT == portalmcp.DEFAULT_WAIT


def test_a_retried_ask_still_fits_under_the_relays_own_timeout():
    """The worst case: an ask that waits its full cap, is cut by a restart,
    and is retried at the end of the budget with no wait left."""
    assert portalmcp.MAX_WAIT + mcpstdio.POST_RETRY_SEC < mcpstdio.CALL_TIMEOUT


def test_the_relay_gives_the_portal_no_less_time_than_the_hook_relay_does():
    from app import hookrelay
    assert mcpstdio.POST_RETRY_SEC >= hookrelay.POST_RETRY_SEC


def test_an_unreadable_wait_counts_down_from_the_default_not_from_zero(clock, monkeypatch):
    """At 30 s elapsed the two readings differ (90 left against 0); the
    negative-wait test above, at 500 s, cannot tell them apart."""
    posted = _posting(monkeypatch, [30, {"content": []}], clock)
    mcpstdio.Relay("http://127.0.0.1:1", "7", "t").call(
        "ask", {"question": "Blue?", "wait_seconds": "nonsense"}
    )
    assert posted[1]["wait_seconds"] == pytest.approx(mcpstdio.DEFAULT_WAIT - 30, abs=1)

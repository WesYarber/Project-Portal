"""One project asking another a question and getting an answer (app/inquiry.py).

Wes, 2026-08-29: projects should "be able to inquire of one another ... so I
don't have to reexplain everything to them or be the middleman going between the
different agents". `app/crossproject.py` shipped the reading half of that; this
is the asking half.

These tests pin the decisions rather than the prose:

- who may ask whom is `crossproject`'s rule, not a second copy of it, and a
  project you may not read is refused in the same words as one that is not there;
- the question is on the target's record *before* the answer is attempted, so a
  failed answer still shows somebody asked;
- the answering agent is read-only and carries **no MCP config**, which is the
  only thing stopping A→B→A→B forever;
- the wait is bounded, and an answer that arrives after it is delivered to the
  asking project rather than dropped;
- the per-run cap counts, and `portalmcp.end` clears it - ids restart at 1 in
  every test, so a leaked tally is read by the next test as its own.
"""
from __future__ import annotations

import asyncio

import pytest

from app import ask, config, crossproject, db, inquiry, people, portalmcp


@pytest.fixture
def workspaces(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(config, "PROJECTS_DIR", root)
    return root


def make(title: str, slug: str, **kw) -> int:
    return int(db.create_project(title, slug=slug, stage="active", **kw)["id"])


def _kinds(project_id):
    return [(r["author"], r["kind"]) for r in db.list_journal(project_id, limit=50)]


def _bodies(project_id):
    return [r["content_md"] or "" for r in db.list_journal(project_id, limit=50)]


def _canned(text, sink=None):
    async def fake(prompt, cwd, model):
        if sink is not None:
            sink.append({"prompt": prompt, "cwd": cwd, "model": model})
        return text

    return fake


def _never(sink=None):
    """A `run_ask` that hangs, so the tool's wait is what ends the call."""

    async def fake(prompt, cwd, model):
        await asyncio.sleep(3600)
        return "never"  # pragma: no cover

    return fake


# --- who may ask whom ------------------------------------------------------


def test_a_project_you_cannot_read_cannot_be_asked(workspaces):
    her = people.add("Erin", gender="female")
    his = make("His Thing", "his-thing")
    hers = make("Her Thing", "her-thing")
    people.set_members(hers, [her])

    with pytest.raises(crossproject.Denied) as refused:
        asyncio.run(inquiry.inquire(hers, 1, "his-thing", "what colors?"))
    # The same answer as a project that is not there: telling a run "that
    # exists, but not for you" reports somebody else's project to a person who
    # is not on it.
    with pytest.raises(crossproject.Denied) as missing:
        asyncio.run(inquiry.inquire(hers, 1, "no-such-thing", "what colors?"))
    assert str(refused.value) == str(missing.value).replace("no-such-thing", "his-thing")
    assert his


def test_a_project_cannot_ask_itself(workspaces):
    mine = make("Mine", "mine")
    make("Other", "other")
    with pytest.raises(crossproject.Denied):
        asyncio.run(inquiry.inquire(mine, 1, "mine", "what am I?"))


def test_an_empty_question_is_refused(workspaces):
    mine = make("Mine", "mine")
    make("Other", "other")
    with pytest.raises(crossproject.Denied):
        asyncio.run(inquiry.inquire(mine, 1, "other", "   "))


def test_the_whole_feature_switches_off_with_cross_project_reading(workspaces):
    mine = make("Mine", "mine")
    make("Other", "other")
    db.set_setting("cross_project", "0")
    assert not inquiry.enabled()
    with pytest.raises(crossproject.Denied):
        asyncio.run(inquiry.inquire(mine, 1, "other", "anything?"))


# --- the exchange ----------------------------------------------------------


def test_the_answer_comes_back_and_both_halves_are_on_the_target(workspaces, monkeypatch):
    mine = make("Mine", "mine")
    theirs = make("Theirs", "theirs")
    monkeypatch.setattr(ask, "run_ask", _canned("Cobalt blue, from `filaments.json`."))

    out = asyncio.run(inquiry.inquire(mine, 1, "theirs", "which blue do you sell?"))

    assert "Cobalt blue" in out and "theirs" in out
    kinds = _kinds(theirs)
    assert ("agent", inquiry.QUESTION_KIND) in kinds
    assert ("agent", inquiry.ANSWER_KIND) in kinds
    bodies = "\n".join(_bodies(theirs))
    assert "which blue do you sell?" in bodies
    assert "Mine" in bodies and "`mine`" in bodies
    assert "Cobalt blue" in bodies
    # Nothing on the asking project: the run that asked has the answer in hand
    # and will say what it did with it in its own report.
    assert _kinds(mine) == []


def test_the_question_is_recorded_even_when_the_answer_fails(workspaces, monkeypatch):
    """A failed answer must still leave a record that somebody asked - otherwise
    the target's history says the exchange never happened."""
    mine = make("Mine", "mine")
    theirs = make("Theirs", "theirs")
    monkeypatch.setattr(ask, "run_ask", _canned(""))

    out = asyncio.run(inquiry.inquire(mine, 1, "theirs", "how does the lid seat?"))

    assert "could not answer" in out
    kinds = _kinds(theirs)
    assert ("agent", inquiry.QUESTION_KIND) in kinds
    assert ("agent", inquiry.ANSWER_KIND) not in kinds


def test_a_crash_in_the_answering_agent_is_not_a_crash_in_the_asking_run(
    workspaces, monkeypatch
):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")

    async def boom(prompt, cwd, model):
        raise RuntimeError("no")

    monkeypatch.setattr(ask, "run_ask", boom)
    assert "could not answer" in asyncio.run(inquiry.inquire(mine, 1, "theirs", "hi?"))


def test_a_long_question_is_cut_before_it_is_put_to_anybody(workspaces, monkeypatch):
    """A question longer than this is a briefing, and the whole of it would go
    into the answering agent's prompt AND onto the target's permanent record."""
    mine = make("Mine", "mine")
    theirs = make("Theirs", "theirs")
    seen: list[dict] = []
    monkeypatch.setattr(ask, "run_ask", _canned("ok", seen))

    asyncio.run(inquiry.inquire(mine, 1, "theirs", "q" * (inquiry.MAX_QUESTION_CHARS + 500)))

    kept = "q" * inquiry.MAX_QUESTION_CHARS
    assert kept in seen[0]["prompt"] and kept + "q" not in seen[0]["prompt"]
    body = "\n".join(_bodies(theirs))
    assert kept in body and kept + "q" not in body


def test_the_question_is_not_in_the_prompt_twice(workspaces, monkeypatch):
    """The prompt carries the target's recent journal, and the question is
    written into that journal. Built in the wrong order it arrives once in the
    tail and once under its own heading, and reads as having been asked twice."""
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    seen: list[dict] = []
    monkeypatch.setattr(ask, "run_ask", _canned("ok", seen))

    asyncio.run(inquiry.inquire(mine, 1, "theirs", "HOW-DOES-THE-LID-SEAT"))
    assert seen[0]["prompt"].count("HOW-DOES-THE-LID-SEAT") == 1


def test_a_long_answer_is_cut_rather_than_pasted_whole(workspaces, monkeypatch):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    monkeypatch.setattr(ask, "run_ask", _canned("x" * (inquiry.MAX_ANSWER_CHARS + 500)))

    out = asyncio.run(inquiry.inquire(mine, 1, "theirs", "everything?"))
    assert out.count("x") == inquiry.MAX_ANSWER_CHARS


# --- the prompt the answering agent gets -----------------------------------


def test_the_prompt_names_the_asking_project_and_stands_in_the_target(workspaces):
    mine = make("Mine", "mine", description="Renders product shots.")
    theirs = make("Theirs", "theirs", description="Sells cases.")
    db.add_journal(theirs, "agent", "progress", "Chose PETG for the shell.")

    prompt = inquiry.build_prompt(
        db.get_project(theirs), db.get_project(mine), "what material?"
    )

    assert "Mine" in prompt and "`mine`" in prompt
    assert "Renders product shots." in prompt  # why they are asking
    assert "Sells cases." in prompt  # the target's own brief
    assert "Chose PETG for the shell." in prompt  # the target's journal
    assert str(config.PROJECTS_DIR / "theirs") in prompt  # its workspace, not ours
    assert "what material?" in prompt
    assert "read-only" in prompt.lower()


def test_the_answering_agent_gets_no_mcp_tools_so_it_cannot_ask_back(
    workspaces, monkeypatch
):
    """The recursion guard is structural: the subprocess `ask.build_command`
    produces has no `--mcp-config` at all, so B has no `ask_project` to answer
    A with. A depth counter could be edited away; an absent flag cannot."""
    cmd = ask.build_command("prompt", "sonnet")
    assert "--mcp-config" not in cmd
    assert "--dangerously-skip-permissions" not in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1 : cmd.index("--disallowedTools")]
    assert "Bash" not in allowed and "Write" not in allowed and "Edit" not in allowed


def test_the_answer_is_written_in_the_target_s_own_workspace(workspaces, monkeypatch):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    seen: list[dict] = []
    monkeypatch.setattr(ask, "run_ask", _canned("ok", seen))

    asyncio.run(inquiry.inquire(mine, 1, "theirs", "where?"))
    assert seen[0]["cwd"] == config.PROJECTS_DIR / "theirs"


# --- the cap ---------------------------------------------------------------


def test_a_run_may_only_ask_so_many_times(workspaces, monkeypatch):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    monkeypatch.setattr(ask, "run_ask", _canned("ok"))

    async def scenario():
        for _ in range(inquiry.MAX_PER_RUN):
            await inquiry.inquire(mine, 42, "theirs", "again?")
        with pytest.raises(crossproject.Denied) as capped:
            await inquiry.inquire(mine, 42, "theirs", "once more?")
        return str(capped.value)

    message = asyncio.run(scenario())
    assert str(inquiry.MAX_PER_RUN) in message
    assert "project_context" in message  # points at the free way to keep going


def test_a_refused_inquiry_does_not_spend_a_slot(workspaces, monkeypatch):
    """The cap counts questions actually put to another project. A slug that
    does not resolve never reached anybody, so charging for it would let a run
    spend its whole allowance on typos."""
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    monkeypatch.setattr(ask, "run_ask", _canned("ok"))

    async def scenario():
        for _ in range(inquiry.MAX_PER_RUN + 2):
            with pytest.raises(crossproject.Denied):
                await inquiry.inquire(mine, 43, "nope", "?")
        await inquiry.inquire(mine, 43, "theirs", "real one?")

    asyncio.run(scenario())
    assert inquiry.asked_count(43) == 1


def test_ending_a_run_forgets_its_tally(workspaces, monkeypatch):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    monkeypatch.setattr(ask, "run_ask", _canned("ok"))
    portalmcp.begin(9, mine, "build")
    asyncio.run(inquiry.inquire(mine, 9, "theirs", "one?"))
    assert inquiry.asked_count(9) == 1
    portalmcp.end(9)
    assert inquiry.asked_count(9) == 0


# --- the wait, and the answer that arrives after it ------------------------


def test_a_slow_answer_hands_off_instead_of_holding_the_run(workspaces, monkeypatch):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    monkeypatch.setattr(ask, "run_ask", _never())

    async def scenario():
        out = await inquiry.inquire(mine, 1, "theirs", "slow one?", wait=0)
        for task in list(inquiry._TASKS):  # noqa: SLF001 - stop the sleeper
            task.cancel()
        return out

    out = asyncio.run(scenario())
    assert "still being written" in out
    assert "journal" in out


def test_an_answer_that_lands_late_reaches_the_asking_project(workspaces, monkeypatch):
    """The run that asked has moved on, so the answer is delivered to the
    project instead - journalled there, and a run queued to act on it."""
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    woken: list[int] = []

    async def slow(prompt, cwd, model):
        await asyncio.sleep(0)
        return "PETG, 0.4mm walls."

    async def fake_note_arrived(project):
        woken.append(int(project["id"]))
        return True

    monkeypatch.setattr(ask, "run_ask", slow)
    from app import worker

    monkeypatch.setattr(worker, "note_arrived", fake_note_arrived)

    async def scenario():
        out = await inquiry.inquire(mine, 1, "theirs", "walls?", wait=0)
        # Let the answering task, the delivery callback and the wake task run.
        for _ in range(8):
            await asyncio.sleep(0)
        return out

    out = asyncio.run(scenario())
    assert "still being written" in out
    bodies = "\n".join(_bodies(mine))
    assert "PETG, 0.4mm walls." in bodies
    assert "Theirs" in bodies
    assert ("agent", inquiry.ANSWER_KIND) in _kinds(mine)
    assert woken == [mine]


def test_a_late_failure_delivers_nothing(workspaces, monkeypatch):
    """An answer that never came is not news. Journalling "it failed" on the
    asking project would put a row in front of a person for every model hiccup."""
    mine = make("Mine", "mine")
    make("Theirs", "theirs")

    async def slow_failure(prompt, cwd, model):
        await asyncio.sleep(0)
        return ""

    monkeypatch.setattr(ask, "run_ask", slow_failure)

    async def scenario():
        await inquiry.inquire(mine, 1, "theirs", "?", wait=0)
        for _ in range(8):
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert _kinds(mine) == []


def test_a_stopped_answering_task_delivers_nothing_and_raises_nothing(
    workspaces, monkeypatch
):
    """`task.result()` on a task that was stopped raises `CancelledError`, which
    is a `BaseException` - so it would escape a done-callback into the event
    loop's exception handler rather than being swallowed by the `except` below
    it. A restart mid-answer is exactly when that happens."""
    mine = make("Mine", "mine")
    theirs = make("Theirs", "theirs")
    reported: list[dict] = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: reported.append(context)
        )
        task = asyncio.ensure_future(asyncio.sleep(3600))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        inquiry._deliver_late(mine, db.get_project(theirs), task)  # noqa: SLF001
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert reported == []
    assert _kinds(mine) == []


def test_the_wake_does_not_chase_a_project_that_has_been_deleted(
    workspaces, monkeypatch
):
    """A run is queued a whole event-loop turn after the answer is journalled,
    and a project can be deleted inside that turn."""
    mine = make("Mine", "mine")
    # Whatever it is handed, not `int(project["id"])`: a stub that unpacks its
    # argument raises on None and the raise is swallowed upstream, so the test
    # would pass with the guard deleted and prove nothing. Found by a sweep.
    woken: list = []

    async def fake_note_arrived(project):
        woken.append(project)
        return True

    from app import worker

    monkeypatch.setattr(worker, "note_arrived", fake_note_arrived)
    db.delete_project(mine)

    asyncio.run(inquiry._wake(mine))  # noqa: SLF001
    assert woken == []


# --- how a run finds out the tool exists -----------------------------------


def test_the_prompt_section_names_the_tool(workspaces):
    mine = make("Commander Case Lid", "commander-case-lid")
    make("Commander Case", "commander-case")
    section = crossproject.prompt_section(db.get_project(mine), offered=True)
    assert inquiry.TOOL_NAME in section
    # And it still steers to the free reads first.
    assert section.index("project_context") < section.index(inquiry.TOOL_NAME)


def test_the_prompt_section_leaves_the_tool_out_when_talk_is_off(workspaces):
    mine = make("Commander Case Lid", "commander-case-lid")
    make("Commander Case", "commander-case")
    db.set_setting("cross_project", "0")
    assert crossproject.prompt_section(db.get_project(mine), offered=True) == ""


def test_the_tool_is_offered_only_where_there_is_somebody_to_ask(workspaces):
    only = make("Only", "only")
    portalmcp.begin(11, only, "build")
    names = [t["name"] for t in portalmcp.tools(11, portalmcp._SCOPES[11].token)]
    assert inquiry.TOOL_NAME not in names
    portalmcp.end(11)


def test_the_tool_is_not_offered_when_cross_project_talk_is_off(workspaces):
    mine = make("Mine", "mine")
    make("Other", "other")
    db.set_setting("cross_project", "0")
    portalmcp.begin(14, mine, "build")
    names = [t["name"] for t in portalmcp.tools(14, portalmcp._SCOPES[14].token)]
    assert names == ["ask"]
    portalmcp.end(14)


# --- through the MCP server ------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_a_tool_call_puts_the_question_and_returns_the_answer(
    workspaces, monkeypatch
):
    mine = make("Mine", "mine")
    theirs = make("Theirs", "theirs")
    monkeypatch.setattr(ask, "run_ask", _canned("It uses `shell.scad`."))
    portalmcp.begin(12, mine, "build")
    token = portalmcp._SCOPES[12].token  # noqa: SLF001

    result = await portalmcp.call(
        12, token, inquiry.TOOL_NAME, {"slug": "theirs", "question": "which file?"}
    )
    portalmcp.end(12)

    assert not result["isError"]
    assert "shell.scad" in result["content"][0]["text"]
    assert ("agent", inquiry.ANSWER_KIND) in _kinds(theirs)


@pytest.mark.anyio
async def test_a_refusal_reaches_the_run_as_an_error_rather_than_a_crash(
    workspaces, monkeypatch
):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    portalmcp.begin(13, mine, "build")
    token = portalmcp._SCOPES[13].token  # noqa: SLF001

    result = await portalmcp.call(
        13, token, inquiry.TOOL_NAME, {"slug": "ghost", "question": "?"}
    )
    portalmcp.end(13)

    assert result["isError"]
    # It names what the run *can* read rather than leaving it guessing at slugs.
    assert "projects" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_an_unregistered_run_files_nothing(workspaces, monkeypatch):
    mine = make("Mine", "mine")
    theirs = make("Theirs", "theirs")
    monkeypatch.setattr(ask, "run_ask", _canned("should never run"))

    result = await portalmcp.call(
        99, "wrong-token", inquiry.TOOL_NAME, {"slug": "theirs", "question": "?"}
    )
    assert result["isError"]
    assert _kinds(theirs) == []
    assert mine

"""The portal as an MCP server for the runs it spawns: ask now, not at the end.

RESEARCH.md §2 names one primitive the portal lacked and every comparable
orchestrator converged on - *notification = decision*. Most of it has shipped:
a question reaches a phone as a push with one-tap answers, and the answer lands
back in the portal in a second. What has not shipped is the other end of that
round trip. A question could only be filed **in the report**, which is the last
thing a run writes. So a run that discovered at minute three that it needed a
decision had exactly two moves: guess, and build fifty minutes on the guess; or
stop, report the question, and wait for the *next* scheduled run to read the
answer. The answer came back in a minute and was acted on hours later.

This module closes that. The portal exposes itself to its own runs as an MCP
server (RESEARCH.md §3/§7's "portal-as-MCP-server", which Vibe Kanban had), and
the one tool on it is `ask` - file the question now, notify now, and *wait* for
the answer for a couple of minutes before carrying on. A decision that used to
cost a whole run cycle now costs about ninety seconds of wall clock.

## What earns a tool

Every tool definition is in the system prompt of every run that carries it, so
tools are not free and a tool that duplicates the report earns nothing. The
report is a good channel: it is schema-validated at the tool-call layer since
`report_schema.py`, and a run that tries to finish without one is bounced by the
Stop hook. Todos, stage moves, summaries and learnings all arrive *at the end*
and are wanted at the end.

A tool has to do something the report cannot, and two things qualify:

- **`ask`** - timing is the whole value. A question in a report is answered
  after the run is over; a question from a tool is answered while the run can
  still act on it.
- **`projects` / `project_context` / `project_files`** - the report cannot
  fetch. A run that needs to know what a *related* project already worked out
  had exactly one source for it: Wes, retyping it into a note. Those three come
  from `app/crossproject.py`, which owns the whole decision - who may read whom,
  what a project's context is, and how a file leaves another workspace.

Anything a run can usefully say at the end stays in the report where it already
works.

## The transport, and why it is a relay again

`app/mcpstdio.py` is a stdlib-only script the CLI runs as a stdio MCP server;
it speaks JSON-RPC on its pipes and forwards every call to this portal over
loopback HTTP, exactly the way `app/hookrelay.py` forwards hook payloads. Same
reasoning: the decision stays here, in one testable place with the database in
reach, and the script stays a dumb pipe that keeps working while the codebase
around it is mid-upgrade.

The per-run scope is the same shape as `hookguard`'s and for the same reason -
a token in the URL, an in-memory registry, `begin`/`end` around the spawn. It
differs in one deliberate way: **hooks fail open, this fails closed.** An
unrecognizable hook post must not brick a healthy run's remaining tool calls,
so it answers "allow". An unrecognizable `ask` would *file a question and push
a notification to somebody's phone*, so it answers with an error and files
nothing. Fail-open is right for a gate and wrong for a mutation.

## The guards, and what each one is for

- **A cap of three asks per run** (`ASK_CAP`). His attention is the scarce
  resource; a run that can ask at will is a run that can turn a build into an
  interrogation. Past the cap the tool says so and points at the report.
- **Dedupe is `db.file_question`'s, not a second copy.** Which buys something
  better than silence: if the duplicate it matches is *already answered*, the
  answer comes straight back and the run never waits at all.
- **The wait is capped at four minutes** (`MAX_WAIT`), under the CLI's own MCP
  tool timeout, so the tool always answers rather than being killed mid-call.
- **A timeout is not a failure.** The question is filed and on the project, so
  the return text says so and tells the run to carry on without it - and to
  leave it out of its report, because it is already asked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets as _secrets
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app import config, crossproject, db, inquiry, notify, people, quickreplies

log = logging.getLogger("portal.mcp")

SERVER_NAME = "portal"
ASK_CAP = 3
DEFAULT_WAIT = 120
MAX_WAIT = 240
# How often the wait re-reads the row. Two seconds is well under any human
# reaction time and costs one indexed SELECT, so the answer feels immediate
# without the portal spinning.
POLL_SEC = 2.0

# The tasks that carry the tool. `reflect` and `compact` rewrite install-wide
# memory rather than working on a decision of anyone's, so a blocking question
# from one of them would hold a maintenance run open for four minutes to ask
# something nobody is waiting to answer.
ASKING_TASKS = frozenset({"triage", "plan", "build", "research"})


@dataclass
class _Scope:
    token: str
    project_id: int
    run_id: int = 0
    asked: int = 0


# In memory, like hookguard's: after a restart there is no run left to serve,
# and a call arriving from an orphaned one is refused rather than honored.
_SCOPES: dict[int, _Scope] = {}

# Question id -> the run currently blocked on it. In memory *because the fact
# is*: "an agent is holding still for this answer" is only ever true while a
# process is alive, so a column would outlive the truth and a restart would
# leave the questions page urging somebody to answer a run that died an hour
# ago. Nothing here needs to survive a restart, and nothing should.
_WAITING: dict[int, int] = {}


def waiting_run(question_id: int) -> Optional[int]:
    """The run holding still for this question's answer, if one is.

    What the questions page shows it for: an answer given now changes what a
    running agent does next, and an answer given in an hour is read by whatever
    run comes after. Those are different asks of a person's attention, and they
    looked identical on the page until this existed."""
    return _WAITING.get(int(question_id))


def enabled() -> bool:
    return (db.get_setting("mcp_tools") or "1") != "0"


def carries_tools(task: str) -> bool:
    """Whether a run on this task is given the portal's MCP server at all.

    Public because the prompt builder needs the same answer: a prompt section
    that tells a run to use `project_context` is worse than no section at all
    when the run was never handed the tool. See `crossproject.prompt_section`.
    """
    return enabled() and task in ASKING_TASKS


def begin(run_id: int, project_id: int, task: str = "build") -> Optional[str]:
    """Register a run's scope and return the `--mcp-config` JSON that points the
    CLI at this portal. Pair with `end(run_id)`."""
    if not carries_tools(task):
        return None
    token = _secrets.token_urlsafe(16)
    _SCOPES[run_id] = _Scope(token=token, project_id=int(project_id), run_id=int(run_id))
    return json.dumps(mcp_config(run_id, token))


def end(run_id: int) -> None:
    _SCOPES.pop(run_id, None)
    # The inquiry cap is counted per run in a module global of its own, so the
    # run that is over has to be forgotten or the next run to be handed the same
    # id (which is every run, on a fresh test database) inherits its tally.
    inquiry.forget_run(run_id)


def mcp_config(run_id: int, token: str) -> dict:
    """The `--mcp-config` document: one stdio server, run by this interpreter.

    The run id and token are argv rather than environment, so they survive
    whatever the CLI does to a server's environment and are visible in a process
    listing when something needs debugging - they authorize one run's calls to
    its own project for the life of that run, and nothing else.
    """
    script = Path(config.APP_ROOT) / "app" / "mcpstdio.py"
    return {
        "mcpServers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(script), _base_url(), str(run_id), token],
            }
        }
    }


def _base_url() -> str:
    return f"http://127.0.0.1:{config.PORT}"


def _scope(run_id: int, token: str) -> Optional[_Scope]:
    scope = _SCOPES.get(int(run_id or 0))
    if scope is None or not token or scope.token != token:
        return None
    return scope


# ---------------------------------------------------------------- the tool


def tools(run_id: int, token: str) -> Optional[list[dict]]:
    """The tool list for one run, or None when the caller is not one of ours.

    Personalized: a run on a project the install owner is not on is working for
    somebody else, and a tool description telling it to "ask Wes" would point
    every mid-run decision at the wrong person - the same bug `people.principal`
    exists to fix in the contract.

    The cross-project tools are appended only when there is something to read:
    on a brand-new install with one project they would be three tool definitions
    in every system prompt that can only ever answer "there is nothing".
    """
    scope = _scope(run_id, token)
    if scope is None:
        return None
    person = people.principal(scope.project_id)
    name = people.name_of(person)
    _they, them, their, _theirs = people.pronouns_of(person)
    return _ask_tool(name, them, their) + _cross_tools(scope.project_id, name)


def _cross_tools(project_id: int, name: str) -> list[dict]:
    try:
        if not crossproject.enabled() or not crossproject.readable(project_id):
            return []
        # No second `inquiry.enabled()` check: it *is* `crossproject.enabled()`,
        # so the early return above already covers it. A sweep found that
        # deleting the extra guard changed nothing a test could see, which is
        # what an unreachable branch looks like from the outside.
        return crossproject.tool_specs(name) + [inquiry.tool_spec(name)]
    except Exception:  # noqa: BLE001 - a broken listing must not cost the run `ask`
        log.exception("Could not build the cross-project tools for %s", project_id)
        return []


def _ask_tool(name: str, them: str, their: str) -> list[dict]:
    return [
        {
            "name": "ask",
            "description": (
                f"Ask {name} a question right now and wait for the answer, instead of "
                f"parking it until your report.\n\n"
                f"Use this the moment you hit a decision only {them} can make and cannot "
                f"usefully continue without - it reaches {their} phone immediately with your "
                f"options as one-tap buttons, so an answer often comes back within a minute. "
                f"Anything you can sensibly decide yourself, decide yourself; anything you "
                f"can carry on past, put in your report instead and keep working.\n\n"
                f"Returns {name}'s answer if one arrives before the wait runs out, otherwise "
                f"a note that it is filed and waiting. Either way the question is now on the "
                f"project, so do NOT repeat it in your report's \"questions\" list.\n\n"
                f"At most {ASK_CAP} times in one run."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "The question, written to stand alone - assume it is read on a "
                            "phone with none of the rest of your work in view."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional background shown under the question.",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional one-tap answers, at most 4, a few words each, each "
                            "phrased as the answer itself (\"merge it\", \"keep both\"). "
                            "Leave out for an open-ended question."
                        ),
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "description": (
                            f"How long to wait for an answer before carrying on. "
                            f"Default {DEFAULT_WAIT}, maximum {MAX_WAIT}."
                        ),
                    },
                },
                "required": ["question"],
            },
        }
    ]


async def call(run_id: int, token: str, name: str, arguments: Any) -> dict:
    """Dispatch one `tools/call`, returning an MCP tool result."""
    scope = _scope(run_id, token)
    if scope is None:
        # Fails closed - see the module docstring. The run is told plainly
        # rather than being left to wonder why nothing reached anyone.
        return _result(
            "The portal does not recognize this run, so nothing was filed. "
            "Put the question in your report instead.",
            is_error=True,
        )
    if not isinstance(arguments, dict):
        arguments = {}
    if name in crossproject.TOOL_NAMES:
        return _cross(scope, name, arguments)
    if name == inquiry.TOOL_NAME:
        return await _inquire(scope, arguments)
    if name != "ask":
        return _result(f"No such tool: {name}", is_error=True)
    try:
        return await _ask(scope, arguments)
    except Exception:  # noqa: BLE001 - a broken tool must not kill the run
        log.exception("ask failed on run %s", run_id)
        return _result(
            "The portal could not file that question. Put it in your report instead.",
            is_error=True,
        )


def _cross(scope: _Scope, name: str, arguments: dict) -> dict:
    """One cross-project read. Uncapped, unlike `ask`, and deliberately so:
    reading costs nobody's attention, and a run that reads a related project
    twice more than it needed to has wasted only its own context."""
    try:
        return _result(crossproject.handle(scope.project_id, name, arguments))
    except crossproject.Denied as refusal:
        # A refusal is information, not a crash: the run is told which projects
        # it *can* read rather than being left to guess at slugs.
        return _result(str(refusal), is_error=True)
    except Exception:  # noqa: BLE001 - a broken tool must not kill the run
        log.exception("%s failed on run %s", name, scope.run_id)
        return _result(
            f"The portal could not answer `{name}`. Carry on without it.",
            is_error=True,
        )


async def _inquire(scope: _Scope, args: dict) -> dict:
    """One project's question put to another. Capped per run inside `inquiry`,
    which also owns who may ask whom - the same rule as who may read whom."""
    try:
        return _result(
            await inquiry.inquire(
                scope.project_id,
                scope.run_id,
                str(args.get("slug") or ""),
                str(args.get("question") or ""),
            )
        )
    except crossproject.Denied as refusal:
        return _result(str(refusal), is_error=True)
    except Exception:  # noqa: BLE001 - a broken tool must not kill the run
        log.exception("%s failed on run %s", inquiry.TOOL_NAME, scope.run_id)
        return _result(
            f"The portal could not put that question to another project. Carry on "
            f"without it, or read the project with `project_context`.",
            is_error=True,
        )


async def _ask(scope: _Scope, args: dict) -> dict:
    question = str(args.get("question") or "").strip()
    if not question:
        return _result("A question needs text.", is_error=True)
    if scope.asked >= ASK_CAP:
        return _result(
            f"You have already asked {scope.asked} questions this run, which is the cap. "
            f"Put anything else in your report's \"questions\" list and carry on.",
            is_error=True,
        )

    project = db.get_project(scope.project_id)
    if project is None:
        return _result("That project no longer exists.", is_error=True)
    person = people.principal(scope.project_id)
    name = people.name_of(person)

    options = quickreplies.derive(question, args.get("options"))
    filing = db.file_question(
        scope.project_id,
        question,
        str(args.get("context") or "").strip(),
        quick_options=quickreplies.encode(options),
    )
    row = filing.row
    # The cap counts asks, not insertions: three deduped asks are still three
    # attempts to interrupt somebody, and a run that keeps rewording the same
    # question should be stopped by the cap rather than spinning on the dedupe.
    scope.asked += 1

    if not filing.created:
        # Already being asked, in some wording. If it has an answer, that is the
        # best possible outcome of an ask - the run gets it with no wait at all.
        answered = _answer_of(row)
        if answered:
            return _result(
                f"{name} has already answered this, in these words:\n\n{answered}\n\n"
                f"Do not repeat it in your report."
            )
        log.info(
            "MCP ask on project %s deduped against question %s",
            scope.project_id, row["id"],
        )
    else:
        asyncio.create_task(
            notify.notify(
                "New question",
                question,
                question_id=row["id"],
                project_title=project["title"],
                question_slot=row["slot"],
                project_id=project["id"],
            )
        )

    wait = _wait_seconds(args.get("wait_seconds"))
    outcome, answer = await _watch(scope, int(row["id"]), wait)
    if outcome == "answered":
        return _result(f"{name} answered:\n\n{answer}\n\nDo not repeat it in your report.")
    if outcome == "skipped":
        return _result(
            f"{name} put the question aside for now rather than answering it. "
            f"Carry on without the answer, and do not repeat it in your report."
        )
    return _result(
        f"No answer in {wait}s. The question is filed on {project['title']} and waiting, "
        f"so carry on without it - do the parts that do not depend on the answer, and "
        f"leave it out of your report's \"questions\" list, since it is already asked."
    )


def _wait_seconds(raw: Any) -> int:
    try:
        wait = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WAIT
    return max(0, min(MAX_WAIT, wait))


async def _watch(scope: _Scope, question_id: int, wait: int) -> tuple[str, str]:
    """`_await_answer`, with the question marked as one a run is holding for.

    The `finally` is the point: a wait that ends any way at all - answered, put
    aside, timed out, or the run killed underneath it - must clear the mark, or
    the questions page keeps promising an agent that is not there.
    """
    if scope.run_id:
        _WAITING[question_id] = scope.run_id
    try:
        return await _await_answer(question_id, wait)
    finally:
        _WAITING.pop(question_id, None)


async def _await_answer(question_id: int, wait: int) -> tuple[str, str]:
    """Watch one question until it stops being open, or the wait runs out.

    Returns ("answered", text) / ("skipped", "") / ("waiting", ""). Polling
    rather than a condition variable on purpose: an answer can arrive from the
    web UI, a push action, the Telegram bot or another process entirely, and a
    signal every one of those would have to remember to raise is a signal one of
    them eventually forgets.
    """
    deadline = asyncio.get_event_loop().time() + max(0, wait)
    while True:
        row = db.get_question(question_id)
        if row is None:
            return "skipped", ""
        status = str(row["status"] or "")
        if status == "answered":
            return "answered", _answer_of(row) or "(no text)"
        if status in ("dismissed", "deleted"):
            return "skipped", ""
        if asyncio.get_event_loop().time() >= deadline:
            return "waiting", ""
        await asyncio.sleep(min(POLL_SEC, max(0.05, deadline - asyncio.get_event_loop().time())))


def _answer_of(row: sqlite3.Row) -> str:
    if str(row["status"] or "") != "answered":
        return ""
    return str(row["answer"] or "").strip()


def _result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}

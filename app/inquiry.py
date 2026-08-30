"""One project's run asking another project a question, and getting a real answer.

Wes, 2026-08-29:

    "Projects should be able to talk to one another when helpful or requested
    to. They should be able to inquire of one another ... so I don't have to
    reexplain everything to them or be the middleman going between the
    different agents to bridge their context."

`app/crossproject.py` shipped the *reading* half of that: a run can list the
projects it may read, pull one's brief, todo list and journal digest, and open
files in its workspace. This module is the *inquiring* half - the second verb in
his sentence, and the one that needs somebody on the other end.

## Why reading was not enough

Reading answers "what is that project and what has it been doing". It does not
answer "how does the counter configurator encode a filament color", because
nothing in that project's brief or journal says so - the answer is in six files
nobody has written a paragraph about. A run that needs it can crawl the other
workspace through `project_files` and burn a large part of its own context
window rediscovering a codebase it will never touch again, or it can ask.

So `ask_project` hands the question to an agent that starts *inside* that
project - its brief, its journal, its workspace as the working directory - and
brings back a paragraph instead of a directory tree.

## It is an ask, not a run

The answering agent is `app/ask.py`'s, unchanged in posture: `Read`/`Glob`/`Grep`
only, `Bash`/`Edit`/`Write` denied, no `--dangerously-skip-permissions`, no
`runs` row, no workspace lease. That matters three ways.

- **The fence does not move.** A run still cannot write, or shell out, anywhere
  outside its own workspace family; what crosses the line is a question and a
  paragraph of prose, decided here, in one testable place, exactly as
  `crossproject` decides who may read whom.
- **It cannot recurse.** The answering subprocess is spawned with no
  `--mcp-config` at all, so it has no `ask_project` of its own. A cannot ask B a
  question that makes B ask A a question that makes A ask B. The loop is closed
  by construction rather than by a depth counter, which is the only way to close
  it that a future edit cannot quietly reopen.
- **It costs nobody's attention.** Unlike `ask`, which interrupts a person, this
  interrupts a model. That is why the cap here is higher than `ASK_CAP` and the
  wait is longer: the scarce thing being protected is different.

## Who may ask whom

`crossproject.resolve` decides, so the rule is the one already written down: the
asking project's principal must be a member of the target, or the target must
have no members. If a run may read a project, it may ask it - and a project it
may not read is refused with the same words as one that does not exist.

## The wait, and the answer that arrives after it

An answering agent takes tens of seconds to a couple of minutes. The tool waits
`WAIT_SEC` for it, which is under the CLI's own MCP tool timeout, so the tool
always answers rather than being killed mid-call.

When the wait runs out the subprocess is *not* killed. It finishes, and its
answer is journalled onto the **asking** project and a run queued there, so the
answer reaches the project that wanted it even though the run that asked has
moved on. It is not lost even if no run can start: the journal tail is what a
run prompt is built from, so the next run on that project reads it either way.

Both sides get a journal entry for every exchange, on purpose. The question is
written on the target before the answer is attempted, so a failed answer still
leaves a record that somebody asked - and a person scrolling either project can
see what the other one was told.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Optional

from app import ask, crossproject, db

log = logging.getLogger("portal.inquiry")

# How long the tool blocks its caller. Under the CLI's own MCP tool timeout, the
# same ceiling `portalmcp.MAX_WAIT` sits below, and deliberately shorter than
# the subprocess's own timeout so a slow answer hands off rather than being cut.
WAIT_SEC = 200

# A backstop above `ask.run_ask`'s own timeout, which already bounds the part
# that can actually take time (waiting on the subprocess). The margin is what
# makes this a second line of defense rather than a duplicate of the first: at
# the same number the two would race, and whichever fired would look like the
# other one having failed.
ANSWER_TIMEOUT_SEC = ask.ASK_TIMEOUT_SEC + 20

# Inquiries one run may make. Higher than `portalmcp.ASK_CAP` (3) because what
# that cap rations is a person's attention and what this one rations is model
# time - but a cap all the same, since a run that can ask at will is a run that
# can spend its hour interviewing the rest of the board instead of building.
MAX_PER_RUN = 5

# Question and answer sizes. A question longer than this is a briefing, and an
# answer longer than this is the other project's whole context back again -
# which is what `project_context` is for.
MAX_QUESTION_CHARS = 2_000
MAX_ANSWER_CHARS = 12_000

# The journal kinds this module writes. Distinct from `ask`'s `user/ask` and
# `agent/answer` so they are not swept into the side thread (`db.SIDE_THREAD`),
# which is a person's private conversation with one project: these are between
# two projects and belong in the visible record of both.
QUESTION_KIND = "inquiry"
ANSWER_KIND = "inquiry-answer"

_INSTRUCTIONS = """\
You are answering a question that an agent working on ANOTHER project on this
portal has asked about this one. You are the side that knows this project; they
are the side that needs to know something about it.

Rules:
- Answer for THIS project, from its brief, its journal and its files. You are
  standing in its workspace - read whatever you need to answer accurately.
- You are read-only. You cannot edit, write or run anything, and you must not
  try. Do not start the asking project's work, and do not do work here either.
- Be concrete. Name files, functions, URLs, field names, exact values and
  decisions rather than describing them in the abstract - the reader is an agent
  that will act on your answer and cannot see anything you do not quote.
- If this project genuinely does not know, say so plainly in one line and say
  where the answer might actually live. A wrong confident answer is worse than
  no answer, because the asking agent has no way to check it.
- Reply with the answer itself as markdown - no preamble, no restating the
  question, no sign-off.
"""


def enabled() -> bool:
    """Off with the rest of cross-project talk. One switch, because a portal
    where projects may not read each other is not one where they may interview
    each other."""
    return crossproject.enabled()


# ------------------------------------------------------------------ prompt


def build_prompt(target: sqlite3.Row, asker: sqlite3.Row, question: str) -> str:
    """The whole prompt for one inquiry. Pure, so it can be asserted on.

    Reuses the agent's own project header, so the answering agent sees the
    target exactly as a run on it would - and then names the asking project, so
    "who wants to know and why" is part of the question rather than missing
    from it.
    """
    from app import agent_runner  # local: agent_runner imports nothing from here

    idea = " ".join((asker["description"] or asker["initial_idea"] or "").split())
    return "\n\n".join(
        [
            _INSTRUCTIONS,
            agent_runner._project_section(target),  # noqa: SLF001
            ask.journal_section(int(target["id"])),
            (
                "## Workspace\n"
                f"This project's files are your current working directory "
                f"(`{ask.workspace(target['slug'])}`). Read whatever you need."
            ),
            (
                f"## The question, from {asker['title']} (`{asker['slug']}`)\n"
                + (f"That project is: {idea}\n\n" if idea else "")
                + question
            ),
        ]
    )


# ------------------------------------------------------------------ asking


# Run id -> how many inquiries it has made. In memory because the fact is: the
# cap is about one run's behavior, and a run that is over is not asking anything.
_ASKED: dict[int, int] = {}

# Answers still being written, so the app does not lose the task to the garbage
# collector while the tool that started it has already returned.
_TASKS: set[asyncio.Task] = set()


def asked_count(run_id: int) -> int:
    return _ASKED.get(int(run_id or 0), 0)


def forget_run(run_id: int) -> None:
    _ASKED.pop(int(run_id or 0), None)


async def inquire(
    asker_id: int,
    run_id: int,
    slug: str,
    question: str,
    wait: int = WAIT_SEC,
) -> str:
    """Ask another project a question and return the answer, or where it went.

    Raises `crossproject.Denied` for a project this run may not ask, which is
    the same answer it gets for one that is not there.
    """
    if not enabled():
        raise crossproject.Denied("Cross-project talk is switched off on this portal.")
    question = (question or "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        raise crossproject.Denied("An inquiry needs a question.")

    target = crossproject.resolve(asker_id, slug)
    asker = db.get_project(int(asker_id))
    if asker is None:  # pragma: no cover - a run whose project vanished mid-call
        raise crossproject.Denied("This project no longer exists.")

    if run_id and asked_count(run_id) >= MAX_PER_RUN:
        raise crossproject.Denied(
            f"You have already asked {MAX_PER_RUN} other projects this run, which is "
            f"the cap. Read what you still need with `project_context` and "
            f"`project_files`, or put the question in your report."
        )
    if run_id:
        _ASKED[int(run_id)] = asked_count(run_id) + 1

    # Built BEFORE the question is journalled, and the order is the point: the
    # prompt carries the target's recent journal, so a question written into
    # that journal first arrives in the prompt twice - once in the tail and once
    # under its own heading - and reads as having been asked twice.
    prompt = build_prompt(target, asker, question)

    # Journalled before the answer is attempted, so an inquiry that times out or
    # falls over still leaves the target's record showing it was asked.
    db.add_journal(
        int(target["id"]),
        "agent",
        QUESTION_KIND,
        f"**{asker['title']}** (`{asker['slug']}`) asks:\n\n{question}",
    )

    task = asyncio.ensure_future(_answer(prompt, target, asker))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)

    try:
        answer = await asyncio.wait_for(asyncio.shield(task), timeout=max(0, wait))
    except asyncio.TimeoutError:
        # The task is left running rather than stopped: it finishes and
        # delivers its answer to the asking project instead.
        task.add_done_callback(lambda done: _deliver_late(int(asker["id"]), target, done))
        return (
            f"No answer from `{target['slug']}` in {wait}s - it is still being written. "
            f"Carry on without it: when it lands it is added to this project's journal "
            f"and a run queued to act on it, so it is not lost and you do not need to "
            f"ask again."
        )
    if not answer:
        return (
            f"`{target['slug']}` could not answer that one - the model call failed or "
            f"timed out. Carry on without it, or read the project yourself with "
            f"`project_context` and `project_files`."
        )
    return f"`{target['slug']}` answered:\n\n{answer}"


async def _answer(prompt: str, target: sqlite3.Row, asker: sqlite3.Row) -> str:
    """Run the read-only agent inside the target and journal what it said.

    Takes the prompt already built rather than building it, so its caller can
    fix the moment it describes - see `inquire`.

    Never raises: a broken inquiry must not take down the run that made it, and
    "" is the caller's signal that nothing came back.
    """
    try:
        text = await asyncio.wait_for(
            ask.run_ask(
                prompt,
                ask.workspace(str(target["slug"])),
                ask.ask_model(),
            ),
            timeout=ANSWER_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        log.warning("inquiry into %s timed out", target["slug"])
        return ""
    except Exception:  # noqa: BLE001 - a broken inquiry must not take the app down
        log.exception("inquiry into %s failed", target["slug"])
        return ""
    text = (text or "").strip()[:MAX_ANSWER_CHARS]
    if not text:
        return ""
    db.add_journal(
        int(target["id"]),
        "agent",
        ANSWER_KIND,
        f"Answered **{asker['title']}** (`{asker['slug']}`):\n\n{text}",
    )
    return text


def _deliver_late(asker_id: int, target: sqlite3.Row, task: asyncio.Future) -> None:
    """Route an answer that arrived after the asking run stopped waiting.

    The journal entry is the delivery; queueing a run is only how it gets acted
    on *sooner*. If no run can start - the workspace is busy, the project is
    paused, the day's budget is spent - the answer still reaches the next run
    through its journal tail, which is where a run prompt comes from.
    """
    if task.cancelled():
        return
    try:
        answer = task.result()
    except Exception:  # noqa: BLE001 - `_answer` swallows its own; nothing to deliver
        return
    if not answer:
        return
    try:
        db.add_journal(
            int(asker_id),
            "agent",
            ANSWER_KIND,
            f"**{target['title']}** (`{target['slug']}`) answered a question this "
            f"project asked, after the run that asked it had moved on:\n\n{answer}",
        )
    except Exception:  # noqa: BLE001 - best effort; the exchange is on the target too
        log.exception("could not journal a late inquiry answer on %s", asker_id)
        return
    # Held in `_TASKS` for the same reason the answer itself is: asyncio keeps
    # only a weak reference to a running task, so without this the garbage
    # collector can cancel the wake between the journal write and the run.
    wake = asyncio.ensure_future(_wake(asker_id))
    _TASKS.add(wake)
    wake.add_done_callback(_TASKS.discard)


async def _wake(asker_id: int) -> None:
    from app import worker  # local: worker imports plenty and this is a leaf

    project = db.get_project(int(asker_id))
    if project is None:
        return
    try:
        await worker.note_arrived(project)
    except Exception:  # noqa: BLE001 - the journal entry is the delivery
        log.exception("could not queue a run for a late inquiry answer on %s", asker_id)


# ------------------------------------------------------------------- tool


TOOL_NAME = "ask_project"


def tool_spec(name: str) -> dict:
    """The MCP tool definition. `name` is the run's own principal, only so the
    description can say whose re-explaining this exists to spare."""
    return {
        "name": TOOL_NAME,
        "description": (
            "Ask another project on this portal a question, and get an answer from "
            "an agent that reads that project's own brief, journal and workspace.\n\n"
            f"Use it when this project's work depends on something another one "
            f"already worked out and reading it yourself would be slow or "
            f"guesswork - how its code encodes something, why it chose an "
            f"approach, what its data actually looks like, whether it already "
            f"solved this. It is how you find out without making {name} be the "
            f"go-between.\n\n"
            "Read first: `project_context` gives you that project's brief, todos and "
            "recent journal for nothing, and `project_files` opens its files. Ask "
            "when those are not enough - an answer costs a whole model call, and the "
            "agent answering it can only tell you what is in the same workspace you "
            "can already read.\n\n"
            "The answering agent is read-only and changes nothing on that project, "
            "and it cannot ask you anything back. Whatever it says, act on it here - "
            "do NOT do that project's work in this workspace.\n\n"
            f"Takes a slug, which `projects` lists. Blocks for up to {WAIT_SEC}s; if "
            f"the answer is slower than that it is added to this project's journal "
            f"instead, so ask once and carry on either way. At most "
            f"{MAX_PER_RUN} times in one run."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "The other project's slug, e.g. `commander-case`.",
                },
                "question": {
                    "type": "string",
                    "description": (
                        "The question, written to stand alone. The agent answering it "
                        "knows its own project and nothing whatsoever about yours, so "
                        "say what you are trying to do and what you need from it - not "
                        "just the bare question."
                    ),
                },
            },
            "required": ["slug", "question"],
        },
    }


def prompt_line() -> str:
    """The sentence `crossproject.prompt_section` adds about this tool."""
    return (
        f"When reading is not enough, `{TOOL_NAME}` puts your question to an agent "
        f"that starts inside that project's own workspace and answers from it - use "
        f"it for what a brief and a file tree cannot tell you, not for what they can."
    )


__all__ = [
    "ANSWER_KIND",
    "MAX_PER_RUN",
    "QUESTION_KIND",
    "TOOL_NAME",
    "WAIT_SEC",
    "build_prompt",
    "enabled",
    "forget_run",
    "inquire",
    "prompt_line",
    "tool_spec",
]

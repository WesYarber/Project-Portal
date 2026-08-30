"""Ask a question about a project without letting anything change.

A run is expensive and consequential: it writes code, moves the project's
status, files questions and burns part of the day's budget. Sometimes the thing
Wes actually wants is smaller than that - "what did you end up choosing for the
display?", "is this thing even started?", "why is the plan structured that way?"
- the equivalent of `/btw` in Claude Code.

So an *ask* is deliberately not a run:

- It never writes a `runs` row, so it doesn't count against the daily budget,
  the parallel cap or the pacing interval. Asking a question should never
  starve the thing you're asking about.
- The subprocess gets read-only tools only (`Read`, `Glob`, `Grep`, plus web
  search for "does a library exist for this"). `Bash`, `Edit` and `Write` are
  explicitly denied, and unlike a real run it is started *without*
  `--dangerously-skip-permissions`, so anything outside the allow-list is
  refused by the CLI rather than by the prompt's good manners.
- Its answer is journalled but nothing else is applied: no status change, no
  new questions, no report.json. There is no path from an ask to a mutation.

Both halves are written to the project journal (`user/ask` then `agent/answer`)
so the exchange is on the permanent record, but neither is *shown* in the
journal feed and neither goes into a run's prompt. They are a conversation of
their own, read back by `db.ask_thread` - Wes, 2026-08-16: "For 'Asking' the
project stuff, it should maintain the context of previously asked questions. I
also want the questions to be asked and answered all up at the 'Ask' area
instead of in line in the journal."
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Optional, Sequence

from app import config, db, notify, promptbudget

log = logging.getLogger("portal.ask")

# Longer than the NL router (which is one classification) and far shorter than a
# run: this is a handful of file reads and an answer.
ASK_TIMEOUT_SEC = 240
ASK_MAX_TURNS = 20

# Read-only by construction. WebSearch/WebFetch are here because "is there an
# off-the-shelf thing for this?" is a question worth being able to ask; they
# cannot touch the workspace.
ALLOWED_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]
DENIED_TOOLS = ["Bash", "Edit", "Write", "NotebookEdit", "MultiEdit"]

# `$OWNER`/`$THEIR` are filled in from the site config, same as both agent
# contracts - see app/site.py's template_vars().
_ASK_INSTRUCTIONS_TEMPLATE = """\
You are answering a question $OWNER has asked about one of $THEIR projects. This is a
question, NOT a work request.

Rules:
- Answer the question. Do not start doing the work, and do not propose to.
- You are read-only: you have no ability to edit, write or run anything, and
  you must not try. Reading files in the workspace to answer accurately is
  expected and encouraged.
- Do NOT write .portal/report.json. Nothing you say changes the project's
  status, title, description or questions.
- If you genuinely do not know, say so plainly and say what you'd need to find
  out. A short honest answer beats a long hedged one.
- Reply with the answer itself as markdown - no preamble, no restating the
  question, no sign-off.
"""

ASK_INSTRUCTIONS = Template(_ASK_INSTRUCTIONS_TEMPLATE).safe_substitute(
    **config.SITE.template_vars()
)

# The ask thread's share of the prompt, in bytes. Sized against the run
# prompt's journal budget (24 KB, config `prompt_journal_kb`): an answer is a
# few paragraphs where a run's progress entry is a whole report, so ten
# exchanges fit comfortably inside this and a longer history degrades to
# headings the same way a journal tail does. See app/promptbudget.py.
THREAD_BUDGET_BYTES = 12_000

# How long after the last thing said in the thread the ask box still opens
# itself on a fresh page load.
#
# The box is folded to a button the rest of the time (Wes, 2026-07-21) because
# it is a thing you reach for. But the whole point of his 2026-08-16 note is
# that an answer arrives where the question was asked - and an answer arrives
# minutes later, usually while he is somewhere else. Coming back to the project
# and having to open a box to find out what it said is the same "in line in the
# journal" hunt in a different place. Half an hour covers "I asked, I went off,
# I came back"; a day later the thread is history and the button is enough.
OPEN_WITHIN = timedelta(minutes=30)


def ask_model() -> str:
    """The model that answers asks.

    Separate from `worker_model` for the same reason the Telegram router is:
    an ask is judged on how fast a straight answer comes back, not on how much
    code it can write. An unrecognized stored value falls back to the default
    rather than being handed to `claude --model`, where it would fail.
    """
    value = (db.get_setting("ask_model") or "").strip()
    return value if value in config.MODEL_VALUES else config.ASK_MODEL


def workspace(slug: str) -> Path:
    return config.PROJECTS_DIR / slug


def thread_section(rows: Sequence[sqlite3.Row]) -> str:
    """The ask conversation so far, rendered for the prompt under a budget.

    Wes, 2026-08-16: "For 'Asking' the project stuff, it should maintain the
    context of previously asked questions."

    It used to hold that only by accident: the ask prompt read the last twenty
    journal entries *including* the side thread, so on a busy project - twenty
    entries being about two days of runs here - the question he asked yesterday
    had already fallen out of the window, and a follow-up got answered as if it
    were the first thing anyone had said. The thread is its own block now, and
    it is the whole thread rather than whatever of it survived a journal tail.

    `promptbudget.journal_for_prompt` is what keeps that bounded: past the
    budget the oldest exchanges degrade to their first paragraph, the newest
    stays whole, and nothing silently disappears.
    """
    if not rows:
        return ""
    entries = [
        promptbudget.JournalEntry(
            prefix=("- You asked: " if row["author"] == "user" else "- Answer: "),
            body=row["content_md"] or "",
        )
        for row in rows
    ]
    body = promptbudget.journal_for_prompt(entries, THREAD_BUDGET_BYTES)
    return (
        "## This ask thread so far\n"
        "Earlier questions on this project and the answers they got, oldest "
        "first. A follow-up may be building on one of them - this is the "
        "conversation you are continuing, and no agent run has seen any of "
        f"it.\n{body}"
    )


def journal_section(project_id: int, limit: int = 20) -> str:
    """The project's recent journal, as a read-only agent is shown it.

    The side thread is excluded here for the same reason `agent_runner` excludes
    it, and to the opposite end: it does not belong in a journal *tail* at all.
    A run must not read it, and `build_prompt` reads it in full below instead of
    in whatever fragment of it fits in the last twenty entries.

    Shared with `app/inquiry.py`, whose answering agent wants exactly this
    picture of the project it is answering for.
    """
    journal = db.list_journal_asc(project_id, limit=limit, exclude=db.SIDE_THREAD)
    journal_txt = "\n".join(
        f"- [{row['ts']}] {row['author']}/{row['kind']}: {row['content_md']}" for row in journal
    ) or "(no journal entries yet)"
    return f"## Recent journal (last {len(journal)})\n{journal_txt}"


def build_prompt(project: sqlite3.Row, question: str) -> str:
    """The whole prompt for an ask. Pure, so it can be asserted on in tests.

    Reuses the agent's own project header (brief, description, status, build
    approval) so an ask sees exactly the same picture of the project a run
    would - just without the contract that tells it to go and do something.
    """
    from app import agent_runner  # local: agent_runner imports nothing from here

    parts = [ASK_INSTRUCTIONS, agent_runner._project_section(project)]  # noqa: SLF001
    parts.append(journal_section(int(project["id"])))

    # `start` journals the question before the answer is even attempted, so by
    # the time this prompt is built the newest entry in the thread IS the
    # question being asked. It is put at the end, under its own heading, and
    # asking it twice would read as two questions.
    history = list(db.ask_thread(project["id"]))
    if history and (history[-1]["content_md"] or "") == question:
        history = history[:-1]
    thread = thread_section(history)
    if thread:
        parts.append(thread)

    qa = db.answered_qa(project["id"])
    if qa:
        qa_txt = "\n".join(f"- Q: {row['question']}\n  A: {row['answer']}" for row in qa)
        parts.append(f"## Answered questions\n{qa_txt}")

    parts.append(
        "## Workspace\n"
        f"The project's files are your current working directory "
        f"(`{workspace(project['slug'])}`). Read whatever you need."
    )
    parts.append(f"## {config.SITE.owners} question\n{question}")
    return "\n\n".join(parts)


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{Path.home() / '.local' / 'bin'}:{env.get('PATH', '')}"
    return env


def build_command(prompt: str, model: str) -> list[str]:
    """The argv for an ask. Pure, and asserted on in tests - the read-only
    posture of this feature lives entirely in these flags."""
    return [
        "claude",
        "-p",
        prompt,
        "--model",
        config.cli_model(model),
        "--output-format",
        "json",
        "--max-turns",
        str(ASK_MAX_TURNS),
        "--allowedTools",
        *ALLOWED_TOOLS,
        "--disallowedTools",
        *DENIED_TOOLS,
    ]


async def run_ask(prompt: str, cwd: Path, model: str) -> str:
    """Run the read-only subprocess and return its answer text ("" on failure).

    Takes no workspace lease, and that is a decision rather than an oversight.
    Every agent the portal spawns that can *write* to a shared directory leases
    it (see app/worklock.py), but `build_command` denies Bash, Edit and Write,
    so this one collides with nothing - and the lease is `--nonblock`, so giving
    it one would turn "ask a question about a project that happens to be
    mid-run" into a refusal. Reading a workspace while somebody writes it is
    what a question about live work IS.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            *build_command(prompt, model),
            cwd=str(cwd),
            env=_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=ASK_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        log.warning("ask timed out after %ss", ASK_TIMEOUT_SEC)
        return ""
    except (FileNotFoundError, OSError) as exc:
        log.warning("ask could not start claude: %s", exc)
        return ""
    if proc.returncode != 0:
        log.warning("ask exited %s: %s", proc.returncode, stderr_b.decode(errors="replace")[:500])
    try:
        payload = json.loads(stdout_b.decode(errors="replace"))
    except json.JSONDecodeError:
        return ""
    return str(payload.get("result", "") or "").strip()


# Asks in flight, by project id. In-memory on purpose: a restart kills the
# subprocess, and a "thinking..." marker that outlived it would be a lie.
_PENDING: set[int] = set()


def pending(project_id: int) -> bool:
    return project_id in _PENDING


def pending_count() -> int:
    return len(_PENDING)


def opens(rows: Sequence[sqlite3.Row], is_pending: bool, now: Optional[datetime] = None) -> bool:
    """Whether the project page renders the ask box already unfolded.

    Open while a question is in flight - the reader is standing there waiting
    for it - and for `OPEN_WITHIN` after the last thing said in the thread, so
    an answer that landed while he was elsewhere is on the page when he comes
    back rather than behind a click. Folded again after that: the thread is
    then history, and the button is the whole point of the box being folded.

    A timestamp that cannot be read counts as old rather than as now. An
    unparseable `ts` is not a reason to prise the box open on every page load
    forever, which is what treating it as "recent" would do.
    """
    if is_pending:
        return True
    if not rows:
        return False
    stamp = str(rows[-1]["ts"] or "")
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - last <= OPEN_WITHIN


FAILED_ANSWER = (
    "_I couldn't answer that one - the model call failed or timed out. "
    "Try again, or ask it as a note so the next run picks it up._"
)


async def answer(project_id: int, question: str, reply_chat_id: Optional[str] = None) -> str:
    """Answer an already-journalled question and journal the reply.

    Never raises: an ask that falls over journals a plain apology rather than
    leaving the question sitting there with no response at all, which from the
    outside is indistinguishable from the portal having lost it.
    """
    _PENDING.add(project_id)
    project = db.get_project(project_id)
    if project is None:
        _PENDING.discard(project_id)
        return ""
    try:
        text = await run_ask(
            build_prompt(project, question), workspace(project["slug"]), ask_model()
        )
    except Exception:  # noqa: BLE001 - a broken ask must not take the app down
        log.exception("ask failed for project %s", project_id)
        text = ""
    finally:
        _PENDING.discard(project_id)

    body = text or FAILED_ANSWER
    db.add_journal(project_id, "agent", "answer", body)
    try:
        if reply_chat_id:
            await notify.send_telegram_text(reply_chat_id, f"{project['title']}: {body}")
        else:
            await notify.notify(f"Re: {project['title']}", body[:1500], project_id=project_id)
    except Exception:  # noqa: BLE001 - notification is best effort
        log.exception("ask notification failed")
    return body


def start(project_id: int, question: str, reply_chat_id: Optional[str] = None) -> int:
    """Journal the question now, answer it in the background.

    The question is written synchronously so the page that triggered it renders
    the question on the very next request - the answer lands minutes later. The
    returned journal id is the question's.
    """
    question = (question or "").strip()
    journal_id = db.add_journal(project_id, "user", "ask", question)
    _PENDING.add(project_id)  # before the task starts, so the redirect sees it
    task = asyncio.create_task(answer(project_id, question, reply_chat_id))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return journal_id


# asyncio keeps only a weak reference to a running task, so without this the
# garbage collector can cancel an ask mid-flight.
_TASKS: set[asyncio.Task] = set()

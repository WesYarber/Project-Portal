"""One-off agent tasks: a scratch chat session with an agent, no project.

Wes types a task; an agent runs on it in a throwaway workspace and its final
message comes back as a chat reply. He can answer, and the next run resumes
the same Claude CLI session (`--resume`), so the agent remembers the whole
exchange rather than being re-briefed from a transcript. The session lives
until Wes archives it.

This module owns the prompt and the workspace; starting runs and settling
their rows stays in app/worker.py with every other kind of run.
"""
from __future__ import annotations

from pathlib import Path
from string import Template

import sqlite3

from app import agent_runner, config, db

# The first prompt of a session carries the contract; every later prompt is
# just Wes's reply, because the resumed session already has all of this.
_ONEOFF_CONTRACT_TEMPLATE = """\
You are an agent running a ONE-OFF TASK for $OWNER - a scratch session, not a
project. There is no plan, no journal and no report file. The rules:

- Work ONLY inside your current working directory (a scratch workspace made
  for this task). Put anything you produce - scripts, files, output - in it.
- Your final printed message IS your reply to $OWNER. It is shown to $THEM on
  a chat-style page, rendered as markdown. Write it to $THEM directly: what you
  did, what you found, and anything you need from $THEM. Do NOT write
  .portal/report.json - that is for project runs, and nothing reads it here.
- This is a conversation. If you need a decision, a credential or a detail
  only $OWNER has, ask for it in your reply and stop - $THEY will answer on the same
  page, and the next run resumes THIS session with everything you know intact.
- Keep the reply honest and concrete: if something failed, say what and show
  the error rather than rounding it up to success.
- Any URL or command in your reply must use a hostname $OWNER can reach from
  another device - write `$HOST`, never `localhost` or `127.0.0.1`.
- Your reply can show things, not just say them: markdown image syntax with a
  workspace-relative path (`![result](shot.png)`) renders inline on the page,
  gifs animate, and a video file the same way becomes a player. If you made
  or checked something visual, save the image in the workspace and embed it.
"""

# Same `string.Template` substitution as the project contract - see
# agent_runner._AGENT_CONTRACT_TEMPLATE.
ONEOFF_CONTRACT = Template(_ONEOFF_CONTRACT_TEMPLATE).safe_substitute(
    **config.SITE.template_vars()
)


def workspace(task_id: int) -> Path:
    return config.TASKS_DIR / f"task-{int(task_id)}"


def _messages_block(messages: list[sqlite3.Row]) -> str:
    texts = [(m["content_md"] or "").strip() for m in messages]
    texts = [t for t in texts if t]
    if len(texts) <= 1:
        return texts[0] if texts else "(empty message)"
    # Several messages typed while no agent was looking arrive as one batch,
    # oldest first - same contract as project notes, and for the same reason:
    # a later message may correct an earlier one.
    joined = "\n\n---\n\n".join(texts)
    return (
        f"{config.SITE.owner} wrote {len(texts)} messages (oldest first - a later one may "
        f"correct an earlier one):\n\n{joined}"
    )


def build_prompt(task: sqlite3.Row, pending: list[sqlite3.Row]) -> str:
    """The prompt for the next run on this task.

    First run: the contract, the task, and the standing context project runs
    get (skills index, profile, learnings tail) - a one-off is exactly the
    kind of run that needs to know about Wes's machines and skills.
    Later runs resume the CLI session, which already holds all of that, so
    the prompt is only what is new: Wes's reply.
    """
    if not task["cli_session_id"]:
        parts = [ONEOFF_CONTRACT,
                 f"## The task, from {config.SITE.owner}\n\n{_messages_block(pending)}"]
        skills_txt = agent_runner._skills_section()  # noqa: SLF001 - same package
        if skills_txt:
            parts.append(skills_txt)
        profile = (
            config.PROFILE_MD.read_text(encoding="utf-8")
            if config.PROFILE_MD.exists()
            else "(none)"
        )
        parts.append(f"## Memory: profile.md (full)\n{profile}")
        learnings = "(none)"
        if config.LEARNINGS_MD.exists():
            lines = config.LEARNINGS_MD.read_text(encoding="utf-8").splitlines()
            learnings = "\n".join(lines[-100:])
        parts.append(f"## Memory: learnings.md (tail)\n{learnings}")
        return "\n\n".join(parts)
    return f"{config.SITE.owner} replies:\n\n{_messages_block(pending)}"


def session_lost(result: agent_runner.RunResult, resume_session: str | None) -> bool:
    """True when a resumed run failed because the CLI no longer has the
    session (pruned storage, a different HOME, an upgrade). Left alone, a
    stale id would make every future message on the task fail the same way -
    the caller clears it so the next message starts a fresh session instead."""
    if result.ok or not resume_session:
        return False
    haystack = f"{result.result_text}\n{result.raw_stderr}".lower()
    return "session" in haystack and (
        "no conversation" in haystack or "not found" in haystack or "unknown" in haystack
    )

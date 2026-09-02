"""Read a run's report back out of the Claude CLI's own transcript.

The portal learns what a run did from the CLI's stdout: the `result` event at
the end of the stream-json carries the `structured_output` the agent submitted
through the StructuredOutput tool. That pipe belongs to the portal process that
spawned the run, and the portal restarts itself several times an hour to load
its own updates. A run that outlives that process keeps working - each run is
in its own systemd scope, which a service restart does not touch - and the new
process correctly *adopts* it (`db._reconcile_orphaned_runs`), but when the
agent finishes, its report goes down a pipe nobody is reading.

Runs 1441 and 1442 on 2026-09-02 went exactly that way: both committed real
work, both filed a full report, and both were settled as "error - nothing was
watching when it finished". Wes saw two failed runs and no summary of what had
been built.

The report is not lost, though. The CLI writes every turn of a session to
`~/.claude/projects/<encoded cwd>/<session id>.jsonl` as it goes, including the
assistant's StructuredOutput tool call with the report as its `input` - the
same object the `result` event would have carried. This module finds that file
for a run and reads the report and the agent's last words out of it, so
`worker._reap_adopted` can settle an adopted run the way a watched run settles:
status ok, the summary on the run list, the journal entry on the project page.

Locating the file needs the session id, which the CLI announces in its first
stream event (`system` / `init`) and which `worker._live_logger` now records the
moment it arrives. Runs from before that change have no session id on their
row, so there is a fallback: the one transcript in the workspace's directory
that began within a short window of the run's start. One agent per workspace is
enforced by the kernel lease, so two transcripts in that window would mean the
fallback cannot tell them apart, and it declines rather than guess.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import climemory, config

log = logging.getLogger("portal.transcript")

# A transcript's first line is stamped when the CLI enqueues the prompt, a
# second or so after the portal stamped `runs.started_at`. The window is wide
# enough for a slow start (the CLI loading, the MCP server booting) and narrow
# enough that a run started after this one finished cannot fall inside it.
START_WINDOW_BEFORE = timedelta(seconds=10)
START_WINDOW_AFTER = timedelta(minutes=3)

REPORT_TOOL = "StructuredOutput"


@dataclass
class Recovered:
    path: Path
    session_id: Optional[str] = None
    # The StructuredOutput call's input, as the CLI recorded it. None when the
    # agent stopped without filing one.
    report: Optional[dict] = None
    # The agent's last words: the final text block it wrote. What a one-off
    # task shows as the reply, and the only summary a run without a report has.
    reply: str = ""
    turns: int = 0
    ended_at: Optional[str] = None
    # Lines the parser could not read, for the log. A transcript being written
    # while it is read can end on a partial line; that is not a broken file.
    unreadable_lines: int = 0
    _message_ids: set = field(default_factory=set, repr=False)


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_timestamp(path: Path) -> Optional[datetime]:
    """The stamp on the first line that carries one. Reads a handful of lines,
    not the file: a transcript can be megabytes."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    return None
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    ts = _parse_ts(event.get("timestamp"))
                    if ts is not None:
                        return ts
    except OSError:
        return None
    return None


def transcript_dir(cwd, root: Optional[Path] = None) -> Path:
    return (root or config.cli_projects_dir()) / climemory.encode_cwd(cwd)


def locate(
    cwd,
    session_id: Optional[str],
    started_at: Optional[str],
    root: Optional[Path] = None,
) -> Optional[Path]:
    """The transcript of the run that ran in `cwd`.

    With a session id this is a file name. Without one, it is the single
    transcript in that directory whose first line is stamped within the start
    window of `started_at` - and None when there are none or several, because a
    guess here would file one run's report on another run's row."""
    directory = transcript_dir(cwd, root)
    if session_id:
        path = directory / f"{session_id}.jsonl"
        return path if path.is_file() else None
    started = _parse_ts(started_at)
    if started is None or not directory.is_dir():
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    low, high = started - START_WINDOW_BEFORE, started + START_WINDOW_AFTER
    candidates: list[Path] = []
    for path in directory.glob("*.jsonl"):
        first = _first_timestamp(path)
        if first is None:
            continue
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        if low <= first <= high:
            candidates.append(path)
    if len(candidates) != 1:
        if candidates:
            log.warning(
                "%d transcripts in %s began within the start window of %s; "
                "not guessing which one is the run's",
                len(candidates), directory, started_at,
            )
        return None
    return candidates[0]


def read(path: Path) -> Recovered:
    """Everything the portal wants from a transcript, in one pass.

    Sidechain lines (a subagent's turns) are skipped: a report is the main
    agent's to file, and a subagent's last words are not the run's reply."""
    rec = Recovered(path=path)
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Could not open transcript %s: %s", path, exc)
        return rec
    with fh:
        for line in fh:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                rec.unreadable_lines += 1
                continue
            if not isinstance(event, dict):
                rec.unreadable_lines += 1
                continue
            _fold(rec, event)
    return rec


def _fold(rec: Recovered, event: dict) -> None:
    if not rec.session_id and event.get("sessionId"):
        rec.session_id = str(event["sessionId"])
    if event.get("timestamp"):
        rec.ended_at = str(event["timestamp"])
    if event.get("type") != "assistant" or event.get("isSidechain"):
        return
    message = event.get("message")
    if not isinstance(message, dict):
        return
    msg_id = message.get("id")
    if msg_id:
        if msg_id not in rec._message_ids:
            rec._message_ids.add(msg_id)
            rec.turns += 1
    else:
        rec.turns += 1
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            rec.reply = content.strip()
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = str(block.get("text") or "").strip()
            if text:
                rec.reply = text
        elif kind == "tool_use" and block.get("name") == REPORT_TOOL:
            # The last one wins, as it does on the stream: an agent that files
            # twice meant the second.
            if isinstance(block.get("input"), dict):
                rec.report = block["input"]


def recover(
    cwd,
    session_id: Optional[str],
    started_at: Optional[str],
    root: Optional[Path] = None,
) -> Optional[Recovered]:
    """`locate` then `read`; None when there is no transcript to read."""
    path = locate(cwd, session_id, started_at, root)
    if path is None:
        return None
    rec = read(path)
    if rec.unreadable_lines:
        log.info("%s: %d unreadable lines skipped", path.name, rec.unreadable_lines)
    return rec

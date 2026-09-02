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

from . import climemory, config, pricing, runlog

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
    # Token counts per model id, summed over every API call in the file - the
    # main agent's and any subagent's, since a subagent's tokens were spent by
    # this run too. One API call is one message id; the CLI writes a message
    # across several lines (one per content block) with the same `usage` on
    # each, so a message is counted once.
    usage_by_model: dict = field(default_factory=dict)
    _message_ids: set = field(default_factory=set, repr=False)

    def totals(self) -> dict[str, int]:
        """The run's token counts over every model, in app/pricing.py's names."""
        out = {key: 0 for key in pricing.USAGE_KEYS}
        for counts in self.usage_by_model.values():
            for key in pricing.USAGE_KEYS:
                out[key] += int(counts.get(key) or 0)
        return out

    def cost(self) -> Optional[float]:
        """What the run cost at list prices, or None when a model that spent
        tokens is not in the price table."""
        return pricing.estimate(self.usage_by_model)


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
    if event.get("type") != "assistant":
        return
    message = event.get("message")
    if not isinstance(message, dict):
        return
    msg_id = message.get("id")
    first_line = True
    if msg_id:
        first_line = msg_id not in rec._message_ids
        rec._message_ids.add(msg_id)
    if first_line:
        _add_usage(rec, message)
    if event.get("isSidechain"):
        return
    if first_line:
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


def _add_usage(rec: Recovered, message: dict) -> None:
    counts = pricing.totals_from_usage(
        message.get("usage") if isinstance(message.get("usage"), dict) else None
    )
    if not pricing.billable(counts):
        return
    model = str(message.get("model") or "")
    bucket = rec.usage_by_model.setdefault(model, {key: 0 for key in pricing.USAGE_KEYS})
    for key in pricing.USAGE_KEYS:
        bucket[key] += counts[key]


@dataclass
class Rendered:
    """A transcript drawn the way the portal's own log is drawn, so the run
    page can show it in the same console with the same reader."""
    path: Path
    text: str = ""
    turns: int = 0
    has_report: bool = False


RENDERED_LEAD = (
    f"{runlog.STATUS} transcript read from the agent's own session file - the "
    "portal's log of this run stops where the service restarted"
)


def render(path: Path) -> Rendered:
    """The whole run as console lines, from the CLI's transcript.

    The portal's log (`data/runs/<id>.log`) is written from the run's stdout by
    the process that started it, so for a run that outlived a restart it stops
    mid-run - the run page of a recovered run showed the first half and nothing
    after. The CLI's transcript has every turn, in the same event shape the
    live logger reads (`type`, `message.content`), so `runlog.render_event`
    draws it line for line the way the live log would have been drawn: the
    agent's words unmarked, tool calls and results dimmed, thinking marked.
    Subagent lines are skipped, as `read` skips them, and the file's own
    bookkeeping records (queue-operation, attachment, ai-title) render to
    nothing. There is no `result` event in a transcript, so the closing line is
    written here from whether a report was filed."""
    out = Rendered(path=path)
    rec = Recovered(path=path)
    lines = [RENDERED_LEAD]
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Could not open transcript %s: %s", path, exc)
        return out
    with fh:
        for line in fh:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("isSidechain"):
                continue
            _fold(rec, event)
            lines.extend(runlog.render_event(event))
    out.turns = rec.turns
    out.has_report = rec.report is not None
    if out.has_report:
        lines.append(f"{runlog.STATUS} run complete  ({rec.turns} turns, report filed)")
    else:
        lines.append(f"{runlog.STATUS} transcript ends without a report")
    out.text = "\n".join(lines)
    return out


def run_cwd(row) -> Optional[Path]:
    """Where a finished run's agent ran, which names its transcript directory.

    A parallel run leased a worktree of its own, and the transcript keeps that
    directory's name even after the worktree is merged and removed; an ordinary
    project run ran in the workspace; a one-off task in its own directory."""
    from . import db, oneoff, parallel  # noqa: PLC0415 - both import this module's users

    slug = None
    if row["project_id"] is not None:
        slug = db._row_get(row, "project_slug")  # noqa: SLF001
        if not slug:
            project = db.get_project(int(row["project_id"]))
            slug = project["slug"] if project is not None else None
    if slug:
        if db.is_parallel_run(row):
            return parallel.worktree_for(str(slug), int(row["id"]))
        return config.PROJECTS_DIR / str(slug)
    oneoff_id = db._row_get(row, "oneoff_id")  # noqa: SLF001
    if oneoff_id is not None:
        return oneoff.workspace(int(oneoff_id))
    return None


def render_for_run(row) -> Optional[Rendered]:
    """The console text for a finished run whose own log is not the whole
    story; None when there is no transcript to draw it from."""
    cwd = run_cwd(row)
    if cwd is None:
        return None
    path = locate(cwd, row["session_id"], row["started_at"])
    if path is None:
        return None
    rendered = render(path)
    return rendered if rendered.text else None


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

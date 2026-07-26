"""Turning `claude --output-format stream-json` events into a live text log.

Each run gets one append-only text file under `data/runs/<run_id>.log`. The web
UI tails it by byte offset (see `/api/run/{id}/log`), which is why the writer
only ever appends and never rewrites: an offset handed out earlier stays valid.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from app import config

# The field of a tool's input that best identifies what it is doing. Anything
# not listed falls back to the first short string value in the input dict.
TOOL_SUMMARY_KEYS: dict[str, str] = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "Task": "description",
    "Agent": "description",
    "WebFetch": "url",
    "WebSearch": "query",
    "Skill": "skill",
}

MAX_SUMMARY_CHARS = 160


def _clip(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def summarize_tool_input(name: str, tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return _clip(tool_input or "")
    key = TOOL_SUMMARY_KEYS.get(name)
    if key and tool_input.get(key):
        return _clip(tool_input[key])
    for value in tool_input.values():
        if isinstance(value, str) and value.strip():
            return _clip(value)
    return ""


def _content_blocks(event: dict) -> list[dict]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


def render_event(event: dict) -> list[str]:
    """One stream-json event -> zero or more display lines.

    Returns a list (not a string) because a single assistant turn can carry
    both prose and several tool calls, and each deserves its own line.
    """
    etype = event.get("type")

    if etype == "system" and event.get("subtype") == "init":
        model = event.get("model") or "?"
        tools = event.get("tools") or []
        return [f"* session start  model={model}  tools={len(tools)}"]

    if etype == "assistant":
        lines: list[str] = []
        for block in _content_blocks(event):
            if block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    lines.extend(f"  {ln}" for ln in text.splitlines())
            elif block.get("type") == "tool_use":
                name = block.get("name") or "tool"
                lines.append(f"> {name}({summarize_tool_input(name, block.get('input'))})")
        return lines

    if etype == "user":
        lines = []
        for block in _content_blocks(event):
            if block.get("type") != "tool_result":
                continue
            text = _result_text(block)
            count = len([ln for ln in text.splitlines() if ln.strip()])
            if block.get("is_error"):
                lines.append(f"! error: {_clip(text)}")
            else:
                lines.append(f"< ok ({count} line{'' if count == 1 else 's'})")
        return lines

    if etype == "result":
        turns = event.get("num_turns")
        cost = event.get("total_cost_usd")
        bits = []
        if turns is not None:
            bits.append(f"{turns} turns")
        if cost is not None:
            # Log lines are frozen at write time, so they can't follow the
            # display-units setting the way the templates do. They use the
            # unit-neutral weight suffix rather than claiming a dollar amount.
            bits.append(f"{cost:.3f}w")
        detail = f"  ({', '.join(bits)})" if bits else ""
        if event.get("is_error"):
            # The subtype is frequently the only "why" the CLI gives - on a
            # max-turns kill the result string is empty - so the log line
            # carries it rather than reporting an anonymous failure.
            subtype = event.get("subtype")
            why = ""
            if subtype == "error_max_turns":
                why = " - hit the turn limit"
            elif subtype and subtype != "success":
                why = f" - {subtype}"
            return [f"! run failed{why}{detail}"]
        return [f"* run complete{detail}"]

    return []


def log_path(run_id: int) -> Path:
    return config.RUNS_DIR / f"{run_id}.log"


KEEP_LOGS = 200


def prune(keep: int = KEEP_LOGS) -> int:
    """Drop all but the `keep` newest transcripts. Called when a run starts, so
    a long-lived portal doesn't accumulate log files forever. Returns how many
    were removed."""
    if not config.RUNS_DIR.exists():
        return 0
    logs = sorted(config.RUNS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for stale in logs[keep:]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            pass
    return removed


class RunLog:
    """Append-only writer for one run's live log."""

    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        self.path = log_path(run_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        prune()
        # A re-used run id (only possible if the DB was reset) must not show
        # the previous run's transcript.
        self.path.write_text("", encoding="utf-8")

    def append(self, lines: list[str]) -> None:
        if not lines:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("".join(f"{line}\n" for line in lines))


def read_log(run_id: int, offset: int = 0) -> tuple[str, int]:
    """Bytes from `offset` on, plus the new offset. Missing file -> ("", 0)."""
    path = log_path(run_id)
    if not path.exists():
        return "", 0
    size = path.stat().st_size
    if offset > size:  # log was truncated/restarted under us
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    return chunk.decode("utf-8", errors="replace"), offset + len(chunk)


def tail(run_id: int, max_lines: int = 40) -> str:
    text, _ = read_log(run_id, 0)
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def parse_line(raw: str) -> Optional[dict]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None

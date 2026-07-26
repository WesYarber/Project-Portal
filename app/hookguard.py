"""PreToolUse guardrails: every tool call a run makes is posted back to the
portal, which denies anything that touches the portal's own data or source
from a run that has no business there (RESEARCH.md §5, todo #219).

The portal spawns `claude -p` with `--dangerously-skip-permissions`, so until
OS sandboxing lands (#216 waits on bwrap) nothing stood between a misdirected
or prompt-injected run and `data/portal.db`, another project's workspace, or
the OAuth credentials every run's subscription hangs off. Claude Code hooks
close that gap at the CLI layer: a PreToolUse hook's deny blocks the tool call
even under skip-permissions (verified live against CLI 2.1.215), and hooks
supplied via `--settings` fire without any workspace opt-in.

The CLI build in use ignores URL-type hooks, so the transport is a command
hook running `app/hookrelay.py` - a stdlib-only script that POSTs the CLI's
payload to `/hooks/pre-tool` on this portal and speaks the hook protocol back.
The decision stays here, in one testable place, with a DB audit trail.

Scope is deliberately narrow, because a false deny breaks a run - worse than
the alert-fatigue the learnings warn about:

- File-tool writes (Write/Edit/NotebookEdit) are denied inside the portal's
  source tree and data dir, *except* the run's own workspace family. Family
  matters: board-games sub-projects legitimately write into their parent's
  workspace and deploy from there.
- Bash is screened for absolute paths into the data dir outside the family,
  and for `portal.db` / `.credentials.json` in any form - the two names whose
  mere mention in a foreign run's shell command is already wrong.
- Reads are only denied for `~/.claude/.credentials.json` (the documented
  workflows where any project reads `secrets/cloudflare.txt` or runs
  `deploy/screenshot.sh` from the repo stay allowed).
- The meta-project is exempt - its whole job is editing the portal - as are
  the reflect/compaction runs whose cwd *is* the memory dir. The worker
  simply doesn't install hooks for those.

Fail-open end to end: an unknown run id, a stale token, a portal restart
mid-run, a DB hiccup or any exception here resolves to "allow". The guard
exists to stop a hostile tool call, never to strand a healthy run.

The same relay also carries a PostToolUse *audit trail* (todo #219's last
hook piece): every tool call a run makes lands as one structured row in
hook_events, so a run's page can answer "what did this run actually do"
without re-reading a 2MB transcript - and the record outlives the transcript,
which is pruned to the newest 200 runs. Bounded on purpose, because Wes
distrusts unbounded anything: at most AUDIT_CAP rows per run (then one
capped marker; the transcript has the rest), and plain audit rows age out
after AUDIT_RETENTION_DAYS while denials and stop bounces are kept forever.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets as _secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app import config, db

log = logging.getLogger("portal.hookguard")

# Tools whose file_path/notebook_path/path is checked. Bash is screened by
# command text; everything else (WebFetch, Task, ...) passes untouched.
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Anchored: the CLI's matcher is a regex over the tool name, and un-anchored
# "Edit" would also rope in NotebookEdit (harmless) or a future "EditX" (not).
HOOK_MATCHER = "^(Write|Edit|MultiEdit|NotebookEdit|Read|Grep|Glob|Bash)$"

# Absolute-path tokens in a shell command: a run of non-delimiter characters
# starting at a slash. Best-effort by design - hooks are defense-in-depth on
# top of prompts, not a sandbox (that's #216).
_BASH_PATH_RE = re.compile(r"/[^\s'\"`;|&<>()\[\]]+")


def _credentials_path() -> Path:
    return (Path.home() / ".claude" / ".credentials.json").resolve()


@dataclass
class _Scope:
    token: str
    allowed: list[Path]
    # Stop-hook report nudge (the #219 definition-of-done): when set, a run
    # that tries to finish without having delivered its report is blocked once
    # at the Stop hook and told to submit it. `workspace` is where the
    # report.json fallback would live; `started` lets a stale file from a
    # previous run not count as this run's report.
    report_expected: bool = False
    workspace: Optional[Path] = None
    started: float = field(default_factory=time.time)
    stop_blocked: bool = False
    # PostToolUse audit trail: whether this run records one, and how many
    # rows it has written so far (the per-run cap lives here, in memory -
    # a restart orphans the scope and the audit simply stops, fail-open).
    audit: bool = False
    audited: int = 0


# In-memory on purpose, like agent_runner's process registry: after a service
# restart there is no supervised process left, and a hook post from an orphaned
# run fails open to "allow" rather than bricking its remaining tool calls.
_SCOPES: dict[int, _Scope] = {}


def enabled() -> bool:
    return (db.get_setting("hook_guardrails") or "1") != "0"


def stop_nudge_enabled() -> bool:
    return (db.get_setting("stop_report_nudge") or "1") != "0"


def audit_enabled() -> bool:
    return (db.get_setting("hook_audit") or "1") != "0"


def _relay_command(endpoint: str, run_id: int, token: str) -> str:
    relay = Path(config.APP_ROOT) / "app" / "hookrelay.py"
    url = f"http://127.0.0.1:{config.PORT}/hooks/{endpoint}?run={run_id}&token={token}"
    # Single-quoted so the shell never sees the & in the query string.
    return f"'{sys.executable}' '{relay}' '{url}'"


def begin(
    run_id: int,
    allowed_workspaces: list[Path],
    *,
    report_expected: bool = False,
    pre_tool: bool = True,
    audit: bool = False,
) -> Optional[str]:
    """Register a run's scope and return the `--settings` JSON that installs
    the relay hooks for it. Caller must pair with `end(run_id)`.

    `pre_tool` installs the write guardrail; `report_expected` installs the
    Stop-hook report nudge (a run that finishes without delivering its report
    is bounced once and told to submit it); `audit` installs the PostToolUse
    trail recording every tool call. The meta-project runs with the nudge and
    the audit but no write guardrail - editing the portal is its job, but it
    must still report, and its tool calls are as worth seeing as anyone's."""
    resolved: list[Path] = []
    for ws in allowed_workspaces:
        try:
            resolved.append(Path(ws).resolve())
        except OSError:
            continue
    token = _secrets.token_urlsafe(16)
    _SCOPES[run_id] = _Scope(
        token=token,
        allowed=resolved,
        report_expected=report_expected,
        workspace=resolved[0] if resolved else None,
        audit=audit,
    )
    hooks: dict = {}
    if pre_tool:
        hooks["PreToolUse"] = [
            {
                "matcher": HOOK_MATCHER,
                "hooks": [
                    {"type": "command", "command": _relay_command("pre-tool", run_id, token), "timeout": 15}
                ],
            }
        ]
    if report_expected:
        hooks["Stop"] = [
            {
                "hooks": [
                    {"type": "command", "command": _relay_command("stop", run_id, token), "timeout": 15}
                ],
            }
        ]
    if audit:
        # No matcher: the audit wants every tool call, not just the risky
        # subset the PreToolUse guardrail screens.
        hooks["PostToolUse"] = [
            {
                "hooks": [
                    {"type": "command", "command": _relay_command("post-tool", run_id, token), "timeout": 15}
                ],
            }
        ]
    if not hooks:
        _SCOPES.pop(run_id, None)
        return None
    return json.dumps({"hooks": hooks})


def end(run_id: int) -> None:
    _SCOPES.pop(run_id, None)


def family_workspaces(project) -> list[Path]:
    """The workspaces a project's run may touch: its own, its parent's and its
    children's. The board-games flow - a child game merging itself into the
    parent monorepo workspace and running the parent's deploy - is the reason
    this is a family, not a single directory."""
    own = config.PROJECTS_DIR / project["slug"]
    allowed = [own]
    try:
        pid = db.parent_id_of(project)
        if pid:
            parent = db.get_project(pid)
            if parent is not None:
                allowed.append(config.PROJECTS_DIR / parent["slug"])
        for child in db.child_projects(project["id"]):
            allowed.append(config.PROJECTS_DIR / child["slug"])
    except Exception:  # noqa: BLE001 - a family lookup bug must not block the run
        log.exception("family_workspaces failed for %s", project["slug"])
    return allowed


def decide(run_id: int, token: str, payload: dict) -> tuple[str, str]:
    """The endpoint's brain: ("allow"|"deny", reason). Every uncertain case
    allows - see the module docstring."""
    try:
        scope = _SCOPES.get(run_id)
        if scope is None or scope.token != token:
            log.warning("Hook post for unknown/stale run %s; allowing", run_id)
            return "allow", ""
        verdict = evaluate(payload, scope.allowed)
        if verdict is None:
            return "allow", ""
        reason, detail = verdict
        _record(run_id, payload, reason, detail)
        return "deny", reason
    except Exception:  # noqa: BLE001
        log.exception("hookguard.decide failed; allowing")
        return "allow", ""


# -- PostToolUse audit trail --------------------------------------------------

# Rows one run may write before the trail caps with a marker row. A 30-minute
# run makes a few hundred tool calls at most; anything past this is a runaway
# whose long tail belongs in the transcript, not the DB.
AUDIT_CAP = 400

# The keys worth showing for a tool call, in preference order - the first one
# present becomes the row's detail. Covers the built-in tools; an unknown
# tool's row still lands, just with an empty detail.
_DETAIL_KEYS = (
    "command", "file_path", "notebook_path", "path", "url",
    "pattern", "query", "prompt", "description",
)


def record_tool_use(run_id: int, token: str, payload: dict) -> None:
    """One audit row per tool call a run makes. Pure observation: nothing here
    ever influences the call (the endpoint answers the relay with no decision),
    so unlike `decide` there is no verdict to fail open to - a bad payload or
    a DB hiccup just means one unrecorded row."""
    try:
        scope = _SCOPES.get(run_id)
        if scope is None or scope.token != token or not scope.audit:
            return
        if scope.audited >= AUDIT_CAP:
            return
        scope.audited += 1
        if scope.audited >= AUDIT_CAP:
            db.add_hook_event(
                run_id, "post_tool_use", "audit", "ok",
                f"Trail capped at {AUDIT_CAP} tool calls - the run's transcript has the rest.",
                None,
            )
            return
        tool = str(payload.get("tool_name") or "?")
        decision = "error" if _tool_errored(payload.get("tool_response")) else "ok"
        db.add_hook_event(
            run_id, "post_tool_use", tool, decision, None,
            _audit_detail(payload.get("tool_input")),
        )
    except Exception:  # noqa: BLE001 - the audit is best-effort by contract
        log.exception("record_tool_use failed for run %s", run_id)


def _audit_detail(tool_input) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in _DETAIL_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return _excerpt(val)
    return ""


def _tool_errored(response) -> bool:
    """Best-effort: the PostToolUse payload's tool_response shape varies by
    tool, so only an unmistakable error marker counts."""
    if isinstance(response, dict):
        return bool(response.get("is_error") or response.get("isError"))
    if isinstance(response, list):
        return any(isinstance(b, dict) and b.get("is_error") for b in response)
    return False


# What the model is told when it tries to finish reportless. This goes to the
# agent, not to Wes - it must say exactly how to comply and then let go.
STOP_NUDGE_REASON = (
    "You are finishing without having delivered your report. Call the "
    "StructuredOutput tool now with your report JSON (summary, journal_entry_md "
    "and the rest of the contract fields). If no StructuredOutput tool is "
    "available in your session, write the same JSON to .portal/report.json in "
    "your workspace instead. Then finish."
)


def decide_stop(run_id: int, token: str, payload: dict) -> tuple[str, str]:
    """The Stop-hook endpoint's brain: ("allow"|"block", reason). Blocks at
    most once per run, and only when the run demonstrably has not delivered
    its report; every uncertain case allows (same fail-open contract as
    `decide`). A wrong block costs one extra turn; a missed one is just
    today's behaviour."""
    try:
        scope = _SCOPES.get(run_id)
        if scope is None or scope.token != token:
            return "allow", ""
        if not scope.report_expected or not stop_nudge_enabled():
            return "allow", ""
        # Belt and braces against a nudge loop: the CLI sets stop_hook_active
        # after a block, and we also remember having blocked - either alone
        # ends the nudging.
        if scope.stop_blocked or payload.get("stop_hook_active"):
            return "allow", ""
        if _report_delivered(payload, scope):
            return "allow", ""
        scope.stop_blocked = True
        _record_stop(run_id, payload)
        return "block", STOP_NUDGE_REASON
    except Exception:  # noqa: BLE001
        log.exception("hookguard.decide_stop failed; allowing")
        return "allow", ""


def _report_delivered(payload: dict, scope: _Scope) -> bool:
    """Has this run submitted its report yet - a StructuredOutput tool call in
    the session transcript, or a fresh .portal/report.json in the workspace?
    An unreadable transcript reads as "not delivered": blocking once is
    bounded and harmless, while treating it as delivered would silently kill
    the nudge the day the payload shape changes."""
    if _transcript_has_report(str(payload.get("transcript_path") or "")):
        return True
    if scope.workspace is not None:
        try:
            report = scope.workspace / ".portal" / "report.json"
            # The freshness check is what keeps a stale file from a previous
            # run in the same workspace from counting as this run's report.
            if report.stat().st_mtime >= scope.started - 1:
                return True
        except OSError:
            pass
    return False


def _transcript_has_report(transcript_path: str) -> bool:
    """True if the session transcript holds an assistant StructuredOutput
    tool_use. Structural on purpose: the word also appears as plain text in
    the prompt (the contract documents the tool), so a substring match would
    call every run reported."""
    if not transcript_path:
        return False
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "StructuredOutput" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                message = obj.get("message") if isinstance(obj, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == "StructuredOutput"
                    ):
                        return True
    except OSError:
        return False
    return False


def _record_stop(run_id: int, payload: dict) -> None:
    log.info("Stop nudge: run %s tried to finish without a report", run_id)
    try:
        db.add_hook_event(
            run_id,
            "stop",
            "Stop",
            "block",
            "Tried to finish without delivering its report; bounced once and told to submit it.",
            str(payload.get("transcript_path") or "")[:500],
        )
    except Exception:  # noqa: BLE001 - the audit row is best-effort; the block is not
        log.exception("Could not record stop nudge for run %s", run_id)


def evaluate(payload: dict, allowed: list[Path]) -> Optional[tuple[str, str]]:
    """None to allow, (reason, detail) to deny. Pure of the DB and registry."""
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    cwd = str(payload.get("cwd") or "")

    if tool == "Bash":
        return _check_bash(str(tool_input.get("command") or ""), allowed)

    target = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if not target or not isinstance(target, str):
        return None
    path = _resolve(target, cwd)
    if path is None:
        return None

    if path == _credentials_path():
        return (
            "This run may not touch ~/.claude/.credentials.json - the portal's "
            "Claude OAuth credentials are off limits to agents.",
            target,
        )

    if tool in _WRITE_TOOLS and _protected(path) and not _in_allowed(path, allowed):
        return (
            "This run may only write inside its own project workspace (and its "
            "parent/child workspaces) - not the portal's source or data. "
            f"Refused write to {path}.",
            target,
        )
    return None


def _check_bash(command: str, allowed: list[Path]) -> Optional[tuple[str, str]]:
    if ".credentials.json" in command:
        return (
            "This run may not touch ~/.claude/.credentials.json - the portal's "
            "Claude OAuth credentials are off limits to agents.",
            _excerpt(command),
        )
    if "portal.db" in command:
        return (
            "This run may not touch the portal's database (portal.db). The "
            "portal's own state is off limits to agents; report through the "
            "normal contract instead.",
            _excerpt(command),
        )
    data_dir = _data_dir()
    for raw in _BASH_PATH_RE.findall(command):
        candidate = Path(os.path.normpath(raw))
        if _under(candidate, data_dir) and not _in_allowed(candidate, allowed):
            return (
                "This run may only touch its own project workspace (and its "
                "parent/child workspaces) inside the portal data dir - "
                f"{raw} belongs to the portal or another project.",
                _excerpt(command),
            )
    return None


def _record(run_id: int, payload: dict, reason: str, detail: str) -> None:
    tool = str(payload.get("tool_name") or "?")
    log.warning("Guardrail denied %s for run %s: %s", tool, run_id, detail[:200])
    try:
        db.add_hook_event(run_id, "pre_tool_use", tool, "deny", reason, detail[:500])
    except Exception:  # noqa: BLE001 - the audit row is best-effort; the deny is not
        log.exception("Could not record hook denial for run %s", run_id)


# -- path plumbing -----------------------------------------------------------

def _data_dir() -> Path:
    try:
        return Path(config.DATA_DIR).resolve()
    except OSError:
        return Path(config.DATA_DIR)


def _protected(path: Path) -> bool:
    """Inside the portal's source tree or data dir. Two checks, not one: in
    production DATA_DIR sits inside APP_ROOT, but tests (and any future split
    layout) point them at unrelated directories."""
    roots = [_data_dir()]
    try:
        roots.append(Path(config.APP_ROOT).resolve())
    except OSError:
        pass
    return any(_under(path, root) for root in roots)


def _in_allowed(path: Path, allowed: list[Path]) -> bool:
    return any(_under(path, root) for root in allowed)


def _under(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except (ValueError, OSError):
        return False


def _resolve(target: str, cwd: str) -> Optional[Path]:
    try:
        p = Path(target).expanduser()
        if not p.is_absolute():
            p = Path(cwd or "/") / p
        # resolve() follows symlinks, so a link inside a workspace pointing at
        # portal.db is judged by where it lands, not where it sits.
        return p.resolve()
    except (OSError, ValueError):
        return None


def _excerpt(command: str) -> str:
    text = " ".join(command.split())
    return text[:200]

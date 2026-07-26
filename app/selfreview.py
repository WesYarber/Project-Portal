"""Agent self-review: a run critiques its own diff before it surfaces for Wes.

RESEARCH.md §3 found that the human review queue - not agent capacity - is the
bottleneck in every long-lived scheduled-agent setup, and that the tools which
handle it best (GitHub Copilot's coding agent especially) have the agent review
its *own* diff, iterate, and only then ping the human. The portal's review loop
is prose notes: a run flips a project to `review`, Wes gets a notification, and
he is the first pair of eyes on work that may be half-finished or may not even do
what its own report claims.

This closes that gap. When a run proposes surfacing to review, a **read-only**
critic - a separate `claude -p` that can look at the workspace but not touch it -
reads the run's committed diff against the run's own summary/journal and the
project's still-open todos, and returns a verdict: is this actually done, or are
there concrete gaps? If it is done, the project surfaces to review as normal. If
it is not, the project is held on the *active* shelf, the specific gaps are
journalled as the next run's marching orders, and Wes is not pinged - it is not
his turn yet.

Two things make this safe rather than a trap:

- **It fails open in every direction.** A missing critic, a timeout, an
  unparseable answer, an empty diff, any exception - all resolve to "surface it".
  The cost of a wrong hold is a project stuck off Wes's radar, which is far worse
  than the cost of a wrong pass (he reviews something not-quite-done, exactly
  today's behaviour), so every uncertain case surfaces.
- **It will not bounce the same work forever.** The critic is shown the recent
  journal, which already contains any prior hold note, and is told to pass work
  through rather than hold it a second time on gaps that are not being closed.
  Combined with fail-open, a project cannot get wedged behind an over-zealous
  critic.

The read-only posture lives entirely in `build_command`'s flags, mirroring
app/ask.py, and is asserted on in tests - the critic must never be able to change
the very diff it is judging.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app import config, db, orphans

log = logging.getLogger("portal.selfreview")

# The critic reads the workspace and runs git to see its own diff; it writes
# nothing. Same shape as ask.py - a read-only agent's whole safety story is these
# two flag lists, so they are asserted on in tests.
ALLOWED_TOOLS = ("Read", "Grep", "Glob", "Bash(git*)")
DENIED_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

SELF_REVIEW_MAX_TURNS = 30
SELF_REVIEW_TIMEOUT_SEC = 240

# How much of the diff to hand the critic. A huge refactor's full diff would blow
# the prompt; past this the critic gets the diffstat plus a truncation marker and
# is told to read files itself with its Read/Bash(git) tools. Characters, not
# lines, because one machine-generated line can be enormous.
MAX_DIFF_CHARS = 40_000


def enabled() -> bool:
    """Whether the self-review gate is on. Default on; a stored '0' turns it off
    cleanly (the pre-self-review behaviour: review-bound work surfaces at once)."""
    return (db.get_setting("self_review") or "1") != "0"


def review_model() -> str:
    value = (db.get_setting("self_review_model") or "").strip()
    return value if value in config.MODEL_VALUES else config.SELF_REVIEW_MODEL


def wants_review(report: Optional[dict], task: str) -> bool:
    """Did this report ask to surface for review?

    Both the current `new_stage: 'review'` and the legacy `new_status: 'review'`
    count - a contract change is executed by old-shape runs first, so both
    vocabularies must be honoured forever. A research burst never surfaces work
    for review (it writes RESEARCH.md), so it is exempt regardless of what its
    report says, matching how _apply_report already strips a burst's stage moves.
    """
    if task == "research" or not report:
        return False
    return report.get("new_stage") == "review" or report.get("new_status") == "review"


@dataclass
class Verdict:
    """A critic's judgement. `ready=True` means surface for review; `False` means
    hold on the active shelf and journal `blocking`."""

    ready: bool
    blocking: list[str] = field(default_factory=list)
    note: str = ""


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def run_diff(repo: Optional[Path], before_sha: Optional[str]) -> tuple[str, str]:
    """The run's committed work as (diffstat, patch), portal bookkeeping excluded.

    Both empty when there is no baseline, no repo, nothing committed, or git
    fails - which `build_review` reads as "nothing to judge" and passes. Only
    committed work is judged, same as proof.py: uncommitted edits are orphans.py's
    problem, and a review-bound run that committed nothing has no diff to defend.
    """
    if repo is None or not before_sha:
        return "", ""
    after = orphans._git(repo, "rev-parse", "HEAD", quiet=True)
    after_sha = (after or "").strip()
    if not after_sha or after_sha == before_sha:
        return "", ""
    rng = f"{before_sha}..{after_sha}"
    stat = orphans._git(
        repo, "diff", "--stat", rng, "--", *orphans.EXCLUDE_PATHSPEC, quiet=True
    )
    patch = orphans._git(
        repo, "diff", rng, "--", *orphans.EXCLUDE_PATHSPEC, quiet=True
    )
    return (stat or "").strip(), (patch or "").strip()


def build_prompt(
    project: db.sqlite3.Row,
    report: dict,
    diffstat: str,
    patch: str,
    recent_journal: str = "",
) -> str:
    """The critic's prompt: judge the diff against the run's own claims.

    It is handed the report's summary and journal (what the run *says* it did),
    the actual committed diff (what it *actually* did), the project's open todos
    (what "done" would mean), and the recent journal (so it can see a prior hold
    note and not re-hold the same unaddressed gap). The workspace is its cwd, so
    it can Read any file or run git for context the prompt did not include.
    """
    summary = report.get("summary") or []
    summary_txt = "\n".join(f"- {b}" for b in summary if isinstance(b, str)) or "(none given)"
    journal = (report.get("journal_entry_md") or "").strip() or "(none given)"

    open_todos = [t for t in db.list_todos(project["id"]) if not t["done"]]
    todo_txt = (
        "\n".join(f"- {t['text']}" for t in open_todos) if open_todos else "(none open)"
    )

    patch_txt, clipped = _clip(patch, MAX_DIFF_CHARS)
    if clipped:
        patch_txt += (
            "\n\n[diff truncated here - it is large. Use your Read and "
            "Bash(git ...) tools to inspect the rest of the change directly.]"
        )
    if not patch_txt:
        patch_txt = "(this run committed no code - see the summary/journal above.)"

    parts = [
        "You are reviewing another agent's completed run BEFORE it is surfaced to "
        f"{config.SITE.owner} for human review. Your job is a definition-of-done check, "
        f"not a full code review: did this run actually finish work worth {config.SITE.owners} "
        "limited review time, or is it half-done, broken, or not what it claims?",
        f"## Project\n{project['title']}\n\n{project['description'] or ''}".strip(),
        f"## What the run SAYS it did (its report)\nSummary:\n{summary_txt}\n\n"
        f"Journal entry:\n{journal}",
        f"## What the run ACTUALLY committed\nDiffstat:\n{diffstat or '(no diffstat)'}"
        f"\n\nPatch:\n```diff\n{patch_txt}\n```",
        f"## Still-open todo items on this project\n{todo_txt}",
    ]
    if recent_journal.strip():
        parts.append(
            "## Recent journal (for context)\n"
            "If a recent entry is already a self-review HOLD on this same work and "
            "the gaps it named are still not addressed, do NOT hold again - pass it "
            f"through to {config.SITE.owner} rather than bouncing it a second time.\n\n"
            f"{recent_journal.strip()}"
        )
    parts.append(
        "## Your verdict\n"
        "Hold the work back ONLY for concrete, checkable gaps that make it a waste "
        f"of {config.SITE.owners} review time right now: the report claims a feature the diff does "
        "not contain, code that plainly will not run, tests referenced but absent, "
        "or an obviously unfinished edit. Do NOT hold for style, taste, "
        "nice-to-haves, or things you would merely have done differently - when in "
        "doubt, PASS. Reply with ONLY a JSON object, no prose around it:\n"
        '{"ready": true|false, "blocking": ["specific gap", ...], '
        f'"note": "one sentence for {config.SITE.owner} or the next run"}}\n'
        f"`ready:true` surfaces it to {config.SITE.owner} now (leave `blocking` empty). "
        "`ready:false` holds it and hands `blocking` to the next run as its "
        "to-do list, so each item must be concrete and actionable."
    )
    return "\n\n".join(parts)


def build_command(prompt: str, model: str) -> list[str]:
    """The argv for a self-review. Pure, and asserted on in tests - the read-only
    posture of the critic lives entirely in these flags."""
    return [
        "claude",
        "-p",
        prompt,
        "--model",
        config.cli_model(model),
        "--output-format",
        "json",
        "--max-turns",
        str(SELF_REVIEW_MAX_TURNS),
        "--allowedTools",
        *ALLOWED_TOOLS,
        "--disallowedTools",
        *DENIED_TOOLS,
    ]


def parse_verdict(text: str) -> Verdict:
    """Parse the critic's answer into a Verdict, failing open to ready=True.

    Anything the critic could return that is not a clear, well-formed `ready:false`
    resolves to ready=True: an empty answer, junk, JSON with no `ready` key, or
    `ready:false` with no concrete blocking items. Holding work off Wes's radar is
    the expensive mistake, so only an unambiguous "not done, and here is why" holds.
    """
    raw = (text or "").strip()
    if not raw:
        return Verdict(ready=True)
    # The model may wrap the object in prose or a fenced block despite instructions;
    # take the outermost {...}.
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return Verdict(ready=True)
    try:
        obj = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return Verdict(ready=True)
    if not isinstance(obj, dict) or obj.get("ready") is not False:
        return Verdict(ready=True)
    blocking = [
        str(b).strip()
        for b in (obj.get("blocking") or [])
        if isinstance(b, str) and str(b).strip()
    ]
    note = str(obj.get("note") or "").strip()
    if not blocking:
        # "Not ready" with no actionable reason is not something the next run can
        # act on, so it surfaces rather than bouncing on a vibe.
        return Verdict(ready=True, note=note)
    return Verdict(ready=False, blocking=blocking, note=note)


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{Path.home() / '.local' / 'bin'}:{env.get('PATH', '')}"
    return env


async def run_review(prompt: str, cwd: Path, model: str) -> Verdict:
    """Run the read-only critic subprocess and return its Verdict.

    Never raises and fails open: a subprocess that will not start, times out,
    exits non-zero with no parseable output, or returns junk all yield
    ready=True. The whole point is that a broken critic can never trap a project
    off the review shelf."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *build_command(prompt, model),
            cwd=str(cwd),
            env=_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=SELF_REVIEW_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        log.warning("self-review timed out after %ss; passing", SELF_REVIEW_TIMEOUT_SEC)
        return Verdict(ready=True)
    except (FileNotFoundError, OSError) as exc:
        log.warning("self-review could not start claude: %s; passing", exc)
        return Verdict(ready=True)
    if proc.returncode != 0:
        log.warning(
            "self-review exited %s: %s",
            proc.returncode,
            stderr_b.decode(errors="replace")[:300],
        )
    try:
        payload = json.loads(stdout_b.decode(errors="replace"))
    except json.JSONDecodeError:
        return Verdict(ready=True)
    return parse_verdict(str(payload.get("result", "") or ""))


def hold_note(verdict: Verdict) -> str:
    """The system journal note left when self-review holds work back.

    Phrased as the next run's marching orders, not a scolding - it stays on the
    active shelf, so the next agent reads this and closes the gaps. It restates
    that the work was otherwise reported complete so the next run knows it is
    finishing, not starting."""
    items = "\n".join(f"- {b}" for b in verdict.blocking)
    tail = f"\n\n{verdict.note}" if verdict.note else ""
    return (
        "**Self-review held this back from review.** The run reported its work as "
        "finished, but a read-only self-review of the committed diff found gaps "
        f"that would waste {config.SITE.owners} review time. Kept on the active shelf; close these, "
        "then surface for review again:\n\n"
        f"{items}{tail}"
    )


def build_review(
    project: db.sqlite3.Row,
    report: dict,
    repo: Optional[Path],
    before_sha: Optional[str],
    recent_journal: str = "",
) -> Optional[str]:
    """Prepare the critic prompt for a review-bound run, or None to skip.

    Returns None (skip the critic, surface as normal) when the run committed no
    diff to judge - there is nothing to hold work back on, and spending a
    subprocess to confirm an empty diff is done is pure cost. Otherwise returns
    the prompt string for `run_review`.
    """
    diffstat, patch = run_diff(repo, before_sha)
    if not patch:
        return None
    return build_prompt(project, report, diffstat, patch, recent_journal)

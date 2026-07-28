"""Proof-shot obligation: a run that changes how a project *looks* should show
what it changed, not only describe it.

RESEARCH.md §3 found that every mature agent-report tool converged on the same
discipline - a UI-touching run attaches a screenshot or a short capture, and a
report that merely *says* "the layout is fixed now" is worth far less than one
that shows it. Wes's own instinct is identical: he tests by looking, and he
asked for journal images to render inline and open in a zoomable lightbox so he
can actually see them without leaving the page.

This module is the backstop for that discipline. It cannot force a screenshot
into a report that has already been produced, so it does the honest thing:
when a run *committed* a change to a recognized front-end file but embedded no
image or video in its journal entry, it leaves a visible system note. That note
tells Wes the run shipped UI blind, and - because the recent journal is pasted
into every subsequent run's prompt - it nudges the next agent on this project to
prove its work. The loop is: the contract asks for a screenshot, this notices
when one was skipped, the next run reads the notice.

Deliberately conservative. It speaks only when a front-end file actually changed
between the run's starting commit and its finishing commit, so a run that
touched only backend code, docs or tests is silent. A nag that fires on almost
every run stops being read - the exact failure this is meant to prevent on the
reporting side (see EXCLUDE_PATHSPEC's story in orphans.py).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from app import config, memory, orphans

log = logging.getLogger("portal.proof")

# Front-end source file types. A change to one of these is "the look changed" for
# the purposes of the obligation. Kept to genuine browser-facing sources: the
# portal's own UI lives in app/static/*.css, *.js and app/templates/*.html, and
# Wes's other projects are Bun/TypeScript front-ends and plain HTML, so this set
# covers both. Notably absent: .py and .md - a run that only rewrote a FastAPI
# handler or a doc did not change how anything looks in a way a screenshot would
# capture, and nagging it would be noise.
UI_EXTENSIONS = frozenset(
    {
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".vue",
        ".svelte",
    }
)

# How many changed front-end files to name in the note before summarizing the
# rest as a count - same reasoning as orphans.MAX_NAMED_FILES.
MAX_NAMED_FILES = 8

# A markdown media embed: an image, and (because the journal renders a video or
# audio file behind the same syntax) any `![alt](path)`. This is exactly the
# shape the agent contract tells runs to use - `![the new layout](shots/x.png)`
# - so matching it is matching "did the report show something".
_MEDIA_EMBED_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")


def _is_ui_path(path: str) -> bool:
    return Path(path).suffix.lower() in UI_EXTENSIONS


def head_sha(repo: Optional[Path]) -> Optional[str]:
    """The repo's current HEAD, or None if there is no repo or no commit yet.

    Captured before a run so `changed_ui_files` can diff against it afterwards.
    A freshly-`git init`ed workspace with no commits resolves to None here, which
    is correct: there is no baseline to diff against, so the run's first commit
    is not judged for proof - a project's very first run is rarely a UI polish
    pass, and false-positive avoidance wins ties.
    """
    if repo is None or not (repo / ".git").exists():
        return None
    out = orphans._git(repo, "rev-parse", "HEAD", quiet=True)
    if not out:
        return None
    sha = out.strip()
    return sha or None


def changed_ui_files(repo: Optional[Path], before_sha: Optional[str]) -> list[str]:
    """Front-end files committed between `before_sha` and HEAD, sorted.

    Empty when there is no baseline, no repo, the diff fails, or nothing
    front-end changed. Only *committed* work is seen: a run that left its edits
    uncommitted is handled by orphans.py, not nagged here - it has a bigger
    problem than a missing screenshot.
    """
    if repo is None or not before_sha:
        return []
    after_sha = head_sha(repo)
    if not after_sha or after_sha == before_sha:
        return []  # nothing committed this run, or HEAD did not move
    out = orphans._git(
        repo,
        "diff",
        "--name-only",
        f"{before_sha}..{after_sha}",
        "--",
        *orphans.EXCLUDE_PATHSPEC,
        quiet=True,
    )
    if not out:
        return []
    names = {line.strip() for line in out.splitlines() if line.strip()}
    return sorted(name for name in names if _is_ui_path(name))


def report_has_proof(report: Optional[dict]) -> bool:
    """True if the report shows something - a media embed in the journal entry or
    in any summary bullet.

    Both are checked because a run occasionally puts the screenshot only in a
    summary bullet (`![](shots/x.png)` renders there too). A bare `preview_url`
    is deliberately *not* counted as proof: a live link is where to go look, not
    a picture of what changed, and it frequently persists unchanged across runs -
    counting it would silence the nag on every run of any project that serves a
    page.
    """
    if not report:
        return False
    if _MEDIA_EMBED_RE.search(report.get("journal_entry_md") or ""):
        return True
    for bullet in report.get("summary") or []:
        if isinstance(bullet, str) and _MEDIA_EMBED_RE.search(bullet):
            return True
    return False


# Words in a skill's name or one-line description that mean "this skill can put
# a page on a screen and photograph it". Deliberately loose: naming the wrong
# skill costs a moment's confusion, while naming none at all costs the nudge
# most of its force - the standing lesson here is that agents only reach for a
# capability the prompt actually names.
_RENDER_SKILL_HINTS = ("render", "screenshot", "browser", "chromium", "headless")


def render_skill_hint() -> str:
    """A parenthetical naming a skill that could take the screenshot, if any.

    Discovered rather than hard-coded, because *which* machine or tool can
    render a page is a fact about the installation, not about the portal. An
    install with no such skill gets the general form, which is still true.
    """
    names: list[str] = []
    for root in (config.SKILLS_DIR, memory.promoted_skills_dir()):
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            haystack = f"{entry.name} {memory.skill_description(skill_md)}".lower()
            if any(hint in haystack for hint in _RENDER_SKILL_HINTS):
                names.append(entry.name)
    if not names:
        return "the workspace has no browser, so use a skill or machine that can render a page"
    listed = " or ".join(f"the **{name}** skill" for name in names[:2])
    return f"the workspace has no browser - use {listed}"


def missing_proof_note(files: list[str]) -> str:
    """The markdown system note for a UI-touching run that showed no proof.

    Phrased as a remark, not an accusation - the run may have been unable to
    render (a workspace with no browser, the render machine busy). It names what
    changed so the next run knows exactly what to screenshot, and restates the
    how, because the recent journal is the only place the next agent will read
    this.
    """
    named = files[:MAX_NAMED_FILES]
    listed = "\n".join(f"- `{name}`" for name in named)
    extra = len(files) - len(named)
    if extra > 0:
        listed += f"\n- ...and {extra} more"
    count = f"{len(files)} front-end file{'s' if len(files) != 1 else ''}"
    return (
        f"**note: this run changed {count} but its report showed no screenshot.** "
        f"{config.SITE.owner} tests by looking, so a UI change is worth much more when the journal "
        "shows it: a run that touches the look of a project should render the page "
        f"({render_skill_hint()}) and embed the "
        "result with `![what changed](shots/whatever.png)`, which renders inline "
        "and opens in a zoomable lightbox. Changed here:\n\n"
        f"{listed}"
    )

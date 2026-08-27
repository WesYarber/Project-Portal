"""Reading a run's diff on a phone, and turning a line of it into the next prompt.

RESEARCH.md §3 lists "diff review as the steering mechanism" as a thing every
comparable orchestrator has and the portal does not: in Vibe Kanban, Cursor and
GitHub's coding agent you comment on a line of the agent's diff and the comment
becomes the next run's instruction. The portal's review loop was prose - Wes
read a summary, then typed a note about it from memory, without ever seeing the
code. `app/revert.py` gave him a button to undo a run; this gives him the thing
he needs *before* he decides whether to press it.

Three decisions are load-bearing here.

**The quoted line comes from the server, never from the form.** A comment posts
a path and a line index; this module re-reads the diff and pulls the text
itself. The alternative - shipping the line back in a hidden field - would let
whatever posts the form dictate what the journal says an agent wrote, and the
journal is the record a later run believes. It also gets validation for free:
an index that is not in the diff is refused rather than quoted as empty.

**The diff of `before..after` is immutable, so an index is a stable address.**
Both ends are recorded SHAs, so the same two commits produce the same diff
bytes forever. If history is rewritten under it the commits stop existing and
`landed()` already refuses, which is the case that would otherwise silently
shift every line number by one.

**Size is decided before the diff is fetched, not while rendering it.**
`--numstat` is one line per file and costs nothing, so the totals are known
first. A run that vendored a library or committed a build directory is reported
as a file list with counts and no body, rather than being fetched into memory
and truncated afterwards. That ordering is the whole cap: a 200 MB diff is
never read at all.

Binary files are listed with their counts and carry no lines, because git's own
numstat reports `-` for both sides - there is nothing to show and nothing to
comment on.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app import orphans

log = logging.getLogger("portal.rundiff")

# Same bound and the same reason as revert/orphans: git can hang on a repo whose
# index was left locked by a run that died badly.
GIT_TIMEOUT_S = 30

# Above this many changed lines across the whole run, the body is not fetched.
# A normal run of this portal commits a few hundred; the runs that blow past
# this are the ones that added a vendored dependency or a build directory, where
# a line-by-line review is not what anybody wants anyway.
MAX_TOTAL_LINES = 6000

# Files listed with a body. Beyond this they are still named with their counts -
# knowing a run touched 60 files matters even when only the first 40 are shown.
MAX_FILES = 40

# Rendered lines per file, hunk headers included. A single file over this is
# nearly always generated or moved wholesale, and the point of the view is to
# read what changed, not to scroll a phone through 4000 lines.
MAX_LINES_PER_FILE = 400

# Every line of every file the page renders, so a diff of 40 files each just
# under the per-file cap cannot still produce a 16 000-row page. Each row
# carries a radio input as well as its text (see `_run_diff.html`), so this is
# a page-weight cap on the phone as much as a readability one.
MAX_RENDERED_LINES = 2500

# A diff smaller than this opens unfolded: a run that changed thirty lines
# should be readable without tapping anything, and everything larger stays
# folded per file so the page opens on a list rather than a wall.
OPEN_UNDER_LINES = 120

_KIND_PREFIX = {"ctx": " ", "add": "+", "del": "-"}


@dataclass(frozen=True)
class Line:
    """One row of the rendered diff.

    `kind` is "hunk" for an `@@` header (never commentable - it addresses no
    code), and "ctx"/"add"/"del" for the three kinds of body line. `old` and
    `new` are the line numbers on each side, either of which is None where that
    side has no such line.
    """

    kind: str
    text: str
    old: Optional[int] = None
    new: Optional[int] = None

    @property
    def commentable(self) -> bool:
        return self.kind in _KIND_PREFIX

    @property
    def marker(self) -> str:
        return _KIND_PREFIX.get(self.kind, "")

    @property
    def anchor(self) -> str:
        """How this line is named in a comment: the new-side number when it has
        one, the old-side number for a deletion. That is the number a person
        reading the file afterwards can actually find."""
        if self.new is not None:
            return str(self.new)
        if self.old is not None:
            return f"{self.old} (removed)"
        return ""


@dataclass
class FileDiff:
    path: str
    insertions: int = 0
    deletions: int = 0
    binary: bool = False
    old_path: str = ""
    lines: list[Line] = field(default_factory=list)
    truncated: int = 0

    @property
    def renamed(self) -> bool:
        return bool(self.old_path) and self.old_path != self.path

    @property
    def churn(self) -> int:
        return self.insertions + self.deletions

    @property
    def slug(self) -> str:
        """A DOM-safe id for this file's fold, so a comment form can be linked to."""
        return "".join(c if c.isalnum() else "-" for c in self.path).strip("-")


@dataclass
class RunDiff:
    files: list[FileDiff] = field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    too_big: bool = False
    unlisted: int = 0
    note: str = ""

    @property
    def changed_files(self) -> int:
        return len(self.files) + self.unlisted

    @property
    def any_body(self) -> bool:
        return any(f.lines for f in self.files)

    @property
    def rendered_lines(self) -> int:
        return sum(len(f.lines) for f in self.files)

    @property
    def open_by_default(self) -> bool:
        return 0 < self.rendered_lines <= OPEN_UNDER_LINES


def _git(repo: Path, *args: str) -> Optional[str]:
    """stdout, or None if git failed. Read-only throughout, so a failure means
    "cannot show the diff" and never "the diff is empty" - the two are rendered
    differently on purpose."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git %s in %s failed: %s", args[0] if args else "?", repo, exc)
        return None
    if proc.returncode != 0:
        log.warning("git %s in %s: %s", args[0] if args else "?", repo, proc.stderr[:200])
        return None
    return proc.stdout


def _numstat(repo: Path, before: str, after: str) -> Optional[list[FileDiff]]:
    """One entry per changed file, with authoritative paths.

    `-z` is what makes the paths trustworthy: without it git C-quotes anything
    with a space, a quote or a non-ASCII byte in it, and every consumer has to
    unquote identically or silently disagree about which file is which. With
    `-z` the records are NUL-separated raw bytes, and a rename arrives as three
    fields (counts, old, new) rather than the `{a => b}` arrow form.
    """
    out = _git(repo, "diff", "--numstat", "-M", "-z", f"{before}..{after}")
    if out is None:
        return None
    fields = out.split("\0")
    files: list[FileDiff] = []
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if not record.strip():
            continue
        adds, _, rest = record.partition("\t")
        dels, _, path = rest.partition("\t")
        old_path = ""
        if path == "":
            # Rename or copy: the two paths follow as their own NUL-terminated
            # fields, old first.
            if i + 1 >= len(fields):
                break
            old_path, path = fields[i], fields[i + 1]
            i += 2
        binary = adds.strip() == "-" or dels.strip() == "-"
        files.append(
            FileDiff(
                path=path,
                insertions=0 if binary else int(adds or 0),
                deletions=0 if binary else int(dels or 0),
                binary=binary,
                old_path=old_path,
            )
        )
    return files


def _split_blocks(text: str) -> list[list[str]]:
    """The raw diff cut into one block of lines per file."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _block_path(block: list[str]) -> str:
    """The path a diff block is about, or "" when git quoted it.

    numstat and the full diff walk the same file list in the same order, so
    zipping them positionally is correct - but the failure mode if that ever
    stopped being true is a comment quoting a line out of the wrong file, which
    nothing downstream could notice. So the path is read back out of the block
    and used to match when git wrote it plainly, with position as the fallback.
    A C-quoted path (spaces, quotes, non-ASCII) is reported as unknown rather
    than half-unquoted here: numstat's `-z` output is the one that is already
    right, and two unquoting implementations that disagree is the bug this is
    guarding against in the first place.
    """
    new_side = old_side = ""
    for line in block:
        if line.startswith("@@"):
            break
        if line.startswith("+++ b/"):
            new_side = line[6:].rstrip()
        elif line.startswith("--- a/"):
            old_side = line[6:].rstrip()
    # The new side wins, because that is what numstat names for a rename; the
    # old side is only reached for a deletion, whose new side is /dev/null.
    path = new_side or old_side
    return "" if path.startswith('"') else path


def _parse_hunks(block: list[str], cap: int) -> tuple[list[Line], int]:
    """The body lines of one file's diff block, and how many were dropped.

    Everything before the first `@@` is header noise (mode changes, the index
    line, the `---`/`+++` pair) and is skipped: the paths are already known from
    numstat, and a phone-width column is too scarce to spend on `index a1b2c3d`.
    """
    lines: list[Line] = []
    dropped = 0
    old_no = new_no = 0
    in_body = False
    for raw in block:
        if raw.startswith("@@"):
            in_body = True
            old_no, new_no = _hunk_start(raw)
            if len(lines) >= cap:
                dropped += 1
                continue
            lines.append(Line("hunk", _hunk_label(raw, old_no, new_no)))
            continue
        if not in_body:
            continue
        if raw.startswith("\\"):
            # "\ No newline at end of file" - true of the line above it, not a
            # line of its own, and numbering must not advance for it.
            continue
        if raw.startswith("+"):
            kind, old, new, text = "add", None, new_no, raw[1:]
            new_no += 1
        elif raw.startswith("-"):
            kind, old, new, text = "del", old_no, None, raw[1:]
            old_no += 1
        elif raw.startswith(" "):
            # A context line that is itself empty arrives as a single space -
            # git always writes the marker - so an empty string here is the
            # trailing element of splitting the diff on newlines, never a line
            # of the file. Counting it as context appended a phantom blank row
            # to the last file of every diff, and that row was commentable.
            kind, old, new, text = "ctx", old_no, new_no, raw[1:]
            old_no += 1
            new_no += 1
        else:
            continue
        if len(lines) >= cap:
            dropped += 1
            continue
        lines.append(Line(kind, text, old, new))
    return lines, dropped


def _hunk_label(header: str, old_start: int, new_start: int) -> str:
    """`@@ -12,7 +12,9 @@ def thing():` as something worth reading on a phone.

    The raw header is not kept. Two reasons, one cosmetic and one not: the
    portal's terminal font has no glyph for `@`, so a real `@@` header renders
    as two tofu boxes on the page (seen in the first proof shot of this view);
    and the two ranges say the same thing twice, where what a reviewer actually
    wants is where in the file they are. git puts the enclosing function or
    section after the closing `@@` when it can find one, and that is the most
    useful part of the line, so it is kept verbatim.
    """
    _, _, trailer = header.partition("@@")
    _, _, heading = trailer.partition("@@")
    heading = heading.strip()
    if new_start:
        where = f"line {new_start}"
    elif old_start:
        # Nothing on the new side at all: the whole hunk is gone, so the only
        # honest number is the one it used to have.
        where = f"line {old_start}, removed"
    else:
        where = "start of file"
    return f"{where}, in {heading}" if heading else where


def _hunk_start(header: str) -> tuple[int, int]:
    """The two starting line numbers out of `@@ -12,7 +12,9 @@ def thing():`."""
    try:
        _, spec, _ = header.split("@@", 2)
    except ValueError:
        return 0, 0
    old = new = 0
    for part in spec.split():
        if part.startswith("-"):
            old = int(part[1:].split(",")[0] or 0)
        elif part.startswith("+"):
            new = int(part[1:].split(",")[0] or 0)
    return old, new


def for_run(plan) -> Optional[RunDiff]:
    """What a run changed, ready to render, or None if there is nothing to show.

    Takes a `revert.Landed`, because that is the object that already knows the
    repo and the two commits and has already established they still exist.
    """
    if plan is None:
        return None
    files = _numstat(plan.repo, plan.before, plan.after)
    if files is None:
        return RunDiff(note="The diff for this run could not be read from its workspace.")
    if not files:
        return None

    diff = RunDiff(
        insertions=sum(f.insertions for f in files),
        deletions=sum(f.deletions for f in files),
    )
    if diff.insertions + diff.deletions > MAX_TOTAL_LINES:
        diff.too_big = True
        diff.files = files[:MAX_FILES]
        diff.unlisted = max(0, len(files) - MAX_FILES)
        diff.note = (
            f"{diff.insertions + diff.deletions:,} changed lines is more than this "
            f"page will render. The files are listed; read the diff in the workspace."
        )
        return diff

    diff.files = files[:MAX_FILES]
    diff.unlisted = max(0, len(files) - MAX_FILES)

    raw = _git(
        plan.repo, "diff", "-M", "--unified=3", "--no-color", f"{plan.before}..{plan.after}"
    )
    if raw is None:
        diff.note = "The diff for this run could not be read from its workspace."
        return diff

    blocks = _split_blocks(raw)
    by_path: dict[str, list[str]] = {}
    for block in blocks:
        path = _block_path(block)
        if path and path not in by_path:
            by_path[path] = block
    budget = MAX_RENDERED_LINES
    for i, f in enumerate(diff.files):
        if f.binary:
            continue
        block = by_path.get(f.path) or (blocks[i] if i < len(blocks) else None)
        if block is None:
            continue
        cap = max(0, min(MAX_LINES_PER_FILE, budget))
        lines, dropped = _parse_hunks(block, cap)
        f.lines = lines
        f.truncated = dropped
        budget -= len(lines)
    return diff


def line_at(diff: Optional[RunDiff], path: str, index: int) -> Optional[tuple[FileDiff, Line]]:
    """The file and line a comment addresses, or None if it addresses nothing.

    None is the refusal the comment route turns into a sentence: it covers a
    path that is not in this diff, an index past the end of a truncated file,
    and a hunk header somebody posted the index of.
    """
    if diff is None:
        return None
    for f in diff.files:
        if f.path != path:
            continue
        if index < 0 or index >= len(f.lines):
            return None
        line = f.lines[index]
        return (f, line) if line.commentable else None
    return None


def quote_for(file: FileDiff, line: Line) -> str:
    """The markdown that goes into the journal above the comment.

    A fenced block rather than a blockquote, because the thing being quoted is
    code: a blockquote would reflow it and lose the leading whitespace, which on
    a Python line is the part that says what the line does.
    """
    where = f"{file.path}:{line.anchor}" if line.anchor else file.path
    body = f"{line.marker}{line.text}".rstrip()
    return f"`{where}`\n\n```\n{body}\n```"


# Same shape as `quoting.QUOTE_CAPTION`, and here for the same reason: a reader
# and a model both need to be able to tell a quoted line from a person's own
# words, and the caption is the only thing that says where the quote came from.
COMMENT_CAPTION = "_(commented on run #{run_id}'s diff)_"

# A comment is one instruction about one line. Longer than this and it is a
# note, which the project page already has a box for - and the whole thing is
# going into the next run's prompt, where a pasted essay costs the journal it
# would otherwise carry.
MAX_COMMENT_CHARS = 1500


def note_body(run_id: int, file: FileDiff, line: Line, comment: str) -> str:
    """The journal entry a diff comment becomes.

    The quote goes first and the comment after it, so that a run reading the
    note sees the code before the instruction about it - the same order as
    `quoting.frame`, which is what makes a highlighted-journal note and a diff
    comment read the same way in a prompt.
    """
    parts = [quote_for(file, line), COMMENT_CAPTION.format(run_id=run_id)]
    text = (comment or "").strip()[:MAX_COMMENT_CHARS].strip()
    if text:
        parts.append(text)
    return "\n\n".join(parts)


def repo_for_slug(slug: str) -> Optional[Path]:
    """Re-exported so callers needing a project's repo do not have to know that
    the portal's own source is the one project whose repo is not its
    workspace."""
    return orphans.repo_for(slug)

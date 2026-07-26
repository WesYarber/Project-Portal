"""Surface the Claude CLI's own per-workspace auto-memory.

As it runs, the CLI writes memory files of its own accord to
`~/.claude/projects/<encoded-cwd>/memory/*.md` (an index `MEMORY.md` plus one
file per fact). That is a *second* memory system, running alongside the
portal's profile.md / learnings.md, and Wes never sees it - the research burst
(#223) flagged it as invisible "double-memory" worth surfacing or disabling.

This module makes it visible and strictly READ-ONLY: the portal never writes,
edits or deletes the CLI's files. It only reads them so /memory can show what
the CLI has quietly recorded, mapped back to the portal project whose workspace
it belongs to when that mapping is known.

Every function fails soft: a missing directory, an unreadable file or a
permission error yields empty results rather than raising, because a broken
read here must never take down the /memory page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config

# The CLI names each project directory after the absolute cwd it was launched
# in, replacing every character that is not a letter, digit or hyphen with a
# hyphen. So /home/ada/project-portal/data/projects/foo becomes
# -home-ada-project-portal-data-projects-foo. We reproduce that encoding to
# look a workspace up, rather than trying to invert it (which is ambiguous:
# a hyphen in the dir name could have been a slash, a dot or a real hyphen).
_UNSAFE = re.compile(r"[^A-Za-z0-9-]")

# `description: "..."` out of a memory file's YAML-ish frontmatter.
_FRONT_DESC = re.compile(r'^description:\s*(.*?)\s*$', re.MULTILINE)


def encode_cwd(path) -> str:
    """Encode an absolute path the way the CLI names its per-project dir."""
    return _UNSAFE.sub("-", str(path))


@dataclass
class MemoryFile:
    name: str          # the filename, e.g. "the-agent-machine.md"
    size: int          # bytes
    lines: int
    description: str    # frontmatter description, else the first real line


@dataclass
class AutoMemoryDir:
    label: str          # portal project/task title if known, else the raw dir
    dir_name: str       # the CLI's encoded directory name (used in file URLs)
    kind: str           # "project" | "task" | "other"
    mapped: bool        # True when dir_name matched a known portal workspace
    index: str          # MEMORY.md text ("" if the CLI has not written one)
    files: list[MemoryFile] = field(default_factory=list)
    total_size: int = 0

    @property
    def file_count(self) -> int:
        return len(self.files)


def _description_of(text: str, name: str) -> str:
    """A one-line gist of a memory file: the frontmatter `description:` if it
    has one, otherwise the first line of real prose after the frontmatter."""
    lines = text.splitlines()
    body = lines
    if lines and lines[0].strip() == "---":
        # Skip the frontmatter block, but read its description first.
        m = _FRONT_DESC.search(text)
        if m:
            desc = m.group(1).strip().strip('"').strip()
            # Frontmatter often escapes inner quotes (\" ) inside the quoted
            # value; unescape them so the gist reads as prose, not source.
            return desc.replace('\\"', '"').strip()
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body = lines[i + 1:]
                break
    for raw in body:
        line = raw.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def _read_files(mem_dir: Path) -> tuple[str, list[MemoryFile], int]:
    """(index_text, non-index files, total bytes) for one memory directory.

    MEMORY.md is the CLI's own index and is pulled out separately; the rest are
    listed as individual facts. Sorted by name for a stable display."""
    index = ""
    files: list[MemoryFile] = []
    total = 0
    try:
        entries = sorted(mem_dir.glob("*.md"), key=lambda p: p.name.lower())
    except OSError:
        return "", [], 0
    for path in entries:
        try:
            text = path.read_text(encoding="utf-8")
            size = path.stat().st_size
        except (OSError, UnicodeDecodeError):
            continue
        total += size
        if path.name == "MEMORY.md":
            index = text
            continue
        files.append(
            MemoryFile(
                name=path.name,
                size=size,
                lines=len(text.splitlines()),
                description=_description_of(text, path.name),
            )
        )
    return index, files, total


def scan(
    known: Optional[dict[str, tuple[str, str]]] = None,
    root: Optional[Path] = None,
) -> list[AutoMemoryDir]:
    """Every CLI memory directory that actually holds a file, newest-heaviest
    first, mapped to a portal project/task label where `known` says so.

    `known` maps an encoded dir name -> (label, kind); the caller builds it from
    the live projects and one-off tasks. A directory not in `known` is shown as
    "other" (a workspace the CLI ran in outside the portal - an interactive
    session, or a project's own source tree). Transient pytest temp dirs are
    dropped unless a caller explicitly mapped them.
    """
    known = known or {}
    root = root or config.cli_projects_dir()
    out: list[AutoMemoryDir] = []
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    for d in dirs:
        mem = d / "memory"
        if not mem.is_dir():
            continue
        index, files, total = _read_files(mem)
        if not index and not files:
            continue  # an empty memory/ dir the CLI created but never filled
        label, kind = known.get(d.name, (d.name, "other"))
        mapped = d.name in known
        # A pytest run leaves a memory dir under /tmp; encoded, that starts
        # "-tmp-". Never clutter the page with those unless a test maps them.
        if not mapped and d.name.startswith("-tmp-"):
            continue
        out.append(
            AutoMemoryDir(
                label=label,
                dir_name=d.name,
                kind=kind,
                mapped=mapped,
                index=index,
                files=files,
                total_size=total,
            )
        )
    # Mapped (portal-owned) directories first, then by weight - the biggest
    # invisible piles are the ones Wes most wants to see.
    out.sort(key=lambda a: (not a.mapped, -a.total_size, a.label.lower()))
    return out


def read_file(dir_name: str, filename: str, root: Optional[Path] = None) -> Optional[str]:
    """The text of one CLI memory file, or None if it is not a real `.md` file
    inside a memory directory under `root`.

    Guards against path traversal: both `dir_name` and `filename` must be plain
    names (no separators, no `..`), and the resolved path must stay inside the
    memory directory. The portal only ever reads here.
    """
    root = root or config.cli_projects_dir()
    if not dir_name or not filename or not filename.endswith(".md"):
        return None
    if "/" in dir_name or "\\" in dir_name or dir_name in (".", ".."):
        return None
    if "/" in filename or "\\" in filename or ".." in filename:
        return None
    mem = (root / dir_name / "memory").resolve()
    target = (mem / filename).resolve()
    try:
        # target must be directly inside mem, not escaped via a symlink/..
        if target.parent != mem:
            return None
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def build_known(projects, tasks=None) -> dict[str, tuple[str, str]]:
    """Map encoded dir name -> (label, kind) for the portal's own workspaces.

    `projects` are rows with `slug` and `title`; `tasks` (optional) are one-off
    rows with `id` and `title`. The portal's own source checkout is included so
    a self-improvement run's CLI memory is attributed to it rather than showing
    as an anonymous "other" directory.
    """
    known: dict[str, tuple[str, str]] = {}
    for p in projects or []:
        try:
            ws = config.PROJECTS_DIR / p["slug"]
            known[encode_cwd(ws)] = (p["title"], "project")
        except (KeyError, TypeError):
            continue
    for t in tasks or []:
        try:
            ws = config.TASKS_DIR / f"task-{int(t['id'])}"
            title = t["title"] or f"task {t['id']}"
            known[encode_cwd(ws)] = (title, "task")
        except (KeyError, TypeError, ValueError):
            continue
    # The portal's own source tree: runs on the meta-project work here, not in a
    # data/projects/<slug> workspace (its workspace is a placeholder).
    known[encode_cwd(config.APP_ROOT)] = ("Project Portal (source)", "project")
    return known

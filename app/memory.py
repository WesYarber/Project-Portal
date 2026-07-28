"""Versioned history for the shared memory files.

`profile.md` and `learnings.md` are rewritten in three different ways - Wes
edits them on /memory, the daily reflect job rewrites the profile, and the
compaction job rewrites the learnings - and until now every one of those was a
destructive overwrite with no copy kept anywhere. Wes typed things about
himself into the profile that a later reflect quietly replaced, and there is no
record of what they were: the files are not in git (data/ is ignored), the run
logs record tool *names* rather than file contents, and nothing else on the
box had ever read them.

So: before anything writes one of these files, the previous contents are copied
here. This is deliberately dumb - a directory of timestamped copies, newest
first, with a restore button - because the failure it exists to prevent is
"the only copy is gone", and any cleverer scheme has more ways to lose it.

The history directory lives beside the memory directory rather than inside it,
because MEMORY_DIR is the *cwd* of the reflect and compaction agents: forty old
copies of learnings.md sitting in the working directory would be forty files
those agents can read, and a compaction agent that reads its own history is
being fed exactly the noise it was launched to remove.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Iterable, Optional

from . import config

# The files this module will version, by the short name used in URLs. Anything
# not in here is refused: the restore route takes a name from the URL and ends
# in a write, so the set of writable targets is a fixed list rather than
# anything derived from what the caller asked for.
#
# Both this and the history directory are resolved on every call rather than
# bound at import: app.config's paths are module constants that the test
# fixture (and deploy/preview.py) repoint at a throwaway directory, and a
# module that cached them at import time would write into the live data
# directory from a test run.
TRACKED_STEMS = ("profile", "learnings")


def tracked() -> dict[str, Path]:
    return {"profile": config.PROFILE_MD, "learnings": config.LEARNINGS_MD}


def history_dir() -> Path:
    return config.DATA_DIR / "memory-history"

# How many copies of each file to keep. Past this the oldest go: these are
# whole-file copies of something that is at most a few tens of kilobytes, and
# the value of a revision drops off sharply once there are newer ones either
# side of it.
KEEP = 60

# <stem>-<YYYYmmddTHHMMSSZ>.md, and nothing else. Used to parse a filename back
# into a timestamp *and* as the guard on the restore route.
_NAME_RE = re.compile(r"^(profile|learnings)-(\d{8}T\d{6}Z)\.md$")


@dataclass
class Revision:
    name: str
    stem: str
    ts: str  # ISO-8601 UTC, for the templates' timeago filter
    size: int
    lines: int

    @property
    def path(self) -> Path:
        return history_dir() / self.name


def _stamp(when: Optional[datetime] = None) -> str:
    when = when or datetime.now(timezone.utc)
    return when.strftime("%Y%m%dT%H%M%SZ")


def _iso(stamp: str) -> str:
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:  # pragma: no cover - _NAME_RE has already matched
        return stamp


def snapshot(stem: str, *, when: Optional[datetime] = None) -> Optional[Path]:
    """Copy the current contents of a tracked file into the history.

    Returns the new revision's path, or None if there was nothing worth
    copying: the file does not exist, it is empty, or it is byte-identical to
    the newest revision already stored. That last case is what keeps the
    history readable - the portal restarts itself after most runs and several
    callers snapshot defensively, so without it the list would fill up with
    identical copies and push the revision Wes actually wants off the end.
    """
    path = tracked().get(stem)
    if path is None:
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not content.strip():
        return None

    existing = revisions(stem)
    if existing:
        try:
            if existing[0].path.read_text(encoding="utf-8") == content:
                return None
        except OSError:
            pass

    history_dir().mkdir(parents=True, exist_ok=True)
    stamp = _stamp(when)
    target = history_dir() / f"{stem}-{stamp}.md"
    # Two writes inside the same second want the same second-resolution name.
    # Walk forward a second at a time rather than overwriting: losing the older
    # of two revisions is the one thing this module must not do.
    if target.exists():
        base = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        for bump in range(1, 61):
            candidate = history_dir() / f"{stem}-{_stamp(base + timedelta(seconds=bump))}.md"
            if not candidate.exists():
                target = candidate
                break
        else:
            return None
    try:
        target.write_text(content, encoding="utf-8")
    except OSError:
        return None
    prune(stem)
    return target


def revisions(stem: str = "") -> list[Revision]:
    """Stored revisions, newest first. `stem` empty means every tracked file."""
    if not history_dir().is_dir():
        return []
    out: list[Revision] = []
    try:
        entries = list(history_dir().iterdir())
    except OSError:
        return []
    for entry in entries:
        match = _NAME_RE.match(entry.name)
        if not match or not entry.is_file():
            continue
        if stem and match.group(1) != stem:
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out.append(
            Revision(
                name=entry.name,
                stem=match.group(1),
                ts=_iso(match.group(2)),
                size=len(text.encode("utf-8")),
                lines=text.count("\n") + (0 if text.endswith("\n") or not text else 1),
            )
        )
    # By timestamp, then name. Sorting on the whole filename would be right
    # within one file (the stamp is fixed-width UTC) and wrong across two,
    # where it would order by "learnings" vs "profile" first and put an older
    # profile revision above a newer learnings one.
    out.sort(key=lambda r: (r.ts, r.name), reverse=True)
    return out


def read_revision(name: str) -> Optional[str]:
    """The text of one revision, or None if the name is not one of ours."""
    if not _NAME_RE.match(name or ""):
        return None
    path = history_dir() / name
    try:
        if path.resolve().parent != history_dir().resolve():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def restore(name: str) -> bool:
    """Put a stored revision back, after snapshotting what is there now.

    The snapshot first is the whole point: restoring is itself a destructive
    write, and getting the restore wrong must not be the thing that loses the
    current version.
    """
    text = read_revision(name)
    if text is None:
        return False
    match = _NAME_RE.match(name)
    stem = match.group(1)  # type: ignore[union-attr]
    target = tracked()[stem]
    snapshot(stem)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError:
        return False
    return True


def snapshot_all(*, when: Optional[datetime] = None) -> list[Path]:
    """Snapshot every tracked file. Called on startup, so that the version on
    disk at boot always exists somewhere else too - it costs nothing when
    nothing has changed (identical contents are not re-stored) and it means the
    history starts filling up without waiting for someone to press save."""
    out = []
    for stem in TRACKED_STEMS:
        path = snapshot(stem, when=when)
        if path is not None:
            out.append(path)
    return out


def prune(stem: str, keep: int = KEEP) -> int:
    """Drop the oldest revisions of one file past `keep`. Returns how many went."""
    extra = revisions(stem)[keep:]
    gone = 0
    for revision in extra:
        try:
            revision.path.unlink()
            gone += 1
        except OSError:
            continue
    return gone


# --- Superseded-learnings archive --------------------------------------------
#
# The write gate on learnings.md (worker._append_learnings) supersedes a bullet
# in place when a refined rephrasing arrives, and drops a bullet outright on an
# explicit delete. The whole-file revision snapshots already make the *previous
# file* one click from restore, but reading a single dropped fact back out of a
# 200-line before/after diff is real work, and Wes distrusts memory that loses
# things quietly. So every line the gate removes is also appended here, with the
# date and the reason it went, as a plain browsable trail of retired facts.
#
# Like the revision history, this file lives BESIDE the memory directory, never
# inside it: MEMORY_DIR is the cwd of the reflect and compaction agents, and a
# growing log of every superseded learning sitting in their working directory is
# exactly the stale noise those jobs exist to remove.

# `- [YYYY-MM-DD] (reason) text`, and nothing else. Parsed back for the UI.
_ARCHIVE_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\] \(([^)]*)\) (.*\S)\s*$")


@dataclass
class ArchivedLearning:
    date: str  # YYYY-MM-DD, as written
    reason: str  # short phrase: "superseded", "retired", ...
    text: str  # the retired bullet's text


def archive_path() -> Path:
    return config.DATA_DIR / "learnings-archive.md"


def archive_learning(text: str, reason: str, *, when: Optional[datetime] = None) -> None:
    """Append one retired learning to the archive. Never raises: losing the
    archive line must never be the thing that turns a good write into a crash -
    the learning has already been removed from the live file by the caller."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return
    reason = re.sub(r"[()\n]", " ", reason or "retired").strip() or "retired"
    day = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    line = f"- [{day}] ({reason}) {text}\n"
    try:
        archive_path().parent.mkdir(parents=True, exist_ok=True)
        with open(archive_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        return


def archived_learnings(limit: int = 200) -> list[ArchivedLearning]:
    """Retired learnings, newest first (the file is append-order, so oldest
    first on disk). Returns at most `limit`."""
    path = archive_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[ArchivedLearning] = []
    for ln in raw.splitlines():
        m = _ARCHIVE_RE.match(ln)
        if m:
            out.append(ArchivedLearning(date=m.group(1), reason=m.group(2), text=m.group(3)))
    out.reverse()
    return out[:limit]


# --- Live-learning freshness sidecar ------------------------------------------
#
# learnings.md deliberately carries no inline dates: Wes asked for its lines to
# read as durable facts, not a log, and the file is pasted into every run of
# every project, so a `[2026-07-23]` stamped on every line is exactly the noise
# he complained about. But the memory overhaul (#220) wants an added and a
# last-confirmed date per entry, so the compaction job - and Wes on /memory -
# can tell a fact that many runs keep re-noticing from one added once months ago
# and never seen since. So the dates live in a sidecar keyed by the same
# normalized text the write gate dedupes on, never in the prompt-facing file.
#
# The key idea: the write gate already drops a re-observed duplicate as a NOOP.
# That NOOP is *evidence the fact is still true* - so rather than discard it
# silently, the gate records it as a confirmation, bumping the entry's
# last-confirmed date and its observation count. A line seen six times and
# confirmed today is plainly load-bearing; one added in April with count 1 and
# never re-seen is a compaction candidate.
#
# Like the archive and the revision history, this sidecar lives BESIDE the
# memory directory, never inside it (MEMORY_DIR is the reflect/compaction
# agents' cwd). It is best-effort: hand-edits and compaction rewrites change the
# wording and orphan a key, so the gate garbage-collects keys no longer present
# in the live file on every write, and an untracked live line simply shows as
# undated. Losing this file loses only the age hints, never a learning.


def learnings_meta_path() -> Path:
    return config.DATA_DIR / "learnings-meta.json"


def load_learnings_meta() -> dict:
    """The sidecar, or {} if it is missing or unreadable. Never raises."""
    try:
        data = json.loads(learnings_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_learnings_meta(meta: dict) -> None:
    """Persist the sidecar. Never raises - losing the age hints must not turn a
    good learnings write into a crash."""
    try:
        learnings_meta_path().parent.mkdir(parents=True, exist_ok=True)
        learnings_meta_path().write_text(
            json.dumps(meta, sort_keys=True, indent=0), encoding="utf-8"
        )
    except OSError:
        return


def today(when: Optional[datetime] = None) -> str:
    return (when or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def meta_touch(meta: dict, key: str, *, day: str) -> None:
    """Record that `key` was observed on `day`. The first sighting creates the
    entry (added = confirmed = day, count 1); a later sighting bumps confirmed
    and count and leaves the original added date alone."""
    if not key:
        return
    ent = meta.get(key)
    if isinstance(ent, dict) and ent.get("added"):
        ent["confirmed"] = day
        ent["count"] = int(ent.get("count", 1) or 1) + 1
    else:
        meta[key] = {"added": day, "confirmed": day, "count": 1}


def meta_supersede(meta: dict, old_key: str, new_key: str, *, day: str) -> None:
    """A refined rephrasing replaced old_key's line with new_key's text: carry
    the original added date forward (the fact is the same, only reworded) and
    treat the rewrite as a fresh confirmation."""
    if not new_key:
        return
    old = meta.get(old_key) if isinstance(meta.get(old_key), dict) else None
    if old_key and old_key != new_key:
        meta.pop(old_key, None)
    added = (old or {}).get("added") or day
    count = int((old or {}).get("count", 1) or 1)
    meta[new_key] = {"added": added, "confirmed": day, "count": count + 1}


def meta_forget(meta: dict, key: str) -> None:
    meta.pop(key, None)


def meta_prune(meta: dict, live_keys: Iterable[str]) -> None:
    """Drop metadata for keys no longer present as live bullets. Hand-edits and
    compaction rewrites orphan keys, and an unbounded sidecar is the same rot
    the write gate exists to prevent."""
    live = set(live_keys)
    for k in list(meta):
        if k not in live:
            meta.pop(k, None)


# --- Promoted skills ----------------------------------------------------------
#
# The last piece of the memory overhaul (#226): a learning that is really a
# PROCEDURE - a how-to whose value is its concrete steps or exact commands - is
# worth more as a skill than as a bullet. A skill is copied into every workspace
# and indexed by name in every run's prompt, so the recipe stays discoverable
# while its full text stops taxing every run's context. The compaction agent
# does the judging and the writing (it is already the pass that reads every
# learning); this module owns where they live and gives /memory a window and a
# delete button, because Wes distrusts memory he cannot see or prune.
#
# The directory lives BESIDE the memory directory, never inside it (MEMORY_DIR
# is the reflect/compaction agents' cwd), exactly like the revision history,
# the retired-learnings archive and the freshness sidecar.

# Skill names come back in from URLs (view/delete) and go out as directory
# names an agent creates, so the accepted shape is a fixed pattern rather than
# anything path-like: one plain component, no separators, no dots.
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass
class PromotedSkill:
    name: str  # the directory name, e.g. "render-a-page-headlessly"
    description: str  # the SKILL.md frontmatter one-liner shown in prompts
    lines: int
    size: int


def promoted_skills_dir() -> Path:
    return config.DATA_DIR / "skills"


def skill_description(skill_md: Path) -> str:
    """The frontmatter `description:` line of a SKILL.md, or "". Two-line parse
    on purpose - the only field that matters is a flat string, not worth a yaml
    dependency (same call agent_runner's prompt index makes). Never raises.

    Substituted for this installation, like the copy worker._localise_skill
    ships into a workspace: a shipped skill carries `$OWNER`/`$HOST` rather
    than a name and a hostname (#254), and this description is read straight
    out of the *source* file - so without this the prompt index and /memory
    would both show a literal "$OWNERS dark terminal theme".
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines()[:20]:
        if line.startswith("description:"):
            raw = line.split(":", 1)[1].strip()
            return Template(raw).safe_substitute(**config.SITE.template_vars())
    return ""


def promoted_skills() -> list[PromotedSkill]:
    """The skills the compaction agent has promoted out of learnings.md, by
    name. A directory without a SKILL.md is skipped (half-written, or junk a
    confused agent left) rather than listed broken. Fails soft: a missing or
    unreadable root reads as no skills, never a raise."""
    root = promoted_skills_dir()
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    out: list[PromotedSkill] = []
    for path in entries:
        md = path / "SKILL.md"
        if not md.is_file():
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out.append(
            PromotedSkill(
                name=path.name,
                description=skill_description(md),
                lines=len(text.splitlines()),
                size=len(text),
            )
        )
    return out


def read_promoted_skill(name: str) -> Optional[str]:
    """One promoted skill's SKILL.md text, or None. The name arrives from a
    URL, so it is shape-checked and the resolved path is confirmed to sit
    inside the skills root before any read - the same traversal posture as the
    CLI-memory viewer."""
    if not _SKILL_NAME_RE.match(name or ""):
        return None
    root = promoted_skills_dir()
    md = root / name / "SKILL.md"
    try:
        if md.resolve().parent.parent != root.resolve() or not md.is_file():
            return None
        return md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def delete_promoted_skill(name: str) -> bool:
    """Remove one promoted skill directory, with the same guards as reading.
    Deliberate and Wes-only (a button on /memory): promotion is an agent's
    judgement call, and this is how he overrules it."""
    if not _SKILL_NAME_RE.match(name or ""):
        return False
    root = promoted_skills_dir()
    target = root / name
    try:
        if target.resolve().parent != root.resolve() or not target.is_dir():
            return False
        shutil.rmtree(target)
        return True
    except OSError:
        return False

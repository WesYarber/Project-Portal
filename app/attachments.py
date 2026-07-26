"""Files Wes drops onto a project: images, audio, video, anything.

Storage lives *inside the project workspace* (`<workspace>/attachments/`) rather
than in a separate blob directory, because the workspace is the agent's cwd.
That means a dropped screenshot is readable by the agent with a plain relative
path - `Read attachments/0007-screenshot.png` - with no extra tooling, and it
travels with the workspace if the project is ever moved or archived.

The DB row is the index (who uploaded what, when, attached to which note); the
file on disk is the payload. The row is written first so the id can be baked
into the stored filename, which is what makes collisions impossible without
inventing a second uniqueness scheme.
"""
from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import Optional

from app import config, db

log = logging.getLogger("portal.attachments")

# A generous cap: a phone photo is ~5 MB and a minute of webm audio is well
# under 1 MB, but a screen recording can be tens of MB.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

# Types we are willing to render inline in the browser. Everything else is
# served as a download with a neutral content type - user-supplied HTML or SVG
# served inline from this origin would be script execution on the portal's own
# domain, which is a real hole even on a single-user LAN tool.
INLINE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
    "audio/mpeg",
    "audio/mp4",
    "audio/ogg",
    "audio/wav",
    "audio/x-wav",
    "audio/webm",
    "video/mp4",
    "video/webm",
    "video/ogg",
    "video/quicktime",
    "text/plain",
}

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
MAX_NAME_LEN = 60


def safe_name(raw: str) -> str:
    """A filename that is safe to join onto a directory.

    Takes the basename only (a browser on some platforms sends a full path),
    collapses anything outside a conservative allowlist, and refuses to produce
    a name that is empty, dot-prefixed, or all dots - each of which would either
    escape the directory or create a hidden file the workspace listing misses.
    """
    base = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = _SAFE_CHARS.sub("-", base).strip("-.")
    if not cleaned:
        return "upload"
    if len(cleaned) <= MAX_NAME_LEN:
        return cleaned
    # Truncate the stem, never the extension: a .png that becomes extensionless
    # stops rendering as an image everywhere downstream.
    stem, dot, ext = cleaned.rpartition(".")
    if dot and 0 < len(ext) <= 8:
        keep = max(1, MAX_NAME_LEN - len(ext) - 1)
        return f"{stem[:keep]}.{ext}"
    return cleaned[:MAX_NAME_LEN]


def guess_mime(name: str, declared: str = "") -> str:
    """Prefer what the browser said, fall back to the extension.

    A MediaRecorder blob arrives as `audio/webm;codecs=opus`; the parameters are
    dropped so the value can be compared against INLINE_TYPES directly.
    """
    declared = (declared or "").split(";", 1)[0].strip().lower()
    if declared and "/" in declared and declared != "application/octet-stream":
        return declared
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def media_kind(mime: str) -> str:
    """Which player/preview the UI should use: image | audio | video | file."""
    top = (mime or "").split("/", 1)[0]
    return top if top in {"image", "audio", "video"} else "file"


def human_size(num: Optional[int]) -> str:
    if not num:
        return "0 B"
    step = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if step < 1024 or unit == "GB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} GB"


def attachments_dir(slug: str) -> Path:
    return config.PROJECTS_DIR / slug / "attachments"


# The workspace-relative prefix the agent is told about. Kept as one constant so
# the prompt, the file listing and the storage path can never drift apart.
REL_DIR = "attachments"


def rel_path(stored_name: str) -> str:
    return f"{REL_DIR}/{stored_name}"


def store(
    project_id: int,
    slug: str,
    orig_name: str,
    data: bytes,
    declared_mime: str = "",
    journal_id: Optional[int] = None,
    note: str = "",
) -> dict:
    """Write an upload to the project workspace and index it.

    Raises ValueError for an empty or oversized upload, so the route can turn
    either into a 400 rather than half-creating an attachment.
    """
    if not data:
        raise ValueError("Empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"File is {human_size(len(data))}; the limit is {human_size(MAX_UPLOAD_BYTES)}"
        )

    name = safe_name(orig_name)
    mime = guess_mime(name, declared_mime)
    # The row first: its id becomes part of the stored filename, so two files
    # called screenshot.png can never fight over the same path.
    row_id = db.add_attachment(
        project_id=project_id,
        orig_name=name,
        stored_name="",
        mime=mime,
        size=len(data),
        journal_id=journal_id,
        note=note,
    )
    stored_name = f"{row_id:04d}-{name}"
    directory = attachments_dir(slug)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        (directory / stored_name).write_bytes(data)
    except OSError:
        # Don't leave an index entry pointing at a file that isn't there.
        db.delete_attachment(row_id)
        raise
    db.set_attachment_stored_name(row_id, stored_name)
    log.info("Stored attachment %s for %s (%s, %s)", stored_name, slug, mime, human_size(len(data)))
    return dict(db.get_attachment(row_id))


def disk_path(slug: str, stored_name: str) -> Optional[Path]:
    """Resolve an attachment's path, refusing anything that isn't really inside
    the project's attachments directory. `stored_name` comes from the DB, but
    this is the last gate before an open() so it is checked anyway."""
    directory = attachments_dir(slug)
    try:
        root = directory.resolve()
        candidate = (directory / stored_name).resolve()
    except OSError:
        return None
    if candidate.parent != root or not candidate.is_file():
        return None
    return candidate


def remove_file(slug: str, stored_name: str) -> None:
    path = disk_path(slug, stored_name)
    if path is not None:
        path.unlink(missing_ok=True)


def prompt_section(project_id: int) -> str:
    """The `## Attachments` block for the agent prompt.

    Paths are workspace-relative because the agent's cwd *is* the workspace, and
    the note each file was attached to is quoted alongside it - a screenshot
    with no idea which sentence it illustrates is much less useful.
    """
    rows = db.list_attachments(project_id)
    if not rows:
        return ""
    lines = [
        "## Attachments",
        f"Files {config.SITE.owner} uploaded to this project. They are on disk in your working "
        "directory - read them with their relative path (images are readable "
        "directly with the Read tool).",
        "",
    ]
    for row in rows:
        note = (row["note"] or "").strip().replace("\n", " ")
        if len(note) > 120:
            note = note[:117] + "..."
        detail = f" - with note: {note}" if note else ""
        lines.append(
            f"- `{rel_path(row['stored_name'])}` ({row['mime']}, "
            f"{human_size(row['size'])}, added {row['created_at']}){detail}"
        )
    return "\n".join(lines)

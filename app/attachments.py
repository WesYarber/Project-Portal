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


def wants_transcript(mime: str) -> bool:
    """Only audio gets transcribed (app/transcribe.py). A screen-recording
    video may carry speech too, but transcribing every video upload spends
    minutes of CPU on files that are usually silent; voice memos are the
    feature. Lives here rather than in transcribe.py because prompt_section
    needs it and transcribe.py imports this module."""
    return (mime or "").startswith("audio/")


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


def incoming_dir(slug: str) -> Path:
    """Where an upload waits until a run is ready to be told about it.

    Outside every workspace on purpose - see config.INCOMING_DIR.
    """
    return config.INCOMING_DIR / slug


# The workspace-relative prefix the agent is told about. Kept as one constant so
# the prompt, the file listing and the storage path can never drift apart.
REL_DIR = "attachments"


def rel_path(stored_name: str) -> str:
    return f"{REL_DIR}/{stored_name}"


# The listing a note carries in its own text when files ride along with it, and
# the surgery that takes one file back out of that listing again. Both live
# here, one above the other, because they are a single format read from two ends
# and a copy of it in `main.add_note` is how the two drift apart.
#
# The listing is not decoration. A note's markdown goes into the prompt verbatim
# (app/notes.render), so these lines are an instruction to the agent to go read
# those paths - which means a listing that still names a file Wes has since
# removed is an instruction to read something that is not in the workspace.
ATTACHED_HEAD = "**Attached {n} file(s):**"
_ATTACHED_RE = re.compile(r"^\*\*Attached \d+ file\(s\):\*\*$")
# Deliberately anchored on the attachments/ prefix rather than on "a bullet with
# a backticked thing in it": a note whose own text lists source files - "- `x.py`
# is broken" - must not be read as part of this block.
_LISTED_RE = re.compile(rf"^- `({re.escape(REL_DIR)}/[^`]+)`")
# Removing the whole block leaves the blank line that separated it behind.
_BLANK_RUN = re.compile(r"\n{3,}")


def listing_block(rows: list[dict]) -> str:
    """The `**Attached N file(s):**` block for a note that carried uploads."""
    lines = "\n".join(
        f"- `{rel_path(r['stored_name'])}` ({r['mime']}, {human_size(r['size'])})"
        for r in rows
    )
    return f"{ATTACHED_HEAD.format(n=len(rows))}\n{lines}"


def strip_from_note(body: str, stored_name: str) -> str:
    """The same note body with one file no longer named in it.

    Returns `body` unchanged when the file is not listed at all, which is the
    ordinary case for anything uploaded before this listing existed or for a
    note whose text has since been rewritten by hand - the caller uses that
    identity to decide whether the note is worth an UPDATE at all.

    The header count is **recounted** from the lines that survive rather than
    decremented. A decrement is correct only if the number was correct to begin
    with, and this body is editable by hand between the two events; recounting
    cannot leave "Attached 2 file(s)" over one line.
    """
    target = rel_path(stored_name)
    kept: list[str] = []
    dropped = False
    for line in (body or "").splitlines():
        match = _LISTED_RE.match(line)
        if match and match.group(1) == target:
            dropped = True
            continue
        kept.append(line)
    if not dropped:
        return body

    out: list[str] = []
    for i, line in enumerate(kept):
        if not _ATTACHED_RE.match(line):
            out.append(line)
            continue
        # How many listed files this header still has under it. A header with
        # none left is dropped with them: "Attached 0 file(s):" over nothing is
        # a sentence about an absence, and the note reads better without it.
        count = 0
        for follower in kept[i + 1:]:
            if not _LISTED_RE.match(follower):
                break
            count += 1
        if count:
            out.append(ATTACHED_HEAD.format(n=count))
    return _BLANK_RUN.sub("\n\n", "\n".join(out)).strip()


# The transcript that whisper writes beside a voice memo (app/transcribe.py).
# Named here because the sidecar has to travel with the audio when it is
# revealed: a .txt appearing alone in a running agent's workspace is the same
# surprise as the audio appearing alone, and half of it is the memo's words.
def sidecar_name(stored_name: str) -> str:
    return f"{stored_name}.txt"


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
    # Staged, not placed. The workspace is a running agent's cwd, and a file
    # appearing in it mid-run is a file whose explanation has not arrived yet -
    # see config.INCOMING_DIR and `reveal` below.
    directory = incoming_dir(slug)
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


def _within(directory: Path, stored_name: str) -> Optional[Path]:
    """`directory / stored_name`, but only if it really lands inside it.

    `stored_name` comes from the DB rather than from a request, but this is the
    last gate before an open() and a traversal is cheap to refuse.
    """
    try:
        root = directory.resolve()
        candidate = (directory / stored_name).resolve()
    except OSError:
        return None
    if candidate.parent != root or not candidate.is_file():
        return None
    return candidate


def disk_path(slug: str, stored_name: str) -> Optional[Path]:
    """Where this attachment's bytes actually are, wherever that is.

    Both locations, workspace first, because a file spends the first part of
    its life staged (`incoming_dir`) and the rest of it in the workspace. Every
    reader goes through here - the download route, the transcriber, the delete -
    so none of them has to know which half of that life the file is in, and Wes
    can open a file he has just uploaded without waiting for a run to place it.
    His own upload is his to look at immediately; it is only the *agent* that is
    made to wait.
    """
    for directory in (attachments_dir(slug), incoming_dir(slug)):
        found = _within(directory, stored_name)
        if found is not None:
            return found
    return None


def reveal(project_id: int, slug: str) -> list[str]:
    """Move this project's staged uploads into its workspace. Returns the names.

    Called once at the top of a prompt build, so the run that is told about a
    file is the run whose prompt also carries the note it arrived with. Wes,
    2026-08-16: "Just want to solve this where agents dont keep asking questions
    about the attached files before they receive the prompts where I actually
    explained it along side the upload."

    Three things it deliberately tolerates, because all three are ordinary:

    * **the file is already in the workspace.** Every row that predates
      `revealed_at` is in exactly this state, as is anything restored from a
      backup or moved by hand. The row is stamped and nothing is moved, which
      is what makes this the whole of the migration.
    * **a name already taken in the workspace.** Cannot happen from `store`
      (the row id is in the filename), so it means something else put a file
      there - and overwriting it would destroy whatever that was. The staged
      copy stays put and is retried next run.
    * **the move failing.** Logged and left unrevealed: the file is still in
      staging, still served to Wes by `disk_path`, and still on the list for
      the next run. A file the agent never sees is a bad day; a file that
      disappears is a lost upload, which is the worse one.

    Never raises. A prompt build that dies because a screenshot could not be
    moved would take the run with it.
    """
    workspace = attachments_dir(slug)
    moved: list[str] = []
    for row in db.unrevealed_attachments(project_id):
        stored = row["stored_name"]
        staged = _within(incoming_dir(slug), stored)
        if staged is None:
            # Already where it belongs (or gone). Stamping either way: a row
            # whose file has vanished must not be retried on every future run
            # forever, and `prompt_section` skips a missing file anyway.
            db.mark_attachment_revealed(row["id"])
            continue
        target = workspace / stored
        if target.exists():
            log.warning(
                "Not revealing %s/%s: a different file already has that name",
                slug, stored,
            )
            continue
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            staged.replace(target)
        except OSError:
            log.exception("Could not reveal attachment %s to %s", stored, slug)
            continue
        # The memo's words travel with the memo. Best-effort and after the
        # audio has landed: a transcript that fails to move is a sidecar the
        # agent reads out of the prompt instead, not a reason to leave the
        # audio staged.
        side = _within(incoming_dir(slug), sidecar_name(stored))
        if side is not None:
            try:
                side.replace(workspace / sidecar_name(stored))
            except OSError:
                log.exception("Could not move the transcript beside %s", stored)
        db.mark_attachment_revealed(row["id"])
        moved.append(stored)
    if moved:
        log.info("Revealed %d attachment(s) to %s: %s", len(moved), slug, ", ".join(moved))
    return moved


def remove_file(slug: str, stored_name: str) -> None:
    """Delete the bytes from wherever they are, and the transcript with them."""
    for directory in (attachments_dir(slug), incoming_dir(slug)):
        for name in (stored_name, sidecar_name(stored_name)):
            found = _within(directory, name)
            if found is not None:
                found.unlink(missing_ok=True)


def prompt_section(project_id: int) -> str:
    """The `## Attachments` block for the agent prompt.

    Paths are workspace-relative because the agent's cwd *is* the workspace, and
    the note each file was attached to is quoted alongside it - a screenshot
    with no idea which sentence it illustrates is much less useful.

    Only files that have been revealed, which on the normal path means "every
    file, because `reveal` runs immediately above this". The filter is here
    anyway rather than being left to the caller's ordering: a path that named a
    staged file would be telling the agent to read something that is not in its
    workspace, which is a worse failure than the one this is all about.
    """
    rows = [r for r in db.list_attachments(project_id) if r["revealed_at"]]
    if not rows:
        return ""
    lines = [
        "## Attachments",
        # Blamed on nobody in particular: attachments are not attributed rows,
        # and on a shared project naming the install owner as the uploader of
        # every file is a guess dressed as a fact.
        "Files uploaded to this project. They are on disk in your working "
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
        # A voice memo's words are the whole point of the file, so they go in
        # the prompt itself rather than behind a "go read the sidecar" hop.
        # Quoted with a > prefix so a multi-line transcript cannot be mistaken
        # for more list items. Bounded: past ~1500 chars the sidecar file next
        # to the audio has the rest and the agent is told so.
        transcript = (row["transcript"] or "").strip() if "transcript" in row.keys() else ""
        if transcript:
            clipped = transcript[:1500]
            more = (
                f" [...the full transcript is in `{rel_path(row['stored_name'])}.txt`]"
                if len(transcript) > 1500
                else ""
            )
            quoted = "\n".join("  > " + line for line in clipped.splitlines() if line.strip())
            lines.append(f"{quoted}{more}")
        elif wants_transcript(row["mime"]):
            lines.append(
                "  > (voice memo - transcription is still running; it will be "
                "in the next run's prompt)"
            )
    return "\n".join(lines)

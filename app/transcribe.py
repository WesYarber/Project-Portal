"""Voice notes become text an agent can read.

A recorded voice memo lands in the project workspace as `audio/webm` (Android /
desktop Chrome) or `audio/mp4` (an iPhone) - formats no agent can listen to.
This module turns each one into text with whisper.cpp running in a local Docker
container. The image is `portal-whisper:latest`, built once from
`deploy/whisper/Dockerfile`: the upstream ghcr.io/ggml-org/whisper.cpp image
carries the model and the source but its prebuilt binary dies with SIGILL on
this machine's AVX2-only CPU, so the Dockerfile recompiles it in place.

One transcript is stored in two places, each for a different reader:

- `attachments.transcript` in the DB - what the UI and the prompt's
  `## Attachments` section show (see attachments.prompt_section).
- a sidecar file beside the audio in the workspace
  (`attachments/0005-voice-memo.webm.txt`) - what an agent reads in full with
  a plain relative path, exactly the way it reads an image.

Failure is loud, per the house rule: a failed transcription stores
`[transcription failed: ...]` where the text would be, so the Files shelf and
the agent prompt both say so instead of showing silence that looks like an
untranscribed file.

Timing matters for one path: "add & run now" with a voice memo attached must
not build the run's prompt before the transcript exists, or the agent reads
"(transcription in progress)" and misses the entire instruction. `kick()`
therefore takes the run-queueing coroutine as a continuation and awaits it
only after the transcripts land.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from pathlib import Path
from typing import Awaitable, Optional

from app import attachments, config, db

log = logging.getLogger("portal.transcribe")

IMAGE = "portal-whisper:latest"

# Container wall-clock budget. base.en on this box transcribes ~10s of speech
# per second including container start, so 300s covers any voice memo the
# 64 MB upload cap can hold, with a wide margin for a cold disk cache.
TIMEOUT_S = 300

_availability_lock = threading.Lock()
_available: Optional[bool] = None


class TranscribeError(Exception):
    pass


def wants(mime: str) -> bool:
    """See attachments.wants_transcript - one predicate, defined where the
    prompt renderer can reach it without a circular import."""
    return attachments.wants_transcript(mime)


def available() -> bool:
    """True when Docker can run the portal-whisper image.

    A positive answer is cached for the process lifetime; a negative one is
    re-checked on every call (cheap - one `docker image inspect`) so building
    the image starts working without a portal restart.
    """
    global _available
    with _availability_lock:
        if _available:
            return True
        try:
            proc = subprocess.run(
                ["docker", "image", "inspect", IMAGE],
                capture_output=True,
                timeout=15,
            )
            _available = proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _available = False
        if not _available:
            log.warning(
                "Transcription unavailable: %s not found - build it with "
                "`docker build -t %s deploy/whisper/`",
                IMAGE,
                IMAGE,
            )
        return _available


def transcribe_path(path: Path) -> str:
    """Run one audio file through the container; return the transcript text.

    The file is bind-mounted read-only under its own name (the extension is
    ffmpeg's demuxer hint), with no network and a hard memory cap - the
    container gets the bytes and nothing else.
    """
    cmd = [
        "docker", "run", "--rm",
        "--network", "none",
        "--memory", "2g",
        "-v", f"{path}:/audio/{path.name}:ro",
        IMAGE,
        f"/audio/{path.name}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise TranscribeError(f"gave up after {TIMEOUT_S}s") from exc
    except OSError as exc:
        raise TranscribeError(f"docker would not start: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise TranscribeError(detail.splitlines()[-1] if detail else f"exit {proc.returncode}")
    text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    # whisper emits "[BLANK_AUDIO]" (sometimes per segment) for silence; an
    # empty-but-successful run means the memo had no words in it.
    text = " ".join(
        line for line in text.splitlines() if line.strip() and "[BLANK_AUDIO]" not in line
    ).strip()
    if not text:
        return "[no speech detected]"
    return text


def sidecar_path(slug: str, stored_name: str, audio: Optional[Path] = None) -> Path:
    """Where the transcript goes: beside the audio, wherever the audio is.

    An upload waits in config.INCOMING_DIR until a run is ready for it, and the
    transcript has to wait with it - a stray .txt landing in a running agent's
    workspace is the same unexplained-file surprise as the audio landing there,
    and it is the half that has the words in it. `attachments.reveal` moves the
    pair together.

    Falls back to the workspace when the audio cannot be located, which keeps
    the old behavior for a caller that has only a name.
    """
    if audio is not None:
        return audio.parent / attachments.sidecar_name(stored_name)
    return attachments.attachments_dir(slug) / attachments.sidecar_name(stored_name)


def transcribe_attachment(row: dict) -> None:
    """Do the whole job for one attachment row: transcribe, write the sidecar,
    update the DB. Never raises - the failure text IS the loud failure."""
    slug = row.get("project_slug") or ""
    path = attachments.disk_path(slug, row["stored_name"]) if slug else None
    if path is None:
        log.warning("Attachment %s has no file on disk; skipping transcription", row["id"])
        return
    if not available():
        db.set_attachment_transcript(
            row["id"],
            "[transcription failed: the portal-whisper Docker image is not built - "
            "run `docker build -t portal-whisper:latest deploy/whisper/`]",
        )
        return
    try:
        text = transcribe_path(path)
        log.info("Transcribed %s/%s (%d chars)", slug, row["stored_name"], len(text))
    except TranscribeError as exc:
        text = f"[transcription failed: {exc}]"
        log.warning("Transcription of %s/%s failed: %s", slug, row["stored_name"], exc)
    else:
        try:
            sidecar_path(slug, row["stored_name"], path).write_text(
                text + "\n", encoding="utf-8"
            )
        except OSError:
            log.exception("Could not write transcript sidecar for %s", row["stored_name"])
    db.set_attachment_transcript(row["id"], text)


# Tasks hold a reference here so the event loop cannot garbage-collect one
# mid-flight (create_task keeps only a weak set).
_TASKS: set = set()


def kick(attachment_ids: list[int], after: Optional[Awaitable] = None) -> None:
    """Transcribe in the background, then await `after` once the text is in.

    `after` is the continuation - queueing the manual run, waking the project -
    and it runs whether or not transcription succeeded, because a note whose
    memo failed to transcribe is still a note. With no ids it runs immediately.
    """

    async def job() -> None:
        for aid in attachment_ids:
            try:
                row = db.get_attachment(aid)
                if row is not None:
                    await asyncio.to_thread(transcribe_attachment, dict(row))
            except Exception:  # noqa: BLE001 - the continuation must not be eaten
                # transcribe_attachment is contractually no-raise; this is the
                # belt to that suspender, because losing the run this note
                # asked for is strictly worse than losing its transcript.
                log.exception("Transcription of attachment %s blew up", aid)
        if after is not None:
            await after

    task = asyncio.get_running_loop().create_task(job())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def backfill() -> None:
    """Transcribe every audio attachment that predates this feature.

    Called at startup; runs serially on a daemon thread so six old memos do
    not fight each other for cores. The query is idempotent - a row gets a
    transcript exactly once, even a failed one, so a broken engine cannot
    retry itself into a loop at every boot.
    """
    rows = [dict(r) for r in db.attachments_needing_transcript()]
    if not rows:
        return
    if not available():
        return

    def work() -> None:
        log.info("Transcribing %d pre-existing audio attachment(s)", len(rows))
        for row in rows:
            transcribe_attachment(row)

    threading.Thread(target=work, name="transcribe-backfill", daemon=True).start()

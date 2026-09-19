"""The "run now" queue, which now survives the restart that used to erase it.

Every manual run - Wes pressing "run agent", a note that reactivates a
project, an agent kicking off a run on another project - goes through here on
its way to `worker._start_one`. It was an `asyncio.Queue`, and being purely in
memory cost the board nine and a half hours on 2026-09-19.

The deadlock, in one breath: the worker deliberately lets manual runs through
while a self-update's restart is waiting, *because* the queue would not
survive that restart. A queued run for a project whose only busy-ness was a
run paused for the usage window could never start, so `_start_one` put it back
and the queue was never empty, so the restart never fired, so `_tick` returned
before ever reaching `_resume_paused_runs` - which is the one thing that would
have freed the project. Ten projects and one update sat there, at 0% of the
allowance used, until the service was restarted by hand.

Writing the queue through to the database removes the reason for that
exception. The restart now waits on runs actually in flight and nothing else,
which is a condition that always clears, and the run somebody asked for comes
back after the restart instead of being dropped in silence.

The class keeps exactly the `asyncio.Queue` surface the worker used, plus two
things it could not do: it can be read without consuming it, and every change
is persisted. `get()` raises `QueueEmpty` rather than blocking, because every
caller checks `empty()` first and a worker tick that hangs forever is a worse
failure than one that logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Iterable

from app import db

log = logging.getLogger("portal.manualqueue")

# The settings row the queue lives in between processes.
SETTING = "manual_run_queue"


class ManualQueue:
    """Project ids waiting for a manual run, oldest first."""

    def __init__(self) -> None:
        self._queue: deque[int] = deque()
        self._restored = False

    # -- the asyncio.Queue surface, unchanged for every existing caller ----

    async def put(self, project_id: int) -> None:
        self.put_nowait(project_id)

    def put_nowait(self, project_id: int) -> None:
        self._ensure_restored()
        self._queue.append(int(project_id))
        self._persist()

    async def get(self) -> int:
        return self.get_nowait()

    def get_nowait(self) -> int:
        self._ensure_restored()
        try:
            value = self._queue.popleft()
        except IndexError:
            raise asyncio.QueueEmpty from None
        self._persist()
        return value

    def empty(self) -> bool:
        self._ensure_restored()
        return not self._queue

    def qsize(self) -> int:
        self._ensure_restored()
        return len(self._queue)

    # -- what an asyncio.Queue could not do --------------------------------

    def ids(self) -> list[int]:
        """The queue as a list, without consuming it. The dashboard and the
        tick both want to know what is waiting; neither wants to take it."""
        self._ensure_restored()
        return list(self._queue)

    def _ensure_restored(self) -> None:
        """Read the saved queue before this one is touched for the first
        time, whatever touches it first.

        The worker loop calls `restore` explicitly on its way in, and that is
        still where the log line comes from - but "the loop's first statement
        runs before the first HTTP request is served" is an ordering nobody
        should have to rely on, and getting it wrong means the very first
        "run now" of a process silently overwrites everything the previous
        process was holding. So restoring is not a step that can be skipped;
        it is the condition of being usable.
        """
        if not self._restored:
            self.restore()

    def _persist(self) -> None:
        try:
            db.set_setting(SETTING, json.dumps(list(self._queue)))
        except Exception:  # noqa: BLE001 - a queue that cannot be saved still works
            log.exception("Could not persist the manual run queue")

    def restore(self) -> int:
        """Reload what a previous process left behind. Returns how many ids
        came back.

        Called from the top of the worker loop, and by `_ensure_restored`
        for whatever touches the queue first. Idempotent: the second call
        reads a setting this one has already rewritten. Ids are appended
        rather than assigned, and a project that has since been deleted is
        dropped here rather than left to fail quietly in `_pick_project`
        every tick forever.
        """
        already, self._restored = self._restored, True
        if already:
            return 0
        try:
            raw = db.get_setting(SETTING)
        except Exception:  # noqa: BLE001
            log.exception("Could not read the saved manual run queue")
            return 0
        restored = 0
        for project_id in _parse(raw):
            if db.get_project(project_id) is None:
                log.info("Dropping queued manual run for project %s: it is gone", project_id)
                continue
            self._queue.append(project_id)
            restored += 1
        self._persist()
        if restored:
            log.info("Restored %d queued manual run(s) across the restart", restored)
        return restored


def _parse(raw) -> Iterable[int]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("The saved manual run queue is not readable JSON; dropping it")
        return []
    if not isinstance(data, list):
        return []
    out: list[int] = []
    for item in data:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out

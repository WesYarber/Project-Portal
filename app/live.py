"""Change detection for live-updating pages.

The pages used to update themselves the blunt way: `window.location.reload()`
whenever a run started or finished, and not at all for anything else - a note
arriving from the phone, a journal entry landing, a todo ticked from another
tab. Wes asked for pages that refresh in place, without him reloading and
without the view jumping around as content loads in.

The client half of that (app.js) needs one thing from the server: a cheap
answer to "has anything changed since I last looked?". This module is that
answer.

It works off SQLite's `PRAGMA data_version`, which increments - as seen from
one connection - every time a DIFFERENT connection commits a change to the
database file. The whole app writes through the single shared connection in
app.db, so a dedicated observer connection here sees every commit the app
makes, with no instrumentation of any write path. Nothing has to remember to
bump a counter, so nothing can forget to.

The version token is `<boot>:<data_version>`. The boot half is fresh every
process start: when it changes, the client does a full reload rather than a
morph, because a restart means the templates, CSS and JS it is holding may no
longer match the server. The data half changing means "same code, new data" -
safe to patch the page in place.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional, Tuple

from app import config

# Fresh every process start, by design - see the module docstring.
BOOT_ID = uuid.uuid4().hex[:12]

_LOCK = threading.Lock()
_OBSERVER: Optional[sqlite3.Connection] = None
# The path the observer was opened against. Tests repoint config.DB_PATH per
# test; an observer still watching the previous test's file would report a
# version that never changes.
_OBSERVER_PATH: Optional[Path] = None


def _observer() -> sqlite3.Connection:
    global _OBSERVER, _OBSERVER_PATH
    if _OBSERVER is not None and _OBSERVER_PATH == config.DB_PATH:
        return _OBSERVER
    if _OBSERVER is not None:
        _OBSERVER.close()
    # Not opened read-only via URI: the database may not exist yet on first
    # boot (init_db creates it), and a plain connect that never writes is
    # observationally the same thing.
    _OBSERVER = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    _OBSERVER_PATH = config.DB_PATH
    return _OBSERVER


def data_version() -> int:
    with _LOCK:
        row = _observer().execute("PRAGMA data_version").fetchone()
        return int(row[0])


def version_token() -> str:
    return f"{BOOT_ID}:{data_version()}"


def reset() -> None:
    """Test hook: forget the observer so the next call reopens it."""
    global _OBSERVER, _OBSERVER_PATH
    with _LOCK:
        if _OBSERVER is not None:
            _OBSERVER.close()
        _OBSERVER = None
        _OBSERVER_PATH = None

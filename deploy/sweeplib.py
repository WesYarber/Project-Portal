"""Capture and restore for mutation sweeps that must edit the tree in place.

A sweep proves a suite has teeth by editing a line, running the tests, and
putting the line back. Putting it back restores the *bytes*. It does not
restore the *clock*: `write_text` stamps a new mtime even though not one byte
changed, so when the sweep finishes, every file it touched looks freshly
edited to anything reading mtimes. A 67-mutation sweep reads from outside as
67 urgent deploys that never happened - which is exactly what happened on this
estate on 2026-09-21, when three projects each answered a deploy-drift
UNDEPLOYED finding within six minutes of one another, having measured their
own running code, found it current, and traced the finding to a sweep.

The portal's own preferred answer is one level up: sweep an export in /tmp and
never write the checkout at all (see `scripts/sweep_preview_allowlist.py`). This
module is for the sweeps that genuinely edit in place, and it exists because
the naive fix - "remember to call os.utime afterwards" - is a second argument
every call site can forget.

So the pairing is the design: `capture()` reads each file's text AND its clock
together and hands back a mapping that carries the stamps with it, and
`restore()` takes that mapping and is the only thing you call. There is no way
to restore the content without restoring the clock, because there is no
signature that lets you.

    ORIGINAL = capture([APP_JS, TEMPLATE])      # before any mutation
    try:
        ...mutate, run the suite...
    finally:
        restore(ORIGINAL)

`restore()` also belongs in a `finally`, which fixes the older problem that a
sweep killed by a timeout leaves its mutation in the working tree.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable


class Originals(dict):
    """`{path: text}`, carrying each file's `(atime_ns, mtime_ns)` alongside.

    A dict subclass rather than a pair of values so an existing sweep, whose
    restore loop already reads `for path, text in ORIGINAL.items()`, keeps
    working unchanged while `restore()` still has the stamps to put back.
    """

    def __init__(self, *args, stamps: dict[Path, tuple[int, int]] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.stamps: dict[Path, tuple[int, int]] = dict(stamps or {})

    def remember(self, path: Path, *, encoding: str = "utf-8") -> str:
        """Record one more file's text and clock, and return the text.

        For the sweeps that fill their originals as they go (`ORIGINAL[path] =
        path.read_text()` on first touch) rather than up front. Same pairing:
        there is no way to record the text without recording the clock.
        """
        path = Path(path)
        info = path.stat()
        text = path.read_text(encoding=encoding)
        self[path] = text
        self.stamps[path] = (info.st_atime_ns, info.st_mtime_ns)
        return text


def capture(paths: Iterable[Path], *, encoding: str = "utf-8") -> Originals:
    """Read text and clock together, BEFORE the first mutation."""
    originals = Originals()
    for path in paths:
        originals.remember(Path(path), encoding=encoding)
    return originals


def restore(originals: Originals, *, encoding: str = "utf-8") -> None:
    """Put the bytes back, then the clock - in that order, per file.

    The clock goes LAST because writing the content is itself what moved the
    mtime, and so does clearing `__pycache__`: a stale `.pyc` beside a restored
    `.py` is its own class of ghost, and removing it touches the directory, not
    the file, but the ordering rule is the same one either way.
    """
    for path, text in originals.items():
        path = Path(path)
        if not path.exists() or path.read_text(encoding=encoding) != text:
            path.write_text(text, encoding=encoding)
        shutil.rmtree(path.parent / "__pycache__", ignore_errors=True)
        stamp = originals.stamps.get(path)
        if stamp is not None:
            os.utime(path, ns=stamp)

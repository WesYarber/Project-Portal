"""How much of a project's journal one page render is allowed to carry.

Wes, 2026-08-28: "The slowness and the not scrolling back down happening on page
reload is a big problem. It also seems to take much longer to reload a page, and
my key commands to jump to a certain area of the page do not work when it is
taking this time to load."

Measured before writing any of this: the project page for the portal's own
meta-project was **596 KB of HTML**, and 490 KB of that - four fifths - was the
journal. `db.list_journal(limit=200)` fetched two hundred entries and the
template rendered every one of them, markdown and all, on every single load.
That is the "much longer to reload", and it is also why his jump keys do nothing
while it happens: they are bound at DOMContentLoaded, which cannot fire until
the document has been parsed.

So the page carries a WINDOW onto the journal, and a link asks for the rest.

The budget is in characters rather than entries alone, because entry sizes here
span three orders of magnitude - a one-line note next to a 12 KB agent report -
and a count would leave the page weight swinging by 10x between two projects
that look equally busy. The entry count is the second cap, so a project with
hundreds of one-line notes does not render them all just because they are small.

`min_entries` is the floor that outranks the character budget: a single entry
longer than the whole budget must still be shown, or a project whose newest
report is a long one would open on an empty journal and a "show older" link,
which reads as the journal being broken rather than as it being long.
"""
from __future__ import annotations

from typing import Any, Sequence

# ~120 KB of markdown, which renders to roughly 150 KB of HTML - about a
# quarter of what the page used to weigh, and still more journal than fits on
# any screen. Both caps were chosen against real measurements of this install
# (see tests/test_journal_window.py, which pins the arithmetic, not the taste).
MAX_CHARS = 120_000

# The second cap: many small entries. Thirty is comfortably more than the
# "since I last looked" window anyone actually reads in one sitting.
MAX_ENTRIES = 30

# The floor that beats MAX_CHARS. Five rather than one so that a run of long
# agent reports still gives you the shape of what has been happening.
MIN_ENTRIES = 5


def window(
    entries: Sequence[Any],
    *,
    show_all: bool = False,
    max_entries: int = MAX_ENTRIES,
    max_chars: int = MAX_CHARS,
    min_entries: int = MIN_ENTRIES,
) -> tuple[list[Any], int]:
    """Split `entries` into the slice to render and the count left out.

    `entries` arrive newest-first, the way `db.list_journal` returns them, and
    the window is taken from the front - the newest entries are the ones worth
    the page weight.

    Returns ``(shown, hidden)``. ``hidden`` is a count rather than the rows
    themselves: the only thing the page does with it is say how many more there
    are, and holding the rows would keep the very objects this exists to avoid
    rendering.
    """
    rows = list(entries)
    if show_all:
        return rows, 0

    used = 0
    shown: list[Any] = []
    for row in rows:
        # The entry cap is checked before the character cap so that it applies
        # even to entries so short the budget never notices them.
        if len(shown) >= max_entries:
            break
        size = _size(row)
        # Below the floor, an entry goes in whatever it costs. At or above it,
        # an entry that would push us past the budget ends the window - and it
        # ends the window rather than being skipped, because the journal is a
        # timeline and a hole in the middle of one is a lie.
        if len(shown) >= min_entries and used + size > max_chars:
            break
        shown.append(row)
        used += size
    return shown, len(rows) - len(shown)


def _size(row: Any) -> int:
    """The rendering cost of one entry, as the length of its markdown.

    A sqlite3.Row raises rather than returning None for a column it does not
    have, and this is also handed plain dicts by the tests, so the read is
    guarded in both shapes. An entry whose size cannot be read counts as zero:
    the caps exist to bound a page, and refusing to show a row because its
    length was unreadable would be a worse failure than showing it.
    """
    try:
        content = row["content_md"]
    except (KeyError, IndexError, TypeError):
        return 0
    return len(content or "")

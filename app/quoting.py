"""Highlight a passage in the journal, then ask about it or note about it.

Wes, 2026-07-25: *"When highlighting text in the journal, allow me to ask a
question or make a new note with reference to that text. Asking a question
being the mode where it is sort of asked in parallel and not factored into the
rest of the journal context and is not allowed to code and whatnot."*

Two things fall out of that.

**The reference.** Selecting text and pressing a button has to carry the words
themselves, not a journal-entry id: a report is long, and "about entry 412"
still leaves the reader hunting for the sentence. So the selection travels as a
`quote` form field alongside whatever Wes types, and this module is the single
place that turns the pair into the one string that lands in the journal and in
the model's prompt. Both the note route and the ask route call `frame()`, so
the two modes can never drift into quoting differently.

**The parallel mode.** The second half of his sentence describes what an *ask*
is, and the portal already had two thirds of it: `app/ask.py` is read-only by
construction and never writes a run. The missing third was context. An ask and
its answer used to land in the journal tail every run read, so a passing "why
did you pick that?" became a permanent line in every future prompt. They are
now a side thread: still journalled, still shown, but skipped when a run's
prompt is built (`db.SIDE_THREAD` / `db.list_journal_asc(exclude=...)`).
`ask.build_prompt` deliberately does NOT skip them, so the side thread has its
own continuity - a follow-up ask sees the earlier ones, a run does not.
"""
from __future__ import annotations

import re

# Long enough for a paragraph of a report, short enough that a stray
# select-all cannot paste a whole run's journal entry into a prompt.
MAX_QUOTE_CHARS = 700

# The line under the quote. It is here rather than in the two routes so the
# journal reads identically whichever button was pressed, and so a reader (or a
# model) can always tell a quoted passage from Wes's own words.
QUOTE_CAPTION = "_(highlighted in the journal)_"

_BLANK_RUN = re.compile(r"\n{3,}")


def normalize(raw: str) -> str:
    """Tidy a raw browser selection into something worth quoting.

    A selection dragged across rendered markdown arrives with the page's own
    line wrapping, hard tabs and long blank runs where a code block or an image
    used to be. None of that is meaning, and all of it costs prompt space.
    """
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", " ")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANK_RUN.sub("\n\n", text).strip()
    if len(text) <= MAX_QUOTE_CHARS:
        return text
    # Cut at the last word boundary inside the budget so the quote does not end
    # mid-word; fall back to a hard cut for text with no spaces in it at all.
    cut = text[:MAX_QUOTE_CHARS]
    space = cut.rfind(" ")
    if space > MAX_QUOTE_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip() + " ..."


def as_blockquote(quote: str) -> str:
    """Markdown blockquote. Blank lines keep their `>` so the quote stays one
    block rather than splitting into two at the first paragraph break."""
    if not quote:
        return ""
    return "\n".join(f"> {line}".rstrip() for line in quote.split("\n"))


def frame(quote: str, body: str) -> str:
    """The text that gets journalled (and prompted) for a quoted note or ask.

    Returns the body unchanged when there is no quote, which is what makes this
    safe to call unconditionally from routes that are usually reached from a
    plain form with no selection at all.
    """
    body = (body or "").strip()
    cleaned = normalize(quote)
    if not cleaned:
        return body
    parts = [as_blockquote(cleaned), QUOTE_CAPTION]
    if body:
        parts.append(body)
    return "\n\n".join(parts)

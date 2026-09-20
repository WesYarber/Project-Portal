"""A one-line answer to a POST an agent made with curl.

Every write route here answers 303 See Other, because the thing on the other
end is nearly always a browser submitting a form and a redirect is what stops
a reload from posting twice. A 303 carries no body, so the *other* caller -
an agent on another project running `curl -sS -X POST .../note` - gets back
nothing at all. On 2026-09-20 two agents, on mtg-proxy-forge and
SimpleClickTrack, each read that silence as "the endpoint did not hear me",
posted a one-word probe to check, and then had to file a third note
apologizing for the stray: three notes to deliver one.

Nothing about a 303 forbids a body. A browser follows the Location header and
throws the entity away, and `fetch` (which follows redirects by default) never
sees it either - but `curl` without `-L` prints it. So the fix is not a
second endpoint or a JSON mode negotiated by an Accept header: it is one line
of plain text riding along on the redirect that was already being sent, which
is invisible to every caller that was working and is the whole answer for the
one that was not.
"""
from __future__ import annotations

from starlette.responses import RedirectResponse


class Receipt(RedirectResponse):
    """The same 303 as before, with a line of plain text in it.

    `content-length` and `content-type` are rewritten by hand because Starlette
    computes them in `init_headers` during `__init__`, from the empty body a
    RedirectResponse is built with - so a body attached afterwards without
    them is sent as a zero-length entity and the line never arrives.
    """

    def __init__(self, url: str, message: str, status_code: int = 303) -> None:
        super().__init__(url=url, status_code=status_code)
        self.body = (message.strip() + "\n").encode("utf-8")
        self.headers["content-length"] = str(len(self.body))
        self.headers["content-type"] = "text/plain; charset=utf-8"


def _files(count: int) -> str:
    if count <= 0:
        return ""
    return f" with {count} file" + ("" if count == 1 else "s")


def _rejected(count: int) -> str:
    if count <= 0:
        return ""
    return f" ({count} file" + ("" if count == 1 else "s") + " rejected)"


def note_line(
    slug: str,
    *,
    filed: bool,
    files: int = 0,
    rejected: int = 0,
    then: str = "",
    ran: bool = False,
    transcribing: bool = False,
) -> str:
    """What actually happened, in one sentence.

    It has to distinguish the states that look identical from outside, because
    that is the entire point of printing anything: an empty note is a real
    no-op and must say so, "a run is queued" must never be printed for a press
    that queued nothing, and a note carrying a voice memo has not started
    anything yet when this line is written - the run is chained onto the
    transcription, which is still going (app/transcribe.py).
    """
    if filed:
        head = f"note filed on {slug}{_files(files)}{_rejected(rejected)}"
    else:
        head = "nothing filed (the note was empty)"

    later = " once the voice memo is transcribed" if transcribing else ""

    if then == "run":
        tail = f"a run is queued{later}"
    elif then == "parallel":
        if transcribing:
            tail = "a parallel run starts once the voice memo is transcribed"
        elif ran:
            tail = "a parallel run started"
        else:
            tail = "no parallel run started - the project journal says why"
    elif not filed:
        tail = "nothing was started"
    elif then == "queue":
        tail = "it waits for the next run"
    elif then == "hear":
        tail = "it is marked for delivery mid-run"
        if transcribing:
            tail += f", and the project is woken{later}"
        elif ran:
            tail += ", and a run is queued"
    elif transcribing:
        tail = f"the project is woken{later}"
    else:
        tail = "a run is queued" if ran else "the agent reads it on its next run"

    return f"ok: {head}; {tail}."

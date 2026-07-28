"""How a workspace file should be shown in the browser.

The viewer used to do one thing: read the bytes, refuse anything with a NUL in
it, and dump the text into a <pre>. That is fine for the source files an agent
writes and useless for everything else it produces - a screenshot, a recorded
clip, a PDF spec, a README whose whole point is that it is formatted.

This module answers two questions and nothing else:

  * `describe()` - what kind of thing is this, and how should the page render
    it (image, audio, video, pdf, markdown, highlighted text, or nothing)?
  * `inline_media_type()` - is it safe to serve these bytes to a browser
    *inline*, on the portal's own origin?

The second question is the reason this is a whitelist rather than a call to
mimetypes.guess_type(). Workspace files are written by agents; serving one
inline under a type the browser will execute (text/html, image/svg+xml, and
anything it might sniff into one) would be script execution on the portal's
origin, with the portal's cookies and its unauthenticated LAN pages. So only
media types that browsers render as *content* are served inline, and even those
go out with nosniff and a sandbox CSP. Everything else stays on the download
route, which is attachment-only under application/octet-stream.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.util import ClassNotFound

# Beyond this a text file is not read into memory or highlighted; the page
# offers the download link instead. Media files are never read by the portal
# at all - the browser streams them off the raw route - so the cap does not
# apply to them.
MAX_TEXT_BYTES = 500 * 1024

# Types a browser renders as content rather than as a document that can script.
# Deliberately NOT here: image/svg+xml (an SVG can carry <script>), text/html,
# and anything application/* other than pdf.
INLINE_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/avif",
    "image/bmp",
    "image/x-icon",
    "image/vnd.microsoft.icon",
    "image/tiff",
}
INLINE_AUDIO_TYPES = {
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "audio/ogg",
    "audio/wav",
    "audio/x-wav",
    "audio/webm",
    "audio/flac",
    "audio/x-flac",
}
INLINE_VIDEO_TYPES = {
    "video/mp4",
    "video/webm",
    "video/ogg",
    "video/quicktime",
}
INLINE_DOC_TYPES = {"application/pdf"}

MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd"}

# The pygments class prefix; the matching colors live in style.css under
# `.highlight` so the file viewer is themed with the rest of the portal rather
# than by an injected <style> block.
CSS_CLASS = "highlight"


@dataclass(frozen=True)
class FileView:
    """Everything the template needs to draw one workspace file."""

    kind: str  # image | audio | video | pdf | markdown | text | binary
    mime: str
    size: int
    text: Optional[str] = None  # raw source, for markdown and text
    html: Optional[str] = None  # rendered/highlighted body
    language: Optional[str] = None  # pygments lexer name, for the header
    reason: Optional[str] = None  # why there is nothing to show, if kind is binary

    @property
    def is_media(self) -> bool:
        return self.kind in {"image", "audio", "video", "pdf"}


def guess_mime(path: Path) -> str:
    """The declared type of a workspace file, from its name only.

    Content sniffing is deliberately absent: the name is what the download
    route already trusts, and a guess made from the bytes would be a second,
    disagreeing opinion about a file the user is about to be served.
    """
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def inline_media_type(path: Path) -> Optional[str]:
    """The content type this file may be served inline as, or None.

    None is not an error - it means "download it instead", which is what the
    /download route has always done for everything.
    """
    mime = guess_mime(path)
    if (
        mime in INLINE_IMAGE_TYPES
        or mime in INLINE_AUDIO_TYPES
        or mime in INLINE_VIDEO_TYPES
        or mime in INLINE_DOC_TYPES
    ):
        return mime
    return None


def media_kind(mime: str) -> Optional[str]:
    if mime in INLINE_IMAGE_TYPES:
        return "image"
    if mime in INLINE_AUDIO_TYPES:
        return "audio"
    if mime in INLINE_VIDEO_TYPES:
        return "video"
    if mime in INLINE_DOC_TYPES:
        return "pdf"
    return None


def highlight_text(path: Path, text: str) -> tuple[str, str]:
    """Return (html, language-name) for a text file.

    Lexing is by filename only. `guess_lexer` on the *content* is the obvious
    alternative and it is worse here: it happily calls a short config file
    Perl, and a plausible-but-wrong language colors the file misleadingly
    rather than not at all. An unknown extension falls back to plain text,
    which still gets the escaping and the styled block.
    """
    try:
        lexer = get_lexer_for_filename(path.name, text)
    except ClassNotFound:
        lexer = TextLexer()
    formatter = HtmlFormatter(cssclass=CSS_CLASS, nowrap=False)
    return highlight(text, lexer, formatter), lexer.name


def describe(path: Path, *, size: Optional[int] = None) -> FileView:
    """Decide how to present one workspace file.

    Never raises on content: a file the viewer cannot show comes back as
    kind="binary" with a reason, because a 415 on a click from the file list
    is a dead end, and "this is a binary, here is the download link" is not.
    """
    if size is None:
        size = path.stat().st_size

    mime = guess_mime(path)
    kind = media_kind(mime)
    if kind:
        # The bytes are never loaded here - the <img>/<audio>/<video>/<iframe>
        # fetches them from the raw route, so a 40MB video costs this request
        # nothing.
        return FileView(kind=kind, mime=mime, size=size)

    if size > MAX_TEXT_BYTES:
        return FileView(
            kind="binary",
            mime=mime,
            size=size,
            reason=f"too large to display ({size:,} bytes) - download it instead",
        )

    raw = path.read_bytes()
    if b"\x00" in raw:
        return FileView(
            kind="binary", mime=mime, size=size, reason="binary file - download it instead"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return FileView(
            kind="binary", mime=mime, size=size, reason="not valid UTF-8 text - download it instead"
        )

    if path.suffix.lower() in MARKDOWN_SUFFIXES:
        # The rendered HTML is filled in by the caller, which owns the markdown
        # renderer (and its extension set) for the whole app. Keeping it there
        # means the file viewer and the journal render the same markdown the
        # same way rather than drifting apart.
        return FileView(kind="markdown", mime="text/markdown", size=size, text=text)

    html, language = highlight_text(path, text)
    return FileView(kind="text", mime=mime or "text/plain", size=size, text=text,
                    html=html, language=language)


def pygments_css() -> str:
    """The stylesheet pygments would generate for the highlighted block."""
    return HtmlFormatter(cssclass=CSS_CLASS, style="native").get_style_defs(f".{CSS_CLASS}")


def pygments_rules() -> list[str]:
    """Just the `.highlight ...` rules, which is what style.css carries.

    get_style_defs() also emits a bare `pre { line-height: 125% }` and a set of
    `span.linenos` rules. Those are unscoped: pasted into the portal's
    stylesheet they would restyle the agent console and every other <pre> on
    the site. Only the scoped rules are shipped.

    Nothing calls this at runtime - it exists so the checked-in CSS can be
    regenerated, and so a test can assert the two have not drifted apart.
    """
    return [line for line in pygments_css().splitlines() if line.startswith(f".{CSS_CLASS}")]

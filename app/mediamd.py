"""Resolve workspace media inside rendered agent markdown.

Wes's ask, in his words: "Make it where agents can and do use/include images,
videos, gifs in their replies when relevant."

The *can* half lives here. An agent writing its journal entry knows its media
as workspace-relative paths - `![the dashboard](shots/dashboard.png)` - because
the workspace is the only filesystem it has. The journal is read on the portal,
where that relative path resolves against the page URL and 404s. This module
post-processes the rendered HTML so a relative `src` points at the route that
already serves workspace files inline (`/raw/<slug>/<path>`, which whitelists
media types and serves them under a sandbox CSP - see `raw_file` in main.py).

It rewrites rendered HTML rather than the markdown source because the journal
stores what the agent wrote: the entry must keep reading correctly if the
serving route ever moves, and the file viewer must keep showing the same
markdown source the agent committed.

Markdown has an image syntax but no video syntax, so agents are told to write
videos as images too - `![demo](demo.mp4)` - and the suffix decides here
whether the tag becomes an `<img>` or a `<video controls>`. Gifs are just
images and animate on their own.

The *do* half is a paragraph in the agent contract (agent_runner.py) telling
agents the syntax exists and to use it.
"""
from __future__ import annotations

import re
from typing import Callable, Optional
from urllib.parse import quote

# Suffixes that turn an image reference into a player. These mirror
# fileview's INLINE_* whitelists - a suffix outside those sets would resolve
# to a URL the /raw route refuses to serve inline anyway.
VIDEO_SUFFIXES = (".mp4", ".webm", ".ogv", ".mov", ".m4v")
AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac")

_IMG_TAG = re.compile(r"<img\b[^>]*?/?>", re.IGNORECASE)
# Raw <video>/<audio>/<source> written directly in an entry: only their src
# needs resolving, the agent already chose the element.
_PLAYER_SRC = re.compile(
    r'(<(?:video|audio|source)\b[^>]*?\bsrc=")([^"]*)(")', re.IGNORECASE
)
_SRC_ATTR = re.compile(r'\bsrc="([^"]*)"', re.IGNORECASE)
_ALT_ATTR = re.compile(r'\balt="([^"]*)"', re.IGNORECASE)
# "am I already inside a link?" - the markdown for a linked image
# ([![alt](img)](target)) renders as <a ...><img ...></a>, and wrapping the
# img in a second anchor would nest anchors, which HTML forbids.
_OPEN_ANCHOR_BEFORE = re.compile(r"<a\s[^>]*>\s*$", re.IGNORECASE)

_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def resolve_src(src: str, raw_base: Optional[str]) -> str:
    """A media `src` as the journal page should fetch it.

    Absolute URLs, root-relative paths, data: URIs and fragments pass through
    untouched; a workspace-relative path is rooted at `raw_base` with each
    segment percent-quoted (an agent's screenshot name can contain spaces).
    A path trying to climb out with `..` is left alone - the /raw route would
    refuse it, and rewriting it would only dress the refusal up as a portal
    URL.
    """
    src = (src or "").strip()
    if not raw_base or not src:
        return src
    if src.startswith(("/", "#")) or _SCHEME.match(src):
        return src
    while src.startswith("./"):
        src = src[2:]
    segments = [s for s in src.split("/") if s not in ("", ".")]
    if not segments or ".." in segments:
        return src
    return raw_base.rstrip("/") + "/" + "/".join(quote(s) for s in segments)


def _suffix(src: str) -> str:
    path = src.split("?", 1)[0].split("#", 1)[0]
    dot = path.rfind(".")
    return path[dot:].lower() if dot != -1 else ""


def resolve_media(html: str, raw_base: Optional[str]) -> str:
    """Rewrite rendered-markdown HTML so its media loads from the workspace.

    Every `<img>` gets its relative src resolved; one pointing at a video or
    audio file (markdown's image syntax is the only embed syntax an agent has)
    becomes the matching player element; a plain image additionally becomes a
    link to itself, full size in a new tab, unless the entry already wrapped
    it in a link. Raw <video>/<audio>/<source> tags keep their element and
    just get the src resolved.
    """
    if not html:
        return html

    def _rewrite_player(match: re.Match) -> str:
        return match.group(1) + resolve_src(match.group(2), raw_base) + match.group(3)

    html = _PLAYER_SRC.sub(_rewrite_player, html)

    out: list[str] = []
    last = 0
    for match in _IMG_TAG.finditer(html):
        out.append(html[last:match.start()])
        last = match.end()
        tag = match.group(0)
        src_match = _SRC_ATTR.search(tag)
        if not src_match:
            out.append(tag)
            continue
        src = resolve_src(src_match.group(1), raw_base)
        suffix = _suffix(src)
        if suffix in VIDEO_SUFFIXES:
            out.append(
                f'<video class="journal-media" controls preload="metadata" '
                f'src="{src}"></video>'
            )
            continue
        if suffix in AUDIO_SUFFIXES:
            out.append(f'<audio class="journal-media" controls src="{src}"></audio>')
            continue
        alt_match = _ALT_ATTR.search(tag)
        alt = alt_match.group(1) if alt_match else ""
        img = (
            f'<img class="journal-media" src="{src}" alt="{alt}" loading="lazy">'
        )
        preceding = "".join(out)
        if _OPEN_ANCHOR_BEFORE.search(preceding):
            out.append(img)
        else:
            # data-lightbox marks this as a portal-generated self-link, which
            # app.js intercepts to open a zoomable in-page viewer instead of
            # navigating away (Wes's ask). The href/target stay so a
            # cmd/middle-click still opens the raw image in a new tab, and so
            # the image is reachable with JS off.
            out.append(
                f'<a class="journal-media-link" data-lightbox href="{src}" '
                f'target="_blank" rel="noopener">{img}</a>'
            )
    out.append(html[last:])
    return "".join(out)


# Wes, 2026-10-13: "I want to allow the journal, when it references files, to
# hyperlink to them where I can click the link to open or download the file."
#
# Two shapes of reference, both resolved to the file page (`/file/<slug>/...`),
# which renders the file and carries its download link - so one click opens it
# and the page it lands on downloads it:
#
#   * a markdown link with a workspace-relative target, `[the plan](PLAN.md)`,
#     which otherwise resolves against the journal page's own URL and 404s;
#   * a path in a code span, `` `attachments/0154-SPEC.md` ``, which is how
#     agents name files nearly every time. Only a span naming a file that
#     really exists in the workspace becomes a link: an entry on this project
#     names source files outside its workspace all the time, and a link that
#     opens "File not found" is worse than plain text.
_ANCHOR_HREF = re.compile(r'(<a\b[^>]*?\bhref=")([^"]*)(")', re.IGNORECASE)
_CODE_SPAN = re.compile(r"<code>([^<]{1,240})</code>")
# Path-shaped: no spaces, no entities, and either a directory separator or a
# file extension. A trailing `:12` or `:12-40` line reference is allowed and
# kept in the text, but the link is to the file.
_PATHISH = re.compile(r"^([\w.~+-]+(?:/[\w.~+-]+)*)(?::\d+(?:-\d+)?)?$")
_PRE_BEFORE = re.compile(r"<pre[^>]*>\s*$", re.IGNORECASE)


def _workspace_path(text: str) -> Optional[str]:
    """`text` as a workspace-relative file path, or None if it is not one."""
    match = _PATHISH.match(text.strip())
    if not match:
        return None
    path = match.group(1)
    segments = path.split("/")
    if ".." in segments or "." in segments:
        return None
    if "/" not in path and "." not in path.strip("."):
        return None
    return path


def resolve_links(
    html: str, file_base: Optional[str], exists: Optional[Callable[[str], bool]] = None
) -> str:
    """Point the workspace files a rendered entry names at the file page.

    `file_base` is the viewer route for this workspace (`/file/<slug>`); with
    none the HTML passes through untouched. `exists(path)` decides whether a
    code span names a real file; without it code spans are left alone and only
    explicit markdown links are rewritten.
    """
    if not html or not file_base:
        return html

    def _rewrite_href(match: re.Match) -> str:
        return match.group(1) + resolve_src(match.group(2), file_base) + match.group(3)

    # resolve_src passes absolute, rooted and fragment hrefs through, which
    # includes the /raw links the media pass already produced.
    html = _ANCHOR_HREF.sub(_rewrite_href, html)
    if exists is None:
        return html

    out: list[str] = []
    last = 0
    for match in _CODE_SPAN.finditer(html):
        out.append(html[last:match.start()])
        last = match.end()
        span = match.group(0)
        path = _workspace_path(match.group(1))
        # The tag just before the span: `<pre>` is a fenced block, an open
        # `<a>` means the agent already linked it ([`x`](url)).
        preceding = html[max(0, match.start() - 400):match.start()]
        if (
            path is None
            or _PRE_BEFORE.search(preceding)
            or _OPEN_ANCHOR_BEFORE.search(preceding)
            or not exists(path)
        ):
            out.append(span)
            continue
        href = resolve_src(path, file_base)
        out.append(f'<a class="journal-file" href="{href}">{span}</a>')
    out.append(html[last:])
    return "".join(out)

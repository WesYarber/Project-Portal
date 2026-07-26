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
from typing import Optional
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

"""Serving `/static/vendor/` - third-party assets this install hosts itself.

Why there is a vendor directory at all: a `<link>` to fonts.googleapis.com
hands Google the visitor's IP, user agent and - in the `Referer` - the exact
page they opened, *before* the page draws; it makes Google's outage this
page's outage, because a stylesheet is render-blocking; and it forces
`style-src https://fonts.googleapis.com` and `font-src https://fonts.gstatic.com`
into any Content-Security-Policy the app wants. Serving the bytes ourselves
costs 140 KB in the tree and removes all three.

Why this module exists: a vendored asset is only cheap if the browser fetches
it once. The deal is that a vendor directory carries its upstream version in
its NAME - `fira-code-v27`, not `fira-code` - so its contents can never change
under a given URL, and an upgrade is a new directory rather than an edit. That
makes `immutable` honest, and `immutable` is what stops a revalidation request
per page load for the life of the cache entry.

The two halves of that deal are enforced here rather than trusted: a path
under `vendor/` gets the year-long header only if its directory name is
version-pinned, so dropping an unversioned `vendor/foo/bar.js` in gets
ordinary caching instead of being frozen into every visitor's browser for a
year with no way to recall it.
"""

from __future__ import annotations

import os
import re
from pathlib import PurePath
from typing import Any

from starlette.responses import Response
from starlette.staticfiles import StaticFiles

# A year, which is the maximum RFC 9111 suggests anyone bother with, plus
# `immutable` so a reload does not send a conditional request either.
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"

# The directory whose children are version-pinned, relative to the static root.
VENDOR_DIR = "vendor"

# `fira-code-v27`, `pdfjs-v4.10.38`: a trailing `-v` plus digits and dots. The
# anchor at the end is the point - `v27-scratch` is somebody's working copy,
# not a released version, and must not be frozen for a year.
_VERSION_PINNED = re.compile(r"-v\d+(\.\d+)*$")


def is_version_pinned(rel_path: str | os.PathLike[str]) -> bool:
    """Whether `rel_path` (relative to the static root) may be cached forever.

    True only for a file *inside* a version-named directory *inside* `vendor/`:
    `vendor/fira-code-v27/font.css` yes, `vendor/fira-code/font.css` no,
    `vendor/anything.css` no (a bare file there has no name to pin it), and
    `terminal-theme.css` no - the portal's own stylesheets change in place and
    are versioned by the `?v=<mtime>` `static_url` puts on them.
    """
    parts = PurePath(rel_path).parts
    if len(parts) < 3 or parts[0] != VENDOR_DIR:
        return False
    return bool(_VERSION_PINNED.search(parts[1]))


class VersionedStatic(StaticFiles):
    """`StaticFiles` that caches version-pinned vendor assets for a year.

    Everything else it serves is untouched, so the portal's own CSS and JS keep
    Starlette's default (an ETag and a revalidation), which is what lets a
    self-modifying run change a stylesheet and have the next reload show it.
    """

    def file_response(
        self,
        full_path: Any,
        stat_result: os.stat_result,
        scope: Any,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        # Set on the 304 as well as the 200: a `NotModifiedResponse` is the
        # browser's chance to learn it never needed to ask again.
        if is_version_pinned(self.get_path(scope)):
            response.headers["cache-control"] = IMMUTABLE_CACHE_CONTROL
        return response

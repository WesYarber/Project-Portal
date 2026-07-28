"""One standard place to click to see what a project actually built.

Wes's ask, in his words:

    "When a project is building a web page or serving a web page somewhere,
    add a place to click to launch/build/view the web page. Whatever needs to
    be done to view the tool or whatever it is. Should make it easy and
    standardized for opening what was built."

Most of his projects are dependency-free web things - a page and some JS in the
workspace - so the portal can simply serve them, and the answer to "how do I
look at SimpleClickTrack" stops being "ssh in, start a server, work out the
port". The workspace is scanned for an `index.html`, and if there is one the
project page grows an **open it** button.

Two things are deliberate and neither is obvious.

**It is served on its own port, not on the portal's.** Everything the portal
already serves out of a workspace it serves as an attachment or under a sandbox
CSP, with a comment saying why: these files are written by agents, and running
them on the portal's origin is script execution on the portal's origin - the
same origin that has an unauthenticated POST endpoint for deleting a project.
A sandbox CSP is not usable here, because it also gives the document an opaque
origin and so takes away `localStorage`, which every one of Wes's little web
apps uses to keep its state. A second port is a second origin, which buys the
isolation without breaking the apps.

**An explicit URL always wins.** A project that runs a real server (a Bun room
server, a thermal-printer daemon) cannot be served as static files, so
`projects.preview_url` overrides the scan. Wes can type it, and an agent can
set it from its report - it is the agent who knows which port it just bound.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import quote

from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.staticfiles import StaticFiles

from app import config, db

log = logging.getLogger(__name__)

# Where a built page tends to live, in the order they are tried. The workspace
# root comes first because Wes's projects are mostly a bare index.html beside
# the code; the rest are the conventional output directories, checked one level
# down only. Nothing recurses: a match six directories deep is more likely to
# be a fixture or a node_modules stowaway than the thing he wants to look at.
WEB_ROOT_CANDIDATES = ("", "dist", "build", "public", "www", "site", "web", "docs", "src")

# Directories that never count as a web root even if one of the names above
# matches inside them.
_SKIP = {".git", "node_modules", ".portal", ".claude", "venv", "__pycache__"}


def web_root(slug: str) -> Optional[str]:
    """The directory inside this project's workspace holding an `index.html`.

    Returned relative to the workspace and POSIX-separated (`""` for the root),
    or None if the project has no page to serve.
    """
    workspace = config.PROJECTS_DIR / slug
    for rel in WEB_ROOT_CANDIDATES:
        if rel in _SKIP:
            continue
        candidate = workspace / rel if rel else workspace
        try:
            if (candidate / "index.html").is_file():
                return rel
        except OSError:  # pragma: no cover - unreadable workspace
            continue
    return None


def has_page(slug: str) -> bool:
    return web_root(slug) is not None


def base_url(scheme: str, host: str) -> str:
    """The preview server's address, derived from the page you are reading.

    The portal is typically reachable at more than one address - a LAN IP or
    hostname, and an HTTPS name over a tailnet - so a hardcoded base would
    hand a phone a link to an address it cannot reach. The host is carried
    across from the current request and only the port changes, which keeps a
    link working wherever it was rendered. The site config's host is only the
    fallback, for when the request carried no host at all.
    """
    host = (host or "").split(":")[0] or config.HOST_LABEL
    if scheme == "https":
        return f"https://{host}:{config.PREVIEW_HTTPS_PORT}"
    return f"http://{host}:{config.PREVIEW_PORT}"


def link_for(project: Any, scheme: str = "http", host: str = "") -> Optional[dict]:
    """Where to click to see this project's thing, or None if there is nothing.

    Returns `{"url": ..., "hint": ...}`. The hint says *what* is about to open,
    because "open it" pointing at a static copy of a page and "open it"
    pointing at a server the project runs are different promises - the second
    one is only there while that server is up.
    """
    if project is None:
        return None
    explicit = ""
    try:
        explicit = (project["preview_url"] or "").strip()
    except (IndexError, KeyError):  # row read before the column existed
        explicit = ""
    if explicit:
        return {"url": explicit, "hint": "the address this project serves on"}

    slug = project["slug"]
    if not has_page(slug):
        return None
    return {
        "url": f"{base_url(scheme, host)}/{quote(slug)}/",
        "hint": "the page in this workspace, served read-only by the portal",
    }


# --------------------------------------------------------------------------
# The static server
# --------------------------------------------------------------------------

_MOUNTS: dict[str, StaticFiles] = {}


def _mount_for(slug: str) -> Optional[StaticFiles]:
    """A StaticFiles rooted at one project's web root.

    Cached, because a mount is re-created on every request otherwise, but keyed
    on the resolved directory so a project whose web root moves (or whose
    workspace is renamed out from under it) is not served from the old one.
    """
    rel = web_root(slug)
    if rel is None:
        return None
    directory = (config.PROJECTS_DIR / slug / rel).resolve()
    key = f"{slug}:{directory}"
    mount = _MOUNTS.get(key)
    if mount is None:
        mount = StaticFiles(directory=str(directory), html=True)
        _MOUNTS[key] = mount
    return mount


_INDEX = """<!doctype html><meta charset="utf-8"><title>portal previews</title>
<body style="font:14px ui-monospace,monospace;background:#0b1020;color:#cbd5e1;padding:2rem">
<p>This is Project Portal's preview server. It serves the built page of one
project at <code>/&lt;slug&gt;/</code>.</p>
<p>It is a separate origin from the portal on purpose, so a page written by an
agent cannot reach the portal's own endpoints. Go back to
<a style="color:#38bdf8" href="/">the portal</a> to pick a project.</p>
"""


async def preview_asgi(scope, receive, send) -> None:
    """`/<slug>/<path>` -> that project's web root.

    A plain ASGI callable rather than a mounted Starlette app because the set of
    projects changes while the process is running: mounting them at startup
    would mean a project built today is 404 until the next restart.
    """
    if scope["type"] != "http":  # pragma: no cover - no websockets here
        return
    path = scope["path"].lstrip("/")
    slug, _, rest = path.partition("/")
    if not slug:
        await HTMLResponse(_INDEX)(scope, receive, send)
        return
    # A slug is the folder name, so anything that is not one cannot address a
    # project - and refusing here means the traversal never reaches a mount.
    if not db.SLUG_RE.fullmatch(slug):
        await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
        return
    mount = _mount_for(slug)
    if mount is None:
        await PlainTextResponse(
            f"{slug} has no page to preview.", status_code=404
        )(scope, receive, send)
        return
    if not path.endswith("/") and not rest:
        # `/slug` -> `/slug/`, or every relative URL in the page resolves one
        # level too high and the app loads with no CSS.
        await Response(status_code=307, headers={"location": f"/{slug}/"})(scope, receive, send)
        return
    await mount(dict(scope, path="/" + rest), receive, send)


# StaticFiles signals a missing (or escaping-symlink) file by *raising*
# HTTPException, which without this wrapper is an unhandled exception and so
# a 500 - a refusal reported as a crash. The middleware turns it back into
# the 404 it is.
preview_app = ExceptionMiddleware(preview_asgi)


async def serve_loop() -> None:
    """Run the preview server beside the portal, in the portal's event loop.

    A second process would need its own unit, its own restart-on-self-update
    and its own log; a second uvicorn Server in this loop gets all three for
    free and dies with the portal, which is the behavior you want from
    something whose only job is to make the portal's links work.
    """
    import contextlib

    import uvicorn

    class _Quiet(uvicorn.Server):
        """A uvicorn Server that does not touch the process's signal handlers.

        This is the whole reason a second server in one process is safe. A
        normal `Server.serve()` installs its own SIGINT/SIGTERM handlers, so
        the second one to start wins and `systemctl stop project-portal` would
        stop *the preview server* while the portal itself carried on ignoring
        the signal. The outer uvicorn keeps the signals; this one exits when
        its task is canceled, which is what the shutdown handler already does.
        """

        def install_signal_handlers(self) -> None:  # pragma: no cover - trivial
            return

        @contextlib.contextmanager
        def capture_signals(self):  # pragma: no cover - trivial
            yield

    server = _Quiet(uvicorn.Config(
        preview_app,
        host="0.0.0.0",
        port=config.PREVIEW_PORT,
        log_level="warning",
        access_log=False,
    ))
    try:
        await server.serve()
    except asyncio.CancelledError:
        raise
    except (OSError, SystemExit) as exc:
        # Almost always "address already in use" - a second portal booted for a
        # preview, or a stale process. Worth one line, not a traceback, and
        # never a reason to take the portal itself down.
        #
        # SystemExit is not paranoia and not an OSError: uvicorn catches the
        # bind error itself, logs it and calls sys.exit(1). That is a
        # BaseException, so it goes straight past `except OSError` and out of
        # the task, and asyncio answers an escaping SystemExit by stopping the
        # loop - i.e. the portal would exit because a *preview* port was busy.
        # Which is exactly the situation deploy/preview.py creates every time a
        # second portal is booted against a copy of the database.
        log.warning("Preview server could not bind port %s: %s", config.PREVIEW_PORT, exc)
    except Exception:  # pragma: no cover - defensive
        log.exception("Preview server stopped")


# --------------------------------------------------------------------------
# The report field
# --------------------------------------------------------------------------

def apply_report(project: Any, report: dict) -> Optional[str]:
    """Store the address an agent says its project serves on. Returns it.

    Only ever set from a report, never cleared by one: an agent that forgets to
    repeat the field on its next run would otherwise silently unpublish the
    button. Wes clears it by emptying the box on the project page.
    """
    url = report.get("preview_url")
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or not url.startswith(("http://", "https://")):
        return None
    if len(url) > 500:
        return None
    db.update_project(project["id"], preview_url=url)
    return url

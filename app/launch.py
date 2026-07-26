"""Start a project's server when "open it" finds it down.

Wes's ask, 2026-07-23: "the button to open whatever has been built should also
run the thing if it isn't running. When I click it, it should surely open a
working version of whatever page it is."

The static half of "open it" already keeps that promise - the portal serves
those pages itself. This module covers the other half: a project whose
`preview_url` points at a real server (a Bun room server, a FastAPI app). The
/open route probes that address, and when nothing answers, starts the server
from a recipe the project's agent left in its workspace:

    .portal/serve.json
    {"cmd": "bun server.js", "cwd": "."}

`cmd` runs through `/bin/sh -lc` from the workspace (or `cwd` inside it). The
recipe lives in the workspace rather than the database because the agent who
binds the port is the one who knows the command, it belongs in version control
with the code it starts, and it moves with the workspace when a project is
converted to a sub-project.

The server is started as a transient systemd *user* unit (`portal-app-<slug>`),
not a child of the portal: the portal restarts itself on every self-update, and
a restart kills everything in the service's cgroup - Wes's app dying because
the *portal* updated is exactly the "surely open a working version" complaint
again, one layer down. `systemd-run --collect` also gives each app its own
journalctl log and a clean `systemctl --user stop portal-app-<slug>`. When
systemd-run is unavailable (tests, another machine), a detached subprocess
logging to `.portal/serve.log` is the fallback; it dies with the portal, and
the button simply starts it again.

Starting happens from a GET, so a cooldown keeps a double-click (or a browser
prefetch) from stacking a second copy while the first is still binding its
port - the probe alone cannot prevent that, because a booting server probes
as down.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from app import config

log = logging.getLogger("portal.launch")

SERVE_FILE = ".portal/serve.json"

# Seconds after a start attempt during which /open will not start again.
# Long enough for a Bun or uvicorn server to bind; short enough that a
# genuinely crashed command can be retried by clicking again.
COOLDOWN_SECONDS = 20.0

_last_start: dict[str, float] = {}


def serve_config(slug: str) -> Optional[dict[str, Any]]:
    """The validated launch recipe from a workspace, or None.

    None means "no recipe" whether the file is absent, unreadable, malformed
    or escapes the workspace - the /open page says the same thing for all of
    them, and an agent-written file must never crash a page render.
    """
    workspace = config.PROJECTS_DIR / slug
    path = workspace / SERVE_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cmd = data.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 1000:
        return None
    cwd = data.get("cwd") or "."
    if not isinstance(cwd, str):
        return None
    try:
        resolved = (workspace / cwd).resolve()
        resolved.relative_to(workspace.resolve())
    except (OSError, ValueError):
        return None
    return {"cmd": cmd.strip(), "cwd": cwd}


def probe(url: str, timeout: float = 2.5) -> bool:
    """Is something answering HTTP at `url`?

    Any HTTP response counts as up, including an error status - a 500 still
    means the server is running, and it is the server Wes asked to open. Only
    a connection-level failure (refused, unreachable, timeout) counts as down.
    """
    if not url.startswith(("http://", "https://")):
        return False
    request = urllib.request.Request(url, headers={"User-Agent": "portal-open-probe"})
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def unit_name(slug: str) -> str:
    return f"portal-app-{slug}"


def _launch_path() -> str:
    """PATH for the launched command: the portal's own, plus the user-level
    bin dirs where bun and friends live - a systemd unit does not read
    .bashrc, so a bare `bun server.js` would otherwise be command-not-found."""
    home = Path.home()
    extras = [str(home / ".bun" / "bin"), str(home / ".local" / "bin")]
    current = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    parts = [p for p in extras if p not in current.split(":")]
    return ":".join(parts + [current])


def start(slug: str, cfg: dict[str, Any]) -> tuple[bool, str]:
    """Start the project's server. Returns (started, how).

    Called only after a probe said the server is down, so an existing
    `portal-app-<slug>` unit is a corpse or a hang - it is stopped first,
    otherwise systemd-run refuses the duplicate name and the button dead-ends.
    """
    now = time.monotonic()
    last = _last_start.get(slug)
    if last is not None and now - last < COOLDOWN_SECONDS:
        return False, "recently started - giving it time to come up"
    _last_start[slug] = now

    workdir = (config.PROJECTS_DIR / slug / cfg["cwd"]).resolve()
    if not workdir.is_dir():
        return False, f"working directory {cfg['cwd']!r} does not exist"

    unit = unit_name(slug)
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", f"{unit}.service"],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["systemctl", "--user", "reset-failed", f"{unit}.service"],
            capture_output=True, timeout=10,
        )
        result = subprocess.run(
            [
                "systemd-run", "--user", "--collect", f"--unit={unit}",
                f"--working-directory={workdir}",
                f"--setenv=PATH={_launch_path()}",
                "/bin/sh", "-lc", cfg["cmd"],
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            log.info("Started %s via systemd-run: %s", unit, cfg["cmd"])
            return True, f"started as user unit {unit}"
        log.warning(
            "systemd-run failed for %s (%s); falling back to a detached process",
            unit, (result.stderr or "").strip(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("systemd-run unavailable for %s (%s); falling back", unit, exc)

    return _start_detached(slug, cfg, workdir)


def _start_detached(slug: str, cfg: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """The no-systemd fallback: a session-leader child logging to serve.log.

    It lives in the portal's cgroup, so a portal restart takes it down too -
    acceptable, because the button's whole job is to start it again.
    """
    portal_dir = config.PROJECTS_DIR / slug / ".portal"
    try:
        portal_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(portal_dir / "serve.log", "ab")
    except OSError as exc:
        return False, f"could not open serve.log: {exc}"
    env = dict(os.environ, PATH=_launch_path())
    try:
        with log_file:
            process = subprocess.Popen(
                ["/bin/sh", "-lc", cfg["cmd"]],
                cwd=workdir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        (portal_dir / "serve.pid").write_text(str(process.pid))
    except OSError as exc:
        return False, f"could not start: {exc}"
    log.info("Started %s detached (pid %s): %s", slug, process.pid, cfg["cmd"])
    return True, f"started (pid {process.pid}, log in .portal/serve.log)"


def reset_cooldowns() -> None:
    """Test hook."""
    _last_start.clear()

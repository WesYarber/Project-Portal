"""The other portals this one knows about, and keeping them on the same code.

Until 2026-09-02 there was one install of the portal in the world, on the box
it was written on. Then Wes put a second machine on his tailnet - the one on
his desk at the office, on that building's own LAN - and asked for the portal
there too, kept up to date with this one, and for *this* portal to know the
other exists. That last part is the piece the mirror alone cannot give him:
`app/mirror.py` pushes the source to GitHub and `deploy/update.py` pulls it
down, but neither side ever hears from the other. The office install could sit
a week behind, or down, and the board at home would look exactly the same.

So this module is a small registry of *nodes* - other installs, each with a
URL and optionally an ssh target - and three things done with it:

- **A probe.** Every node's `/api/node` is asked, on a poller, what commit it is
  running and whether it is up. The answer is cached in the settings table like
  the tailnet reading is, so a page render never waits on a request to another
  building.
- **A version comparison.** The public repository has a fresh history on
  purpose, so a follower's HEAD never equals this repo's HEAD. What both sides
  share is the `Source-commit:` trailer the publish stamps into every mirror
  commit: a follower reports the trailer of the commit it is on, this node
  reports the trailer of the last commit it published, and the two either
  match or they do not.
- **An update push.** A node with an ssh target can be told to run its own
  `deploy/update.py` - from a button, and automatically after every publish,
  once the node has no agent run in flight. The follower's systemd timer
  (`deploy/project-portal-update.timer`) is the backstop for when this machine
  cannot reach it; this is the path that makes an update land in minutes
  rather than at the next tick of a half-hour clock.

Every reading and every push runs off the event loop, and everything the pages
show comes from the cache. The same discipline as app/netinfo.py, for the same
reason: a probe crosses the internet to another building, and a page is not
allowed to wait on that.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from app import config, db, live, mirror

log = logging.getLogger("portal.nodes")

REGISTRY_KEY = "nodes_json"
STATUS_KEY = "nodes_status_json"

# Two minutes. A node going down or coming back is worth knowing about within
# a few minutes; the version it runs changes a few times a day at most. Each
# poll is one HTTP request per node with a short timeout.
POLL_INTERVAL_SEC = 120
STARTUP_DELAY_SEC = 12
# A node across the internet answers in well under a second; one that has not
# answered in five is down or unreachable, and the poller should say so
# rather than hold every other node's reading behind it.
PROBE_TIMEOUT_SEC = 5
# `deploy/update.py` on the far side: a fetch, maybe a pip install, a restart
# and a ping. Measured at about ten seconds; the ceiling is for a pip install
# on a slow link.
UPDATE_TIMEOUT_SEC = 600
# Where a follower's checkout lives unless it says otherwise - the path
# `deploy/project-portal.service` assumes too.
DEFAULT_PATH = "~/project-portal"

# A node is "seen" until it has been silent this long; after that it is
# reported as down rather than as a stale reading.
_ID_RE = re.compile(r"[^a-z0-9]+")

# In-flight update pushes, by node id. A push is a subprocess that can run for
# minutes, and the button that starts one must not be pressed twice.
_UPDATES: dict[str, asyncio.Task] = {}
# The identity this process reports, computed once: HEAD does not change under
# a running service without a restart, and a subprocess per /api/node request
# is not free.
_identity: Optional[dict] = None


# --- who we are --------------------------------------------------------------

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(config.APP_ROOT), capture_output=True, text=True
    )


def source_commit() -> str:
    """The upstream commit this checkout's code corresponds to.

    On the machine that publishes, that is HEAD. On a follower, HEAD is a
    commit of the public repository, whose history is unrelated to the
    source's - but its message carries the `Source-commit:` trailer the publish
    stamped into it, and that is the id both sides can compare. A checkout with
    neither (not git at all) reports "".
    """
    if not (config.APP_ROOT / ".git").exists():
        return ""
    done = _git("log", "-1", "--format=%B")
    if done.returncode == 0:
        for line in done.stdout.splitlines():
            line = line.strip()
            if line.startswith(mirror.TRAILER):
                sha = line[len(mirror.TRAILER):].strip()
                if sha:
                    return sha
    return mirror.source_head()


def published_commit() -> str:
    """The newest source commit any follower could be on.

    On the publishing machine that is the trailer of the mirror's last commit
    - not this checkout's HEAD, which may be a commit the mirror has not been
    given yet (a dirty tree waits). Anywhere else it is our own commit: a
    follower compares its peers against what it runs itself.
    """
    if mirror.configured():
        return mirror.published_head() or ""
    return source_commit()


def identity(fresh: bool = False) -> dict:
    """What this portal says about itself to another one, at /api/node."""
    global _identity
    if _identity is None or fresh:
        _identity = {
            "portal": "project-portal",
            "name": config.HOST_LABEL,
            "hostname": socket.gethostname(),
            "port": config.PORT,
            "commit": source_commit(),
            "publishes": mirror.configured(),
        }
    out = dict(_identity)
    out["boot"] = live.BOOT_ID
    out["published"] = published_commit() if out["publishes"] else ""
    try:
        out["running"] = db.count_running()
        out["open_questions"] = len(db.open_questions())
        out["worker_enabled"] = db.get_setting("worker_enabled") == "1"
    except Exception:  # noqa: BLE001 - the identity must answer even with the DB mid-migration
        out["running"] = 0
        out["open_questions"] = 0
        out["worker_enabled"] = False
    out["time"] = int(time.time())
    return out


# --- the registry ------------------------------------------------------------

def slug(name: str) -> str:
    return _ID_RE.sub("-", (name or "").strip().lower()).strip("-")


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url):
        url = "http://" + url
    return url.rstrip("/") + "/"


def registry() -> list[dict]:
    raw = db.get_setting(REGISTRY_KEY)
    if not raw:
        return []
    try:
        nodes = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [n for n in nodes if isinstance(n, dict) and n.get("id") and n.get("url")]


def _save(nodes: list[dict]) -> None:
    db.set_setting(REGISTRY_KEY, json.dumps(nodes))


def get(node_id: str) -> Optional[dict]:
    for node in registry():
        if node["id"] == node_id:
            return node
    return None


def add(name: str, url: str, ssh: str = "", path: str = "") -> Optional[dict]:
    """Register a node. Returns it, or None when the input could not be one.

    Adding a name that is already registered replaces that entry - the form
    is the one way to fix a wrong URL, and "add it again" is what a person
    reaches for.
    """
    node_id = slug(name)
    url = _normalize_url(url)
    if not node_id or not url:
        return None
    node = {
        "id": node_id,
        "name": (name or "").strip(),
        "url": url,
        "ssh": (ssh or "").strip(),
        "path": (path or "").strip() or DEFAULT_PATH,
    }
    nodes = [n for n in registry() if n["id"] != node_id]
    nodes.append(node)
    nodes.sort(key=lambda n: n["name"].lower())
    _save(nodes)
    return node


def remove(node_id: str) -> bool:
    nodes = registry()
    kept = [n for n in nodes if n["id"] != node_id]
    if len(kept) == len(nodes):
        return False
    _save(kept)
    statuses = _statuses()
    statuses.pop(node_id, None)
    db.set_setting(STATUS_KEY, json.dumps(statuses))
    return True


# --- probing -----------------------------------------------------------------

def _fetch_json(url: str, timeout: float = PROBE_TIMEOUT_SEC) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "project-portal-node"})
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        return json.loads(answer.read().decode("utf-8", "replace"))


def probe(node: dict) -> dict:
    """Ask one node who it is. Blocking; never raises."""
    started = time.monotonic()
    out: dict[str, Any] = {"checked_at": int(time.time()), "ok": False, "error": "", "node": None}
    try:
        answer = _fetch_json(node["url"] + "api/node")
    except urllib.error.HTTPError as exc:
        # A 404 is a real web server that is not a portal (or an old one,
        # from before /api/node existed) - worth saying apart from "down".
        out["error"] = f"answered HTTP {exc.code}" + (" - not a portal, or one too old to say" if exc.code == 404 else "")
        return out
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        out["error"] = str(reason)[:200] or "no answer"
        return out
    if not isinstance(answer, dict) or answer.get("portal") != "project-portal":
        out["error"] = "answered, but not as a portal"
        return out
    out["ok"] = True
    out["latency_ms"] = int((time.monotonic() - started) * 1000)
    out["seen_at"] = out["checked_at"]
    out["node"] = {
        "name": str(answer.get("name") or ""),
        "hostname": str(answer.get("hostname") or ""),
        "commit": str(answer.get("commit") or ""),
        "boot": str(answer.get("boot") or ""),
        "running": int(answer.get("running") or 0),
        "open_questions": int(answer.get("open_questions") or 0),
        "worker_enabled": bool(answer.get("worker_enabled")),
        "publishes": bool(answer.get("publishes")),
    }
    return out


def _statuses() -> dict[str, dict]:
    raw = db.get_setting(STATUS_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _material(old: Optional[dict], new: dict) -> bool:
    """Is this reading different enough to write?

    Every write to the settings table bumps the data version that every open
    page polls, and a poller that rewrites the same truth every two minutes
    would have the dashboard patching itself for nothing. A reading is written
    when what it says changes - up or down, which commit, which boot - and
    otherwise only often enough that "seen 3 minutes ago" is not a lie.
    """
    if old is None:
        return True
    if old.get("ok") != new.get("ok") or old.get("error") != new.get("error"):
        return True
    if (old.get("node") or {}) != (new.get("node") or {}):
        return True
    if old.get("pending_update") != new.get("pending_update"):
        return True
    return int(new.get("checked_at") or 0) - int(old.get("checked_at") or 0) >= 10 * 60


def snapshot(node_ids: Optional[list[str]] = None) -> dict[str, dict]:
    """Probe every registered node (or the ones named) and cache the answers.

    Returns the whole status map. Blocking - callers are the poller and
    `/api/nodes?refresh=1`.
    """
    statuses = _statuses()
    changed = False
    for node in registry():
        if node_ids is not None and node["id"] not in node_ids:
            continue
        old = statuses.get(node["id"])
        new = probe(node)
        if old:
            # Carry forward what a failed probe cannot know: when it was last
            # seen, and what it said then, so "down since" has a date.
            new.setdefault("seen_at", old.get("seen_at"))
            if not new["ok"]:
                new["node"] = old.get("node")
            new["pending_update"] = bool(old.get("pending_update"))
            if old.get("update"):
                new["update"] = old["update"]
        else:
            new["pending_update"] = False
        if _material(old, new):
            changed = True
        statuses[node["id"]] = new
    if changed:
        db.set_setting(STATUS_KEY, json.dumps(statuses))
    return statuses


def _mark(node_id: str, **fields: Any) -> None:
    statuses = _statuses()
    entry = statuses.get(node_id) or {"ok": False, "error": "", "node": None, "checked_at": 0}
    entry.update(fields)
    statuses[node_id] = entry
    db.set_setting(STATUS_KEY, json.dumps(statuses))


# --- the view ----------------------------------------------------------------

def _state(node: dict, status: Optional[dict], published: str) -> tuple[str, str]:
    """(state, one-line detail) for a node as the pages show it.

    States: `off` (never answered / down), `behind`, `ok`, `unknown` (up, but
    nothing to compare against - this install publishes nothing and knows no
    upstream), `updating`.
    """
    if node["id"] in _UPDATES and not _UPDATES[node["id"]].done():
        return "updating", "running deploy/update.py there now"
    if not status:
        return "off", "not checked yet"
    if not status.get("ok"):
        since = status.get("seen_at")
        ago = f", last seen {_ago(since)}" if since else ", never answered"
        return "off", f"{status.get('error') or 'no answer'}{ago}"
    info = status.get("node") or {}
    theirs = info.get("commit") or ""
    if not published or not theirs:
        return "unknown", f"running {theirs[:7] or 'an unknown commit'}"
    if theirs == published:
        return "ok", f"up to date at {theirs[:7]}"
    if status.get("pending_update"):
        return "behind", f"on {theirs[:7]}, {published[:7]} is published - update waits for its {info.get('running', 0)} run(s) to finish"
    return "behind", f"on {theirs[:7]}, {published[:7]} is published"


def _ago(ts: Optional[int]) -> str:
    if not ts:
        return "never"
    secs = max(0, int(time.time()) - int(ts))
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def view() -> list[dict]:
    """Every node with its cached status, shaped for a template. Reads only."""
    if not registry():
        return []
    statuses = _statuses()
    published = published_commit()
    out = []
    for node in registry():
        status = statuses.get(node["id"])
        state, detail = _state(node, status, published)
        info = (status or {}).get("node") or {}
        update = (status or {}).get("update") or {}
        out.append(
            {
                **node,
                "state": state,
                "detail": detail,
                "online": bool(status and status.get("ok")),
                "commit": info.get("commit") or "",
                "running": info.get("running") or 0,
                "open_questions": info.get("open_questions") or 0,
                "worker_enabled": info.get("worker_enabled"),
                "latency_ms": (status or {}).get("latency_ms"),
                "checked": _ago((status or {}).get("checked_at")) if status else "never",
                "seen": _ago((status or {}).get("seen_at")) if status and status.get("seen_at") else "never",
                "can_update": bool(node.get("ssh")),
                "update_at": _ago(update.get("at")) if update.get("at") else "",
                "update_ok": update.get("ok"),
                "update_output": update.get("output") or "",
            }
        )
    return out


def summary() -> list[dict]:
    """The dashboard's one line per node: name, link, state, detail."""
    return [
        {"id": n["id"], "name": n["name"], "url": n["url"], "state": n["state"], "detail": n["detail"]}
        for n in view()
    ]


# --- pushing an update -------------------------------------------------------

def _ssh_command(node: dict) -> list[str]:
    path = node.get("path") or DEFAULT_PATH
    # `python3`, not the venv's: update.py bootstraps from the system
    # interpreter the same way setup.py does, and finds the venv itself.
    remote = f"cd {shlex.quote(path) if not path.startswith('~') else path} && python3 deploy/update.py"
    return [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new", node["ssh"], remote,
    ]


def push_update(node_id: str) -> dict:
    """Run `deploy/update.py` on a node over ssh. Blocking; never raises.

    The result - exit status and the tail of what the script printed - is
    stored on the node's status and journaled on the meta project, so a push
    that failed at 3am is read the next morning rather than lost.
    """
    node = get(node_id)
    if node is None:
        return {"ok": False, "output": "no such node"}
    if not node.get("ssh"):
        return {"ok": False, "output": "no ssh target for this node"}
    result: dict[str, Any] = {"at": int(time.time()), "ok": False, "output": ""}
    try:
        done = subprocess.run(
            _ssh_command(node), capture_output=True, text=True, timeout=UPDATE_TIMEOUT_SEC
        )
        text = (done.stdout + ("\n" + done.stderr if done.stderr.strip() else "")).strip()
        result["ok"] = done.returncode == 0
        result["output"] = text[-3000:]
    except subprocess.TimeoutExpired:
        result["output"] = f"update.py did not finish within {UPDATE_TIMEOUT_SEC}s"
    except (OSError, subprocess.SubprocessError) as exc:
        result["output"] = str(exc)
    _mark(node_id, update=result, pending_update=False)
    _note(
        f"Pushed an update to the `{node['name']}` portal ({node['url']}): "
        + ("it fast-forwarded and is serving." if result["ok"] else "it did not go through.")
        + f"\n\n```\n{result['output'][-1200:]}\n```"
    )
    log.info("Update push to %s: %s", node_id, "ok" if result["ok"] else "failed")
    # Re-read the node straight away so the page says what it is now running,
    # not what it ran before the push.
    snapshot([node_id])
    return result


def _note(detail: str) -> None:
    project = db.get_project_by_slug(config.META_PROJECT_SLUG)
    if project is None:
        return
    db.add_journal(int(project["id"]), "system", "status", detail)


def start_update(node_id: str) -> bool:
    """Push an update in the background. False if one is already running."""
    task = _UPDATES.get(node_id)
    if task is not None and not task.done():
        return False
    node = get(node_id)
    if node is None or not node.get("ssh"):
        return False
    _UPDATES[node_id] = asyncio.create_task(asyncio.to_thread(push_update, node_id))
    return True


def request_update_all() -> list[str]:
    """After a publish: mark every ssh-reachable node as owed an update.

    Marked rather than pushed, because a push restarts the node's service and
    a node with an agent run in flight should finish it first. The poller
    pushes the moment a marked node reports no run in flight (or the next time
    it answers at all, if it was down).
    """
    marked = []
    for node in registry():
        if node.get("ssh"):
            _mark(node["id"], pending_update=True)
            marked.append(node["id"])
    return marked


def due_updates(statuses: dict[str, dict], published: str) -> list[str]:
    """Nodes marked for an update that are up, idle, and actually behind."""
    due = []
    for node in registry():
        status = statuses.get(node["id"]) or {}
        if not status.get("pending_update") or not status.get("ok") or not node.get("ssh"):
            continue
        info = status.get("node") or {}
        if published and info.get("commit") == published:
            # Its own timer got there first. Nothing to push.
            _mark(node["id"], pending_update=False)
            continue
        if int(info.get("running") or 0) > 0:
            continue
        due.append(node["id"])
    return due


# --- the poller --------------------------------------------------------------

async def poll_loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_SEC)
    while True:
        try:
            if registry():
                statuses = await asyncio.to_thread(snapshot)
                for node_id in due_updates(statuses, published_commit()):
                    start_update(node_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a poller that dies is worse than one that logs
            log.exception("node poll failed")
        await asyncio.sleep(POLL_INTERVAL_SEC)

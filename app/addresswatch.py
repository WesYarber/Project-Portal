"""Notice when a shared skill starts pointing at a port that no longer answers.

A skill is a paragraph of instructions handed to every agent run, and several
of them name a service by its address on the local network -
`http://192.168.1.10:3002` for an uptime monitor, `homeserver:8500` for the
portal's own API. Those addresses go stale silently. The case that prompted
this: a project moved its container onto a shared Docker network and published
no port at all, which closed the address a shared skill had been telling about
fifteen other projects to fetch three paths from. Every one of those calls
refused, for weeks, and nothing in the portal noticed - the report eventually
came from a scanner in another project, which had found it by reading git
repositories and could not see this directory at all.

That is the gap this closes. The shared skills live in `data/skills/`, which is
runtime state with no git history, so a scanner that reads repositories is
blind to exactly the copy that every run is handed. The portal is the only
thing that can check it, so the portal checks it.

## What counts as an address

Only what the portal can actually take a position on:

- **In a URL** (`scheme://host:port`), or **in an inline code span**
  (`` `homeserver:8500` ``). Prose is not scanned, because "Note:8 items" and
  `-L 9334:127.0.0.1:9222` both read as `host:port` to a regex and neither is
  one. Those two shapes are where every real address in these skills is already
  written. The bare shape also needs a port of 1024 or more, which is what
  tells a real address from the `A1:1` aspect ratio in a YUV4MPEG header.
- **Whose host is on the local network**: a private IPv4 literal (10/8,
  172.16/12, 192.168/16, 169.254/16), a tailnet address (100.64/10), or a
  dotless hostname like `homeserver`. A hostname with a dot in it is public and
  usually behind a CDN, and a single-page app behind `try_files` answers 200 to
  every path there, so a TCP probe would prove nothing about it.
- **Not loopback.** `127.0.0.1:9222` in a skill means "on whatever machine you
  are on" - often another machine at the far end of an ssh tunnel - and the
  portal has no standing to say whether that is up.

`$HOST` is substituted first, the same way `worker._localize_skill` substitutes
it into the copy shipped to a workspace, because the shipped copy is what the
agent actually reads.

## What "answers" means

A TCP connect, nothing more. The failure this exists to catch is a port that
stopped being published - connection refused, or a name that stopped resolving.
Judging an HTTP response would need to know each service's healthy shape, and
would call a service behind a login broken. A port that accepts a connection is
alive for this purpose.

## Saying so

A dead address is not a decision for anyone to make, so it does not notify and
does not ask: it opens a todo on the portal's own project, which is where the
fix has to be made, and which the scheduler already treats as work an agent can
pick up. The text carries no date, so the daily re-check folds into the same
row rather than filing a new one each morning.

## Opting out

Some addresses in a skill are deliberately not services: a placeholder origin
inside a snippet, an example port, a container name only reachable from inside
a Docker network. A file says so for itself, anywhere in its text, in
frontmatter or an HTML comment:

    address-watch-ignore: homeserver:9500

Per file rather than global, so that a port some other skill later names for
real is still watched.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Iterable, Optional

from app import config, db, memory, site

log = logging.getLogger("portal.addresswatch")

SETTING_ENABLED = "address_watch"
RESULT_KEY = "skill_addresses_json"

# How long to wait for a TCP connect. A LAN port either answers in single-digit
# milliseconds or is not there; three seconds is generous enough that a loaded
# box is not called dead, and short enough that a whole sweep of a dozen
# addresses cannot outlast a worker tick.
PROBE_TIMEOUT = 3.0

# `scheme://host:port`. The scheme is not captured or checked - http, https, ws
# and redis all name the same kind of thing here.
URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://([A-Za-z0-9_.\-]+):(\d{1,5})")

# A markdown inline code span. Fenced blocks are deliberately not included:
# their real addresses are written as URLs, and their non-addresses (ssh -L
# port forwards, dict literals, JSON) are what a bare host:port regex trips on.
CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")

# A bare host:port inside a code span, at token boundaries so that the middle
# of an `ssh -L 9334:127.0.0.1:9222` forward cannot be read as a host.
BARE_RE = re.compile(r"(?<![A-Za-z0-9_.\-:])([A-Za-z0-9_.\-]+):(\d{1,5})(?![\d:])")

IGNORE_RE = re.compile(r"address-watch-ignore:\s*([^\r\n>]*)", re.IGNORECASE)

# Tailscale hands out addresses from 100.64.0.0/10, which `is_private` calls
# public because it is carrier-grade NAT space. On this estate it is the LAN.
TAILNET = ipaddress.ip_network("100.64.0.0/10")


@dataclass(frozen=True)
class Address:
    """One `host:port` as some skill writes it."""

    host: str
    port: int
    skill: str
    source: str  # path relative to the skills root, for the report

    @property
    def text(self) -> str:
        return f"{self.host}:{self.port}"


# ---------------------------------------------------------------------------
# Finding the addresses
# ---------------------------------------------------------------------------

def is_watchable_host(host: str) -> bool:
    """Is this a host the portal can meaningfully probe? See the module docstring."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A name. Dotless means LAN or tailnet; a dot means a public hostname
        # behind Cloudflare, where a TCP connect proves nothing.
        if "." in host or host in site.LOOPBACK_HOSTS:
            return False
        return bool(re.fullmatch(r"[a-z][a-z0-9\-]*", host))
    if ip.is_loopback:
        return False
    if ip.version == 4 and ip in TAILNET:
        return True
    return bool(ip.is_private)


def ignored_addresses(text: str) -> set[str]:
    """Every `host:port` this file declares the watch should leave alone."""
    out: set[str] = set()
    for line in IGNORE_RE.findall(text or ""):
        for token in re.split(r"[,\s]+", line.strip().strip("-").strip()):
            token = token.strip("`'\"").rstrip(".")
            if ":" in token:
                out.add(token.lower())
    return out


def addresses_in(text: str) -> list[tuple[str, int]]:
    """The watchable `host:port` pairs in one document, in the order found.

    Both shapes are collected before filtering, so that an address written once
    as a URL and once in backticks is found either way.
    """
    found: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    def keep(host: str, port_text: str, bare: bool = False) -> None:
        host = host.strip().lower().rstrip(".")
        try:
            port = int(port_text)
        except ValueError:
            return
        if not (0 < port < 65536) or not is_watchable_host(host):
            return
        # A bare `a:b` in a code span is often not an address at all: a
        # YUV4MPEG header carries a literal `A1:1` aspect ratio, which is a
        # watchable host and a valid port by every rule above it. A service
        # on a privileged port is written as a URL in practice, and the URL
        # shape has no floor.
        if bare and port < 1024:
            return
        if (host, port) not in seen:
            seen.add((host, port))
            found.append((host, port))

    for host, port_text in URL_RE.findall(text or ""):
        keep(host, port_text)
    for span in CODE_SPAN_RE.findall(text or ""):
        if "://" in span:
            continue  # already taken by URL_RE, in its own form
        for host, port_text in BARE_RE.findall(span):
            keep(host, port_text, bare=True)
    return found


def skill_roots() -> list[Path]:
    """Where shared skills come from - the same two roots `worker._sync_skills`
    copies into every workspace, so the watch sees what a run sees."""
    roots: list[Path] = []
    try:
        promoted = memory.promoted_skills_dir()
        if promoted.is_dir():
            roots.append(promoted)
    except OSError:
        pass
    if config.SKILLS_DIR.is_dir():
        roots.append(config.SKILLS_DIR)
    return roots


def scan(roots: Optional[Iterable[Path]] = None) -> tuple[list[Address], list[Address]]:
    """Read every skill and return (watched, ignored).

    `$HOST` and the rest of this installation's tokens are filled in first, so
    a skill that ships with `http://$HOST:8500` is checked against the host it
    will name in the workspace.
    """
    watched: list[Address] = []
    ignored: list[Address] = []
    seen: set[tuple[str, int]] = set()
    for root in list(roots) if roots is not None else skill_roots():
        try:
            files = sorted(p for p in root.rglob("*.md") if p.is_file())
        except OSError:
            continue
        for path in files:
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            text = Template(raw).safe_substitute(**config.SITE.template_vars())
            skip = ignored_addresses(text)
            try:
                relative = path.relative_to(root)
            except ValueError:  # pragma: no cover - rglob roots always match
                relative = Path(path.name)
            skill = relative.parts[0] if relative.parts else path.name
            for host, port in addresses_in(text):
                address = Address(host, port, skill, str(relative))
                if address.text in skip:
                    ignored.append(address)
                    continue
                # A built-in skill and a promoted one can name the same
                # service; probing it twice would file the same todo twice
                # under two names. First writer wins, as in `_sync_skills`.
                if (host, port) in seen:
                    continue
                seen.add((host, port))
                watched.append(address)
    return watched, ignored


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def answers(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """Does anything accept a TCP connection there? Never raises."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def check(roots: Optional[Iterable[Path]] = None) -> dict:
    """Scan, probe, and report. Blocking; `run_check` puts it on a thread."""
    watched, ignored = scan(roots)
    alive: list[Address] = []
    dead: list[Address] = []
    for address in watched:
        (alive if answers(address.host, address.port) else dead).append(address)
    return {
        "ok": True,
        "checked": len(watched),
        "alive": alive,
        "dead": dead,
        "ignored": ignored,
    }


# ---------------------------------------------------------------------------
# Saying so
# ---------------------------------------------------------------------------

def todo_text(address: Address) -> str:
    """One sentence, and deliberately date-free: `db.add_todo` folds a repeat
    into the row that is already open, which only works if the text is stable
    from one morning to the next."""
    return (
        f'The shared skill "{address.skill}" points at {address.text}, '
        f"which no longer answers - repoint it or drop the line."
    )


def file_todo(address: Address) -> bool:
    """Open a todo for one dead address. True if this filed a new row."""
    try:
        project = db.get_project_by_slug(config.META_PROJECT_SLUG)
    except Exception:  # noqa: BLE001 - a missing meta project is not fatal
        return False
    if project is None:
        return False
    before = _existing_todo_texts(project["id"])
    row = db.add_todo(project["id"], todo_text(address), owner="agent", tags=["cleanup"])
    if row is None:
        return False
    # `add_todo` returns the matched row on a repeat, including a row already
    # ticked off, so the row alone cannot say whether this is news. An item Wes
    # or an agent has closed stays closed: re-opening it every morning is the
    # nagging this watch is supposed to replace.
    return row["text"] not in before


def _existing_todo_texts(project_id: int) -> set[str]:
    try:
        rows = db.list_todos(project_id)
    except Exception:  # noqa: BLE001
        return set()
    return {row["text"] for row in rows}


def store(result: dict) -> None:
    """Keep the last sweep for the settings card."""
    db.set_setting(RESULT_KEY, json.dumps({
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checked": result.get("checked", 0),
        "alive": [a.text for a in result.get("alive", [])],
        "dead": [{"address": a.text, "skill": a.skill} for a in result.get("dead", [])],
        "ignored": [a.text for a in result.get("ignored", [])],
    }))


def last_result() -> dict:
    """What the settings page shows. Never raises, never returns a non-dict."""
    try:
        blob = json.loads(db.get_setting(RESULT_KEY) or "")
    except (TypeError, ValueError):
        blob = None
    if not isinstance(blob, dict):
        return {"checked_at": "", "checked": 0, "alive": [], "dead": [], "ignored": []}
    return blob


def enabled() -> bool:
    return (db.get_setting(SETTING_ENABLED) or "1") == "1"


async def run_check(roots: Optional[Iterable[Path]] = None) -> dict:
    """The whole job: scan the skills, probe what they name, file what is dead.

    Every probe is a blocking socket call, so the scan and the probing go to a
    thread together. Never raises - a watcher that cannot see is never a reason
    for the worker to stop.
    """
    if not enabled():
        return {"ok": False, "checked": 0, "alive": [], "dead": [], "ignored": [], "filed": []}
    try:
        result = await asyncio.to_thread(check, roots)
    except Exception:  # noqa: BLE001
        log.exception("Address watch failed")
        return {"ok": False, "checked": 0, "alive": [], "dead": [], "ignored": [], "filed": []}
    filed: list[str] = []
    for address in result["dead"]:
        log.warning(
            "Shared skill %s names %s, which does not answer", address.skill, address.text
        )
        try:
            if file_todo(address):
                filed.append(address.text)
        except Exception:  # noqa: BLE001
            log.exception("Could not file a todo for %s", address.text)
    try:
        store(result)
    except Exception:  # noqa: BLE001
        log.exception("Could not record the address sweep")
    result["filed"] = filed
    return result

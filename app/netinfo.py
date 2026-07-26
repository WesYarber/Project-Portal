"""How the portal is actually reachable, and from which machine on the tailnet.

Two long-running puzzles live here.

The first is HTTPS. The browser hides `getUserMedia` outside a secure context,
so voice memos - the whole reason for wanting HTTPS - are invisible over plain
HTTP. `tailscale serve` fixes that by terminating TLS on port 443 of the
tailnet address with a real Let's Encrypt certificate, so the portal's
`*.ts.net` name is a secure context on a phone with no certificate warning and
nothing exposed to the public internet.

The second is stranger, and cost two runs of guessing: the portal's port
answered on the LAN address but not on the tailnet one. Nothing on the host
explained it - uvicorn binds `0.0.0.0` and there was no firewall rule. The
answer was in the tailnet ACL: the server carried one tag, and the packet
filter it had been handed only admitted a different one, so a machine signed
in as the owner personally was dropped before it ever reached the socket.

That is a fact about *the ACL*, not about the portal, and no amount of reading
the server could have produced it - so this module reads the filter the
coordination server actually handed us and says, per machine, whether it can
reach the portal at all. A blocked machine is a one-line answer instead of an
afternoon.

Discipline copied from app/limits.py, for the same reasons: every reading is
taken by a background poller into a settings-table cache, and never from
inside a request handler. `tailscale` is a subprocess talking to a local
daemon that talks to the network, and a page render is not allowed to wait on
that.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import shutil
import subprocess
import time
from typing import Any, Optional

from app import config, db

log = logging.getLogger("portal.netinfo")

CACHE_KEY = "tailnet_json"
# Ten minutes. The things being watched - a machine joining the tailnet, Wes
# editing his ACL, a serve config changing - move on the order of days, and
# each poll is three subprocesses.
POLL_INTERVAL_SEC = 600
# Same reason as the limits poller: this service restarts itself on every
# self-update, and a run of restarts should not become a run of subprocesses.
STARTUP_DELAY_SEC = 8
# `tailscale` talks to a local daemon; if it has not answered in this long it
# is wedged, and waiting longer only holds the poller open.
CLI_TIMEOUT_SEC = 15


# --- running the CLI --------------------------------------------------------

def _tailscale(*args: str) -> Optional[Any]:
    """Run `tailscale <args>` and parse its stdout as JSON.

    Returns None for every failure mode - not installed, daemon down, non-zero
    exit, unparseable output. Absence of a reading is a normal state here
    (the portal must run on a machine with no tailscale at all), so it is
    reported as None rather than raised.
    """
    binary = shutil.which("tailscale")
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("tailscale %s failed: %s", " ".join(args), exc)
        return None
    if proc.returncode != 0:
        log.info("tailscale %s exited %s: %s", " ".join(args), proc.returncode, proc.stderr.strip()[:200])
        return None
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError:
        return None


# --- parsing ----------------------------------------------------------------

def _clean_dns(name: str) -> str:
    """DNSNames come back with a trailing dot, which is correct DNS and wrong
    in a URL."""
    return (name or "").rstrip(".")


def _display_name(node: dict) -> str:
    """What to call a machine on screen.

    The tailnet name (the first label of the DNS name), not `HostName`. Wes's
    iPhone reports its hostname as `localhost` - iOS does this - so a list keyed
    on HostName has an entry called "localhost" and no entry for his phone,
    which is the one machine the reachability question is usually about.
    """
    dns = _clean_dns(node.get("DNSName") or "")
    return dns.split(".")[0] if dns else (node.get("HostName") or "")


def parse_status(raw: Any) -> dict:
    """The tailnet as this node sees it: who we are, and who else is on it."""
    if not isinstance(raw, dict):
        return {"self": None, "peers": []}
    self_node = raw.get("Self") or {}
    me = {
        "host": _display_name(self_node),
        "dns": _clean_dns(self_node.get("DNSName") or ""),
        "ips": [ip for ip in (self_node.get("TailscaleIPs") or []) if isinstance(ip, str)],
        "tags": list(self_node.get("Tags") or []),
    }
    peers = []
    for peer in (raw.get("Peer") or {}).values():
        if not isinstance(peer, dict):
            continue
        peers.append(
            {
                "host": _display_name(peer),
                "dns": _clean_dns(peer.get("DNSName") or ""),
                "ips": [ip for ip in (peer.get("TailscaleIPs") or []) if isinstance(ip, str)],
                "tags": list(peer.get("Tags") or []),
                "os": peer.get("OS") or "",
                "online": bool(peer.get("Online")),
            }
        )
    # Online first, then by name: the machines Wes might be sitting at are the
    # ones he is asking about, and a 25-day-offline laptop is noise.
    peers.sort(key=lambda p: (not p["online"], p["host"].lower()))
    return {"self": me if me["dns"] or me["ips"] else None, "peers": peers}


def parse_serve(raw: Any, target_port: int = config.PORT) -> dict:
    """What `tailscale serve` is currently doing, from its own config.

    We only claim HTTPS when a handler actually proxies to *our* port. A serve
    config fronting some other service on this host is not the portal being
    reachable over HTTPS, and saying it was would send Wes to a URL that
    answers with something else entirely.
    """
    out = {"https": False, "url": "", "target": ""}
    if not isinstance(raw, dict):
        return out
    needle = f":{target_port}"
    for host_port, web in (raw.get("Web") or {}).items():
        if not isinstance(web, dict):
            continue
        for path, handler in (web.get("Handlers") or {}).items():
            proxy = (handler or {}).get("Proxy") or ""
            if not proxy.endswith(needle) and f"{needle}/" not in proxy:
                continue
            host, _, port = str(host_port).rpartition(":")
            # `tailscale serve` on 443 is the plain https:// URL; anything else
            # has to carry its port or the link is wrong.
            base = f"https://{host}" if port == "443" else f"https://{host}:{port}"
            out["https"] = True
            out["url"] = base + (path if path and path != "/" else "/")
            out["target"] = proxy
            return out
    return out


def _covers(dst: Any, ip: str, port: int) -> bool:
    """Does one packet-filter destination cover our address and port?"""
    if not isinstance(dst, dict):
        return False
    ports = dst.get("Ports") or {}
    try:
        first = int(ports.get("First", 0))
        last = int(ports.get("Last", 0))
    except (TypeError, ValueError):
        return False
    if not (first <= port <= last):
        return False
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(str(dst.get("Net") or ""), strict=False)
    except ValueError:
        return False


def parse_filter(raw: Any, self_ips: list[str], port: int = config.PORT) -> Optional[list[str]]:
    """Source prefixes allowed to reach `port` on this node.

    None means "no filter information", which is very different from an empty
    list ("nothing may reach us") - a missing netmap must never be rendered as
    every machine being blocked.
    """
    if not isinstance(raw, dict):
        return None
    packet_filter = raw.get("PacketFilter")
    if not isinstance(packet_filter, list):
        return None
    allowed: list[str] = []
    for rule in packet_filter:
        if not isinstance(rule, dict):
            continue
        dsts = rule.get("Dsts") or []
        if not any(_covers(dst, ip, port) for dst in dsts for ip in self_ips):
            continue
        for src in rule.get("Srcs") or []:
            if isinstance(src, str) and src not in allowed:
                allowed.append(src)
    return allowed


def _src_matches(src: str, ips: list[str]) -> bool:
    try:
        net = ipaddress.ip_network(src, strict=False)
    except ValueError:
        return False
    for ip in ips:
        try:
            if ipaddress.ip_address(ip) in net:
                return True
        except ValueError:
            continue
    return False


def annotate_reach(peers: list[dict], allowed: Optional[list[str]]) -> list[dict]:
    """Mark each peer as able to reach the portal, blocked, or unknown."""
    out = []
    for peer in peers:
        annotated = dict(peer)
        if allowed is None:
            annotated["reach"] = "unknown"
        else:
            annotated["reach"] = "yes" if any(_src_matches(src, peer.get("ips") or []) for src in allowed) else "blocked"
        out.append(annotated)
    return out


# --- the snapshot -----------------------------------------------------------

def snapshot() -> dict:
    """Take a fresh reading. Blocking - callers are the poller and /api."""
    status = parse_status(_tailscale("status", "--json"))
    serve = parse_serve(_tailscale("serve", "status", "--json"))
    self_ips = (status["self"] or {}).get("ips") or []
    allowed = parse_filter(_tailscale("debug", "netmap"), self_ips) if self_ips else None
    return {
        "fetched_at": int(time.time()),
        "lan_url": f"http://{config.HOST_LABEL}:{config.PORT}/",
        "self": status["self"],
        "https": serve["https"],
        "https_url": serve["url"],
        "peers": annotate_reach(status["peers"], allowed),
        "acl_known": allowed is not None,
    }


def store(snap: dict) -> None:
    db.set_setting(CACHE_KEY, json.dumps(snap))


def cached() -> Optional[dict]:
    """The last stored reading, with its age. None if there has never been one."""
    raw = db.get_setting(CACHE_KEY)
    if not raw:
        return None
    try:
        snap = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(snap, dict):
        return None
    snap = dict(snap)
    snap["age_sec"] = max(0, int(time.time()) - int(snap.get("fetched_at") or 0))
    return snap


def blocked_peers(snap: Optional[dict]) -> list[dict]:
    """Online machines the ACL will not let through. Offline ones are excluded:
    a laptop that has not been seen for 25 days is not a reachability problem
    anyone is having today."""
    if not snap or not snap.get("acl_known"):
        return []
    return [p for p in snap.get("peers") or [] if p.get("reach") == "blocked" and p.get("online")]


async def poll_loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_SEC)
    while True:
        try:
            snap = await asyncio.to_thread(snapshot)
            # A failed reading must not blank a good one: if tailscale is
            # momentarily wedged we would rather show a four-minute-old truth
            # than nothing at all.
            if snap.get("self") or not cached():
                store(snap)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a poller that dies is worse than one that logs
            log.exception("tailnet poll failed")
        await asyncio.sleep(POLL_INTERVAL_SEC)

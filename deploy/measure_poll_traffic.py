"""Count the requests one open portal tab makes, visible and hidden.

Wes, 2026-08-13: the portal was keeping his Mac's fans up. The client-side fix
is in app/static/app.js, and tests/test_console_poll.py proves the code path
against a stub DOM - but a stub DOM cannot prove that a REAL browser goes quiet.
This does, by counting what actually reaches the server.

It stands up two throwaway portals under /tmp with their own databases (the
recipe in docs/looking-at-the-ui.md), one serving the app.js at a given git
ref and one serving the working tree's, points a headless chromium on the
render machine at each in turn, and counts /api/ hits over a window - visible,
then hidden, then visible again.

    venv/bin/python deploy/measure_poll_traffic.py --window 40

Nothing here touches the live instance or the live data directory; both copies
assert their DATA_DIR is under /tmp before they write a byte.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "venv" / "bin" / "python"

sys.path.insert(0, str(ROOT / "deploy"))
import websockets  # noqa: E402  (comes from the portal's own venv)
from interact import BOX, _ssh  # noqa: E402

# Its own debug port and its own profile, rather than interact.py's 9222 and
# ~/.portal-interact-profile. Two reasons, both from the browser skill: another
# project's run may be holding 9222 (one was, six days stale, when this was
# written), and a --user-data-dir OUTSIDE the snap's confinement is silently
# ignored - a second chromium then hands its tabs to whichever instance already
# owns the default profile and exits 0, with nothing logged anywhere.
CDP_PORT = 9245
PROFILE = "$HOME/snap/chromium/common/poll-traffic-profile"


class OwnChrome:
    """A headless chromium on the render machine, on a port nobody else uses."""

    def __enter__(self) -> "OwnChrome":
        # A run killed mid-flight leaves its `ssh -N` holding the local end,
        # and the next attempt then tunnels into nothing: "Address already in
        # use" followed by a websocket that closes mid-measurement. Kill by
        # PORT OWNER - never `pkill -f`, which matches the shell running it.
        subprocess.run(["fuser", "-k", f"{CDP_PORT}/tcp"], capture_output=True)
        _ssh("pkill", "-f", f"remote-debugging-port={CDP_PORT}")
        _ssh(f"rm -f {PROFILE}/Singleton*")
        _ssh(
            # Old headless: --headless=new never fires the load event on a page
            # holding an SSE stream open, and every portal page holds one.
            "nohup /snap/bin/chromium --headless --disable-gpu --no-sandbox "
            f"--hide-scrollbars --no-first-run --remote-debugging-port={CDP_PORT} "
            f"--window-size=1280,900 --user-data-dir={PROFILE} "
            "about:blank >/dev/null 2>&1 & sleep 2"
        )
        self.tunnel = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-N",
             "-L", f"{CDP_PORT}:127.0.0.1:{CDP_PORT}", BOX])
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1):
                    return self
            except Exception:
                time.sleep(0.5)
        raise SystemExit("chromium on the render machine never answered")

    def __exit__(self, *exc) -> None:
        self.tunnel.terminate()
        _ssh("pkill", "-f", f"remote-debugging-port={CDP_PORT}")

    def page_ws(self) -> str:
        with urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=5) as r:
            pages = [t for t in json.load(r) if t.get("type") == "page"]
        if not pages:
            raise SystemExit("chromium started with no page target")
        return pages[0]["webSocketDebuggerUrl"]

SEED = """
import sys
sys.path.insert(0, {root!r})
from app import config, db
assert str(config.DATA_DIR).startswith("/tmp/"), "refusing to touch the live data dir"
db.init_db()
db.set_setting("worker_enabled", "0")
p = db.create_project("Poller traffic", "A project to open a page of.",
                      kind="software", stage="active")
# create_run inserts with status 'running', which is what puts the console into
# its live state - and the console poller is the one Wes's diagnosis named. The
# worker is off, so nothing ever finishes it: the page stays in exactly the
# state a tab left open on a running agent is in.
db.create_run(p["id"], "measuring what an open tab costs", "claude-opus-5")
print(p["slug"])
"""


def build(root: Path, ref: str | None) -> str:
    """A source tree with no data directory, optionally with app.js from a ref."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for name in ("app", "deploy", "portal.toml"):
        src = ROOT / name
        (shutil.copytree if src.is_dir() else shutil.copy)(src, root / name)
    shutil.rmtree(root / "data", ignore_errors=True)
    if ref:
        for rel in ("app/static/app.js", "app/templates/oneoff.html"):
            blob = subprocess.run(
                ["git", "show", f"{ref}:{rel}"],
                capture_output=True, text=True, cwd=str(ROOT), check=True,
            ).stdout
            (root / rel).write_text(blob)
    seeded = subprocess.run([str(PY), "-c", SEED.format(root=str(root))],
                            cwd=str(root), check=True, capture_output=True, text=True)
    return seeded.stdout.strip().splitlines()[-1]


def serve(root: Path, port: int, log: Path):
    log.write_text("")
    proc = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "info"],
        cwd=str(root), stdout=log.open("ab"), stderr=subprocess.STDOUT,
    )
    for _ in range(60):
        if b'"GET' in log.read_bytes() or _probe(port):
            return proc
        time.sleep(0.5)
    proc.kill()
    raise SystemExit(f"the throwaway portal on {port} never came up - see {log}")


def _probe(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1):
            return True
    except Exception:
        return False


# document.hidden and document.visibilityState are accessors on Document.prototype,
# so redefining them there is what every poller in app.js actually reads.
SET_HIDDEN = """
(function () {{
  Object.defineProperty(Document.prototype, "hidden",
    {{configurable: true, get: function () {{ return {v}; }}}});
  Object.defineProperty(Document.prototype, "visibilityState",
    {{configurable: true, get: function () {{ return {v} ? "hidden" : "visible"; }}}});
  document.dispatchEvent(new Event("visibilitychange"));
  return document.hidden;
}})()
"""

def tally(urls: list) -> dict:
    """Requests per API path, ignoring the query string."""
    out = {}
    for url in urls:
        path = url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0]
        path = "/" + path
        if path.startswith("/api/"):
            out[path] = out.get(path, 0) + 1
    return out


async def watch(port: int, window: float, slug: str) -> dict:
    """Open the project page, then measure visible / hidden / visible again.

    The count comes from CDP Network events rather than the server's access log
    (uvicorn's access logger is off in this app), which is the better
    measurement anyway: it counts what the BROWSER issued, which is the thing
    that was keeping a connection alive.
    """
    # Same hazard at the far end: the reverse tunnel's port lives on the render
    # machine, where a previous run's stale listener would silently shadow this
    # one and serve a dead forward.
    _ssh("fuser", "-k", f"{port}/tcp")
    tunnel = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-N", "-R",
         f"{port}:127.0.0.1:{port}", BOX])
    try:
        with OwnChrome() as chrome:
            async with websockets.connect(chrome.page_ws(), max_size=8 << 20) as ws:
                msg_id = 0
                seen: list = []
                pending: dict = {}
                loop = asyncio.get_running_loop()

                async def reader():
                    async for raw in ws:
                        m = json.loads(raw)
                        if "id" in m:
                            fut = pending.pop(m["id"], None)
                            if fut and not fut.done():
                                fut.set_result(m)
                        elif m.get("method") == "Network.requestWillBeSent":
                            seen.append(m["params"]["request"]["url"])

                pump = asyncio.create_task(reader())

                async def send(method: str, **params):
                    nonlocal msg_id
                    msg_id += 1
                    fut = loop.create_future()
                    pending[msg_id] = fut
                    await ws.send(json.dumps(
                        {"id": msg_id, "method": method, "params": params}))
                    reply = await asyncio.wait_for(fut, 30)
                    if "error" in reply:
                        raise SystemExit(f"{method}: {reply['error']}")
                    return reply.get("result", {})

                await send("Page.enable")
                await send("Network.enable")
                await send("Page.navigate",
                           url=f"http://127.0.0.1:{port}/project/{slug}")
                await asyncio.sleep(5)  # let the page settle before counting

                result = {}
                mark = len(seen)
                await asyncio.sleep(window)
                result["visible"] = tally(seen[mark:])

                # Backgrounding the tab, as the page experiences it.
                #
                # Emulation.setPageVisibilityOverride is the obvious way and no
                # longer exists - modern chromium answers -32601. Headless has
                # no window manager to hide a tab from either, so the visibility
                # API is redefined in the page instead. Everything downstream of
                # that is real: the real app.js, in a real browser, making real
                # requests to a real server, which is what counts them.
                await send("Runtime.evaluate", expression=SET_HIDDEN.format(v="true"),
                           returnByValue=True)
                mark = len(seen)
                await asyncio.sleep(window)
                result["hidden"] = tally(seen[mark:])

                mark = len(seen)
                await send("Runtime.evaluate", expression=SET_HIDDEN.format(v="false"),
                           returnByValue=True)
                await asyncio.sleep(2)
                result["onReturn2s"] = tally(seen[mark:])
                pump.cancel()
                return result
    finally:
        tunnel.terminate()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=40.0,
                    help="seconds to count for in each state")
    ap.add_argument("--before-ref", default="HEAD",
                    help="git ref whose app.js is the 'before' (default HEAD)")
    args = ap.parse_args()

    runs = [
        ("before (%s)" % args.before_ref, Path("/tmp/portal-poll-before"), 8811, args.before_ref),
        ("after (working tree)", Path("/tmp/portal-poll-after"), 8812, None),
    ]
    report = {}
    for label, root, port, ref in runs:
        log = Path(f"/tmp/portal-poll-{port}.log")
        print(f"--- {label} on {port}", flush=True)
        slug = build(root, ref)
        proc = serve(root, port, log)
        try:
            report[label] = await watch(port, args.window, slug)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        print(json.dumps(report[label], indent=2), flush=True)

    print("\n=== requests in %.0fs, one open tab ===" % args.window)
    for label, r in report.items():
        vis = sum(r["visible"].values())
        hid = sum(r["hidden"].values())
        print(f"{label:26} visible {vis:4}   hidden {hid:4}   "
              f"within 2s of returning {sum(r['onReturn2s'].values()):3}")
    Path("/tmp/poll-traffic.json").write_text(json.dumps(report, indent=2))
    print("MEASURE COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

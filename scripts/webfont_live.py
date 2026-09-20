#!/usr/bin/env python3
"""Prove in a real browser that the font is ours and Google is never asked.

The unit tests grep the templates and read the response headers. Neither can
answer the question that actually matters, which is what a browser *does*:
a `@font-face` whose `url()` 404s falls back to the system monospace silently,
which on a dark terminal page looks very nearly right - so "the page renders"
is not evidence the font arrived, and a screenshot alone would not have caught
a flattened vendor directory.

So this drives the real headless chromium on the render machine with the CDP
`Network` domain on, loads a page, and reports:

  * every request URL the page made, and specifically whether any of them went
    to a Google font host (the defect this change exists to remove),
  * the status and `cache-control` of the font stylesheet and of the `.woff2`
    the page actually pulled,
  * `document.fonts.check()` for Fira Code, and the real glyph advance width
    measured against a fallback, which is the only thing that distinguishes
    "Fira Code loaded" from "the browser silently used monospace".

The server is a scratch portal on a spare port with an empty temp data dir -
never the live database, which would mean this check filed rows on real
projects. It binds 0.0.0.0 because the browser is on another machine.

    venv/bin/python scripts/webfont_live.py [--shot PATH]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from deploy.interact import Chrome  # noqa: E402


def browser_reachable_host() -> str:
    """The address for this machine that the *browser* can resolve.

    The URL is resolved on the render machine, not here, and that machine
    cannot necessarily resolve this server by name - on this estate it reaches
    it only by LAN address. So the name from the install's own settings is
    resolved to an address here and the address is what gets handed over.
    Falls back to the name if resolution fails, and `--host` overrides both.
    """
    from app import config

    # Not gethostbyname(): on a box whose own name is in /etc/hosts that
    # answers 127.0.1.1, which is this machine to nobody but this machine.
    # Opening a UDP socket toward an off-box address binds no packets but does
    # make the kernel choose the outward-facing interface, and its local
    # address is the one a machine on the same LAN can reach.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, routed nowhere
            return str(s.getsockname()[0])
    except OSError:
        return config.SITE.host


GOOGLE_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")

# Measured in the page: render the same string in Fira Code and in a font that
# certainly is not there, and compare advance widths. Equal widths mean the
# browser fell back, whatever `document.fonts.check` claims about a face it has
# merely *declared*. `document.fonts.load` first, because unicode-range means
# nothing is fetched until something needs rendering.
PROBE = """
(async () => {
  await document.fonts.load('400 16px "Fira Code"', 'ABC abc 012 ()=>');
  await document.fonts.ready;
  const span = document.createElement('span');
  span.style.cssText = 'position:absolute;visibility:hidden;font-size:64px;white-space:pre';
  span.textContent = 'ABC abc 012 ()=> mmmiiillll';
  document.body.appendChild(span);
  const width = (family) => { span.style.fontFamily = family; return span.getBoundingClientRect().width; };
  const fira = width('"Fira Code"');
  const mono = width('monospace');
  const nonsense = width('"NoSuchFontAnywhere"');
  span.remove();
  const body = getComputedStyle(document.body).fontFamily;
  return JSON.stringify({
    declared: document.fonts.check('400 16px "Fira Code"'),
    loaded: [...document.fonts].filter(f => f.family === 'Fira Code' && f.status === 'loaded').length,
    fira, mono, nonsense,
    distinct_from_fallback: Math.abs(fira - nonsense) > 0.5,
    body_font_family: body,
  });
})()
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


def start_scratch_portal(port: int) -> tempfile.TemporaryDirectory:
    """A portal on `port` against an empty data dir, in a background thread.

    `PORTAL_SMOKE_TEST=1` before `app.main` is imported, and it is not
    optional. Without it this process is a *service* start: the worker loop
    ticks, an empty board sends its first tick straight to the daily reflect,
    and a throwaway server meant only to render one page spawns a real, billed
    `claude -p` within seconds. It also reconciles "orphaned" runs - which
    against the live data directory means settling the running service's runs -
    and binds the preview server's fixed port out from under it. The flag is
    read once at startup, so it has to be set before the import.
    """
    os.environ["PORTAL_SMOKE_TEST"] = "1"
    tmp = tempfile.TemporaryDirectory(prefix="webfont-live-")
    root = Path(tmp.name)
    from app import config

    config.DATA_DIR = root
    config.DB_PATH = root / "portal.db"
    config.MEMORY_DIR = root / "memory"
    config.PROJECTS_DIR = root / "projects"
    config.RUNS_DIR = root / "runs"
    config.TASKS_DIR = root / "tasks"
    config.INCOMING_DIR = root / "incoming"
    # Separate module constants, not derived from MEMORY_DIR: leaving them is
    # how a scratch server ends up compacting the real learnings file.
    config.PROFILE_MD = root / "memory" / "profile.md"
    config.LEARNINGS_MD = root / "memory" / "learnings.md"
    config.SUGGESTIONS_MD = root / "memory" / "suggestions.md"

    import uvicorn

    from app import main

    server = uvicorn.Server(
        uvicorn.Config(main.app, host="0.0.0.0", port=port, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return tmp
        except OSError:
            time.sleep(0.1)
    raise SystemExit(f"the scratch portal never came up on :{port}")


async def measure(url: str, shot: Path | None) -> dict:
    with Chrome(1200, 900) as chrome:
        ws_url = chrome.page_ws()
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            msg_id = 0
            requests: list[str] = []
            responses: dict[str, dict] = {}

            async def send(method: str, **params):
                nonlocal msg_id
                msg_id += 1
                mine = msg_id
                await ws.send(json.dumps({"id": mine, "method": method, "params": params}))
                while True:
                    reply = json.loads(await ws.recv())
                    if reply.get("method") == "Network.requestWillBeSent":
                        requests.append(reply["params"]["request"]["url"])
                    elif reply.get("method") == "Network.responseReceived":
                        r = reply["params"]["response"]
                        responses[r["url"]] = {
                            "status": r["status"],
                            "cache-control": {
                                k.lower(): v for k, v in r.get("headers", {}).items()
                            }.get("cache-control", ""),
                            "type": reply["params"]["type"],
                        }
                    elif reply.get("id") == mine:
                        if "error" in reply:
                            raise SystemExit(f"{method}: {reply['error']}")
                        return reply.get("result", {})

            await send("Network.enable")
            # Against a server that only exists for this run, a cached response
            # from a previous one would read as the fix working when it is not.
            await send("Network.setCacheDisabled", cacheDisabled=True)
            await send("Page.enable")
            await send("Emulation.setFocusEmulationEnabled", enabled=True)
            await send("Page.navigate", url=url)
            await asyncio.sleep(3)

            result = await send(
                "Runtime.evaluate", expression=PROBE, awaitPromise=True, returnByValue=True
            )
            if result.get("exceptionDetails"):
                raise SystemExit(f"probe failed: {result}")
            probe = json.loads(result["result"]["value"])

            if shot:
                data = await send("Page.captureScreenshot")
                shot.write_bytes(base64.b64decode(data["data"]))

            return {"requests": requests, "responses": responses, "probe": probe}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", type=Path, default=None)
    ap.add_argument("--path", default="/style", help="the portal page to load")
    ap.add_argument("--host", default=None, help="the address the browser should open")
    args = ap.parse_args()

    port = free_port()
    tmp = start_scratch_portal(port)
    url = f"http://{args.host or browser_reachable_host()}:{port}{args.path}"
    print(f"scratch portal on {url}\n", flush=True)
    try:
        out = asyncio.run(measure(url, args.shot))
    finally:
        tmp.cleanup()

    requests = out["requests"]
    responses = out["responses"]
    probe = out["probe"]

    to_google = [u for u in requests if any(h in u for h in GOOGLE_HOSTS)]
    font_css = [u for u in requests if u.endswith("/font.css")]
    woff2 = [u for u in requests if u.endswith(".woff2")]

    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"{'ok  ' if passed else 'FAIL'}  {label}{': ' + detail if detail else ''}")

    print(f"{len(requests)} requests from the page\n")
    check("nothing was asked of a Google font host", not to_google, ", ".join(to_google))
    check("the font stylesheet came from this origin", bool(font_css), font_css[0] if font_css else "not requested")
    if font_css:
        r = responses.get(font_css[0], {})
        check("  it answered 200", r.get("status") == 200, str(r.get("status")))
        check("  cached for a year, immutable", "immutable" in r.get("cache-control", "") and "31536000" in r.get("cache-control", ""), r.get("cache-control", "<none>"))
    check("a .woff2 subset was actually fetched", bool(woff2), f"{len(woff2)} of 7 (unicode-range fetches only what it renders)")
    if woff2:
        r = responses.get(woff2[0], {})
        check("  it answered 200", r.get("status") == 200, str(r.get("status")))
        check("  cached for a year, immutable", "immutable" in r.get("cache-control", ""), r.get("cache-control", "<none>"))
    check("the browser reports the face as loaded", probe["loaded"] > 0, f"{probe['loaded']} face(s)")
    check(
        "Fira Code is really rendering, not a silent monospace fallback",
        probe["distinct_from_fallback"],
        f"Fira Code {probe['fira']:.1f}px vs fallback {probe['nonsense']:.1f}px for the same string",
    )
    print(f"\nbody font-family: {probe['body_font_family']}")
    for u in sorted(set(requests)):
        print(f"  {responses.get(u, {}).get('status', '?')}  {u}")
    print("\n" + ("every check passed" if ok else "SOMETHING IS WRONG"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

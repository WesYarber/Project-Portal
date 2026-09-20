#!/usr/bin/env python
"""Render a portal page on the render machine *after clicking things*, and bring the PNG back.

`deploy/screenshot.sh` renders a page as it arrives. That is enough for
anything served whole, and no use at all for anything that only exists after
the browser has done something - the workspace file tree arrives with every
folder shut and empty, so a plain screenshot of it proves the shut state and
nothing about the fetch that fills a folder.

So this drives chromium over the DevTools protocol instead:

  * chromium is started on the render machine with --remote-debugging-port,
    listening on loopback only - the port is reached through an ssh tunnel
    rather than exposed on the LAN, because an open DevTools port is a
    remote-control channel for that browser;
  * the script runs here, in the portal's venv, which already has `websockets`
    (the render machine may have no pip at all, so installing a client there
    would have meant apt and sudo for a debugging tool);
  * the screenshot comes back over the same tunnel as base64 in the CDP
    response, so there is no scp step and nothing left on the render machine
    but the profile directory.

The machine comes from `render_host` in portal.toml (see app/site.py); $BOX
overrides it for a one-off.

Usage:
    venv/bin/python deploy/interact.py URL OUT.png [--js 'expr'] [--wait 2.5]

`--js` may be repeated; each expression is awaited (a promise is fine) in page
context, in order, before the shot is taken.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402 - after the path fix-up

# Env first so one run can target another machine without editing config.
BOX = os.environ.get("BOX") or config.SITE.render_host
PORT = 9222


def _ssh(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", BOX, *cmd],
        capture_output=True,
        text=True,
    )


class Chrome:
    """A headless chromium on the render machine, reachable on localhost:PORT here."""

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.tunnel: subprocess.Popen | None = None

    def __enter__(self) -> "Chrome":
        _ssh("pkill", "-f", "remote-debugging-port=%d" % PORT)
        # Old headless: --headless=new never fires the load event on a page
        # holding an SSE stream open, and every portal page holds one.
        _ssh(
            "nohup /snap/bin/chromium --headless --disable-gpu --no-sandbox "
            f"--hide-scrollbars --no-first-run --remote-debugging-port={PORT} "
            f"--window-size={self.width},{self.height} "
            "--user-data-dir=$HOME/.portal-interact-profile "
            "about:blank >/dev/null 2>&1 & sleep 2"
        )
        # ExitOnForwardFailure, because the default is the quiet disaster: with
        # the local port already held, ssh prints "Address already in use" to a
        # stderr nobody reads and then *keeps running* with no forward at all.
        # The readiness poll below then succeeds against whatever else is
        # listening, and the run drives a browser it did not start, on a
        # machine it did not pick. An 18-day-old orphan tunnel was doing
        # exactly that here. With the flag, ssh exits, and `_own_the_forward`
        # gets to decide what that means.
        self.tunnel = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-N",
             "-L", f"{PORT}:127.0.0.1:{PORT}", BOX],
        )
        # The tunnel and the browser both need a moment; poll rather than sleep
        # a guessed amount, or this is either flaky or slow.
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1) as r:
                    reached = json.loads(r.read())
                self._own_the_forward(reached)
                return self
            except SystemExit:
                raise
            except Exception:
                time.sleep(0.5)
        raise SystemExit("chromium on the render machine never answered on the tunnel")

    def _own_the_forward(self, reached: dict) -> None:
        """Refuse to drive a browser reached through somebody else's tunnel.

        The identity is read every time, and deliberately not skipped when our
        own ssh still looks alive. `ExitOnForwardFailure` makes ssh exit
        *asynchronously*, while a stray forward answers the local port
        instantly - so the very first readiness poll wins that race and finds
        our tunnel mid-death, reporting it as the owner of a forward it had
        just failed to bind. Measured here on 2026-09-20, which is why this
        costs one ssh round trip per run rather than none.

        What is compared: chromium's /json/version carries a
        `webSocketDebuggerUrl` with a per-browser-session id in it, so reading
        it *directly on the render machine* over ssh says which endpoint the
        local port really reaches. Same id, and riding whoever's forward it is
        is harmless - say so and carry on, because failing would break every
        screenshot on the box until somebody cleaned up a stray that was doing
        no harm. Different id, or no answer, and the run stops: the
        alternative is a screenshot of the wrong machine's browser, which is a
        lie shaped exactly like evidence.
        """
        probe = _ssh("curl", "-sS", f"http://127.0.0.1:{PORT}/json/version")
        try:
            on_box = json.loads(probe.stdout)
        except ValueError:
            on_box = {}
        ours = on_box.get("webSocketDebuggerUrl")
        if not ours or ours != reached.get("webSocketDebuggerUrl"):
            raise SystemExit(
                f"local port {PORT} is forwarded by another process, and it does not "
                f"reach the browser on {BOX}. Refusing to drive it. Find the holder "
                f"with `ss -tlnp | grep {PORT}` and kill that pid."
            )
        # Polled after the probe, not before it: the ssh round trip above is
        # the grace period ssh needs to have finished exiting, so by here a
        # dead tunnel really means we did not bind the port.
        if self.tunnel is not None and self.tunnel.poll() is not None:
            print(
                f"note: local port {PORT} was already forwarded by another process; "
                f"it reaches the same browser on {BOX}, so riding it.",
                file=sys.stderr,
            )

    def __exit__(self, *exc) -> None:
        if self.tunnel:
            self.tunnel.terminate()
        _ssh("pkill", "-f", "remote-debugging-port=%d" % PORT)

    def page_ws(self) -> str:
        """The about:blank tab chromium was started with.

        Not /json/new - recent chromium answers that with 405 unless it is a
        PUT, and there is already exactly one page target here.
        """
        with urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as r:
            targets = json.load(r)
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise SystemExit("chromium started with no page target")
        return pages[0]["webSocketDebuggerUrl"]


async def shoot(url: str, out: str, js: list[str], wait: float,
                width: int, height: int, viewport_only: bool = False) -> None:
    with Chrome(width, height) as chrome:
        ws_url = chrome.page_ws()
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            msg_id = 0

            async def send(method: str, **params):
                nonlocal msg_id
                msg_id += 1
                await ws.send(json.dumps({"id": msg_id, "method": method,
                                          "params": params}))
                while True:
                    reply = json.loads(await ws.recv())
                    if reply.get("id") == msg_id:
                        if "error" in reply:
                            raise SystemExit(f"{method}: {reply['error']}")
                        return reply.get("result", {})

            await send("Page.enable")
            # Headless chromium's window never has OS focus, so the page reports
            # document.hasFocus() false and fires *no* focus, blur, focusin or
            # focusout events at all - element.focus() moves activeElement and
            # tells nobody. Anything hung off losing focus (an on-blur save, a
            # validate-on-leave, a close-on-focus-loss menu) then looks broken
            # here while working in every real browser: a false negative in the
            # direction that wastes an afternoon. This makes the page agree with
            # what a focused window reports about itself, and must be sent
            # before the navigation the page under test arrives on.
            await send("Emulation.setFocusEmulationEnabled", enabled=True)
            await send("Page.navigate", url=url)
            await asyncio.sleep(wait)
            for expr in js:
                result = await send(
                    "Runtime.evaluate", expression=expr,
                    awaitPromise=True, returnByValue=True,
                )
                if result.get("exceptionDetails"):
                    raise SystemExit(f"page JS failed: {expr}\n{result}")
                # Printed, not discarded: half the reason to drive the page is
                # to ask it a question (what is this element's computed style,
                # did that handler run), and a shot of the answer is a poor
                # substitute for the answer.
                value = result.get("result", {}).get("value")
                if value is not None:
                    print(f"js> {value}")
                await asyncio.sleep(wait)
            # captureBeyondViewport renders the whole document, which puts every
            # `position: fixed` element back at the top of the page - so the
            # lightbox, the context menu and the selection bar are all
            # unphotographable that way. --viewport captures what a person
            # would actually be looking at instead.
            shot = await send(
                "Page.captureScreenshot", captureBeyondViewport=not viewport_only
            )
            with open(out, "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
    print(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--js", action="append", default=[])
    ap.add_argument("--wait", type=float, default=2.0)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=1600)
    ap.add_argument("--viewport", action="store_true",
                    help="capture the viewport only (keeps fixed elements in place)")
    args = ap.parse_args()
    if not BOX:
        raise SystemExit(
            "No render machine configured. Set render_host = \"user@host\" in "
            "portal.toml (a machine with chromium and key-based SSH from here), "
            "or run with BOX=user@host."
        )
    asyncio.run(shoot(args.url, args.out, args.js, args.wait,
                      args.width, args.height, args.viewport))


if __name__ == "__main__":
    main()

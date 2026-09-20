#!/usr/bin/env python3
"""Measure focus emulation against the live portal in a real headless chromium.

The unit tests in `tests/test_interact.py` prove the driver *sends*
`Emulation.setFocusEmulationEnabled`. They cannot prove chromium does anything
useful with it, and that is the whole claim. So this drives the real browser on
the render machine, loads a real portal page, and runs the same probe twice on
the same tab: once with focus emulation off (how every screenshot run behaved
until 2026-09-20) and once with it on.

The probe is the portal's own inline title editor (`initTitleRename` in
`app/static/app.js`), chosen because it is deliberately blur-driven and
deliberately harmless: clicking the project name opens an input, and moving
focus away **closes it without saving**. Nothing is renamed, nothing is posted;
the only observable is whether the editor shuts.

    venv/bin/python scripts/focus_live.py [URL] [--shots DIR]

URL defaults to this install's own project page. Pass it explicitly when the
render machine cannot resolve this server's hostname - several of them reach it
only by address - since the URL is resolved by the *browser*, not by this
process.

Prints a line per pass and, with --shots, leaves `focus-off.png` and
`focus-on.png` - the same page, the same script, the editor stuck open in one
and shut in the other.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from app import config  # noqa: E402
from deploy.interact import Chrome  # noqa: E402

DEFAULT_URL = f"{config.SITE.base_url}/project/project-portal"

# One IIFE so the listeners are installed and the page is driven inside a single
# synchronous turn - a focus event that fires between two CDP round trips would
# be missed by a probe split across them.
PROBE = """
(() => {
  const form = document.querySelector('.title-rename');
  if (!form) return JSON.stringify({error: 'no .title-rename on this page'});
  const head = form.querySelector('[data-rename-title]');
  const input = form.querySelector('input[name="title"]');
  const elsewhere = document.querySelector('a[href], button');
  if (!head || !input || !elsewhere) {
    return JSON.stringify({error: 'the rename form is not shaped as expected'});
  }
  const log = [];
  input.addEventListener('focus', () => log.push('input:focus'));
  input.addEventListener('blur', () => log.push('input:blur'));
  document.addEventListener('focusin', () => log.push('doc:focusin'));
  document.addEventListener('focusout', () => log.push('doc:focusout'));

  head.click();
  const opened = !input.hidden;
  const caret = document.activeElement === input;
  elsewhere.focus();
  return JSON.stringify({
    hasFocus: document.hasFocus(),
    opened: opened,
    caretInTheInput: caret,
    closedOnLeaving: input.hidden,
    events: log,
  });
})()
"""


async def probe(url: str, shots: Path | None) -> int:
    with Chrome(1135, 900) as chrome:
        async with websockets.connect(chrome.page_ws(),
                                      max_size=64 * 1024 * 1024) as ws:
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
            verdicts = {}
            for label, enabled in (("off", False), ("on", True)):
                await send("Emulation.setFocusEmulationEnabled", enabled=enabled)
                await send("Page.navigate", url=url)
                await asyncio.sleep(3.0)
                result = await send("Runtime.evaluate", expression=PROBE,
                                    awaitPromise=True, returnByValue=True)
                if result.get("exceptionDetails"):
                    raise SystemExit(f"the probe threw: {result}")
                answer = json.loads(result["result"]["value"])
                if "error" in answer:
                    raise SystemExit(f"focus emulation {label}: {answer['error']}")
                verdicts[label] = answer
                print(f"focus emulation {label}:")
                print(f"  document.hasFocus()   {answer['hasFocus']}")
                print(f"  editor opened         {answer['opened']}")
                print(f"  caret in the input    {answer['caretInTheInput']}")
                print(f"  closed on leaving     {answer['closedOnLeaving']}")
                print(f"  events fired          {answer['events'] or '[]'}")
                if shots:
                    shot = await send("Page.captureScreenshot",
                                      captureBeyondViewport=False)
                    out = shots / f"focus-{label}.png"
                    out.write_bytes(base64.b64decode(shot["data"]))
                    print(f"  shot                  {out}")

    # The point of the run: the same page, the same script, two answers.
    bad = []
    if verdicts["off"]["events"]:
        bad.append("focus emulation off still fired events - chromium changed")
    if verdicts["off"]["closedOnLeaving"]:
        bad.append("the editor closed with focus emulation off - probe is wrong")
    if not verdicts["on"]["events"]:
        bad.append("focus emulation on fired nothing - the fix does not work")
    if not verdicts["on"]["closedOnLeaving"]:
        bad.append("the editor stayed open with focus emulation on")
    if not verdicts["on"]["hasFocus"]:
        bad.append("the page still reports itself unfocused")
    for line in bad:
        print(f"FAIL  {line}")
    if not bad:
        print("\nok  blur is invisible without the fix and fires with it")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default=DEFAULT_URL)
    ap.add_argument("--shots", type=Path, default=None)
    args = ap.parse_args()
    if args.shots:
        args.shots.mkdir(parents=True, exist_ok=True)
    return asyncio.run(probe(args.url, args.shots))


if __name__ == "__main__":
    sys.exit(main())

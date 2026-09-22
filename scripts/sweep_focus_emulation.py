#!/usr/bin/env python3
"""Delete-the-fix sweep for focus emulation in the CDP driver.

The defect this guards is a *false negative*, which is the expensive kind: the
driver worked, the app worked, and the driver reported the app's on-blur
behavior as never happening. Every mutation here puts back one shade of that -
the command not sent, sent disabled, sent as a string, sent after the page has
already loaded - and each must be caught, because none of them changes a single
pixel of the screenshot that comes back.

The ordering mutation is the one to read first. Moving
`Emulation.setFocusEmulationEnabled` to *after* `Page.navigate` still sends the
command, still returns a shot, and still leaves the page under test to load,
bind its handlers and settle in the unfocused world. That is the original bug
wearing the fix's clothes.

Follows docs/verifying-with-mutations.md: refuses a dirty tree, restores however
it dies, prints `SWEEP COMPLETE`, and counts a skipped mutation as a broken
sweep rather than a lower score.
"""
from __future__ import annotations

import atexit
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "deploy"))
import sweeplib  # noqa: E402  (a sibling script, not on the path)

IA = ROOT / "deploy" / "interact.py"
SUITE = ["tests/test_interact.py"]

ORIGINAL = sweeplib.Originals()


def restore_all() -> None:
    sweeplib.restore(ORIGINAL)


# (file, find, replace, label)
MUTATIONS = [
    # --- the fix itself ------------------------------------------------------
    (IA,
     '            await send("Emulation.setFocusEmulationEnabled", enabled=True)\n'
     '            await send("Page.navigate", url=url)',
     '            await send("Page.navigate", url=url)',
     "the command is deleted, so blur and focusout never fire again"),

    (IA,
     '            await send("Emulation.setFocusEmulationEnabled", enabled=True)',
     '            await send("Emulation.setFocusEmulationEnabled", enabled=False)',
     "the command is sent asking for the broken state explicitly"),

    (IA,
     '            await send("Emulation.setFocusEmulationEnabled", enabled=True)',
     '            await send("Emulation.setFocusEmulationEnabled", enabled="true")',
     'the flag is the string "true", which CDP rejects as a type error'),

    (IA,
     '            await send("Emulation.setFocusEmulationEnabled", enabled=True)',
     '            await send("Emulation.setFocusEmulationEnabled")',
     "the flag is omitted, which chromium reads as no change at all"),

    (IA,
     '            await send("Emulation.setFocusEmulationEnabled", enabled=True)',
     '            await send("Emulation.setFocusEmulation", enabled=True)',
     "the command is misspelled, so the driver errors out on a live browser"),

    # --- where it is sent, which is half the fix -----------------------------
    (IA,
     '            await send("Emulation.setFocusEmulationEnabled", enabled=True)\n'
     '            await send("Page.navigate", url=url)',
     '            await send("Page.navigate", url=url)\n'
     '            await send("Emulation.setFocusEmulationEnabled", enabled=True)',
     "focus is emulated only after the page under test has already loaded"),

    (IA,
     '            await send("Page.enable")\n',
     "",
     "Page.enable is dropped, so nothing else in the sequence is load-bearing"),

    # --- the rest of the conversation, which had no test before this ---------
    (IA,
     '            await send("Page.navigate", url=url)',
     '            await send("Page.navigate", url="about:blank")',
     "every shot is of a blank page rather than the URL asked for"),

    (IA,
     "                if result.get(\"exceptionDetails\"):\n"
     "                    raise SystemExit(f\"page JS failed: {expr}\\n{result}\")",
     "                if False:\n"
     "                    raise SystemExit(f\"page JS failed: {expr}\\n{result}\")",
     "a page script that throws is ignored and a shot of the wrong state returned"),

    (IA,
     "                value = result.get(\"result\", {}).get(\"value\")\n"
     "                if value is not None:\n"
     '                    print(f"js> {value}")',
     "                value = result.get(\"result\", {}).get(\"value\")\n"
     "                if value is None:\n"
     '                    print(f"js> {value}")',
     "the answer a driven page gives back is swallowed and 'None' printed"),

    (IA,
     "            shot = await send(\n"
     '                "Page.captureScreenshot", captureBeyondViewport=not viewport_only\n'
     "            )",
     "            shot = await send(\n"
     '                "Page.captureScreenshot", captureBeyondViewport=bool(viewport_only)\n'
     "            )",
     "--viewport is inverted, so fixed elements are unphotographable again"),

    (IA,
     "                    if reply.get(\"id\") == msg_id:",
     "                    if True:",
     "an interleaved CDP event is taken as the reply to the last command"),

    (IA,
     "                        if \"error\" in reply:\n"
     "                            raise SystemExit(f\"{method}: {reply['error']}\")",
     "                        if \"err\" in reply:\n"
     "                            raise SystemExit(f\"{method}: {reply['error']}\")",
     "a refused CDP command is reported as a successful one"),

    (IA,
     "        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:",
     "        async with websockets.connect(ws_url) as ws:",
     "the frame limit goes back to 1 MiB, which drops a full-page screenshot"),

    (IA,
     "        pages = [t for t in targets if t.get(\"type\") == \"page\"]",
     "        pages = list(targets)",
     "a background page is driven instead of the tab"),

    (IA,
     "        if not pages:\n"
     '            raise SystemExit("chromium started with no page target")',
     "        if pages is None:\n"
     '            raise SystemExit("chromium started with no page target")',
     "a browser with no tab dies on an IndexError instead of saying so"),

    (IA,
     "import websockets",
     "import websockets  # noqa",
     "(control) a comment-only edit must NOT go red"),
]


def anchors() -> list[tuple[Path, str]]:
    """Every (file, exact string) this sweep mutates, for tests/test_sweep_anchors.py."""
    return [(path, find) for path, find, _repl, _label in MUTATIONS]


def run_suite() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [str(ROOT / "venv" / "bin" / "python"), "-m", "pytest", *SUITE, "-q",
         "--no-header", "--tb=no", "-p", "no:warnings", "-p", "no:randomly"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=240,
    )
    failures = [
        line.split("::")[-1].split()[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED")
    ]
    return proc.returncode, failures


# A mutation whose label contains this substring is the only one run. Lets one
# re-check take twenty seconds instead of the whole sweep.
ONLY = sys.argv[1] if len(sys.argv) > 1 else ""


def main() -> int:
    if subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT).returncode != 0:
        sys.exit("REFUSING: tree is dirty. A sweep must start from a committed tree.")
    for path in {p for p, _f, _r, _l in MUTATIONS}:
        ORIGINAL.remember(path)
    atexit.register(restore_all)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit("killed by signal"))

    caught = 0
    ran = 0
    escaped: list[str] = []
    skipped: list[str] = []
    for i, (path, find, replace, label) in enumerate(MUTATIONS, 1):
        if ONLY and ONLY not in label:
            continue
        text = ORIGINAL[path]
        if text.count(find) != 1:
            print(f"{i:2}. SKIP (anchor occurs {text.count(find)}x) - {label}", flush=True)
            skipped.append(label)
            continue
        ran += 1
        path.write_text(text.replace(find, replace, 1), encoding="utf-8")
        rc, failures = run_suite()
        restore_all()
        control = label.startswith("(control)")
        if (rc == 0) != control:
            print(f"{i:2}. ESCAPED  - {label}", flush=True)
            escaped.append(label)
        else:
            caught += 1
            print(f"{i:2}. {'held    ' if control else 'caught  '} - {label}", flush=True)
            if failures:
                print(f"      by {', '.join(sorted(set(failures))[:3])}", flush=True)

    print(f"\n{caught}/{ran} caught, {len(escaped)} escaped, {len(skipped)} skipped", flush=True)
    for label in skipped:
        print(f"  skipped (anchor no longer holds): {label}", flush=True)
    print("SWEEP COMPLETE", flush=True)
    return 1 if skipped or escaped else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Delete-the-fix sweep for owning the local DevTools forward (todo #1314).

The bug being guarded is silent in both directions, which is why it survived
eighteen days. A stray `ssh -L` holding the local port made plain ssh print to
a stderr nobody reads and keep running forwardless; the readiness poll then
succeeded against the stray, and every screenshot since was taken through a
tunnel this code did not own. Nothing failed. Nothing looked wrong. The
screenshot came back.

Two mutations are the ones to read. Putting the identity check back behind
`if self.tunnel.poll() is None` restores a version that *passed its own tests*
and did nothing in real life, because ssh exits asynchronously and the stray
answers instantly, so the check is skipped in exactly the case it exists for.
And making a missing or unparseable probe answer count as agreement turns "I
could not tell" into "yes", which is how a check ends up rubber-stamping the
thing it was written to catch.

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
IA = ROOT / "deploy" / "interact.py"
SUITE = ["tests/test_interact.py"]

ORIGINAL: dict[Path, str] = {}


def restore_all() -> None:
    for path, text in ORIGINAL.items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"  restored {path.name}", flush=True)


# (file, find, replace, label)
MUTATIONS = [
    # --- the forward has to fail loudly rather than linger ------------------
    (IA,
     '["ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-N",',
     '["ssh", "-o", "BatchMode=yes", "-N",',
     "ssh lingers forwardless again, so a stray owns every screenshot silently"),

    # --- the check must not be skippable ------------------------------------
    (IA,
     "        probe = _ssh(\"curl\", \"-sS\", f\"http://127.0.0.1:{PORT}/json/version\")",
     "        if self.tunnel is None or self.tunnel.poll() is None:\n"
     "            return\n"
     "        probe = _ssh(\"curl\", \"-sS\", f\"http://127.0.0.1:{PORT}/json/version\")",
     "the check goes back behind a liveness test it loses a race to"),

    (IA,
     "        self._own_the_forward(reached)\n",
     "",
     "the check is never called, so nothing verifies the forward at all"),

    # --- 'I could not tell' must never read as 'yes' -------------------------
    (IA,
     "        if not ours or ours != reached.get(\"webSocketDebuggerUrl\"):",
     "        if ours and ours != reached.get(\"webSocketDebuggerUrl\"):",
     "a probe that answers nothing is taken as agreement"),

    (IA,
     "        if not ours or ours != reached.get(\"webSocketDebuggerUrl\"):",
     "        if not ours:",
     "any browser that answers is accepted, whichever machine it is on"),

    (IA,
     "        if not ours or ours != reached.get(\"webSocketDebuggerUrl\"):",
     "        if not ours or ours == reached.get(\"webSocketDebuggerUrl\"):",
     "the comparison is inverted: the right browser is refused, the wrong one driven"),

    (IA,
     "        except ValueError:\n            on_box = {}",
     "        except ValueError:\n            on_box = reached",
     "an unparseable probe answer is replaced with the answer it was checking"),

    (IA,
     "            raise SystemExit(\n"
     "                f\"local port {PORT} is forwarded by another process, and it does not \"",
     "            print(\n"
     "                f\"local port {PORT} is forwarded by another process, and it does not \"",
     "the mismatch is printed instead of stopping the run"),

    # --- what is compared ----------------------------------------------------
    (IA,
     '        ours = on_box.get("webSocketDebuggerUrl")',
     '        ours = on_box.get("Browser")',
     "the chromium *version* is compared, which two different boxes share"),

    (IA,
     '        probe = _ssh("curl", "-sS", f"http://127.0.0.1:{PORT}/json/version")',
     '        probe = _ssh("curl", "-sS", f"http://127.0.0.1:{PORT}/json/list")',
     "the probe reads the tab list, which carries no browser identity"),

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
        ORIGINAL[path] = path.read_text(encoding="utf-8")
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

#!/usr/bin/env python3
"""Delete-the-fix sweep for `usage.anatomy` and `usage.turn_trend`.

Follows all three safety rules in docs/verifying-with-mutations.md:

1. REFUSES a dirty tree, and restores however it dies (atexit + SIGTERM/SIGINT
   that call sys.exit, since a bare handler that returns does not run atexit).
2. Prints `SWEEP COMPLETE` at the end; nothing may read app/ until it appears.
3. The caller re-runs the plain suite afterwards.

Scoped to the owning test file rather than the full suite - the same trade
commit b1dcd35 made and for the same reason. That trade is real and asymmetric:
a CAUGHT here is conclusive, but an ESCAPED only means *this file* does not hold
the line, and has to be re-checked against the whole suite before it is believed.
"""
import atexit, signal, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USAGE = ROOT / "app" / "usage.py"
TESTS = "tests/test_breakdown.py"

if subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT).returncode != 0:
    sys.exit("REFUSING: tree is dirty. A sweep must start from a committed tree.")

ORIGINAL = USAGE.read_text(encoding="utf-8")

def restore():
    USAGE.write_text(ORIGINAL, encoding="utf-8")

atexit.register(restore)
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_: sys.exit(f"killed by signal"))

# (label, find, replace). Each is one decision the code makes.
MUTATIONS = [
    ("prompt charged once per run, not per turn",
     "prompt += int(pbytes / BYTES_PER_TOKEN) * turns",
     "prompt += int(pbytes / BYTES_PER_TOKEN)"),
    ("CLI head charged once per run, not per turn",
     "cli += CLI_HEAD_TOKENS * turns",
     "cli += CLI_HEAD_TOKENS"),
    ("CLI head folded into the prompt's share",
     "cli += CLI_HEAD_TOKENS * turns",
     "prompt += CLI_HEAD_TOKENS * turns"),
    ("runs with no recorded turns are counted",
     "if not turns or not read or not pbytes:",
     "if not read or not pbytes:"),
    ("runs with no recorded prompt size are counted",
     "if not turns or not read or not pbytes:",
     "if not turns or not read:"),
    ("prompt share is not clamped",
     "prompt_pct = min(100.0, 100.0 * prompt / reads)",
     "prompt_pct = 100.0 * prompt / reads"),
    ("cli share is not clamped against the prompt's",
     "cli_pct = min(100.0 - prompt_pct, 100.0 * cli / reads)",
     "cli_pct = min(100.0, 100.0 * cli / reads)"),
    ("the three slices no longer close to 100",
     "run_pct = 100.0 - prompt_pct - cli_pct",
     "run_pct = 100.0 - prompt_pct"),
    ("a one-day window still reports a trend",
     "if len(live) < 2:",
     "if len(live) < 1:"),
    ("idle days are not filtered out of the trend",
     'live = [b for b in buckets if b.get("runs") and b.get("turns")]',
     "live = list(buckets)"),
    ("the window is not split in half",
     "half = len(live) // 2",
     "half = 1"),
    ("a change from a zero baseline divides anyway",
     "if not old:\n        return 0",
     "if False:\n        return 0"),
]

caught = escaped = skipped = 0
for label, find, repl in MUTATIONS:
    if find not in ORIGINAL:
        print(f"SKIP (pattern missing): {label}", flush=True)
        skipped += 1
        continue
    USAGE.write_text(ORIGINAL.replace(find, repl, 1), encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "pytest", TESTS, "-qx", "--no-header", "-p", "no:randomly"],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"ESCAPED: {label}", flush=True)
        escaped += 1
    else:
        print(f"caught:  {label}", flush=True)
        caught += 1
    restore()

print(f"\n{caught} caught, {escaped} escaped, {skipped} skipped, of {len(MUTATIONS)}")
print("SWEEP COMPLETE", flush=True)

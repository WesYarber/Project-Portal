#!/usr/bin/env python3
"""Delete-the-fix sweep for the sweep-anchor check (tests/test_sweep_anchors.py).

Unusual shape, for a good reason: the thing under test IS a test, so the code
mutated here is `loose_anchors` and the harness around it, and what must go red
is that same file. That is still the right question - "would a weaker version of
this rule still fail?" - and the answers are what say whether 'exactly once'
earns its wording.

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
TA = ROOT / "tests" / "test_sweep_anchors.py"
SUITE = ["tests/test_sweep_anchors.py"]

ORIGINAL: dict[Path, str] = {}


def restore_all() -> None:
    for path, text in ORIGINAL.items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"  restored {path.name}", flush=True)


# (file, find, replace, label)
MUTATIONS = [
    # --- the rule: exactly once, not at-least-once --------------------------
    (TA,
     "        if count != 1:",
     "        if count == 0:",
     "a duplicated anchor passes, so a sweep mutates the wrong line and is believed"),

    (TA,
     "        if count != 1:",
     "        if count > 1:",
     "a vanished anchor passes, which is the whole failure this was written for"),

    (TA,
     "        if count != 1:",
     "        if False:",
     "nothing is ever loose, so the check is decorative"),

    # --- a file that moved rather than a line that moved ---------------------
    (TA,
     "        if not path.is_file():\n"
     '            loose.append(f"file is gone: {path}")\n'
     "            continue",
     "        if not path.is_file():\n"
     "            continue",
     "an anchor on a deleted file is skipped instead of reported"),

    # --- every loose anchor, not just the first ------------------------------
    (TA,
     "            loose.append(f\"{where}: anchor occurs {count}x - {first!r}\")\n"
     "    return loose",
     "            loose.append(f\"{where}: anchor occurs {count}x - {first!r}\")\n"
     "            return loose\n"
     "    return loose",
     "reporting stops at the first loose anchor, hiding the rest of the rot"),

    # --- assert_anchors_hold: the assertion that fires -----------------------
    (TA,
     "    loose = loose_anchors(anchors)\n    if loose:",
     "    loose = loose_anchors(anchors)\n    if False:",
     "the rot is computed and then never raised on"),

    # --- assert_sweep_declares_anchors: the three ways a sweep goes unchecked -
    (TA,
     '    if not hasattr(module, "anchors"):',
     '    if not hasattr(module, "MUTATIONS"):',
     "a sweep with no anchors() at all goes unchecked"),

    (TA,
     "    if not found:\n",
     "    if False:\n",
     "a sweep whose anchors() returns nothing is accepted"),

    (TA,
     "    if len(found) != len(module.MUTATIONS):",
     "    if len(found) > len(module.MUTATIONS):",
     "an anchors() covering only some of its mutations is accepted"),

    # --- the harness finding the sweeps at all -------------------------------
    #
    # The floor's own assertion is the one thing here caught only by this check
    # tripping over its own anchors: nothing else in the repo asserts how many
    # sweeps there are, and a second test saying so would be the same assertion
    # twice, which is why the two that did were merged into one. What it does
    # have is two independent walks of scripts/ - glob and iterdir - so the
    # mutation that matters, the glob going blind, has a real owner.
    (TA,
     'SWEEPS = sorted((ROOT / "scripts").glob("sweep_*.py"))',
     'SWEEPS = sorted((ROOT / "scripts").glob("sweep_nothing_*.py"))',
     "the glob stops finding the sweeps, so every per-sweep check vanishes"),

    (TA,
     "    assert len(SWEEPS) == len(on_disk) >= 8",
     "    assert len(SWEEPS) <= len(on_disk)",
     "the glob may miss sweeps that are on disk (self-referential catch, see above)"),
]


def anchors() -> list[tuple[Path, str]]:
    """Every (file, exact string) this sweep mutates, for tests/test_sweep_anchors.py.

    Yes, this sweep is checked by the very test it mutates. That is not circular:
    the check runs against the committed file, and a mutation applied to it is
    what this script then proves goes red.
    """
    return [(path, find) for path, find, _repl, _label in MUTATIONS]


def run_suite() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [str(ROOT / "venv" / "bin" / "python"), "-m", "pytest", *SUITE, "-q",
         "--no-header", "--tb=no", "-p", "no:warnings", "-p", "no:randomly"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600,
    )
    failures = [
        line.split("::")[-1].split()[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED")
    ]
    return proc.returncode, failures


def main() -> int:
    if subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT).returncode != 0:
        sys.exit("REFUSING: tree is dirty. A sweep must start from a committed tree.")
    ORIGINAL[TA] = TA.read_text(encoding="utf-8")
    atexit.register(restore_all)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit("killed by signal"))

    caught = 0
    skipped: list[str] = []
    for i, (path, find, replace, label) in enumerate(MUTATIONS, 1):
        text = ORIGINAL[path]
        if text.count(find) != 1:
            print(f"{i:2}. SKIP (anchor occurs {text.count(find)}x) - {label}", flush=True)
            skipped.append(label)
            continue
        path.write_text(text.replace(find, replace, 1), encoding="utf-8")
        rc, failures = run_suite()
        restore_all()
        if rc == 0:
            print(f"{i:2}. ESCAPED  - {label}", flush=True)
        else:
            caught += 1
            print(f"{i:2}. caught   - {label}", flush=True)
            print(f"      by {', '.join(sorted(set(failures))[:3])}", flush=True)

    print(f"\n{caught}/{len(MUTATIONS)} caught, {len(skipped)} skipped", flush=True)
    for label in skipped:
        print(f"  skipped (anchor no longer holds): {label}", flush=True)
    print("SWEEP COMPLETE", flush=True)
    return 1 if skipped or caught < len(MUTATIONS) else 0


if __name__ == "__main__":
    sys.exit(main())

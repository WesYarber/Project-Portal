#!/usr/bin/env python3
"""Delete-the-fix sweep for the requirements security floors.

Everything guarded here fails silently, which is the only reason the floor is
worth having at all. A venv running a vulnerable `cryptography` imports fine,
serves fine, signs VAPID fine and passes every other test in this repo; the
sole symptom is a line in a weekly report from another project. So the tests
are the only thing standing between "the floor was raised" and "the floor was
raised and every install actually took it".

Three groups of decision point, and they fail in different directions:

- **The parser.** `_requirements` and `_floors` decide what is even checked. A
  mutation that makes them yield nothing - or drop the `>=` lines specifically
  - turns `test_the_running_venv_satisfies_every_floor` into a loop over an
  empty list, which passes. That is the dangerous shape: a green test enforcing
  nothing, on exactly the file a security fix lives in.
- **The comparison.** `installed >= floor` is the assertion. Flipping it, or
  comparing strings instead of `Version`s ("49.0.0" > "50.0.0" is False by
  luck, but "9.0.0" > "50.0.0" is True), decides whether a downgrade is caught.
- **The floor itself**, in `requirements.txt`. Dropping back to a bare
  `cryptography`, or to a floor below 50.0.0, is the actual regression this
  whole commit exists to prevent.

Three mutations were written, escaped, and are deliberately NOT in the list
below, because no test can catch them and pretending otherwise would be worse
than saying so:

    assert floors["cryptography"] >= Version("50.0.0")
        -> assert floors["cryptography"] >= Version("1.0.0")
    assert {"fastapi", "cryptography", "pytest"} <= names, names
        -> assert names is not None
    assert len(searched) > 20,  ->  assert len(searched) >= 0,

Each weakens an *assertion* rather than any logic. Nothing observable changes,
so the only thing that could notice is a further test of the test, which has
the same hole one level up. That regress has to stop somewhere, and it stops
here - recorded rather than scored. A reviewer wondering whether these were
considered should read this paragraph as the answer: they were, and the honest
count below excludes them.

Follows docs/verifying-with-mutations.md: refuses a dirty tree, restores
however it dies, prints `SWEEP COMPLETE`, and counts a skipped mutation as a
broken sweep rather than a lower score.
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

TEST = ROOT / "tests" / "test_requirements_floors.py"
REQ = ROOT / "requirements.txt"

SUITE = ["tests/test_requirements_floors.py"]

ORIGINAL = sweeplib.Originals()


def restore_all() -> None:
    sweeplib.restore(ORIGINAL)


# (file, find, replace, label)
MUTATIONS = [
    # --- the floor in requirements.txt: the thing being protected -----------
    (REQ,
     "cryptography>=50.0.0",
     "cryptography",
     "the floor is dropped back to a bare name, so pip may resolve to 49.0.0"),

    (REQ,
     "cryptography>=50.0.0",
     "cryptography>=49.0.0",
     "the floor is lowered to the vulnerable version the watch reported"),

    (REQ,
     "cryptography>=50.0.0",
     "cryptography>=5.0.0",
     "a plausible typo: 5.0.0 reads like a floor and forbids nothing"),

    (REQ,
     "cryptography>=50.0.0",
     "cryptography==50.0.0",
     "(control) an exact pin is stricter, not weaker, and must NOT go red"),

    # --- the parser: a mutation here makes the checks vacuous ---------------
    (TEST,
     "        if not line or line.startswith(\"#\"):\n            continue\n        out.append(Requirement(line))",
     "        if not line or line.startswith(\"#\"):\n            continue",
     "the parser collects nothing, so every floor check loops over an empty list"),

    (TEST,
     '        line = raw.split(" #", 1)[0].strip()',
     "        line = raw.strip()",
     "a trailing comment is parsed as part of the requirement"),

    (TEST,
     '        if not line or line.startswith("#"):',
     "        if not line:",
     "comment lines are parsed as requirements, so the file stops parsing at all"),

    (TEST,
     "            if spec.operator in (\">=\", \"==\")",
     "            if spec.operator in (\"<=\", \"<\")",
     "upper bounds are read as floors, so no real floor is ever enforced"),

    (TEST,
     "            if spec.operator in (\">=\", \"==\")",
     '            if spec.operator == "=="',
     "only exact pins count, so every '>=' floor in the file is ignored"),

    (TEST,
     "            out.append((req.name, max(bounds)))",
     "            out.append((req.name, min(bounds)))",
     "the weakest bound wins where several are declared"),

    # --- the comparison that decides pass from fail -------------------------
    (TEST,
     "        if Version(raw) < floor:",
     "        if Version(raw) > floor:",
     "the comparison is inverted: only an up-to-date venv is complained about"),

    (TEST,
     "        if Version(raw) < floor:",
     "        if Version(raw) < Version(raw):",
     "the floor is compared against itself, so nothing is ever unmet"),

    (TEST,
     "        if Version(raw) < floor:",
     "        if raw < str(floor):",
     "strings are compared, so an installed 9.0.0 clears a 50.0.0 floor"),

    (TEST,
     "        if raw is None:\n"
     '            complaints.append(f"{name} is in requirements.txt but not installed")\n'
     "            continue",
     "        if raw is None:\n"
     "            continue",
     "a package that is not installed at all passes its floor"),

    # --- the named cryptography guard ---------------------------------------
    (TEST,
     '    assert "cryptography" in floors, (',
     '    assert "cryptography" not in floors, (',
     "the named guard is inverted and passes only when the floor is gone"),

    # --- the PKCS#7 reachability finding ------------------------------------
    (TEST,
     '    return "pkcs7" in text.lower()',
     '    return "pkcs7" in text',
     "only lowercase pkcs7 is found, so PKCS7SignatureBuilder slips past"),

    (TEST,
     '    return "pkcs7" in text.lower()',
     "    return False",
     "the PKCS#7 check answers no to everything"),

    (TEST,
     "from packaging.version import Version",
     "from packaging.version import Version  # noqa",
     "(control) a comment-only edit must NOT go red"),
]


def anchors() -> list[tuple[Path, str]]:
    """Every (file, exact string) this sweep mutates, for tests/test_sweep_anchors.py."""
    return [(path, find) for path, find, _repl, _label in MUTATIONS]


def run_suite() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [str(ROOT / "venv" / "bin" / "python"), "-m", "pytest", *SUITE, "-q",
         "--no-header", "--tb=no", "-p", "no:warnings", "-p", "no:randomly"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )
    failures = [
        line.split("::")[-1].split()[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED")
    ]
    return proc.returncode, failures


# A mutation whose label contains this substring is the only one run.
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

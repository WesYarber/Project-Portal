"""Delete-the-fix sweep over the Fable 5.1 pin and its CLI version gate.

Every mutation edits app/config.py in place, runs the owning tests, and expects
RED. A mutation that survives is a decision point no test is actually holding.

The safety fence is deliberately outside the code under test: the original
bytes are read once up front and restored in a `finally`, so an exception or a
KeyboardInterrupt still puts the file back. Run it in the FOREGROUND - see
.claude/skills/run-a-mutation-sweep/SKILL.md - and check `git diff` afterwards.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "app" / "config.py"
TESTS = ["tests/test_models.py"]

# (name, old, new) - `old` must appear exactly once, or the mutation is not
# the one described and the sweep says so instead of reporting a false catch.
MUTATIONS = [
    (
        "the version gate is deleted entirely (the pin always applies)",
        "    required = MODEL_MIN_CLI.get(alias)\n"
        "    if required and _version_tuple(cli_version()) < _version_tuple(required):\n"
        "        return alias\n",
        "",
    ),
    (
        "the gate is inverted (new CLIs degrade, old ones get the pin)",
        "if required and _version_tuple(cli_version()) < _version_tuple(required):",
        "if required and _version_tuple(cli_version()) >= _version_tuple(required):",
    ),
    (
        "< becomes <= (the exact required version degrades)",
        "if required and _version_tuple(cli_version()) < _version_tuple(required):",
        "if required and _version_tuple(cli_version()) <= _version_tuple(required):",
    ),
    (
        "the comparison operands are swapped",
        "_version_tuple(cli_version()) < _version_tuple(required)",
        "_version_tuple(required) < _version_tuple(cli_version())",
    ),
    (
        "the versions are compared as strings, not tuples",
        "_version_tuple(cli_version()) < _version_tuple(required)",
        "str(cli_version()) < str(required)",
    ),
    (
        "the `required and` guard is dropped (an ungated pin gets gated too)",
        "if required and _version_tuple(cli_version()) < _version_tuple(required):",
        "if _version_tuple(cli_version()) < _version_tuple(required or '0'):",
    ),
    (
        "a too-old CLI returns the pin anyway instead of the alias",
        "    if required and _version_tuple(cli_version()) < _version_tuple(required):\n"
        "        return alias\n"
        "    return pinned",
        "    if required and _version_tuple(cli_version()) < _version_tuple(required):\n"
        "        return pinned\n"
        "    return pinned",
    ),
    # KNOWN EQUIVALENT, and left in as the record of why. `.get(alias, alias)`
    # with no None check reaches `return pinned` with the alias itself, because
    # an unpinned alias has no MODEL_MIN_CLI entry either - which is not luck,
    # `test_every_pinned_alias_is_a_real_portal_model` forbids a gate without a
    # pin. It changes no answer, so no test can catch it. Do not chase it.
    (
        "the unpinned-alias passthrough returns the alias table's default",
        "    pinned = CLI_MODEL_IDS.get(alias)\n    if pinned is None:\n        return alias\n",
        "    pinned = CLI_MODEL_IDS.get(alias, alias)\n",
    ),
    (
        "fable is pinned to the OLD Fable id",
        '"fable": "claude-fable-5-1",',
        '"fable": "claude-fable-5",',
    ),
    (
        "the fable pin is removed altogether",
        '    "fable": "claude-fable-5-1",\n',
        "",
    ),
    (
        "the fable gate is removed, so an old CLI 400s",
        '    "fable": "2.1.251",\n',
        "",
    ),
    (
        "the requirement is lowered below the version that actually gates it",
        '"fable": "2.1.251",',
        '"fable": "2.1.200",',
    ),
    (
        "the requirement is raised above the installed CLI",
        '"fable": "2.1.251",',
        '"fable": "9.9.9",',
    ),
    (
        "opus gains a gate it should not have",
        'MODEL_MIN_CLI: dict[str, str] = {\n    "fable": "2.1.251",',
        'MODEL_MIN_CLI: dict[str, str] = {\n    "opus": "2.1.251",\n    "fable": "2.1.251",',
    ),
    (
        "_version_tuple stops truncating and raises on a build suffix",
        "        if not part.isdigit():\n            break\n",
        "",
    ),
    (
        "_version_tuple skips a bad part instead of stopping there",
        "        if not part.isdigit():\n            break\n",
        "        if not part.isdigit():\n            continue\n",
    ),
    (
        "the dropdown still says Fable 5",
        '("fable", "Fable 5.1"),',
        '("fable", "Fable 5"),',
    ),
    (
        "DEFAULT_CLI_VERSION rises above the gate (an unreadable CLI 400s)",
        'DEFAULT_CLI_VERSION = "2.1.215"',
        'DEFAULT_CLI_VERSION = "2.1.999"',
    ),
]


def run_tests() -> bool:
    """True when the suite is GREEN."""
    proc = subprocess.run(
        [str(ROOT / "venv" / "bin" / "python"), "-m", "pytest", *TESTS, "-q", "-x"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return proc.returncode == 0


def main() -> int:
    original = TARGET.read_text()
    caught, escaped, broken = 0, [], []
    try:
        # Prove the suite can be green before trusting any red.
        if not run_tests():
            print("BASELINE IS RED - fix that before sweeping")
            return 2
        print(f"baseline green; {len(MUTATIONS)} mutations\n")
        for name, old, new in MUTATIONS:
            count = original.count(old)
            if count != 1:
                broken.append((name, count))
                print(f"  SKIP  ({count} matches) {name}")
                continue
            TARGET.write_text(original.replace(old, new, 1))
            if run_tests():
                escaped.append(name)
                print(f"  ESCAPED  {name}")
            else:
                caught += 1
                print(f"  caught   {name}")
            TARGET.write_text(original)
    finally:
        TARGET.write_text(original)

    total = len(MUTATIONS) - len(broken)
    print(f"\n{caught}/{total} caught")
    for name in escaped:
        print(f"  ESCAPED: {name}")
    for name, count in broken:
        print(f"  UNANCHORED ({count} matches): {name}")
    return 1 if (escaped or broken) else 0


if __name__ == "__main__":
    sys.exit(main())

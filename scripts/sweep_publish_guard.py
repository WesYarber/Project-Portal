#!/usr/bin/env python
"""Delete-the-fix sweep over the decision points added by the publish-guard work.

Run detached, per docs/verifying-with-mutations.md §1:

    cd <your checkout>
    setsid nohup venv/bin/python -u scripts/sweep_publish_guard.py \
        > /tmp/sweep-publish-guard.log 2>&1 < /dev/null &

then wait for the marker `SWEEP COMPLETE` in that log rather than on the pid.

Against the OWNING TEST FILES rather than the whole suite (§7): the question a
delete-the-fix pass asks is "does the test written to own this line fail without
it", and two files answer that in seconds instead of three minutes - which also
keeps ~3 GB of pytest tmpfs out of this run's own memory cgroup.
"""

from __future__ import annotations

import atexit
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEAK = ROOT / "app" / "leakscan.py"
SETUP = ROOT / "deploy" / "setup.py"
TEST_FILES = ["tests/test_leakscan.py", "tests/test_setup.py"]

ORIGINAL = {p: p.read_text(encoding="utf-8") for p in (LEAK, SETUP)}


def restore_all() -> None:
    for path, text in ORIGINAL.items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore_all)
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_: sys.exit(1))


# (label, file, find, replace-with, the test that must fail)
MUTATIONS: list[tuple[str, Path, str, str, str]] = [
    (
        "a file with no extension is scanned",
        LEAK,
        "if path.suffix not in SCANNED_SUFFIXES and path.name not in SCANNED_NAMES:",
        "if path.suffix not in SCANNED_SUFFIXES:",
        "test_a_file_with_no_extension_is_still_read",
    ),
    (
        "the example config is read at all",
        LEAK,
        "        if path.name in SKIP_NAMES:",
        "        if path.name in EXEMPT_NAMES:",
        "test_the_example_config_is_read_but_exempt_from_the_identity_half",
    ),
    (
        "the example config is exempt from the identity half",
        LEAK,
        "        identity = path.name not in IDENTITY_EXEMPT_NAMES",
        "        identity = True",
        "test_the_example_config_is_still_checked_for_keys",
    ),
    (
        "the machine's addresses become needles",
        LEAK,
        "    found |= set(local_addresses() if addresses is None else addresses)",
        "    pass",
        "test_the_machines_addresses_are_needles",
    ),
    (
        "loopback and link-local are dropped",
        LEAK,
        "        if addr.is_loopback or addr.is_link_local:\n            continue",
        "        pass",
        "test_loopback_and_link_local_are_not_needles",
    ),
    (
        "a machine with no `ip` binary is survivable",
        LEAK,
        '    exe = shutil.which("ip")\n    if not exe:',
        '    exe = shutil.which("ip") or "ip"\n    if False:',
        "test_reading_the_addresses_never_raises_on_a_machine_without_ip",
    ),
    (
        "a credential line is not also reported as an identity leak",
        LEAK,
        "                leaks.append(Leak(rel, lineno, _redact(line), shape))\n                continue",
        "                leaks.append(Leak(rel, lineno, _redact(line), shape))",
        "test_a_credential_is_reported_redacted_and_the_hostname_beside_it_too",
    ),
    (
        "a credential is reported redacted",
        LEAK,
        "leaks.append(Leak(rel, lineno, _redact(line), shape))",
        "leaks.append(Leak(rel, lineno, line, shape))",
        "test_a_credential_is_reported_redacted_and_the_hostname_beside_it_too",
    ),
    (
        "redaction covers every shape on the line, not just the one that matched",
        LEAK,
        "    for _, pattern in _SHAPES:\n        line = pattern.sub(REDACTED, line)\n    return line",
        "    name = credentials(line)\n    for shape, pattern in _SHAPES:\n"
        "        if shape == name:\n            return pattern.sub(REDACTED, line)\n    return line",
        "test_two_keys_on_one_line_are_both_redacted",
    ),
    (
        "the AWS shape has no closing word boundary",
        LEAK,
        r'("aws-access-key", r"\bAKIA[0-9A-Z]{16}"),',
        r'("aws-access-key", r"\bAKIA[0-9A-Z]{16}\b"),',
        "test_a_fixed_length_key_is_caught_inside_a_longer_run",
    ),
    (
        "the Google shape has no closing word boundary",
        LEAK,
        r'("google-api-key", r"\bAIza[A-Za-z0-9_-]{35}"),',
        r'("google-api-key", r"\bAIza[A-Za-z0-9_-]{35}\b"),',
        "test_a_fixed_length_key_is_caught_inside_a_longer_run",
    ),
    (
        "an assigned secret must contain a digit",
        LEAK,
        r"[\"'](?=[^\"'\s]*\d)(?=[^\"'\s]*[A-Za-z])[^\"'\s]{20,}[\"']",
        r"[\"'](?=[^\"'\s]*[A-Za-z])[^\"'\s]{20,}[\"']",
        "test_an_obviously_fake_fixture_is_not_a_credential",
    ),
    (
        "an assigned secret must be long",
        LEAK,
        r"[^\"'\s]{20,}[\"']",
        r"[^\"'\s]{6,}[\"']",
        "test_an_obviously_fake_fixture_is_not_a_credential",
    ),
    (
        "the Anthropic threshold clears the fixtures",
        LEAK,
        r'("anthropic-key", r"sk-ant-[A-Za-z0-9_-]{32,}"),',
        r'("anthropic-key", r"sk-ant-[A-Za-z0-9_-]{8,}"),',
        "test_an_obviously_fake_fixture_is_not_a_credential",
    ),
    (
        "the OpenAI threshold clears the fixtures",
        LEAK,
        r'("openai-key", r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}"),',
        r'("openai-key", r"sk-(?:proj-)?[A-Za-z0-9_-]{8,}"),',
        "test_an_obviously_fake_fixture_is_not_a_credential",
    ),
    (
        "an empty needle set still matches nothing",
        LEAK,
        '        return re.compile(r"(?!x)x")',
        '        return re.compile("")',
        "test_no_identity_matches_nothing_rather_than_everything",
    ),
    (
        "--check does not boot the portal",
        SETUP,
        "if not args.no_smoke_test and not args.check and not report.failed",
        "if not args.no_smoke_test and not report.failed",
        "test_check_mode_never_boots_the_portal",
    ),
    (
        "the smoke test checks the answer, not just the connection",
        SETUP,
        "                if body == PING_EXPECTED:",
        "                if True:",
        "test_the_smoke_test_rejects_the_wrong_answer",
    ),
    (
        "the smoke test notices the process dying",
        SETUP,
        "            if proc.poll() is not None:",
        "            if False:",
        "test_the_smoke_test_fails_when_the_portal_will_not_start",
    ),
    (
        "a free port is asked of the kernel",
        SETUP,
        '        probe.bind(("127.0.0.1", 0))\n        return probe.getsockname()[1]',
        '        probe.bind(("127.0.0.1", 0))\n        return 8599',
        "test_two_calls_do_not_hand_back_the_same_port",
    ),
    (
        "a missing Claude CLI is a job for a person, not a failure",
        SETUP,
        "        report.needs_a_person(\n            \"install the Claude Code CLI",
        "        report.bad(\n            \"install the Claude Code CLI",
        "test_a_missing_claude_cli_is_a_job_for_a_person_not_a_failure",
    ),
]


def run_tests() -> tuple[bool, list[str], str]:
    """(pytest produced a summary, the names it reported as failing, raw tail).

    §3: "no FAILED lines" is only evidence if pytest actually finished. A run
    that crashed emits no summary, and reading that as "uncaught" is the exact
    opposite of the truth. §6: ERROR lines count too - a mutation that breaks
    an import reports every test wanting that module as an ERROR with no FAILED
    line anywhere.
    """
    done = subprocess.run(
        [sys.executable, "-m", "pytest", *TEST_FILES, "-q", "--no-header", "-p", "no:randomly"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    out = done.stdout + done.stderr
    summarized = any(
        marker in out for marker in (" passed", " failed", " error", "no tests ran")
    )
    names: list[str] = []
    for line in out.splitlines():
        stripped = line.strip()
        for marker in ("FAILED ", "ERROR "):
            if stripped.startswith(marker):
                # §6: the first space-separated token of a FAILED line is the
                # word FAILED itself. The name is what follows it, up to the
                # first space after the nodeid.
                names.append(stripped[len(marker):].split(" ")[0])
    return summarized, names, out[-700:]


def main() -> int:
    print(f"Baseline over {' '.join(TEST_FILES)}")
    summarized, names, tail = run_tests()
    if not summarized or names:
        # §2: a sweep means nothing unless the unmutated tree is green.
        print(f"BASELINE IS NOT GREEN - refusing to sweep.\n{tail}")
        return 2
    print("Baseline green.\n")

    caught, missed, skipped = 0, [], []
    for index, (label, path, find, replace, owner) in enumerate(MUTATIONS, 1):
        text = ORIGINAL[path]
        if find not in text:
            print(f"{index:2}. SKIP    {label} (pattern missing)")
            skipped.append(label)
            continue
        if text.count(find) != 1:
            print(f"{index:2}. SKIP    {label} (pattern appears {text.count(find)}x)")
            skipped.append(label)
            continue
        path.write_text(text.replace(find, replace), encoding="utf-8")
        try:
            summarized, names, tail = run_tests()
        finally:
            path.write_text(text, encoding="utf-8")
        if not summarized:
            print(f"{index:2}. CRASH   {label} - pytest emitted no summary, no data point\n{tail}")
            skipped.append(label)
        elif any(owner in name for name in names):
            print(f"{index:2}. CAUGHT  {label}  <- {owner}")
            caught += 1
        else:
            print(f"{index:2}. MISSED  {label}")
            print(f"      expected {owner}, got {names or 'no failures'}")
            missed.append(label)

    restore_all()
    print(f"\n{caught} caught, {len(missed)} missed, {len(skipped)} skipped, of {len(MUTATIONS)}")
    for label in missed:
        print(f"  missed: {label}")
    for label in skipped:
        print(f"  skipped: {label}")
    print("SWEEP COMPLETE")
    return 0 if not missed and not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())

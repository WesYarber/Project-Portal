"""Delete-the-fix sweep over the decision points added by the module-state work.

Follows docs/verifying-with-mutations.md: restore however we die (atexit +
signal handlers that exit), print unbuffered, mark completion with a marker
line, treat a crashed pytest run as SKIP rather than as a pass, and parse the
FAILED lines properly rather than by splitting on the first space.

The owning file is tests/test_module_state.py plus tests/test_find_module_state_leaks.py;
the mutations live in test infrastructure, so a first pass runs the whole suite
(unexpected breakage elsewhere is exactly the signal) at -n0, because the
harness parses the output it reads.
"""
from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MS = ROOT / "tests" / "module_state.py"
CF = ROOT / "tests" / "conftest.py"

ORIGINAL = {p: p.read_text() for p in (MS, CF)}


def restore_all() -> None:
    for path, text in ORIGINAL.items():
        path.write_text(text)


atexit.register(restore_all)
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_: sys.exit(1))

# (label, file, find, replace, the test that should fail)
MUTATIONS = [
    ("restore hands back the dirtied object, not a fresh one", MS,
     "setattr(module, name, type(value)() if isinstance(\n"
     "            value, (dict, list, set)) else value)",
     "setattr(module, name, value)",
     "test_a_filled_container_comes_back_empty_not_merely_reattached"),

    ("restore does not skip an unimported module", MS,
     "        module = sys.modules.get(module_name)\n"
     "        if module is None:\n            continue\n",
     "        module = sys.modules[module_name]\n",
     "test_restore_survives_a_module_that_is_not_imported"),

    ("restore never calls the module's own resetter", MS,
     "    for module_name, func_name in MODULE_RESETTERS.items():",
     "    for module_name, func_name in {}.items():",
     "(unknown)"),

    ("the probe keeps the memo it created", MS,
     "    try:\n        return call()\n    finally:\n"
     "        setattr(module, name, before)",
     "    return call()",
     "test_probe_without_memoizing_returns_the_real_answer_and_leaves_no_memo"),

    ("discover ignores `global` rebinds", MS,
     "            if isinstance(node, ast.Global):\n"
     "                found.update((module, name) for name in node.names)",
     "            pass",
     "test_the_registry_names_nothing_that_no_longer_exists"),

    ("discover ignores empty containers", MS,
     "            if _is_empty_container(value):\n"
     "                found.update((module, t.id) for t in targets)",
     "            pass",
     "test_the_registry_names_nothing_that_no_longer_exists"),

    ("discover ignores annotated assignments", MS,
     "            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):\n"
     "                targets, value = [node.target], node.value",
     "            elif False:\n                targets, value = [], None",
     "test_the_registry_names_nothing_that_no_longer_exists"),

    ("a non-empty dict counts as state too", MS,
     "    if isinstance(value, ast.Dict):\n        return not value.keys",
     "    if isinstance(value, ast.Dict):\n        return True",
     "test_every_mutable_global_in_app_is_accounted_for"),

    ("the fixture never clears BEFORE the test", CF,
     "    clear()\n    yield\n    clear()",
     "    yield\n    clear()",
     "(unknown)"),

    ("the fixture never clears AFTER the test", CF,
     "    clear()\n    yield\n    clear()",
     "    clear()\n    yield",
     "test_the_next_test_does_not_inherit_that_registry"),

    ("the manual-run queue is never drained", CF,
     "        while not worker.manual_queue.empty():\n"
     "            worker.manual_queue.get_nowait()",
     "        pass",
     "(unknown)"),

    ("collection is never sampled", CF,
     "    _STATE_AFTER_COLLECTION.clear()\n"
     "    _STATE_AFTER_COLLECTION.update(module_state.capture())",
     "    pass",
     "test_collection_leaves_every_global_at_its_import_value"),
]

# ERROR as well as FAILED, per docs/verifying-with-mutations.md section 6. An
# assertion raised from a teardown hook - which is exactly how the module-state
# invariant is enforced - is reported by pytest as `ERROR at teardown of ...`,
# never as FAILED. A parser looking only for FAILED reads that as MISSED while
# printing "2 errors" two lines further down, and the mutation that IS caught
# gets a second sweep spent on it.
FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)


def run_suite(args: list[str]) -> tuple[int, list[str], bool]:
    proc = subprocess.run(
        [str(ROOT / "venv/bin/python"), "-m", "pytest", *args, "-n0", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=1800)
    out = proc.stdout + proc.stderr
    failed = [m.group(1) for m in FAILED_RE.finditer(out)]
    # A run that emitted no summary line at all crashed; that is a SKIP, not a
    # pass (docs/verifying-with-mutations.md section 3).
    summarized = bool(re.search(r"\d+ (passed|failed|error)", out))
    return proc.returncode, failed, summarized


def main() -> None:
    targets = sys.argv[1:] or ["tests/"]
    print(f"baseline: {targets}", flush=True)
    rc, failed, ok = run_suite(targets)
    if failed or not ok:
        print(f"BASELINE NOT GREEN rc={rc} failed={failed[:5]} summarized={ok}")
        print("SWEEP COMPLETE", flush=True)
        return

    caught = missed = skipped = 0
    for label, path, find, replace, owner in MUTATIONS:
        text = ORIGINAL[path]
        if find not in text:
            print(f"SKIP (pattern missing): {label}", flush=True)
            skipped += 1
            continue
        path.write_text(text.replace(find, replace, 1))
        try:
            rc, failed, ok = run_suite(targets)
        finally:
            path.write_text(text)

        if not ok:
            print(f"SKIP (pytest crashed rc={rc}): {label}", flush=True)
            skipped += 1
        elif failed:
            hit = any(owner in f for f in failed)
            mark = "CAUGHT" if hit else "CAUGHT-BY-OTHER"
            print(f"{mark} ({len(failed)}): {label}", flush=True)
            if not hit:
                print(f"    expected {owner}, got {failed[:4]}", flush=True)
            caught += 1
        else:
            print(f"MISSED: {label}", flush=True)
            missed += 1

    print(f"\ncaught={caught} missed={missed} skipped={skipped}", flush=True)
    print("SWEEP COMPLETE", flush=True)


if __name__ == "__main__":
    main()

"""Every `app.*` module global a test can change, and the walk that proves the
list is complete.

A module global is process-wide, so what one test leaves behind the next test in
that process inherits. Serial running hides this almost perfectly - the files
execute in the same order every time, so a leak lands in the same place and
usually falls where nothing reads it. Distribute the suite across xdist workers
and the set of tests sharing a process changes run to run, so the same leak
starts landing somewhere that cares. That is how the 2026-08-29 "flakiness that
was not flakiness" was found: `app.worker.manual_queue` held an id a *previous
file* had put there, and `_start_one` was called twice.

Two hazards make this worse here than in most suites:

- **Ids restart at 1 in every test.** Each test gets a fresh database, so run
  ids and project ids begin again from 1. A dict or set keyed on one of them -
  `runlimit._scopes`, `agent_runner._CANCEL_REQUESTED`, `ask._PENDING`,
  `hookguard._SCOPES` - does not merely grow: a leaked entry for id 1 is read by
  the *next* test as being about its own id 1. A leftover in
  `_CANCEL_REQUESTED` makes the next test's run look canceled.
- **Capability caches memoize a subprocess.** `worklock.available()` and
  `runlimit.pool_available()` shell out once and remember. A test that stubs the
  subprocess to simulate a machine without `flock` poisons every later test in
  that process into believing the same, silently disabling the locking whose
  behavior they were written to check.

So the rule this module enforces is a flat one: **after any test, every one of
these globals equals the value it had at import.** `tests/conftest.py` restores
them around every test, and `tests/test_module_state.py` fails if `app/` grows a
new one that nobody has decided about.

The complementary tool is `deploy/find_module_state_leaks.py`, which watches the
real values drift as the suite runs. This module is the static half (a new
global cannot be added silently); that one is the dynamic half (a global that
drifts despite all this is visible).
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import sys

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"

# Globals restored to their import-time value before and after every test.
RESET: tuple[tuple[str, str], ...] = (
    ("app.agent_runner", "_ACTIVE_PROCS"),
    ("app.agent_runner", "_CANCEL_REQUESTED"),
    ("app.ask", "_PENDING"),
    ("app.ask", "_TASKS"),
    ("app.claudelogin", "_last_result"),
    ("app.claudelogin", "_pending"),
    ("app.hookguard", "_SCOPES"),
    ("app.midrun", "_HOLDS"),
    ("app.inquiry", "_ASKED"),
    ("app.inquiry", "_TASKS"),
    ("app.launch", "_last_start"),
    ("app.main", "_BACKGROUND_TASKS"),
    ("app.people", "_WHOIS_CACHE"),
    ("app.mirror", "_backoff"),
    ("app.mirror", "_next_attempt"),
    ("app.mirror", "_reported"),
    ("app.nodes", "_UPDATES"),
    ("app.nodes", "_identity"),
    ("app.portalmcp", "_SCOPES"),
    ("app.portalmcp", "_WAITING"),
    ("app.preview", "_MOUNTS"),
    ("app.runlimit", "_available"),
    ("app.runlimit", "_pool_applied"),
    ("app.runlimit", "_scopes"),
    ("app.runlimit", "_slice_ok"),
    ("app.transcribe", "_TASKS"),
    ("app.worker", "_PARALLEL_SAID"),
    ("app.worker", "_audit_pruned_day"),
    ("app.worker", "_inflight"),
    ("app.worker", "_last_stray_sweep"),
    ("app.worker", "_lease_free_since"),
    ("app.worker", "_model_checked_day"),
    ("app.worker", "_pending_restart"),
    ("app.worker", "_restarting"),
    ("app.worklock", "_available"),
)

# Globals deliberately NOT restored here, each with the reason. An unexplained
# exclusion is how a real leak gets to look normal, so the fence test requires
# every name to be in one list or the other - the decision has to be made, it
# just does not have to come out the same way twice.
EXEMPT: dict[tuple[str, str], str] = {
    ("app.db", "_CONN"):
        "The `temp_data_dir` fixture owns this: it closes the cached connection "
        "and reopens a new database for every test. A second resetter would "
        "close a connection that fixture is still using.",
    ("app.config", "_cli_version_cache"):
        "Pinned for the length of each test by `temp_data_dir`, so that no test "
        "shells out to the real `claude --version`. Restoring it around the "
        "test would fight that monkeypatch rather than help it.",
    ("app.transcribe", "_available"):
        "Pinned by `temp_data_dir` for the same reason: an unpinned value lets "
        "one test reach the real Docker daemon and start portal-whisper.",
    ("app.live", "_OBSERVER"):
        "A live sqlite3 connection, not a value. Dropping the reference leaks "
        "the file descriptor, so it is reset by calling `live.reset()`, which "
        "closes it first - see MODULE_RESETTERS.",
    ("app.live", "_OBSERVER_PATH"):
        "Half of the observer cache key; `live.reset()` clears it with the "
        "connection it belongs to.",
}

# Modules that own their own teardown. Called instead of assigning, because the
# value is a live resource that has to be closed rather than dropped.
MODULE_RESETTERS: dict[str, str] = {"app.live": "reset"}


def discover() -> set[tuple[str, str]]:
    """Every module-level name in `app/` that code can change at runtime.

    Two signals, because neither alone is enough:

    - a name in a `global` statement is rebound at runtime by definition, which
      catches the scalars (`_available`, `_pending_restart`, `_last_start`);
    - a module-level name bound to an EMPTY dict, list or set is a registry or a
      cache - something whose whole purpose is to be filled in later. A
      non-empty literal is a constant table, and a constant is not state.

    Deliberately syntactic rather than run-time: it sees a global in a module no
    test has imported yet, which is exactly the one nobody has thought about.
    """
    found: set[tuple[str, str]] = set()
    for path in sorted(APP_DIR.glob("*.py")):
        module = "app." + path.stem
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                found.update((module, name) for name in node.names)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = [t for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets, value = [node.target], node.value
            else:
                continue
            if value is None or not targets:
                continue
            if _is_empty_container(value):
                found.update((module, t.id) for t in targets)
    return found


def _is_empty_container(value: ast.expr) -> bool:
    if isinstance(value, ast.Dict):
        return not value.keys
    if isinstance(value, (ast.List, ast.Set)):
        return not value.elts
    return (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id in ("dict", "list", "set")
            and not value.args and not value.keywords)


def probe_without_memoizing(module_name: str, name: str, call):
    """Ask a memoizing capability check at import time, leaving no memo behind.

    A `@pytest.mark.skipif` condition has to be a value at decoration time, so a
    file that skips itself on a machine without `flock` genuinely must call
    `worklock.available()` while the module is being imported. The trouble is
    that the call is what fills the cache: collection - before any test runs,
    and so before any fixture can clear it - leaves the real machine's answer in
    a process-wide global. Two files did exactly that, and the first test of
    every serial run inherited a memo nobody in that test had asked for.

    The answer is not to stop asking, it is to put the global back afterwards.
    The probe is still a real one; only the side effect is undone.
    """
    module = importlib.import_module(module_name)
    before = getattr(module, name)
    try:
        return call()
    finally:
        setattr(module, name, before)


def capture() -> dict[tuple[str, str], object]:
    """The import-time value of every global in `RESET`.

    Containers are COPIED, and that is the whole subtlety. Storing the object
    itself makes a snapshot useless for detecting a mutation: the dict a test
    fills is the very object the snapshot holds, so comparing them finds them
    equal no matter what happened to it. That is not hypothetical - the teardown
    assertion in `conftest` was written against a reference-holding snapshot and
    a sweep deleting the fixture's teardown reset walked straight through it.

    A shallow copy is enough. Every value in `RESET` is a scalar or an EMPTY
    container at import - that is what `discover` selects for - so there is
    never any nesting to lose.
    """
    snapshot: dict[tuple[str, str], object] = {}
    for module_name, name in RESET:
        module = importlib.import_module(module_name)
        value = getattr(module, name)
        snapshot[(module_name, name)] = (
            type(value)(value) if isinstance(value, (dict, list, set)) else value)
    return snapshot


def restore(snapshot: dict[tuple[str, str], object]) -> None:
    """Put every global back to its import-time value.

    Containers are replaced with a fresh empty one of the same type rather than
    with the remembered object itself: the remembered object is the very one a
    test may have filled, and handing it back would restore the leak instead of
    clearing it.

    Assigns rather than monkeypatching, on purpose. A `monkeypatch.setattr` here
    would give the test a clean value and then put the dirty one back at
    teardown, so the leak would survive with a clean window cut into it. This
    has to be the durable kind of reset for the invariant to hold between tests.
    """
    for (module_name, name), value in snapshot.items():
        module = sys.modules.get(module_name)
        if module is None:
            continue
        setattr(module, name, type(value)() if isinstance(
            value, (dict, list, set)) else value)
    for module_name, func_name in MODULE_RESETTERS.items():
        module = sys.modules.get(module_name)
        if module is not None:
            getattr(module, func_name)()

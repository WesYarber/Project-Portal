"""Find tests that leave `app.*` module globals changed behind them.

A module global is process-wide. Under a serial run the files always execute in
the same order, so a leak lands in the same place every time and usually falls
where nothing reads it. Distribute the suite across xdist workers and the set of
tests sharing a process changes run to run, so the same leak starts landing
somewhere that cares - which is how the 2026-08-29 "flakiness that was not
flakiness" was found (`app.worker`'s `manual_queue`).

This is the general form of that hunt. It is a pytest plugin: snapshot every
module-level name in every imported `app.*` module after setup, compare after
teardown, print what drifted and which test did it.

    venv/bin/python -m pytest tests/ -n0 -q -p deploy.find_module_state_leaks

Run it SERIALLY (`-n0`). Under xdist each worker is its own process with its own
report, and the plugin's summary would be split across them.

It is a diagnostic, not a fence: it only reports. The fence that keeps the
findings fixed is `tests/test_no_module_state_leaks.py`, which pins the same
check against an allowlist so a NEW leak fails the suite.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tests"))
import module_state  # noqa: E402


def _ignored() -> set:
    """Names whose drift is not a leak.

    Read from `tests/module_state.py` rather than kept as a second list here.
    An exemption is a decision with a reason attached, and there should be
    exactly one place that decision lives - a private copy in this file would go
    stale the first time somebody changed the real one, and would go stale
    silently, in the direction of hiding a leak.
    """
    return set(module_state.EXEMPT)


def _interesting(module_name: str, name: str, value: object) -> bool:
    """Is this a module-level value worth watching for drift?

    Functions, classes, modules and imported types are the module's structure
    rather than its state. What is left - dicts, lists, sets, and rebound
    scalars - is where a cache, a queue or a latch lives.
    """
    if (module_name, name) in _ignored():
        return False
    if callable(value):
        return False
    return isinstance(
        value, (dict, list, set, frozenset, tuple, bool, int, float, str,
                bytes, type(None)),
    )


def _snapshot() -> dict:
    """Every watchable global in every imported `app.*` module, as a repr.

    A repr rather than a copy: it captures a rebind and a mutation alike, it
    survives values that cannot be deep-copied, and it is what gets printed.
    """
    out = {}
    for module_name, module in list(sys.modules.items()):
        if not (module_name == "app" or module_name.startswith("app.")):
            continue
        for name, value in list(vars(module).items()):
            if name.startswith("__") or not _interesting(module_name, name, value):
                continue
            try:
                out[(module_name, name)] = repr(value)
            except Exception:  # noqa: BLE001 - a broken repr is not our problem
                pass
    return out


class _LeakFinder:
    def __init__(self) -> None:
        self.before: dict = {}
        # (module, name) -> list of test ids that changed it
        self.drift: dict[tuple[str, str], list[str]] = {}

    def record(self, item) -> None:
        after = _snapshot()
        for key, value in after.items():
            was = self.before.get(key)
            if was is not None and was != value:
                self.drift.setdefault(key, []).append(item.nodeid)

    def pytest_terminal_summary(self, terminalreporter) -> None:  # noqa: D102
        write = terminalreporter.write_line
        write("")
        if not self.drift:
            write("module-state: no app.* global drifted across any test.")
            return
        write(f"module-state: {len(self.drift)} global(s) drifted:")
        for (module_name, name), tests in sorted(self.drift.items()):
            write(f"  {module_name}.{name}  ({len(tests)} test(s))")
            for test in tests[:3]:
                write(f"      {test}")
            if len(tests) > 3:
                write(f"      ... and {len(tests) - 3} more")


_finder = _LeakFinder()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    """Snapshot BEFORE the item's fixtures run.

    The snapshot point is the whole subtlety. Taking it after setup and
    comparing after teardown reports every `monkeypatch.setattr` in a fixture as
    drift, because the value in between is the patched one and the value
    afterwards is the restored original - which is the fixture working, not a
    leak. Taken before setup, the comparison asks the question that matters:
    did this test hand the next one a module in a different state than the one
    it inherited?
    """
    _finder.before = _snapshot()
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Compare after every fixture - including monkeypatch - has finalized."""
    yield
    _finder.record(item)


def pytest_terminal_summary(terminalreporter):  # noqa: D103
    _finder.pytest_terminal_summary(terminalreporter)

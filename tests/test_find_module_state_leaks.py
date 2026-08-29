"""The leak detector can actually see a leak.

`deploy/find_module_state_leaks.py` is the dynamic half of the module-state
check: it watches `app.*` globals as the suite runs and prints what drifted. Its
whole output today is "no app.* global drifted across any test", which is only
worth believing if the thing can go the other way. A detector that reports clean
because it is broken is indistinguishable from a clean tree, and it is the more
likely of the two once nobody is looking.

So this pins the two halves that could rot silently - what it watches, and what
it ignores - rather than the plugin hooks, which pytest calls or does not.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))

import find_module_state_leaks as detector  # noqa: E402

import module_state  # noqa: E402
from app import runlimit  # noqa: E402


def test_it_sees_a_container_that_was_filled():
    before = detector._snapshot()
    runlimit._scopes[7] = "portal-run-7-1-1.scope"
    try:
        after = detector._snapshot()
    finally:
        runlimit._scopes.pop(7, None)

    key = ("app.runlimit", "_scopes")
    assert before[key] != after[key], "a filled dict must read as changed"


def test_it_sees_a_rebound_scalar():
    before = detector._snapshot()
    runlimit._slice_ok = False
    try:
        after = detector._snapshot()
    finally:
        runlimit._slice_ok = None

    assert before[("app.runlimit", "_slice_ok")] != after[("app.runlimit", "_slice_ok")]


def test_it_ignores_functions_and_classes():
    """Structure is not state. Watching every callable would bury the handful of
    values that matter under a module's whole namespace."""
    assert not detector._interesting("app.runlimit", "scope_name", runlimit.scope_name)
    assert not detector._interesting("app.runlimit", "Path", Path)


def test_it_watches_the_shapes_that_leak():
    assert detector._interesting("app.x", "_cache", {})
    assert detector._interesting("app.x", "_seen", set())
    assert detector._interesting("app.x", "_flag", None)
    assert detector._interesting("app.x", "_day", "2026-08-29")


def test_its_exemptions_come_from_the_one_registry():
    """Not a private copy. A second list of exemptions goes stale silently, and
    in the direction of hiding a leak."""
    assert detector._ignored() == set(module_state.EXEMPT)
    for key in module_state.EXEMPT:
        assert not detector._interesting(key[0], key[1], None)


def test_the_snapshot_only_covers_the_app_package():
    """A snapshot of every module in the process would be enormous and would
    report drift in pytest's own internals on every test."""
    snapshot = detector._snapshot()
    assert snapshot, "it must find something to watch"
    assert all(m == "app" or m.startswith("app.") for m, _ in snapshot)

"""The registry of `app.*` module globals stays complete and actually resets.

`tests/module_state.py` lists every module global a test can change; the autouse
fixture in `conftest.py` puts them all back to their import value around every
test. Both halves are only worth anything if the list cannot go stale, so this
file pins three claims:

1. the list covers everything `app/` actually has (the AST walk agrees with it);
2. the reset really clears a filled container, rather than handing back the very
   object a test dirtied;
3. the two most dangerous shapes - an id-keyed registry, and a memoized
   capability check - do not survive from one test into the next.

Claim 3 is checked by a pair of tests that would pass individually even with the
fixture removed. They are written as "the previous test filled it, this one must
not see it", which is the only shape that catches the real defect.
"""
from __future__ import annotations

import ast
import sqlite3
import sys

import pytest

import module_state
from app import (agent_runner, ask, hookguard, live, people, preview, runlimit,
                 worker, worklock)


def test_every_mutable_global_in_app_is_accounted_for():
    """A new module global must be classified, not merely added.

    This is the fence. If it fails, `app/` grew a global that nobody has decided
    about: either it needs resetting between tests (add it to `RESET`) or it
    genuinely does not (add it to `EXEMPT` with the reason). The failure names
    the module and the attribute, so the decision is a one-liner either way.
    """
    declared = set(module_state.RESET) | set(module_state.EXEMPT)
    actual = module_state.discover()

    unclassified = sorted(actual - declared)
    assert not unclassified, (
        "app/ has module globals nothing has decided about:\n  "
        + "\n  ".join(f"{m}.{n}" for m, n in unclassified)
        + "\n\nA module global is process-wide, so whatever a test leaves in one "
        "the next test in that process inherits. Add each to RESET in "
        "tests/module_state.py if a test must not inherit it, or to EXEMPT with "
        "the reason it is safe (or owned by another fixture)."
    )


def test_the_registry_names_nothing_that_no_longer_exists():
    """A renamed or deleted global leaves a dead entry that quietly protects
    nothing - and reads, to the next person, as coverage that is there."""
    stale = sorted(
        (set(module_state.RESET) | set(module_state.EXEMPT))
        - module_state.discover()
    )
    assert not stale, (
        "tests/module_state.py names globals app/ no longer has:\n  "
        + "\n  ".join(f"{m}.{n}" for m, n in stale)
    )


def test_every_exemption_carries_a_reason():
    for key, reason in module_state.EXEMPT.items():
        assert reason and len(reason) > 40, (
            f"{key} is exempt with no real explanation. An unexplained "
            "exclusion is how a real leak gets to look normal."
        )


def test_the_discovery_walk_reads_syntax_not_a_live_import():
    """It must see a global in a module no test has imported.

    Asking the running process for its module attributes would miss exactly the
    modules nobody has touched - which are the ones nobody has thought about
    either. Checked by walking a snippet directly rather than by trusting the
    docstring.
    """
    tree = ast.parse("CONST = {'a': 1}\n_cache = {}\n_seen = set()\n"
                     "_flag = None\ndef f():\n    global _flag\n    _flag = 1\n")
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names.update(node.names)
    for node in tree.body:
        if isinstance(node, ast.Assign) and module_state._is_empty_container(
                node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))

    # A constant table is not state; an empty container and a `global` rebind are.
    assert names == {"_cache", "_seen", "_flag"}


def test_a_filled_container_comes_back_empty_not_merely_reattached():
    """The restore hands back a fresh container, not the dirtied one.

    Remembering the import-time object and re-assigning it would look like a
    reset and be a no-op, since the object a test filled IS the import-time
    object. This is the difference between the fixture working and the fixture
    existing.
    """
    snapshot = module_state.capture()
    runlimit._scopes[41] = "portal-run-41-1-1.scope"
    people._WHOIS_CACHE["10.0.0.1"] = ("someone", 0.0)

    module_state.restore(snapshot)

    assert runlimit._scopes == {}
    assert people._WHOIS_CACHE == {}


def test_capture_copies_a_container_rather_than_holding_it():
    """Otherwise every snapshot comparison is between an object and itself.

    This is what makes the teardown assertion in conftest able to see anything
    at all: with a reference, a test that fills `runlimit._scopes` leaves the
    snapshot's own value filled too, and the invariant check reports no drift
    from a genuinely dirty module.
    """
    snapshot = module_state.capture()
    runlimit._scopes[3] = "portal-run-3-1-1.scope"
    try:
        assert snapshot[("app.runlimit", "_scopes")] == {}, (
            "the snapshot must not have changed with the module"
        )
    finally:
        runlimit._scopes.pop(3, None)


def test_restore_puts_a_memoized_capability_back_to_unknown():
    snapshot = module_state.capture()
    worklock._available = False
    runlimit._slice_ok = False

    module_state.restore(snapshot)

    assert worklock._available is None
    assert runlimit._slice_ok is None


def test_restore_survives_a_module_that_is_not_imported(monkeypatch):
    """It is called from an autouse fixture, so it must never be the thing that
    breaks a test run - a module absent from `sys.modules` is skipped.

    The capture has to happen BEFORE the module is removed. Written the other
    way round the test is vacuous: `capture()` calls `importlib.import_module`,
    which puts the module straight back into `sys.modules`, so `restore` never
    meets the absence the test is named for. A sweep deleting the `is None`
    guard walked through the first version of this test untouched.
    """
    snapshot = module_state.capture()
    monkeypatch.delitem(sys.modules, "app.preview", raising=False)

    module_state.restore(snapshot)  # must not raise

    assert "app.preview" not in sys.modules, (
        "restore must not import a module back to reset it - that would make "
        "the fixture the reason a module is loaded"
    )


def test_a_live_resource_is_closed_by_its_own_reset_not_dropped():
    """`live._OBSERVER` is a sqlite3 connection, so assigning None over it leaks
    the file descriptor. It is exempt from the value-restore for that reason and
    reset by calling `live.reset()`, which closes it first - a line nothing was
    holding in place until this test.
    """
    live.reset()
    connection = live._observer()
    assert live._OBSERVER is not None

    module_state.restore(module_state.capture())

    assert live._OBSERVER is None, "the observer must be forgotten"
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")  # closed, not merely dereferenced


# --- The pair. Order matters: the first fills, the second must not inherit. ---
#
# pytest runs the tests in a file in definition order, so `_fills_` runs before
# `_inherits_`. Both would pass on their own with the fixture deleted; only the
# pair catches the leak, which is the whole point.

def test_the_manual_run_queue_is_filled_here():
    """The original leak: `worker.manual_queue` is a module-level asyncio.Queue,
    so an id one file put there was read by `_start_one` in another."""
    worker.manual_queue.put_nowait(1)
    assert worker.manual_queue.qsize() == 1


def test_the_next_test_does_not_inherit_the_queue():
    assert worker.manual_queue.empty(), (
        "an id left in the queue makes the next test's worker start a run "
        "nobody in that test asked for"
    )


def test_a_run_id_keyed_registry_is_filled_here():
    runlimit._scopes[1] = "portal-run-1-9999-1.scope"
    agent_runner._CANCEL_REQUESTED.add(1)
    ask._PENDING.add(1)
    hookguard._SCOPES[1] = object()
    preview._MOUNTS["leaky"] = object()
    worklock._available = False

    assert runlimit.known_scopes() == {"portal-run-1-9999-1.scope"}


def test_the_next_test_does_not_inherit_that_registry():
    """Ids restart at 1 in every test, so a leaked entry is not merely stale -
    it is read by the next test as being about its own run 1."""
    assert runlimit._scopes == {}
    assert runlimit.known_scopes() == set()
    assert not agent_runner.cancel_requested(1)
    assert 1 not in ask._PENDING
    assert hookguard._SCOPES == {}
    assert preview._MOUNTS == {}
    assert worklock._available is None


def test_a_fresh_scope_name_is_minted_for_run_1_again():
    """The consequence, stated in the caller's terms rather than the global's.

    Two tests both spawning run 1 must get two different scope names; the
    previous pair proves the map is empty, this proves the map being empty is
    what `scope_name` needs to do its job.
    """
    first = runlimit.scope_name(1)
    module_state.restore(module_state.capture())
    second = runlimit.scope_name(1)

    assert first != second


def test_probe_without_memoizing_returns_the_real_answer_and_leaves_no_memo():
    """Both halves matter: an undo that also threw the answer away would just be
    a broken probe, and a probe that kept the answer is the bug it exists for."""
    calls = []

    def fake_available():
        calls.append(1)
        worklock._available = True  # what a real memoizing check does
        return True

    answer = module_state.probe_without_memoizing(
        "app.worklock", "_available", fake_available)

    assert answer is True and calls == [1], "the probe must really run"
    assert worklock._available is None, "and must leave no memo behind"


def test_probe_restores_the_global_even_when_the_check_raises():
    def explodes():
        worklock._available = False
        raise RuntimeError("no flock here")

    with pytest.raises(RuntimeError):
        module_state.probe_without_memoizing(
            "app.worklock", "_available", explodes)

    assert worklock._available is None


def test_collection_leaves_every_global_at_its_import_value():
    """No test module may fill one of these while it is being imported.

    Collection happens before any fixture exists, so an import-time call to a
    memoizing check - `worklock.available()` for a `skipif` condition - leaves
    the real machine's answer in a process-wide global that nothing can clear in
    time. Two files did exactly that, and the only sign was that the first test
    of a serial run drifted and no other did.

    Measured rather than pattern-matched. Looking for calls that *look*
    memoizing finds the direct ones and misses `test_strays.py`, which reaches
    the same cache through a module-level helper called from a `skipif`
    argument. Comparing the actual values before and after collection cannot be
    fooled by an indirection, and needs no list of which functions memoize.

    Running this file alone imports only this file, so the check is vacuous
    then; it has teeth in the full run, which is where it matters.
    """
    from conftest import _IMPORT_STATE, _STATE_AFTER_COLLECTION

    # Without this the test is vacuous in the worst way: if the hook stops
    # firing, the loop below iterates nothing and the check passes forever while
    # measuring nothing at all.
    assert _STATE_AFTER_COLLECTION, (
        "conftest.pytest_collection_finish did not fire, so there is no reading "
        "to compare - this test would otherwise pass by having no data."
    )

    filled = {
        key: (_IMPORT_STATE[key], value)
        for key, value in _STATE_AFTER_COLLECTION.items()
        if _IMPORT_STATE.get(key) != value
    }

    assert not filled, (
        "importing the test modules changed globals no fixture can clean up "
        "in time:\n  "
        + "\n  ".join(f"{m}.{n}: {was!r} -> {now!r}"
                      for (m, n), (was, now) in sorted(filled.items()))
        + "\n\nSomething runs at module level in a test file. Wrap it in "
        "module_state.probe_without_memoizing(), which still asks the real "
        "question but puts the global back."
    )


@pytest.mark.parametrize("module_name,name", module_state.RESET)
def test_each_listed_global_really_exists(module_name, name):
    """A typo in RESET is silently harmless - `restore` skips what it cannot
    find - so the list is checked against the modules themselves."""
    module = __import__(module_name, fromlist=["_"])
    assert hasattr(module, name), f"{module_name} has no {name}"

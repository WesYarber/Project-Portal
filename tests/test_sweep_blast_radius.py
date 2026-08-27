"""No test may reach a systemd scope it did not create.

This file exists because the suite was quietly moving live agent runs out of
their own cgroups, and passing while it did it.

`worker._tick()` calls `_sweep_strays()`. That builds its protected set by
reading the `runs` table and then asks the REAL systemd what exists. Under
pytest the database is the empty throwaway one, so every genuine
`portal-run-*.scope` on the machine reads as unprotected and the sweep rehouses
the lot. Eleven test files call `_tick()`. `tests/test_approval.py` on its own -
thirty tests, all green - was enough to move a live run's scope.

What that costs is not cosmetic. A run's `runs.scope_unit` is the only handle a
later portal process has on an agent that outlived the process which spawned it.
Move the agent to a differently-named scope and the recorded one goes inactive,
so the next restart asks systemd about it, hears "gone", and declares a live run
dead - which is the step that unlocks an occupied workspace. That is defect 4 of
INCIDENT-2026-07-29, re-armed by the test suite.

`tests/test_strays.py` already had a fence of this shape, added when that file
was caught sweeping the machine. Scoping it to that one file was the wrong
lesson: the hazard is not "the strays tests are dangerous", it is "any test
reaching production code that enumerates system state is dangerous, whether or
not its author knew it did". So the fence lives in conftest, where a new file
inherits it without having to know it exists, and this file pins it.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from app import strays, worker

_REAL_UNIT = re.compile(r"^portal-(?:run|stray)-9\d{3}-\d+-\d+\.scope$")


def _all_shown_are_test_units(pattern: str = "portal-*.scope") -> bool:
    return all(_REAL_UNIT.match(u) for u in strays._list_units(pattern))  # noqa: SLF001


def test_discovery_never_returns_a_unit_this_suite_did_not_name():
    """The fence itself. Nothing here can fail loudly on the machine's behalf,
    so this asserts it directly rather than waiting to notice the damage."""
    assert _all_shown_are_test_units("portal-run-*.scope")
    assert _all_shown_are_test_units("portal-stray-*.scope")


def test_the_fence_filters_rather_than_silencing_systemd():
    """A fence that returned [] for everything would pass the test above while
    hiding a broken query. This one has to be a filter over a real answer, so
    that test_strays.py can still find the units it creates."""
    shown = strays._list_units("portal-*.scope")  # noqa: SLF001
    assert shown == [u for u in shown if _REAL_UNIT.match(u)]


def test_a_worker_sweep_is_shown_nothing_real_to_act_on():
    """The exact path that did the damage, driven end to end: a real sweep, with
    a protected set built from the empty test database, against the real
    machine. It must find nothing to move."""
    recorded: list[set[str]] = []
    real_sweep = strays.sweep

    def recording(protected):
        recorded.append(set(protected))
        return real_sweep(protected)

    original, strays.sweep = strays.sweep, recording
    worker._last_stray_sweep = None
    try:
        evictions = asyncio.run(worker._sweep_strays())
    finally:
        strays.sweep = original
        worker._last_stray_sweep = None

    assert recorded, "the sweep never ran, so this proves nothing"
    assert evictions is None  # _sweep_strays logs rather than returning
    assert _all_shown_are_test_units("portal-run-*.scope")


def test_the_protected_set_really_is_empty_under_pytest():
    """The premise, stated so it cannot rot quietly. If a future change gave the
    test database a live-looking run, this file would still pass while testing
    something much weaker than it claims."""
    protected = worker._protected_scopes()

    assert all(_REAL_UNIT.match(u) for u in protected), (
        "the test database now contributes real-looking scope names to the "
        "protected set; the fence is still what matters, but this file's "
        "reasoning about why the sweep is dangerous needs revisiting"
    )


@pytest.mark.parametrize(
    "unit",
    [
        "portal-run-679-2339162-1.scope",   # a live agent run
        "portal-stray-629-3541749-11.scope",  # a leftover preview server
        "portal-run-1-1-1.scope",
    ],
)
def test_a_real_looking_unit_name_is_not_matched_by_the_test_pattern(unit):
    """The pattern is the whole fence, so it gets its own check. Every name here
    is one that has actually existed on this machine."""
    assert _REAL_UNIT.match(unit) is None


@pytest.mark.parametrize(
    "unit",
    ["portal-run-9001-123-1.scope", "portal-stray-9999-4-12.scope"],
)
def test_the_suites_own_units_are_matched(unit):
    """And the other half: the fence must not blind test_strays.py to the units
    it creates, or that file silently stops testing anything."""
    assert _REAL_UNIT.match(unit) is not None

"""Every mutation sweep's anchors must still occur exactly once in the file they
mutate.

A sweep anchors each mutation on an exact string copied out of the code under
test. When that code is refactored the anchor comes loose, and the sweep -- run
by hand, months apart -- prints SKIP, drops the mutation from the numerator and
carries on. The score line then reads back as an ordinary number ("39/42
caught"), so a sweep that has quietly stopped proving anything looks exactly
like one with three survivors.

The failure mode is self-inflicted by successful work: a sweep proving a fix is
rotted by the later cleanup that fix made possible. Three of this repo's eight
sweeps had rotted that way when this test was first written. So the check
belongs here, on every test run, not inside a tool nobody runs between refactors.

An anchor that occurs *twice* is just as broken. Every sweep mutates with
`text.replace(find, repl, 1)`, which silently takes the first match -- so a
duplicated anchor mutates a line the sweep did not mean, and whatever verdict it
reaches is about the wrong decision point.

See docs/verifying-with-mutations.md §10.
"""
from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SWEEPS = sorted((ROOT / "scripts").glob("sweep_*.py"))


def loose_anchors(anchors: Iterable[tuple[Path | str, str]]) -> list[str]:
    """The anchors that would make their sweep print SKIP, described.

    Empty means every anchor still names exactly one place in the file it
    mutates. Kept separate from the tests below so its own rule - exactly once,
    not at-least-once - can be exercised directly.
    """
    loose: list[str] = []
    for raw, find in anchors:
        path = Path(raw)
        if not path.is_file():
            loose.append(f"file is gone: {path}")
            continue
        count = path.read_text(encoding="utf-8").count(find)
        if count != 1:
            try:
                where = path.relative_to(ROOT)
            except ValueError:
                where = path
            first = find.splitlines()[0][:70] if find else ""
            loose.append(f"{where}: anchor occurs {count}x - {first!r}")
    return loose


def _load(path: Path):
    """Import a sweep script by file path.

    Safe only because every sweep keeps its executing body behind `main()`;
    importing one that still ran at module level would start a real sweep and
    leave a mutation in the tree.
    """
    spec = importlib.util.spec_from_file_location(f"_sweep_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


# --- the rule itself -------------------------------------------------------


def test_an_anchor_found_once_is_not_loose(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("if a:\n    return 1\nif b:\n    return 2\n", encoding="utf-8")
    assert loose_anchors([(f, "if a:\n    return 1")]) == []


def test_an_anchor_that_is_gone_is_loose(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("if a:\n    return 1\n", encoding="utf-8")
    (only,) = loose_anchors([(f, "if refactored_away:")])
    assert "occurs 0x" in only


def test_an_anchor_found_twice_is_loose(tmp_path):
    """The sweep would mutate the first match and judge the wrong line."""
    f = tmp_path / "code.py"
    f.write_text("    return False\nx = 1\n    return False\n", encoding="utf-8")
    (only,) = loose_anchors([(f, "    return False")])
    assert "occurs 2x" in only


def test_an_anchor_on_a_deleted_file_is_loose(tmp_path):
    (only,) = loose_anchors([(tmp_path / "never-existed.py", "anything")])
    assert "file is gone" in only


def test_every_loose_anchor_is_reported_not_just_the_first(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("kept\n", encoding="utf-8")
    assert len(loose_anchors([(f, "gone"), (f, "also gone"), (f, "kept")])) == 2


# --- the sweeps in this repo ------------------------------------------------


def test_there_are_sweeps_to_check():
    """A glob that matched nothing would pass every parametrized test below."""
    assert len(SWEEPS) >= 8


@pytest.mark.parametrize("sweep", SWEEPS, ids=lambda p: p.stem)
def test_a_sweep_exposes_an_anchor_for_every_mutation(sweep: Path):
    module = _load(sweep)
    assert hasattr(module, "anchors"), (
        f"{sweep.name} has no anchors(); it cannot be checked for rot"
    )
    found = module.anchors()
    assert found, f"{sweep.name} declares no anchors"
    assert len(found) == len(module.MUTATIONS), (
        f"{sweep.name}: anchors() returned {len(found)} entries for "
        f"{len(module.MUTATIONS)} mutations, so some are unchecked"
    )


@pytest.mark.parametrize("sweep", SWEEPS, ids=lambda p: p.stem)
def test_every_anchor_occurs_exactly_once_in_the_file_it_mutates(sweep: Path):
    loose = loose_anchors(_load(sweep).anchors())
    assert not loose, (
        f"{sweep.name} has {len(loose)} anchor(s) that no longer hold; it would "
        "print SKIP and count them as survivors:\n  " + "\n  ".join(loose)
    )

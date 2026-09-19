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


def assert_anchors_hold(name: str, anchors: Iterable[tuple[Path | str, str]]) -> None:
    """Raise unless every anchor still names exactly one place in its file.

    A function rather than an inline assert so the tests below can prove the
    assertion fires. Inline, it could only ever be exercised against the sweeps
    in this repo, all of which pass -- so nothing could tell it from a `loose`
    that is computed and then never looked at.
    """
    loose = loose_anchors(anchors)
    if loose:
        raise AssertionError(
            f"{name} has {len(loose)} anchor(s) that no longer hold; it would "
            "print SKIP and count them as survivors:\n  " + "\n  ".join(loose)
        )


def assert_sweep_declares_anchors(name: str, module) -> None:
    """Raise unless the sweep offers one anchor per mutation.

    A sweep with no `anchors()` is not a sweep that passes; it is a sweep
    nothing can check. Same for one that returns a subset.
    """
    if not hasattr(module, "anchors"):
        raise AssertionError(f"{name} has no anchors(); it cannot be checked for rot")
    found = module.anchors()
    if not found:
        raise AssertionError(f"{name} declares no anchors")
    if len(found) != len(module.MUTATIONS):
        raise AssertionError(
            f"{name}: anchors() returned {len(found)} entries for "
            f"{len(module.MUTATIONS)} mutations, so some are unchecked"
        )


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


def test_the_glob_finds_every_sweep_script_on_disk():
    """A glob that matched nothing would pass every parametrized test below by
    matching nothing to check, so the count is pinned against `scripts/` walked
    a second way rather than against a number written here."""
    on_disk = [p for p in (ROOT / "scripts").iterdir()
               if p.is_file() and p.name.startswith("sweep_") and p.suffix == ".py"]
    assert len(SWEEPS) == len(on_disk) >= 8


@pytest.mark.parametrize("sweep", SWEEPS, ids=lambda p: p.stem)
def test_a_sweep_exposes_an_anchor_for_every_mutation(sweep: Path):
    assert_sweep_declares_anchors(sweep.name, _load(sweep))


@pytest.mark.parametrize("sweep", SWEEPS, ids=lambda p: p.stem)
def test_every_anchor_occurs_exactly_once_in_the_file_it_mutates(sweep: Path):
    assert_anchors_hold(sweep.name, _load(sweep).anchors())


# --- the harness, against sweeps built for the purpose -----------------------
#
# The eight real sweeps all pass, which is the point of them but makes them
# useless for proving the harness reacts. These build a sweep on disk instead,
# so each guard has an owner that does not depend on the repo being broken.


def _fake_sweep(tmp_path: Path, anchors_body: str, mutations: int = 2) -> Path:
    target = tmp_path / "code.py"
    target.write_text("line one\nline two\n", encoding="utf-8")
    script = tmp_path / "sweep_made_up.py"
    script.write_text(
        "from pathlib import Path\n"
        f"TARGET = Path({str(target)!r})\n"
        f"MUTATIONS = [(TARGET, 'x', 'y', 'z')] * {mutations}\n"
        f"{anchors_body}\n",
        encoding="utf-8",
    )
    return script


def test_a_sweep_whose_anchor_rotted_raises(tmp_path):
    script = _fake_sweep(
        tmp_path, "def anchors():\n    return [(TARGET, 'line one'), (TARGET, 'gone')]"
    )
    with pytest.raises(AssertionError, match="1 anchor"):
        assert_anchors_hold(script.name, _load(script).anchors())


def test_a_sweep_whose_anchors_all_hold_raises_nothing(tmp_path):
    script = _fake_sweep(
        tmp_path, "def anchors():\n    return [(TARGET, 'line one'), (TARGET, 'line two')]"
    )
    assert_anchors_hold(script.name, _load(script).anchors()) is None


def test_a_sweep_whose_anchors_returns_nothing_raises(tmp_path):
    """An empty sweep is not a passing sweep. With no mutations either, the
    per-mutation count below agrees with it, so only this guard is left."""
    script = _fake_sweep(tmp_path, "def anchors():\n    return []", mutations=0)
    with pytest.raises(AssertionError, match="declares no anchors"):
        assert_sweep_declares_anchors(script.name, _load(script))


def test_a_sweep_with_no_anchors_function_raises(tmp_path):
    script = _fake_sweep(tmp_path, "# no anchors() here")
    with pytest.raises(AssertionError, match="no anchors"):
        assert_sweep_declares_anchors(script.name, _load(script))


def test_a_sweep_whose_anchors_cover_only_some_mutations_raises(tmp_path):
    script = _fake_sweep(
        tmp_path, "def anchors():\n    return [(TARGET, 'line one')]", mutations=2
    )
    with pytest.raises(AssertionError, match="some are unchecked"):
        assert_sweep_declares_anchors(script.name, _load(script))


def test_a_sweep_declaring_an_anchor_per_mutation_raises_nothing(tmp_path):
    script = _fake_sweep(
        tmp_path,
        "def anchors():\n    return [(TARGET, 'line one'), (TARGET, 'line two')]",
    )
    assert assert_sweep_declares_anchors(script.name, _load(script)) is None

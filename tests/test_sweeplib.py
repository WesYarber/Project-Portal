"""A sweep that puts a file back must put its clock back too.

Filed from site-wide-tools on 2026-09-21. Writing the original bytes back
rewrites the mtime even though not one byte changed, so after an in-place
sweep every file it touched looks freshly edited. Three projects on this
estate - secret-shopper-helper, mtg-proxy-forge and cork-engraving-modeler -
each answered a deploy-drift UNDEPLOYED finding within six minutes of each
other that morning, having measured their own running code, found it current,
and traced the finding to exactly this. One sweep touched ~40 files.

Two claims here: `deploy/sweeplib` restores content and clock together, and
no sweep script in this repo restores content any other way.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

import sweeplib  # noqa: E402


# An arbitrary fixed instant, far enough in the past that no real edit could
# land on it by accident. What matters is only that it is not "now".
THEN = 1_600_000_000
THEN_NS = THEN * 1_000_000_000


@pytest.fixture
def module(tmp_path):
    path = tmp_path / "thing.py"
    path.write_text("VALUE = 1\n")
    os.utime(path, (THEN, THEN))
    return path


def test_a_naive_restore_moves_the_clock(module):
    """The defect this module exists to prevent, proved rather than asserted -
    without it the tests below could be green for the wrong reason."""
    original = module.read_text()
    module.write_text("VALUE = 2\n")
    module.write_text(original)
    assert module.read_text() == original
    assert os.stat(module).st_mtime_ns != THEN_NS


def test_capture_and_restore_put_back_the_bytes_and_the_clock(module):
    originals = sweeplib.capture([module])
    module.write_text("VALUE = 2\n")
    sweeplib.restore(originals)
    assert module.read_text() == "VALUE = 1\n"
    assert os.stat(module).st_mtime_ns == THEN_NS


def test_a_file_the_sweep_never_touched_keeps_its_clock(module):
    # The common case: a sweep captures four files and mutates one at a time.
    originals = sweeplib.capture([module])
    sweeplib.restore(originals)
    assert os.stat(module).st_mtime_ns == THEN_NS


def test_the_clock_is_restored_after_the_content_not_before(module, monkeypatch):
    """Ordering is the whole trick: writing the content is itself what moves
    the mtime, so a restore that stamps the clock first stamps it and then
    immediately loses it again."""
    order: list[str] = []
    real_utime = os.utime

    def watched_utime(path, *args, **kwargs):
        order.append("clock")
        return real_utime(path, *args, **kwargs)

    real_write = Path.write_text

    def watched_write(self, *args, **kwargs):
        order.append("content")
        return real_write(self, *args, **kwargs)

    originals = sweeplib.capture([module])
    module.write_text("VALUE = 2\n")
    monkeypatch.setattr(os, "utime", watched_utime)
    monkeypatch.setattr(Path, "write_text", watched_write)
    sweeplib.restore(originals)
    assert order == ["content", "clock"]


def test_a_stale_pycache_is_purged_beside_the_restored_file(module):
    cache = module.parent / "__pycache__"
    cache.mkdir()
    (cache / "thing.cpython-314.pyc").write_bytes(b"stale")
    originals = sweeplib.capture([module])
    module.write_text("VALUE = 2\n")
    sweeplib.restore(originals)
    assert not cache.exists()
    assert os.stat(module).st_mtime_ns == THEN_NS


def test_capture_reads_the_clock_before_any_mutation(module):
    """`capture` is the only chance to see the real mtime, so it has to stat
    the file itself rather than trusting a caller to hand one over."""
    originals = sweeplib.capture([module])
    assert originals.stamps[module] == (THEN_NS, THEN_NS)


def test_the_mapping_still_reads_as_the_plain_dict_a_sweep_expects(module):
    # Every sweep in deploy/ already loops `for path, text in ORIGINAL.items()`.
    originals = sweeplib.capture([module])
    assert dict(originals) == {module: "VALUE = 1\n"}


# --------------------------------------------------------------------------
# And the rule, applied to every sweep this repo ships
# --------------------------------------------------------------------------

def _sweeps() -> list[Path]:
    """Every mutation sweep this repo ships, in both places they live."""
    found = sorted(
        p
        for directory in ("deploy", "scripts")
        for p in (ROOT / directory).glob("*.py")
        if re.match(r"(sweep_|mutsweep_)", p.name)
    )
    assert len(found) > 10, "sweep scripts not found - this guard would pass vacuously"
    return found


@pytest.mark.parametrize("path", _sweeps(), ids=lambda p: p.name)
def test_every_sweep_either_works_on_an_export_or_restores_the_clock(path):
    """The two honest shapes. Either the sweep exports the tracked files to a
    scratch directory and never writes the checkout at all - which is what the
    newer ones do and is strictly better - or it edits in place and goes
    through `sweeplib`, which cannot restore content without the clock.

    A hand-rolled `path.write_text(original)` is neither, and it is what
    stamped ~40 files as freshly deployed.
    """
    source = path.read_text(encoding="utf-8")
    exports = "mkdtemp" in source
    uses_lib = "sweeplib" in source
    assert exports or uses_lib, (
        f"{path.name} restores files by hand; use deploy/sweeplib.capture/restore "
        "or sweep an export in /tmp"
    )


@pytest.mark.parametrize("path", _sweeps(), ids=lambda p: p.name)
def test_no_sweep_hand_rolls_a_write_back(path):
    """The specific line that loses the clock: writing a previously-read text
    back into the tree. Read off the syntax tree rather than grepped, because
    the string form varies (`encoding=` or not, `Path` or `TARGET`)."""
    source = path.read_text(encoding="utf-8")
    if "mkdtemp" in source:
        return  # writes a scratch export, not the checkout
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "write_text":
            # Restoring is writing back a name captured earlier; mutating
            # writes an expression (`original.replace(...)`). Only the first
            # is the clock-losing shape.
            if node.args and isinstance(node.args[0], ast.Name):
                pytest.fail(
                    f"{path.name}:{node.lineno} writes a captured text straight "
                    "back; route it through deploy/sweeplib.restore"
                )

"""Surfacing the Claude CLI's own auto-memory read-only on /memory (#223).

The CLI writes memory files at ~/.claude/projects/<encoded-cwd>/memory/*.md as
it runs - a second store the portal does not own. climemory.py reads it (never
writes) so /memory can show it. These pin the encoding, the scan, the
description parse, the mapping to portal projects, and the path-traversal guard
on the file reader.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import climemory, config


def _mem(root: Path, dir_name: str, files: dict[str, str]) -> Path:
    """Create a CLI project dir `dir_name` with a memory/ holding `files`."""
    mem = root / dir_name / "memory"
    mem.mkdir(parents=True)
    for name, text in files.items():
        (mem / name).write_text(text, encoding="utf-8")
    return mem


# --- encode_cwd ------------------------------------------------------------

def test_encode_matches_cli_scheme():
    assert (
        climemory.encode_cwd("/home/someone/project-portal/data/projects/project-portal")
        == "-home-someone-project-portal-data-projects-project-portal"
    )


def test_encode_replaces_dots_and_underscores():
    assert climemory.encode_cwd("/tmp/pytest_of_someone/x.y") == "-tmp-pytest-of-someone-x-y"


def test_encode_keeps_hyphens_and_alnum():
    assert climemory.encode_cwd("/home/someone/tabletop-online") == "-home-someone-tabletop-online"


# --- description parsing ---------------------------------------------------

def test_description_from_frontmatter():
    text = (
        '---\nname: foo\ndescription: "a self-hosted thing at testhost"\n'
        "metadata:\n  type: project\n---\n\nBody line here.\n"
    )
    assert climemory._description_of(text, "foo.md") == "a self-hosted thing at testhost"


def test_description_falls_back_to_first_prose_line():
    text = "# Heading\n\nThe first real line of prose.\nSecond line.\n"
    assert climemory._description_of(text, "x.md") == "The first real line of prose."


def test_description_skips_frontmatter_without_a_description():
    text = "---\nname: foo\nmetadata: bar\n---\n\nReal body.\n"
    assert climemory._description_of(text, "foo.md") == "Real body."


def test_description_empty_file():
    assert climemory._description_of("", "x.md") == ""


# --- scan ------------------------------------------------------------------

def test_scan_reads_files_and_splits_index(tmp_path):
    _mem(
        tmp_path,
        "-home-someone",
        {
            "MEMORY.md": "# Memory Index\n- [foo](foo.md) - a thing\n",
            "foo.md": "---\ndescription: the foo fact\n---\n\nfoo body\n",
            "bar.md": "# Bar\n\nbar body line\n",
        },
    )
    dirs = climemory.scan(root=tmp_path)
    assert len(dirs) == 1
    d = dirs[0]
    assert d.index.startswith("# Memory Index")
    assert d.file_count == 2  # MEMORY.md pulled out of the file list
    names = {f.name for f in d.files}
    assert names == {"foo.md", "bar.md"}
    foo = next(f for f in d.files if f.name == "foo.md")
    assert foo.description == "the foo fact"
    assert d.total_size > 0


def test_scan_maps_known_workspace_to_project(tmp_path):
    ws = config.PROJECTS_DIR / "tabletop"
    enc = climemory.encode_cwd(ws)
    _mem(tmp_path, enc, {"note.md": "# n\n\nbody\n"})
    dirs = climemory.scan(known={enc: ("Tabletop Online", "project")}, root=tmp_path)
    assert dirs[0].mapped is True
    assert dirs[0].label == "Tabletop Online"
    assert dirs[0].kind == "project"


def test_scan_unmapped_is_other_with_raw_dir(tmp_path):
    _mem(tmp_path, "-home-someone-tabletop-online", {"x.md": "# x\n\nb\n"})
    dirs = climemory.scan(root=tmp_path)
    assert dirs[0].mapped is False
    assert dirs[0].kind == "other"
    assert dirs[0].label == "-home-someone-tabletop-online"


def test_scan_skips_empty_memory_dir(tmp_path):
    (tmp_path / "-empty" / "memory").mkdir(parents=True)
    assert climemory.scan(root=tmp_path) == []


def test_scan_skips_dir_without_memory(tmp_path):
    (tmp_path / "-nomem").mkdir()
    assert climemory.scan(root=tmp_path) == []


def test_scan_drops_pytest_temp_dirs(tmp_path):
    _mem(tmp_path, "-tmp-pytest-of-someone-x", {"x.md": "# x\n\nb\n"})
    assert climemory.scan(root=tmp_path) == []


def test_scan_keeps_tmp_dir_when_explicitly_mapped(tmp_path):
    _mem(tmp_path, "-tmp-thing", {"x.md": "# x\n\nb\n"})
    dirs = climemory.scan(known={"-tmp-thing": ("A task", "task")}, root=tmp_path)
    assert len(dirs) == 1
    assert dirs[0].label == "A task"


def test_scan_orders_mapped_first_then_by_weight(tmp_path):
    _mem(tmp_path, "-big-other", {"x.md": "x" * 500 + "\n"})
    _mem(tmp_path, "-small-mapped", {"y.md": "y\n"})
    dirs = climemory.scan(
        known={"-small-mapped": ("Mine", "project")}, root=tmp_path
    )
    assert [d.label for d in dirs] == ["Mine", "-big-other"]


def test_scan_missing_root_is_empty(tmp_path):
    assert climemory.scan(root=tmp_path / "does-not-exist") == []


# --- read_file (path-traversal guard) --------------------------------------

def test_read_file_returns_text(tmp_path):
    _mem(tmp_path, "-d", {"note.md": "hello world\n"})
    assert climemory.read_file("-d", "note.md", root=tmp_path) == "hello world\n"


def test_read_file_rejects_non_md(tmp_path):
    _mem(tmp_path, "-d", {"note.md": "x\n"})
    (tmp_path / "-d" / "memory" / "secret.txt").write_text("nope\n")
    assert climemory.read_file("-d", "secret.txt", root=tmp_path) is None


def test_read_file_rejects_traversal_in_filename(tmp_path):
    _mem(tmp_path, "-d", {"note.md": "x\n"})
    (tmp_path / "evil.md").write_text("secret\n")
    assert climemory.read_file("-d", "../../evil.md", root=tmp_path) is None


def test_read_file_rejects_slash_in_dir(tmp_path):
    _mem(tmp_path, "-d", {"note.md": "x\n"})
    assert climemory.read_file("-d/memory", "note.md", root=tmp_path) is None


def test_read_file_missing_is_none(tmp_path):
    _mem(tmp_path, "-d", {"note.md": "x\n"})
    assert climemory.read_file("-d", "gone.md", root=tmp_path) is None


# --- build_known -----------------------------------------------------------

class _Row(dict):
    """A stand-in for a sqlite3.Row that supports both ["x"] access."""


def test_build_known_maps_projects_tasks_and_source():
    projects = [_Row(slug="tabletop", title="Tabletop Online")]
    tasks = [_Row(id=7, title="A scratch task")]
    known = climemory.build_known(projects, tasks)
    assert known[climemory.encode_cwd(config.PROJECTS_DIR / "tabletop")] == (
        "Tabletop Online",
        "project",
    )
    assert known[climemory.encode_cwd(config.TASKS_DIR / "task-7")] == (
        "A scratch task",
        "task",
    )
    # The portal's own source tree is always attributed.
    assert known[climemory.encode_cwd(config.APP_ROOT)] == (
        "Project Portal (source)",
        "project",
    )


def test_description_unescapes_inner_quotes():
    text = '---\ndescription: "when he says \\"foo\\" he means bar"\n---\n\nbody\n'
    assert climemory._description_of(text, "x.md") == 'when he says "foo" he means bar'

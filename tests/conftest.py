"""Test fixtures: each test gets a throwaway data dir and a fresh DB.

`app.config` exposes its paths as module-level constants and `app.db` caches a
single connection, so the fixture repoints the constants and clears the cached
connection before `init_db()` runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "portal.db")
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(config, "PROFILE_MD", tmp_path / "memory" / "profile.md")
    monkeypatch.setattr(config, "LEARNINGS_MD", tmp_path / "memory" / "learnings.md")
    monkeypatch.setattr(config, "SUGGESTIONS_MD", tmp_path / "memory" / "suggestions.md")

    # `config.cli_version()` shells out to `claude --version` and memoizes the
    # answer in a module global, so exactly one test per process paid for a
    # real subprocess - whichever one happened to run first. That made
    # `test_the_tick_holds_new_runs_and_fires_once_quiet` (which asserts a tick
    # spawns nothing) pass in a full run and fail on its own, and it meant the
    # suite behaved differently on a machine with no CLI installed. Pinned, so
    # no test can reach the real binary.
    monkeypatch.setattr(config, "_cli_version_cache", config.DEFAULT_CLI_VERSION,
                        raising=False)

    if db._CONN is not None:  # noqa: SLF001
        db._CONN.close()  # noqa: SLF001
    monkeypatch.setattr(db, "_CONN", None, raising=False)
    # Tests want an empty portal, not the first-run demo project + question.
    monkeypatch.setattr(db, "_seed_data", lambda: None)

    db.init_db()
    yield tmp_path

    if db._CONN is not None:  # noqa: SLF001
        db._CONN.close()  # noqa: SLF001
    db._CONN = None  # noqa: SLF001

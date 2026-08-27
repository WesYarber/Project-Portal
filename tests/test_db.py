"""Schema migration and the dashboard notification-badge query."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app import config, db


def test_open_question_counts_groups_by_project():
    a = db.create_project("A")
    b = db.create_project("B")
    db.create_project("C")
    db.create_question(a["id"], "q1")
    db.create_question(a["id"], "q2")
    db.create_question(b["id"], "q3")

    counts = db.open_question_counts()
    assert counts == {a["id"]: 2, b["id"]: 1}


def test_answered_questions_drop_out_of_the_counts():
    a = db.create_project("A")
    q = db.create_question(a["id"], "q1")
    db.answer_question(q["id"], "yes")
    assert db.open_question_counts() == {}


def test_dismissed_questions_drop_out_of_the_counts():
    a = db.create_project("A")
    q = db.create_question(a["id"], "q1")
    db.dismiss_question(q["id"])
    assert db.open_question_counts() == {}


def test_init_db_adds_model_column_to_a_v1_database(tmp_path, monkeypatch):
    """A portal.db created before the per-project agent feature must upgrade
    in place rather than crashing on the missing column."""
    old_db = tmp_path / "old.db"
    conn = sqlite3.connect(old_db)
    conn.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'unknown',
            status TEXT NOT NULL DEFAULT 'inbox',
            priority INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO projects (slug, title, created_at, updated_at)
        VALUES ('legacy', 'Legacy', '2026-01-01', '2026-01-01');
        """
    )
    conn.commit()
    conn.close()

    db._CONN.close()  # noqa: SLF001
    monkeypatch.setattr(db, "_CONN", None, raising=False)
    monkeypatch.setattr(config, "DB_PATH", old_db)
    db.init_db()

    project = db.get_project_by_slug("legacy")
    assert project is not None
    assert project["model"] is None
    # The state-model migration also ran: the old status column is renamed
    # aside and the stage carries what it meant.
    assert project["stage"] == "backlog"
    assert project["status_old"] == "inbox"
    assert not db.is_paused(project)
    db.update_project(project["id"], model="opus")
    assert db.get_project_by_slug("legacy")["model"] == "opus"


def test_init_db_is_idempotent():
    before = len(db.list_projects())
    db.init_db()
    db.init_db()
    assert len(db.list_projects()) == before


def test_added_columns_has_no_duplicate_table_keys():
    """A repeated key in the `_ADDED_COLUMNS` literal silently discards every
    migration in the earlier block.

    This has to read the SOURCE: by the time the module is imported Python has
    already collapsed the duplicates, so the dict cannot be asked what it lost.
    Two tables had been in this state - `runs` (five columns) and `todos` (two)
    - and it was invisible for months, because `SCHEMA` creates those columns on
    any fresh install. Only a database old enough to predate them would ever
    have noticed, and by then the failure is a missing column at runtime.
    """
    import ast

    tree = ast.parse(Path(db.__file__).read_text(encoding="utf-8"))
    literal = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and getattr(node.target, "id", "") == "_ADDED_COLUMNS"
    )
    names = [k.value for k in literal.keys if isinstance(k, ast.Constant)]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"repeated table keys silently drop migrations: {duplicates}"


def test_every_declared_migration_actually_reaches_the_table():
    """Whatever `_ADDED_COLUMNS` names must exist after `init_db()`.

    The direct assertion that the mechanism works, rather than that the literal
    looks right - it catches a typo'd table name as well as the duplicate-key
    bug above.
    """
    db.init_db()
    conn = db.get_conn()
    for table, columns in db._ADDED_COLUMNS.items():  # noqa: SLF001
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert present, f"{table} does not exist"
        missing = {name for name, _ in columns} - present
        assert not missing, f"{table} is missing declared columns: {missing}"

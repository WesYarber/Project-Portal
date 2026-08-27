"""Wes's note of 2026-08-06 17:26, the page-layout half.

  * "hide these errored lines then from the agent inside the tool call
    collapsed sections ... make them just slightly red" - tested where the
    console lives, in test_console.py;
  * "the deleted question section should be nested inside the question
    section, and it should be less prominent ... Deleted questions should
    fully delete after 7 days" - markup in test_ui_notes_2026_08_01.py, the
    purge in test_questions.py, and its daily wiring here;
  * "Hide the priority value picker for the projects" - in
    test_priority_removed.py and test_ui_polish_2.py;
  * "Have the description of the project be collapsed by default as it takes
    a lot of space and is rarely read after the project is underway" - here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db, main, worker

STYLE_CSS = Path(main.__file__).parent / "static" / "style.css"


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture
def project(temp_data_dir):
    row = db.create_project(
        "Dice Tower", "A thing that rolls dice.", stage="active",
        build_approved=True, slug="dice-tower",
    )
    ws = config.PROJECTS_DIR / "dice-tower"
    ws.mkdir(parents=True, exist_ok=True)
    return row


# --------------------------------------------------------------------------
# The description starts folded
# --------------------------------------------------------------------------

def test_the_description_is_collapsed_by_default(client, project):
    """The prose is the fold's body now, not its summary - so a closed fold
    actually hides it, where before "closed" only hid the edit form."""
    body = client.get("/project/dice-tower").text
    fold = body[body.index('class="details-editor') :]
    opening_tag = fold[: fold.index(">")]
    assert "quiet-fold" in opening_tag
    assert "open" not in opening_tag
    # The prose sits after the summary, inside the body.
    assert fold.index("</summary>") < fold.index("A thing that rolls dice.")


def test_the_editor_is_a_second_fold_inside_it(client, project):
    """Opening the description to read it must not also unroll the form."""
    body = client.get("/project/dice-tower").text
    fold = body[body.index('class="details-form') :]
    opening_tag = fold[: fold.index(">")]
    assert "open" not in opening_tag
    assert fold.index("</summary>") < fold.index('name="description"')


def test_a_project_with_no_description_opens_straight_to_the_editor(client, temp_data_dir):
    """There is nothing to collapse; hiding an empty state behind a click
    would just cost the click."""
    db.create_project("Bare", "", stage="active", slug="bare")
    (config.PROJECTS_DIR / "bare").mkdir(parents=True, exist_ok=True)
    body = client.get("/project/bare").text
    fold = body[body.index('class="details-editor') :]
    assert " open" in fold[: fold.index(">")]
    assert "No description - click to write one." in fold


def test_the_quiet_fold_line_is_small_and_dim():
    """The shared look Wes described for the deleted questions - "just a small
    gray line of text that can be clicked to expand it" - and the description
    line wears it too."""
    css = re.sub(r"/\*.*?\*/", "", STYLE_CSS.read_text(encoding="utf-8"), flags=re.S)
    match = re.search(r"\.quiet-fold > summary \{([^}]*)\}", css)
    assert match, "no .quiet-fold > summary rule in style.css"
    decls = match.group(1)
    assert "color: var(--terminal-dim)" in decls
    assert "cursor: pointer" in decls
    assert "list-style: none" in decls


# --------------------------------------------------------------------------
# Deleted questions age out daily
# --------------------------------------------------------------------------

def test_the_daily_prune_ages_deleted_questions(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "prune_hook_audit", lambda: 0)
    monkeypatch.setattr(db, "prune_deleted_questions", lambda: calls.append(1) or 0)
    monkeypatch.setattr(worker, "_audit_pruned_day", None)
    worker._daily_audit_prune()  # noqa: SLF001
    worker._daily_audit_prune()  # noqa: SLF001
    assert len(calls) == 1


def test_a_failing_question_prune_waits_a_day_without_raising(monkeypatch):
    monkeypatch.setattr(db, "prune_hook_audit", lambda: 0)
    monkeypatch.setattr(db, "prune_deleted_questions", lambda: 1 / 0)
    monkeypatch.setattr(worker, "_audit_pruned_day", None)
    worker._daily_audit_prune()  # must not raise  # noqa: SLF001

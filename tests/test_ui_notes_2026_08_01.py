"""Wes's note of 2026-08-01 19:42, the project-page half.

  * "Uppercase 'Since you last looked' on side-bar and make it jump-able with
    'S'";
  * "combine the workspace and attachments sections into the new files section.
    Make it jump to with 'F'. When opening an attachment file, it should use the
    file pop up stuff to open it without leaving the page if possible. If not
    possible, at least open it on a new page with the same file viewer that
    would be used if opening a file from what used to be the workspace";
  * "have the todo section be collapsed by default";
  * "make the 'Saved for later' not its own unique section, but put it at the
    bottom as a sort of sub-header under 'Questions'. Same for deleted
    questions - move that to Questions."

The rail half of the same note is in test_rail_chapters.py; the layout half is
in test_rail_layout.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db, jumpkeys, main

APP_JS = Path(main.__file__).parent / "static" / "app.js"


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture
def project(temp_data_dir):
    row = db.create_project(
        "Dice Tower", "A thing.", stage="active", build_approved=True, slug="dice-tower"
    )
    ws = config.PROJECTS_DIR / "dice-tower"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "NOTE.md").write_text("hello")
    return row


def page(client) -> str:
    return client.get("/project/dice-tower").text


# --------------------------------------------------------------------------
# "Since you last looked"
# --------------------------------------------------------------------------

def _unacked_run(project_id: int) -> int:
    run_id = db.create_run(project_id, "build", "opus")
    db.finish_run(run_id, "ok", summary="Shipped a thing.")
    return run_id


def test_the_banner_heading_is_capitalized(client, project):
    _unacked_run(project["id"])
    body = page(client)
    assert "Since you last looked" in body
    assert "since you last looked" not in body


def test_the_banner_is_a_jump_target(client, project):
    """The rail builds its chapter list from these, so one attribute makes the
    section reachable by click and by key at once."""
    _unacked_run(project["id"])
    assert 'data-jump="summary"' in page(client)


def test_s_is_bound_to_the_banner(client, project):
    assert jumpkeys.bindings({})["s"] == ["summary"]


def test_the_banner_target_is_absent_once_there_is_nothing_to_read(client, project):
    """A key that lands on a section that is not there does nothing, which is
    correct - the banner disappears the moment it is acknowledged."""
    assert 'data-jump="summary"' not in page(client)


# --------------------------------------------------------------------------
# Files: one section for the workspace and the uploads
# --------------------------------------------------------------------------

def test_there_is_one_files_section_and_not_two(client, project):
    db.add_attachment(project["id"], "shot.png", "0001-shot.png", "image/png", 12)
    body = page(client)
    assert 'id="files"' in body
    # The old pair of top-level folds is gone; their ids survive as the shelves
    # inside, because links to them exist in journal entries already written.
    assert 'class="fold-section attach-block"' not in body
    assert 'class="fold-section workspace-block"' not in body
    assert '<h3 class="files-sub" id="attachments">Uploaded</h3>' in body
    assert '<h3 class="files-sub" id="workspace">Workspace</h3>' in body


def test_f_is_bound_to_the_files_section(client, project):
    assert jumpkeys.bindings({})["f"] == ["files"]
    assert 'data-jump="files"' in page(client)


def test_an_uploaded_image_opens_in_the_in_page_viewer(client, project):
    """"it should use the file pop up stuff to open it without leaving the
    page if possible". `journal-media-link data-lightbox` is the anchor app.js
    binds the image viewer to - the same one a journal image uses."""
    db.add_attachment(project["id"], "shot.png", "0001-shot.png", "image/png", 12)
    body = page(client)
    assert '<a class="journal-media-link" data-lightbox href="/attachment/1">' in body


def test_an_uploaded_file_that_is_not_an_image_opens_in_the_file_viewer(client, project):
    """"at least open it on a new page with the same file viewer that would be
    used if opening a file from what used to be the workspace".

    It is the same viewer because it is the same file: attachments.py writes an
    upload into `<workspace>/attachments/`, so /file/<slug>/attachments/<name>
    is the workspace route pointed at the very bytes /attachment/<id> serves.
    """
    db.add_attachment(project["id"], "spec.pdf", "0001-spec.pdf", "application/pdf", 12)
    body = page(client)
    assert "/file/dice-tower/attachments/0001-spec.pdf" in body


def test_no_upload_lands_on_a_raw_download_any_more(client, project):
    """The name beside a file used to link at the bytes, which on anything the
    browser will not render inline is a download rather than a look."""
    db.add_attachment(project["id"], "notes.txt", "0001-notes.txt", "text/plain", 12)
    body = page(client)
    assert '<a href="/attachment/1">notes.txt</a>' not in body
    assert '<a href="/file/dice-tower/attachments/0001-notes.txt">notes.txt</a>' in body


def test_the_uploads_shelf_is_absent_with_nothing_in_it(client, project):
    body = page(client)
    assert "Uploaded" not in body
    # ...but the workspace shelf is always there, because the ssh line and the
    # tree are what the section is for.
    assert 'id="workspace"' in body


# --------------------------------------------------------------------------
# The todo list starts folded
# --------------------------------------------------------------------------

def test_the_todo_section_is_collapsed_by_default(client, project):
    db.add_todo(project["id"], "do the thing", owner="agent")
    body = page(client)
    assert 'class="fold-section todo-block" id="todos"' in body
    assert 'id="todos" open' not in body


def test_the_open_count_is_readable_without_opening_it(client, project):
    db.add_todo(project["id"], "one", owner="agent")
    db.add_todo(project["id"], "two", owner="user")
    done = db.add_todo(project["id"], "three", owner="agent")
    db.set_todo_done(done["id"], True)
    assert "2 open" in page(client)


def test_the_fold_remembers_being_opened(client, project):
    """Every tick, tag, refile and add in that list is a POST that redirects
    back here. Without a memory each one would shut the list you are working
    in, and "collapsed by default" would read as "collapsed no matter what you
    do"."""
    assert "data-fold-remember" in page(client)
    src = APP_JS.read_text(encoding="utf-8")
    assert "details[data-fold-remember]" in src
    assert 'FOLD_PREFIX = "portal-fold:"' in src


def test_the_server_owned_folds_do_not_remember(client, project):
    """The console opens while a run is going, the ask box opens when there is
    a question pending, sub-projects open when there are some. Remembering
    those would be overruling a live fact with a stale click."""
    body = page(client)
    console = body[body.index('id="agent-console-details"'):]
    console = console[: console.index(">")]
    assert "data-fold-remember" not in console


# --------------------------------------------------------------------------
# Saved and deleted questions live under Questions
# --------------------------------------------------------------------------

def test_deleted_questions_sit_under_the_questions_heading(client, project):
    """And as a quiet-fold, not a fold-section peer of "Saved for later" -
    Wes, 2026-08-06: "less prominent ... just a small gray line of text that
    can be clicked to expand it"."""
    q = db.create_question(project["id"], "Was this a good idea?")
    db.delete_question(q["id"])
    body = page(client)
    assert body.index(">Questions<") < body.index("Was this a good idea?")
    assert 'class="quiet-fold deleted-questions"' in body
    assert "1 deleted question<" in body
    assert "fold-section deleted-questions" not in body


def test_the_heading_renders_when_only_the_quiet_shelves_have_anything(client, project):
    """A project with nothing open but four saved ones used to draw a fold with
    no section above it."""
    q = db.create_question(project["id"], "Shall I?")
    client.post(f"/questions/{q['id']}/dismiss")
    body = page(client)
    assert ">Questions<" in body
    assert "Nothing open right now." in body


def test_there_is_no_questions_heading_on_a_project_with_none(client, project):
    assert ">Questions<" not in page(client)


def test_the_quiet_shelves_are_not_chapters_of_their_own(client, project):
    """They are shelves of the Questions section, and the rail lists sections.
    Offering "Questions" and then "Saved for later" beside it would re-make the
    equality Wes asked twice to have removed."""
    q = db.create_question(project["id"], "Shall I?")
    client.post(f"/questions/{q['id']}/dismiss")
    body = page(client)
    tag = body[body.rindex("<details", 0, body.index("saved-questions")) :]
    assert 'data-jump-nav="off"' in tag[: tag.index(">")]


def test_the_subprojects_optout_is_a_real_attribute(client, project):
    """An attribute written as a Jinja expression is autoescaped, so
    `{{ '...' if x else 'data-jump-nav="off"' }}` emits `&#34;` and the browser
    reads one long attribute name. It renders, it validates, and the opt-out
    does nothing - which is how the empty sub-projects fold went on listing
    itself in the rail after this was supposedly fixed."""
    body = page(client)
    tag = body[body.index('id="subprojects"') :]
    tag = tag[: tag.index(">")]
    assert 'data-jump-nav="off"' in tag
    assert "&#34;" not in tag

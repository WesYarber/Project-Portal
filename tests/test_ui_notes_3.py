"""Wes's 00:44 note on 2026-07-22, item by item.

  * scroll caps on the workspace file list (10), the todo lists (16) and the
    journal (much longer), with the workspace section fully collapsible;
  * dropdowns the same height as the text fields;
  * the ssh copy button actually copying;
  * the per-project runs/day cap out of the default view;
  * `pause building` moved into the action row between `run agent now` and
    `queue research burst`, all at one height;
  * `attach files` on the same line as `add note`, at the same height, with
    the "record audio needs https" sentence gone;
  * the offline splash art, which was hand-typed and shearing.

The file-type viewer half of the same note is in test_fileview.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db, main

STYLE = Path(main.__file__).parent / "static" / "style.css"
APP_JS = Path(main.__file__).parent / "static" / "app.js"
BASE_HTML = Path(main.__file__).parent / "templates" / "base.html"


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture
def project(temp_data_dir):
    row = db.create_project("Dice Tower", "A thing.", stage="active", build_approved=True, slug="dice-tower")
    db.update_project(row["id"], build_approved=1)
    ws = config.PROJECTS_DIR / "dice-tower"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    return db.get_project_by_slug("dice-tower")


def page(client) -> str:
    resp = client.get("/project/dice-tower")
    assert resp.status_code == 200
    return resp.text


# --- scroll caps -----------------------------------------------------------

def test_the_three_lists_are_capped_and_the_caps_are_named_variables():
    """The numbers live in :root so Wes can tune them without hunting through
    rules - he said outright these "may be tuned later"."""
    css = STYLE.read_text(encoding="utf-8")
    assert "--files-rows: 10;" in css
    assert "--todo-rows: 16;" in css
    assert "--journal-max-h:" in css
    assert ".scroll-cap-files { max-height: calc(var(--files-rows) * var(--files-row-h)); }" in css
    assert ".scroll-cap-todo { max-height: calc(var(--todo-rows) * var(--todo-row-h)); }" in css


def test_the_row_heights_are_pinned_so_a_cap_is_a_whole_number_of_items():
    """A cap of "10 files" is only 10 files if a file row is exactly
    --files-row-h tall. A ratio line-height drifts with the font."""
    css = STYLE.read_text(encoding="utf-8")
    assert ".file-list li { padding: 0.15rem 0; line-height: 1.5rem; }" in css
    assert "line-height: 1.35rem" in css  # .todo-text
    # 1.5 + 2*0.15 = 1.8; 1.35 + 2*0.35 = 2.05
    assert "--files-row-h: 1.8rem;" in css
    assert "--todo-row-h: 2.05rem;" in css


def test_the_journal_cap_is_much_larger_than_the_list_caps():
    """Wes: "make the journal a scrollable list, but make it much longer than
    these others"."""
    css = STYLE.read_text(encoding="utf-8")
    journal = re.search(r"--journal-max-h:\s*(\d+)vh", css)
    assert journal and int(journal.group(1)) >= 60


def test_each_list_actually_carries_its_cap_class(client, project):
    db.add_todo(project["id"], "something to do", owner="agent")
    db.add_journal(project["id"], "user", "note", "a journal entry")
    body = page(client)
    # The workspace list became a tree, so its cap moved from the <ul> onto the
    # wrapper around it - on the list, an opened folder would push the box back
    # open instead of scrolling inside it.
    assert 'class="file-tree-scroll scroll-cap scroll-cap-files"' in body
    assert 'class="todo-list scroll-cap scroll-cap-todo"' in body
    assert "scroll-cap scroll-cap-journal" in body


def test_a_capped_list_still_renders_every_item(client, project):
    """The cap is a scrollbox, not a truncation - nothing may be dropped, or
    the agent's list and the page would disagree about what is on it."""
    for n in range(25):
        db.add_todo(project["id"], f"item number {n}", owner="agent")
    body = page(client)
    for n in range(25):
        assert f"item number {n}" in body


def test_the_workspace_section_folds(client, project):
    """Fully collapsible, with the count in the summary so it can be read
    without opening it - the same shape as the console and attachments."""
    body = page(client)
    assert 'class="fold-section workspace-block" id="workspace"' in body
    assert '<span class="fold-section-label">Workspace</span>' in body
    assert "1 file<" in body


# --- control heights -------------------------------------------------------

def test_selects_and_text_fields_share_one_pinned_height():
    """Wes: "Dropdown menus all over need to be the same height as the text
    entry fields." A native select sizes itself off the font and ignores the
    padding, which is why min-height alone never made them match."""
    css = STYLE.read_text(encoding="utf-8")
    assert "--control-h: 2.3rem;" in css
    assert 'input[type="text"], input[type="number"], input[type="password"], select {\n  height: var(--control-h);\n}' in css
    assert "appearance: none;" in css
    assert "-webkit-appearance: none;" in css


def test_a_textarea_is_not_pinned_to_the_control_height():
    """It is the one control meant to be taller, and app.js grows it to fit."""
    css = STYLE.read_text(encoding="utf-8")
    pinned = re.search(r'input\[type="text"\][^{]*\{\s*height: var\(--control-h\);', css)
    assert pinned and "textarea" not in pinned.group(0)


def test_buttons_share_the_same_height_as_the_fields():
    """Which is what makes a <label class="btn"> (attach files) exactly as tall
    as the <button> beside it."""
    css = STYLE.read_text(encoding="utf-8")
    block = css.split("button, .btn {", 1)[1].split("}", 1)[0]
    assert "height: var(--control-h);" in block
    assert "display: inline-flex;" in block


def test_the_delete_cross_opts_out_of_the_shared_height():
    """A 2.3rem cross would set the floor for every todo row and blow the
    16-row cap."""
    css = STYLE.read_text(encoding="utf-8")
    block = css.split(".todo-del {", 1)[1].split("}", 1)[0]
    assert "height: auto;" in block


# --- the copy button -------------------------------------------------------

def test_copy_falls_back_to_execcommand():
    """navigator.clipboard does not exist on plain http, which is how Wes
    reaches the portal - so on his machine the modern API never runs. The old
    fallback only selected the text, which is why pressing copy did nothing."""
    js = APP_JS.read_text(encoding="utf-8")
    assert 'document.execCommand("copy")' in js
    assert "function legacyCopy" in js


def test_copy_says_something_either_way():
    js = APP_JS.read_text(encoding="utf-8")
    assert '"copied"' in js
    assert '"press ctrl-c"' in js


def test_repeat_presses_do_not_capture_the_confirmation_as_the_label():
    """Without this the second press restores the button to "copied" for
    ever."""
    js = APP_JS.read_text(encoding="utf-8")
    assert 'btn.getAttribute("data-copy-label")' in js
    assert 'btn.setAttribute("data-copy-label", was)' in js


def test_the_ssh_line_is_still_on_the_page_as_text(client, project):
    """Because a copy button that fails silently is worse than no button, the
    command itself stays selectable."""
    body = page(client)
    assert 'id="ssh-command"' in body
    assert 'data-copy="#ssh-command"' in body
    assert "cd " in body and "dice-tower" in body


# --- the runs/day cap ------------------------------------------------------
# The fold moved out of the control bar here, then came off the page entirely
# on Wes's 2026-07-23 note ("I don't use that on projects at all") - the
# on-page assertions live in test_ui_notes_4.py now. Only the route remains.

def test_the_cap_can_still_be_set(client, project):
    client.post("/project/dice-tower/run-cap", data={"max_runs_per_day": "5"},
                follow_redirects=False)
    assert db.get_project_by_slug("dice-tower")["max_runs_per_day"] == 5


# --- the action row --------------------------------------------------------

def test_run_now_sits_before_queue_research(client, project):
    """`pause building` used to sit between these two.

    It came off the page on Wes's 2026-07-27 note ("I know I've never used it")
    - and the journal agreed: its route writes an entry every time it runs, and
    there were zero of them across 841. What is left is still ordered by
    commitment: do it now, then do it when the allowance is free. The button's
    absence is pinned in test_priority_toggle.py.
    """
    body = page(client)
    row = body.split('class="button-row action-row"', 1)[1].split("</div>", 1)[0]
    assert "pause building" not in row
    assert row.index("run agent now") < row.index("queue research burst")


def test_no_button_in_the_action_row_is_small(client, project):
    """Wes: "Make the buttons the same height." A row of three buttons at two
    heights reads as a mistake."""
    body = page(client)
    row = body.split('class="button-row action-row"', 1)[1].split("<h2", 1)[0]
    assert "btn secondary small" not in row


def test_pause_building_is_gone_from_the_hint_line(client, project):
    body = page(client)
    hint = body.split('class="hint">Build approved', 1)[1].split("</p>", 1)[0]
    assert "pause building" not in hint


def test_a_gated_project_has_no_pause_button(client, temp_data_dir):
    """There is nothing to pause: an unapproved project is already not
    building."""
    db.create_project("Gated", "x", stage="backlog", slug="gated")
    body = client.get("/project/gated").text
    assert "pause building" not in body


def test_pause_building_still_works(client, project):
    client.post("/project/dice-tower/revoke-build", follow_redirects=False)
    assert not db.get_project_by_slug("dice-tower")["build_approved"]


# --- the note form ---------------------------------------------------------

def test_add_note_shares_the_attach_row(client, project):
    """Wes: "make the attach files button on the add note section in line with
    add note"."""
    body = page(client)
    row = body.split('class="attach-row"', 1)[1].split("</div>", 1)[0]
    assert "attach files" in row
    assert "add note" in row
    assert "record audio" in row


def test_the_submit_button_is_still_a_submit(client, project):
    body = page(client)
    row = body.split('class="attach-row"', 1)[1].split("</div>", 1)[0]
    assert '<button type="submit" class="btn go">add note</button>' in row


def test_adding_a_note_still_works(client, project):
    client.post("/project/dice-tower/note", data={"note": "hello there"},
                follow_redirects=False)
    entries = db.list_journal(project["id"], limit=10)
    assert any("hello there" in e["content_md"] for e in entries)


def test_the_https_sentence_is_gone():
    """Wes: "get rid of the little text on the line about record audio needs
    https." The https address is still named on Settings > access."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "record audio needs https" not in js
    assert "offerSecureUrl" not in js


def test_the_secure_url_is_still_advertised_somewhere(client, temp_data_dir):
    """Removing the sentence must not remove the only way to find the https
    address - that would make voice memos invisible again."""
    panel = Path(main.__file__).parent / "templates" / "settings.html"
    assert "https" in panel.read_text(encoding="utf-8")


# --- the offline splash ----------------------------------------------------

def test_the_offline_art_is_rectangular():
    """Every line the same width. The old art was hand-typed from figlet's
    "small" font with an F rendered as a C, and lines of four different
    widths."""
    html = BASE_HTML.read_text(encoding="utf-8")
    art = html.split('<pre class="offline-art">', 1)[1].split("</pre>", 1)[0]
    lines = art.split("\n")
    assert len(lines) == 5
    widths = {len(line.rstrip()) for line in lines}
    # Trailing space is not meaningful in the art; what matters is that no line
    # is wildly out of step with the rest.
    assert max(widths) - min(widths) <= 2


def test_the_offline_art_still_reads_as_offline():
    """A cheap smoke test that the letterforms are pygments-free figlet output
    and not, say, half of some other word."""
    html = BASE_HTML.read_text(encoding="utf-8")
    art = html.split('<pre class="offline-art">', 1)[1].split("</pre>", 1)[0]
    # figlet "standard" builds letters out of these; a garbled copy tends to
    # lose the underscore row or gain a stray backtick (the old one had `.\``).
    assert "`" not in art
    assert art.count("|") >= 24
    assert art.startswith("  ___")


def test_the_art_is_not_centered_line_by_line():
    """The box centers its text, and a centered <pre> centers every LINE
    independently - which shears the letterforms apart. That, as much as the
    mistyped glyphs, is what made this look broken."""
    css = STYLE.read_text(encoding="utf-8")
    block = css.split(".offline-art {", 1)[1].split("}", 1)[0]
    assert "text-align: left;" in block
    assert "display: inline-block;" in block


def test_the_overlay_is_still_hidden_by_default(client, project):
    body = page(client)
    assert '<div id="offline-overlay" hidden>' in body

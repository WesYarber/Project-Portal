"""Looking at, renaming and removing a file before the note carrying it is sent.

Wes, 2026-08-28: "I still can't modify (delete) files that I have included in a
note before sending it. I want to be able to view what I have included, rename
it, or remove it."

The second report of this. The first (2026-08-17, "Add a way of removing note
file attachments before the prompt is sent") was answered with a remove button
on each file of an already-POSTED note, in the journal - which is a different
moment. This is about the compose box: files picked, dropped, pasted or shot
with the camera that have not gone anywhere yet. All the form said about them
was one line of gray text - "3 files: a.png, b.png, +1 more" - which is a count,
not a way to change your mind.

Everything here is about a FileList, the one collection in the DOM you cannot
edit: no remove, no reorder, and `name` is read-only. Every operation is a full
rebuild through a DataTransfer, so what can go wrong is all about what the
rebuild does to the OTHER files - dropping them, reordering them, or losing the
bytes - and none of that is visible in the source text. The real functions are
driven under bun against a stub input (tests/js/attach_shelf.mjs).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"
PROJECT_HTML = ROOT / "app" / "templates" / "project.html"
STYLE_CSS = ROOT / "app" / "static" / "style.css"


@pytest.fixture(scope="module")
def ran():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "attach_shelf.mjs"
    out = subprocess.run(
        [bun, str(harness), str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --------------------------------------------------------------------------
# View
# --------------------------------------------------------------------------

def test_every_staged_file_gets_its_own_row_with_all_three_controls(ran):
    rows = ran["rendered"]

    assert [r["name"] for r in rows] == ["shot.png", "statement.pdf"]
    for row in rows:
        assert row["buttons"] == ["rename", "remove"]
        assert row["size"]


def test_a_picture_is_shown_as_the_picture(ran):
    """"View what I have included" means the image for a screenshot - which is
    most of what he attaches, straight off an iPhone with a name like
    IMG_0838.png that says nothing about which screenshot it is."""
    shot, statement = ran["rendered"]

    assert shot["hasThumb"] is True
    assert shot["ext"] is None
    # Anything that cannot be shown says what kind of file it is instead.
    assert statement["hasThumb"] is False
    assert statement["ext"] == "pdf"


def test_sizes_read_as_sizes(ran):
    assert ran["sizes"] == ["512 B", "2 KB", "5.0 MB"]


def test_a_file_too_big_to_send_says_so_on_its_own_row(ran):
    """Beside the file, not only in a status line elsewhere on the form.

    With one line of text for the whole set, "too big" named a file you then had
    to go and find; the row is where the remove button for it already is."""
    small, huge = ran["oversize"]

    assert small["oversize"] is False
    assert huge["oversize"] is True
    assert "too big" in huge["size"]


# --------------------------------------------------------------------------
# Remove
# --------------------------------------------------------------------------

def test_removing_one_file_keeps_the_others_and_their_order(ran):
    """A FileList cannot have an entry taken out of it, so a remove is a full
    rebuild - and the way a rebuild fails is by losing the files either side of
    the one that was meant to go."""
    removed = ran["removed"]

    assert removed["left"] == ["a.png", "c.png"]
    # And the list the reader is looking at agrees with what will be posted.
    assert removed["shelf"] == ["a.png", "c.png"]


# --------------------------------------------------------------------------
# Rename
# --------------------------------------------------------------------------

def test_renaming_rewrites_the_file_that_will_actually_be_posted(ran):
    """A File's name is read-only, so a rename is a new File built from the
    same bytes. Renaming the ROW alone would show him a name the server never
    hears about, which is worse than not offering a rename at all."""
    renamed = ran["renamed"]

    assert renamed["names"] == ["a.png", "the todo list on my phone.png", "c.png"]
    assert renamed["shelf"] == renamed["names"]


def test_a_renamed_file_is_the_same_file(ran):
    """Same bytes, same type, same timestamp: only the name differs."""
    renamed = ran["renamed"]

    assert renamed["size"] == 2048
    assert renamed["type"] == "image/png"
    assert renamed["lastModified"] == 1000


def test_a_renamed_file_stays_where_it_was_in_the_list(ran):
    """Remove-then-re-add would be the easy rebuild, and it would move the file
    to the end. "Nothing moves that he didn't move" - a picture jumping to the
    bottom of the shelf because you fixed its name is exactly that complaint."""
    assert ran["renamed"]["names"][1] == "the todo list on my phone.png"


def test_escape_abandons_a_rename(ran):
    escaped = ran["escaped"]

    assert escaped["names"] == ["keep-me.png"]
    assert escaped["fieldGone"] is True
    assert escaped["nameVisible"] is True


def test_escape_does_not_also_reach_the_page_wide_handler(ran):
    """app.js binds Escape globally to "close whatever is open". Left to
    propagate, abandoning a rename would also collapse the section around it."""
    assert ran["escaped"]["stoppedPropagation"] is True


def test_a_rename_onto_another_staged_files_name_is_refused(ran):
    """Uploads are stored under the name they arrive with, so two files
    claiming one name is one of them silently overwriting the other."""
    dup = ran["duplicate"]

    assert dup["result"] == ""
    assert dup["names"] == ["a.png", "b.png"]


def test_an_empty_or_unchanged_name_is_refused(ran):
    """Blurring the field without typing anything is the common way in, and it
    must not produce a file called ""."""
    empty = ran["emptyName"]

    assert empty["blank"] == ""
    assert empty["same"] == ""
    assert empty["names"] == ["a.png"]


# --------------------------------------------------------------------------
# Living next to the recorder
# --------------------------------------------------------------------------

def test_a_voice_memo_is_left_to_the_recorders_own_shelf(ran):
    """It already has a row up there with a player and a delete button on it.
    Listed here too, one recording would have two rows and two ways to delete
    it - and deleting it in one place would leave the other row behind."""
    excluded = ran["recordingsExcluded"]

    assert excluded["shelf"] == ["shot.png"]
    # Excluded from the LIST, not from the upload.
    assert excluded["posted"] == ["shot.png", "voice-memo-2026-08-28.webm"]


def test_a_file_merely_named_like_a_recording_is_still_listed(ran):
    """The exclusion is a registration the recorder makes, not a guess from the
    name. Guessing from the name would hide a file the user chose from the only
    list that can remove it - and there would be no recorder row to remove it
    from either, so the file would be unremovable."""
    assert ran["namesakeNotHidden"] == ["voice-memo-notes.webm"]


# --------------------------------------------------------------------------
# Not leaking
# --------------------------------------------------------------------------

def test_redrawing_the_shelf_revokes_the_thumbnails_it_replaced(ran):
    """Each image thumbnail is an object URL, which pins the whole file in
    memory until it is revoked. Dropping ten screenshots into a note and
    changing your mind about them would otherwise hold all ten for as long as
    the tab is open - on a phone, that is the tab being killed."""
    urls = ran["objectUrls"]

    assert urls["createdOnFirstDraw"] == 2
    assert urls["revokedOnRedraw"] == 2
    # And the redraw replaces the rows rather than adding to them.
    assert urls["rowsAfterRedraw"] == 2


# --------------------------------------------------------------------------
# The wiring
# --------------------------------------------------------------------------

def test_the_note_form_has_a_shelf_for_the_renderer_to_fill():
    """The whole feature is unreachable without this one element."""
    html = PROJECT_HTML.read_text(encoding="utf-8")

    assert "data-attach-shelf" in html
    # In the note form rather than somewhere else on the page.
    note_form = html.split('class="note-form"')[1].split("</form>")[0]
    assert "data-attach-shelf" in note_form


def test_a_staged_row_survives_a_background_patch():
    """`.attach-row-item` has to be in MORPH_KEEP.

    A staged file is client-only state - the server's render knows nothing
    about it - so a live patch arriving while he is composing would rebuild the
    note form and throw away a screenshot he had dropped in but not sent. The
    same reason `.rec-row` is already there."""
    js = APP_JS.read_text(encoding="utf-8")
    keep = re.search(r"var MORPH_KEEP = (.+?);", js, re.S)

    assert keep, "MORPH_KEEP is gone"
    assert ".attach-row-item" in keep.group(1)


def test_the_rename_field_cannot_zoom_an_iphone_in():
    """Safari zooms the page in on focusing any control whose computed
    font-size is under 16px, and never zooms back out. A literal 16px, not a
    rem - the root font size here is under 16."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.attach-row-rename\s*\{([^}]*)\}", css)

    assert rule, "the rename field has no styling"
    assert "font-size: 16px" in rule.group(1)

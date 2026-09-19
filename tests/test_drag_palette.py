"""The drop palette that opens beside a card while it is being dragged.

Wes, 2026-09-19: "when dragging project cards on the dashboard to change their
status, have a sort of pop-up menu show up next to the project card for you to
drag the project card onto to set that status rather than having to scroll
through the page while dragging that project card to find the correct project
status area to drop it into."

So the sections stay droppable - nothing is taken away from a gesture that
already worked - and a palette of every status opens at the card, which is the
part that makes the drag fit on one screen. The behavior runs for real under
bun (tests/js/drag_palette.mjs) against the actual initProjectDrag out of
app.js, because everything that can break is choreography and arithmetic: what
the palette offers, whether a chip is a live drop target at all (a dragover
that does not preventDefault silently refuses every drop), where the palette
lands for a card at each edge of the viewport, and whether it is taken down
again. The look is pinned by the CSS assertions below and by screenshot.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"
STYLE = ROOT / "app" / "static" / "style.css"


@pytest.fixture(scope="module")
def ran():
    bun = shutil.which("bun")
    if not bun:  # pragma: no cover - bun is present on the machines that matter
        pytest.skip("bun is not installed")
    proc = subprocess.run(
        [bun, str(Path(__file__).parent / "js" / "drag_palette.mjs"), str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- what it offers ----------------------------------------------------------


def test_the_palette_offers_every_zone_in_page_order(ran):
    assert [c["status"] for c in ran["opened"]] == [
        "active", "review", "paused", "backlog", "done",
    ]


def test_the_entries_are_labeled_the_way_the_sections_are(ran):
    """Read off each zone's own data-zone-label, so this file never holds a
    second copy of the status vocabulary to drift from the page's."""
    assert [c["text"] for c in ran["opened"]][1:] == ["review", "paused", "backlog", "done"]
    assert ran["head"] == "drop to move to"


def test_where_the_card_already_is_is_shown_grayed_out_not_removed(ran):
    here = ran["opened"][0]
    assert here["status"] == "active"
    assert "is-current" in here["cls"]
    assert here["text"] == "active (here now)"


def test_the_current_entry_is_not_a_drop_target(ran):
    """It refuses the dragover, so the browser shows "no drop" over it rather
    than accepting a gesture that would do nothing."""
    assert ran["currentChipRefusesDragover"] is True
    assert ran["currentChipPosted"] == []


# --- dropping on it ----------------------------------------------------------


def test_a_chip_accepts_the_drag_and_lights_up(ran):
    """preventDefault on dragover is what makes a drop possible at all: without
    it every drop on the palette is silently refused by the browser."""
    assert ran["dragoverDefaulted"] is True
    assert ran["dragoverLit"] is True
    assert ran["dragleaveUnlit"] is True


def test_dropping_on_a_chip_posts_that_status_for_that_card(ran):
    assert ran["dropPosted"] == [
        {"action": "/project/metronome/status", "fields": {"status": "review"}}
    ]


def test_the_sections_still_take_a_drop_and_still_ignore_a_no_op_one(ran):
    """The palette is an addition. Dragging to a section works as before, and
    dropping a card back in the section it came from still posts nothing."""
    assert ran["zoneLit"] is True
    assert ran["zonePosted"] == [
        {"action": "/project/metronome/status", "fields": {"status": "review"}}
    ]


# --- where it lands ----------------------------------------------------------


def test_the_palette_sits_beside_the_card_it_came_from(ran):
    """Card at x 200-500, y 300-460 in a 1400x900 viewport; a 160x220 palette
    goes to its right and centers on it vertically."""
    assert ran["placedMiddle"] == {"left": "510px", "top": "270px"}


def test_a_card_at_the_right_edge_flips_the_palette_to_its_left(ran):
    """Card at x 1100-1380: to its right the palette would start 10px from the
    edge and run 150px off-screen, which is the bug being fixed, not a new
    place to put it."""
    assert ran["placedRightEdge"]["left"] == "930px"


def test_a_card_near_the_bottom_clamps_the_palette_into_view(ran):
    """Centered on a card at y 820-890 the palette would end at 965, below a
    900px viewport. It is pushed up to sit 8px clear of the bottom."""
    assert ran["placedBottom"]["top"] == "672px"
    assert int(ran["placedBottom"]["top"][:-2]) + 220 == 900 - 8


# --- and comes down again ----------------------------------------------------


def test_the_palette_closes_on_the_drop_and_on_a_canceled_drag(ran):
    assert ran["closedOnDrop"] is True
    assert ran["closedOnDragend"] is True


def test_a_second_drag_never_leaves_two_palettes_on_the_page(ran):
    assert ran["paletteCount"] == 1
    assert ran["allClosed"] == 0


# --- the look ----------------------------------------------------------------


def test_the_palette_is_fixed_so_it_holds_still_while_the_page_scrolls():
    css = STYLE.read_text()
    block = css.split(".drag-palette {", 1)[1].split("}", 1)[0]
    assert "position: fixed" in block
    # Above the right-click menu's layer (60), so a palette never opens behind
    # a menu left over from the same card.
    assert "z-index: 70" in block


def test_a_chip_is_sized_as_a_drop_target_not_as_a_menu_line():
    css = STYLE.read_text()
    block = css.split("\n.drag-chip {", 1)[1].split("}", 1)[0]
    assert "padding: 0.45rem 0.7rem" in block


def test_the_current_entry_is_dimmed():
    css = STYLE.read_text()
    assert ".drag-chip.is-current { opacity: 0.4; }" in css

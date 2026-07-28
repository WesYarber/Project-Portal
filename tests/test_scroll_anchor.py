"""Keeping the view still while a live patch changes the page.

Wes, 2026-07-28:

    "There is also this weird continuous scroll bug that happens in settings
    where every second or so, it scrolls up the page. Very frustrating when
    trying to read things on the page. It does also remind me of some
    improvements I've wanted made to scrolling for a bit - when in the journal
    or somewhere on a page and the page is updating because a run is ongoing,
    just finished and is adding summary text above, or something else that
    changes the dimensions/size of content on the page, it is moving my current
    view around. I instead want it to, if it is adding something outside my
    screen, to not disturb my current view but instead sort of extend the view
    above outside my screen if that makes sense. I want to lock my current views
    in place better when content is being modified."

Two claims, one mechanism. The behavior runs for real under bun
(tests/js/scroll_anchor.mjs) against a stub DOM whose boxes have document
positions and whose rects are those positions minus the scroll - so "content
was inserted above the viewport" is a real arithmetic fact rather than a
mocked one, and the numbers below are the ones the code computes.

Why a stub DOM and not a browser: Chrome implements scroll anchoring in the
engine (`overflow-anchor`), so a headless chromium silently corrects whatever
this code gets wrong and every scene passes with the fix removed. Measured on
the live portal at 1280 and at 500 wide, twenty-four seconds each: the page did
not drift by a pixel either way. WebKit has no such feature, and an iPhone is
where Wes reads this - so on the browser that has the bug, this code IS the
mechanism.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import config

APP_JS = Path(config.APP_ROOT) / "app" / "static" / "app.js"
SCENES = Path(__file__).parent / "js" / "scroll_anchor.mjs"

pytestmark = pytest.mark.skipif(shutil.which("bun") is None, reason="bun not installed")


@pytest.fixture(scope="module")
def scenes():
    out = subprocess.run(
        ["bun", str(SCENES), str(APP_JS)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(out.stdout)


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

def test_content_added_above_the_view_does_not_move_the_reader(scenes):
    """The whole ask, in one number: 300px of banner appears above everything
    on screen, and the scroll moves by exactly 300 so the line being read stays
    on the same row of pixels. Left alone, that text would have slid 300px down
    the screen - "the view extends above" rather than the view moving."""
    s = scenes["growth_above_the_view_does_not_move_the_reader"]
    assert s["scrollY"] == 1300  # was 1000, grew by 300 above the fold


def test_content_added_below_the_view_moves_nothing(scenes):
    """The correction has to be free when nothing visible moved, or every new
    journal entry at the bottom of the page becomes a jolt at the top of it."""
    assert scenes["growth_below_the_view_moves_nothing"]["scrollY"] == 1000


def test_a_second_correction_converges_rather_than_doubling(scenes):
    """Layout is not final when the patch lands - an image the patch brought in
    has no height until it decodes - so the correction runs again on the next
    frame. It measures against the position recorded BEFORE the patch, never
    against its own last move, so running it twice lands in the same place."""
    s = scenes["holding_twice_converges"]
    assert s["once"] == s["twice"] == 1300


def test_the_anchor_is_the_line_in_view_not_the_section_around_it(scenes):
    """This is what changed. It used to hold the topmost element carrying an
    `id`, and an id is a fact about what somebody once needed to link to - on a
    page whose only ids are its sections, the nearest one can be a screenful
    above the text being read, and holding IT still is no help at all when the
    growth happened in between. The walk descends to the leaf."""
    assert scenes["the_anchor_is_a_leaf_not_the_card_around_it"]["chain"] == [
        "para-b",
        "card-two",
    ]


def test_a_replaced_anchor_falls_back_to_its_ancestor(scenes):
    """The anchor is a node reference, which works because the morph reuses the
    nodes it matches. When it genuinely does replace one, the ancestors are
    carried along for exactly this - the card is still there and moved by the
    same 300px the paragraph inside it did."""
    s = scenes["a_replaced_anchor_falls_back_to_its_ancestor"]
    assert s["leaf"] == "para-b"
    assert s["fellBackTo"] == "card-two"
    assert s["scrollY"] == 1400  # 1100 + the 300 that appeared above


def test_a_pinned_element_is_never_the_anchor(scenes):
    """A `position: fixed` header does not move when the document does, so
    anchoring on it reports "nothing moved" for every patch forever - the
    mechanism would be silently switched off rather than visibly broken. It is
    the first thing in the body, so the walk meets it every single time."""
    s = scenes["a_pinned_header_is_never_the_anchor"]
    assert s["anchoredOn"] == "card-one"
    assert s["scrollY"] == 800  # 500 + 300; an anchored header would leave 500


def test_at_the_top_of_the_page_there_is_no_anchor(scenes):
    """At scroll 0 the top of the document is what the reader is looking at,
    and it cannot move. Anchoring there would push the page down every time
    anything grew."""
    assert scenes["at_the_top_there_is_no_anchor"]["anchor"] is None


# --------------------------------------------------------------------------
# The panels that scroll inside themselves
# --------------------------------------------------------------------------

def test_a_scrolling_panel_holds_its_own_place(scenes):
    """The journal is a box with its own scrollbar, so the page-level
    correction cannot see inside it: an entry arriving at its top slides what
    you are reading down the box while the page has not moved at all. Each
    panel gets its own anchor."""
    s = scenes["an_inner_panel_holds_its_own_place"]
    assert s["anchoredOn"] == "entry-2"
    assert s["scrollTop"] == 750  # was 400, an entry of 350 landed above it


def test_a_panel_followed_at_its_end_stays_at_its_end(scenes):
    """A live transcript is the one case where the reader wants to be moved:
    they are watching the bottom, so new output must pull them along. The
    bottom pin beats the anchor."""
    s = scenes["a_panel_pinned_to_its_bottom_stays_pinned"]
    assert s["atBottom"] is True
    assert s["scrollTop"] == 1400


def test_a_panel_that_went_away_does_not_hand_its_place_to_another(scenes):
    """Panels used to be matched by their index in the document, so two patches
    apart - a project whose journal box appeared or vanished - one panel's
    scroll position was applied to a different panel. They are matched by node
    now."""
    assert scenes["a_vanished_panel_does_not_get_another_panels_place"]["boxB"] == 0


# --------------------------------------------------------------------------
# Where the correction sits in the patch
# --------------------------------------------------------------------------

def test_the_correction_runs_after_the_enhancers_not_before():
    """The settings bug, and the one thing the stub DOM cannot show.

    reinit() re-enhances the dropdowns, re-hides the settings panels the user is
    not looking at and re-sizes the textareas - all of which change heights
    above the viewport. Correcting before it ran measured a layout that existed
    for one frame and was then replaced, so the leftover shift stuck and every
    patch added another in the same direction.
    """
    src = APP_JS.read_text()
    body = src.split("function liveRefreshNow")[1].split("function liveReload")[0]
    assert body.index("morphNode(document.body") < body.index("reinit()")
    assert body.index("reinit()") < body.index("holdEverything(anchor")


def test_the_reader_scrolling_beats_the_second_correction():
    """The next-frame pass is a nicety; a person's own scroll is not. If the
    scroll moved between the two, ours is abandoned."""
    src = APP_JS.read_text()
    assert "Math.abs((window.scrollY || 0) - settled) < 2" in src

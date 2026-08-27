"""Typing a note on a phone: the zoom, and the jumping.

Wes, 2026-08-18:

    "adding a note in mobile (in the mobile app as well if that is relevant) is
    frustrating, because 1: it zooms in slightly when selecting a field to type
    into, then you have to manually zoom back out. And 2: at some point when
    typing a paragraph into the add note field, the page starts jumping around
    any time there is an auto-correct suggestion or an auto-correct that just
    happens. Please fix both of these issues. The 2nd issue might start
    happening once the text box is at its maximum allowed height before it
    starts just scrolling further down into the text box rather than continuing
    to expand. Selecting text also becomes a bit buggy at this point it seems."

Two separate faults with the same shape - the browser doing something on its
own that the page never asked it to undo.

The zoom is a CSS number: iOS Safari magnifies the page whenever it focuses a
control whose computed font-size is under 16px, and never magnifies back. The
portal's controls were 0.9rem, which is 14.4px.

The jumping is autosize(). `height: auto` on a <textarea> resolves to the
`rows` attribute, not to the content - so measuring the content by setting it
collapsed a 300px note box to three lines, and the page scroll was clamped to
the document that briefly left behind. Restoring the height did not restore the
scroll. The behavior runs for real under bun (tests/js/note_autosize.mjs)
against a stub DOM whose textarea has a real used-height calculation and whose
window clamps its scroll to the document height on every layout, so "the page
was yanked 222px" is arithmetic rather than a fixture string. The old
measurement is in that file too and runs in the same world, which is where the
before-and-after numbers below come from.

His third symptom - selection going strange - is the same mechanism seen from a
different angle, and is covered here by the write counts rather than by a
selection model: at the cap the code now touches nothing at all, so there is no
layout invalidation left for a selection to be lost to.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import config

APP_JS = Path(config.APP_ROOT) / "app" / "static" / "app.js"
STYLE = Path(config.APP_ROOT) / "app" / "static" / "style.css"
SCENES = Path(__file__).parent / "js" / "note_autosize.mjs"

pytestmark_bun = pytest.mark.skipif(shutil.which("bun") is None, reason="bun not installed")


@pytest.fixture(scope="module")
def scenes():
    out = subprocess.run(
        ["bun", str(SCENES), str(APP_JS)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(out.stdout)


# --------------------------------------------------------------------------
# 1. The zoom
# --------------------------------------------------------------------------

def _coarse_block() -> str:
    """Every @media (pointer: coarse) block, brace-matched out of style.css and
    joined. There is more than one - the todo row's long-press opt-out is keyed
    on touch too - and each lives beside the rules it changes rather than in
    one pile at the bottom, so nothing here may assume it is looking at the
    first or the only one."""
    css = STYLE.read_text()
    found, at = [], 0
    while True:
        at = css.find("@media (pointer: coarse)", at)
        if at < 0:
            break
        depth = 0
        for i in range(css.index("{", at), len(css)):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    found.append(css[at:i + 1])
                    at = i
                    break
        else:
            raise AssertionError("unbalanced braces after @media (pointer: coarse)")
    assert found, "no coarse-pointer block in style.css"
    return "\n".join(found)


def test_a_touch_keyboard_gets_sixteen_pixel_controls():
    """16 is not a taste, it is the threshold: iOS Safari zooms in on focus at
    15.9px and does not at 16. Anything below it and his complaint is back.

    That the rule is inside a pointer query rather than a width one is part of
    this assertion, not a separate test - _coarse_block() collects nothing
    else. A narrow desktop window is not a touch keyboard and keeps the tighter
    type; an iPad in landscape is one and is wider than any phone breakpoint.
    """
    block = _coarse_block()
    assert "font-size: 16px;" in block
    for control in ("textarea", "select", 'input[type="text"]'):
        assert control in block


def test_every_typable_control_is_covered_not_just_the_note_box():
    """He named the note field because that is where he types paragraphs, but
    the zoom fires on any of them - the todo box, a settings field, the ask
    form. A fix for one control would have him pinching back out on the next
    page instead."""
    block = _coarse_block()
    for kind in ("text", "number", "password", "url", "search", "email", "tel"):
        assert 'input[type="%s"]' % kind in block


def test_the_page_keeps_its_pinch_zoom():
    """The other way to stop the zoom is maximum-scale=1 on the viewport, which
    works by taking pinch-zoom away from the entire site. He asked not to be
    zoomed in against his will, not to be stopped from zooming."""
    for tpl in ("base.html", "style_guide.html"):
        meta = (Path(config.APP_ROOT) / "app" / "templates" / tpl).read_text()
        assert "maximum-scale" not in meta
        assert "user-scalable" not in meta


def test_the_taller_type_still_fits_one_control_height():
    """Every control on a row is pinned to --control-h (2.3rem, 36.8px), and
    16px of type inside 0.5rem of padding and a border needs 34px of that 36.8
    before line spacing. The padding comes in with the type or the text clips.
    """
    block = _coarse_block()
    assert "padding-top: 0.3rem;" in block
    assert "padding-bottom: 0.3rem;" in block


# --------------------------------------------------------------------------
# 2. The jumping - what it used to do
# --------------------------------------------------------------------------

@pytestmark_bun
def test_the_old_measurement_yanked_the_page_on_every_keystroke(scenes):
    """The bug, as a number. A note box at its 300px cap, the reader at the
    bottom of the page where the add-note form is. One character arrives - an
    autocorrect on iOS fires an input event for every suggestion it draws - and
    the page ends up 222px from where it was, because `height: auto` briefly
    made the box three rows tall and the scroll was clamped to that shorter
    document. Nothing put it back."""
    s = scenes["the_old_measurement_yanked_the_page"]
    assert s["before"] == 3600
    assert s["after"] == 3378
    assert s["moved"] == 222
    assert s["writes"] == 2  # and both of them for a height that never changed


@pytestmark_bun
def test_the_old_measurement_overshot_a_delete_by_ten_times(scenes):
    """Deleting two lines out of a capped box costs the page 22px of document.
    The old code charged 222 for it, for the same reason: the measurement went
    via three rows and the clamp on the way past stuck."""
    s = scenes["the_old_measurement_overshot_a_delete"]
    assert s["before"] == 3600
    assert s["after"] == 3378
    assert s["height"] == "278px"


# --------------------------------------------------------------------------
# 3. The jumping - what it does now
# --------------------------------------------------------------------------

@pytestmark_bun
def test_at_the_cap_a_keystroke_moves_the_page_not_at_all(scenes):
    """The state his paragraph lives in: box at the cap, content overflowing
    it, the browser scrolling the box natively. There is no height for the code
    to compute here - it cannot grow past the cap and the content still fills
    it - so the whole function is an early return. Zero writes, zero movement.
    """
    s = scenes["at_the_cap_an_edit_moves_nothing"]
    assert s["before"] == s["after"] == 3600
    assert s["writes"] == 0


@pytestmark_bun
def test_at_the_cap_an_autocorrect_swap_moves_the_page_not_at_all(scenes):
    """"teh" becoming "the" is the commonest thing iOS does to a note and the
    one that changes nothing about the layout at all. It used to cost 222px."""
    s = scenes["at_the_cap_a_same_length_autocorrect_moves_nothing"]
    assert s["before"] == s["after"] == 3600
    assert s["writes"] == 0


@pytestmark_bun
def test_a_delete_at_the_cap_costs_the_page_only_what_the_box_lost(scenes):
    """A delete is the one edit that genuinely has to collapse the box to
    measure it, so this is where the hazard survives - and where the scroll is
    saved across the measurement. The box goes 300 -> 278, so the document is
    22px shorter and the page moves 22px. Not 222."""
    s = scenes["a_delete_at_the_cap_moves_the_page_only_by_what_it_lost"]
    assert s["before"] == 3600
    assert s["after"] == 3578
    assert s["height"] == "278px"


@pytestmark_bun
def test_typing_inside_a_line_writes_no_height_at_all(scenes):
    """Below the cap as well as at it: the content fits the box and did not get
    shorter, so nothing about the height can have changed and the function has
    nothing to do. This is most keystrokes."""
    s = scenes["below_the_cap_typing_within_a_line_writes_nothing"]
    assert s["writes"] == 0
    assert s["height"] == "78px"


# --------------------------------------------------------------------------
# 4. It still does the job it was written for
# --------------------------------------------------------------------------

@pytestmark_bun
def test_the_box_still_grows_a_line_at_a_time(scenes):
    """None of the above is worth anything if the box stopped growing. rows=3
    is the floor, so the fourth line is the first that moves it, and each line
    after adds its 20px."""
    s = scenes["below_the_cap_a_new_line_still_grows_the_box"]
    assert s["threeLines"] == "78px"
    assert s["fourLines"] == "98px"
    assert s["fiveLines"] == "118px"


@pytestmark_bun
def test_the_box_still_shrinks_when_text_is_deleted(scenes):
    """Six lines back down to two. The measurement that can only be made by
    collapsing is still made - just on a delete rather than on a keystroke."""
    s = scenes["a_delete_shrinks_the_box"]
    assert s["tall"] == "138px"
    assert s["short"] == "78px"


@pytestmark_bun
def test_the_box_stops_at_its_cap(scenes):
    """4000 characters into a 300px box. The inline height is the cap itself
    rather than the 2000-odd px the content wants - which is what makes "am I
    at the cap?" answerable by comparing the two next time round."""
    s = scenes["the_box_never_grows_past_its_cap"]
    assert s["height"] == "300px"
    assert s["offsetHeight"] == 300


@pytestmark_bun
def test_an_uncapped_textarea_is_not_pinned_to_anything(scenes):
    """heightCap returns 0 for max-height:none, and every clamp in the function
    is guarded on it. A textarea outside the portal's own CSS - or one whose
    cap a theme removes - grows to its content the way it always did."""
    s = scenes["an_uncapped_box_still_grows_to_fit"]
    assert s["cap"] == 0
    assert s["height"] == "318px"


# --------------------------------------------------------------------------
# 5. The wiring
# --------------------------------------------------------------------------

def test_the_input_listener_still_reaches_every_textarea():
    """The whole fix lives inside autosize(), so the delegated listener that
    calls it has to stay exactly as broad as it was."""
    src = APP_JS.read_text()
    assert 'document.addEventListener("input", function (ev) {\n' \
           '  if (ev.target.tagName === "TEXTAREA") autosize(ev.target);' in src

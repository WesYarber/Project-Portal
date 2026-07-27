"""Single-key jumps to a section (n / j / t / p).

Wes, 2026-07-27: "Hitting the 'N' key when no text box is being typed into in a
project page should jump to the 'add note' section and highlight the text box
for it so a note can begin being typed immediately. If on the overall project
dashboard, hitting n should jump to the new ideas section and highlight to start
typing in a title. The page scroll should be set so that the title section is at
the top of the page rather than the bottom or something. Same for the add note
bit. Set the journal section up to be jumped to in project pages by pressing J.
When doing this, the top of the scrollable journal section should be placed
along the top of the browser window. T should do the same with todo, and p with
the top project status/settings stuff."

Two halves, and both are needed:

- The templates declare their own targets with `data-jump`, so these tests pin
  that the four sections on a project page and the one on the dashboard are
  actually marked up. app.js knows no template structure at all.
- The behaviour runs for real under bun (tests/js/jump_keys.mjs) against a stub
  DOM. Matching strings in app.js would only prove it mentions scrollIntoView;
  this proves the section top is what gets aligned, the cursor lands in the box,
  and - the one that matters most - the key does nothing at all while you are
  typing a letter into a field.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import config

STATIC = Path(config.APP_ROOT) / "app" / "static"
TEMPLATES = Path(config.APP_ROOT) / "app" / "templates"


def _template(name: str) -> str:
    return (TEMPLATES / name).read_text()


# --------------------------------------------------------------------------
# The targets the templates declare
# --------------------------------------------------------------------------

def test_project_page_declares_all_four_sections():
    html = _template("project.html")
    for name in ("note", "journal", "todo", "project"):
        assert f'data-jump="{name}"' in html, name


def test_n_on_a_project_page_puts_the_cursor_in_the_note_box():
    # The jump is not "look at where notes go" - Wes asked to be able to start
    # typing, so the note section names the field to focus.
    html = _template("project.html")
    assert 'data-jump="note" data-jump-focus=".note-form textarea[name=\'note\']"' in html
    # ...and that selector has to match the box the note form really uses.
    assert 'class="note-form"' in html
    assert '<textarea name="note"' in html


def test_dashboard_declares_the_idea_section_and_focuses_the_title():
    html = _template("index.html")
    assert 'data-jump="idea" data-jump-focus="#title"' in html
    assert 'id="title" name="title"' in html
    # N is the same key on both pages, so the dashboard must NOT also declare a
    # `note` target - the first match wins and the wrong one would shadow it.
    assert 'data-jump="note"' not in html


def test_the_journal_target_is_the_heading_not_the_scroll_box():
    # Measured in a real chromium at 1280x900 (viewport 757 tall): pressing J
    # puts the <h2> at y=14 and the scroll box's own top edge at y=130, so the
    # heading and the run-count line stay on screen and the box still starts in
    # the first sixth of the window.
    html = _template("project.html")
    assert '<h2 data-jump="journal">Journal</h2>' in html


def test_the_footer_hint_is_rendered_empty_and_hidden():
    # It is filled from the page's own [data-jump] targets, so it can never
    # advertise a key that does nothing on that page.
    html = _template("base.html")
    assert '<span class="jump-hint" hidden></span>' in html


def test_the_hint_is_rewritten_after_a_live_refresh():
    # The morph syncs the footer back to the server's copy of that span, which
    # is empty and hidden - so the hint has to be re-derived in reinit(), or it
    # vanishes the first time a live patch lands.
    js = (STATIC / "app.js").read_text()
    reinit = js.split("function reinit() {", 1)[1].split("\n}", 1)[0]
    assert "jumpHintSync()" in reinit


def test_the_hint_cannot_be_unhidden_by_an_author_display_rule():
    # An author `display` beats the HTML hidden attribute, which is how a
    # "hidden" element ends up on screen. Pinned explicitly.
    css = (STATIC / "style.css").read_text()
    assert ".jump-hint[hidden] { display: none; }" in css


def test_targets_get_scroll_margin_so_they_are_not_flush_to_the_edge():
    css = (STATIC / "style.css").read_text()
    assert "[data-jump] { scroll-margin-top" in css


def test_the_highlight_survives_animations_being_turned_off():
    # body.anim-off kills every animation, so a flash built only as a keyframe
    # would be invisible on Wes's install if he ever turns motion off. The
    # outline is a plain rule; the fade is the decoration on top of it.
    css = (STATIC / "style.css").read_text()
    block = css.split(".jump-flash {", 1)[1].split("}", 1)[0]
    assert "outline: 2px solid" in block
    assert "animation:" in block


def test_the_ring_is_held_before_it_fades():
    # Found by photographing it: the first cut faded from 0% over 1.4s on an
    # ease-out curve, which is ~95% gone a second in - the screenshot taken to
    # check the highlight showed no highlight. It holds to 70% now.
    css = (STATIC / "style.css").read_text()
    assert "0%, 70% { outline-color: var(--ansi-cyan)" in css


# --------------------------------------------------------------------------
# The behaviour, run for real under bun
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def jumps():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "jump_keys.mjs"
    out = subprocess.run(
        [bun, str(harness), str(STATIC / "app.js")],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _scrolled(scene) -> list[str]:
    return [s["name"] for s in scene["scrolls"]]


def _focused(scene) -> list[str]:
    return [f["name"] for f in scene["focuses"]]


def test_n_scrolls_the_note_heading_to_the_top_and_focuses_the_box(jumps):
    scene = jumps["noteOnProject"]
    assert _scrolled(scene) == ["noteHead"]
    # block:start is the ask: the section's top edge at the top of the window,
    # not centred and not merely scrolled into view.
    assert scene["scrolls"][0]["opts"]["block"] == "start"
    assert _focused(scene) == ["noteBox"]
    assert scene["classAdds"] == [["noteBox", "jump-flash"]]


def test_the_focus_is_told_not_to_scroll(jumps):
    # focus() scrolls the field into view by itself, which would land the FIELD
    # near the top and throw away the heading alignment we are here to do.
    assert jumps["noteOnProject"]["focuses"][0]["opts"] == {"preventScroll": True}


def test_shift_is_not_treated_as_a_modifier(jumps):
    # Wes wrote "N" and "J". Shift is how you type a capital, not a chord.
    assert _scrolled(jumps["shiftedNote"]) == ["noteHead"]


def test_j_t_and_p_are_navigation_only(jumps):
    for key, target in (("journal", "journalHead"), ("todo", "todoHead"), ("project", "projectCard")):
        scene = jumps[key]
        assert _scrolled(scene) == [target], key
        assert scene["scrolls"][0]["opts"]["block"] == "start", key
        assert _focused(scene) == [], key


def test_n_on_the_dashboard_finds_the_idea_section_instead(jumps):
    scene = jumps["ideaOnDashboard"]
    assert _scrolled(scene) == ["ideaHead"]
    assert _focused(scene) == ["titleBox"]


def test_a_key_with_no_target_on_this_page_does_nothing(jumps):
    # J on the dashboard. Nothing happens, and the keystroke is NOT swallowed -
    # preventDefault on a key we did not act on would eat browser shortcuts.
    scene = jumps["journalOnDashboard"]
    assert _scrolled(scene) == []
    assert scene["defaultPrevented"] is False
    assert jumps["unmappedKey"]["defaultPrevented"] is False
    assert jumps["arrowKey"]["defaultPrevented"] is False


def test_typing_a_letter_into_a_field_never_jumps(jumps):
    # The bug this guard exists for: typing "not now" into the note box would
    # otherwise fire N, then T, and lose the sentence twice over.
    for name in ("whileTypingInTextarea", "whileTypingInInput"):
        scene = jumps[name]
        assert _scrolled(scene) == [], name
        assert scene["defaultPrevented"] is False, name


def test_modified_keys_stay_the_browsers(jumps):
    # Ctrl+N is a new window, Cmd+P is print.
    for name in ("ctrlN", "metaP"):
        assert _scrolled(jumps[name]) == [], name
        assert jumps[name]["defaultPrevented"] is False, name


def test_the_open_image_viewer_swallows_the_keys(jumps):
    # It is modal: scrolling the page behind it does nothing you can see, and
    # Escape is the documented way out.
    assert _scrolled(jumps["lightboxOpen"]) == []
    # Closed, the same key works - so this is the viewer's state, not a
    # blanket refusal whenever the viewer exists in the DOM.
    assert _scrolled(jumps["lightboxClosed"]) == ["journalHead"]


def test_a_folded_target_is_unfolded_first(jumps):
    scene = jumps["foldedTarget"]
    assert scene["detailsOpen"] is True
    assert _scrolled(scene) == ["todoHead"]


def test_motion_preferences_drop_the_smooth_scroll(jumps):
    # Both the appearance setting (body.anim-off) and the OS-level preference:
    # a long page sliding under you is exactly the motion both of them mean.
    for name in ("animOff", "reducedMotion"):
        assert jumps[name]["scrolls"][0]["opts"]["behavior"] == "auto", name
    assert jumps["journal"]["scrolls"][0]["opts"]["behavior"] == "smooth"


# --------------------------------------------------------------------------
# The bindings are configuration, read off <body data-jump-keys>
#
# Wes, the same morning: "Allow these key commands to be reconfigured in
# settings." The server half - storing, validating and de-duplicating the
# letters - is tests/test_jump_key_settings.py. This is the half that proves
# app.js actually obeys what it is handed.
# --------------------------------------------------------------------------

def test_a_rebound_letter_jumps_and_the_old_one_goes_back_to_being_a_letter(jumps):
    assert _scrolled(jumps["rebound"]) == ["journalHead"]
    assert jumps["rebound"]["defaultPrevented"] is True
    # The default J is now nothing at all - and, crucially, does not swallow
    # the keystroke, so it is still available to the browser.
    assert _scrolled(jumps["reboundOldKeyIsFree"]) == []
    assert jumps["reboundOldKeyIsFree"]["defaultPrevented"] is False


def test_focus_belongs_to_the_target_not_to_the_letter(jumps):
    # N's job is to put the cursor in the box; rebinding it to A must not turn
    # the jump into navigation-only.
    scene = jumps["reboundStillFocuses"]
    assert _scrolled(scene) == ["noteHead"]
    assert _focused(scene) == ["noteBox"]


def test_turning_every_jump_off_is_honoured_rather_than_overruled(jumps):
    # `{}` is a deliberate choice. Falling back to the shipped letters here -
    # the obvious `bindings || DEFAULTS` - would be a switch that turns itself
    # back on, which is the one behaviour this whole feature must not have.
    scene = jumps["allUnbound"]
    assert _scrolled(scene) == []
    assert scene["defaultPrevented"] is False


def test_a_page_with_no_attribute_keeps_the_shipped_letters(jumps):
    # A tab cached from before the setting existed, or a template rendered
    # somewhere else: the keys degrade to their defaults, not to nothing.
    assert _scrolled(jumps["attributeAbsent"]) == ["journalHead"]


def test_an_unreadable_attribute_falls_back_instead_of_throwing(jumps):
    # A throw here would abort the whole script tag, taking every other
    # behaviour in app.js with it - so this is not only about the jumps.
    for name in ("bindingsNotJson", "bindingsNotAnObject"):
        assert _scrolled(jumps[name]) == ["journalHead"], name


def test_an_entry_the_handler_could_not_act_on_is_dropped(jumps):
    # A multi-character key can never match a keydown, and an empty or junk
    # target list would give a letter that swallows the keystroke and goes
    # nowhere - worse than an unbound key, because the browser never sees it.
    assert _scrolled(jumps["bindingsJunkEntriesDropped"]) == ["journalHead"]
    assert jumps["bindingsAllTargetsJunk"]["defaultPrevented"] is False
    assert _scrolled(jumps["bindingsAllTargetsJunk"]) == []

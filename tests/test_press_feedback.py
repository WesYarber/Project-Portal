"""One press, one action - and the press is visible while you wait.

Wes, 2026-08-27:

    "I'm having issues since changing how the page reloads where when I click a
     button to answer a question, add a note, etc, it often hangs a bit before
     completing the task I clicked the button for. There is no feedback that
     anything was done when clicking the button, though, and clicking it again
     multiple times will repeat the action a few times."

Three complaints and one gap behind all of them: between the press and the page
changing, a submitted form looked exactly like an unsubmitted one.

The change he is dating this from is the one that stopped these forms
navigating (2026-08-04, then #560 on 2026-08-07). It did not INVENT the wait -
a navigating POST always took a round trip - but it took away the two things
that used to cover it. A navigation repaints the page when it lands, and a
browser spins something in its chrome while it is in flight. A fetch does
neither, and on the Home Screen install he reads this from there is no chrome to
spin in the first place.

Both of the things he named are one each, so this is fixed on both paths:

- Answering a question is `data-inplace` - fetch, then a second round trip for
  the patch. On top of that the patch could WAIT: refreshBlocked() held it back
  for a text box focused anywhere on the page, and the only thing that drained
  the queue was the 2.5s version poll. Tick a todo half-way through writing a
  note and the press went unanswered for up to a whole poll interval. That is
  the "it often hangs a bit" half, and refreshHeld() is the fix - a patch the
  reader ASKED for by pressing a button does not wait for a sentence in
  progress, though it still waits for the things a patch would destroy rather
  than interrupt (an open menu, a drag, a selection).
- Adding a note is an ordinary navigating form, so nothing here is about fetch
  at all. It gets the same busy mark and the same guard.

Two things are checked, each its own way:

- The BEHAVIOR, driven for real under bun in tests/js/inplace_submit.mjs, with
  the confirm handler, the busy guard, the scroll stash and the in-place poster
  registered in file order - because everything that can go wrong here is about
  which listener sees the press first.
- The LOOK, read out of style.css: the feedback has to survive both
  `body.anim-off` and `prefers-reduced-motion`, each of which kills every
  animation on the page with `!important`.

Both are checked again end to end, in a real browser at phone size, by
scripts/press_feedback_shot.py - which is where the `data-busy` marker came
from. A class was stripped straight back off a press still in flight by the next
live-refresh patch, and only a browser was going to show that.
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
STYLE_CSS = ROOT / "app" / "static" / "style.css"


@pytest.fixture(scope="module")
def ran():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "inplace_submit.mjs"
    out = subprocess.run(
        [bun, str(harness), str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --------------------------------------------------------------------------
# "There is no feedback that anything was done when clicking the button"
# --------------------------------------------------------------------------

def test_the_press_shows_on_the_button_that_was_pressed(ran):
    """And on that one only.

    The note form carries three submit buttons - "add note", "queue note" and
    "add & run now" - so lighting up the form would report three presses for
    one. The pulse follows ev.submitter; the guard is the mark on the form.
    """
    seen = ran["pressIsVisible"]
    assert seen["pressed"] == ["data-busy"]
    assert seen["pressedAria"] == "true"
    assert seen["sibling"] == []
    assert seen["formBusy"] is True


def test_the_button_is_never_disabled(ran):
    """aria-busy, deliberately not `disabled`.

    Disabling a submit button from inside its own submit event is the classic
    way to drop its name/value from the payload the browser is still
    serializing. On the note form that name/value is the whole difference
    between "add & run now" and a note that quietly queues; on a question card
    it is the difference between answering and deleting.
    """
    src = APP_JS.read_text()
    guard = src.split("// --- One press, one action")[1].split("// Ctrl/Cmd+Enter")[0]
    # Comments stripped: the section EXPLAINS the trap at length, so a plain
    # substring test would only ever be reading its own warning back.
    code = "\n".join(
        line for line in guard.splitlines() if not line.lstrip().startswith("//")
    )
    assert "aria-busy" in code
    assert "disabled" not in code
    # ...and the proof it still carries: the pressed button's name and value
    # reach the post, which is what scene 8 of the harness measures.
    assert ran["quickOption"]["body"].endswith("choice=merge+it")


# --------------------------------------------------------------------------
# "clicking it again multiple times will repeat the action a few times"
# --------------------------------------------------------------------------

def test_a_second_press_of_an_in_place_form_posts_nothing(ran):
    """Answering a question twice files the answer twice."""
    again = ran["repeatPressInPlace"]
    assert again["posts"] == 1
    assert again["secondPrevented"] is True
    assert again["thirdPrevented"] is True


def test_the_form_is_usable_again_once_it_has_settled(ran):
    """A todo ticked by mistake has to be tickable back."""
    again = ran["repeatPressInPlace"]
    assert again["busyAfter"] is False
    assert again["markAfter"] == []


def test_a_second_press_of_a_navigating_form_is_swallowed_too(ran):
    """Which is what "add a note" is - no fetch anywhere near it.

    The browser is mid-navigation with no new page to show yet, so nothing but
    this stops the second tap posting a duplicate note.
    """
    nav = ran["repeatPressNavigating"]
    assert nav["firstPrevented"] is False  # the real navigation still happens
    assert nav["secondPrevented"] is True
    assert nav["busy"] is True


def test_the_guard_runs_ahead_of_the_scroll_stash(ran):
    """Why this listener is registered where it is.

    The stash writes the scroll position of a form that is about to navigate.
    A swallowed second press is NOT going to navigate, so a position stashed
    for it is never consumed - and the next ordinary navigation to this page
    would eat it and scroll a page the reader had only just opened. One press,
    one stash.
    """
    assert ran["repeatPressNavigating"]["stashedCount"] == 1


def test_a_browser_that_names_no_submitter_still_guards(ran):
    """Safari before 15.4. There is nothing to pulse, but the guard lives on
    the form, so the half that costs him a duplicate note still holds."""
    old = ran["noSubmitterStillGuards"]
    assert old["busy"] is True
    assert old["secondPrevented"] is True


# --------------------------------------------------------------------------
# Letting go again - every way the press can end
# --------------------------------------------------------------------------

def test_the_back_button_thaws_a_form_that_navigated(ran):
    """A navigating form stays busy until its page goes away, and the way back
    to it is the back button - which restores it from the bfcache exactly as it
    was left, frozen busy, with no load event coming to thaw it. Without
    `pageshow` the note box would be dead until a manual reload."""
    back = ran["backButtonThaws"]
    assert back["frozen"] is True
    assert back["thawed"] is True
    assert back["markCleared"] is True
    assert back["ariaCleared"] is True
    assert back["resubmits"] is True


def test_a_canceled_confirm_leaves_nothing_busy(ran):
    """It never happened. Marking it would leave a dead delete button until the
    page was reloaded - which is exactly why this listener sits BEHIND the
    confirm handler in the file and reads defaultPrevented."""
    canceled = ran["canceledLeavesNothingBusy"]
    assert canceled["busy"] is False
    assert canceled["marked"] == []
    # Not merely "not busy": pressing again and confirming really does delete.
    assert canceled["retryPosts"] == 1


def test_a_refused_post_hands_the_control_back(ran):
    """The route said no (a run in flight, a parent with children). There is no
    morph on that path - the page still shows what it showed - so nothing else
    would ever take the mark off."""
    refused = ran["refusedHandsItBack"]
    assert refused["alerts"] == 1
    assert refused["busy"] is False
    assert refused["marked"] == []


def test_the_mark_outlives_the_post_and_waits_for_the_patch(ran):
    """The window this closes is small and real.

    Between the POST returning and the patch landing, the page still shows the
    old state. A button that looks live over stale text is the double press
    this whole section exists to stop, so the mark comes off after the PATCH.
    That is what postForm returning its promise - and chaining onDone into it
    rather than firing it and forgetting - is for.
    """
    held = ran["markOutlivesThePost"]
    assert held["patchStarted"] == 1
    assert held["busyWhilePatching"] is True
    assert held["busyAfterPatch"] is False


# --------------------------------------------------------------------------
# "it often hangs a bit before completing the task I clicked the button for"
# --------------------------------------------------------------------------

def test_the_patch_answering_a_press_is_forced(ran):
    forced = ran["thePatchIsForced"]
    assert forced["patches"] == 1
    assert forced["forced"] == 1


def test_a_press_does_not_wait_for_a_sentence_somewhere_else(ran):
    """The hang, exactly.

    refreshBlocked() holds a patch back while a text box has focus, and
    releaseFocus() only lets go of a field inside the form that was posted -
    on purpose, since ticking a todo must not close the keyboard over the note
    box. So ticking a todo half-way through writing a note left the press
    unanswered until the 2.5s version poll came round to drain the queue.

    A polled patch still waits. One the reader asked for does not: it cannot
    eat the sentence either way, because preservedAttr refuses to write a
    field's value across a morph.
    """
    waits = ran["whatAPatchWaitsFor"]
    assert waits["typing"] is True
    assert waits["typingForced"] is False
    assert waits["numberField"] is True
    assert waits["numberFieldForced"] is False
    # A press with nothing in the way was never held by either.
    assert waits["idle"] is False
    assert waits["idleForced"] is False
    # A checkbox is not typing - nothing was ever held for it.
    assert waits["checkbox"] is False


def test_a_press_still_waits_for_what_a_patch_would_destroy(ran):
    """The line the force does not cross.

    An open dropdown and a context menu are rebuilt by reinit() and lose their
    open-ness, a drag loses the card in flight, a selection is gone the moment
    its text node is replaced. Being right about which button was pressed is no
    reason to throw away what the reader was holding.
    """
    waits = ran["whatAPatchWaitsFor"]
    for state in ("openWidget", "dragging", "selecting"):
        assert waits[state] is True, state
        assert waits[state + "Forced"] is True, state


def test_a_patch_nobody_asked_for_waits_while_a_press_is_still_in_flight(ran):
    """What keeps an optimistic change from flickering.

    Between the press and the patch, the page is deliberately showing a state
    the server has not confirmed - the banner already folded away, the note
    already in the journal. An unforced background patch renders from a server
    that has not heard about the press yet, so it would put all of that back,
    and the forced patch would take it off again a fraction of a second later.

    The forced patch is the one that press asked for and it must never wait
    here: held, it could never land, and the optimistic state would be
    permanent rather than temporary.
    """
    waits = ran["whatAPatchWaitsFor"]
    assert waits["pressing"] is True
    assert waits["pressingForced"] is False


def test_the_press_hold_lets_go_rather_than_freezing_the_page(ran):
    """Both ways out, because this is the one guard that could wedge a page.

    A fetch that never settles leaves the busy mark on forever. Held on that
    alone, a page would stop refreshing for good - a far worse bug than the
    flicker the hold exists to avoid - so the hold expires and the patch is
    then free to correct whatever the stale optimistic state got wrong.

    And a press that has LANDED holds nothing, even though the timestamp of it
    is still fresh: the mark and the clock are read together. Read apart, every
    page would go dead for ten seconds after any button on it was touched.
    """
    waits = ran["whatAPatchWaitsFor"]
    assert waits["pressingStale"] is False
    assert waits["pressedAndDone"] is False


# --------------------------------------------------------------------------
# The look
# --------------------------------------------------------------------------

def _rule(selector: str) -> str:
    css = STYLE_CSS.read_text()
    at = css.index(selector)
    return css[at : css.index("}", at)]


def test_the_wait_reads_with_every_animation_switched_off():
    """Two signals, and only one of them moves.

    `body.anim-off *` and `@media (prefers-reduced-motion: reduce) *` both kill
    every animation and transition on this page with `!important`. A spinner
    alone would leave a reader with either of those looking at a button that
    still says nothing. So the dim and the lit border carry the message on
    their own, and the sweep is the part that catches the eye in passing.
    """
    static = _rule("button[data-busy], .btn[data-busy]")
    assert "opacity" in static
    assert "border-color: currentColor" in static
    assert "cursor: progress" in static
    # The moving half is a separate rule, so switching it off cannot take the
    # static half with it.
    moving = _rule("button[data-busy]::before")
    assert "animation: busy-sweep" in moving


def test_the_sweep_does_not_take_the_note_buttons_chevron():
    """::before, not ::after.

    At phone width `.attach-row button.go::after` is already the ⌄ that hints
    at the note form's press-and-hold menu - on the very button he presses to
    add a note. Two rules claiming one pseudo-element means the more specific
    one wins and the other silently does nothing.
    """
    css = STYLE_CSS.read_text()
    assert ".attach-row button.go::after" in css  # still there
    assert "button[data-busy]::after" not in css


def test_the_pulse_cannot_move_the_label():
    """Absolutely positioned, so the pseudo-element is not laid out as a flex
    item of the inline-flex button. A row of controls that twitches as the
    state comes and goes is worse than no feedback at all."""
    moving = _rule("button[data-busy]::before")
    assert "position: absolute" in moving


def test_the_marker_is_named_once_in_each_file():
    """The marker is a contract between app.js and style.css, and both ends are
    written by hand. A rename on one side only would leave a guard that works
    and a button that never says so."""
    js = APP_JS.read_text()
    assert 'var BUSY_ATTR = "data-busy"' in js
    css = STYLE_CSS.read_text()
    assert re.search(r"button\[data-busy\]", css)


def test_a_patch_landing_mid_press_does_not_strip_the_mark():
    """Why the marker is an attribute and not a class.

    The live-refresh poller patches this page every 2.5 seconds. The fresh HTML
    it patches from has no busy form in it - the server has no idea a press is
    in flight - so a `class` marker would be synced straight back off a button
    whose POST had not returned yet. Both halves would go with it: the pulse the
    reader is looking at, and the guard that makes their second tap free.

    Found by the browser check in scripts/press_feedback_shot.py, not by
    reasoning about it. The fix reuses a rule the morph already had rather than
    adding a special case: preservedAttr refuses to REMOVE a `data-*` attribute
    that the server did not render, because those are script-set markers it
    knows nothing about.
    """
    src = APP_JS.read_text()
    assert 'var BUSY_ATTR = "data-busy"' in src
    # The rule being leaned on, in the morph, unchanged.
    assert 'if (removing && name.indexOf("data-") === 0) return true;' in src
    # And nothing sets the mark as a class alongside it, which would put the
    # stripping back through the other door.
    guard = src.split("// --- One press, one action")[1].split("// Ctrl/Cmd+Enter")[0]
    code = "\n".join(
        line for line in guard.splitlines() if not line.lstrip().startswith("//")
    )
    assert "classList" not in code

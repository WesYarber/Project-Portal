"""The press-and-hold menu on the note form's green button.

Wes, 2026-08-04: "look at the way the buttons show up currently on a mobile
phone view for adding a note. It is messy. Let's change the 'add note' to be
able to be clicked and held on mobile to reveal a sort of dropdown menu with
the 'add and run now' and 'queue and don't run' options. Then, for recording
audio, add a microphone icon (but not emoji) button to indicate this. All 3
buttons should fit on one line."

That second entry reads "queue note" since his 2026-08-10 note, and the menu is
now built from whichever alternates the server rendered rather than a hardcoded
pair - "add & run now" is gone whenever the green button runs by itself.

The behavior runs for real under bun (tests/js/note_menu.mjs) against the
actual initNoteMenu out of app.js, because everything that can break is
choreography: the hold opening the menu, the ending click being swallowed
rather than plain-submitting the note, and the picks routing through the
hidden alternate buttons so their then=... values ride along. The one-line
phone layout itself is pinned by the CSS assertions below and verified by
screenshot.
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
STYLE = ROOT / "app" / "static" / "style.css"


@pytest.fixture(scope="module")
def ran():
    bun = shutil.which("bun")
    if not bun:  # pragma: no cover - bun is present on the machines that matter
        pytest.skip("bun is not installed")
    proc = subprocess.run(
        [bun, str(Path(__file__).parent / "js" / "note_menu.mjs"), str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_hold_opens_exactly_the_two_alternates(ran):
    assert ran["opened"] == ["add & run now", "queue note"]


def test_the_menu_follows_the_buttons_the_server_rendered(ran):
    """On a project that can be run now the green button runs by itself, so the
    template drops "add & run now" - and the menu, built off the form rather
    than a hardcoded pair, drops it too instead of offering a dead entry."""
    assert ran["runsNowMenu"] == ["queue note"]
    assert ran["runsNowPick"] == {"submittedVia": "queue", "closed": True}


def test_the_click_ending_the_hold_is_swallowed_not_submitted(ran):
    """The long press ends in a click on a SUBMIT button. Unswallowed, holding
    for the menu would also add the note the plain way."""
    assert ran["endClickSwallowed"] is True
    assert ran["menuSurvivedEndClick"] is True


def test_a_later_click_is_left_alone_and_closes_the_menu(ran):
    """The swallow is a 600ms window, not a permanent tax on the button."""
    assert ran["lateClickLeftAlone"] is True
    assert ran["lateClickClosed"] is True


def test_picks_submit_through_the_matching_hidden_button(ran):
    """requestSubmit(alt) carries the alternate's then=... exactly as if it
    had been pressed - one server code path, no duplicated form logic."""
    assert ran["runPick"] == {"submittedVia": "run", "closed": True}
    assert ran["queuePick"] == {"submittedVia": "queue", "closed": True}


def test_a_mouse_never_arms_the_press(ran):
    assert ran["mouseArmed"] is False


def test_drift_and_lift_cancel_the_press(ran):
    assert ran["driftCanceled"] is True
    assert ran["liftCanceled"] is True


# --- the one-line phone row --------------------------------------------------
# Layout facts live in the stylesheet; a stub DOM has no layout, so these pin
# the rules and the screenshot in the journal pins the pixels.

def phone_block() -> str:
    css = STYLE.read_text()
    m = re.search(r"@media \(max-width: 560px\) \{[^@]*?button\[name=\"then\"\][^@]*?\n\}", css)
    assert m, "the phone attach-row block is gone from style.css"
    return m.group(0)


def test_phone_hides_the_alternate_submits_and_right_justifies_the_green():
    block = phone_block()
    assert 'button[name="then"] { display: none; }' in block
    assert "button.go { margin-left: auto; }" in block


def test_phone_gives_the_status_line_its_own_row():
    """A long filename in the picked-files status must wrap below the buttons,
    not push the third button off the line."""
    block = phone_block()
    assert "[data-attach-status]" in block and "flex-basis: 100%" in block

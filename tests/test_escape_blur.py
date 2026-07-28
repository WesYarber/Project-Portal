"""Escape lets go of whatever field the cursor is in.

Wes, 2026-07-28: "hitting escape should de-select whatever text field is
selected."

This is the missing half of the jump keys rather than a separate nicety. N puts
the cursor in the note box on purpose, and the jumps deliberately do nothing
while you are typing - so before this, once you had jumped there was no keyboard
way back out and every letter was text.

Two kinds of test here, because two different things can break:

- The behaviour, run for real under bun (tests/js/escape_blur.mjs) against a
  stub DOM. That is what proves the field is actually let go of.
- The source ORDER, checked here. app.js has six other Escape handlers, each of
  which reads `ev.target` to find the thing it closes, and this one is last on
  purpose so those decide first. Nothing at runtime enforces that; moving the
  block up the file would be silently wrong, so the contract is pinned here.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
APP_JS = STATIC / "app.js"


# --------------------------------------------------------------------------
# The behaviour, run for real under bun
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def esc():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "escape_blur.mjs"
    out = subprocess.run(
        [bun, str(harness), str(APP_JS)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.parametrize(
    "scene,field",
    [
        ("textarea", "noteBox"),
        ("input", "titleBox"),
        ("select", "ownerPick"),
        ("contentEditable", "richText"),
    ],
)
def test_escape_lets_go_of_every_kind_of_field(esc, scene, field):
    assert esc[scene]["blurred"] == [field]


def test_escape_with_nothing_focused_lets_go_of_nothing(esc):
    # This fires on every Escape that closes a menu or a lightbox. Blurring
    # <body> would be harmless but it would also be a lie.
    assert esc["nothingFocused"]["blurred"] == []


def test_a_plain_element_is_not_a_field(esc):
    assert esc["nonField"]["blurred"] == []


@pytest.mark.parametrize("scene", ["otherKeyInField", "enterInField"])
def test_only_escape_lets_go(esc, scene):
    # Without this the note box would empty itself of focus on the first letter.
    assert esc[scene]["blurred"] == []


def test_a_target_without_blur_does_not_throw(esc):
    # This handler runs on every Escape on every page; an exception here would
    # break the menus and the image viewer as collateral damage.
    assert esc["targetWithoutBlur"]["blurred"] == []


def test_escape_is_not_swallowed(esc):
    # Escape has browser-level meanings (stopping a load, dismissing an IME
    # candidate window). Taking it to blur a textarea would be taking more than
    # was asked for.
    assert esc["textarea"]["defaultPrevented"] is False
    assert esc["textarea"]["propagationStopped"] is False


def test_the_round_trip_is_what_makes_the_jump_keys_usable(esc):
    # Fed through the REAL `typingInto` gate from the jump section, so this
    # fails if either half drifts away from the other.
    trip = esc["roundTrip"]
    assert trip["jumpsBlockedWhileFocused"] is True
    assert trip["jumpsWorkAfterBlur"] is False


# --------------------------------------------------------------------------
# The contract that is invisible at runtime: this handler goes last
# --------------------------------------------------------------------------

def test_the_blur_handler_is_the_last_escape_handler_in_the_file():
    src = APP_JS.read_text()
    mine = src.index("function escapeBlurTarget")
    # Every other place in the file that acts on Escape from a keydown. Each
    # reads ev.target to find what it closes; letting go of the field first
    # would not break them, but running last means they decide first and this
    # only ever adds to what they did.
    others = [m.start() for m in re.finditer(r'ev\.key\s*(===|!==)\s*"Escape"', src)]
    assert len(others) >= 5, "expected app.js to still have its other Escape handlers"
    # Exactly one of them is inside this block; every other one is above it.
    below = [o for o in others if o > mine]
    assert len(below) == 1, "an Escape handler was added after the blur handler"


def test_the_lightbox_keeps_priority_via_the_capture_phase():
    # While the image viewer is open, Escape closes the viewer and nothing else
    # - it captures and stops propagation, so the blur handler never runs. That
    # is the right precedence, and it is a one-character `true` away from being
    # lost.
    src = APP_JS.read_text()
    block = src[src.index("// Escape closes the viewer"):]
    block = block[: block.index("\n\n")]
    assert "stopPropagation()" in block
    assert block.rstrip().endswith("}, true);")

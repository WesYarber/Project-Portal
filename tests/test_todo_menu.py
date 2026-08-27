"""The right-click menu on a todo row.

Wes, 2026-08-04: "compress the +tag, whose? and x buttons into a right click
menu for a todo list item. Also, fold the tags into this menu, aside from the
'blocked' tag. Too many tags can be present and restrict the space available
for the todo item itself."

The row's rendering half of this (one chip at most, data attributes for the
menu) is pinned in test_todo_mobile.py and test_todo_people.py. This file runs
the menu itself - the real initTodoMenu out of app.js, under bun against a
stub DOM (tests/js/todo_menu.mjs) - because everything that can break here is
behavior: what the menu builds, what each entry posts, and the long-press
choreography a phone depends on.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"


@pytest.fixture(scope="module")
def ran():
    bun = shutil.which("bun")
    if not bun:  # pragma: no cover - bun is present on the machines that matter
        pytest.skip("bun is not installed")
    proc = subprocess.run(
        [bun, str(Path(__file__).parent / "js" / "todo_menu.mjs"), str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_menu_holds_everything_the_row_used_to(ran):
    """Head, the tags, add-tag, move-to and delete - the whole set of controls
    the row used to wear, in one place."""
    entries = [e["text"] for e in ran["built"]]
    assert entries[0] == "#7 wire the thing up"
    assert "× ui" in entries and "× verify" in entries
    assert "add a tag..." in entries
    assert "delete..." in entries


def test_move_to_skips_where_the_item_already_is(ran):
    """The list's data-here names the current destination; offering it as a
    move is a button that changes nothing you can see."""
    entries = [e["text"] for e in ran["built"]]
    assert "Karli" in entries and "the agent" in entries and "nobody" in entries
    assert "Wes" not in entries  # here="3" is Wes in the fixture


def test_picking_a_tag_removes_it_and_closes_the_menu(ran):
    assert ran["removeTag"]["posted"] == {"action": "/todo/7/tag", "fields": {"remove": "ui"}}
    assert ran["removeTag"]["closed"] is True


def test_move_to_posts_the_person_route(ran):
    assert ran["refile"]["posted"] == {"action": "/todo/7/person", "fields": {"person": "5"}}


def test_delete_asks_first(ran):
    assert ran["deleteCanceled"]["posted"] == 0
    assert ran["deleteConfirmed"]["posted"] == {"action": "/todo/9/delete", "fields": {}}


def test_add_a_tag_swaps_to_an_input_and_enter_posts_it_trimmed(ran):
    assert ran["addTag"]["focused"] is True
    assert ran["addTag"]["posted"] == {"action": "/todo/7/tag", "fields": {"add": "polish"}}
    assert ran["addTag"]["closed"] is True


def test_a_done_row_offers_only_its_tags_and_delete(ran):
    """No add-tag and no move-to on a finished item - the same trimming the old
    inline controls did with `{% if not t.done %}`."""
    entries = [e["text"] for e in ran["doneRow"]]
    assert "add a tag..." not in entries
    assert "the agent" not in entries
    assert "delete..." in entries
    assert "× ui" in entries


def test_escape_closes_the_menu(ran):
    assert ran["escapeClosed"] is True


def test_a_long_press_opens_it_and_survives_its_own_ending_click(ran):
    """A long press ends in a click on the row under the finger. The close-on-
    click-away handler must not read that click as 'clicked away', or the menu
    would shut the instant it opened - but a genuinely later click still
    closes it."""
    assert ran["longPress"]["opened"] is True
    assert ran["longPress"]["survivedItsOwnClick"] is True
    assert ran["longPress"]["laterClickClosed"] is True


def test_drifting_cancels_the_press(ran):
    """Scrolling the list with a finger starts on a row; opening a menu at the
    end of every scroll would make the list unusable."""
    assert ran["drift"]["canceled"] is True
    assert ran["drift"]["timerGone"] is True


def test_a_mouse_never_arms_the_long_press(ran):
    """The mouse has a real right button; holding the left one down is how a
    person hesitates, not how they ask for a menu."""
    assert ran["mouse"]["armed"] is False

"""The number app.js paints on the Home Screen icon.

A web push carries `app_badge` and iOS applies it while the app is shut. That
is the only thing that can reach the icon from outside - but it also means
answering the last question in the browser leaves the old number sitting there
until some unrelated notification happens along. `syncAppBadge` closes that:
every page states its own count on <body>, and this runs off it on load and
after every live patch.

Run for real under bun against a stub navigator (tests/js/app_badge.mjs),
because the interesting part is which of the two Badging API calls it picks and
what it does when there is no Badging API at all - none of which a string match
on the source can see.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"


@pytest.fixture(scope="module")
def badge():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "app_badge.mjs"
    out = subprocess.run(
        [bun, str(harness), str(APP_JS)], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_real_count_sets_the_badge_to_that_number(badge):
    assert badge["three"]["calls"] == [["set", 3]]


def test_zero_clears_the_icon_rather_than_setting_zero(badge):
    """setAppBadge(0) is specified to show a dot with no number on some
    platforms - a mark on the icon with nothing waiting behind it."""
    assert badge["zero"]["calls"] == [["clear", None]]


def test_a_missing_count_leaves_the_icon_alone(badge):
    """Not every page is the portal's own render - an error page, or a
    document mid-morph. Clearing on one would wipe a badge a push had
    legitimately set."""
    assert badge["missing"]["calls"] == []


def test_junk_and_negative_counts_touch_nothing(badge):
    assert badge["junk"]["calls"] == []
    assert badge["negative"]["calls"] == []


def test_a_browser_with_no_badging_api_does_not_throw(badge):
    """Desktop Safari and every non-installed tab. syncAppBadge runs from
    reinit(), so an exception here would take the whole live patch with it -
    the selects, the fold state and the scroll anchor all stop being restored."""
    assert badge["noApi"]["threw"] is None
    assert badge["noApi"]["calls"] == []

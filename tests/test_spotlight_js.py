"""The card a notification was about lights up (app.js spotlightHashTarget).

Run for real under bun against a stub document (tests/js/spotlight.mjs), so
what is asserted is what the function does to the DOM rather than that app.js
contains the words.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "static" / "app.js"
HARNESS = ROOT / "tests" / "js" / "spotlight.mjs"


@pytest.fixture(scope="module")
def outcome():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    out = subprocess.run([bun, str(HARNESS), str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_question_hash_lights_that_card_and_scrolls_to_it(outcome):
    case = outcome["question"]
    assert case["threw"] is None
    assert case["lit"] == ["question-12"]
    assert case["scrolled"] == [["question-12", {"block": "center", "behavior": "auto"}]]


def test_a_hash_naming_nothing_on_the_page_does_nothing(outcome):
    assert outcome["missing"] == {"lit": [], "scrolled": [], "threw": None}
    assert outcome["none"] == {"lit": [], "scrolled": [], "threw": None}


def test_a_section_anchor_is_left_alone(outcome):
    assert outcome["section"] == {"lit": [], "scrolled": [], "threw": None}


def test_a_second_notification_moves_the_light(outcome):
    assert outcome["moves"]["lit"] == ["question-13"]


def test_a_proposal_anchor_lights_too(outcome):
    assert outcome["proposal"]["lit"] == ["proposal-3"]


def test_the_page_wires_it_to_load_and_hash_change():
    src = APP_JS.read_text()
    assert 'document.addEventListener("DOMContentLoaded", spotlightHashTarget)' in src
    assert 'window.addEventListener("hashchange", spotlightHashTarget)' in src

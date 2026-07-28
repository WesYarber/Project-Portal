"""The theme you pick is the theme you get.

Wes, 2026-07-28: "the theme setting is not sticking when I pick it."

The save was never broken. Two separate things were, and the suite could not
see either of them because between them they fall in the gap it had left:

1. **The live patch ate the pick.** Every page re-renders itself in place every
   couple of seconds off `/api/version`. `findMatch` pairs live nodes with
   incoming ones by id first, and `enhanceSelect` had moved the real `<select>`
   *inside* a wrapper it built - so the id the server renders belongs to a
   child, the id branch found nothing and returned before the wrapper fallback
   below it could fire, and the morph deleted the widget and the unsaved pick
   with it. Within 2.5 seconds of picking a theme the dropdown snapped back to
   the saved one, and saving then wrote the value it had snapped back to. That
   is run for real against a stub DOM in tests/js/morph_select.mjs; this file
   drives it and pins the two source rules that harness cannot see.

2. **`ui_theme` was never posted end to end by any test.** test_person_theme.py
   saves the five *other* appearance layers, and test_themes.py sets the row
   directly with `db.set_setting`. Nothing walked a theme from the form to the
   rendered `<body>`, which is exactly the path Wes was reporting on, so the
   last section here does.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, people

APP_JS = config.APP_ROOT / "app" / "static" / "app.js"


# --------------------------------------------------------------------------
# The morph, run for real under bun
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def morph():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "morph_select.mjs"
    out = subprocess.run(
        [bun, str(harness), str(APP_JS)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_an_unsaved_pick_survives_a_live_patch(morph):
    """The whole bug, in one assertion. Before the fix this came back
    "terminal" - the saved value, put back by a patch the user never asked
    for - which is precisely what "not sticking" looked like."""
    assert morph["pickSurvivesAPatch"] == "paper"


def test_the_patch_keeps_the_very_widget_the_user_is_looking_at(morph):
    """Not just the value: the same wrapper node. Replacing it would drop the
    listeners and reset the open menu mid-click even when the value happened to
    come out right."""
    assert morph["widgetWasKept"]
    assert morph["stillWrapped"]


def test_an_id_bearing_select_pairs_with_the_wrapper_holding_it(morph):
    """The exact line that was returning null."""
    assert morph["matchesWrapperById"]


def test_the_fix_does_not_pair_a_select_with_just_any_wrapper(morph):
    """The cheap version of this fix - fall back to any `.sel` sibling - would
    let ui_theme adopt ui_font's widget whenever the two moved past each other,
    which reads as one dropdown changing the other."""
    assert morph["aDifferentIdDoesNotMatch"]


def test_a_reordered_card_is_still_found_by_its_id(morph):
    """What the id branch was written for, and the thing the fix must not cost:
    a card that moved between shelves keeps its node and its listeners."""
    assert morph["reorderedCardStillFoundById"]


def test_a_genuinely_new_option_list_still_rebuilds_the_widget(morph):
    """A preserved widget must not outlive the options it was built from - a
    retired theme would go on being offered by a menu nothing refreshes."""
    assert morph["rebuiltOnANewOptionList"]
    assert morph["pickCarriedAcrossTheRebuild"]


def test_a_pick_the_new_list_no_longer_offers_falls_back(morph):
    """Carrying the value across a rebuild must stop at values that still
    exist, or the control holds something it cannot submit."""
    assert morph["aRetiredPickFallsBack"]


# --------------------------------------------------------------------------
# Two source rules the harness cannot reach
# --------------------------------------------------------------------------

def test_the_preview_reads_its_selects_at_paint_time():
    """`paint` closing over the NodeList it was built with was the second half
    of this bug: once a patch legitimately replaced a select, every later paint
    was reading detached nodes and the page stopped following the dropdown."""
    src = APP_JS.read_text()
    body = src.split("function initAppearancePreview()")[1].split("\ndocument.add")[0]
    assert "appearanceSelects().forEach" in body


def test_the_saved_baseline_is_not_re_read_after_a_patch():
    """The server re-renders the panel from the SAVED value, so re-reading the
    baseline from the live selects after a patch would call an unsaved preview
    clean and drop the "not saved yet" marker while the page still showed it."""
    src = APP_JS.read_text()
    init = src.split("function initAppearancePreview()")[1].split("function paint()")[0]
    assert "appearanceSaved === null" in init


# --------------------------------------------------------------------------
# The save, end to end - the path no test had walked
# --------------------------------------------------------------------------

@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _save_theme(client, theme):
    return client.post(
        "/settings",
        data={"_section": "appearance", "_fields": ",".join(config.APPEARANCE_CHOICES),
              "ui_theme": theme},
        follow_redirects=False,
    )


def test_picking_a_theme_reaches_the_next_page_you_load(client):
    """Form -> person row -> <body> class, which is the round trip Wes was
    describing and the one nothing covered."""
    other = next(t for t in config.APPEARANCE_CHOICES["ui_theme"] if t[0] != "terminal")[0]
    _save_theme(client, other)

    assert people.appearance_of(people.owner())["ui_theme"] == other
    html = client.get("/").text
    start = html.index('<body class="') + len('<body class="')
    assert f"theme-{other}" in html[start:html.index('"', start)].split()


def test_the_settings_page_comes_back_showing_what_you_picked(client):
    """The dropdown itself, not just the page around it: it is the control that
    was snapping back, so it is the control worth asserting on."""
    other = next(t for t in config.APPEARANCE_CHOICES["ui_theme"] if t[0] != "terminal")[0]
    _save_theme(client, other)
    html = client.get("/settings").text
    marker = f'<option value="{other}"'
    assert "selected" in html[html.index(marker):html.index(">", html.index(marker))]


def test_a_theme_that_is_not_on_offer_never_reaches_a_page(client):
    """A hand-posted form cannot leave somebody wearing a theme with no
    stylesheet - which renders as an unstyled page they have no obvious way
    back from. The value is coerced to the default rather than dropped, so what
    is asserted is the outcome (whatever is stored is a real theme) and not the
    mechanism."""
    _save_theme(client, "not-a-theme")
    stored = people.appearance_of(people.owner()).get("ui_theme")
    assert stored in dict(config.APPEARANCE_CHOICES["ui_theme"])
    html = client.get("/").text
    start = html.index('<body class="') + len('<body class="')
    assert f"theme-{stored}" in html[start:html.index('"', start)].split()

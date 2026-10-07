"""The dashboard's project search (Wes, 2026-10-07: "Add a search field for
searching for specific projects on the dashboard.").

The filtering is run for real under bun against a stub dashboard
(tests/js/project_search.mjs), so what is asserted is what it does to the
shelves; the render tests check the page carries the field and the hooks the
script reads.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import db, people

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "static" / "app.js"
HARNESS = ROOT / "tests" / "js" / "project_search.mjs"


@pytest.fixture(scope="module")
def outcome():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    out = subprocess.run([bun, str(HARNESS), str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_an_empty_field_leaves_the_board_alone(outcome):
    case = outcome["empty"]
    assert case["count"] is None and case["none"] is False and case["first"] is None
    assert not any(s["hidden"] for s in case["shelves"].values())
    assert case["shelves"]["paused"]["open"] is True
    assert case["shelves"]["backlog"]["open"] is False
    assert case["shelves"]["paused"]["emptyHidden"] == [False]
    assert case["shelves"]["active"]["cards"] == ["project-portal", "proxytable"]


def test_a_match_in_a_shut_shelf_opens_it_and_hides_the_rest(outcome):
    case = outcome["oneShutShelf"]
    shelves = case["shelves"]
    assert shelves["backlog"] == {
        "hidden": False, "open": True, "was": "0", "cards": ["commander-case"], "emptyHidden": [],
    }
    for name in ("active", "review", "paused", "done"):
        assert shelves[name]["hidden"] is True, name
    assert case["heads"] == {"active": True, "review": True}
    # A shelf with nothing in it says so normally; mid-search that line is noise.
    assert shelves["paused"]["emptyHidden"] == [True]
    assert case["count"] == "1 match"
    assert case["first"] == "commander-case"
    assert case["none"] is False
    # A hidden shelf is shut too, so nothing is left open behind the search.
    assert shelves["done"]["open"] is False


def test_only_the_matching_cards_of_a_shelf_show(outcome):
    assert outcome["partial"]["shelves"]["active"]["cards"] == ["project-portal"]
    assert outcome["partial"]["shelves"]["active"]["hidden"] is False


def test_enter_opens_the_first_match_in_page_order(outcome):
    assert outcome["twoMatches"]["first"] == "project-portal"
    assert outcome["twoMatches"]["count"] == "2 matches"


def test_typing_a_letter_at_a_time_still_clears_back_to_the_board(outcome):
    assert outcome["typedThenCleared"] == outcome["empty"]


def test_an_empty_board_with_no_search_does_not_say_no_match(outcome):
    assert outcome["bareBoard"]["none"] is False


def test_clearing_puts_every_shelf_back_as_it_was(outcome):
    assert outcome["cleared"] == outcome["empty"]


def test_every_word_must_match_in_any_order_and_case(outcome):
    assert outcome["anyOrder"]["shelves"]["backlog"]["cards"] == ["commander-case"]
    assert outcome["anyOrder"]["count"] == "1 match"
    nothing = outcome["allWords"]
    assert nothing["count"] == "0 matches"
    assert nothing["none"] is True
    assert all(s["hidden"] for s in nothing["shelves"].values())
    assert outcome["matches"] == {"words": True, "missing": False, "blank": True}


def test_slug_description_and_parent_are_searched(outcome):
    assert outcome["bySlug"]["first"] == "commander-case"
    assert outcome["byDescription"]["first"] == "proxytable"
    assert outcome["byParent"]["first"] == "kvk-planner"


def test_badge_text_is_not_searched(outcome):
    # Every card says "agent working" somewhere in its markup.
    assert outcome["notBadges"]["count"] == "0 matches"


def test_whitespace_is_not_a_search(outcome):
    assert outcome["spaces"] == outcome["empty"]


def test_a_live_patch_refilters_the_board():
    src = APP_JS.read_text()
    reinit = src[src.index("function reinit()"):]
    reinit = reinit[: reinit.index("\n}\n")]
    assert "projectSearchApply()" in reinit
    assert 'document.addEventListener("DOMContentLoaded", initProjectSearch)' in src


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


@pytest.fixture
def client(temp_data_dir):
    from starlette.testclient import TestClient
    from app import main

    return TestClient(main.app)


def test_the_dashboard_has_the_field_and_the_shelf_hooks(client):
    db.create_project("Card Case", "A deck box", stage="backlog")
    body = client.get("/").text
    assert 'id="project-search"' in body
    assert 'type="search"' in body
    assert 'data-shelf-head="active"' in body
    assert 'data-shelf-head="review"' in body
    # The cards the script reads its words from.
    assert 'data-title="Card Case"' in body


def test_the_no_match_line_points_at_everyone_only_where_that_tab_exists(client):
    body = client.get("/").text
    assert 'id="project-search-none"' in body
    none = body[body.index('id="project-search-none"'):]
    none = none[: none.index("</p>")]
    assert "/everyone" not in none
    people.add("Erin", gender="female", background="Newer to all of this.")
    body = client.get("/").text
    none = body[body.index('id="project-search-none"'):]
    none = none[: none.index("</p>")]
    assert 'href="/everyone"' in none

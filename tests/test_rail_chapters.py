"""The side rail's chapter list, and the sections it can reach.

Wes, 2026-08-01: "I want the option to jump to sections from the side-bar even
if they are not sections I can jump to with a hot-key."

The first cut built the list from `[data-jump]` elements alone, which is the set
the keyboard jumps use. That set is bounded by the number of letters he is
willing to bind - eight - and the portal has far more sections than that, so
whole pages listed nothing at all: /activity has four headings and no
annotations, /settings has eight, /memory has ten. A table of contents built
only from shortcuts is a list of the shortcuts.

So the list is now `[data-jump]` targets AND every plain `<h2>` in the content
body, merged in document order. Two kinds of test:

- The behavior, run for real under bun (tests/js/rail_chapters.mjs) against a
  stub DOM. That is what proves a bare heading becomes a clickable chapter
  wired to the right node.
- The wiring in the templates, checked here.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"
TEMPLATES = ROOT / "app" / "templates"


@pytest.fixture(scope="module")
def built():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "rail_chapters.mjs"
    out = subprocess.run(
        [bun, str(harness), str(APP_JS)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --------------------------------------------------------------------------
# The behavior
# --------------------------------------------------------------------------

def test_a_page_with_no_annotations_at_all_still_gets_its_sections(built):
    # /activity, which is the page Wes was looking at when he asked. Four
    # headings, zero `data-jump` attributes, and before this an empty rail.
    page = built["activity"]

    assert page["hidden"] is False
    assert [c["label"] for c in page["chapters"]] == [
        "Last 14 days", "By project", "Prompt size", "Runs",
    ]


def test_the_page_heading_is_not_a_chapter(built):
    # <h1> is the name of the page you are already on, not a place on it.
    assert "Activity" not in [c["label"] for c in built["activity"]["chapters"]]


def test_a_chapter_with_no_hot_key_is_still_a_link_to_somewhere(built):
    # The whole ask. A heading nobody annotated has no name to look up later,
    # so the anchor has to carry the node itself or the click goes nowhere.
    for chapter in built["activity"]["chapters"]:
        assert chapter["key"] == ""
        assert chapter["jumpTo"] is None
        assert chapter["targetTag"] == "H2"
        assert chapter["target"] == chapter["label"]


def test_annotated_and_bare_sections_come_out_in_document_order(built):
    # Not annotated ones and then the rest: the list has to read down the page
    # the way the page does, or it is not a table of contents.
    assert [c["label"] for c in built["project"]["chapters"]] == [
        "Ask", "Since you last looked", "Overview", "Todo", "Journal",
    ]


def test_a_heading_inside_a_declared_section_is_not_a_second_chapter(built):
    # The project page's overview card declares itself a target; the headings
    # inside it are its own contents, not places to go.
    labels = [c["label"] for c in built["project"]["chapters"]]
    assert "a heading inside the card" not in labels
    assert labels.count("Overview") == 1


def test_a_target_marked_off_the_nav_stays_off_it(built):
    # The journal declares two targets so the J key works on an empty project,
    # but they are one place to go.
    assert "the box" not in [c["label"] for c in built["project"]["chapters"]]


def test_the_bound_letter_still_rides_beside_the_chapters_that_have_one(built):
    # How a person finds out their own keybindings exist without opening
    # settings - and now the contrast that says which sections have one.
    keyed = {c["label"]: c["key"] for c in built["project"]["chapters"]}
    assert keyed["Todo"] == "t"
    assert keyed["Journal"] == "j"
    assert keyed["Ask"] == ""


def test_a_count_inside_a_heading_is_not_part_of_its_name(built):
    # "phone push none enrolled" is a heading plus a live badge. A chapter whose
    # title changes when a device enrolls is not a chapter title.
    assert built["badges"]["chapters"][0]["label"] == "phone push"


def test_a_long_heading_is_cut_on_a_word(built):
    assert built["wordy"]["chapters"][0]["label"] == "What we've learned about…"


def test_one_section_is_a_link_rather_than_a_table_of_contents(built):
    assert built["lonely"]["hidden"] is True
    assert built["lonely"]["chapters"] == []


# --------------------------------------------------------------------------
# Collapsible sections are sections. Wes, 2026-08-01: "add additional click
# points for Agent console, sub projects (only if there are some), and files"
# --------------------------------------------------------------------------

def test_a_collapsible_section_is_a_chapter(built):
    """Agent console, Sub-projects and Files are <details class="fold-section">
    rather than headings, so a list built from `[data-jump], h2` could not see
    any of them - which is why three of the project page's biggest sections
    were unreachable from the rail."""
    labels = [c["label"] for c in built["folds"]["chapters"]]
    assert "Agent console" in labels
    console = next(c for c in built["folds"]["chapters"] if c["label"] == "Agent console")
    assert console["targetTag"] == "DETAILS"


def test_a_folds_chapter_name_is_its_label_not_its_whole_summary(built):
    """A summary is "<label> <a sentence of muted detail>" - "Agent console"
    is the chapter, "last run - 3 minutes ago" is a fact about the page that
    would change under the reader."""
    labels = [c["label"] for c in built["folds"]["chapters"]]
    assert "Agent console" in labels
    assert not any("minutes ago" in label for label in labels)


def test_an_empty_fold_is_not_offered_as_a_place_to_go(built):
    """"only if there are some": the sub-projects fold renders on every
    top-level project because it is also where you create the first child, and
    it opts out of the nav with data-jump-nav="off" until there is one."""
    assert "Sub-projects" not in [c["label"] for c in built["folds"]["chapters"]]


def test_a_heading_inside_a_fold_belongs_to_that_fold(built):
    """"Files" is the chapter; "Uploaded" and "Workspace" are what is in it.

    Both folds, and the SECOND one is the test. A heading inside Files is
    already dropped by the ancestor-target rule one branch earlier, because
    Files declares `data-jump="files"` - so the fold rule is never reached and
    deleting it broke nothing. The delete-the-fix sweep of 2026-08-01 caught
    that; the Agent console fold, which declares no target of its own, is the
    case that actually exercises it.
    """
    # The whole list, not an absence: a label over 28 characters is cut to a
    # word boundary and an ellipsis, so "'a heading somebody put inside the
    # fold' is not in labels" passed whether the rule worked or not. That is how
    # this stayed uncaught through the first re-sweep as well as the first.
    assert [c["label"] for c in built["folds"]["chapters"]] == [
        "Questions", "Agent console", "Files",
    ]


# --------------------------------------------------------------------------
# The dashboard. Wes, 2026-08-01: "When on the dashboard, the left tab bar
# should not show the recent activity entries as individual items as it
# currently does."
# --------------------------------------------------------------------------

def test_headings_inside_rendered_markdown_are_not_chapters(built):
    """Every agent progress entry opens with an `##` heading, and the
    dashboard's activity feed renders a dozen of them - so the chapter list was
    a list of other projects' journal entries rather than of this page."""
    labels = [c["label"] for c in built["dashboard"]["chapters"]]
    assert labels == ["New idea", "Recent activity"]


# --------------------------------------------------------------------------
# The wiring
# --------------------------------------------------------------------------

def test_the_list_is_scoped_to_the_content_body():
    # Not `document`: the rail's own links would become chapters pointing at
    # themselves, and the nav tabs and footer are not sections of the page.
    src = APP_JS.read_text()
    body = src.split("function railChapters()")[1].split("\n}")[0]

    assert 'querySelector(".terminal-body")' in body
    # Three kinds of chapter in one comma'd selector, so the browser hands them
    # back in document order: annotated targets, plain headings, and the folds
    # (Agent console, Sub-projects, Files) Wes asked for click points on.
    assert '"[data-jump], h2, details.fold-section"' in body


def test_a_chapter_on_another_settings_tab_opens_that_tab_first():
    """The settings page is the one place in the portal where a section that
    exists is not on screen: the panels are all in the DOM and JS hides the ones
    whose tab is not open. Seven of the eight chapters /settings lists are
    therefore on a tab you are not looking at, and without this every one of
    them is a link that scrolls to nothing.

    Verified for real against a live throwaway on 2026-08-01 - clicking "phone
    push" from the agent tab switched to notifications, set the hash and landed
    on the heading."""
    src = APP_JS.read_text()
    body = src.split("function jumpTo(el)")[1].split("\n}")[0]

    assert '.settings-panel[hidden]' in body
    assert "subtabsShow" in body
    # Before the scroll, not after: scrolling to an element that is still
    # `hidden` measures nothing and lands nowhere.
    assert body.index("subtabsShow") < body.index("scrollIntoView")


def test_opening_a_panel_from_outside_is_the_same_act_as_clicking_its_tab():
    """One function, so a chapter jump leaves the page in exactly the state a
    tab click would - including the hash, which is where a settings save
    redirects back to."""
    src = APP_JS.read_text()
    init = src.split("function initSubtabs()")[1].split("\ndocument.addEventListener")[0]

    assert "subtabsShow = selectPanel" in init
    assert "selectPanel(t.dataset.panel)" in init
    assert "history.replaceState" in init.split("function selectPanel")[1].split("\n  }")[0]


def test_the_long_pages_now_have_a_table_of_contents():
    # The four pages long enough to need one, and the three that listed nothing
    # before this change. Two headings is the threshold railChapters itself
    # uses, and none of these is close to it - /memory alone has ten.
    #
    # The short pages (/tasks, /questions, a run console) are deliberately not
    # here: one section is a link, not a table of contents, and the rail
    # correctly hides the block on them.
    for name in ("activity.html", "settings.html", "memory.html", "index.html"):
        markup = (TEMPLATES / name).read_text()
        assert markup.count("<h2") >= 2, name

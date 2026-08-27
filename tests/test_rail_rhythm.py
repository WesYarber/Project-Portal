"""The side rail's vertical rhythm: one gap between groups, one kind of line.

Wes, 2026-08-15, over a screenshot of the rail:

  "Look at the inconsistent spacing and line separators here. Please fix this"

The rail had grown four groups - the running-agent widget, the portal's own
sections, this page's chapters, the project list - and each one had arrived in
its own run with its own spacing, so no two boundaries between them looked
alike:

  * above "dashboard", *twice* the gap of anywhere else, because
    `.rail-status` paid a margin-bottom and `#rail-nav` paid a margin-top and
    `.rail-inner` is a flex column, where two margins do not collapse into one;
  * between the nav and THIS PAGE, two lines a single text row apart - the
    nav's own border-bottom and then the heading's underline;
  * above RECENT, no gap at all, because the project list is a plain <div> and
    the shelf inside it is therefore a `:first-child` with its margin zeroed.

The fix is a rhythm rather than three edits, and these tests are that rhythm
written down: every distance in the rail is one of two named variables, the gap
between two groups is always spent by the group *below* (so two of them can
never stack), and the only line the rail draws is a group heading's underline -
which means every group has a heading.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = config.BASE_DIR / "app" / "templates"
STATIC = config.BASE_DIR / "app" / "static"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


# --- reading the stylesheet -------------------------------------------------

def _rail_css() -> str:
    """Just the rail's own section of the sheet.

    Scoped deliberately: `margin-bottom` is a perfectly ordinary thing for the
    rest of the portal to declare, and a test that read the whole file would
    either fail on someone else's card or have to name exceptions forever.
    """
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    return css.split("/* The desktop side rail")[1].split(
        "/* The landing page for an answer button"
    )[0]


def _rules(css: str) -> list[tuple[str, str]]:
    """(selector, declarations) for every rule, including inside a @media.

    A brace walk rather than a regex, because `@media (min-width: 1400px) {`
    nests and a flat `([^{}]+)\\{([^{}]*)\\}` silently returns nothing for the
    two placement blocks - which is exactly where a rail spacing rule is most
    likely to be hiding.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: list[tuple[str, str]] = []
    stack: list[str | None] = []
    buf = ""
    for ch in css:
        if ch == "{":
            selector = buf.strip()
            stack.append(None if selector.startswith("@") else selector)
            buf = ""
        elif ch == "}":
            selector = stack.pop() if stack else None
            if selector is not None:
                rules.append((selector, buf))
            buf = ""
        else:
            buf += ch
    return rules


def _declarations(css: str, selector: str) -> dict[str, str]:
    """Every property a selector sets, across all the rules that name it."""
    found: dict[str, str] = {}
    for sel, body in _rules(css):
        if selector not in [part.strip() for part in sel.split(",")]:
            continue
        for decl in body.split(";"):
            if ":" in decl:
                prop, _, value = decl.partition(":")
                found[prop.strip()] = value.strip()
    return found


def test_the_reader_finds_rules_inside_a_media_query():
    """The parser above is load-bearing for every test in this file - a version
    that quietly returned nothing would make all of them pass."""
    sample = "a { color: red; }\n@media (min-width: 10px) { b { margin-top: 1px; } }"

    assert _declarations(sample, "a") == {"color": "red"}
    assert _declarations(sample, "b") == {"margin-top": "1px"}


# --- one gap between groups, spent once -------------------------------------

def test_the_gap_between_two_groups_is_one_named_distance():
    css = _rail_css()

    assert _declarations(css, ".rail-section")["margin-top"] == "var(--rail-group-gap)"
    assert _declarations(css, ".rail-projects")["margin-top"] == "var(--rail-group-gap)"


def test_the_project_list_is_not_welded_to_the_group_above_it():
    """RECENT sat directly under the last chapter row, with the ~14px every
    other boundary got missing entirely, because `.rail-projects` is a plain
    div: the shelf inside it is a `:first-child`, and `:first-child` is where
    the group gap is deliberately zeroed."""
    css = _rail_css()

    assert ".rail-section:first-child { margin-top: 0; }" in css
    assert "margin-top" in _declarations(css, ".rail-projects")


def test_no_group_in_the_rail_pays_a_gap_below_itself():
    """The doubled space above "dashboard", stated as a rule rather than as the
    one edit that fixed it.

    `.rail-inner` is `display: flex`, so a margin-bottom here and a margin-top
    there do NOT collapse - they add up, and the boundary that has both is
    twice as far apart as the boundaries that have one. So the gap is owned by
    the group below, always, and nothing in the rail declares one underneath
    itself."""
    # A label's own gap down to its first row is the one exception, and it is
    # not an exception to the rule: nothing follows it but the rows it labels,
    # so it never meets another margin to stack with.
    allowed = ("0", "0px", "var(--rail-head-gap)")

    for selector, body in _rules(_rail_css()):
        if not selector.startswith((".rail", "#rail")):
            continue
        for decl in body.split(";"):
            prop, _, value = decl.partition(":")
            if prop.strip() == "margin-bottom":
                assert value.strip() in allowed, (
                    f"{selector} pays a gap below itself: {decl.strip()}"
                )
            # `margin: a b c` sets the bottom too, and would stack the same way.
            if prop.strip() == "margin":
                parts = value.split()
                bottom = parts[2] if len(parts) > 2 else parts[0]
                assert bottom in allowed, f"{selector}: {decl.strip()}"


def test_a_group_label_sits_the_same_distance_above_its_rows_everywhere():
    css = _rail_css()

    assert _declarations(css, ".rail-head")["margin"].endswith("var(--rail-head-gap)")
    # The status widget's bold "N agents working" line is a label too.
    assert _declarations(css, ".rail-runs")["margin"].startswith("var(--rail-head-gap)")


# --- one kind of line -------------------------------------------------------

def test_the_only_line_the_rail_draws_is_a_group_headings_underline():
    """The nav used to close itself with a border-bottom, which put a second
    line one text row above THIS PAGE's underline. Two lines, one job - and a
    reader has to decide what the difference between them means."""
    drawn = [
        selector
        for selector, body in _rules(_rail_css())
        if "border-bottom" in body and "border-bottom: none" not in body
    ]

    assert drawn == [".rail-head"]


def test_every_group_of_links_carries_the_same_heading():
    """PORTAL, THIS PAGE and RECENT: same element, same class, same rule under
    it. The nav was the odd one out - the only group with no label at all."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")

    nav = base.split('id="rail-nav"')[1].split("</nav>")[0]
    assert '<h3 class="rail-head">Portal</h3>' in nav
    # The shelves, server-side; the chapter list, built by app.js.
    assert '<h3 class="rail-head">{{ shelf.label }}' in base
    assert 'head.className = "rail-head"' in app_js


def test_one_row_is_one_row_wherever_it_is():
    """A running-agent row and a project row are the same kind of thing at the
    same indent, so they read as one column rather than two lists that happen
    to be stacked."""
    css = _rail_css()

    assert _declarations(css, ".rail-runs a")["padding"] == "var(--rail-row-pad)"
    assert _declarations(css, ".rail-section a")["padding"] == "var(--rail-row-pad)"
    # The narrow `margin` placement tightens every row by redefining the
    # variable, rather than by overriding one of the two rules above and
    # leaving the other at its wide-rail padding.
    assert _declarations(css, "body.rail-margin")["--rail-row-pad"]


# --- what the page actually renders ----------------------------------------

def test_the_rail_labels_the_portals_own_sections(client, temp_data_dir):
    db.create_project("Working", description="x", stage="active", slug="working")

    for path in ("/", "/project/working", "/settings"):
        nav = client.get(path).text.split('id="rail-nav"')[1].split("</nav>")[0]
        assert '<h3 class="rail-head">Portal</h3>' in nav, path
        # Above the rows it labels, not below them.
        assert nav.index("rail-head") < nav.index("rail-nav-dashboard"), path


def test_the_heading_is_a_label_and_not_another_place_to_go(client, temp_data_dir):
    """It sits in the list of links, so the thing it must not be is a link -
    nor a jump target that steals a key from the rows under it."""
    nav = client.get("/").text.split('id="rail-nav"')[1].split("</nav>")[0]
    head = nav.split("<h3")[1].split("</h3>")[0]

    assert "<a" not in head
    assert "data-jump" not in head

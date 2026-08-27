"""Wes's 2026-07-29 04:45 note, both asks.

- The person picker should REPLACE whatever the page put in the top-right
  corner, not sit beside it. His example was the questions page, which showed
  him the picker and "2 open" crowding the same slot.
- The blinking cursor in the bottom-right corner of every page goes away.

Both are about chrome that appears on every page, so the assertions here sweep
several pages rather than trusting one. The rule that keeps a single-person
install unchanged applies as it does everywhere else in the people work: with
one person there is no picker, so the stat is exactly where it always was.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db, main, people

STATIC = Path(config.APP_ROOT) / "app" / "static"


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def project():
    # Active, not the default backlog: a backlog project's questions are
    # shelved and deliberately left out of the nav badge, and the badge is
    # half of what the questions-page test below is about.
    return db.create_project(title="A project", description="d", stage="active")


@pytest.fixture
def erin():
    return people.add(name="Erin", gender="female", background="newer to this")


def corner(body: str) -> str:
    """Just the top-right slot. Slicing this finely matters twice over: the
    strings involved ("2 open", the slug) also appear in the page content, and
    the picker's own hidden `next` field carries the current path - so a naive
    "the slug is not in the header" passes on the dashboard and fails on every
    project page for a reason that has nothing to do with the stat."""
    start = body.index('class="window-stats"')
    return body[start:body.index("</div>", start)]


def has_stat(body: str) -> bool:
    """A page stat is the one bare <span> in that slot. The picker contributes
    a form, a label and options, and no bare span at all."""
    return "<span>" in corner(body)


def footer(body: str) -> str:
    start = body.index('<div class="terminal-footer">')
    return body[start:body.index("</div>", start)]


# --------------------------------------------------------------------------
# The picker takes the corner
# --------------------------------------------------------------------------

def test_alone_in_the_portal_the_corner_still_shows_the_page_stat(client, project):
    """The single-person install is unchanged: no picker, so nothing replaced."""
    assert f"<span>{config.SITE.handle}</span>" in corner(client.get("/").text)
    assert "runs logged" in corner(client.get("/activity").text)
    assert project["slug"] in corner(client.get(f"/project/{project['slug']}").text)


def test_the_questions_page_shows_the_picker_instead_of_the_open_count(
    client, project, erin
):
    """Wes's own example. The count is still on the tab, which is where he
    reads it; the corner is the picker's."""
    db.create_question(project["id"], "Which way?")
    db.create_question(project["id"], "And this one?")
    body = client.get("/questions").text

    assert 'action="/whoami"' in corner(body)
    assert "2 open" not in corner(body)
    assert not has_stat(body)
    # Not deleted from the portal, just out of the corner: the nav tab still
    # carries the count.
    assert '<span class="nav-count">2</span>' in body


@pytest.mark.parametrize(
    "path_of",
    [
        lambda p: "/",
        lambda p: "/activity",
        lambda p: f"/project/{p['slug']}",
        lambda p: f"/project/{p['slug']}/todos/history",
    ],
)
def test_every_page_that_sets_a_stat_gives_the_corner_up(client, project, erin, path_of):
    body = client.get(path_of(project)).text
    assert 'action="/whoami"' in corner(body)
    assert not has_stat(body)


def test_the_picker_still_names_everybody_once_it_has_the_corner(client, erin):
    """The replacement must not have cost the control anything it did before."""
    client.cookies.set(people.COOKIE, "erin")
    slot = corner(client.get("/").text)
    assert '<option value="erin" selected>Erin</option>' in slot
    assert f">{config.SITE.owner}<" in slot


# --------------------------------------------------------------------------
# The blinking cursor
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/activity", "/questions", "/tasks", "/settings"])
def test_no_page_blinks_a_cursor_in_its_corner(client, project, path):
    assert 'class="cursor"' not in footer(client.get(path).text)


def test_the_project_page_does_not_either(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert 'class="cursor"' not in footer(body)


def test_the_footer_keeps_everything_else(client):
    """The corner emptied, not the footer. The jump hint lives in the same row
    and app.js fills it from the page's own [data-jump] targets."""
    foot = footer(client.get("/").text)
    assert "project portal" in foot
    assert 'class="jump-hint"' in foot


def test_the_cursor_is_still_a_component_the_style_guide_offers(client):
    """Deliberately not deleted. /style documents the terminal theme other
    projects copy (the terminal-style skill ships the same rule), so the swatch
    stays even though no portal page wears one."""
    body = client.get("/style").text
    assert 'class="cursor">blinking cursor' in body, "the swatch is still shown"
    assert 'class="cursor"' not in footer(body), "but the page itself has none"

    css = (STATIC / "style.css").read_text()
    assert re.search(r"^\.cursor::after", css, re.M), "the rule is still shipped"

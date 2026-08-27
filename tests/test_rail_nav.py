"""The portal's own sections, in the rail that does not scroll away.

Wes, 2026-08-15:

  "I want the dashboard, tasks, etc to show up on the nav bar on the side even
  when in project pages."

They were on a project page already - as the tabs across the top of the
content, above a page that runs to several thousand pixels. Reaching /tasks
from the todo list at the bottom of it meant scrolling back to the top first.
The rail is fixed, so the fix is to put the same list in it.

"The same list" is the load-bearing part: `sidebar.nav_rows` is the one
definition of what the sections are, which one is current and what each badge
counts, and base.html draws BOTH the tabs and the rail's copy from it. A second
hand-written copy in a template is exactly how the two would come to disagree
about which tab is lit on /run/12.

Wes, 2026-08-16: "From the side-bar on projects, remove the memory, everyone,
and settings options." So the rail draws FEWER of those rows than the tabs do -
`sidebar.RAIL_OMIT` says which - and the tests below hold both halves of that:
the three are gone from the rail, and every one of them still has a tab.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db, people, sidebar

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "app" / "templates" / "base.html"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


# --- what the list is --------------------------------------------------------

def test_it_lists_the_portals_sections_in_tab_order():
    keys = [r["key"] for r in sidebar.nav_rows("/")]

    assert keys == ["dashboard", "tasks", "questions", "activity", "memory", "settings"]


def test_the_everyone_door_is_the_owners_and_only_when_shared():
    assert "everyone" not in [r["key"] for r in sidebar.nav_rows("/")]
    assert "everyone" in [r["key"] for r in sidebar.nav_rows("/", everyone=True)]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/", "dashboard"),
        ("/tasks", "tasks"),
        ("/tasks/4", "tasks"),
        ("/questions", "questions"),
        ("/activity", "activity"),
        # A run console belongs to the activity log, which is where it is
        # reached from - the tabs have always said so and the rail says the
        # same.
        ("/run/12", "activity"),
        ("/memory", "memory"),
        ("/settings", "settings"),
    ],
)
def test_the_current_section_comes_off_the_path(path, expected):
    current = [r["key"] for r in sidebar.nav_rows(path) if r["current"]]

    assert current == [expected]


def test_a_project_page_lights_no_section_rather_than_all_of_them():
    """Every path in the portal starts with "/", so a prefix test on the
    dashboard's href would light the dashboard on every page in the site."""
    assert [r["key"] for r in sidebar.nav_rows("/project/portal") if r["current"]] == []


def test_the_dashboard_is_current_only_on_the_dashboard_itself():
    assert sidebar.nav_rows("/")[0]["current"] is True
    assert sidebar.nav_rows("/settings")[0]["current"] is False


def test_the_counts_ride_the_rows_they_belong_to():
    rows = {r["key"]: r["count"] for r in sidebar.nav_rows("/", questions=3, tasks=2)}

    assert rows["questions"] == 3
    assert rows["tasks"] == 2
    assert rows["activity"] == 0


# --- one definition, drawn twice ---------------------------------------------

def test_the_template_draws_both_copies_from_the_same_list():
    """Not "a nav exists" - that the tabs and the rail read the same variable.
    A hand-written second copy is how the two would come to disagree."""
    html = BASE.read_text(encoding="utf-8")

    assert "{% set nav = nav_links(path) %}" in html
    # Both loops, and no literal <a href="/tasks"> left anywhere in the chrome.
    assert html.count("{% for item in nav") == 2
    assert 'href="/questions"' not in html


def test_the_rail_draws_its_shorter_list_off_the_same_rows(client, temp_data_dir):
    """The rail filters on the flag the row already carries. A template that
    named the omitted sections itself would be the second list this file
    exists to prevent."""
    html = BASE.read_text(encoding="utf-8")

    assert "{% for item in nav if item.rail %}" in html
    for key in sidebar.RAIL_OMIT:
        assert key not in html.split('id="rail-nav"')[1].split("</nav>")[0]


def test_every_page_carries_the_nav_in_its_rail(client, temp_data_dir):
    db.create_project("Working", description="x", stage="active", slug="working")

    for path in ("/", "/project/working", "/settings", "/activity", "/questions"):
        html = client.get(path).text
        assert 'id="rail-nav"' in html, path
        assert 'id="rail-nav-tasks"' in html, path
        assert 'id="rail-nav-activity"' in html, path


def test_the_rail_marks_the_section_you_are_in(client, temp_data_dir):
    html = client.get("/tasks").text
    row = html.split('id="rail-nav-tasks"')[1].split("</li>")[0]

    assert 'aria-current="page"' in row
    assert 'class="here"' in row.split(">")[0]


# --- the three sections he took off the rail ---------------------------------

def test_the_rail_leaves_out_memory_everyone_and_settings(client, temp_data_dir):
    """Wes, 2026-08-16: "From the side-bar on projects, remove the memory,
    everyone, and settings options.\""""
    db.create_project("Working", description="x", stage="active", slug="working")
    people.add("Karli", gender="female")  # so the everyone row exists at all

    keys = _rail_keys(client, "/project/working")

    assert "memory" not in keys
    assert "everyone" not in keys
    assert "settings" not in keys
    assert keys == ["dashboard", "tasks", "questions", "activity"]


def test_taking_them_off_the_rail_does_not_take_them_off_the_portal(
    client, temp_data_dir
):
    """A shorter rail, not a smaller portal: the tabs across the top of the
    content are where all three were reached from before the rail carried a nav
    at all, and they are still there."""
    db.create_project("Working", description="x", stage="active", slug="working")

    tabs = _tab_keys(client, "/project/working")

    assert "memory" in tabs and "settings" in tabs


def test_nav_rows_says_which_rows_the_rail_draws():
    rows = {r["key"]: r["rail"] for r in sidebar.nav_rows("/", everyone=True)}

    assert rows["dashboard"] is True and rows["questions"] is True
    assert rows["memory"] is False
    assert rows["everyone"] is False
    assert rows["settings"] is False


def test_the_rail_omits_only_sections_that_exist():
    """A key renamed in NAV_LINKS and not here would quietly put settings back
    in the rail, which is the one thing his note asked for."""
    keys = {key for key, _, _, _ in sidebar.NAV_LINKS}

    assert sidebar.RAIL_OMIT <= keys


def test_a_project_page_marks_no_section_in_the_rail(client, temp_data_dir):
    db.create_project("Working", description="x", stage="active", slug="working")

    nav = client.get("/project/working").text.split('id="rail-nav"')[1].split("</nav>")[0]

    assert 'aria-current="page"' not in nav


def test_the_badge_counts_the_same_thing_the_tab_does(client, temp_data_dir):
    project = db.create_project("Working", description="x", stage="active", slug="w")
    db.create_question(project["id"], "Which one?")

    nav = client.get("/project/w").text.split('id="rail-nav"')[1].split("</nav>")[0]

    assert 'id="rail-nav-questions"' in nav
    assert ">1</span>" in nav


def test_the_nav_never_takes_a_number_key_from_the_project_list(client, temp_data_dir):
    """The digits jump to projects (app.js: railDigitTarget) and the letters to
    the page's own chapters. A nav row quietly holding a digit would move a
    shortcut he already has in his fingers."""
    db.create_project("Working", description="x", stage="active", slug="working")

    nav = client.get("/").text.split('id="rail-nav"')[1].split("</nav>")[0]

    assert "data-rail-digit" not in nav


def test_the_nav_survives_a_database_that_cannot_be_counted(client, monkeypatch):
    """Chrome may never be the reason a page 500s - the same rule the rail
    itself follows. A badge that cannot be counted costs the badge."""
    from app import main

    monkeypatch.setattr(main, "open_question_total", lambda: 1 / 0)

    assert [r["key"] for r in main.nav_links("/tasks")]
    assert client.get("/").status_code == 200


def test_the_owners_everyone_tab_appears_once_there_is_somebody_else(
    client, temp_data_dir
):
    assert "everyone" not in _nav_keys(client, "/")

    people.add("Karli", gender="female")

    assert "everyone" in _nav_keys(client, "/")


def test_the_everyone_tab_is_not_offered_to_the_person_who_is_not_the_owner(
    client, temp_data_dir
):
    """It is a door into other people's boards. The portal has no passwords, so
    this is a view filter rather than a lock - but a filter that hands the tab
    to everybody is not even that."""
    her_id = people.add("Karli", gender="female")
    client.cookies.set(people.COOKIE, people.get(her_id)["slug"])

    assert "everyone" not in _nav_keys(client, "/")
    assert "[ everyone ]" not in client.get("/").text


def _nav_keys(client, path):
    """Which sections the reader was offered anywhere on the page.

    The tabs rather than the rail: the rail draws a subset now (RAIL_OMIT), so
    a question about whether somebody is offered a section at all - the everyone
    door, say - has to be asked of the list that carries every section."""
    return _tab_keys(client, path)


def _tab_keys(client, path):
    html = client.get(path).text.split('class="nav-tabs"')[1].split("</nav>")[0]
    return [key for key, _, href, _ in sidebar.NAV_LINKS if f'href="{href}"' in html]


def _rail_keys(client, path):
    """Which sections the rail actually drew, in order, read back off the page."""
    html = client.get(path).text.split('id="rail-nav"')[1].split("</nav>")[0]
    seen = [(html.index(f'id="rail-nav-{key}"'), key)
            for key, _, _, _ in sidebar.NAV_LINKS if f'id="rail-nav-{key}"' in html]
    return [key for _, key in sorted(seen)]


def test_the_nav_is_styled_as_part_of_the_rail():
    css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")

    # Fixed height, like the status widget above it: the project list is what
    # takes the leftover space and scrolls, and a nav that flexed would push it
    # off a short window.
    assert "#rail-nav" in css.split(".rail-projects {")[1].split("/* Wes, 2026-08-06")[0]
    assert ".rail-count-badge" in css


def test_the_section_setting_still_lists_every_tab():
    """The nav table and the jump-key/section machinery are separate lists that
    have to keep naming the same portal - a section added to one and not the
    other is a tab with no way to reach it."""
    hrefs = {href for _, _, href, _ in sidebar.NAV_LINKS}

    assert "/" in hrefs and "/settings" in hrefs
    assert len(hrefs) == len(sidebar.NAV_LINKS)

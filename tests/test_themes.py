"""Themes, and looking at the portal the way somebody else does.

Wes, 2026-07-28: "she doesn't like this kind of terminal, tech-y theme that I
have... it would be cool for her to be able to set her own... Also allow users
to switch themes and view whatever it would look like for another user."

The two halves are tested for different things. A theme is tested for what it
may NOT do - it is CSS somebody chose for themselves, and the failure mode
that matters is a look you cannot get out of. The preview is tested for what
it may not REACH: it changes the pixels and nothing else, and in particular
never who you are.
"""
from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from app import config, db, main, people

THEMES_CSS = config.BASE_DIR / "app" / "static" / "themes.css"


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


def _css() -> str:
    return THEMES_CSS.read_text()


def _declarations(css: str) -> list[str]:
    """Every property name declared in the sheet, comments stripped.

    Deliberately hand-rolled rather than regexed off `\\w+:` in the raw text:
    the file is mostly prose comments explaining why each override exists, and
    those contain colons and property names in sentences.
    """
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [m.group(1).lower() for m in re.finditer(r"(?:^|[{;])\s*([a-zA-Z-]+)\s*:", body)]


# --- what a theme may not do ------------------------------------------------

# Anything that can move a control, hide one, or take it out of the flow. A
# theme that can do these is a theme that can break the portal for the person
# using it, and the one thing worse than a look you dislike is a look you
# cannot get out of - the settings page has to stay reachable in every theme.
BANNED = {
    "display", "visibility", "position", "float", "order", "z-index",
    "top", "right", "bottom", "left", "inset", "clip-path",
    "content-visibility", "pointer-events", "grid-template-columns",
    "grid-template-rows", "flex-direction", "overflow",
}


def test_a_theme_only_changes_how_things_look():
    declared = set(_declarations(_css()))
    assert not (declared & BANNED), sorted(declared & BANNED)


def test_the_theme_sheet_is_entirely_scoped_to_a_theme_class():
    # "terminal" is the shipped look and must be the total absence of anything
    # in this file, so every selector has to name a theme. A bare rule here
    # would change Wes's portal under him.
    body = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    selectors = [s.strip() for chunk in body.split("}") for s in chunk.split("{")[:-1]]
    stray = [s for s in selectors if s and not s.startswith("@") and "theme-" not in s]
    assert stray == [], stray


def test_every_theme_has_a_browser_chrome_color():
    # The <meta> is read before any CSS is applied, so a theme with no entry
    # here flashes the wrong color on every page load - and on iOS leaves the
    # status bar the other theme's shade for as long as the app is open.
    for value, _label in config.APPEARANCE_CHOICES["ui_theme"]:
        assert value in config.THEME_CHROME, value


def test_every_theme_but_the_shipped_one_actually_has_rules():
    # A theme that is only a name in a dropdown is a setting that appears to
    # save and does nothing.
    css = _css()
    for value, _label in config.APPEARANCE_CHOICES["ui_theme"]:
        if value == "terminal":
            continue
        assert f"body.theme-{value}" in css, value


def test_the_font_variable_for_things_that_line_up_is_not_re_pointed():
    # Learned from a screenshot: setting --font-mono to the paper serif is the
    # obvious way to get a proportional title bar, and it renders the
    # dashboard's box-drawing wordmark as a mangled hatch. The variable means
    # "the font for things that have to line up", not "the chrome font".
    body = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    assert "--font-mono:" not in body


# --- the shipped look is untouched ------------------------------------------

def test_a_portal_with_no_overrides_is_on_the_terminal_theme(client):
    assert main.appearance()["ui_theme"] == "terminal"
    assert "theme-terminal" in main.body_classes()
    assert main.theme_chrome() == config.THEME_CHROME["terminal"]


def test_the_dark_meta_still_ships_by_default(client):
    html = client.get("/").text
    assert 'content="dark"' in html
    assert config.THEME_CHROME["terminal"] in html


def test_choosing_paper_reaches_the_page(client):
    person = people.owner()
    people.set_appearance(person["id"], {"ui_theme": "paper"})
    html = client.get("/").text
    assert "theme-paper" in html
    assert 'content="light"' in html
    assert config.THEME_CHROME["paper"] in html


def test_the_theme_sheet_is_linked_after_the_base_one(client):
    # Order is the whole mechanism: a theme is a set of overrides on style.css.
    html = client.get("/").text
    assert html.index("style.css") < html.index("themes.css")


# --- looking at it as somebody else -----------------------------------------

@pytest.fixture
def project():
    return db.create_project("Dice Tower", stage="active", slug="dice-tower")


@pytest.fixture
def erin(client):
    person_id = people.add("Erin", gender="female")
    people.set_appearance(person_id, {"ui_theme": "paper"})
    return people.get(person_id)


def test_previewing_renders_the_other_persons_theme(client, erin):
    assert "theme-paper" not in client.get("/").text

    client.post("/look", data={"person": "erin", "next": "/"})
    html = client.get("/").text
    assert "theme-paper" in html
    assert "Viewing the portal as" in html
    assert "Erin" in html


def test_a_preview_does_not_change_who_you_are(client, erin, project):
    # The one guarantee that matters. Previewing her theme must not make the
    # note you post get her name on it - which is why the preview lives in its
    # own ContextVar rather than swapping the acting person.
    client.post("/look", data={"person": "erin", "next": "/"})
    client.post(f"/project/{project['slug']}/note", data={"note": "still mine"})

    entry = [r for r in db.list_journal(project["id"]) if r["kind"] == "note"][0]
    assert entry["person_id"] == people.owner()["id"]


def test_ending_the_preview_puts_your_own_look_back(client, erin):
    client.post("/look", data={"person": "erin", "next": "/"})
    assert "theme-paper" in client.get("/").text

    client.post("/look", data={"person": "", "next": "/"})
    html = client.get("/").text
    assert "theme-paper" not in html
    assert "Viewing the portal as" not in html


def test_previewing_yourself_is_not_previewing(client, erin):
    # No banner for a no-op, or the banner becomes noise you learn to ignore.
    client.post("/look", data={"person": "wes", "next": "/"})
    assert "Viewing the portal as" not in client.get("/").text


def test_an_unknown_person_ends_the_preview_rather_than_erroring(client, erin):
    client.post("/look", data={"person": "erin", "next": "/"})
    client.post("/look", data={"person": "nobody-by-that-name", "next": "/"})
    assert "theme-paper" not in client.get("/").text


def test_the_preview_cookie_does_not_outlive_the_browser(client, erin):
    # Asymmetric with the identity cookie on purpose: forgetting who you are
    # silently misattributes your notes (ten years), forgetting that you were
    # trying her theme on costs one click.
    resp = client.post("/look", data={"person": "erin"}, follow_redirects=False)
    cookie = resp.headers["set-cookie"]
    assert main.LOOK_COOKIE in cookie
    assert "Max-Age" not in cookie and "Expires" not in cookie


def test_the_settings_panel_still_edits_your_own_theme_while_previewing(client, erin):
    # A panel whose dropdown said "terminal" while the line under it said "you
    # are on the paper theme" is a page arguing with itself. It saves against
    # you, so it must describe you.
    client.post("/look", data={"person": "erin", "next": "/settings"})
    html = client.get("/settings").text
    assert "theme-paper" in html          # the page is rendered in her look...
    assert "Each layer is independent" in html   # ...but the panel describes yours
    assert "no effect while you are on the paper theme" not in html


def test_no_preview_control_on_a_one_person_portal(client):
    # A picker with one option is not a control.
    assert "See it as someone else does" not in client.get("/settings").text


def test_the_preview_control_appears_once_there_is_somebody_else(client, erin):
    html = client.get("/settings").text
    assert "See it as someone else does" in html
    assert 'value="erin"' in html

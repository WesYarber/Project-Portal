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


# --------------------------------------------------------------------------
# More themes, and the stock they print on
#
# Wes, 2026-07-28: "Generate some additional themes that would be cool as
# options and when one is chosen in from the drop-down in settings, instantly
# change that page to preview that theme that it was changed to."
#
# The point of the stock split is that adding a theme is a palette and nothing
# else. These are the checks that keep it that way: if a new theme has to write
# structure, one of them fails and says which structure it forgot.
# --------------------------------------------------------------------------

def _light_themes() -> list[str]:
    return [t for t, stock in config.THEME_STOCK.items() if stock == "light"]


def test_every_theme_says_which_stock_it_prints_on():
    """A theme missing from the table would silently fall to `dark`, and a
    light theme rendered on the dark stock is the actual bug this caught in a
    browser: meadow's colors wearing the CRT's clothes - a scanlined console,
    bracketed tabs and a hatched footer."""
    for value, _label in config.APPEARANCE_CHOICES["ui_theme"]:
        assert value in config.THEME_STOCK, value
        assert config.THEME_STOCK[value] in ("light", "dark"), value


def test_the_light_stock_carries_the_structure_not_the_themes():
    """Everything a light theme has to undo is written once. If this list ever
    appears under a theme's own name instead, the next light theme ships with
    scanlines on it."""
    css = _css()
    for marker in (
        "body.theme-stock-light .terminal-header",   # the chrome re-faced
        "body.theme-stock-light.scan-all::before",   # the CRT killed
        "body.theme-stock-light .badge::before",     # the borrowed punctuation
        "body.theme-stock-light .heat-day",          # the 7px squares
    ):
        assert marker in css, marker


def test_every_light_theme_defines_what_the_light_stock_reads():
    """The stock's rules are written against variables so they know nothing
    about which light theme is on screen - which means a theme that forgets one
    renders that rule with no value at all. In CSS that is not an error: it is
    a transparent button or an invisible heatmap square, and nothing says so.
    """
    body = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    stock_block = body.split("THE PALETTES")[0]
    needed = {
        v for v in re.findall(r"var\((--(?:stock|heat)-[a-z0-9-]+)\)", stock_block)
    }
    assert needed, "the stock stopped using variables; this test is now vacuous"
    for theme in _light_themes():
        block = body.split(f"body.theme-{theme} {{", 1)[1].split("\n}", 1)[0]
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", block))
        assert not (needed - declared), (theme, sorted(needed - declared))


def test_a_dark_theme_needs_no_structure_at_all():
    """The dark stock is style.css exactly as shipped, so a dark theme is
    variables plus the handful of CRT literals style.css spells out. If one
    ever needs a font-family or a border-radius, the split has gone wrong."""
    body = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    for theme, stock in config.THEME_STOCK.items():
        if stock != "dark" or theme == "terminal":
            continue
        for chunk in body.split("}"):
            if f"body.theme-{theme}" not in chunk or "{" not in chunk:
                continue
            decls = chunk.split("{")[-1]
            for banned in ("font-family", "border-radius", "letter-spacing", "content"):
                assert banned not in decls, (theme, banned, decls.strip()[:80])


def test_the_terminal_theme_is_still_untouched_by_all_of_this():
    """The one thing every new theme must not do. `terminal` is the absence of
    any rule, so a portal with no override renders the exact bytes it always
    did."""
    body = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    selectors = [s.strip() for chunk in body.split("}") for s in chunk.split("{")[:-1]]
    for sel in selectors:
        assert "theme-terminal" not in sel or sel.startswith("html:has"), sel


def test_the_new_themes_are_actually_offered(client):
    """A theme in the sheet that is not in the dropdown is dead CSS."""
    offered = {v for v, _ in config.APPEARANCE_CHOICES["ui_theme"]}
    assert {"midnight", "amber", "meadow"} <= offered
    html = client.get("/settings").text
    for value in offered:
        assert f'value="{value}"' in html, value


def test_a_light_theme_gets_the_light_color_scheme(client):
    """`color-scheme` is what paints the scrollbars, the checkbox glyphs and the
    native select popup - none of which any body-scoped stylesheet reaches. It
    used to be decided by `!= 'paper'`, which would have left every future light
    theme with black scrollbars and no visible way to notice."""
    for theme in _light_themes():
        db.set_setting("ui_theme", theme)
        html = client.get("/").text
        assert 'content="light"' in html, theme
        assert "theme-stock-light" in html, theme
    db.set_setting("ui_theme", "midnight")
    html = client.get("/").text
    assert 'content="dark"' in html
    assert "theme-stock-light" not in html


# --------------------------------------------------------------------------
# Trying one on before you save it
# --------------------------------------------------------------------------

APP_JS = config.BASE_DIR / "app" / "static" / "app.js"


def test_the_settings_page_hands_the_preview_everything_it_needs(client):
    """The dropdown carries the body-class prefix and the page carries the
    chrome and stock tables - all three read from the same Python dicts the
    server renders <body> from, so a new appearance setting or a new theme
    previews without a second copy of the table to keep in step."""
    html = client.get("/settings").text
    assert 'data-appearance-prefix="theme"' in html
    assert 'data-appearance-prefix="scan"' in html
    assert "data-theme-chrome=" in html
    assert "data-theme-stock=" in html
    for theme in config.THEME_CHROME:
        assert config.THEME_CHROME[theme] in html, theme


def test_the_preview_swaps_the_stock_class_as_well_as_the_theme():
    """Caught in a real browser and not by any test: swapping only the theme
    class previewed meadow's palette on the CRT's structure. The stock class
    has to move with it."""
    src = APP_JS.read_text()
    assert 'classList.toggle("theme-stock-light", scheme === "light")' in src


def test_the_preview_survives_a_live_patch():
    """The page patches itself every couple of seconds while a run is going,
    and the morph resets <body>'s class to the server's render - which would
    snap an unsaved preview back and read as the dropdown not working."""
    src = APP_JS.read_text()
    reinit = src.split("function reinit()")[1].split("\n}")[0]
    assert "appearanceApply" in reinit


def test_the_preview_says_it_is_not_saved(client):
    """A page that changes its entire look on a dropdown and says nothing has
    just told you it saved."""
    assert "previewing" in client.get("/settings").text
    assert ".appearance-previewing .appearance-unsaved" in (
        (config.BASE_DIR / "app" / "static" / "style.css").read_text()
    )


def test_the_sample_strip_shows_what_a_settings_page_has_none_of(client):
    """Wes: "It could even be good to show a preview of other elements that
    might not show up on the settings page that would want to be previewed."

    Real markup with the real classes, so it cannot drift from the rest of the
    app - only the words in it are made up."""
    html = client.get("/settings").text
    sample = html.split('class="theme-sample"')[1].split("</div>\n\n")[0]
    for cls in ("badge", "todo-tag", "journal-entry", "console-out", "heat-day",
                "quick-option"):
        assert cls in sample, cls

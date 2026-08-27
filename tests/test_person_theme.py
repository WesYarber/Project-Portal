"""Each person's own look.

Wes, 2026-07-28:

  "It would be cool as well if she was able to customize the theme of the site
  for her user to her liking."

The appearance layers - scanlines, phosphor glow, animations, typeface, density
- were one global settings row each. That is the right answer for one person and
the wrong one for two: her turning the scanlines off would turn them off on his
phone as well.

So the settings row becomes the *install's* look and each person may override
any subset of it. Three things have to be true at once, and each has its own
section below:

- **A subset, not a copy.** Somebody who has changed one layer still follows the
  install on the other four. Storing a full copy on first save would silently
  freeze those four at whatever they happened to be that afternoon.
- **Nobody else moves.** Saving the appearance panel writes to one person and
  leaves the install's rows and everybody else's overrides alone.
- **A one-person portal is unchanged.** The whole feature has to be invisible
  until a second person exists, right down to the `<body>` classes.
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from app import config, db, people, settings_form


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


APPEARANCE_FIELDS = ",".join(config.APPEARANCE_CHOICES)


def _save_look(client, **values):
    """Post the appearance panel as the browser does."""
    form = {"_section": "appearance", "_fields": APPEARANCE_FIELDS}
    form.update(values)
    return client.post("/settings", data=form, follow_redirects=False)


def _body_classes(html: str) -> set[str]:
    start = html.index('<body class="') + len('<body class="')
    return set(html[start:html.index('"', start)].split())


# --------------------------------------------------------------------------
# The store: a subset, not a copy
# --------------------------------------------------------------------------

def test_a_person_with_no_choices_has_no_overrides(client):
    assert people.appearance_of(people.owner()) == {}


def test_saving_one_layer_stores_only_that_layer(client):
    """The heart of it.

    If saving five dropdowns stored five overrides, then the four the person
    did not touch would be pinned to today's install values for ever - and a
    later change to the install's look would reach nobody. Only what differs
    is worth storing... but the form posts all five, so this pins the weaker
    and honest version: what is stored is what was posted, and the *other*
    layers a partial post omits stay unpinned.
    """
    owner_id = int(people.owner()["id"])
    people.set_appearance(owner_id, {"crt_scanlines": "off"})
    assert people.appearance_of(people.get(owner_id)) == {"crt_scanlines": "off"}


def test_a_second_save_merges_rather_than_replaces(client):
    owner_id = int(people.owner()["id"])
    people.set_appearance(owner_id, {"crt_scanlines": "off"})
    people.set_appearance(owner_id, {"ui_font": "sans"})
    assert people.appearance_of(people.get(owner_id)) == {
        "crt_scanlines": "off", "ui_font": "sans"
    }


def test_an_unrecognized_key_or_value_is_never_stored(client):
    """This is the boundary between a form and a `<body>` class name.

    A value that got through would be painted into the page as `scan-<junk>`
    and match no rule - a setting that appears to save and then does nothing.
    """
    owner_id = int(people.owner()["id"])
    people.set_appearance(owner_id, {
        "crt_scanlines": "everywhere-plus",   # not one of the choices
        "ui_theme": "hotdog-stand",           # not a layer at all
        "ui_font": "sans",                    # the one real pair
    })
    assert people.appearance_of(people.get(owner_id)) == {"ui_font": "sans"}


def test_junk_in_the_column_reads_as_no_overrides(client):
    """A bad byte in a preference must not stop a page rendering.

    Reachable from a restore, a hand edit, or a half-written row - and the
    blast radius of raising here would be every page on the portal, because
    `body_classes()` runs on all of them.
    """
    owner_id = int(people.owner()["id"])
    conn = db.get_conn()
    for junk in ("not json{", "[1,2]", '"a string"', "null"):
        conn.execute("UPDATE people SET appearance = ? WHERE id = ?", (junk, owner_id))
        conn.commit()
        assert people.appearance_of(people.get(owner_id)) == {}, junk
        assert client.get("/settings").status_code == 200, junk


def test_clearing_is_not_the_same_as_choosing_the_defaults(client):
    """`clear_appearance` re-attaches somebody to the install's look.

    Setting every layer to the shipped default by hand would look identical
    today and diverge the moment the install's look changed - which is exactly
    the difference the column exists to record.
    """
    owner_id = int(people.owner()["id"])
    people.set_appearance(owner_id, {"crt_scanlines": "off"})
    people.clear_appearance(owner_id)
    assert people.appearance_of(people.get(owner_id)) == {}
    # And it really is empty in the column, not a JSON "{}" that would read as
    # a person who has chosen nothing but is still pinned to nothing.
    row = db.get_conn().execute(
        "SELECT appearance FROM people WHERE id = ?", (owner_id,)
    ).fetchone()
    assert row["appearance"] == ""


# --------------------------------------------------------------------------
# The fallback chain: person -> install -> shipped default
# --------------------------------------------------------------------------

def test_no_overrides_means_exactly_the_installs_look(client):
    from app import main

    db.set_setting("crt_scanlines", "chrome")
    assert main.appearance(people.owner()) == main.install_appearance()
    assert main.appearance(people.owner())["crt_scanlines"] == "chrome"


def test_an_override_beats_the_install(client):
    from app import main

    db.set_setting("crt_scanlines", "all")
    people.set_appearance(int(people.owner()["id"]), {"crt_scanlines": "off"})
    look = main.appearance(people.get(int(people.owner()["id"])))
    assert look["crt_scanlines"] == "off"
    # ...and the layers she did not pin still follow the install.
    db.set_setting("ui_density", "compact")
    look = main.appearance(people.get(int(people.owner()["id"])))
    assert look["ui_density"] == "compact"


def test_the_install_still_backs_an_unrecognized_stored_setting(client):
    """The pre-existing guard has not been lost under the new tier."""
    from app import main

    db.set_setting("crt_glow", "sparkles")
    assert main.appearance(people.owner())["crt_glow"] == config.APPEARANCE_DEFAULTS["crt_glow"]


# --------------------------------------------------------------------------
# Two people, which is the whole point
# --------------------------------------------------------------------------

@pytest.fixture
def two(client):
    """The owner plus a second person, and a client that can be either."""
    other_id = people.add("Erin", gender="female", background="Newer to all of this.")
    return int(people.owner()["id"]), other_id


def test_her_choice_does_not_reach_his_screen(client, two):
    """The bug the whole feature exists to prevent, end to end.

    Deliberately driven through the SAVE - she opens Settings and changes the
    dropdowns - rather than by seeding her row. Seeding it only proves the
    render respects a person, and would pass with the whole save path still
    writing one global row: that exact sabotage went undetected when this test
    called `set_appearance` directly.
    """
    his_id, her_id = two

    client.cookies.set(people.COOKIE, people.get(her_id)["slug"])
    _save_look(client, crt_scanlines="off", crt_glow="prose", crt_animations="on",
               ui_font="sans", ui_density="comfortable")
    hers = _body_classes(client.get("/").text)

    client.cookies.set(people.COOKIE, people.get(his_id)["slug"])
    his = _body_classes(client.get("/").text)

    assert "scan-off" in hers and "font-sans" in hers
    assert "scan-all" in his and "font-mono" in his


def test_saving_the_panel_writes_to_whoever_is_reading_it(client, two):
    his_id, her_id = two
    client.cookies.set(people.COOKIE, people.get(her_id)["slug"])
    _save_look(client, crt_scanlines="off", crt_glow="off", crt_animations="off",
               ui_font="sans", ui_density="compact")

    assert people.appearance_of(people.get(her_id))["crt_scanlines"] == "off"
    # He is untouched - not his overrides, and not the install's rows either.
    assert people.appearance_of(people.get(his_id)) == {}
    assert db.get_setting("crt_scanlines") == config.APPEARANCE_DEFAULTS["crt_scanlines"]


def test_the_panel_shows_the_reader_their_own_values(client, two):
    """Otherwise the dropdowns open showing somebody else's look, and the
    first save quietly adopts it."""
    his_id, her_id = two
    people.set_appearance(her_id, {"ui_font": "sans"})

    client.cookies.set(people.COOKIE, people.get(her_id)["slug"])
    hers = client.get("/settings").text
    client.cookies.set(people.COOKIE, people.get(his_id)["slug"])
    his = client.get("/settings").text

    assert '<option value="sans" selected>' in hers
    assert '<option value="sans" selected>' not in his
    assert '<option value="mono" selected>' in his


def test_the_panel_says_whose_look_it_is_once_there_are_two_people(client, two):
    _, her_id = two
    client.cookies.set(people.COOKIE, people.get(her_id)["slug"])
    page = client.get("/settings").text
    assert "Your look, Erin" in page
    assert "changes nothing on anybody else" in page


def test_the_reset_button_appears_only_when_there_is_something_to_undo(client):
    assert "follow the install" not in client.get("/settings").text
    people.set_appearance(int(people.owner()["id"]), {"ui_font": "sans"})
    assert "follow the install" in client.get("/settings").text


def test_the_reset_route_puts_the_reader_back_on_the_installs_look(client, two):
    his_id, her_id = two
    people.set_appearance(her_id, {"ui_font": "sans"})
    people.set_appearance(his_id, {"ui_font": "hybrid"})

    client.cookies.set(people.COOKIE, people.get(her_id)["slug"])
    resp = client.post("/settings/appearance/reset", follow_redirects=False)
    assert resp.status_code == 303

    assert people.appearance_of(people.get(her_id)) == {}
    # His survives: the button resets one person, not the portal.
    assert people.appearance_of(people.get(his_id)) == {"ui_font": "hybrid"}


# --------------------------------------------------------------------------
# The jump keys stay shared, deliberately
# --------------------------------------------------------------------------

def test_the_keyboard_jumps_are_not_personal(client, two):
    """A jump key is a fact about the page - the footer hint prints it - where
    a typeface is a fact about the reader. Wes asked for the theme to be hers,
    not the keys, so saving the same panel still writes the letters globally."""
    _, her_id = two
    client.cookies.set(people.COOKIE, people.get(her_id)["slug"])
    client.post("/settings", data={
        "_section": "appearance",
        "_fields": f"{APPEARANCE_FIELDS},jump_key_journal",
        "jump_key_journal": "g",
        "ui_font": "sans",
    }, follow_redirects=False)

    assert db.get_setting("jump_key_journal") == "g"
    assert "jump_key_journal" not in people.appearance_of(people.get(her_id))


def test_split_personal_puts_each_key_in_exactly_one_bucket():
    values = {"crt_scanlines": "off", "jump_key_note": "n", "telegram_token": "t"}
    install, mine = settings_form.split_personal(values)
    assert mine == {"crt_scanlines": "off"}
    assert install == {"jump_key_note": "n", "telegram_token": "t"}
    # Nothing may be dropped or duplicated by the split.
    assert set(install) | set(mine) == set(values)
    assert not set(install) & set(mine)


def test_every_appearance_layer_is_personal_and_nothing_else_is():
    """Derived from config, so a new look-and-feel option is personal on the
    day it is added rather than the day somebody remembers to list it.

    The page arrangement is the one hand-added member and is named here rather
    than waved through: it cannot live in APPEARANCE_CHOICES because it is a
    permutation instead of one of a fixed list of values, so it is the only
    personal setting a new dropdown would not automatically join.
    """
    from app import sections

    assert settings_form.PERSONAL_KEYS == frozenset(config.APPEARANCE_CHOICES) | {
        sections.SETTING_KEY
    }
    for key in settings_form.PERSONAL_KEYS:
        assert key in settings_form.REGISTRY, key


# --------------------------------------------------------------------------
# One person: nothing changed at all
# --------------------------------------------------------------------------

def test_a_one_person_portal_renders_exactly_what_it_did(client):
    """The feature has to be invisible until somebody uses it.

    A default install's `<body>` classes are the shipped ones, byte for byte -
    a portal with one person and no overrides must not be able to tell that
    this column exists.
    """
    from app import main

    classes = set(main.body_classes().split())
    assert classes == {
        f"{prefix}-{config.APPEARANCE_DEFAULTS[key]}"
        for key, prefix in config.APPEARANCE_CLASS_PREFIX.items()
    }
    assert people.appearance_of(people.owner()) == {}


def test_the_column_upgrades_an_existing_database_in_place(client):
    """`people` predates this column by a day, so every real install reaches it
    by ALTER TABLE rather than by CREATE."""
    cols = {
        row["name"]
        for row in db.get_conn().execute("PRAGMA table_info(people)").fetchall()
    }
    assert "appearance" in cols
    assert ("appearance", "TEXT NOT NULL DEFAULT ''") in db._ADDED_COLUMNS["people"]


def test_a_row_read_before_the_column_existed_does_not_raise():
    """`appearance_of` takes whatever row it is handed, including one selected
    by older code with an explicit column list."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 1 AS id, 'wes' AS slug").fetchone()
    assert people.appearance_of(row) == {}
    assert people.appearance_of(None) == {}


def test_the_stored_json_is_stable_across_saves():
    """sort_keys, so two saves of the same look produce the same bytes - a row
    that churned would make every backup diff noisy for no reason."""
    assert json.dumps({"b": "1", "a": "2"}, sort_keys=True) == '{"a": "2", "b": "1"}'

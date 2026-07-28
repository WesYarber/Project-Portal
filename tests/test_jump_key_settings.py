"""The jump keys become configuration.

Wes, 2026-07-27: "Allow these key commands to be reconfigured in settings."

The letters n / j / t / p shipped the same morning as constants in app.js. This
is the half that makes them settings, and there are three separate things that
can be wrong with a feature like that:

- The stored value. Blank means "no key" and must survive a round trip - the
  obvious `settings.get(key) or default` reads it as "never set" and silently
  puts the letter back, which is a switch that turns itself on again.
- Two sections claiming the same letter. app.js gets one map, so a duplicate
  resolves to whichever entry the serialiser wrote last: a coin toss nobody can
  see. The loser is unbound instead, and the settings row says so.
- The page telling the truth. The settings row, the `<body>` attribute and the
  footer hint all have to agree with what the browser will actually do.

The behaviour of the keys themselves - that a rebound letter jumps and the old
one goes back to being a letter - runs for real under bun in
tests/test_jump_keys.py, against the same app.js the portal serves.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db, jumpkeys, settings_form

TEMPLATES = Path(config.APP_ROOT) / "app" / "templates"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _rendered_bindings(page: str) -> dict:
    """The `data-jump-keys` map as a browser would see it.

    Jinja autoescapes the attribute, so the JSON's own double quotes arrive as
    `&#34;` - which a browser decodes on the way into getAttribute, and a test
    has to decode too. Unescaping here rather than turning autoescape off is
    the point: the escaping is what keeps the attribute from being terminated
    by its own contents.
    """
    match = re.search(r"data-jump-keys='([^']*)'", page)
    assert match, "no data-jump-keys attribute on the page"
    return json.loads(htmllib.unescape(match.group(1)))


def _appearance_form(**overrides: str) -> dict[str, str]:
    """A save of the appearance panel, as the browser posts it.

    The panel declares its own fields, so a save that omits a checkbox means
    "off" - which is exactly why the jump fields have to be listed in `_fields`
    for their blanks to be honoured at all.
    """
    form = {
        "_section": "appearance",
        "_fields": ",".join(
            ["crt_scanlines", "crt_glow", "crt_animations", "ui_font", "ui_density",
             "show_priority", *jumpkeys.SETTING_KEYS]
        ),
        "show_priority": "1",
    }
    form.update({key: value for key, value in overrides.items()})
    return form


# --------------------------------------------------------------------------
# The module itself: pure functions of a settings mapping
# --------------------------------------------------------------------------

def test_the_shipped_letters_are_the_defaults():
    assert jumpkeys.configured({}) == {
        "note": "n", "ask": "a", "journal": "j", "todo": "t", "project": "p"
    }
    assert jumpkeys.bindings({}) == {
        "n": ["note", "idea"],
        "a": ["ask"],
        # The scrolling box first, its heading second - Wes asked for the box's
        # own top edge at the top of the window, and the heading is the
        # fallback for a project whose journal is empty (no box is rendered).
        "j": ["journal-box", "journal"],
        "t": ["todo"],
        "p": ["project"],
    }


def test_every_action_is_seeded_into_the_default_settings():
    # Otherwise a fresh install starts with rows missing rather than with the
    # keys Wes asked for, and the settings page renders four blank boxes.
    for key in jumpkeys.SETTING_KEYS:
        assert config.DEFAULT_SETTINGS[key] == jumpkeys.clean(config.DEFAULT_SETTINGS[key])
    assert config.DEFAULT_SETTINGS["jump_key_journal"] == "j"


def test_a_blank_setting_means_off_and_is_not_read_as_unset():
    """The bug this file exists to prevent.

    `settings.get(k) or default` cannot tell "" from missing, so turning a jump
    off would last exactly until the next page render.
    """
    assert jumpkeys.configured({"jump_key_journal": ""})["journal"] == ""
    assert "j" not in jumpkeys.bindings({"jump_key_journal": ""})
    # A missing row genuinely is unset, and does take the default.
    assert jumpkeys.configured({})["journal"] == "j"


def test_a_rebound_letter_replaces_the_old_one_entirely():
    binds = jumpkeys.bindings({"jump_key_journal": "g"})
    assert binds["g"] == ["journal-box", "journal"]
    assert "j" not in binds


def test_clean_refuses_anything_that_could_never_match_a_keypress():
    for bad in ("", None, "  ", "ab", "1", "/", "shift", "N N"):
        assert jumpkeys.clean(bad) == jumpkeys.OFF, bad
    # An uppercase letter is what you get if you type it with shift held; it is
    # the same key, so it is stored as the letter the handler matches on.
    assert jumpkeys.clean("N") == "n"
    assert jumpkeys.clean(" g ") == "g"


def test_two_sections_cannot_hold_the_same_letter():
    # `note` comes first in ACTIONS, so it keeps j and `journal` is unbound -
    # rather than app.js being handed two entries for one key.
    chosen = jumpkeys.configured({"jump_key_note": "j"})
    assert chosen["note"] == "j"
    assert chosen["journal"] == ""
    assert jumpkeys.bindings({"jump_key_note": "j"})["j"] == ["note", "idea"]


def test_the_conflict_rule_does_not_depend_on_submission_order():
    a = jumpkeys.deconflict({"journal": "x", "todo": "x", "note": "x"})
    b = jumpkeys.deconflict({"todo": "x", "note": "x", "journal": "x"})
    assert a == b == {"note": "x", "journal": "", "todo": ""}


def test_a_hand_edited_database_still_yields_an_unambiguous_map():
    # `configured` runs the conflict pass on the READ path too, so a duplicate
    # written straight into the settings table by hand cannot reach app.js.
    binds = jumpkeys.bindings({"jump_key_todo": "j", "jump_key_journal": "j"})
    assert binds["j"] == ["journal-box", "journal"]
    assert list(binds.values()).count(["todo"]) == 0


def test_the_json_is_exactly_what_app_js_expects():
    parsed = json.loads(jumpkeys.bindings_json({}))
    assert parsed == jumpkeys.bindings({})
    for key, targets in parsed.items():
        assert len(key) == 1
        assert targets and all(isinstance(name, str) and name for name in targets)


def test_every_action_names_a_target_some_template_declares():
    """A settings row for a section no page has is a key that does nothing."""
    html = "".join(
        (TEMPLATES / name).read_text() for name in ("project.html", "index.html")
    )
    for action in jumpkeys.ACTIONS:
        assert any(f'data-jump="{name}"' in html for name in action.targets), action.name


# --------------------------------------------------------------------------
# Saving from the settings page
# --------------------------------------------------------------------------

def test_the_appearance_panel_declares_every_jump_field(client):
    """Checked on the RENDERED page, because the template names no section.

    If a field is missing from `_fields`, `apply` never looks at it - and
    clearing a letter would post a blank the server quietly ignores, which is
    the failure mode that looks like the setting not saving at all.
    """
    page = client.get("/settings").text
    declared = set()
    for value in re.findall(r'name="_fields" value="([^"]*)"', page):
        declared.update(value.split(","))
    for key in jumpkeys.SETTING_KEYS:
        assert key in declared, key
        assert key in settings_form.REGISTRY, key


def test_saving_a_new_letter_sticks(client):
    resp = client.post("/settings", data=_appearance_form(jump_key_journal="g"),
                       follow_redirects=False)
    assert resp.status_code == 303
    assert db.get_setting("jump_key_journal") == "g"
    assert json.loads(jumpkeys.bindings_json(db.get_all_settings()))["g"] == ["journal-box", "journal"]


def test_saving_a_blank_unbinds_and_stays_unbound(client):
    client.post("/settings", data=_appearance_form(jump_key_todo=""),
                follow_redirects=False)
    assert db.get_setting("jump_key_todo") == ""
    page = client.get("/settings").text
    assert "no key - nothing jumps here" in page
    # And the page render did not quietly restore the default underneath it.
    assert db.get_setting("jump_key_todo") == ""


def test_a_letter_the_browser_could_never_send_is_stored_as_off(client):
    client.post("/settings", data=_appearance_form(jump_key_todo="!!"),
                follow_redirects=False)
    assert db.get_setting("jump_key_todo") == ""


def test_a_duplicate_letter_is_resolved_at_save_time(client):
    """Both halves matter: the winner keeps it AND the loser is written blank.

    Leaving the loser's old letter in the database would give a settings page
    showing a binding the browser has just been told to ignore.
    """
    client.post("/settings", data=_appearance_form(jump_key_note="j", jump_key_journal="j"),
                follow_redirects=False)
    assert db.get_setting("jump_key_note") == "j"
    assert db.get_setting("jump_key_journal") == ""


def test_a_partial_save_cannot_unbind_a_field_it_did_not_submit(temp_data_dir):
    # `apply` is given one section's fields at a time, so the conflict pass has
    # to consider only what was actually posted.
    out = settings_form.apply({"jump_key_journal": "g"}, declared="jump_key_journal")
    assert out == {"jump_key_journal": "g"}


def test_saving_the_page_untouched_changes_nothing(client):
    before = {key: db.get_setting(key) for key in jumpkeys.SETTING_KEYS}
    current = {key: (before[key] or "") for key in jumpkeys.SETTING_KEYS}
    client.post("/settings", data=_appearance_form(**current), follow_redirects=False)
    assert {key: db.get_setting(key) for key in jumpkeys.SETTING_KEYS} == before


# --------------------------------------------------------------------------
# What the pages actually carry
# --------------------------------------------------------------------------

def test_every_page_carries_the_bindings_for_the_first_keystroke(client):
    """Rendered, not fetched: a page that learned its keys from a round trip
    would drop the N you pressed while it was still asking."""
    for path in ("/", "/settings"):
        assert _rendered_bindings(client.get(path).text) == \
            jumpkeys.bindings(db.get_all_settings()), path


def test_the_attribute_follows_the_setting(client):
    client.post("/settings", data=_appearance_form(jump_key_journal="g", jump_key_todo=""),
                follow_redirects=False)
    binds = _rendered_bindings(client.get("/").text)
    assert binds["g"] == ["journal-box", "journal"]
    assert "j" not in binds
    assert "t" not in binds


def test_the_attribute_survives_its_own_quotes(client):
    """The value is JSON, so it is full of double quotes.

    Two things have to hold at once and neither is obvious: the attribute is
    single-quoted in the template, and Jinja escapes the contents anyway. Either
    alone would be fine today; together they mean no future edit that drops one
    of them silently truncates the map at its first quote.
    """
    base = (TEMPLATES / "base.html").read_text()
    assert "data-jump-keys='{{ jump_keys_json() }}'" in base
    assert '"' in jumpkeys.bindings_json({})
    raw = re.search(r"data-jump-keys='([^']*)'", client.get("/").text).group(1)
    assert '"' not in raw  # escaped, so it cannot terminate the attribute
    assert json.loads(htmllib.unescape(raw)) == jumpkeys.bindings(db.get_all_settings())


def test_the_settings_page_shows_a_row_per_action_with_its_current_key(client):
    html = client.get("/settings").text
    for action in jumpkeys.ACTIONS:
        assert jumpkeys.setting_key(action.name) in html, action.name
        assert action.label in html, action.name
    assert "j jumps here" in html


def test_the_settings_rows_are_derived_not_listed(client):
    """The template must not name the four sections itself, or adding a fifth
    would render everywhere except the page that configures it."""
    html = (TEMPLATES / "settings.html").read_text()
    assert "for row in jump_keys" in html
    for action in jumpkeys.ACTIONS:
        assert f'name="{jumpkeys.setting_key(action.name)}"' not in html, action.name


def test_a_settings_read_failure_costs_the_keys_their_config_not_their_existence(
    monkeypatch, temp_data_dir
):
    from app import main

    monkeypatch.setattr(main.db, "get_all_settings", lambda: (_ for _ in ()).throw(RuntimeError))
    assert json.loads(main.jump_keys_json()) == jumpkeys.bindings({})


# --------------------------------------------------------------------------
# The letter box has to be the size of a letter
#
# The first cut of this shipped `.jump-key-input { width: 3.2rem }` and it did
# nothing at all: the shared `input[type="text"] { width: 100% }` is an
# attribute selector, which outweighs a bare class, so every binding rendered
# as a full-width box with one character floating in the middle of it. A DOM
# test cannot see that - the test shims apply no CSS - so this pins the cascade
# itself, the same way tests/test_todo_mobile.py pins the mobile row.
# --------------------------------------------------------------------------

STYLE = (Path(config.APP_ROOT) / "app" / "static" / "style.css").read_text()


def _rules(css: str) -> list[tuple[str, str]]:
    """(selector, declarations) for every rule, `@media` blocks included.

    A depth counter rather than a `selector { ... }` regex, which silently
    matches the INNER rule of a nested block and hands back the outer half as
    a selector nobody wrote.
    """
    out: list[tuple[str, str]] = []
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    depth, buf, selector = 0, [], ""
    i = 0
    while i < len(css):
        ch = css[i]
        if ch == "{":
            depth += 1
            if depth == 1:
                selector = "".join(buf).strip()
                buf = []
            elif depth == 2:
                buf = []
            else:
                buf.append(ch)
        elif ch == "}":
            depth -= 1
            if depth == 0:
                if not selector.startswith("@"):
                    out.append((selector, "".join(buf)))
                buf = []
            elif depth == 1:
                # A rule inside @media: its selector is whatever we collected.
                inner = "".join(buf)
                cut = inner.rfind("{")
                if cut >= 0:
                    out.append((inner[:cut].split("}")[-1].strip(), inner[cut + 1:]))
                buf = []
            else:
                buf.append(ch)
        else:
            buf.append(ch)
        i += 1
    return out


def _specificity(selector: str) -> tuple[int, int, int]:
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+", selector)) + len(re.findall(r"\[[^\]]+\]", selector))
    elements = len(re.findall(r"(?:^|[\s>+~])([a-zA-Z][\w-]*)", selector))
    return (ids, classes, elements)


def _matches_the_letter_box(selector: str) -> bool:
    """Would this simple selector match `<input type="text" class="jump-key-input">`?"""
    selector = selector.strip()
    if " " in selector or ">" in selector or "," in selector or ":" in selector:
        return False
    if selector in ('input[type="text"]', "input.jump-key-input", ".jump-key-input", "input"):
        return True
    return False


def test_the_letter_box_is_actually_narrow_once_the_cascade_has_run():
    winner = None
    for order, (selector, decls) in enumerate(_rules(STYLE)):
        for part in selector.split(","):
            if not _matches_the_letter_box(part):
                continue
            if not re.search(r"(^|[;{\s])width\s*:", decls):
                continue
            key = (_specificity(part.strip()), order)
            if winner is None or key > winner[0]:
                winner = (key, part.strip(), decls)
    assert winner, "nothing in style.css sets a width on the letter box"
    selector, decls = winner[1], winner[2]
    assert "jump-key" in selector, (
        f"{selector!r} wins the cascade, so the letter box renders at its width "
        "- the jump rule needs to be at least as specific and come after it"
    )
    width = re.search(r"(^|[;{\s])width\s*:\s*([^;]+)", decls).group(2).strip()
    assert width not in ("100%", "auto"), width


def test_the_letter_box_is_not_stretched_back_out_by_its_flex_cell():
    """`.field-grid .field` is a flex column, and a flex item with no
    `align-self` stretches to the cell's width - which would undo the rule
    above without changing it."""
    for selector, decls in _rules(STYLE):
        if "jump-key-input" in selector:
            assert "align-self" in decls, selector
            return
    raise AssertionError("no .jump-key-input rule at all")

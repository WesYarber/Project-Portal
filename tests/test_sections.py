"""A person arranges the project page for themselves.

Wes, 2026-07-28, on what a theme should let Karli do: "all of the functional
pieces are still there, but she can change how they appear, where they appear,
how they look." Themes did the looking; `app/sections.py` does the "where".

The rules that actually carry risk, and which each get a test below:

- **Nothing may ever go missing.** The order is stored as a list of names in a
  free-text preference. A truncated, hand-edited or stale value must still
  render all nine blocks - a page that silently drops its todo list because a
  string got clipped is data loss wearing a layout bug's clothes.
- **A section shipped later must land somewhere findable.** Every order saved
  before a new block exists is missing its name. Appending is the obvious
  repair and the wrong one: it would bury the new block below the journal for
  everybody who has ever saved an arrangement.
- **The default is stored as nothing.** Dragging a section back where it
  started has to re-attach you to the shipped page, not pin you to today's
  copy of it - the same distinction `clear_appearance` draws for the theme.
- **The page renders in real markup order**, so the rail's chapter list, the
  tab sequence and a screen reader all agree with what is on screen.

The template half is asserted against the real rendered page, because the whole
feature is "the markup comes out in a different order" and a unit test of the
ordering function cannot see whether the template dispatched on it.
"""
from __future__ import annotations

import json
import re

import pytest
from starlette.testclient import TestClient

from app import config, db, people, sections, settings_form


def _journal_first() -> list[str]:
    """A full permutation with the journal pulled to the top - the shape the
    settings form actually posts, and the rearrangement most likely to be
    wanted (the journal is what you open a project to read)."""
    return ["journal"] + [n for n in sections.DEFAULT_ORDER if n != "journal"]


# --- the ordering rules, with no database and no page -----------------------


def test_default_order_is_the_page_as_written():
    """An install where nobody has touched this renders what it always did."""
    assert sections.order("") == list(sections.DEFAULT_ORDER)
    assert sections.order(None) == list(sections.DEFAULT_ORDER)
    assert sections.DEFAULT_ORDER[0] == "ask"
    assert sections.DEFAULT_ORDER[-1] == "journal"


def test_every_section_survives_a_junk_value():
    """The load-bearing invariant: whatever is stored, all nine come back once.

    A preference that has been truncated by a bad restore, hand-edited, or
    written by a newer version of the portal must not be able to take a block
    off the page.
    """
    for raw in ("", "journal", "nonsense,todo,nonsense", "todo,todo,todo", ",,,", "a" * 500):
        resolved = sections.order(raw)
        assert sorted(resolved) == sorted(sections.DEFAULT_ORDER), raw
        assert len(resolved) == len(set(resolved)), raw


def test_a_full_stored_order_is_honored_exactly():
    """What the settings form actually posts: all nine names, in order."""
    wanted = ["journal", "todo", "note", "questions", "files", "project", "console", "subprojects", "ask"]
    assert sections.order(",".join(wanted)) == wanted


def test_a_partial_value_keeps_relative_order_not_position():
    """The rule for a value that names only some sections, spelled out because
    it is the one people guess wrong: the names present keep their order
    relative to each other, and everything absent goes back to its default
    neighborhood. So this is "journal above todo", not "journal at the top"."""
    resolved = sections.order("journal,todo")
    assert resolved.index("journal") < resolved.index("todo")
    assert resolved[0] == "ask"


def test_unknown_names_are_dropped_not_rendered():
    """`{{ SECTIONS[name]() }}` would raise on a name with no macro, so an
    unrecognized token has to die here rather than at render time."""
    assert "evil" not in sections.order("evil,journal")


def test_a_new_section_lands_beside_its_neighbor_not_at_the_bottom():
    """The upgrade path. An order saved before a section existed omits it.

    Simulated by deleting a name from the middle of a full order: `todo`
    shipped between `questions` and `note`, so restoring it must put it back
    between them - not after the journal, where nobody would find it.
    """
    without_todo = [n for n in sections.DEFAULT_ORDER if n != "todo"]
    restored = sections.order(",".join(without_todo))
    assert restored == list(sections.DEFAULT_ORDER)
    assert restored[-1] != "todo"


def test_a_new_first_section_lands_at_the_front():
    """No preceding sibling to anchor to is the edge case that would otherwise
    index off the start of the list."""
    without_first = [n for n in sections.DEFAULT_ORDER if n != "ask"]
    assert sections.order(",".join(without_first))[0] == "ask"


def test_restoring_a_missing_section_keeps_the_change_the_person_made():
    """Someone who pulled the journal to the top and then upgraded keeps that,
    and still gets the section that did not exist when they saved."""
    stored = "journal,ask,project,console,subprojects,questions,note,files"
    resolved = sections.order(stored)
    assert resolved[0] == "journal"
    assert "todo" in resolved
    assert sorted(resolved) == sorted(sections.DEFAULT_ORDER)


def test_the_default_arrangement_is_stored_as_nothing():
    """So a later change to the page's own order still reaches this person."""
    assert sections.clean(",".join(sections.DEFAULT_ORDER)) == ""
    assert sections.clean("") == ""
    assert sections.is_default("journal,ask") is False


def test_clean_round_trips_a_real_arrangement():
    moved = sections.clean("journal,todo")
    assert sections.order(moved) == sections.order("journal,todo")
    assert sections.clean(moved) == moved


# --- nudging one section ----------------------------------------------------


def test_move_swaps_with_the_neighbor():
    after = sections.move("", "project", -1)
    assert sections.order(after)[:2] == ["project", "ask"]


def test_move_down_swaps_the_other_way():
    after = sections.move("", "ask", 1)
    assert sections.order(after)[:2] == ["project", "ask"]


def test_moving_off_either_end_does_nothing_rather_than_wrapping():
    """A "down" on the last section must not teleport it to the top. A control
    that jumps the journal from the bottom of the page to the very top when you
    press it once too often reads as a bug, not as a wrap."""
    assert sections.order(sections.move("", "journal", 1)) == list(sections.DEFAULT_ORDER)
    assert sections.order(sections.move("", "ask", -1)) == list(sections.DEFAULT_ORDER)


def test_moving_an_unknown_section_is_a_no_op():
    assert sections.order(sections.move("", "nope", 1)) == list(sections.DEFAULT_ORDER)


def test_a_move_and_its_undo_return_to_following_the_page():
    once = sections.move("", "journal", -1)
    assert once != ""
    assert sections.move(once, "journal", 1) == ""


def test_describe_names_what_moved():
    assert sections.describe("") == "the shipped arrangement"
    described = sections.describe(sections.move("", "journal", -1))
    assert "Journal" in described


def test_describe_names_the_one_section_that_was_moved():
    """Pulling the journal to the top shifts the other eight down by one, and
    the first version of this listed all nine - a wall of nouns for one move.
    A section displaced by a single place is collateral, not a decision."""
    described = sections.describe(",".join(_journal_first()))
    assert described == "Journal"


def test_describe_falls_back_when_nothing_moved_far():
    """Two neighbors swapped displaces each by exactly one, so the "more than
    one place" rule finds nothing and must not report an empty line."""
    described = sections.describe(sections.move("", "ask", 1))
    assert "Ask project" in described and "Overview" in described


def test_describe_stays_one_line():
    """A reversed page is a real thing somebody could do, and the honest
    summary of it is a count rather than nine labels."""
    described = sections.describe(",".join(reversed(sections.DEFAULT_ORDER)))
    assert described.count(",") <= 2
    assert "more" in described


# --- the settings field -----------------------------------------------------


def test_the_field_is_registered_and_personal():
    """Personal like the theme, not global like a jump key: her arrangement
    must not rearrange his phone."""
    assert sections.SETTING_KEY in settings_form.REGISTRY
    assert sections.SETTING_KEY in settings_form.PERSONAL_KEYS


def test_apply_cleans_a_submitted_order():
    posted = ",".join(_journal_first())
    out = settings_form.apply({sections.SETTING_KEY: posted}, declared=sections.SETTING_KEY)
    assert sections.order(out[sections.SETTING_KEY])[:2] == ["journal", "ask"]


def test_apply_refuses_junk_from_a_stale_form():
    out = settings_form.apply(
        {sections.SETTING_KEY: "<script>,journal"}, declared=sections.SETTING_KEY
    )
    assert "<script>" not in out[sections.SETTING_KEY]
    assert sorted(sections.order(out[sections.SETTING_KEY])) == sorted(sections.DEFAULT_ORDER)


def test_the_value_survives_the_appearance_blob(temp_data_dir):
    """`_valid_appearance` drops any key it does not recognize, so a personal
    setting that is not a fixed-choice dropdown has to be let through by name -
    otherwise it saves, silently stores nothing, and reverts on reload."""
    person_id = people.add("Ordertester")
    people.set_appearance(person_id, {sections.SETTING_KEY: ",".join(_journal_first())})
    stored = people.appearance_of(people.get(person_id))
    assert sections.order(stored[sections.SETTING_KEY]) == _journal_first()


def test_the_arrangement_can_be_put_back(temp_data_dir):
    """Blank means "follow the shipped page". The merge in `set_appearance`
    would otherwise fold the old arrangement straight back over the top, and
    the reset button would look like it had done nothing."""
    person_id = people.add("Resettester")
    people.set_appearance(person_id, {sections.SETTING_KEY: ",".join(_journal_first())})
    people.set_appearance(person_id, {sections.SETTING_KEY: ""})
    stored = people.appearance_of(people.get(person_id))
    assert sections.SETTING_KEY not in stored


def test_putting_the_arrangement_back_leaves_the_theme_alone(temp_data_dir):
    """The removal above is scoped to what was submitted, so it must not take
    the person's theme with it."""
    person_id = people.add("Themetester")
    people.set_appearance(
        person_id,
        {"ui_theme": "paper", sections.SETTING_KEY: ",".join(_journal_first())},
    )
    people.set_appearance(person_id, {sections.SETTING_KEY: ""})
    stored = people.appearance_of(people.get(person_id))
    assert stored.get("ui_theme") == "paper"
    assert sections.SETTING_KEY not in stored


def test_a_junk_arrangement_in_the_row_does_not_reach_the_page(temp_data_dir):
    person_id = people.add("Junktester")
    conn = db.get_conn()
    with db._LOCK:
        conn.execute(
            "UPDATE people SET appearance = ? WHERE id = ?",
            (json.dumps({sections.SETTING_KEY: "evil,journal"}), person_id),
        )
        conn.commit()
    stored = people.appearance_of(people.get(person_id))
    assert "evil" not in sections.order(stored.get(sections.SETTING_KEY))


# --- the page itself --------------------------------------------------------


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _order_on_page(html: str) -> list[str]:
    """The sections in the order the MARKUP puts them.

    Deliberately reads the document rather than any CSS: the whole promise of
    this feature is that the rail's chapter list, the tab sequence and a screen
    reader move with the page, which flexbox `order` would not have done.
    """
    return re.findall(r"<!-- section: ([a-z]+) -->", html)


def _project(client: TestClient) -> str:
    db.create_project("Arranged", stage="active", build_approved=True, slug="arranged")
    return "arranged"


def test_the_page_ships_in_the_default_order(client):
    slug = _project(client)
    found = _order_on_page(client.get(f"/project/{slug}").text)
    assert found == [n for n in sections.DEFAULT_ORDER if n in found]
    assert "todo" in found and "journal" in found


def test_every_movable_section_is_wrapped(client):
    """A block that forgot its wrapper would be silently unmovable: it would
    render in its hard-coded place and no reordering would touch it."""
    slug = _project(client)
    found = set(_order_on_page(client.get(f"/project/{slug}").text))
    assert found == set(sections.DEFAULT_ORDER)


def test_a_saved_arrangement_reorders_the_real_page(client):
    slug = _project(client)
    people.set_appearance(people.ensure_owner(), {sections.SETTING_KEY: ",".join(_journal_first())})
    found = _order_on_page(client.get(f"/project/{slug}").text)
    assert found[0] == "journal"
    assert found == _journal_first()


def test_the_banner_and_the_danger_zone_do_not_move(client):
    """The three pinned blocks are pinned by not being wrapped at all, so the
    strongest assertion is that no arrangement can name them."""
    assert "danger" not in sections.SECTION_NAMES
    assert "summary" not in sections.SECTION_NAMES
    slug = _project(client)
    html = client.get(f"/project/{slug}").text
    # The danger zone renders after every movable section, whatever the order.
    if "danger-zone" in html:
        assert html.index("danger-zone") > html.rindex("<!-- section: ")


def test_the_settings_page_lists_every_section_to_move(client):
    html = client.get("/settings").text
    for section in sections.SECTIONS:
        assert f'data-arrange-row="{section.name}"' in html, section.name

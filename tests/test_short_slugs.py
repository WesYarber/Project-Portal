"""Short directory names, and renaming a project by clicking its name.

Wes, 2026-07-22 01:25: "For workspace folders, I want their folder names to not
be exactly the same as the project name. I want the folder name to be a smaller,
more brief directory name which is more like a directory name than a long
project name with hyphens between the words. [...] Also, I want to be able to
rename a project by clicking on its name at the top in the project page. This
shouldn't prompt a change of directory name now that they aren't explicitly
trying to be the same."

Those two halves are one change: the folder stops tracking the title, which is
exactly what makes a one-click title rename safe.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _make(title, slug, **fields):
    row = db.create_project(title, description="x", slug=slug)
    (config.PROJECTS_DIR / slug).mkdir(parents=True, exist_ok=True)
    if fields:
        db.update_project(row["id"], **fields)
    return db.get_project(row["id"])


def _fresh(project):
    return db.get_project(project["id"])


# --- what a short folder name looks like ------------------------------------
#
# These cases are the shapes of real portal titles, not invented ones (only the
# host and domain names are neutralised), because the whole judgement being
# tested is "would the owner want to cd into that".

@pytest.mark.parametrize(
    "title, expected",
    [
        ("Board games for example.net/games", "board-games"),
        ("Self-hosted mail on testhost", "self-hosted-mail"),
        ("Giftable Home NAS Box", "giftable-home-nas"),
        ("Silhouette print-and-cut for MTG proxies", "silhouette"),
        ("Manabase — fast offline MTG life counter (PWA)", "manabase"),
        ("Handwriting to Plotter Pipeline", "handwriting-plotter"),
        ("OpenJournal", "openjournal"),
    ],
)
def test_short_slug_on_the_real_titles(title, expected):
    assert db.short_slug(title) == expected


def test_a_hyphenated_compound_stays_one_word():
    # "self-hosted" split into two words would spend the whole budget on the
    # first half and produce a name that reads as a truncation.
    assert db.short_slug("Self-hosted mail on testhost") == "self-hosted-mail"


def test_filler_words_are_dropped():
    # "a", "for", "the", "of" and "my" are five of the eight words.
    assert db.short_slug("A tool for the tracking of my books") == "tool-tracking-books"


def test_the_subtitle_after_a_dash_is_dropped():
    assert db.short_slug("Manabase - a fast offline life counter") == "manabase"
    assert db.short_slug("Manabase: a fast offline life counter") == "manabase"
    assert db.short_slug("Manabase (fast, offline)") == "manabase"


def test_a_title_that_is_all_filler_still_gets_a_name():
    # Dropping every word would leave nothing to cd into.
    assert db.short_slug("All About It") == "all-about-it"


def test_the_name_stays_within_budget():
    slug = db.short_slug("Distributed Telemetry Aggregation Platform Service Layer")
    assert len(slug) <= db.SHORT_SLUG_BUDGET
    assert slug.count("-") + 1 <= db.SHORT_SLUG_MAX_WORDS


def test_the_first_word_is_kept_whole_even_past_the_budget():
    # A name chopped mid-word ("electroencephalog") is worse than one that is
    # a few characters over, so the first word is never cut to fit.
    slug = db.short_slug("Electroencephalography viewer")
    assert slug == "electroencephalography"
    assert len(slug) > db.SHORT_SLUG_BUDGET


def test_a_single_enormous_word_is_still_bounded():
    assert len(db.short_slug("x" * 200)) <= db.SHORT_SLUG_HARD_LEN


def test_junk_and_empty_titles_do_not_raise():
    assert db.short_slug("") == "idea"
    assert db.short_slug("!!! ???") == "idea"
    assert db.short_slug(None) == "idea"


# --- which folders are actually offered a rename ----------------------------

def test_raw_idea_text_is_untidy():
    assert db.slug_is_untidy("various-games-to-implement-and-host-on-my-website")
    assert db.slug_is_untidy("home-server-email-server-here-on-testhost")


@pytest.mark.parametrize(
    "slug",
    [
        "board-games",
        "cork-engraving-modeler",
        "electric-car-cost-calc",
        "portal-pipeline-smoke-test",
        "thermalproxy",
        "life-organizer",
    ],
)
def test_a_real_folder_name_is_left_alone(slug):
    # These are all names Wes could plausibly have typed himself. Offering to
    # "tidy" one is noise, and noise is what teaches him to dismiss the offers
    # that matter.
    assert not db.slug_is_untidy(slug)


def test_a_title_change_alone_never_proposes_a_folder_move(temp_data_dir):
    # The old rule was `slugify_title(title) != slug`, which meant every title
    # edit raised a "rename the folder?" prompt. Folder names no longer track
    # titles, so this must stay silent.
    project = _make("Life OS", "life-organizer")
    db.update_project(project["id"], title="Life Operating System")
    assert db.suggested_slug(_fresh(project)) is None


def test_a_raw_folder_still_gets_a_short_suggestion(temp_data_dir):
    project = _make(
        "Board games for example.net/games",
        "various-games-to-implement-and-host-on-my-website-at-example-net-games",
    )
    assert db.suggested_slug(project) == "board-games"


def test_no_suggestion_when_the_short_name_is_taken(temp_data_dir):
    _make("Board games", "board-games")
    other = _make(
        "Board games for example.net/games",
        "various-games-to-implement-and-host-on-my-website-at-example-net-games",
    )
    assert db.suggested_slug(other) is None


# --- renaming a project by clicking its name --------------------------------

def test_rename_changes_the_title(client, temp_data_dir):
    project = _make("Dice Tower", "dice-tower")
    resp = client.post(
        "/project/dice-tower/rename", data={"title": "Dice Tower Mk2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _fresh(project)["title"] == "Dice Tower Mk2"


def test_rename_never_touches_the_folder(client, temp_data_dir):
    project = _make("Dice Tower", "dice-tower")
    client.post("/project/dice-tower/rename", data={"title": "Something Else Entirely"})
    after = _fresh(project)
    assert after["slug"] == "dice-tower"
    assert (config.PROJECTS_DIR / "dice-tower").is_dir()
    # And it does not leave a "tidy the folder?" prompt behind either.
    assert db.suggested_slug(after) is None


def test_rename_locks_the_title(client, temp_data_dir):
    # A name Wes typed himself being replaced by the next agent's report is the
    # obvious way for this button to become annoying.
    project = _make("Dice Tower", "dice-tower")
    client.post("/project/dice-tower/rename", data={"title": "Dice Tower Mk2"})
    assert _fresh(project)["title_locked"] == 1


def test_rename_is_journalled(client, temp_data_dir):
    project = _make("Dice Tower", "dice-tower")
    client.post("/project/dice-tower/rename", data={"title": "Dice Tower Mk2"})
    entries = db.list_journal(project["id"])
    assert any("Dice Tower Mk2" in e["content_md"] for e in entries)


def test_rename_scrubs_the_title(client, temp_data_dir):
    project = _make("Dice Tower", "dice-tower")
    client.post("/project/dice-tower/rename", data={"title": "Dice  Tower\r Mk2 "})
    assert _fresh(project)["title"] == "Dice Tower Mk2"


def test_an_empty_rename_changes_nothing(client, temp_data_dir):
    project = _make("Dice Tower", "dice-tower")
    resp = client.post(
        "/project/dice-tower/rename", data={"title": "   "}, follow_redirects=False
    )
    assert resp.status_code == 303
    after = _fresh(project)
    assert after["title"] == "Dice Tower"
    # Nothing happened, so nothing was locked and nothing was journalled.
    assert after["title_locked"] == 0
    assert not any("Renamed" in e["content_md"] for e in db.list_journal(project["id"]))


def test_renaming_to_the_same_title_is_a_no_op(client, temp_data_dir):
    project = _make("Dice Tower", "dice-tower")
    client.post("/project/dice-tower/rename", data={"title": "Dice Tower"})
    assert _fresh(project)["title_locked"] == 0


def test_rename_404s_on_an_unknown_project(client, temp_data_dir):
    resp = client.post("/project/nope/rename", data={"title": "x"})
    assert resp.status_code == 404


def test_the_page_makes_the_title_clickable(client, temp_data_dir):
    _make("Dice Tower", "dice-tower")
    body = client.get("/project/dice-tower").text
    assert 'action="/project/dice-tower/rename"' in body
    assert "data-rename-title" in body


def test_the_meta_project_title_is_renameable(client, temp_data_dir):
    # The slug is pinned for the self-update check, but the *name* is not, and
    # this route cannot move a folder anyway.
    project = _make("Project Portal", config.META_PROJECT_SLUG)
    client.post(f"/project/{config.META_PROJECT_SLUG}/rename", data={"title": "The Portal"})
    after = _fresh(project)
    assert after["title"] == "The Portal"
    assert after["slug"] == config.META_PROJECT_SLUG


# --- the layout fixes from the same note ------------------------------------

def test_the_workspace_section_arrives_collapsed(client, temp_data_dir):
    _make("Dice Tower", "dice-tower")
    (config.PROJECTS_DIR / "dice-tower" / "PLAN.md").write_text("plan")
    body = client.get("/project/dice-tower").text
    block = body.split('id="workspace"')[1].split(">")[0]
    assert "open" not in block


def test_a_folder_row_carries_no_extra_gap():
    # `details { margin-bottom: 1rem }` applies to every folder in the tree, so
    # a folder row was 1rem taller than a file row and the ten-row scroll cap
    # stopped being ten rows.
    css = (config.APP_ROOT / "app" / "static" / "style.css").read_text()
    assert ".tree-dir { margin-bottom: 0; }" in css


def test_a_label_styled_as_a_button_carries_no_bottom_margin():
    # This is why `attach files` sat visibly higher than `add note`: the global
    # `label` rule's 0.35rem bottom margin is part of the box that the row
    # centers.
    css = (config.APP_ROOT / "app" / "static" / "style.css").read_text()
    assert "label.btn { margin-bottom: 0; }" in css


def test_the_journal_box_is_taller_than_it_was():
    # 75 -> 90 -> 95vh, one step per ask.
    css = (config.APP_ROOT / "app" / "static" / "style.css").read_text()
    assert "--journal-max-h: 95vh;" in css


def test_the_submit_chord_covers_single_line_inputs_too():
    # Wes asked for shift+enter to submit "the add note field here or ... an
    # idea input field". Both of those are textareas and were already covered;
    # the title box above the idea box is an <input> and was not, so the chord
    # silently did nothing in the one field that looks identical to its
    # neighbour.
    js = (config.APP_ROOT / "app" / "static" / "app.js").read_text()
    assert "SUBMIT_ON_CHORD" in js
    assert 'el.tagName === "INPUT"' in js

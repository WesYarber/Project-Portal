"""Folder names that follow the title, and titles that carry no junk.

A project is created before it has a name: the slug is cut from whatever Wes
typed when he had the idea, which is why the live portal has folders called
`make-the-silhouette-card-cutter-work-with-my-mtg-proxy-forge-maybe-modify-the-e`
sitting under the title "Silhouette print-and-cut for MTG proxies". Agents have
been naming projects for a while now; this is the other half - closing the gap
between the name and the folder, without a run ever doing it unasked.
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


# --- titles are scrubbed at the one place they are written -----------------

def test_clean_title_strips_control_characters():
    # The real bug: a title pasted into the form arrived as "SimpleClickTrack\r"
    # and the CR traveled into notification text and ssh commands unseen.
    assert db.clean_title("SimpleClickTrack\r") == "SimpleClickTrack"
    assert db.clean_title("Life\tOS\n") == "Life OS"
    assert db.clean_title("  spaced   out  ") == "spaced out"


def test_clean_title_caps_length():
    assert len(db.clean_title("x" * 500)) == 200


def test_create_project_cleans_the_title(temp_data_dir):
    row = db.create_project("Dice Tower\r")
    assert row["title"] == "Dice Tower"


def test_update_project_cleans_the_title(temp_data_dir):
    row = _make("Dice Tower", "dice-tower")
    db.update_project(row["id"], title="Rolled\r\nTower")
    assert _fresh(row)["title"] == "Rolled Tower"


def test_an_all_junk_title_is_dropped_rather_than_blanking_the_column(temp_data_dir):
    row = _make("Dice Tower", "dice-tower")
    db.update_project(row["id"], title="\r\n\t ")
    assert _fresh(row)["title"] == "Dice Tower"


def test_an_all_junk_title_alone_does_not_touch_other_columns(temp_data_dir):
    """`update_project` returns early when the scrub empties the only field, so
    the row must be left exactly as it was - not stamped with a new
    updated_at."""
    row = _make("Dice Tower", "dice-tower")
    before = _fresh(row)["updated_at"]
    db.update_project(row["id"], title="\r")
    assert _fresh(row)["updated_at"] == before


def test_junk_titles_are_backfilled_on_startup(temp_data_dir):
    row = _make("Dice Tower", "dice-tower")
    # Write past the scrub, the way a pre-clean_title portal would have.
    conn = db.get_conn()
    conn.execute("UPDATE projects SET title = ? WHERE id = ?", ("Dice Tower\r", row["id"]))
    conn.commit()
    db.init_db()
    assert _fresh(row)["title"] == "Dice Tower"


# --- what the suggested folder name is -------------------------------------

def test_slugify_title_truncates_at_a_word_boundary():
    slug = db.slugify_title("Manabase - a fast offline MTG life counter for commander games")
    assert len(slug) <= db.MAX_SLUG_LEN
    assert not slug.endswith("-")
    # Cut between words, never mid-word.
    assert all(part for part in slug.split("-"))
    assert slug.startswith("manabase-a-fast-offline-mtg-life-counter")


def test_slugify_title_leaves_a_short_title_alone():
    assert db.slugify_title("Silhouette print-and-cut for MTG proxies") == (
        "silhouette-print-and-cut-for-mtg-proxies"
    )


def test_a_single_enormous_word_is_still_cut_to_length():
    assert len(db.slugify_title("x" * 200)) <= db.MAX_SLUG_LEN


def test_suggests_a_short_folder_name_when_the_folder_is_the_old_idea_text(temp_data_dir):
    project = _make(
        "Silhouette print-and-cut for MTG proxies",
        "make-the-silhouette-card-cutter-work-with-my-mtg-proxy-forge",
    )
    # A brief directory name, not a hyphenated copy of the title.
    assert db.suggested_slug(project) == "silhouette"


def test_no_suggestion_when_the_folder_already_matches(temp_data_dir):
    assert db.suggested_slug(_make("Cork Engraving Modeler", "cork-engraving-modeler")) is None


def test_no_suggestion_once_dismissed(temp_data_dir):
    project = _make("Dice Tower", "a-tower-that-rolls-dice", slug_locked=1)
    assert db.suggested_slug(project) is None


def test_no_suggestion_for_the_portals_own_project(temp_data_dir):
    project = _make("Something Else Entirely", config.META_PROJECT_SLUG)
    assert db.suggested_slug(project) is None


def test_no_suggestion_when_another_project_holds_that_slug(temp_data_dir):
    _make("Dice Tower", "dice-tower")
    other = _make("Dice Tower", "some-idea-about-dice")
    # Renaming would collide, so there is nothing to offer.
    assert db.suggested_slug(other) is None


def test_an_untitled_project_gets_no_suggestion(temp_data_dir):
    project = _make("!!!", "some-idea")
    assert db.suggested_slug(project) is None


def test_the_backfill_list_covers_every_stale_project(temp_data_dir):
    _make("Dice Tower", "an-idea-about-a-dice-tower-that-rolls-them")
    _make("Cork Engraving Modeler", "cork-engraving-modeler")
    # `life-organizer` is a perfectly good folder name; the fact that it does
    # not match the title is no longer a reason to offer anything.
    _make("Life OS", "life-organizer")
    pairs = db.projects_with_suggested_slugs()
    assert {p["slug"]: target for p, target in pairs} == {
        "an-idea-about-a-dice-tower-that-rolls-them": "dice-tower",
    }


# --- applying it -----------------------------------------------------------

def test_apply_renames_the_folder_and_the_column(client, temp_data_dir):
    project = _make("Dice Tower", "an-idea-about-a-dice-tower")
    (config.PROJECTS_DIR / "an-idea-about-a-dice-tower" / "PLAN.md").write_text("plan")

    resp = client.post(
        "/project/an-idea-about-a-dice-tower/tidy-slug",
        data={"action": "apply"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/project/dice-tower"

    assert _fresh(project)["slug"] == "dice-tower"
    assert (config.PROJECTS_DIR / "dice-tower" / "PLAN.md").read_text() == "plan"
    assert not (config.PROJECTS_DIR / "an-idea-about-a-dice-tower").exists()


def test_apply_locks_the_slug_so_it_is_not_offered_again(client, temp_data_dir):
    project = _make("Dice Tower", "an-idea-about-a-dice-tower")
    client.post("/project/an-idea-about-a-dice-tower/tidy-slug", data={"action": "apply"})
    after = _fresh(project)
    assert after["slug_locked"] == 1
    assert db.suggested_slug(after) is None


def test_apply_is_journalled(client, temp_data_dir):
    project = _make("Dice Tower", "an-idea-about-a-dice-tower")
    client.post("/project/an-idea-about-a-dice-tower/tidy-slug", data={"action": "apply"})
    entries = db.list_journal(project["id"], limit=10)
    assert any("dice-tower" in e["content_md"] and e["author"] == "user" for e in entries)


def test_dismiss_keeps_the_folder_and_stops_asking(client, temp_data_dir):
    project = _make("Dice Tower", "an-idea-about-a-dice-tower")
    client.post("/project/an-idea-about-a-dice-tower/tidy-slug", data={"action": "dismiss"})
    after = _fresh(project)
    assert after["slug"] == "an-idea-about-a-dice-tower"
    assert after["slug_locked"] == 1
    assert (config.PROJECTS_DIR / "an-idea-about-a-dice-tower").exists()


def test_apply_is_refused_while_an_agent_is_running_in_the_workspace(client, temp_data_dir):
    project = _make("Dice Tower", "an-idea-about-a-dice-tower")
    db.create_run(project["id"], "build", "opus")
    resp = client.post(
        "/project/an-idea-about-a-dice-tower/tidy-slug",
        data={"action": "apply"},
        follow_redirects=False,
    )
    assert resp.status_code == 409
    assert _fresh(project)["slug"] == "an-idea-about-a-dice-tower"
    assert (config.PROJECTS_DIR / "an-idea-about-a-dice-tower").exists()


def test_apply_on_an_already_tidy_project_is_a_no_op(client, temp_data_dir):
    project = _make("Dice Tower", "dice-tower")
    resp = client.post(
        "/project/dice-tower/tidy-slug", data={"action": "apply"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert _fresh(project)["slug"] == "dice-tower"


def test_apply_refuses_when_the_target_appeared_on_disk_meanwhile(client, temp_data_dir):
    """A leftover directory with no project behind it must not be merged into."""
    project = _make("Dice Tower", "an-idea-about-a-dice-tower")
    (config.PROJECTS_DIR / "dice-tower").mkdir(parents=True)
    resp = client.post(
        "/project/an-idea-about-a-dice-tower/tidy-slug",
        data={"action": "apply"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert _fresh(project)["slug"] == "an-idea-about-a-dice-tower"


def test_the_portals_own_project_cannot_be_tidied(client, temp_data_dir):
    project = _make("Renamed Portal", config.META_PROJECT_SLUG)
    resp = client.post(
        f"/project/{config.META_PROJECT_SLUG}/tidy-slug",
        data={"action": "apply"},
        follow_redirects=False,
    )
    # No suggestion exists for it, so this is a no-op rather than an error -
    # but the slug must be untouched either way.
    assert resp.status_code == 303
    assert _fresh(project)["slug"] == config.META_PROJECT_SLUG


# --- a manual rename counts as having had your say --------------------------

def test_renaming_by_hand_stops_the_suggestion(client, temp_data_dir):
    project = _make("Dice Tower", "an-idea-about-a-dice-tower")
    client.post(
        "/project/an-idea-about-a-dice-tower/details",
        data={"title": "Dice Tower", "description": "x", "new_slug": "towers"},
    )
    after = _fresh(project)
    assert after["slug"] == "towers"
    assert after["slug_locked"] == 1
    assert db.suggested_slug(after) is None


def test_editing_details_without_renaming_leaves_the_suggestion_alone(client, temp_data_dir):
    project = _make("Dice Tower", "an-idea-about-a-dice-tower")
    client.post(
        "/project/an-idea-about-a-dice-tower/details",
        data={"title": "Dice Tower", "description": "new words", "new_slug": ""},
    )
    after = _fresh(project)
    assert after["slug_locked"] == 0
    assert db.suggested_slug(after) == "dice-tower"


# --- the pages actually offer it -------------------------------------------

def test_the_project_page_offers_the_rename(client, temp_data_dir):
    _make("Dice Tower", "an-idea-about-a-dice-tower")
    body = client.get("/project/an-idea-about-a-dice-tower").text
    assert "slug-suggestion" in body
    assert "dice-tower" in body


def test_a_tidy_project_page_says_nothing_about_it(client, temp_data_dir):
    _make("Dice Tower", "dice-tower")
    assert "slug-suggestion" not in client.get("/project/dice-tower").text


def test_settings_lists_every_stale_folder(client, temp_data_dir):
    _make("Dice Tower", "an-idea-about-a-dice-tower")
    _make("Life OS", "an-operating-system-for-my-whole-life")
    _make("Cork Engraving Modeler", "cork-engraving-modeler")
    body = client.get("/settings").text
    assert "workspace folder names" in body
    assert "an-idea-about-a-dice-tower" in body
    assert "an-operating-system-for-my-whole-life" in body
    assert body.count("/tidy-slug") == 2


def test_settings_hides_the_section_when_everything_is_tidy(client, temp_data_dir):
    _make("Dice Tower", "dice-tower")
    assert "workspace folder names" not in client.get("/settings").text

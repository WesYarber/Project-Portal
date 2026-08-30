"""Related projects declared by hand (`project_links`).

The slug heuristic in `app/crossproject.py` pairs `commander-case-custom-lid`
with `commander-case-counter-configurator` for free, and it will never pair
`commander-case` with `3d-vectorizer` - the two names share not one token. That
pair is exactly the one Wes has to be the wire for today, so it gets a declared
link instead.

These tests pin the decisions rather than the wording:

- the pair is stored once and unordered, so a link declared on one project is
  seen from the other end too;
- a declared link is carried whole and ahead of the guessed ones, because a
  person said so and a prefix rule guessed;
- it is still readable-filtered, so a link is a pointer and never a grant;
- it drops out when the pair becomes family, because the sub-project section a
  few lines above already names them with the slug this section exists to add;
- and deleting either project takes the link with it.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import crossproject, db, people


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def make(title: str, slug: str, **kw) -> int:
    return int(db.create_project(title, slug=slug, stage="active", **kw)["id"])


# ------------------------------------------------------------------ storage


def test_a_link_is_one_row_whichever_way_round_it_is_declared():
    """Two rows for one relationship would let a link exist in one direction
    only - a thing a person would have to notice and repair by hand."""
    shop = make("Shop", "commander-case")
    tool = make("Vectorizer", "3d-vectorizer")

    assert db.link_projects(shop, tool) is True
    # The mirror image is the same link, not a second one.
    assert db.link_projects(tool, shop) is False
    assert db.link_projects(shop, tool) is False

    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM project_links").fetchone()[0] == 1


def test_the_link_is_seen_from_both_ends():
    shop = make("Shop", "commander-case")
    tool = make("Vectorizer", "3d-vectorizer")
    db.link_projects(shop, tool)

    assert db.linked_project_ids(shop) == {tool}
    assert db.linked_project_ids(tool) == {shop}


def test_a_project_cannot_be_linked_to_itself():
    """Refused rather than left to the CHECK constraint: an IntegrityError out
    of a button press reads as a portal fault when it is really a bad pick."""
    solo = make("Solo", "solo")

    assert db.link_projects(solo, solo) is False
    assert db.linked_project_ids(solo) == set()


def test_unlinking_works_from_either_end():
    shop = make("Shop", "commander-case")
    tool = make("Vectorizer", "3d-vectorizer")
    db.link_projects(shop, tool)

    # Declared on `shop`, removed from `tool`.
    assert db.unlink_projects(tool, shop) is True
    assert db.linked_project_ids(shop) == set()
    # And a second removal is honestly reported as having removed nothing.
    assert db.unlink_projects(tool, shop) is False


@pytest.mark.parametrize("which", ["a_id", "b_id"])
def test_deleting_a_project_takes_its_links_with_it(which):
    """Otherwise a recycled row id would inherit a stranger's links.

    Both ends are checked because the pair is stored once: the row holds the
    smaller id in `a_id`, so deleting only the later-created project would
    exercise one of the two cascades and leave the other unproven."""
    shop = make("Shop", "commander-case")
    tool = make("Vectorizer", "3d-vectorizer")
    db.link_projects(shop, tool)
    # `shop` was created first, so it holds the smaller id and sits in a_id.
    assert shop < tool

    doomed, survivor = (shop, tool) if which == "a_id" else (tool, shop)
    db.delete_project(doomed)

    assert db.linked_project_ids(survivor) == set()
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM project_links").fetchone()[0] == 0


# --------------------------------------------------------------- relatedness


def test_a_declared_link_relates_two_projects_no_name_could_pair():
    shop = make("Card Case Shop", "commander-case")
    tool = make("Vectorizer", "3d-vectorizer")

    # The heuristic on its own finds nothing - that is the whole premise.
    assert crossproject.related(shop) == []

    db.link_projects(shop, tool)

    assert [r["slug"] for r in crossproject.related(shop)] == ["3d-vectorizer"]
    assert [r["slug"] for r in crossproject.related(tool)] == ["commander-case"]


def test_a_declared_link_is_never_crowded_out_by_guessed_ones():
    """`RELATED_CAP` bounds what the heuristic may ADD, never what a person
    asked for. A guess must not push out a statement."""
    mine = make("Mine", "kingshot-mine")
    for n in range(crossproject.RELATED_CAP + 3):
        make(f"Guessed {n}", f"kingshot-guessed-{n}")
    # Padding, so `kingshot` stays under the rarity ceiling: ten projects
    # sharing a token on a board of twelve is a token that groups nothing, and
    # the heuristic would correctly return none of them.
    for n in range(20):
        make(f"Filler {n}", f"unrelated-filler-{n}")
    hand = make("Unrelated By Name", "totally-different")
    db.link_projects(mine, hand)

    slugs = [r["slug"] for r in crossproject.related(mine)]
    assert slugs[0] == "totally-different"
    # The declared one takes a slot; the guessed ones fill what is left.
    assert len(slugs) == crossproject.RELATED_CAP
    assert slugs.count("totally-different") == 1


def test_many_declared_links_are_all_carried():
    """More declared links than the cap means the person wanted them all.

    There are real guessed candidates here too, so this also pins what happens
    when the leftover room goes NEGATIVE: `derived` must answer nothing, not
    slice its ranking from the end and leak the surplus back in."""
    mine = make("Mine", "kingshot-mine")
    for n in range(4):
        make(f"Guessed {n}", f"kingshot-guessed-{n}")
    for n in range(20):
        make(f"Filler {n}", f"unrelated-filler-{n}")
    # The heuristic really does have something to offer, so an empty guessed
    # list below means it was refused the room, not that it found nothing.
    assert crossproject.derived(mine)

    linked = []
    for n in range(crossproject.RELATED_CAP + 2):
        other = make(f"Hand {n}", f"totally-apart-{n}")
        db.link_projects(mine, other)
        linked.append(f"totally-apart-{n}")

    slugs = [r["slug"] for r in crossproject.related(mine)]
    assert sorted(slugs) == sorted(linked)


def test_the_guessed_list_shrinks_by_what_the_declared_one_took():
    """Together they stay inside `RELATED_CAP`. Without this the prompt would
    carry the cap's worth of guesses PLUS however many links exist."""
    mine = make("Mine", "kingshot-mine")
    for n in range(crossproject.RELATED_CAP + 2):
        make(f"Guessed {n}", f"kingshot-guessed-{n}")
    for n in range(20):
        make(f"Filler {n}", f"unrelated-filler-{n}")
    for n in range(2):
        db.link_projects(mine, make(f"Hand {n}", f"totally-apart-{n}"))

    assert len(crossproject.related(mine)) == crossproject.RELATED_CAP

    section = crossproject.prompt_section(db.get_project(mine), offered=True)
    named = [s for s in section.splitlines() if s.startswith("- **")]
    assert len(named) == crossproject.RELATED_CAP
    assert sum("totally-apart-" in line for line in named) == 2


def test_a_project_appears_once_even_when_declared_and_guessed_both_find_it():
    mine = make("Gift Codes", "kingshot-gift-code")
    bear = make("Auto Bear", "kingshot-auto-bear")
    db.link_projects(mine, bear)

    slugs = [r["slug"] for r in crossproject.related(mine)]
    assert slugs == ["kingshot-auto-bear"]
    # And it is reported as the declared one, which is the stronger claim.
    assert [r["slug"] for r in crossproject.declared(mine)] == ["kingshot-auto-bear"]
    assert crossproject.derived(mine, exclude={bear}) == []


def test_a_link_is_a_pointer_and_never_a_grant():
    """A link to a project the run's principal is not a member of names
    nothing. Otherwise declaring one would be a way to hand a run a project its
    own person cannot see."""
    her = people.add("Erin", gender="female")
    hers = make("Hers", "hers")
    people.set_members(hers, [her])
    mine = make("Mine", "mine")

    db.link_projects(mine, hers)

    assert crossproject.declared(mine) == []
    assert crossproject.related(mine) == []
    assert "hers" not in crossproject.listing(mine)


def test_a_link_row_that_outlived_its_project_is_ignored_rather_than_fatal(monkeypatch):
    """The cascade makes this impossible, and "impossible" is one schema edit
    from being possible. The alternative is `family_ids(None)` raising in the
    middle of building a run prompt, which loses the run over a stale row."""
    gone = make("Gone", "gone")
    db.delete_project(gone)
    monkeypatch.setattr(db, "linked_project_ids", lambda pid: {999})

    assert crossproject.declared(gone) == []
    assert crossproject.related(gone) == []


def test_the_setting_switches_declared_links_off_too(monkeypatch):
    monkeypatch.setattr(db, "get_setting", lambda key, *a, **k: "0" if key == "cross_project" else None)
    shop = make("Shop", "commander-case")
    tool = make("Vectorizer", "3d-vectorizer")
    db.link_projects(shop, tool)

    assert crossproject.declared(shop) == []
    assert crossproject.related(shop) == []


def test_a_link_that_became_family_is_left_to_the_subproject_section():
    """Not because a link there is wrong, but because the block directly above
    in the prompt already names every child with the slug this section adds."""
    parent = make("Parent", "widgets")
    kid = make("Kid", "totally-different")
    db.link_projects(parent, kid)
    assert [r["slug"] for r in crossproject.declared(parent)] == ["totally-different"]

    db.update_project(kid, parent_id=parent)

    assert crossproject.declared(parent) == []
    assert crossproject.declared(kid) == []


# ------------------------------------------------------------------- prompt


def test_the_prompt_says_a_person_declared_these_and_says_it_first():
    mine = make("Gift Codes", "kingshot-gift-code")
    make("Auto Bear", "kingshot-auto-bear")
    hand = make("Vectorizer", "3d-vectorizer")
    db.link_projects(mine, hand)

    section = crossproject.prompt_section(db.get_project(mine), offered=True)

    assert "linked these projects to this one by hand" in section
    assert "`3d-vectorizer`" in section
    assert "`kingshot-auto-bear`" in section
    # The declared one is named before the guessed one, because a run choosing
    # which neighbor to spend context on should choose from the stronger list.
    assert section.index("3d-vectorizer") < section.index("kingshot-auto-bear")


def test_a_project_with_only_a_declared_link_still_gets_the_section():
    """The heuristic finds nothing here, so without declared links reaching
    `prompt_section` this project would be told about no neighbors at all."""
    shop = make("Shop", "commander-case")
    make("Vectorizer", "3d-vectorizer")
    assert crossproject.prompt_section(db.get_project(shop), offered=True) == ""

    db.link_projects(shop, db.get_project_by_slug("3d-vectorizer")["id"])

    section = crossproject.prompt_section(db.get_project(shop), offered=True)
    assert "`3d-vectorizer`" in section
    assert "project_context" in section


def test_the_guessed_sentence_does_not_claim_to_be_the_only_list():
    """With a declared list above it, "These are other projects" would read as
    if the declared ones were something else entirely."""
    mine = make("Gift Codes", "kingshot-gift-code")
    make("Auto Bear", "kingshot-auto-bear")

    alone = crossproject.prompt_section(db.get_project(mine), offered=True)
    assert "These are other projects" in alone

    db.link_projects(mine, make("Vectorizer", "3d-vectorizer"))
    with_hand = crossproject.prompt_section(db.get_project(mine), offered=True)
    assert "These other projects" in with_hand
    assert "These are other projects" not in with_hand


def test_the_listing_tells_a_declared_link_apart_from_a_guessed_one():
    mine = make("Gift Codes", "kingshot-gift-code")
    make("Auto Bear", "kingshot-auto-bear")
    db.link_projects(mine, make("Vectorizer", "3d-vectorizer"))

    listing = crossproject.listing(mine)
    lines = {line.split("`")[1]: line for line in listing.splitlines() if "`" in line}
    assert "[linked to yours by hand]" in lines["3d-vectorizer"]
    assert "[related to yours]" in lines["kingshot-auto-bear"]
    assert "by hand" not in lines["kingshot-auto-bear"]


# -------------------------------------------------------------------- pages


def test_the_page_links_and_unlinks(client):
    shop = make("Shop", "commander-case")
    tool = make("Vectorizer", "3d-vectorizer")

    resp = client.post("/project/commander-case/link", data={"other_id": tool})
    assert resp.status_code == 200  # followed the redirect
    assert db.linked_project_ids(shop) == {tool}

    body = client.get("/project/commander-case").text
    assert "Related projects" in body
    assert "3d-vectorizer" in body

    client.post("/project/commander-case/unlink", data={"other_id": tool})
    assert db.linked_project_ids(shop) == set()


def test_the_other_end_shows_the_link_and_can_remove_it(client):
    shop = make("Shop", "commander-case")
    tool = make("Vectorizer", "3d-vectorizer")
    client.post("/project/commander-case/link", data={"other_id": tool})

    body = client.get("/project/3d-vectorizer").text
    assert "commander-case" in body

    client.post("/project/3d-vectorizer/unlink", data={"other_id": shop})
    assert db.linked_project_ids(shop) == set()


def test_a_link_that_stopped_being_readable_drops_off_the_page(client):
    """Membership can change after a link is made. Drawing the row anyway would
    name somebody else's project on a page its viewer can open - and the route
    that creates a link checks readability, so only this later drift can get
    here. It stays removable from the other end, where they are a member."""
    her = people.add("Erin", gender="female")
    mine = make("Mine", "mine")
    other = make("Other", "other")
    client.post("/project/mine/link", data={"other_id": other})
    assert "Other" in client.get("/project/mine").text

    people.set_members(other, [her])

    body = client.get("/project/mine").text
    assert "/project/other" not in body
    # The link itself is untouched - it is hidden here, not revoked.
    assert db.linked_project_ids(mine) == {other}
    assert crossproject.declared(mine) == []


def test_the_page_refuses_to_link_a_project_the_runs_could_not_read(client):
    her = people.add("Erin", gender="female")
    hers = make("Hers", "hers")
    people.set_members(hers, [her])
    mine = make("Mine", "mine")

    resp = client.post("/project/mine/link", data={"other_id": hers})

    assert resp.status_code == 400
    assert db.linked_project_ids(mine) == set()


def test_the_page_refuses_a_project_that_is_not_there(client):
    make("Mine", "mine")

    assert client.post("/project/mine/link", data={"other_id": 9999}).status_code == 404


def test_the_picker_never_offers_what_is_already_linked_or_family(client):
    parent = make("Parent", "widgets")
    kid = db.create_project("Kid", slug="widgets-kid", stage="active", parent_id=parent)
    already = make("Already", "already-linked")
    free = make("Free", "free-to-link")
    db.link_projects(parent, already)

    ctx = client.get("/project/widgets")  # renders without raising
    assert ctx.status_code == 200

    from app import main

    candidates = main._related_context(db.get_project(parent))["link_candidates"]
    slugs = {r["slug"] for r in candidates}
    assert slugs == {"free-to-link"}
    assert free and kid


def test_the_fold_is_not_drawn_at_all_when_cross_project_is_off(client, monkeypatch):
    make("Mine", "mine")
    make("Other", "other")
    db.set_setting("cross_project", "0")

    body = client.get("/project/mine").text

    assert "Related projects" not in body
    assert "/project/mine/link" not in body

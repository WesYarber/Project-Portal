"""The dashboard's activity feed is folded shut and fetched on demand.

Wes, 2026-08-29: *"recent activity on the dashboard should also be more
efficiently loaded / don't load everything possible but just load a certain
range of entries. It should also be collapsed by default as I almost never use
that."*

Both halves are one change, and the first is the reason the second is worth
doing. The feed was 25 journal entries rendered inline on every load of the
dashboard - and an agent's entry is not a line, it is several KB of markdown
with tables in it. Measured on the live board the day this changed: 35 KB of
markdown pushed through `markdown_media` before the page he opens most often
from his phone could be sent.

Collapsing alone would have cost exactly the same, because a hidden element is
still a rendered one. So the dashboard sends the fold EMPTY and app.js fetches
`/activity/feed` the first time it is opened. That is the distinction most of this
file exists to pin: not "is it hidden" but "is it absent".
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import db, main

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture
def client(temp_data_dir):
    from app import main as app_main

    return TestClient(app_main.app)


@pytest.fixture
def project():
    return db.create_project(
        "Fridge Board", description="A thing.", stage="active",
        build_approved=True, slug="fridge",
    )


def _js() -> str:
    return (STATIC / "app.js").read_text()


# --- the dashboard does not carry the feed ------------------------------------


def test_the_dashboard_does_not_render_the_entries(client, project):
    """The whole point. A distinctive phrase in a journal entry must not appear
    in the dashboard HTML at all - not hidden, not folded, absent."""
    db.add_journal(project["id"], "agent", "progress", "quartzine sheet 42-BRAVO")

    body = client.get("/").text

    assert "quartzine sheet 42-BRAVO" not in body


def test_the_fold_is_shut_and_lazy(client, project):
    db.add_journal(project["id"], "agent", "progress", "something happened")

    body = client.get("/").text

    assert 'id="activity"' in body
    assert 'data-lazy-src="/activity/feed"' in body
    # `<details open>` would render the summary expanded. It must not.
    assert "<details class=\"fold-section\" id=\"activity\" open" not in body


def test_a_big_feed_does_not_grow_the_dashboard(client, project):
    """The claim is about page WEIGHT, so it is worth measuring rather than
    inferring from the absence of one string. Thirty fat entries must leave the
    dashboard the same size as none."""
    empty = len(client.get("/").text)
    for i in range(30):
        db.add_journal(project["id"], "agent", "progress", f"## Entry {i}\n\n" + ("x" * 2000))

    assert len(client.get("/").text) == empty


# --- /activity/feed serves them ----------------------------------------------------


def test_the_fragment_carries_the_entries(client, project):
    db.add_journal(project["id"], "agent", "progress", "quartzine sheet 42-BRAVO")

    body = client.get("/activity/feed").text

    assert "quartzine sheet 42-BRAVO" in body


def test_the_fragment_is_a_fragment(client, project):
    """It is swapped into an existing page, so a whole document would nest a
    second <html> inside the dashboard."""
    db.add_journal(project["id"], "agent", "progress", "hello")

    body = client.get("/activity/feed").text

    assert "<html" not in body.lower()
    assert "<body" not in body.lower()


def test_an_empty_feed_says_so_rather_than_coming_back_blank(client):
    """A fold that opens onto nothing at all reads as a failed fetch."""
    assert "No activity yet." in client.get("/activity/feed").text


def test_the_first_open_asks_for_a_bounded_range(client, project):
    """"Don't load everything possible but just load a certain range." The fold
    asks for ACTIVITY_PAGE, not for the whole journal."""
    for i in range(main.ACTIVITY_PAGE + 20):
        db.add_journal(project["id"], "agent", "progress", f"entry number {i}")

    body = client.get("/activity/feed").text

    assert body.count('class="journal-entry') == main.ACTIVITY_PAGE


def test_show_more_asks_for_a_bigger_range(client, project):
    for i in range(main.ACTIVITY_PAGE + 20):
        db.add_journal(project["id"], "agent", "progress", f"entry number {i}")

    body = client.get("/activity/feed").text
    assert f'data-lazy-src="/activity/feed?limit={main.ACTIVITY_PAGE * 2}"' in body

    more = client.get(f"/activity/feed?limit={main.ACTIVITY_PAGE * 2}").text
    assert more.count('class="journal-entry') == main.ACTIVITY_PAGE * 2


def test_no_show_more_once_the_feed_is_exhausted(client, project):
    """A short read means there is no more to get, and a "show more" that comes
    back identical is a control that reads as broken."""
    for i in range(3):
        db.add_journal(project["id"], "agent", "progress", f"entry {i}")

    assert "show more activity" not in client.get("/activity/feed").text


def test_the_limit_is_clamped_rather_than_trusted(client, project):
    """It arrives in a query string. Unbounded, a request meant to keep the page
    light renders every entry the install has ever written."""
    for i in range(main.ACTIVITY_MAX + 10):
        db.add_journal(project["id"], "agent", "progress", f"entry {i}")

    body = client.get(f"/activity/feed?limit={main.ACTIVITY_MAX + 500}").text

    assert body.count('class="journal-entry') == main.ACTIVITY_MAX
    # And at the ceiling it stops offering to go further.
    assert "show more activity" not in body


def test_a_negative_limit_does_not_render_the_whole_journal(client, project):
    """The floor on the clamp, and it is not cosmetic: SQLite reads a NEGATIVE
    `LIMIT` as **no limit at all**. So `?limit=-1` on an unclamped endpoint does
    not return one entry or zero, it returns every journal entry the install has
    ever written - the exact opposite of what this whole change is for, and one
    query string away from anybody.

    Found by a mutation sweep: dropping the `max(1, ...)` left the suite green,
    because the test here only asserted the response was a 200. It was.
    """
    for i in range(main.ACTIVITY_PAGE + 40):
        db.add_journal(project["id"], "agent", "progress", f"entry {i}")

    for bad in ("-1", "-5", "0"):
        body = client.get(f"/activity/feed?limit={bad}").text
        count = body.count('class="journal-entry')
        assert count == 1, f"limit={bad} rendered {count} entries"


def test_an_unparseable_limit_does_not_500(client, project):
    db.add_journal(project["id"], "agent", "progress", "hello")

    assert client.get("/activity/feed?limit=abc").status_code == 422


# --- it is scoped to the reader, like the dashboard it belongs to -------------


def test_the_fragment_is_scoped_to_the_readers_projects(client):
    """A real address answers whoever asks it, so the membership rule has to
    live here and not in the caller. Otherwise the cheapest way to read another
    person's journal is to request the fragment directly."""
    from app import people

    mine = db.create_project("Mine", stage="active", slug="mine")
    theirs = db.create_project("Theirs", stage="active", slug="theirs")
    wes = people.ensure_owner()
    karli = people.add(name="Karli", gender="female")
    people.set_members(int(mine["id"]), [wes])
    people.set_members(int(theirs["id"]), [karli])
    db.add_journal(mine["id"], "agent", "progress", "belongs to wes")
    db.add_journal(theirs["id"], "agent", "progress", "belongs to karli")

    # The cookie carries the person's slug, not their id - see people.resolve.
    body = client.get(
        "/activity/feed", cookies={people.COOKIE: people.get(wes)["slug"]}
    ).text

    assert "belongs to wes" in body
    assert "belongs to karli" not in body


# --- the client half ----------------------------------------------------------


def test_the_fold_fetches_on_first_open_only():
    """A second toggle must not re-fetch, and the flag is set before the request
    so a fast double-click cannot fire two."""
    src = _js()

    assert "initLazyFolds();" in src
    assert 'd.matches("details[data-lazy-src]")' in src
    assert "if (!d.open || d.dataset.lazyLoaded) return;" in src


def test_a_loaded_fold_survives_the_live_patch():
    """The morph replaces the page with the server's version, and the server's
    version of this fold is empty - so without an exemption a live refresh would
    empty out a feed the reader is in the middle of. Wes's "nothing moves that
    he did not move", in its most literal form."""
    src = _js()

    assert 'live.matches("details[data-lazy-loaded]")' in src


def test_a_failed_fetch_says_so_and_allows_a_retry():
    """A fold that silently stayed empty would read as "there is nothing here",
    which is a different claim than "this did not load"."""
    src = _js()

    assert "could not load - close and reopen to retry" in src
    assert "delete box.parentNode.dataset.lazyLoaded" in src


def test_show_more_shows_the_press():
    """Wes: a button with no visible response gets pressed again. It is disabled
    and relabeled in the same turn as the click, so the second press is
    swallowed rather than queued."""
    src = _js()

    assert "b.disabled = true;" in src


def test_the_rail_still_lists_the_section(client, project):
    """The heading became a <summary>, and the rail builds its chapters from
    headings and jump targets - so without an explicit target this section
    disappears from the one list that claims to name every section on the page.
    """
    body = client.get("/").text

    assert 'data-jump="activity"' in body
    assert 'data-jump-label="Recent activity"' in body

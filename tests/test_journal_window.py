"""The project page carries a window onto the journal, not the whole thing.

Wes, 2026-08-28: "The slowness and the not scrolling back down happening on page
reload is a big problem. It also seems to take much longer to reload a page, and
my key commands to jump to a certain area of the page do not work when it is
taking this time to load. It is again very frustrating!"

Measured before any of this was written: the project page for the portal's own
meta-project was 596 KB of HTML, and 490 KB of that was two hundred rendered
journal entries. The jump keys are bound at DOMContentLoaded, which cannot fire
until the whole document has been parsed - so the keys were not broken during
the load, they did not exist yet.

Every assertion here is about a boundary. The caps are arithmetic (a flipped
comparison keeps the page working and quietly doubles its weight, which is
exactly the failure that started this), so they are checked at the edge rather
than in the comfortable middle.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import db, journalwindow


@pytest.fixture()
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def entry(chars: int = 100):
    return {"content_md": "x" * chars}


# --------------------------------------------------------------------------
# The window itself
# --------------------------------------------------------------------------

def test_a_short_journal_is_shown_whole_and_hides_nothing():
    shown, hidden = journalwindow.window([entry(), entry(), entry()])

    assert len(shown) == 3
    assert hidden == 0


def test_an_empty_journal_is_not_an_error():
    assert journalwindow.window([]) == ([], 0)


def test_the_entry_cap_holds_at_its_own_boundary():
    """Many small entries: the character budget never notices them, so the
    count is the only thing that can stop the page growing without limit."""
    at_cap = [entry(10) for _ in range(journalwindow.MAX_ENTRIES)]
    shown, hidden = journalwindow.window(at_cap)
    assert len(shown) == journalwindow.MAX_ENTRIES
    assert hidden == 0

    one_over = [entry(10) for _ in range(journalwindow.MAX_ENTRIES + 1)]
    shown, hidden = journalwindow.window(one_over)
    assert len(shown) == journalwindow.MAX_ENTRIES
    assert hidden == 1


def test_the_character_cap_stops_a_page_full_of_long_reports():
    """Ten entries is under the entry cap, so only the character budget can
    stop this. 20 KB an entry keeps the MIN_ENTRIES floor out of it, so what is
    measured here is the budget alone rather than the floor overriding it."""
    shown, hidden = journalwindow.window([entry(20_000) for _ in range(10)])

    assert len(shown) < 10
    assert sum(len(row["content_md"]) for row in shown) <= journalwindow.MAX_CHARS
    assert hidden == 10 - len(shown)


def test_the_character_cap_holds_at_its_own_boundary():
    """Exactly at the budget fits; one character more does not.

    Written as two runs of the same shape because the interesting mutation
    here is > vs >=, and only a pair either side of the line can see it."""
    size = journalwindow.MAX_CHARS // 10
    exactly = [entry(size) for _ in range(10)]
    shown, _ = journalwindow.window(exactly, max_entries=100)
    assert len(shown) == 10

    over_by_one = [entry(size) for _ in range(10)]
    over_by_one[-1] = entry(size + 1)
    shown, hidden = journalwindow.window(over_by_one, max_entries=100)
    assert len(shown) == 9
    assert hidden == 1


def test_the_minimum_beats_the_character_budget():
    """A run of long reports must still show something.

    Without the floor, a project whose newest entries are five 200 KB agent
    reports opens on an empty journal above a "show older" link - which reads
    as the journal being broken, not as it being long."""
    shown, hidden = journalwindow.window([entry(200_000) for _ in range(8)])

    assert len(shown) == journalwindow.MIN_ENTRIES
    assert hidden == 8 - journalwindow.MIN_ENTRIES


def test_one_entry_longer_than_the_whole_budget_is_still_shown():
    """The degenerate case of the floor: the newest entry is the page."""
    shown, hidden = journalwindow.window([entry(journalwindow.MAX_CHARS * 3)])

    assert len(shown) == 1
    assert hidden == 0


def test_the_window_is_taken_from_the_newest_end():
    """list_journal returns newest-first, and the newest entries are the ones
    worth the page weight. Taking the window from the wrong end would show a
    reader the oldest thirty entries on this project and call them the journal."""
    rows = [{"content_md": "x" * 10, "n": i} for i in range(50)]

    shown, hidden = journalwindow.window(rows)

    assert [row["n"] for row in shown] == list(range(journalwindow.MAX_ENTRIES))
    assert hidden == 50 - journalwindow.MAX_ENTRIES


def test_show_all_bypasses_both_caps():
    shown, hidden = journalwindow.window([entry(50_000) for _ in range(200)], show_all=True)

    assert len(shown) == 200
    assert hidden == 0


def test_the_window_is_contiguous_rather_than_skipping_big_entries():
    """A cheap entry AFTER an expensive one is not smuggled in.

    Skipping the entry that busts the budget and carrying on would leave a
    hole in the middle of the timeline while the page still said "show N
    older" - so the reader would be looking at a journal with something
    missing from it and no sign that anything was."""
    rows = [entry(10) for _ in range(journalwindow.MIN_ENTRIES)]
    rows.append(entry(journalwindow.MAX_CHARS + 1))
    rows.append(entry(10))

    shown, hidden = journalwindow.window(rows, max_entries=100)

    assert len(shown) == journalwindow.MIN_ENTRIES
    assert hidden == 2


def test_an_entry_with_no_readable_content_costs_nothing_rather_than_raising():
    """sqlite3.Row raises on a column it does not have, and the caps exist to
    bound a page - refusing to render a row because its length was unreadable
    would be a worse failure than rendering it."""
    shown, hidden = journalwindow.window([{"other": "x"}, {"content_md": None}])

    assert len(shown) == 2
    assert hidden == 0


# --------------------------------------------------------------------------
# The page, end to end
# --------------------------------------------------------------------------

@pytest.fixture()
def loaded(client, temp_data_dir):
    """A project with more journal than one page is allowed to carry."""
    project = db.create_project("Portal", description="x", stage="active", slug="portal")
    for i in range(journalwindow.MAX_ENTRIES + 12):
        db.add_journal(project["id"], "agent", "progress", f"entry number {i}")
    return project


def test_the_page_renders_a_window_and_offers_the_rest(client, loaded):
    body = client.get("/project/portal").text

    # Newest first: the last one written is on the page, the oldest is not.
    assert f"entry number {journalwindow.MAX_ENTRIES + 11}" in body
    assert "entry number 0" not in body
    assert "show 12 older entries" in body


def test_asking_for_all_of_it_gets_all_of_it(client, loaded):
    body = client.get("/project/portal?journal=all").text

    assert "entry number 0" in body
    assert f"entry number {journalwindow.MAX_ENTRIES + 11}" in body
    assert "show 12 older entries" not in body
    # And a way back to the fast page, or expanding it once is permanent for
    # anyone who bookmarked the address.
    assert "show fewer" in body


def test_show_older_sits_after_the_last_entry_not_before_the_first(client, loaded):
    """Wes, 2026-08-29: "for the 'show 170 older entries' stuff in the journal,
    move that to the very end of the scroll rather than the top."

    Asserted by position in the document rather than by presence, because
    presence is what the tests above already cover and position is the whole of
    what he asked for. Both the newest entry (the first one rendered) and the
    oldest one on the page (the last) have to come before the control, or it is
    only "further down", not "the very end".

    Anchored on `class="journal-more"` rather than on the label text, because
    the label text is not unique on a real page: the note in which Wes ASKED
    for this move contains the words "show 170 older entries", and it renders
    as a journal entry near the top of the very page the control sits at the
    bottom of. Searching for the words finds his sentence first."""
    body = client.get("/project/portal").text

    control = body.index('class="journal-more"')
    newest = body.index(f"entry number {journalwindow.MAX_ENTRIES + 11}")
    oldest_shown = body.index("entry number 12")

    assert newest < control
    assert oldest_shown < control
    # It is the control, and it is the only one.
    assert "show 12 older entries" in body[control:]
    assert body.count('class="journal-more"') == 1
    # Nothing of the journal feed may follow it.
    assert "journal-entry" not in body[control:]


def test_show_fewer_sits_at_the_end_too(client, loaded):
    """The expanded page's control is the same control in the same place: at
    the bottom of a 42-entry scroll, not floating above it."""
    body = client.get("/project/portal?journal=all").text

    control = body.index('class="journal-more"')

    assert body.index("entry number 0") < control
    assert "show fewer" in body[control:]
    assert "journal-entry" not in body[control:]


def test_a_short_journal_offers_no_link_at_all(client, temp_data_dir):
    """The control is noise on a project that has nothing hidden."""
    project = db.create_project("Small", description="x", stage="active", slug="small")
    db.add_journal(project["id"], "agent", "progress", "the only entry")

    body = client.get("/project/small").text

    assert "the only entry" in body
    assert "older entr" not in body
    assert "show fewer" not in body


def test_one_hidden_entry_is_named_in_the_singular(client, temp_data_dir):
    """"show 1 older entries" is the kind of thing nobody notices for a year."""
    project = db.create_project("Edge", description="x", stage="active", slug="edge")
    for i in range(journalwindow.MAX_ENTRIES + 1):
        db.add_journal(project["id"], "agent", "progress", f"entry number {i}")

    body = client.get("/project/edge").text

    assert "show 1 older entry" in body
    assert "older entries" not in body


def test_the_window_cuts_the_page_weight_it_was_built_to_cut(client, temp_data_dir):
    """The measurement the whole change exists for.

    A page of realistic agent reports (about 6 KB of markdown each, which is
    what this project's actually run) must not carry all of them. Asserted as a
    ratio rather than an absolute byte count so it does not go red every time
    someone adds a section to the page."""
    project = db.create_project("Heavy", description="x", stage="active", slug="heavy")
    for i in range(60):
        db.add_journal(project["id"], "agent", "progress", f"# entry {i}\n\n" + ("word " * 1200))

    windowed = len(client.get("/project/heavy").text)
    whole = len(client.get("/project/heavy?journal=all").text)

    assert windowed * 2 < whole, f"windowed {windowed} vs whole {whole}"

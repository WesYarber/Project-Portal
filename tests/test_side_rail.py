"""The desktop side rail.

Wes, 2026-08-01:

  "When on desktop with extra unused horizontal space, let's add a nav bar to
  the side. at the top, it should show a compressed status widget showing how
  many agents are active at the time and for what projects. Then, under that,
  it should show a list of active projects, then projects in review as well as
  their status. They should update in real time and allow you to click on them
  to jump to that project. I haven't decided yet if im ok with shifting the
  rest of the main interface here that already exists over to the right to make
  more space for this. Maybe we can try it and see if I like it? Would also be
  good to have one click access to any given tab/section in the project I'm
  in."

Four asks and one open question, and the open question is why `ui_sidebar` is a
setting: `margin` (the default) floats the rail in the empty space beside the
centered page so nothing moves, `beside` is the variant he described and pushes
the page right, `off` is the portal exactly as it was.

The rules that could go wrong quietly, and what owns them here:

- The rail and the dashboard must never disagree about which shelf a project is
  on. Both go through `db.shelf_of`, and the tests below drive the rail through
  the same states `tests/test_shelves.py` drives the board through.
- The rail is on EVERY page, so it must never be the reason one 500s.
- It carries project titles, so it must be scoped to the reader like the board.
- One status per row, in a precedence order - the row has space for one fact.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db, sidebar

ROOT = Path(__file__).resolve().parents[1]

TEMPLATES = config.BASE_DIR / "app" / "templates"
STATIC = config.BASE_DIR / "app" / "static"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _project(title, slug, stage="active", **kw):
    return db.create_project(title, description="x", stage=stage, slug=slug, **kw)


def _blocked(title, slug, why):
    row = _project(title, slug)
    db.update_project(row["id"], blocked_on=why)
    return db.get_project(row["id"])


# --- which projects the rail lists -----------------------------------------

def test_it_leads_with_active_and_review_and_tails_with_the_rest(temp_data_dir):
    # Wes, 2026-08-04: "Allow the page's unused vertical space on the side
    # nav-bar to be filled with additional projects if applicable." Paused and
    # backlog go to a dimmed "More" tail; done and abandoned stay off the rail.
    rows = [
        _project("Working", "working", stage="active", build_approved=True),
        _project("Waiting", "waiting", stage="review"),
        _project("Idea", "idea", stage="backlog"),
        _project("Finished", "finished", stage="done"),
        _project("Dropped", "dropped", stage="abandoned"),
    ]

    rail = sidebar.build(rows, mode="shelf")

    assert [s["name"] for s in rail["shelves"]] == ["active", "review", "more"]
    assert [r["slug"] for r in rail["shelves"][0]["rows"]] == ["working"]
    assert [r["slug"] for r in rail["shelves"][1]["rows"]] == ["waiting"]
    assert [r["slug"] for r in rail["shelves"][2]["rows"]] == ["idea"]
    assert rail["listed"] == 3


def test_an_empty_shelf_is_dropped_rather_than_drawn_empty(temp_data_dir):
    # "Hide what is empty, show what is live" - a heading with nothing under it
    # is chrome carrying no information, and the rail is nothing but chrome.
    rail = sidebar.build([_project("Waiting", "waiting", stage="review")], mode="shelf")

    assert [s["name"] for s in rail["shelves"]] == ["review"]


def test_a_project_being_worked_is_active_whatever_its_stage_says(temp_data_dir):
    # The rule that used to live inline in the dashboard route. A run in flight
    # outranks the stored stage, so a project an agent is working on cannot be
    # filed under "in review" on the one widget that is about work in flight.
    row = _project("Waiting", "waiting", stage="review")

    rail = sidebar.build([row], running_ids={row["id"]}, mode="shelf")

    assert [s["name"] for s in rail["shelves"]] == ["active"]


def test_a_run_never_drags_a_finished_project_back_onto_a_shelf(temp_data_dir):
    # `done` is Wes's word about the project. A late run on one - a cleanup, a
    # final report - does not undo it.
    row = _project("Finished", "finished", stage="done")

    rail = sidebar.build([row], running_ids={row["id"]})

    assert rail["shelves"] == []


def test_a_paused_project_is_in_the_more_tail_not_on_a_working_shelf(temp_data_dir):
    row = _project("Parked", "parked", stage="active")
    db.pause_project(row["id"])

    rail = sidebar.build([db.get_project(row["id"])], mode="shelf")

    assert [s["name"] for s in rail["shelves"]] == ["more"]
    assert [r["slug"] for r in rail["shelves"][0]["rows"]] == ["parked"]


def test_a_paused_project_says_it_is_paused_rather_than_only_a_date(temp_data_dir):
    """In recent mode a parked project sits in the same list as the work in
    flight, so the row has to explain why it is dimmed."""
    row = _project("Parked", "parked", stage="active")
    db.pause_project(row["id"])

    rail = sidebar.build([db.get_project(row["id"])])
    item = rail["shelves"][0]["rows"][0]

    assert item["status"] == "paused"
    assert item["dim"] is True


def test_a_question_on_a_paused_project_still_outranks_the_pause(temp_data_dir):
    """The pause is Wes's word about the work; the question is still answerable
    from his phone, so it is the fact the row spends its one line on."""
    row = _project("Parked", "parked", stage="active")
    db.pause_project(row["id"])

    rail = sidebar.build([db.get_project(row["id"])], question_counts={row["id"]: 1})

    assert rail["shelves"][0]["rows"][0]["status"] == "1 question"


def test_the_rail_shelves_a_project_exactly_as_the_dashboard_does(temp_data_dir):
    # The claim the whole design rests on, asserted directly rather than
    # inferred: two lists of "what is active" on one screen that disagree about
    # a project are worse than either being wrong alone, because nothing on the
    # page says which to believe.
    states = [
        _project("Plain", "plain", stage="active"),
        _project("Review", "review", stage="review"),
        _project("Backlog", "backlog", stage="backlog"),
        _project("Gated", "gated", stage="active", build_approved=False),
    ]
    db.update_project(states[-1]["id"], build_requested=1)
    rows = [db.get_project(p["id"]) for p in states]
    running = {rows[0]["id"]}

    for row in rows:
        assert db.shelf_of(row, 0, row["id"] in running) == (
            "active" if row["id"] in running else db.project_shelf(row, 0)
        )


# --- the status under a name, which is a precedence and not a list ----------

def test_working_now_outranks_everything(temp_data_dir):
    row = _blocked("Busy", "busy", "a key")

    assert sidebar.project_status(row, 2, running=True, gated=True) == (
        "working now",
        "working",
    )


def test_blocked_outranks_a_question(temp_data_dir):
    row = _blocked("Stuck", "stuck", "an API key")

    assert sidebar.project_status(row, 3) == ("blocked", "blocked")


def test_a_question_outranks_a_build_request(temp_data_dir):
    row = _project("Asking", "asking", stage="active")

    assert sidebar.project_status(row, 1, gated=True) == ("1 question", "asking")


def test_questions_are_counted_in_english(temp_data_dir):
    row = _project("Asking", "asking", stage="active")

    assert sidebar.project_status(row, 1)[0] == "1 question"
    assert sidebar.project_status(row, 4)[0] == "4 questions"


def test_a_build_request_is_the_last_thing_said(temp_data_dir):
    row = _project("Gated", "gated", stage="active")

    assert sidebar.project_status(row, 0, gated=True) == ("needs your OK", "gate")


def test_a_project_with_nothing_waiting_says_nothing(temp_data_dir):
    # The template falls back to how long it has been sitting, which is the
    # useful status on a review row and noise on one being worked.
    row = _project("Quiet", "quiet", stage="active")

    assert sidebar.project_status(row, 0) == ("", "")


def test_whitespace_in_blocked_on_is_not_a_blockage(temp_data_dir):
    row = _blocked("Clear", "clear", "   ")

    assert sidebar.project_status(row, 0) == ("", "")


# --- the status widget ------------------------------------------------------

def test_it_counts_and_names_the_projects_being_worked(temp_data_dir):
    snapshot = {
        "runs": [
            {"run_id": 7, "project_id": 1, "project_slug": "a", "project_title": "Alpha", "elapsed": "3m"},
            {"run_id": 8, "project_id": 2, "project_slug": "b", "project_title": "Beta", "elapsed": "1m"},
        ]
    }

    runs = sidebar.visible_runs(snapshot, {1, 2})

    assert [r["label"] for r in runs] == ["Alpha", "Beta"]
    assert [r["href"] for r in runs] == ["/project/a", "/project/b"]
    assert sidebar.build([], runs=runs)["running"] == 2


def test_a_run_on_somebody_elses_project_is_not_named_in_your_chrome(temp_data_dir):
    # The rail is on every page, so an unscoped one would announce what
    # everybody else is working on from the chrome of all of them.
    snapshot = {
        "runs": [
            {"run_id": 7, "project_id": 1, "project_slug": "a", "project_title": "Mine"},
            {"run_id": 8, "project_id": 9, "project_slug": "h", "project_title": "Hers"},
        ]
    }

    runs = sidebar.visible_runs(snapshot, {1})

    assert [r["label"] for r in runs] == ["Mine"]


def test_a_one_off_session_still_counts_as_an_agent_working(temp_data_dir):
    # A one-off carries no project_id, so a membership test drops every one of
    # them - and the widget would then say "no agents working" with an agent
    # visibly working, which is the one thing it must never do.
    snapshot = {"runs": [{"run_id": 3, "project_id": None, "oneoff_id": 12, "project_title": "a chore"}]}

    runs = sidebar.visible_runs(snapshot, set(), admin=True)

    assert len(runs) == 1
    assert runs[0]["href"] == "/tasks/12"


def test_a_one_off_is_not_shown_to_anybody_but_the_owner(temp_data_dir):
    snapshot = {"runs": [{"run_id": 3, "project_id": None, "oneoff_id": 12, "project_title": "a chore"}]}

    assert sidebar.visible_runs(snapshot, set(), admin=False) == []


# --- knowing where you are --------------------------------------------------

def test_it_marks_the_project_you_are_looking_at(temp_data_dir):
    rows = [_project("Here", "here"), _project("There", "there")]

    rail = sidebar.build(rows, path="/project/here")

    marked = {r["slug"]: r["current"] for r in rail["shelves"][0]["rows"]}
    assert marked == {"here": True, "there": False}


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/project/here", "here"),
        ("/project/here/todos/history", "here"),
        ("/", ""),
        ("/settings", ""),
        ("", ""),
        ("/project", ""),
    ],
)
def test_the_current_slug_comes_off_the_path(path, expected):
    assert sidebar.current_slug(path) == expected


# --- it is chrome, and chrome may never break a page ------------------------

def test_a_broken_rail_costs_the_rail_and_not_the_page(client, monkeypatch):
    from app import main

    # Patched on the rail's own collaborator rather than on `scope`, which the
    # nav badge also calls: the point is that a rail failure costs the rail, and
    # a patch that breaks half the page proves nothing about that.
    monkeypatch.setattr(main.sidebar, "build", lambda *a, **kw: 1 / 0)

    assert main.side_rail() == sidebar.empty()
    assert client.get("/").status_code == 200


def test_every_page_carries_the_rail(client, temp_data_dir):
    _project("Working", "working", stage="active")

    for path in ("/", "/project/working", "/settings", "/questions", "/activity"):
        body = client.get(path).text
        assert 'id="side-rail"' in body, path


def test_the_rail_lists_the_project_on_the_dashboard(client, temp_data_dir):
    _project("Rail Test Project", "rail-test", stage="active")

    body = client.get("/").text

    assert 'id="rail-shelf-recent"' in body
    assert "Rail Test Project" in body
    assert 'href="/project/rail-test"' in body


def test_an_idle_portal_says_so_rather_than_showing_a_bare_zero(client, temp_data_dir):
    assert "no agents working" in client.get("/").text


# --- the layout switch ------------------------------------------------------

def test_the_setting_offers_all_three_placements(temp_data_dir):
    values = [v for v, _ in config.APPEARANCE_CHOICES["ui_sidebar"]]

    assert values == ["margin", "beside", "off"]
    # Wes, 2026-08-04: "update the sidebar options naming convention. For the
    # 'on' options, one should be 'On - interface shift' and 'On - use existing
    # space' with existing space being the default."
    assert config.APPEARANCE_DEFAULTS["ui_sidebar"] == "margin"

    labels = dict(config.APPEARANCE_CHOICES["ui_sidebar"])
    assert labels["margin"] == "on - use existing space"
    assert labels["beside"] == "on - interface shift"
    # Named for what each does to the page, which is the only difference a
    # person can feel. Nothing in the label describes where the rail lands.
    assert "margin" not in labels["margin"]
    assert "beside" not in labels["beside"]


def test_the_placement_rides_the_body_class(client, temp_data_dir):
    assert config.APPEARANCE_CLASS_PREFIX["ui_sidebar"] == "rail"
    assert 'rail-margin' in client.get("/").text


def test_the_appearance_form_declares_the_field(client, temp_data_dir):
    # A field missing from its section's `_fields` is dropped on save, which
    # reads exactly like the setting refusing to stick. Asserted against the
    # RENDERED page, not the template source: the control comes out of the
    # `select_field` macro, so the source only ever contains `name="{{ key }}"`.
    html = client.get("/settings").text

    assert 'name="ui_sidebar"' in html
    # The appearance form's `_fields`, found by the panel it lives in - the
    # settings page has one form per section and each declares its own list, so
    # taking the first match on the page reads the agent panel's.
    panel = html.split('id="panel-appearance"')[1]
    declared = panel.split('name="_fields" value="')[1].split('"')[0].split(",")
    assert "ui_sidebar" in declared


def test_it_saves_as_a_personal_choice(client, temp_data_dir):
    from app import people

    client.post(
        "/settings",
        data={"_section": "appearance", "_fields": "ui_sidebar", "ui_sidebar": "beside"},
        follow_redirects=False,
    )

    assert people.appearance_of(people.owner())["ui_sidebar"] == "beside"
    assert "rail-beside" in client.get("/").text


def test_a_nonsense_placement_is_refused(client, temp_data_dir):
    from app import people

    client.post(
        "/settings",
        data={"_section": "appearance", "_fields": "ui_sidebar", "ui_sidebar": "everywhere"},
        follow_redirects=False,
    )

    # What matters is that it never reaches a page: a value that got through
    # would be painted on <body> as `rail-everywhere`, match no rule in the
    # stylesheet, and present as the setting saving and then doing nothing.
    # (The form coerces it to the default on the way in, so what is stored is a
    # real placement rather than nothing - either is fine, junk on <body> is
    # not.)
    assert people.appearance_of(people.owner()).get("ui_sidebar", "margin") in {
        v for v, _ in config.APPEARANCE_CHOICES["ui_sidebar"]
    }
    assert "rail-everywhere" not in client.get("/").text


# --- the CSS, which is where the two placements actually differ -------------

def test_neither_placement_shows_a_rail_on_a_phone(temp_data_dir):
    # The markup is on every page because the server cannot know the window
    # width. The width test therefore has to be in the stylesheet, and the rail
    # has to start hidden - or a phone gets a fixed panel over the page.
    css = (STATIC / "style.css").read_text()

    assert "#side-rail { display: none; }" in css
    body = css.split("/* The desktop side rail")[1]
    for rule in ("body.rail-margin #side-rail", "body.rail-beside #side-rail"):
        before = body.split(rule)[0]
        assert "@media (min-width:" in before, rule


def test_a_rail_appears_on_the_window_wes_actually_uses(temp_data_dir):
    # 2026-08-01, on a rail that had shipped the day before: "Is it not deployed
    # yet? It is not active for me." His window is about 1135px - measured off
    # the usage-tab screenshot he sent the same night, which reproduces at that
    # width - and the first cut of this feature started at 1360. A rail nobody's
    # window is wide enough for is a rail that does not exist, so the breakpoint
    # is the feature, not a detail of it.
    css = (STATIC / "style.css").read_text().split("/* The desktop side rail")[1]

    shown = [
        int(block.split("px)")[0])
        for block in css.split("@media (min-width: ")[1:]
        if "#side-rail { display: flex" in block or "#side-rail { display: flex;" in block
    ]
    assert shown, "no breakpoint reveals the rail at all"
    assert min(shown) <= 1135, f"the rail never appears on a 1135px window: {shown}"


def test_the_margin_placement_never_moves_the_page(temp_data_dir):
    """Wes, 2026-08-01: "The left column nav bar does not respect the setting
    telling it not to shift over the main window."

    "Use existing space" only exists once there is space to use, and it never
    touches <body>'s padding at any width. Buying a rail at 1135px by quietly
    pushing the page is the thing he caught."""
    css = (STATIC / "style.css").read_text().split("/* The desktop side rail")[1]

    narrow = css.split("@media (min-width: 1100px)")[1].split("@media")[0]
    assert "rail-margin" not in narrow

    wide = css.split("@media (min-width: 1400px)")[1]
    assert "body.rail-margin #side-rail" in wide
    # The rail floats in the margin; <body> is not mentioned, so the page keeps
    # the place it had.
    assert "padding-left" not in wide


def test_the_margin_placement_shrinks_to_whatever_margin_there_is(temp_data_dir):
    """Wes, 2026-08-04: "I want it to be more flexible for the 'Use existing
    space' version to be able to be more narrow to still apply and use the space
    to the left of the interface."

    It used to demand a full --rail-w and hide itself below 1620px when it could
    not have one. Now it is pinned by its right edge against the content column
    and takes whatever is left, capped at --rail-w - so between 1400 and 1620 he
    gets a narrower rail instead of no rail."""
    css = (STATIC / "style.css").read_text().split("/* The desktop side rail")[1]
    wide = css.split("@media (min-width: 1400px)")[1]

    # Pinned by the right edge - that is what lets the width vary at all.
    assert "right: calc(50% + 540px + var(--rail-margin-gap))" in wide
    assert "left: auto" in wide
    # ...and capped, so a very wide window gets the rail it always had rather
    # than a 400px column of names.
    assert "min(" in wide and "var(--rail-w)" in wide
    assert "50vw - 540px" in wide


def test_the_margin_rail_is_positioned_off_the_stylesheet_the_portal_serves():
    """540 is half of `.screen`'s max-width, and the trap is that TWO files
    define `.screen`.

    static/terminal-theme.css says 1100px - and the portal does not load it.
    It is the reusable terminal-style skill's sheet; base.html links style.css
    and themes.css and nothing else, so the rule that applies is the one in
    style.css, at 1080px. A rail positioned off the other file's number sits
    10px away from where its own comment says it does, and no test that reads
    terminal-theme.css can tell."""
    css = (STATIC / "style.css").read_text()
    base = (TEMPLATES / "base.html").read_text()

    assert ".screen { max-width: 1080px; margin: 0 auto; }" in css
    assert "terminal-theme.css" not in base
    rail = css.split("/* The desktop side rail")[1]
    assert "550px" not in rail


def test_the_page_sits_against_the_rail_rather_than_centering_beside_it(temp_data_dir):
    """The other half of the same note: "on neither setting does the main window
    come as close to the left side bar as it could and should."

    `.screen` is `max-width: 1080px; margin: 0 auto`, so it centers in whatever
    space is left - and the padding that makes room for the rail is part of what
    it centers in. On a 1920px window that parked the page ~290px away from the
    rail it is supposed to sit beside."""
    css = (STATIC / "style.css").read_text().split("/* The desktop side rail")[1]
    narrow = css.split("@media (min-width: 1100px)")[1].split("@media")[0]

    assert "body.rail-beside" in narrow
    assert "padding-left: calc(var(--rail-w)" in narrow
    assert "body.rail-beside .screen { margin-left: 0; }" in narrow


def test_the_rail_scrolls_rather_than_capping_its_list(temp_data_dir):
    # A rail that silently stopped at ten projects would read as "that is all of
    # them". `scroll-cap` in the markup is also what makes a live patch put the
    # scroll position back where the reader left it (SCROLL_SEL in app.js).
    assert "overflow-y: auto" in (STATIC / "style.css").read_text()
    # The scroller is the project list itself since 2026-08-01, so it can take
    # the room left under the status widget and the chapters rather than the
    # whole rail scrolling as one.
    assert 'class="rail-projects scroll-cap" id="rail-projects"' in (TEMPLATES / "base.html").read_text()


# --- the chapter list -------------------------------------------------------

def test_the_project_page_declares_a_chapter_for_each_section(client, temp_data_dir):
    p = _project("Chapters", "chapters", stage="active")
    db.create_question(p["id"], "is this a chapter?")

    body = client.get("/project/chapters").text

    for name in ("ask", "project", "questions", "todo", "note", "journal"):
        assert f'data-jump="{name}"' in body, name


def test_the_journal_declares_two_targets_and_one_chapter(client, temp_data_dir):
    # The J key needs both the scrolling box and the heading above it, because a
    # project with no entries renders no box - but they are one place to go, so
    # the box is kept out of the chapter list.
    body = (TEMPLATES / "project.html").read_text()

    assert 'data-jump="journal-box"' in body
    assert 'data-jump-nav="off"' in body


def test_a_chapter_is_labeled_when_its_own_text_is_not_a_name(temp_data_dir):
    # The ask block is a <details> holding a whole form; the project card is a
    # whole page section. Neither's text content is a nav label.
    body = (TEMPLATES / "project.html").read_text()

    assert 'data-jump-label="Ask"' in body
    assert 'data-jump-label="Overview"' in body


def test_the_chapter_list_is_rebuilt_after_a_live_patch(temp_data_dir):
    # The morph resets this element to the server's copy, which is empty and
    # hidden - so without the reinit() call the chapter list disappears the
    # first time anything in the database changes, i.e. within seconds.
    src = (STATIC / "app.js").read_text()

    assert "railChapters" in src
    reinit = src.split("function reinit()")[1].split("\n}")[0]
    assert "railChapters" in reinit


def test_the_server_renders_the_chapter_slot_empty_and_hidden(temp_data_dir):
    assert 'id="rail-chapters" hidden' in (TEMPLATES / "base.html").read_text()


def test_a_chapter_click_scrolls_rather_than_navigates(temp_data_dir):
    # Delegated, because the list is rebuilt wholesale after every live patch
    # and a listener bound to the old nodes would go with them.
    src = (STATIC / "app.js").read_text()

    assert 'closest("#rail-chapters a")' in src
    handler = src.split('closest("#rail-chapters a")')[1].split("});")[0]
    # preventDefault only AFTER the target is found: a chapter whose section has
    # gone should be an ordinary link, not a link that does nothing.
    assert handler.index("if (!el) return;") < handler.index("ev.preventDefault();")


def test_rows_is_the_key_not_items(temp_data_dir):
    # Jinja resolves a dotted name to the attribute first, and every dict has an
    # `.items` method - so `shelf.items` in a template silently yields a bound
    # method, and `|length` on it raises from inside base.html on every page in
    # the portal. This cost a 241-test failure once.
    shelf = sidebar.build([_project("A", "a")])["shelves"][0]

    assert "items" not in shelf
    assert isinstance(shelf["rows"], list)


# --- what the list is, and the digits that reach it -------------------------
# Wes, 2026-08-01: "On this side bar here and in projects, have it show as many
# of the most recent projects that have been worked on as will fit on the
# remainder of the screen (without having to scroll). Allow this section to be
# scrolled to view older ones, and have a section in settings to change this
# from recent back to kind of what we have now which is just based on status.
# Allow me to press number keys to jump to the projects over there based on
# their order on the list."

def _touched(title, slug, when, **kw):
    # Written straight to the column: db.update_project stamps `updated_at` with
    # now() on every call, which is exactly the behavior that makes it "when this
    # was last worked on" and exactly why it cannot be used to fake a date.
    row = _project(title, slug, **kw)
    conn = db.get_conn()
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (when, row["id"]))
    conn.commit()
    return db.get_project(row["id"])


def test_recent_is_one_list_newest_first(temp_data_dir):
    rows = [
        _touched("Oldest", "oldest", "2026-07-01T00:00:00+00:00"),
        _touched("Newest", "newest", "2026-08-01T00:00:00+00:00"),
        _touched("Middle", "middle", "2026-07-15T00:00:00+00:00", stage="review"),
    ]

    rail = sidebar.build(rows, mode="recent")

    assert [s["name"] for s in rail["shelves"]] == ["recent"]
    assert [r["slug"] for r in rail["shelves"][0]["rows"]] == [
        "newest", "middle", "oldest",
    ]


def test_recent_is_the_default(temp_data_dir):
    assert config.APPEARANCE_DEFAULTS["ui_rail_projects"] == "recent"
    assert sidebar.build([_project("A", "a")])["mode"] == "recent"


def test_recent_ignores_the_order_it_was_handed(temp_data_dir):
    """The caller passes the reader's own dashboard order, which is usually by
    priority. "Recently worked on" is a different question and this has to
    answer it rather than inherit the answer to the other one."""
    rows = [
        _touched("High Priority", "high", "2026-07-01T00:00:00+00:00"),
        _touched("Just Touched", "touched", "2026-08-01T00:00:00+00:00"),
    ]

    rail = sidebar.build(rows, mode="recent")

    assert rail["shelves"][0]["rows"][0]["slug"] == "touched"


def test_recent_is_one_list_and_status_decides_nothing_about_where_a_row_lands(
    temp_data_dir,
):
    """Wes, 2026-08-15: "I also want the projects shown along the side to be
    sorted by what was most recently run or modified."

    It was not, quite. Recent mode sorted by date *within* the working shelves
    and pushed paused and backlogged projects into a "More" tail behind all of
    them - so on the day he wrote that, ProxyTable (paused, run an hour
    earlier) sat ten rows down under projects last touched a week before. In
    recent mode every unfinished project is now in one list in date order;
    grouping by status is what the shelf mode beside it is for.

    The finished stay off the rail entirely, in both modes."""
    rows = [
        _touched("Idea", "idea", "2026-08-05T00:00:00+00:00", stage="backlog"),
        _touched("Working", "working", "2026-08-04T00:00:00+00:00"),
        _touched("Waiting", "waiting", "2026-08-02T00:00:00+00:00", stage="review"),
        _touched("Finished", "finished", "2026-08-06T00:00:00+00:00", stage="done"),
    ]
    paused = _touched("Put Down", "putdown", "2026-08-03T00:00:00+00:00")
    db.pause_project(paused["id"])
    # Pausing stamps `updated_at`, so the date has to be written back after it.
    conn = db.get_conn()
    conn.execute(
        "UPDATE projects SET updated_at = ? WHERE id = ?",
        ("2026-08-03T00:00:00+00:00", paused["id"]),
    )
    conn.commit()
    rows.append(db.get_project(paused["id"]))

    rail = sidebar.build(rows, mode="recent")

    assert [s["name"] for s in rail["shelves"]] == ["recent"]
    assert [r["slug"] for r in rail["shelves"][0]["rows"]] == [
        "idea", "working", "putdown", "waiting",
    ]


def test_a_project_appears_once_in_the_recent_list(temp_data_dir):
    """The tail and the list are two views of the same rows in recent mode, so
    drawing both would list every parked project twice."""
    paused = _project("Put Down", "putdown")
    db.pause_project(paused["id"])

    rail = sidebar.build([db.get_project(paused["id"])], mode="recent")

    slugs = [r["slug"] for s in rail["shelves"] for r in s["rows"]]
    assert slugs == ["putdown"]


def test_shelf_mode_still_groups_by_status(temp_data_dir):
    """The other half of the setting, and the reason recent mode is free to be
    strictly chronological: somebody who wants status grouping has a place to
    say so (Settings > appearance > rail project list)."""
    rows = [
        _touched("Idea", "idea", "2026-08-05T00:00:00+00:00", stage="backlog"),
        _touched("Working", "working", "2026-08-04T00:00:00+00:00"),
    ]

    rail = sidebar.build(rows, mode="shelf")

    assert [s["name"] for s in rail["shelves"]] == ["active", "more"]
    assert [r["slug"] for r in rail["shelves"][0]["rows"]] == ["working"]


# --- the "More" tail ---------------------------------------------------------
# Wes, 2026-08-04: "Allow the page's unused vertical space on the side nav-bar
# to be filled with additional projects if applicable." The working shelves
# lead; whatever is not finished tails behind them, dimmed, inside the same
# scrolling list - so on a tall window the leftover height holds projects
# instead of nothing, and on a short one the extras are simply in the overflow.

def test_the_more_tail_is_most_recently_touched_first(temp_data_dir):
    rows = [
        _touched("Old Idea", "old-idea", "2026-06-01T00:00:00+00:00", stage="backlog"),
        _touched("New Idea", "new-idea", "2026-08-01T00:00:00+00:00", stage="backlog"),
    ]

    rail = sidebar.build(rows, mode="shelf")

    assert [r["slug"] for r in rail["shelves"][-1]["rows"]] == ["new-idea", "old-idea"]


def test_the_more_tail_shares_the_overall_row_budget(temp_data_dir):
    """A long backlog cannot crowd the markup: the tail takes what is left of
    RAIL_MAX_ROWS after the working shelves, never more."""
    rows = [_project(f"A{i}", f"a{i}") for i in range(sidebar.RAIL_MAX_ROWS - 2)]
    rows += [_project(f"B{i}", f"b{i}", stage="backlog") for i in range(6)]

    rail = sidebar.build(rows, mode="shelf")

    assert sum(len(s["rows"]) for s in rail["shelves"]) == sidebar.RAIL_MAX_ROWS
    assert len(rail["shelves"][-1]["rows"]) == 2


def test_a_full_rail_has_no_more_tail_at_all(temp_data_dir):
    rows = [_project(f"A{i}", f"a{i}") for i in range(sidebar.RAIL_MAX_ROWS)]
    rows.append(_project("Idea", "idea", stage="backlog"))

    rail = sidebar.build(rows, mode="shelf")

    assert [s["name"] for s in rail["shelves"]] == ["active"]


def test_the_digits_run_on_into_the_more_tail(temp_data_dir):
    """The number keys jump by position on the visible list, and the tail is
    part of that list - digit 2 must mean the second row a person counts."""
    rows = [
        _project("Working", "working"),
        _project("Idea", "idea", stage="backlog"),
    ]

    rail = sidebar.build(rows, mode="shelf")

    assert rail["shelves"][0]["rows"][0]["digit"] == "1"
    assert rail["shelves"][1]["rows"][0]["digit"] == "2"


def test_the_more_tail_is_dimmed_as_a_group():
    css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
    assert "#rail-shelf-more .rail-name" in css


def test_a_parked_project_is_dimmed_one_row_at_a_time_too():
    """Recent mode has no "More" group to dim - the parked projects are sorted
    in among the live ones by date - so the row carries the dimming itself."""
    css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
    html = TEMPLATES.joinpath("base.html").read_text(encoding="utf-8")

    assert ".rail-section li.dim .rail-name" in css
    assert "'dim' if item.dim" in html


def test_the_first_ten_rows_carry_a_digit(temp_data_dir):
    rows = [_project(f"P{n}", f"p{n}") for n in range(12)]

    listed = sidebar.build(rows, mode="recent")["shelves"][0]["rows"]

    assert [r["digit"] for r in listed[:10]] == list("1234567890")
    # Ten digits, ten rows. The eleventh is still listed and still clickable -
    # it just has no key, which is the honest thing for a row there is no key
    # for.
    assert [r["digit"] for r in listed[10:]] == ["", ""]


def test_the_digits_run_across_the_shelves_rather_than_restarting(temp_data_dir):
    """In shelf mode the numbering runs through Active and on into In review,
    because a person counting down the rail counts the rows they can see, not
    the rows under one heading."""
    rows = [
        _project("Working", "working"),
        _project("Waiting", "waiting", stage="review"),
    ]

    shelves = sidebar.build(rows, mode="shelf")["shelves"]

    assert shelves[0]["rows"][0]["digit"] == "1"
    assert shelves[1]["rows"][0]["digit"] == "2"


def test_the_list_is_cut_at_a_length_no_window_can_show(temp_data_dir):
    """"As many as will fit" is a fact about the window and is decided in CSS.
    This cap only stops the markup growing without bound; at ~2.4rem a row it is
    around 1000px of list, more than any window has left."""
    rows = [_project(f"P{n:03d}", f"p{n:03d}") for n in range(sidebar.RAIL_MAX_ROWS + 5)]

    rail = sidebar.build(rows, mode="recent")

    assert len(rail["shelves"][0]["rows"]) == sidebar.RAIL_MAX_ROWS
    assert rail["listed"] == sidebar.RAIL_MAX_ROWS


def test_the_setting_offers_both_lists(temp_data_dir):
    assert [v for v, _ in config.APPEARANCE_CHOICES["ui_rail_projects"]] == [
        "recent", "shelf",
    ]
    # Not a body class: the list is built on the server, so there is nothing for
    # the browser to preview and a class that changed nothing on screen would
    # read as the setting not working.
    assert "ui_rail_projects" not in config.APPEARANCE_CLASS_PREFIX


def test_the_setting_is_on_the_appearance_form(client, temp_data_dir):
    html = client.get("/settings").text
    panel = html.split('id="panel-appearance"')[1]

    assert 'name="ui_rail_projects"' in panel
    declared = panel.split('name="_fields" value="')[1].split('"')[0].split(",")
    assert "ui_rail_projects" in declared


def test_switching_to_shelf_brings_the_headings_back(client, temp_data_dir):
    _project("Working", "working")
    assert 'id="rail-shelf-recent"' in client.get("/").text

    client.post(
        "/settings",
        data={
            "_section": "appearance",
            "_fields": "ui_rail_projects",
            "ui_rail_projects": "shelf",
        },
        follow_redirects=False,
    )

    assert 'id="rail-shelf-active"' in client.get("/").text


def test_a_row_advertises_the_digit_that_reaches_it(client, temp_data_dir):
    _project("Rail Test Project", "rail-test")

    body = client.get("/").text

    assert 'data-rail-digit="1"' in body
    assert '<span class="rail-digit">1</span>' in body


def test_a_digit_navigates_only_while_the_rail_is_on_screen(temp_data_dir):
    """Below its placement's breakpoint, and on the `off` setting, `#side-rail`
    is display:none - and a digit that navigated somewhere invisible would be a
    key that teleports you for no reason you can see.

    The test for that is getClientRects().length, and it must NOT be
    offsetParent: the spec says offsetParent is null whenever an element's own
    computed position is `fixed`, which the rail always is. The first cut used
    offsetParent, so the gate answered "not on screen" at every width on every
    setting and the number keys had never once worked. Probed in a real browser
    on 2026-08-04: offsetParent null, offsetWidth 183, one client rect."""
    src = (STATIC / "app.js").read_text()
    fn = src.split("function railDigitTarget(key)")[1].split("\n}")[0]

    assert "getClientRects()" in fn
    assert "offsetParent" not in fn
    assert 'data-rail-digit="' in fn

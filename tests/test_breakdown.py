"""Per-project usage breakdown, and the cancel controls in the web UI."""
from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from app import config, db, usage

from tests.test_history import _run, client, project  # noqa: F401 - fixture reuse


# --------------------------------------------------------------------------
# share_bar
# --------------------------------------------------------------------------

def test_share_bar_is_proportional():
    assert usage.share_bar(1.0, 8) == "█" * 8
    assert usage.share_bar(0.5, 8) == "█" * 4 + "·" * 4
    assert usage.share_bar(0.0, 8) == "·" * 8


def test_a_tiny_share_still_shows_one_block():
    """Rounding a 1% share to zero blocks would render "spent nothing" for a
    project that did in fact spend something."""
    assert usage.share_bar(0.01, 16).startswith("█")
    assert len(usage.share_bar(0.01, 16)) == 16


def test_share_bar_clamps_out_of_range_input():
    assert usage.share_bar(2.0, 4) == "████"
    assert usage.share_bar(-1.0, 4) == "····"


# --------------------------------------------------------------------------
# by_project
# --------------------------------------------------------------------------

def _row(project_id, status="ok", cost=0.0, turns=1):
    return {
        "project_id": project_id,
        "status": status,
        "cost_usd": cost,
        "num_turns": turns,
        "started_at": "2026-07-21T10:00:00+00:00",
        "ended_at": "2026-07-21T10:05:00+00:00",
    }


NAMES = {1: {"title": "Alpha", "slug": "alpha"}, 2: {"title": "Beta", "slug": "beta"}}


def test_groups_are_ranked_by_cost_not_run_count():
    """One long run can outweigh several cheap ones; ranking by count would
    point at the wrong project."""
    rows = [_row(1, cost=5.0), _row(2, cost=0.1), _row(2, cost=0.1), _row(2, cost=0.1)]
    groups = usage.by_project(rows, NAMES)
    assert [g["title"] for g in groups] == ["Alpha", "Beta"]
    assert groups[0]["share"] == 94.3
    assert groups[1]["runs"] == 3


def test_shares_add_up_to_a_hundred():
    rows = [_row(1, cost=1.0), _row(2, cost=3.0)]
    groups = usage.by_project(rows, NAMES)
    assert round(sum(g["share"] for g in groups)) == 100


def test_projectless_reflect_runs_get_their_own_group():
    """Dropping them would leave the shares not adding up to the window total."""
    groups = usage.by_project([_row(1, cost=1.0), _row(None, cost=1.0)], NAMES)
    titles = {g["title"] for g in groups}
    assert "memory / reflect" in titles
    assert next(g for g in groups if g["project_id"] is None)["slug"] == ""


def test_a_deleted_or_unknown_project_id_still_renders():
    groups = usage.by_project([_row(99, cost=1.0)], NAMES)
    assert groups[0]["title"] == "project #99"


def test_shares_fall_back_to_run_count_when_nothing_has_a_cost():
    """Older runs predate cost recording; an all-zero window should still rank
    projects rather than draw every bar empty."""
    groups = usage.by_project([_row(1), _row(2), _row(2)], NAMES)
    beta = next(g for g in groups if g["title"] == "Beta")
    assert beta["share"] == 66.7
    assert "█" in beta["bar"]


def test_group_success_rate_ignores_canceled_runs():
    groups = usage.by_project([_row(1, "ok"), _row(1, "cancelled"), _row(1, "error")], NAMES)
    assert groups[0]["cancelled"] == 1
    assert groups[0]["failed"] == 1
    assert groups[0]["success_rate"] == 50.0


def test_by_project_on_no_runs_is_empty():
    assert usage.by_project([], NAMES) == []


def test_history_includes_the_breakdown(project):
    _run(project["id"], cost=0.4)
    payload = usage.history(7)
    assert payload["by_project"][0]["title"] == "History Project"
    assert payload["by_project"][0]["cost"] == 0.4


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def test_activity_page_shows_the_breakdown(client, project):
    _run(project["id"], cost=0.4)
    body = client.get("/activity").text
    assert "By project" in body
    assert "share" in body


def test_activity_page_renders_cost_as_weight_by_default(client, project):
    _run(project["id"], cost=0.4)
    body = client.get("/activity").text
    assert "0.400w" in body
    assert "$0.400" not in body
    assert "not a bill" in body  # the explanatory note


def test_activity_page_renders_dollars_when_asked(client, project):
    db.set_setting("cost_units", "usd")
    _run(project["id"], cost=0.4)
    body = client.get("/activity").text
    assert "$0.400" in body
    assert "0.400w" not in body
    assert "not a bill" not in body


def test_run_page_and_scoped_activity_follow_the_units_setting(client, project):
    """This used to also check the project page, which carried its own copy of
    the runs table. That table is gone (Wes: "get rid of the Runs section");
    the project page now links to this scoped activity view instead."""
    run_id = _run(project["id"], cost=0.4)
    assert "0.400w" in client.get(f"/run/{run_id}").text
    assert "0.400w" in client.get(f"/activity?project={project['slug']}").text


def test_breakdown_is_hidden_when_scoped_to_one_project(client, project):
    _run(project["id"], cost=0.4)
    body = client.get("/activity?project=history-project").text
    assert "By project" not in body


def test_cancel_button_appears_only_while_a_run_is_running(client, project):
    running = _run(project["id"], status="running")
    assert f"/run/{running}/cancel" in client.get(f"/run/{running}").text
    assert f"/run/{running}/cancel" in client.get("/project/history-project").text
    assert f"/run/{running}/cancel" in client.get("/").text

    finished = _run(project["id"], status="ok")
    assert f"/run/{finished}/cancel" not in client.get(f"/run/{finished}").text


def test_cancel_route_settles_the_run_and_redirects_back(client, project):
    run_id = _run(project["id"], status="running")
    resp = client.post(
        f"/run/{run_id}/cancel",
        data={"next": "/project/history-project"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/project/history-project"
    assert db.get_run(run_id)["status"] == "cancelled"


@pytest.mark.parametrize("target", ["https://evil.example/x", "//evil.example", "javascript:x"])
def test_cancel_never_redirects_off_site(client, project, target):
    run_id = _run(project["id"], status="running")
    resp = client.post(
        f"/run/{run_id}/cancel", data={"next": target}, follow_redirects=False
    )
    assert resp.headers["location"] == "/"


def test_canceling_an_unknown_run_is_harmless(client):
    resp = client.post("/run/4242/cancel", data={"next": "/"}, follow_redirects=False)
    assert resp.status_code == 303


# --------------------------------------------------------------------------
# prompt_sizes
# --------------------------------------------------------------------------
#
# Wes, when the prompt-budget work was proposed: "I'm not sure what all of this
# means to be honest. I will trust you to make the right call on it." The call
# was byte budgets, and this is the panel that lets him check the call was
# right without taking anybody's word for it.

def _sized(project_id, prompt_bytes):
    row = _row(project_id)
    row["prompt_bytes"] = prompt_bytes
    return row


def test_prompt_sizes_averages_only_the_runs_that_recorded_one():
    """The column landed 2026-07-28, so every older run has NULL in it.
    Averaging those in as zero would show the prompt halving on exactly the day
    the portal started measuring it."""
    rows = [_sized(1, 100 * 1024), _sized(1, 50 * 1024), _row(1)]
    out = usage.prompt_sizes(rows, NAMES)
    assert out["runs"] == 2
    assert out["avg_kb"] == 75.0


def test_a_zero_byte_prompt_does_not_count_as_measured():
    """`prompt_bytes = 0` is a run that failed before it built one, not a run
    with an empty prompt."""
    assert usage.prompt_sizes([_sized(1, 0)], NAMES)["runs"] == 0


def test_the_largest_prompt_is_reported_not_just_the_average():
    out = usage.prompt_sizes([_sized(1, 10 * 1024), _sized(1, 200 * 1024)], NAMES)
    assert out["max_kb"] == 200.0


def test_projects_are_ranked_by_average_prompt_size():
    """Which project builds the biggest prompt is the actionable half - it is
    the one whose journal or checklist has outgrown its budget."""
    rows = [_sized(1, 10 * 1024), _sized(2, 100 * 1024), _sized(2, 100 * 1024)]
    out = usage.prompt_sizes(rows, NAMES)
    assert [g["title"] for g in out["by_project"]] == ["Beta", "Alpha"]
    assert out["biggest"]["title"] == "Beta"


def test_a_run_with_no_project_still_gets_a_row():
    """Reflect and compaction runs build prompts too, and they are not small."""
    out = usage.prompt_sizes([_sized(None, 70 * 1024)], NAMES)
    assert out["by_project"][0]["title"] == "(not a project)"


def test_prompt_sizes_on_no_measured_runs_is_empty():
    out = usage.prompt_sizes([], NAMES)
    assert out["runs"] == 0 and out["by_project"] == []


def test_the_activity_page_draws_the_panel_once_a_run_has_a_size(client, project):
    run_id = _run(project["id"])
    db.get_conn().execute(
        "UPDATE runs SET prompt_bytes = ? WHERE id = ?", (96 * 1024, run_id)
    )
    db.get_conn().commit()

    body = client.get("/activity").text
    assert "Prompt size" in body
    assert "96.0 KB" in body


def test_the_panel_is_absent_before_anything_has_been_measured(client, project):
    _run(project["id"])
    assert "Prompt size" not in client.get("/activity").text


def test_a_stat_row_at_the_top_of_a_card_drops_its_separator():
    """The rule under `.stat-row` exists to separate it from a chart above it.
    Both the run page and the prompt-size panel put one at the top of a card,
    where it separates nothing and reads as a rendering fault."""
    css = (config.BASE_DIR / "app" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".card > .stat-row:first-child" in css
    assert "border-top: none" in css.split(".card > .stat-row:first-child")[1][:120]


# --------------------------------------------------------------------------
# The table fitting its card
#
# Wes photographed this on 2026-08-01: on the usage tab the breakdown table ran
# past the card's own right border, "avg length" broke across two lines, and the
# last column was sliced off mid-word. The slicing is the part with teeth -
# `overflow-x: hidden` on <body> is two elements up, so nothing on screen says
# there is a column you are not being shown.
# --------------------------------------------------------------------------

def _css():
    return (config.BASE_DIR / "app" / "static" / "style.css").read_text(encoding="utf-8")


def test_a_table_too_wide_for_its_card_scrolls_inside_it():
    css = _css()
    assert ".table-scroll" in css
    rules = css.split(".table-scroll {")[1].split("}")[0]
    assert "overflow-x: auto" in rules
    # Without this the box grows to its content and the wrapper does nothing at
    # all - the table simply overflows one element further out.
    assert "max-width: 100%" in rules


def test_every_breakdown_table_is_inside_one():
    """Both breakdown tables and the run feed. The run feed is nine columns with
    a task line of arbitrary length in the middle of it."""
    markup = (config.BASE_DIR / "app" / "templates" / "activity.html").read_text(
        encoding="utf-8"
    )
    tables = markup.count('<table class="run-table')
    # Four since 2026-08-07, when "Where the tokens go" landed. The count is a
    # canary rather than the point - the invariant is the line below it, that
    # every one of them is wrapped. Bump it when you add a table on purpose.
    assert tables == 4
    assert markup.count('<div class="table-scroll">') == tables


def test_a_breakdown_header_never_wraps_and_a_breakdown_cell_always_can():
    """The two halves of what looked broken. A wrapped header ("avg" over
    "leng") reads as a rendering fault; a project title held on one line by
    `.run-table td { white-space: nowrap }` is what made the table too wide to
    hold a header in the first place."""
    css = _css()
    assert "white-space: nowrap" in css.split(".breakdown th {")[1].split("}")[0]
    assert "white-space: normal" in css.split(".breakdown td {")[1].split("}")[0]


def test_the_share_bar_shrinks_in_a_table():
    """It is decoration in the widest column. At the sparkline's own 1.35rem it
    cost about a third of the card."""
    css = _css()
    bar = css.split(".breakdown .share-bar {")[1].split("}")[0]
    assert "font-size: 1rem" in bar


def test_a_short_fact_in_the_status_line_is_held_on_one_line():
    """The row wraps BETWEEN facts; each fact holds its own line. Without it a
    non-wrapping flex row makes every item break inside itself instead - "runs
    today:" over "6 / 80", "adjust" over "budget" - which reads as broken labels
    rather than as a narrow window.

    Found by a delete-the-fix sweep on 2026-08-01: removing the rule broke no
    test, because the CSS and the markup were each tested against the other's
    absence and neither against the behavior."""
    css = _css()
    row = css.split(".status-line {")[1].split("}")[0]
    assert "flex-wrap: wrap" in row
    assert "white-space: nowrap" in css.split(".status-line .stat-fact {")[1].split("}")[0]

    markup = (config.BASE_DIR / "app" / "templates" / "activity.html").read_text(
        encoding="utf-8"
    )
    # Every short fact on the row carries it: the run cap, the reset clock, a
    # live run's line, and the link out to the budget.
    # The attribute, not the word: the comment above the row names it too.
    assert markup.count('stat-fact"') == 4
    # And the idle reason, which is a whole sentence, deliberately does not.
    idle = [l for l in markup.splitlines() if "idle_reason" in l]
    assert idle and "stat-fact" not in idle[0]


# --------------------------------------------------------------------------
# The tables on a phone
#
# The scrolling box above kept every column reachable, but reachable is not the
# same as readable: at 390px the breakdown showed 2 of its 7 columns and the run
# feed 2 of its 9, and iOS draws overlay-only scrollbars, so nothing on screen
# said the other five were sideways.
#
# The decision (todo #534, 2026-08-06) was the stacked layout rather than a
# scroll affordance on `.table-scroll`: a fade at the edge only tells you the
# table is unusable here. Below 560px a row becomes a block - `cell-name` is its
# heading, and every other cell carries the column header it lost in its own
# `data-label`.
# --------------------------------------------------------------------------

_EXEMPT = {
    # The heading of the block. It needs no label: on a breakdown it is the
    # project, on the run feed the task, and both say what they are.
    "cell-name",
    # A picture, not a fact, and it gets its own full-width line.
    "cell-bar",
    # A badge that already reads "ok" in green. A label over it is the word twice.
    "cell-status",
}


def _media_block(css: str, marker: str) -> str:
    """The body of the `@media (max-width: 560px)` block containing `marker`.

    There is more than one such block in the sheet, so the marker - not the
    first match - is what picks it.
    """
    at = 0
    while True:
        start = css.index("@media (max-width: 560px)", at)
        i = css.index("{", start)
        begin, depth = i, 0
        while True:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = css[begin : i + 1]
        if marker in body:
            return body
        at = i


def _tables(html: str):
    """Every `.run-table` on the page, as (headers, first row's cells)."""
    import re

    out = []
    for m in re.finditer(r'<table class="run-table[^"]*">(.*?)</table>', html, re.S):
        body = m.group(1)
        headers = re.findall(r"<th>(.*?)</th>", body, re.S)
        first_row = re.search(r"<tbody>\s*<tr[^>]*>(.*?)</tr>", body, re.S)
        assert first_row, "a table rendered with no rows"
        cells = re.findall(r"<td([^>]*)>", first_row.group(1))
        out.append((headers, cells))
    return out


@pytest.fixture
def three_tables(client, project):
    """A page carrying all three tables: the breakdown wants two projects, and
    the prompt-size table only draws once more than one has a measured size."""
    other = db.create_project("Other Thing", stage="active", slug="other-thing")
    for pid, size in ((project["id"], 96 * 1024), (other["id"], 40 * 1024)):
        run_id = _run(pid, cost=0.4)
        db.get_conn().execute(
            "UPDATE runs SET prompt_bytes = ? WHERE id = ?", (size, run_id)
        )
    db.get_conn().commit()
    return client.get("/activity").text


def test_every_column_a_phone_row_loses_is_named_on_its_own_cell(three_tables):
    """The contract with teeth. Add an eighth column without a `data-label` and
    on a phone it renders as a bare number with nothing saying what it counts -
    which is exactly the failure the stacked layout exists to fix."""
    tables = _tables(three_tables)
    assert len(tables) == 3, "expected the breakdown, the prompt sizes and the run feed"
    for headers, cells in tables:
        assert len(headers) == len(cells), (headers, cells)
        for header, attrs in zip(headers, cells):
            if any(f'"{cls}"' in attrs or f" {cls}" in attrs for cls in _EXEMPT):
                continue
            assert f'data-label="{header}"' in attrs, (
                f"the {header!r} column has no data-label, so a phone row drops it: {attrs}"
            )


def test_exactly_one_cell_per_row_is_the_block_heading(three_tables):
    for _headers, cells in _tables(three_tables):
        assert sum("cell-name" in a for a in cells) == 1, cells


def test_the_run_feed_is_headed_by_its_project(three_tables):
    """Not the run number ("#712" tells you nothing) and not the task: almost
    every scheduled run on this portal is called "build", so a column of "build"
    headings would identify nothing at all. Shot at 390px before this was
    swapped, and it read as four identical blocks."""
    headers, cells = _tables(three_tables)[-1]
    assert headers[:4] == ["run", "when", "project", "task"]
    assert "cell-name" in cells[2]
    assert "cell-name" not in cells[0] and "cell-name" not in cells[3]


def test_the_cost_column_labels_itself_in_the_unit_that_is_showing(client, project):
    """The header is `cost_noun()`, so a hardcoded label would say "weight" on a
    page reading in dollars."""
    db.set_setting("cost_units", "usd")
    _run(project["id"], cost=0.4)
    body = client.get("/activity").text
    assert 'data-label="cost"' in body
    assert 'data-label="weight"' not in body


def test_the_phone_block_unwinds_the_table_and_hides_its_header_row(three_tables):
    css = _css()
    block = _media_block(css, ".run-table")
    assert re.search(r"\.run-table thead \{[^}]*display: none", block), block
    # The row is the block. Flex rather than plain blocks so `order` can put the
    # heading first even when it is the fourth cell in the markup.
    row = re.search(r"\.run-table tr \{(.*?)\}", block, re.S).group(1)
    assert "display: flex" in row and "flex-wrap: wrap" in row


def test_the_heading_takes_a_whole_line_and_comes_first(three_tables):
    block = _media_block(_css(), ".run-table")
    rule = re.search(r"\.run-table td\.cell-name \{(.*?)\}", block, re.S).group(1)
    assert "order: -1" in rule, rule
    # A flex item this wide cannot share a line with the chips after it.
    assert "100%" in rule, rule
    # A flex item's default `min-width: auto` is its longest word, which for a
    # task line is how a page ends up wider than the phone.
    assert "min-width: 0" in rule, rule


def test_a_labeled_cell_prints_its_column_name(three_tables):
    block = _media_block(_css(), ".run-table")
    rule = re.search(r"\.run-table td\[data-label\]::before \{(.*?)\}", block, re.S)
    assert rule, "nothing draws the labels, so the stacked cells are unlabeled"
    assert "attr(data-label)" in rule.group(1)


def test_the_desktop_table_is_untouched_outside_the_phone_block():
    """The stacking must not leak upward - at any width above 560px this is
    still a table, and the seven columns still line up under their headers."""
    css = _css()
    # "Everything above the phone block" is measured from the block that holds
    # *this* table, not from the first 560px query in the sheet. There are
    # several of those and a new one landing earlier in the file is an ordinary
    # change, not a regression in this one - which is what broke this test on
    # 2026-08-07, when an unrelated fold-heading rule became the first match.
    phone_block = _media_block(css, ".run-table")
    # Comments stripped after slicing: the note explaining the stacking sits
    # above the media query and names every class in it.
    before = re.sub(r"/\*.*?\*/", "", css[: css.index(phone_block)], flags=re.S)
    for selector in ("cell-name", "cell-bar", "cell-status", "data-label"):
        assert selector not in before, f"{selector} escaped the media query"
    # And the desktop rules that make the table a table are still there.
    assert "white-space: nowrap" in before.split(".run-table td {")[1].split("}")[0]


# --------------------------------------------------------------------------
# Where the tokens go
# --------------------------------------------------------------------------
# Wes, 2026-08-07: "I seem to blow through so much more usage than I used to.
# Is it all the bloat from system prompt, memory, chat history, etc?" It is not,
# and these pin the arithmetic that says so - a run re-reads its whole context
# once per turn, so every slice is tokens x turns and the portal's prompt is the
# smallest of the three.

def _read(project_id, prompt_bytes, turns, reads):
    row = _row(project_id, turns=turns)
    row["prompt_bytes"] = prompt_bytes
    row["cache_read_tokens"] = reads
    return row


def test_the_prompt_is_charged_once_per_turn_not_once_per_run():
    """The whole point of the panel. A 40,000-byte prompt is 10k tokens; over 10
    turns that is 100k of re-reads, so against 200k of reads it is half - not
    the 5% a once-per-run reading would report.

    Byte counts here are round numbers rather than KB, so the expected figure is
    exact arithmetic on BYTES_PER_TOKEN and not a rounding of it."""
    out = usage.anatomy([_read(1, 40_000, turns=10, reads=200_000)])
    assert out["prompt_pct"] == 50.0


def test_doubling_the_turns_doubles_the_prompts_share_of_a_fixed_read_budget():
    """Guards the multiplication itself, not just its result on one row."""
    ten = usage.anatomy([_read(1, 40 * 1024, turns=10, reads=400_000)])
    twenty = usage.anatomy([_read(1, 40 * 1024, turns=20, reads=400_000)])
    assert twenty["prompt_pct"] == 2 * ten["prompt_pct"]


def test_claude_codes_own_head_is_reported_separately_from_the_portals_prompt():
    """~34k tokens of system prompt and tool schemas ride on every turn and the
    portal cannot shrink them. Folding them into the prompt's share would
    overstate what a prompt diet buys - which is the mistake the panel exists to
    stop somebody making."""
    out = usage.anatomy([_read(1, 40_000, turns=10, reads=1_000_000)])
    # 34,300 x 10 turns = 343k of a million; the prompt's 10k x 10 is 100k.
    assert out["cli_pct"] == 34.3
    assert out["prompt_pct"] == 10.0
    assert out["run_pct"] == pytest.approx(55.7, abs=0.1)


def test_the_three_slices_always_account_for_all_of_it():
    out = usage.anatomy([_read(1, 90 * 1024, turns=150, reads=26_000_000)])
    total = out["prompt_pct"] + out["cli_pct"] + out["run_pct"]
    assert total == pytest.approx(100.0, abs=0.05)


def test_a_modeled_head_bigger_than_the_recorded_reads_never_goes_negative():
    """The head slices are modeled and `reads` is recorded, so they can disagree.
    A run that somehow read less than its own head is 'essentially all head',
    not a negative share of one."""
    out = usage.anatomy([_read(1, 400 * 1024, turns=100, reads=1000)])
    assert out["prompt_pct"] == 100.0
    assert out["cli_pct"] == 0.0
    assert out["run_pct"] == 0.0


def test_runs_from_before_the_token_columns_landed_are_skipped():
    """Same reason `prompt_sizes` skips them: the columns landed 2026-07-28 and
    counting the older runs as zero would show the prompt vanishing on the day
    measurement started."""
    assert usage.anatomy([_row(1, turns=50)])["runs"] == 0
    assert usage.anatomy([])["runs"] == 0


def test_a_run_that_recorded_no_turns_is_not_divided_by():
    assert usage.anatomy([_read(1, 40 * 1024, turns=0, reads=200_000)])["runs"] == 0


def test_a_run_that_never_built_a_prompt_is_not_counted_as_having_an_empty_one():
    """A run that died before building a prompt has `prompt_bytes = 0` while
    still recording turns and reads - so the two guards above it do not catch
    it. Counting it would report a run whose prompt was 0% of its own context,
    dragging the prompt slice down with a number that describes a failure.

    Found by the mutation sweep: dropping `not pbytes` from the guard escaped,
    because every runs-are-skipped case tested until now was missing the token
    columns too and was caught by `not read`."""
    row = _row(1, turns=50)
    row["cache_read_tokens"] = 500_000
    row["prompt_bytes"] = 0
    assert usage.anatomy([row])["runs"] == 0


# --- what changed ---------------------------------------------------------

def _day(date, runs, turns, cost):
    return {"date": date, "runs": runs, "turns": turns, "cost": cost}


def test_the_trend_splits_the_window_in_half_by_day():
    older = [_day(f"2026-07-2{i}", runs=10, turns=500, cost=10.0) for i in range(2)]
    newer = [_day(f"2026-08-0{i}", runs=10, turns=1500, cost=60.0) for i in range(2)]
    out = usage.turn_trend(older + newer)
    assert out["older"]["turns_per_run"] == 50.0
    assert out["newer"]["turns_per_run"] == 150.0
    assert out["turns_change_pct"] == 200


def test_the_two_left_hand_numbers_multiply_into_the_third():
    """Wes's actual question is why a run costs more. Turns up 3x and weight per
    turn up 2x is a run 6x dearer, and the panel has to show all three or the
    reader will assume they add."""
    older = [_day("2026-07-21", runs=10, turns=500, cost=10.0)]
    newer = [_day("2026-08-04", runs=10, turns=1500, cost=60.0)]
    out = usage.turn_trend(older + newer)
    assert out["turns_change_pct"] == 200      # 50 -> 150 turns a run
    assert out["per_turn_change_pct"] == 100   # 0.02 -> 0.04 a turn
    assert out["per_run_change_pct"] == 500    # 1.00 -> 6.00 a run


def test_a_window_that_opens_on_an_idle_day_still_finds_its_older_half():
    """A window with a weekend in it must not read as the work getting cheaper.

    The idle day has to lead for this to bite, and the mutation sweep is what
    made that clear. With the quiet day in the MIDDLE, dropping the filter still
    gives the right answer by accident: the extra day carries no runs and no
    turns, so it changes neither ratio. Leading, it becomes the whole of the
    older half, which then has nothing to divide by - and the panel reports no
    change at all on a fortnight where runs doubled in length."""
    days = [
        _day("2026-07-20", runs=0, turns=0, cost=0.0),
        _day("2026-07-21", runs=10, turns=500, cost=10.0),
        _day("2026-08-04", runs=10, turns=1500, cost=60.0),
    ]
    assert usage.turn_trend(days)["turns_change_pct"] == 200


def test_a_window_too_short_to_have_two_halves_reports_no_trend():
    assert usage.turn_trend([_day("2026-08-04", runs=10, turns=500, cost=1.0)])["runs"] == 0
    assert usage.turn_trend([])["runs"] == 0


def test_a_change_from_nothing_is_zero_rather_than_infinite():
    older = [_day("2026-07-21", runs=10, turns=500, cost=0.0)]
    newer = [_day("2026-08-04", runs=10, turns=500, cost=50.0)]
    assert usage.turn_trend(older + newer)["per_turn_change_pct"] == 0


def test_the_activity_page_draws_both_new_panels(client, project):
    run_id = _run(project["id"])
    db.get_conn().execute(
        "UPDATE runs SET prompt_bytes = ?, cache_read_tokens = ?, num_turns = ? "
        "WHERE id = ?",
        (96 * 1024, 5_000_000, 120, run_id),
    )
    db.get_conn().commit()
    body = client.get("/activity").text
    assert "Where the tokens go" in body
    # And it names the slice that is not the portal's to cut, so the page cannot
    # be read as blaming the prompt for all of it.
    assert "not ours to cut" in body

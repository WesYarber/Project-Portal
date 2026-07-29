"""The "since you last looked" banner at the top of a project.

From Wes's note: "Add to each project a summary of the previous work done to
it. This summary should be brief and live at the top of the project page. There
should be an 'Acknowledged' button under it that basically resets that context
window and clears the update and returns the space that it used to occupy
visually."

The behaviors that make that true, and which these tests pin down:

- a run's one-line `summary` from report.json is recorded against the run;
- the project page shows the summaries Wes has not acknowledged, oldest first
  (Wes, 2026-07-28: "should go from oldest to newest from top to bottom, since
  that is how they are read"), while still capping to the most RECENT few;
- pressing acknowledged clears them and the section disappears entirely, giving
  its space back rather than leaving an empty card behind;
- work finished *after* the button was pressed is not swallowed by it.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import agent_runner, db, worker


@pytest.fixture
def client(temp_data_dir):
    from app import main

    # No context manager: that would run the lifespan hook and start the worker.
    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project("Fridge Board", description="A thing.", stage="active", build_approved=True, slug="fridge")


def _finished_run(project_id: int, summary: str) -> int:
    run_id = db.create_run(project_id, "build", "opus")
    db.finish_run(run_id, "ok")
    db.set_run_report_summary(run_id, summary)
    return run_id


# --- recording -------------------------------------------------------------

def test_a_report_summary_is_recorded_against_its_run(project):
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok")
    result = agent_runner.RunResult(
        ok=True, report={"summary": "Built the todo list", "journal_entry_md": "..."}
    )
    worker._apply_report(db.get_project(project["id"]), result, run_id)

    assert db.get_run(run_id)["report_summary"] == "Built the todo list"


def test_a_report_without_a_summary_records_nothing(project):
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok")
    worker._apply_report(
        db.get_project(project["id"]),
        agent_runner.RunResult(ok=True, report={"summary": "  ", "journal_entry_md": "x"}),
        run_id,
    )
    assert db.get_run(run_id)["report_summary"] is None
    assert db.unacknowledged_work(project["id"]) == []


def test_a_summary_is_scrubbed_and_capped(project):
    run_id = _finished_run(project["id"], "line\tone" + " x" * 1000)
    stored = db.get_run(run_id)["report_summary"]
    assert "\n" not in stored and "\t" not in stored
    assert stored.startswith("line one")
    assert len(stored) <= 1200


# --- bullets, not one contentless line -------------------------------------
#
# Wes's 21:35 note: the banner was rendering "Done. Two commits in
# /home/someone/project-portal" and "Done. Two todo items shipped" - lines that
# count the agent's own output instead of naming what changed. The prompt now
# asks for concrete bullets; these pin the plumbing that carries them.

def test_a_list_summary_is_kept_as_separate_bullets(project):
    run_id = _finished_run(
        project["id"], ["Completed todos age out after 16 hours", "The wordmark moved above the tabs"]
    )
    assert db.unacknowledged_work(project["id"])[0]["bullets"] == [
        "Completed todos age out after 16 hours",
        "The wordmark moved above the tabs",
    ]
    # Stored newline-separated in the one column, so no migration was needed.
    assert db.get_run(run_id)["report_summary"].count("\n") == 1


def test_one_string_holding_several_lines_is_split_back_apart(project):
    _finished_run(project["id"], "Fixed the favicon\nMuted paused projects")
    assert db.unacknowledged_work(project["id"])[0]["bullets"] == [
        "Fixed the favicon",
        "Muted paused projects",
    ]


def test_a_plain_one_line_summary_is_one_bullet(project):
    _finished_run(project["id"], "Built the per-project todo list")
    assert db.unacknowledged_work(project["id"])[0]["bullets"] == [
        "Built the per-project todo list"
    ]


@pytest.mark.parametrize(
    "raw, want",
    [
        ("Done. Built the todo list", "Built the todo list"),
        ("done: built the todo list", "built the todo list"),
        ("- Built the todo list", "Built the todo list"),
        ("* Built the todo list", "Built the todo list"),
        ("1. Built the todo list", "Built the todo list"),
        ("✔ Built the todo list", "Built the todo list"),
        ("Summary: Built the todo list", "Built the todo list"),
        ("- Done. Built the todo list", "Built the todo list"),
    ],
)
def test_leading_noise_is_stripped_from_a_bullet(project, raw, want):
    """The page draws its own tick, and "Done." never said anything."""
    _finished_run(project["id"], raw)
    assert db.unacknowledged_work(project["id"])[0]["bullets"] == [want]


def test_a_hyphen_mid_sentence_is_not_a_bullet_marker(project):
    _finished_run(project["id"], "Fixed the favicon - it was missing at the origin root")
    assert db.unacknowledged_work(project["id"])[0]["bullets"] == [
        "Fixed the favicon - it was missing at the origin root"
    ]


def test_bullets_are_capped(project):
    _finished_run(project["id"], [f"thing {i}" for i in range(10)])
    bullets = db.unacknowledged_work(project["id"])[0]["bullets"]
    assert len(bullets) == db.MAX_SUMMARY_BULLETS


def test_bullets_are_deduped(project):
    _finished_run(project["id"], ["same thing", "same thing", "other thing"])
    assert db.unacknowledged_work(project["id"])[0]["bullets"] == ["same thing", "other thing"]


@pytest.mark.parametrize("junk", [None, [], ["  ", ""], [None, {}, []], "Done."])
def test_a_summary_with_nothing_in_it_records_nothing(project, junk):
    """A malformed or contentless summary must not put an empty row on the page."""
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok")
    db.set_run_report_summary(run_id, junk)
    assert db.get_run(run_id)["report_summary"] is None
    assert db.unacknowledged_work(project["id"]) == []


def test_a_list_summary_survives_the_report_pipeline(project):
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok")
    worker._apply_report(
        db.get_project(project["id"]),
        agent_runner.RunResult(
            ok=True, report={"summary": ["Bullet one", "Bullet two"], "journal_entry_md": "x"}
        ),
        run_id,
    )
    assert db.unacknowledged_work(project["id"])[0]["bullets"] == ["Bullet one", "Bullet two"]


def test_the_fallback_line_loses_its_done_prefix(project):
    """Runs before this feature recorded no report summary; the CLI's last line
    is the fallback, and every one of those opened with a contentless "Done."."""
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok", summary="Done. Built the todo list.\nMore detail below.")
    assert db.unacknowledged_work(project["id"])[0]["bullets"] == ["Built the todo list."]


def test_the_page_renders_each_bullet_as_its_own_item(client, project):
    _finished_run(project["id"], ["Bullet one", "Bullet two"])
    body = client.get(f"/project/{project['slug']}").text
    assert "<li>Bullet one</li>" in body
    assert "<li>Bullet two</li>" in body
    # The tick is drawn in CSS, not baked into the text.
    assert "Done." not in body


def test_the_page_stacks_the_runs_oldest_at_the_top(client, project):
    """Rendered order, not just query order - the template iterates as given."""
    _finished_run(project["id"], "Landed the first thing")
    _finished_run(project["id"], "Landed the second thing")
    _finished_run(project["id"], "Landed the third thing")

    body = client.get(f"/project/{project['slug']}").text
    banner = body[body.index("since you last looked"):body.index("acknowledged</button>")]
    assert (
        banner.index("Landed the first thing")
        < banner.index("Landed the second thing")
        < banner.index("Landed the third thing")
    )


def test_the_prompt_demands_concrete_bullets():
    """The real fix is the contract: the plumbing cannot make a bad line good."""
    contract = agent_runner.AGENT_CONTRACT
    assert '"summary": [' in contract
    assert "two commits" in contract.lower()


# --- what the banner contains ----------------------------------------------

def test_unacknowledged_work_is_oldest_first(project):
    """Reading order: you read a stack of these top to bottom, earliest first."""
    _finished_run(project["id"], "first thing")
    _finished_run(project["id"], "second thing")

    rows = db.unacknowledged_work(project["id"])
    assert [r["report_summary"] for r in rows] == ["first thing", "second thing"]


def test_runs_without_a_summary_are_not_in_the_banner(project):
    db.finish_run(db.create_run(project["id"], "triage", "haiku"), "ok")
    assert db.unacknowledged_work(project["id"]) == []


def test_the_banner_is_capped(project):
    for i in range(db.MAX_UNACKED_SHOWN + 3):
        _finished_run(project["id"], f"run {i}")
    assert len(db.unacknowledged_work(project["id"])) == db.MAX_UNACKED_SHOWN


def test_the_cap_keeps_the_newest_runs_even_though_it_shows_them_oldest_first(project):
    """The trap in reversing the banner.

    Ordering the SELECT ascending and taking the first `limit` reads the same
    from the outside - a list running oldest to newest - but it keeps the
    WRONG end: the oldest runs, dropping the work you actually opened the
    project to read. The query has to stay newest-first and the reversal has
    to happen after the cap.
    """
    for i in range(db.MAX_UNACKED_SHOWN + 3):
        _finished_run(project["id"], f"run {i}")

    shown = [r["report_summary"] for r in db.unacknowledged_work(project["id"])]
    newest = [f"run {i}" for i in range(3, db.MAX_UNACKED_SHOWN + 3)]
    assert shown == newest
    assert "run 0" not in shown


def test_another_projects_work_is_not_shown(project):
    other = db.create_project("Other", slug="other")
    _finished_run(other["id"], "not yours")
    assert db.unacknowledged_work(project["id"]) == []


# --- acknowledging ---------------------------------------------------------

def test_acknowledging_clears_the_banner(project):
    _finished_run(project["id"], "did a thing")
    db.acknowledge_work(project["id"])
    assert db.unacknowledged_work(project["id"]) == []


def test_work_finished_after_acknowledging_still_shows(project, monkeypatch):
    _finished_run(project["id"], "old news")
    db.acknowledge_work(project["id"])

    # Timestamps are second-resolution, so a run finishing in the same second
    # as the click would be ambiguous; push this one clearly past it.
    monkeypatch.setattr(db, "now", lambda: "2099-01-01T00:00:00+00:00")
    _finished_run(project["id"], "brand new")

    assert [r["report_summary"] for r in db.unacknowledged_work(project["id"])] == ["brand new"]


# --- the web UI ------------------------------------------------------------

def test_the_project_page_shows_the_summary_at_the_top(client, project):
    _finished_run(project["id"], "Built the todo list")

    body = client.get(f"/project/{project['slug']}").text
    assert "since you last looked" in body
    assert "Built the todo list" in body
    # Above the ask button, which is itself above everything else on the page.
    assert body.index("since you last looked") < body.index("ask project")


def test_the_section_is_absent_with_nothing_to_report(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert "since you last looked" not in body
    assert "work-summary" not in body


def test_pressing_acknowledged_gives_the_space_back(client, project):
    _finished_run(project["id"], "Built the todo list")

    r = client.post(f"/project/{project['slug']}/acknowledge", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/project/{project['slug']}"

    body = client.get(f"/project/{project['slug']}").text
    assert "since you last looked" not in body
    assert "Built the todo list" not in body


def test_acknowledging_an_unknown_project_is_a_404(client):
    assert client.post("/project/nope/acknowledge", follow_redirects=False).status_code == 404

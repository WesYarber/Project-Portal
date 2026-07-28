"""Per-project usage breakdown, and the cancel controls in the web UI."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import db, usage

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

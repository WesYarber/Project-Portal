"""The dashboard's live strip shows a run's hold, and can pause and resume it.

The previous pass (app/midrun.py) put pause and resume on the project page and
the run page and left the dashboard strip with only "stop" - so the one place
that lists every agent at once could stop one but not hold it, and showed a
green pulse over an agent that was deliberately doing nothing. These pin the
server's render of the strip, the API the poller reads, and - driven under bun
against a stub row (tests/js/live_strip.mjs) - the poller repainting the dot,
the pausing/paused badge and the button's direction from each poll, because a
pause pressed on another page or phone, and the hold engaging at the run's
next tool call, both happen without a button on this page being pressed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import db, hookguard, midrun

from tests.test_midrun import _live_run, _project

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"
INDEX = ROOT / "app" / "templates" / "index.html"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _strip(page: str) -> str:
    start = page.index('id="live-run"')
    return page[start:page.index("section-head", start)]


# --- the server's render ---------------------------------------------------

def test_strip_offers_pause_only_while_the_run_can_be_reached(client):
    project = _project()
    run_id = db.create_run(project["id"], "build", "opus")
    strip = _strip(client.get("/").text)
    assert f"/run/{run_id}/cancel" in strip
    assert f"/run/{run_id}/pause" not in strip
    assert 'class="dot running"' in strip
    assert "live-hold-form" not in strip
    db.finish_run(run_id, "success")
    run_id, _ = _live_run(project)
    try:
        strip = _strip(client.get("/").text)
        assert f'action="/run/{run_id}/pause"' in strip
        assert ">pause</button>" in strip
        assert f"/run/{run_id}/cancel" in strip
        # Held: hidden until the run is actually on hold, so the poller has
        # the element to show without building one.
        assert '<span class="small live-hold" hidden>pausing</span>' in strip
        assert 'class="live-run-row"' in strip
    finally:
        hookguard.end(run_id)


def test_strip_says_pausing_then_paused_and_offers_resume(client):
    project = _project()
    run_id, token = _live_run(project)
    try:
        midrun.pause(run_id)
        strip = _strip(client.get("/").text)
        assert f'action="/run/{run_id}/resume"' in strip
        assert ">resume</button>" in strip
        assert 'class="live-run-row held"' in strip
        assert 'class="dot held"' in strip
        assert '<span class="small live-hold">pausing</span>' in strip
        # The hold engages at the run's next tool call.
        midrun.after_tool_call(run_id, token, {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {}})
        strip = _strip(client.get("/").text)
        assert '<span class="small live-hold">paused</span>' in strip
        midrun.resume(run_id)
        strip = _strip(client.get("/").text)
        assert f'action="/run/{run_id}/pause"' in strip
        assert 'class="dot running"' in strip
        assert '<span class="small live-hold" hidden>pausing</span>' in strip
    finally:
        hookguard.end(run_id)


def test_pause_form_posts_in_place_and_comes_back_to_the_dashboard(client):
    """The strip's form is a `data-inplace` one like the project page's, and
    its `next` is the dashboard - a press must not land the reader on the
    project page."""
    project = _project()
    run_id, _ = _live_run(project)
    try:
        strip = _strip(client.get("/").text)
        form = re.search(r'<form[^>]*live-hold-form[^>]*>(.*?)</form>', strip, re.S)
        assert form is not None
        assert "data-inplace" in form.group(0)
        assert '<input type="hidden" name="next" value="/">' in form.group(1)
        res = client.post(f"/run/{run_id}/pause", data={"next": "/"}, follow_redirects=False)
        assert res.status_code == 303 and res.headers["location"] == "/"
        assert midrun.is_paused(run_id)
    finally:
        hookguard.end(run_id)


def _card(page: str, slug: str) -> str:
    start = page.index(f'data-slug="{slug}"')
    return page[start:page.index("</a>", start)]


def test_project_card_says_paused_under_a_held_run(client):
    """The strip and the card are two views of one run: a green "agent
    working" pulse on the card under an amber "paused" in the strip is the
    disagreement Wes reads as a bug."""
    project = _project()
    run_id, _ = _live_run(project)
    other, _ = _live_run(_project("other"))
    try:
        card = _card(client.get("/").text, project["slug"])
        assert "agent working" in card and 'class="dot running"' in card
        midrun.pause(other)
        card = _card(client.get("/").text, project["slug"])
        assert "agent working" in card, "another project's hold is not this card's"
        midrun.pause(run_id)
        card = _card(client.get("/").text, project["slug"])
        assert "agent paused" in card and 'class="dot held"' in card
        assert "agent working" not in card
        midrun.resume(run_id)
        assert "agent working" in _card(client.get("/").text, project["slug"])
    finally:
        hookguard.end(run_id)
        hookguard.end(other)


def test_api_active_run_carries_each_runs_hold_state(client):
    """What the poller paints from: per run, not only the newest."""
    a, _ = _live_run(_project("alpha"))
    b, _ = _live_run(_project("beta"))
    try:
        midrun.pause(b)
        runs = {r["run_id"]: r for r in client.get("/api/active-run").json()["runs"]}
        assert runs[a]["paused"] is False and runs[a]["can_pause"] is True
        assert runs[b]["paused"] is True and runs[b]["engaged"] is False
    finally:
        hookguard.end(a)
        hookguard.end(b)


# --- what the poller does with it -----------------------------------------

@pytest.fixture(scope="module")
def painted():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "live_strip.mjs"
    out = subprocess.run([bun, str(harness), str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_poller_paints_a_pause_pressed_somewhere_else(painted):
    before = painted["running"]
    assert before["dot"] == ["dot", "running"] and before["badgeHidden"] is True
    assert before["action"] == "/run/7/pause" and before["button"] == "pause"

    pausing = painted["pausing"]
    assert pausing["row"] == ["held", "live-run-row"]
    assert pausing["dot"] == ["dot", "held"]
    assert pausing["badgeHidden"] is False and pausing["badge"] == "pausing"
    assert pausing["action"] == "/run/7/resume" and pausing["button"] == "resume"


def test_poller_paints_the_hold_engaging_and_the_resume(painted):
    assert painted["paused"]["badge"] == "paused"
    assert painted["paused"]["badgeHidden"] is False

    resumed = painted["resumed"]
    assert resumed["row"] == ["live-run-row"]
    assert resumed["dot"] == ["dot", "running"]
    assert resumed["badgeHidden"] is True
    assert resumed["action"] == "/run/7/pause" and resumed["button"] == "pause"
    # Same run throughout: the hold changing is a repaint, never a reload.
    assert painted["liveReloads"] == 0


def test_poller_never_invents_a_pause_button(painted):
    assert painted["unreachable"]["action"] is None
    assert painted["unreachable"]["activity"] == "> Bash(ls)"


def test_poller_holds_only_the_held_run_of_two(painted):
    two = painted["twoRuns"]
    assert two["9"]["dot"] == ["dot", "running"] and two["9"]["button"] == "pause"
    assert two["10"]["dot"] == ["dot", "held"] and two["10"]["badge"] == "paused"
    assert two["10"]["button"] == "resume"


# --- the two halves agree on their names ----------------------------------

def test_template_and_poller_use_the_same_hooks():
    """No mutation describes a rename: the poller finds the badge and the form
    by class, so the template has to render exactly those classes."""
    html = INDEX.read_text()
    js = APP_JS.read_text()
    for name in ("live-hold", "live-hold-form", "live-activity", "live-meta"):
        assert f'class="{name}"' in html or f" {name}" in html or f'"{name}' in html, name
        assert f'querySelector(".{name}")' in js, name

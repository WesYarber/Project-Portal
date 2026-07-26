"""Live-updating pages (Wes's 2026-07-22 04:05 note).

    "Could the page sort of update/refresh in real time rather than me needing
    to refresh it myself? it would be important to keep the current view from
    shifting around when stuff loads in and adds additional size to an element
    on the screen. It should be done smartly."

The server half is app/live.py: a version token off SQLite's data_version,
read by a dedicated observer connection so every commit the app makes is seen
without instrumenting any write path. The client half is the morph in app.js,
which cannot run under pytest - so its load-bearing rules (what is preserved,
what defers a patch, what replaces a reload) are pinned as source assertions,
and the behaviour was verified for real in a browser on render-box.
"""
from __future__ import annotations

import re
import sqlite3

import pytest
from starlette.testclient import TestClient

from app import config, db, live


@pytest.fixture
def client(temp_data_dir):
    from app import main

    # Not a context manager on purpose: lifespan would start the worker and
    # Telegram pollers. The DB is already initialised by the autouse fixture.
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def fresh_observer():
    # The observer caches per DB path; tests each get a new tmp path so it
    # reopens naturally, but resetting keeps every test order-independent.
    live.reset()
    yield
    live.reset()


def js() -> str:
    return (config.APP_ROOT / "app" / "static" / "app.js").read_text()


# --------------------------------------------------------------------------
# The version token
# --------------------------------------------------------------------------

def test_token_shape_and_stable_boot_id():
    a = live.version_token()
    b = live.version_token()
    assert re.fullmatch(r"[0-9a-f]{12}:\d+", a)
    # Nothing wrote between the two reads, so the whole token is unchanged -
    # a token that drifts on its own would make the client patch in a loop.
    assert a == b
    assert a.split(":")[0] == live.BOOT_ID


def test_data_half_moves_when_the_app_writes():
    before = live.version_token()
    # An ordinary write through the app's own shared connection - the exact
    # thing the observer exists to notice without being told.
    db.create_project("Ping", description="", slug="ping")
    after = live.version_token()
    assert before != after
    assert before.split(":")[0] == after.split(":")[0]  # same boot, new data


def test_every_kind_of_write_is_seen():
    project = db.create_project("P", description="", slug="p")
    v1 = live.version_token()
    db.add_journal(project["id"], "user", "note", "hello")
    v2 = live.version_token()
    assert v2 != v1
    db.set_setting("worker_enabled", "0")
    v3 = live.version_token()
    assert v3 != v2


def test_observer_follows_a_repointed_db_path(tmp_path, monkeypatch):
    """Tests repoint config.DB_PATH per test; an observer stuck on the old
    file would report a version that never changes and every page would go
    quiet. The cache is keyed on the path it opened."""
    live.version_token()  # opens against the current path
    other = tmp_path / "other.db"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    monkeypatch.setattr(config, "DB_PATH", other)
    v1 = live.version_token()
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    v2 = live.version_token()
    conn.close()
    assert v1 != v2


def test_api_version_route(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    v = r.json()["v"]
    assert re.fullmatch(r"[0-9a-f]{12}:\d+", v)
    db.create_project("Bump", description="", slug="bump")
    assert client.get("/api/version").json()["v"] != v


# --------------------------------------------------------------------------
# The client's rules, pinned
# --------------------------------------------------------------------------

def test_page_polls_the_version_and_morphs():
    src = js()
    assert "initLiveRefresh" in src
    assert '"/api/version"' in src
    assert "morphNode(document.body, doc.body)" in src
    # A boot change reloads for real - patching across a code change would
    # marry new HTML to stale CSS/JS.
    assert "window.location.reload()" in src


def test_reloads_became_in_place_refreshes():
    """The three places that used to window.location.reload() on data changes
    now patch in place - the reloads were exactly the view-jumps Wes named."""
    src = js()
    assert src.count("liveReload()") >= 3
    # The run-transition branch specifically: it reloaded on every run
    # start/finish, which hit every open page.
    transition = src.split("runIds !== lastRunIds")[1][:200]
    assert "liveReload()" in transition
    assert "window.location.reload()" not in transition


def test_user_state_survives_a_patch():
    """What the morph must never touch: an open fold, a field mid-edit, a
    textarea's autosize, JS-owned hidden state, script-set data markers."""
    src = js()
    assert 'live.tagName === "DETAILS" && name === "open"' in src
    assert '"value" || name === "checked" || name === "selected"' in src
    assert 'live.tagName === "TEXTAREA" && name === "style"' in src
    assert 'name === "hidden"' in src
    assert 'name.indexOf("data-") === 0' in src
    # And whole subtrees other scripts own.
    assert 'live.id === "console-out"' in src
    assert '.tree-dir[data-tree-loaded]' in src
    # Client-only nodes survive the child walk.
    assert ".draft-note, .ctx-menu, #pull-refresh" in src


def test_interaction_defers_the_patch():
    src = js()
    blocked = src.split("function refreshBlocked")[1]
    blocked = blocked.split("\n}")[0]
    assert "TEXTAREA" in blocked
    assert "dragging-project" in blocked
    assert ".sel.open" in blocked
    assert ".ctx-menu" in blocked
    assert "isCollapsed" in blocked
    # ...and a held-back patch is applied later rather than dropped.
    assert "refreshQueued" in src


def test_scroll_and_anchor_are_kept():
    src = js()
    assert '".scroll-cap, #console-out"' in src
    assert "snapshotScrolls" in src
    assert "viewAnchor" in src
    # A panel being read from its end stays pinned to its end.
    assert "atBottom" in src


def test_themed_selects_survive_or_rebuild():
    """The morph meets a JS-built widget where the server renders a bare
    <select>: same options leave the widget alone, changed options rebuild it
    keeping the user's pick."""
    src = js()
    assert "selSignature" in src
    assert 'nextChild.tagName === "SELECT" && fromNode.classList.contains("sel")' in src
    assert "enhanceSelect(next)" in src


def test_reinit_is_guarded_against_double_binding():
    """reinit() re-runs the per-element enhancers after a patch; each one must
    skip nodes it already enhanced, and the guard must be a JS property (the
    morph resets attributes to the server's render, so a data- guard on a kept
    node would... survive, but the point is drag state: a NEW card must join
    the SAME dragged variable the zones already read."""
    src = js()
    assert src.count("_enhanced") >= 8  # set+check on each enhancer
    # dragged is module-level, not per-call closure state.
    drag_section = src.split("function initProjectDrag")[0]
    assert "var dragged = null;" in drag_section

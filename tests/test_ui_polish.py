"""A batch of Wes's UI notes, each about the page getting in his way.

From his notes of 2026-07-21, in the order he sent them:

- "the page should not reset/scroll to the top" after a submit;
- "have the attachments section not show up unless there *are* attachments";
- "make the 'Just asking' section be collapsed by default and show up at the
  top of the page as a 'Ask project' button";
- "get rid of the 'Runs' section and instead put its one line ... at the top of
  the Journal section";
- "have the 'agent console' be collapsed by default and the default scroll
  position within the last run transcript window be the very bottom";
- "make paused projects look less obnoxious ... by muting their yellow border";
- "the favicon for this site is not showing on the main dashboard page";
- "try this file as an app icon and favicon. clean up the name, though."

Most of these are markup, so most of these tests read markup. The two that are
not - the scroll restore and the pin-to-bottom - live in app.js and are pinned
here by asserting the behavior the page depends on being wired up, since there
is no browser in this environment to drive.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db

STATIC = Path(config.BASE_DIR) / "app" / "static"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    # No context manager: that would run the lifespan hook and start the worker.
    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project("Fridge Board", description="A thing.", stage="active", build_approved=True, slug="fridge")


def _page(client, slug="fridge"):
    r = client.get(f"/project/{slug}")
    assert r.status_code == 200
    return r.text


# --- attachments: gone when there are none ---------------------------------


def test_attachments_section_absent_with_no_attachments(client, project):
    html = _page(client)
    assert 'id="attachments"' not in html
    assert "Nothing attached yet" not in html


def test_attachments_section_appears_once_something_is_attached(client, project):
    db.add_attachment(project["id"], "shot.png", "0001-shot.png", "image/png", 1234)
    html = _page(client)
    assert 'id="attachments"' in html
    assert "shot.png" in html


def test_note_box_stays_when_attachments_are_hidden(client, project):
    """Hiding the section must not hide the way to create one."""
    html = _page(client)
    assert "attach files" in html
    assert "data-dropzone" in html


# --- "just asking" folded into a button at the top -------------------------


def test_ask_is_a_collapsed_button_not_a_section(client, project):
    html = _page(client)
    assert "<h2>Just asking</h2>" not in html
    assert 'class="ask-block"' in html
    assert ">ask project</summary>" in html


def test_ask_button_sits_above_the_agent_console(client, project):
    html = _page(client)
    assert html.index('id="ask"') < html.index("Agent console")


def test_ask_form_still_posts_where_it_did(client, project):
    html = _page(client)
    assert 'action="/project/fridge/ask"' in html


def test_ask_opens_itself_while_an_answer_is_pending(client, project, monkeypatch):
    from app import ask, main

    monkeypatch.setattr(main.ask, "pending", lambda pid: True)
    html = _page(client)
    # The <details> carries `open`, so the "thinking..." line is not folded
    # away. Matched loosely across the tag's other attributes rather than by
    # their exact order: the ask block also carries the A key's data-jump
    # attributes now, and this test is about `open`, not about markup order.
    tag = re.search(r"<details class=\"ask-block\"[^>]*>", html)
    assert tag and " open" in tag.group(0)
    assert "thinking..." in html


# --- the Runs section, reduced to its one line above the journal -----------


def test_runs_section_is_gone(client, project):
    html = _page(client)
    assert "<h2>Runs</h2>" not in html
    # ...and so is the table it hid, which duplicated /activity row for row.
    assert "<th>turns</th>" not in html


def test_run_count_line_is_gone_too(client, project):
    """Wes, 2026-07-28: "Get rid of the '50 runs on this project * usage over
    time' text in the journal section."

    It was the last remnant of the Runs table, kept when that table went so the
    count and the link would survive somewhere. Both survive elsewhere now: the
    activity grid moved into the control bar at the top of this page on the
    morning of the same day and is itself a link to /activity for this project,
    so the line was saying a second time what the grid says with a picture.
    """
    db.create_run(project["id"], "build", "opus")
    db.create_run(project["id"], "build", "opus")
    html = _page(client)
    assert "runs on this project" not in html
    assert "run on this project" not in html


def test_the_journal_still_reaches_the_activity_page(client, project):
    """What the line was FOR is not gone - only the sentence is. The heatmap in
    the control bar links to the same filtered activity view."""
    db.create_run(project["id"], "build", "opus")
    html = _page(client)
    assert 'href="/activity?project=fridge"' in html


# --- the agent console, folded shut between runs ---------------------------


def test_console_is_collapsed_when_nothing_is_running(client, project):
    html = _page(client)
    assert 'id="agent-console-details"' in html
    assert not re.search(r'<details class="fold-section console-details" id="agent-console-details" open', html)


def test_console_forces_itself_open_for_a_live_run(client, project, monkeypatch):
    from app import main

    monkeypatch.setattr(
        main,
        "active_run_snapshot",
        lambda: {
            "active": True,
            "project_ids": [project["id"]],
            "runs": [
                {
                    "active": True,
                    "run_id": 7,
                    "project_id": project["id"],
                    "task": "build",
                    "model": "opus",
                    "elapsed": "1m",
                    "events": 3,
                }
            ],
        },
    )
    html = _page(client)
    assert re.search(r'<details class="fold-section console-details" id="agent-console-details" open', html)


def test_console_transcript_is_pinned_to_the_bottom():
    """The <pre> renders the tail of the log scrolled to the *top* of that
    tail. app.js pins it, and must re-pin when the fold opens - a shut
    <details> has no layout, so scrollHeight is 0 and setting scrollTop then
    does nothing at all."""
    js = (STATIC / "app.js").read_text()
    assert "function pinConsole()" in js
    assert "pinConsole();" in js
    assert 'out.scrollTop = out.scrollHeight' in js
    assert 'details.addEventListener("toggle"' in js


# --- staying where you were across a submit --------------------------------


def test_scroll_position_is_stashed_on_submit_and_restored():
    js = (STATIC / "app.js").read_text()
    assert "function restoreScroll()" in js
    assert "restoreScroll();" in js
    assert "sessionStorage.setItem(SCROLL_KEY" in js
    # One-shot: consumed on read, so an ordinary later visit starts at the top.
    assert "sessionStorage.removeItem(SCROLL_KEY)" in js
    # A canceled [data-confirm] never navigates - it must not leave an entry
    # that fires on whatever page loads next.
    assert "if (ev.defaultPrevented) return;" in js
    # An explicit #anchor - a link the user followed on purpose - still wins.
    assert "if (location.hash) return;" in js


def test_todo_actions_redirect_without_an_anchor(client, project):
    """Ticking an item must leave the scroll restore free to do its job.

    This used to redirect to `#todos`, and Wes reported the exact symptom that
    implies: ticking something halfway down the list threw him back up to the
    section heading. `restoreScroll` bails whenever `location.hash` is set, so
    the anchor was suppressing the fix."""
    todo = db.add_todo(project["id"], "something", owner="agent")
    r = client.post(f"/todo/{todo['id']}/toggle", data={"done": "1"}, follow_redirects=False)
    assert r.headers["location"] == f"/project/{project['slug']}"
    assert "#" not in r.headers["location"]


# --- paused projects, muted ------------------------------------------------


def _cells(client):
    html = client.get("/").text
    start = html.index('data-status-zone="active"')
    end = html.index("New idea")
    return html[start:end]


def test_paused_project_gets_the_quiet_border(client):
    p = db.create_project("Resting", slug="resting", stage="active")
    db.pause_project(p["id"])
    db.create_question(p["id"], "well?")
    grid = _cells(client)
    assert "project-cell resting" in grid
    assert "needs-attention" not in grid


def test_paused_project_keeps_its_question_count_without_the_pulse(client):
    p = db.create_project("Resting", slug="resting", stage="active")
    db.pause_project(p["id"])
    db.create_question(p["id"], "well?")
    db.create_question(p["id"], "and?")
    grid = _cells(client)
    assert 'class="cell-alert quiet"' in grid
    assert ">2</span>" in grid


def test_an_asking_project_folds_away_but_keeps_shouting(client):
    """Under the state model an active project holding an open question folds
    to the Paused shelf ("even if they need user input... I want them in the
    paused/backlog section") - but its question still counts in the nav badge,
    because agent-parked waiting is Wes's to end."""
    p = db.create_project("Mid build", slug="mid", stage="active", build_approved=True)
    db.create_question(p["id"], "which one?")
    html = client.get("/").text
    shelf = html[html.index('id="paused"'):html.index('id="backlog"')]
    assert "Mid build" in shelf
    assert 'class="nav-count">1<' in html


def test_a_paused_project_with_no_questions_is_still_muted(client):
    """The old rule keyed on the question count, so a paused project you had
    already answered went back to looking like every other card."""
    p = db.create_project("Resting", slug="resting", stage="active")
    db.pause_project(p["id"])
    grid = _cells(client)
    assert "project-cell resting" in grid


def test_a_running_paused_project_is_not_muted(client, monkeypatch):
    """An agent working on it is the one thing that outranks 'you put it
    down' - the running border should not be dimmed out from under it."""
    from app import main

    p = db.create_project("Resting", slug="resting", stage="active")
    db.pause_project(p["id"])
    monkeypatch.setattr(
        main,
        "active_run_snapshot",
        lambda: {"active": True, "project_ids": [p["id"]], "runs": [], "run_ids": ""},
    )
    grid = _cells(client)
    assert "project-cell resting" not in grid
    assert "running" in grid


def test_muted_styles_exist():
    css = (STATIC / "style.css").read_text()
    assert ".project-cell.resting" in css
    assert ".cell-alert.quiet" in css


# --- icons -----------------------------------------------------------------


def test_favicon_ico_is_served_from_the_origin_root(client):
    """Browsers probe /favicon.ico whatever the <link> tags say, and the 404
    there is the likeliest reason the dashboard tab had no icon."""
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/x-icon"
    assert r.content[:4] == b"\x00\x00\x01\x00"  # ICO magic


def test_every_page_declares_the_icons(client, project):
    from app import live

    for url in ("/", "/project/fridge", "/settings", "/activity", "/questions", "/memory"):
        html = client.get(url).text
        assert 'rel="icon" type="image/svg+xml"' in html, url
        # PNG links too: they are the one icon form every browser agrees on
        # (Safari ignores SVG favicons entirely).
        assert 'rel="icon" type="image/png" sizes="32x32"' in html, url
        assert 'rel="icon" type="image/png" sizes="16x16"' in html, url
        # Versioned, because Chrome caches a *failed* icon fetch against the
        # URL - one request into a restart gap and the tab stayed blank until
        # the URL changed.
        assert 'href="/static/favicon.ico?v=' in html, url
        # ...and salted with the boot id, because the mtime version alone only
        # changes when the icon file is edited, which is roughly never - the
        # poisoned URL must die on the restart that poisoned it.
        assert f"b={live.BOOT_ID}" in html, url
        assert 'rel="apple-touch-icon"' in html, url


def test_png_favicons_exist_at_their_declared_sizes():
    import struct

    for size in (16, 32):
        raw = (STATIC / f"favicon-{size}.png").read_bytes()
        w, h = struct.unpack(">II", raw[16:24])
        assert (w, h) == (size, size)


def test_app_icon_files_exist_at_the_sizes_the_manifest_claims():
    import json
    import struct

    manifest = json.loads((STATIC / "manifest.webmanifest").read_text())
    pngs = [i for i in manifest["icons"] if i["type"] == "image/png"]
    assert {i["sizes"] for i in pngs} == {"192x192", "512x512"}
    for icon in pngs:
        path = STATIC / icon["src"].split("/")[-1]
        assert path.exists(), icon["src"]
        # PNG IHDR: 8 byte signature, 4 length, 4 type, then width/height.
        w, h = struct.unpack(">II", path.read_bytes()[16:24])
        assert f"{w}x{h}" == icon["sizes"]


def test_apple_touch_icon_is_180px_and_opaque():
    """iOS composites the touch icon onto white, so an RGBA icon with a
    transparent glow would get a white halo around a deliberately dark tile."""
    import struct

    raw = (STATIC / "apple-touch-icon.png").read_bytes()
    w, h = struct.unpack(">II", raw[16:24])
    assert (w, h) == (180, 180)
    color_type = raw[25]
    assert color_type in (0, 2, 3)  # grayscale / truecolor / palette, no alpha


def test_the_tidied_names_replaced_the_attachment_filename():
    """Wes asked for his artwork under a name that is not
    `0001-project_portal-app-icon-clean.png`."""
    names = {p.name for p in STATIC.iterdir()}
    assert {"icon-192.png", "icon-512.png", "apple-touch-icon.png", "favicon.ico"} <= names
    assert not any(n.startswith("0001-") for n in names)

"""The strip, the rail and the Telegram status show what an agent SAID, not
only the tool call it is on.

Until 2026-09-02 every one-line "what is this run doing" surface showed the
newest rendered log line, which for almost every assistant turn is the tool
call that ended it - "> Bash(cd /srv/portal && venv/bin/python -
<<'EOF' import sqlite3" - and on a phone that says nothing. The agent narrates
its work because its harness asks it to, so app/runlog.py `said` picks those
words out of each event, the worker keeps the newest of them on the run row
(`runs.last_said`) through the tool-only turns that follow, and the strip
paints them above the tool line from every poll.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import db, runlog, worker

from tests.test_midrun import _project

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool(name: str = "Bash", **kw) -> dict:
    return {"type": "tool_use", "name": name, "input": kw or {"command": "ls"}}


# --- picking the words out of an event -------------------------------------

def test_said_is_the_prose_of_an_assistant_turn_and_nothing_else():
    assert runlog.said(_assistant(_text("Checking the run page."), _tool())) == "Checking the run page."
    assert runlog.said(_assistant(_tool())) == ""
    assert runlog.said(_assistant({"type": "thinking", "thinking": "hmm"}, _tool())) == ""
    assert runlog.said({"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}) == ""
    assert runlog.said({"type": "system", "subtype": "init"}) == ""
    # A user turn can carry text too (the prompt echo, a mid-run note); it is
    # not the agent speaking.
    assert runlog.said({"type": "user", "message": {"content": [_text("a note for you")]}}) == ""
    assert runlog.said({"type": "result", "num_turns": 3}) == ""


def test_said_takes_a_plain_string_content_too():
    assert runlog.said({"type": "assistant", "message": {"content": "On it."}}) == "On it."


def test_said_flattens_lines_and_drops_markdown_furniture():
    text = "## What I did\n\n- fixed the strip\n- **kept** the tool line\n\n> quoted\n1. one"
    assert runlog.said(_assistant(_text(text))) == "What I did fixed the strip **kept** the tool line quoted one"
    two = _assistant(_text("First block."), _tool(), _text("  Second   block.  "))
    assert runlog.said(two) == "First block. Second block."


def test_said_clips_long_prose_at_a_word_boundary():
    words = " ".join(f"word{i}" for i in range(80))
    out = runlog.said(_assistant(_text(words)))
    assert len(out) <= runlog.MAX_SAID_CHARS + 1
    assert out.endswith("…")
    assert not out[:-1].endswith(" ")
    # Cut between words, never inside one.
    assert out[:-1].split(" ")[-1] in words.split(" ")
    # A single unbroken token is clipped hard rather than emptied.
    blob = "x" * 400
    out = runlog.said(_assistant(_text(blob)))
    assert out == "x" * runlog.MAX_SAID_CHARS + "…"
    # ...and one short word before a huge token does not shrink the line to
    # that word: the boundary only counts in the back half of the cut.
    out = runlog.said(_assistant(_text("word " + "y" * 300)))
    assert out.startswith("word yyyy") and len(out) == runlog.MAX_SAID_CHARS + 1
    # Trailing punctuation left by the cut is dropped before the ellipsis.
    text = ("a" * 190) + " and, " + "b" * 50
    assert runlog.said(_assistant(_text(text))) == ("a" * 190) + " and…"


# --- the run row keeps the words through the tool calls that follow --------

def test_live_logger_keeps_the_words_while_the_tool_line_moves(temp_data_dir):
    run_id = db.create_run(None, "build", "opus")
    on_event = worker._live_logger(run_id)  # noqa: SLF001

    ev = _assistant(_text("Reading the strip."), _tool("Read", file_path="a"))
    on_event(ev, runlog.render_event(ev))
    run = db.get_run(run_id)
    assert run["last_said"] == "Reading the strip."
    assert run["last_activity"] == "> Read(a)"

    ev = _assistant(_tool("Bash", command="ls"))
    on_event(ev, runlog.render_event(ev))
    run = db.get_run(run_id)
    assert run["last_said"] == "Reading the strip.", "a tool-only turn must not blank the words"
    assert run["last_activity"] == "> Bash(ls)"

    ev = _assistant(_text("Now the poller."))
    on_event(ev, runlog.render_event(ev))
    assert db.get_run(run_id)["last_said"] == "Now the poller."


def test_update_run_activity_without_words_leaves_them_alone(temp_data_dir):
    run_id = db.create_run(None, "build", "opus")
    db.update_run_activity(run_id, "> Bash(ls)", 1, "Hello.")
    db.update_run_activity(run_id, "< ok (1 line)", 2)
    run = db.get_run(run_id)
    assert run["last_said"] == "Hello." and run["last_activity"] == "< ok (1 line)"


# --- the surfaces -----------------------------------------------------------

@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _strip(page: str) -> str:
    start = page.index('id="live-run"')
    return page[start:page.index("section-head", start)]


def test_strip_and_api_carry_the_words_and_hide_the_line_until_there_are_any(client):
    project = _project()
    run_id = db.create_run(project["id"], "build", "opus")
    db.update_run_activity(run_id, "> Bash(ls)", 1)
    strip = _strip(client.get("/").text)
    assert '<div class="live-said" hidden></div>' in strip
    assert client.get("/api/active-run").json()["runs"][0]["last_said"] == ""

    db.update_run_activity(run_id, "> Read(app.js)", 2, "Reading the poller & its stub.")
    strip = _strip(client.get("/").text)
    assert '<div class="live-said">Reading the poller &amp; its stub.</div>' in strip
    assert strip.index("live-said") < strip.index("live-activity"), "words above the tool line"
    snap = client.get("/api/active-run").json()
    assert snap["runs"][0]["last_said"] == "Reading the poller & its stub."
    assert snap["runs"][0]["last_activity"] == "> Read(app.js)"


def test_rail_says_what_the_agent_said_under_its_name(client):
    project = _project()
    run_id = db.create_run(project["id"], "build", "opus")
    db.update_run_activity(run_id, "> Bash(ls)", 1)
    page = client.get("/").text
    assert f'id="rail-run-{run_id}"' in page
    assert "rail-run-said" not in page
    db.update_run_activity(run_id, "> Bash(ls)", 2, "Wiring the rail.")
    page = client.get("/").text
    li = page[page.index(f'id="rail-run-{run_id}"'):]
    li = li[:li.index("</li>")]
    assert '<span class="rail-run-said">Wiring the rail.</span>' in li
    assert 'title="Wiring the rail."' in li


def test_telegram_status_puts_the_words_before_the_tool_line(temp_data_dir):
    from app import telegram_bot

    project = _project()
    run_id = db.create_run(project["id"], "build", "opus")
    db.update_run_activity(run_id, "> Bash(ls)", 1)
    text = telegram_bot._status_summary()  # noqa: SLF001
    assert "  > Bash(ls)" in text
    db.update_run_activity(run_id, "> Bash(ls)", 2, "Fixing the status.")
    text = telegram_bot._status_summary()  # noqa: SLF001
    assert text.index("  Fixing the status.") < text.index("  > Bash(ls)")


# --- the poller -------------------------------------------------------------

@pytest.fixture(scope="module")
def painted():
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "live_strip.mjs"
    out = subprocess.run([bun, str(harness), str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_poller_paints_the_words_when_they_arrive(painted):
    assert painted["silent"]["saidHidden"] is True and painted["silent"]["said"] == ""
    spoke = painted["spoke"]
    assert spoke["saidHidden"] is False
    assert spoke["said"] == "Checking the run page for holds."
    assert spoke["activity"] == "> Read(app/templates/run.html)"


def test_poller_hides_the_line_for_a_run_that_has_said_nothing(painted):
    # Runs in the older scenarios send no `last_said` at all; the line stays hidden.
    assert painted["resumed"]["saidHidden"] is True and painted["resumed"]["said"] == ""


def test_template_and_poller_agree_on_the_words_hook():
    html = (ROOT / "app" / "templates" / "index.html").read_text()
    assert 'class="live-said"' in html
    assert 'querySelector(".live-said")' in APP_JS.read_text()

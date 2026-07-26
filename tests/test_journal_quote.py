"""Highlight a passage in the journal, then ask or note about it.

Wes, 2026-07-25: "When highlighting text in the journal, allow me to ask a
question or make a new note with reference to that text. Asking a question
being the mode where it is sort of asked in parallel and not factored into the
rest of the journal context and is not allowed to code and whatnot."

Two halves, pinned here:

1. The reference - a selection travels as a `quote` form field and is framed
   into the journal entry and the prompt by app/quoting.py, identically for
   both modes.
2. The parallel mode - an ask and its answer are a side thread, left out of the
   journal tail a RUN reads while staying in the one an ASK reads. The
   exclusion is by (author, kind) pair, because `user/answer` (Wes answering a
   portal question) must keep reaching runs.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import agent_runner, ask, config, db, quoting
from app.main import app

STATIC = Path(config.APP_ROOT) / "app" / "static"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _no_leaked_asks():
    # ask._PENDING is module-level and survives the per-test database, so a
    # project id reused by the next test would look like it already had an ask
    # in flight and the route would silently do nothing.
    ask._PENDING.clear()  # noqa: SLF001
    yield
    ask._PENDING.clear()  # noqa: SLF001


def _project(**kw):
    return db.create_project(
        title=kw.get("title", "Manabase"),
        description=kw.get("description", "A thing."),
        kind="software",
    )


# --------------------------------------------------------------------------
# quoting.normalise
# --------------------------------------------------------------------------

def test_a_selection_is_tidied_not_reproduced_verbatim():
    raw = "  first line   \n\n\n\n  second\tline  \n"
    assert quoting.normalise(raw) == "first line\n\n  second line"


def test_crlf_and_lone_cr_become_newlines():
    assert quoting.normalise("a\r\nb\rc") == "a\nb\nc"


def test_an_empty_or_whitespace_selection_normalises_away():
    assert quoting.normalise("") == ""
    assert quoting.normalise("   \n\n\t ") == ""
    assert quoting.normalise(None) == ""


def test_a_runaway_selection_is_capped_at_a_word_boundary():
    raw = ("word " * 400).strip()
    out = quoting.normalise(raw)
    assert len(out) <= quoting.MAX_QUOTE_CHARS + 4  # + the " ..." marker
    assert out.endswith(" ...")
    # Cut between words, so the quote never ends mid-word.
    assert out[: -len(" ...")].endswith("word")


def test_a_long_run_with_no_spaces_is_still_capped():
    out = quoting.normalise("x" * 5000)
    assert out.endswith(" ...")
    assert len(out) <= quoting.MAX_QUOTE_CHARS + 4


# --------------------------------------------------------------------------
# quoting.frame
# --------------------------------------------------------------------------

def test_frame_puts_the_quote_above_the_body_as_a_blockquote():
    out = quoting.frame("the guardrail toggle", "why is this off by default?")
    assert out == (
        "> the guardrail toggle\n\n"
        f"{quoting.QUOTE_CAPTION}\n\n"
        "why is this off by default?"
    )


def test_a_blank_line_inside_a_quote_keeps_its_marker():
    # Otherwise markdown splits one quote into two blocks with a paragraph of
    # the caption's text stranded between them.
    out = quoting.frame("first para\n\nsecond para", "hm?")
    assert "> first para\n>\n> second para" in out


def test_no_quote_leaves_the_body_completely_alone():
    # Every plain note and every typed ask goes through frame(), so this is the
    # path that must not change at all.
    assert quoting.frame("", "just a note") == "just a note"
    assert quoting.frame("   ", "just a note") == "just a note"


def test_a_quote_with_nothing_typed_is_still_a_message():
    # Highlight and press "ask about this" without typing: "what is this?" is a
    # real question and must not be dropped as empty.
    out = quoting.frame("the spend-down offer", "")
    assert out.startswith("> the spend-down offer")
    assert quoting.QUOTE_CAPTION in out


def test_frame_of_nothing_is_nothing():
    assert quoting.frame("", "") == ""


# --------------------------------------------------------------------------
# The routes accept a quote
# --------------------------------------------------------------------------

def test_a_quoted_note_lands_in_the_journal_with_the_passage(client):
    p = _project()
    r = client.post(
        f"/project/{p['slug']}/note",
        data={"note": "this bit is wrong", "quote": "2000 pytest cases", "then": "queue"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    entry = db.list_journal(p["id"])[0]
    assert entry["kind"] == "note"
    assert "> 2000 pytest cases" in entry["content_md"]
    assert "this bit is wrong" in entry["content_md"]


def test_a_note_with_only_a_quote_is_not_treated_as_an_empty_box(client):
    p = _project()
    client.post(
        f"/project/{p['slug']}/note",
        data={"note": "", "quote": "the report nudge", "then": "queue"},
        follow_redirects=False,
    )
    entries = db.list_journal(p["id"])
    assert entries and "> the report nudge" in entries[0]["content_md"]


def test_a_plain_note_is_unchanged_by_the_new_field(client):
    p = _project()
    client.post(
        f"/project/{p['slug']}/note",
        data={"note": "plain words", "then": "queue"},
        follow_redirects=False,
    )
    assert db.list_journal(p["id"])[0]["content_md"] == "plain words"


def test_a_quoted_ask_journals_and_prompts_with_the_passage(client, monkeypatch):
    p = _project()
    seen: dict = {}

    async def fake_answer(project_id, question, reply_chat_id=None):
        seen["question"] = question
        return "ok"

    monkeypatch.setattr(ask, "answer", fake_answer)
    r = client.post(
        f"/project/{p['slug']}/ask",
        data={"question": "why?", "quote": "the write guardrail"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    entry = db.list_journal(p["id"])[0]
    assert entry["author"] == "user" and entry["kind"] == "ask"
    assert "> the write guardrail" in entry["content_md"]
    assert "why?" in entry["content_md"]


def test_an_ask_with_only_a_quote_still_starts(client, monkeypatch):
    p = _project()
    started: list = []

    def fake_start(project_id, question, reply_chat_id=None):
        started.append(question)
        return 1

    monkeypatch.setattr(ask, "start", fake_start)
    client.post(
        f"/project/{p['slug']}/ask",
        data={"question": "", "quote": "per-task git worktrees"},
        follow_redirects=False,
    )
    assert started and "> per-task git worktrees" in started[0]


def test_an_empty_ask_with_no_quote_still_starts_nothing(client, monkeypatch):
    p = _project()
    started: list = []
    monkeypatch.setattr(ask, "start", lambda *a, **k: started.append(a))
    client.post(f"/project/{p['slug']}/ask", data={"question": "  "}, follow_redirects=False)
    assert started == []


# --------------------------------------------------------------------------
# The side thread: excluded from a run's journal, kept for an ask's
# --------------------------------------------------------------------------

def test_list_journal_asc_can_exclude_author_kind_pairs():
    p = _project()
    db.add_journal(p["id"], "user", "note", "a note")
    db.add_journal(p["id"], "user", "ask", "a question")
    db.add_journal(p["id"], "agent", "answer", "an answer")
    db.add_journal(p["id"], "agent", "progress", "a report")

    kept = [r["content_md"] for r in db.list_journal_asc(p["id"], exclude=db.SIDE_THREAD)]
    assert kept == ["a note", "a report"]
    # Unfiltered is still everything, in order.
    assert len(db.list_journal_asc(p["id"])) == 4


def test_wes_answering_a_portal_question_is_not_a_side_thread():
    # `user/answer` is Wes replying to the portal's own question - an
    # instruction. Only `agent/answer` (an ask's reply) is marginalia, which is
    # why the exclusion matches the pair and not the kind.
    assert db.is_side_thread("agent", "answer") is True
    assert db.is_side_thread("user", "ask") is True
    assert db.is_side_thread("user", "answer") is False
    assert db.is_side_thread("user", "note") is False

    p = _project()
    db.add_journal(p["id"], "user", "answer", "**Q:** build it? **A:** yes")
    kept = [r["content_md"] for r in db.list_journal_asc(p["id"], exclude=db.SIDE_THREAD)]
    assert kept == ["**Q:** build it? **A:** yes"]


def test_the_exclusion_happens_before_the_limit_so_asks_dont_eat_the_tail():
    # A chatty ask thread must not push real journal entries out of the window
    # a run sees.
    p = _project()
    for i in range(5):
        db.add_journal(p["id"], "user", "ask", f"q{i}")
        db.add_journal(p["id"], "agent", "answer", f"a{i}")
    for i in range(3):
        db.add_journal(p["id"], "agent", "progress", f"report {i}")

    rows = db.list_journal_asc(p["id"], limit=3, exclude=db.SIDE_THREAD)
    assert [r["content_md"] for r in rows] == ["report 0", "report 1", "report 2"]


def test_a_runs_prompt_leaves_the_ask_thread_out(tmp_path, monkeypatch):
    p = _project()
    db.add_journal(p["id"], "agent", "progress", "SHIPPED-THE-THING")
    db.add_journal(p["id"], "user", "ask", "WES-ASKED-THIS")
    db.add_journal(p["id"], "agent", "answer", "THE-ASK-REPLY")

    prompt = agent_runner.build_prompt("build", db.get_project(p["id"]))
    assert "SHIPPED-THE-THING" in prompt
    assert "WES-ASKED-THIS" not in prompt
    assert "THE-ASK-REPLY" not in prompt


def test_an_asks_prompt_keeps_the_ask_thread_for_continuity():
    # The side thread has its own memory: a follow-up question sees the earlier
    # question and its answer, even though a run never does.
    p = _project()
    db.add_journal(p["id"], "user", "ask", "WES-ASKED-THIS")
    db.add_journal(p["id"], "agent", "answer", "THE-ASK-REPLY")

    prompt = ask.build_prompt(db.get_project(p["id"]), "and what about the other one?")
    assert "WES-ASKED-THIS" in prompt
    assert "THE-ASK-REPLY" in prompt


def test_the_journal_badges_a_side_thread_entry(client):
    p = _project()
    db.add_journal(p["id"], "user", "ask", "a question")
    db.add_journal(p["id"], "user", "note", "a note")
    html = client.get(f"/project/{p['slug']}").text
    assert "badge-aside" in html
    assert "side thread" in html
    assert "journal-aside" in html


# --------------------------------------------------------------------------
# The front end
# --------------------------------------------------------------------------

def _js():
    return (STATIC / "app.js").read_text()


def test_js_only_offers_the_bar_for_a_selection_inside_a_journal_entry():
    js = _js()
    assert '"#journal .journal-entry .content"' in js
    # Both ends of the selection are checked, so a drag that runs out of the
    # entry into the page furniture does not quote the furniture.
    assert "inJournalContent(sel.anchorNode) || !inJournalContent(sel.focusNode)" in js


def test_js_listens_for_selectionchange_so_touch_selection_works():
    # mouseup never fires for an iOS text selection; selectionchange does.
    js = _js()
    assert 'addEventListener("selectionchange"' in js
    assert "selBarSync" in js


def test_js_drops_the_quote_into_the_existing_forms_not_a_new_one():
    js = _js()
    assert '".ask-form"' in js and '".note-form"' in js
    assert 'name="quote"' in js
    # The passage is written as a value/textContent, never as HTML - it came
    # off the page and must not be able to put markup back into it.
    assert ".textContent = text" in js
    assert "innerHTML = text" not in js


def test_the_bar_and_the_chip_survive_a_live_refresh():
    js = _js()
    keep = js.split("var MORPH_KEEP =")[1].split(";")[0]
    assert "#sel-actions" in keep
    assert ".quote-chip" in keep


def test_css_floats_the_bar_and_styles_the_chip():
    css = (STATIC / "style.css").read_text()
    assert "#sel-actions" in css
    assert ".quote-chip" in css
    assert ".badge-aside" in css


def test_css_stacks_the_bar_when_it_is_parked_in_the_gutter():
    # Wes, 2026-07-25: show the buttons off to the right of the journal when
    # there is room. A stacked column fits a gutter a two-button row would not.
    css = (STATIC / "style.css").read_text()
    assert "#sel-actions.side" in css
    assert "flex-direction: column" in css.split("#sel-actions.side")[1][:120]


# --------------------------------------------------------------------------
# Where the bar actually lands
#
# These run the REAL selBarShow() out of app.js under bun against a stub DOM
# (tests/js/selbar_geometry.mjs) and read back the coordinates it set. Matching
# strings in the source would only prove the code mentions the journal's right
# edge; this proves the arithmetic.
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def selbar_geometry():
    import json
    import shutil
    import subprocess

    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun is not on PATH")
    harness = Path(__file__).parent / "js" / "selbar_geometry.mjs"
    out = subprocess.run(
        [bun, str(harness), str(STATIC / "app.js")],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_wide_window_parks_the_bar_in_the_gutter_beside_the_journal(selbar_geometry):
    # 1080px page column centred in a 1920px window -> a 420px gutter each side.
    wide = selbar_geometry["wide"]
    assert wide["side"] is True
    assert wide["left"] == 1510  # journal right edge (1500) + the 10px gap
    # Vertically centred on the selection (top 400, height 24) for a 66px bar.
    assert wide["top"] == 379
    assert wide["hidden"] is False
    assert wide["quote"] == "a quoted passage"


def test_a_phone_falls_back_to_floating_above_the_selection(selbar_geometry):
    # The journal card runs to the window edge: no gutter, so the bar goes back
    # over the selection as a row, horizontally centred on it.
    narrow = selbar_geometry["narrow"]
    assert narrow["side"] is False
    assert narrow["left"] == 50   # selection centre 170 - half of a 240px row
    assert narrow["top"] == 358   # selection top 400 - 34px row - the 8px gap


def test_a_gutter_too_thin_for_the_bar_does_not_spill_off_the_monitor(selbar_geometry):
    # The whole of Wes's ask is "without going off the monitor": 1130 + 10 +
    # 130 + 8 is wider than the 1180px window, so this must NOT take the side.
    thin = selbar_geometry["thinGutter"]
    assert thin["side"] is False
    assert thin["left"] == 130


def test_the_bar_stays_on_screen_at_the_edges(selbar_geometry):
    # Beside the journal but level with a selection at the very top: clamped to
    # the 8px margin rather than hanging off the top of the window.
    assert selbar_geometry["wideTop"]["side"] is True
    assert selbar_geometry["wideTop"]["top"] == 8
    # No room above the selection on a phone: drop below it instead of covering
    # the words being quoted.
    below = selbar_geometry["noRoomAbove"]
    assert below["side"] is False
    assert below["top"] == 36  # selection bottom 28 + the 8px gap

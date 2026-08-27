"""Reading an agent transcript.

Wes, 2026-07-28: "For the agent console (or any of the live or non-live agent
transcript views), please improve the readability of it. For example, make all
the tool calls and '>' lines show as indented gray/dimmed text with the other
lines where the agent is just talking or thinking show as non-indented text
that is not dimmed."

The whole console used to be one dim color, and the indentation was the wrong
way round: `render_event` gave the agent's own prose a two-space indent and put
tool calls at column 0. So the machinery was the foreground and the thing you
opened the transcript to read was the inset.

Three things can break here, and they are tested in three different ways:

- What each kind of line is classified as, and how a chunk split across two
  polls is put back together. That is browser behavior, so it runs for real
  under bun (tests/js/console_lines.mjs) rather than being asserted about the
  source text.
- The MARKER TABLE agreeing between app/runlog.py (which writes the lines) and
  app/static/app.js (which has to classify lines arriving mid-run, with no
  round trip available to ask Python). Two copies of one table is a drift
  waiting to happen, so the drift is what is pinned.
- The stylesheet actually doing what the note asked - dimmed and indented for
  machinery, neither for prose. No DOM assertion can see a color, so the
  rules are read out of style.css.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import runlog

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"
STYLE_CSS = ROOT / "app" / "static" / "style.css"


# --------------------------------------------------------------------------
# What render_event writes
# --------------------------------------------------------------------------

def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def test_prose_is_unmarked_and_unindented():
    """The thing being read sits at the margin; the machinery is what moves."""
    lines = runlog.render_event(_assistant({"type": "text", "text": "Let me look.\nThen fix it."}))
    assert lines == ["Let me look.", "Then fix it."]


def test_a_tool_call_carries_the_tool_marker():
    lines = runlog.render_event(
        _assistant({"type": "tool_use", "name": "Bash", "input": {"command": "git status"}})
    )
    assert lines == ["> Bash(git status)"]


def test_thinking_reaches_the_transcript_at_all():
    """It was dropped on the floor before this.

    Wes named it - "where the agent is just talking or thinking" - and a
    transcript that shows every tool call but none of the reasoning between
    them is a list of actions with the why cut out.
    """
    lines = runlog.render_event(
        _assistant({"type": "thinking", "thinking": "Check the tests first.\n\nThen edit."})
    )
    assert lines == ["~ Check the tests first.", "~ Then edit."]


@pytest.mark.parametrize(
    "written",
    [
        "> a markdown blockquote",
        "* a markdown bullet",
        "! surprising",
        "< less than",
        "~ approximately",
    ],
)
def test_markdown_prose_cannot_masquerade_as_machinery(written):
    """Agent prose is markdown, and markdown starts lines with these.

    Unescaped, the agent's own quoted line would be drawn as a tool call: dim,
    indented, and attributed to the machine rather than to the agent. One
    leading space is all it takes, because a marker only counts at position 0.
    """
    lines = runlog.render_event(_assistant({"type": "text", "text": written}))
    assert lines == [" " + written]
    assert lines[0][0] not in runlog.MARKERS


def test_escaping_leaves_indentation_inside_prose_alone():
    """A fenced code block means its indentation - nothing is stripped."""
    assert runlog.escape_prose("    def foo():") == "    def foo():"
    assert runlog.escape_prose("plain") == "plain"


def test_every_line_render_event_can_write_is_classifiable():
    """Whatever it writes, the reader must have a rule for.

    Built from real events rather than from a list of strings, so a new line
    added to render_event without a marker shows up here.
    """
    events = [
        {"type": "system", "subtype": "init", "model": "claude-opus-5", "tools": [1, 2]},
        _assistant({"type": "text", "text": "words"}),
        _assistant({"type": "thinking", "thinking": "hmm"}),
        _assistant({"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}),
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "no", "is_error": True}]}},
        {"type": "result", "num_turns": 7},
        {"type": "result", "is_error": True, "subtype": "error_max_turns"},
    ]
    for event in events:
        for line in runlog.render_event(event):
            marker = line[:1]
            assert marker not in runlog.MARKERS or line[1:2] in ("", " "), (
                f"{line!r} starts with a marker but not a marker plus a space, so the "
                "reader will draw it as prose"
            )


# --------------------------------------------------------------------------
# The two copies of the marker table
# --------------------------------------------------------------------------

def test_app_js_knows_exactly_the_markers_runlog_writes():
    """The one thing that cannot be caught at runtime.

    A marker added to runlog.py and not to app.js does not raise anything - the
    line simply renders as if the agent had said it, which is precisely the
    confusion this whole change exists to remove.
    """
    source = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"var CONSOLE_KINDS = \{(.*?)\};", source, re.S)
    assert match, "app.js no longer declares CONSOLE_KINDS - the reader has moved"
    in_js = dict(re.findall(r'"(.)":\s*"(\w+)"', match.group(1)))
    assert in_js == runlog.MARKERS, (
        "app/static/app.js and app/runlog.py disagree about what a transcript "
        f"line means: js={in_js} python={runlog.MARKERS}"
    )


# --------------------------------------------------------------------------
# The behavior, run for real under bun
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def drawn():
    bun = shutil.which("bun")
    if not bun:  # pragma: no cover - bun is present on the machines that matter
        pytest.skip("bun is not installed")
    proc = subprocess.run(
        [bun, str(Path(__file__).parent / "js" / "console_lines.mjs"), str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_each_kind_of_line_is_classified_by_the_real_reader(drawn):
    kinds = {json.loads(k): v for k, v in drawn["kinds"].items()}
    assert kinds["> Bash(git status)"] == "tool"
    assert kinds["< ok (12 lines)"] == "result"
    assert kinds["! error: something broke"] == "error"
    assert kinds["* session start  model=claude-opus-5  tools=19"] == "status"
    assert kinds["~ I should check the tests first"] == "think"
    assert kinds["Let me look at the settings page."] == "say"
    # An escaped markdown line is prose, which is the whole point of escaping.
    assert kinds[" > a markdown quote, escaped by runlog.escape_prose"] == "say"
    assert kinds[" * a markdown bullet, escaped the same way"] == "say"
    # A marker needs its space. "*bold text*" is not a status line.
    assert kinds["*not a status line, no space after the star"] == "say"
    # ...and "![alt](url)" is an image, not an error.
    assert kinds["![a screenshot](shots/x.png)"] == "say"
    # Prose keeps its own indentation: inside a code block it means something.
    assert kinds["    def foo():"] == "say"


def test_a_paragraph_of_reasoning_does_not_draw_a_column_of_tildes(drawn):
    """runlog writes one `~ ` per SOURCE line, so a wrapped thought arrives as
    a run of marked lines. Drawn, the marker is dropped: `.cl-think` is the
    only kind that sits at the margin in italic, so it already says everything
    the tilde said, and three lines of reasoning were showing three tildes.

    The machinery keeps its markers deliberately - `.cl-tool` and `.cl-result`
    are styled identically, so `>` and `<` are the only thing distinguishing a
    call from its answer.
    """
    lines = drawn["thinkingParagraph"]
    assert [line["kind"] for line in lines] == ["think"] * 3 + ["tool", "result"]

    thoughts = [line["text"] for line in lines if line["kind"] == "think"]
    assert not any(text.startswith("~") for text in thoughts), thoughts
    assert thoughts[0] == "The settings page is 500ing. That smells like a template"
    assert thoughts[2] == "check is whether it was rendered since the last restart."

    # ...and the call/answer pair still says which way it went.
    assert lines[3]["text"] == "> Read(app/main.py)"
    assert lines[4]["text"] == "< ok (3 lines)"


def test_the_raw_log_still_marks_every_line_of_thinking():
    """The marker is dropped when DRAWING, not when writing.

    Two reasons it has to stay in the file. The reader classifies one line at a
    time with no memory, which is what makes a line split across two polls come
    out right; and `cat`ing the log has to show the same shape the browser does.
    """
    lines = runlog.render_event(
        {
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": "one\ntwo\nthree"}]},
        }
    )
    assert lines == ["~ one", "~ two", "~ three"]


def test_a_transcript_draws_one_element_per_line(drawn):
    assert drawn["wholeTranscript"] == [
        {"kind": "status", "text": "* session start"},
        {"kind": "say", "text": "Let me look."},
        {"kind": "tool", "text": "> Read(app/main.py)"},
        {"kind": "result", "text": "< ok (3 lines)"},
    ]


def test_a_line_split_across_two_polls_ends_up_as_one_line(drawn):
    """The poller reads by byte offset, so this happens on any busy run.

    The half-line is drawn straight away - a console that waited for the
    newline would visibly lag the run it is showing - and then redrawn whole,
    rather than being left in place with its remainder appended beside it.
    """
    assert drawn["afterFirstPoll"][-1] == {"kind": "tool", "text": "> Read(app/ma"}
    assert drawn["afterSecondPoll"] == [
        {"kind": "say", "text": "Let me look."},
        {"kind": "tool", "text": "> Read(app/main.py)"},
        {"kind": "result", "text": "< ok (3 lines)"},
    ]


def test_a_split_between_a_marker_and_its_space_still_classifies(drawn):
    """The case that would go wrong if the tail were committed, not redrawn.

    "*" on its own is a status line; "*run complete" (no space) is prose. A
    reader that kept its first guess would have this one permanently wrong.
    """
    assert drawn["afterMarkerSplit"] == [
        {"kind": "status", "text": "* run complete  (7 turns)"}
    ]


def test_the_first_paint_replaces_rather_than_appends(drawn):
    assert drawn["afterReplace"] == [{"kind": "tool", "text": "> Read(b)"}]


# --------------------------------------------------------------------------
# What it looks like
# --------------------------------------------------------------------------

def _declarations(selector: str) -> dict[str, str]:
    """The declarations of the first rule whose selector list contains one.

    Comments are stripped first, and not as tidiness: a selector is matched by
    splitting the text before the brace on commas, and these rules are
    documented in prose that contains commas - so an un-stripped comment
    leaves the real selector welded to the tail of the sentence above it and
    every lookup misses.
    """
    css = re.sub(r"/\*.*?\*/", "", STYLE_CSS.read_text(encoding="utf-8"), flags=re.S)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = [s.strip() for s in match.group(1).split(",")]
        if selector in selectors:
            return {
                k.strip(): v.strip()
                for k, _, v in (d.partition(":") for d in match.group(2).split(";"))
                if k.strip()
            }
    raise AssertionError(f"no rule in style.css for {selector!r}")


def test_the_machinery_is_indented_and_the_agents_words_are_not():
    """Wes's note, as a rule-level contract.

    A DOM test cannot see this: the test shims apply no CSS at all, so the
    classes could be perfect and every line still render identically.
    """
    machinery = _declarations(".cl-tool")
    assert machinery["padding-left"] == "2ch"
    assert machinery["color"] == "var(--terminal-dim)"

    said = _declarations(".cl-say")
    assert "padding-left" not in said, "the agent's own words must sit at the margin"
    assert said["color"] == "var(--terminal-fg)", "prose must not be dimmed"

    thought = _declarations(".cl-think")
    assert "padding-left" not in thought
    assert thought["color"] == "var(--terminal-fg)"


def test_a_failure_is_only_slightly_red():
    """Wes, 2026-08-06: "make them less offensive to look at like something
    has gone wrong ... just slightly red." The 2026-08-06 triage showed 82% of
    error lines are the agent's own try/fail/adjust loop, so the full alarm
    red came off: the color is the machinery's dim gray with red mixed in,
    with the plain dim gray declared first as the no-color-mix fallback."""
    error = _declarations(".cl-error")
    assert error["padding-left"] == "2ch"
    # Duplicate `color` keys collapse to the last declaration, which must be
    # the mix; the fallback's presence is asserted on the raw rule text.
    assert error["color"].startswith("color-mix(")
    assert "var(--ansi-red)" in error["color"]
    assert "var(--terminal-dim)" in error["color"]
    rule = STYLE_CSS.read_text(encoding="utf-8").split(".cl-error {")[1].split("}")[0]
    assert rule.count("color:") == 2, "the plain dim fallback must stay"


def test_a_transcript_line_is_a_block():
    """So the indent reaches a wrapped tool call's continuation lines.

    An inline element's padding lands on its first line only, which would
    leave a long Bash command starting indented and finishing at the margin -
    exactly the ragged look the indentation is meant to fix.
    """
    assert _declarations(".cl")["display"] == "block"


# --------------------------------------------------------------------------
# Folding the machinery
#
# Wes, 2026-08-01: "For the Agent console, on the top line where it says 'last
# run transcript' or whatever it says when active, have an option that
# compressed down/hides and prints a sort of summary (but not using AI to
# summarize) of what commands have been run and whatnot in between the white
# text it sends. It could say something like '10 tools called' or '5 commands
# run' or whatever it is that happens. Have this option on by default where
# these are compressed down."
#
# "Not using AI to summarize" is the design constraint and it is met by
# construction: the fold COUNTS lines it has already classified, so it can
# never claim something the transcript does not say.
# --------------------------------------------------------------------------

def test_a_run_of_tool_calls_collapses_to_one_line_that_counts_them(drawn):
    rows = drawn["foldedTranscript"]

    assert [r["kind"] for r in rows] == ["say", "fold", "say"]
    # Two Bash calls and one Read, with their three results, in one line.
    assert rows[1]["text"] == "2 commands run · 1 tool called"
    assert rows[1]["hidden"] is True
    assert len(rows[1]["lines"]) == 6


def test_the_prose_around_a_fold_is_untouched(drawn):
    rows = drawn["foldedTranscript"]
    assert rows[0]["text"] == "Let me look at the settings page."
    assert rows[2]["text"] == "Three tests fail."


def test_an_error_folds_with_the_calls_around_it(drawn):
    """Wes, 2026-08-06: "hide these errored lines then from the agent inside
    the tool call collapsed sections". The reversal of the old never-fold rule:
    an error rides the same group as the calls beside it, and the group's head
    counts it, so a collapsed run still admits a failure happened."""
    rows = drawn["neverFolded"]
    kinds = [r["kind"] for r in rows]

    assert kinds == ["status", "fold", "think"]
    assert rows[1]["text"] == "1 command run · 1 tool called · 1 error"
    assert rows[1]["hidden"] is True
    assert "! error: pytest: command not found" in rows[1]["lines"]


def test_a_fold_holding_only_an_error_says_so(drawn):
    """A failure with no calls beside it must not be relabeled "1 line of
    tool output" - the head is the error count alone."""
    rows = drawn["errorOnlyFold"]
    assert [r["kind"] for r in rows] == ["say", "fold", "say"]
    assert rows[1]["text"] == "1 error"
    assert rows[1]["lines"] == ["! error: the workspace fence refused the write"]


def test_status_and_thinking_stay_in_the_clear(drawn):
    """A status line is four a run (session start, run complete) and each is a
    fact about the run rather than machinery; thinking is prose."""
    rows = drawn["neverFolded"]
    assert rows[0]["text"].startswith("* session start")
    assert rows[-1]["text"] == "that is not right"


def test_a_line_split_across_two_polls_is_counted_once(drawn):
    """The partial line is drawn (the console must not lag its own run) and
    then taken back off and redrawn whole. Counted twice, "1 command run" would
    become "2 commands run" for a command that ran once."""
    assert drawn["foldMidSplit"][0]["text"] == "1 command run"
    after = drawn["foldAfterSplit"][0]
    assert after["text"] == "1 command run"
    assert after["lines"] == ["> Bash(pytest -q)", "< ok (3 lines)"]


def test_a_fold_emptied_by_a_redraw_goes_with_its_last_line(drawn):
    """Otherwise the console carries an empty heading counting nothing."""
    after = drawn["foldEmptied"]["after"]
    assert drawn["foldEmptied"]["midCount"] == 2
    assert [r["kind"] for r in after] == ["say", "fold"]
    assert after[1]["text"] == "1 command run"


@pytest.mark.parametrize(
    "index,expected",
    [
        (0, "5 commands run"),
        (1, "10 tools called"),
        # Singulars, because "1 commands run" is the tell that a count was
        # printed rather than written.
        (2, "1 command run · 1 tool called"),
        (3, "3 commands run · 7 tools called"),
        # Results with no call in front of them - the first poll of a run that
        # was already going when the page opened.
        (4, "4 lines of tool output"),
        # Errors are counted by the head painter's own span, not the label -
        # the label only keeps them out of the tool-output arithmetic.
        (5, "3 lines of tool output"),
        (6, ""),
    ],
)
def test_the_label_says_what_happened(drawn, index, expected):
    assert drawn["labels"][index] == expected


def test_the_folded_transcript_can_be_read_back_out_whole(drawn):
    """How the toggle redraws without re-fetching: the hidden lines come back
    and the headings, which are not in the log, do not."""
    assert drawn["foldedTextBack"] == "hello\n> Bash(x)\n< ok (1 line)\nbye\n"


def test_folding_is_on_by_default():
    """"Have this option on by default where these are compressed down." A
    browser that refuses localStorage entirely gets the same default rather
    than an unfolded console."""
    src = APP_JS.read_text(encoding="utf-8")
    fn = src.split("function consoleFolded()")[1].split("\n}")[0]

    assert 'getItem(CONSOLE_FOLD_KEY) !== "0"' in fn
    assert "return true;" in fn


def test_the_toggle_is_on_the_consoles_own_top_line():
    """Wes named the line: "on the top line where it says 'last run
    transcript'". Both pages that draw a transcript carry it, because it is the
    same transcript in the same reader."""
    for name in ("project.html", "run.html"):
        html = (ROOT / "app" / "templates" / name).read_text(encoding="utf-8")
        head = html.split('class="console-head"')[1].split("</div>")[0]
        assert 'id="console-fold-toggle"' in head, name
        # Hidden until app.js unhides it: the folding happens in the browser, so
        # with scripting off this would be a button that does nothing.
        assert "hidden" in head, name


def test_a_fold_left_with_nothing_in_it_is_removed(drawn):
    """The case scenario 10 cannot reach, and the delete-the-fix sweep of
    2026-08-01 proved it: there, the redraw put another tool line straight back
    into the same group, so leaving the empty wrapper in the DOM produced an
    identical result and deleting the removal broke no test.

    Here the completed line is prose, so the fold it opened has to go with it
    rather than sit there heading nothing."""
    mid = drawn["foldEmptiedByProse"]["mid"]
    after = drawn["foldEmptiedByProse"]["after"]

    assert [r["kind"] for r in mid] == ["say", "fold"]
    # And the count is the count: one line is "1 line", not "2 lines". `lines`
    # is the total, and it used to double as the bucket for anything that was
    # not a tool call, so a result incremented it twice.
    assert mid[1]["text"] == "1 line of tool output"
    assert [r["kind"] for r in after] == ["say", "say"]


def test_reading_back_a_thought_keeps_its_marker(drawn):
    """A thinking line is DRAWN without its "~ " (one marker per source line
    would draw a column of tildes down the page), so the drawn text is not the
    log's text. Read back off the drawing - which is what the show/hide toggle
    does - a paragraph of reasoning would come home as prose and be redrawn as
    something the agent said out loud."""
    assert drawn["thinkingTextBack"] == "~ let me check the tests\n> Bash(x)\nhello\n"


# --------------------------------------------------------------------------
# Inline markdown in the agent's prose
#
# Wes, 2026-08-04: "These are some lines from the agent console that just
# render in plain text and not in the sort of markdown that is intended" -
# pasting a summary whose "**bold**" lead and backticked file names were
# showing as their raw source. Only the agent's own words are rendered;
# machinery and errors are quoted verbatim, and block markdown (lists, fences)
# stays as source because the console draws line by line.
# --------------------------------------------------------------------------

def test_bold_and_code_render_in_prose(drawn):
    lead = drawn["markdown"][0]
    assert lead["kind"] == "cl cl-say"
    assert [k.get("cls") for k in lead["kids"]] == ["cl-md-b", None, "cl-md-c", None]
    assert lead["kids"][0]["t"] == "My own 540→550 change was wrong."
    assert lead["kids"][2]["t"] == "terminal-theme.css"
    # The drawn text is the text without the markers - nothing lost, nothing added.
    assert lead["text"] == "My own 540→550 change was wrong. terminal-theme.css is reusable."


def test_a_heading_line_loses_its_hashes_and_gains_their_weight(drawn):
    head = drawn["markdown"][1]
    assert head["kind"] == "cl cl-say cl-md-h"
    assert head["text"] == "The fix"


def test_bold_inside_a_numbered_item_and_an_escaped_bullet(drawn):
    numbered = drawn["markdown"][2]
    assert numbered["kids"][1] == {"cls": "cl-md-b", "t": "Two gaps", "kids": [{"t": "Two gaps"}]}
    bullet = drawn["markdown"][3]
    # runlog.escape_prose's leading space survives: the bullet stays a bullet.
    assert bullet["kids"][0]["t"] == " * a bullet with "
    assert bullet["kids"][1]["cls"] == "cl-md-b"


def test_spaced_asterisks_are_not_bold(drawn):
    """"a ** b ** c" is arithmetic or emphasis nobody wrote - CommonMark's own
    rule is that ** followed by whitespace opens nothing."""
    assert drawn["markdown"][4]["kids"] == [{"t": "a ** b that is not bold ** c"}]


def test_code_nests_inside_bold_one_level(drawn):
    bold = drawn["markdown"][5]["kids"][0]
    assert bold["cls"] == "cl-md-b"
    assert bold["kids"][0] == {"cls": "cl-md-c", "t": "style.css:199", "kids": []}


def test_machinery_is_never_markdown(drawn):
    """A tool line quoting "**x**" is quoting it. Rendering the machinery would
    redraw the agent's actual command as something it never typed."""
    tool = drawn["markdown"][6]
    assert tool["kind"] == "cl cl-tool"
    assert tool["kids"] == []
    assert tool["text"] == "> Bash(echo **not markdown**)"


def test_the_markdown_styles_exist():
    assert _declarations(".cl-md-h")["font-weight"] == "700"
    assert _declarations(".cl-md-b")["font-weight"] == "700"
    code = _declarations(".cl-md-c")
    assert "background" in code and "color" in code


def test_the_fold_head_hover_is_subtle():
    """Wes, 2026-08-04: "Hovering over the '+ 2 commands run' sections makes
    the whole section turn blue where you can't read the text under it anymore."

    The cause is the generic `button:hover` slab (`background: var(--ansi-blue)`
    at specificity 0,1,1): the fold head is a full-width button, and its own
    hover rule only overrode `color`. The fix overrides every property the
    generic rule sets, so this pins all of them.
    """
    hover = _declarations(".cl-fold-head:hover")
    assert "background" in hover and "var(--ansi-blue)" not in hover["background"]
    assert hover["box-shadow"] == "none"
    assert "var(--ansi-blue)" not in hover.get("color", "")

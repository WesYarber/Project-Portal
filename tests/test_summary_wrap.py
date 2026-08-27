"""The "Since you last looked" bullets at phone width.

Wes, 2026-08-07, about one of Karli's website projects: "the green 'Since you
last checked in' type bullet points at the top... spill off the right side of
the page pretty bad on mobile."

The cause is a flex floor, not a long sentence. `.work-summary-text` is
`flex: 1 1 auto` inside `.work-summary-list > li`, and a flex item's default
`min-width: auto` will not shrink below its **min-content** width. An agent
summary routinely names a URL - the two on her projects were a 40-character
admin address on the home server and a 50-character OAuth callback on her own
domain - and a URL is one unbreakable token, so min-content ran to 436px and
503px against a 390px screen. Nothing scrolled: `<body>` carries `overflow-x: hidden`, so the browser
simply sliced each line off at the screen edge.

Two declarations fix it, and neither alone is enough:

  * `min-width: 0` lets the flex item shrink at all;
  * `overflow-wrap: anywhere` - not `break-word`, which does NOT move
    min-content - lets the token itself break once it has.

A third rule earns its place on the phone: the "3 hours ago" link on the right
of the row is `white-space: nowrap`, so once the bullets could shrink they
shrank, down to a 255px column of a 325px card with a URL breaking three times
inside it. Below 560px the timestamp takes its own line, exactly as `.todo-meta`
does, and the bullets get the card's full width.

Measured in a real 390x844 chromium (scripts/summary_wrap_shot.py and
scripts/overflow_sweep.py in the project workspace), which is what these
assertions stand in for: with the two declarations undone the bullet's right
edge is 436px on a 390px screen; with them it is 330px, inside the card's 345px
border. The sweep then walked eight real pages and found nothing else past the
screen edge - after first proving it could still see this bug.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    """The declarations of the first top-level `selector { ... }` rule."""
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert m, f"no rule for {selector}"
    return m.group(1)


def _phone_block(css: str, marker: str) -> str:
    """The body of the `@media (max-width: 560px)` block holding `marker`.

    The same walk `tests/test_todo_mobile.py` does, and for the same reason:
    there is more than one phone block in this sheet, so a test must say which
    one it means.
    """
    at = 0
    while True:
        start = css.index("@media (max-width: 560px)", at)
        depth, i = 0, css.index("{", start)
        begin = i
        while True:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if marker in css[begin : i + 1]:
            return css[begin : i + 1]
        at = i + 1


def test_the_bullets_can_shrink_below_their_longest_token():
    body = _rule(_css(), ".work-summary-text")
    assert re.search(r"min-width:\s*0\b", body), body


def test_the_bullets_break_a_url_rather_than_leave_the_card():
    body = _rule(_css(), ".work-summary-text")
    # `anywhere`, specifically. `break-word` wraps the visible line but leaves
    # min-content at the whole token, so the flex item would still be 436px
    # wide and the fix above would buy nothing.
    assert re.search(r"overflow-wrap:\s*anywhere", body), body
    assert "break-word" not in body


def test_the_timestamp_takes_its_own_line_on_a_phone():
    block = _phone_block(_css(), ".work-summary-list")
    row = _rule(block, ".work-summary-list > li")
    assert re.search(r"flex-wrap:\s*wrap", row), row
    link = _rule(block, ".work-summary-list > li > a")
    # 100% is the whole move: a flex item this wide cannot share a line.
    assert re.search(r"flex:\s*1\s+0\s+100%", link), link
    assert re.search(r"text-align:\s*right", link), link


def test_the_stacking_does_not_leak_to_the_desktop():
    """Every rule that moves the timestamp must live inside the phone block.

    The desktop row is one line - bullets left, timestamp right - and stayed
    that way in the 1400px capture. A `flex-wrap` that escaped the media query
    would break that silently, since the bullets only wrap when a long token
    forces them to.
    """
    css = _css()
    # Comments name these selectors while explaining the change; strip them
    # first so the prose cannot fail the test.
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    phone = _phone_block(stripped, ".work-summary-list")
    outside = stripped.replace(phone, "")
    assert ".work-summary-list > li > a" not in outside
    assert not re.search(r"\.work-summary-list\s*>\s*li\s*\{[^}]*flex-wrap", outside)

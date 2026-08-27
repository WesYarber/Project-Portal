"""The todo row on a phone: full-width text, tag chips underneath.

From Wes's 2026-07-26 note, with a screenshot attached: "The todo list view on
mobile is all messed up with the tags... I think a good solution would be to
allow the todo item to take up the full width of that section and then add the
tags underneath each line item for mobile".

His screenshot showed a tagged todo rendered as a **three-character-wide column**
of text ("Onc / e / the / Str / ipe...") with the chips stacked beside it.

The cause is worth writing down, because it is not the obvious one and the
obvious fix does not work. `.todo-item` was already `flex-wrap: wrap` with a
comment saying chips "drop under the text on a phone" - and they never did.
`.todo-text` is `flex: 1` (so `flex-basis: 0`) with `overflow-wrap: anywhere`,
and `anywhere` reduces a flex item's **min-content width to a single
character**. A flex line only wraps when its items cannot fit at min-content, so
the text and the chips could always "fit" together and `flex-wrap` had nothing
to do. Flex then resolved the shortfall the only other way it can: by crushing
the flexible item down to its one-character floor.

So the fix cannot be a width nudge - it has to stop the two sharing a line at
all. Everything that is not the text is grouped into one `.todo-meta` flex item,
and on a phone that group gets `flex-basis: 100%`, which cannot share a line
with anything.

Verified by measurement, not by reading the CSS: rendered in a real 390px
viewport, every row reports the chips starting below the text's bottom edge,
the text 236px wide (it was ~24px), the first chip's left edge at exactly the
text's left edge, and no document overflow. On desktop every row reports the
chips still beside the text. See the journal entry for the shot.
"""
from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from app import config, db

STATIC = config.BASE_DIR / "app" / "static"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    # No context manager: the lifespan hook would start the worker.
    return TestClient(main.app)


@pytest.fixture
def project():
    p = db.create_project("Clicks", stage="active", build_approved=True, slug="clicks")
    db.add_todo(
        p["id"],
        "Once the Stripe keys exist: wire them into the compose service and "
        "verify a real team purchase end to end",
        owner="agent",
        tags=["blocked", "billing"],
    )
    db.add_todo(p["id"], "An untagged one", owner="agent")
    return p


def _row_html(html: str) -> str:
    """The first .todo-item element's markup."""
    # `[^>]*` after the class: the row also carries an id since 2026-08-04, so
    # the class is no longer the last attribute on the tag.
    m = re.search(r'<li class="todo-item[^"]*"[^>]*>(.*?)</li>', html, re.S)
    assert m, "no todo row rendered"
    return m.group(1)


# --- the markup hook -------------------------------------------------------


def test_only_the_blocked_chip_renders_and_it_is_grouped(client, project):
    """Wes, 2026-08-04: "fold the tags into this menu, aside from the 'blocked'
    tag. Too many tags can be present and restrict the space available for the
    todo item itself." So the row wears one chip at most, and it still sits in
    the .todo-meta group `flex-basis: 100%` can hand its own line on a phone."""
    row = _row_html(client.get("/project/clicks").text)
    meta = re.search(r'<span class="todo-meta">(.*?)</span>\s*$', row, re.S)
    assert meta, "the .todo-meta group is not closed at the end of the row"
    inner = meta.group(1)
    assert ">blocked<" in inner, "the blocked chip must be inside the group"
    assert "billing" not in inner, "every other tag belongs in the menu, not on the row"
    # The old inline controls are gone - they live in the right-click menu now.
    for cls in ("tag-add-btn", "todo-del", "todo-who", "tag-x"):
        assert cls not in row, f"{cls} should have moved into the menu"


def test_the_text_stays_outside_the_group(client, project):
    """`.todo-item.done .todo-text` strikes the text through. The chips must
    remain siblings of it, not descendants, or a completed item would strike its
    own tags as well."""
    row = _row_html(client.get("/project/clicks").text)
    text_at = row.index('class="todo-text"')
    meta_at = row.index('class="todo-meta"')
    assert text_at < meta_at, "the text must precede the group, not sit inside it"
    span = re.search(r'<span class="todo-text">(.*?)</span>', row, re.S)
    assert span and "todo-tag" not in span.group(1)


def test_an_unblocked_row_carries_no_group_at_all(client, project):
    """The group used to hold controls every row needed. Now it exists only for
    the blocked chip, so a row without that tag renders nothing after its text -
    an empty span would just be markup for the flex layout to trip on."""
    html = client.get("/project/clicks").text
    rows = re.findall(r'<li class="todo-item[^"]*"[^>]*>(.*?)</li>', html, re.S)
    assert len(rows) >= 2
    blocked = [r for r in rows if "tag-blocked" in r]
    plain = [r for r in rows if "tag-blocked" not in r]
    assert blocked and plain
    for row in plain:
        assert 'class="todo-meta"' not in row


# --- the CSS contract ------------------------------------------------------
#
# Rule-level rather than DOM-level on purpose: the test shims apply no CSS, so a
# DOM assertion cannot see a layout bug at all. These pin the two declarations a
# future edit could quietly drop, both of which were measured in a browser.


def _decls(body: str) -> str:
    """A rule body with its comments stripped.

    Needed because the comment explaining *why* `.todo-tag` carries no
    `margin-left` contains the words "margin-left", and a naive substring check
    fails on the very note that documents the fix.
    """
    return re.sub(r"/\*.*?\*/", "", body, flags=re.S)


def _phone_block(css: str, marker: str = ".todo-meta") -> str:
    """The body of the `@media (max-width: 560px)` block holding `marker`.

    The sheet has more than one phone block since the activity tables learned to
    stack (2026-08-06), and this used to take the first one - which silently
    became the wrong one the moment another was added above it.
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
        at = i


def test_the_phone_rule_gives_the_group_its_own_line():
    block = _phone_block((STATIC / "style.css").read_text(encoding="utf-8"))
    meta = re.search(r"\.todo-meta\s*\{(.*?)\}", block, re.S)
    assert meta, "no .todo-meta rule inside the phone media query"
    body = meta.group(1)
    # 100% is the whole fix: a flex item this wide cannot share a line.
    assert re.search(r"flex-basis:\s*100%", body), body
    # Indented to line up under the text rather than under the checkbox.
    assert re.search(r"padding-left:\s*1\.375rem", body), body


def test_the_chip_carries_no_margin_that_would_break_the_indent():
    """`.todo-tag` used to carry `margin-left: 0.3rem`. Inside the indented
    group that margin applies too, so the first chip hung 5px past the text's
    left edge - measured at 93px against the text's 88px. `.todo-meta`'s `gap`
    does the spacing now."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    rule = re.search(r"\n\.todo-tag\s*\{(.*?)\}", css, re.S)
    assert rule, "no .todo-tag rule"
    assert "margin-left" not in _decls(rule.group(1)), rule.group(1)
    meta = re.search(r"\n\.todo-meta\s*\{(.*?)\}", css, re.S)
    assert meta and "gap:" in _decls(meta.group(1))


def test_the_group_cannot_set_a_floor_wider_than_a_phone():
    """A flex item's default `min-width: auto` here would be the whole unwrapped
    chip run - which is how a layout ends up wider than the viewport, and on a
    phone that does not present as overflow, it just looks zoomed out."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    meta = re.search(r"\n\.todo-meta\s*\{(.*?)\}", css, re.S)
    assert re.search(r"min-width:\s*0", meta.group(1)), meta.group(1)


def _meta_group(row_inner: str) -> str:
    """The contents of `.todo-meta`, found by counting `<span>` depth.

    A regex cannot do this once the group has nested spans in it, and getting
    that wrong is silent: a non-greedy `(.*?)</span>\\s*$` stops at the first
    close and a greedy one runs to the LAST `</span>` in the row - which, if
    something has escaped the group and sits after it, is that escapee's own
    close tag, so the test reports the content as inside the group when it is
    not. This function was written after that exact false pass.
    """
    start = row_inner.index('<span class="todo-meta">')
    depth = 0
    for m in re.finditer(r"<span\b[^>]*>|</span>", row_inner[start:]):
        depth += 1 if m.group(0).startswith("<span") else -1
        if depth == 0:
            end = start + m.start()
            assert not row_inner[start + m.end():].strip(), (
                "the .todo-meta group must be the last thing in the row"
            )
            return row_inner[start:end]
    raise AssertionError("the .todo-meta group is never closed")


def test_a_long_press_does_not_fight_text_selection(client, project):
    """On a phone the row's menu opens by long press, and iOS answers a long
    press with text selection and the copy callout unless the element opts
    out. Coarse pointers only: desktop copy-by-drag still works."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    # Every coarse-pointer block, not the first one: touch is a fact about the
    # device rather than about one feature, so the rules keyed on it live
    # beside the things they change (the 16px form controls that stop iOS
    # zooming on focus are in another such block, up in the forms section).
    blocks = re.findall(r"@media \(pointer: coarse\)\s*\{(.*?)\n\}", css, re.S)
    assert blocks, "no coarse-pointer block in style.css"
    body = "\n".join(blocks)
    assert ".todo-item" in body
    assert "user-select: none" in body
    assert "-webkit-touch-callout: none" in body

"""Bounds the two blocks of a run's prompt that grow without anybody deciding to.

Everything else in a prompt is bounded by something real - a project has one
description, a todo list is as long as the work is. Two blocks are not:

* **learnings.md** grows by one line every time an agent learns something, and
  is byte-identical in every project's prompt.
* **the journal tail** is capped at 20 entries but each entry is an agent's
  progress report, written in as much markdown as it felt like.

Measured on 2026-07-28 across the seven active projects: the average build
prompt was 105 KB, of which learnings was 36 KB and the journal 28 KB. A day
earlier the same measurement said 85 KB. Neither block had a ceiling, so the
number only ever went one way.

Both budgets are byte budgets rather than counts, because a count is a proxy
for size that any one verbose entry breaks.

WHY NOT REORDER INSTEAD? Because it does nothing, and this was measured rather
than assumed. The prompt is one string on stdin, which the CLI sends as one
content block with its cache breakpoint at the end, and Anthropic's prompt
cache matches at breakpoints - there is no partial-prefix match inside a block.
Two runs sharing a 22.7k-token *prefix* got zero cache reads from each other;
a byte-identical prompt got a full hit. See `test_promptbudget.py` for the
recorded numbers. So the only lever on prompt cost is the prompt's size.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

# A markdown `## ` heading. The learnings file is authored in sections and the
# order of them is the author's own priority order - see `learnings_for_prompt`.
_HEADING = re.compile(r"^##\s+\S")

# One learning is one `- ` bullet, possibly with indented continuation lines.
_BULLET = re.compile(r"^-\s+\S")


@dataclass(frozen=True)
class Section:
    heading: str
    entries: list[str]

    @property
    def text(self) -> str:
        return render(self.heading, self.entries)

    def size(self) -> int:
        return len(self.text) + 1


def render(heading: str, entries: list[str]) -> str:
    if not entries:
        return heading
    return heading + "\n\n" + "\n".join(entries)


def split_sections(text: str) -> tuple[str, list[Section]]:
    """Split a markdown memory file into its preamble and its `## ` sections.

    An entry is a `- ` bullet together with any continuation lines under it, so
    that a budget can drop whole learnings and never half of one. A half
    sentence about a platform hazard is worse than no sentence: it reads as
    complete and it is wrong.
    """
    preamble: list[str] = []
    sections: list[Section] = []
    entries: list[str] = []
    heading: Optional[str] = None

    def flush() -> None:
        if heading is not None:
            # The blank line under a heading, and the one before the next one,
            # are layout rather than content. Counted as entries they inflate
            # every "N older entries were trimmed" number by one per section
            # and let a budget be spent on nothing.
            kept = [e.rstrip() for e in entries if e.strip()]
            sections.append(Section(heading, kept))

    for line in text.splitlines():
        if _HEADING.match(line):
            flush()
            heading, entries = line, []
            continue
        if heading is None:
            preamble.append(line)
            continue
        if _BULLET.match(line) or not entries:
            entries.append(line)
        else:
            # A continuation of the entry above it, so it rides along with it.
            entries[-1] = entries[-1] + "\n" + line
    flush()
    return "\n".join(preamble).strip(), sections


def learnings_for_prompt(text: str, budget: int, full_path: str = "") -> str:
    """The part of learnings.md that goes in a prompt, under a byte budget.

    This replaces `"\\n".join(lines[-100:])`, which was not a budget at all but
    a line count - and, measured on 2026-07-28 against the live file, one that
    landed 98 lines inside the last of seven sections. Every prompt the portal
    had ever built was therefore missing *all seven headings*, the preamble
    stating what earns a place in the file, and with them the sections on who
    the owner is, how they want agents to work, and what their machines can and
    cannot do (no compiler, no pip, no node on the server) - while keeping
    36 KB of one-off domain trivia. The high-signal half of the file was the
    half being dropped.

    So the fill order here is the file's own order, which is the order its
    author chose: general and durable first, domain notes last. Whole sections
    are taken while they fit; the section that overflows keeps its NEWEST
    entries, because those are the ones about work that is still going on; and
    anything past that is listed by heading and count so an agent can see that
    it exists and go and read it.
    """
    preamble, sections = split_sections(text)
    if not sections:
        # No headings at all - a fresh install, or a file an agent rewrote flat.
        # Fall back to the newest whole entries that fit rather than to nothing.
        # The sentinel heading has to be a real one: `_HEADING` requires a
        # non-space after the hashes, so "## " on its own matches nothing and
        # this fell through to an empty string.
        _, flat = split_sections("## _\n" + text)
        kept, dropped = _fit_entries(flat[0].entries if flat else [], budget)
        return _with_pointer("\n".join(kept), dropped, 0, full_path, len(text))

    out: list[str] = []
    used = 0
    if preamble:
        out.append(preamble)
        used += len(preamble) + 1

    dropped_entries = 0
    listed: list[str] = []
    overflowed = False
    for sec in sections:
        if not overflowed and used + sec.size() <= budget:
            out.append(sec.text)
            used += sec.size()
            continue
        if not overflowed:
            # The one section that straddles the budget: keep its newest
            # entries, whole, and say how many were left behind.
            room = budget - used - len(sec.heading) - 2
            kept, dropped = _fit_entries(sec.entries, room)
            if kept:
                out.append(render(sec.heading, kept))
                used += len(render(sec.heading, kept)) + 1
                dropped_entries += dropped
                overflowed = True
                continue
            # Not even one entry fits, so this section is listed like the rest.
        overflowed = True
        dropped_entries += len(sec.entries)
        listed.append(f"{sec.heading.lstrip('# ').strip()} ({len(sec.entries)})")

    return _with_pointer("\n\n".join(out), dropped_entries, len(listed), full_path,
                         len(text), listed)


def _fit_entries(entries: list[str], room: int) -> tuple[list[str], int]:
    """The newest whole entries that fit in `room` bytes, oldest dropped first."""
    kept: list[str] = []
    used = 0
    for entry in reversed(entries):
        cost = len(entry) + 1
        if used + cost > room:
            break
        kept.append(entry)
        used += cost
    kept.reverse()
    return kept, len(entries) - len(kept)


def _with_pointer(body: str, dropped: int, _n_listed: int, full_path: str,
                  total: int, listed: Optional[list[str]] = None) -> str:
    """Say what was left out and where to find it.

    Silence here would be the worst outcome of a budget: an agent reading a
    trimmed file has no way to tell it is trimmed, so it concludes the hazard
    it is about to rediscover was never known. The pointer also carries an
    explicit read license, because the agent contract tells it to stay inside
    its workspace and this file is not in one.
    """
    if not dropped and not listed:
        return body
    bits = [body, ""]
    if listed:
        bits.append("Also in the file, not shown here: " + "; ".join(listed) + ".")
    if full_path:
        bits.append(
            f"({dropped} older entries are trimmed to keep this prompt bounded - the "
            f"whole file is {total // 1024} KB at `{full_path}`. You may READ it, and "
            "only read it, if you hit a strange platform behavior and want to check "
            "whether it is already known.)"
        )
    else:
        bits.append(f"({dropped} older entries trimmed to keep this prompt bounded.)")
    return "\n".join(bits)


# --- The journal tail --------------------------------------------------------

@dataclass(frozen=True)
class JournalEntry:
    prefix: str   # "- [ts] author/kind: "
    body: str


def _title_of(body: str) -> str:
    for line in body.splitlines():
        if line.strip():
            return line.strip()
    return ""


def digest(body: str, cap: int = 700) -> str:
    """A journal entry's heading plus its first paragraph.

    The agent contract asks for the first paragraph of a progress entry to be a
    self-contained summary of what was done and what state things were left in,
    which is what makes this a designed digest rather than an amputation.
    """
    lines = body.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return ""
    title = lines[i].strip()
    i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    para: list[str] = []
    while i < len(lines) and lines[i].strip():
        para.append(lines[i].strip())
        i += 1
    text = title + ("\n" + " ".join(para) if para else "")
    if len(text) > cap:
        text = text[:cap].rstrip() + " ..."
    return text


def journal_for_prompt(entries: Iterable[JournalEntry], budget: int) -> str:
    """The journal tail under a byte budget, degrading depth before breadth.

    Three levels, and the order they are given up in is the point:

    1. every entry keeps its heading, always - an agent that cannot see that a
       run happened on the 25th will happily redo it;
    2. older entries fall back to heading + first paragraph;
    3. the newest entries are restored to their full text with whatever budget
       is left, newest first.

    The newest entry is exempt and is always whole. It is the handover from the
    run immediately before this one, and trimming it to save bytes would be
    saving them in precisely the wrong place.
    """
    rows = list(entries)
    if not rows:
        return "(no journal entries yet)"

    titles = [f"{e.prefix}{_title_of(e.body)}" for e in rows]
    digests = [f"{e.prefix}{digest(e.body)}" for e in rows]
    fulls = [f"{e.prefix}{e.body}" for e in rows]

    # Level 1, and the newest entry whole on top of it.
    chosen = list(titles)
    chosen[-1] = fulls[-1]
    trimmed = [i for i in range(len(rows) - 1)]

    def size(sel: list[str]) -> int:
        return sum(len(s) + 1 for s in sel)

    # Level 2: upgrade to digests from the newest backward while they fit.
    for i in range(len(rows) - 2, -1, -1):
        grown = size(chosen) - len(chosen[i]) + len(digests[i])
        if grown > budget:
            break
        chosen[i] = digests[i]

    # Level 3: full text, newest backward, for whatever budget survives.
    for i in range(len(rows) - 2, -1, -1):
        grown = size(chosen) - len(chosen[i]) + len(fulls[i])
        if grown > budget:
            break
        chosen[i] = fulls[i]
        trimmed.remove(i)

    text = "\n".join(chosen)
    if trimmed:
        text += (
            f"\n\n({len(trimmed)} older entries above are shortened to their heading "
            "and opening paragraph to keep this prompt bounded. Every entry is listed, "
            "so nothing is hidden - the full text of any of them is on the project "
            "page.)"
        )
    return text

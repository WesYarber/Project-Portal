"""Bounds the blocks of a run's prompt that grow without anybody deciding to.

Everything else in a prompt is bounded by something real - a project has one
description, a project section is as long as its description. These are not:

* **learnings.md** grows by one line every time an agent learns something, and
  is byte-identical in every project's prompt.
* **the journal tail** is capped at 20 entries but each entry is an agent's
  progress report, written in as much markdown as it felt like.
* **the completed half of the todo list** was capped at 15 items per half, and
  a portal todo runs to 500 characters, so the cap admitted 15 KB.
* **the answered-questions log** had no bound of any kind. Every question Wes
  has ever answered on a project is in every prompt that project will ever
  build, forever.

Measured on 2026-07-28 across the seven active projects: the average build
prompt was 105 KB, of which learnings was 36 KB and the journal 28 KB. A day
earlier the same measurement said 85 KB. Neither block had a ceiling, so the
number only ever went one way.

Measured again on 2026-07-29, after those two budgets shipped, across all
twenty active projects (average prompt 73 KB): learnings held at 16.2 KB and
the journal at 15.1 KB, so the budgets were doing their job - and the two
blocks nobody had bounded had moved to the top of the list on the projects with
the most history. Project Portal's own prompt was 102 KB, of which **20 KB was
the todo list** (96 completed items) and **11.8 KB the answered questions** (25
of them, ten being the same question about spending down a Claude window,
answered "yes" ten times).

Every budget here is a byte budget rather than a count, because a count is a
proxy for size that any one verbose entry breaks - which is exactly how a
15-item cap on the completed todo tail came to admit 15 KB.

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
from typing import Any, Iterable, Optional

from app import qdedupe

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


@dataclass(frozen=True)
class Plan:
    """Which parts of learnings.md a given byte budget admits.

    Computed once and read twice: `learnings_for_prompt` renders it into the
    prompt, and `reach` turns it into the numbers the compaction job and
    /memory need. Two walks over the same fill rule would eventually disagree,
    and the disagreement would be invisible - a compaction agent told the file
    fits while the prompt builder quietly drops a third of it.
    """
    preamble: str
    whole: tuple[Section, ...]                     # carried entire
    partial: Optional[Section]                     # the one that straddles the budget
    kept: tuple[str, ...]                          # of `partial`, the entries that fit
    dropped: int                                   # of `partial`, the entries that did not
    listed: tuple[Section, ...]                    # left out whole, named only
    flat: bool                                     # a file with no `## ` headings at all


def _plan(text: str, budget: int) -> Plan:
    """The fill decision, in one place.

    Fill order is the file's own order, which is the order its author chose:
    general and durable first, domain notes last. Whole sections are taken
    while they fit; the section that overflows keeps its NEWEST entries,
    because those are the ones about work that is still going on; and anything
    past that is named but left out.
    """
    preamble, sections = split_sections(text)
    if not sections:
        # No headings at all - a fresh install, or a file an agent rewrote flat.
        # Fall back to the newest whole entries that fit rather than to nothing.
        # The sentinel heading has to be a real one: `_HEADING` requires a
        # non-space after the hashes, so "## " on its own matches nothing and
        # this fell through to an empty string.
        _, flat = split_sections("## _\n" + text)
        entries = flat[0].entries if flat else []
        kept, dropped = _fit_entries(entries, budget)
        return Plan("", (), Section("", entries), tuple(kept), dropped, (), True)

    used = len(preamble) + 1 if preamble else 0
    whole: list[Section] = []
    listed: list[Section] = []
    partial: Optional[Section] = None
    kept: list[str] = []
    dropped = 0
    overflowed = False
    for sec in sections:
        if not overflowed and used + sec.size() <= budget:
            whole.append(sec)
            used += sec.size()
            continue
        if not overflowed:
            # The one section that straddles the budget.
            room = budget - used - len(sec.heading) - 2
            fits, left = _fit_entries(sec.entries, room)
            if fits:
                partial, kept, dropped = sec, fits, left
                used += len(render(sec.heading, fits)) + 1
                overflowed = True
                continue
            # Not even one entry fits, so this section is listed like the rest.
        overflowed = True
        listed.append(sec)

    return Plan(preamble, tuple(whole), partial, tuple(kept), dropped, tuple(listed), False)


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
    """
    plan = _plan(text, budget)
    if plan.flat:
        return _with_pointer("\n".join(plan.kept), plan.dropped, 0, full_path, len(text))

    out: list[str] = []
    if plan.preamble:
        out.append(plan.preamble)
    out.extend(sec.text for sec in plan.whole)
    if plan.partial is not None:
        out.append(render(plan.partial.heading, list(plan.kept)))
    listed = [f"{_bare(sec.heading)} ({len(sec.entries)})" for sec in plan.listed]
    dropped_entries = plan.dropped + sum(len(sec.entries) for sec in plan.listed)

    return _with_pointer("\n\n".join(out), dropped_entries, len(listed), full_path,
                         len(text), listed)


def _bare(heading: str) -> str:
    return heading.lstrip("# ").strip()


# --- What the file weighs, versus what a prompt actually carries --------------
#
# The two are not the same number, and until 2026-08-07 nothing in the portal
# knew that. The auto-compaction trigger counted LINES while the prompt spends
# BYTES, so on the live file - 189 lines against a 200-line cap - the trigger
# had been asleep since 2026-07-28 while the file grew to 58 KB, of which a
# prompt carried 16 KB. All 111 entries of its last section had never appeared
# in a single prompt. Agents were writing into a section no agent ever read.
#
# So the cap is a byte cap now (worker.learnings_cap_kb), and this is what both
# it and the compaction agent are told: not "how long is the file" but "how
# much of it is doing its job".


@dataclass(frozen=True)
class Unreachable:
    heading: str   # bare, no leading hashes
    entries: int   # how many of its entries a prompt never sees
    size: int      # how many bytes of it a prompt never sees


@dataclass(frozen=True)
class Reach:
    total: int                            # bytes of the whole file
    budget: int                           # bytes a prompt carries it in
    in_prompt: int                        # bytes of the file that reach a prompt
    entries_total: int
    entries_in_prompt: int
    unreachable: tuple[Unreachable, ...]  # in file order, worst-placed last

    @property
    def bytes_out(self) -> int:
        return max(0, self.total - self.in_prompt)

    @property
    def entries_out(self) -> int:
        return max(0, self.entries_total - self.entries_in_prompt)

    @property
    def fits(self) -> bool:
        return self.entries_out == 0


def reach(text: str, budget: int) -> Reach:
    """How much of learnings.md a prompt at `budget` bytes actually carries."""
    plan = _plan(text, budget)
    sections = ([plan.partial] if plan.partial is not None else []) + list(plan.listed)
    if plan.flat:
        entries_total = len(plan.partial.entries) if plan.partial else 0
        in_prompt = len("\n".join(plan.kept))
    else:
        entries_total = (
            sum(len(s.entries) for s in plan.whole)
            + (len(plan.partial.entries) if plan.partial else 0)
            + sum(len(s.entries) for s in plan.listed)
        )
        in_prompt = (len(plan.preamble) + 1 if plan.preamble else 0)
        in_prompt += sum(s.size() for s in plan.whole)
        if plan.partial is not None:
            in_prompt += len(render(plan.partial.heading, list(plan.kept))) + 1

    out: list[Unreachable] = []
    if plan.partial is not None and plan.dropped:
        lost = plan.partial.entries[: plan.dropped]
        out.append(Unreachable(_bare(plan.partial.heading), plan.dropped,
                               sum(len(e) + 1 for e in lost)))
    for sec in plan.listed:
        out.append(Unreachable(_bare(sec.heading), len(sec.entries), sec.size()))

    return Reach(
        total=len(text),
        budget=budget,
        in_prompt=min(in_prompt, len(text)),
        entries_total=entries_total,
        entries_in_prompt=entries_total - sum(u.entries for u in out),
        unreachable=tuple(out),
    )


# --- The owner profile ------------------------------------------------------

def profile_for_prompt(text: str, budget: int, full_path: str = "") -> str:
    """The owner profile under a byte ceiling, trimmed by WHOLE sections only.

    profile.md is the last unbounded block in a prompt, and by 2026-08-07 it
    was the largest: 26.7 KB, **31% of the average 85.9 KB build prompt**, in
    every run of all 24 active projects, byte-identical in each. It had grown
    from 16.6 KB in ten days, because the daily reflect rewrites it every day
    and nothing ever told the reflect a size.

    So the real fix is not here - it is at the write end, where the reflect is
    handed a cap and told to come back under it (see `agent_runner.build_prompt`
    and `worker.run_reflect`). This function is the backstop for when that
    fails, and it is deliberately blunter than `learnings_for_prompt`:

    * **whole sections or nothing.** learnings.md is a list of independent
      bullets, so keeping the newest half of a section loses nothing but old
      bullets. The profile is a coherent authored document - half of "How he
      wants things built" reads as the whole of it, and an agent would build to
      a standard it cannot see the rest of. Better to lose a named topic than
      to be quietly wrong about one.
    * **the file's own order is the priority order**, top down, and once one
      section overflows everything below it goes too even if it would have fit.
      A profile with a hole in the middle still reads as complete; a profile cut
      off at a stated point does not.
    * **every dropped section is named** and the path is given, because the
      failure this whole module exists to avoid is an agent concluding that
      something it cannot see was never written down.

    The `# ` title and the preamble under it always survive - they say what the
    document is and whose it is, which is what makes the pointer legible.
    """
    if len(text) <= budget:
        return text
    preamble, sections = split_sections(text)
    if not sections:
        return _profile_hard_cut(text, budget, full_path)

    out: list[str] = []
    used = 0
    if preamble:
        out.append(preamble)
        used += len(preamble) + 1

    dropped: list[str] = []
    for sec in sections:
        if not dropped and used + sec.size() <= budget:
            out.append(sec.text)
            used += sec.size()
            continue
        dropped.append(sec.heading.lstrip("# ").strip())

    if not dropped:  # pragma: no cover - len(text) > budget makes this unreachable
        return text
    return "\n\n".join(out) + "\n\n" + _profile_note(dropped, budget, full_path, len(text))


def _profile_note(dropped: list[str], budget: int, full_path: str, total: int) -> str:
    where = f" The whole file is at `{full_path}`." if full_path else ""
    return (
        f"(This profile is {total // 1024} KB, over the {budget // 1024} KB a prompt "
        f"carries it in, so {len(dropped)} section(s) below are left out WHOLE rather "
        f"than cut in half: {'; '.join(dropped)}.{where} You may READ it, and only read "
        "it, if one of those sections would change what you are about to do.)"
    )


def _profile_hard_cut(text: str, budget: int, full_path: str) -> str:
    """A profile with no `## ` headings at all, over budget.

    Nothing structural to cut on, so this cuts at the last blank line that fits
    and says plainly that it did. It is the backstop's backstop - a flattened
    file, or a reflect that ran away - and it should never fire on a profile
    anybody wrote.
    """
    head = text[:budget]
    at = head.rfind("\n\n")
    if at > budget // 2:
        head = head[:at]
    where = f" The whole file is at `{full_path}`." if full_path else ""
    return head.rstrip() + (
        f"\n\n(This profile has no `## ` sections to trim on and is {len(text) // 1024} KB, "
        f"over the {budget // 1024} KB a prompt carries it in, so it is CUT OFF here "
        f"mid-document.{where} You may READ it, and only read it, if you need the rest.)"
    )


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


def journal_for_prompt(entries: Iterable[JournalEntry], budget: int,
                       full_path: str = "") -> str:
    """The journal tail under a byte budget, degrading depth before breadth.

    Three levels, and the order they are given up in is the point:

    1. every entry keeps its heading, always - an agent that cannot see that a
       run happened on the 25th will happily redo it;
    2. older entries fall back to heading + first paragraph;
    3. the newest entries are restored to their full text with whatever budget
       is left, newest first.

    The newest entry is exempt and is always whole. It is the handover from the
    run immediately before this one, and trimming it to save bytes would be
    saving them in precisely the wrong place. That exemption is also what lets
    `journalfile` mirror the journal to disk without locking anything - see the
    ordering argument at the top of that module.

    `full_path` is where the untrimmed text can be read, if anywhere. Passing ""
    keeps the older wording, which points at the project page: a human can open
    that and an agent cannot, which is the gap the file closes.
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
        where = (f"read the full text of any of them in `{full_path}` in this "
                 "workspace, searching for the timestamp above" if full_path
                 else "the full text of any of them is on the project page")
        text += (
            f"\n\n({len(trimmed)} older entries above are shortened to their heading "
            "and opening paragraph to keep this prompt bounded. Every entry is listed, "
            f"so nothing is hidden - {where}.)"
        )
    return text


# --- The answered-questions log ----------------------------------------------

@dataclass(frozen=True)
class Answered:
    question: str
    answer: str
    who: str = ""        # "" on a one-person install, where naming is noise
    options: Any = None  # the one-tap menu, if the question carried one


def _render_qa(qa: Answered, repeats: int = 0) -> str:
    who = f" ({qa.who})" if qa.who else ""
    again = (
        f"\n  (asked {repeats + 1} times in near-identical wordings; "
        "the newest answer is the one above)"
    ) if repeats else ""
    return f"- Q: {qa.question}\n  A{who}: {qa.answer}{again}"


def collapse_repeats(pairs: list[Answered]) -> list[tuple[Answered, int]]:
    """Fold near-identical askings of one question into their newest answer.

    Oldest-first in, oldest-first out, each surviving question paired with how
    many earlier askings folded into it.

    This is a correctness fix before it is a size one. Ten copies of "shall I
    spend the window down?" in a prompt do not read as one decision recorded
    ten times; they read as a subject Wes cares about ten times as much as
    anything else on the page, which is the opposite of what he said about it:

        You asked me way too many times here. I just want to be asked once.

    The newest asking wins, and it wins whether or not the answers agree,
    because a later answer supersedes an earlier one - that is what answering
    again means. The count is still shown: an agent that can see a question was
    asked ten times learns something true about how it behaved, and hiding the
    repeats would be the portal quietly covering for itself.

    `qdedupe` does the matching, so the log collapses under exactly the rule
    that stops the questions being asked twice in the first place. Anything it
    would have refused as a duplicate at filing time is a duplicate here.
    """
    kept: list[Answered] = []
    repeats: list[int] = []
    for qa in reversed(pairs):  # newest first, so the newest is the survivor
        match = -1
        best = 0.0
        for i, other in enumerate(kept):
            score = qdedupe.similarity(
                qa.question, other.question, qa.options, other.options
            )
            if score >= qdedupe.THRESHOLD and score > best:
                match, best = i, score
        if match >= 0:
            repeats[match] += 1
            continue
        kept.append(qa)
        repeats.append(0)
    out = list(zip(kept, repeats))
    out.reverse()
    return out


def answered_for_prompt(pairs: Iterable[Answered], budget: int) -> str:
    """The `## Answered questions` block, oldest first, under a byte budget.

    Two passes, and the order matters: repeats collapse *before* the budget is
    spent, so a project that asked one question ten times does not lose nine
    real decisions paying for nine copies of one.

    What survives the budget is the NEWEST answers, which is the opposite of
    the reading order the block is rendered in. Both are deliberate: a decision
    Wes made this week is likelier to still bind than one from three weeks ago,
    and he reads oldest-to-newest, so the block is filled newest-first and then
    printed the way round he reads it.
    """
    rows = list(pairs)
    if not rows:
        return "(none)"
    collapsed = collapse_repeats(rows)
    folded = sum(n for _, n in collapsed)

    lines = [_render_qa(qa, n) for qa, n in collapsed]
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line) + 1
        if used + cost > budget and kept:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    dropped = len(lines) - len(kept)

    text = "\n".join(kept)
    notes = []
    if folded:
        notes.append(
            f"{folded} near-identical re-asking(s) of a question already listed "
            "were folded into it"
        )
    if dropped:
        notes.append(
            f"{dropped} older answered question(s) are left out to keep this "
            "prompt bounded - they are all on the project page"
        )
    if notes:
        text += "\n\n(" + "; ".join(notes) + ".)"
    return text

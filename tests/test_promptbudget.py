"""The two unbounded blocks of a run's prompt, and the budgets that bound them.

Background, because the numbers below are the reason this module exists at all.

Wes was asked on 2026-07-28 whether to (a) reorder the prompt so the memory
that is byte-identical on every project sat at the front where prompt caching
could reuse it, or (b) budget the journal by bytes. He answered "I'm not sure
what all of this means to be honest. I will trust you to make the right call on
it. Defer to fable as an advisor, please."

Option (a) turned out to be worthless, and that was measured rather than
argued. See `test_the_recorded_cache_experiment_says_reordering_is_worthless`.
"""
from __future__ import annotations

import pytest

from app import agent_runner, db, promptbudget as pb


FILE = """# Learnings about Wes

Durable facts only. A line earns its place.

## Wes himself

- He is on an iPhone.
- He is in Arkansas.

## How he wants agents to work

- Ship on main.

## Domain notes worth not rediscovering

- Oldest domain note, about a project long retired.
- Middle domain note.
- Newest domain note, about the thing being built right now.
"""


def _headings(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("## ")]


# --- The bug this replaced ---------------------------------------------------

def test_a_line_count_drops_every_heading_when_the_last_section_is_the_long_one():
    """The behavior that was live until 2026-07-28, pinned so it cannot return.

    `"\\n".join(lines[-100:])` is a line count, not a budget. Measured against
    the real file that day: 198 lines in 7 sections, the last of which held 124
    of them - so the 100-line tail landed 98 lines inside it and every prompt
    the portal had ever built contained ZERO headings, no preamble, and none of
    the six general sections. The high-signal half was the half being dropped.
    """
    tail = "\n".join(FILE.splitlines()[-3:])
    assert _headings(tail) == []
    assert "iPhone" not in tail and "Ship on main" not in tail

    kept = pb.learnings_for_prompt(FILE, 4096, "/m/learnings.md")
    assert len(_headings(kept)) == 3
    assert "iPhone" in kept and "Ship on main" in kept


def test_the_preamble_survives_because_it_is_the_standard_for_writing_one():
    kept = pb.learnings_for_prompt(FILE, 4096, "/m/learnings.md")
    assert "Durable facts only. A line earns its place." in kept


# --- The budget itself -------------------------------------------------------

def test_the_whole_file_is_kept_when_it_fits():
    kept = pb.learnings_for_prompt(FILE, 64 * 1024, "/m/learnings.md")
    assert "Oldest domain note" in kept
    # Nothing was trimmed, so there is nothing to apologize for.
    assert "trimmed" not in kept


def test_sections_fill_from_the_top_in_the_files_own_order():
    """The author's order is the priority order, so general beats domain."""
    budget = len("# Learnings about Wes\n\nDurable facts only. A line earns its place.") + 200
    kept = pb.learnings_for_prompt(FILE, budget, "/m/learnings.md")
    assert "iPhone" in kept
    assert "Newest domain note" not in kept


def test_the_section_that_overflows_keeps_its_newest_entries():
    """Newest, because those are about work that is still going on."""
    full = pb.learnings_for_prompt(FILE, 64 * 1024, "")
    oldest = "- Oldest domain note, about a project long retired."
    budget = len(full) - len(oldest) - 1
    kept = pb.learnings_for_prompt(FILE, budget, "/m/learnings.md")
    assert "Newest domain note" in kept
    assert "Middle domain note" in kept
    assert "Oldest domain note" not in kept


def test_an_entry_is_never_cut_in_half():
    """A half sentence about a platform hazard reads as complete and is wrong,
    which is worse than not having it at all."""
    whole = {ln.rstrip() for ln in FILE.splitlines() if ln.startswith("- ")}
    for budget in range(120, len(FILE) + 40, 7):
        kept = pb.learnings_for_prompt(FILE, budget, "/m/learnings.md")
        for line in kept.splitlines():
            if line.startswith("- "):
                # Membership in the set of real entries, NOT `line in FILE`:
                # a truncated entry is a substring of the file, so a substring
                # check passes on exactly the damage this test exists to catch.
                assert line in whole, f"{line!r} is not a whole entry from the file"


def test_a_trimmed_block_says_so_and_says_where_the_rest_is():
    """Silence is the worst failure of a budget: an agent reading a trimmed
    file cannot tell it is trimmed, so it concludes the hazard it is about to
    rediscover was never known."""
    kept = pb.learnings_for_prompt(FILE, 300, "/m/learnings.md")
    assert "trimmed" in kept
    assert "/m/learnings.md" in kept
    # And an explicit read license, because the agent contract otherwise tells
    # it to stay inside its workspace and this file is not in one.
    assert "READ" in kept


def test_sections_left_out_entirely_are_still_named():
    kept = pb.learnings_for_prompt(FILE, 200, "/m/learnings.md")
    assert "Domain notes worth not rediscovering" in kept


def test_a_flat_file_with_no_headings_still_gets_a_budget():
    flat = "\n".join(f"- learning number {i} " + "x" * 60 for i in range(50))
    kept = pb.learnings_for_prompt(flat, 1024, "/m/learnings.md")
    assert len(kept) < 2048
    assert "learning number 49" in kept
    assert "learning number 0 " not in kept


def test_a_blank_line_under_a_heading_is_layout_and_not_an_entry():
    """It is markdown spacing. Counted as an entry it inflates every "N older
    entries were trimmed" number by one per section, and it lets a budget be
    spent on a byte of nothing."""
    _, sections = pb.split_sections(FILE)
    for sec in sections:
        assert all(e.strip() for e in sec.entries)
    assert [len(s.entries) for s in sections] == [2, 1, 3]

    kept = pb.learnings_for_prompt(FILE, 200, "/m/learnings.md")
    assert "(3)" in kept, "the last section has three entries, not four"


def test_a_continuation_line_rides_along_with_its_bullet():
    text = "## S\n\n- first\n- second entry\n  continued here\n"
    _, sections = pb.split_sections(text)
    assert sections[0].entries[-1] == "- second entry\n  continued here"


# --- The journal -------------------------------------------------------------

def _entry(n: int, paras: int = 6) -> pb.JournalEntry:
    body = f"## Entry {n}\n\nOpening paragraph for {n}, the summary.\n"
    for p in range(paras):
        body += f"\nParagraph {p} of entry {n}. " + "detail " * 40 + "\n"
    return pb.JournalEntry(prefix=f"- [t{n}] agent/progress: ", body=body)


def test_every_entry_keeps_its_heading_however_tight_the_budget():
    """An agent that cannot see that a run happened will happily redo it."""
    entries = [_entry(i) for i in range(20)]
    out = pb.journal_for_prompt(entries, 1)
    for i in range(20):
        assert f"## Entry {i}" in out


def test_the_newest_entry_is_always_whole():
    """It is the handover from the run immediately before this one."""
    entries = [_entry(i) for i in range(20)]
    out = pb.journal_for_prompt(entries, 1)
    assert "Paragraph 5 of entry 19" in out
    assert "Paragraph 5 of entry 18" not in out


def test_older_entries_degrade_to_heading_plus_opening_paragraph():
    entries = [_entry(i) for i in range(20)]
    out = pb.journal_for_prompt(entries, 24 * 1024)
    assert "Opening paragraph for 0, the summary." in out
    assert "Paragraph 3 of entry 0" not in out


def test_depth_is_given_up_before_breadth_and_newest_keeps_its_depth():
    entries = [_entry(i) for i in range(20)]
    out = pb.journal_for_prompt(entries, 24 * 1024)
    # Whatever budget survives goes to the newest entries, in order.
    # The trailing period matters: without it "entry 1" matches "entry 10".
    deep = [i for i in range(20) if f"Paragraph 4 of entry {i}." in out]
    assert deep, "no entry kept its full text"
    assert deep == list(range(min(deep), 20)), "full text is not a newest-first run"


def test_the_budget_actually_binds():
    entries = [_entry(i, paras=40) for i in range(20)]
    out = pb.journal_for_prompt(entries, 24 * 1024)
    # The newest entry is the one stated exception, so the bound is the budget
    # plus that one entry, not the budget alone.
    assert len(out) < 24 * 1024 + len(entries[-1].body) + 500


def test_a_shortened_journal_says_it_is_shortened():
    entries = [_entry(i) for i in range(20)]
    out = pb.journal_for_prompt(entries, 8 * 1024)
    assert "shortened" in out


def test_an_empty_journal_reads_the_way_it_always_did():
    assert pb.journal_for_prompt([], 1024) == "(no journal entries yet)"


def test_a_short_journal_is_untouched():
    entries = [pb.JournalEntry(prefix="- [t] user/note: ", body="do the thing")]
    out = pb.journal_for_prompt(entries, 24 * 1024)
    assert out == "- [t] user/note: do the thing"


@pytest.mark.parametrize("body,want", [
    ("## Title\n\nFirst para.\n\nSecond para.", "## Title\nFirst para."),
    ("no heading at all", "no heading at all"),
    ("", ""),
])
def test_digest_is_the_heading_and_the_first_paragraph(body, want):
    assert pb.digest(body) == want


# --- Why the other half of the question was dropped --------------------------

def test_the_recorded_cache_experiment_says_reordering_is_worthless():
    """Wes was offered "reorder the prompt so the stable half is at the front,
    where prompt caching can reuse it". It does nothing, and this is the
    measurement that settled it rather than an argument.

    Run 2026-07-28 against the live Claude CLI with a ~22.7k-token filler
    block, `--output-format json`, reading the result event's `usage`:

        shared PREFIX (stable first, volatile last)
            A warm    create=27063  read=17418
            B reuse   create=22765  read=21719
        shared SUFFIX (volatile first, stable last - the order in use)
            C warm    create=22763  read=21719
            D reuse   create=22769  read=21719
        CONTROL, byte-identical prompt twice
            E warm    create=26633  read=17418
            F same    create=0      read=44051

    B is the decisive one: it shared a 22.7k-token prefix with A and still
    wrote the whole thing to cache fresh. F shows the cache works perfectly
    well - on an exact match. So the CLI sends the prompt as one content block
    with its breakpoint at the end, and Anthropic's cache matches at
    breakpoints; there is no partial-prefix match inside a block. Two different
    projects' prompts are never byte-identical, so no ordering of the bytes
    inside that block can ever help.

    This test guards the conclusion, not the API: if someone reads this module
    and wonders why it budgets instead of reorders, the numbers are here.
    """
    prefix_shared_reuse_created = 22765
    identical_reuse_created = 0
    assert prefix_shared_reuse_created > 20000, "a shared prefix bought nothing"
    assert identical_reuse_created == 0, "an identical prompt was a full hit"
    # And so the only lever left on prompt cost is the prompt's size, which is
    # what every other test in this file is about.
    assert pb.learnings_for_prompt(FILE, 300, "/m") != FILE


# --- The answered-questions log ----------------------------------------------
#
# Measured on the live database 2026-07-29: Project Portal's prompt carried 25
# answered questions, 11.8 KB, of which TEN were the same question about
# spending down a Claude window - and Wes's answer to the seventh of them was
# "You asked me way too many times here. I just want to be asked once."
#
# The block had no bound of any kind. Every question ever answered on a project
# was in every prompt that project would ever build.


def _spend_down(minutes: int) -> pb.Answered:
    return pb.Answered(
        question=(
            f"Your weekly Claude window resets in 7h {minutes:02d}m with 47% of it "
            "unused - that headroom does not roll over, it just disappears at the "
            "reset. Want me to spend it down? Say yes and I will lift the portal's "
            "own run budget and pacing until then and work through the backlog; say "
            "no and I will leave it alone."
        ),
        answer="yes",
    )


# Genuinely unrelated questions, because "Distinct question number 3?" is NOT
# distinct to `qdedupe` - only a digit varies, and a digit is not a topic. That
# was found by writing the lazy fixture first and watching forty "different"
# questions collapse into one, which is the dedupe working correctly.
_TOPICS = [
    "which license the public repository should carry",
    "whether the fridge dashboard should redraw hourly or twice a day",
    "what the drum click preset ought to sound like",
    "whether chores expire at midnight or at the day-boundary hour",
    "how a proxy card should print its mana symbols",
    "whether backups belong on the NAS or off-site",
    "what happens to a deck import that names an unknown card",
    "whether the tunnel should serve games on a path or a subdomain",
    "how long a frozen milk bag stays good for",
    "whether a scanned mesh gets decimated before it reaches Fusion",
    "what the e-ink refresh should do on a low battery",
    "whether the bass tab editor writes MIDI or MusicXML",
]


def _varied(n: int) -> list[pb.Answered]:
    return [
        pb.Answered(
            f"A decision about {_TOPICS[i % len(_TOPICS)]}, number {i}?",
            f"answer {i} " + "x" * 400,
        )
        for i in range(n)
    ]


def test_ten_askings_of_one_question_collapse_to_one():
    pairs = [_spend_down(m) for m in (59, 56, 46, 36, 31, 21, 16, 6, 3, 1)]
    collapsed = pb.collapse_repeats(pairs)
    assert len(collapsed) == 1
    assert collapsed[0][1] == 9


def test_the_newest_asking_is_the_one_that_survives():
    """A later answer supersedes an earlier one - that is what answering again
    means - so the survivor is the newest whether or not the answers agree."""
    pairs = [
        pb.Answered("Shall I use SQLite or Postgres for this?", "SQLite"),
        pb.Answered("Shall I use Postgres or SQLite for this?", "Postgres, actually"),
    ]
    collapsed = pb.collapse_repeats(pairs)
    assert len(collapsed) == 1
    assert collapsed[0][0].answer == "Postgres, actually"


def test_two_genuinely_different_questions_never_collapse():
    pairs = [
        pb.Answered("Which license should the public repo carry?", "AGPL-3.0"),
        pb.Answered("Should the settings page group fields into sub-tabs?", "yes"),
    ]
    assert len(pb.collapse_repeats(pairs)) == 2


def test_the_order_out_is_the_order_in():
    """Wes reads oldest-to-newest. The block is FILLED newest-first, so this is
    the assertion that the two directions have not been confused."""
    pairs = [
        pb.Answered("First question ever asked?", "first answer"),
        pb.Answered("A completely unrelated second matter?", "second answer"),
    ]
    text = pb.answered_for_prompt(pairs, 10_000)
    assert text.index("first answer") < text.index("second answer")


def test_the_repeat_count_is_shown_rather_than_hidden():
    """An agent that can see a question was asked ten times learns something
    true about how it behaved. Hiding it is the portal covering for itself."""
    text = pb.answered_for_prompt([_spend_down(m) for m in (59, 46, 31)], 10_000)
    assert "asked 3 times" in text
    assert "near-identical" in text


def test_a_question_asked_once_carries_no_repeat_note():
    text = pb.answered_for_prompt([pb.Answered("Just the once?", "yes")], 10_000)
    assert "asked" not in text
    assert text == "- Q: Just the once?\n  A: yes"


def test_the_block_is_bounded_by_bytes():
    pairs = _varied(12)
    text = pb.answered_for_prompt(pairs, 4096)
    # The notice about what was left out rides on top of the budget; the Q&A
    # lines themselves are what it bounds.
    body = text.split("\n\n(")[0]
    assert len(body) <= 4096


def test_the_newest_decisions_are_the_ones_kept():
    """A decision made this week is likelier to still bind than one from three
    weeks ago."""
    pairs = _varied(12)
    text = pb.answered_for_prompt(pairs, 4096)
    assert "answer 11 " in text
    assert "answer 0 " not in text


def test_a_trimmed_block_says_what_was_left_out_and_where_it_is():
    pairs = _varied(12)
    text = pb.answered_for_prompt(pairs, 4096)
    assert "older answered question(s) are left out" in text
    assert "project page" in text


def test_an_unbudgeted_block_carries_no_notice():
    pairs = [pb.Answered("A?", "1"), pb.Answered("Something else entirely, B?", "2")]
    text = pb.answered_for_prompt(pairs, 10_000)
    assert "(" not in text


def test_repeats_collapse_before_the_budget_is_spent():
    """The ordering is the whole point: a project that asked one question ten
    times must not lose nine real decisions paying for nine copies of one."""
    pairs = [_spend_down(m) for m in (59, 56, 46, 36, 31, 21, 16, 6, 3, 1)]
    pairs += _varied(5)
    text = pb.answered_for_prompt(pairs, 4096)
    for i in range(5):
        assert f"answer {i} " in text


def test_no_questions_at_all_reads_as_none():
    assert pb.answered_for_prompt([], 4096) == "(none)"


def test_one_answer_survives_a_budget_too_small_for_it():
    """Same rule as the completed todo tail: a block that admits nothing claims
    nothing was ever decided."""
    text = pb.answered_for_prompt([pb.Answered("Q?", "x" * 5000)], 100)
    assert "x" * 5000 in text


def test_the_person_who_answered_is_named_when_there_is_one():
    text = pb.answered_for_prompt([pb.Answered("Q?", "yes", who="Karli")], 4096)
    assert "A (Karli): yes" in text


def test_nobody_is_named_on_a_one_person_install():
    text = pb.answered_for_prompt([pb.Answered("Q?", "yes")], 4096)
    assert "A: yes" in text


# --- the wiring into an actual run prompt ------------------------------------
#
# The functions above are pure and easy to test; what the delete-the-fix sweep
# found on 2026-07-29 is that NOTHING owned the line joining them to a real
# prompt. Replacing the budget with a billion at the call site broke no test.


def _answer(project_id, question, answer):
    row = db.create_question(project_id, question)
    db.answer_question(row["id"], answer)
    return row


def test_the_answered_block_in_a_real_prompt_honors_its_budget(temp_data_dir):
    project = db.create_project("Budgeted", stage="active", build_approved=True,
                                slug="budgeted")
    db.set_setting("prompt_answered_kb", "1")
    for topic in _TOPICS:
        _answer(project["id"], f"A decision about {topic}?", "y" * 400)

    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "## Answered questions" in prompt
    assert "older answered question(s) are left out" in prompt


def test_raising_the_setting_puts_the_older_answers_back(temp_data_dir):
    project = db.create_project("Budgeted", stage="active", build_approved=True,
                                slug="budgeted")
    for topic in _TOPICS:
        _answer(project["id"], f"A decision about {topic}?", "y" * 400)

    db.set_setting("prompt_answered_kb", "128")
    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "left out" not in prompt


def test_a_project_with_no_answered_questions_still_gets_the_heading(temp_data_dir):
    project = db.create_project("Fresh", stage="active", build_approved=True, slug="fresh")
    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "## Answered questions\n(none)" in prompt

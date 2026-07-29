"""The learned half of a person's background (#357).

Wes, 2026-07-28, on his wife using the portal: "It would be good if it could
learn what she understands and speak to her at that level."

Her `background` sentence is what somebody typed. This is the other half: a
short file per person that the daily reflect grows from the notes and answers
that person actually wrote, injected under their bullet in every prompt for a
project they are on.

What is worth pinning here, in the order it can go wrong:

- **No agent may ever write the hand-typed half.** They are stored apart, which
  is what makes that structural rather than a rule somebody has to keep. The
  same failure - "Wes typed things about himself into the profile that a later
  reflect quietly replaced" - is why app/memory.py exists at all.
- The prompt is byte-for-byte unchanged while there is only one person, exactly
  like the rest of the people work.
- The learned lines are labeled in the prompt as INFERRED, because an agent
  weighing them against a person's own words has to know which is which.
- The render cap holds, and it trims at a line boundary - half a claim about a
  person is worse in a prompt than no claim.
- The slug reaching the clear route cannot escape the memory directory.
- The reflect prompt hands the agent evidence, and that evidence is only ever
  words that person typed.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import agent_runner, config, db, main, people


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project(title="A project", description="d")


@pytest.fixture
def karli():
    """A second person, so the people sections switch on at all."""
    pid = people.add(name="Karli", gender="female", background="New to all of this.")
    return people.get(pid)


def _write_learned(slug: str, text: str) -> None:
    path = people.learned_path(slug)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# The two halves stay apart
# --------------------------------------------------------------------------

def test_the_hand_written_background_lives_in_the_database_and_the_learned_half_on_disk(karli):
    """The whole design in one assertion.

    An agent maintaining the learned half has a file and nothing else, so there
    is no code path by which a reflect can reach the sentence a person typed
    about themselves. That is the point of splitting them rather than asking an
    agent to preserve part of one field.
    """
    _write_learned("karli", "- Knows what a git commit is.")
    assert people.read_learned("karli") == "- Knows what a git commit is."
    # Untouched by anything the learned half did.
    assert people.get(int(karli["id"]))["background"] == "New to all of this."
    # And clearing the learned half leaves the typed half exactly where it was.
    assert people.clear_learned("karli") is True
    assert people.read_learned("karli") == ""
    assert people.get(int(karli["id"]))["background"] == "New to all of this."


def test_learned_text_is_not_writable_through_the_person_editor(karli):
    """`update()` takes a fixed field list, and the learned half is not on it -
    so a stray form field or a future caller cannot start writing inferences
    into the half that is supposed to be a person's own words."""
    assert "learned" not in people._EDITABLE
    people.update(int(karli["id"]), learned="- something an agent decided")
    assert people.read_learned("karli") == ""


# --------------------------------------------------------------------------
# What reaches a run's prompt
# --------------------------------------------------------------------------

def test_a_single_person_install_gets_no_learned_text_at_all(project):
    """The same promise the rest of the people work makes: nothing shifts under
    a feature nobody is using. `prompt_section` is empty below two people, so a
    learned file left on disk cannot leak into a one-person prompt."""
    _write_learned(people.owner()["slug"], "- Should not appear.")
    assert people.prompt_section(project["id"]) == ""


def test_the_learned_lines_reach_the_project_prompt(project, karli):
    people.add_member(project["id"], int(karli["id"]))
    _write_learned("karli", "- Knows what a git commit is now, so it can be named.")
    section = people.prompt_section(project["id"])
    assert "Knows what a git commit is now" in section
    # Directly under her own sentence, not in a section of its own: an agent
    # reading one has to be reading the other.
    assert section.index("New to all of this.") < section.index("Knows what a git commit is")


def test_the_learned_lines_are_labeled_as_inferred_not_stated(karli):
    """An agent weighing "she knows what a commit is" against her own words has
    to be able to tell which of the two she actually said. Without this the two
    halves are indistinguishable once they are one block of prompt text."""
    _write_learned("karli", "- Knows what a git commit is.")
    line = people.describe(people.get(int(karli["id"])))
    assert "not stated by her" in line
    assert "inferred from what she wrote" in line


def test_the_label_does_not_conjugate_a_verb_to_the_pronoun(karli):
    """"what she wrote" reads correctly for he, she and they alike; "has
    written" does not. A person's own pronouns are not a place to be sloppy,
    and this is the kind of thing no other test would ever look at."""
    _write_learned("karli", "- A line.")
    for gender, expected in (("female", "what she wrote"), ("male", "what he wrote")):
        people.update(int(karli["id"]), gender=gender)
        line = people.describe(people.get(int(karli["id"])))
        assert expected in line


def test_a_person_with_no_learned_file_renders_exactly_as_before(project, karli):
    """The bullet for somebody the reflect has said nothing about must not grow
    an empty label, a stray colon or a blank line."""
    people.add_member(project["id"], int(karli["id"]))
    with_none = people.describe(people.get(int(karli["id"])))
    assert "inferred" not in with_none
    assert with_none.rstrip() == with_none
    assert with_none.splitlines() == ["- **Karli** (she/her)", "  New to all of this."]


# --------------------------------------------------------------------------
# The cap
# --------------------------------------------------------------------------

def test_a_runaway_learned_file_is_capped_before_it_reaches_a_prompt(karli):
    """Every run of every project this person is on pays for this file, so an
    agent that ignores its line limit must degrade to "some is dropped", never
    to "every prompt grows forever"."""
    _write_learned("karli", "\n".join(f"- Line number {i} about her." for i in range(500)))
    trimmed = people._learned_for_prompt("karli")
    assert 0 < len(trimmed) <= people.LEARNED_PROMPT_CHARS
    assert "- Line number 0 about her." in trimmed


def test_the_cap_trims_at_a_line_boundary(karli):
    """Half a sentence about a person is worse in a prompt than no sentence:
    "She does not understand" is a different claim from "She does not
    understand why it needs a restart, but picks it up when told once"."""
    long_line = "- " + ("word " * 60).strip() + "."
    _write_learned("karli", "\n".join([long_line] * 20))
    trimmed = people._learned_for_prompt("karli")
    assert trimmed
    for line in trimmed.splitlines():
        assert line == long_line


def test_a_file_under_the_cap_is_passed_through_untouched(karli):
    _write_learned("karli", "- One.\n- Two.")
    assert people._learned_for_prompt("karli") == "- One.\n- Two."


# --------------------------------------------------------------------------
# The slug arrives from a URL
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "slug", ["../learnings", "..", "a/b", "/etc/passwd", "", "  ", "-nope", "Karli!"]
)
def test_a_slug_that_could_escape_the_memory_directory_is_refused(slug):
    assert people.learned_path(slug) is None
    assert people.read_learned(slug) == ""
    assert people.clear_learned(slug) is False


def test_clearing_something_that_was_never_learned_is_not_a_success(karli):
    """The route turns False into a 404, and "nothing happened" must not read
    as "cleared" - otherwise the page says it threw something away that is
    still in every prompt."""
    assert people.clear_learned("karli") is False


def test_the_learned_path_sits_inside_the_memory_directory(karli):
    path = people.learned_path("karli")
    assert path is not None
    assert path.resolve().parent == (config.MEMORY_DIR / "people").resolve()


# --------------------------------------------------------------------------
# What the reflect job is given
# --------------------------------------------------------------------------

def test_the_reflect_section_is_empty_with_only_one_person():
    """Same promise as everywhere else in the people work: the reflect prompt
    is byte-for-byte what it was until there is a second person."""
    assert people.reflect_section() == ""


def test_the_reflect_prompt_carries_the_per_person_section(karli, project):
    prompt = agent_runner.build_prompt("reflect", None)
    assert "## What each person understands" in prompt
    assert "people/karli.md" in prompt
    # And the task guidance points at it, so an agent that reads the top of its
    # prompt and stops knows there is a second job further down.
    assert "What each person understands" in agent_runner.TASK_GUIDANCE["reflect"]


def test_the_reflect_section_shows_both_halves_and_says_which_is_which(karli):
    _write_learned("karli", "- Knows what a commit is.")
    section = people.reflect_section()
    assert "Says about themselves: New to all of this." in section
    assert "Knows what a commit is." in section
    assert "Do not touch what they said about themselves." in section


def test_the_reflect_section_hands_over_the_evidence(project, karli):
    """The rule the agent is given is "write only what the evidence shows", so
    the evidence has to be in the prompt. Without this the instruction is an
    invitation to invent."""
    db.add_journal(project["id"], "user", "note", "How do I know which port it is on?",
                   person_id=int(karli["id"]))
    section = people.reflect_section()
    assert "How do I know which port it is on?" in section


def test_the_evidence_is_only_ever_words_that_person_typed(project, karli):
    """The one way this feature becomes actively harmful is by attributing
    somebody else's words to a person and then reasoning about how they think.
    Rows with no person behind them - an agent's report, a system status line,
    another person's note - must not appear under their heading."""
    db.add_journal(project["id"], "user", "note", "KARLI-WROTE-THIS",
                   person_id=int(karli["id"]))
    db.add_journal(project["id"], "user", "note", "WES-WROTE-THIS",
                   person_id=int(people.owner()["id"]))
    db.add_journal(project["id"], "agent", "progress", "AGENT-WROTE-THIS")
    db.add_journal(None, "system", "status", "SYSTEM-WROTE-THIS")

    rows = db.list_person_writings(int(karli["id"]))
    bodies = [r["content_md"] for r in rows]
    assert bodies == ["KARLI-WROTE-THIS"]

    # And in the section, her heading is followed by her line and not by his.
    section = people.reflect_section()
    hers = section.index("### Karli")
    assert "KARLI-WROTE-THIS" in section[hers:]
    assert "WES-WROTE-THIS" not in section[hers:]
    assert "AGENT-WROTE-THIS" not in section
    assert "SYSTEM-WROTE-THIS" not in section


def test_an_unattributed_row_is_never_guessed_at(project, karli):
    """`person_id` NULL means "the channel could not say", and the portal's
    rule throughout is that a missing attribution beats an invented one. A
    falsy person id must not match those rows either."""
    db.add_journal(project["id"], "user", "note", "NOBODY-KNOWS-WHO")
    assert db.list_person_writings(0) == []
    assert db.list_person_writings(None) == []
    for row in db.list_person_writings(int(karli["id"])):
        assert row["content_md"] != "NOBODY-KNOWS-WHO"


def test_a_person_who_has_written_nothing_is_told_to_be_left_alone(karli):
    """A reflect with no evidence for somebody must change nothing about them.
    Saying so explicitly beats leaving a heading with an empty space under it,
    which reads as an invitation to fill it in."""
    section = people.reflect_section()
    assert "there is no evidence and their file must not change" in section


def test_the_evidence_for_one_entry_is_bounded(project, karli):
    """A single dictated note can run to thousands of characters, and a dozen
    of those across every person would crowd out the rest of the reflect."""
    db.add_journal(project["id"], "user", "note", "x" * 5000, person_id=int(karli["id"]))
    section = people.reflect_section()
    assert "x" * people.EVIDENCE_CHARS in section
    assert "x" * (people.EVIDENCE_CHARS + 1) not in section


def test_the_compaction_agent_is_told_to_leave_these_files_alone():
    """It runs with the same cwd as the reflect and is told to rewrite a file
    in it, so "rewrite learnings.md" and "there are other files here" have to
    be said in the same breath."""
    assert "people/" in agent_runner.TASK_GUIDANCE["compact"]


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

def test_the_overview_lists_a_person_with_nothing_learned_yet(karli):
    """"The reflect has not worked this person out yet" is the answer /memory
    is being asked for, so an absence has to be visible rather than a row that
    simply is not there."""
    rows = {r["slug"]: r for r in people.learned_overview()}
    assert "karli" in rows
    assert rows["karli"]["lines"] == []
    assert rows["karli"]["background"] == "New to all of this."


def test_the_overview_skips_a_file_belonging_to_nobody(karli):
    """A file left behind by a deleted person stays on disk - deleting somebody
    should not reach into the memory directory - but it must never be shown or
    reach a prompt."""
    _write_learned("ghost", "- About somebody who is gone.")
    slugs = {r["slug"] for r in people.learned_overview()}
    assert "ghost" not in slugs


def test_the_memory_page_shows_nothing_of_this_on_a_one_person_install(client):
    """Every install is a one-person install until somebody adds a second, and
    a heading over one row that can never fill in is worse than no heading."""
    body = client.get("/memory").text
    assert "What we've learned about each person" not in body


def test_the_memory_page_shows_both_halves(client, karli):
    _write_learned("karli", "- Knows what a git commit is.")
    body = client.get("/memory").text
    assert "What we've learned about each person" in body
    assert "Knows what a git commit is." in body
    assert "New to all of this." in body


def test_the_clear_button_removes_it_and_leaves_what_she_said_untouched(client, karli):
    _write_learned("karli", "- Knows what a git commit is.")
    r = client.post("/memory/person/karli/clear", follow_redirects=False)
    assert r.status_code == 303
    assert people.read_learned("karli") == ""
    assert people.get(int(karli["id"]))["background"] == "New to all of this."


def test_clearing_a_slug_that_is_nobody_is_a_404_not_a_redirect(client, karli):
    """A traversal attempt and a typo both land here, and both have to fail
    loudly - a 303 back to /memory would say something was thrown away."""
    for slug in ("nobody", "..", "..%2Flearnings"):
        assert client.post(f"/memory/person/{slug}/clear",
                           follow_redirects=False).status_code == 404


def test_the_confirm_prompt_says_what_survives(client, karli):
    """Wes reads this on his phone before tapping. It has to say that his own
    sentence about her is kept, or the button reads as more destructive than
    it is and never gets pressed."""
    _write_learned("karli", "- A line.")
    body = client.get("/memory").text
    assert "What Karli said about themselves is kept" in body

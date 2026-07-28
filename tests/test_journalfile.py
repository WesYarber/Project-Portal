"""The journal mirror in the workspace, and the promise the prompt makes about it.

On 2026-07-28 the journal section of a run's prompt was put under a byte budget:
the newest entry stays whole, older ones degrade to their heading plus opening
paragraph. That run's own report named the gap it left:

    "An agent can read the full learnings file on disk, but the full journal
    lives in the database, so a shortened entry has no fallback an agent can
    reach."

The notice in the prompt said the full text was "on the project page", which is
true and useless: a human can open that page and an agent inside a workspace
cannot. So the portal now writes the journal to `<workspace>/.portal/journal.md`
before every run and the notice names that path instead.

The load-bearing property is not that a file exists. It is that **every entry the
prompt shortened is in it** - a pointer into a file that does not contain the
entry is worse than no pointer, because an agent that follows it and finds
nothing concludes the history is gone rather than that a write failed. That
property is pinned by `test_every_entry_the_prompt_shortened_is_in_the_file`, and
the ordering that guarantees it under a race by
`test_the_file_is_written_before_the_prompt_that_points_at_it`.
"""
from __future__ import annotations

import pytest

from app import agent_runner, config, db, filetree, journalfile, promptbudget, worker


@pytest.fixture
def project():
    return db.create_project("Metronome", description="A thing.", stage="active",
                             build_approved=True, slug="metronome")


@pytest.fixture
def workspace(project):
    ws = config.PROJECTS_DIR / project["slug"]
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _mirror(project, workspace) -> str:
    journalfile.write(project, workspace)
    return (workspace / journalfile.RELPATH).read_text(encoding="utf-8")


# --- what lands in the file --------------------------------------------------


def test_the_whole_journal_is_written_oldest_first(project, workspace):
    for n in range(1, 4):
        db.add_journal(project["id"], "agent", "progress", f"## Run {n}\n\nBody {n}.")
    text = _mirror(project, workspace)

    for n in range(1, 4):
        assert f"Body {n}." in text
    assert text.index("Body 1.") < text.index("Body 2.") < text.index("Body 3.")


def test_the_ask_side_thread_is_left_out_here_as_it_is_in_the_prompt(project, workspace):
    # An ask is a parallel question to the portal, not an instruction to a run.
    # The prompt drops it on purpose (db.SIDE_THREAD); a fallback that carried it
    # would smuggle back in exactly what the prompt excludes.
    db.add_journal(project["id"], "agent", "progress", "A real progress entry.")
    db.add_journal(project["id"], "user", "ask", "What does this button do?")
    db.add_journal(project["id"], "agent", "answer", "It arms the metronome.")

    text = _mirror(project, workspace)
    assert "A real progress entry." in text
    assert "arms the metronome" not in text
    assert "What does this button do?" not in text


def test_a_note_dictated_on_a_phone_does_not_give_the_file_two_line_endings(project,
                                                                            workspace):
    # A browser posts a textarea with CRLF, so every note Wes dictates from his
    # phone arrives that way, while everything an agent writes is LF. This was
    # invisible to the fixtures above - they are all LF, so they shared the
    # assumption with the code - and turned up only on rendering the real journal.
    #
    # Read in BINARY, and that is the whole test. `Path.read_text` opens in
    # universal-newlines mode and turns every CRLF on disk back into LF, so the
    # obvious version of this test passes just as happily with the fix deleted -
    # it is reading through the very layer that hides the bug. The first attempt
    # here did exactly that and survived its own sabotage.
    db.add_journal(project["id"], "user", "note",
                   "Get rid of the counter.\r\n\r\nAnd fix the suggestion error.")
    db.add_journal(project["id"], "agent", "progress", "## A run\n\nBody.")

    journalfile.write(project, workspace)
    raw = (workspace / journalfile.RELPATH).read_bytes()
    assert b"\r" not in raw
    assert b"Get rid of the counter.\n\nAnd fix the suggestion error." in raw


def test_the_file_warns_that_it_is_rewritten_and_not_to_be_committed(project, workspace):
    db.add_journal(project["id"], "agent", "progress", "Something.")
    text = _mirror(project, workspace)
    assert "before each run" in text
    assert "do not commit it" in text


# --- finding one entry in it -------------------------------------------------


def test_an_entry_starts_at_a_heading_whose_text_is_a_timestamp(project, workspace):
    # A progress report's body is itself full of `## ` headings, so "a heading"
    # cannot be the delimiter. Requiring the heading text to be a bracketed ISO
    # timestamp is what prose never accidentally produces - including prose that
    # deliberately writes a bracketed heading of its own.
    db.add_journal(project["id"], "agent", "progress",
                   "## The fix\n\n## [see the note above]\n\nStill one entry.")
    db.add_journal(project["id"], "agent", "progress", "## Another run\n\nBody.")

    text = _mirror(project, workspace)
    assert len(journalfile.ENTRY_RE.findall(text)) == 2


def test_an_entry_is_found_by_the_timestamp_the_prompt_showed_for_it(project, workspace):
    db.add_journal(project["id"], "agent", "progress", "## A run\n\nThe details.")
    row = db.list_journal(project["id"])[0]
    text = _mirror(project, workspace)

    # The prompt prefixes an entry with `- [ts] author/kind: `; the file heads it
    # with `## [ts] author/kind`. Same two fields in the same order, so the
    # timestamp off the prompt is a search that lands.
    assert f"## [{row['ts']}] agent/progress" in text


# --- the promise the prompt makes -------------------------------------------


def test_every_entry_the_prompt_shortened_is_in_the_file(project, workspace, monkeypatch):
    # Twelve fat entries against a small budget, so the prompt has no choice but
    # to shorten most of them. Whatever it shortens must be readable in the file.
    bodies = []
    for n in range(12):
        # Every third one CRLF, the way a note dictated on a phone arrives, so the
        # promise is tested against both line endings and not only the tidy one.
        nl = "\r\n" if n % 3 == 0 else "\n"
        body = (f"## Run number {n}{nl}{nl}Opening paragraph for run {n}.{nl}{nl}"
                + f"Detail line {n} that only the full text carries.{nl}" * 40)
        bodies.append(body)
        db.add_journal(project["id"], "agent", "progress", body)

    journalfile.write(project, workspace)
    monkeypatch.setattr(db, "get_setting",
                        lambda k, *a, **kw: "4" if k == "prompt_journal_kb" else None)
    prompt = agent_runner.build_prompt("build", project)
    text = (workspace / journalfile.RELPATH).read_text(encoding="utf-8")

    shortened = [b for b in bodies if b not in prompt]
    assert shortened, "budget too loose - this test is not testing anything"
    for body in shortened:
        assert journalfile._lf(body).strip() in text


def test_the_prompt_names_the_file_only_when_it_is_actually_there(project, workspace,
                                                                  monkeypatch):
    for n in range(12):
        db.add_journal(project["id"], "agent", "progress",
                       f"## Run {n}\n\nOpening.\n\n" + f"Filler {n}.\n" * 40)
    monkeypatch.setattr(db, "get_setting",
                        lambda k, *a, **kw: "4" if k == "prompt_journal_kb" else None)

    without = agent_runner.build_prompt("build", project)
    assert "shortened to their heading" in without
    assert journalfile.RELPATH not in without
    assert "on the project page" in without

    journalfile.write(project, workspace)
    with_file = agent_runner.build_prompt("build", project)
    assert journalfile.RELPATH in with_file


def test_a_short_journal_needs_no_pointer_at_all(project, workspace):
    db.add_journal(project["id"], "agent", "progress", "## One run\n\nShort.")
    journalfile.write(project, workspace)
    prompt = agent_runner.build_prompt("build", project)
    assert "shortened to their heading" not in prompt
    assert journalfile.RELPATH not in prompt


def test_the_pointer_is_the_relative_path_the_agent_can_open(project, workspace):
    assert journalfile.pointer(project) == ""
    journalfile.write(project, workspace)
    assert journalfile.pointer(project) == journalfile.RELPATH
    assert not journalfile.RELPATH.startswith("/")


# --- the ceiling -------------------------------------------------------------


def test_the_cap_drops_whole_oldest_entries_and_says_how_many(project, workspace,
                                                              monkeypatch):
    monkeypatch.setattr(journalfile, "MAX_BYTES", 2000)
    for n in range(20):
        db.add_journal(project["id"], "agent", "progress",
                       f"## Run {n}\n\n" + f"Body of run {n}.\n" * 20)

    text = _mirror(project, workspace)
    assert "Body of run 19." in text          # newest survives
    assert "Body of run 0." not in text       # oldest dropped
    assert "oldest entries are not in this file" in text
    assert "on the project page" in text


def test_an_entry_is_never_cut_in_half(project, workspace, monkeypatch):
    # A journal entry truncated mid-sentence reads as a complete report of work
    # that was in fact done differently. Absent and counted beats present and
    # wrong, so the ceiling drops entries, never bytes off one.
    monkeypatch.setattr(journalfile, "MAX_BYTES", 1500)
    for n in range(10):
        db.add_journal(project["id"], "agent", "progress",
                       f"## Run {n}\n\nSTART{n} " + "x" * 400 + f" END{n}")

    text = _mirror(project, workspace)
    for n in range(10):
        assert (f"START{n}" in text) == (f"END{n}" in text)


def test_one_entry_larger_than_the_whole_ceiling_is_still_written(project, workspace,
                                                                  monkeypatch):
    # Otherwise the newest handover - the one thing the budget exists to protect -
    # would be the single entry the fallback drops.
    monkeypatch.setattr(journalfile, "MAX_BYTES", 100)
    db.add_journal(project["id"], "agent", "progress", "## Huge\n\n" + "y" * 5000)
    assert "y" * 5000 in _mirror(project, workspace)


# --- failing without taking the run down -------------------------------------


def test_a_workspace_that_cannot_be_written_does_not_stop_the_run(project, workspace,
                                                                  monkeypatch):
    def boom(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(journalfile.Path, "write_text", boom)
    assert journalfile.write(project, workspace) is None
    assert journalfile.pointer(project) == ""


def test_yesterdays_file_is_still_a_good_fallback(project, workspace):
    # A stale mirror is not a broken promise: everything the budget shortens is by
    # definition not the newest entry, so a file one run behind still holds it.
    db.add_journal(project["id"], "agent", "progress", "## Yesterday\n\nOld body.")
    journalfile.write(project, workspace)
    db.add_journal(project["id"], "agent", "progress", "## Today\n\nNew body.")

    assert journalfile.pointer(project) == journalfile.RELPATH
    text = (workspace / journalfile.RELPATH).read_text(encoding="utf-8")
    assert "Old body." in text


# --- the wiring in the worker ------------------------------------------------


@pytest.mark.asyncio
async def test_the_file_is_written_before_the_prompt_that_points_at_it(project,
                                                                       monkeypatch):
    # This ordering is the entire correctness argument. Written first, an entry
    # created in the gap can only be the newest one - which the prompt always
    # shows whole. Written second, the same race shortens an entry in the prompt
    # that is missing from the file.
    db.add_journal(project["id"], "agent", "progress", "## A run\n\nBody.")
    seen: dict = {}
    real = agent_runner.build_prompt

    def spy(task, proj):
        seen["existed"] = (config.PROJECTS_DIR / proj["slug"]
                           / journalfile.RELPATH).is_file()
        return real(task, proj)

    monkeypatch.setattr(agent_runner, "build_prompt", spy)

    async def fake_run(*a, **kw):
        return agent_runner.RunResult(ok=False, subtype="error_during_execution")

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    await worker.run_project_task(project, "build")
    assert seen["existed"] is True


@pytest.mark.asyncio
async def test_the_mirror_is_kept_out_of_the_projects_own_git(project, monkeypatch):
    # .portal/ is the portal's drop box in someone else's repository. Rewritten
    # before every run, it would otherwise be a permanently dirty `git status`
    # and a diff on every commit an agent makes.
    db.add_journal(project["id"], "agent", "progress", "## A run\n\nBody.")

    async def fake_run(*a, **kw):
        return agent_runner.RunResult(ok=False, subtype="error_during_execution")

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    await worker.run_project_task(project, "build")

    exclude = (config.PROJECTS_DIR / project["slug"] / ".git" / "info" / "exclude")
    assert ".portal/" in exclude.read_text(encoding="utf-8").split()


# --- and out of the workspace file browser -----------------------------------


def test_the_mirror_is_not_a_row_in_the_workspace_file_browser(project, workspace):
    # It is the portal's own file, its content is already rendered in the journal
    # timeline further up the same page, and at its real size (530 KB on this
    # install) fileview would refuse to display it anyway.
    db.add_journal(project["id"], "agent", "progress", "## A run\n\nBody.")
    journalfile.write(project, workspace)
    (workspace / ".portal" / "report.json").write_text("{}")

    names = [e.name for e in filetree.children(workspace, ".portal")]
    assert "journal.md" not in names
    assert "report.json" in names, "the rest of .portal/ stays visible"


def test_the_hidden_mirror_is_not_counted_in_the_file_total(project, workspace):
    # The header count is meant to match what opening every folder would show.
    db.add_journal(project["id"], "agent", "progress", "## A run\n\nBody.")
    (workspace / "main.py").write_text("print(1)")
    before, _ = filetree.count_files(workspace)
    journalfile.write(project, workspace)
    after, _ = filetree.count_files(workspace)
    assert after == before


# --- the notice itself -------------------------------------------------------


def test_the_notice_says_search_for_the_timestamp(project):
    entries = [promptbudget.JournalEntry(prefix=f"- [ts{n}] agent/progress: ",
                                         body=f"## Run {n}\n\nOpening.\n\n" + "x" * 900)
               for n in range(6)]
    text = promptbudget.journal_for_prompt(entries, 1200, journalfile.RELPATH)
    assert journalfile.RELPATH in text
    assert "timestamp" in text
    assert "on the project page" not in text

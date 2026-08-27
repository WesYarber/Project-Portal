"""The shared memory files: versioning, compaction, and what agents may add.

Wes, 2026-07-22 03:15 and 03:17:

    "My 'learnings' memory is very messy and contains a lot of unhelpful info
    it seems. Add a button to trigger an agent to go through and compress it
    down. I don't care about adding the time unless it is specifically relevant
    ... the agents storing these memories should be more considerate about what
    they are doing there."

    "I also had some important 'about me' stuff that I had typed to the profile
    or learnings sections that is now all gone. Do you have record of what these
    files use to be...?"

The answer to the second was no - nothing anywhere kept a copy - so these tests
cover the mechanism that makes the answer yes from now on, and they are grouped
by the claim they defend: the history (which is the part that must never lose
anything), what a run is allowed to append, the compaction task, and the page.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, memory, worker


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _write(stem: str, text: str) -> None:
    path = memory.tracked()[stem]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


T0 = datetime(2026, 7, 22, 4, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# The history
# --------------------------------------------------------------------------

def test_a_snapshot_copies_the_current_file(temp_data_dir):
    _write("profile", "# Profile\n- Ada lives in Arkansas\n")
    path = memory.snapshot("profile", when=T0)
    assert path is not None
    assert path.name == "profile-20260722T040000Z.md"
    assert path.read_text() == "# Profile\n- Ada lives in Arkansas\n"


def test_the_history_lives_outside_the_agents_working_directory(temp_data_dir):
    # MEMORY_DIR is the cwd of the reflect and compaction agents. Old copies of
    # learnings.md sitting in there would be handed to the very agent whose job
    # is to cut the file down.
    _write("learnings", "- a thing\n")
    path = memory.snapshot("learnings", when=T0)
    assert config.MEMORY_DIR not in path.parents


def test_an_unchanged_file_is_not_snapshotted_twice(temp_data_dir):
    _write("profile", "same\n")
    assert memory.snapshot("profile", when=T0) is not None
    assert memory.snapshot("profile", when=T0 + timedelta(hours=1)) is None
    assert len(memory.revisions("profile")) == 1


def test_a_changed_file_is_snapshotted_again(temp_data_dir):
    _write("profile", "one\n")
    memory.snapshot("profile", when=T0)
    _write("profile", "two\n")
    memory.snapshot("profile", when=T0 + timedelta(minutes=1))
    assert [r.name.split("-")[1] for r in memory.revisions("profile")] == [
        "20260722T040100Z.md",
        "20260722T040000Z.md",
    ]


def test_two_snapshots_in_the_same_second_both_survive(temp_data_dir):
    _write("profile", "one\n")
    first = memory.snapshot("profile", when=T0)
    _write("profile", "two\n")
    second = memory.snapshot("profile", when=T0)
    assert first != second
    assert len(memory.revisions("profile")) == 2


def test_a_missing_or_empty_file_is_not_snapshotted(temp_data_dir):
    assert memory.snapshot("profile", when=T0) is None
    _write("profile", "   \n\n")
    assert memory.snapshot("profile", when=T0) is None


def test_an_unknown_stem_is_refused(temp_data_dir):
    assert memory.snapshot("portal.db", when=T0) is None


def test_revisions_are_newest_first_and_can_be_filtered(temp_data_dir):
    _write("profile", "p\n")
    _write("learnings", "l\n")
    memory.snapshot("profile", when=T0)
    memory.snapshot("learnings", when=T0 + timedelta(minutes=5))
    everything = memory.revisions()
    assert [r.stem for r in everything] == ["learnings", "profile"]
    assert [r.stem for r in memory.revisions("profile")] == ["profile"]


def test_a_revision_reports_its_size_and_line_count(temp_data_dir):
    _write("learnings", "- one\n- two\n- three\n")
    memory.snapshot("learnings", when=T0)
    revision = memory.revisions("learnings")[0]
    assert revision.lines == 3
    assert revision.size == len("- one\n- two\n- three\n")
    assert revision.ts.startswith("2026-07-22T04:00:00")


def test_reading_a_revision_refuses_anything_that_is_not_one_of_ours(temp_data_dir):
    _write("profile", "p\n")
    memory.snapshot("profile", when=T0)
    assert memory.read_revision("profile-20260722T040000Z.md") == "p\n"
    for junk in [
        "",
        "profile.md",
        "../portal.db",
        "profile-20260722T040000Z.md/../../portal.db",
        "portal-20260722T040000Z.md",
        "profile-nonsense.md",
    ]:
        assert memory.read_revision(junk) is None


def test_restoring_puts_the_old_text_back(temp_data_dir):
    _write("profile", "the text the owner typed\n")
    memory.snapshot("profile", when=T0)
    _write("profile", "what an agent replaced it with\n")
    assert memory.restore("profile-20260722T040000Z.md") is True
    assert config.PROFILE_MD.read_text() == "the text the owner typed\n"


def test_restoring_keeps_a_copy_of_what_it_replaced(temp_data_dir):
    # Restoring is itself a destructive write. Getting the restore wrong must
    # not be the thing that loses the current version.
    _write("profile", "old\n")
    memory.snapshot("profile", when=T0)
    _write("profile", "current\n")
    memory.restore("profile-20260722T040000Z.md")
    assert any(r.path.read_text() == "current\n" for r in memory.revisions("profile"))


def test_restoring_an_unknown_revision_changes_nothing(temp_data_dir):
    _write("profile", "current\n")
    assert memory.restore("profile-19990101T000000Z.md") is False
    assert config.PROFILE_MD.read_text() == "current\n"


def test_the_oldest_revisions_are_pruned(temp_data_dir):
    for i in range(memory.KEEP + 5):
        _write("profile", f"version {i}\n")
        memory.snapshot("profile", when=T0 + timedelta(minutes=i))
    kept = memory.revisions("profile")
    assert len(kept) == memory.KEEP
    # The ones that went are the oldest, and the newest is still there.
    assert kept[0].path.read_text() == f"version {memory.KEEP + 4}\n"


def test_snapshot_all_covers_both_files(temp_data_dir):
    _write("profile", "p\n")
    _write("learnings", "l\n")
    assert len(memory.snapshot_all(when=T0)) == 2


def test_revisions_survive_junk_in_the_directory(temp_data_dir):
    _write("profile", "p\n")
    memory.snapshot("profile", when=T0)
    (memory.history_dir() / "notes.txt").write_text("not mine")
    (memory.history_dir() / "subdir").mkdir()
    assert [r.stem for r in memory.revisions()] == ["profile"]


# --------------------------------------------------------------------------
# What a run may append
# --------------------------------------------------------------------------

def test_a_learning_is_written_without_a_timestamp(temp_data_dir):
    worker._append_learnings(["Ada is in Arkansas"])
    assert config.LEARNINGS_MD.read_text() == "- Ada is in Arkansas\n"


def test_a_run_may_only_add_a_few_learnings(temp_data_dir):
    worker._append_learnings([f"fact number {i}" for i in range(10)])
    lines = config.LEARNINGS_MD.read_text().splitlines()
    assert len(lines) == worker.MAX_LEARNINGS_PER_RUN
    # The first ones are kept: a model puts what it thinks matters most first.
    assert lines[0] == "- fact number 0"


def test_a_learning_already_in_the_file_is_not_added_again(temp_data_dir):
    _write("learnings", "- Ada prefers Telegram for notifications\n")
    worker._append_learnings(["Ada prefers Telegram for notifications."])
    assert config.LEARNINGS_MD.read_text().count("Telegram") == 1


def test_duplicates_within_one_report_collapse(temp_data_dir):
    worker._append_learnings(["He likes short folder names", "he likes SHORT folder names!"])
    assert len(config.LEARNINGS_MD.read_text().splitlines()) == 1


def test_a_list_marker_the_model_added_is_stripped(temp_data_dir):
    worker._append_learnings(["- already bulleted"])
    assert config.LEARNINGS_MD.read_text() == "- already bulleted\n"


def test_empty_and_non_string_learnings_are_skipped(temp_data_dir):
    worker._append_learnings(["", "   ", None, {"a": 1}, "a real one"])
    assert config.LEARNINGS_MD.read_text() == "- a real one\n"


def test_nothing_worth_writing_leaves_the_file_alone(temp_data_dir):
    _write("learnings", "- existing\n")
    worker._append_learnings(["existing"])
    assert config.LEARNINGS_MD.read_text() == "- existing\n"


def test_the_contract_tells_agents_to_be_sparing(temp_data_dir):
    contract = agent_runner.AGENT_CONTRACT
    assert '- "learnings": almost always an empty list.' in contract
    assert "every run of every project" in contract


def test_the_contract_documents_the_delete_op(temp_data_dir):
    contract = agent_runner.AGENT_CONTRACT
    assert '"op": "delete"' in contract
    assert "the stale fact" in contract


# --------------------------------------------------------------------------
# The ADD/UPDATE/DELETE/NOOP write gate
# --------------------------------------------------------------------------

def test_a_rephrasing_replaces_the_old_line_in_place(temp_data_dir):
    # The near-duplicate pile-up Wes complained about: a later run phrases the
    # same fact more fully. The refined wording should supersede the old one,
    # not sit beside it.
    _write("learnings", "- Bun is the default runtime\n")
    worker._append_learnings(["Bun is the default runtime on the box now"])
    lines = config.LEARNINGS_MD.read_text().splitlines()
    assert lines == ["- Bun is the default runtime on the box now"]


def test_an_update_leaves_the_surrounding_document_untouched(temp_data_dir):
    _write("learnings", "## Tooling\n\n- Bun is the default runtime\n- Ports away from 3000\n")
    worker._append_learnings(["Bun is the default runtime on the box now"])
    assert config.LEARNINGS_MD.read_text() == (
        "## Tooling\n\n- Bun is the default runtime on the box now\n- Ports away from 3000\n"
    )


def test_a_distinct_but_related_fact_is_not_merged(temp_data_dir):
    # Both lines share "port ... is ..." but are different facts. Merging them
    # would silently lose one - the failure the conservative threshold avoids.
    _write("learnings", "- Port 3000 is Homepage\n")
    worker._append_learnings(["Port 3002 is Uptime Kuma"])
    lines = config.LEARNINGS_MD.read_text().splitlines()
    assert lines == ["- Port 3000 is Homepage", "- Port 3002 is Uptime Kuma"]


def test_an_in_place_update_snapshots_first(temp_data_dir):
    _write("learnings", "- Bun is the default runtime\n")
    assert not memory.revisions("learnings")
    worker._append_learnings(["Bun is the default runtime on the box now"])
    revs = memory.revisions("learnings")
    assert revs and "Bun is the default runtime\n" in revs[0].path.read_text()


def test_a_pure_append_does_not_snapshot(temp_data_dir):
    _write("learnings", "- Port 3000 is Homepage\n")
    worker._append_learnings(["Port 3002 is Uptime Kuma"])
    # Nothing existing was rewritten, so there is nothing to protect a copy of.
    assert not memory.revisions("learnings")


def test_an_explicit_delete_removes_the_matching_line(temp_data_dir):
    _write("learnings", "- Port 25 outbound is blocked to MX hosts\n- Bun is the default\n")
    worker._append_learnings([{"op": "delete", "text": "Port 25 outbound is blocked to MX hosts"}])
    assert config.LEARNINGS_MD.read_text() == "- Bun is the default\n"


def test_a_delete_snapshots_before_removing(temp_data_dir):
    _write("learnings", "- Port 25 outbound is blocked\n- Bun is the default\n")
    worker._append_learnings([{"op": "delete", "text": "Port 25 outbound is blocked"}])
    revs = memory.revisions("learnings")
    assert revs and "Port 25 outbound is blocked" in revs[0].path.read_text()


def test_a_delete_with_no_close_match_changes_nothing(temp_data_dir):
    _write("learnings", "- Bun is the default runtime\n")
    worker._append_learnings([{"op": "delete", "text": "the moon is made of cheese"}])
    assert config.LEARNINGS_MD.read_text() == "- Bun is the default runtime\n"
    assert not memory.revisions("learnings")


def test_a_forced_add_never_overwrites_a_close_line(temp_data_dir):
    # op "add" is the escape hatch when a run genuinely wants a second, similar
    # line kept rather than collapsed into the first.
    _write("learnings", "- Bun is the default runtime\n")
    worker._append_learnings([{"op": "add", "text": "Bun is the default runtime on the box now"}])
    lines = config.LEARNINGS_MD.read_text().splitlines()
    assert len(lines) == 2


def test_an_explicit_update_with_no_match_appends(temp_data_dir):
    _write("learnings", "- Bun is the default runtime\n")
    worker._append_learnings([{"op": "update", "text": "The public IP is dynamic"}])
    lines = config.LEARNINGS_MD.read_text().splitlines()
    assert lines == ["- Bun is the default runtime", "- The public IP is dynamic"]


def test_an_update_preserves_the_bullets_original_marker(temp_data_dir):
    _write("learnings", "* Bun is the default runtime\n")
    worker._append_learnings(["Bun is the default runtime on the box now"])
    assert config.LEARNINGS_MD.read_text() == "* Bun is the default runtime on the box now\n"


def test_an_update_counts_toward_the_per_run_cap(temp_data_dir):
    _write("learnings", "- alpha fact one\n- beta fact two\n")
    # Two updates plus two adds; only MAX_LEARNINGS_PER_RUN net operations land.
    worker._append_learnings([
        "alpha fact one revised here",   # update
        "beta fact two revised here",    # update
        "gamma brand new thing",         # add
        "delta another new thing",       # add (over cap -> dropped)
    ])
    text = config.LEARNINGS_MD.read_text()
    assert "revised here" in text
    assert "delta another new thing" not in text


def test_a_dict_learning_with_a_bad_op_is_skipped(temp_data_dir):
    worker._append_learnings([{"op": "frobnicate", "text": "nope"}, "a real one"])
    assert config.LEARNINGS_MD.read_text() == "- a real one\n"


# --------------------------------------------------------------------------
# The superseded-learnings archive
# --------------------------------------------------------------------------

def test_the_archive_lives_outside_the_agents_working_directory(temp_data_dir):
    # MEMORY_DIR is the cwd of the reflect/compaction agents; a growing log of
    # retired facts there is exactly the noise they exist to remove.
    assert memory.archive_path().parent != config.MEMORY_DIR


def test_a_superseded_learning_is_archived_with_its_old_text(temp_data_dir):
    _write("learnings", "- Bun is the default runtime\n")
    worker._append_learnings(["Bun is the default runtime on the box now"])
    archived = memory.archived_learnings()
    assert len(archived) == 1
    assert archived[0].reason == "superseded"
    assert archived[0].text == "Bun is the default runtime"
    # And the live file has the new wording, not the old.
    assert config.LEARNINGS_MD.read_text() == "- Bun is the default runtime on the box now\n"


def test_a_retired_learning_is_archived_with_its_text(temp_data_dir):
    _write("learnings", "- Port 25 outbound is blocked\n- Bun is the default\n")
    worker._append_learnings([{"op": "delete", "text": "Port 25 outbound is blocked"}])
    archived = memory.archived_learnings()
    assert len(archived) == 1
    assert archived[0].reason == "retired"
    assert archived[0].text == "Port 25 outbound is blocked"


def test_a_pure_append_archives_nothing(temp_data_dir):
    # A new fact removes no existing line, so nothing is retired.
    _write("learnings", "- Port 3000 is Homepage\n")
    worker._append_learnings(["Port 3002 is Uptime Kuma"])
    assert memory.archived_learnings() == []


def test_a_delete_with_no_match_archives_nothing(temp_data_dir):
    _write("learnings", "- Bun is the default runtime\n")
    worker._append_learnings([{"op": "delete", "text": "the moon is made of cheese"}])
    assert memory.archived_learnings() == []


def test_the_archive_reads_newest_first(temp_data_dir):
    _write("learnings", "- alpha fact one\n- beta fact two\n")
    worker._append_learnings(["alpha fact one revised"])   # supersedes alpha
    worker._append_learnings(["beta fact two revised"])    # supersedes beta, later
    archived = memory.archived_learnings()
    assert [a.text for a in archived] == ["beta fact two", "alpha fact one"]


def test_archive_learning_stamps_the_given_day(temp_data_dir):
    when = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    memory.archive_learning("some retired fact", "retired", when=when)
    archived = memory.archived_learnings()
    assert archived[0].date == "2026-07-23"


def test_archive_learning_ignores_empty_text(temp_data_dir):
    memory.archive_learning("   ", "retired")
    assert memory.archived_learnings() == []


def test_archive_survives_a_reason_with_parens(temp_data_dir):
    # The line format uses parens to delimit the reason, so they must be
    # stripped from the reason itself or the round-trip would mis-parse.
    memory.archive_learning("a fact", "retired (per Ada)")
    archived = memory.archived_learnings()
    assert len(archived) == 1
    assert archived[0].text == "a fact"


def test_a_missing_archive_reads_as_empty(temp_data_dir):
    assert not memory.archive_path().exists()
    assert memory.archived_learnings() == []


def test_the_memory_page_shows_retired_learnings(temp_data_dir, client):
    _write("learnings", "- Bun is the default runtime\n")
    worker._append_learnings(["Bun is the default runtime on the box now"])
    body = client.get("/memory").text
    assert "Retired learnings" in body
    assert "Bun is the default runtime</div>" in body or "Bun is the default runtime<" in body


# --------------------------------------------------------------------------
# The compaction task
# --------------------------------------------------------------------------

def test_the_compact_prompt_says_what_to_keep_and_what_to_drop(temp_data_dir):
    _write("profile", "# Profile\n- Ada\n")
    prompt = agent_runner.build_prompt("compact", None)
    assert "Task: COMPACT THE LEARNINGS" in prompt
    assert "KEEP" in prompt and "DROP" in prompt
    assert "TIMESTAMPS" in prompt
    assert "Do not touch profile.md" in prompt


def test_the_compact_prompt_does_not_paste_the_file_it_is_rewriting(temp_data_dir):
    # It has the file in its cwd. A tail in the prompt as well would give it two
    # versions to edit, and the tail is the part it needs least.
    _write("learnings", "- a very distinctive line\n")
    prompt = agent_runner.build_prompt("compact", None)
    assert "a very distinctive line" not in prompt


def test_the_compact_prompt_carries_the_profile_as_context(temp_data_dir):
    _write("profile", "- Ada runs testhost\n")
    prompt = agent_runner.build_prompt("compact", None)
    assert "Ada runs testhost" in prompt
    assert "do not rewrite it" in prompt


def test_compaction_snapshots_the_file_before_the_agent_touches_it(temp_data_dir, monkeypatch):
    _write("learnings", "- the long messy version\n")
    seen = {}

    async def fake_run(*args, **kwargs):
        # Whatever the agent does next, the copy must already exist.
        seen["revisions"] = memory.revisions("learnings")
        raise RuntimeError("stop here")

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    with pytest.raises(RuntimeError):
        import asyncio

        asyncio.run(worker.run_compaction())
    assert seen["revisions"]
    assert seen["revisions"][0].path.read_text() == "- the long messy version\n"


def test_compaction_journals_the_before_and_after_size(temp_data_dir, monkeypatch):
    import asyncio

    _write("learnings", "- one\n- two\n- three\n- four\n")

    async def fake_run(*args, **kwargs):
        config.LEARNINGS_MD.write_text("- one distilled line\n", encoding="utf-8")
        return agent_runner.RunResult(ok=True, result_text="done", report=None)

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    asyncio.run(worker.run_compaction())
    entry = db.list_journal(project_id=None, limit=5)[0]["content_md"]
    assert "4 lines" in entry and "1 lines" in entry
    assert "revisions" in entry


def test_only_one_compaction_runs_at_a_time(temp_data_dir, monkeypatch):
    import asyncio

    async def main():
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run(*args, **kwargs):
            started.set()
            await release.wait()
            return agent_runner.RunResult(ok=True, result_text="done")

        monkeypatch.setattr(agent_runner, "run_claude", fake_run)
        assert worker.start_compaction() is True
        await started.wait()
        assert worker.compaction_running() is True
        assert worker.start_compaction() is False
        release.set()
        await worker._inflight[worker.COMPACT_SLOT]
        assert worker.compaction_running() is False

    asyncio.run(main())
    worker._inflight.pop(worker.COMPACT_SLOT, None)


def test_the_reflect_snapshots_the_profile_it_is_about_to_rewrite(temp_data_dir, monkeypatch):
    import asyncio

    _write("profile", "- the about-me text the owner typed\n")

    async def fake_run(*args, **kwargs):
        raise RuntimeError("stop here")

    monkeypatch.setattr(agent_runner, "run_claude", fake_run)
    with pytest.raises(RuntimeError):
        asyncio.run(worker.run_reflect())
    assert memory.revisions("profile")[0].path.read_text() == "- the about-me text the owner typed\n"


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

def test_the_memory_page_shows_the_size_and_the_compact_button(client, temp_data_dir):
    _write("learnings", "- one\n- two\n")
    body = client.get("/memory").text
    assert "compact this file" in body
    assert "2 lines" in body


def test_saving_a_file_snapshots_the_previous_version(client, temp_data_dir):
    _write("profile", "before\n")
    client.post("/memory/profile", data={"content": "after\n"})
    assert config.PROFILE_MD.read_text() == "after\n"
    assert memory.revisions("profile")[0].path.read_text() == "before\n"


def test_saving_learnings_snapshots_too(client, temp_data_dir):
    _write("learnings", "before\n")
    client.post("/memory/learnings", data={"content": "after\n"})
    assert memory.revisions("learnings")[0].path.read_text() == "before\n"


def test_the_revisions_section_lists_and_links_them(client, temp_data_dir):
    _write("profile", "old text\n")
    memory.snapshot("profile", when=T0)
    body = client.get("/memory").text
    assert "/memory/revision/profile-20260722T040000Z.md" in body
    assert "restore" in body


def test_a_revision_can_be_read(client, temp_data_dir):
    _write("profile", "the text the owner typed\n")
    memory.snapshot("profile", when=T0)
    body = client.get("/memory/revision/profile-20260722T040000Z.md").text
    assert "the text the owner typed" in body


def test_an_unknown_revision_is_a_404(client, temp_data_dir):
    assert client.get("/memory/revision/profile-19990101T000000Z.md").status_code == 404
    assert client.get("/memory/revision/portal.db").status_code == 404


def test_restoring_through_the_page(client, temp_data_dir):
    _write("profile", "old text\n")
    memory.snapshot("profile", when=T0)
    _write("profile", "replaced\n")
    response = client.post(
        "/memory/revision/profile-20260722T040000Z.md/restore", follow_redirects=False
    )
    assert response.status_code == 303
    assert config.PROFILE_MD.read_text() == "old text\n"


def test_restoring_something_that_is_not_a_revision_is_a_404(client, temp_data_dir):
    assert client.post("/memory/revision/portal.db/restore").status_code == 404


def test_pressing_compact_starts_a_run(client, temp_data_dir, monkeypatch):
    started = []
    monkeypatch.setattr(worker, "start_compaction", lambda: started.append(True) or True)
    client.post("/memory/compact")
    assert started == [True]


# --------------------------------------------------------------------------
# Suggestions you can actually read
# --------------------------------------------------------------------------
# Wes, 2026-07-28: "Allow the 'Suggestions' in the memory tab to be opened to
# view their full description. As of now, it gets cut off in the widget view
# with no way of seeing the rest of it."
#
# The two-line clamp itself is right - a grid whose cells are as tall as their
# longest description is not a grid - so what was missing was a way out of it,
# not the removal of it.

LONG_DESCRIPTION = (
    "A tool that watches the drum click track and the set list together, so a "
    "cue fired from the phone lands on the right bar of the right song even "
    "when the band has skipped a repeat, and the whole thing keeps working "
    "with no network in the building."
)


def test_a_long_suggestion_can_be_opened(client):
    db.add_suggestion("Click cue watcher", LONG_DESCRIPTION)
    html = client.get("/memory").text

    assert "cell-expand" in html, "no way to open it - this is the bug Wes reported"
    # The full text is in the page, not a server-truncated prefix: the clamp is
    # a CSS effect, so opening it must not need another request.
    assert LONG_DESCRIPTION in html
    # One copy, not two. A hidden duplicate is the same words twice in the DOM:
    # a find-in-page hit that goes nowhere, and read out twice by a screen
    # reader. The <summary> IS the description.
    assert html.count(LONG_DESCRIPTION) == 1


def test_a_short_suggestion_gets_no_pointless_toggle(client):
    """A "more" that reveals nothing is worse than no "more" at all."""
    db.add_suggestion("A tiny idea", "One line.")
    html = client.get("/memory").text
    assert "One line." in html
    assert "cell-expand" not in html


def test_the_dashboards_own_cells_are_left_alone(client):
    """`.cell-desc` is shared with the project grid, which wants the clamp.

    The expansion hangs off `.cell-expand` for this reason - a fix applied to
    the clamped class itself would have made every dashboard cell as tall as
    its description, which is not what was asked for and is a regression on
    the page Wes looks at most.
    """
    db.create_project(title="A project", description=LONG_DESCRIPTION)
    html = client.get("/").text
    assert "cell-desc" in html
    assert "cell-expand" not in html


# --------------------------------------------------------------------------
# What a compaction dropped
#
# The write gate archives a superseded or deleted learning, but the compaction
# rewrites the whole file without going through it, so nothing it cut was ever
# archived. The 2026-08-07 compaction took 171 bullets to 46 with not one
# surviving verbatim, and those 125 exist only in a revision that ages out of a
# 60-deep history - which is the loss these tests exist to stop repeating.
# --------------------------------------------------------------------------

_PKILL_OLD = (
    "`pkill -f <pattern>` matches the running shell's OWN command line, so a "
    "cleanup like `pkill -f \"demo.db\"` inside a command mentioning demo.db "
    "kills the agent's own run at exit code 144."
)
_PKILL_NEW = (
    "`pkill -f` / `pgrep -f` is the foot-gun that has bitten most often: the "
    "pattern matches the shell running it, including the agent's own `claude "
    "-p`, whose command line embeds the whole task prompt, so the run dies at "
    "rc 144. Kill by port owner instead."
)
_WAVESHARE = (
    "Waveshare IT8951 e-paper driver HATs take 5V on VCC but their SPI logic "
    "is 3.3V with no level translator, MISO is required, and HRDY is inverted."
)


def test_a_bullet_reworded_into_a_merged_line_is_not_treated_as_dropped():
    # A compaction rewrites every line it KEEPS as well as dropping the rest,
    # so a set difference would call this dropped. It is not: the fact is still
    # in the file, in sharper words.
    assert memory.dropped_bullets(f"- {_PKILL_OLD}\n", f"- {_PKILL_NEW}\n") == []


def test_a_bullet_with_no_trace_left_in_the_new_file_is_dropped():
    dropped = memory.dropped_bullets(f"- {_WAVESHARE}\n", f"- {_PKILL_NEW}\n")
    assert dropped == [_WAVESHARE]


def test_the_two_are_told_apart_in_one_pass():
    # The real shape of a compaction: some lines merged, some cut outright.
    before = f"- {_PKILL_OLD}\n- {_WAVESHARE}\n"
    assert memory.dropped_bullets(before, f"- {_PKILL_NEW}\n") == [_WAVESHARE]


def test_an_emptied_file_drops_everything_rather_than_nothing():
    # A compaction that wrote an empty file has dropped every fact, and the
    # archive is the whole point in exactly that case. A `max()` over no new
    # bullets must not read as "everything matched".
    before = f"- {_PKILL_OLD}\n- {_WAVESHARE}\n"
    assert memory.dropped_bullets(before, "# Learnings about Wes\n") == [
        _PKILL_OLD, _WAVESHARE,
    ]


def test_headings_and_prose_are_not_archived_as_learnings():
    # The `# ` title and the paragraph under it are boilerplate the compaction
    # is told to keep; only bullets state facts.
    before = "# Learnings about Wes\n\nDurable facts only, because every line costs context.\n"
    assert memory.dropped_bullets(before, "# Learnings about Wes\n") == []


def test_a_dropped_learning_reaches_the_archive_with_its_own_words(temp_data_dir):
    n = memory.archive_compaction_losses(f"- {_WAVESHARE}\n", f"- {_PKILL_NEW}\n")
    assert n == 1
    archived = memory.archived_learnings()
    assert len(archived) == 1
    assert archived[0].reason == "compacted"
    assert archived[0].text == _WAVESHARE


def test_a_second_compaction_does_not_stack_the_same_dropped_fact(temp_data_dir):
    # A compaction runs against a file the previous one already rewrote, so the
    # same loss is re-derivable every time and would pile up without this.
    memory.archive_compaction_losses(f"- {_WAVESHARE}\n", f"- {_PKILL_NEW}\n")
    again = memory.archive_compaction_losses(f"- {_WAVESHARE}\n", f"- {_PKILL_NEW}\n")
    assert again == 0
    assert len(memory.archived_learnings()) == 1


def test_dedup_survives_a_rewording_of_the_punctuation(temp_data_dir):
    memory.archive_compaction_losses(f"- {_WAVESHARE}\n", "- unrelated new line here\n")
    reworded = _WAVESHARE.replace(",", " -").rstrip(".") + "!"
    assert memory.archive_compaction_losses(
        f"- {reworded}\n", "- unrelated new line here\n"
    ) == 0


def test_archiving_a_loss_never_raises(temp_data_dir, monkeypatch):
    # The live file has already been rewritten by the time this runs, so a
    # failure here must not turn a good compaction into a failed run.
    monkeypatch.setattr(memory, "dropped_bullets", _boom)
    assert memory.archive_compaction_losses("- a\n", "- b\n") == 0


def _boom(*_a, **_k):
    raise RuntimeError("archive is on fire")


def test_a_compaction_that_kept_everything_archives_nothing(temp_data_dir):
    assert memory.archive_compaction_losses(f"- {_PKILL_OLD}\n", f"- {_PKILL_NEW}\n") == 0
    assert memory.archived_learnings() == []

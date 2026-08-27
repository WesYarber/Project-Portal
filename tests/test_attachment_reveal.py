"""An upload waits until a run is ready for it (attachments.reveal).

Wes, 2026-08-16:

  "Projects that are currently running when I attach a file to a note/prompt im
  currently working on (or maybe when I send it but the other run is still not
  done) seem to see the file get attached and ask a question about it. Maybe it
  could wait to be revealed to the agent that the file was added until it is
  dealing with that prompt? Or something like that? Just want to solve this
  where agents dont keep asking questions about the attached files before they
  receive the prompts where I actually explained it along side the upload."

The leak was the workspace itself. `<workspace>/attachments/` is a directory in
the agent's own cwd, so an upload landed under a run whose prompt had been built
minutes earlier - before the note explaining the file existed. The agent found
an unexplained screenshot beside its work and did the reasonable thing.

So a file is now staged outside every workspace and moved in by the run whose
prompt carries its note. Two properties, and both are tested here because either
one alone is the old bug: nothing appears in the workspace before a prompt build
(`test_a_run_in_flight_never_sees_the_file`), and everything is there by the
time the prompt names it (`test_the_run_that_is_told_gets_the_note_too`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import agent_runner, attachments, config, db

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def project():
    return db.create_project(
        "Shot Project", stage="active", build_approved=True, slug="shot-project"
    )


def upload(project, name="shot.png", data=PNG, mime="image/png", note=""):
    return attachments.store(project["id"], project["slug"], name, data, mime, note=note)


def workspace(project):
    return config.PROJECTS_DIR / project["slug"] / "attachments"


def staging(project):
    return config.INCOMING_DIR / project["slug"]


# --- the bug itself ---------------------------------------------------------


def test_a_run_in_flight_never_sees_the_file(project):
    """The whole report, as one assertion: an agent working right now has this
    project's workspace as its cwd, and nothing about the upload is in it."""
    row = upload(project, note="look at this")

    assert not (workspace(project) / row["stored_name"]).exists()
    assert not workspace(project).exists()


def test_the_staging_area_is_outside_every_workspace(project):
    """Stated as a property rather than as a path, because "outside" is the
    entire point and a later refactor that moved INCOMING_DIR under
    PROJECTS_DIR would reintroduce the bug while every other test passed."""
    upload(project)

    assert config.PROJECTS_DIR not in config.INCOMING_DIR.parents
    assert config.INCOMING_DIR.resolve() != config.PROJECTS_DIR.resolve()
    assert staging(project).exists()


def test_the_prompt_of_a_run_that_started_first_does_not_name_it(project):
    """A prompt built before the upload, which is what a run in flight has."""
    before = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "## Attachments" not in before

    upload(project, note="the broken layout")

    # And the file still is not in the workspace, so the run holding `before`
    # cannot stumble on it either.
    assert not (workspace(project) / "0001-shot.png").exists()


def test_the_run_that_is_told_gets_the_note_too(project):
    """The other half. A file held back forever is not a fix - the run whose
    prompt carries the note must find the file exactly where it is told."""
    row = upload(project, note="the broken layout")

    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))

    assert "## Attachments" in prompt
    assert f"`attachments/{row['stored_name']}`" in prompt
    assert "the broken layout" in prompt
    # Told where it is, and it is there.
    assert (workspace(project) / row["stored_name"]).read_bytes() == PNG
    assert not (staging(project) / row["stored_name"]).exists()


def test_a_staged_file_is_never_named_in_a_prompt(project):
    """`prompt_section` filters on revealed rather than trusting the caller to
    have revealed first: a path naming a staged file would send the agent to
    read something that is not in its workspace, which is a worse failure than
    the one this is all about."""
    upload(project)

    assert attachments.prompt_section(project["id"]) == ""


# --- what reveal does -------------------------------------------------------


def test_reveal_moves_the_bytes_and_returns_the_names(project):
    row = upload(project)

    assert attachments.reveal(project["id"], project["slug"]) == [row["stored_name"]]
    assert (workspace(project) / row["stored_name"]).read_bytes() == PNG


def test_revealing_twice_moves_nothing_the_second_time(project):
    upload(project)
    attachments.reveal(project["id"], project["slug"])

    assert attachments.reveal(project["id"], project["slug"]) == []


def test_the_stamp_is_when_the_agent_first_saw_it(project, monkeypatch):
    """A second pass must not push the timestamp forward - it records the first
    reveal, and rewriting it would erase the only record of when that was.

    The clock is driven rather than read: `db.now()` has second resolution, so
    two reveals in the same test tick produce the same string and this would
    pass with the `revealed_at IS NULL` guard deleted."""
    row = upload(project)
    monkeypatch.setattr(db, "now", lambda: "2026-08-16T09:00:00+00:00")
    attachments.reveal(project["id"], project["slug"])

    monkeypatch.setattr(db, "now", lambda: "2026-08-16T17:30:00+00:00")
    attachments.reveal(project["id"], project["slug"])

    assert db.get_attachment(row["id"])["revealed_at"] == "2026-08-16T09:00:00+00:00"


def test_stamping_an_already_stamped_row_leaves_it_alone(project, monkeypatch):
    """`db.mark_attachment_revealed` guards on `revealed_at IS NULL` as a second
    lock on top of the worklist filter, and this is the only place the guard can
    be seen: `reveal` never offers it a revealed row, because the worklist has
    already dropped one. Tested at the db level rather than deleted as
    redundant - the two locks fail in different directions, and a caller that
    ever reaches for this function directly should not be able to erase when
    the agent first saw a file."""
    row = upload(project)
    monkeypatch.setattr(db, "now", lambda: "2026-08-16T09:00:00+00:00")
    db.mark_attachment_revealed(row["id"])

    monkeypatch.setattr(db, "now", lambda: "2026-08-16T17:30:00+00:00")
    db.mark_attachment_revealed(row["id"])

    assert db.get_attachment(row["id"])["revealed_at"] == "2026-08-16T09:00:00+00:00"


def test_the_worklist_is_only_what_has_not_been_revealed(project):
    """`db.unrevealed_attachments` is the list `reveal` walks. Asserted here
    directly because every effect of the filter downstream is a no-op that a
    deleted filter reproduces exactly."""
    first = upload(project, name="one.png")
    attachments.reveal(project["id"], project["slug"])
    second = upload(project, name="two.png")

    waiting = db.unrevealed_attachments(project["id"])

    assert [r["id"] for r in waiting] == [second["id"]]
    assert first["id"] not in [r["id"] for r in waiting]


def test_files_are_revealed_oldest_first(project):
    a = upload(project, name="one.png")
    b = upload(project, name="two.png")

    assert attachments.reveal(project["id"], project["slug"]) == [
        a["stored_name"],
        b["stored_name"],
    ]


def test_one_projects_reveal_does_not_touch_anothers(project):
    """`reveal` takes a project id and a slug, and a bug that ignored the id
    would drain every project's staging into whichever workspace ran first."""
    other = db.create_project("Other", stage="active", slug="other")
    mine = upload(project)
    theirs = attachments.store(other["id"], other["slug"], "theirs.png", PNG, "image/png")

    attachments.reveal(project["id"], project["slug"])

    assert (workspace(project) / mine["stored_name"]).exists()
    assert (config.INCOMING_DIR / "other" / theirs["stored_name"]).exists()
    assert not (config.PROJECTS_DIR / "other" / "attachments").exists()
    # The damage a scoping bug actually does, and it is silent: the other
    # project's row is picked up, found to have no file in THIS project's
    # staging, and stamped as revealed. Nothing moves and nothing errors - but
    # that file is now marked as seen and will never be shown to the agent it
    # was uploaded for. Asserting on the file locations alone misses it.
    assert db.get_attachment(theirs["id"])["revealed_at"] is None


# --- the states reveal has to tolerate --------------------------------------


def test_a_file_already_in_the_workspace_is_stamped_rather_than_moved(project):
    """Every row that predates `revealed_at` is in this state, as is anything
    restored from a backup. This tolerance IS the migration - there is no
    backfill step anywhere else."""
    row = upload(project)
    workspace(project).mkdir(parents=True, exist_ok=True)
    (staging(project) / row["stored_name"]).replace(workspace(project) / row["stored_name"])

    assert attachments.reveal(project["id"], project["slug"]) == []
    assert db.get_attachment(row["id"])["revealed_at"]
    assert (workspace(project) / row["stored_name"]).read_bytes() == PNG


def test_a_row_whose_file_has_vanished_is_not_retried_forever(project):
    row = upload(project)
    (staging(project) / row["stored_name"]).unlink()

    attachments.reveal(project["id"], project["slug"])

    assert db.get_attachment(row["id"])["revealed_at"]


def test_a_name_already_taken_in_the_workspace_is_not_overwritten(project):
    """Cannot happen from `store` - the row id is in the filename - so it means
    something else put a file there, and clobbering it would destroy whatever
    that was. The upload stays staged and is retried next run."""
    row = upload(project)
    workspace(project).mkdir(parents=True, exist_ok=True)
    (workspace(project) / row["stored_name"]).write_bytes(b"someone else's work")

    assert attachments.reveal(project["id"], project["slug"]) == []
    assert (workspace(project) / row["stored_name"]).read_bytes() == b"someone else's work"
    assert (staging(project) / row["stored_name"]).read_bytes() == PNG
    assert db.get_attachment(row["id"])["revealed_at"] is None


def test_a_reveal_that_cannot_move_a_file_does_not_take_the_run_with_it(project, monkeypatch):
    """A prompt build that raised because a screenshot could not be moved would
    kill the run. The file stays staged, still served to Wes, still on the list
    for next time."""
    row = upload(project)

    def refuse(self, target):
        raise OSError("disk is having a day")

    monkeypatch.setattr(Path, "replace", refuse)

    assert attachments.reveal(project["id"], project["slug"]) == []
    assert db.get_attachment(row["id"])["revealed_at"] is None
    assert attachments.disk_path(project["slug"], row["stored_name"]) is not None


# --- Wes can still see his own upload immediately ---------------------------


def test_he_can_open_a_file_he_has_just_uploaded(project):
    """Only the agent is made to wait. His own upload is his to look at, so
    `disk_path` resolves a staged file exactly like a placed one - which is
    what keeps the download route and the journal preview working."""
    row = upload(project)

    found = attachments.disk_path(project["slug"], row["stored_name"])

    assert found is not None and found.read_bytes() == PNG


def test_disk_path_follows_the_file_across_the_move(project):
    row = upload(project)
    staged = attachments.disk_path(project["slug"], row["stored_name"])
    attachments.reveal(project["id"], project["slug"])
    placed = attachments.disk_path(project["slug"], row["stored_name"])

    assert staged.parent != placed.parent
    assert placed.read_bytes() == PNG


def test_the_journal_and_the_project_page_still_list_a_staged_file(temp_data_dir, project):
    """The note he just wrote shows its own attachment back to him. Holding the
    file from the agent must not hide it from the person who uploaded it."""
    from starlette.testclient import TestClient
    from app import main

    client = TestClient(main.app)
    client.post(
        f"/project/{project['slug']}/note",
        data={"note": "look at this", "then": "queue"},
        files={"files": ("shot.png", PNG, "image/png")},
    )

    body = client.get(f"/project/{project['slug']}").text

    assert "shot.png" in body
    assert db.list_attachments(project["id"])[0]["revealed_at"] is None


def test_deleting_a_staged_file_removes_it(project):
    row = upload(project)

    attachments.remove_file(project["slug"], row["stored_name"])

    assert attachments.disk_path(project["slug"], row["stored_name"]) is None


# --- a voice memo's transcript travels with it ------------------------------


def test_the_transcript_sidecar_moves_with_the_audio(project):
    """A stray .txt appearing alone in a running agent's workspace is the same
    surprise as the audio appearing alone, and it is the half with the words."""
    row = upload(project, name="memo.webm", data=b"not-really-audio", mime="audio/webm")
    sidecar = staging(project) / attachments.sidecar_name(row["stored_name"])
    sidecar.write_text("Center the buttons.\n", encoding="utf-8")

    attachments.reveal(project["id"], project["slug"])

    assert not sidecar.exists()
    moved = workspace(project) / attachments.sidecar_name(row["stored_name"])
    assert moved.read_text() == "Center the buttons.\n"


def test_the_audio_is_revealed_even_if_its_transcript_will_not_move(project, monkeypatch):
    """Best-effort, and after the audio has landed: a sidecar that fails to move
    is a transcript the agent reads out of the prompt instead, not a reason to
    hold the memo back."""
    row = upload(project, name="memo.webm", data=b"not-really-audio", mime="audio/webm")
    (staging(project) / attachments.sidecar_name(row["stored_name"])).write_text("x")

    real = Path.replace
    calls = {"n": 0}

    def flaky(self, target):
        calls["n"] += 1
        if self.name.endswith(".txt"):
            raise OSError("nope")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)

    assert attachments.reveal(project["id"], project["slug"]) == [row["stored_name"]]
    assert (workspace(project) / row["stored_name"]).exists()

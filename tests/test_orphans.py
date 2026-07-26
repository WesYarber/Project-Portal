"""Work a dead run left uncommitted: finding it, journalling it, and putting it
in front of the next agent."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import agent_runner, config, db, orphans, worker


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path) -> Path:
    """A real git repo with one commit. Real rather than mocked, because every
    bug this module has is in what git actually prints."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    (r / "README.md").write_text("hello\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "first commit")
    return r


# --- scan() -----------------------------------------------------------------


def test_a_clean_repo_reports_nothing(repo):
    assert orphans.scan(repo) is None


def test_a_modified_tracked_file_is_found(repo):
    (repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    work = orphans.scan(repo)
    assert work is not None
    assert work.files == ["README.md"]
    assert work.total_files == 1
    assert work.insertions == 1
    assert work.deletions == 0


def test_an_untracked_file_is_found(repo):
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    work = orphans.scan(repo)
    assert work is not None
    assert work.files == ["feature.py"]


def test_untracked_files_inside_a_new_directory_are_listed_individually(repo):
    """`git status` collapses a new directory to `newdir/` by default, which
    would report a ten-file feature as one entry. --untracked-files=all is what
    makes the count mean what the journal says it means."""
    (repo / "app").mkdir()
    (repo / "app" / "a.py").write_text("a", encoding="utf-8")
    (repo / "app" / "b.py").write_text("b", encoding="utf-8")
    work = orphans.scan(repo)
    assert work is not None
    assert work.files == ["app/a.py", "app/b.py"]
    assert work.total_files == 2


def test_staged_changes_count_too(repo):
    (repo / "staged.py").write_text("y = 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    work = orphans.scan(repo)
    assert work is not None
    assert work.files == ["staged.py"]
    assert work.insertions == 1


def test_staged_and_unstaged_edits_to_one_file_are_counted_once(repo):
    """The file appears in both `git diff` and `git diff --cached`, and in a
    single porcelain line. It must not be double-counted in the file list."""
    (repo / "README.md").write_text("hello\nstaged\n", encoding="utf-8")
    git(repo, "add", "-A")
    (repo / "README.md").write_text("hello\nstaged\nunstaged\n", encoding="utf-8")
    work = orphans.scan(repo)
    assert work is not None
    assert work.total_files == 1


def test_a_rename_reports_the_new_path(repo):
    git(repo, "mv", "README.md", "READTHIS.md")
    work = orphans.scan(repo)
    assert work is not None
    assert work.files == ["READTHIS.md"]


def test_a_deleted_file_is_found(repo):
    (repo / "README.md").unlink()
    work = orphans.scan(repo)
    assert work is not None
    assert work.files == ["README.md"]
    assert work.deletions == 1


def test_a_binary_file_does_not_break_the_diffstat(repo):
    """git prints `-` instead of a line count for a binary file; int() on that
    would raise and take the whole scan down."""
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
    git(repo, "add", "-A")
    work = orphans.scan(repo)
    assert work is not None
    assert work.files == ["blob.bin"]
    assert work.insertions == 0


def test_the_branch_and_last_commit_are_recorded(repo):
    (repo / "x.py").write_text("x", encoding="utf-8")
    work = orphans.scan(repo)
    assert work is not None
    assert work.branch == "main"
    assert "first commit" in work.last_commit


def test_a_missing_directory_reports_nothing(tmp_path):
    assert orphans.scan(tmp_path / "nope") is None


def test_a_directory_that_is_not_a_repo_reports_nothing(tmp_path):
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "file.txt").write_text("x", encoding="utf-8")
    assert orphans.scan(tmp_path / "plain") is None


def test_none_reports_nothing():
    assert orphans.scan(None) is None


def test_a_git_that_fails_reports_nothing_rather_than_raising(repo, monkeypatch):
    """A broken git must read as "nothing to say", not as a crash on the very
    path that runs when something has already gone wrong."""
    def boom(*a, **kw):
        raise OSError("git is gone")

    monkeypatch.setattr(orphans.subprocess, "run", boom)
    assert orphans.scan(repo) is None


def test_a_git_that_times_out_reports_nothing(repo, monkeypatch):
    def slow(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=15)

    monkeypatch.setattr(orphans.subprocess, "run", slow)
    assert orphans.scan(repo) is None


# --- the portal's own bookkeeping -------------------------------------------


def test_an_untracked_report_json_is_not_orphaned_work(repo):
    """The portal writes this into every workspace on every run. Counting it
    would fire the warning on almost every project, on every prompt."""
    (repo / ".portal").mkdir()
    (repo / ".portal" / "report.json").write_text("{}", encoding="utf-8")
    assert orphans.scan(repo) is None


def test_a_deleted_committed_report_json_is_not_orphaned_work(repo):
    """The shape actually found on 13 of 17 live projects: report.json was
    committed once, and every run deletes it at startup, so it is permanently
    dirty as a *deletion* rather than as an untracked file."""
    (repo / ".portal").mkdir()
    (repo / ".portal" / "report.json").write_text("{}", encoding="utf-8")
    git(repo, "add", "-Af")
    git(repo, "commit", "-qm", "commit the report")
    (repo / ".portal" / "report.json").unlink()
    assert orphans.scan(repo) is None


def test_report_json_does_not_hide_real_work_beside_it(repo):
    """Excluding the portal's file must not excuse the whole run."""
    (repo / ".portal").mkdir()
    (repo / ".portal" / "report.json").write_text("{}", encoding="utf-8")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    work = orphans.scan(repo)
    assert work is not None
    assert work.files == ["feature.py"]


def test_report_json_lines_are_left_out_of_the_diffstat(repo):
    """The file list and the line counts have to agree; a +200 from a report
    the reader cannot see in the list reads as a bug."""
    (repo / ".portal").mkdir()
    (repo / ".portal" / "report.json").write_text("\n".join(["x"] * 50), encoding="utf-8")
    git(repo, "add", "-Af")
    git(repo, "commit", "-qm", "commit the report")
    (repo / ".portal" / "report.json").write_text("\n".join(["y"] * 50), encoding="utf-8")
    (repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    work = orphans.scan(repo)
    assert work is not None
    assert work.files == ["README.md"]
    assert work.insertions == 1


# --- a repo with no commits -------------------------------------------------


def test_a_repo_with_no_commits_still_reports_its_files(tmp_path):
    """Four live workspaces are `git init`ed with nothing committed. HEAD does
    not resolve there, and that must not lose the file list."""
    r = tmp_path / "fresh"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "PLAN.md").write_text("plan\n", encoding="utf-8")
    work = orphans.scan(r)
    assert work is not None
    assert work.files == ["PLAN.md"]
    assert work.last_commit == ""
    assert work.branch == "main"
    assert "Last commit" not in work.describe()


# --- the file cap -----------------------------------------------------------


def test_a_huge_dirty_tree_names_a_capped_number_and_counts_the_rest(repo):
    for i in range(40):
        (repo / f"f{i:02d}.py").write_text("x", encoding="utf-8")
    work = orphans.scan(repo)
    assert work is not None
    assert work.total_files == 40
    assert len(work.named) == orphans.MAX_NAMED_FILES
    assert work.unnamed == 40 - orphans.MAX_NAMED_FILES
    assert f"and {work.unnamed} more" in work.describe()


def test_a_small_dirty_tree_names_everything_and_claims_no_remainder(repo):
    (repo / "a.py").write_text("x", encoding="utf-8")
    (repo / "b.py").write_text("x", encoding="utf-8")
    work = orphans.scan(repo)
    assert work is not None
    assert work.unnamed == 0
    assert "more" not in work.describe()


def test_describe_names_the_files_and_the_repo(repo):
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    text = orphans.scan(repo).describe()
    assert "`feature.py`" in text
    assert str(repo) in text
    assert "1 uncommitted file" in text
    assert "files" not in text.split(" in ")[0]  # singular, not "1 uncommitted files"


# --- repo_for() -------------------------------------------------------------


def test_an_ordinary_project_uses_its_workspace(temp_data_dir):
    ws = config.PROJECTS_DIR / "some-project"
    (ws / ".git").mkdir(parents=True)
    assert orphans.repo_for("some-project") == ws


def test_a_workspace_without_git_is_not_a_repo(temp_data_dir):
    (config.PROJECTS_DIR / "plain").mkdir(parents=True)
    assert orphans.repo_for("plain") is None


def test_the_meta_project_uses_the_portal_source_not_its_workspace(temp_data_dir):
    """The portal's own workspace is a near-empty folder; the code that a dead
    run leaves dirty is at APP_ROOT. Getting this wrong would mean the one
    project this has actually happened to is the one project never checked."""
    assert orphans.repo_for(config.META_PROJECT_SLUG) == config.APP_ROOT


# --- journal_note() ---------------------------------------------------------


def test_journal_note_is_none_on_a_clean_repo(temp_data_dir, monkeypatch):
    monkeypatch.setattr(orphans, "repo_for", lambda slug: None)
    assert orphans.journal_note("x", "build", "errored") is None


def test_journal_note_names_the_files_and_says_nothing_is_lost(repo, monkeypatch):
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: repo)
    note = orphans.journal_note("x", "build", "errored")
    assert note is not None
    assert "errored" in note
    assert "Nothing has been lost" in note
    assert "`feature.py`" in note


# --- prompt_section() -------------------------------------------------------


def test_prompt_section_is_empty_on_a_clean_repo(repo, monkeypatch):
    monkeypatch.setattr(orphans, "repo_for", lambda slug: repo)
    assert orphans.prompt_section("x") == ""


def test_prompt_section_tells_the_agent_to_read_before_building(repo, monkeypatch):
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: repo)
    text = orphans.prompt_section("x")
    assert "Read it before you build anything" in text
    assert "git diff" in text
    assert "`feature.py`" in text
    # The whole point: do not start the same feature again.
    assert "starting the same feature again" in text


# --- through build_prompt() -------------------------------------------------


def test_a_clean_project_prompt_carries_no_orphan_block(temp_data_dir, monkeypatch):
    project = db.create_project("Clean", "nothing uncommitted")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: None)
    prompt = agent_runner.build_prompt("build", project)
    assert "Uncommitted work already in the repo" not in prompt


def test_a_dirty_project_prompt_carries_the_orphan_block(temp_data_dir, repo, monkeypatch):
    project = db.create_project("Dirty", "a dead run left something")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: repo)
    prompt = agent_runner.build_prompt("build", project)
    assert "Uncommitted work already in the repo" in prompt
    assert "`feature.py`" in prompt


def test_the_orphan_block_sits_above_the_journal(temp_data_dir, repo, monkeypatch):
    """An agent that reads only the top of a long prompt must still see it."""
    project = db.create_project("Dirty", "x")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: repo)
    prompt = agent_runner.build_prompt("build", project)
    assert prompt.index("Uncommitted work already in the repo") < prompt.index("## Recent journal")


def test_a_raising_scan_never_stops_a_prompt_being_built(temp_data_dir, monkeypatch):
    project = db.create_project("Boom", "x")

    def boom(slug):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(orphans, "prompt_section", boom)
    prompt = agent_runner.build_prompt("build", project)
    assert "Task: BUILD" in prompt


# --- through the worker -----------------------------------------------------


def test_a_failed_run_journals_the_orphaned_work(temp_data_dir, repo, monkeypatch):
    project = db.create_project("Dirty", "x")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: repo)

    worker._note_orphaned_work(project, "build", "errored")

    entries = db.list_journal(project["id"], limit=5)
    assert any("feature.py" in (e["content_md"] or "") for e in entries)


def test_a_failed_run_on_a_clean_repo_journals_nothing_extra(temp_data_dir, monkeypatch):
    project = db.create_project("Clean", "x")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: None)

    before = len(db.list_journal(project["id"], limit=50))
    worker._note_orphaned_work(project, "build", "errored")
    assert len(db.list_journal(project["id"], limit=50)) == before


def test_a_raising_scan_never_breaks_the_failure_path(temp_data_dir, monkeypatch):
    """This runs when a run has already failed. If it raised, the failure would
    go unrecorded - strictly worse than the bug it is diagnosing."""
    project = db.create_project("Boom", "x")

    def boom(*a, **kw):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(orphans, "journal_note", boom)
    worker._note_orphaned_work(project, "build", "errored")  # must not raise

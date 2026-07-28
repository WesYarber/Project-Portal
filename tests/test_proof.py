"""The proof-shot obligation: a run that changes the look of a project without
showing a screenshot gets a visible nudge (proof.py, RESEARCH.md §3)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import agent_runner, db, proof, worker


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path) -> Path:
    """A real git repo with one commit - real, because every edge here is in
    what git actually prints for a range diff."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    (r / "README.md").write_text("hello\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "first")
    return r


def commit(repo: Path, path: str, body: str = "x") -> None:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"add {path}")


# --- head_sha ---------------------------------------------------------------


def test_head_sha_returns_the_current_commit(repo):
    sha = proof.head_sha(repo)
    assert sha and len(sha) == 40


def test_head_sha_is_none_without_a_repo(tmp_path):
    assert proof.head_sha(tmp_path / "nope") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert proof.head_sha(plain) is None


def test_head_sha_is_none_before_the_first_commit(tmp_path):
    r = tmp_path / "fresh"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    assert proof.head_sha(r) is None


# --- changed_ui_files -------------------------------------------------------


def test_a_committed_css_change_is_seen(repo):
    before = proof.head_sha(repo)
    commit(repo, "app/static/style.css", "body{color:red}")
    assert proof.changed_ui_files(repo, before) == ["app/static/style.css"]


def test_a_committed_html_and_js_change_are_both_seen(repo):
    before = proof.head_sha(repo)
    commit(repo, "index.html", "<h1>hi</h1>")
    commit(repo, "app.js", "console.log(1)")
    assert proof.changed_ui_files(repo, before) == ["app.js", "index.html"]


def test_a_backend_only_change_is_not_ui(repo):
    before = proof.head_sha(repo)
    commit(repo, "app/worker.py", "x = 1")
    commit(repo, "docs/notes.md", "# notes")
    assert proof.changed_ui_files(repo, before) == []


def test_uncommitted_ui_work_is_not_reported(repo):
    """A run that left its edits uncommitted moved no HEAD - orphans.py handles
    that case, not this one, so the range diff is empty."""
    before = proof.head_sha(repo)
    (repo / "style.css").write_text("body{}", encoding="utf-8")
    assert proof.changed_ui_files(repo, before) == []


def test_no_baseline_means_nothing_reported(repo):
    commit(repo, "style.css", "body{}")
    assert proof.changed_ui_files(repo, None) == []


def test_portal_bookkeeping_is_excluded(repo):
    """.portal/ is portal-written, not the agent's UI - and report.json is not a
    front-end file anyway, but the exclude is what keeps the diff honest."""
    before = proof.head_sha(repo)
    commit(repo, ".portal/report.json", "{}")
    commit(repo, "style.css", "body{}")
    assert proof.changed_ui_files(repo, before) == ["style.css"]


# --- report_has_proof -------------------------------------------------------


def test_a_journal_image_counts_as_proof():
    report = {"journal_entry_md": "Fixed it.\n\n![the layout](shots/x.png)\n"}
    assert proof.report_has_proof(report) is True


def test_a_journal_video_counts_as_proof():
    assert proof.report_has_proof({"journal_entry_md": "![demo](demo.mp4)"}) is True


def test_an_image_in_a_summary_bullet_counts():
    report = {"summary": ["shipped the widget ![](shots/w.png)"], "journal_entry_md": "text only"}
    assert proof.report_has_proof(report) is True


def test_prose_alone_is_not_proof():
    report = {"journal_entry_md": "The layout is fixed now. It looks great."}
    assert proof.report_has_proof(report) is False


def test_a_preview_url_is_not_proof():
    """A live link is where to look, not a picture of what changed - and it
    persists across runs, so counting it would silence the nag forever."""
    report = {"journal_entry_md": "done", "preview_url": "http://testhost:8500"}
    assert proof.report_has_proof(report) is False


def test_an_empty_or_missing_report_has_no_proof():
    assert proof.report_has_proof(None) is False
    assert proof.report_has_proof({}) is False


def test_a_plain_link_is_not_a_media_embed():
    """`[text](url)` navigates; only `![alt](url)` shows something inline."""
    report = {"journal_entry_md": "see [the page](http://testhost:8500/x)"}
    assert proof.report_has_proof(report) is False


# --- missing_proof_note -----------------------------------------------------


def test_the_note_names_the_changed_files():
    note = proof.missing_proof_note(["style.css", "index.html"])
    assert "no screenshot" in note
    assert "`style.css`" in note and "`index.html`" in note
    assert "2 front-end files" in note


def test_the_note_summarizes_the_overflow():
    note = proof.missing_proof_note([f"f{i}.css" for i in range(12)])
    assert "...and 4 more" in note  # MAX_NAMED_FILES is 8


def test_one_file_is_singular():
    assert "1 front-end file " in proof.missing_proof_note(["a.css"])


# --- worker wiring ----------------------------------------------------------


def _result(report):
    return agent_runner.RunResult(ok=True, report=report)


def test_worker_notes_a_ui_run_without_proof(repo, monkeypatch, tmp_path):
    project = {"id": 7, "slug": "demo"}
    monkeypatch.setattr(worker.orphans, "repo_for", lambda slug: repo)
    entries = []
    monkeypatch.setattr(worker.db, "add_journal", lambda pid, a, k, t: entries.append((pid, t)))

    before = proof.head_sha(repo)
    commit(repo, "style.css", "body{}")
    worker._note_missing_proof(project, _result({"journal_entry_md": "shipped a restyle"}), "build", before)

    assert len(entries) == 1
    pid, text = entries[0]
    assert pid == 7
    assert "no screenshot" in text and "`style.css`" in text


def test_worker_is_silent_when_proof_is_present(repo, monkeypatch):
    project = {"id": 7, "slug": "demo"}
    monkeypatch.setattr(worker.orphans, "repo_for", lambda slug: repo)
    entries = []
    monkeypatch.setattr(worker.db, "add_journal", lambda *a: entries.append(a))

    before = proof.head_sha(repo)
    commit(repo, "style.css", "body{}")
    worker._note_missing_proof(
        project, _result({"journal_entry_md": "![it](shots/s.png)"}), "build", before
    )
    assert entries == []


def test_worker_is_silent_on_a_backend_only_run(repo, monkeypatch):
    project = {"id": 7, "slug": "demo"}
    monkeypatch.setattr(worker.orphans, "repo_for", lambda slug: repo)
    entries = []
    monkeypatch.setattr(worker.db, "add_journal", lambda *a: entries.append(a))

    before = proof.head_sha(repo)
    commit(repo, "app/worker.py", "x = 1")
    worker._note_missing_proof(project, _result({"journal_entry_md": "refactor"}), "build", before)
    assert entries == []


def test_worker_exempts_research_bursts(repo, monkeypatch):
    project = {"id": 7, "slug": "demo"}
    monkeypatch.setattr(worker.orphans, "repo_for", lambda slug: repo)
    entries = []
    monkeypatch.setattr(worker.db, "add_journal", lambda *a: entries.append(a))

    before = proof.head_sha(repo)
    commit(repo, "style.css", "body{}")
    worker._note_missing_proof(project, _result({"journal_entry_md": "read"}), "research", before)
    assert entries == []


def test_worker_ignores_a_reportless_run(repo, monkeypatch):
    project = {"id": 7, "slug": "demo"}
    monkeypatch.setattr(worker.orphans, "repo_for", lambda slug: repo)
    entries = []
    monkeypatch.setattr(worker.db, "add_journal", lambda *a: entries.append(a))

    before = proof.head_sha(repo)
    commit(repo, "style.css", "body{}")
    worker._note_missing_proof(project, agent_runner.RunResult(ok=True, report=None), "build", before)
    assert entries == []


# --- which skill the nudge points at ----------------------------------------
#
# The nudge used to hard-code one particular personal skill by name. It now
# discovers a render-capable skill from the skills directories, so the wording
# is right on any installation - including one that has no such skill at all.

def _skill(root: Path, name: str, description: str) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n",
        encoding="utf-8",
    )


def test_the_nudge_names_a_render_capable_skill(tmp_path, monkeypatch):
    shipped, promoted = tmp_path / "shipped", tmp_path / "promoted"
    shipped.mkdir()
    promoted.mkdir()
    _skill(shipped, "some-desktop", "Render a web page and look at the screenshot.")
    _skill(shipped, "note-taking", "Keep notes tidy.")
    monkeypatch.setattr(proof.config, "SKILLS_DIR", shipped)
    monkeypatch.setattr(proof.memory, "promoted_skills_dir", lambda: promoted)

    hint = proof.render_skill_hint()
    assert "**some-desktop**" in hint
    # A skill that cannot render must not be offered as one that can.
    assert "note-taking" not in hint
    assert "**some-desktop**" in proof.missing_proof_note(["app/static/style.css"])


def test_the_nudge_matches_on_the_skill_name_too(tmp_path, monkeypatch):
    shipped, promoted = tmp_path / "shipped", tmp_path / "promoted"
    shipped.mkdir()
    promoted.mkdir()
    _skill(shipped, "screenshot-box", "Uses the machine in the cupboard.")
    monkeypatch.setattr(proof.config, "SKILLS_DIR", shipped)
    monkeypatch.setattr(proof.memory, "promoted_skills_dir", lambda: promoted)
    assert "**screenshot-box**" in proof.render_skill_hint()


def test_the_nudge_still_works_with_no_render_skill_at_all(tmp_path, monkeypatch):
    """A fresh install has no personal machine skill; the nudge must survive."""
    shipped, promoted = tmp_path / "shipped", tmp_path / "promoted"
    shipped.mkdir()
    promoted.mkdir()
    _skill(shipped, "note-taking", "Keep notes tidy.")
    monkeypatch.setattr(proof.config, "SKILLS_DIR", shipped)
    monkeypatch.setattr(proof.memory, "promoted_skills_dir", lambda: promoted)

    hint = proof.render_skill_hint()
    assert "**" not in hint
    assert "render a page" in hint
    note = proof.missing_proof_note(["app/static/style.css"])
    assert "no screenshot" in note and "shots/whatever.png" in note


def test_a_missing_skills_directory_is_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(proof.config, "SKILLS_DIR", tmp_path / "nope")
    monkeypatch.setattr(proof.memory, "promoted_skills_dir", lambda: tmp_path / "also-nope")
    assert proof.render_skill_hint()

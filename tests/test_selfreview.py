"""Agent self-review: a run critiques its own committed diff before the work
surfaces for Wes to review, holding it on the active shelf if it isn't done
(selfreview.py, RESEARCH.md §3)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import agent_runner, config, db, orphans, selfreview, worker


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    (r / "README.md").write_text("hello\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "first")
    return r


def commit(repo: Path, path: str, body: str = "x\n") -> str:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"add {path}")
    return orphans._git(repo, "rev-parse", "HEAD", quiet=True).strip()


def project():
    return db.create_project("Thing", "does a thing", stage="active")


# --- wants_review -----------------------------------------------------------


def test_wants_review_on_new_stage_and_legacy_status():
    assert selfreview.wants_review({"new_stage": "review"}, "build") is True
    assert selfreview.wants_review({"new_status": "review"}, "build") is True


def test_wants_review_false_for_other_stages_and_no_report():
    assert selfreview.wants_review({"new_stage": "active"}, "build") is False
    assert selfreview.wants_review({}, "build") is False
    assert selfreview.wants_review(None, "build") is False


def test_research_burst_never_wants_review():
    assert selfreview.wants_review({"new_stage": "review"}, "research") is False


# --- enabled / model --------------------------------------------------------


def test_enabled_defaults_on_and_reads_off():
    assert selfreview.enabled() is True
    db.set_setting("self_review", "0")
    assert selfreview.enabled() is False
    db.set_setting("self_review", "1")
    assert selfreview.enabled() is True


def test_review_model_default_and_override():
    assert selfreview.review_model() == config.SELF_REVIEW_MODEL
    db.set_setting("self_review_model", "opus")
    assert selfreview.review_model() == "opus"
    db.set_setting("self_review_model", "nonsense")
    assert selfreview.review_model() == config.SELF_REVIEW_MODEL


# --- parse_verdict (fail-open is the whole safety story) ---------------------


def test_parse_clean_ready():
    v = selfreview.parse_verdict('{"ready": true, "blocking": [], "note": "looks done"}')
    assert v.ready is True
    assert v.blocking == []


def test_parse_hold_with_reasons():
    v = selfreview.parse_verdict(
        '{"ready": false, "blocking": ["no tests", "missing endpoint"], "note": "half done"}'
    )
    assert v.ready is False
    assert v.blocking == ["no tests", "missing endpoint"]
    assert v.note == "half done"


def test_parse_extracts_object_from_surrounding_prose():
    v = selfreview.parse_verdict('Sure!\n```json\n{"ready": false, "blocking": ["x"]}\n```\ndone')
    assert v.ready is False
    assert v.blocking == ["x"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not json at all",
        "{bad json",
        '{"ready": "maybe"}',  # not a strict False
        '{"note": "no ready key"}',
    ],
)
def test_parse_fails_open_to_ready(text):
    assert selfreview.parse_verdict(text).ready is True


def test_hold_without_concrete_reasons_surfaces():
    # "not ready" but nothing the next run could act on -> pass, don't bounce on a vibe.
    v = selfreview.parse_verdict('{"ready": false, "blocking": [], "note": "meh"}')
    assert v.ready is True


def test_parse_drops_blank_blocking_items():
    v = selfreview.parse_verdict('{"ready": false, "blocking": ["real", "", "  "]}')
    assert v.ready is False
    assert v.blocking == ["real"]


# --- run_diff ---------------------------------------------------------------


def test_run_diff_returns_stat_and_patch(repo):
    before = orphans._git(repo, "rev-parse", "HEAD", quiet=True).strip()
    commit(repo, "app/thing.py", "def f():\n    return 1\n")
    stat, patch = selfreview.run_diff(repo, before)
    assert "app/thing.py" in stat
    assert "return 1" in patch


def test_run_diff_empty_without_baseline_or_new_commit(repo):
    before = orphans._git(repo, "rev-parse", "HEAD", quiet=True).strip()
    assert selfreview.run_diff(repo, None) == ("", "")
    assert selfreview.run_diff(None, before) == ("", "")
    # No new commit since `before`.
    assert selfreview.run_diff(repo, before) == ("", "")


def test_run_diff_excludes_portal_bookkeeping(repo):
    before = orphans._git(repo, "rev-parse", "HEAD", quiet=True).strip()
    commit(repo, ".portal/report.json", '{"x": 1}\n')
    stat, patch = selfreview.run_diff(repo, before)
    assert stat == "" and patch == ""


# --- build_review -----------------------------------------------------------


def test_build_review_none_when_nothing_committed(repo):
    p = project()
    before = orphans._git(repo, "rev-parse", "HEAD", quiet=True).strip()
    assert selfreview.build_review(p, {"new_stage": "review"}, repo, before) is None


def test_build_review_prompt_mentions_diff_and_open_todos(repo):
    p = project()
    db.add_todo(p["id"], "wire up the endpoint")
    before = orphans._git(repo, "rev-parse", "HEAD", quiet=True).strip()
    commit(repo, "app/thing.py", "def f():\n    return 1\n")
    prompt = selfreview.build_review(
        db.get_project(p["id"]),
        {"new_stage": "review", "summary": ["did the thing"], "journal_entry_md": "details"},
        repo,
        before,
    )
    assert prompt is not None
    assert "app/thing.py" in prompt
    assert "wire up the endpoint" in prompt
    assert "did the thing" in prompt


def test_build_prompt_truncates_a_huge_diff():
    p = project()
    giant = "x" * (selfreview.MAX_DIFF_CHARS + 5000)
    prompt = selfreview.build_prompt(p, {}, "stat", giant)
    assert "diff truncated here" in prompt
    assert len(prompt) < len(giant) + 5000


# --- build_command (read-only posture) --------------------------------------


def test_build_command_is_read_only():
    cmd = selfreview.build_command("PROMPT", "sonnet")
    assert cmd[0] == "claude" and "-p" in cmd and "PROMPT" in cmd
    assert "--model" in cmd and "sonnet" in cmd
    # The safety story: writing tools are explicitly denied, editing never allowed.
    joined = " ".join(cmd)
    assert "--disallowedTools" in cmd
    for denied in ("Edit", "Write", "MultiEdit"):
        assert denied in cmd, denied
    assert "--allowedTools" in cmd


# --- run_review fails open (no claude on PATH in tests) ----------------------


@pytest.mark.asyncio
async def test_run_review_fails_open_when_claude_missing(tmp_path, monkeypatch):
    # No `claude` binary resolvable -> FileNotFoundError -> ready=True.
    monkeypatch.setattr(selfreview, "build_command", lambda *a: ["definitely-not-a-real-binary-xyz"])
    v = await selfreview.run_review("p", tmp_path, "sonnet")
    assert v.ready is True


# --- notes ------------------------------------------------------------------


def test_hold_note_lists_blocking_items():
    note = selfreview.hold_note(selfreview.Verdict(False, ["no tests", "broken import"], "close these"))
    assert "no tests" in note and "broken import" in note
    assert "close these" in note
    assert "active shelf" in note


# --- worker wiring ----------------------------------------------------------


def _result(report):
    return agent_runner.RunResult(ok=True, report=report)


@pytest.mark.asyncio
async def test_worker_holds_review_when_critic_finds_gaps(repo, monkeypatch):
    p = db.create_project("Thing", "x", stage="active", slug="thing")
    # Simulate _apply_report having flipped it to review.
    db.update_project(p["id"], stage="review")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: repo)
    before = orphans._git(repo, "rev-parse", "HEAD", quiet=True).strip()
    commit(repo, "app/thing.py", "def f():\n    return 1\n")

    async def fake_review(prompt, cwd, model):
        return selfreview.Verdict(False, ["no tests added"], "add tests")

    monkeypatch.setattr(selfreview, "run_review", fake_review)
    await worker._maybe_self_review(db.get_project(p["id"]), _result({"new_stage": "review"}), "build", before)

    assert db.get_project(p["id"])["stage"] == "active"
    journal = db.list_journal_asc(p["id"], limit=10)
    assert any("Self-review held" in row["content_md"] for row in journal)
    assert any("no tests added" in row["content_md"] for row in journal)


@pytest.mark.asyncio
async def test_worker_surfaces_when_critic_passes(repo, monkeypatch):
    p = db.create_project("Thing", "x", stage="active", slug="thing")
    db.update_project(p["id"], stage="review")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: repo)
    before = orphans._git(repo, "rev-parse", "HEAD", quiet=True).strip()
    commit(repo, "app/thing.py", "def f():\n    return 1\n")

    async def fake_review(prompt, cwd, model):
        return selfreview.Verdict(True)

    monkeypatch.setattr(selfreview, "run_review", fake_review)
    await worker._maybe_self_review(db.get_project(p["id"]), _result({"new_stage": "review"}), "build", before)

    assert db.get_project(p["id"])["stage"] == "review"
    assert not any(
        "Self-review held" in row["content_md"] for row in db.list_journal_asc(p["id"], limit=10)
    )


@pytest.mark.asyncio
async def test_worker_skips_when_disabled(repo, monkeypatch):
    db.set_setting("self_review", "0")
    p = db.create_project("Thing", "x", stage="active", slug="thing")
    db.update_project(p["id"], stage="review")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: repo)
    before = orphans._git(repo, "rev-parse", "HEAD", quiet=True).strip()
    commit(repo, "app/thing.py", "code\n")

    called = False

    async def fake_review(prompt, cwd, model):
        nonlocal called
        called = True
        return selfreview.Verdict(False, ["x"])

    monkeypatch.setattr(selfreview, "run_review", fake_review)
    await worker._maybe_self_review(db.get_project(p["id"]), _result({"new_stage": "review"}), "build", before)
    assert called is False
    assert db.get_project(p["id"])["stage"] == "review"


@pytest.mark.asyncio
async def test_worker_skips_non_review_runs(repo, monkeypatch):
    p = db.create_project("Thing", "x", stage="active", slug="thing")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: repo)
    before = orphans._git(repo, "rev-parse", "HEAD", quiet=True).strip()

    called = False

    async def fake_review(prompt, cwd, model):
        nonlocal called
        called = True
        return selfreview.Verdict(False, ["x"])

    monkeypatch.setattr(selfreview, "run_review", fake_review)
    # A run that did not ask for review is never self-reviewed.
    await worker._maybe_self_review(db.get_project(p["id"]), _result({"summary": ["did stuff"]}), "build", before)
    assert called is False


@pytest.mark.asyncio
async def test_worker_surfaces_when_no_diff_to_judge(repo, monkeypatch):
    p = db.create_project("Thing", "x", stage="active", slug="thing")
    db.update_project(p["id"], stage="review")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: repo)
    before = orphans._git(repo, "rev-parse", "HEAD", quiet=True).strip()  # no new commit

    called = False

    async def fake_review(prompt, cwd, model):
        nonlocal called
        called = True
        return selfreview.Verdict(False, ["x"])

    monkeypatch.setattr(selfreview, "run_review", fake_review)
    await worker._maybe_self_review(db.get_project(p["id"]), _result({"new_stage": "review"}), "build", before)
    # Nothing committed -> build_review returns None -> critic never runs, stays in review.
    assert called is False
    assert db.get_project(p["id"])["stage"] == "review"


@pytest.mark.asyncio
async def test_worker_fails_open_on_exception(repo, monkeypatch):
    p = db.create_project("Thing", "x", stage="active", slug="thing")
    db.update_project(p["id"], stage="review")
    monkeypatch.setattr(orphans, "repo_for", lambda slug: repo)
    before = orphans._git(repo, "rev-parse", "HEAD", quiet=True).strip()
    commit(repo, "app/thing.py", "code\n")

    async def boom(prompt, cwd, model):
        raise RuntimeError("critic blew up")

    monkeypatch.setattr(selfreview, "run_review", boom)
    # Must not raise, and must leave the project surfaced for review.
    await worker._maybe_self_review(db.get_project(p["id"]), _result({"new_stage": "review"}), "build", before)
    assert db.get_project(p["id"])["stage"] == "review"

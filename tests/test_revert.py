"""Undoing what a run committed: app/revert.py and the run page's button.

The tests that matter most here are the refusals. A revert is the one thing in
the portal that changes files in a workspace without an agent in the loop, so
"it declined to touch anything" has to be as well pinned as "it worked".
"""
from __future__ import annotations

import subprocess

import pytest
from starlette.testclient import TestClient

from app import config, db, revert, worklock


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check
    )


def head(repo) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def project(temp_data_dir):
    """A project whose workspace is a real git repo with one commit in it."""
    row = db.create_project(
        "Widget", description="x", stage="active", build_approved=True, slug="widget"
    )
    ws = config.PROJECTS_DIR / "widget"
    ws.mkdir(parents=True, exist_ok=True)
    git(ws, "init", "-q", "-b", "main")
    git(ws, "config", "user.email", "t@t")
    git(ws, "config", "user.name", "T")
    (ws / "app.txt").write_text("original\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", "base")
    return row


def a_run(project, ws, *, files: dict[str, str], message="agent work", status="ok"):
    """Record a run that committed `files`, exactly as the worker would."""
    before = head(ws)
    run_id = db.create_run(project["id"], "build", "opus")
    for name, text in files.items():
        (ws / name).write_text(text)
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", message)
    db.finish_run(run_id, status)
    db.set_run_workspace_heads(run_id, before, head(ws))
    return run_id


# --- what the run committed ------------------------------------------------


def test_landed_names_the_commits_the_run_made(project):
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"}, message="make it better")

    plan = revert.landed(db.get_run_with_project(run_id))
    assert plan is not None
    assert [c.subject for c in plan.commits] == ["make it better"]
    assert plan.blocker is None and plan.can_undo


def test_a_run_that_committed_nothing_offers_no_undo(project):
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok")
    assert revert.landed(db.get_run_with_project(run_id)) is None


def test_a_run_from_before_the_columns_existed_offers_no_undo(project):
    """The range cannot be reconstructed from a timestamp, and guessing it would
    make a destructive button act on commits it was never told about."""
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"})
    db.set_run_workspace_heads(run_id, None, None)  # as an old row reads
    assert revert.landed(db.get_run_with_project(run_id)) is None


def test_the_worker_records_both_heads_or_neither(project):
    """A `before` with no `after` would name an open-ended range, so the undo
    would offer to revert every commit made since - including other runs'."""
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"})
    row = db.get_run_with_project(run_id)
    assert bool(row["ws_head_before"]) == bool(row["ws_head_after"])


# --- the undo itself -------------------------------------------------------


def test_undo_puts_the_files_back(project):
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n", "new.txt": "added\n"})

    outcome = revert.undo(db.get_run_with_project(run_id), who="Wes")
    assert outcome.ok, outcome.message
    assert (ws / "app.txt").read_text() == "original\n"
    assert not (ws / "new.txt").exists()


def test_undo_adds_a_commit_rather_than_erasing_history(project):
    """A `git reset --hard` would be the obvious implementation and would throw
    away every later run's work. The reverted commit must still be reachable."""
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"}, message="the work")
    reverted_sha = head(ws)

    outcome = revert.undo(db.get_run_with_project(run_id))
    assert outcome.ok
    log = git(ws, "log", "--format=%s").stdout
    assert "the work" in log, "the undone commit must survive in history"
    assert log.splitlines()[0].startswith(f"Undo run #{run_id}")
    # And the content of it is still retrievable, which is what makes this safe.
    assert "changed" in git(ws, "show", f"{reverted_sha}:app.txt").stdout


def test_undoing_an_early_run_leaves_a_later_run_standing(project):
    """The whole reason for a revert over a reset: undoing run #1 must not
    delete run #2's unrelated work."""
    ws = config.PROJECTS_DIR / "widget"
    first = a_run(project, ws, files={"one.txt": "one\n"}, message="feature one")
    a_run(project, ws, files={"two.txt": "two\n"}, message="feature two")

    outcome = revert.undo(db.get_run_with_project(first))
    assert outcome.ok, outcome.message
    assert not (ws / "one.txt").exists()
    assert (ws / "two.txt").read_text() == "two\n", "the later run's work survives"


def test_a_conflicting_undo_refuses_and_leaves_the_tree_clean(project):
    """A later run editing the same lines makes the inverse patch not apply.
    The failure that matters is not the refusal - it is being left with conflict
    markers in a workspace the next agent will open."""
    ws = config.PROJECTS_DIR / "widget"
    first = a_run(project, ws, files={"app.txt": "version two\n"}, message="v2")
    a_run(project, ws, files={"app.txt": "version three\n"}, message="v3")

    outcome = revert.undo(db.get_run_with_project(first))
    assert not outcome.ok
    assert "same lines" in outcome.message
    assert (ws / "app.txt").read_text() == "version three\n"
    assert git(ws, "status", "--porcelain").stdout.strip() == "", "tree left clean"
    assert not (ws / ".git" / "REVERT_HEAD").exists(), "sequencer state cleared"


def test_undo_refuses_while_the_tree_is_dirty(project):
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"})
    (ws / "app.txt").write_text("someone is mid-edit\n")

    outcome = revert.undo(db.get_run_with_project(run_id))
    assert not outcome.ok
    assert "uncommitted" in outcome.message
    assert (ws / "app.txt").read_text() == "someone is mid-edit\n"


def test_the_portals_own_report_json_does_not_block_an_undo(project):
    """`.portal/report.json` is rewritten on every run, so counting it as dirt
    would leave almost every workspace permanently un-revertable."""
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"})
    (ws / ".portal").mkdir(exist_ok=True)
    (ws / ".portal" / "report.json").write_text("{}")

    outcome = revert.undo(db.get_run_with_project(run_id))
    assert outcome.ok, outcome.message


def test_undo_refuses_while_the_run_is_still_going(project):
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"}, status="ok")
    db.get_conn().execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
    db.get_conn().commit()

    outcome = revert.undo(db.get_run_with_project(run_id))
    assert not outcome.ok
    assert "still going" in outcome.message


def test_undo_refuses_while_an_agent_holds_the_workspace(project, monkeypatch):
    """The lease is real mutual exclusion, not a check: reverting under a live
    agent would change the tree while it is mid-edit."""
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"})

    import contextlib

    @contextlib.contextmanager
    def busy(_):
        raise worklock.Busy("held")
        yield  # pragma: no cover

    monkeypatch.setattr(worklock, "held", busy)
    outcome = revert.undo(db.get_run_with_project(run_id))
    assert not outcome.ok
    assert "working in this workspace" in outcome.message
    assert (ws / "app.txt").read_text() == "changed\n", "nothing touched"


def test_a_second_undo_of_the_same_run_refuses(project, client):
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"})

    assert client.post(f"/run/{run_id}/revert", follow_redirects=False).status_code == 303
    row = db.get_run_with_project(run_id)
    assert row["reverted_at"]
    outcome = revert.undo(row)
    assert not outcome.ok and "already been undone" in outcome.message


# --- the lease -------------------------------------------------------------


def test_held_raises_rather_than_failing_open(tmp_path):
    """Every other lease in worklock fails open; this one guards a destructive
    operation, so `could not lock` has to mean `did not do it`."""
    missing = tmp_path / "gone"
    with pytest.raises(worklock.Unavailable):
        with worklock.held(missing):
            pass  # pragma: no cover


def test_held_and_a_spawned_lease_exclude_each_other(tmp_path):
    """Both are BSD locks on the same directory inode, which is the only reason
    the in-process undo and a spawned agent can be trusted not to overlap."""
    d = tmp_path / "ws"
    d.mkdir()
    with worklock.held(d):
        assert worklock.is_busy(d) is True
    assert worklock.is_busy(d) is False


# --- the page --------------------------------------------------------------


def test_the_run_page_shows_what_landed_and_offers_the_undo(project, client):
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"}, message="make it better")

    body = client.get(f"/run/{run_id}").text
    assert "what this run committed" in body
    assert "make it better" in body
    assert f"/run/{run_id}/revert" in body


def test_a_refusal_renders_on_the_page_not_as_a_json_blob(project, client):
    """FastAPI's default error page is JSON, and this refusal is a sentence a
    person has to read and act on - on a phone."""
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"})
    (ws / "app.txt").write_text("mid-edit\n")

    resp = client.post(f"/run/{run_id}/revert", follow_redirects=False)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Could not undo this run" in resp.text
    assert "uncommitted" in resp.text


def test_undoing_from_the_page_journals_it_for_the_next_agent(project, client):
    """Without this the next run finds the feature missing, concludes the last
    run died before committing, and builds the whole thing again."""
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"}, message="make it better")

    client.post(f"/run/{run_id}/revert", follow_redirects=False)
    entries = [j["content_md"] for j in db.list_journal(project["id"])]
    note = next(n for n in entries if "undid" in n)
    assert "make it better" in note
    assert "do not simply rebuild it" in note


def test_the_page_says_why_when_it_cannot_undo(project, client):
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"})
    (ws / "app.txt").write_text("mid-edit\n")

    body = client.get(f"/run/{run_id}").text
    assert "uncommitted work in this workspace" in body
    assert f"/run/{run_id}/revert" not in body, "no button that would only refuse"


def test_an_undone_run_shows_that_rather_than_the_button(project, client):
    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"})
    client.post(f"/run/{run_id}/revert", follow_redirects=False)

    body = client.get(f"/run/{run_id}").text
    assert "reverted, not" in body
    assert f"/run/{run_id}/revert" not in body


def test_reverting_the_portals_own_source_schedules_a_restart(project, client, monkeypatch):
    """Imported Python does not change until the process does, so an undo of a
    meta-project run would otherwise appear to work and change nothing."""
    from app import main

    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"})
    # BOTH, and never only the first: `orphans.repo_for` resolves the meta
    # project to `config.APP_ROOT`, so renaming the meta slug alone points this
    # test's revert at the portal's own live source tree.
    monkeypatch.setattr(config, "META_PROJECT_SLUG", "widget")
    monkeypatch.setattr(config, "APP_ROOT", ws)

    fired: list[tuple] = []
    monkeypatch.setattr(main.worker, "schedule_source_restart", lambda *a: fired.append(a))

    assert client.post(f"/run/{run_id}/revert", follow_redirects=False).status_code == 303
    assert len(fired) == 1, "the reverted source must reach the running process"


def test_an_ordinary_project_revert_does_not_restart_the_portal(project, client, monkeypatch):
    from app import main

    ws = config.PROJECTS_DIR / "widget"
    run_id = a_run(project, ws, files={"app.txt": "changed\n"})
    fired: list[tuple] = []
    monkeypatch.setattr(main.worker, "schedule_source_restart", lambda *a: fired.append(a))

    client.post(f"/run/{run_id}/revert", follow_redirects=False)
    assert fired == []

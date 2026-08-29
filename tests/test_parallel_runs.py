"""Two agents on one project at once, each in its own git worktree.

Wes, 2026-08-28: *"I want to be able to run parallel agents for projects. I
think the way to implement it would be to show it as an option when adding a
note and a run is already going. Have it as an additional option next to queue
note. Call it 'parallel run'."*

The whole point of these tests is that a parallel run must not become the
2026-07-29 double-run by another name. So they assert the isolation with real
git rather than with mocks: a real repo, a real worktree, real commits on both
sides, and a real merge.

Not to be confused with `test_parallel.py`, which is about the *global*
concurrency cap - how many runs the whole portal may have in flight at once.
This file is about two agents inside one project.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess

import pytest
from starlette.testclient import TestClient

from app import (
    agent_runner, config, daycycle, db, main, orphans, parallel, worker, worklock,
)


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clean_worker_state(temp_data_dir):
    db.set_setting("last_reflect_date", daycycle.current_day())
    worker._PARALLEL_SAID.clear()

    def reset():
        worker._inflight.clear()
        while not worker.manual_queue.empty():
            worker.manual_queue.get_nowait()

    reset()
    yield
    reset()
    worker._PARALLEL_SAID.clear()


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def make_project(slug="thing", stage="active"):
    return db.create_project(title=slug.title(), description="", stage=stage, slug=slug)


def make_workspace(slug="thing"):
    """A real git repo with one commit, which is what every workspace the
    portal has actually run in looks like."""
    ws = config.PROJECTS_DIR / slug
    ws.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(ws), check=True)
    subprocess.run(["git", "config", "user.email", "portal@example.invalid"],
                   cwd=str(ws), check=True)
    subprocess.run(["git", "config", "user.name", "Portal Test"],
                   cwd=str(ws), check=True)
    (ws / "README.md").write_text("start\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "first")
    return ws


def commit_in(repo, name, text, message):
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


# --------------------------------------------------------------------------
# The worktree itself
# --------------------------------------------------------------------------

def test_a_parallel_worktree_is_a_separate_directory_on_its_own_branch(temp_data_dir):
    make_workspace()
    tree = parallel.open_worktree("thing", 7)

    assert tree is not None
    assert tree != config.PROJECTS_DIR / "thing"
    assert tree.is_dir()
    assert (tree / "README.md").read_text(encoding="utf-8") == "start\n"
    assert _git(tree, "rev-parse", "--abbrev-ref", "HEAD") == "portal/parallel-7"
    # Not inside data/projects/, which is scanned as one-folder-per-project.
    assert config.PROJECTS_DIR not in tree.parents


def test_the_two_checkouts_do_not_see_each_others_edits(temp_data_dir):
    ws = make_workspace()
    tree = parallel.open_worktree("thing", 7)

    commit_in(ws, "main_side.txt", "from the ordinary run\n", "ordinary")
    commit_in(tree, "parallel_side.txt", "from the parallel run\n", "parallel")

    assert not (tree / "main_side.txt").exists()
    assert not (ws / "parallel_side.txt").exists()


def test_a_workspace_with_no_commits_still_gets_a_worktree(temp_data_dir):
    """`git worktree add` refuses a repo with no HEAD, and a brand-new project
    is exactly that - so the feature must not be dead on a fresh project."""
    ws = config.PROJECTS_DIR / "fresh"
    ws.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(ws), check=True)
    subprocess.run(["git", "config", "user.email", "p@example.invalid"],
                   cwd=str(ws), check=True)
    subprocess.run(["git", "config", "user.name", "P"], cwd=str(ws), check=True)

    tree = parallel.open_worktree("fresh", 3)
    assert tree is not None and tree.is_dir()


def test_a_directory_that_is_not_a_repo_refuses_rather_than_falling_back(temp_data_dir):
    """None is the only safe answer: handing the caller the shared workspace
    instead would be the double-run this module exists to prevent."""
    (config.PROJECTS_DIR / "bare").mkdir(parents=True)
    assert parallel.open_worktree("bare", 1) is None


def test_a_workspace_that_is_not_a_repo_never_commits_into_an_enclosing_one(
    temp_data_dir,
):
    """The "is this a repo" check is not decorative, and this is why.

    Without it a workspace that is merely a *directory* falls through to the
    empty-commit seeding below - and `git commit` in a directory with no repo of
    its own walks up and commits into whichever repo encloses it. On this box
    that is a real arrangement: a workspace nested under some other checkout.
    Committing into somebody else's history to open a worktree we are about to
    refuse anyway would be the worst kind of side effect.
    """
    outer = config.PROJECTS_DIR
    outer.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(outer), check=True)
    subprocess.run(["git", "config", "user.email", "o@example.invalid"],
                   cwd=str(outer), check=True)
    subprocess.run(["git", "config", "user.name", "Outer"], cwd=str(outer), check=True)
    (outer / "kept.txt").write_text("outer\n", encoding="utf-8")
    _git(outer, "add", "-A")
    _git(outer, "commit", "-m", "outer first")
    before = _git(outer, "rev-parse", "HEAD")

    (outer / "nested").mkdir()
    assert parallel.open_worktree("nested", 1) is None
    assert _git(outer, "rev-parse", "HEAD") == before


def test_a_file_uploaded_with_the_note_reaches_the_worktree(temp_data_dir):
    """The attachment that started the parallel run is by definition not
    committed yet, so a worktree cut from HEAD would not have the screenshot
    the prompt tells the agent to look at."""
    ws = make_workspace()
    (ws / "attachments").mkdir()
    (ws / "attachments" / "0007-shot.png").write_bytes(b"PNG")

    tree = parallel.open_worktree("thing", 9)
    assert (tree / "attachments" / "0007-shot.png").read_bytes() == b"PNG"


# --------------------------------------------------------------------------
# Merging back
# --------------------------------------------------------------------------

def test_a_parallel_branch_merges_back_into_the_workspace(temp_data_dir):
    ws = make_workspace()
    tree = parallel.open_worktree("thing", 7)
    commit_in(tree, "feature.py", "print('hi')\n", "the parallel work")

    result = parallel.merge_back("thing", 7, running=False)

    assert result.status == "merged"
    assert result.commits == 1
    assert (ws / "feature.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert parallel.pending("thing") == []


def test_both_agents_work_survives_the_merge(temp_data_dir):
    ws = make_workspace()
    tree = parallel.open_worktree("thing", 7)
    commit_in(ws, "main_side.txt", "ordinary\n", "ordinary")
    commit_in(tree, "parallel_side.txt", "parallel\n", "parallel")

    assert parallel.merge_back("thing", 7).status == "merged"
    assert (ws / "main_side.txt").exists()
    assert (ws / "parallel_side.txt").exists()


def test_the_merge_is_refused_while_a_run_is_in_flight(temp_data_dir):
    ws = make_workspace()
    tree = parallel.open_worktree("thing", 7)
    commit_in(tree, "feature.py", "x\n", "parallel work")

    result = parallel.merge_back("thing", 7, running=True)

    assert result.status == "busy"
    assert result.kept
    assert not (ws / "feature.py").exists()
    # Kept, not dropped: the work is still there to merge later.
    assert parallel.pending("thing") == [7]


def test_the_merge_is_refused_into_a_dirty_workspace(temp_data_dir):
    """A `git merge` into a checkout somebody is editing is the double-run
    with extra steps, so a dirty tree counts as occupied."""
    ws = make_workspace()
    tree = parallel.open_worktree("thing", 7)
    commit_in(tree, "feature.py", "x\n", "parallel work")
    (ws / "half-written.py").write_text("the other agent is typing\n", encoding="utf-8")

    assert parallel.merge_back("thing", 7, running=False).status == "busy"
    assert parallel.pending("thing") == [7]


def test_the_merge_is_refused_while_an_agent_holds_the_workspace_lease(temp_data_dir):
    """The lease is the check that matters when the runs table is wrong.

    A run adopted across a service restart, or one whose row was cleared by a
    bug, reads as not-running - which is precisely the 2026-07-29 failure. The
    `flock` an agent holds is the answer that does not depend on any of that,
    so it is checked even when the database says the project is idle.
    """
    ws = make_workspace()
    tree = parallel.open_worktree("thing", 7)
    commit_in(tree, "feature.py", "x\n", "parallel work")

    with worklock.held(ws):
        result = parallel.merge_back("thing", 7, running=False)

    assert result.status == "busy"
    assert not (ws / "feature.py").exists()
    assert parallel.pending("thing") == [7]
    # And it lands once the lease is released, so the guard is a delay and not
    # a permanent refusal.
    assert parallel.merge_back("thing", 7, running=False).status == "merged"


def test_a_deferred_merge_lands_by_itself_once_the_workspace_frees_up(temp_data_dir):
    ws = make_workspace()
    tree = parallel.open_worktree("thing", 7)
    commit_in(tree, "feature.py", "x\n", "parallel work")
    assert parallel.merge_back("thing", 7, running=True).status == "busy"

    results = parallel.drain("thing", running=False)

    assert [r.status for r in results] == ["merged"]
    assert (ws / "feature.py").exists()
    assert not tree.exists()  # the checkout is cleaned up once its branch lands


def test_a_conflict_keeps_the_branch_and_leaves_no_half_merge(temp_data_dir):
    ws = make_workspace()
    tree = parallel.open_worktree("thing", 7)
    commit_in(ws, "shared.py", "the ordinary run's version\n", "ordinary")
    commit_in(tree, "shared.py", "the parallel run's version\n", "parallel")

    result = parallel.merge_back("thing", 7)

    assert result.status == "conflict"
    assert result.kept
    assert parallel.pending("thing") == [7]
    # No merge left half-applied in the index, which would leave the next
    # ordinary run in a checkout it cannot commit from.
    assert _git(ws, "status", "--porcelain") == ""
    assert (ws / "shared.py").read_text(encoding="utf-8") == "the ordinary run's version\n"
    note = parallel.journal_note("thing", result)
    assert "portal/parallel-7" in note
    assert "Nothing is lost" in note


def test_a_branch_with_no_commits_is_dropped_rather_than_waited_for(temp_data_dir):
    make_workspace()
    parallel.open_worktree("thing", 7)

    result = parallel.merge_back("thing", 7, running=True)

    assert result.status == "empty"
    assert not result.kept
    assert parallel.pending("thing") == []
    assert parallel.journal_note("thing", result) is None


def test_drain_stops_at_the_first_busy_branch(temp_data_dir):
    """The workspace cannot free up between two iterations of one loop, so
    trying the rest is wasted git."""
    make_workspace()
    for run_id in (7, 8):
        tree = parallel.open_worktree("thing", run_id)
        commit_in(tree, f"f{run_id}.py", "x\n", f"work {run_id}")

    results = parallel.drain("thing", running=True)

    assert [r.status for r in results] == ["busy"]
    assert parallel.pending("thing") == [7, 8]


def test_drain_can_be_narrowed_to_the_runs_that_have_finished(temp_data_dir):
    """Merging a branch whose agent is still committing to it is exactly as
    wrong as merging into a tree somebody is editing."""
    ws = make_workspace()
    for run_id in (7, 8):
        tree = parallel.open_worktree("thing", run_id)
        commit_in(tree, f"f{run_id}.py", "x\n", f"work {run_id}")

    parallel.drain("thing", running=False, run_ids=[7])

    assert (ws / "f7.py").exists()
    assert not (ws / "f8.py").exists()
    assert parallel.pending("thing") == [8]


def test_projects_with_branches_finds_the_slug_from_the_directory(temp_data_dir):
    make_workspace("my-thing")
    parallel.open_worktree("my-thing", 12)
    assert parallel.projects_with_branches() == ["my-thing"]


def test_projects_with_branches_is_empty_when_nothing_is_parallel(temp_data_dir):
    make_workspace()
    assert parallel.projects_with_branches() == []


# --------------------------------------------------------------------------
# The prompt the parallel agent gets
# --------------------------------------------------------------------------

def test_the_parallel_agent_is_told_where_it_is_and_who_else_is_working(temp_data_dir):
    section = parallel.prompt_section(
        "thing", 7, config.DATA_DIR / "parallel" / "thing-run7", others=1
    )
    assert "PARALLEL run" in section
    assert "portal/parallel-7" in section
    assert "thing-run7" in section
    assert "Commit." in section


def test_only_the_meta_project_is_warned_about_the_shared_source_tree(temp_data_dir):
    ordinary = parallel.prompt_section("thing", 7, config.DATA_DIR / "x", others=1)
    meta = parallel.prompt_section(
        config.META_PROJECT_SLUG, 7, config.DATA_DIR / "x", others=1
    )
    assert "same source tree" not in ordinary
    assert "same source tree" in meta


def test_an_ordinary_prompt_is_unchanged_by_the_feature(temp_data_dir):
    from app import agent_runner

    project = make_project()
    plain = agent_runner.build_prompt("build", project)
    assert "PARALLEL run" not in plain
    assert agent_runner.build_prompt("build", project, parallel_note="") == plain


def test_the_parallel_note_reaches_the_prompt(temp_data_dir):
    from app import agent_runner

    project = make_project()
    prompt = agent_runner.build_prompt("build", project, parallel_note="## MARKER")
    assert "## MARKER" in prompt


# --------------------------------------------------------------------------
# The button, and what it does
# --------------------------------------------------------------------------

def test_the_parallel_button_appears_only_while_an_agent_is_working(client):
    project = make_project()
    page = client.get(f"/project/{project['slug']}").text
    assert 'value="parallel"' not in page

    db.create_run(project["id"], "build", "opus")
    page = client.get(f"/project/{project['slug']}").text
    assert 'value="parallel"' in page
    assert "parallel run" in page


def test_pressing_parallel_run_starts_a_second_agent_beside_the_first(client, monkeypatch):
    project = make_project()
    make_workspace(project["slug"])
    first = db.create_run(project["id"], "build", "opus")

    spawned: list[tuple[int, bool]] = []

    def fake_spawn(proj, task, parallel=False):
        run_id = db.create_run(proj["id"], task, "opus", parallel=parallel)
        spawned.append((run_id, parallel))
        return run_id

    monkeypatch.setattr(worker, "spawn_run", fake_spawn)

    client.post(f"/project/{project['slug']}/note",
                data={"note": "do the other half", "then": "parallel"})

    assert len(spawned) == 1
    assert spawned[0][1] is True
    live = db.running_runs_for_project(project["id"])
    assert {int(r["id"]) for r in live} == {first, spawned[0][0]}
    assert db.is_parallel_run(db.get_run(spawned[0][0])) is True
    assert db.is_parallel_run(db.get_run(first)) is False


def test_the_note_is_stored_even_when_the_parallel_run_is_refused(client, monkeypatch):
    project = make_project()
    db.create_run(project["id"], "build", "opus")
    db.set_setting(parallel.MAX_AGENTS_SETTING, "1")
    monkeypatch.setattr(worker, "spawn_run",
                        lambda *a, **k: pytest.fail("should not have spawned"))

    client.post(f"/project/{project['slug']}/note",
                data={"note": "an idea", "then": "parallel"})

    bodies = [r["content_md"] for r in db.list_journal(project_id=project["id"])]
    assert any("an idea" in b for b in bodies)
    assert any("is its limit" in b for b in bodies)


def test_the_per_project_agent_cap_is_enforced(temp_data_dir):
    project = make_project()
    db.set_setting(parallel.MAX_AGENTS_SETTING, "2")
    db.create_run(project["id"], "build", "opus")
    db.create_run(project["id"], "build", "opus", parallel=True)

    started, why = asyncio.run(worker.start_parallel_run(project))

    assert started is False
    assert "limit" in why


def test_the_portal_wide_run_cap_still_applies(temp_data_dir, monkeypatch):
    project = make_project()
    db.create_run(project["id"], "build", "opus")
    monkeypatch.setitem(worker._inflight, 999, None)
    monkeypatch.setattr(worker.pacing, "parallel_cap", lambda n: 1)
    monkeypatch.setattr(worker, "spawn_run",
                        lambda *a, **k: pytest.fail("should not have spawned"))

    started, why = asyncio.run(worker.start_parallel_run(project))

    assert started is False
    assert "cap" in why


def test_with_nothing_running_the_button_just_runs_normally(temp_data_dir, monkeypatch):
    """A run can finish while Wes is typing. Refusing then would read as the
    button being broken, so it does what he asked for: an agent, now."""
    project = make_project()
    queued: list[int] = []
    monkeypatch.setattr(worker, "queue_manual_run",
                        lambda pid: asyncio.sleep(0, result=queued.append(pid)))

    started, why = asyncio.run(worker.start_parallel_run(project))

    assert started is True and why == ""
    assert queued == [project["id"]]


def test_a_parked_project_is_put_back_on_the_working_shelf(temp_data_dir, monkeypatch):
    project = make_project(stage="review")
    db.create_run(project["id"], "build", "opus")
    monkeypatch.setattr(worker, "spawn_run", lambda *a, **k: 42)

    started, _ = asyncio.run(worker.start_parallel_run(project))

    assert started is True
    assert db.get_project(project["id"])["stage"] == "active"


def test_max_agents_never_reads_as_zero(temp_data_dir):
    for value in ("0", "-3", "", "nonsense"):
        db.set_setting(parallel.MAX_AGENTS_SETTING, value)
        assert worker.max_agents_per_project() >= 1


# --------------------------------------------------------------------------
# What the worker does around a finished parallel run
# --------------------------------------------------------------------------

def test_a_finished_parallel_run_is_merged_and_journaled(temp_data_dir):
    project = make_project()
    ws = make_workspace(project["slug"])
    run_id = db.create_run(project["id"], "build", "opus", parallel=True)
    tree = parallel.open_worktree(project["slug"], run_id)
    commit_in(tree, "feature.py", "x\n", "the parallel work")
    db.finish_run(run_id, "ok")

    results = worker.merge_parallel_work(project)

    assert [r.status for r in results] == ["merged"]
    assert (ws / "feature.py").exists()
    bodies = [r["content_md"] for r in db.list_journal(project_id=project["id"])]
    assert any("merged back into the workspace" in b for b in bodies)


def test_a_branch_whose_run_is_still_going_is_left_alone(temp_data_dir):
    project = make_project()
    ws = make_workspace(project["slug"])
    run_id = db.create_run(project["id"], "build", "opus", parallel=True)
    tree = parallel.open_worktree(project["slug"], run_id)
    commit_in(tree, "feature.py", "x\n", "half-written")

    assert worker.merge_parallel_work(project) == []
    assert not (ws / "feature.py").exists()


def test_a_parallel_run_works_in_its_worktree_and_not_the_shared_workspace(
    temp_data_dir, monkeypatch
):
    """The end-to-end claim, asserted where it is actually decided.

    Everything else here tests app/parallel.py in isolation. This is the test
    that would notice `run_project_task` forgetting to swap the working
    directory over - which would put two agents in one checkout while every
    unit test above stayed green.
    """
    project = make_project()
    ws = make_workspace(project["slug"])
    seen: dict = {}

    async def fake_claude(prompt, cwd, model, timeout_min, **kwargs):
        seen["cwd"] = cwd
        seen["lock_dir"] = kwargs.get("lock_dir")
        seen["prompt"] = prompt
        # Commit something, so the heads the run records can be checked.
        commit_in(cwd, "made.py", "x\n", "the parallel run's commit")
        return agent_runner.RunResult(ok=True, result_text="", report=None)

    monkeypatch.setattr(agent_runner, "run_claude", fake_claude)
    monkeypatch.setattr(worker, "_sync_skills", lambda ws: None)
    run_id = db.create_run(project["id"], "build", "opus", parallel=True)

    asyncio.run(worker.run_project_task(project, "build", run_id=run_id,
                                        model="opus", parallel=True))

    assert seen["cwd"] == parallel.worktree_for(project["slug"], run_id)
    assert seen["cwd"] != ws
    # The lease is taken on the worktree, so it can never contend with the
    # ordinary run's lease on the workspace.
    assert seen["lock_dir"] == seen["cwd"]
    assert "PARALLEL run" in seen["prompt"]
    # And the heads recorded point at the repo the commits actually landed in,
    # so the run page can name them and the undo button works.
    row = db.get_run(run_id)
    assert row["ws_head_before"] and row["ws_head_after"]
    assert row["ws_head_after"] == _git(seen["cwd"], "rev-parse", "HEAD")


def test_a_failed_parallel_run_never_reports_the_other_agents_edits(temp_data_dir):
    """`orphans.journal_note` describes what a dead run left uncommitted. Aimed
    at the shared workspace it would describe the OTHER agent's live edits as
    this run's abandoned work, and send the next run to tidy up a file still
    being written - the same trap the lock-conflict path already avoids."""
    ws = make_workspace()
    tree = parallel.open_worktree("thing", 7)
    (ws / "the-other-agent-is-typing.py").write_text("half\n", encoding="utf-8")

    assert orphans.journal_note("thing", "build", "errored", repo=tree) is None
    # And it still reports the parallel run's own leftovers.
    (tree / "mine.py").write_text("half\n", encoding="utf-8")
    note = orphans.journal_note("thing", "build", "errored", repo=tree)
    assert note and "mine.py" in note


def test_the_waiting_line_is_written_once_not_once_per_tick(temp_data_dir):
    project = make_project()
    make_workspace(project["slug"])
    run_id = db.create_run(project["id"], "build", "opus", parallel=True)
    tree = parallel.open_worktree(project["slug"], run_id)
    commit_in(tree, "feature.py", "x\n", "the parallel work")
    db.finish_run(run_id, "ok")
    # Something else is still in the project, so every attempt reports busy.
    db.create_run(project["id"], "build", "opus")

    for _ in range(3):
        worker.merge_parallel_work(project)

    bodies = [r["content_md"] for r in db.list_journal(project_id=project["id"])]
    assert sum("will be merged as soon as" in b for b in bodies) == 1


# --------------------------------------------------------------------------
# Deleting the project the checkouts belong to
# --------------------------------------------------------------------------
#
# A parallel checkout lives at `data/parallel/<slug>-run<id>`, outside the
# workspace, so deleting a project used to leave it behind whichever box was
# ticked. Worse, it was left behind *actively*: `projects_with_branches()`
# reads that directory listing every worker tick and `_drain_parallel_branches`
# then skips any slug the database no longer knows, so the leftovers were
# re-listed once a minute forever with nothing able to clear them.

def test_deleting_a_project_removes_its_parallel_checkouts(client, temp_data_dir):
    project = make_project()
    make_workspace(project["slug"])
    run_id = db.create_run(project["id"], "build", "opus", parallel=True)
    tree = parallel.open_worktree(project["slug"], run_id)
    commit_in(tree, "feature.py", "x\n", "the parallel work")
    db.finish_run(run_id, "ok")
    assert tree.is_dir()

    client.post(f"/project/{project['slug']}/delete",
                data={"confirm": project["slug"], "delete_workspace": "1"},
                follow_redirects=False)

    assert not tree.exists()
    assert parallel.projects_with_branches() == []


def test_the_checkouts_go_even_when_the_workspace_is_kept(client, temp_data_dir):
    """The box Wes ticks is about his files, not about the portal's bookkeeping.
    Keeping the workspace keeps the branch - that is where the commits are -
    but the checkout still has to go, because nothing would ever drain it."""
    project = make_project()
    workspace = make_workspace(project["slug"])
    run_id = db.create_run(project["id"], "build", "opus", parallel=True)
    tree = parallel.open_worktree(project["slug"], run_id)
    commit_in(tree, "feature.py", "x\n", "the parallel work")
    db.finish_run(run_id, "ok")

    client.post(f"/project/{project['slug']}/delete",
                data={"confirm": project["slug"]},
                follow_redirects=False)

    assert not tree.exists()
    assert workspace.is_dir(), "the workspace was not asked for"
    # The work itself survives in the repo Wes chose to keep.
    assert parallel.branch_for(run_id) in _git(workspace, "branch", "--list",
                                               "--format=%(refname:short)")


def test_another_projects_checkouts_are_left_alone(temp_data_dir):
    """`_DIR_RE` splits on the last `-run<digits>`, and slugs are free text, so
    this is the guard against `thing` reaping `thing-two`'s checkouts."""
    make_workspace("thing")
    make_workspace("thing-two")
    mine = parallel.open_worktree("thing", 1)
    theirs = parallel.open_worktree("thing-two", 2)

    assert parallel.discard_all("thing") == [1]

    assert not mine.exists()
    assert theirs.is_dir()


def test_discarding_with_no_parallel_directory_at_all_is_quiet(temp_data_dir, caplog):
    """Most projects have never had a parallel run, so `data/parallel/` does not
    exist and deleting one of them takes this path.

    Asserting the empty list is not enough to hold the `is_dir()` guard up: a
    missing directory makes `iterdir()` raise FileNotFoundError, which is an
    OSError, so the defensive `except` below returns the same empty list. What
    the guard is actually for is the *log* - without it the most ordinary
    delete on the board files a warning saying the portal could not read a
    directory, which is a support question about nothing.
    """
    make_workspace()
    assert not parallel.root().exists()
    with caplog.at_level(logging.WARNING, logger="app.parallel"):
        assert parallel.discard_all("thing") == []
    assert caplog.records == [], (
        "a project that never had a parallel run must delete without complaint"
    )


def test_every_checkout_of_one_project_goes_not_just_the_first(temp_data_dir):
    make_workspace()
    first = parallel.open_worktree("thing", 3)
    second = parallel.open_worktree("thing", 4)

    assert parallel.discard_all("thing") == [3, 4]

    assert not first.exists()
    assert not second.exists()


def test_a_stray_file_in_the_parallel_directory_is_not_mistaken_for_a_checkout(
    temp_data_dir,
):
    make_workspace()
    parallel.open_worktree("thing", 5)
    stray = parallel.root() / "thing-run6"
    stray.write_text("not a directory\n", encoding="utf-8")

    assert parallel.discard_all("thing") == [5]

    assert stray.is_file()

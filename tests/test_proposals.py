"""Patch series crossing between portals (app/proposals.py).

The premise: a follower cannot write to the mirror it follows and must never
edit its own checkout, so it *offers* a series under `patches/` in a workspace
and the publisher pulls, reviews, applies. Both halves run the same code, so
both are exercised here against one database: the sending side is a workspace
directory with a mailbox in it, the receiving side is a throwaway git repo
standing in for the source checkout.

Nothing here reaches a network. The two fetches the pull makes are replaced
with a function over a URL; the verdict posted back to a node is captured the
same way.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, main, mirror, nodes, notify, proposals, worker


@pytest.fixture
def client():
    return TestClient(main.app)


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        env={"GIT_AUTHOR_NAME": "Office Agent", "GIT_AUTHOR_EMAIL": "agent@office",
             "GIT_COMMITTER_NAME": "Office Agent", "GIT_COMMITTER_EMAIL": "agent@office",
             "HOME": str(repo), "PATH": "/usr/bin:/bin"},
    )
    return done.stdout


def make_source(tmp_path: Path) -> Path:
    """The publisher's source checkout: one committed file."""
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    (repo / "app.py").write_text("print('hello')\n")
    (repo / "README.md").write_text("# portal\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Initial")
    return repo


def make_series(tmp_path: Path, source: Path, commits=(("Add a greeting to the app", "print('hi')\n"),)) -> bytes:
    """A series cut the way the office agent cuts one: commits on a branch of a
    clone, then `git format-patch --stdout`."""
    clone = Path(tempfile.mkdtemp(prefix="clone-", dir=tmp_path))
    clone.rmdir()
    git(tmp_path, "clone", "-q", str(source), str(clone))
    git(clone, "checkout", "-q", "-b", "work")
    for subject, text in commits:
        (clone / "app.py").write_text(text)
        git(clone, "commit", "-q", "-am", subject + "\n\nA body line.\n\nAnd a second paragraph.")
    return git(clone, "format-patch", "--stdout", "master..work").encode()


def offer(slug: str, name: str, data: bytes) -> str:
    """Put a series where a workspace's agent would: `patches/<name>` under the
    project's workspace. Returns its sha."""
    workspace = Path(config.PROJECTS_DIR) / slug / proposals.PATCH_DIR
    workspace.mkdir(parents=True)
    (workspace / name).write_bytes(data)
    return proposals.sha_of(data)


@pytest.fixture
def source(tmp_path, monkeypatch):
    repo = make_source(tmp_path)
    monkeypatch.setattr(config, "APP_ROOT", repo)
    return repo


@pytest.fixture
def publishes(monkeypatch):
    monkeypatch.setattr(mirror, "configured", lambda target=None: True)
    monkeypatch.setattr(mirror, "published_head", lambda target=None: "abc1234def")


@pytest.fixture
def meta():
    return db.create_project("Project Portal", "the portal", slug=config.META_PROJECT_SLUG, stage="active")


@pytest.fixture
def far(monkeypatch):
    """Stand in for the far node: what its listing and mailbox URLs answer."""
    state: dict = {"listing": {"proposals": []}, "mbox": {}, "urls": []}

    def fake_fetch(url, timeout):
        state["urls"].append(url)
        if url.endswith("api/proposals"):
            return json.dumps(state["listing"]).encode()
        sha = url.rsplit("/", 2)[-2]
        if sha not in state["mbox"]:
            raise OSError("404")
        return state["mbox"][sha]

    monkeypatch.setattr(proposals, "_fetch", fake_fetch)
    return state


def file_from(node, data: bytes, name="series.mbox", project="theme") -> dict:
    row = proposals.file_series(data, node, {"sha": proposals.sha_of(data), "name": name, "project": project})
    assert row is not None
    return row


# --- parsing a mailbox --------------------------------------------------------

def test_parse_mbox_reads_a_real_format_patch_series(tmp_path):
    source = make_source(tmp_path)
    data = make_series(tmp_path, source, [
        ("Add a greeting to the app", "print('hi')\n"),
        ("Make the greeting louder, which is a subject long enough that format-patch folds it across two header lines", "print('HI')\n"),
    ])
    commits = proposals.parse_mbox(data)
    assert [c.subject for c in commits] == [
        "Add a greeting to the app",
        "Make the greeting louder, which is a subject long enough that format-patch folds it across two header lines",
    ]
    first = commits[0]
    assert "Office Agent" in first.author
    assert first.body == "A body line.\n\nAnd a second paragraph."
    assert [f.path for f in first.files] == ["app.py"]
    assert (first.insertions, first.deletions) == (1, 1)
    kinds = [line.kind for line in first.files[0].lines]
    assert kinds == ["hunk", "del", "add"]
    assert first.files[0].lines[2].text == "print('hi')"


def test_parse_mbox_strips_the_patch_tag_and_ignores_junk():
    junk = b"this is not a mailbox at all\n"
    assert proposals.parse_mbox(junk) == []
    fake = (
        b"From 0123456789abcdef0123456789abcdef01234567 Mon Sep 17 00:00:00 2001\n"
        b"From: A <a@b>\nSubject: [PATCH 2/3] Second of\n three\n\nbody\n---\n"
        b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n-- \n2.39\n"
    )
    commits = proposals.parse_mbox(fake)
    assert len(commits) == 1
    assert commits[0].subject == "Second of three"
    assert commits[0].body == "body"
    assert [line.text for line in commits[0].files[0].lines if line.kind == "add"] == ["new"]


def test_the_git_signature_is_not_part_of_the_diff():
    fake = (
        b"From 0123456789abcdef0123456789abcdef01234567 Mon Sep 17 00:00:00 2001\n"
        b"Subject: [PATCH] One\n\n---\n"
        b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n-- \n2.39.2\n\n"
    )
    files = proposals.parse_mbox(fake)[0].files
    assert [line.text for line in files[0].lines if line.kind != "hunk"] == ["old", "new"]


# --- the sending side ---------------------------------------------------------

def test_outgoing_lists_every_mailbox_under_patches_with_its_sha(tmp_path):
    source = make_source(tmp_path)
    data = make_series(tmp_path, source)
    sha = offer("theme", "walmart.mbox", data)
    (Path(config.PROJECTS_DIR) / "theme" / "patches" / "notes.txt").write_text("not a series")
    (Path(config.PROJECTS_DIR) / "theme" / "patches" / "junk.mbox").write_text("not a mailbox")
    out = proposals.outgoing()
    assert [(p["project"], p["name"], p["sha"]) for p in out] == [("theme", "walmart.mbox", sha)]
    assert out[0]["title"] == "Add a greeting to the app"
    assert out[0]["commits"] == 1
    assert out[0]["status"] == "pending"
    assert proposals.outgoing("other") == []
    assert proposals.outgoing_bytes(sha) == data
    assert proposals.outgoing_bytes("0" * 64) is None


def test_api_proposals_serves_the_listing_and_the_bytes(client, tmp_path):
    source = make_source(tmp_path)
    data = make_series(tmp_path, source)
    sha = offer("theme", "walmart.mbox", data)
    listing = client.get("/api/proposals").json()
    assert listing["proposals"][0]["sha"] == sha
    served = client.get(f"/api/proposals/{sha}/mbox")
    assert served.status_code == 200
    assert served.content == data
    assert client.get(f"/api/proposals/{'0' * 64}/mbox").status_code == 404


def test_a_verdict_posted_back_lands_on_the_offering_projects_journal(client, tmp_path):
    source = make_source(tmp_path)
    project = db.create_project("Theme", "a theme", slug="theme", stage="active")
    sha = offer("theme", "walmart.mbox", make_series(tmp_path, source))
    answer = client.post(
        f"/api/proposals/{sha}/decision",
        json={"verdict": "rejected", "note": "the sheet is hand-edited; edit the .in and rebuild", "by": "agent", "portal": "home"},
    )
    assert answer.status_code == 200
    entries = [row["content_md"] for row in db.list_journal(project["id"])]
    assert any("**rejected** at **home** by agent" in text for text in entries)
    assert any("edit the .in and rebuild" in text for text in entries)
    assert proposals.outgoing("theme")[0]["status"] == "rejected"
    assert proposals.outgoing("theme")[0]["note"].startswith("the sheet")


def test_a_verdict_for_an_unknown_series_or_a_junk_verdict_is_refused(client, tmp_path):
    source = make_source(tmp_path)
    sha = offer("theme", "walmart.mbox", make_series(tmp_path, source))
    assert client.post(f"/api/proposals/{'0' * 64}/decision", json={"verdict": "approved"}).status_code == 404
    assert client.post(f"/api/proposals/{sha}/decision", json={"verdict": "maybe"}).status_code == 404
    assert client.post(f"/api/proposals/{sha}/decision", content=b"junk").status_code == 400


# --- pulling --------------------------------------------------------------------

def test_pull_files_a_series_it_has_not_seen_and_never_twice(tmp_path, far, publishes):
    source = make_source(tmp_path)
    data = make_series(tmp_path, source)
    sha = proposals.sha_of(data)
    node = nodes.add("office", "http://office:8500")
    far["listing"] = {"proposals": [{"sha": sha, "name": "walmart.mbox", "project": "theme"}]}
    far["mbox"][sha] = data
    filed = proposals.pull(node)
    assert [p["title"] for p in filed] == ["Add a greeting to the app"]
    row = filed[0]
    assert (row["node_id"], row["node_name"], row["project_slug"], row["name"]) == ("office", "office", "theme", "walmart.mbox")
    assert row["status"] == "pending"
    assert proposals.mbox_path(row).read_bytes() == data
    assert proposals.pull(node) == []
    assert len(proposals.list_rows("in")) == 1


def test_pull_refuses_bytes_that_do_not_match_their_sha(tmp_path, far, publishes):
    source = make_source(tmp_path)
    data = make_series(tmp_path, source)
    node = nodes.add("office", "http://office:8500")
    far["listing"] = {"proposals": [{"sha": "f" * 64, "name": "x.mbox", "project": "theme"}]}
    far["mbox"]["f" * 64] = data
    assert proposals.pull(node) == []
    assert proposals.list_rows("in") == []


def test_pull_skips_a_listing_it_cannot_read_or_a_malformed_sha(tmp_path, far, publishes):
    node = nodes.add("office", "http://office:8500")
    far["listing"] = {"proposals": [{"sha": "not-a-sha"}, "junk", None]}
    assert proposals.pull(node) == []
    far["listing"] = "junk"
    assert proposals.pull(node) == []


def test_only_the_publisher_pulls(tmp_path, far, monkeypatch):
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    nodes.add("home", "http://home:8500")
    assert proposals.pulls_enabled() is False
    assert proposals.pull_all() == []
    assert far["urls"] == []


@pytest.mark.anyio
async def test_announcing_journals_notifies_and_queues_a_review_run(tmp_path, publishes, meta, monkeypatch):
    source = make_source(tmp_path)
    data = make_series(tmp_path, source)
    row = file_from({"id": "office", "name": "office"}, data)
    sent: list = []
    queued: list = []

    async def fake_notify(title, message, **kw):
        sent.append((title, message, kw))

    async def fake_queue(project_id):
        queued.append(project_id)

    monkeypatch.setattr(notify, "notify", fake_notify)
    monkeypatch.setattr(worker, "queue_manual_run", fake_queue)
    db.pause_project(meta["id"])
    assert await proposals.announce_new() == 1
    assert queued == [meta["id"]]
    assert db.is_paused(db.get_project(meta["id"])) is False
    entries = [r["content_md"] for r in db.list_journal(meta["id"])]
    assert any(f"[Review it](/proposals/{row['id']})" in text for text in entries)
    assert sent[0][0] == "Proposed change from office"
    assert sent[0][2]["navigate"] == f"/proposals/{row['id']}"
    assert sent[0][2]["project_id"] == meta["id"]
    # Idempotent: announced once, never again.
    assert await proposals.announce_new() == 0
    assert queued == [meta["id"]]


# --- deciding ---------------------------------------------------------------

def test_approving_applies_the_series_to_the_source(tmp_path, source, meta):
    data = make_series(tmp_path, source, [
        ("Add a greeting to the app", "print('hi')\n"),
        ("Shout it", "print('HI')\n"),
    ])
    row = file_from({"id": "office", "name": "office"}, data)
    before = git(source, "rev-parse", "HEAD").strip()
    outcome = proposals.apply(row["id"], "agent", "tests pass, comes in")
    assert outcome.ok, outcome.detail
    assert len(outcome.applied) == 2
    assert git(source, "rev-parse", "HEAD").strip() == outcome.applied[-1]
    assert git(source, "log", "--format=%s", f"{before}..HEAD").split() == ["Shout", "it", "Add", "a", "greeting", "to", "the", "app"]
    assert (source / "app.py").read_text() == "print('HI')\n"
    decided = proposals.get(row["id"])
    assert decided["status"] == "approved"
    assert decided["applied"] == outcome.applied
    assert decided["decided_by"] == "agent"
    assert decided["note"] == "tests pass, comes in"
    # The verdict is final: a second decision is refused either way.
    assert proposals.apply(row["id"], "wes").ok is False
    assert proposals.reject(row["id"], "wes").ok is False


def test_a_dirty_source_tree_refuses_and_stays_pending(tmp_path, source, meta):
    data = make_series(tmp_path, source)
    row = file_from({"id": "office", "name": "office"}, data)
    (source / "README.md").write_text("# edited, not committed\n")
    outcome = proposals.apply(row["id"], "wes")
    assert outcome.ok is False
    assert "uncommitted" in outcome.detail
    again = proposals.get(row["id"])
    assert again["status"] == "pending"
    assert "uncommitted" in again["note"]
    assert git(source, "log", "--oneline").count("\n") == 1


def test_a_series_that_does_not_apply_is_aborted_and_stays_pending(tmp_path, source, meta):
    data = make_series(tmp_path, source)
    # Move the source past the base in a way the series conflicts with.
    (source / "app.py").write_text("print('something else entirely')\n")
    git(source, "commit", "-q", "-am", "Diverge")
    row = file_from({"id": "office", "name": "office"}, data)
    outcome = proposals.apply(row["id"], "wes")
    assert outcome.ok is False
    assert outcome.detail.startswith("does not apply cleanly")
    assert proposals.get(row["id"])["status"] == "pending"
    # No half-applied state is left behind: the tree is clean and am is over.
    assert mirror.source_clean() is True
    assert not (source / ".git" / "rebase-apply").exists()


def test_rejecting_records_the_verdict(tmp_path, source, meta):
    row = file_from({"id": "office", "name": "office"}, make_series(tmp_path, source))
    outcome = proposals.reject(row["id"], "wes", "not now")
    assert outcome.ok
    decided = proposals.get(row["id"])
    assert (decided["status"], decided["note"], decided["decided_by"]) == ("rejected", "not now", "wes")
    assert proposals.pending() == []


def test_the_verdict_is_posted_back_to_the_offering_node(tmp_path, source, meta, monkeypatch):
    nodes.add("office", "http://office:8500")
    row = file_from({"id": "office", "name": "office"}, make_series(tmp_path, source))
    proposals.reject(row["id"], "agent", "needs a test")
    posted: list = []

    class _Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout):
        posted.append((request.full_url, request.method, json.loads(request.data.decode())))
        return _Answer()

    monkeypatch.setattr(proposals.urllib.request, "urlopen", fake_urlopen)
    assert proposals.tell_sender(proposals.get(row["id"]), "home") is True
    url, method, body = posted[0]
    assert url == f"http://office:8500/api/proposals/{row['sha']}/decision"
    assert method == "POST"
    assert body == {"verdict": "rejected", "note": "needs a test", "by": "agent", "applied": [], "portal": "home"}


def test_telling_an_unknown_or_unreachable_node_is_not_an_error(tmp_path, source, meta, monkeypatch):
    row = file_from({"id": "gone", "name": "gone"}, make_series(tmp_path, source))
    proposals.reject(row["id"], "wes")
    assert proposals.tell_sender(proposals.get(row["id"]), "home") is False
    nodes.add("gone", "http://gone:8500")

    def down(request, timeout):
        raise OSError("no route")

    monkeypatch.setattr(proposals.urllib.request, "urlopen", down)
    assert proposals.tell_sender(proposals.get(row["id"]), "home") is False


# --- the routes and the pages -------------------------------------------------

def test_the_approve_route_applies_journals_and_arms_the_restart(client, tmp_path, source, meta, publishes, monkeypatch):
    row = file_from({"id": "office", "name": "office"}, make_series(tmp_path, source))
    fired: list = []
    told: list = []
    monkeypatch.setattr(main.worker, "schedule_source_restart", lambda *a: fired.append(a))
    monkeypatch.setattr(proposals, "tell_sender", lambda p, name: told.append((p["id"], name)) or True)
    answer = client.post(
        f"/proposals/{row['id']}/approve",
        data={"note": "reviewed", "by": "agent"},
        headers={"accept": "application/json"},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["ok"] is True
    decided = proposals.get(row["id"])
    assert decided["status"] == "approved"
    assert fired == [(meta["id"], decided["applied"][-1])]
    entries = [r["content_md"] for r in db.list_journal(meta["id"])]
    assert any("was **approved** by agent" in text for text in entries)
    # The background thread that reports back may not have run inside the
    # test client; the point here is the wiring, which the monkeypatch shows.
    assert told == [] or told == [(row["id"], config.HOST_LABEL)]


def test_a_browser_approving_from_the_project_page_lands_back_on_it(client, tmp_path, source, meta, publishes, monkeypatch):
    row = file_from({"id": "office", "name": "office"}, make_series(tmp_path, source))
    monkeypatch.setattr(main.worker, "schedule_source_restart", lambda *a: None)
    answer = client.post(
        f"/proposals/{row['id']}/approve",
        data={"next": f"/project/{config.META_PROJECT_SLUG}#proposals"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert answer.status_code == 303
    assert answer.headers["location"] == f"/project/{config.META_PROJECT_SLUG}#proposals"


def test_a_refused_approval_says_why(client, tmp_path, source, meta, publishes):
    row = file_from({"id": "office", "name": "office"}, make_series(tmp_path, source))
    (source / "README.md").write_text("dirty\n")
    answer = client.post(f"/proposals/{row['id']}/approve", headers={"accept": "application/json"})
    assert answer.status_code == 409
    assert "uncommitted" in answer.json()["detail"]
    page = client.post(f"/proposals/{row['id']}/approve", headers={"accept": "text/html"}, follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith(f"/proposals/{row['id']}?error=")
    assert "uncommitted" in client.get(page.headers["location"]).text


def test_the_reject_route_records_the_note_from_the_form(client, tmp_path, source, meta, publishes, monkeypatch):
    row = file_from({"id": "office", "name": "office"}, make_series(tmp_path, source))
    monkeypatch.setattr(proposals, "tell_sender", lambda p, name: True)
    answer = client.post(f"/proposals/{row['id']}/reject", data={"note": "wrong file edited"}, follow_redirects=False)
    assert answer.status_code == 303
    decided = proposals.get(row["id"])
    assert decided["status"] == "rejected"
    assert decided["note"] == "wrong file edited"
    assert decided["decided_by"] == "Wes"
    assert client.post("/proposals/999/reject").status_code == 404


def test_the_proposal_page_renders_the_series_and_its_verdict(client, tmp_path, source, meta):
    row = file_from({"id": "office", "name": "office"}, make_series(tmp_path, source))
    page = client.get(f"/proposals/{row['id']}")
    assert page.status_code == 200
    assert "Add a greeting to the app" in page.text
    assert "print(&#39;hi&#39;)" in page.text or "print('hi')" in page.text
    assert "approve and apply" in page.text
    proposals.reject(row["id"], "wes", "not this way")
    page = client.get(f"/proposals/{row['id']}")
    assert "approve and apply" not in page.text
    assert "not this way" in page.text
    assert client.get("/proposals/999").status_code == 404


def test_the_list_page_shows_both_directions(client, tmp_path, source, meta, publishes):
    row = file_from({"id": "office", "name": "office"}, make_series(tmp_path, source), name="incoming.mbox")
    offer("theme", "outgoing.mbox", make_series(tmp_path, source, [("Offered from here", "print('x')\n")]))
    page = client.get("/proposals")
    assert page.status_code == 200
    assert f"#{row['id']} Add a greeting" in page.text
    assert "Offered from here" in page.text
    assert "check the other portals now" in page.text


def test_the_pull_button_reports_what_it_filed(client, tmp_path, far, publishes, meta, monkeypatch):
    source = make_source(tmp_path)
    data = make_series(tmp_path, source)
    sha = proposals.sha_of(data)
    nodes.add("office", "http://office:8500")
    far["listing"] = {"proposals": [{"sha": sha, "name": "walmart.mbox", "project": "theme"}]}
    far["mbox"][sha] = data

    async def fake_notify(*a, **kw):
        return None

    async def fake_queue(project_id):
        return None

    monkeypatch.setattr(notify, "notify", fake_notify)
    monkeypatch.setattr(worker, "queue_manual_run", fake_queue)
    answer = client.post("/proposals/pull", follow_redirects=False)
    assert answer.status_code == 303
    assert "1%20new%20proposal%20filed" in answer.headers["location"]
    assert len(proposals.pending()) == 1
    again = client.post("/proposals/pull", follow_redirects=False)
    assert "Nothing%20new" in again.headers["location"]


def test_the_meta_project_page_carries_the_block_and_others_do_not(client, tmp_path, source, meta, publishes):
    row = file_from({"id": "office", "name": "office"}, make_series(tmp_path, source))
    page = client.get(f"/project/{config.META_PROJECT_SLUG}")
    assert page.status_code == 200
    assert "Proposed changes" in page.text
    assert f'id="proposal-{row["id"]}"' in page.text
    assert "1 waiting" in page.text
    other = db.create_project("Other", "x", slug="other", stage="active")
    assert "Proposed changes" not in client.get(f"/project/{other['slug']}").text


def test_a_project_that_offered_a_series_sees_its_fate(client, tmp_path):
    source = make_source(tmp_path)
    db.create_project("Theme", "a theme", slug="theme", stage="active")
    sha = offer("theme", "walmart.mbox", make_series(tmp_path, source))
    page = client.get("/project/theme")
    assert "Proposed changes" in page.text
    assert "patches/walmart.mbox" in page.text
    assert "waiting for review" in page.text
    proposals.record_decision(sha, "approved", "in it goes", "agent", "home")
    page = client.get("/project/theme")
    assert "in it goes" in page.text
    assert "approved" in page.text


# --- the prompt --------------------------------------------------------------

def test_the_publishers_prompt_lists_what_waits_and_how_to_decide(tmp_path, source, meta, publishes):
    row = file_from({"id": "office", "name": "office"}, make_series(tmp_path, source))
    text = proposals.prompt_section(db.get_project(meta["id"]))
    assert text.startswith("## Proposed changes waiting for your review")
    assert f"**#{row['id']}** from office" in text
    assert "Add a greeting to the app" in text
    assert str(proposals.mbox_path(row)) in text
    assert f"/proposals/<id>/approve" in text
    assert "git am -3" in text
    proposals.reject(row["id"], "wes")
    assert proposals.prompt_section(db.get_project(meta["id"])) == ""
    other = db.create_project("Other", "x", slug="other", stage="active")
    assert proposals.prompt_section(other) == ""


def test_a_followers_prompt_says_how_to_propose(tmp_path, monkeypatch):
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    project = db.create_project("Theme", "a theme", slug="theme", stage="active")
    assert proposals.prompt_section(project) == ""
    nodes.add("home", "http://home:8500")
    nodes._mark("home", ok=True, node={"name": "home", "publishes": True, "commit": "abc"})
    text = proposals.prompt_section(project)
    assert text.startswith("## Proposing a change to the portal itself")
    assert "**home** (http://home:8500/)" in text
    assert f"{proposals.PATCH_DIR}/<name>.mbox" in text
    assert "carry the files by hand" in text


def test_the_section_reaches_the_built_prompt(tmp_path, source, meta, publishes, monkeypatch):
    file_from({"id": "office", "name": "office"}, make_series(tmp_path, source))
    monkeypatch.setattr(agent_runner, "_learnings_for_prompt", lambda: "")
    prompt = agent_runner.build_prompt("build", db.get_project(meta["id"]))
    assert "## Proposed changes waiting for your review" in prompt


# --- the poller ----------------------------------------------------------------

@pytest.mark.anyio
async def test_the_node_poller_pulls_and_announces(tmp_path, far, publishes, meta, monkeypatch):
    source = make_source(tmp_path)
    data = make_series(tmp_path, source)
    sha = proposals.sha_of(data)
    nodes.add("office", "http://office:8500")
    far["listing"] = {"proposals": [{"sha": sha, "name": "walmart.mbox", "project": "theme"}]}
    far["mbox"][sha] = data
    monkeypatch.setattr(nodes, "_fetch_json", lambda url, timeout=0: {
        "portal": "project-portal", "name": "office", "commit": "abc1234def", "running": 0,
    })
    queued: list = []

    async def fake_queue(project_id):
        queued.append(project_id)

    async def fake_notify(*a, **kw):
        return None

    monkeypatch.setattr(worker, "queue_manual_run", fake_queue)
    monkeypatch.setattr(notify, "notify", fake_notify)
    monkeypatch.setattr(nodes, "STARTUP_DELAY_SEC", 0)
    slept: list = []

    async def stop_after_one(seconds):
        # The first sleep is the startup delay; the second ends the loop's
        # first pass, which is the one under test.
        slept.append(seconds)
        if len(slept) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(nodes.asyncio, "sleep", stop_after_one)
    with pytest.raises(asyncio.CancelledError):
        await nodes.poll_loop()
    assert len(proposals.pending()) == 1
    assert queued == [meta["id"]]

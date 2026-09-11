"""Other portals this one knows about (app/nodes.py).

The premise these guard: two installs with *unrelated* git histories (the
public repository starts fresh) must still be able to say whether they run
the same code, and the install that publishes must be able to tell the other
to update - but never while that other has an agent run in flight.

Nothing here reaches a network. The probe is a function over a URL, and the
tests replace the one fetch it makes; the ssh push is a subprocess, and the
tests replace that too, asserting on the command it would have run.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time

import pytest
from starlette.testclient import TestClient

from app import config, db, main, mirror, nodes


@pytest.fixture
def client():
    return TestClient(main.app)


def _answer(commit="abc1234def", running=0, **more):
    base = {
        "portal": "project-portal",
        "name": "office",
        "hostname": "office-box",
        "commit": commit,
        "boot": "b1",
        "running": running,
        "open_questions": 1,
        "worker_enabled": True,
        "publishes": False,
    }
    base.update(more)
    return base


@pytest.fixture
def far(monkeypatch):
    """Stand in for the far portal: what /api/node answers, or an exception."""
    state = {"answer": _answer(), "raise": None, "urls": []}

    def fake_fetch(url, timeout=0):
        state["urls"].append(url)
        if state["raise"] is not None:
            raise state["raise"]
        return state["answer"]

    monkeypatch.setattr(nodes, "_fetch_json", fake_fetch)
    return state


@pytest.fixture
def publishes(monkeypatch):
    """Make this install the one that publishes, at a known source commit."""
    monkeypatch.setattr(mirror, "configured", lambda target=None: True)
    monkeypatch.setattr(mirror, "published_head", lambda target=None: "abc1234def")


# --- the registry ------------------------------------------------------------

def test_add_normalizes_the_url_and_slugs_the_name():
    node = nodes.add("The Office", "that-machine:8500", ssh=" wes@that-machine ")
    assert node["id"] == "the-office"
    assert node["url"] == "http://that-machine:8500/"
    assert node["ssh"] == "wes@that-machine"
    assert node["path"] == nodes.DEFAULT_PATH
    assert [n["id"] for n in nodes.registry()] == ["the-office"]


def test_adding_the_same_name_again_replaces_it():
    nodes.add("office", "http://a:8500")
    nodes.add("office", "http://b:8500")
    assert [n["url"] for n in nodes.registry()] == ["http://b:8500/"]


@pytest.mark.parametrize("name,url", [("", "http://x/"), ("office", ""), ("!!!", "http://x/")])
def test_add_refuses_an_empty_name_or_url(name, url):
    assert nodes.add(name, url) is None
    assert nodes.registry() == []


def test_remove_drops_the_node_and_its_status(far):
    nodes.add("office", "http://office:8500")
    nodes.snapshot()
    assert "office" in nodes._statuses()
    assert nodes.remove("office") is True
    assert nodes.registry() == []
    assert nodes._statuses() == {}
    assert nodes.remove("office") is False


def test_registry_survives_junk_in_the_setting():
    db.set_setting(nodes.REGISTRY_KEY, "not json")
    assert nodes.registry() == []
    db.set_setting(nodes.REGISTRY_KEY, json.dumps([{"name": "no id"}, "x", {"id": "a", "url": "http://a/"}]))
    assert [n["id"] for n in nodes.registry()] == ["a"]


# --- the probe ---------------------------------------------------------------

def test_probe_asks_api_node_and_records_the_answer(far):
    nodes.add("office", "http://office:8500")
    status = nodes.snapshot()["office"]
    assert far["urls"] == ["http://office:8500/api/node"]
    assert status["ok"] is True
    assert status["node"]["commit"] == "abc1234def"
    assert status["node"]["running"] == 0
    assert status["seen_at"] == status["checked_at"]


def test_probe_reports_a_dead_node_and_keeps_when_it_was_last_seen(far):
    nodes.add("office", "http://office:8500")
    first = nodes.snapshot()["office"]
    far["raise"] = OSError("connection refused")
    second = nodes.snapshot()["office"]
    assert second["ok"] is False
    assert "connection refused" in second["error"]
    assert second["seen_at"] == first["seen_at"]
    # What it said when it last answered is kept, so the page can still say
    # which commit it was on.
    assert second["node"]["commit"] == "abc1234def"


def test_probe_tells_a_non_portal_apart_from_a_dead_host(far):
    nodes.add("thing", "http://thing:8500")
    far["answer"] = {"hello": "world"}
    status = nodes.snapshot()["thing"]
    assert status["ok"] is False
    assert "not as a portal" in status["error"]


def test_snapshot_writes_only_when_the_reading_changes(far):
    nodes.add("office", "http://office:8500")
    nodes.snapshot()
    before = db.get_setting(nodes.STATUS_KEY)
    nodes.snapshot()
    assert db.get_setting(nodes.STATUS_KEY) == before, "an unchanged reading must not bump the data version"
    far["answer"] = _answer(commit="fffffff000")
    nodes.snapshot()
    assert db.get_setting(nodes.STATUS_KEY) != before


# --- the comparison ----------------------------------------------------------

def test_a_node_on_the_published_commit_is_up_to_date(far, publishes):
    nodes.add("office", "http://office:8500")
    nodes.snapshot()
    (row,) = nodes.view()
    assert row["state"] == "ok"
    assert "abc1234" in row["detail"]


def test_a_node_on_another_commit_is_behind(far, publishes):
    nodes.add("office", "http://office:8500")
    far["answer"] = _answer(commit="0000000111")
    nodes.snapshot()
    (row,) = nodes.view()
    assert row["state"] == "behind"
    assert "0000000" in row["detail"] and "abc1234" in row["detail"]


def test_an_install_that_publishes_nothing_cannot_say_behind(far, monkeypatch):
    """A follower comparing against itself: it knows its own commit, so a peer
    on a different one is 'unknown', not 'behind' - it has no idea which of
    the two is newer."""
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    monkeypatch.setattr(nodes, "source_commit", lambda: "")
    nodes.add("home", "http://home:8500")
    nodes.snapshot()
    (row,) = nodes.view()
    assert row["state"] == "unknown"


def test_a_follower_does_not_call_the_publisher_behind_when_it_is_the_stale_one(far, monkeypatch):
    """The bug this guards: a follower has no local knowledge of the published
    head - `published_commit()` used to fall through to *our own* commit, so
    the moment this install fell behind, the publisher's row read "behind" and
    accused the machine that is ahead. The publisher hands us the real answer
    in its /api/node reply; it is the authority, not us."""
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    monkeypatch.setattr(nodes, "source_commit", lambda: "0000000111")  # we are stale
    far["answer"] = _answer(name="home", commit="abc1234def", publishes=True, published="abc1234def")
    nodes.add("home", "http://home:8500")
    nodes.snapshot()
    (row,) = nodes.view()
    assert row["state"] == "ok", "the publisher runs what it publishes; it is not behind"
    assert "behind" not in row["detail"]


def test_a_follower_takes_the_published_head_from_the_peer_that_publishes(far, monkeypatch):
    """And having taken it, it can judge a *third* node truthfully - which an
    install that only knows its own commit can never do."""
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    monkeypatch.setattr(nodes, "source_commit", lambda: "0000000111")
    far["answer"] = _answer(name="home", commit="abc1234def", publishes=True, published="abc1234def")
    nodes.add("home", "http://home:8500")
    nodes.snapshot()
    assert nodes.authority() == "abc1234def"


def test_a_publishing_peer_that_lags_its_own_mirror_is_behind(far, monkeypatch):
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    monkeypatch.setattr(nodes, "source_commit", lambda: "abc1234def")
    far["answer"] = _answer(name="home", commit="0000000111", publishes=True, published="abc1234def")
    nodes.add("home", "http://home:8500")
    nodes.snapshot()
    (row,) = nodes.view()
    assert row["state"] == "behind"
    assert "0000000" in row["detail"] and "abc1234" in row["detail"]


def test_the_probe_carries_the_peers_published_head(far):
    far["answer"] = _answer(publishes=True, published="abc1234def")
    nodes.add("office", "http://office:8500")
    statuses = nodes.snapshot()
    assert statuses["office"]["node"]["published"] == "abc1234def"


def test_this_install_publishing_outranks_what_a_peer_claims(far, publishes, monkeypatch):
    """On the machine that publishes, our own mirror is the authority and a
    peer's claim cannot displace it."""
    far["answer"] = _answer(commit="0000000111", publishes=True, published="0000000111")
    nodes.add("office", "http://office:8500")
    nodes.snapshot()
    assert nodes.authority() == "abc1234def"


def test_with_nobody_publishing_there_is_no_authority(far, monkeypatch):
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    monkeypatch.setattr(nodes, "source_commit", lambda: "abc1234def")
    nodes.add("office", "http://office:8500")
    nodes.snapshot()
    assert nodes.authority() == ""


def test_a_peer_that_does_not_publish_is_not_taken_as_the_authority(far, monkeypatch):
    """Only a node that says it publishes speaks for the published head. A
    follower reports `published: ""` for itself, but a stale cached reading or
    a future field could carry something; it is still not authoritative."""
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    monkeypatch.setattr(nodes, "source_commit", lambda: "abc1234def")
    far["answer"] = _answer(name="desk", commit="0000000111", publishes=False, published="dddddd0000")
    nodes.add("desk", "http://desk:8500")
    nodes.snapshot()
    assert nodes.authority() == ""


def test_a_publisher_with_nothing_published_yet_does_not_shadow_a_real_one(far, monkeypatch):
    """A publishing node whose mirror has never been pushed reports an empty
    head. It must be skipped, not accepted as "the answer is nothing" - the
    next publisher in the registry may know."""
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    monkeypatch.setattr(nodes, "source_commit", lambda: "0000000111")
    answers = {
        "http://fresh:8500/api/node": _answer(name="fresh", commit="0000000111", publishes=True, published=""),
        "http://home:8500/api/node": _answer(name="home", commit="abc1234def", publishes=True, published="abc1234def"),
    }
    monkeypatch.setattr(nodes, "_fetch_json", lambda url, timeout=0: answers[url])
    nodes.add("fresh", "http://fresh:8500")
    nodes.add("home", "http://home:8500")
    nodes.snapshot()
    assert nodes.authority() == "abc1234def"


def test_a_dead_node_shows_off_with_its_last_sighting(far, publishes):
    nodes.add("office", "http://office:8500")
    nodes.snapshot()
    far["raise"] = OSError("no route to host")
    nodes.snapshot()
    (row,) = nodes.view()
    assert row["state"] == "off"
    assert "last seen" in row["detail"]
    assert row["online"] is False


def test_summary_is_the_dashboard_shape(far, publishes):
    nodes.add("office", "http://office:8500")
    nodes.snapshot()
    assert nodes.summary() == [
        {"id": "office", "name": "office", "url": "http://office:8500/", "state": "ok", "detail": "up to date at abc1234"}
    ]


# --- identity ----------------------------------------------------------------

def test_source_commit_reads_the_mirror_trailer_on_a_follower(tmp_path, monkeypatch):
    repo = tmp_path / "follower"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, text=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("config", "user.name", "T")
    run("config", "user.email", "t@example.invalid")
    (repo / "f").write_text("x")
    run("add", "-A")
    run("commit", "-q", "-m", f"Update from upstream\n\n{mirror.TRAILER} 1234567890abcdef")
    monkeypatch.setattr(config, "APP_ROOT", repo)
    assert nodes.source_commit() == "1234567890abcdef"


def test_source_commit_is_head_where_there_is_no_trailer(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, text=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("config", "user.name", "T")
    run("config", "user.email", "t@example.invalid")
    (repo / "f").write_text("x")
    run("add", "-A")
    run("commit", "-q", "-m", "a commit")
    head = run("rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(config, "APP_ROOT", repo)
    assert nodes.source_commit() == head


def test_api_node_answers_as_a_portal(client, monkeypatch):
    monkeypatch.setattr(nodes, "source_commit", lambda: "abc1234def")
    body = client.get("/api/node").json()
    assert body["portal"] == "project-portal"
    assert body["commit"] == "abc1234def"
    assert body["name"] == config.HOST_LABEL
    assert body["running"] == 0
    assert "boot" in body


# --- the update push ---------------------------------------------------------

@pytest.fixture
def ssh(monkeypatch):
    """Replace the subprocess the push runs; record the command, script the result."""
    calls = []
    state = {"rc": 0, "out": "Up to date and serving.", "err": ""}

    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if cmd[0] != "ssh":
            return real_run(cmd, **kw)  # git, for the identity
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, state["rc"], state["out"], state["err"])

    monkeypatch.setattr(nodes.subprocess, "run", fake_run)
    return {"calls": calls, "state": state}


def test_push_update_runs_update_py_over_ssh_and_journals_it(far, ssh, publishes):
    nodes.add("office", "http://office:8500", ssh="wes@office", path="~/portal")
    result = nodes.push_update("office")
    assert result["ok"] is True
    (cmd,) = ssh["calls"]
    assert cmd[0] == "ssh" and "BatchMode=yes" in cmd and cmd[-2] == "wes@office"
    assert cmd[-1] == "cd ~/portal && python3 deploy/update.py"
    status = nodes._statuses()["office"]
    assert status["update"]["ok"] is True
    assert "serving" in status["update"]["output"]
    assert status["pending_update"] is False


def test_push_update_records_a_failure(far, ssh):
    nodes.add("office", "http://office:8500", ssh="wes@office")
    ssh["state"].update(rc=1, out="", err="Permission denied (publickey)")
    result = nodes.push_update("office")
    assert result["ok"] is False
    assert "Permission denied" in result["output"]
    assert nodes.view()[0]["update_ok"] is False


def test_push_update_needs_an_ssh_target(far, ssh):
    nodes.add("office", "http://office:8500")
    assert nodes.push_update("office")["ok"] is False
    assert nodes.push_update("nope")["ok"] is False
    assert ssh["calls"] == []


def test_a_publish_marks_every_ssh_node_and_the_poller_pushes_when_idle(far, ssh, publishes):
    nodes.add("office", "http://office:8500", ssh="wes@office")
    nodes.add("readonly", "http://ro:8500")
    far["answer"] = _answer(commit="0000000111", running=2)
    statuses = nodes.snapshot()
    assert nodes.request_update_all() == ["office"]
    statuses = nodes.snapshot()
    # Two runs in flight there: not yet.
    assert nodes.due_updates(statuses, nodes.published_commit()) == []
    assert "waits for its 2 run" in nodes.view()[0]["detail"]
    far["answer"] = _answer(commit="0000000111", running=0)
    statuses = nodes.snapshot()
    assert nodes.due_updates(statuses, nodes.published_commit()) == ["office"]


def test_a_node_that_caught_up_by_itself_is_not_pushed(far, ssh, publishes):
    nodes.add("office", "http://office:8500", ssh="wes@office")
    nodes.snapshot()
    nodes.request_update_all()
    statuses = nodes.snapshot()  # already on the published commit
    assert nodes.due_updates(statuses, nodes.published_commit()) == []
    assert nodes._statuses()["office"]["pending_update"] is False


def test_a_dead_node_is_not_pushed_until_it_answers(far, ssh, publishes):
    nodes.add("office", "http://office:8500", ssh="wes@office")
    far["raise"] = OSError("down")
    statuses = nodes.snapshot()
    nodes.request_update_all()
    statuses = nodes.snapshot()
    assert nodes.due_updates(statuses, nodes.published_commit()) == []


def test_start_update_refuses_a_second_push_while_one_runs(far, ssh):
    nodes.add("office", "http://office:8500", ssh="wes@office")

    async def go():
        first = nodes.start_update("office")
        second = nodes.start_update("office")
        await nodes._UPDATES["office"]
        return first, second

    assert asyncio.run(go()) == (True, False)
    assert ssh["calls"] and ssh["calls"][0][0] == "ssh"


def test_start_update_needs_a_node_with_ssh(far):
    nodes.add("office", "http://office:8500")

    async def go():
        return nodes.start_update("office"), nodes.start_update("missing")

    assert asyncio.run(go()) == (False, False)


# --- the pages ---------------------------------------------------------------

def test_settings_lists_the_node_and_the_dashboard_shows_a_chip(client, far, publishes):
    nodes.add("office", "http://office:8500", ssh="wes@office")
    nodes.snapshot()
    page = client.get("/settings").text
    assert 'id="nodes"' in page
    assert "http://office:8500/" in page
    assert "up to date at abc1234" in page
    # The same word for the same state on both pages.
    assert "node-state\">up to date<" in page
    assert "/nodes/office/update" in page
    home = client.get("/").text
    assert "node-chip node-ok" in home
    assert 'href="http://office:8500/">office</a>' in home


def test_the_add_form_registers_and_probes_the_node(client, far):
    answer = client.post("/nodes/add", data={"name": "Office", "url": "office:8500", "ssh": "wes@office"}, follow_redirects=False)
    assert answer.status_code == 303 and answer.headers["location"].endswith("#nodes")
    assert nodes.registry()[0]["url"] == "http://office:8500/"
    assert far["urls"] == ["http://office:8500/api/node"]


def test_the_forget_form_removes_it(client, far):
    nodes.add("office", "http://office:8500")
    client.post("/nodes/office/remove", follow_redirects=False)
    assert nodes.registry() == []


def test_api_nodes_reports_the_publishers_head_on_a_follower(client, far, monkeypatch):
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    monkeypatch.setattr(nodes, "source_commit", lambda: "0000000111")
    far["answer"] = _answer(name="home", commit="abc1234def", publishes=True, published="abc1234def")
    nodes.add("home", "http://home:8500")
    nodes.snapshot()
    body = client.get("/api/nodes").json()
    assert body["published"] == "abc1234def", "not 0000000111 - our own commit is not a published head"


def test_api_nodes_reports_the_published_commit(client, far, publishes):
    nodes.add("office", "http://office:8500")
    body = client.get("/api/nodes?refresh=1").json()
    assert body["published"] == "abc1234def"
    assert body["nodes"][0]["state"] == "ok"


# --- this install ------------------------------------------------------------
#
# The row that answers Wes's own question. Everything above is about *other*
# portals; none of it could ever say whether the machine you are looking at is
# current, which is the thing he actually asked on 2026-09-09.


@pytest.fixture
def breadcrumb(tmp_path, monkeypatch):
    """Stand in for `data/update-status.json`, the file deploy/update.py drops."""
    path = tmp_path / "update-status.json"
    monkeypatch.setattr(nodes, "UPDATE_STATUS_PATH", path)

    def write(**fields):
        base = {"at": int(time.time()), "mode": "check", "ok": True, "changed": False,
                "summary": "already up to date with origin/main",
                "head": "ffff000", "source_commit": "abc1234def", "failures": [], "human": []}
        base.update(fields)
        path.write_text(json.dumps(base))
    return write


def test_this_install_is_up_to_date_when_it_runs_the_published_head(far, monkeypatch, breadcrumb):
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    monkeypatch.setattr(nodes, "source_commit", lambda: "abc1234def")
    far["answer"] = _answer(name="home", commit="abc1234def", publishes=True, published="abc1234def")
    nodes.add("home", "http://home:8500")
    nodes.snapshot()
    breadcrumb()

    me = nodes.this_install()
    assert me["state"] == "ok"
    assert me["commit"] == "abc1234def"
    assert "up to date" in me["detail"]


def test_this_install_says_it_is_behind_rather_than_blaming_the_publisher(far, monkeypatch, breadcrumb):
    """The whole point of the fix: when this machine is the stale one, the
    stale one is what gets named."""
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    monkeypatch.setattr(nodes, "source_commit", lambda: "0000000111")
    far["answer"] = _answer(name="home", commit="abc1234def", publishes=True, published="abc1234def")
    nodes.add("home", "http://home:8500")
    nodes.snapshot()
    breadcrumb()

    (peer,) = nodes.view()
    me = nodes.this_install()
    assert peer["state"] == "ok", "the publisher is ahead, not behind"
    assert me["state"] == "behind"
    assert "0000000" in me["detail"] and "abc1234" in me["detail"]


def test_this_install_cannot_judge_itself_with_no_publisher_around(far, monkeypatch, breadcrumb):
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    monkeypatch.setattr(nodes, "source_commit", lambda: "0000000111")
    breadcrumb()
    me = nodes.this_install()
    assert me["state"] == "unknown"


def test_a_silent_updater_is_a_warning_in_its_own_right(monkeypatch, breadcrumb, publishes):
    """The timer runs every 30 minutes. Nothing recorded in over 90 means the
    automation itself has stopped - which is what Wes suspected was happening
    and could not check. Up to date at a commit nobody is refreshing is not
    'up to date', it is a clock that stopped on a true reading."""
    monkeypatch.setattr(nodes, "source_commit", lambda: "abc1234def")
    breadcrumb(at=int(time.time()) - 100 * 60)

    me = nodes.this_install()
    assert me["stale_timer"] is True
    assert me["state"] == "behind", "a stopped updater must not render green"
    assert "not run" in me["detail"]


def test_an_updater_inside_its_cadence_is_not_called_stale(monkeypatch, breadcrumb, publishes):
    monkeypatch.setattr(nodes, "source_commit", lambda: "abc1234def")
    breadcrumb(at=int(time.time()) - 80 * 60)
    me = nodes.this_install()
    assert me["stale_timer"] is False
    assert me["state"] == "ok"


def test_a_failed_update_run_is_reported_with_its_reason(monkeypatch, breadcrumb, publishes):
    monkeypatch.setattr(nodes, "source_commit", lambda: "abc1234def")
    breadcrumb(ok=False, summary="stopped: a local edit to app/nodes.py",
               failures=["a local edit to app/nodes.py"])

    me = nodes.this_install()
    assert me["update_ok"] is False
    assert me["state"] == "behind"
    assert "local edit" in me["detail"]


def test_no_breadcrumb_at_all_reads_as_never_run(monkeypatch, publishes, tmp_path):
    """A fresh install, or one whose updater has never fired. Not an error -
    but not silence either, because silence is what sent Wes to ask."""
    monkeypatch.setattr(nodes, "UPDATE_STATUS_PATH", tmp_path / "nothing.json")
    monkeypatch.setattr(nodes, "source_commit", lambda: "abc1234def")

    me = nodes.this_install()
    assert me["ran"] == "never"
    assert me["stale_timer"] is True
    assert me["state"] == "behind"


def test_junk_in_the_breadcrumb_does_not_take_a_page_down(monkeypatch, publishes, breadcrumb, tmp_path):
    monkeypatch.setattr(nodes, "source_commit", lambda: "abc1234def")
    (tmp_path / "update-status.json").write_text("{not json at all")
    me = nodes.this_install()
    assert me["ran"] == "never"
    assert me["commit"] == "abc1234def"


def test_a_checkout_that_is_not_git_cannot_judge_itself_either(far, monkeypatch, breadcrumb):
    """`source_commit()` returns "" for a tree that is not a git checkout at
    all. Knowing the published head does not help when we cannot say what we
    run - that is 'unknown', not 'behind'."""
    monkeypatch.setattr(mirror, "configured", lambda target=None: False)
    monkeypatch.setattr(nodes, "source_commit", lambda: "")
    far["answer"] = _answer(name="home", commit="abc1234def", publishes=True, published="abc1234def")
    nodes.add("home", "http://home:8500")
    nodes.snapshot()
    breadcrumb()

    me = nodes.this_install()
    assert me["state"] == "unknown"
    assert "unknown commit" in me["detail"]

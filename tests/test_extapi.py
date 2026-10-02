"""The Claude app plugin's API (app/extapi.py) and the rules behind it
(app/claudeapp.py).

Wes, 2026-10-02: a Mac or iOS app cannot be built on this server, so it is
built in the Claude app on another machine, and the portal stays its record and
hands each session its context. His decisions, each pinned here:

1. record + context only - a project hosted elsewhere is never scheduled, by
   any door, and a note or an answer on it goes into the next context;
2. stars live on the portal, per person;
3. search finds done and abandoned projects.

Hosts and paths in these tests are invented; the real ones are runtime data.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import claudeapp, config, crossproject, db, notify, people, worker

HOST = "test-laptop"
PATH = "/Users/someone/Projects/Metronome"
API = "/api/ext/v1"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    # No context manager: that would run the lifespan hook and start the worker.
    return TestClient(main.app, headers={"X-Portal-Host": HOST,
                                         "X-Portal-Client": "claude-app-plugin/0.1"})


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, temp_data_dir):
    sent: list[tuple] = []

    async def fake_notify(title, message, **kw):
        sent.append((title, message, kw))

    monkeypatch.setattr(notify, "notify", fake_notify)

    def reset():
        while not worker.manual_queue.empty():
            worker.manual_queue.get_nowait()

    reset()
    yield sent
    reset()


def queued() -> list[int]:
    return list(worker.manual_queue._queue)  # type: ignore[attr-defined]


def make_elsewhere(client, title="Metronome iOS", **extra) -> dict:
    body = {"title": title, "description": "A native click track.", "host": HOST,
            "path": PATH, "remote": "git@example.invalid:me/metronome.git",
            "head": "4c1e9a2", **extra}
    r = client.post(f"{API}/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()["project"]


def checkout(slug: str) -> None:
    """Give a project a git checkout here, as its first run would."""
    (config.PROJECTS_DIR / slug / ".git").mkdir(parents=True)


# ------------------------------------------------------------------ root


def test_root_lists_every_feature(client):
    r = client.get(f"{API}/")
    assert r.status_code == 200
    assert r.json() == {"api": 1, "features": [
        "projects", "context", "link", "sessions", "journal", "todos", "questions", "stars"]}
    assert client.get(API).json()["api"] == 1


def test_errors_use_the_contract_shape(client):
    r = client.get(f"{API}/projects/no-such-thing/context")
    assert r.status_code == 404
    assert "no-such-thing" in r.json()["error"]
    r = client.get(f"{API}/nonsense")
    assert r.status_code == 404 and "error" in r.json()
    r = client.post(f"{API}/projects", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and "error" in r.json()


# -------------------------------------------------------------- creating


def test_creating_a_project_records_it_and_schedules_nothing(client):
    p = make_elsewhere(client)
    assert p["stage"] == "active"
    assert p["hosted_elsewhere"] is True
    assert p["locations"][0]["host"] == HOST and p["locations"][0]["head"] == "4c1e9a2"
    row = db.get_project_by_slug(p["slug"])
    assert row["title_locked"] == 1
    assert not (config.PROJECTS_DIR / p["slug"]).exists()
    entries = db.list_journal(int(row["id"]))
    assert any("Linked from the Claude app" in e["content_md"] and HOST in e["content_md"]
               for e in entries)
    assert queued() == []


def test_a_sub_project_joins_its_parents_family(client):
    parent = db.create_project("Metronome", slug="metronome", stage="active")
    p = make_elsewhere(client, parent="metronome")
    assert p["parent"] == "metronome"
    assert db.get_project_by_slug(p["slug"])["parent_id"] == parent["id"]


def test_create_needs_a_host_and_a_path(client):
    r = client.post(f"{API}/projects", json={"title": "x", "path": PATH},
                    headers={"X-Portal-Host": ""})
    assert r.status_code == 400
    assert client.post(f"{API}/projects", json={"title": "x", "host": HOST}).status_code == 400


# ------------------------------------------------------------- listing


def test_closed_projects_only_come_back_from_search(client):
    db.create_project("Live Thing", slug="live-thing", stage="active")
    done = db.create_project("Old Click Track", slug="old-click-track", stage="done")
    db.create_project("Dropped Idea", slug="dropped-idea", stage="abandoned")

    slugs = [p["slug"] for p in client.get(f"{API}/projects").json()["projects"]]
    assert "live-thing" in slugs and "old-click-track" not in slugs and "dropped-idea" not in slugs

    found = client.get(f"{API}/projects", params={"q": "click", "include_closed": "1"}).json()
    assert [p["slug"] for p in found["projects"]] == [done["slug"]]
    assert client.get(f"{API}/projects", params={"q": "click"}).json()["projects"] == []


def test_search_ranks_the_title_above_the_description(client):
    db.create_project("Garden Planner", slug="garden", description="mentions metronome once",
                      stage="active")
    db.create_project("Metronome", slug="metronome", stage="active")
    db.create_project("Unrelated", slug="unrelated", stage="active")
    found = client.get(f"{API}/projects", params={"q": "metronome"}).json()["projects"]
    assert [p["slug"] for p in found] == ["metronome", "garden"]


def test_listing_shows_only_the_callers_projects(client):
    karli = people.add("Karli")
    db.create_project("Mine", slug="mine", stage="active")
    db.create_project("Hers", slug="hers", stage="active", person_id=karli)
    slugs = [p["slug"] for p in client.get(f"{API}/projects").json()["projects"]]
    assert "mine" in slugs and "hers" not in slugs


def test_limit_caps_the_list(client):
    for i in range(3):
        db.create_project(f"P{i}", slug=f"p{i}", stage="active")
    assert len(client.get(f"{API}/projects", params={"limit": 2}).json()["projects"]) == 2


# ---------------------------------------------------------------- stars


def test_stars_keep_their_order_and_are_per_person(client):
    for slug in ("a", "b", "c"):
        db.create_project(slug.upper(), slug=slug, stage="active")
    assert client.put(f"{API}/stars/b").json() == {"stars": ["b"]}
    client.put(f"{API}/stars/a")
    assert client.put(f"{API}/stars/b").json() == {"stars": ["b", "a"]}  # idempotent
    assert client.get(f"{API}/stars").json() == {"stars": ["b", "a"]}

    listed = {p["slug"]: p for p in client.get(f"{API}/projects").json()["projects"]}
    assert listed["a"]["starred"] and listed["a"]["star_position"] == 1
    assert listed["c"]["starred"] is False and listed["c"]["star_position"] is None

    assert client.put(f"{API}/stars", json={"stars": ["c", "b", "nope"]}).json() == {"stars": ["c", "b"]}
    assert client.delete(f"{API}/stars/c").json() == {"stars": ["b"]}

    karli = people.add("Karli")
    slug = people.get(karli)["slug"]
    other = TestClient(client.app, cookies={people.COOKIE: slug})
    assert other.get(f"{API}/stars").json() == {"stars": []}


def test_a_finished_project_keeps_its_star(client):
    p = db.create_project("A", slug="a", stage="active")
    client.put(f"{API}/stars/a")
    db.update_project(p["id"], stage="done")
    assert client.get(f"{API}/stars").json() == {"stars": ["a"]}


# -------------------------------------------------------------- locations


def test_linking_and_unlinking_moves_a_project(client):
    p = db.create_project("Existing", slug="existing", stage="active")
    r = client.post(f"{API}/projects/existing/locations", json={"path": PATH, "head": "abc1234"})
    assert r.json()["project"]["hosted_elsewhere"] is True
    # A second post refreshes the row rather than adding one.
    r = client.post(f"{API}/projects/existing/locations", json={"path": PATH, "head": "def5678"})
    assert [l["head"] for l in r.json()["project"]["locations"]] == ["def5678"]
    r = client.delete(f"{API}/projects/existing/locations", params={"host": HOST, "path": PATH})
    assert r.json()["project"]["locations"] == []
    assert claudeapp.hosted_elsewhere(db.get_project(p["id"])) is False


def test_a_project_with_a_checkout_here_is_not_hosted_elsewhere(client):
    """Decision 1's other half: a project also built here keeps its scheduling."""
    p = db.create_project("Both", slug="both", stage="active")
    claudeapp.link(p["id"], HOST, PATH)
    assert claudeapp.hosted_elsewhere(db.get_project(p["id"])) is True
    checkout("both")
    assert claudeapp.hosted_elsewhere(db.get_project(p["id"])) is False


# -------------------------------------------------------------- sessions


def test_a_session_is_journaled_once_when_it_ends(client):
    p = make_elsewhere(client)
    url = f"{API}/projects/{p['slug']}/sessions"
    start = {"session_id": "s-1", "host": HOST, "path": PATH, "title": "Tap tempo",
             "started_at": "2026-10-02T10:00:00Z", "ended_at": None, "head_start": "4c1e9a2"}
    r = client.post(url, json=start)
    assert r.status_code == 200 and r.json()["session"]["ended_at"] is None
    pid = db.get_project_by_slug(p["slug"])["id"]
    before = len(db.list_journal(pid))

    end = {"session_id": "s-1", "ended_at": "2026-10-02T11:00:00Z", "head_end": "9f00d1e",
           "commits": [{"sha": "9f00d1e0000", "subject": "Add tap tempo"}],
           "summary": "Tap tempo works."}
    s = client.post(url, json=end).json()["session"]
    assert s["title"] == "Tap tempo" and s["head_start"] == "4c1e9a2"  # kept from the start
    assert s["ended_at"] == "2026-10-02T11:00:00+00:00"
    entries = db.list_journal(pid)
    assert len(entries) == before + 1
    entry = entries[0]
    assert entry["author"] == "user" and entry["kind"] == "session"
    assert f"Claude app session on {HOST}" in entry["content_md"]
    assert "Add tap tempo" in entry["content_md"] and "Tap tempo works." in entry["content_md"]
    assert entry["person_id"] == people.owner()["id"]

    client.post(url, json={**end, "summary": "Tap tempo works, and is tested."})
    entries = db.list_journal(pid)
    assert len(entries) == before + 1
    assert "and is tested" in entries[0]["content_md"]

    listed = client.get(f"{API}/projects/{p['slug']}/context").json()["project"]
    assert listed["locations"][0]["head"] == "9f00d1e"
    assert listed["last_session_at"] == "2026-10-02T11:00:00+00:00"


def test_a_session_with_nothing_to_show_is_not_journaled(client):
    p = make_elsewhere(client)
    pid = db.get_project_by_slug(p["slug"])["id"]
    before = len(db.list_journal(pid))
    client.post(f"{API}/projects/{p['slug']}/sessions",
                json={"session_id": "s-2", "started_at": "2026-10-02T10:00:00Z",
                      "ended_at": "2026-10-02T10:05:00Z", "commits": []})
    assert len(db.list_journal(pid)) == before


def test_a_silent_session_times_out(client):
    p = make_elsewhere(client)
    client.post(f"{API}/projects/{p['slug']}/sessions",
                json={"session_id": "s-3", "started_at": "2026-10-02T10:00:00Z"})
    old = (datetime.now(timezone.utc) - timedelta(hours=claudeapp.SESSION_TIMEOUT_HOURS + 1)
           ).isoformat(timespec="seconds")
    db.get_conn().execute("UPDATE app_sessions SET updated_at = ?", (old,))
    db.get_conn().commit()
    pid = db.get_project_by_slug(p["slug"])["id"]
    [row] = claudeapp.sessions(pid)
    assert row["ended_at"] == old and row["timed_out"] == 1
    # A fresh one is left alone.
    client.post(f"{API}/projects/{p['slug']}/sessions",
                json={"session_id": "s-4", "started_at": "2026-10-02T12:00:00Z"})
    assert claudeapp.get_session("s-4")["ended_at"] is None


def test_a_session_id_cannot_move_between_projects(client):
    a = make_elsewhere(client, title="A")
    b = make_elsewhere(client, title="B")
    client.post(f"{API}/projects/{a['slug']}/sessions", json={"session_id": "s-5"})
    r = client.post(f"{API}/projects/{b['slug']}/sessions", json={"session_id": "s-5"})
    assert r.status_code == 409 and "error" in r.json()


# ------------------------------------------------------- journal and todos


def test_journal_entries_are_the_persons(client):
    p = make_elsewhere(client)
    r = client.post(f"{API}/projects/{p['slug']}/journal", json={"text": "Shipped to TestFlight."})
    assert r.status_code == 201
    entry = r.json()["entry"]
    assert entry["kind"] == "note" and entry["text"] == "Shipped to TestFlight."
    row = db.get_journal(entry["id"])
    assert row["author"] == "user" and row["person_id"] == people.owner()["id"]


def test_todos_round_trip(client):
    p = make_elsewhere(client)
    url = f"{API}/projects/{p['slug']}/todos"
    t = client.post(url, json={"text": "Add an app icon", "owner": "user"}).json()["todo"]
    assert t["owner"] == "user" and t["done"] is False
    assert client.post(url, json={"text": "x", "owner": "robot"}).status_code == 400
    assert client.patch(f"{url}/{t['id']}", json={"text": "Add the app icon"}).json()["todo"]["text"] == "Add the app icon"
    assert client.patch(f"{url}/{t['id']}", json={"done": True}).json()["todo"]["done"] is True
    assert [x["id"] for x in client.get(url).json()["todos"]] == [t["id"]]
    other = db.create_project("Other", slug="other", stage="active")
    assert client.patch(f"{API}/projects/other/todos/{t['id']}", json={"done": False}).status_code == 404
    assert other


# ---------------------------------------------------------------- questions


def test_a_question_is_filed_notified_and_pollable(client, _quiet):
    p = make_elsewhere(client)
    r = client.post(f"{API}/projects/{p['slug']}/questions",
                    json={"question": "Ship to TestFlight or the App Store?",
                          "options": ["TestFlight", "App Store"]})
    assert r.status_code == 201
    q = r.json()["question"]
    assert q["status"] == "open" and q["answer"] is None
    assert q["options"] == ["TestFlight", "App Store"]
    assert [s[0] for s in _quiet] == ["New question"]

    db.answer_question_and_resume(q["id"], "TestFlight")
    polled = client.get(f"{API}/questions/{q['id']}").json()["question"]
    assert polled == {**q, "status": "answered", "answer": "TestFlight"}

    again = client.post(f"{API}/projects/{p['slug']}/questions",
                        json={"question": "Ship to TestFlight or the App Store?"})
    assert again.status_code == 200
    assert again.json()["question"]["answer"] == "TestFlight"
    assert len(_quiet) == 1
    assert client.get(f"{API}/questions/99999").status_code == 404


# ------------------------------------------------------------------- context


def test_context_puts_what_is_new_first(client):
    p = make_elsewhere(client)
    pid = db.get_project_by_slug(p["slug"])["id"]
    db.add_journal(pid, "user", "note", "an old note")
    db.get_conn().execute("UPDATE journal SET ts = '2026-01-01T00:00:00+00:00'")
    db.get_conn().commit()
    db.add_journal(pid, "user", "note", "Use the system haptics.")
    q = db.create_question(pid, "Dark mode too?", quick_options='["yes", "no"]')
    asked = db.create_question(pid, "Which font?")
    db.answer_question_and_resume(asked["id"], "SF Mono")

    ctx = client.get(f"{API}/projects/{p['slug']}/context",
                     params={"since": "2026-06-01T00:00:00Z"}).json()
    md = ctx["markdown"]
    assert ctx["project"]["slug"] == p["slug"]
    assert md.startswith("# Metronome iOS")
    new = md.index("## New since your last session")
    assert "Use the system haptics." in md[new:]
    assert "SF Mono" in md[new:]
    assert "an old note" not in md[new: md.index("## Open questions")]
    assert f"(#{q['id']}) Dark mode too? (options: yes, no)" in md
    assert new < md.index("## Open questions") < md.index("## What it is")
    assert f"{HOST}:{PATH}" in md
    assert "project_files" not in md  # there are no files here to browse


def test_context_names_the_parent(client):
    db.create_project("Metronome", slug="metronome", stage="active",
                      description="The whole click-track family.")
    p = make_elsewhere(client, parent="metronome")
    md = client.get(f"{API}/projects/{p['slug']}/context").json()["markdown"]
    assert "## Its parent: Metronome (`metronome`)" in md
    assert "The whole click-track family." in md


def test_context_stays_short_by_cutting_the_journal(client):
    p = make_elsewhere(client)
    pid = db.get_project_by_slug(p["slug"])["id"]
    for i in range(12):
        db.add_journal(pid, "agent", "progress", f"## Entry {i}\n\n" + "words " * 300)
    db.add_todo(pid, "Keep this todo")
    db.update_project(pid, description="A long brief. " * 150)
    whole = crossproject.render(db.get_project(pid))
    assert len(whole) > claudeapp.CONTEXT_CAP  # or this test proves nothing
    md = client.get(f"{API}/projects/{p['slug']}/context").json()["markdown"]
    assert len(md) <= claudeapp.CONTEXT_CAP
    assert "Keep this todo" in md
    assert "## Entry 11" in md  # the newest entries are the ones kept


def test_context_since_includes_related_runs(client):
    db.create_project("Metronome Web", slug="metronome-web", stage="active")
    p = make_elsewhere(client, title="Metronome iOS")
    sibling = db.get_project_by_slug("metronome-web")
    db.add_journal(sibling["id"], "agent", "progress", "## Tempo map exported as JSON\n\nbody")
    pid = db.get_project_by_slug(p["slug"])["id"]
    db.link_projects(pid, sibling["id"])
    md = client.get(f"{API}/projects/{p['slug']}/context",
                    params={"since": "2020-01-01T00:00:00Z"}).json()["markdown"]
    assert "`metronome-web`: Tempo map exported as JSON" in md


# ------------------------------------------------- decision 1: never scheduled


def test_the_scheduler_never_picks_a_project_hosted_elsewhere(client):
    p = make_elsewhere(client)
    pid = db.get_project_by_slug(p["slug"])["id"]
    assert pid not in {int(r["id"]) for r in db.list_schedulable_projects()}
    assert worker._pick_project(None) == (None, False)  # noqa: SLF001
    checkout(p["slug"])
    assert pid in {int(r["id"]) for r in db.list_schedulable_projects()}


def test_a_research_burst_skips_it(client):
    p = make_elsewhere(client)
    pid = db.get_project_by_slug(p["slug"])["id"]
    db.queue_research(pid)
    assert db.list_research_queued() == []


def test_run_now_on_it_starts_nothing(client, monkeypatch):
    """Not even a person's request: and the refused request must not fall
    through to a scheduled pick of some other project."""
    p = make_elsewhere(client)
    db.create_project("Something else", slug="else", stage="active")
    pid = db.get_project_by_slug(p["slug"])["id"]
    started = []
    monkeypatch.setattr(worker, "spawn_run", lambda proj, task: started.append(proj["id"]) or 1)
    asyncio.run(worker.manual_queue.put(pid))
    assert asyncio.run(worker._start_one()) is False  # noqa: SLF001
    assert started == []


@pytest.mark.asyncio
async def test_a_note_does_not_wake_it(client):
    p = make_elsewhere(client)
    pid = db.get_project_by_slug(p["slug"])["id"]
    db.update_project(pid, stage="review")
    db.add_journal(pid, "user", "note", "One more thing.")
    row = db.get_project(pid)
    assert await worker.reactivate_on_note(row) is False
    assert await worker.note_arrived(row) is False
    assert db.get_project(pid)["stage"] == "review"
    assert queued() == []
    db.update_project(pid, stage="active")
    assert worker.can_run_now(db.get_project(pid)) is False
    assert await worker._rerun_for_unseen_notes(db.get_project(pid)) is False  # noqa: SLF001
    assert queued() == []


@pytest.mark.asyncio
async def test_answering_its_question_queues_no_run(client):
    p = make_elsewhere(client)
    pid = db.get_project_by_slug(p["slug"])["id"]
    q = db.create_question(pid, "Which font?")
    db.answer_question_and_resume(q["id"], "SF Mono")
    assert await worker.answer_arrived(db.get_question(q["id"])) is False
    assert queued() == []


@pytest.mark.asyncio
async def test_the_same_project_built_here_still_wakes(client):
    """The guard is about where the code is, not about having a location."""
    p = make_elsewhere(client)
    checkout(p["slug"])
    pid = db.get_project_by_slug(p["slug"])["id"]
    db.update_project(pid, stage="review")
    assert await worker.reactivate_on_note(db.get_project(pid)) is True
    assert queued() == [pid]


# ------------------------------------------------------- other projects reading


def test_other_projects_can_read_it_but_are_told_where_the_files_are(client):
    p = make_elsewhere(client)
    reader = db.create_project("Reader", slug="reader", stage="active")
    digest = crossproject.digest(int(reader["id"]), p["slug"])
    assert f"`{HOST}:{PATH}`" in digest
    assert "**Workspace:**" not in digest
    with pytest.raises(crossproject.Denied) as err:
        crossproject.browse(int(reader["id"]), p["slug"])
    assert "not built on this server" in str(err.value) and HOST in str(err.value)


# ------------------------------------------------------------ the portal page


def test_the_page_shows_where_it_lives_and_offers_no_run(client):
    p = make_elsewhere(client)
    client.post(f"{API}/projects/{p['slug']}/sessions",
                json={"session_id": "s-9", "title": "Tap tempo", "path": PATH,
                      "started_at": "2026-10-02T10:00:00Z"})
    html = client.get(f"/project/{p['slug']}").text
    assert 'id="claude-app"' in html
    assert f"{HOST}:{PATH}" in html and "Tap tempo" in html
    assert f"built in the Claude app on {HOST}" in html
    assert f'action="/project/{p["slug"]}/run"' not in html
    assert 'id="workspace"' not in html
    assert "queue research burst" not in html and "add &amp; run now" not in html


def test_a_project_nobody_linked_renders_as_before(client):
    db.create_project("Plain", slug="plain", stage="active")
    html = client.get("/project/plain").text
    assert 'id="claude-app"' not in html
    assert 'action="/project/plain/run"' in html and 'id="workspace"' in html
    assert "queue research burst" in html

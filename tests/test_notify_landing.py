"""Where a tapped notification lands (app/notify.py `landing`, app/webpush.py
`navigate_url`).

Wes, 2026-09-08: "tapping notifications on a smart device that open the web
app should take the user directly to the relevant project and highlight the
question, if possible." Before this every push carried the portal's root as
its navigate URL, so a tap opened the dashboard and left him to find the
question himself.
"""
from __future__ import annotations

import json
import time

import pytest

from app import config, db, netinfo, notify, webpush


def _project():
    return db.create_project("Fridge Board", "an idea", slug="fridge-board", stage="active")


def test_a_question_lands_on_its_projects_page_at_the_question():
    project = _project()
    q = db.create_question(project["id"], "Which color?")
    assert notify.landing(q["id"], project["id"]) == f"/project/fridge-board#question-{q['id']}"


def test_a_question_finds_its_project_by_itself():
    project = _project()
    q = db.create_question(project["id"], "Which color?")
    assert notify.landing(q["id"], None) == f"/project/fridge-board#question-{q['id']}"


def test_anything_else_about_a_project_lands_on_the_project():
    project = _project()
    assert notify.landing(None, project["id"]) == "/project/fridge-board"


def test_nothing_in_particular_lands_on_the_dashboard():
    assert notify.landing(None, None) == ""
    assert notify.landing(None, 9999) == ""
    assert notify.landing(9999, None) == ""


def test_navigate_url_joins_the_path_onto_the_reachable_address(temp_data_dir):
    netinfo.store({
        "fetched_at": int(time.time()), "lan_url": "http://testhost:8500/", "https": True,
        "https_url": "https://testhost.tailnet1234.ts.net/", "self": None, "peers": [], "acl_known": False,
    })
    assert webpush.navigate_url("") == "https://testhost.tailnet1234.ts.net/"
    assert webpush.navigate_url("/project/x#question-3") == "https://testhost.tailnet1234.ts.net/project/x#question-3"
    assert webpush.navigate_url("project/x") == "https://testhost.tailnet1234.ts.net/project/x"
    assert webpush.navigate_url("https://elsewhere/p") == "https://elsewhere/p"


@pytest.mark.anyio
async def test_push_to_puts_the_landing_page_in_the_payload(monkeypatch):
    bodies: list[dict] = []

    async def fake_send(sub, body, urgency):
        bodies.append(json.loads(body.decode()))
        return True

    monkeypatch.setattr(webpush, "send_one", fake_send)
    subs = [{"endpoint": "https://push.example/1", "person_id": None}]
    await webpush.push_to(subs, "New question", "Which?", navigate="/project/fridge-board#question-4")
    assert bodies[0]["notification"]["navigate"] == f"http://{config.HOST_LABEL}:{config.PORT}/project/fridge-board#question-4"
    bodies.clear()
    await webpush.push_to(subs, "Hello", "Nothing in particular")
    assert bodies[0]["notification"]["navigate"] == f"http://{config.HOST_LABEL}:{config.PORT}/"


@pytest.mark.anyio
async def test_notify_hands_the_landing_page_to_web_push(monkeypatch):
    project = _project()
    q = db.create_question(project["id"], "Which color?")
    db.add_push_subscription("https://push.example/1", "k", "a", person_id=None) if hasattr(db, "add_push_subscription") else None
    seen: list[dict] = []

    async def fake_push(subs, title, message, urgency="normal", badges=None, actions=None, navigate=""):
        seen.append({"navigate": navigate, "title": title})
        return 0

    monkeypatch.setattr(webpush, "push_to", fake_push)
    await notify.notify("New question", "Which color?", question_id=q["id"], project_id=project["id"])
    assert seen[0]["navigate"] == f"/project/fridge-board#question-{q['id']}"
    await notify.notify("Run finished", "done", project_id=project["id"])
    assert seen[1]["navigate"] == "/project/fridge-board"
    await notify.notify("Proposal", "x", project_id=project["id"], navigate="/proposals/3")
    assert seen[2]["navigate"] == "/proposals/3"

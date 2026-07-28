"""Dashboard shelves, pausing, and questions that are not pressing - under the
redesigned state model (docs/state-model.md, approved by Wes 2026-07-22).

One user-owned `stage` (backlog | active | review | done | abandoned), an
orthogonal `paused` timestamp only Wes sets, and agent facts (`build_requested`,
`blocked_on`) beside it. Shelving is arithmetic: paused, blocked, awaiting a
build OK or holding an open question folds an active project to the Paused
shelf; everything else sits on its stage's own shelf.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _project(title, slug, stage="active", build_approved=True):
    return db.create_project(
        title, description="x", stage=stage, slug=slug, build_approved=build_approved
    )


# --- pausing is orthogonal to the stage -------------------------------------

def test_the_picker_stamps_a_pause_without_touching_the_stage(client):
    p = _project("Fridge", "fridge")
    client.post("/project/fridge/status", data={"status": "paused"})

    row = db.get_project(p["id"])
    assert row["stage"] == "active"
    assert row["paused"]
    assert db.is_paused(row)
    assert db.display_state(row) == "paused"


def test_the_old_status_vocabulary_still_works_on_the_route(client):
    """`waiting_user` is baked into old bookmarks and any page rendered before
    a deploy - it must keep meaning what choosing it meant: a pause."""
    p = _project("Fridge", "fridge")
    client.post("/project/fridge/status", data={"status": "waiting_user"})
    assert db.is_paused(db.get_project(p["id"]))

    client.post("/project/fridge/status", data={"status": "inbox"})
    row = db.get_project(p["id"])
    assert row["stage"] == "backlog"
    assert not db.is_paused(row)


def test_an_agent_blocked_on_wes_is_not_a_pause():
    """The old model's `waiting_user` overload, split: an agent blocked on Wes
    records `blocked_on`, which folds the card away on the dashboard but keeps
    its questions loud."""
    p = _project("Fridge", "fridge")
    db.update_project(p["id"], blocked_on="a credential only the owner has")

    row = db.get_project(p["id"])
    assert row["stage"] == "active"
    assert not db.is_paused(row)
    assert db.project_shelf(row) == "paused"
    assert row["id"] not in db.shelved_project_ids()


def test_choosing_a_stage_clears_the_pause():
    """Choosing a shelf IS the unpause - and it is Wes's own action, which is
    the only thing allowed to lift his pause."""
    p = _project("Fridge", "fridge")
    db.pause_project(p["id"])
    assert db.is_paused(db.get_project(p["id"]))

    db.set_user_state(db.get_project(p["id"]), "active")
    assert not db.is_paused(db.get_project(p["id"]))


def test_an_agent_stage_move_leaves_the_pause_alone():
    """An agent finishing its chunk (stage -> review) must not quietly unpark a
    project Wes put down."""
    p = _project("Fridge", "fridge")
    db.pause_project(p["id"])
    db.update_project(p["id"], stage="review")
    assert db.is_paused(db.get_project(p["id"]))


def test_a_non_stage_update_leaves_the_pause_alone():
    p = _project("Fridge", "fridge")
    db.pause_project(p["id"])
    db.update_project(p["id"], priority=9)
    assert db.is_paused(db.get_project(p["id"]))


# --- the shelf arithmetic ---------------------------------------------------

def test_every_state_lands_on_its_shelf():
    p = _project("Fridge", "fridge")
    for stage, shelf in [
        ("backlog", "backlog"),
        ("active", "active"),
        ("review", "review"),
        ("done", "done"),
        ("abandoned", "done"),
    ]:
        db.update_project(p["id"], stage=stage)
        assert db.project_shelf(db.get_project(p["id"])) == shelf, stage


def test_anything_waiting_on_wes_folds_to_the_paused_shelf():
    """"Even if they need user input or something, I don't care - I want them
    in the paused/backlog section!" An active project waiting on Wes for any
    reason - his pause, a block, an unanswered build request, an open question -
    is folded away."""
    p = _project("Fridge", "fridge")
    db.pause_project(p["id"])
    assert db.project_shelf(db.get_project(p["id"])) == "paused"

    p2 = _project("Blocked", "blocked")
    db.update_project(p2["id"], blocked_on="an SSH key")
    assert db.project_shelf(db.get_project(p2["id"])) == "paused"

    p3 = _project("Gated", "gated", build_approved=False)
    db.update_project(p3["id"], build_requested=1)
    assert db.project_shelf(db.get_project(p3["id"])) == "paused"

    p4 = _project("Asking", "asking")
    assert db.project_shelf(db.get_project(p4["id"]), open_questions=1) == "paused"


def test_a_review_project_with_a_question_stays_on_review():
    """Review already means "your turn" - folding it into Paused would hide it
    from the one shelf that exists to be looked at."""
    p = _project("Read Me", "readme", stage="review")
    assert db.project_shelf(db.get_project(p["id"]), open_questions=1) == "review"


def test_a_wes_pause_beats_every_other_shelf():
    p = _project("Read Me", "readme", stage="review")
    db.pause_project(p["id"])
    assert db.project_shelf(db.get_project(p["id"])) == "paused"


def test_shelved_ids_are_paused_and_backlog_only():
    live = _project("Live", "live")
    blocked = _project("Blocked", "blocked")
    db.update_project(blocked["id"], blocked_on="something")
    paused = _project("Paused", "paused")
    db.pause_project(paused["id"])
    backlog = _project("Backlog", "backlog", stage="backlog")

    assert db.shelved_project_ids() == {paused["id"], backlog["id"]}
    assert live["id"] not in db.shelved_project_ids()
    assert blocked["id"] not in db.shelved_project_ids()


# --- the dashboard ----------------------------------------------------------

def test_the_dashboard_splits_into_state_sections(client):
    """Active on top, review under it, then paused and backlog, with
    done/abandoned collapsed at the end."""
    _project("Live One", "live")
    _project("Read Me", "readme", stage="review")
    db.pause_project(_project("Put Down", "putdown")["id"])
    _project("Not Started", "notstarted", stage="backlog")

    html = client.get("/").text
    assert "<h2>Active</h2>" in html
    assert "<h2>Review</h2>" in html
    # The put-down shelves are <details>, shut by default.
    assert 'id="paused"' in html and 'id="backlog"' in html
    assert "1 waiting or put down" in html
    assert "1 not started" in html
    # Everything is still on the page - folded, not dropped - in section order.
    assert (
        html.index("Live One")
        < html.index("Read Me")
        < html.index("Put Down")
        < html.index("Not Started")
    )


def test_a_review_project_sits_in_the_review_section_not_active(client):
    _project("Read Me", "readme", stage="review")
    html = client.get("/").text
    active = html[html.index("<h2>Active</h2>"):html.index("<h2>Review</h2>")]
    assert "Read Me" not in active
    assert "Read Me" in html[html.index("<h2>Review</h2>"):]


def test_blocked_and_asking_projects_sit_in_the_paused_shelf(client):
    """His 06:02 note, after seeing them at the top twice: "All of these paused
    tasks are still in the top section with the building stuff. Even if they
    need user input or something, I don't care - I want them in the
    paused/backlog section!" Nobody-running + waiting = folded away."""
    _project("Live One", "live")
    blocked = _project("Parked", "parked")
    db.update_project(blocked["id"], blocked_on="waiting on a part")
    asking = _project("Asking", "asking")
    db.create_question(asking["id"], "Which color?")

    html = client.get("/").text
    active = html[html.index("<h2>Active</h2>"):html.index("<h2>Review</h2>")]
    assert "Parked" not in active
    assert "Asking" not in active
    shelf = html[html.index('id="paused"'):html.index('id="backlog"')]
    assert "Parked" in shelf
    assert "Asking" in shelf
    assert "2 waiting or put down" in shelf


def test_a_blocked_card_wears_a_blocked_badge(client):
    p = _project("Parked", "parked")
    db.update_project(p["id"], blocked_on="waiting on a part")
    html = client.get("/").text
    assert ">blocked</span>" in html
    assert 'title="waiting on a part"' in html


def test_every_section_is_a_drop_zone_with_its_state(client):
    """Dragging a card onto a section posts that section's state; the mapping
    lives on the zone element so app.js never holds a second copy of it."""
    _project("Live One", "live")
    db.update_project(_project("Old", "old")["id"], stage="done")
    html = client.get("/").text
    for state in ("active", "review", "paused", "backlog", "done"):
        assert f'data-status-zone="{state}"' in html, state
    # And the cards carry what the drag and the menu need.
    assert 'data-slug="live"' in html
    assert 'draggable="true"' in html


def test_dropping_a_card_on_a_zone_applies_its_state(client):
    p = _project("Live One", "live")
    client.post("/project/live/status", data={"status": "review"})
    assert db.get_project(p["id"])["stage"] == "review"

    client.post("/project/live/status", data={"status": "active"})
    row = db.get_project(p["id"])
    assert row["stage"] == "active"
    assert row["build_approved"] == 1  # dropping into Active is the approval


def test_the_put_down_shelves_render_even_empty(client):
    """An empty shelf used to be dropped from the page; now it is a drop
    target, so it must exist to be dragged into."""
    _project("Live One", "live")
    html = client.get("/").text
    assert 'id="paused"' in html
    assert 'id="backlog"' in html


def test_a_running_project_is_never_shelved(client, monkeypatch):
    """Work in flight outranks "you put it down" - a paused project an agent is
    actually running on belongs at the top, not folded away."""
    from app import main

    paused = _project("Put Down", "putdown")
    db.pause_project(paused["id"])
    snap = dict(main.active_run_snapshot())
    snap["project_ids"] = [paused["id"]]
    monkeypatch.setattr(main, "active_run_snapshot", lambda: snap)

    html = client.get("/").text
    # The card is up in the Active section, not folded inside the shelf.
    assert html.index("Put Down") < html.index('id="paused"')


def test_the_header_carries_no_run_count(client):
    """Wes's 2026-07-23 note: the "N active / X/Y runs today" readout came off
    the dashboard's top-right header. The base template's default shows."""
    _project("Live One", "live")
    db.pause_project(_project("Put Down", "putdown")["id"])
    html = client.get("/").text
    assert "active &middot;" not in html
    # The default header stat is this install's own user@host label.
    assert config.SITE.handle in html


# --- the question badge -----------------------------------------------------

def test_a_manual_pause_takes_its_questions_out_of_the_badge(client):
    p = _project("Fridge", "fridge")
    db.create_question(p["id"], "Which color?")
    assert client.get("/").text.count('class="nav-count"') == 1

    db.pause_project(p["id"])
    html = client.get("/").text
    assert 'class="nav-count"' not in html


def test_a_blocked_projects_question_still_counts(client):
    """The distinction Wes drew explicitly: a project waiting on him because an
    agent asked something is exactly what the badge is for."""
    p = _project("Fridge", "fridge")
    db.create_question(p["id"], "Which color?")
    db.update_project(p["id"], blocked_on="an answer")

    html = client.get("/").text
    assert 'class="nav-count">1<' in html


def test_a_backlog_projects_questions_are_not_in_the_badge(client):
    p = _project("Fridge", "fridge", stage="backlog")
    db.create_question(p["id"], "Which color?")
    assert 'class="nav-count"' not in client.get("/").text


def test_the_dashboard_banner_matches_the_badge(client):
    """The "N open questions waiting on you" banner counts the same things the
    badge does, or the two disagree on the same screen."""
    p = _project("Fridge", "fridge")
    db.create_question(p["id"], "Which color?")
    db.pause_project(p["id"])
    html = client.get("/").text
    assert "banner-alert" not in html
    # The card itself keeps its count - the question has not gone anywhere,
    # it just stopped being something the top of the page shouts about.
    assert 'title="1 open question"' in html


# --- the questions page -----------------------------------------------------

def test_shelved_questions_get_their_own_section_below(client):
    live = _project("Live", "live")
    db.create_question(live["id"], "Pressing thing?")
    paused = _project("Paused", "paused")
    db.create_question(paused["id"], "Resting thing?")
    db.pause_project(paused["id"])

    html = client.get("/questions").text
    assert "Resting thing?" in html
    assert "Paused &amp; backlog" in html
    # Order matters: the pressing one is above the fold.
    assert html.index("Pressing thing?") < html.index("Resting thing?")


def test_a_shelved_question_is_still_answerable(client):
    paused = _project("Paused", "paused")
    q = db.create_question(paused["id"], "Resting thing?")
    db.pause_project(paused["id"])

    html = client.get("/questions").text
    assert f'action="/questions/{q["id"]}/answer"' in html

    r = client.post(f"/questions/{q['id']}/answer", data={"answer": "yes", "next": "/questions"})
    assert r.status_code == 200
    assert db.get_question(q["id"])["status"] == "answered"


def test_only_shelved_questions_says_so_rather_than_nothing_waiting(client):
    """"Nothing waiting on you" would be a lie with a question folded below."""
    paused = _project("Paused", "paused")
    db.create_question(paused["id"], "Resting thing?")
    db.pause_project(paused["id"])

    html = client.get("/questions").text
    assert "Nothing waiting on you" not in html
    assert "Nothing pressing" in html


def test_a_genuinely_empty_questions_page_is_unchanged(client):
    html = client.get("/questions").text
    assert "Nothing waiting on you" in html
    assert "Paused &amp; backlog" not in html


# --- the other way in -------------------------------------------------------

def test_pausing_over_telegram_is_also_a_real_pause(monkeypatch):
    """Two doors into the same room. If only the picker stamped the flag, a
    project Wes paused from his phone would keep shouting at him."""
    import asyncio

    from app import notify, telegram_bot

    p = _project("Fridge", "fridge")
    monkeypatch.setattr(notify, "send_telegram_text", _noop_async)
    monkeypatch.setattr(telegram_bot.notify, "send_telegram_text", _noop_async)

    asyncio.run(
        telegram_bot._dispatch_intent(
            {
                "intent": "set_status",
                "project_slug": "fridge",
                "status": "paused",
                "confidence": 1.0,
            },
            "pause the fridge project",
            "1",
        )
    )
    assert db.is_paused(db.get_project(p["id"]))


async def _noop_async(*args, **kwargs):
    return None

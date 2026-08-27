"""A question answerable from the lock screen, and a count on the app icon.

RESEARCH.md §2 ranked "push notification tied to a decision" as the portal's
biggest gap, and half of it was already here: the portal has spoken Declarative
Web Push since the enrollment surface was built. What it sent was a title, a
body and a URL - so a question reached Wes's lock screen and answering it still
meant opening the app, finding the question and tapping an option.

Two fields close that:

- `app_badge`, the number on the Home Screen icon, so the phone says how many
  decisions are waiting without being opened at all;
- `actions`, real buttons on the notification, each navigating to a page that
  submits the answer for you.

The things worth pinning are mostly refusals. A badge must never show one
person another person's number. A four-way choice must never be truncated down
to the two buttons that fit, because a tap on a truncated list is a decision
made without seeing the alternatives. A payload must never quietly grow past
what a push service will accept. And the URL those buttons navigate to must not
answer anything by being fetched.
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from app import db, notify, people, quickreplies, scope, webpush


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def notification(body: bytes) -> dict:
    """The notification object out of a declarative payload."""
    return json.loads(body.decode())["notification"]


# --- what the payload carries ----------------------------------------------

def test_a_payload_with_no_badge_omits_the_field_rather_than_sending_zero():
    """Omitted and zero are different instructions to iOS: zero clears the
    icon, absent leaves it alone. A caller that does not know whose device it
    is must not clear somebody's real count."""
    n = notification(webpush.payload("T", "body", "https://x/"))
    assert "app_badge" not in n
    assert "actions" not in n


def test_a_zero_badge_is_sent_and_clears_the_icon():
    assert notification(webpush.payload("T", "b", "https://x/", 0))["app_badge"] == 0


def test_a_negative_badge_never_reaches_the_wire():
    assert notification(webpush.payload("T", "b", "https://x/", -3))["app_badge"] == 0


def test_the_payload_keeps_the_declarative_shape_with_the_new_fields():
    body = webpush.payload(
        "Question", "Merge it?", "https://x/", 2,
        [{"action": "q1-0", "title": "yes", "navigate": "https://x/questions/1/tap?opt=0"}],
    )
    data = json.loads(body.decode())
    # 8030 is what marks this as Declarative Web Push; iOS ignores the payload
    # entirely without it and the notification never appears.
    assert data["web_push"] == 8030
    n = data["notification"]
    assert n["title"] == "Question" and n["body"] == "Merge it?"
    assert n["app_badge"] == 2
    assert n["actions"][0]["title"] == "yes"


# --- the size guard ---------------------------------------------------------

def test_an_oversized_payload_drops_its_buttons_before_its_words():
    """A push service refuses an oversized body outright, so growing past the
    record means the notification is not delivered at all. Buttons cost a tap;
    a dropped notification costs the whole message."""
    long_body = "x" * (webpush.MAX_PLAINTEXT - 120)
    actions = [
        {"action": "q1-0", "title": "yes", "navigate": "https://x/questions/1/tap?opt=0"},
        {"action": "q1-1", "title": "no", "navigate": "https://x/questions/1/tap?opt=1"},
    ]
    out = webpush.payload("T", long_body, "https://x/", 1, actions)
    assert len(out) <= webpush.MAX_PLAINTEXT
    n = notification(out)
    assert "actions" not in n
    # The words survived intact - only the buttons were spent.
    assert n["body"] == long_body


def test_a_body_too_long_even_without_buttons_is_trimmed_to_fit():
    out = webpush.payload("T", "y" * (webpush.MAX_PLAINTEXT * 2), "https://x/")
    assert len(out) <= webpush.MAX_PLAINTEXT
    assert notification(out)["body"].endswith("…")


def test_a_payload_that_already_fits_is_left_exactly_alone():
    actions = [{"action": "a", "title": "yes", "navigate": "https://x/t"}]
    n = notification(webpush.payload("T", "short", "https://x/", 1, actions))
    assert n["body"] == "short"
    assert len(n["actions"]) == 1


# --- which buttons a notification gets --------------------------------------

def test_two_options_become_two_buttons_pointing_at_their_own_index():
    acts = quickreplies.push_actions(7, ["merge it", "keep both"], "https://x.ts.net/")
    assert [a["title"] for a in acts] == ["merge it", "keep both"]
    assert acts[0]["navigate"] == "https://x.ts.net/questions/7/tap?opt=0"
    assert acts[1]["navigate"] == "https://x.ts.net/questions/7/tap?opt=1"
    # Distinct ids, or the service worker cannot tell which was tapped.
    assert len({a["action"] for a in acts}) == 2


def test_more_options_than_fit_get_no_buttons_at_all():
    """Not the first two. Showing "merge it" and "keep both" from a four-way
    choice invites a one-tap answer from somebody who never saw the other two -
    an uninformed tap recorded as a decision."""
    assert quickreplies.push_actions(7, ["a", "b", "c"], "https://x/") == []
    assert quickreplies.push_actions(7, ["a", "b", "c", "d"], "https://x/") == []


def test_a_question_with_no_options_gets_no_buttons():
    assert quickreplies.push_actions(7, [], "https://x/") == []


def test_skip_is_not_offered_on_a_lock_screen():
    """It costs one of the two slots and does the same thing dismissing the
    notification already does."""
    acts = quickreplies.push_actions(7, ["yes", "no"], "https://x/")
    assert quickreplies.SKIP not in [a["title"] for a in acts]


def test_a_base_url_without_a_trailing_slash_still_builds_one_path():
    acts = quickreplies.push_actions(3, ["yes", "no"], "https://x.ts.net")
    assert acts[0]["navigate"] == "https://x.ts.net/questions/3/tap?opt=0"


# --- the badge is per person ------------------------------------------------

@pytest.fixture
def board(client):
    """His question, her question, and a shared project with one more."""
    her_id = people.add("Erin", gender="female", background="Newer to all of this.")
    his_id = int(people.owner()["id"])

    his = int(db.create_project("His Thing", stage="active")["id"])
    hers = int(db.create_project("Her Thing", stage="active")["id"])
    both = int(db.create_project("Shared Thing", stage="active")["id"])
    people.set_members(hers, [her_id])
    people.set_members(both, [his_id, her_id])

    db.create_question(his, "his only?")
    db.create_question(hers, "hers only?")
    db.create_question(both, "shared?")
    return {"his_id": his_id, "her_id": her_id, "his": his, "hers": hers, "both": both}


def test_each_person_is_counted_only_their_own_questions(board):
    badges = notify.question_badges()
    # He is on his own project and the shared one; she is on hers and the
    # shared one. Neither sees the other's.
    assert badges[board["his_id"]] == 2
    assert badges[board["her_id"]] == 2


def test_a_question_on_a_paused_project_is_not_waiting_on_anybody(board):
    db.pause_project(board["his"])
    assert notify.question_badges()[board["his_id"]] == 1


def test_the_nav_badge_and_the_icon_badge_count_the_same_thing(board, client):
    """One rule, two surfaces. An icon saying 3 that opens onto a list of 1 is
    worse than an icon saying nothing."""
    from app import main

    owner = people.owner()
    assert len(scope.pending_questions(owner)) == notify.question_badges()[board["his_id"]]


def test_a_device_with_no_person_is_sent_no_badge_at_all(board, monkeypatch):
    """An unattributed device receives everything, so no count is right for it.
    Sending none leaves its icon as it was rather than confidently wrong."""
    sent: list[bytes] = []

    async def fake_send_one(sub, body, urgency="normal"):
        sent.append(body)
        return True

    monkeypatch.setattr(webpush, "send_one", fake_send_one)
    import anyio

    subs = [
        {"endpoint": "https://push/1", "p256dh": "x", "auth": "y", "person_id": board["his_id"]},
        {"endpoint": "https://push/2", "p256dh": "x", "auth": "y", "person_id": None},
    ]
    anyio.run(
        lambda: webpush.push_to(subs, "T", "b", "normal", notify.question_badges())
    )
    assert notification(sent[0])["app_badge"] == 2
    assert "app_badge" not in notification(sent[1])


def test_two_people_on_one_send_get_their_own_numbers(board, monkeypatch):
    seen: dict[str, bytes] = {}

    async def fake_send_one(sub, body, urgency="normal"):
        seen[sub["endpoint"]] = body
        return True

    monkeypatch.setattr(webpush, "send_one", fake_send_one)
    import anyio

    # Give her one extra question so the two counts genuinely differ.
    db.create_question(board["hers"], "and another?")
    subs = [
        {"endpoint": "https://push/his", "p256dh": "x", "auth": "y", "person_id": board["his_id"]},
        {"endpoint": "https://push/hers", "p256dh": "x", "auth": "y", "person_id": board["her_id"]},
    ]
    anyio.run(
        lambda: webpush.push_to(subs, "T", "b", "normal", notify.question_badges())
    )
    assert notification(seen["https://push/his"])["app_badge"] == 2
    assert notification(seen["https://push/hers"])["app_badge"] == 3


# --- the page a button lands on ---------------------------------------------

def test_tapping_a_button_does_not_answer_by_being_fetched(client):
    """The URL is a GET that Safari may preload and that sits in history. It
    renders a form; only the POST it fires answers anything."""
    project = db.create_project("Thing", stage="active")
    q = db.create_question(
        project["id"], "Merge it?", quick_options=quickreplies.encode(["merge it", "keep both"])
    )
    resp = client.get(f"/questions/{q['id']}/tap?opt=0")
    assert resp.status_code == 200
    assert "merge it" in resp.text
    # Untouched: still open, still unanswered.
    assert db.get_question(q["id"])["status"] == "open"


def test_the_landing_page_submits_the_offered_text_not_the_index(client):
    project = db.create_project("Thing", stage="active")
    q = db.create_question(
        project["id"], "Merge it?", quick_options=quickreplies.encode(["merge it", "keep both"])
    )
    body = client.get(f"/questions/{q['id']}/tap?opt=1").text
    assert 'name="choice" value="keep both"' in body
    assert f'action="/questions/{q["id"]}/answer"' in body


def test_the_form_it_renders_really_does_answer_the_question(client):
    project = db.create_project("Thing", stage="active")
    q = db.create_question(
        project["id"], "Merge it?", quick_options=quickreplies.encode(["merge it", "keep both"])
    )
    client.get(f"/questions/{q['id']}/tap?opt=0")
    resp = client.post(
        f"/questions/{q['id']}/answer", data={"choice": "merge it", "next": "/questions"}
    )
    assert resp.status_code in (200, 303)
    row = db.get_question(q["id"])
    assert row["status"] != "open"
    assert "merge it" in (row["answer"] or "")


def test_an_index_that_no_longer_maps_to_an_option_refuses_to_guess(client):
    """The question was re-asked with a different list, or the notification is
    old. Answering with whatever is at index 3 today would record a decision
    nobody made."""
    project = db.create_project("Thing", stage="active")
    q = db.create_question(
        project["id"], "Merge it?", quick_options=quickreplies.encode(["merge it", "keep both"])
    )
    body = client.get(f"/questions/{q['id']}/tap?opt=7").text
    assert "no longer maps to an answer" in body
    assert 'name="choice"' not in body


def test_a_non_numeric_index_refuses_the_same_way(client):
    project = db.create_project("Thing", stage="active")
    q = db.create_question(project["id"], "Merge it?", quick_options=quickreplies.encode(["yes"]))
    assert "no longer maps" in client.get(f"/questions/{q['id']}/tap?opt=nonsense").text


def test_a_stale_tap_cannot_overwrite_an_answer_already_given(client):
    """A phone can hold a notification for a day. Whoever answered first wins."""
    project = db.create_project("Thing", stage="active")
    q = db.create_question(
        project["id"], "Merge it?", quick_options=quickreplies.encode(["merge it", "keep both"])
    )
    db.answer_question_and_resume(q["id"], "keep both", None)
    body = client.get(f"/questions/{q['id']}/tap?opt=0").text
    assert "already been answered" in body
    assert 'name="choice"' not in body
    assert "keep both" in (db.get_question(q["id"])["answer"] or "")


def test_a_tap_on_a_question_that_does_not_exist_is_a_404(client):
    assert client.get("/questions/99999/tap?opt=0").status_code == 404


# --- the badge the page itself paints ---------------------------------------

def test_every_page_states_the_count_for_the_icon(client):
    """`navigator.setAppBadge` runs from app.js off this attribute, so opening
    the app corrects an icon a push left stale."""
    project = db.create_project("Thing", stage="active")
    db.create_question(project["id"], "one?")
    body = client.get("/").text
    assert 'data-open-questions="1"' in body


def test_answering_the_last_question_takes_the_number_off_the_page(client):
    project = db.create_project("Thing", stage="active")
    q = db.create_question(project["id"], "one?")
    db.answer_question_and_resume(q["id"], "done", None)
    assert 'data-open-questions="0"' in client.get("/").text

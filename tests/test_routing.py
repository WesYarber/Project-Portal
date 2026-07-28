"""A question for her does not go to his phone.

Wes's brief for per-person notifications was one sentence long and every test
here is a way of failing it. What is worth pinning, in the order it can go
wrong:

- **A one-person install is unchanged.** Every route resolves to the install's
  own topic, chat and devices. If this file only proved the new behavior it
  would be perfectly happy with a portal that had stopped notifying Wes.
- **Nobody goes quiet by default.** A person with no channels falls back to the
  install's. The strict alternative - route to their own or to nothing - turns
  adding a person into a mute switch, which is the quietest possible failure
  and the one Wes complains about most.
- **The fallback does not double-send.** Deduping happens on the concrete
  target, not on the person, so two people both falling back produce one send.
- **Attribution never guesses.** An unclaimed Telegram chat is nobody's, not
  the owner's, even where the owner is the only candidate.
"""
from __future__ import annotations

import pytest

from app import db, notify, people, routing, webpush


@pytest.fixture
def install_channels():
    """The install's own channels, as Wes has them set up."""
    db.set_setting("ntfy_url", "http://ntfy.example:8095")
    db.set_setting("ntfy_topic", "portal")
    db.set_setting("telegram_chat_id", "111")
    db.set_setting("telegram_token", "t:oken")
    db.set_setting("telegram_enabled", "1")


@pytest.fixture
def wes():
    return people.owner()


@pytest.fixture
def karli():
    return people.get(people.add(name="Karli", gender="female"))


# --------------------------------------------------------------------------
# One person: nothing changes
# --------------------------------------------------------------------------

def test_one_person_still_gets_the_installs_own_channels(install_channels, wes):
    rows = routing.recipients(None)
    assert [r["id"] for r in rows] == [wes["id"]]
    assert routing.ntfy_topics(rows, "portal") == ["portal"]
    assert routing.telegram_chats(rows, "111") == ["111"]


def test_a_project_with_one_member_routes_to_the_installs_channels(install_channels, wes):
    project = db.create_project(title="P", description="d")
    people.add_member(project["id"], int(wes["id"]))
    rows = routing.recipients(project["id"])
    assert routing.ntfy_topics(rows, "portal") == ["portal"]


# --------------------------------------------------------------------------
# Recipients
# --------------------------------------------------------------------------

def test_a_projects_members_are_its_recipients(wes, karli):
    hers = db.create_project(title="Hers", description="d")
    people.set_members(hers["id"], [int(karli["id"])])
    assert [r["id"] for r in routing.recipients(hers["id"])] == [karli["id"]]


def test_a_project_with_no_members_reaches_everybody(wes, karli):
    """A defensive branch, and deliberately kept as one.

    Two invariants currently make a memberless project unreachable:
    `db.create_project` always adds one, and `people.set_members` refuses to
    remove the last. But routing is not the layer that should depend on both of
    those still holding - a memberless project is an *unassigned* project, not
    a private one, and the cost of getting this wrong is a project that
    notifies nobody and says nothing about it. So the state is forced here
    rather than reached, which is the honest way to test a branch that exists
    for a state the code above it promises never to produce.
    """
    orphan = db.create_project(title="Nobody's", description="d")
    conn = db.get_conn()
    with db._LOCK:  # noqa: SLF001
        conn.execute("DELETE FROM project_people WHERE project_id = ?", (orphan["id"],))
        conn.commit()
    assert people.members(orphan["id"]) == []
    ids = {r["id"] for r in routing.recipients(orphan["id"])}
    assert ids == {wes["id"], karli["id"]}


def test_no_project_reaches_everybody(wes, karli):
    ids = {r["id"] for r in routing.recipients(None)}
    assert ids == {wes["id"], karli["id"]}


def test_archived_people_are_not_recipients(wes, karli):
    people.archive(int(karli["id"]))
    assert [r["id"] for r in routing.recipients(None)] == [wes["id"]]


# --------------------------------------------------------------------------
# Channels: the fallback, and the dedupe that makes it safe
# --------------------------------------------------------------------------

def test_a_person_with_their_own_topic_gets_only_it(install_channels, karli):
    people.update(int(karli["id"]), ntfy_topic="karli-portal")
    rows = [people.get(int(karli["id"]))]
    assert routing.ntfy_topics(rows, "portal") == ["karli-portal"]


def test_a_person_with_no_topic_falls_back_rather_than_going_quiet(install_channels, karli):
    rows = [people.get(int(karli["id"]))]
    assert routing.ntfy_topics(rows, "portal") == ["portal"]


def test_two_people_both_falling_back_send_once(install_channels, wes, karli):
    rows = routing.recipients(None)
    assert len(rows) == 2
    assert routing.ntfy_topics(rows, "portal") == ["portal"]


def test_a_mixed_pair_reaches_both_places(install_channels, wes, karli):
    people.update(int(karli["id"]), ntfy_topic="karli-portal")
    rows = routing.recipients(None)
    assert sorted(routing.ntfy_topics(rows, "portal")) == ["karli-portal", "portal"]


def test_an_install_with_no_topic_at_all_sends_nowhere(karli):
    """Empty everywhere is the one case that legitimately routes to nothing -
    an unconfigured channel, not a person the portal forgot about."""
    rows = routing.recipients(None)
    assert routing.ntfy_topics(rows, "") == []


def test_telegram_chats_follow_the_same_rule(install_channels, wes, karli):
    people.update(int(karli["id"]), telegram_chat_id="222")
    rows = routing.recipients(None)
    assert sorted(routing.telegram_chats(rows, "111")) == ["111", "222"]


# --------------------------------------------------------------------------
# Enrolled devices
# --------------------------------------------------------------------------

def _sub(endpoint: str, person_id=None):
    db.add_push_subscription(endpoint, "p", "a", ua="ua", person_id=person_id)


def test_a_device_with_no_person_receives_everything(wes, karli):
    _sub("https://push/old")
    hers = db.create_project(title="Hers", description="d")
    people.set_members(hers["id"], [int(karli["id"])])
    subs = routing.push_subscriptions(
        routing.recipients(hers["id"]), db.list_push_subscriptions()
    )
    assert [s["endpoint"] for s in subs] == ["https://push/old"]


def test_his_phone_does_not_get_her_projects_question(wes, karli):
    _sub("https://push/wes", person_id=int(wes["id"]))
    _sub("https://push/karli", person_id=int(karli["id"]))
    hers = db.create_project(title="Hers", description="d")
    people.set_members(hers["id"], [int(karli["id"])])
    subs = routing.push_subscriptions(
        routing.recipients(hers["id"]), db.list_push_subscriptions()
    )
    assert [s["endpoint"] for s in subs] == ["https://push/karli"]


def test_a_shared_project_reaches_both_phones(wes, karli):
    _sub("https://push/wes", person_id=int(wes["id"]))
    _sub("https://push/karli", person_id=int(karli["id"]))
    ours = db.create_project(title="Ours", description="d")
    people.set_members(ours["id"], [int(wes["id"]), int(karli["id"])])
    subs = routing.push_subscriptions(
        routing.recipients(ours["id"]), db.list_push_subscriptions()
    )
    assert len(subs) == 2


def test_re_enrolling_without_a_cookie_keeps_the_person_it_had(wes):
    """A browser that has lost its identity cookie may still refresh its push
    keys. Letting that write NULL over a good attribution would hand that phone
    everybody's notifications, silently and permanently."""
    _sub("https://push/wes", person_id=int(wes["id"]))
    _sub("https://push/wes", person_id=None)
    row = db.list_push_subscriptions()[0]
    assert row["person_id"] == wes["id"]


# --------------------------------------------------------------------------
# Telegram: who may speak, and who the portal thinks spoke
# --------------------------------------------------------------------------

def test_the_allowlist_admits_the_install_and_the_people(install_channels, wes, karli):
    people.update(int(karli["id"]), telegram_chat_id="222")
    assert routing.telegram_allowlist() == {"111", "222"}


def test_archiving_somebody_closes_the_bot_to_them(install_channels, karli):
    people.update(int(karli["id"]), telegram_chat_id="222")
    people.archive(int(karli["id"]))
    assert routing.telegram_allowlist() == {"111"}


def test_an_unconfigured_install_admits_nobody(wes):
    assert routing.telegram_allowlist() == set()


def test_a_chat_resolves_to_the_person_who_claimed_it(karli):
    people.update(int(karli["id"]), telegram_chat_id="222")
    assert people.by_telegram_chat_id("222")["id"] == karli["id"]


def test_an_unclaimed_chat_is_nobodys_not_the_owners(install_channels, wes):
    """The install's chat id is obviously his on a portal with one person in
    it. Resolving it to him anyway is how `name_of` would have behaved, and it
    is exactly the guess that sends the next agent to the wrong person - see
    people.known_name."""
    assert people.by_telegram_chat_id("111") is None


def test_an_empty_chat_id_matches_nobody(karli):
    assert people.by_telegram_chat_id("") is None
    assert people.by_telegram_chat_id("   ") is None


def test_a_retired_person_still_wrote_what_they_wrote(karli):
    """The allowlist shuts them out; attribution does not rewrite history."""
    people.update(int(karli["id"]), telegram_chat_id="222")
    people.archive(int(karli["id"]))
    assert people.by_telegram_chat_id("222")["id"] == karli["id"]


# --------------------------------------------------------------------------
# End to end through notify()
# --------------------------------------------------------------------------

@pytest.fixture
def wire(monkeypatch):
    """Record what would have gone out, per channel."""
    sent = {"ntfy": [], "telegram": [], "push": []}

    async def fake_ntfy(url, topic, title, message):
        sent["ntfy"].append(topic)

    async def fake_telegram(token, chat_id, text, question_id, record_msg_id=True):
        sent["telegram"].append(chat_id)

    async def fake_push(subs, title, message, urgency="normal"):
        sent["push"].extend(s["endpoint"] for s in subs)
        return len(subs)

    monkeypatch.setattr(notify, "_send_ntfy", fake_ntfy)
    monkeypatch.setattr(notify, "_send_telegram", fake_telegram)
    monkeypatch.setattr(webpush, "push_to", fake_push)
    return sent


@pytest.mark.asyncio
async def test_her_project_does_not_reach_his_channels(install_channels, wire, wes, karli):
    people.update(int(karli["id"]), ntfy_topic="karli-portal", telegram_chat_id="222")
    _sub("https://push/wes", person_id=int(wes["id"]))
    _sub("https://push/karli", person_id=int(karli["id"]))
    hers = db.create_project(title="Hers", description="d")
    people.set_members(hers["id"], [int(karli["id"])])

    await notify.notify("New question", "why?", project_id=hers["id"])

    assert wire["ntfy"] == ["karli-portal"]
    assert wire["telegram"] == ["222"]
    assert wire["push"] == ["https://push/karli"]


@pytest.mark.asyncio
async def test_a_one_person_install_notifies_exactly_as_before(install_channels, wire, wes):
    _sub("https://push/wes")
    project = db.create_project(title="P", description="d")
    people.set_members(project["id"], [int(wes["id"])])

    await notify.notify("New question", "why?", project_id=project["id"])

    assert wire["ntfy"] == ["portal"]
    assert wire["telegram"] == ["111"]
    assert wire["push"] == ["https://push/wes"]


@pytest.mark.asyncio
async def test_an_install_wide_notification_still_reaches_everyone(
    install_channels, wire, wes, karli
):
    people.update(int(karli["id"]), ntfy_topic="karli-portal")
    await notify.notify("New model", "Opus 6 is out")
    assert sorted(wire["ntfy"]) == ["karli-portal", "portal"]


@pytest.mark.asyncio
async def test_only_the_first_telegram_copy_records_the_message_id(
    install_channels, monkeypatch, wes, karli
):
    """There is one column for the message id and a question has one, so the
    second person's copy keeps its buttons. It still answers correctly - that
    is a stale-looking message, not a wrong one."""
    people.update(int(karli["id"]), telegram_chat_id="222")
    recorded = []

    async def fake_telegram(token, chat_id, text, question_id, record_msg_id=True):
        recorded.append((chat_id, record_msg_id))

    async def fake_ntfy(*a, **k):
        return None

    async def fake_push(*a, **k):
        return 0

    monkeypatch.setattr(notify, "_send_telegram", fake_telegram)
    monkeypatch.setattr(notify, "_send_ntfy", fake_ntfy)
    monkeypatch.setattr(webpush, "push_to", fake_push)

    await notify.notify("New question", "why?", question_id=1)

    assert [flag for _, flag in recorded] == [True, False]

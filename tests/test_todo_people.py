"""Which person a todo is waiting on, once there is more than one person.

`owner` has always said agent-or-human. It could never say *which* human,
because until 2026-07-28 there was only one. Wes's note asked for the checklist
to "split by person instead of only by agent-versus-you", and the shape of that
turns out to be two facts rather than one:

- **What was recorded.** `todos.person_id`, set when somebody picks a name or
  an agent writes one. NULL on every row that predates the column, and nothing
  is back-filled: stamping four hundred old rows with the owner would look
  exactly like somebody having decided that.
- **What can be deduced.** An unassigned item on a project with exactly one
  member is that member's - the set of people who could do it has one element
  in it. That single rule is also what keeps a one-person install unchanged,
  since there the sole member is the owner and every human item resolves to
  him.

The two must not blur. A deduction is re-derived from live membership on every
read, so adding a second person to a project correctly turns "Wes's" back into
"nobody recorded which"; a stored attribution would have gone on claiming it
knew. Everything below is a way of failing one of those.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db, people, todos


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def wes():
    return people.owner()


@pytest.fixture
def karli():
    return people.get(people.add(name="Karli", gender="female"))


@pytest.fixture
def project(wes):
    return db.create_project(
        "Fridge Board", description="A thing.", stage="active", slug="fridge"
    )


# --------------------------------------------------------------------------
# One person: nothing changes at all
# --------------------------------------------------------------------------

def test_one_person_gets_the_prompt_section_it_always_had(project):
    """The whole feature is invisible on an install with one person in it.

    Not "roughly the same": byte-identical. The prompt is under a byte budget
    (app/promptbudget.py), and a heading that gained a name would spend some of
    it saying something nobody could not already have known.
    """
    db.add_todo(project["id"], "Collapse the agent console", "agent")
    db.add_todo(project["id"], "Enable Tailscale HTTPS", "user")

    section = todos.prompt_section(project["id"])

    assert f"### {config.SITE.owners} (only {config.SITE.they} can do these)" in section
    assert "Waiting on a person" not in section
    # And nothing teaching a field whose only possible value is the one person
    # reading it.
    assert '"person"' not in section


def test_one_person_gets_one_heading_even_when_some_items_are_stamped(project, wes):
    """A stamped item and an unstamped one land in the same block.

    Both resolve to the same person - one because it says so, one because he is
    the only member - so grouping them apart would print his name twice with
    half his list under each.
    """
    db.add_todo(project["id"], "Enable Tailscale HTTPS", "user")
    db.add_todo(project["id"], "Rotate the sudo password", "user", person_id=wes["id"])

    groups = todos.by_person(db.list_todos(project["id"], owner="user"), project["id"])

    assert len(groups) == 1
    assert [r["text"] for r in groups[0][1]] == [
        "Enable Tailscale HTTPS",
        "Rotate the sudo password",
    ]


def test_an_empty_human_half_still_says_whose_it_would_be(project):
    """"(nothing outstanding)" under a name, not under "nobody".

    A list with nothing in it has no fact to report about who it is waiting on,
    so the heading names whoever the next item added there would land on.
    """
    db.add_todo(project["id"], "Collapse the agent console", "agent")

    section = todos.prompt_section(project["id"])

    assert f"### {config.SITE.owners} (only {config.SITE.they} can do these)" in section
    assert "(nothing outstanding)" in section


# --------------------------------------------------------------------------
# Storage: what is recorded, and what a re-statement may change
# --------------------------------------------------------------------------

def test_naming_a_person_makes_it_a_humans_item(project, karli):
    """"Karli has to do this" and "the agent has to do this" cannot both hold.

    Keeping owner='agent' would put an item wearing somebody's name on the
    agent's backlog, where the scheduler counts it as work a run could pick up.
    """
    row = db.add_todo(project["id"], "Paste an ntfy topic in", "agent", person_id=karli["id"])

    assert row["owner"] == "user"
    assert row["person_id"] == karli["id"]


def test_re_stating_an_item_fills_an_absent_attribution(project, karli):
    """Saying "and that one is Karli's" about an unclaimed item is new
    information, and the dedupe path already merges new tags the same way."""
    db.add_todo(project["id"], "Paste an ntfy topic in", "user")

    db.add_todo(project["id"], "paste an ntfy topic in.", "user", person_id=karli["id"])

    rows = db.list_todos(project["id"], owner="user")
    assert len(rows) == 1
    assert rows[0]["person_id"] == karli["id"]


def test_re_stating_an_item_never_moves_it_between_two_people(project, wes, karli):
    """A re-statement is not a re-assignment.

    An agent restates the whole list back in its report most runs. If a
    restatement could move work from one person to the other, the list would be
    unstable in exactly the way nobody could trace - it would look like a
    decision somebody made.
    """
    db.add_todo(project["id"], "Paste an ntfy topic in", "user", person_id=wes["id"])

    db.add_todo(project["id"], "Paste an ntfy topic in", "user", person_id=karli["id"])

    rows = db.list_todos(project["id"], owner="user")
    assert len(rows) == 1
    assert rows[0]["person_id"] == wes["id"]


def test_re_stating_an_agent_item_with_a_name_does_not_hand_it_over(project, karli):
    """Filling in an absent fact is not the same as moving the work.

    An agent item is one the agent is doing; a name arriving on a restatement
    of it is far more likely to be a slip than a decision to take it off the
    agent's list, and the failure mode of believing it is silent - the item
    stops being workable and the run that was doing it never says why.
    """
    db.add_todo(project["id"], "Wire up the routing", "agent")

    db.add_todo(project["id"], "Wire up the routing", "user", person_id=karli["id"])

    rows = db.list_todos(project["id"])
    assert len(rows) == 1
    assert rows[0]["owner"] == "agent"
    assert rows[0]["person_id"] is None


def test_nothing_is_back_filled(project, wes, karli):
    """A row written before anyone could say whose it was still says nothing.

    This is the same rule the answer and Telegram attribution follow: an
    invented attribution reads exactly like a real one.
    """
    db.add_todo(project["id"], "Enable Tailscale HTTPS", "user")

    assert db.list_todos(project["id"], owner="user")[0]["person_id"] is None


# --------------------------------------------------------------------------
# Deduction: the sole member, and where it stops
# --------------------------------------------------------------------------

def test_a_sole_member_owns_the_projects_unassigned_items(project, wes, karli):
    """Two people on the install, one on the project: no ambiguity to report."""
    db.add_todo(project["id"], "Enable Tailscale HTTPS", "user")

    groups = todos.by_person(db.list_todos(project["id"], owner="user"), project["id"])

    assert [g[0]["id"] for g in groups] == [wes["id"]]


def test_a_second_member_stops_the_deduction(project, wes, karli):
    """The moment somebody else joins, an unassigned item honestly says nobody
    recorded which - and it says so on the next read, because the deduction is
    re-derived rather than stored."""
    db.add_todo(project["id"], "Enable Tailscale HTTPS", "user")
    people.add_member(project["id"], karli["id"])

    groups = todos.by_person(db.list_todos(project["id"], owner="user"), project["id"])

    assert [g[0] for g in groups] == [None]
    section = todos.prompt_section(project["id"])
    # And it names who it could be. Saying WHICH of two people has to do a
    # thing is a claim the portal cannot make; listing the two who could is a
    # fact it holds, and it is what lets the next run ask the right person.
    assert f"### Waiting on a person (nobody recorded which of: {config.SITE.owner}, Karli)" in section


def test_a_stamped_item_survives_a_second_member_joining(project, wes, karli):
    """What was recorded is not a deduction and does not evaporate."""
    db.add_todo(project["id"], "Rotate the sudo password", "user", person_id=wes["id"])
    db.add_todo(project["id"], "Enable Tailscale HTTPS", "user")
    people.add_member(project["id"], karli["id"])

    groups = todos.by_person(db.list_todos(project["id"], owner="user"), project["id"])

    assert [(g[0]["id"] if g[0] else None) for g in groups] == [wes["id"], None]


def test_an_archived_sole_member_is_not_deduced_onto(project, wes, karli):
    """Retiring somebody is the act of saying they are not doing things.

    An open task deduced onto an archived person is a task assigned to somebody
    the portal has been told to stop asking.
    """
    people.set_members(project["id"], [karli["id"]])
    people.archive(karli["id"])
    db.add_todo(project["id"], "Enable Tailscale HTTPS", "user")

    assert todos.sole_member(project["id"]) is None


def test_an_archived_person_keeps_the_items_recorded_against_them(project, wes, karli):
    """Archiving a person must not make their open work vanish off the page.

    `people.everyone()` does not list them, so the grouping has to reach past
    it - a group that quietly disappeared would present as work going missing.
    """
    db.add_todo(project["id"], "Paste an ntfy topic in", "user", person_id=karli["id"])
    people.archive(karli["id"])

    groups = todos.by_person(db.list_todos(project["id"], owner="user"), project["id"])

    assert [g[0]["id"] for g in groups] == [karli["id"]]


def test_two_peoples_blocks_are_both_in_the_prompt(project, wes, karli):
    people.add_member(project["id"], karli["id"])
    db.add_todo(project["id"], "Rotate the sudo password", "user", person_id=wes["id"])
    db.add_todo(project["id"], "Paste an ntfy topic in", "user", person_id=karli["id"])

    section = todos.prompt_section(project["id"])

    # Karli's heading in full: she is created by this test, so her name and her
    # pronoun are both known here. The owner's are not - his name comes from
    # the machine the portal is installed on, and this suite also runs against
    # a fresh clone of the public repo where he is somebody else entirely.
    assert "### Karli's (only she can do these)" in section
    assert f"### {people.possessive(wes['name'])} (only " in section
    assert "Rotate the sudo password" in section
    assert "Paste an ntfy topic in" in section
    # And each item is under its own heading rather than both under one.
    assert section.index("Rotate the sudo password") < section.index("### Karli's")


def test_the_prompt_teaches_the_person_field_once_there_are_two(project, wes, karli):
    db.add_todo(project["id"], "Enable Tailscale HTTPS", "user")

    section = todos.prompt_section(project["id"])

    assert '"person"' in section
    assert "Karli" in section


# --------------------------------------------------------------------------
# A report naming somebody
# --------------------------------------------------------------------------

def test_a_report_can_file_an_item_against_a_person(project, wes, karli):
    todos.apply_updates(
        project["id"],
        {"add": [{"text": "Paste an ntfy topic in", "owner": "user", "person": "Karli"}]},
    )

    row = db.list_todos(project["id"], owner="user")[0]
    assert row["person_id"] == karli["id"]


def test_a_name_in_the_owner_field_is_read_as_a_name(project, wes, karli):
    """A model told to write "agent" or "user" and also told about people will
    write `"owner": "Karli"` sooner or later.

    The old code dropped anything it did not recognize to "agent", which put an
    item somebody has to do onto the agent's backlog and told nobody. Reading
    it as a name is the only interpretation that is not a silent loss.
    """
    todos.apply_updates(project["id"], {"add": [{"text": "Paste an ntfy topic in", "owner": "Karli"}]})

    rows = db.list_todos(project["id"])
    assert rows[0]["owner"] == "user"
    assert rows[0]["person_id"] == karli["id"]


def test_a_person_nobody_matches_is_dropped_not_guessed_at(project, wes, karli):
    """An item filed against the wrong person is worse than one filed against
    nobody, because only the second reads as the open question it is."""
    todos.apply_updates(
        project["id"],
        {"add": [{"text": "Ask the neighbor", "owner": "user", "person": "Nobody Here"}]},
    )

    row = db.list_todos(project["id"], owner="user")[0]
    assert row["person_id"] is None


def test_an_unrecognized_owner_that_is_not_a_name_still_lands_on_the_agent(project):
    """The old fallback is intact for the case it was written for."""
    todos.apply_updates(project["id"], {"add": [{"text": "Do the thing", "owner": "nonsense"}]})

    assert db.list_todos(project["id"])[0]["owner"] == "agent"


def test_a_slug_resolves_as_well_as_a_name(project, wes, karli):
    todos.apply_updates(
        project["id"], {"add": [{"text": "Paste an ntfy topic in", "person": "karli"}]}
    )

    assert db.list_todos(project["id"], owner="user")[0]["person_id"] == karli["id"]


def test_agent_and_user_are_not_read_as_people(project, wes, karli):
    """The two words the contract has always used keep meaning what they meant."""
    todos.apply_updates(project["id"], {"add": [{"text": "Do the thing", "owner": "user"}]})

    row = db.list_todos(project["id"])[0]
    assert row["owner"] == "user"
    assert row["person_id"] is None


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

def test_the_picker_offers_the_other_person(client, project, wes, karli):
    html = client.get(f"/project/{project['slug']}").text

    assert f'value="person:{karli["id"]}"' in html
    assert ">for Karli</option>" in html


def test_the_picker_does_not_offer_the_person_using_it_twice(client, project, wes, karli):
    """"for me" already is them; a second row with their own name is clutter."""
    client.cookies.set(people.COOKIE, wes["slug"])

    html = client.get(f"/project/{project['slug']}").text

    assert f'value="person:{wes["id"]}"' not in html
    assert ">for me</option>" in html


def test_a_one_person_install_has_no_extra_options(client, project, wes):
    html = client.get(f"/project/{project['slug']}").text

    assert "person:" not in html


def test_adding_for_me_records_who_that_was(client, project, wes, karli):
    """Whoever is holding the phone, not the owner - who is only the right
    answer on the install where he is the only answer."""
    client.cookies.set(people.COOKIE, karli["slug"])

    client.post(f"/project/{project['slug']}/todo", data={"text": "Buy a lamp", "owner": "user"})

    assert db.list_todos(project["id"], owner="user")[0]["person_id"] == karli["id"]


def test_adding_for_somebody_else_records_them(client, project, wes, karli):
    client.post(
        f"/project/{project['slug']}/todo",
        data={"text": "Paste an ntfy topic in", "owner": f"person:{karli['id']}"},
    )

    row = db.list_todos(project["id"], owner="user")[0]
    assert row["owner"] == "user"
    assert row["person_id"] == karli["id"]


def test_a_person_id_that_names_nobody_still_lands_on_a_human(client, project, wes, karli):
    """The one thing the picker is certain about is that a human was chosen."""
    client.post(
        f"/project/{project['slug']}/todo",
        data={"text": "Paste an ntfy topic in", "owner": "person:9999"},
    )

    row = db.list_todos(project["id"])[0]
    assert row["owner"] == "user"
    assert row["person_id"] is None


def test_the_page_heads_each_block_with_whose_it_is(client, project, wes, karli):
    people.add_member(project["id"], karli["id"])
    db.add_todo(project["id"], "Rotate the sudo password", "user", person_id=wes["id"])
    db.add_todo(project["id"], "Paste an ntfy topic in", "user", person_id=karli["id"])
    client.cookies.set(people.COOKIE, wes["slug"])

    html = client.get(f"/project/{project['slug']}").text

    assert ">For you" in html
    assert ">For Karli" in html


def test_the_page_says_for_you_to_whoever_is_reading_it(client, project, wes, karli):
    """Second person for yourself and third for everyone else, from both ends."""
    people.add_member(project["id"], karli["id"])
    db.add_todo(project["id"], "Paste an ntfy topic in", "user", person_id=karli["id"])
    client.cookies.set(people.COOKIE, karli["slug"])

    html = client.get(f"/project/{project['slug']}").text

    assert ">For you" in html
    assert ">For Karli" not in html


def test_a_one_person_page_still_says_for_you(client, project, wes):
    db.add_todo(project["id"], "Enable Tailscale HTTPS", "user")

    html = client.get(f"/project/{project['slug']}").text

    assert ">For you" in html
    assert "For somebody" not in html


def test_an_empty_human_half_still_renders_its_heading(client, project, wes):
    """The heading and its empty state are how you know where the picker's
    "for me" would put something."""
    html = client.get(f"/project/{project['slug']}").text

    assert ">For you" in html
    assert "Nothing waiting on you." in html


def test_the_history_page_groups_the_same_way(client, project, wes, karli):
    people.add_member(project["id"], karli["id"])
    row = db.add_todo(project["id"], "Paste an ntfy topic in", "user", person_id=karli["id"])
    db.set_todo_done(row["id"], True)
    client.cookies.set(people.COOKIE, wes["slug"])

    # Jinja escapes the apostrophe in a possessive, so read the page the way a
    # browser would rather than asserting against the raw entity.
    html = client.get(f"/project/{project['slug']}/todos/history").text.replace("&#39;", "'")

    assert ">Karli's" in html


def test_the_history_page_is_unchanged_for_one_person(client, project, wes):
    row = db.add_todo(project["id"], "Enable Tailscale HTTPS", "user")
    db.set_todo_done(row["id"], True)

    html = client.get(f"/project/{project['slug']}/todos/history").text

    assert ">Yours" in html
    assert "Nobody in particular" not in html


# --------------------------------------------------------------------------
# The axis that was already there
# --------------------------------------------------------------------------

def test_the_agent_half_never_grows_a_person(project, wes, karli):
    """`owner` keeps its own job. The scheduler reads it to decide whether a
    run could make progress, and an agent item is nobody's in particular."""
    db.add_todo(project["id"], "Wire up the routing", "agent")

    row = db.list_todos(project["id"], owner="agent")[0]
    assert todos.responsible_for(row, wes) is None


# --------------------------------------------------------------------------
# Re-filing an existing item (#420)
# --------------------------------------------------------------------------
# Until this existed, changing whose an item was meant deleting it and adding
# it again, which threw away its tags and its age. Home Hub is the case that
# wanted it: three human items on a project both of them are on, where the
# single-member deduction correctly declines to guess.

def test_refiling_moves_an_item_onto_a_person(client, project, wes, karli):
    people.add_member(project["id"], karli["id"])
    row = db.add_todo(project["id"], "Paste the ntfy topic in", "user")
    assert row["person_id"] is None

    client.post(f"/todo/{row['id']}/person", data={"person": str(karli["id"])})

    assert db.get_todo(row["id"])["person_id"] == karli["id"]


def test_refiling_keeps_the_item_it_was(client, project, wes, karli):
    """The whole point of re-filing over delete-and-re-add: nothing else moves."""
    people.add_member(project["id"], karli["id"])
    row = db.add_todo(project["id"], "Paste the ntfy topic in", "user", tags=["blocked"])
    created = db.get_todo(row["id"])["created_at"]

    client.post(f"/todo/{row['id']}/person", data={"person": str(karli["id"])})

    after = db.get_todo(row["id"])
    assert after["id"] == row["id"]
    assert after["text"] == "Paste the ntfy topic in"
    assert db.todo_tags(after) == ["blocked"]
    assert after["created_at"] == created


def test_refiling_to_nobody_clears_the_person(client, project, wes, karli):
    """"Nobody" is a real answer - a person has to do this, we cannot say which
    - so it stays on the human half rather than falling back to the agent."""
    people.add_member(project["id"], karli["id"])
    row = db.add_todo(project["id"], "Decide on the icons", "user", person_id=karli["id"])

    client.post(f"/todo/{row['id']}/person", data={"person": ""})

    after = db.get_todo(row["id"])
    assert after["person_id"] is None
    assert after["owner"] == "user"


def test_refiling_onto_a_non_member_files_it_onto_nobody(client, project, wes, karli):
    """An item can only be somebody's if they can see the project it is on.

    The UI never offers a non-member, so this needs a hand-made request - but
    a wrong attribution is worse than a missing one, which is the same call
    the create route makes for an id that names nobody.
    """
    row = db.add_todo(project["id"], "Something", "user")
    assert not people.is_member(project["id"], karli["id"])

    client.post(f"/todo/{row['id']}/person", data={"person": str(karli["id"])})

    assert db.get_todo(row["id"])["person_id"] is None


def test_refiling_an_item_that_does_not_exist_is_a_404(client, project, karli):
    assert client.post("/todo/98765/person", data={"person": str(karli["id"])}).status_code == 404


def _refile_data(html: str) -> list[dict]:
    """The re-file destinations the page hands the right-click menu.

    The control is initTodoMenu in app.js since 2026-08-04 ("compress the
    +tag, whose? and x buttons into a right click menu"); the page's half of
    the contract is a JSON attribute on the card, HTML-escaped by Jinja.
    """
    import html as html_mod
    import json
    import re

    m = re.search(r"data-refile='([^']*)'", html)
    assert m, "the todo card no longer carries data-refile"
    return json.loads(html_mod.unescape(m.group(1)))


def test_the_menu_offers_the_re_file_choices_once_there_are_two_of_you(
    client, project, wes, karli
):
    people.add_member(project["id"], karli["id"])
    db.add_todo(project["id"], "Paste the ntfy topic in", "user")

    choices = _refile_data(client.get(f"/project/{project['slug']}").text)

    assert "Karli" in [c["label"] for c in choices]


def test_one_member_still_gets_the_menu_because_the_agent_is_a_destination(
    client, project, wes
):
    """This used to be the case with NO control at all (#420): with one member,
    a picker offering that one name changes nothing you can see.

    Adding the agent as a destination (#428) is what earns it back. There are
    now two places a one-person project's item can be, so the control has
    something to say - but still no "nobody", because with one member an
    unattributed human item already resolves to them by deduction, and two
    buttons for one outcome is the thing the old rule was really objecting to.
    """
    db.add_todo(project["id"], "Paste the ntfy topic in", "user")

    choices = _refile_data(client.get(f"/project/{project['slug']}").text)

    assert [c["label"] for c in choices] == ["the agent", "Wes"]
    assert [c["label"] for c in todos.refile_choices(project["id"])] == ["the agent", "Wes"]


def test_the_agent_half_gets_the_menu_too(client, project, wes, karli):
    """An agent item that turns out to need somebody's hands should move, not
    be retyped. One data-refile on the card serves both halves; each list says
    where its items already sit."""
    people.add_member(project["id"], karli["id"])
    db.add_todo(project["id"], "Wire up the routing", "agent")

    html = client.get(f"/project/{project['slug']}").text

    assert _refile_data(html)
    assert 'data-here="agent"' in html


def test_the_list_marks_where_the_item_already_is(client, project, wes, karli):
    people.add_member(project["id"], karli["id"])
    db.add_todo(project["id"], "Paste the ntfy topic in", "user", person_id=karli["id"])

    html = client.get(f"/project/{project['slug']}").text

    assert f'data-here="{karli["id"]}"' in html


def test_a_re_filed_item_moves_to_the_other_heading(client, project, wes, karli):
    """The end to end thing Wes would actually see happen on the page."""
    people.add_member(project["id"], karli["id"])
    row = db.add_todo(project["id"], "Paste the ntfy topic in", "user", person_id=wes["id"])

    client.post(f"/todo/{row['id']}/person", data={"person": str(karli["id"])})

    grouped = todos.by_person(db.list_todos(project["id"], owner="user"), project["id"])
    who = [p["name"] for p, items in grouped if any(i["id"] == row["id"] for i in items)]
    assert who == ["Karli"]


# --------------------------------------------------------------------------
# Handing an item back to the agent (#428)
# --------------------------------------------------------------------------
# #420 could move an item BETWEEN people; it could never move one across the
# agent/human line, because `set_todo_person` only ever pushed rows toward the
# human side. So an agent item that turned out to need somebody's hands - or a
# human item the agent could do after all - still had to be deleted and typed
# again, losing its tags and its age, which is the exact thing #420 existed to
# stop.
#
# The load-bearing distinction in everything below: "nobody" and "the agent"
# are different destinations. Nobody says *a person has to do this and we
# cannot yet say which*; the agent says *no person has to do this at all*.
# Collapsing them would make the blank value mean two things and would quietly
# take work off the human list.

def test_handing_a_human_item_back_to_the_agent(client, project, wes, karli):
    people.add_member(project["id"], karli["id"])
    row = db.add_todo(project["id"], "Wire up the routing", "user", person_id=karli["id"])

    client.post(f"/todo/{row['id']}/person", data={"person": "agent"})

    after = db.get_todo(row["id"])
    assert after["owner"] == "agent"
    assert after["person_id"] is None


def test_handing_an_agent_item_to_a_person(client, project, wes, karli):
    people.add_member(project["id"], karli["id"])
    row = db.add_todo(project["id"], "Paste the ntfy topic in", "agent")

    client.post(f"/todo/{row['id']}/person", data={"person": str(karli["id"])})

    after = db.get_todo(row["id"])
    assert after["owner"] == "user"
    assert after["person_id"] == karli["id"]


def test_handing_an_agent_item_to_nobody_puts_it_on_the_human_half(
    client, project, wes, karli
):
    """"Nobody" is not a synonym for "the agent". Picking it off an agent item
    says a person has to do this and we cannot say which yet, so the item must
    cross to the human half with no name on it - not sit where it was."""
    people.add_member(project["id"], karli["id"])
    row = db.add_todo(project["id"], "Decide on the icons", "agent")

    client.post(f"/todo/{row['id']}/person", data={"person": ""})

    after = db.get_todo(row["id"])
    assert after["owner"] == "user"
    assert after["person_id"] is None


def test_handing_back_keeps_the_item_it_was(client, project, wes, karli):
    """Same promise as re-filing between people: nothing but the destination
    changes, which is the whole reason this beats delete-and-re-add."""
    people.add_member(project["id"], karli["id"])
    row = db.add_todo(project["id"], "Wire up the routing", "user", tags=["blocked"])
    created = db.get_todo(row["id"])["created_at"]

    client.post(f"/todo/{row['id']}/person", data={"person": "agent"})

    after = db.get_todo(row["id"])
    assert after["id"] == row["id"]
    assert after["text"] == "Wire up the routing"
    assert db.todo_tags(after) == ["blocked"]
    assert after["created_at"] == created


def test_a_handed_back_item_moves_to_the_agent_heading(client, project, wes, karli):
    """The end to end thing Wes would actually see happen on the page: it
    leaves the human grouping entirely and appears on the agent's list."""
    people.add_member(project["id"], karli["id"])
    row = db.add_todo(project["id"], "Wire up the routing", "user", person_id=wes["id"])

    client.post(f"/todo/{row['id']}/person", data={"person": "agent"})

    human = db.list_todos(project["id"], owner="user")
    assert row["id"] not in [r["id"] for r in human]
    assert row["id"] in [r["id"] for r in db.list_todos(project["id"], owner="agent")]


def test_the_agent_is_the_first_choice_offered(project, wes, karli):
    """Order is the menu's order. The agent leads because it is the half that
    exists on every project; people follow; "nobody" is last because it is the
    answer you give when none of the others is one."""
    people.add_member(project["id"], karli["id"])

    assert [c["label"] for c in todos.refile_choices(project["id"])] == [
        "the agent",
        "Wes",
        "Karli",
        "nobody",
    ]


def test_an_archived_person_is_not_a_destination(project, wes, karli):
    """Retiring somebody is the act of saying they are not doing things any
    more, so handing them an open task is a destination nobody wants. Same
    reasoning as `sole_member`, which has skipped archived people all along."""
    people.add_member(project["id"], karli["id"])
    people.archive(karli["id"])

    labels = [c["label"] for c in todos.refile_choices(project["id"])]
    assert "Karli" not in labels
    # And with one live member left, "nobody" goes with her - there is nobody
    # left to be undecided between.
    assert labels == ["the agent", "Wes"]


def test_a_project_with_nobody_on_it_gets_no_control(project):
    """The "a control that changes nothing you can see" rule, which used to be
    the one-member case and is now this one: with no people, the agent is the
    only destination there is, and a menu of one is a menu of none."""
    people.set_members(project["id"], [])
    db.get_conn().execute(
        "DELETE FROM project_people WHERE project_id = ?", (project["id"],)
    )
    db.get_conn().commit()

    assert todos.refile_choices(project["id"]) == []


def test_the_agent_half_marks_the_agent_as_current(client, project, wes, karli):
    """Each list tells the menu where it already sits, so the current choice
    can be skipped on the agent half exactly as on a person's."""
    people.add_member(project["id"], karli["id"])
    db.add_todo(project["id"], "Wire up the routing", "agent")

    html = client.get(f"/project/{project['slug']}").text

    assert 'data-here="agent"' in html
    assert "agent" in [c["value"] for c in _refile_data(html)]


def test_the_unattributed_group_marks_nobody_as_current(client, project, wes, karli):
    """`refile_value` reads the group heading, not the row - and a heading with
    no person is the "nobody" choice, whose value is the empty string."""
    people.add_member(project["id"], karli["id"])
    db.add_todo(project["id"], "Decide on the icons", "user")

    html = client.get(f"/project/{project['slug']}").text

    assert todos.refile_value(None) == ""
    assert 'data-here=""' in html

"""The people feature as it is actually used: a browser, a cookie, a form.

tests/test_people.py pins the model. This pins the parts a person touches - the
switcher in the masthead, the panel in Settings, the member boxes on a project -
and above all the thing that has to be right for any of it to mean anything: a
note posted from her browser is attributed to her.

The recurring assertion is "nothing appears until there are two of them". A
single-person install is every install until somebody adds a second person, and
it must look and behave exactly as it did.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, db, main, people


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def project():
    return db.create_project(title="A project", description="d")


@pytest.fixture
def erin():
    return people.add(name="Erin", gender="female", background="newer to this")


# --------------------------------------------------------------------------
# The switcher in the masthead
# --------------------------------------------------------------------------

def test_one_person_gets_no_switcher(client, project):
    body = client.get("/").text
    assert 'name="person"' not in body
    assert client.get(f"/project/{project['slug']}").status_code == 200


def test_two_people_get_a_switcher_naming_both(client, project, erin):
    body = client.get("/").text
    assert 'action="/whoami"' in body
    assert ">Erin<" in body
    assert f">{config.SITE.owner}<" in body


def test_the_switcher_shows_who_the_portal_thinks_you_are(client, erin):
    client.cookies.set(people.COOKIE, "erin")
    body = client.get("/").text
    # The selected option is hers, not the owner's.
    assert '<option value="erin" selected>Erin</option>' in body


# --------------------------------------------------------------------------
# Saying who you are
# --------------------------------------------------------------------------

def test_picking_a_person_sets_a_long_lived_cookie(client, erin):
    r = client.post("/whoami", data={"person": "erin", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "portal_person=erin" in cookie
    # Not a session cookie. Expiring it would silently start attributing her
    # notes to him, which is the exact failure this feature exists to prevent.
    assert "Max-Age=" in cookie
    assert "Max-Age=0" not in cookie


def test_an_unknown_person_leaves_the_cookie_alone(client):
    r = client.post("/whoami", data={"person": "nobody"}, follow_redirects=False)
    assert r.status_code == 303
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize(
    "target,expected",
    [
        ("/project/x", "/project/x"),
        # `//evil.example` is a protocol-relative URL - the case a naive
        # startswith("/") check waves straight through.
        ("//evil.example/", "/"),
        ("https://evil.example/", "/"),
        ("", "/"),
    ],
)
def test_the_return_path_cannot_leave_the_portal(client, erin, target, expected):
    r = client.post(
        "/whoami", data={"person": "erin", "next": target}, follow_redirects=False
    )
    assert r.headers["location"] == expected


# --------------------------------------------------------------------------
# The point of all of it: a note is signed
# --------------------------------------------------------------------------

def test_a_note_from_her_browser_is_hers(client, project, erin):
    client.cookies.set(people.COOKIE, "erin")
    client.post(f"/project/{project['slug']}/note", data={"note": "how do I start?"})
    entry = db.list_journal(project["id"])[0]
    assert int(entry["person_id"]) == erin


def test_a_note_with_no_cookie_is_the_owners(client, project, erin):
    client.post(f"/project/{project['slug']}/note", data={"note": "do the thing"})
    entry = db.list_journal(project["id"])[0]
    assert int(entry["person_id"]) == int(people.owner()["id"])


def test_the_agent_is_told_who_wrote_it(client, project, erin):
    from app import notes

    client.cookies.set(people.COOKIE, "erin")
    client.post(f"/project/{project['slug']}/note", data={"note": "how do I start?"})
    block = notes.render(notes.pending(project["id"]))
    assert block.startswith("## A note from Erin since your last run")
    assert "She wrote this" in block


def test_a_forwarded_for_header_cannot_claim_to_be_somebody(client, project):
    # The only thing the client address is used for is a `tailscale whois`
    # hint, so believing a spoofable header would let anyone be anyone in
    # exchange for nothing.
    people.add(name="Erin", tailnet_login="erin@example.com")
    client.post(
        f"/project/{project['slug']}/note",
        data={"note": "not hers"},
        headers={"X-Forwarded-For": "100.9.9.9"},
    )
    entry = db.list_journal(project["id"])[0]
    assert int(entry["person_id"]) == int(people.owner()["id"])


# --------------------------------------------------------------------------
# A project added from her browser is hers
# --------------------------------------------------------------------------
# Wes, 2026-08-06: "When Karli adds a project, it should just assign it to her
# by default. It is currently assigning it to me, though."

def _idea_from(client, note: str) -> db.sqlite3.Row:
    r = client.post("/ideas", data={"idea": note}, follow_redirects=False)
    assert r.status_code == 303
    return db.get_project_by_slug(r.headers["location"].rsplit("/", 1)[1])


def test_an_idea_from_her_browser_is_her_project(client, erin):
    client.cookies.set(people.COOKIE, "erin")
    created = _idea_from(client, "A recipe box")
    assert people.member_ids(created["id"]) == {erin}


def test_her_ideas_seed_note_is_signed_by_her_too(client, erin):
    client.cookies.set(people.COOKIE, "erin")
    created = _idea_from(client, "A recipe box")
    entry = db.list_journal(created["id"])[0]
    assert int(entry["person_id"]) == erin


def test_an_idea_with_no_cookie_is_the_owners(client, erin):
    created = _idea_from(client, "A recipe box")
    assert people.member_ids(created["id"]) == {int(people.owner()["id"])}


# --------------------------------------------------------------------------
# Settings > people
# --------------------------------------------------------------------------

def test_the_panel_lists_everybody_including_the_archived(client, erin):
    people.archive(erin)
    body = client.get("/settings").text
    assert 'id="panel-people"' in body
    # A person who has vanished from every screen is a person nobody can bring
    # back, so this is the one page that shows them.
    assert "Erin" in body
    assert "bring back" in body


def test_adding_somebody_through_the_form(client):
    client.post(
        "/people/add",
        data={"name": "Erin", "gender": "female", "background": "newer to this"},
    )
    person = people.by_slug("erin")
    assert person is not None
    assert person["gender"] == "female"
    assert person["background"] == "newer to this"


def test_adding_a_nameless_person_does_nothing(client):
    before = len(people.everyone())
    client.post("/people/add", data={"name": "   "})
    assert len(people.everyone()) == before


def test_editing_somebody_through_the_form(client, erin):
    client.post(
        f"/people/{erin}/edit",
        data={
            "name": "Erin Y",
            "gender": "female",
            "background": "getting the hang of it",
            "tailnet_login": "Erin@Example.com",
        },
    )
    person = people.get(erin)
    assert person["name"] == "Erin Y"
    assert person["background"] == "getting the hang of it"
    assert person["tailnet_login"] == "erin@example.com"


def test_the_owners_name_cannot_be_changed_through_the_form(client):
    owner_id = int(people.owner()["id"])
    client.post(
        f"/people/{owner_id}/edit",
        data={"name": "Somebody Else", "gender": "female", "background": "still me"},
    )
    person = people.owner()
    assert person["name"] == config.SITE.owner
    assert person["background"] == "still me"


def test_the_panel_says_where_the_owners_name_comes_from(client, erin):
    # Otherwise a read-only field is just a field that does not work.
    body = client.get("/settings").text
    assert "portal.toml" in body


def test_archiving_and_bringing_somebody_back(client, erin):
    client.post(f"/people/{erin}/archive", data={})
    assert erin not in {int(p["id"]) for p in people.everyone()}
    client.post(f"/people/{erin}/archive", data={"restore": "1"})
    assert erin in {int(p["id"]) for p in people.everyone()}


# --------------------------------------------------------------------------
# Whose project is it
# --------------------------------------------------------------------------

def test_one_person_gets_no_member_boxes(client, project):
    body = client.get(f"/project/{project['slug']}").text
    assert 'name="member"' not in body


def test_the_member_boxes_show_who_it_belongs_to(client, project, erin):
    body = client.get(f"/project/{project['slug']}").text
    assert 'name="member"' in body
    owner_id = int(people.owner()["id"])
    # Scoped to the members form: the priority control on the same page also
    # has an option with value="1", and searching the whole page finds that.
    form = body[body.index("/members\""):]
    form = form[: form.index("</form>")]

    def box(person_id: int) -> str:
        start = form.index(f'value="{person_id}"')
        return form[start : form.index("</label>", start)]

    # His is ticked, hers is offered but not - the project is the owner's so far.
    assert "checked" in box(owner_id)
    assert "checked" not in box(erin)


def test_reassigning_a_project(client, project, erin):
    client.post(f"/project/{project['slug']}/members", data={"member": [str(erin)]})
    assert people.member_ids(project["id"]) == {erin}


def test_sharing_a_project(client, project, erin):
    owner_id = int(people.owner()["id"])
    client.post(
        f"/project/{project['slug']}/members",
        data={"member": [str(owner_id), str(erin)]},
    )
    assert people.member_ids(project["id"]) == {owner_id, erin}


def test_unticking_everybody_falls_back_to_the_owner(client, project, erin):
    client.post(f"/project/{project['slug']}/members", data={})
    assert people.member_ids(project["id"]) == {int(people.owner()["id"])}


def test_members_of_an_unknown_project_is_a_404(client, erin):
    assert client.post("/project/no-such-thing/members", data={}).status_code == 404


# --------------------------------------------------------------------------
# A rule the DOM cannot see
# --------------------------------------------------------------------------

def test_the_switcher_never_un_hides_the_native_select():
    """app.js replaces every <select> with a `.sel-trigger` laid over the top,
    leaving the native control at `opacity: 0` underneath.

    So any rule that reaches an ENHANCED select as a descendant - `.whoami
    select` - outranks `.sel select { opacity: 0 }` and renders the invisible
    control on top of its own replacement: doubled text, two borders, two
    carets. That is what the first version of this stylesheet shipped, and no
    DOM test can see it because the test shims apply no CSS. The child
    combinator is the fix, because an enhanced select is a child of `.sel`, not
    of the form.
    """
    css = _css()
    assert ".whoami .sel-trigger" in css, "the trigger is the control that is actually seen"
    for line in css.splitlines():
        stripped = line.strip()
        if not stripped.startswith(".whoami"):
            continue
        assert ".whoami select" not in stripped, (
            f"{stripped!r} reaches an enhanced select as a descendant and will "
            "un-hide the native control on top of its replacement"
        )


def test_the_switchers_caret_is_one_chevron_and_not_a_blob():
    """The caret is two triangles that MEET to form a chevron.

    `background-size` is 0.3rem, so the two `background-position` offsets have
    to differ by exactly 0.3rem. An earlier version used 0.55rem and 0.4rem - a
    0.15rem gap - which overlapped them into a smudge. Rule-level for the same
    reason as above: there is no DOM to ask.
    """
    import re

    css = _css()
    block = css[css.index(".whoami .sel-trigger,"):]
    block = block[: block.index("}")]
    offsets = [float(x) for x in re.findall(r"right ([\d.]+)rem center", block)]
    assert len(offsets) == 2, "the caret should still be positioned as two layers"
    size = float(re.search(r"background-size: ([\d.]+)rem", css[css.index(".sel-trigger {"):]).group(1))
    assert round(abs(offsets[0] - offsets[1]), 4) == size


def _css() -> str:
    from pathlib import Path as _P

    return (_P(__file__).resolve().parents[1] / "app" / "static" / "style.css").read_text()

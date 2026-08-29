"""One project's runs reading another's context (app/crossproject.py).

Wes, 2026-08-29: several of his projects are about the same physical product,
and until now the only channel between them was him retyping what one knew into
a note on another.

These tests pin the decisions rather than the prose: who may read whom (and that
"not there" and "not yours" are the same answer), that family is left to the
sub-project section instead of being listed twice, that relatedness matches a
slug *prefix* rather than any shared word, that a workspace cannot be read out
of, and that the tools reach a run through the MCP server it already carries.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import (
    agent_runner, config, crossproject, db, hookguard, people, portalmcp, subprojects,
)


@pytest.fixture
def workspaces(tmp_path, monkeypatch):
    """A projects dir the tests can put real files in."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(config, "PROJECTS_DIR", root)
    return root


def make(title: str, slug: str, **kw) -> int:
    return int(db.create_project(title, slug=slug, stage="active", **kw)["id"])


def workspace(root: Path, slug: str) -> Path:
    ws = root / slug
    ws.mkdir(parents=True, exist_ok=True)
    return ws


# ------------------------------------------------------------------ who


def test_a_run_reads_the_projects_its_person_is_on():
    her = people.add("Erin", gender="female")
    mine = make("Mine", "mine")
    also_mine = make("Also Mine", "also-mine")
    hers = make("Hers", "hers")
    people.set_members(hers, [her])

    slugs = {r["slug"] for r in crossproject.readable(mine)}
    assert slugs == {"also-mine"}
    assert "hers" not in slugs
    # And the reading project is never in its own list - its prompt already
    # carries everything the tool would say about it.
    assert "mine" not in slugs
    assert also_mine  # the id is what the fixture named, kept for the reader


def test_her_run_cannot_read_his_projects():
    her = people.add("Erin", gender="female")
    his = make("His Thing", "his-thing")
    hers = make("Her Thing", "her-thing")
    people.set_members(hers, [her])

    assert crossproject.readable(hers) == []
    with pytest.raises(crossproject.Denied):
        crossproject.digest(hers, "his-thing")
    assert his


def test_a_memberless_project_is_readable_by_everyone():
    """`create_project` always adds a member, so this state is forced - but
    `app/scope.py` already treats a memberless project as everyone's rather than
    dropping it out of every feed, and this rule agrees with that one."""
    her = people.add("Erin", gender="female")
    hers = make("Her Thing", "her-thing")
    people.set_members(hers, [her])
    orphan = make("Nobody's", "nobodys")
    db.get_conn().execute("DELETE FROM project_people WHERE project_id = ?", (orphan,))

    assert "nobodys" in {r["slug"] for r in crossproject.readable(hers)}


def test_a_project_you_cannot_read_is_indistinguishable_from_one_that_is_gone():
    """Otherwise a run could report the existence of somebody else's project to
    a person who is not on it, which is the one thing the rule is for."""
    her = people.add("Erin", gender="female")
    make("His Secret Thing", "his-secret-thing")
    hers = make("Her Thing", "her-thing")
    people.set_members(hers, [her])

    with pytest.raises(crossproject.Denied) as forbidden:
        crossproject.digest(hers, "his-secret-thing")
    with pytest.raises(crossproject.Denied) as absent:
        crossproject.digest(hers, "no-such-project-at-all")

    assert "his-secret-thing" in str(forbidden.value)
    # Same shape, and neither says whether the project exists.
    assert str(forbidden.value).replace("his-secret-thing", "X") == str(
        absent.value
    ).replace("no-such-project-at-all", "X")
    assert "His Secret" not in str(forbidden.value)


def test_you_get_the_project_you_asked_for_and_not_merely_a_readable_one():
    """Two readable projects, so a resolver that returned the first thing on the
    list would look right on a board with only one."""
    mine = make("Mine", "mine")
    first = make("First", "aaa-first")
    db.update_project(first, description="The wrong one.")
    second = make("Second", "zzz-second")
    db.update_project(second, description="The right one.")

    text = crossproject.digest(mine, "zzz-second")
    assert "The right one." in text
    assert "The wrong one." not in text


def test_the_setting_switches_the_whole_thing_off(monkeypatch):
    mine = make("Mine", "mine")
    make("Other", "other")
    assert crossproject.readable(mine)

    db.set_setting("cross_project", "0")
    assert crossproject.readable(mine) == []
    with pytest.raises(crossproject.Denied):
        crossproject.handle(mine, "projects", {})


# ------------------------------------------------------------ relatedness


def test_a_shared_slug_prefix_relates_two_projects():
    mine = make("Gift Codes", "kingshot-gift-code")
    make("Auto Bear", "kingshot-auto-bear")
    make("Something Else", "e-ink-fridge-dashboard")

    assert [r["slug"] for r in crossproject.related(mine)] == ["kingshot-auto-bear"]


def test_a_word_shared_in_the_middle_does_not_relate():
    """`secret-shopper-helper` and `board-games-secret-hitler` share the word
    "secret" and nothing else at all. A word-anywhere match paired them."""
    mine = make("Secret Shopper", "secret-shopper-helper")
    make("Secret Hitler", "board-games-secret-hitler")

    assert crossproject.related(mine) == []


def test_a_generic_leading_token_still_relates_through_the_next_one():
    """On a board where every slug starts `wes`, that token has stopped saying
    anything - but the pair sharing the *second* one must survive it."""
    for n in range(10):
        make(f"Thing {n}", f"wes-thing{n}")
    mine = make("Kingshot A", "wes-kingshot-a")
    make("Kingshot B", "wes-kingshot-b")

    slugs = [r["slug"] for r in crossproject.related(mine, cap=20)]
    assert slugs == ["wes-kingshot-b"]


def test_a_token_covering_most_of_the_board_relates_nothing():
    """The other side of the same rule: when nearly every project shares a
    token, it has stopped being a grouping and naming six of them in a prompt is
    just a second dashboard."""
    for n in range(9):
        make(f"Game {n}", f"board-game{n}")
    mine = make("Tak", "board-tak")

    assert crossproject.related(mine, cap=20) == []


def test_a_stop_token_at_the_front_relates_nothing():
    """Under a prefix match a stop token only ever matters when it *leads* -
    a trailing `-com` cannot pair anything on its own. Two projects both called
    "the something" have that word in common and nothing else.

    Deliberately a three-letter stop token: a shorter one is already dropped by
    the length floor, so a test using "my" would pass with the stop list gone
    and prove nothing about it."""
    mine = make("Budget", "the-budget-tracker")
    make("Recipes", "the-recipe-book")

    assert crossproject.related(mine) == []
    assert all(len(tok) >= 3 for tok in crossproject.STOP_TOKENS)


def test_a_two_letter_leading_token_relates_nothing():
    """Short tokens are initials and separators, not subjects. `ab-alpha` and
    `ab-beta` are not two halves of one body of work."""
    mine = make("Alpha", "ab-alpha")
    make("Beta", "ab-beta")

    assert crossproject.related(mine) == []


def test_relatedness_is_ranked_by_how_rare_the_shared_prefix_is():
    for n in range(6):
        make(f"Common {n}", f"shared-thing-{n}")
    make("Rare Twin", "unusual-twin-a")
    mine = make("Rare", "unusual-twin-b")
    make("Common Cousin", "shared-thing-x")
    # `mine` shares nothing with the `shared-*` ones, so give it one of each.
    close = make("Closer", "unusual-twin-c")

    ranked = [r["slug"] for r in crossproject.related(mine)]
    assert set(ranked) == {"unusual-twin-a", "unusual-twin-c"}
    assert close


def test_related_never_returns_a_project_the_reader_may_not_read():
    her = people.add("Erin", gender="female")
    hers = make("Her Kingshot", "kingshot-hers")
    people.set_members(hers, [her])
    mine = make("My Kingshot", "kingshot-mine")

    assert crossproject.related(mine) == []


# ------------------------------------------------------------------ family


def test_family_is_left_to_the_subproject_section():
    """A parent and its children are already named, with descriptions, a few
    lines above in the same prompt. Listing them again is the same bytes twice.

    The board is padded so `widgets` stays under the rarity ceiling - without
    the padding the family would drop out as a too-common token and this would
    pass whether or not family was excluded at all."""
    for n in range(6):
        make(f"Filler {n}", f"unrelated-filler-{n}")
    parent = make("Parent", "widgets")
    kid_a = db.create_project("Kid A", slug="widgets-alpha", stage="active", parent_id=parent)
    kid_b = db.create_project("Kid B", slug="widgets-beta", stage="active", parent_id=parent)

    # The tokens really would pair them if family were not excluded.
    assert crossproject._shared_prefix(
        crossproject._tokens("widgets-alpha"), crossproject._tokens("widgets-beta")
    ) == ["widgets"]

    assert crossproject.related(int(kid_a["id"])) == []
    assert crossproject.family_ids(kid_a) == {parent, int(kid_b["id"])}


def test_the_family_gets_its_slugs_named_because_the_other_section_withholds_them():
    """`subprojects.prompt_section` names a sibling by title and state only.
    These tools take a slug, and that gap is the whole reason the family is
    mentioned here at all."""
    parent = make("Parent", "widgets")
    kid = db.create_project("Kid A", slug="widgets-a", stage="active", parent_id=parent)
    db.create_project("Kid B", slug="widgets-b", stage="active", parent_id=parent)

    above = subprojects.prompt_section(kid)
    assert "Kid B" in above  # named...
    assert "widgets-b" not in above  # ...but with no slug for the tool to take

    section = crossproject.prompt_section(kid, offered=True)
    assert "`widgets-b`" in section
    assert "`widgets`" in section
    assert "project_context" in section


def test_no_section_at_all_for_a_project_with_no_family_and_no_neighbors():
    """An ordinary prompt on a one-of-a-kind project stays byte-for-byte what it
    was before this feature existed."""
    lonely = make("Lonely", "lonely-thing")
    make("Unrelated", "totally-different")

    assert crossproject.prompt_section(db.get_project(lonely), offered=True) == ""


def test_a_run_with_no_mcp_server_is_not_told_about_tools_it_does_not_have():
    mine = make("Gift Codes", "kingshot-gift-code")
    make("Auto Bear", "kingshot-auto-bear")
    project = db.get_project(mine)

    assert crossproject.prompt_section(project, offered=True) != ""
    assert crossproject.prompt_section(project, offered=False) == ""


def test_the_section_actually_reaches_a_build_prompt():
    """The section is only worth anything if it is in the prompt - and it sits
    under the sub-project block, which is where the family it refers to is
    named."""
    mine = make("Gift Codes", "kingshot-gift-code")
    make("Auto Bear", "kingshot-auto-bear")

    prompt = agent_runner.build_prompt("build", db.get_project(mine))
    assert "## Reading other projects" in prompt
    assert "`kingshot-auto-bear`" in prompt
    assert prompt.index("## Reading other projects") < prompt.index("## Recent journal")


def test_a_reflect_prompt_never_names_these_tools():
    """`reflect` carries no MCP server, so naming `project_context` in its
    prompt would point it at a tool it cannot call."""
    mine = make("Gift Codes", "kingshot-gift-code")
    make("Auto Bear", "kingshot-auto-bear")

    assert "## Reading other projects" not in agent_runner.build_prompt(
        "reflect", db.get_project(mine)
    )


def test_carries_tools_is_the_gate_the_prompt_section_asks():
    """The two must not drift: `reflect` gets no MCP server, so a reflect prompt
    must not name `project_context`."""
    assert portalmcp.carries_tools("build") is True
    assert portalmcp.carries_tools("reflect") is False
    db.set_setting("mcp_tools", "0")
    assert portalmcp.carries_tools("build") is False


# ----------------------------------------------------------------- context


def test_the_digest_carries_what_the_other_project_knows(workspaces):
    mine = make("Mine", "mine")
    theirs = make("Their Thing", "theirs", kind="software")
    db.update_project(theirs, description="A shop that sells cases.")
    db.add_todo(theirs, "Repaint the lid preview", owner="agent")
    db.add_journal(theirs, "agent", "progress", "## A heading\n\nThe opening paragraph.\n\nMore.")

    text = crossproject.digest(mine, "theirs")
    assert "A shop that sells cases." in text
    assert "Repaint the lid preview" in text
    assert "A heading" in text
    assert "The opening paragraph." in text


def test_a_done_todo_is_not_in_the_digest():
    mine = make("Mine", "mine")
    theirs = make("Theirs", "theirs")
    db.add_todo(theirs, "Still to do", owner="agent")
    done = db.add_todo(theirs, "Long since finished", owner="agent")
    db.set_todo_done(int(done["id"]), True)

    text = crossproject.digest(mine, "theirs")
    assert "Still to do" in text
    assert "Long since finished" not in text


def test_the_digest_names_the_workspace_so_project_files_can_be_aimed(workspaces):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    workspace(workspaces, "theirs")

    text = crossproject.digest(mine, "theirs")
    assert str(workspaces / "theirs") in text
    assert "project_files" in text


# ------------------------------------------------------------------- files


def test_a_file_comes_back_with_its_text(workspaces):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    (workspace(workspaces, "theirs") / "PLAN.md").write_text("# The plan\n\nStep one.\n")

    text = crossproject.browse(mine, "theirs", "PLAN.md")
    assert "Step one." in text
    assert "theirs/PLAN.md" in text


def test_a_directory_comes_back_as_a_listing(workspaces):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    ws = workspace(workspaces, "theirs")
    (ws / "src").mkdir()
    (ws / "src" / "main.ts").write_text("export {}\n")
    (ws / "README.md").write_text("hi\n")

    root = crossproject.browse(mine, "theirs")
    assert "src/" in root
    assert "README.md" in root

    inner = crossproject.browse(mine, "theirs", "src")
    assert "src/main.ts" in inner


def test_a_path_cannot_climb_out_of_the_workspace(workspaces):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    make("Third", "third")
    workspace(workspaces, "theirs")
    (workspace(workspaces, "third") / "secret.txt").write_text("not yours\n")

    for escape in ("../third/secret.txt", "../../etc/passwd", "src/../../third"):
        with pytest.raises(crossproject.Denied):
            crossproject.browse(mine, "theirs", escape)


def test_a_symlink_out_of_the_workspace_is_refused(workspaces):
    """resolve() collapses the link before the containment test, so this needs
    no separate is_symlink() check - and a test, because it is easy to lose."""
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    outside = workspaces.parent / "outside.txt"
    outside.write_text("not yours\n")
    (workspace(workspaces, "theirs") / "link.txt").symlink_to(outside)

    with pytest.raises(crossproject.Denied):
        crossproject.browse(mine, "theirs", "link.txt")


def test_a_big_file_is_truncated_rather_than_poured_into_the_context(workspaces):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    body = "x" * (crossproject.MAX_FILE_BYTES + 5000)
    (workspace(workspaces, "theirs") / "huge.txt").write_text(body)

    text = crossproject.browse(mine, "theirs", "huge.txt")
    assert "truncated" in text
    assert len(text) < crossproject.MAX_FILE_BYTES + 2000


def test_a_binary_is_named_and_pointed_at_rather_than_quoted(workspaces):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    shot = workspace(workspaces, "theirs") / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")

    text = crossproject.browse(mine, "theirs", "shot.png")
    assert "binary" in text
    assert str(shot) in text


def test_the_binary_answer_points_at_a_path_the_run_may_actually_read(workspaces):
    """It tells the run to open an image with its own Read tool. That claim is
    only true while `hookguard` keeps permitting reads outside the family - if
    that ever changes, this goes red rather than the advice going quietly
    wrong."""
    theirs = workspace(workspaces, "theirs")
    shot = theirs / "shot.png"
    shot.write_bytes(b"\x89PNG\x00")

    verdict = hookguard.evaluate(
        {"tool_name": "Read", "tool_input": {"file_path": str(shot)}, "cwd": "/tmp"},
        [workspaces / "mine"],
    )
    assert verdict is None


def test_a_missing_file_says_so_instead_of_raising(workspaces):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")
    workspace(workspaces, "theirs")

    text = crossproject.browse(mine, "theirs", "nope.md")
    assert "not in theirs's workspace" in text


def test_a_project_with_no_workspace_on_disk_is_refused_clearly(workspaces):
    mine = make("Mine", "mine")
    make("Theirs", "theirs")

    with pytest.raises(crossproject.Denied) as refusal:
        crossproject.browse(mine, "theirs")
    assert "no workspace on disk" in str(refusal.value)


# ------------------------------------------------------------------- tools


def test_the_tools_reach_a_run_through_the_mcp_server():
    mine = make("Mine", "mine")
    make("Other", "other")
    token = portalmcp.begin(7, mine, "build")
    assert token

    names = [t["name"] for t in portalmcp.tools(7, portalmcp._SCOPES[7].token)]
    assert names == ["ask", "projects", "project_context", "project_files"]
    portalmcp.end(7)


def test_a_lone_project_is_not_given_tools_that_can_only_say_nothing():
    """A fresh install has one project. Three tool definitions in every system
    prompt that can only ever answer "there is nothing" is a tax for no gain."""
    only = make("Only", "only")
    portalmcp.begin(8, only, "build")
    names = [t["name"] for t in portalmcp.tools(8, portalmcp._SCOPES[8].token)]
    assert names == ["ask"]
    portalmcp.end(8)


@pytest.mark.anyio
async def test_a_call_returns_the_other_project_s_context():
    mine = make("Mine", "mine")
    theirs = make("Theirs", "theirs")
    db.update_project(theirs, description="Sells cases.")
    portalmcp.begin(9, mine, "build")
    token = portalmcp._SCOPES[9].token

    result = await portalmcp.call(9, token, "project_context", {"slug": "theirs"})
    assert not result["isError"]
    assert "Sells cases." in result["content"][0]["text"]
    portalmcp.end(9)


@pytest.mark.anyio
async def test_a_refusal_comes_back_as_a_tool_error_not_a_crash():
    her = people.add("Erin", gender="female")
    hers = make("Hers", "hers")
    people.set_members(hers, [her])
    make("His", "his")
    portalmcp.begin(10, hers, "build")
    token = portalmcp._SCOPES[10].token

    result = await portalmcp.call(10, token, "project_context", {"slug": "his"})
    assert result["isError"] is True
    assert "his" in result["content"][0]["text"]
    portalmcp.end(10)


@pytest.mark.anyio
async def test_an_unregistered_run_gets_nothing():
    """Fails closed, like `ask` - a call from an orphaned run is refused."""
    result = await portalmcp.call(999, "made-up", "projects", {})
    assert result["isError"] is True


@pytest.mark.anyio
async def test_cross_project_reads_are_not_capped_like_asks():
    """`ask` spends a person's attention and is capped at three. Reading spends
    only the run's own context, so a fourth read must still work."""
    mine = make("Mine", "mine")
    make("Other", "other")
    portalmcp.begin(11, mine, "build")
    token = portalmcp._SCOPES[11].token

    for _ in range(5):
        result = await portalmcp.call(11, token, "projects", {})
        assert not result["isError"]
    portalmcp.end(11)


def test_the_listing_marks_which_projects_are_related_to_this_one():
    mine = make("Gift Codes", "kingshot-gift-code")
    make("Auto Bear", "kingshot-auto-bear")
    make("Unrelated", "e-ink-fridge-dashboard")

    text = crossproject.listing(mine)
    near, far = text.index("kingshot-auto-bear"), text.index("e-ink-fridge-dashboard")
    assert "[related to yours]" in text[near : near + 200]
    assert "[related to yours]" not in text[far : far + 200]


def test_the_tool_descriptions_address_the_run_s_own_principal():
    """A run on a project the owner is not on works for somebody else, and the
    description saying whose re-explaining it saves must name that person."""
    her = people.add("Erin", gender="female")
    hers = make("Hers", "hers")
    people.set_members(hers, [her])
    make("Her Other", "hers-other")
    people.set_members(
        int(db.get_project_by_slug("hers-other")["id"]), [her]
    )

    specs = crossproject.tool_specs(people.name_of(people.principal(hers)))
    context = next(s for s in specs if s["name"] == "project_context")
    assert "Erin" in context["description"]

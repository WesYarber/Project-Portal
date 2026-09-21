"""The skill that tells one project's agent how to start a run on another.

Filed against this project on 2026-09-21 by an agent on site-wide-tools, and
it had already cost a real run: the skill said

    `then=run` files the note **and** queues a run on that project. Omit it to
    leave the note without starting a run.

and the second sentence is false. `POST /project/<slug>/note` special-cases
`run`, `parallel` and `queue`; **everything else**, a missing `then` included,
falls through to the plain green "add note", which wakes a put-down project
and starts a run whenever one could start at all. An agent wanting to check a
slug existed posted a one-word "ping" with `then=none`, believing that filed
nothing - and the portal filed "ping" on a stranger's project and queued a run
on it.

So this file defends the SKILL against the ROUTE, which is the class of defect
that produced the note. Prose in a gitignored directory can drift from the code
forever and nothing notices; prose in the repo, checked here, cannot. The two
couplings that matter:

- every `then` value the route branches on appears in the skill's table, so
  adding a fifth one to `add_note` fails here until it is written down; and
- the sentence that got somebody hurt is asserted as *behavior*, not as a
  string match - a handful of plausible "don't run" words are posted at the
  real endpoint and every one of them must start a run.

The move that made this possible is its own fix: the skill was a promoted
skill under gitignored `data/skills/` (per-install, unpublished, untested) and
now ships from `app/skills/` like `terminal-style`. It says nothing about any
particular machine - `$OWNER`, `$THEY` and `$BASE_URL` are filled in at sync
time by `worker._localize_skill` - so it is publishable, and the office portal
and any clone get the corrected copy rather than this box's.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from string import Template

import pytest
from starlette.testclient import TestClient

from app import config, db, main, receipt, site, worker

SKILL = config.SKILLS_DIR / "kick-off-a-run-on-another-project" / "SKILL.md"


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _clean_worker_state():
    def reset():
        worker._inflight.clear()
        worker._wake = asyncio.Event()
        while not worker.manual_queue.empty():
            worker.manual_queue.get_nowait()

    reset()
    yield
    reset()


def text() -> str:
    return SKILL.read_text(encoding="utf-8")


def post(client, slug, **data):
    """The agent's call: curl does NOT follow redirects unless asked."""
    return client.post(f"/project/{slug}/note", data=data, follow_redirects=False)


def _project(slug="mtg-proxy-forge", stage="active"):
    p = db.create_project("Proxy Forge", description="cards", slug=slug)
    db.update_project(p["id"], stage=stage)
    return db.get_project(p["id"])


def queued() -> list[int]:
    return list(worker.manual_queue._queue)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# It ships at all, and it ships to somebody else's machine
# --------------------------------------------------------------------------


def test_the_skill_ships_from_the_publishable_tree():
    assert SKILL.is_file()
    body = text()
    assert body.startswith("---\n")
    assert "name: kick-off-a-run-on-another-project" in body
    assert "description:" in body


def test_the_skill_names_no_machine_and_no_port():
    """A publishable skill that hard-codes this box is a skill that lies on
    every other install. The substitution happens at sync time, so the source
    carries placeholders and the workspace copy carries the address."""
    body = text()
    host = site.SITE.host
    if host and host not in site.LOOPBACK_HOSTS:
        assert host not in body
    assert str(site.SITE.port) not in body
    assert "$BASE_URL" in body
    # And not a loopback address either, which `tests/test_no_localhost.py`
    # enforces across every shipped skill for a reason that bites here
    # exactly: an agent that read `127.0.0.1:8500` in a skill pastes it into
    # its report, and the person reading that report is on another device.
    assert "127.0.0.1" not in body and "localhost" not in body


def test_every_placeholder_in_it_is_one_the_portal_fills():
    """A `$WORD` with no variable behind it renders as a literal `$WORD` in
    front of the agent - which is how a skill quietly stops naming an address
    at all."""
    known = set(site.SITE.template_vars())
    used = set(re.findall(r"\$([A-Z][A-Z_]+)\b", text()))
    assert used <= known, sorted(used - known)


def test_substitution_leaves_a_real_address_behind():
    filled = Template(text()).safe_substitute(**site.SITE.template_vars())
    assert "$" not in filled
    assert f"{site.SITE.base_url}/project/<slug>/note" in filled


# --------------------------------------------------------------------------
# The skill's table of `then` values, against the route that reads them
# --------------------------------------------------------------------------


def _then_values_the_route_branches_on() -> set[str]:
    """Every literal `then` is compared against inside `add_note`.

    Read out of the source rather than listed here, so a fifth value added to
    the route is caught by this file instead of by an agent following prose
    that predates it.
    """
    source = inspect.getsource(main.add_note)
    return set(re.findall(r'then\s*[!=]=\s*"([a-z]+)"', source))


def test_the_scan_finds_the_route_s_own_values():
    """A lookup bug here would make the coupling below vacuously pass."""
    found = _then_values_the_route_branches_on()
    assert {"run", "parallel", "queue"} <= found
    assert len(found) >= 3


@pytest.mark.parametrize("value", sorted(_then_values_the_route_branches_on()))
def test_the_skill_documents_every_then_the_route_understands(value):
    assert f"`{value}`" in text(), f"the skill's table does not mention then={value}"


def test_the_skill_says_queue_is_the_only_one_that_starts_nothing():
    body = text()
    assert "`then=queue` is the only way to file a note WITHOUT starting a run" in body


def test_the_skill_no_longer_claims_omitting_then_files_quietly():
    """The exact retracted claim, in the shapes it could come back as."""
    body = text().lower()
    for wrong in (
        "omit it to leave the note without starting a run",
        "omit it to file the note without",
        "omitting it files the note without",
    ):
        assert wrong not in body


# --------------------------------------------------------------------------
# ...and the behavior the table describes, at the real endpoint
# --------------------------------------------------------------------------


@pytest.mark.parametrize("then", ["none", "no", "nothing", "false", "", "add"])
def test_anything_that_is_not_queue_starts_a_run(client, then):
    """The sentence that cost a run, asserted as behavior.

    Each of these reads like "do not run" to a person skimming an API. Every
    one of them takes the plain-note path.
    """
    p = _project()
    r = post(client, "mtg-proxy-forge", note="an agent needs X", then=then)
    assert r.status_code == 303
    assert queued() == [p["id"]]
    assert "run" in r.text


def test_an_absent_then_starts_a_run_too(client):
    """The case the skill got wrong: the parameter is not sent at all."""
    p = _project()
    r = client.post(
        "/project/mtg-proxy-forge/note",
        data={"note": "an agent needs X"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert queued() == [p["id"]]


def test_queue_is_the_one_value_that_starts_nothing(client):
    p = _project()
    r = post(client, "mtg-proxy-forge", note="a finding, not a request", then="queue")
    assert r.status_code == 303
    assert queued() == []
    assert "waits for the next run" in r.text
    # ...and it really did file. Not starting a run is not the same as no-op.
    assert any(
        "a finding, not a request" in (row["content_md"] or "")
        for row in db.list_journal(p["id"])
    )


def test_then_run_queues_a_run_as_the_table_says(client):
    p = _project(stage="backlog")
    r = post(client, "mtg-proxy-forge", note="please do X", then="run")
    assert r.status_code == 303
    assert queued() == [p["id"]]
    # "waking it if it was put down" - the table's claim about the shelf.
    assert db.display_state(db.get_project(p["id"])) == "active"


def test_then_hear_marks_the_note_for_delivery_mid_run(client):
    p = _project()
    post(client, "mtg-proxy-forge", note="stop, read this", then="hear")
    rows = [r for r in db.list_journal(p["id"]) if "stop, read this" in (r["content_md"] or "")]
    assert rows and rows[0]["hear_now"]


# --------------------------------------------------------------------------
# "You do not need to check the slug first"
# --------------------------------------------------------------------------


def test_an_unknown_slug_is_a_404(client):
    r = post(client, "no-such-project", note="hello", then="run")
    assert r.status_code == 404


def test_an_unknown_slug_files_nothing_and_starts_nothing(client):
    """The whole reason the skill can say "just post the real note": the slug
    lookup is the route's first line, ahead of every side effect. If a 404
    could file, a wrong guess would be as expensive as the probe it replaces.
    """
    p = _project()
    before = len(db.list_journal(p["id"]))
    post(client, "mtg-proxy-forgee", note="a typo'd slug", then="run")
    assert queued() == []
    assert len(db.list_journal(p["id"])) == before


def test_the_skill_quotes_the_404_body_the_route_actually_sends(client):
    r = post(client, "no-such-project", note="hello", then="run")
    assert r.json()["detail"] in text()


def test_the_skill_tells_the_agent_not_to_probe():
    body = text().lower()
    assert "ping" in body
    assert "404" in body


# --------------------------------------------------------------------------
# The receipt lines it prints as examples must be lines the code can emit
# --------------------------------------------------------------------------


def _example_receipt_lines() -> list[str]:
    return [line for line in text().splitlines() if line.startswith("ok: ")]


def test_the_examples_are_there_to_check():
    assert len(_example_receipt_lines()) >= 4


@pytest.mark.parametrize("line", _example_receipt_lines())
def test_every_example_line_is_one_the_receipt_can_really_produce(line):
    """Otherwise the skill teaches an agent to look for a sentence that no
    longer exists, and silence reads as failure again."""
    producible = {
        receipt.note_line("mtg-proxy-forge", filed=True, then="run"),
        receipt.note_line("mtg-proxy-forge", filed=True, then="queue"),
        receipt.note_line("mtg-proxy-forge", filed=True, then="", ran=False),
        receipt.note_line("mtg-proxy-forge", filed=True, then="", ran=True),
        receipt.note_line("mtg-proxy-forge", filed=False, then=""),
    }
    assert line in {p.rstrip() for p in producible}

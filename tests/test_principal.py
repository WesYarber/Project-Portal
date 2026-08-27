"""A project the owner is not on is not his, and the prompt must not say it is.

Wes, 2026-08-06: a project Karli created "keeps talking to her as if she were
me or as if she needed to get me to do stuff on that project rather than
talking to her as the project owner." The cause: the agent contract, the task
guidance and the project header were all rendered once at import with the
install owner's name, and profile.md - a document entirely about Wes - was
pasted in unframed. On her project the agent was literally instructed that it
"works on behalf of Wes."

What is pinned here, in the order it can go wrong:

- **The principal.** Owner on every project he is on (which is every project
  on a one-person install), the first member on a project he is not on.
- **The words.** Contract, guidance and project header rendered for a
  Karli-only project name her, with her pronouns, and never claim the work is
  on the owner's behalf.
- **Nothing shifts for the owner.** The per-project rendering of an
  owner-project is byte-identical to the module-level constant.
- **The profile is framed.** On a project the owner is not on, profile.md is
  introduced as being about somebody who is NOT the person the project
  belongs to.
"""
from __future__ import annotations

import pytest

from app import agent_runner, config, db, people, todos


@pytest.fixture
def wes():
    return people.owner()


@pytest.fixture
def karli():
    return people.get(people.add(name="Karli", gender="female"))


def _hers(karli):
    project = db.create_project(title="Hers", description="her idea", stage="active")
    people.set_members(project["id"], [int(karli["id"])])
    return db.get_project(project["id"])


# --------------------------------------------------------------------------
# The principal
# --------------------------------------------------------------------------

def test_the_owner_is_the_principal_of_his_own_project(wes):
    project = db.create_project(title="His", description="d")
    assert people.principal(project["id"])["id"] == wes["id"]


def test_the_first_member_is_the_principal_when_the_owner_is_not_on_it(wes, karli):
    project = _hers(karli)
    assert people.principal(project["id"])["id"] == karli["id"]


def test_a_shared_project_stays_the_owners(wes, karli):
    project = db.create_project(title="Ours", description="d")
    people.set_members(project["id"], [int(wes["id"]), int(karli["id"])])
    assert people.principal(project["id"])["id"] == wes["id"]


def test_no_project_resolves_to_the_owner(wes):
    assert people.principal(None)["id"] == wes["id"]


# --------------------------------------------------------------------------
# The vars
# --------------------------------------------------------------------------

def test_vars_for_the_owner_are_the_sites_untouched(wes):
    assert people.template_vars_for(wes) == config.SITE.template_vars()
    assert people.template_vars_for(None) == config.SITE.template_vars()


def test_vars_for_another_person_swap_the_person_and_keep_the_machine(karli):
    tvars = people.template_vars_for(karli)
    site_vars = config.SITE.template_vars()
    assert tvars["OWNER"] == "Karli"
    assert tvars["OWNERS"] == "Karli's"
    assert (tvars["THEY"], tvars["THEM"], tvars["THEIR"]) == ("she", "her", "her")
    assert tvars["HOST"] == site_vars["HOST"]
    assert tvars["BASE_URL"] == site_vars["BASE_URL"]
    assert tvars["PORTAL_ROOT"] == site_vars["PORTAL_ROOT"]


# --------------------------------------------------------------------------
# The rendered words
# --------------------------------------------------------------------------

def test_her_projects_contract_is_addressed_to_her(wes, karli):
    project = _hers(karli)
    contract = agent_runner.contract_for(project)
    assert "working on behalf of Karli" in contract
    assert f"working on behalf of {config.SITE.owner}" not in contract


def test_her_projects_guidance_is_addressed_to_her(wes, karli):
    project = _hers(karli)
    guidance = agent_runner.guidance_for("build", project)
    assert "ready for Karli to" in guidance


def test_his_projects_contract_is_byte_identical_to_the_constant(wes):
    project = db.create_project(title="His", description="d")
    assert agent_runner.contract_for(project) == agent_runner.AGENT_CONTRACT


def test_her_prompt_carries_her_name_in_the_project_header(wes, karli):
    project = _hers(karli)
    db.approve_build(project["id"])
    prompt = agent_runner.build_prompt("build", db.get_project(project["id"]))
    assert "Karli has approved building this" in prompt
    assert "Karli's original idea" in prompt


def test_her_prompt_frames_the_owners_profile(wes, karli, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROFILE_MD", tmp_path / "profile.md")
    config.PROFILE_MD.write_text("# Profile: Wes\nAll about him.", encoding="utf-8")
    project = _hers(karli)
    prompt = agent_runner.build_prompt("build", project)
    assert "NOT the person this project belongs to" in prompt
    assert "work for Karli" in prompt


def test_his_prompt_does_not_frame_his_own_profile(wes, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROFILE_MD", tmp_path / "profile.md")
    config.PROFILE_MD.write_text("# Profile: Wes\nAll about him.", encoding="utf-8")
    project = db.create_project(title="His", description="d")
    prompt = agent_runner.build_prompt("build", project)
    assert "NOT the person this project belongs to" not in prompt


def test_her_todo_trailer_names_her_not_the_owner(wes, karli):
    project = _hers(karli)
    db.add_todo(project["id"], "do a thing", owner="agent")
    section = todos.prompt_section(project["id"])
    assert "anything Karli has asked for" in section
    assert f"anything {config.SITE.owner} has asked for" not in section


def test_his_todo_trailer_still_names_him(wes):
    project = db.create_project(title="His", description="d")
    db.add_todo(project["id"], "do a thing", owner="agent")
    section = todos.prompt_section(project["id"])
    assert f"anything {config.SITE.owner} has asked for" in section

"""The verification-depth section: Wes's 2026-08-07 answer, in the prompt.

He asked how thoroughly a routine run should verify itself and answered "sweep
only when logic changed". The obligation it replaces was never in the code - it
lived in the journal, where twenty entries in a row described a heavy mutation
sweep and every new run copied them. So the tests that matter here are not
"does the string exist": they are that the section reaches a build prompt, that
it lands ABOVE the journal it has to outrank, that it says out loud not to copy
the journal, and that no setting can buy a run permission to lie about a test it
did not run.
"""
from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, settings_form, verifydepth


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _project():
    return db.get_project(db.create_project("Depth", "d", stage="active")["id"])


# --- The setting -----------------------------------------------------------

def test_the_default_is_proportionate(temp_data_dir):
    """His answer, not the old behavior: a fresh install scales to the change."""
    assert verifydepth.current_depth() == "proportionate"
    assert config.DEFAULT_SETTINGS[verifydepth.SETTING_KEY] == "proportionate"


def test_a_set_depth_is_honored(temp_data_dir):
    db.set_setting(verifydepth.SETTING_KEY, "light")
    assert verifydepth.current_depth() == "light"


def test_an_unknown_value_falls_back_rather_than_raising(temp_data_dir):
    """An older build, a hand-edited row, a typo in a migration. Guidance is
    never worth losing a run over, so anything unrecognized reads as the
    default."""
    db.set_setting(verifydepth.SETTING_KEY, "paranoid")
    assert verifydepth.current_depth() == "proportionate"
    assert verifydepth.prompt_section("build")


def test_an_unknown_explicit_depth_falls_back_rather_than_raising(temp_data_dir):
    """The setting is not the only way in - `prompt_section` takes a depth
    directly. Anything unrecognized there has to land on the configured depth,
    not index straight into `_BODIES` and raise a KeyError inside build_prompt.
    Found by the sweep: `chosen = depth or current_depth()` escaped every test
    until this one existed."""
    db.set_setting(verifydepth.SETTING_KEY, "light")
    text = verifydepth.prompt_section("build", depth="paranoid")
    assert "no mutation sweep at all" in text


def test_a_blank_value_falls_back_too(temp_data_dir):
    """What a cleared field posts. The literal default in `current_depth` is
    only reachable through a blank or missing row, so this is the case that
    actually exercises it."""
    db.set_setting(verifydepth.SETTING_KEY, "")
    assert verifydepth.current_depth() == "proportionate"


def test_the_setting_is_registered_and_validated(temp_data_dir):
    """A field the form does not own is silently dropped on save - the exact
    bug test_settings_form.py exists for."""
    assert verifydepth.SETTING_KEY in settings_form.REGISTRY
    kept = settings_form.apply(
        {verifydepth.SETTING_KEY: "thorough"}, verifydepth.SETTING_KEY
    )
    assert kept[verifydepth.SETTING_KEY] == "thorough"
    junk = settings_form.apply(
        {verifydepth.SETTING_KEY: "nonsense"}, verifydepth.SETTING_KEY
    )
    assert junk[verifydepth.SETTING_KEY] == "proportionate"


def test_every_choice_has_a_label_for_the_dropdown():
    """A value with no label renders as a blank option - selectable, and it
    reads as a bug."""
    assert [value for value, _ in verifydepth.DEPTH_LABELS] == sorted(
        verifydepth.DEPTH_CHOICES
    )
    assert all(label.strip() for _, label in verifydepth.DEPTH_LABELS)


def test_the_settings_page_declares_the_field_and_offers_every_choice(client):
    """Checked on the RENDERED page. A key missing from `_fields` is never
    looked at by `apply`, so the dropdown moves and the setting silently does
    not save - the failure mode that reads as the page being broken."""
    page = client.get("/settings").text
    declared: set[str] = set()
    for value in re.findall(r'name="_fields" value="([^"]*)"', page):
        declared.update(value.split(","))
    assert verifydepth.SETTING_KEY in declared
    for value, label in verifydepth.DEPTH_LABELS:
        assert f'value="{value}"' in page
        assert label in page


def test_the_dropdown_shows_what_is_actually_set(client):
    db.set_setting(verifydepth.SETTING_KEY, "thorough")
    page = client.get("/settings").text
    assert re.search(r'value="thorough"\s+selected', page)


def test_saving_a_depth_sticks(client):
    resp = client.post(
        "/settings",
        data={"_fields": verifydepth.SETTING_KEY, verifydepth.SETTING_KEY: "light"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert verifydepth.current_depth() == "light"


# --- What each depth actually says -----------------------------------------

def test_proportionate_permits_a_sweep_only_for_logic(temp_data_dir):
    text = verifydepth.prompt_section("build", depth="proportionate")
    assert "sweep only when logic changed" in text
    # The tiers have to be nameable from the diff, or the advice is unusable.
    for tier in ("Docs", "Tests, fixtures or data only", "Logic"):
        assert tier in text
    assert "nothing is owed" in text


def test_light_forbids_the_sweep_outright(temp_data_dir):
    text = verifydepth.prompt_section("build", depth="light")
    assert "no mutation sweep at all" in text
    assert "sweep only when logic changed" not in text


def test_thorough_is_the_old_behavior(temp_data_dir):
    text = verifydepth.prompt_section("build", depth="thorough")
    assert "full test suite" in text
    assert "mutation sweep" in text


@pytest.mark.parametrize("depth", verifydepth.DEPTH_CHOICES)
def test_no_depth_buys_a_dirty_commit_or_an_unwatched_result(temp_data_dir, depth):
    """The floor, and it is not for sale. This setting decides how much proving
    a change is worth - never whether a claim in the report has to be true."""
    text = verifydepth.prompt_section("build", depth=depth)
    assert "green before you commit" in text
    assert "did not actually watch run" in text


@pytest.mark.parametrize("depth", ("proportionate", "light"))
def test_the_lighter_depths_say_not_to_copy_the_journal(temp_data_dir, depth):
    """The load-bearing sentence. Twenty journal entries describing a twenty-
    case sweep sit below this section in the same prompt, and an agent reads
    them as the local standard unless it is told they are not."""
    text = verifydepth.prompt_section("build", depth=depth)
    assert "not an instruction for this one" in text
    assert "derive it from your own diff" in text


# --- Where it lands --------------------------------------------------------

def test_a_build_prompt_carries_the_section(temp_data_dir):
    prompt = agent_runner.build_prompt("build", _project())
    assert "## How much to verify this run" in prompt


def test_it_sits_above_the_journal_it_has_to_outrank(temp_data_dir):
    """Order is the whole mechanism. Below the journal it is one more opinion
    among twenty entries that all say otherwise; above it, it is the standing
    rule and they are history."""
    prompt = agent_runner.build_prompt("build", _project())
    assert prompt.index("## How much to verify this run") < prompt.index(
        "## Recent journal"
    )


@pytest.mark.parametrize("task", ("triage", "plan", "research"))
def test_a_run_that_writes_no_code_is_not_told_how_to_test(temp_data_dir, task):
    """Those prompts are budgeted to the byte, and telling a triage run about
    mutation sweeps is noise that pushes something useful out."""
    assert verifydepth.prompt_section(task) == ""
    assert "## How much to verify this run" not in agent_runner.build_prompt(
        task, _project()
    )


def test_the_depth_setting_changes_the_prompt(temp_data_dir):
    project = _project()
    db.set_setting(verifydepth.SETTING_KEY, "thorough")
    heavy = agent_runner.build_prompt("build", project)
    db.set_setting(verifydepth.SETTING_KEY, "light")
    slim = agent_runner.build_prompt("build", project)
    assert "no mutation sweep at all" in slim
    assert "no mutation sweep at all" not in heavy


def test_the_section_survives_a_broken_settings_read(temp_data_dir, monkeypatch):
    """Fail-open, like every other prompt section: a run is worth far more than
    the paragraph, so a fault here degrades to the default text rather than
    killing build_prompt."""
    # Set to something other than the default first, so a patch that failed to
    # bite would read "light" back and this test would fail rather than pass
    # for the wrong reason.
    db.set_setting(verifydepth.SETTING_KEY, "light")
    assert verifydepth.current_depth() == "light"

    def boom(*a, **k):
        raise RuntimeError("no database today")

    monkeypatch.setattr(db, "get_setting", boom)
    assert verifydepth.current_depth() == "proportionate"
    assert "## How much to verify this run" in verifydepth.prompt_section("build")

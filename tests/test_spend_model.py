"""Spend-down model upgrade: while a spend-down is running, a run pinned to a
cheaper model is routed up to the spend model, so an expiring weekly window is
spent on quality rather than only on more runs.

Wes's precedent for overriding a cost pin: a research burst already ignores a
project's cheaper model because a burst spends allowance that would be lost
anyway. This extends the same reasoning to an approved spend-down - but only
ever *upgrades*, and only during the explicit spend-down, never the automatic
ahead-of-pace boost (which just runs more often).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import agent_runner, config, db, limits, pacing


def _spend_down_on(hours: float = 3.0) -> None:
    """Open a spend-down window against the real clock, so spending_down() with
    no `now` (as resolve_model calls it) reads it as active."""
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    db.set_setting(pacing.ACTIVE_UNTIL, until.isoformat(timespec="seconds"))


# --------------------------------------------------------------------------
# spend_model(): the target, from the setting
# --------------------------------------------------------------------------


def test_spend_model_defaults_to_opus(temp_data_dir):
    assert pacing.spend_model() == "opus"


def test_spend_model_reads_the_setting(temp_data_dir):
    db.set_setting(pacing.SPEND_MODEL_SETTING, "sonnet")
    assert pacing.spend_model() == "sonnet"


@pytest.mark.parametrize("value", ["", "   ", "gpt-5", "nonsense"])
def test_spend_model_falls_back_to_default_on_junk(temp_data_dir, value):
    db.set_setting(pacing.SPEND_MODEL_SETTING, value)
    assert pacing.spend_model() == pacing.DEFAULT_SPEND_MODEL


# --------------------------------------------------------------------------
# route_for_spend(): the upgrade decision (pure)
# --------------------------------------------------------------------------


def test_no_upgrade_when_no_spend_down(temp_data_dir):
    # No spend-down running: a cheap pin runs exactly as pinned.
    assert pacing.route_for_spend("haiku") == ("haiku", "")


def test_cheaper_pin_upgrades_during_spend_down(temp_data_dir):
    _spend_down_on()
    model, why = pacing.route_for_spend("haiku")
    assert model == "opus"
    assert "opus" in why and "haiku" in why


def test_sonnet_upgrades_to_opus_during_spend_down(temp_data_dir):
    _spend_down_on()
    assert pacing.route_for_spend("sonnet")[0] == "opus"


def test_the_target_model_is_never_upgraded(temp_data_dir):
    _spend_down_on()
    assert pacing.route_for_spend("opus") == ("opus", "")


def test_a_model_above_the_target_is_left_alone(temp_data_dir):
    # Target sonnet: opus outranks it, so a spend-down never *lowers* opus to it.
    _spend_down_on()
    db.set_setting(pacing.SPEND_MODEL_SETTING, "sonnet")
    assert pacing.route_for_spend("opus") == ("opus", "")


def test_equal_rank_is_not_upgraded(temp_data_dir):
    # fable and sonnet share a spend rank, so a fable pin is not churned to
    # sonnet (and vice versa) - only a strictly cheaper model moves.
    _spend_down_on()
    db.set_setting(pacing.SPEND_MODEL_SETTING, "sonnet")
    assert pacing.route_for_spend("fable") == ("fable", "")


def test_a_custom_target_is_honored(temp_data_dir):
    _spend_down_on()
    db.set_setting(pacing.SPEND_MODEL_SETTING, "sonnet")
    assert pacing.route_for_spend("haiku")[0] == "sonnet"


def test_research_bursts_keep_their_own_model(temp_data_dir):
    # A burst carries its own model (fable) and is the spend-down's mechanism,
    # not a project whose cost pin needs overriding.
    _spend_down_on()
    assert pacing.route_for_spend("fable", task="research") == ("fable", "")


def test_an_unknown_model_is_never_touched(temp_data_dir):
    _spend_down_on()
    assert pacing.route_for_spend("gpt-5") == ("gpt-5", "")


# --------------------------------------------------------------------------
# resolve_model(): the three layers together
# --------------------------------------------------------------------------


def test_resolve_upgrades_the_global_default_during_spend_down(temp_data_dir):
    db.set_setting("worker_model", "haiku")
    assert agent_runner.resolve_model(None) == "haiku"  # no spend-down yet
    _spend_down_on()
    assert agent_runner.resolve_model(None) == "opus"


def test_resolve_upgrades_a_project_pin_during_spend_down(temp_data_dir):
    project = db.create_project("Thing", slug="thing", stage="active")
    db.update_project(project["id"], model="sonnet")
    row = db.get_project(project["id"])
    assert agent_runner.resolve_model(row) == "sonnet"  # no spend-down yet
    _spend_down_on()
    assert agent_runner.resolve_model(row) == "opus"


def test_resolve_leaves_a_research_burst_on_its_own_model(temp_data_dir):
    _spend_down_on()
    db.set_setting("research_model", "fable")
    assert agent_runner.resolve_model(None, "research") == "fable"


def test_spend_down_upgrade_supersedes_the_fable_fallback(temp_data_dir):
    # A fable global during a spend-down goes straight to opus by the upgrade,
    # not by the usage fallback - so it happens even with the window wide open.
    db.set_setting("worker_model", "fable")
    _spend_down_on()
    assert agent_runner.resolve_model(None) == "opus"


# --------------------------------------------------------------------------
# The dashboard line announces the upgrade
# --------------------------------------------------------------------------


def test_status_line_names_the_upgrade_target(temp_data_dir):
    _spend_down_on()
    line = pacing.status_line()
    assert "spending down" in line
    assert "upgraded to opus" in line


def test_status_line_names_a_custom_target(temp_data_dir):
    _spend_down_on()
    db.set_setting(pacing.SPEND_MODEL_SETTING, "sonnet")
    assert "upgraded to sonnet" in pacing.status_line()


# --------------------------------------------------------------------------
# The setting round-trips through the form validator
# --------------------------------------------------------------------------


def test_setting_is_validated_by_the_form(temp_data_dir):
    from app import settings_form

    cleaned = settings_form.apply(
        {"_fields": "spend_down_model", "spend_down_model": "sonnet"}
    )
    assert cleaned["spend_down_model"] == "sonnet"


def test_form_rejects_a_bogus_model(temp_data_dir):
    from app import settings_form

    cleaned = settings_form.apply(
        {"_fields": "spend_down_model", "spend_down_model": "gpt-5"}
    )
    assert cleaned["spend_down_model"] == config.DEFAULT_MODEL

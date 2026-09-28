"""Per-project agent override and the global default."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def test_default_model_is_opus():
    assert config.DEFAULT_MODEL == "opus"
    assert db.get_setting("worker_model") == "opus"


def test_project_with_no_override_inherits_global():
    project = db.create_project("Thing")
    db.set_setting("worker_model", "sonnet")
    assert agent_runner.resolve_model(db.get_project(project["id"])) == "sonnet"


def test_project_override_wins():
    project = db.create_project("Thing")
    db.set_setting("worker_model", "sonnet")
    db.update_project(project["id"], model="haiku")
    assert agent_runner.resolve_model(db.get_project(project["id"])) == "haiku"


def test_clearing_override_falls_back_to_global():
    project = db.create_project("Thing")
    db.update_project(project["id"], model="haiku")
    db.update_project(project["id"], model=None)
    db.set_setting("worker_model", "sonnet")
    assert agent_runner.resolve_model(db.get_project(project["id"])) == "sonnet"


def test_unknown_override_is_ignored():
    project = db.create_project("Thing")
    db.update_project(project["id"], model="gpt-9")
    db.set_setting("worker_model", "sonnet")
    assert agent_runner.resolve_model(db.get_project(project["id"])) == "sonnet"


def test_unknown_global_falls_back_to_default():
    db.set_setting("worker_model", "banana")
    assert agent_runner.resolve_model(None) == config.DEFAULT_MODEL


def test_reflect_run_uses_global_model():
    db.set_setting("worker_model", "haiku")
    assert agent_runner.resolve_model(None) == "haiku"


# ---------------------------------------------------------------------------
# Pinning an alias to an explicit model id, and the CLI version that gates it
# ---------------------------------------------------------------------------
# Wes answered "adopt it" to Fable 5.1 on 2026-09-02. `--model fable` still
# means claude-fable-5 on CLI 2.1.258, so the explicit id is the only way to
# reach 5.1 - and an older CLI answers that id with a 400 rather than running
# it, which is what MODEL_MIN_CLI guards.


def _at_cli_version(monkeypatch, version):
    """Pretend a given CLI is installed, bypassing the module-level cache."""
    monkeypatch.setattr(config, "cli_version", lambda: version)


def test_fable_spawns_fable_5_1_on_a_current_cli(monkeypatch):
    _at_cli_version(monkeypatch, "2.1.258")
    assert config.cli_model("fable") == "claude-fable-5-1"


def test_fable_degrades_to_the_bare_alias_on_an_old_cli(monkeypatch):
    # 2.1.223 is what this box ran before the adoption, and it answers the
    # explicit id with "version 2.1.251 or newer is required".
    _at_cli_version(monkeypatch, "2.1.223")
    assert config.cli_model("fable") == "fable"


def test_the_gate_opens_exactly_at_the_required_version(monkeypatch):
    required = config.MODEL_MIN_CLI["fable"]
    _at_cli_version(monkeypatch, required)
    assert config.cli_model("fable") == "claude-fable-5-1"


def test_one_patch_below_the_requirement_still_degrades(monkeypatch):
    major, minor, patch = config._version_tuple(config.MODEL_MIN_CLI["fable"])
    _at_cli_version(monkeypatch, f"{major}.{minor}.{patch - 1}")
    assert config.cli_model("fable") == "fable"


def test_a_newer_major_cli_clears_the_gate(monkeypatch):
    _at_cli_version(monkeypatch, "3.0.0")
    assert config.cli_model("fable") == "claude-fable-5-1"


def test_an_unreadable_cli_version_degrades_rather_than_400s(monkeypatch):
    # cli_version() falls back to DEFAULT_CLI_VERSION when `claude --version`
    # cannot be read at all. That fallback MUST stay below every requirement,
    # or an install with no detectable CLI spawns an id it may not support.
    _at_cli_version(monkeypatch, config.DEFAULT_CLI_VERSION)
    assert config.cli_model("fable") == "fable"
    for alias, required in config.MODEL_MIN_CLI.items():
        assert config._version_tuple(config.DEFAULT_CLI_VERSION) < config._version_tuple(
            required
        ), f"DEFAULT_CLI_VERSION must be below {alias}'s requirement"


def test_an_ungated_pin_is_not_held_back_by_an_ancient_cli(monkeypatch):
    # A pin carrying no MODEL_MIN_CLI entry can be withheld by no version at
    # all. A gate that applied to every pin would silently downgrade every run
    # on this portal to whatever the bare alias happens to mean.
    #
    # Stated with a pin of its own rather than by naming whichever shipped
    # alias is ungated this month: opus used to be the example here and became
    # gated the day Opus 5.5 was adopted, which made this test fail for a
    # reason that had nothing to do with the property it exists to hold.
    monkeypatch.setitem(config.CLI_MODEL_IDS, "sonnet", "claude-sonnet-5")
    assert "sonnet" not in config.MODEL_MIN_CLI

    _at_cli_version(monkeypatch, "0.0.1")
    assert config.cli_model("sonnet") == "claude-sonnet-5"
    # Not even an unreadable version may withhold an ungated pin. cli_version()
    # regex-checks its output before returning it, so this should be
    # unreachable today - but the gate must not lean on that, since an empty
    # parse sorts BELOW every requirement and would strip the pin from every run.
    _at_cli_version(monkeypatch, "garbage")
    assert config.cli_model("sonnet") == "claude-sonnet-5"


def test_an_unpinned_alias_passes_through_untouched(monkeypatch):
    _at_cli_version(monkeypatch, "2.1.258")
    assert config.cli_model("sonnet") == "sonnet"
    assert config.cli_model("haiku") == "haiku"
    assert config.cli_model("gpt-9") == "gpt-9"


def test_every_pinned_alias_is_a_real_portal_model():
    # A pin for an alias no dropdown offers is dead code that reads as coverage.
    for alias in config.CLI_MODEL_IDS:
        assert alias in config.MODEL_VALUES
    for alias in config.MODEL_MIN_CLI:
        assert alias in config.CLI_MODEL_IDS, "a gate with no pin gates nothing"


def test_the_dropdown_names_fable_5_1():
    assert dict(config.MODEL_CHOICES)["fable"] == "Fable 5.1"


def test_research_bursts_ride_the_adopted_fable(monkeypatch):
    # RESEARCH_MODEL is the one setting that reaches for the newest model by
    # design, so the adoption has to actually land there.
    _at_cli_version(monkeypatch, "2.1.258")
    assert config.RESEARCH_MODEL == "fable"
    assert config.cli_model(config.RESEARCH_MODEL) == "claude-fable-5-1"


def test_version_tuple_truncates_at_a_non_numeric_part():
    assert config._version_tuple("2.1.258") == (2, 1, 258)
    assert config._version_tuple("2.2.0-rc1") == (2, 2)
    assert config._version_tuple("garbage") == ()
    # It STOPS at the bad part rather than skipping over it: "2.x.5" is (2,),
    # not (2, 5). Skipping would read a trailing number as if it were the minor
    # version and compare two different fields against each other.
    assert config._version_tuple("2.x.5") == (2,)
    # An unparseable version must not sort ABOVE a requirement, or it would
    # open the gate it cannot answer for.
    assert config._version_tuple("garbage") < config._version_tuple("2.1.251")


def test_versions_compare_numerically_not_as_strings(monkeypatch):
    # The gate's whole job is refusing the pinned id to a CLI that would 400 on
    # it, and a string comparison gets exactly that case backwards: "2.1.99" is
    # four dozen releases OLDER than "2.1.251", but sorts after it as text
    # because "9" > "2". Compared as strings, the oldest CLIs most in need of
    # the fallback are the ones that would be handed the pinned id.
    _at_cli_version(monkeypatch, "2.1.99")
    assert "2.1.99" > "2.1.251", "the string comparison this guards against"
    assert config.cli_model("fable") == "fable"


def test_a_two_digit_minor_version_is_newer_than_a_one_digit_one():
    # The same trap one field to the left, and the one that arrives on its own
    # the day the CLI reaches 2.10: as text "2.10.0" sorts BELOW "2.9.0".
    assert config._version_tuple("2.10.0") > config._version_tuple("2.9.0")


# ---------------------------------------------------------------------------
# Saying so: a pin this CLI cannot spawn is a downgrade, and it must be visible
# ---------------------------------------------------------------------------
# Wes, 2026-09-28: "It seems the current run used Opus 5 instead of 5.5 like I
# expect and is set up in the settings." It had: this machine's CLI was 2.1.258
# and `claude-opus-5-5` needs 2.1.280, so every run degraded to the bare alias.
# The pin arrived in a code update from the publishing node rather than from an
# adoption here, so nothing had ever announced the gate.


def test_degraded_models_names_the_alias_the_cli_cannot_spawn(monkeypatch):
    _at_cli_version(monkeypatch, "2.1.258")
    rows = {row["alias"]: row for row in config.degraded_models()}
    assert "opus" in rows
    assert rows["opus"]["model_id"] == "claude-opus-5-5"
    assert rows["opus"]["required_cli"] == config.MODEL_MIN_CLI["opus"]
    assert rows["opus"]["installed_cli"] == "2.1.258"
    assert rows["opus"]["label"] == dict(config.model_choices())["opus"]


def test_nothing_is_degraded_on_a_cli_new_enough_for_every_pin(monkeypatch):
    _at_cli_version(monkeypatch, "99.0.0")
    assert config.degraded_models() == []


def test_the_gate_closing_is_what_puts_a_row_on_the_page(monkeypatch):
    required = config.MODEL_MIN_CLI["opus"]
    major, minor, patch = config._version_tuple(required)

    _at_cli_version(monkeypatch, f"{major}.{minor}.{patch - 1}")
    assert "opus" in {row["alias"] for row in config.degraded_models()}

    _at_cli_version(monkeypatch, required)
    assert "opus" not in {row["alias"] for row in config.degraded_models()}


def test_an_ungated_pin_is_never_called_degraded(monkeypatch):
    # The mirror of test_an_ungated_pin_is_not_held_back_by_an_ancient_cli: a
    # pin with no version requirement spawns on any CLI, so reporting it as a
    # downgrade would put a permanent false warning on the settings page.
    monkeypatch.setitem(config.CLI_MODEL_IDS, "sonnet", "claude-sonnet-5")
    assert "sonnet" not in config.MODEL_MIN_CLI
    _at_cli_version(monkeypatch, "0.0.1")
    assert "sonnet" not in {row["alias"] for row in config.degraded_models()}


def test_an_unpinned_alias_is_never_called_degraded(monkeypatch):
    _at_cli_version(monkeypatch, "0.0.1")
    aliases = {row["alias"] for row in config.degraded_models()}
    assert "haiku" not in aliases


def test_the_warning_and_the_spawn_can_never_disagree(monkeypatch):
    # The property the whole thing rests on: a page claiming a model is
    # degraded while the spawn uses the pinned id (or the reverse) is worse
    # than no page at all. Both sides read one decision, and this holds them
    # to it across every version that matters.
    for version in ("0.0.1", "2.1.223", "2.1.258", "2.1.280", "99.0.0"):
        _at_cli_version(monkeypatch, version)
        degraded = {row["alias"] for row in config.degraded_models()}
        for alias in config.MODEL_VALUES:
            pinned = config.CLI_MODEL_IDS.get(alias)
            if pinned is None:
                assert alias not in degraded, version
                continue
            assert (alias in degraded) == (config.cli_model(alias) != pinned), (
                f"{alias} at CLI {version}"
            )


def test_a_row_carries_the_label_an_adoption_gave_the_alias(monkeypatch):
    from app import modeladopt

    _at_cli_version(monkeypatch, "2.1.258")
    modeladopt.record("opus", "claude-opus-5-5", "2.1.280", label="Opus 5.5")
    rows = {row["alias"]: row for row in config.degraded_models()}
    assert rows["opus"]["label"] == "Opus 5.5"


def test_the_settings_page_says_a_pin_is_not_what_runs(monkeypatch, client):
    _at_cli_version(monkeypatch, "2.1.258")
    page = client.get("/settings").text
    assert "is not what runs on this machine" in page
    assert config.MODEL_MIN_CLI["opus"] in page
    assert "2.1.258" in page
    assert "claude-opus-5-5" in page
    assert "claude update" in page


def test_the_settings_page_stays_quiet_when_every_pin_is_live(monkeypatch, client):
    _at_cli_version(monkeypatch, "99.0.0")
    page = client.get("/settings").text
    assert "is not what runs on this machine" not in page
    # Not even the empty box it would live in. A warning element with no rows
    # in it still paints - a yellow paragraph's margins on a healthy install -
    # and asserting only on the text leaves that invisible to the suite.
    assert 'id="model-degraded"' not in page


def test_the_warning_box_exists_only_while_something_is_degraded(monkeypatch, client):
    _at_cli_version(monkeypatch, "2.1.258")
    assert 'id="model-degraded"' in client.get("/settings").text

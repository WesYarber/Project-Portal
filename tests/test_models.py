"""Per-project agent override and the global default."""
from __future__ import annotations

from app import agent_runner, config, db


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
    # opus is pinned but carries no MODEL_MIN_CLI entry, so no version can
    # withhold it. A gate that applied to every pin would silently downgrade
    # every run on this portal to whatever `--model opus` happens to mean.
    _at_cli_version(monkeypatch, "0.0.1")
    assert "opus" not in config.MODEL_MIN_CLI
    assert config.cli_model("opus") == "claude-opus-5"


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
    # An unparseable version must not sort ABOVE a requirement, or it would
    # open the gate it cannot answer for.
    assert config._version_tuple("garbage") < config._version_tuple("2.1.251")

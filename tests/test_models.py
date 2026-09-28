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


# ---------------------------------------------------------------------------
# The cached CLI version has to notice `claude update`
# ---------------------------------------------------------------------------
# Wes, 2026-09-28: "the current run used Opus 5 instead of 5.5 like I expect".
# It had, because this machine's CLI was older than MODEL_MIN_CLI["opus"] and
# the gate did its job. The part that is a defect is what happened next:
# `claude update` ran, and the portal - up for three days - went on spawning
# the older Opus, because `cli_version()` was memoized for the life of the
# process and the only thing that re-read it was a daily check already stamped
# for that day. So the version is memoized against the binary it was read
# from, and a `claude` that is not that binary any more is read again.


# `temp_data_dir` pins `refresh_cli_version` for every test, so that the daily
# model check's deliberate re-read cannot walk past the fence and shell out to
# the real CLI. Captured here at import, before any fixture runs, so the one
# test that is ABOUT that function can call the genuine one.
_REAL_REFRESH = config.refresh_cli_version


def _fake_cli(tmp_path, version, name="claude", counter=None):
    """A `claude` on PATH that prints a version, and optionally counts calls."""
    home = tmp_path / "fakebin"
    home.mkdir(exist_ok=True)
    path = home / name
    tally = f'echo x >> "{counter}"\n' if counter else ""
    path.write_text(f'#!/bin/sh\n{tally}echo "{version} (Claude Code)"\n')
    path.chmod(0o755)
    return path


def _unmemoized(monkeypatch, tmp_path):
    """A fresh cache with PATH pointing only at the fake bin directory."""
    monkeypatch.setenv("PATH", str(tmp_path / "fakebin"))
    monkeypatch.setattr(config, "_cli_version_cache", None, raising=False)
    monkeypatch.setattr(config, "_cli_binary_stamp", None, raising=False)


def _one_below(version):
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) - 1}"


def test_an_updated_cli_is_read_again_without_a_restart(monkeypatch, tmp_path):
    _fake_cli(tmp_path, "2.1.258")
    _unmemoized(monkeypatch, tmp_path)
    assert config.cli_version() == "2.1.258"

    # `claude update`, underneath a process that already has an answer.
    _fake_cli(tmp_path, "2.1.283")
    assert config.cli_version() == "2.1.283"


def test_the_same_binary_is_read_only_once(monkeypatch, tmp_path):
    # The point of memoizing at all: this is consulted at every spawn and on
    # the usage poller's path, so it must not fork `claude --version` per call.
    counter = tmp_path / "calls"
    _fake_cli(tmp_path, "2.1.283", counter=counter)
    _unmemoized(monkeypatch, tmp_path)
    for _ in range(5):
        assert config.cli_version() == "2.1.283"
    assert counter.read_text().count("x") == 1


def test_a_cache_nobody_stamped_is_never_second_guessed(monkeypatch, tmp_path):
    # tests/conftest.py fences the whole suite off the real binary by pinning
    # `_cli_version_cache` and nothing else. If an unstamped cache were checked
    # against whatever `claude` is on PATH, every test in the suite would shell
    # out to the real CLI - so this holds that fence from the other side.
    counter = tmp_path / "calls"
    _fake_cli(tmp_path, "2.1.283", counter=counter)
    monkeypatch.setenv("PATH", str(tmp_path / "fakebin"))
    monkeypatch.setattr(config, "_cli_version_cache", "1.2.3", raising=False)
    monkeypatch.setattr(config, "_cli_binary_stamp", None, raising=False)
    assert config.cli_version() == "1.2.3"
    assert not counter.exists(), "an unstamped cache shelled out"


def test_a_repointed_version_symlink_is_noticed(monkeypatch, tmp_path):
    # The shape a native install actually has: ~/.local/bin/claude is a symlink
    # into ~/.local/share/claude/versions/<version>, and `claude update` writes
    # a new version directory and repoints the link. The link's own mtime and
    # size say nothing about which version it names, so the stamp has to be
    # taken from the resolved path - here the two targets are made identical in
    # size and mtime so the path is the ONLY thing that differs.
    versions = tmp_path / "versions"
    versions.mkdir()
    old = _fake_cli(tmp_path, "2.1.258", name="old")
    new = _fake_cli(tmp_path, "2.1.283", name="new")
    assert old.stat().st_size == new.stat().st_size, "same size on purpose"
    import os
    os.utime(new, ns=(old.stat().st_mtime_ns, old.stat().st_mtime_ns))
    link = tmp_path / "fakebin" / "claude"
    link.symlink_to(old)
    _unmemoized(monkeypatch, tmp_path)
    assert config.cli_version() == "2.1.258"

    link.unlink()
    link.symlink_to(new)
    assert config.cli_version() == "2.1.283"


def test_a_cli_that_appears_later_is_noticed(monkeypatch, tmp_path):
    # No `claude` at all is itself a stamp: the fallback version must not be
    # kept forever once one is installed.
    (tmp_path / "fakebin").mkdir()
    _unmemoized(monkeypatch, tmp_path)
    assert config.cli_version() == config.DEFAULT_CLI_VERSION

    _fake_cli(tmp_path, "2.1.283")
    assert config.cli_version() == "2.1.283"


def test_refresh_re_reads_a_cli_replaced_without_a_trace(monkeypatch, tmp_path):
    # Same path, same size, same mtime to the nanosecond - a stamp collision,
    # which is why the daily model check's deliberate refresh still has a job.
    import os
    path = _fake_cli(tmp_path, "2.1.258")
    stamp = path.stat().st_mtime_ns
    _unmemoized(monkeypatch, tmp_path)
    assert config.cli_version() == "2.1.258"

    path.write_text(path.read_text().replace("2.1.258", "2.1.283"))
    os.utime(path, ns=(stamp, stamp))
    assert config.cli_version() == "2.1.258", "the collision this describes"
    assert _REAL_REFRESH() == "2.1.283"


def test_a_cli_replaced_at_the_same_instant_is_noticed_by_its_size(monkeypatch,
                                                                   tmp_path):
    # Path and mtime both unchanged - the size is the only thing left to see it
    # by. A sweep survivor on 2026-09-28: every other test here differs in the
    # path or the clock as well, so dropping size from the stamp went unnoticed.
    import os
    path = _fake_cli(tmp_path, "2.1.258")
    stamp = path.stat().st_mtime_ns
    _unmemoized(monkeypatch, tmp_path)
    assert config.cli_version() == "2.1.258"

    _fake_cli(tmp_path, "2.1.283.1")  # one character longer, same path
    os.utime(path, ns=(stamp, stamp))
    assert path.stat().st_mtime_ns == stamp, "the clock is unchanged on purpose"
    assert config.cli_version() == "2.1.283.1"


def test_a_binary_that_vanishes_between_which_and_stat_does_not_raise(monkeypatch,
                                                                     tmp_path):
    # `shutil.which` says yes, and the file is gone by the `stat` a line later.
    # Narrow, but this runs on the path walked at every spawn, and the thing
    # most likely to replace the binary mid-read is the `claude update` the
    # stamp exists to notice - so it must degrade to the fallback, not raise.
    (tmp_path / "fakebin").mkdir()
    _unmemoized(monkeypatch, tmp_path)
    monkeypatch.setattr(config.shutil, "which",
                        lambda _name: str(tmp_path / "gone" / "claude"))
    assert config._binary_stamp() == (), "a failed stat is a stamp, not a crash"
    assert config.cli_version() == config.DEFAULT_CLI_VERSION


def test_a_model_gate_opens_as_soon_as_the_cli_is_updated(monkeypatch, tmp_path):
    # Wes's report, end to end. Below the gate the spawn degrades to the bare
    # alias (an older Opus, but running); the moment `claude update` has run,
    # the very next spawn gets the pinned id - no restart, nothing to re-read
    # by hand, no waiting for tomorrow's model check.
    required = config.MODEL_MIN_CLI["opus"]
    _fake_cli(tmp_path, _one_below(required))
    _unmemoized(monkeypatch, tmp_path)
    assert config.cli_model("opus") == "opus"

    _fake_cli(tmp_path, required)
    assert config.cli_model("opus") == config.CLI_MODEL_IDS["opus"]

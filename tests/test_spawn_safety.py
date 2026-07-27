"""Spawn-safety hardening (RESEARCH.md §1/§5, todo #218).

Two independent guards against a headless run ever landing on pay-as-you-go API
billing instead of Wes's Max subscription:

  1. `_extra_env` strips billing credentials so a stray ANTHROPIC_API_KEY in the
     portal's own environment can never be inherited by a spawned `claude -p`.
  2. `--max-budget-usd` caps the blast radius if a key ever leaks in anyway.

Every big surprise-bill story traces to a stray key, so both are belt and
braces on the same failure.
"""
from __future__ import annotations

import pytest

from app import agent_runner, config, db, settings_form


# --- the environment strip -------------------------------------------------


@pytest.mark.parametrize("var", agent_runner._BILLING_ENV_VARS)
def test_billing_env_vars_never_reach_a_spawned_run(monkeypatch, var):
    monkeypatch.setenv(var, "sk-should-not-leak")
    env = agent_runner._extra_env()
    assert var not in env


def test_the_key_is_stripped_even_when_several_are_set(monkeypatch):
    for var in agent_runner._BILLING_ENV_VARS:
        monkeypatch.setenv(var, "leak")
    env = agent_runner._extra_env()
    assert not any(var in env for var in agent_runner._BILLING_ENV_VARS)


def test_the_path_still_prepends_local_bin(monkeypatch):
    """Stripping the billing vars must not disturb the PATH fix that lets the
    spawned process find the real CLI."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leak")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = agent_runner._extra_env()
    assert env["PATH"].startswith(str(agent_runner.Path.home() / ".local" / "bin"))
    assert env["PATH"].endswith("/usr/bin")


# --- the --max-budget-usd backstop -----------------------------------------


def test_no_budget_flag_when_unset():
    cmd = agent_runner.build_cmd("opus", 400)
    assert "--max-budget-usd" not in cmd


def test_budget_flag_appended_when_given():
    cmd = agent_runner.build_cmd("opus", 400, max_budget_usd=5.0)
    assert cmd[cmd.index("--max-budget-usd") + 1] == "5"


def test_budget_flag_keeps_fractional_amounts():
    cmd = agent_runner.build_cmd("opus", 400, max_budget_usd=2.5)
    assert cmd[cmd.index("--max-budget-usd") + 1] == "2.5"


def test_a_zero_or_negative_budget_is_no_ceiling():
    for value in (0, -1, 0.0):
        cmd = agent_runner.build_cmd("opus", 400, max_budget_usd=value)
        assert "--max-budget-usd" not in cmd


# --- resolving the budget from settings ------------------------------------


def test_configured_budget_reads_the_setting():
    db.set_setting("run_max_budget_usd", "12")
    assert agent_runner._configured_budget_usd() == 12.0


def test_configured_budget_blank_means_none():
    db.set_setting("run_max_budget_usd", "")
    assert agent_runner._configured_budget_usd() is None


def test_configured_budget_missing_means_none():
    # A genuinely absent row (older DB seeded before this key existed).
    conn = db.get_conn()
    conn.execute("DELETE FROM settings WHERE key = 'run_max_budget_usd'")
    conn.commit()
    assert db.get_setting("run_max_budget_usd") is None
    assert agent_runner._configured_budget_usd() is None


def test_configured_budget_junk_fails_open():
    db.set_setting("run_max_budget_usd", "lots please")
    assert agent_runner._configured_budget_usd() is None


def test_configured_budget_ignores_non_positive():
    db.set_setting("run_max_budget_usd", "0")
    assert agent_runner._configured_budget_usd() is None


def test_the_ceiling_is_a_shipped_default_key():
    assert "run_max_budget_usd" in config.DEFAULT_SETTINGS
    assert config.DEFAULT_SETTINGS["run_max_budget_usd"] == ""


# --- the settings-form validator -------------------------------------------


def test_form_accepts_a_positive_amount():
    out = settings_form.apply(
        {"run_max_budget_usd": "7.50"}, declared="run_max_budget_usd"
    )
    assert out["run_max_budget_usd"] == "7.5"


def test_form_blank_stays_blank():
    out = settings_form.apply(
        {"run_max_budget_usd": "  "}, declared="run_max_budget_usd"
    )
    assert out["run_max_budget_usd"] == ""


def test_form_rejects_junk_to_blank():
    out = settings_form.apply(
        {"run_max_budget_usd": "nope"}, declared="run_max_budget_usd"
    )
    assert out["run_max_budget_usd"] == ""


def test_form_rejects_negative_and_absurd():
    for bad in ("-5", "0", "99999"):
        out = settings_form.apply(
            {"run_max_budget_usd": bad}, declared="run_max_budget_usd"
        )
        assert out["run_max_budget_usd"] == ""

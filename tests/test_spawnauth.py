"""API-key spawn mode, and pacing standing down when it does not apply.

Open-sourcing step 3 (todo #255). The portal was built against one arrangement
- Claude Code logged into a subscription - and `app/spawnauth.py` adds the
other without giving up the guard that made the first one safe.

Three properties are load-bearing enough to be worth pinning individually:

1. **Subscription mode is unchanged, bit for bit.** Every existing spawn-safety
   test in `test_spawn_safety.py` still passes untouched; these add the
   negative case, that flipping *no* switch can ever let a key through.
2. **Opting in is explicit.** A key merely being present in the environment
   must never switch modes, because the guard exists precisely to stop an
   ambient key from billing somebody.
3. **Pacing stands down by MODE, not by the reading being missing.** This is
   the one with a real bug behind it: an API-key user very likely also has the
   CLI logged in, so the usage endpoint answers happily with figures about a
   subscription their runs are not spending. Degrading only on "no reading"
   would leave the portal holding runs against somebody else's window.
"""
from __future__ import annotations

import dataclasses

import pytest

from app import agent_runner, db, limits, pacing, site, spawnauth


# --- helpers ---------------------------------------------------------------


def a_site(**overrides) -> site.Site:
    """A resolved Site with no config file and no environment influence."""
    values = site.defaults()
    values.update(overrides)
    return site.Site(**values)


SUB = a_site(auth_mode="subscription")
KEY = a_site(auth_mode="api_key")


@pytest.fixture
def in_api_key_mode(monkeypatch):
    """Put the whole process in API-key mode, the way portal.toml would.

    The modules under test read `spawnauth.mode()` with no argument, which
    reads the process-wide `SITE`, so this patches that one resolved value
    rather than threading a Site through call sites that do not take one.
    """
    monkeypatch.setattr(site, "SITE", KEY)
    monkeypatch.setattr(spawnauth, "SITE", KEY)
    return KEY


# --- resolving the mode ----------------------------------------------------


def test_the_default_is_subscription():
    """The mode that cannot spend money is the one a fresh clone gets."""
    assert site.defaults()["auth_mode"] == "subscription"
    assert spawnauth.mode(a_site()) == spawnauth.MODE_SUBSCRIPTION
    assert spawnauth.paces_on_subscription(a_site()) is True


@pytest.mark.parametrize(
    "raw", ["api_key", "api-key", "APIKEY", " Api_Key ", "api", "anthropic_api_key"]
)
def test_the_spellings_people_write_all_mean_api_key(raw):
    assert spawnauth.mode(a_site(auth_mode=raw)) == spawnauth.MODE_API_KEY


@pytest.mark.parametrize("raw", ["subscription", "max", "oauth", "claude_code", "SUB"])
def test_the_spellings_for_a_subscription_all_mean_subscription(raw):
    assert spawnauth.mode(a_site(auth_mode=raw)) == spawnauth.MODE_SUBSCRIPTION


@pytest.mark.parametrize("raw", ["", "  ", "bedrock", "gpt", "yes please"])
def test_an_unrecognised_mode_falls_back_to_subscription(raw):
    """Never raise, and never fall back to the mode that can spend money."""
    assert spawnauth.mode(a_site(auth_mode=raw)) == spawnauth.MODE_SUBSCRIPTION


def test_the_mode_is_settable_from_the_environment(monkeypatch):
    """A container or a systemd unit must be able to set it without a file."""
    monkeypatch.setenv("PORTAL_AUTH_MODE", "api_key")
    resolved = site.load(env={"PORTAL_AUTH_MODE": "api_key"}, use_file=False)
    assert spawnauth.is_api_key(resolved)


def test_the_mode_is_settable_from_portal_toml(tmp_path):
    path = tmp_path / "portal.toml"
    path.write_text('auth_mode = "api_key"\n')
    resolved = site.load(env={}, path=path)
    assert spawnauth.is_api_key(resolved)


# --- the key is never auto-detected ----------------------------------------


def test_a_key_in_the_environment_does_not_switch_modes(monkeypatch):
    """The whole safety property, stated directly.

    If having ANTHROPIC_API_KEY exported were enough, the guard that exists to
    stop exactly that would be gone: a sourced .env or a CI variable would
    start billing a card mid-run without anybody choosing it.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ambient")
    assert spawnauth.mode(SUB) == spawnauth.MODE_SUBSCRIPTION
    assert spawnauth.api_key(site=SUB) == ""


def test_subscription_mode_never_looks_for_a_key_anywhere(tmp_path):
    key_file = tmp_path / "anthropic_key.txt"
    key_file.write_text("sk-ant-in-a-file\n")
    env = {"PORTAL_ANTHROPIC_API_KEY": "sk-ant-in-the-env"}
    assert spawnauth.key_source(env, key_file, SUB) == ("", "")


# --- where the key comes from ----------------------------------------------


def test_the_portal_specific_env_var_wins(tmp_path):
    key_file = tmp_path / "anthropic_key.txt"
    key_file.write_text("sk-ant-file\n")
    env = {"PORTAL_ANTHROPIC_API_KEY": "sk-ant-portal", "ANTHROPIC_API_KEY": "sk-ant-ambient"}
    key, source = spawnauth.key_source(env, key_file, KEY)
    assert key == "sk-ant-portal"
    assert source == "$PORTAL_ANTHROPIC_API_KEY"


def test_the_ambient_env_var_is_used_when_opted_in(tmp_path):
    key, source = spawnauth.key_source({"ANTHROPIC_API_KEY": "sk-ant-x"}, tmp_path / "none", KEY)
    assert (key, source) == ("sk-ant-x", "$ANTHROPIC_API_KEY")


def test_the_key_file_is_read_when_no_env_var_is_set(tmp_path):
    key_file = tmp_path / "anthropic_key.txt"
    key_file.write_text("sk-ant-from-the-file\n")
    key, source = spawnauth.key_source({}, key_file, KEY)
    assert key == "sk-ant-from-the-file"
    assert source == str(key_file)


def test_the_key_file_skips_comments_and_blank_lines(tmp_path):
    key_file = tmp_path / "anthropic_key.txt"
    key_file.write_text("# my anthropic key\n\n  sk-ant-real  \n")
    assert spawnauth.api_key({}, key_file, KEY) == "sk-ant-real"


def test_a_missing_key_file_is_an_ordinary_state(tmp_path):
    assert spawnauth.api_key({}, tmp_path / "nope.txt", KEY) == ""


def test_an_unreadable_key_file_never_raises(tmp_path):
    """A directory where a file was expected: the portal must still boot."""
    bad = tmp_path / "adir"
    bad.mkdir()
    assert spawnauth.api_key({}, bad, KEY) == ""


def test_a_blank_env_var_does_not_shadow_the_file(tmp_path):
    key_file = tmp_path / "anthropic_key.txt"
    key_file.write_text("sk-ant-real\n")
    assert spawnauth.api_key({"PORTAL_ANTHROPIC_API_KEY": "   "}, key_file, KEY) == "sk-ant-real"


# --- the spawn environment -------------------------------------------------


@pytest.mark.parametrize("var", spawnauth.BILLING_ENV_VARS)
def test_every_billing_var_is_stripped_in_subscription_mode(var, tmp_path):
    env = spawnauth.spawn_env({var: "leak", "PATH": "/usr/bin"}, key_env={}, path=tmp_path, site=SUB)
    assert var not in env
    assert env["PATH"] == "/usr/bin"


def test_the_configured_key_reaches_a_spawn_in_api_key_mode(tmp_path):
    key_file = tmp_path / "anthropic_key.txt"
    key_file.write_text("sk-ant-configured\n")
    env = spawnauth.spawn_env({}, key_env={}, path=key_file, site=KEY)
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-configured"


def test_the_configured_key_replaces_whatever_was_inherited(tmp_path):
    """The spawn sees the key that was configured, not one lying around."""
    key_file = tmp_path / "anthropic_key.txt"
    key_file.write_text("sk-ant-configured\n")
    env = spawnauth.spawn_env(
        {"ANTHROPIC_API_KEY": "sk-ant-stale-and-inherited"}, key_env={}, path=key_file, site=KEY
    )
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-configured"


def test_api_key_mode_with_nothing_configured_passes_no_key_at_all(tmp_path):
    """Strip-then-inject, and this is why the order matters.

    Flipping the mode on its own must not smuggle an ambient credential into a
    spawn. With no key configured the run gets none, and fails loudly at the
    CLI - which `problems()` predicts in words.
    """
    env = spawnauth.spawn_env(
        {"ANTHROPIC_API_KEY": "sk-ant-ambient"}, key_env={}, path=tmp_path / "none", site=KEY
    )
    assert "ANTHROPIC_API_KEY" not in env


@pytest.mark.parametrize("var", ["ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"])
def test_gateway_vars_stay_stripped_in_api_key_mode_too(var, tmp_path):
    """Other providers are out of scope for a first release, and a
    half-supported one is worse than an unsupported one."""
    key_file = tmp_path / "k.txt"
    key_file.write_text("sk-ant-configured\n")
    env = spawnauth.spawn_env({var: "https://gateway.example"}, key_env={}, path=key_file, site=KEY)
    assert var not in env


def test_the_runners_own_env_still_strips_in_subscription_mode(monkeypatch):
    """`agent_runner._extra_env` now delegates here; the old guard still holds."""
    monkeypatch.setattr(site, "SITE", SUB)
    monkeypatch.setattr(spawnauth, "SITE", SUB)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leak")
    assert "ANTHROPIC_API_KEY" not in agent_runner._extra_env()


def test_the_runners_own_env_carries_the_key_in_api_key_mode(in_api_key_mode, monkeypatch):
    monkeypatch.setenv("PORTAL_ANTHROPIC_API_KEY", "sk-ant-configured")
    env = agent_runner._extra_env()
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-configured"
    # And the PATH fix that lets the spawn find the CLI is undisturbed.
    assert env["PATH"].startswith(str(agent_runner.Path.home() / ".local" / "bin"))


def test_the_runner_still_exposes_the_billing_var_list():
    """Named `_BILLING_ENV_VARS` since #218; hooks and tests reference it."""
    assert agent_runner._BILLING_ENV_VARS == spawnauth.BILLING_ENV_VARS


# --- the per-run spend ceiling ---------------------------------------------


def test_no_ceiling_by_default_on_a_subscription():
    """Unchanged: a subscription run bills $0, so there is nothing to cap."""
    db.set_setting("run_max_budget_usd", "")
    assert spawnauth.default_budget_usd(SUB) is None
    assert agent_runner._configured_budget_usd() is None


def test_api_key_mode_has_a_ceiling_without_being_asked(in_api_key_mode):
    """An unattended scheduler with no per-run cap is how a runaway loop
    becomes an invoice. Defaulting that to infinity is a trap."""
    db.set_setting("run_max_budget_usd", "")
    assert agent_runner._configured_budget_usd() == spawnauth.DEFAULT_API_KEY_BUDGET_USD


def test_an_explicit_ceiling_still_wins_in_api_key_mode(in_api_key_mode):
    db.set_setting("run_max_budget_usd", "20")
    assert agent_runner._configured_budget_usd() == 20.0


def test_junk_degrades_to_the_modes_default_not_to_unlimited(in_api_key_mode):
    for bad in ("lots please", "0", "-3"):
        db.set_setting("run_max_budget_usd", bad)
        assert agent_runner._configured_budget_usd() == spawnauth.DEFAULT_API_KEY_BUDGET_USD


def test_the_default_ceiling_reaches_the_command_line(in_api_key_mode):
    db.set_setting("run_max_budget_usd", "")
    cmd = agent_runner.build_cmd("p", "opus", 400, max_budget_usd=agent_runner._configured_budget_usd())
    assert cmd[cmd.index("--max-budget-usd") + 1] == "5"


# --- pacing stands down ----------------------------------------------------


def test_the_usage_snapshot_is_not_applicable_in_api_key_mode(in_api_key_mode):
    snapshot = limits.cached()
    assert snapshot["ok"] is False
    assert snapshot["not_applicable"] is True
    assert snapshot["windows"] == []


def test_a_cached_subscription_reading_cannot_leak_through(in_api_key_mode):
    """The bug this guard exists for.

    An API-key user very likely has the CLI logged in too, so a perfectly good
    reading is available - about a subscription their runs are not spending.
    Storing one and then asking for it must not produce a usable snapshot.
    """
    import json

    db.set_setting(
        limits.CACHE_KEY,
        json.dumps(
            {
                "ok": True,
                "fetched_at": limits.datetime.now(limits.timezone.utc).isoformat(),
                "windows": [{"key": "five_hour", "label": "session", "percent": 99.0}],
            }
        ),
    )
    assert limits.cached()["ok"] is False
    # ... and therefore nothing holds a scheduled run against it.
    assert pacing.scheduled_hold() is None


def test_a_full_window_would_otherwise_hold_a_run():
    """Delete-the-fix control for the test above: the same stored reading DOES
    hold a run in subscription mode, so the assertion above is about the mode
    and not about the fixture being inert."""
    import json

    db.set_setting(
        limits.CACHE_KEY,
        json.dumps(
            {
                "ok": True,
                "fetched_at": limits.datetime.now(limits.timezone.utc).isoformat(),
                "windows": [{"key": "five_hour", "label": "session", "percent": 99.0}],
            }
        ),
    )
    hold = pacing.scheduled_hold()
    assert hold is not None and hold["percent"] == 99.0


def test_the_spend_down_offer_is_never_raised_in_api_key_mode(in_api_key_mode):
    """"Your weekly window resets in 6h with 47% unused, shall I spend it?" is
    a nonsense question for somebody whose runs bill a card."""
    assert pacing.should_offer() is None


def test_refresh_does_not_fetch_in_api_key_mode(in_api_key_mode, monkeypatch):
    called = []
    monkeypatch.setattr(limits, "fetch_raw", lambda *a, **k: called.append(1) or {})
    snapshot = limits.refresh()
    assert called == []
    assert snapshot["not_applicable"] is True


def test_the_poller_returns_immediately_in_api_key_mode(in_api_key_mode, monkeypatch):
    """It returns rather than ticking forever to do nothing.

    Asserted by giving it a `sleep` that fails the test if reached: the loop
    body cannot complete a single iteration without sleeping, so never
    sleeping is exactly "never entered the loop".
    """
    import asyncio

    async def unreachable(*args, **kwargs):
        raise AssertionError("the poller entered its loop in API-key mode")

    monkeypatch.setattr(limits.asyncio, "sleep", unreachable)
    asyncio.run(limits.poll_loop(interval_sec=0, startup_delay_sec=0))


def test_the_poller_does_start_on_a_subscription(monkeypatch):
    """Delete-the-fix control: the same harness proves the loop is normally
    reached, so the test above is about the mode and not about `poll_loop`
    being unreachable for some unrelated reason."""
    import asyncio

    monkeypatch.setattr(site, "SITE", SUB)
    monkeypatch.setattr(spawnauth, "SITE", SUB)
    entered = []

    async def stop_after_first(*args, **kwargs):
        entered.append(1)
        raise asyncio.CancelledError

    monkeypatch.setattr(limits.asyncio, "sleep", stop_after_first)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(limits.poll_loop(interval_sec=0, startup_delay_sec=0))
    assert entered == [1]


# --- what a human is told --------------------------------------------------


def test_status_reports_the_mode_without_the_key(tmp_path):
    key_file = tmp_path / "k.txt"
    key_file.write_text("sk-ant-secret-value\n")
    reported = spawnauth.status({}, key_file, KEY)
    assert reported["mode"] == "api_key"
    assert reported["has_key"] is True
    assert reported["key_source"] == str(key_file)
    # The secret itself appears nowhere in what the UI is handed.
    assert "sk-ant-secret-value" not in repr(reported)


def test_status_on_a_subscription_says_it_is_paced():
    reported = spawnauth.status({}, None, SUB)
    assert reported["paced"] is True
    assert reported["api_key"] is False


def test_api_key_mode_with_no_key_says_so_plainly(tmp_path):
    problems = spawnauth.problems({}, tmp_path / "none", KEY)
    assert problems and "no key was found" in problems[0]


def test_a_key_of_the_wrong_shape_warns_but_is_still_used(tmp_path):
    key_file = tmp_path / "k.txt"
    key_file.write_text("oauth-token-pasted-by-mistake\n")
    problems = spawnauth.problems({}, key_file, KEY)
    assert problems and "sk-ant-" in problems[0]
    # Still passed through: a vendor changing a prefix must not brick the portal.
    assert spawnauth.api_key({}, key_file, KEY) == "oauth-token-pasted-by-mistake"


def test_a_well_formed_key_is_quiet(tmp_path):
    key_file = tmp_path / "k.txt"
    key_file.write_text("sk-ant-api03-realish\n")
    assert spawnauth.problems({}, key_file, KEY) == []


def test_a_stray_key_in_subscription_mode_is_explained(tmp_path):
    """Otherwise this is silent: the key is stripped, nothing is billed to it,
    and the operator has no idea why their key "does not work"."""
    problems = spawnauth.problems({"ANTHROPIC_API_KEY": "sk-ant-x"}, None, SUB)
    assert problems and "subscription mode" in problems[0]
    assert "api_key" in problems[0]


def test_a_clean_subscription_install_is_quiet():
    assert spawnauth.problems({}, None, SUB) == []


def test_the_key_never_appears_in_any_problem_text(tmp_path):
    key_file = tmp_path / "k.txt"
    key_file.write_text("sk-ant-do-not-print-me\n")
    for problems in (
        spawnauth.problems({"ANTHROPIC_API_KEY": "sk-ant-do-not-print-me"}, key_file, SUB),
        spawnauth.problems({}, key_file, KEY),
        spawnauth.problems({"PORTAL_ANTHROPIC_API_KEY": "nope-wrong-shape"}, key_file, KEY),
    ):
        assert not any("do-not-print-me" in p for p in problems)
        assert not any("nope-wrong-shape" in p for p in problems)


# --- the site field itself -------------------------------------------------


def test_auth_mode_is_a_real_site_field():
    assert "auth_mode" in {f.name for f in dataclasses.fields(site.Site)}


def test_the_key_is_not_on_site_where_a_template_could_reach_it():
    """`Site` is a Jinja global on every page. A secret on it would be one
    stray `{{ SITE.… }}` from being served to a browser."""
    names = {f.name for f in dataclasses.fields(site.Site)}
    assert not any("key" in name or "secret" in name or "token" in name for name in names)


def test_a_broken_config_file_still_boots_with_a_safe_mode(tmp_path):
    path = tmp_path / "portal.toml"
    path.write_text('auth_mode = ["not", "a", "string"]\n')
    resolved = site.load(env={}, path=path)
    assert spawnauth.mode(resolved) == spawnauth.MODE_SUBSCRIPTION

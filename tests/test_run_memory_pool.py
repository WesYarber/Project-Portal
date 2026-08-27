"""The shared memory pool across every run in flight (app/runlimit.py).

Wes, 2026-08-07: "I'm not sure whether the set memory limit is a good value.
Does the system in total have 32GB? If so, let's increase the limit for projects
to 16GB for now, unless you think that is too much and will cause issues
elsewhere."

It has 18.8 GB, not 32, so 16 GiB per run is not a cap at all - the kernel's own
global OOM killer, the failure `runlimit` exists to prevent, fires first and
picks from the whole machine. But the instinct was right: the per-run cap was
low (40%, 7.5 GiB) and the Claude CLI's own bundled grep has been measured
asking for 7.4 GB, where being killed presents to an agent as grep returning no
matches (todo #290).

The reason 40% was defensible is that it was also, by accident, the only thing
bounding N runs at once - and with `max_parallel_runs` lifted to 6 it was not
even that: six runs at the cap was 45 GiB of headroom on an 18.8 GiB box. So the
answer is two numbers instead of one:

  * per-run rises to 60% (11.3 GiB here) - one runaway tool;
  * a pool of 75% (14.1 GiB here) on the systemd slice every run scope is
    created inside - six ordinary runs adding up.

The properties below are what make that true, and they inherit `runlimit`'s
first rule: every one of them fails open. A machine whose systemd will not put a
scope in a slice still runs every project, capped one at a time.
"""
from __future__ import annotations

import pytest

from app import config, db, runlimit


@pytest.fixture(autouse=True)
def _fresh_probe(monkeypatch):
    """The same reset `tests/test_runlimit.py` does, plus the two the pool adds.

    `_scopes` matters beyond this file: a scope name minted here with a
    plausible run id survives into `worker._protected_scopes()`, and
    `tests/test_sweep_blast_radius.py` asserts that set holds nothing that looks
    like a real run. monkeypatch puts the module dict back after each test.
    """
    monkeypatch.setattr(runlimit, "_available", None, raising=False)
    monkeypatch.setattr(runlimit, "_slice_ok", None, raising=False)
    monkeypatch.setattr(runlimit, "_pool_applied", None, raising=False)
    monkeypatch.setattr(runlimit, "_scopes", {}, raising=False)


# --- the numbers -----------------------------------------------------------


def test_the_per_run_cap_clears_the_claude_cli_grep(monkeypatch):
    """7.4 GB is a measured allocation, not a hypothetical - and a killed grep
    returns EMPTY output, which reads to an agent as "no matches"."""
    monkeypatch.setattr(runlimit, "total_memory_bytes", lambda: 18 * 1024**3)
    assert runlimit.default_max_bytes() > 8 * 1000**3


def test_the_pool_leaves_the_rest_of_the_machine_something(monkeypatch):
    """The portal, the docker stack and the kernel live outside the pool. On
    2026-08-07 everything on this box that is not a run held ~3.5 GiB of
    anonymous memory."""
    monkeypatch.setattr(runlimit, "total_memory_bytes", lambda: 18 * 1024**3)
    total = 18 * 1024**3
    assert total - runlimit.default_pool_bytes() > 3 * 1024**3


def test_the_pool_is_never_smaller_than_one_run(monkeypatch):
    """A pool under the per-run cap would kill the first run to reach its own
    limit, which makes the per-run number a lie."""
    monkeypatch.setattr(runlimit, "total_memory_bytes", lambda: 2 * 1024**3)
    assert runlimit.default_pool_bytes() >= runlimit.default_max_bytes()


def test_the_pool_is_bigger_than_one_run_on_a_real_machine(monkeypatch):
    """...but on any box worth pooling it must leave room for a SECOND run, or
    it is just the per-run cap with extra steps."""
    monkeypatch.setattr(runlimit, "total_memory_bytes", lambda: 18 * 1024**3)
    assert runlimit.default_pool_bytes() > runlimit.default_max_bytes()


# --- the setting -----------------------------------------------------------


def test_the_shipped_default_is_blank_not_a_number():
    assert config.DEFAULT_SETTINGS["runs_memory_pool"] == ""


def test_blank_means_the_derived_default():
    db.set_setting("runs_memory_pool", "")
    assert runlimit.configured_pool_bytes() == runlimit.default_pool_bytes()


def test_an_explicit_size_wins():
    db.set_setting("runs_memory_pool", "12G")
    assert runlimit.configured_pool_bytes() == 12 * 1024**3


@pytest.mark.parametrize("off", ["0", "off", "none", "no", "unlimited", "OFF"])
def test_the_pool_can_be_turned_off_outright(off):
    db.set_setting("runs_memory_pool", off)
    assert runlimit.configured_pool_bytes() is None


@pytest.mark.parametrize("bad", ["lots", "-2G", "gigabytes"])
def test_an_unparseable_pool_falls_back_to_the_default(bad):
    db.set_setting("runs_memory_pool", bad)
    assert runlimit.configured_pool_bytes() == runlimit.default_pool_bytes()


def test_the_form_accepts_a_size_and_empties_a_typo():
    """A typo is stored as blank, not as itself: a stored "plenty" would sit on
    the settings page looking like a pool that is in force while `runlimit`
    quietly used the default. Same cleaner as the per-run field."""
    from app import settings_form

    clean = settings_form.REGISTRY["runs_memory_pool"].clean
    assert clean("12G") == "12G"
    assert clean("off") == "off"
    assert clean("plenty") == ""


# --- wrapping the spawn ----------------------------------------------------


def test_a_run_is_created_inside_the_pooled_slice(monkeypatch):
    monkeypatch.setattr(runlimit, "available", lambda: True)
    monkeypatch.setattr(runlimit, "apply_pool", lambda limit: True)
    db.set_setting("run_memory_max", "4G")
    argv = runlimit.wrap(["claude", "-p", "hi"], run_id=7)
    assert f"--slice={runlimit.RUNS_SLICE}" in argv
    # The per-run cap still rides on the scope itself: the pool is a second
    # ceiling, not a replacement for the first.
    assert f"MemoryMax={4 * 1024**3}" in argv


def test_a_systemd_that_refuses_slices_still_caps_each_run(monkeypatch):
    """Failing open, one capability at a time. If `--slice` is what this box
    cannot do, the answer is "cap each run and skip the pool" - never "give up
    and run everything uncapped"."""
    monkeypatch.setattr(runlimit, "available", lambda: True)
    monkeypatch.setattr(runlimit, "apply_pool", lambda limit: False)
    db.set_setting("run_memory_max", "4G")
    argv = runlimit.wrap(["claude", "-p", "hi"], run_id=7)
    assert not [a for a in argv if a.startswith("--slice=")]
    assert f"MemoryMax={4 * 1024**3}" in argv
    assert argv[argv.index("--") + 1:] == ["claude", "-p", "hi"]


def test_turning_the_pool_off_leaves_the_run_unpooled(monkeypatch):
    monkeypatch.setattr(runlimit, "available", lambda: True)
    monkeypatch.setattr(runlimit, "pool_available", lambda refresh=False: True)
    db.set_setting("run_memory_max", "4G")
    db.set_setting("runs_memory_pool", "off")
    argv = runlimit.wrap(["claude", "-p", "hi"], run_id=7)
    assert not [a for a in argv if a.startswith("--slice=")]
    # The conftest stub records what apply_pool was asked for; "off" is None.
    assert runlimit.POOL_WRITES[-1] is None


def test_the_pool_write_asks_for_the_configured_number(monkeypatch):
    monkeypatch.setattr(runlimit, "available", lambda: True)
    monkeypatch.setattr(runlimit, "pool_available", lambda refresh=False: True)
    db.set_setting("run_memory_max", "4G")
    db.set_setting("runs_memory_pool", "12G")
    runlimit.wrap(["claude", "-p", "hi"], run_id=7)
    assert runlimit.POOL_WRITES[-1] == 12 * 1024**3


def test_the_pool_is_off_when_capping_is_off(monkeypatch):
    """No cap at all means no slice either - `wrap` returns the bare command,
    so there is nothing to put anywhere."""
    monkeypatch.setattr(runlimit, "available", lambda: True)
    db.set_setting("run_memory_max", "off")
    assert runlimit.wrap(["claude", "-p", "hi"], 7) == ["claude", "-p", "hi"]


# --- what the run is told --------------------------------------------------


def test_a_kill_near_the_per_run_cap_blames_the_per_run_cap(monkeypatch):
    monkeypatch.setattr(runlimit, "pool_available", lambda refresh=False: True)
    db.set_setting("run_memory_max", "4G")
    db.set_setting("runs_memory_pool", "12G")
    note = runlimit.kill_note(peak=int(3.9 * 1024**3))
    assert "per-run" in note
    assert "run_memory_max" in note


def test_a_kill_far_below_the_per_run_cap_blames_the_pool(monkeypatch):
    """Otherwise the note tells Wes to raise a number that was never reached,
    on a run that was killed for somebody else's appetite."""
    monkeypatch.setattr(runlimit, "pool_available", lambda refresh=False: True)
    db.set_setting("run_memory_max", "4G")
    db.set_setting("runs_memory_pool", "12G")
    note = runlimit.kill_note(peak=1 * 1024**3)
    assert "shared memory pool" in note
    assert "runs_memory_pool" in note
    assert "12.0 GiB" in note
    # And it still has to be clear the run itself carried on.
    assert "not stopped" in note


def test_with_no_pool_a_kill_always_blames_the_per_run_cap(monkeypatch):
    monkeypatch.setattr(runlimit, "pool_available", lambda refresh=False: True)
    db.set_setting("run_memory_max", "4G")
    db.set_setting("runs_memory_pool", "off")
    note = runlimit.kill_note(peak=1 * 1024**3)
    assert "per-run" in note


def test_a_kill_with_no_peak_reading_blames_the_per_run_cap(monkeypatch):
    """`memory.peak` is Linux 5.19+. Without it there is no way to tell the two
    ceilings apart, and the per-run cap is the one that was true before the pool
    existed."""
    monkeypatch.setattr(runlimit, "pool_available", lambda refresh=False: True)
    db.set_setting("run_memory_max", "4G")
    db.set_setting("runs_memory_pool", "12G")
    assert "per-run" in runlimit.kill_note(peak=None)


# --- the settings page -----------------------------------------------------


def test_the_settings_page_carries_the_pool_field(client_settings_body):
    body = client_settings_body
    assert 'name="runs_memory_pool"' in body
    # In the submitted field list, or saving the form would silently wipe it.
    assert "runs_memory_pool" in body.split('name="_fields"')[1][:600]


@pytest.fixture
def client_settings_body(temp_data_dir):
    from starlette.testclient import TestClient

    from app import main

    return TestClient(main.app).get("/settings?tab=agent").text

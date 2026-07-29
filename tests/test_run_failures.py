"""Why runs kept dying with "(no output)", and the two portal bugs behind it.

On 2026-07-22 Wes asked what keeps failing SimpleClickTrack's agent runs and
whether the portal is the cause. It was, both ways. Read out of the runs table
and the CLI's own session transcripts:

- Eight runs in one day hit the CLI's `--max-turns` ceiling (then hardcoded at
  100) mid-work. The CLI kills such a run with an empty result string, so the
  journal recorded literally "(no output)", the finished-but-uncommitted work
  sat in the tree, and the next run started the same feature over.
- Eleven more were killed by the portal restarting itself to load a
  self-update while other projects' runs were still in flight: systemd's stop
  SIGTERMs the whole cgroup, `claude` subprocesses included. Four of those
  died gracefully enough to be recorded as anonymous errors; seven were
  orphaned rows.

These tests pin the fixes:

- the turn ceiling comes from the `run_max_turns` setting and defaults far
  higher, with the run timeout as the real backstop;
- a run killed by the ceiling is journalled as exactly that, with the setting
  named, never as "(no output)";
- the result event's subtype is parsed and surfaces in run logs;
- a self-update only restarts the service once no other run is in flight, and
  no new runs start while the restart is waiting.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import textwrap

import pytest

from app import agent_runner, db, runlog, settings_form, worker


@pytest.fixture
def project():
    return db.create_project("Metronome", description="A thing.", stage="active", build_approved=True, slug="metronome")


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)

    def install(body: str) -> None:
        script = bindir / "claude"
        script.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    monkeypatch.setattr(agent_runner, "_extra_env", lambda: dict(env))
    return install


def _emit(events: list[dict]) -> str:
    body = "\n".join(json.dumps(e) for e in events)
    return f"cat <<'PORTAL_EOF'\n{body}\nPORTAL_EOF\n"


def _journal_bodies(project_id: int) -> list[str]:
    return [r["content_md"] for r in db.list_journal(project_id)]


# --- the subtype reaches RunResult ------------------------------------------


@pytest.mark.asyncio
async def test_a_max_turns_kill_is_parsed_off_the_stream(tmp_path, fake_claude):
    """This is the exact shape the CLI emits when --max-turns trips: is_error,
    an error subtype, and no `result` string at all."""
    fake_claude(_emit([
        {"type": "system", "subtype": "init", "model": "opus", "tools": []},
        {"type": "result", "subtype": "error_max_turns", "is_error": True,
         "num_turns": 101, "total_cost_usd": 6.84, "session_id": "s-1"},
    ]))
    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.ok is False
    assert result.subtype == "error_max_turns"
    assert result.hit_max_turns is True
    assert result.result_text == ""


@pytest.mark.asyncio
async def test_a_successful_run_has_a_success_subtype_and_no_max_turns_flag(tmp_path, fake_claude):
    fake_claude(_emit([
        {"type": "result", "subtype": "success", "is_error": False,
         "num_turns": 3, "session_id": "s-2", "result": "done"},
    ]))
    result = await agent_runner.run_claude("prompt", tmp_path / "ws", "opus", timeout_min=1)
    assert result.ok is True
    assert result.subtype == "success"
    assert result.hit_max_turns is False


# --- what the journal says --------------------------------------------------


def _fake_run(monkeypatch, result: agent_runner.RunResult) -> dict:
    seen: dict = {}

    async def fake(prompt, cwd, model, timeout_min, **kwargs):
        seen.update(kwargs, model=model, timeout_min=timeout_min)
        return result

    monkeypatch.setattr(agent_runner, "run_claude", fake)
    return seen


@pytest.mark.asyncio
async def test_a_turn_limit_death_is_journalled_as_the_turn_limit(project, monkeypatch):
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=False, subtype="error_max_turns", num_turns=401, session_id="s",
    ))
    await worker.run_project_task(project, "build")

    bodies = _journal_bodies(project["id"])
    assert any("turn ceiling" in b and "run_max_turns" in b for b in bodies)
    assert not any("(no output)" in b for b in bodies)
    # The run row's summary says the same thing instead of sitting empty.
    run = db.list_runs(project["id"])[0]
    assert "turn ceiling" in run["summary"]


@pytest.mark.asyncio
async def test_an_empty_error_names_its_subtype_instead_of_no_output(project, monkeypatch):
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=False, subtype="error_during_execution", num_turns=63,
    ))
    await worker.run_project_task(project, "build")
    bodies = _journal_bodies(project["id"])
    assert any("error_during_execution" in b for b in bodies)
    assert not any("(no output)" in b for b in bodies)


@pytest.mark.asyncio
async def test_an_error_with_real_text_keeps_its_text(project, monkeypatch):
    _fake_run(monkeypatch, agent_runner.RunResult(
        ok=False, subtype="error_during_execution", result_text="ENOSPC: disk full",
    ))
    await worker.run_project_task(project, "build")
    bodies = _journal_bodies(project["id"])
    assert any("ENOSPC" in b for b in bodies)


@pytest.mark.asyncio
async def test_an_error_with_no_subtype_still_says_no_output(project, monkeypatch):
    _fake_run(monkeypatch, agent_runner.RunResult(ok=False))
    await worker.run_project_task(project, "build")
    assert any("(no output)" in b for b in _journal_bodies(project["id"]))


# --- the ceiling is a setting -----------------------------------------------


@pytest.mark.asyncio
async def test_the_turn_ceiling_default_is_far_above_the_old_100(project, monkeypatch):
    seen = _fake_run(monkeypatch, agent_runner.RunResult(ok=False))
    await worker.run_project_task(project, "build")
    assert seen["max_turns"] == worker.DEFAULT_MAX_TURNS
    assert worker.DEFAULT_MAX_TURNS >= 300


@pytest.mark.asyncio
async def test_the_turn_ceiling_comes_from_settings(project, monkeypatch):
    db.set_setting("run_max_turns", "150")
    seen = _fake_run(monkeypatch, agent_runner.RunResult(ok=False))
    await worker.run_project_task(project, "build")
    assert seen["max_turns"] == 150


def test_a_junk_setting_falls_back_to_the_default():
    db.set_setting("run_max_turns", "lots")
    assert worker.run_max_turns() == worker.DEFAULT_MAX_TURNS
    db.set_setting("run_max_turns", "-5")
    assert worker.run_max_turns() == 1


def test_the_setting_is_registered_on_the_settings_form():
    field = settings_form.REGISTRY["run_max_turns"]
    assert field.clean("250") == "250"
    # Out-of-range and junk both come back as the default rather than raising.
    assert field.clean("0") == "400"
    assert field.clean("nope") == "400"


# --- the run log names the reason -------------------------------------------


def test_the_log_line_for_a_turn_limit_death_says_so():
    lines = runlog.render_event({
        "type": "result", "subtype": "error_max_turns", "is_error": True,
        "num_turns": 101, "total_cost_usd": 6.84,
    })
    assert lines == ["! run failed - hit the turn limit  (101 turns, 6.840w)"]


def test_the_log_line_for_another_error_carries_the_subtype():
    lines = runlog.render_event({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "num_turns": 63,
    })
    assert lines == ["! run failed - error_during_execution  (63 turns)"]


def test_a_plain_failure_line_is_unchanged():
    lines = runlog.render_event({"type": "result", "is_error": True, "num_turns": 5})
    assert lines == ["! run failed  (5 turns)"]


# --- restarts wait for the portal to go quiet --------------------------------


@pytest.fixture
def restart_state(monkeypatch):
    """Fresh module state, and a recorder in place of systemd-run."""
    calls: list[list[str]] = []

    def record(cmd, **kwargs):
        calls.append(cmd)

        class Done:
            returncode = 0
        return Done()

    monkeypatch.setattr(worker.subprocess, "run", record)
    monkeypatch.setattr(worker, "_pending_restart", None)
    monkeypatch.setattr(worker, "_inflight", {})
    # `_restarting` is a one-way latch in production - the only thing that
    # clears it is the process ending - so every test that arms it has to hand
    # back a module that has not been restarted.
    monkeypatch.setattr(worker, "_restarting", False)
    return calls


async def _parked_task() -> tuple[asyncio.Task, asyncio.Event]:
    release = asyncio.Event()

    async def wait():
        await release.wait()

    return asyncio.create_task(wait()), release


@pytest.mark.asyncio
async def test_a_self_update_with_another_run_in_flight_defers(project, restart_state):
    other, release = await _parked_task()
    meta, meta_release = await _parked_task()
    worker._inflight.update({5: other, 9: meta})

    worker._schedule_self_restart(project["id"], "abcdef1234", current_run_id=9)

    assert restart_state == []  # no systemd-run yet
    assert worker.restart_pending() is True
    assert any("still in flight" in b for b in _journal_bodies(project["id"]))
    release.set(), meta_release.set()
    await asyncio.gather(other, meta)


@pytest.mark.asyncio
async def test_a_self_update_on_a_quiet_portal_restarts_immediately(project, restart_state):
    """The meta-run's own task is still in _inflight while it calls this - it
    must not count itself as a reason to wait."""
    meta, release = await _parked_task()
    worker._inflight[9] = meta

    worker._schedule_self_restart(project["id"], "abcdef1234", current_run_id=9)

    assert len(restart_state) == 1 and restart_state[0][0] == "systemd-run"
    assert worker.restart_pending() is False
    release.set()
    await meta


@pytest.mark.asyncio
async def test_the_tick_holds_new_runs_and_fires_once_quiet(project, restart_state, monkeypatch):
    started: list[bool] = []

    async def no_start():
        started.append(True)
        return False

    monkeypatch.setattr(worker, "_start_one", no_start)

    other, release = await _parked_task()
    worker._inflight[5] = other
    monkeypatch.setattr(worker, "_pending_restart", (project["id"], "abcdef1234"))

    # Something still running: no restart, and crucially no new run started.
    await worker._tick()
    assert restart_state == []
    assert started == []

    # The run finishes; the next tick fires the restart and clears the flag.
    release.set()
    await other
    await worker._tick()
    assert len(restart_state) == 1 and restart_state[0][0] == "systemd-run"
    assert worker.restart_pending() is False
    assert any("restarting the service" in b for b in _journal_bodies(project["id"]))


@pytest.mark.asyncio
async def test_idle_reason_explains_the_hold(restart_state, monkeypatch):
    monkeypatch.setattr(worker, "_pending_restart", (1, "abcdef1234"))
    assert "self-update" in worker.idle_reason()


# --- the seed can no longer eat the memory files -----------------------------
#
# Related but distinct from the failures above: the same investigation found
# where Wes's typed "about me" text went. `_seed_data` runs whenever the
# DATABASE is new, but the memory files live beside the database - and on
# 2026-07-21 07:17 a boot that saw a fresh DB overwrote profile.md,
# learnings.md and suggestions.md with the seed text, erasing what he had
# typed. The seed must treat an existing file as his, always.


def test_the_seed_never_overwrites_an_existing_memory_file(temp_data_dir):
    from app import config

    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.PROFILE_MD.write_text("# Profile: Wes\n\n- The text Wes typed himself.\n")
    config.LEARNINGS_MD.write_text("# Learnings\n\n- Hard-won.\n")

    # The real seeding path, not db._seed_data - the test conftest stubs that
    # out wholesale to keep test portals empty.
    db._seed_memory_files()

    assert "The text Wes typed himself" in config.PROFILE_MD.read_text()
    assert "Hard-won" in config.LEARNINGS_MD.read_text()
    # A genuinely missing file is still seeded.
    assert config.SUGGESTIONS_MD.exists()


@pytest.mark.asyncio
async def test_a_manual_run_wes_queued_outranks_the_pending_restart(project, restart_state, monkeypatch):
    """The manual queue is in-memory, so firing the restart with a queued
    "run now" in it would silently eat Wes's click. The tick starts manual
    runs even while a restart waits, and the restart waits for them too."""
    started: list[bool] = []
    release = asyncio.Event()

    async def hold():
        await release.wait()

    async def start_manual():
        # Like the real thing: takes the queued id, occupies a slot, reports
        # that a run started.
        await worker.manual_queue.get()
        started.append(True)
        worker._inflight[7] = asyncio.create_task(hold())
        return True

    monkeypatch.setattr(worker, "_start_one", start_manual)
    monkeypatch.setattr(worker, "_pending_restart", (project["id"], "abcdef1234"))
    await worker.manual_queue.put(project["id"])

    await worker._tick()
    assert started == [True]
    # Not fired while the manual run it just started is in flight.
    assert restart_state == []

    # The manual run finishes; the next tick restarts.
    release.set()
    await worker._inflight[7]
    await worker._tick()
    assert len(restart_state) == 1


# --- the wait is visible on every page ---------------------------------------
# Without this, a committed fix is invisible for as long as a slow run holds
# the restart back - which read to Wes as his request being ignored (it did,
# twice, over the danger zone: asked at 04:52, shipped at 06:14, and at 06:05
# he was still looking at the old layout with no hint anything was coming).


@pytest.fixture
def client(temp_data_dir):
    from starlette.testclient import TestClient

    from app import main

    return TestClient(main.app)


def test_no_pending_restart_means_no_counter_and_no_notice(client):
    assert worker.restart_pending_runs() is None
    assert "update-wait" not in client.get("/").text


@pytest.mark.asyncio
async def test_a_waiting_restart_shows_the_notice_with_the_run_count(
    client, project, restart_state
):
    other, release = await _parked_task()
    worker._inflight[5] = other
    worker._schedule_self_restart(project["id"], "abcdef1234", current_run_id=9)

    assert worker.restart_pending_runs() == 1
    body = client.get("/").text
    assert "update-wait" in body
    assert "waiting for" in body and "1 agent run" in body
    # Not the plural, and not the about-to-restart wording.
    assert "1 agent runs" not in body
    assert "in a moment" not in body

    release.set()
    await other


def test_a_restart_with_nothing_left_in_flight_says_so(client, project, restart_state, monkeypatch):
    """The moment between the last run finishing and the next tick firing the
    restart: '0 agent runs' would be nonsense, so the wording switches."""
    monkeypatch.setattr(worker, "_pending_restart", (project["id"], "abcdef1234"))
    assert worker.restart_pending_runs() == 0
    body = client.get("/").text
    assert "update-wait" in body and "in a moment" in body

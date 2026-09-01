"""Booting the app to check it serves must not act like the service starting.

`deploy/setup.py` boots the app on a scratch port and asks it for `pong`. That
is the last step of installing on a new machine, and until now it started the
whole service on the way past. Three things followed, and they get worse as the
machine gets more real:

- The worker loop schedules runs. On the empty board of a fresh clone the first
  tick goes straight to the daily reflect, so `setup.py` on Wes's second
  computer would spawn a real, billed `claude -p` seconds after printing "the
  portal is ready". That happened on 2026-08-30 from a throwaway preview server
  and cost about ten seconds of his allowance.
- `reconcile_orphaned_runs_on_boot` settles every run it finds in flight as an
  orphan. Pointed at a live data directory - which is what `deploy/update.py`
  would be doing - those are the *running* service's runs, killed on the books
  by a health check.
- `preview.serve_loop` binds a fixed port, so the check collides with the
  portal it is checking.

`PORTAL_SMOKE_TEST=1` is therefore not "no worker". It is "this process is not
the service": take no action that belongs to a real service start.
"""
from __future__ import annotations

import inspect
import io
import subprocess

import pytest

from app import db, main


@pytest.fixture
def quiet_startup(monkeypatch):
    """Stub everything `on_startup` does that reaches outside the test."""
    monkeypatch.setattr(main.memory, "snapshot_all", lambda: None)
    monkeypatch.setattr(main.site, "warnings", lambda: [])
    monkeypatch.setattr(main.spawnauth, "problems", lambda: [])
    monkeypatch.setattr(main.transcribe, "backfill", lambda: None)
    started: list[str] = []

    def fake_create_task(coro):
        started.append(getattr(coro, "cr_code", None).co_name if hasattr(coro, "cr_code") else "task")
        coro.close()

    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)
    yield started
    main._BACKGROUND_TASKS.clear()


@pytest.mark.asyncio
async def test_a_smoke_boot_starts_no_background_loops(monkeypatch, quiet_startup):
    """The worker loop is the one that spends money, and it is started
    unconditionally without this."""
    monkeypatch.setenv("PORTAL_SMOKE_TEST", "1")
    reconciled: list[bool] = []
    monkeypatch.setattr(db, "reconcile_orphaned_runs_on_boot", lambda: reconciled.append(True))

    await main.on_startup()

    assert quiet_startup == []
    assert main._BACKGROUND_TASKS == []
    assert reconciled == []


@pytest.mark.asyncio
async def test_a_smoke_boot_does_not_settle_the_running_services_runs(monkeypatch, quiet_startup):
    """The destructive one. Against a live data directory this reaper is
    looking at another process's live runs, not at leftovers."""
    monkeypatch.setenv("PORTAL_SMOKE_TEST", "1")
    reconciled: list[bool] = []
    monkeypatch.setattr(db, "reconcile_orphaned_runs_on_boot", lambda: reconciled.append(True))

    await main.on_startup()

    assert reconciled == []


@pytest.mark.asyncio
async def test_a_real_boot_still_starts_everything(monkeypatch, quiet_startup):
    """The other side of the flag, and the reason it is opt-in: an unset or
    misspelled variable must leave the service behaving exactly as it did."""
    monkeypatch.delenv("PORTAL_SMOKE_TEST", raising=False)
    reconciled: list[bool] = []
    monkeypatch.setattr(db, "reconcile_orphaned_runs_on_boot", lambda: reconciled.append(True))

    await main.on_startup()

    assert len(quiet_startup) == 5  # worker, telegram, limits, netinfo, preview
    assert reconciled == [True]


@pytest.mark.asyncio
async def test_only_the_exact_value_turns_it_on(monkeypatch, quiet_startup):
    """`PORTAL_SMOKE_TEST=0` and `=false` are somebody saying no. Treating any
    non-empty value as yes is how a variable left in a service unit silently
    disables the worker on the live box."""
    monkeypatch.setenv("PORTAL_SMOKE_TEST", "0")
    monkeypatch.setattr(db, "reconcile_orphaned_runs_on_boot", lambda: None)

    await main.on_startup()

    assert len(quiet_startup) == 5


def test_the_setup_smoke_test_asks_for_it(monkeypatch, tmp_path):
    """The wiring, not the flag. `setup.py` booting without the variable is
    exactly the bug, and no test of `main.on_startup` alone would see it.

    The stand-in interpreter is a real file in `tmp_path` rather than the tree's
    own `venv/bin/python`, because `smoke_test` returns early when it does not
    exist - and it does not exist in the exported copy a mutation sweep runs
    against. That made this test the only red one in the export, and with
    `pytest -x` every mutation after it reported "caught" without running.
    """
    from deploy import setup

    interpreter = tmp_path / "python"
    interpreter.write_text("#!/bin/sh\n")
    monkeypatch.setattr(setup, "venv_python", lambda: interpreter)

    seen = {}

    class FakeProc:
        def __init__(self):
            self.stdout = io.StringIO("exited immediately")

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        seen["env"] = kwargs.get("env") or {}
        return FakeProc()

    monkeypatch.setattr(setup.subprocess, "Popen", fake_popen)

    setup.smoke_test(setup.Report())

    assert seen["env"].get("PORTAL_SMOKE_TEST") == "1"


def test_the_update_import_check_asks_for_it_too():
    """`deploy/update.py` imports the app rather than booting it, so the flag
    is belt as well as braces - but an import of `app.main` that ever grows a
    side effect at module scope would find the braces already on."""
    from deploy import update

    assert "PORTAL_SMOKE_TEST" in inspect.getsource(update.import_check)

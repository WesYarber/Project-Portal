"""Evict what an agent leaves behind from the container that held the run.

Agents detach long-lived helpers. On Wes's box right now, six do: a
`bash scripts/serve.sh` with two `bun run src/index.ts` under it, three more
bun servers, and a `ssh -f -N -R` reverse tunnel whose run journal claimed it
had been closed. Every one of them was started inside its run's
`portal-run-<id>-<pid>-<seq>.scope` and stayed there after the agent exited, so
the scope never emptied and systemd never collected it. Nine scopes were up for
one live run.

Two of those nine held **no processes at all** - `ActiveState=active`,
`TasksCurrent=0` - which is worth naming because it kills the obvious shortcut.
`--collect` is documented to garbage-collect a scope once it goes empty, and it
usually does, but an empty active scope is a state this machine reaches, so a
sweep cannot assume "no processes" means "already gone".

The reason this is not cosmetic
-------------------------------
A leaked scope makes a *finished run look alive forever*, and the portal's
liveness question is exactly `runlimit.scope_is_active(unit)`. Follow it
through:

1. A run records its scope name in `runs.scope_unit` (it must - that row is the
   only handle a later portal process has on an agent that outlived the process
   which spawned it).
2. The portal restarts mid-run. It self-updates several times an hour, so this
   is ordinary, not exotic. `db._reconcile_orphaned_runs` asks systemd, gets
   "active", and correctly *adopts* the run: status stays `running`, the
   workspace stays locked.
3. The agent finishes and detaches a preview server on its way out.
4. The adopting process has no `Popen` to await, so `worker._reap_adopted` is
   what must settle the row - and its signal is the scope dying. The preview
   server holds the scope active. It never dies.
5. The row stays `running` forever, so `db.running_project_ids()` lists that
   project forever, so **the project can never get another run**, with nothing
   anywhere in the UI to say why.

That has not fired yet only because `scope_unit` is a few runs old and no run
has yet both recorded a scope and detached a helper. It is armed, and steps 2
and 3 are each routine on this install.

Evicting the strays disarms it at the root and changes no caller: once the
scope holds nothing, it dies, and `scope_is_active` starts telling the truth
again on its own.

**Except in one case, which this module cannot reach.** The sweep below skips
anything in `worker._protected_scopes`, and that set is built from the same
'running' rows - so for a run *already* stranded at step 5, the leftover holding
its scope open is precisely the thing protected from being moved out of it. The
two mechanisms deadlock: the sweep waits for the row to settle, and the row
waits for the scope the sweep would have emptied.

Breaking that needs a completion signal from outside both, and the workspace
lease is one: `worklock.wrap` passes `--close`, so a detached helper does not
inherit it, and a definitely-free lease proves the agent has exited whatever its
scope says. `worker._reap_adopted` reads it as a second signal and settles the
row, which unprotects the unit, which lets the next sweep here rehouse the
leftover normally. See tests/test_stranded_runs.py.

Adopt, do not kill
------------------
The strays are mostly the servers Wes clicks "open it" on. Killing them at the
end of every run would be a portal that silently takes down the thing the run
was built to show him - "nothing moves that he didn't move", and worse than the
leak it fixes. So a stray is *moved* into a scope of its own
(`portal-stray-<same suffix>.scope`, so it still names the run it came from),
left running, and surfaced on the settings page with a stop button.

The new scope carries the same `MemoryMax` the run had. That is deliberately
not a smaller number: there is no evidence for one, and a cap chosen by guess
would OOM-kill a server Wes is actually using. Same ceiling as before, now
accounted to the stray rather than to a run that ended.

Everything here fails open. No systemd, no cgroup v2, an unreadable
`cgroup.procs`, a `systemd-run` that refuses - all of them mean the sweep does
nothing and runs behave exactly as they did before.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import NamedTuple, Optional

from app import runlimit

log = logging.getLogger("portal.strays")

CGROUP_ROOT = Path("/sys/fs/cgroup")

STRAY_PREFIX = "portal-stray-"
RUN_PREFIX = "portal-run-"

# `portal-run-<tag>-<pid>-<seq>.scope`, capturing the whole suffix so a stray
# scope can be named from it. Reusing the suffix verbatim means the stray unit
# still says which run dropped it, and cannot collide: the suffix was already
# unique per run, per portal process and per spawn.
_RUN_UNIT_RE = re.compile(r"^" + RUN_PREFIX + r"((?:x|\d+)-\d+-\d+)\.scope$")
_RUN_ID_RE = re.compile(r"^(?:" + RUN_PREFIX + "|" + STRAY_PREFIX + r")(\d+)-\d+-\d+\.scope$")

_TIMEOUT_S = 10
# How long to wait for a freshly asked-for scope to exist. Measured at ~1.5s on
# the machine this was written on; five is slack, and running out only means the
# strays stay where they are until the next sweep.
_SCOPE_APPEAR_TIMEOUT_S = 5.0
_SCOPE_POLL_S = 0.2
# The placeholder process exists only to bring the scope into being, and is
# killed the moment the strays are in. The timeout is its dead-man's switch: if
# this portal dies mid-eviction, the placeholder goes away by itself rather than
# holding an otherwise-empty scope open for good.
_PLACEHOLDER_HOLD_S = 300


class Stray(NamedTuple):
    """A process still inside a run's scope after the run ended."""

    pid: int
    command: str


class Eviction(NamedTuple):
    """What one sweep of one run scope did."""

    unit: str
    stray_unit: Optional[str]
    moved: tuple[Stray, ...]
    husk_stopped: bool


class StrayScope(NamedTuple):
    """A live stray scope, for the settings page."""

    unit: str
    run_id: Optional[int]
    processes: tuple[Stray, ...]


# --- reading ---------------------------------------------------------------


def _systemctl(*args: str) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("systemctl --user %s failed: %s", " ".join(args), exc)
        return None


def control_group(unit: str) -> Optional[Path]:
    """The cgroup directory backing a unit, asked of systemd rather than
    computed - the slice layout is not ours to predict, and a wrong guess would
    have us reading somebody else's processes."""
    proc = _systemctl("show", "-p", "ControlGroup", "--value", unit)
    if proc is None or proc.returncode != 0:
        return None
    path = (proc.stdout or "").strip()
    if not path or not path.startswith("/"):
        return None
    return CGROUP_ROOT / path.lstrip("/")


def _command_of(pid: int) -> str:
    """A readable command line for a pid, or a placeholder if it has gone.

    Only for display - nothing branches on it. `/proc/<pid>/cmdline` is
    NUL-separated, and empty for a kernel thread or a process being reaped.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return "(gone)"
    text = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    return text[:200] if text else "(no command line)"


def processes_in(unit: str) -> Optional[list[Stray]]:
    """Every process in a unit's cgroup. None means we could not find out.

    Three answers rather than two, for the same reason `runlimit.scope_is_active`
    gives three: "the scope is empty" and "we could not read it" want opposite
    handling, and only the first one may lead to stopping the unit.
    """
    cg = control_group(unit)
    if cg is None:
        return None
    try:
        raw = (cg / "cgroup.procs").read_text(encoding="utf-8")
    except OSError:
        return None
    out: list[Stray] = []
    for line in raw.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        out.append(Stray(pid=pid, command=_command_of(pid)))
    return out


def _list_units(pattern: str) -> list[str]:
    proc = _systemctl("list-units", pattern, "--all", "--plain", "--no-legend", "--no-pager")
    if proc is None or proc.returncode != 0:
        return []
    units = []
    for line in (proc.stdout or "").splitlines():
        name = line.split(maxsplit=1)[0].strip() if line.split() else ""
        if name.endswith(".scope"):
            units.append(name)
    return units


def run_id_of(unit: str) -> Optional[int]:
    """The run id baked into a run or stray scope name, if it has one. A run
    spawned with `run_id=None` is tagged `x` and yields None."""
    m = _RUN_ID_RE.match(unit or "")
    return int(m.group(1)) if m else None


def stray_unit_for(run_unit: str) -> Optional[str]:
    m = _RUN_UNIT_RE.match(run_unit or "")
    return f"{STRAY_PREFIX}{m.group(1)}.scope" if m else None


# --- evicting --------------------------------------------------------------


def _open_scope(unit: str) -> Optional[subprocess.Popen]:
    """Bring an empty scope into being and hand back the placeholder holding it.

    A scope cannot be created empty: `systemd-run --scope` registers the unit
    and then *becomes* the command, so something has to be in it. The
    placeholder is that something, and it is killed as soon as the strays are
    migrated in - verified on systemd 259 that the scope stays active, keeps its
    `MemoryMax`, and holds only the migrated processes afterwards.
    """
    limit = runlimit.configured_max_bytes()
    argv = [
        "systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}",
    ]
    if limit is not None:
        argv += ["-p", f"MemoryMax={limit}", "-p", "MemorySwapMax=0"]
    # Kill only a greedy process, never the whole group - the same reasoning as
    # the run scope, and it matters more here, where the group is a server Wes
    # is looking at.
    argv += ["-p", "OOMPolicy=continue", "--", "sleep", str(_PLACEHOLDER_HOLD_S)]
    try:
        return subprocess.Popen(
            argv, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not open stray scope %s: %s", unit, exc)
        return None


def _await_cgroup(unit: str, deadline: float) -> Optional[Path]:
    while time.monotonic() < deadline:
        cg = control_group(unit)
        if cg is not None and cg.is_dir():
            return cg
        time.sleep(_SCOPE_POLL_S)
    return None


def _migrate(cg: Path, strays: list[Stray]) -> list[Stray]:
    """Move processes into a cgroup by writing their pids to `cgroup.procs`.

    That single write *is* the move on cgroup v2, and it is permitted inside the
    user manager's own delegated subtree, which is where every scope here lives.
    A pid that has exited in the meantime fails with ESRCH and is simply not
    reported as moved.
    """
    procs = cg / "cgroup.procs"
    moved: list[Stray] = []
    for stray in strays:
        try:
            procs.write_text(str(stray.pid), encoding="utf-8")
        except OSError as exc:
            log.info("Could not move pid %s into %s: %s", stray.pid, cg.name, exc)
            continue
        moved.append(stray)
    return moved


def evict(run_unit: str) -> Optional[Eviction]:
    """Empty a finished run's scope, moving anything left into its own.

    Returns None when there was nothing to do or nothing could be found out -
    the caller is on the end-of-run path and must never care.

    The caller is responsible for the one thing this cannot check: that the run
    really is over. Sweeping a live run would move the agent itself out of the
    scope containing it.
    """
    if not run_unit:
        return None
    strays = processes_in(run_unit)
    if strays is None:
        return None
    if not strays:
        # The empty-but-active case. Nothing to rescue, but the husk is real and
        # `--collect` demonstrably does not always take it.
        stopped = runlimit.stop_scope(run_unit)
        return Eviction(run_unit, None, (), stopped) if stopped else None

    stray_unit = stray_unit_for(run_unit)
    if stray_unit is None:
        log.info("Not a run scope name, leaving it alone: %s", run_unit)
        return None

    placeholder = _open_scope(stray_unit)
    if placeholder is None:
        return None
    moved: list[Stray] = []
    try:
        cg = _await_cgroup(stray_unit, time.monotonic() + _SCOPE_APPEAR_TIMEOUT_S)
        if cg is not None:
            moved = _migrate(cg, strays)
    finally:
        placeholder.terminate()
        try:
            placeholder.wait(timeout=_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            placeholder.kill()

    if not moved:
        # Nothing was rescued, so the run scope still holds them. Stopping it
        # would kill exactly the processes this function exists to preserve.
        log.warning("Could not move %s stray(s) out of %s", len(strays), run_unit)
        return None

    log.info(
        "Moved %s stray process(es) out of %s into %s: %s",
        len(moved), run_unit, stray_unit,
        ", ".join(s.command.split(" ")[0] for s in moved),
    )
    return Eviction(run_unit, stray_unit, tuple(moved), runlimit.stop_scope(run_unit))


# --- sweeping --------------------------------------------------------------


def _minted_by_a_live_stranger(unit: str) -> bool:
    """Is this scope owned by a portal process that is still running and is not
    us? Then it is not ours to sweep, whatever our own database says.

    This is the fence that `protected` cannot be, and the reason is in how
    `protected` is built: from the caller's OWN runs table. That is exactly
    right for the process that spawned those runs and exactly wrong for anybody
    else, because a second reader's table does not describe this machine. A
    throwaway portal instance started under /tmp for a screenshot on 2026-07-29
    booted with an empty database, correctly concluded that nothing on the
    machine was protected, and rehoused the live service's in-flight agent run
    out of its own cgroup - the third firing of this hazard, after two in the
    test suite, and the first from real code rather than from tests.

    That screenshot trick is now a documented, recommended recipe
    (`docs/looking-at-the-ui.md`) - it is the only way to look at the portal's
    own UI without restarting the live service on top of your own run. So this
    fence is load-bearing rather than defensive: an agent will start a second
    portal on this machine on purpose, and nothing else stops it eating the
    first one's scopes. Note that `worker_enabled=0` is NOT that something -
    `_sweep_strays` runs ahead of the worker gate in `worker._tick`.

    The scope name carries the pid of the process that minted it, so the
    question is answerable without any shared state:

    * **Our own pid** - ours, and `protected` decides as before.
    * **A dead pid** - the spawner is gone, so this is a scope that outlived an
      earlier portal process. Sweeping it is the entire point of this module.
    * **A live pid that is not ours** - somebody else's live run. Hands off.

    Pid reuse can make the third case fire on a scope whose real owner died and
    whose number was recycled. That errs toward leaving a leftover in place for
    one more sweep, which is the harmless direction; the alternative errs toward
    evicting a live agent, which is the incident this exists to stop.
    """
    pid = runlimit.minting_pid(unit)
    if pid is None or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, just running as somebody we may not signal. Still a stranger.
        return True
    except OSError:  # pragma: no cover - defensive
        return True
    return True


def finished_scopes(protected: set[str]) -> list[str]:
    """Run scopes whose run is over, newest name last.

    `protected` is the whole safety mechanism *within one portal process* and
    must be over-inclusive: any unit that might belong to a live run belongs in
    it. The caller builds it from both the database (`runs.scope_unit` where
    status is running) and `runlimit.known_scopes()`, because there is a window
    between spawning a run and writing its scope name down in which the database
    alone would call a live run finished.

    Across processes it is no safety mechanism at all - see
    `_minted_by_a_live_stranger`, which is the second filter here and the one
    that does not depend on the caller's database being about this machine.
    """
    return [
        u for u in _list_units(f"{RUN_PREFIX}*.scope")
        if u not in protected and not _minted_by_a_live_stranger(u)
    ]


def sweep(protected: set[str]) -> list[Eviction]:
    """Evict strays from every run scope whose run has ended."""
    out = []
    for unit in finished_scopes(protected):
        eviction = evict(unit)
        if eviction is not None:
            out.append(eviction)
    return out


def listing() -> list[StrayScope]:
    """Live stray scopes and what is in them, for the settings page."""
    out = []
    for unit in sorted(_list_units(f"{STRAY_PREFIX}*.scope")):
        procs = processes_in(unit) or []
        out.append(StrayScope(unit=unit, run_id=run_id_of(unit), processes=tuple(procs)))
    return out


def stop(unit: str) -> bool:
    """Stop one stray scope, killing what is in it. The settings-page button.

    Guarded by the name: this route takes a unit from a form, and stopping an
    arbitrary user unit on request is a different and much larger thing than
    stopping a leftover the portal itself created.
    """
    if not unit.startswith(STRAY_PREFIX) or not unit.endswith(".scope"):
        return False
    return runlimit.stop_scope(unit)

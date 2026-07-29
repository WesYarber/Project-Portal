"""Per-run memory containment: a runaway tool must not take the portal down.

The bug this exists for, seen seven times on 2026-07-26 between 13:40 and 17:02:
a tool inside an agent run allocated ~17 GB on an 18 GB machine, the kernel's
*global* OOM killer fired, and because that tool was a descendant of
`project-portal.service` systemd applied its default `OOMPolicy=stop` to the
whole unit. The portal died, every run in flight was reconciled as "orphaned"
on restart, and the next run did it again. One bad allocation was enough to stop
the whole board, repeatedly, with a message that named neither memory nor the
project responsible - Wes saw only "SimpleClick keeps orphaning runs".

Worth naming precisely, because it shaped the design: the greedy process was
**not always the same project, or the same runtime**. Five events were
SimpleClickTrack's `bun test` (runs 511-515, each orphaned within two minutes of
starting); two were *this* project's own runs writing this very module, as
`python3` at 17 GB (runs 516-517); and an eighth at 17:33 was a 17 GB `python3`
under `claude-remote-control.service`, a different unit entirely. So this is a
property of running untrusted-ish build commands on a fixed-size box, not a
quirk of one project - which is why the fix is per-run containment rather than
anything specific to bun, to tests, or to SimpleClickTrack.

Two things follow, and they are deliberately independent:

1. `deploy/project-portal.service` sets `OOMPolicy=continue`. A descendant
   being OOM-killed is *never* a reason to stop the portal. That line alone
   would have kept the portal up through all five events.
2. This module puts each agent run in its own transient systemd scope with a
   `MemoryMax`, so a runaway allocation hits a cgroup ceiling long before the
   machine runs out. That is what stops the *other* services on the box (docker,
   cloudflared - both were feeling the pressure in the kernel log) being the
   ones the global OOM killer picks.

The scope also carries `OOMPolicy=continue`, which matters more than it looks.
The default (`stop`/`kill`) sets `memory.oom.group` and kills every process in
the cgroup, so an over-eager test command would take the whole `claude` session
with it. With `continue` the kernel kills only the greedy process: the agent's
Bash tool returns "Killed" with rc 137, the agent sees it, and the run carries
on and can report what happened.

Everything here fails open. A machine with no user systemd manager, a kernel
without `memory.peak`, a `systemd-run` that refuses - all of them mean runs
spawn exactly as they did before. A hardening feature that can stop runs from
starting is worse than the problem it solves.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import NamedTuple, Optional

from app import db

log = logging.getLogger("portal.runlimit")

CGROUP_ROOT = Path("/sys/fs/cgroup")
MEMINFO = Path("/proc/meminfo")

# Fraction of the machine's RAM a single run may hold when nothing is
# configured. Two runs in parallel is the shipped default (`max_parallel_runs`),
# so 40% each leaves a fifth of the box for the portal itself and for whatever
# else the machine is running - which on Wes's server is a docker stack.
DEFAULT_FRACTION = 0.40
# ...but never squeeze a run below this. A build that compiles something, or a
# browser harness, legitimately wants a couple of gigabytes, and a cap that
# fires on ordinary work would be read as the portal being broken.
MIN_BYTES = 2 * 1024**3


class Sample(NamedTuple):
    """What a run's cgroup says about its memory, read while it is alive."""

    peak_bytes: Optional[int]
    oom_kills: int


# --- configuration ---------------------------------------------------------

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([kmgt]i?b?)?\s*$", re.I)
_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def parse_size(text: str) -> Optional[int]:
    """"6G", "512M", "1.5 GiB", "0", a plain byte count - or None if it is not
    a size at all. Suffixes are binary throughout, matching both systemd's
    `MemoryMax=` and the way /proc/meminfo reports."""
    m = _SIZE_RE.match(text or "")
    if not m:
        return None
    value = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    if suffix and suffix[0] in _UNITS:
        value *= _UNITS[suffix[0]]
    return int(value)


def total_memory_bytes() -> Optional[int]:
    try:
        for line in MEMINFO.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def default_max_bytes() -> Optional[int]:
    """The cap when the setting is blank: a fraction of this machine's RAM.

    Derived rather than a constant because the number that protects an 18 GB
    home server would either be no protection at all or an unusable straitjacket
    on somebody else's box - the same reasoning as site.py guessing the hostname
    from the machine instead of shipping one.
    """
    total = total_memory_bytes()
    if not total:
        return None
    return max(MIN_BYTES, int(total * DEFAULT_FRACTION))


def configured_max_bytes() -> Optional[int]:
    """The per-run memory ceiling, or None for "do not cap".

    Blank means the derived default. "0", "off" and "none" disable the cap
    outright - an escape hatch that must exist, because a cap is the kind of
    thing that eventually stops legitimate work on a machine nobody foresaw.
    """
    raw = (db.get_setting("run_memory_max") or "").strip()
    if not raw:
        return default_max_bytes()
    if raw.lower() in {"0", "off", "none", "no", "unlimited"}:
        return None
    parsed = parse_size(raw)
    if parsed is None or parsed <= 0:
        log.warning("run_memory_max=%r is not a size; using the default", raw)
        return default_max_bytes()
    return parsed


def human(nbytes: Optional[int]) -> str:
    if not nbytes:
        return "unlimited"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if nbytes < 1024 or unit == "TiB":
            return f"{nbytes:.0f} {unit}" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024.0
    return f"{nbytes:.1f} TiB"


# --- spawning --------------------------------------------------------------

_available: Optional[bool] = None


def available(refresh: bool = False) -> bool:
    """Can we actually make a transient scope here? Probed once, then cached.

    Probed rather than assumed: `systemd-run --user` needs a running user
    manager and a session bus, and the portal has to boot on machines that have
    neither (a container, a cron-launched process, a non-systemd distro).
    """
    global _available
    if _available is not None and not refresh:
        return _available
    try:
        proc = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", "true"],
            capture_output=True,
            timeout=10,
        )
        _available = proc.returncode == 0
        if not _available:
            log.info(
                "Per-run memory caps unavailable: systemd-run exited %s (%s)",
                proc.returncode,
                proc.stderr.decode(errors="replace").strip()[:200] or "no message",
            )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Per-run memory caps unavailable: %s", exc)
        _available = False
    return _available


_seq = itertools.count(1)
# scope name -> the run_id it was minted for, so sample() can check it is
# reading the right cgroup without the name having to be recomputable.
_scopes: dict[Optional[int], str] = {}


def scope_name(run_id: Optional[int]) -> str:
    """This run's scope unit name, minted once and remembered.

    Unique per run, per portal process *and* per spawn. The run id alone is not
    enough: `run_claude` is also called with `run_id=None`, and systemd refuses
    a unit name that already exists - which showed up as the second such run
    producing no output at all rather than as an error anyone could read.
    """
    existing = _scopes.get(run_id)
    if existing is not None:
        return existing
    tag = "x" if run_id is None else str(run_id)
    name = f"portal-run-{tag}-{os.getpid()}-{next(_seq)}.scope"
    _scopes[run_id] = name
    return name


def forget_scope(run_id: Optional[int]) -> None:
    """Drop a finished run's remembered name so the next spawn mints a fresh
    one (and so the map cannot grow forever in a long-lived process)."""
    _scopes.pop(run_id, None)


_UNIT_RE = re.compile(r"^portal-run-(?:x|\d+)-(\d+)-\d+\.scope$")


def minted_here(unit: Optional[str]) -> bool:
    """Was this scope name minted by the process asking? (i.e. is the run one
    *we* started, rather than one that outlived an earlier portal process?)

    The pid in the name is what makes this answerable at all, and asking it this
    way beats keeping a registry, because the registry is what keeps being
    wrong. `worker._reap_adopted` needs "did I start this run", and the obvious
    stand-in - is its id in `_inflight`? - quietly is not: the daily reflect and
    the learnings compaction both create a real `runs` row but register under a
    fixed slot key (REFLECT_SLOT, COMPACT_SLOT) instead of under their run id.
    A reaper trusting `_inflight` would therefore see a live reflect as somebody
    else's orphan and settle its row in the seconds between the CLI exiting and
    the report being written.

    Parsed rather than substring-matched: `f"-{os.getpid()}-" in unit` also
    matches the run-id segment, so a portal whose pid happened to equal a run id
    would decline to reap that run forever.
    """
    m = _UNIT_RE.match(unit or "")
    return bool(m) and m.group(1) == str(os.getpid())


def unit_of(argv: list[str]) -> Optional[str]:
    """The scope unit `wrap` put on this argv, or None if it did not wrap.

    Read back out of the argv rather than asked of `scope_name`, because the
    caller needs to know whether the wrap actually *applied* - `scope_name`
    happily mints a name for a run that was never scoped, and recording that
    name as a live handle would have the boot sweep asking systemd about a unit
    that never existed.
    """
    for arg in argv:
        if arg.startswith("--unit="):
            return arg[len("--unit="):]
    return None


def wrap(cmd: list[str], run_id: Optional[int]) -> list[str]:
    """`cmd`, wrapped so it runs in its own memory-capped scope - or `cmd`
    unchanged when capping is off or unavailable.

    `--scope` (not `--service`) is what keeps this transparent: systemd-run
    registers the scope and then executes the command in its own process, so
    the caller's pipes, cwd, environment and process group all carry through
    untouched. A `--service` spawn would detach it and break streaming, the
    timeout kill and the cancel button in one go.
    """
    limit = configured_max_bytes()
    if limit is None or not available():
        return cmd
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={scope_name(run_id)}",
        "-p",
        f"MemoryMax={limit}",
        # No swap for the run either. Without this a runaway does not hit the
        # wall, it grinds the machine through 4 GB of swap first (which is
        # exactly what the kernel log showed) and every other service on the box
        # crawls for minutes before anything gets killed.
        "-p",
        "MemorySwapMax=0",
        # See the module docstring: kill only the greedy process, not the whole
        # agent session.
        "-p",
        "OOMPolicy=continue",
        "--",
        *cmd,
    ]


# --- liveness --------------------------------------------------------------
#
# A run's scope is a *sibling* of project-portal.service, not a descendant, so
# restarting the service leaves every run alive and running. That fact is what
# the 2026-07-29 double-run incident turned on: the boot sweep declared those
# survivors dead, which unlocked their workspaces and handed each one to a
# second agent. The two calls below are how a fresh portal process asks systemd
# what is actually true instead of assuming.
#
# The unit name is the whole handle, and it must come from the database: it
# carries the pid of the portal process that minted it, so a new process can
# never recompute it.

_IS_ACTIVE_TIMEOUT_S = 10

# `systemctl is-active` prints one of these words. They are read instead of the
# exit code, and that is not a style choice - the exit code is genuinely wrong
# here. The documented 3 means "known unit, not active", but every scope this
# module makes carries `--collect`, so a finished run's unit is UNLOADED rather
# than inactive, and systemd 259 answers that with **4** while still printing
# "inactive". Since collection is the normal end of every run, exit-code-only
# logic got the single most common case wrong, and reported it as "I could not
# find out" - which is the answer that keeps a project locked forever.
#
# `deactivating` counts as alive on purpose: the processes are still in the
# cgroup, so the workspace is still occupied.
_ALIVE_STATES = {"active", "activating", "reloading", "deactivating", "refreshing"}
_GONE_STATES = {"inactive", "failed", "dead"}


def scope_is_active(unit: Optional[str]) -> Optional[bool]:
    """Is this scope unit still running? None means we could not find out.

    Three answers rather than two, because they want different handling and
    conflating them is how a maybe becomes a fact. A caller deciding whether to
    unlock a workspace deserves to know the difference between "systemd says it
    is gone" and "systemd did not answer".
    """
    if not unit:
        return None
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            timeout=_IS_ACTIVE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Could not ask systemd about %s: %s", unit, exc)
        return None
    if proc.returncode == 0:
        return True
    state = proc.stdout.decode(errors="replace").strip().splitlines()
    word = state[-1].strip() if state else ""
    if word in _ALIVE_STATES:
        return True
    if word in _GONE_STATES:
        return False
    log.info(
        "systemctl is-active %s exited %s saying %r (%s); treating as unknown",
        unit, proc.returncode, word,
        proc.stderr.decode(errors="replace").strip()[:200] or "no message",
    )
    return None


def stop_scope(unit: str) -> bool:
    """Kill everything in a scope this process does not own. True if systemd
    accepted the stop.

    This is the only way to cancel a run that outlived the portal process which
    started it: there is no `Popen` to signal, but the cgroup is still there and
    stopping the unit kills every process in it, tool children included.
    """
    if not unit:
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "stop", unit],
            capture_output=True, timeout=_IS_ACTIVE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not stop scope %s: %s", unit, exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "systemctl stop %s exited %s (%s)", unit, proc.returncode,
            proc.stderr.decode(errors="replace").strip()[:200] or "no message",
        )
        return False
    return True


# --- observation -----------------------------------------------------------


def _cgroup_of(pid: int) -> Optional[Path]:
    """The cgroup v2 directory a live process is in, from /proc/<pid>/cgroup.

    Asking the process itself beats computing where systemd *should* have put
    the scope: the answer is right even if the slice layout differs, and it is
    self-checking - if the basename is not our scope the wrap did not take and
    we must not report the portal's own numbers as the run's.
    """
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0":
                return CGROUP_ROOT / parts[2].lstrip("/")
    except (OSError, ValueError):
        pass
    return None


def _read_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def cgroup_for(pid: int, run_id: Optional[int]) -> Optional[Path]:
    """This run's scope directory, or None when the run is not in a scope of
    ours - an uncapped spawn, a dead process, no cgroup v2.

    The name check is the self-check that matters: on an unwrapped spawn the
    process sits in the *portal's own* cgroup, and reporting the portal's
    memory events as the run's would invent an OOM that never happened.
    """
    cg = _cgroup_of(pid)
    if cg is None or cg.name != scope_name(run_id):
        return None
    return cg


def read_sample(cg: Path) -> Optional[Sample]:
    """Read a scope directory that has already been identified.

    Split from `cgroup_for` so a caller can hold on to the path: /proc/<pid>
    stops answering the moment the process is reaped, and the last read - the
    one that catches a kill in the final seconds of a run - has to happen
    without it.
    """
    events = 0
    try:
        for line in (cg / "memory.events").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill":
                events = int(value)
                break
    except (OSError, ValueError):
        return None
    # memory.peak is Linux 5.19+; absent on older kernels, which costs us the
    # nice-to-have number and none of the kill detection.
    return Sample(peak_bytes=_read_int(cg / "memory.peak"), oom_kills=events)


def sample(pid: int, run_id: Optional[int]) -> Optional[Sample]:
    """Resolve this run's scope and read it, in one call."""
    cg = cgroup_for(pid, run_id)
    return None if cg is None else read_sample(cg)


def kill_note(limit: Optional[int] = None) -> str:
    """The sentence shown when a run's scope OOM-killed something."""
    cap = human(limit if limit is not None else configured_max_bytes())
    return (
        f"a command in this run was killed for exceeding the {cap} per-run "
        f"memory cap. The run was not stopped - only the process that asked "
        f"for too much. Raise or clear `run_memory_max` on the settings page "
        f"if the work genuinely needs more."
    )

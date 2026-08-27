"""A kernel-held lease on a project workspace: one agent in a directory, ever.

This is defect 4 of the 2026-07-29 double-run incident, and the only one of the
four that is correct *by construction* rather than by a correctly-written check.

The portal's invariant, stated in `db.running_project_ids`:

    "A project never gets two concurrent runs: they would share one workspace
    and one git checkout."

Until this module that invariant was a SELECT. "Is this project busy" was
derived entirely from `runs.status = 'running'`, so any bug that cleared that
column silently unlocked an occupied workspace - which is exactly what happened
on 2026-07-29, when the boot sweep declared five live runs dead and the fresh
worker handed two busy checkouts to a second agent apiece. Fixing the sweep made
that particular query right. It did not make the *approach* right: a derived
answer is only ever as good as every line of code that maintains its inputs.

A `flock` is a different kind of answer. The kernel holds it, and the kernel
releases it when the holding process dies and at no other time - no timer to
tune, no column to maintain, no restart path to get right, and nothing to
reconcile at boot. A portal that crashes hard, is SIGKILLed, or is restarted
mid-run cannot leak the lock and cannot break it.

Three things about the shape of it are load-bearing.

**The lock target is the workspace directory itself**, not a lock file inside
it. A file inside the workspace is deletable by the very agent it constrains -
one `git clean -xfd` and the next locker creates a fresh file, takes a lock on a
different inode, and walks into an occupied checkout believing it is free. The
directory is the resource being protected, it cannot be unlinked out from under
a live run, and locking it needs nothing created, cleaned up or committed.

**`--close` is not optional.** Without it the lock's file descriptor is
inherited by every process the agent spawns, including the ones it deliberately
detaches - and the 2026-07-29 incident also found five finished runs whose
scopes were still held open by exactly that: a preview server, a `bun` process,
a leftover reverse SSH tunnel. Any one of those would have held its project's
workspace locked forever, which is a far worse failure than the one this module
prevents. With `--close`, `flock(1)` keeps the descriptor in its own parent
process and hands the child a clean set, so the lease lasts exactly as long as
the agent and not one second longer. Verified by running it, not by reading the
man page: a `setsid`ed grandchild outliving the run does not hold the lock.

**The `flock` goes INSIDE the systemd scope, not outside it.** Composed the
other way the holding process would sit in `project-portal.service`'s own
cgroup, so restarting the service would kill the lock holder while the agent
carried on working in its sibling scope - releasing the lease on a workspace
that is still occupied, which is the precise failure this exists to prevent.
See `agent_runner.run_claude` for the composition, and `runlimit` for why the
scope is a sibling in the first place.

Like `runlimit`, everything here fails open. No `flock(1)`, a directory that
cannot be opened, an OS without BSD locks - all of them mean runs spawn exactly
as they did before, back to the SELECT alone. A hardening feature that can stop
every run on the board from starting is worse than the problem it solves.

**What takes a lease, and what deliberately does not.** Every agent that can
write to a directory two agents could share takes one:

* a project run leases `data/projects/<slug>`;
* the daily reflect and the learnings compaction both lease
  `config.MEMORY_DIR`, because they work in the same directory and rewrite the
  same two files - their only previous guard was a pair of in-memory slots that
  die with the portal process, on a box that restarts itself several times an
  hour to load its own new code;
* a one-off task leases its own workspace, whose only previous guard was
  `db.oneoff_running` - a SELECT on `runs.status`, which is the exact derived
  answer this module exists to stop trusting alone. Two agents resuming one CLI
  session fork the conversation as well as the checkout.

An **ask** takes none, and that is a decision rather than an oversight. It is
read-only by construction (`ask.build_command` denies Bash, Edit and Write and
a test pins those flags), so it cannot collide with anything - and because the
lease is `--nonblock`, giving it one would turn "ask a question about a project
that happens to be mid-run" into a refusal. Reading a workspace somebody is
writing is what a question about live work IS.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger("portal.worklock")

# What `flock -n` exits with when the workspace is already leased. Chosen away
# from the small numbers a real command uses: without `-E` a conflict is
# indistinguishable from the agent's own "exited 1", and the portal would report
# a refused run as a crashed one. 75 is EX_TEMPFAIL, which is what this is.
CONFLICT_RC = 75

_FLOCK = "flock"


# --- availability ----------------------------------------------------------

_available: Optional[bool] = None


def available(refresh: bool = False) -> bool:
    """Is `flock(1)` here, and does it understand the options we need?

    Probed rather than assumed, and probed for `--close` specifically: it is a
    util-linux extension, and a build without it would take the lock and then
    leak it into every process the agent detaches. Silently degrading to a
    lock that outlives its run is worse than not locking at all, so a `flock`
    that cannot do this is treated as no `flock`.
    """
    global _available
    if _available is not None and not refresh:
        return _available
    if shutil.which(_FLOCK) is None:
        log.info("Workspace leases unavailable: no %s on PATH", _FLOCK)
        _available = False
        return _available
    try:
        proc = subprocess.run(
            [_FLOCK, "--help"], capture_output=True, timeout=10,
        )
        text = (proc.stdout + proc.stderr).decode(errors="replace")
        _available = "--close" in text and "--conflict-exit-code" in text
        if not _available:
            log.info("Workspace leases unavailable: %s lacks --close", _FLOCK)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Workspace leases unavailable: %s", exc)
        _available = False
    return _available


# --- spawning --------------------------------------------------------------


def wrap(cmd: list[str], lock_dir: Optional[Path]) -> list[str]:
    """`cmd`, wrapped so it holds an exclusive lease on `lock_dir` while it
    runs - or `cmd` unchanged when leasing is unavailable or not asked for.

    Note there is no `--` before the command: `flock(1)` does not accept one and
    fails with "failed to execute --" if given it, which presents as a run that
    produces no output at all. It needs none, because option parsing stops at
    the lock target and the command's own flags pass through untouched.
    """
    if not lock_dir or not available():
        return cmd
    return [
        _FLOCK,
        # Hand the child a clean descriptor set. See the module docstring: this
        # is what stops a detached preview server holding the workspace forever.
        "--close",
        # Never wait. A second agent is not something to queue behind - the
        # scheduler will come back on the next tick, and a blocking lock would
        # tie up a parallel slot for the length of somebody else's run.
        "--nonblock",
        "--conflict-exit-code", str(CONFLICT_RC),
        str(lock_dir),
        *cmd,
    ]


# --- observation -----------------------------------------------------------


def is_busy(lock_dir: Optional[Path]) -> Optional[bool]:
    """Does somebody hold this workspace? None means we could not find out.

    Three answers rather than two, for the same reason `runlimit.scope_is_active`
    gives three: a caller deciding whether to start a run deserves to know the
    difference between "the kernel says it is free" and "we could not ask".

    Taking the lock and immediately dropping it is the only way to read a BSD
    lock - there is no query interface - and it is safe here precisely because
    acquiring it means it was free. Note this answers correctly even when the
    holder is *this* process on another descriptor: `flock` treats two opens of
    one file as independent, so the portal cannot fool itself into thinking a
    workspace it already leased is free.

    This is a cheap pre-flight check and NOT the mutual exclusion. Something
    else may take the lease between this answer and the spawn; `wrap` is what
    actually makes that harmless. The point of asking first is to avoid burning
    a run row, a slot and an entry in the day's budget on a spawn that would
    exit immediately.
    """
    if not lock_dir:
        return None
    try:
        fd = os.open(str(lock_dir), os.O_RDONLY)
    except OSError as exc:
        log.info("Could not open %s to check its lease: %s", lock_dir, exc)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError as exc:
        # A filesystem with no BSD locks (some network mounts). Unknown, not
        # free: reporting "free" here would be inventing a fact.
        log.info("Could not test the lease on %s: %s", lock_dir, exc)
        return None
    else:
        # Explicit, though closing the descriptor below would release it anyway
        # - a BSD lock lives on the open file description, not on the process.
        # Kept because the release is the load-bearing half of this function and
        # a reader should not have to know that to see it happen: if this ever
        # grows a cached descriptor, the `finally` stops being the release and
        # this line starts being it.
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# --- holding one from inside this process ----------------------------------


class Busy(Exception):
    """Somebody else holds the lease."""


class Unavailable(Exception):
    """The lease could not be taken for a reason that is not contention."""


@contextlib.contextmanager
def held(lock_dir: Path) -> Iterator[None]:
    """Hold the workspace lease for the duration of a block of work in *this*
    process, rather than for the lifetime of a spawned agent.

    `wrap` covers the spawn case and cannot cover this one: there is no child to
    wrap when the portal itself edits a workspace (see app/revert.py). The lock
    is the same BSD lock on the same directory, so the two exclude each other -
    an agent's `flock(1)` and this share one inode.

    **This one fails closed**, which is the opposite of every other decision in
    this module, and deliberately. Everywhere else a missing lease means a run
    starts unlocked - annoying at worst. Here the lease guards a *destructive*
    operation on a directory an agent may be mid-edit in, so "could not lock"
    has to mean "did not do it". Raising rather than returning a flag is the
    same argument continued: a caller cannot forget to check an exception.
    """
    try:
        fd = os.open(str(lock_dir), os.O_RDONLY)
    except OSError as exc:
        raise Unavailable(f"cannot open {lock_dir}: {exc}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy(f"{lock_dir} is leased by a live run") from exc
        except OSError as exc:
            raise Unavailable(f"cannot lock {lock_dir}: {exc}") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def workspace_resource(slug: str) -> str:
    """How a project workspace is named in a refusal."""
    return f"the `{slug}` workspace"


# The reflect and the compaction both work in `config.MEMORY_DIR` and both
# rewrite files in it, so to a lease they are one resource and not two - which
# their two separate in-memory slots have never been able to say.
MEMORY_RESOURCE = "the shared memory directory"


def oneoff_resource(task_id: int) -> str:
    """How a one-off task's own workspace is named in a refusal."""
    return f"the workspace for one-off task {task_id}"


def refused_note(resource: str) -> str:
    """The journal line for a run that started and found its directory leased.

    `resource` names the thing in the reader's terms, because not every leased
    directory is a project workspace - see the three helpers above. Passing a
    bare slug here would read as "another agent still holds project-portal",
    which is the one phrasing that does not say what was actually held.

    Worth a journal entry rather than a silent log line: it means the portal's
    own bookkeeping disagreed with the kernel, and the kernel was right. If this
    ever appears, something upstream is scheduling onto a busy directory and the
    lease is the only thing that caught it.
    """
    return (
        f"Run refused: another agent still holds {resource}. "
        f"Nothing was changed. The portal's own bookkeeping said it was free "
        f"and the kernel's lock on the directory said it was not - the lock is "
        f"the one that is right, so the run stopped rather than putting two "
        f"agents in one directory. See app/worklock.py."
    )

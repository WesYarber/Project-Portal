#!/usr/bin/env python3
"""Bring a fresh clone up on this machine, and prove it answers.

    python3 deploy/setup.py            # do it
    python3 deploy/setup.py --check    # report only, change nothing

Written to be run by an agent as much as by a person, which is the whole reason
it exists rather than four lines in a README. Three properties follow from that:

**It never guesses at a person.** Everything it can decide from the machine it
decides (hostname, login, ports); everything it cannot it leaves alone and
prints under HUMAN. An agent cannot log a browser into a Claude subscription,
so the honest end state of an unattended setup is "serving, one thing left for
you" - not a script that hangs on a prompt nobody is there to answer.

**It is idempotent.** Every step checks before it acts and says "already" when
there is nothing to do, so a half-finished setup is fixed by running it again.
That matters more than it sounds: the most likely reader is a run that timed
out somewhere in the middle of the previous attempt.

**It ends by starting the thing and asking it a question.** A setup that
reports success because `pip` exited 0 has checked pip, not the portal. This
one boots uvicorn on a scratch port, waits for `/api/ping` to say `pong`, and
shuts it down again - so "OK" means a real HTTP request got a real answer out
of this checkout.

Exit status is 0 when the portal is serving, 1 when a step failed, and 0 with
a HUMAN list when the only thing outstanding needs hands.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / "venv"
MIN_PYTHON = (3, 11)

# What the smoke test asks for and what it must hear back. Deliberately the
# cheapest endpoint in the app: it touches no database and renders no template,
# so a failure here is "the process is not serving" rather than "some page has
# a bug", which are different problems with different fixes.
PING_PATH = "/api/ping"
PING_EXPECTED = "pong"


class Report:
    """What happened, in the order it happened, plus what is left for a human."""

    def __init__(self) -> None:
        self.human: list[str] = []
        # Steps that are still outstanding but are this script's OWN job, kept
        # apart from `human` because the two lists are read by different
        # readers. `human` is what an unattended agent hands back to a person,
        # so a machine task filed there sends somebody to do work the script
        # does itself - and, worse, left the run believing it had finished.
        self.pending: list[str] = []
        # Kept as well as printed, so a test can assert WHICH failure was
        # reported. "The portal exited during startup" and "did not answer
        # within 45s" both end the run the same way and mean opposite things -
        # a crash to read, versus a machine to wait longer on.
        self.failures: list[str] = []
        self.failed = False

    def ok(self, message: str) -> None:
        print(f"  ok      {message}")

    def already(self, message: str) -> None:
        print(f"  already {message}")

    def did(self, message: str) -> None:
        print(f"  did     {message}")

    def bad(self, message: str) -> None:
        print(f"  FAILED  {message}", file=sys.stderr)
        self.failures.append(message)
        self.failed = True

    def needs_a_person(self, message: str) -> None:
        print(f"  human   {message}")
        self.human.append(message)

    def still_to_do(self, message: str) -> None:
        """Outstanding, but nobody has to be fetched for it."""
        print(f"  todo    {message}")
        self.pending.append(message)


def venv_python() -> Path:
    return VENV / ("Scripts" if os.name == "nt" else "bin") / "python"


def venv_usable() -> bool:
    """Whether the virtualenv is one that can actually install anything.

    Not `venv_python().exists()`, which is what this was and which is wrong on
    the machine it matters on. `python3 -m venv` lays out the directory and the
    interpreter symlink FIRST and bootstraps pip second, so a box without
    ensurepip - Debian and Ubuntu, where it is the separate `python3-venv`
    package - is left with a `venv/bin/python` that exists, runs, and has no
    pip. The file test called that "already", skipped creation on every re-run,
    and then failed further down in a way that named neither cause nor cure.
    """
    python = venv_python()
    if not python.exists():
        return False
    probe = subprocess.run(
        [str(python), "-c", "import pip"], capture_output=True, timeout=60
    )
    return probe.returncode == 0


def free_port() -> int:
    """A port nothing is on, asked of the kernel rather than picked.

    Picking a number and hoping is how a smoke test fails on a busy box and
    reads as a broken checkout.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def check_python(report: Report) -> None:
    if sys.version_info < MIN_PYTHON:
        have = ".".join(str(x) for x in sys.version_info[:3])
        want = ".".join(str(x) for x in MIN_PYTHON)
        report.bad(f"python {want}+ required, this is {have}")
        return
    report.ok(f"python {'.'.join(str(x) for x in sys.version_info[:3])}")


def check_git(report: Report) -> None:
    if shutil.which("git"):
        report.ok("git on PATH")
    else:
        # Not fatal to serving, but every project workspace is a git repo, so a
        # portal without git will run agents that cannot commit their work.
        report.bad("git is not on PATH - agent workspaces cannot be committed without it")


def check_claude_cli(report: Report) -> None:
    """The one dependency that needs a person, and the one worth being clear about.

    A missing CLI is not a failed setup: the portal serves, holds ideas and
    answers questions perfectly well with no way to run an agent. It is a thing
    to be told, at the end, in the list of things only hands can finish.
    """
    exe = shutil.which("claude")
    if not exe:
        report.needs_a_person(
            "install the Claude Code CLI (https://claude.com/claude-code) and log it in "
            "- the portal serves without it, but no agent run can start"
        )
        return
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=20
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    report.ok(f"claude CLI: {out or exe}")
    report.needs_a_person(
        "confirm the CLI is logged in (`claude` once, interactively) or set "
        "auth_mode = \"api_key\" in portal.toml with a key in secrets/anthropic_key.txt"
    )


def make_venv(report: Report, check_only: bool) -> bool:
    if venv_usable():
        report.already(f"virtualenv at {VENV}")
        return True
    half_made = VENV.exists()
    if check_only:
        report.still_to_do(
            f"the virtualenv at {VENV} is unusable and has to be rebuilt"
            if half_made
            else f"no virtualenv at {VENV} yet"
        )
        return False
    if half_made:
        # A previous attempt that died bootstrapping pip. Left in place it is
        # indistinguishable from a good one to everything downstream, so it is
        # cleared rather than reused - and said out loud, because deleting a
        # directory somebody may have put something in should never be silent.
        report.did(f"removing the unusable virtualenv at {VENV}")
        shutil.rmtree(VENV, ignore_errors=True)
    made = subprocess.run([sys.executable, "-m", "venv", str(VENV)], capture_output=True, text=True)
    if made.returncode != 0 or not venv_usable():
        # BOTH streams. `python -m venv` prints its ensurepip failure - the one
        # that names the apt package to install - on STDOUT, so a message built
        # from stderr alone is empty, which is exactly how this was first
        # reported on a fresh clone: "FAILED could not create a virtualenv:"
        # and nothing after the colon.
        why = (made.stdout.strip() + "\n" + made.stderr.strip()).strip()
        report.bad(
            "could not create a working virtualenv"
            + (f":\n{why[:900]}" if why else " and it said nothing about why")
        )
        return False
    report.did(f"created a virtualenv at {VENV}")
    return True


def install_requirements(report: Report, check_only: bool) -> bool:
    python = venv_python()
    if not python.exists():
        return False
    have = subprocess.run(
        [str(python), "-c", "import fastapi, uvicorn, jinja2, httpx, cryptography"],
        capture_output=True,
    )
    if have.returncode == 0:
        report.already("dependencies installed")
        return True
    if check_only:
        report.still_to_do("dependencies from requirements.txt are not installed")
        return False
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
        capture_output=True,
        text=True,
    )
    if installed.returncode != 0:
        report.bad(f"pip install failed: {installed.stderr.strip()[-600:]}")
        return False
    report.did("installed requirements.txt")
    return True


def check_config(report: Report) -> None:
    """Say what the portal has decided about itself, rather than asking.

    Every key is optional and the defaults are read from the machine, so a
    fresh clone needs no file at all. What it DOES need is for somebody to
    notice if the auto-detected hostname is one no phone can resolve - which is
    a thing to print, not a thing to prompt about.
    """
    python = venv_python()
    if not venv_usable():
        # `venv_usable` rather than `python.exists()`: a half-bootstrapped venv
        # has an interpreter that runs, and app/config imports cleanly under
        # it, so this printed a confident "reachable at http://..." line
        # directly beneath the FAILED that said there was no environment.
        return
    shown = subprocess.run(
        [
            str(python),
            "-c",
            "from app import config as c; s = c.SITE; "
            "print(s.host); print(s.port); print(s.owner or ''); print(s.auth_mode)",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        report.bad(f"the app will not import: {shown.stderr.strip()[-600:]}")
        return
    host, port, owner, auth = (shown.stdout.splitlines() + ["", "", "", ""])[:4]
    report.ok(f"reachable at http://{host}:{port} (auth_mode: {auth})")
    if not owner:
        report.needs_a_person(
            'set `owner = "Your Name"` in portal.toml - agents are told whose behalf '
            "they work on, and an unnamed owner reads oddly in every prompt"
        )
    report.needs_a_person(
        f"check that `{host}` is a name your PHONE can resolve; if not, set `host` in "
        "portal.toml - every URL the portal prints is read from another device"
    )


def smoke_test(report: Report) -> bool:
    """Boot it on a scratch port, ask it something, shut it down.

    On a port the kernel handed out rather than the configured one, so this
    neither collides with a portal already running here nor briefly exposes a
    half-configured one on the address people use.

    And under `PORTAL_SMOKE_TEST`, which is the difference between asking the
    app a question and starting it. Without it this function boots the whole
    service: the worker loop schedules runs, and on the empty board of the
    fresh clone that is the case this script exists for, the first tick goes
    straight to the daily reflect and spawns a real, billed `claude -p`. A
    setup script that spends money to prove the install worked is not a check,
    it is a side effect. See `app/main.on_startup` for the other two.
    """
    python = venv_python()
    if not python.exists():
        return False
    port = free_port()
    proc = subprocess.Popen(
        [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PORTAL_SMOKE_TEST": "1"},
    )
    url = f"http://127.0.0.1:{port}{PING_PATH}"
    deadline = time.monotonic() + 45
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                report.bad(f"the portal exited during startup:\n{(proc.stdout.read() or '')[-800:]}")
                return False
            try:
                with urllib.request.urlopen(url, timeout=2) as answer:
                    body = answer.read().decode("utf-8", "replace").strip()
                if body == PING_EXPECTED:
                    report.ok(f"smoke test: {PING_PATH} answered {body!r}")
                    return True
                report.bad(f"{PING_PATH} answered {body!r}, expected {PING_EXPECTED!r}")
                return False
            except (urllib.error.URLError, OSError, TimeoutError):
                time.sleep(0.4)  # still starting
        report.bad(f"the portal did not answer {PING_PATH} within 45s")
        return False
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _print_human(report: Report) -> None:
    """Last, and never merged with the machine's own outstanding work."""
    if not report.human:
        return
    print(f"\n{len(report.human)} thing(s) only a person can do:")
    for item in report.human:
        print(f"  - {item}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Set this checkout up and prove it serves.")
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    ap.add_argument("--no-smoke-test", action="store_true", help="skip booting it at the end")
    args = ap.parse_args()

    print(f"Project Portal setup - {ROOT}\n")
    report = Report()

    print("Prerequisites")
    check_python(report)
    check_git(report)
    check_claude_cli(report)

    print("\nEnvironment")
    if make_venv(report, args.check):
        install_requirements(report, args.check)

    print("\nConfiguration")
    check_config(report)

    # --check promises to change nothing, and booting the app creates `data/`
    # and the database on a fresh clone. So the one step that writes is the one
    # step --check skips.
    if not args.no_smoke_test and not args.check and not report.failed and venv_python().exists():
        print("\nSmoke test")
        smoke_test(report)

    if report.failed:
        print("\nSetup did NOT complete. Fix the FAILED line(s) above and run this again.")
        _print_human(report)
        return 1

    if report.pending:
        # The one lie this script was able to tell. `--check` on a fresh clone
        # never calls bad() - a missing virtualenv is not a failure, it is
        # simply work not done yet - so `report.failed` stayed False and the
        # run printed "The portal is ready" above a start command naming an
        # interpreter that did not exist. An unattended agent reads that,
        # believes it, and skips the install it was about to do.
        print(f"\nSetup is NOT finished - {len(report.pending)} step(s) still to do:")
        for item in report.pending:
            print(f"  - {item}")
        print(f"\nRun it without --check to do them:\n  {sys.executable} deploy/setup.py")
        _print_human(report)
        return 1

    print("\nThe portal is ready. Start it with:")
    print(f"  {venv_python()} -m uvicorn app.main:app --host 0.0.0.0 --port 8500")
    print("  (or install deploy/project-portal.service as a systemd user unit)")
    _print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

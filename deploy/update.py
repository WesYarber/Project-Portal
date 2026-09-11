#!/usr/bin/env python3
"""Bring an installed clone up to the latest published code, and prove it still serves.

    python3 deploy/update.py            # do it
    python3 deploy/update.py --check    # report only, change nothing

`deploy/setup.py` is how an install begins. This is how it keeps up. They are
separate scripts because they answer to different fears: setup's is "does this
machine have what the portal needs", and this one's is "am I about to replace
working code, and can I put it back".

The reason it exists at all is that the portal is developed on one machine and
run on more than one. The development box mirrors its source to GitHub after
every change it makes to itself (`app/mirror.py`); every other install follows
by pulling. Without a command for that half, "follow along" means a person
remembering a sequence of four commands, one of which is conditional - and Wes
does not want to be handed a command line to copy in the first place.

Three properties it is built around, all of them about not making things worse:

**It never merges.** A follower install that has fast-forwarded from origin can
always be reasoned about: its code is a published commit. One that has merged
local work is a fork nobody knows the shape of, and the first sign of it is a
conflict in the middle of an update. So the pull is `--ff-only`, a diverged
checkout is a hard stop naming both sides, and an uncommitted edit stops it
before git is asked to do anything at all.

**It checks the new code before it runs the new code.** Dependencies are
installed and the app is imported on the *new* tree before the service is
restarted, so the ordinary failure - a requirement that moved and was not
installed - is caught while the old process is still happily serving.

**It never boots a second portal.** The obvious health check is setup.py's, and
against a live data directory it is destructive: booting `app.main:app` settles
the running service's in-flight runs as orphans and binds the preview port out
from under it. So the check here is an import on the new tree, and then a real
HTTP request to the real service after it restarts.

Exit status is 0 when the install is up to date and serving, 1 when a step
failed or something needs hands.

**One thing to know before running this from a scratch clone.** `SERVICE` is a
machine-global unit name, so `systemctl --user restart project-portal.service`
restarts *this machine's* portal whatever checkout the script was invoked from.
Testing it against a throwaway clone on the development box therefore restarts
the live service - which is exactly what happened while this was being built on
2026-09-01. No damage came of it (agent runs live in systemd scopes of their
own, not in the service's cgroup, so both in-flight runs were adopted rather
than orphaned), but it is not what the person running it expected. Pass
`--no-restart` when the checkout you are updating is not the one being served.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy.setup import Report, venv_python  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SERVICE = "project-portal.service"

# What the post-restart check asks the real service, and how long it will wait.
# The same endpoint setup.py uses and for the same reason: it touches no
# database and renders no template, so a failure is "not serving" rather than
# "some page is broken".
PING_PATH = "/api/ping"
PING_EXPECTED = "pong"
RESTART_WAIT_SEC = 60

# Changing any of these means the running process is not what the tree says it
# is, so the service has to be restarted for the update to mean anything.
# `requirements.txt` is listed separately below because it needs pip as well.
REQUIREMENTS = "requirements.txt"

# Spelled out rather than imported from `app.mirror`, which is where the
# publish side of it lives: this script runs on the system interpreter before
# the venv is known to be good, and importing `app` would drag fastapi in.
TRAILER = "Source-commit:"


def git(*args: str, repo: Path | None = None) -> subprocess.CompletedProcess:
    """Every git call this script makes, always against `ROOT`.

    `repo` reads the module global at call time rather than defaulting to it in
    the signature. A default argument is evaluated once, at import, so
    `repo: Path = ROOT` binds whatever ROOT was then and quietly ignores it
    being repointed afterwards - which is how a test aimed at a throwaway clone
    ends up running `git merge --ff-only` against the developer's own checkout.
    """
    return subprocess.run(
        ["git", *args], cwd=str(repo or ROOT), capture_output=True, text=True
    )


def branch() -> str:
    name = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return name if name and name != "HEAD" else "main"


def dirty() -> list[str]:
    """Tracked files that differ from HEAD.

    Untracked files are ignored on purpose: `data/`, `secrets/` and
    `portal.toml` are all untracked and all present on any real install, and a
    fast-forward cannot touch them. Counting them would make every update
    refuse.
    """
    done = git("status", "--porcelain", "--untracked-files=no")
    return [line for line in done.stdout.splitlines() if line.strip()]


def check_repo(report: Report) -> bool:
    """That this is a checkout that follows something, before touching it."""
    if not (ROOT / ".git").exists():
        report.bad(f"{ROOT} is not a git checkout, so there is nothing to update from")
        return False
    if git("remote", "get-url", "origin").returncode != 0:
        report.bad(
            "this checkout has no `origin` remote, so it follows nothing. Add the "
            "repository you installed from:  git remote add origin <url>"
        )
        return False
    changes = dirty()
    if changes:
        # Named rather than counted. "3 modified files" sends somebody to go
        # and run git status; the files themselves are the answer.
        report.bad(
            "there are uncommitted changes to tracked files, and an update would "
            "have to merge or discard them:\n        "
            + "\n        ".join(changes[:20])
        )
        return False
    report.ok(f"checkout follows origin on `{branch()}`, working tree clean")
    return True


def fetch(report: Report) -> bool:
    done = git("fetch", "--quiet", "origin", branch())
    if done.returncode != 0:
        report.bad(f"could not fetch from origin: {done.stderr.strip()[-400:]}")
        return False
    report.ok("fetched origin")
    return True


def pending() -> tuple[str, str, list[str]]:
    """(local head, remote head, the commits between them)."""
    local = git("rev-parse", "HEAD").stdout.strip()
    remote = git("rev-parse", f"refs/remotes/origin/{branch()}").stdout.strip()
    log = git("log", "--oneline", f"HEAD..refs/remotes/origin/{branch()}")
    return local, remote, [line for line in log.stdout.splitlines() if line.strip()]


def diverged() -> bool:
    """True when this checkout has a commit origin does not.

    The one state where continuing is worse than stopping. A follower whose
    HEAD is an ancestor of origin's can be fast-forwarded and reasoned about
    afterwards; one that has its own commits is a fork, and the update would
    either refuse in the middle or quietly write a merge nobody asked for.
    """
    return git("merge-base", "--is-ancestor", "HEAD", f"refs/remotes/origin/{branch()}").returncode != 0


def changed_files(old: str, new: str) -> list[str]:
    done = git("diff", "--name-only", old, new)
    return [line for line in done.stdout.splitlines() if line.strip()]


def fast_forward(report: Report, check_only: bool) -> tuple[bool, list[str]]:
    """Move to origin's head, or say why not. Returns (ok, files that changed)."""
    local, remote, commits = pending()
    if not remote:
        report.bad(f"origin has no `{branch()}` branch to follow")
        return False, []
    if local == remote:
        report.already(f"up to date with origin/{branch()} at {local[:7]}")
        return True, []
    if diverged():
        report.bad(
            f"this checkout has commits origin/{branch()} does not, so it cannot be "
            f"fast-forwarded. Nothing has been changed. Look at:\n"
            f"        git log --oneline refs/remotes/origin/{branch()}..HEAD"
        )
        return False, []
    print(f"  {len(commits)} new commit(s):")
    for line in commits[:20]:
        print(f"      {line}")
    if len(commits) > 20:
        print(f"      ... and {len(commits) - 20} more")
    if check_only:
        report.still_to_do(f"fast-forward {len(commits)} commit(s) to {remote[:7]}")
        return True, changed_files(local, remote)
    # `--ff-only` is deliberately redundant with the `diverged()` check above:
    # if HEAD is an ancestor of origin's, a plain merge would fast-forward too.
    # Two guards on one invariant is usually a smell, and this is the exception
    # worth keeping - `diverged()` is here to say *why* before anything is
    # touched, and this is git enforcing the same rule on the one step that
    # cannot be undone, without trusting my comparison to have been right.
    done = git("merge", "--ff-only", f"refs/remotes/origin/{branch()}")
    if done.returncode != 0:
        report.bad(f"the fast-forward failed: {done.stderr.strip()[-400:]}")
        return False, []
    report.did(f"fast-forwarded {local[:7]} -> {remote[:7]}")
    return True, changed_files(local, remote)


def install_requirements(report: Report, files: list[str], check_only: bool) -> bool:
    """pip only when `requirements.txt` actually moved.

    An unconditional `pip install -r` on every update is thirty seconds of
    network for nothing on the overwhelmingly common update, which touches
    Python files only.
    """
    if REQUIREMENTS not in files:
        report.already("dependencies unchanged")
        return True
    if check_only:
        report.still_to_do(f"{REQUIREMENTS} changed and would be installed")
        return True
    python = venv_python()
    if not python.exists():
        report.bad(f"no virtualenv at {python}; run deploy/setup.py first")
        return False
    done = subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "-r", str(ROOT / REQUIREMENTS)],
        capture_output=True, text=True,
    )
    if done.returncode != 0:
        report.bad(f"pip install failed: {done.stderr.strip()[-600:]}")
        return False
    report.did(f"installed the new {REQUIREMENTS}")
    return True


def import_check(report: Report) -> bool:
    """Import the app on the new tree, without starting it.

    This is the whole health check that happens *before* the restart, and it is
    an import rather than a boot for a reason worth keeping: booting
    `app.main:app` against a live data directory settles the running service's
    in-flight runs as orphans and binds the preview port. An import catches
    what actually breaks across an update - a moved dependency, a syntax error,
    a renamed module - while costing the running service nothing.
    """
    python = venv_python()
    if not python.exists():
        report.bad(f"no virtualenv at {python}; run deploy/setup.py first")
        return False
    done = subprocess.run(
        [str(python), "-c", "import app.main"],
        cwd=str(ROOT), capture_output=True, text=True,
        env={**os.environ, "PORTAL_SMOKE_TEST": "1"},
    )
    if done.returncode != 0:
        report.bad(
            "the new code does not import, so the running service has been left "
            f"alone:\n{done.stderr.strip()[-800:]}"
        )
        return False
    report.ok("the new code imports")
    return True


def service_active() -> bool:
    done = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", SERVICE], capture_output=True
    )
    return done.returncode == 0


def restart(report: Report, check_only: bool) -> bool:
    """Restart the unit if there is one, or say what to do instead.

    Not having a systemd unit is not a failure. Somebody running the portal in
    a terminal or a container has a perfectly good install; they just have to
    restart it themselves, and being told so beats being told nothing.
    """
    if not service_active():
        report.needs_a_person(
            f"restart the portal to load the new code ({SERVICE} is not running "
            "here, so this script does not know how you start it)"
        )
        return True
    if check_only:
        report.still_to_do(f"restart {SERVICE}")
        return True
    done = subprocess.run(
        ["systemctl", "--user", "restart", SERVICE], capture_output=True, text=True
    )
    if done.returncode != 0:
        report.bad(f"could not restart {SERVICE}: {done.stderr.strip()[-400:]}")
        return False
    report.did(f"restarted {SERVICE}")
    return True


def serving(report: Report, port: int) -> bool:
    """Ask the real, restarted service the one question, and wait for it."""
    import time

    url = f"http://127.0.0.1:{port}{PING_PATH}"
    deadline = time.monotonic() + RESTART_WAIT_SEC
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as answer:
                body = answer.read().decode("utf-8", "replace").strip()
            if body == PING_EXPECTED:
                report.ok(f"the portal on :{port} answered {PING_PATH} with {body!r}")
                return True
            last = f"answered {body!r}, expected {PING_EXPECTED!r}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = str(exc)
        time.sleep(0.5)
    report.bad(f"the portal on :{port} did not come back within {RESTART_WAIT_SEC}s ({last})")
    return False


def configured_port() -> int:
    """The port the service actually listens on, read the way the app reads it."""
    try:
        from app import site  # imported late: it is only needed if we restarted
        return int(site.SITE.get("port", 8500))
    except Exception:  # noqa: BLE001 - a missing config is not a reason to fail here
        return 8500


def source_commit() -> str:
    """The `Source-commit:` trailer of the commit this checkout is on.

    The published mirror's history is unrelated to the source's on purpose, so
    HEAD here means nothing to the portal that published it. The trailer is
    the one id both sides can compare, which makes it the number the status
    row actually needs.
    """
    done = git("log", "-1", "--format=%B")
    if done.returncode != 0:
        return ""
    for line in done.stdout.splitlines():
        line = line.strip()
        if line.startswith(TRAILER):
            return line[len(TRAILER):].strip()
    return ""


def write_status(mode: str, report: Report, code: int, changed: bool, summary: str) -> int:
    """Record what this run concluded, and hand `code` straight back.

    Before this, the script printed a report and exited holding nothing. The
    only record that the half-hour timer had fired was the systemd journal,
    which no browser can read - so "am I up to date?" could not be answered
    from the portal's own pages, only over ssh. This is the file those pages
    read.

    Two constraints shape it. It goes in `data/`, which is gitignored: a byte
    written anywhere tracked would leave the tree dirty and hard-stop every
    future `--ff-only` pull on this machine. And it is best-effort - the
    return value is the exit status the systemd unit reports, and a status
    file is a nicety that is not allowed to turn a good update into a failed
    one, or a failed one into a success. Any error writing it is swallowed
    on purpose.
    """
    try:
        head = git("rev-parse", "HEAD")
        payload = {
            "at": int(time.time()),
            "mode": mode,
            "ok": code == 0,
            "changed": changed,
            "summary": summary,
            "head": head.stdout.strip() if head.returncode == 0 else "",
            "source_commit": source_commit(),
            "failures": list(report.failures),
            "human": list(report.human),
        }
        path = ROOT / "data" / "update-status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n")
    except Exception:  # noqa: BLE001 - see the docstring: never change the exit status
        pass
    return code


def _stopped(report: Report) -> str:
    return f"stopped: {report.failures[-1]}" if report.failures else "stopped"


def _update(args: argparse.Namespace, report: Report) -> tuple[int, bool, str]:
    """The update itself. Returns (exit code, did the tree move, one line)."""
    print("Checkout")
    if not check_repo(report) or not fetch(report):
        return 1, False, _stopped(report)

    print("\nUpdate")
    ok, files = fast_forward(report, args.check)
    if not ok:
        return 1, False, _stopped(report)
    if not files:
        print("\nAlready up to date. Nothing to restart.")
        return 0, False, "already up to date with origin/main"

    print("\nDependencies")
    if not install_requirements(report, files, args.check):
        return 1, not args.check, _stopped(report)

    if args.check:
        print(f"\n{len(report.pending)} step(s) would run. Do them with:")
        print(f"  {sys.executable} deploy/update.py")
        return 1, False, f"{len(report.pending)} step(s) would run - this was a --check"

    print("\nVerification")
    if not import_check(report):
        return 1, True, _stopped(report)

    if args.no_restart:
        print("\nUpdated. The service was left alone (--no-restart), so it is still "
              "running the old code.")
        return 0, True, "updated; service left alone (--no-restart)"

    print("\nRestart")
    restarted = service_active()
    if not restart(report, args.check):
        return 1, True, _stopped(report)
    if restarted and not serving(report, configured_port()):
        return 1, True, _stopped(report)

    if report.human:
        print(f"\n{len(report.human)} thing(s) only a person can do:")
        for item in report.human:
            print(f"  - {item}")
        return 1, True, f"updated, but {len(report.human)} thing(s) need a person"

    print("\nUp to date and serving.")
    return 0, True, "updated and serving"


def main() -> int:
    ap = argparse.ArgumentParser(description="Update this install to the published code.")
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    ap.add_argument("--no-restart", action="store_true", help="update the tree, leave the service alone")
    args = ap.parse_args()

    print(f"Project Portal update - {ROOT}\n")
    report = Report()
    code, changed, summary = _update(args, report)
    # Last, and on every path out of `_update` including the refusals: a run
    # that stopped is exactly the run somebody needs to be able to see.
    return write_status(
        mode="check" if args.check else "update",
        report=report, code=code, changed=changed, summary=summary,
    )


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Delete-the-fix mutation sweep over the decision points added on 2026-09-01.

Covers `app/mirror.py` (the automatic publish), `deploy/update.py` (the other
end of the wire, on a machine nobody is watching) and the `PORTAL_SMOKE_TEST`
guard in `app/main.on_startup`.

Runs against an EXPORT of the working tree in /tmp, never the tree itself - a
sweep that edits the live checkout is the mistake this repo has paid for most
often, and `git ls-files | tar` rather than `cp -a` because `data/` is far
larger than the tmpfs (see docs and learnings).

Usage: venv/bin/python deploy/sweep_mirror.py [first] [last]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TESTS = [
    "tests/test_mirror.py",
    "tests/test_update.py",
    "tests/test_smoke_boot.py",
    "tests/test_conftest_fence.py",
    "tests/test_run_failures.py",
    "tests/test_restart_survivors.py",
]

# (name, file, find, replace). `find` must be unique in the file - a mutation
# that lands on an identical line elsewhere tests nothing and reports a pass.
MUTATIONS: list[tuple[str, str, str, str]] = [
    # --- app/mirror.py: is this the machine that publishes? -----------------
    (
        "any directory counts as the mirror",
        "app/mirror.py",
        '    if not (target / ".git").exists():\n        return False\n    return _git(target, "remote", "get-url", "origin").returncode == 0',
        "    return True",
    ),
    (
        "a mirror with no origin remote still publishes",
        "app/mirror.py",
        '    return _git(target, "remote", "get-url", "origin").returncode == 0',
        "    return True",
    ),
    (
        "the configured check is skipped in pending",
        "app/mirror.py",
        "    if not configured(target):\n        return None",
        "    pass",
    ),
    # --- the clean-tree guard -----------------------------------------------
    (
        "a dirty source tree is published anyway",
        "app/mirror.py",
        "    if not source_clean():\n        return None",
        "    pass",
    ),
    (
        "the clean check is inverted",
        "app/mirror.py",
        "    if not source_clean():\n        return None",
        "    if source_clean():\n        return None",
    ),
    (
        "untracked files count as dirty",
        "app/mirror.py",
        '    done = _git(config.APP_ROOT, "status", "--porcelain", "--untracked-files=no")',
        '    done = _git(config.APP_ROOT, "status", "--porcelain")',
    ),
    (
        "a non-empty status still reads as clean",
        "app/mirror.py",
        "    return done.returncode == 0 and not done.stdout.strip()",
        "    return done.returncode == 0",
    ),
    (
        "a source that is not a repo reports a head anyway",
        "app/mirror.py",
        '    if not (config.APP_ROOT / ".git").exists():\n        return ""',
        "    pass",
    ),
    (
        "an empty source head does not stop the publish",
        "app/mirror.py",
        "    head = source_head()\n    if not head:\n        return None",
        "    head = source_head()",
    ),
    # --- the trailer --------------------------------------------------------
    (
        "the trailer is matched anywhere in the line, not at its start",
        "app/mirror.py",
        "        if line.startswith(TRAILER):",
        "        if TRAILER not in line:",
    ),
    (
        "an empty trailer value reads as a real sha",
        "app/mirror.py",
        "            return sha or None",
        "            return sha",
    ),
    (
        "the published head is never compared to the source",
        "app/mirror.py",
        '    if published_head(target) != head:\n        return f"the source moved to {head[:7]}"',
        "    pass",
    ),
    (
        "the head comparison is inverted",
        "app/mirror.py",
        "    if published_head(target) != head:",
        "    if published_head(target) == head:",
    ),
    (
        "publish.py stops stamping the trailer",
        "deploy/publish.py",
        'commit = git(target, "commit", "-q", "-m", stamped(message))',
        'commit = git(target, "commit", "-q", "-m", message)',
    ),
    (
        "the trailer is stamped with no head at all",
        "deploy/publish.py",
        '    return f"{message.rstrip()}\\n\\n{mirror.TRAILER} {head}\\n" if head else message',
        "    return message",
    ),
    # --- committed but never pushed -----------------------------------------
    (
        "an unpushed mirror is treated as up to date",
        "app/mirror.py",
        '    if unpushed(target):\n        return "the last publish was committed but never pushed"',
        "    pass",
    ),
    (
        "a mirror that never pushed reads as level with origin",
        "app/mirror.py",
        "    if remote.returncode != 0:\n        return True",
        "    if remote.returncode != 0:\n        return False",
    ),
    (
        "local and remote heads are never compared",
        "app/mirror.py",
        "    return local.stdout.strip() != remote.stdout.strip()",
        "    return False",
    ),
    (
        "an empty mirror reports work to push",
        "app/mirror.py",
        "    if local.returncode != 0:\n        return False  # nothing committed yet; publishing will make the commit",
        "    if local.returncode != 0:\n        return True",
    ),
    # --- the publish subprocess ---------------------------------------------
    (
        "the publish commits without pushing",
        "app/mirror.py",
        '        "--to", str(target),\n        "--push",',
        '        "--to", str(target),',
    ),
    (
        "a nonzero publish reports success",
        "app/mirror.py",
        "    if done.returncode == 0:\n        return Outcome(True, (done.stdout or \"\").strip())",
        '    return Outcome(True, (done.stdout or "").strip())',
    ),
    (
        "the publish has no timeout",
        "app/mirror.py",
        "            timeout=PUBLISH_TIMEOUT_SEC,",
        "",
    ),
    (
        "a timed-out publish reports success",
        "app/mirror.py",
        '        return Outcome(False, f"the publish did not finish within {PUBLISH_TIMEOUT_SEC}s")',
        '        return Outcome(True, "timed out")',
    ),
    # --- the tick -----------------------------------------------------------
    (
        "the tick publishes even when nothing is pending",
        "app/mirror.py",
        "        reason = pending(target)\n        if reason is None:\n            return None",
        "        reason = pending(target) or 'always'",
    ),
    (
        "the backoff window is ignored",
        "app/mirror.py",
        "        if moment < _next_attempt:\n            return None",
        "        pass",
    ),
    (
        "the backoff comparison is inverted",
        "app/mirror.py",
        "        if moment < _next_attempt:",
        "        if moment > _next_attempt:",
    ),
    (
        "the backoff never widens",
        "app/mirror.py",
        "    _backoff = min(_backoff * 2, RETRY_BACKOFF_MAX_SEC)",
        "    _backoff = RETRY_BACKOFF_SEC",
    ),
    (
        "a failure is journalled every single time",
        "app/mirror.py",
        "    if outcome.detail != _reported:\n        _reported = outcome.detail",
        "    if True:\n        _reported = outcome.detail",
    ),
    (
        "a changed failure is swallowed as a repeat",
        "app/mirror.py",
        "    if outcome.detail != _reported:",
        "    if _reported is None:",
    ),
    (
        "a recovery never closes the alarm it opened",
        "app/mirror.py",
        '        if _reported is not None:\n            _note(f"The public mirror is publishing again - {reason}, and it pushed.")',
        "        pass",
    ),
    (
        "every success announces itself in the journal",
        "app/mirror.py",
        "        if _reported is not None:\n            _note(",
        "        if True:\n            _note(",
    ),
    (
        "a raising publish takes the worker tick with it",
        "app/mirror.py",
        '    except Exception:  # noqa: BLE001 - see docstring\n        log.exception("Mirror publish check failed")\n        return None',
        "    except ValueError:\n        return None",
    ),
    # --- app/main.py: the smoke-test guard ----------------------------------
    (
        "any value of PORTAL_SMOKE_TEST turns the service off",
        "app/main.py",
        '    smoke = os.environ.get("PORTAL_SMOKE_TEST", "") == "1"',
        '    smoke = bool(os.environ.get("PORTAL_SMOKE_TEST", ""))',
    ),
    (
        "the smoke guard is inverted",
        "app/main.py",
        '    smoke = os.environ.get("PORTAL_SMOKE_TEST", "") == "1"',
        '    smoke = os.environ.get("PORTAL_SMOKE_TEST", "") != "1"',
    ),
    (
        "a smoke boot settles the running service's runs",
        "app/main.py",
        "    if not smoke:\n        db.reconcile_orphaned_runs_on_boot()",
        "    db.reconcile_orphaned_runs_on_boot()",
    ),
    (
        "a smoke boot starts the worker anyway",
        "app/main.py",
        "    if not smoke:\n        _BACKGROUND_TASKS.append(asyncio.create_task(worker.worker_loop()))",
        "    if True:\n        _BACKGROUND_TASKS.append(asyncio.create_task(worker.worker_loop()))",
    ),
    (
        "setup.py boots the smoke test as the real service",
        "deploy/setup.py",
        '        env={**os.environ, "PORTAL_SMOKE_TEST": "1"},',
        "",
    ),
    # --- deploy/update.py: the refusals -------------------------------------
    (
        "a checkout with no origin is updated anyway",
        "deploy/update.py",
        '    if git("remote", "get-url", "origin").returncode != 0:',
        "    if False:",
    ),
    (
        "a directory that is not a checkout is updated anyway",
        "deploy/update.py",
        '    if not (ROOT / ".git").exists():',
        "    if False:",
    ),
    (
        "an uncommitted edit does not stop the update",
        "deploy/update.py",
        "    changes = dirty()\n    if changes:",
        "    changes = dirty()\n    if False:",
    ),
    (
        "untracked files stop every update forever",
        "deploy/update.py",
        '    done = git("status", "--porcelain", "--untracked-files=no")',
        '    done = git("status", "--porcelain")',
    ),
    (
        "a diverged checkout is merged rather than refused",
        "deploy/update.py",
        "    if diverged():",
        "    if False:",
    ),
    (
        "divergence is decided the wrong way round",
        "deploy/update.py",
        '    return git("merge-base", "--is-ancestor", "HEAD", f"refs/remotes/origin/{branch()}").returncode != 0',
        '    return git("merge-base", "--is-ancestor", "HEAD", f"refs/remotes/origin/{branch()}").returncode == 0',
    ),
    (
        "the fast-forward is a plain merge",
        "deploy/update.py",
        '    done = git("merge", "--ff-only", f"refs/remotes/origin/{branch()}")',
        '    done = git("merge", f"refs/remotes/origin/{branch()}")',
    ),
    (
        "--check moves the checkout anyway",
        "deploy/update.py",
        '    if check_only:\n        report.still_to_do(f"fast-forward {len(commits)} commit(s) to {remote[:7]}")\n        return True, changed_files(local, remote)',
        "    pass",
    ),
    (
        "an install level with origin reports work to do",
        "deploy/update.py",
        '    if local == remote:\n        report.already(f"up to date with origin/{branch()} at {local[:7]}")\n        return True, []',
        "    pass",
    ),
    (
        "pip runs on every update",
        "deploy/update.py",
        '    if REQUIREMENTS not in files:\n        report.already("dependencies unchanged")\n        return True',
        "    pass",
    ),
    (
        "pip never runs, whatever moved",
        "deploy/update.py",
        "    if REQUIREMENTS not in files:",
        "    if True:",
    ),
    (
        "a failed pip install is reported as success",
        "deploy/update.py",
        '    if done.returncode != 0:\n        report.bad(f"pip install failed: {done.stderr.strip()[-600:]}")\n        return False',
        "    pass",
    ),
    (
        "code that does not import is restarted into anyway",
        "deploy/update.py",
        '        report.bad(\n            "the new code does not import, so the running service has been left "\n            f"alone:\\n{done.stderr.strip()[-800:]}"\n        )\n        return False',
        "        return True",
    ),
    (
        "the update boots a second portal to check itself",
        "deploy/update.py",
        '        [str(python), "-c", "import app.main"],',
        '        [str(python), "-m", "uvicorn", "app.main:app"],',
    ),
    (
        "the import check runs as the real service",
        "deploy/update.py",
        '        env={**os.environ, "PORTAL_SMOKE_TEST": "1"},',
        "",
    ),
    (
        "a missing systemd unit is a hard failure",
        "deploy/update.py",
        "    if not service_active():\n        report.needs_a_person(",
        "    if not service_active():\n        report.bad(",
    ),
    # --- the worker wiring, and the test fence ------------------------------
    (
        "the worker never publishes the mirror at all",
        "app/worker.py",
        "    await _publish_mirror()",
        "",
    ),
    (
        "the conftest fence stops pointing the mirror away",
        "tests/conftest.py",
        '    monkeypatch.setattr(mirror, "TARGET", tmp_path / "no-public-mirror")',
        "",
    ),
]


def export(dest: Path) -> None:
    names = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    tar = subprocess.Popen(
        ["tar", "--null", "-T", "-", "-cf", "-"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    untar = subprocess.Popen(["tar", "-xf", "-"], cwd=dest, stdin=tar.stdout)
    tar.stdout.close()
    tar.stdin.write(names)
    tar.stdin.close()
    untar.wait()
    tar.wait()


def main() -> int:
    first = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    last = int(sys.argv[2]) if len(sys.argv) > 2 else len(MUTATIONS)
    root = Path(tempfile.mkdtemp(prefix="sweep-mirror-"))
    dest = root / "portal"
    dest.mkdir()
    export(dest)
    python = str(ROOT / "venv" / "bin" / "python")

    # The baseline, and it is not optional. The first run of this sweep
    # reported 55 mutations caught out of 55, which is not a result, it is a
    # symptom: `test_the_setup_smoke_test_asks_for_it` was already red in the
    # export (it looked for a `venv` the exported tree does not have), and
    # `pytest -x` stopped there every single time. Every mutation "failed the
    # tests" without the tests ever reaching it.
    #
    # A sweep cannot tell a mutation it caught from a suite that was already
    # broken, so it has to be told the suite works first - the same reason a
    # green suite proves nothing until you have seen it go red.
    baseline = subprocess.run(
        [python, "-m", "pytest", "-x", "-q", *TESTS],
        cwd=dest, capture_output=True, text=True,
    )
    if baseline.returncode != 0:
        print("REFUSING TO SWEEP - the unmutated export is already red:\n")
        print(baseline.stdout[-3000:])
        shutil.rmtree(root, ignore_errors=True)
        return 2
    print(f"baseline green ({len(TESTS)} files)\n")

    escaped: list[str] = []
    for index, (name, rel, find, replace) in enumerate(MUTATIONS):
        if not (first <= index < last):
            continue
        path = dest / rel
        original = path.read_text()
        hits = original.count(find)
        if hits != 1:
            print(f"[{index:2}] SKIP  {name}: pattern found {hits} times in {rel}")
            escaped.append(f"{index} (pattern x{hits})")
            continue
        path.write_text(original.replace(find, replace))
        proc = subprocess.run(
            [python, "-m", "pytest", "-x", "-q", *TESTS],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        path.write_text(original)
        caught = proc.returncode != 0
        print(f"[{index:2}] {'caught ' if caught else 'ESCAPED'} {name}")
        if not caught:
            escaped.append(f"{index} {name}")

    print()
    if escaped:
        print(f"{len(escaped)} escaped:")
        for line in escaped:
            print(f"  - {line}")
    else:
        print("all caught")
    shutil.rmtree(root, ignore_errors=True)
    return 1 if escaped else 0


if __name__ == "__main__":
    raise SystemExit(main())

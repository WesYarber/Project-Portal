#!/usr/bin/env python3
"""Delete-the-fix mutation sweep over app/config.py's CLI-version freshness.

The decision points here were added on 2026-09-28, after Wes reported a run
spawning Opus 5 when the picker said 5.5. The gate was right - this machine's
CLI was older than `MODEL_MIN_CLI["opus"]` - but `claude update` then changed
nothing, because `cli_version()` was memoized for the life of a portal process
that had been up for three days.

Every mutation below is a plausible half-fix, and each one either freezes the
version again (so an updated CLI is never noticed, the original bug) or tears
down the fence that keeps the test suite off the real `claude` binary:

- trusting the cache unconditionally, which is the code this replaced;
- stamping the symlink instead of its target, which is what a native install
  actually is - `claude update` repoints the link and leaves its mtime alone;
- dropping mtime or size from the stamp, so a CLI replaced in place is missed;
- treating a missing binary as "never stamped", so an install that appears
  later is never read;
- second-guessing an unstamped cache, which makes every test in the suite shell
  out to the real CLI;
This sweep also earned its keep on the way in. It found `refresh_cli_version()`
clearing the stamp as well as the version - a line that reads like part of the
fix and cannot change any outcome, because a cleared version cache already
forces a re-read and the read path re-stamps regardless. Mutating it away left
the suite green, correctly, and the line was deleted rather than tested.

Runs against an EXPORT of the working tree in /tmp, never the tree itself, so
an interrupted sweep leaves no mutation behind to be read later as an ordinary
bug in whatever is being worked on next.

Usage: venv/bin/python scripts/sweep_cli_version_freshness.py [first] [last]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TESTS = ["tests/test_models.py", "tests/test_module_state.py",
         "tests/test_modeladopt.py", "tests/test_modelwatch.py"]

# (name, file, find, replace). `find` must be unique in the file.
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "the cache is trusted for the life of the process, as it was before",
        "app/config.py",
        "    if _cli_version_cache is not None and not _cli_binary_changed():",
        "    if _cli_version_cache is not None:",
    ),
    (
        "the stamp is taken from the symlink, not the version it points at",
        "app/config.py",
        "        real = os.path.realpath(path)\n        info = os.stat(real)",
        "        real = path\n        info = os.stat(real)",
    ),
    (
        "the stamp drops the mtime, so a CLI replaced at the same size is missed",
        "app/config.py",
        "    return (real, info.st_mtime_ns, info.st_size)",
        "    return (real, info.st_size)",
    ),
    (
        "the stamp drops the size, so only the clock is watched",
        "app/config.py",
        "    return (real, info.st_mtime_ns, info.st_size)\n",
        "    return (real, info.st_mtime_ns)\n",
    ),
    (
        "a missing binary reads as never-stamped, so an install is never noticed",
        "app/config.py",
        '    path = shutil.which("claude")\n    if not path:\n        return ()',
        '    path = shutil.which("claude")\n    if not path:\n        return None',
    ),
    (
        "an unstamped cache is checked, so the suite shells out to the real CLI",
        "app/config.py",
        "    if _cli_binary_stamp is None:\n        return False",
        "    if _cli_binary_stamp is None:\n        return True",
    ),
    (
        "the stamp is never recorded, so every call re-reads the version",
        "app/config.py",
        "    _cli_binary_stamp = stamp\n    return version",
        "    return version",
    ),
    (
        "the stamp is taken AFTER the read, so an update mid-read is lost",
        "app/config.py",
        "    stamp = _binary_stamp()\n    version = DEFAULT_CLI_VERSION",
        "    version = DEFAULT_CLI_VERSION",
    ),
    (
        "an unreadable binary raises instead of stamping, on a path walked per spawn",
        "app/config.py",
        "    except OSError:\n        return ()",
        "    except OSError:\n        raise",
    ),
    (
        "(control) a comment-only edit changes nothing",
        "app/config.py",
        "    `shutil.which` plus one `stat` - no subprocess - so this can guard the",
        "    `shutil.which` plus a `stat` - no subprocess - so this can guard the",
    ),
]

CONTROLS = {len(MUTATIONS) - 1}


def anchors() -> list[tuple[Path, str]]:
    """Every (file, exact string) this sweep mutates, for tests/test_sweep_anchors.py."""
    return [(ROOT / rel, find) for _label, rel, find, _repl in MUTATIONS]


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
    root = Path(tempfile.mkdtemp(prefix="sweep-cliversion-"))
    dest = root / "portal"
    dest.mkdir()
    export(dest)
    python = str(ROOT / "venv" / "bin" / "python")

    wrong: list[str] = []
    for index, (name, rel, find, replace) in enumerate(MUTATIONS):
        if not (first <= index < last):
            continue
        path = dest / rel
        original = path.read_text()
        hits = original.count(find)
        if hits != 1:
            print(f"[{index:2}] SKIP    {name}: pattern found {hits} times in {rel}")
            wrong.append(f"{index} (pattern x{hits})")
            continue
        path.write_text(original.replace(find, replace))
        proc = subprocess.run(
            [python, "-m", "pytest", "-x", "-q", "-p", "no:randomly", *TESTS],
            cwd=dest,
            capture_output=True,
            text=True,
        )
        path.write_text(original)
        caught = proc.returncode != 0
        control = index in CONTROLS
        ok = caught is not control
        if control:
            verdict = "held    " if not caught else "BROKE   "
        else:
            verdict = "caught  " if caught else "ESCAPED "
        print(f"[{index:2}] {verdict}{name}")
        if not ok:
            wrong.append(f"{index} {name}")

    print()
    if wrong:
        print(f"{len(wrong)} wrong:")
        for line in wrong:
            print(f"  - {line}")
    else:
        print("every mutation caught, control held")
    shutil.rmtree(root, ignore_errors=True)
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())

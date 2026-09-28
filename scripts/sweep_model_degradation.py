#!/usr/bin/env python3
"""Delete-the-fix mutation sweep over "this pin is not the model that runs".

Written on 2026-09-28, after Wes asked why a run had used Opus 5 when the
settings picker said Opus 5.5. It had: `config.cli_model()` degrades a pinned
model id to the bare alias when the installed CLI is older than the pin's
`MODEL_MIN_CLI` entry, and nothing on any page said so.

Every decision here decides whether a warning appears. Both directions are
expensive: a missing warning is the silence that started this, and a warning on
a healthy install is a permanent false alarm on the settings page, which is
worse - it is the kind of noise that gets a real one ignored.

The mutations are the plausible half-fixes: a gate that never holds anything
back, one that holds everything back, the boundary off by one version, the
page listing every pin instead of the withheld ones, an announcement that
repeats daily or fires for a live pin, and the two seams between the query and
the page it feeds.

Runs against an EXPORT of the working tree in /tmp, never the tree itself, so
an interrupted sweep leaves no mutation behind to be read later as an ordinary
bug in whatever is being worked on next.

Usage: venv/bin/python scripts/sweep_model_degradation.py [first] [last]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TESTS = ["tests/test_models.py", "tests/test_modeladopt.py",
         "tests/test_modelwatch.py"]

# (name, file, find, replace). `find` must be unique in the file.
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "the gate never holds a pin back, so nothing is ever degraded",
        "app/config.py",
        "    required = gates.get(alias)\n"
        "    return bool(required) and _version_tuple(cli_version()) < _version_tuple(required)",
        "    required = gates.get(alias)\n    return False",
    ),
    (
        "every gated alias is held back whatever version is installed",
        "app/config.py",
        "    return bool(required) and _version_tuple(cli_version()) < _version_tuple(required)",
        "    return bool(required)",
    ),
    (
        "the gate opens one version late, so the CLI that can spawn it still cannot",
        "app/config.py",
        "and _version_tuple(cli_version()) < _version_tuple(required)",
        "and _version_tuple(cli_version()) <= _version_tuple(required)",
    ),
    (
        "every pin is reported as degraded, including the ones that run",
        "app/config.py",
        "        if pinned is None or not _held_back(alias, gates):\n            continue",
        "        if pinned is None:\n            continue",
    ),
    (
        "an announced gate is announced again on every daily check",
        "app/modeladopt.py",
        "        if waiting.get(alias) == model_id:\n"
        "            continue  # already announced, still waiting on `claude update`",
        "        if False:\n"
        "            continue  # already announced, still waiting on `claude update`",
    ),
    (
        "a pin this CLI can spawn is announced as withheld",
        "app/modeladopt.py",
        "        if config.cli_model(alias) == model_id:\n"
        "            continue  # live on this machine; there is nothing to say",
        "        if False:\n"
        "            continue  # live on this machine; there is nothing to say",
    ),
    (
        "the announcement is never recorded, so it repeats and never goes live",
        "app/modeladopt.py",
        "    if fresh:\n        _store(GATED_KEY, waiting)",
        "    if False:\n        _store(GATED_KEY, waiting)",
    ),
    (
        "the settings warning renders with nothing degraded",
        "app/templates/settings.html",
        "      {% if model_degradations %}\n",
        "      {% if True %}\n",
    ),
    (
        "the page is never handed the degradations",
        "app/main.py",
        '            "model_degradations": config.degraded_models(),',
        '            "model_degradations": [],',
    ),
    (
        "(control) a comment-only edit changes nothing",
        "app/config.py",
        "    One row per affected alias: what the picker calls it, the id it is pinned",
        "    One row per affected alias: what the picker names it, the id it is pinned",
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
    root = Path(tempfile.mkdtemp(prefix="sweep-modeldegrade-"))
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

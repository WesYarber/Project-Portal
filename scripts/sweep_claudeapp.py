#!/usr/bin/env python3
"""Delete-the-fix mutation sweep over the Claude app plugin's API.

Wes, 2026-10-02: projects built in the Claude app on another machine keep the
portal as their record, and the portal must never schedule a run on one
(app/claudeapp.py, app/extapi.py). Every guard below is one door a run could
otherwise come through - the scheduled pick, a research burst, a person's "run
now", a note, an answer, a note that arrived mid-run - plus the rules behind
the API that a plausible simplification would quietly break.

Runs against an EXPORT of the working tree in /tmp, never the tree itself.

Usage: venv/bin/python scripts/sweep_claudeapp.py [first] [last]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TESTS = ["tests/test_extapi.py"]

# (name, file, find, replace). `find` must be unique in the file.
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "the scheduled pick lists a project hosted elsewhere",
        "app/db.py",
        '            f"ORDER BY {order}",\n        ).fetchall()\n    elsewhere = claudeapp.hosted_elsewhere_ids()\n    return [r for r in rows if int(r["id"]) not in elsewhere]',
        '            f"ORDER BY {order}",\n        ).fetchall()\n    return rows',
    ),
    (
        "a research burst picks a project hosted elsewhere",
        "app/db.py",
        '"AND research_queued_at != \'\' ORDER BY research_queued_at ASC, id ASC"\n        ).fetchall()\n    elsewhere = claudeapp.hosted_elsewhere_ids()\n    return [r for r in rows if int(r["id"]) not in elsewhere]',
        '"AND research_queued_at != \'\' ORDER BY research_queued_at ASC, id ASC"\n        ).fetchall()\n    return rows',
    ),
    (
        "a person's run-now request on it is honored",
        "app/worker.py",
        "        if claudeapp.hosted_elsewhere(db.get_project(manual_project_id)):",
        "        if False:",
    ),
    (
        "a note wakes it out of review",
        "app/worker.py",
        "    # context instead of waking an agent here (app/claudeapp.py).\n    if claudeapp.hosted_elsewhere(project):",
        "    # context instead of waking an agent here (app/claudeapp.py).\n    if False:",
    ),
    (
        "a run could start on it now",
        "app/worker.py",
        "        return False\n    if claudeapp.hosted_elsewhere(project):\n        return False\n    if db.is_project_running",
        "        return False\n    if db.is_project_running",
    ),
    (
        "a note that arrived mid-run queues another run",
        "app/worker.py",
        "    if claudeapp.hosted_elsewhere(fresh):\n        return False\n",
        "",
    ),
    (
        "a checkout here does not count, so a linked project here stops being scheduled",
        "app/claudeapp.py",
        "    return bool(locations(int(project[\"id\"]))) and not has_workspace_checkout(project)",
        "    return bool(locations(int(project[\"id\"])))",
    ),
    (
        "every project with no checkout counts as hosted elsewhere",
        "app/claudeapp.py",
        "    return bool(locations(int(project[\"id\"]))) and not has_workspace_checkout(project)",
        "    return not has_workspace_checkout(project)",
    ),
    (
        "the pickers' id set ignores the checkout",
        "app/claudeapp.py",
        "    return {int(r[\"id\"]) for r in rows if not has_workspace_checkout(r)}",
        "    return {int(r[\"id\"]) for r in rows}",
    ),
    (
        "closed projects show without search",
        "app/extapi.py",
        "    if not include_closed:",
        "    if False:",
    ),
    (
        "starring twice is an error, not a no-op",
        "app/claudeapp.py",
        '"INSERT OR IGNORE INTO stars',
        '"INSERT INTO stars',
    ),
    (
        "a new star goes first, not last",
        "app/claudeapp.py",
        '"VALUES (?, ?, (SELECT COALESCE(MAX(position), -1) + 1 FROM stars "',
        '"VALUES (?, ?, (SELECT COALESCE(MIN(position), 1) - 1 FROM stars "',
    ),
    (
        "a second end post journals the session again",
        "app/claudeapp.py",
        '        if row["journal_id"]:',
        "        if False:",
    ),
    (
        "a session with nothing to show is journaled",
        "app/claudeapp.py",
        "    if ended and (commits or summary):",
        "    if ended:",
    ),
    (
        "the end post blanks what the start post said",
        "app/claudeapp.py",
        "        if value is None:\n            return stored",
        "        if value is None:\n            return \"\"",
    ),
    (
        "a silent session never times out",
        "app/claudeapp.py",
        '"WHERE ended_at IS NULL AND updated_at < ?",',
        '"WHERE ended_at IS NULL AND updated_at > ?",',
    ),
    (
        "the since section ignores since",
        "app/claudeapp.py",
        "                (pid, since),\n            ).fetchall()\n    else:",
        "                (pid, \"0\"),\n            ).fetchall()\n    else:",
    ),
    (
        "the context is never cut down to size",
        "app/claudeapp.py",
        "        if len(text) <= CONTEXT_CAP:",
        "        if True:",
    ),
    (
        "project_files fails as 'no workspace' instead of saying where it lives",
        "app/crossproject.py",
        "    if elsewhere:\n        # Built in the Claude app",
        "    if False:\n        # Built in the Claude app",
    ),
    (
        "the digest names a workspace that does not exist",
        "app/crossproject.py",
        '        elsewhere or f"**Workspace:** `{workspace}`",',
        '        f"**Workspace:** `{workspace}`",',
    ),
    (
        "the plugin's title is not locked",
        "app/claudeapp.py",
        "        person_id=person_id, title_locked=True,",
        "        person_id=person_id, title_locked=False,",
    ),
    (
        "an already-answered question comes back without its answer",
        "app/extapi.py",
        '        "answer": row["answer"] if status == "answered" else None,',
        '        "answer": None,',
    ),
    (
        "a new question notifies nobody",
        "app/extapi.py",
        "status_code=201, background=task)",
        "status_code=201)",
    ),
    (
        "control: a comment changes, nothing else",
        "app/claudeapp.py",
        "# The context goes into the first message of every Claude app session.",
        "# The context goes into the opening message of every Claude app session.",
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
    root = Path(tempfile.mkdtemp(prefix="sweep-claudeapp-"))
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

#!/usr/bin/env python3
"""Delete-the-fix mutation sweep over the decision points app/inquiry.py added.

Runs against an EXPORT of the working tree in /tmp, never the tree itself - a
sweep that edits the live checkout is the mistake this repo has paid for most
often, and `git ls-files | tar` rather than `cp -a` because `data/` is 11 GB
against a 9.5 GB tmpfs (see docs and learnings).

Usage: venv/bin/python deploy/sweep_inquiry.py [first] [last]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TESTS = [
    "tests/test_inquiry.py",
    "tests/test_crossproject.py",
    "tests/test_portalmcp.py",
    "tests/test_ask.py",
    # `journal_section` was lifted out of `ask.build_prompt`, and the test that
    # owns its side-thread exclusion lives here rather than in test_ask.py. It
    # is on this list because leaving it off is what made mutation 25 "escape"
    # on the first pass - the sweep was not running the test that catches it.
    "tests/test_ask_thread.py",
]

# (name, file, find, replace). `find` must be unique in the file - a mutation
# that lands on an identical line elsewhere tests nothing and reports a pass.
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "the off switch is ignored",
        "app/inquiry.py",
        "    return crossproject.enabled()",
        "    return True",
    ),
    (
        "an empty question is put to another project anyway",
        "app/inquiry.py",
        '    if not question:\n        raise crossproject.Denied("An inquiry needs a question.")',
        "    pass",
    ),
    (
        "the question is not truncated",
        "app/inquiry.py",
        '    question = (question or "").strip()[:MAX_QUESTION_CHARS]',
        '    question = (question or "").strip()',
    ),
    (
        "the per-run cap is off by one",
        "app/inquiry.py",
        "    if run_id and asked_count(run_id) >= MAX_PER_RUN:",
        "    if run_id and asked_count(run_id) > MAX_PER_RUN:",
    ),
    (
        "the cap never counts",
        "app/inquiry.py",
        "    if run_id:\n        _ASKED[int(run_id)] = asked_count(run_id) + 1",
        "    pass",
    ),
    (
        "a refused inquiry spends a slot (counted before the target resolves)",
        "app/inquiry.py",
        "    target = crossproject.resolve(asker_id, slug)",
        "    _ASKED[int(run_id)] = asked_count(run_id) + 1\n"
        "    target = crossproject.resolve(asker_id, slug)",
    ),
    (
        "the question is never written to the target's record",
        "app/inquiry.py",
        '    db.add_journal(\n'
        '        int(target["id"]),\n'
        '        "agent",\n'
        "        QUESTION_KIND,\n"
        "        f\"**{asker['title']}** (`{asker['slug']}`) asks:\\n\\n{question}\",\n"
        "    )",
        "    pass",
    ),
    (
        "the prompt is built after the question is journalled (asked twice)",
        "app/inquiry.py",
        "    task = asyncio.ensure_future(_answer(prompt, target, asker))",
        "    task = asyncio.ensure_future(\n"
        "        _answer(build_prompt(target, asker, question), target, asker)\n"
        "    )",
    ),
    (
        "the slow answer is stopped rather than shielded",
        "app/inquiry.py",
        "        answer = await asyncio.wait_for(asyncio.shield(task), timeout=max(0, wait))",
        "        answer = await asyncio.wait_for(task, timeout=max(0, wait))",
    ),
    (
        "a late answer is never routed anywhere",
        "app/inquiry.py",
        '        task.add_done_callback(lambda done: _deliver_late(int(asker["id"]), target, done))',
        "        pass",
    ),
    (
        "a failed answer reads as an answer",
        "app/inquiry.py",
        "    if not answer:\n        return (\n"
        '            f"`{target[\'slug\']}` could not answer that one - the model call failed or "',
        "    if False:\n        return (\n"
        '            f"`{target[\'slug\']}` could not answer that one - the model call failed or "',
    ),
    (
        "the answer is not truncated",
        "app/inquiry.py",
        '    text = (text or "").strip()[:MAX_ANSWER_CHARS]',
        '    text = (text or "").strip()',
    ),
    (
        "an empty answer is journalled on the target as if it said something",
        "app/inquiry.py",
        "    if not text:\n        return \"\"\n    db.add_journal(",
        "    db.add_journal(",
    ),
    (
        "an empty late answer is journalled on the asking project",
        "app/inquiry.py",
        "    if not answer:\n        return\n    try:\n        db.add_journal(\n"
        "            int(asker_id),",
        "    try:\n        db.add_journal(\n            int(asker_id),",
    ),
    (
        "a stopped answering task is read for a result",
        "app/inquiry.py",
        "    if task.cancelled():\n        return\n    try:\n        answer = task.result()",
        "    try:\n        answer = task.result()",
    ),
    (
        "the wake runs against a project that has been deleted",
        "app/inquiry.py",
        "    project = db.get_project(int(asker_id))\n    if project is None:\n        return",
        "    project = db.get_project(int(asker_id))",
    ),
    (
        "the late answer never queues a run",
        "app/inquiry.py",
        "    wake = asyncio.ensure_future(_wake(asker_id))",
        "    wake = asyncio.ensure_future(asyncio.sleep(0))",
    ),
    (
        "the answering agent stands in the ASKING project's journal",
        "app/inquiry.py",
        '            ask.journal_section(int(target["id"])),',
        '            ask.journal_section(int(asker["id"])),',
    ),
    (
        "the prompt points at the asking project's workspace",
        "app/inquiry.py",
        "                f\"(`{ask.workspace(target['slug'])}`). Read whatever you need.\"",
        "                f\"(`{ask.workspace(asker['slug'])}`). Read whatever you need.\"",
    ),
    (
        "the answering agent is never told who is asking",
        "app/inquiry.py",
        "                f\"## The question, from {asker['title']} (`{asker['slug']}`)\\n\"",
        '                "## The question\\n"',
    ),
    (
        "the answering agent runs in the asking project's workspace",
        "app/inquiry.py",
        '                ask.workspace(str(target["slug"])),',
        '                ask.workspace(str(asker["slug"])),',
    ),
    (
        # `_cross_tools`'s own `crossproject.enabled()` is defense in depth:
        # `readable()` checks the same setting itself, so deleting the outer one
        # is a genuine no-op and mutating it proves nothing. This mutates the
        # check that actually decides, one level down.
        "the tool is offered even when cross-project talk is off",
        "app/crossproject.py",
        "    if not enabled():\n        return []\n    reader_id = int(reader_id)",
        "    reader_id = int(reader_id)",
    ),
    (
        "a run's inquiry tally outlives the run",
        "app/portalmcp.py",
        "    inquiry.forget_run(run_id)",
        "    pass",
    ),
    (
        "the tool call never reaches inquiry",
        "app/portalmcp.py",
        "    if name == inquiry.TOOL_NAME:\n        return await _inquire(scope, arguments)",
        "    pass",
    ),
    (
        "a refusal is reported as a portal failure",
        "app/portalmcp.py",
        "    except crossproject.Denied as refusal:\n        return _result(str(refusal), is_error=True)\n"
        "    except Exception:  # noqa: BLE001 - a broken tool must not kill the run\n"
        '        log.exception("%s failed on run %s", inquiry.TOOL_NAME, scope.run_id)',
        "    except Exception:  # noqa: BLE001 - a broken tool must not kill the run\n"
        '        log.exception("%s failed on run %s", inquiry.TOOL_NAME, scope.run_id)',
    ),
    (
        "the prompt never names the tool",
        "app/crossproject.py",
        "    if inquiry.enabled():\n        lines.extend([\"\", inquiry.prompt_line()])",
        "    pass",
    ),
    (
        "the shared journal section stops excluding the side thread",
        "app/ask.py",
        "    journal = db.list_journal_asc(project_id, limit=limit, exclude=db.SIDE_THREAD)",
        "    journal = db.list_journal_asc(project_id, limit=limit)",
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
    root = Path(tempfile.mkdtemp(prefix="sweep-inquiry-"))
    dest = root / "portal"
    dest.mkdir()
    export(dest)
    python = str(ROOT / "venv" / "bin" / "python")

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
            [python, "-m", "pytest", "-x", "-q", "-p", "no:randomly", *TESTS],
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

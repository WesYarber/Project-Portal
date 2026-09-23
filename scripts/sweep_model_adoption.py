#!/usr/bin/env python3
"""Delete-the-fix mutation sweep over app/modeladopt.py's adoption decision.

The decision points here were added on 2026-09-23, when Wes answered the
adoption question this portal used to file with *"Always adopt the new models-
no need to ask."* Adopting automatically is only safe because of the checks in
this module, so every one of them is a place where a flipped operator would
quietly point every run on the board at a model that 400s - or hold back one
that works.

The mutations are the plausible half-fixes, not arbitrary damage: trusting the
probe's exit status (the real refusal exits 0), comparing date stamps as
version parts (20260101 > 5, which would move Opus 5.5 back to a snapshot of
Opus 5), adopting on `>=` instead of `>`, treating a probe that reached no
verdict as a yes, and skipping the catalog check that stops an unpinned family
adopting an older sibling.

Runs against an EXPORT of the working tree in /tmp, never the tree itself, so
an interrupted sweep leaves no mutation behind to be read later as an ordinary
bug in whatever is being worked on next.

Usage: venv/bin/python scripts/sweep_model_adoption.py [first] [last]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TESTS = ["tests/test_modeladopt.py", "tests/test_modelwatch.py",
         "tests/test_models.py", "tests/test_appearance.py"]

# (name, file, find, replace). `find` must be unique in the file.
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "the probe trusts the exit status, so the 400 that exits 0 reads as a pass",
        "app/modeladopt.py",
        "    found = [marker for marker in ERROR_MARKERS if marker in lowered]\n"
        "    if found:",
        "    found = [marker for marker in ERROR_MARKERS if marker in lowered]\n"
        "    if found and returncode != 0:",
    ),
    (
        "an empty probe answer counts as a pass",
        "app/modeladopt.py",
        '        return {"ok": False, "required_cli": "", "error": "the probe returned nothing"}',
        '        return {"ok": True, "required_cli": "", "error": ""}',
    ),
    (
        "the minimum CLI version is never harvested from the refusal",
        "app/modeladopt.py",
        '            "required_cli": match.group(1) if match else "",',
        '            "required_cli": "",',
    ),
    (
        "date stamps are compared as version parts",
        "app/modeladopt.py",
        "        if len(chunk) >= DATE_PART_DIGITS:\n            continue\n",
        "",
    ),
    (
        "an equal version supersedes, so the same model is adopted forever",
        "app/modeladopt.py",
        "    return version_parts(new_id) > version_parts(current_id)",
        "    return version_parts(new_id) >= version_parts(current_id)",
    ),
    (
        "a different family supersedes, so fable displaces opus",
        "app/modeladopt.py",
        "    if family_of(current_id) != new_family:\n        return False\n",
        "",
    ),
    (
        "an id with no readable version is adoptable",
        "app/modeladopt.py",
        "    if not version_parts(new_id):\n        # An id with no readable version cannot be shown to be an improvement.\n        return False\n",
        "",
    ),
    (
        "the catalog check is dropped, so an unpinned family adopts an older sibling",
        "app/modeladopt.py",
        "    return best_in_family(models, family) == model_id",
        "    return True",
    ),
    (
        "the supersedes check is dropped, so any catalog-newest id is adopted",
        "app/modeladopt.py",
        "    if not supersedes(model_id, pins().get(family)):\n        return False\n",
        "",
    ),
    (
        "a probe that reached no verdict adopts anyway",
        "app/modeladopt.py",
        '    verdict["reason"] = str(result.get("error") or "the probe reached no verdict")\n'
        "    _remember_pending(model, verdict[\"reason\"])\n"
        "    return verdict",
        '    verdict["reason"] = str(result.get("error") or "the probe reached no verdict")\n'
        "    record(verdict[\"alias\"], model_id, label=label)\n"
        "    verdict[\"adopted\"] = True\n"
        "    return verdict",
    ),
    (
        "a CLI-gated adoption is dropped instead of pinned behind its gate",
        "app/modeladopt.py",
        "        record(verdict[\"alias\"], model_id, required, label=label)",
        "        pass",
    ),
    (
        "the gate is recorded from the error text rather than read back from cli_model",
        "app/modeladopt.py",
        '    if required_cli and config.cli_model(alias) != model_id:',
        "    if required_cli:",
    ),
    (
        "cleared_gates never consumes an entry, so 'it is live' repeats daily",
        "app/modeladopt.py",
        "    if keep != blob:\n        _store(GATED_KEY, keep)",
        "    pass",
    ),
    (
        "a superseded gate is announced as live instead of dropped",
        "app/modeladopt.py",
        "        if current.get(alias) != model_id:\n            continue  # superseded while it waited",
        "        if False:\n            continue  # superseded while it waited",
    ),
    (
        "an adoption does not clear a stale min-CLI gate",
        "app/modeladopt.py",
        "    else:\n        gates.pop(alias, None)",
        "    else:\n        pass",
    ),
    (
        "the adopted pin never reaches the spawn boundary",
        "app/config.py",
        "        pins, gates = modeladopt.pins(), modeladopt.min_cli()",
        "        pins, gates = CLI_MODEL_IDS, MODEL_MIN_CLI",
    ),
    (
        "the adopted label never reaches the picker",
        "app/config.py",
        "    return [(value, names.get(value, label)) for value, label in MODEL_CHOICES]",
        "    return list(MODEL_CHOICES)",
    ),
    (
        "a family name matches as a bare prefix, so claude-opusculum-1 is an opus",
        "app/modeladopt.py",
        '        if rest == alias or rest.startswith(f"{alias}-"):',
        "        if rest.startswith(alias):",
    ),
    (
        "probe_fn goes back to a default argument, pinning the real subprocess",
        "app/modeladopt.py",
        "    result = (probe_fn or probe)(model_id)",
        "    result = probe(model_id)",
    ),
    (
        "the daily check never re-reads the CLI version, so a gate never opens",
        "app/modelwatch.py",
        "        await asyncio.to_thread(config.refresh_cli_version)",
        "        pass",
    ),
    (
        "(control) a comment-only edit changes nothing",
        "app/modeladopt.py",
        "# What the probe asks for. Short enough that the spawn costs approximately",
        "# What the probe asks for. Short enough that the spawn costs roughly",
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
    root = Path(tempfile.mkdtemp(prefix="sweep-modeladopt-"))
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

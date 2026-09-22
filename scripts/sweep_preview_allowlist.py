#!/usr/bin/env python3
"""Delete-the-fix mutation sweep over app/preview.py's servable-file allowlist.

The decision points this covers are the ones added on 2026-09-22 to stop the
preview server (:8501, no login, answering the whole LAN) handing out an agent
workspace's `.secrets/`, `.git/` and source. Every mutation here is a plausible
half-fix - guarding GET and not HEAD, a suffix test on the request string
rather than the resolved path, a dotted-segment rule that only looks at the
first path component - and the suite has to go red for each one.

Runs against an EXPORT of the working tree in /tmp, never the tree itself, so
an interrupted sweep leaves no mutation behind to be read later as an ordinary
bug. `git ls-files | tar` rather than `cp -a` because `data/` is enormous
against a 9.5 GB tmpfs. (This is also why nothing here needs the mtime
restoration other projects' in-place sweeps do: no file in the checkout is
ever written.)

Usage: venv/bin/python scripts/sweep_preview_allowlist.py [first] [last]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TESTS = ["tests/test_preview.py"]

# (name, file, find, replace). `find` must be unique in the file.
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "the allowlist is not consulted at all",
        "app/preview.py",
        "        if is_servable(Path(full_path), self._allow_root):\n"
        "            return full_path, stat_result",
        "        if True:\n"
        "            return full_path, stat_result",
    ),
    (
        "the suffix rule is dropped, leaving only the dotted-segment rule",
        "app/preview.py",
        "    return full_path.suffix.lower() in SERVABLE_SUFFIXES",
        "    return True",
    ),
    (
        "the dotted-segment rule is dropped, leaving only the suffix rule",
        "app/preview.py",
        "    if any(part.startswith(\".\") for part in rel.parts):\n        return False",
        "    pass",
    ),
    (
        "only the FIRST path segment is checked for a leading dot",
        "app/preview.py",
        "    if any(part.startswith(\".\") for part in rel.parts):",
        "    if rel.parts and rel.parts[0].startswith(\".\"):",
    ),
    (
        "the suffix is compared without case folding",
        "app/preview.py",
        "    return full_path.suffix.lower() in SERVABLE_SUFFIXES",
        "    return full_path.suffix in SERVABLE_SUFFIXES",
    ),
    (
        "a path outside the web root is servable instead of refused",
        "app/preview.py",
        "        # Outside the web root entirely. StaticFiles refuses this already; a\n"
        "        # second opinion costs nothing and the failure mode is a leak.\n"
        "        return False",
        "        return True",
    ),
    (
        "the regular-file guard is dropped, so a directory is allowlisted too",
        "app/preview.py",
        "        if stat_result is None or not stat.S_ISREG(stat_result.st_mode):",
        "        if stat_result is None:",
    ),
    (
        "the mount goes back to a plain StaticFiles",
        "app/preview.py",
        "        mount = AllowlistedFiles(directory=str(directory), html=True)",
        "        mount = StaticFiles(directory=str(directory), html=True)",
    ),
    (
        "the guard sits in the GET branch instead, so HEAD walks past it",
        "app/preview.py",
        "    def lookup_path(self, path: str) -> tuple[str, Optional[os.stat_result]]:\n"
        "        full_path, stat_result = super().lookup_path(path)\n"
        "        if stat_result is None or not stat.S_ISREG(stat_result.st_mode):\n"
        "            # A directory still has to pass, or html-mode never gets the chance\n"
        "            # to resolve it to its index.html.\n"
        "            return full_path, stat_result\n"
        "        if is_servable(Path(full_path), self._allow_root):\n"
        "            return full_path, stat_result\n"
        '        log.info("preview refused %s (not a servable asset)", full_path)\n'
        '        return "", None',
        "    async def get_response(self, path, scope):\n"
        '        if scope.get("method") == "GET":\n'
        "            full_path, stat_result = StaticFiles.lookup_path(self, path)\n"
        "            if stat_result is not None and stat.S_ISREG(stat_result.st_mode):\n"
        "                if not is_servable(Path(full_path), self._allow_root):\n"
        "                    from starlette.exceptions import HTTPException\n"
        "                    raise HTTPException(status_code=404)\n"
        "        return await super().get_response(path, scope)",
    ),
    (
        "the check reads the request string instead of the resolved path",
        "app/preview.py",
        "        super().__init__(directory=directory, **kwargs)",
        "        StaticFiles.__init__(self, directory=directory, follow_symlink=True, **kwargs)",
    ),
    (
        "a private key suffix is on the allowlist",
        "app/preview.py",
        '    ".woff2", ".woff", ".ttf", ".otf",',
        '    ".woff2", ".woff", ".ttf", ".otf", ".pem",',
    ),
    (
        "a refusal announces itself, turning the 404 into an existence oracle",
        "app/preview.py",
        '        log.info("preview refused %s (not a servable asset)", full_path)\n'
        '        return "", None',
        '        log.info("preview refused %s (not a servable asset)", full_path)\n'
        "        from starlette.exceptions import HTTPException\n"
        '        raise HTTPException(status_code=403, detail="refused")',
    ),
    (
        "(control) a comment-only edit changes nothing",
        "app/preview.py",
        "# What a page in a preview is allowed to ask for:",
        "# What a page in a preview may ask for:",
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
    root = Path(tempfile.mkdtemp(prefix="sweep-preview-"))
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

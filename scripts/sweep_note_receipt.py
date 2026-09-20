#!/usr/bin/env python3
"""Delete-the-fix sweep for the line a posted note answers with.

The whole value of that line is that it is TRUE. A receipt that always reads
"a run is queued" is worse than no receipt: the silence it replaces at least
told an agent nothing rather than something wrong. So most of these mutations
flatten one branch into another - a refused parallel run reported as started,
a note chained onto a transcription reported as already running, an empty
note reported as filed - and the rest attack the plumbing that decides whether
the body reaches the socket at all.

Follows docs/verifying-with-mutations.md: refuses a dirty tree, restores
however it dies, prints `SWEEP COMPLETE`, and counts a skipped mutation as a
broken sweep rather than a lower score.
"""
from __future__ import annotations

import atexit
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RC = ROOT / "app" / "receipt.py"
MN = ROOT / "app" / "main.py"
SUITE = ["tests/test_note_receipt.py"]

ORIGINAL: dict[Path, str] = {}


def restore_all() -> None:
    for path, text in ORIGINAL.items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"  restored {path.name}", flush=True)


# (file, find, replace, label)
MUTATIONS = [
    # --- the body has to actually leave the process --------------------------
    (RC,
     '        self.headers["content-length"] = str(len(self.body))',
     '        pass',
     "the length still says 0, so the line is sent as an empty entity"),

    (RC,
     '        self.body = (message.strip() + "\\n").encode("utf-8")',
     '        self.body = b""',
     "the redirect goes back to carrying nothing"),

    (RC,
     '        self.headers["content-type"] = "text/plain; charset=utf-8"',
     '        pass',
     "the line is served as whatever a redirect defaults to"),

    (RC,
     "    def __init__(self, url: str, message: str, status_code: int = 303) -> None:",
     "    def __init__(self, url: str, message: str, status_code: int = 200) -> None:",
     "a 200 instead of a 303, so the browser never leaves the POST"),

    (RC,
     '        self.body = (message.strip() + "\\n").encode("utf-8")',
     '        self.body = (message + "\\n").encode("utf-8")',
     "the line is not trimmed, so it can arrive with blank lines around it"),

    # --- the line must not claim a run that did not start --------------------
    (RC,
     '''    if then == "run":
        tail = f"a run is queued{later}"''',
     '''    if then == "run":
        tail = "a run is queued"''',
     "a run chained onto a transcription is reported as already queued"),

    (RC,
     '''        elif ran:
            tail = "a parallel run started"
        else:
            tail = "no parallel run started - the project journal says why"''',
     '''        else:
            tail = "a parallel run started"''',
     "a parallel run the portal refused is reported as started"),

    (RC,
     '        tail = "a run is queued" if ran else "the agent reads it on its next run"',
     '        tail = "a run is queued"',
     "a plain note that woke nothing is reported as having queued a run"),

    (RC,
     '        tail = "a run is queued" if ran else "the agent reads it on its next run"',
     '        tail = "the agent reads it on its next run"',
     "a plain note that DID start a run is reported as waiting"),

    (RC,
     '''    elif then == "queue":
        tail = "it waits for the next run"''',
     '''    elif then == "queue":
        tail = "a run is queued"''',
     "an explicitly queued note is reported as running now"),

    (RC,
     '''    elif then == "hear":
        tail = "it is marked for delivery mid-run"''',
     '''    elif then == "hear":
        tail = "it waits for the next run"''',
     "a mid-run delivery is reported as an ordinary queued note"),

    # --- filed or not is the distinction the probes were looking for ---------
    (RC,
     '''    if filed:
        head = f"note filed on {slug}{_files(files)}{_rejected(rejected)}"
    else:
        head = "nothing filed (the note was empty)"''',
     '''    head = f"note filed on {slug}{_files(files)}{_rejected(rejected)}"''',
     "an empty note reads as filed, which is the no-op the probes feared"),

    (RC,
     '''    elif not filed:
        tail = "nothing was started"''',
     '''    elif False:
        tail = "nothing was started"''',
     "an empty note that started nothing claims the agent will read it"),

    (RC,
     '        head = f"note filed on {slug}{_files(files)}{_rejected(rejected)}"',
     '        head = "note filed"',
     "the line does not say which project it was filed on"),

    # --- the counts ----------------------------------------------------------
    (RC,
     '''    if count <= 0:
        return ""
    return f" with {count} file" + ("" if count == 1 else "s")''',
     '''    return f" with {count} file" + ("" if count == 1 else "s")''',
     '"with 0 files" on every note that carried none'),

    (RC,
     '    return f" with {count} file" + ("" if count == 1 else "s")',
     '    return f" with {count} files"',
     "one attachment is reported as \"1 files\""),

    (RC,
     '''def _rejected(count: int) -> str:
    if count <= 0:
        return ""''',
     '''def _rejected(count: int) -> str:
    if count < 0:
        return ""''',
     '"(0 files rejected)" on every note where nothing was'),

    # --- main.py: where `ran` comes from -------------------------------------
    (MN,
     "            then=then,\n            ran=ran,",
     '''            then="",
            ran=ran,''',
     "the line describes a different button than the one pressed"),

    (MN,
     '            ran = await _start_parallel(project)\n    elif then != "queue":',
     '            await _start_parallel(project)\n    elif then != "queue":',
     "a started parallel run is reported as refused"),

    (MN,
     "            receipt.note_line(slug, filed=False, then=then, ran=ran),",
     '            receipt.note_line(slug, filed=False, then="", ran=ran),',
     "an empty post describes a different button than the one pressed"),

    (MN,
     '            ran = await worker.note_arrived(project)',
     '            await worker.note_arrived(project)',
     "a plain note that woke a run is reported as waiting for one"),

    (MN,
     '    return bool(started)',
     '    return True',
     "_start_parallel says it started whatever the worker answered"),

    (MN,
     '''            transcribing=bool(audio_ids),''',
     '''            transcribing=False,''',
     "a voice memo's deferred run is reported as already started"),

    (MN,
     '''            files=len(stored),
            rejected=len(errors),''',
     '''            files=0,
            rejected=len(errors),''',
     "attachments are not counted in the line"),

    (MN,
     '''        return receipt.Receipt(
            f"/project/{slug}",
            receipt.note_line(slug, filed=False, then=then, ran=ran),
        )''',
     '''        return RedirectResponse(url=f"/project/{slug}", status_code=303)''',
     "the empty-note path goes back to answering with silence"),

    (MN,
     '''    return receipt.Receipt(
        f"/project/{slug}",
        receipt.note_line(''',
     '''    return receipt.Receipt(
        f"/project/{slug}",
        _unused_note_line(''',
     "(control) a name that does not exist must go red"),

    (MN,
     '    ran = False\n    if then == "run":',
     '    ran = False  # noqa\n    if then == "run":',
     "(control) a comment-only edit must NOT go red"),
]


def anchors() -> list[tuple[Path, str]]:
    """Every (file, exact string) this sweep mutates, for tests/test_sweep_anchors.py."""
    return [(path, find) for path, find, _repl, _label in MUTATIONS]


def run_suite() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [str(ROOT / "venv" / "bin" / "python"), "-m", "pytest", *SUITE, "-q",
         "--no-header", "--tb=no", "-p", "no:warnings", "-p", "no:randomly"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=240,
    )
    failures = [
        line.split("::")[-1].split()[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED")
    ]
    return proc.returncode, failures


ONLY = sys.argv[1] if len(sys.argv) > 1 else ""


def main() -> int:
    if subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT).returncode != 0:
        sys.exit("REFUSING: tree is dirty. A sweep must start from a committed tree.")
    for path in {p for p, _f, _r, _l in MUTATIONS}:
        ORIGINAL[path] = path.read_text(encoding="utf-8")
    atexit.register(restore_all)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit("killed by signal"))

    caught = 0
    ran = 0
    escaped: list[str] = []
    skipped: list[str] = []
    for i, (path, find, replace, label) in enumerate(MUTATIONS, 1):
        if ONLY and ONLY not in label:
            continue
        text = ORIGINAL[path]
        if text.count(find) != 1:
            print(f"{i:2}. SKIP (anchor occurs {text.count(find)}x) - {label}", flush=True)
            skipped.append(label)
            continue
        ran += 1
        path.write_text(text.replace(find, replace, 1), encoding="utf-8")
        rc, failures = run_suite()
        restore_all()
        control = label.startswith("(control) a comment-only")
        if (rc == 0) != control:
            print(f"{i:2}. ESCAPED  - {label}", flush=True)
            escaped.append(label)
        else:
            caught += 1
            print(f"{i:2}. {'held    ' if control else 'caught  '} - {label}", flush=True)
            if failures:
                print(f"      by {', '.join(sorted(set(failures))[:3])}", flush=True)

    print(f"\n{caught}/{ran} caught, {len(escaped)} escaped, {len(skipped)} skipped", flush=True)
    for label in skipped:
        print(f"  skipped (anchor no longer holds): {label}", flush=True)
    print("SWEEP COMPLETE", flush=True)
    return 1 if skipped or escaped else 0


if __name__ == "__main__":
    sys.exit(main())

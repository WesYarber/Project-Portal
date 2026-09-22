#!/usr/bin/env python3
"""Delete-the-fix sweep for the ntfy publish credential (the `ntfy_token` setting).

Every mutation here weakens one decision in the path a notification takes to a
logged-in ntfy server: whether the setting is read at all, whether it reaches
the sender, and - the one that matters most - what happens when it is empty. A
bare `Bearer ` is not a smaller version of a credential; ntfy reads it as a
failed login and answers 401, so the "no token" case must send no header at
all. That distinction is invisible to a status-code check, which is precisely
why it gets its own mutations here.

Follows docs/verifying-with-mutations.md: refuses a dirty tree, restores however
it dies, prints `SWEEP COMPLETE`, and counts a skipped mutation as a broken
sweep rather than a lower score.
"""
from __future__ import annotations

import atexit
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "deploy"))
import sweeplib  # noqa: E402  (a sibling script, not on the path)

NF = ROOT / "app" / "notify.py"
CF = ROOT / "app" / "config.py"
SF = ROOT / "app" / "settings_form.py"
TP = ROOT / "app" / "templates" / "settings.html"
SUITE = ["tests/test_ntfy_auth.py"]

ORIGINAL = sweeplib.Originals()


def restore_all() -> None:
    sweeplib.restore(ORIGINAL)


# (file, find, replace, label)
MUTATIONS = [
    # --- the empty case, which is the whole point ----------------------------
    (NF,
     "        if token.strip():\n"
     '            headers["Authorization"] = f"Bearer {token.strip()}"',
     "        if True:\n"
     '            headers["Authorization"] = f"Bearer {token.strip()}"',
     'an install with no credential sends a bare "Bearer " and is told 401'),

    (NF,
     "        if token.strip():\n"
     '            headers["Authorization"] = f"Bearer {token.strip()}"',
     "        if token is not None:\n"
     '            headers["Authorization"] = f"Bearer {token.strip()}"',
     "the same, written as a guard that looks like one and rejects nothing"),

    (NF,
     "        if token.strip():",
     "        if token:",
     "a token of whitespace becomes a header, which ntfy refuses"),

    # --- the header itself ---------------------------------------------------
    (NF,
     "        if token.strip():\n"
     '            headers["Authorization"] = f"Bearer {token.strip()}"',
     "        if False:\n"
     '            headers["Authorization"] = f"Bearer {token.strip()}"',
     "the credential is never sent, so a closed server refuses every publish"),

    (NF,
     '            headers["Authorization"] = f"Bearer {token.strip()}"',
     '            headers["Authorization"] = token.strip()',
     "the scheme is dropped, which ntfy reads as a malformed header"),

    (NF,
     '            headers["Authorization"] = f"Bearer {token.strip()}"',
     '            headers["Authorization"] = f"Bearer {token}"',
     "a token pasted with its trailing newline is sent verbatim"),

    (NF,
     '            headers["Authorization"] = f"Bearer {token.strip()}"',
     '            headers["Authentication"] = f"Bearer {token.strip()}"',
     "the header is spelled wrong, so the credential rides along unread"),

    # --- the wiring ----------------------------------------------------------
    (NF,
     '    ntfy_token = settings.get("ntfy_token", "")',
     '    ntfy_token = ""',
     "the setting is never read, so what he typed on the form does nothing"),

    (NF,
     '    ntfy_token = settings.get("ntfy_token", "")',
     '    ntfy_token = settings.get("ntfy_url", "")',
     "the wrong setting is read, so the server URL is sent as the credential"),

    (NF,
     "        await _send_ntfy(ntfy_url, topic, title, text, ntfy_token)",
     "        await _send_ntfy(ntfy_url, topic, title, text)",
     "the token is looked up and then not handed to the sender"),

    (NF,
     "    ntfy_url: str, topic: str, title: str, message: str, token: str = \"\"",
     "    ntfy_url: str, topic: str, title: str, message: str, token: str = \"Bearer\"",
     "the optional parameter defaults to a value instead of to no credential"),

    # --- the send is otherwise unchanged -------------------------------------
    (NF,
     "            resp = await client.post(url, content=message.encode(\"utf-8\"), headers=headers)",
     "            resp = await client.post(url, content=message.encode(\"utf-8\"))",
     "headers stop being sent at all, taking the Title with them"),

    (NF,
     "    if not ntfy_url or not topic:\n        return",
     "    if not ntfy_url:\n        return",
     "a blank topic is published to, which posts to the server's root"),

    # --- the setting ---------------------------------------------------------
    (CF,
     '    "ntfy_token": "",',
     '    "ntfy_token": "changeme",',
     "a fresh install ships a credential nobody set"),

    (SF,
     '        Field("ntfy_token", _text),',
     '        Field("ntfy_token", lambda value: value),',
     "the form stops trimming, so a pasted newline is stored"),

    (SF,
     '        Field("ntfy_token", _text),',
     '        Field("ntfy_topic_token", _text),',
     "the field is not registered, so saving it is dropped at a silent 303"),

    # --- the form ------------------------------------------------------------
    (TP,
     "telegram_chat_id,ntfy_url,ntfy_topic,ntfy_token",
     "telegram_chat_id,ntfy_url,ntfy_topic",
     "the section does not declare the field, so the save writes nothing"),

    (TP,
     'id="ntfy_token" name="ntfy_token"',
     'id="ntfy_token" name="ntfy_tokens"',
     "the input posts under a name the registry does not know"),

    (TP,
     '<input type="text" id="ntfy_token" name="ntfy_token" value="{{ settings.ntfy_token }}"',
     '<input type="text" id="ntfy_token" name="ntfy_token" value=""',
     "the saved credential is not shown back, so a re-save blanks it"),

    (NF,
     "    ntfy_url = settings.get(\"ntfy_url\", \"\")\n    ntfy_token",
     "    ntfy_url = settings.get(\"ntfy_url\", \"\")  # noqa\n    ntfy_token",
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


# A mutation whose label contains this substring is the only one run. Lets one
# re-check take twenty seconds instead of the whole sweep.
ONLY = sys.argv[1] if len(sys.argv) > 1 else ""


def main() -> int:
    if subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT).returncode != 0:
        sys.exit("REFUSING: tree is dirty. A sweep must start from a committed tree.")
    for path in {p for p, _f, _r, _l in MUTATIONS}:
        ORIGINAL.remember(path)
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
        control = label.startswith("(control)")
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

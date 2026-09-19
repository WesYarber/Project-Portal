#!/usr/bin/env python3
"""Delete-the-fix sweep for the shared-skill address watch (app/addresswatch.py).

Every mutation here weakens one decision the watch makes: what it calls an
address, what it calls a host it can speak for, what it calls answering, and
what it does about a dead one. If the suite still passes with the decision
reversed, the test that looked like it owned that line does not.

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
AW = ROOT / "app" / "addresswatch.py"
WK = ROOT / "app" / "worker.py"
SUITE = ["tests/test_addresswatch.py"]

ORIGINAL: dict[Path, str] = {}


def restore_all() -> None:
    for path, text in ORIGINAL.items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"  restored {path.name}", flush=True)


# (file, find, replace, label)
MUTATIONS = [
    # --- which hosts the portal can speak for --------------------------------
    (AW,
     r'        return bool(re.fullmatch(r"[a-z][a-z0-9\-]*", host))',
     r'        return bool(re.fullmatch(r"[a-z][a-z0-9.\-]*", host))',
     "a public hostname is probed, and a CDN answers 200 to every path"),

    (AW,
     '        if host in site.LOOPBACK_HOSTS:\n            return False',
     '        if False:\n            return False',
     "`localhost:8531` is treated as a service this portal can vouch for"),

    (AW,
     "    if ip.is_loopback:\n        return False",
     "    if False:\n        return False",
     "127.0.0.1 inside an ssh-tunnel snippet is probed as if it were local"),

    (AW,
     "    if ip.version == 4 and ip in TAILNET:\n        return True",
     "    if False:\n        return True",
     "a tailnet address is dropped, because is_private calls 100.64/10 public"),

    (AW,
     "    return bool(ip.is_private)",
     "    return True",
     "a public IPv4 literal is probed"),

    (AW,
     r'        return bool(re.fullmatch(r"[a-z][a-z0-9\-]*", host))',
     "        return True",
     "any token is a host, so the `9334` of an ssh port forward gets probed"),

    # --- which text shapes are addresses -------------------------------------
    (AW,
     "        for host, port_text in BARE_RE.findall(span):\n            keep(host, port_text, bare=True)",
     "        for host, port_text in BARE_RE.findall(span):\n            keep(host, port_text)",
     "the bare shape loses its port floor, so an A1:1 aspect ratio is an address"),

    (AW,
     "        if bare and port < 1024:\n            return",
     "        if bare and port < 0:\n            return",
     "same floor, moved rather than deleted"),

    (AW,
     "        if not (0 < port < 65536) or not is_watchable_host(host):",
     "        if not is_watchable_host(host):",
     "a port outside the range is accepted"),

    (AW,
     "    for span in CODE_SPAN_RE.findall(text or \"\"):",
     "    for span in [text or \"\"]:",
     "prose is scanned, so 'Note:8080 items' is an address"),

    (AW,
     '    for host, port_text in URL_RE.findall(text or ""):\n        keep(host, port_text)',
     '    for host, port_text in []:\n        keep(host, port_text)',
     "URLs stop being read, which is how five of seven real addresses are written"),

    # --- the scan ------------------------------------------------------------
    (AW,
     "            text = Template(raw).safe_substitute(**config.SITE.template_vars())",
     "            text = raw",
     "$HOST is left unsubstituted, so a skill that ships with it is never checked"),

    (AW,
     "                if address.text in skip:\n                    ignored.append(address)\n                    continue",
     "                if False:\n                    ignored.append(address)\n                    continue",
     "a declared placeholder is probed anyway, and files a todo every day"),

    (AW,
     "            skip = ignored_addresses(text)",
     "            skip = set()",
     "the opt-out is never read at all"),

    (AW,
     "    seen: set[tuple[str, int]] = set()\n    for root in",
     "    seen: set[tuple[str, int]] = set()  # noqa\n    for root in",
     "(control) a comment-only edit must NOT go red"),

    (AW,
     "                if (host, port) in seen:\n                    continue\n                seen.add((host, port))",
     "                seen.add((host, port))",
     "one address named by two skills is probed and filed twice"),

    (AW,
     '            files = sorted(p for p in root.rglob("*.md") if p.is_file())',
     '            files = sorted(p for p in root.glob("SKILL.md") if p.is_file())',
     "only a top-level SKILL.md is read, so a reference file beside it is missed"),

    (AW,
     "            except (OSError, UnicodeDecodeError):\n                continue",
     "            except OSError:\n                continue",
     "one skill that is not utf-8 takes the whole sweep down with it"),

    # --- the probe -----------------------------------------------------------
    (AW,
     "    except (OSError, ValueError):\n        return False",
     "    except (OSError, ValueError):\n        return True",
     "a refused connection counts as answering, so nothing is ever found"),

    (AW,
     "        (alive if answers(address.host, address.port) else dead).append(address)",
     "        (dead if answers(address.host, address.port) else alive).append(address)",
     "alive and dead are swapped"),

    # --- what it does about a dead address -----------------------------------
    (AW,
     '    return row["text"] not in before',
     "    return True",
     "the same dead address files a fresh todo every morning"),

    (AW,
     '    return row["text"] not in before',
     "    return False",
     "a genuinely new dead address is never reported as filed"),

    (AW,
     '    row = db.add_todo(project["id"], todo_text(address), owner="agent", tags=["cleanup"])',
     '    row = db.add_todo(project["id"], todo_text(address), owner="user", tags=["cleanup"])',
     "the todo lands on a person rather than on the agent that can fix it"),

    (AW,
     "    for address in result[\"dead\"]:",
     "    for address in result[\"alive\"]:",
     "todos are filed for the addresses that DO answer"),

    (AW,
     "    if not enabled():",
     "    if False:",
     "the off switch does nothing"),

    (AW,
     '    return (db.get_setting(SETTING_ENABLED) or "1") == "1"',
     '    return db.get_setting(SETTING_ENABLED) == "1"',
     "the watch is off until somebody saves the settings form"),

    (AW,
     "    except Exception:  # noqa: BLE001\n        log.exception(\"Address watch failed\")",
     "    except ValueError:\n        log.exception(\"Address watch failed\")",
     "a thrown check escapes run_check and stops the worker tick"),

    # --- the record ----------------------------------------------------------
    (AW,
     '    if not isinstance(blob, dict):',
     '    if False:',
     "a garbled record is handed to the template as a list"),

    # --- the daily wiring ----------------------------------------------------
    (WK,
     "    global _address_checked_day\n"
     "    today = datetime.now(timezone.utc).date().isoformat()\n"
     "    if _address_checked_day == today:\n"
     "        return\n"
     "    _address_checked_day = today",
     "    global _address_checked_day\n"
     "    today = datetime.now(timezone.utc).date().isoformat()\n"
     "    _address_checked_day = today",
     "the sweep runs on every worker tick rather than once a day"),

    (WK,
     "    _address_checked_day = today\n    try:\n        result = await addresswatch.run_check()",
     "    try:\n        result = await addresswatch.run_check()\n        _address_checked_day = today",
     "a failing check retries every 60 seconds instead of waiting for tomorrow"),

    (WK,
     "    await _daily_model_check()\n    await _daily_address_check()",
     "    await _daily_model_check()",
     "the check is never called from the tick at all"),

    (AW,
     '    if not (0 < port < 65536) or not is_watchable_host(host):',
     '    if not (0 < port < 65536):',
     "the host filter goes, so every `a:1234` in a code span is probed"),
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

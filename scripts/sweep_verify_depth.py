#!/usr/bin/env python3
"""Delete-the-fix sweep for the verification-depth section.

Follows all three safety rules in docs/verifying-with-mutations.md:

1. REFUSES a dirty tree, and restores however it dies (atexit + SIGTERM/SIGINT
   that call sys.exit, since a bare handler that returns does not run atexit).
2. Prints `SWEEP COMPLETE` at the end; nothing may read app/ until it appears.
3. The caller re-runs the plain suite afterwards.

Two files are mutated, because the feature is split across two: the policy
lives in app/verifydepth.py and its *placement* - above the journal it exists to
outrank - lives in app/agent_runner.py. A sweep that only touched the first
would leave the load-bearing half unproven.

Scoped to the owning test files rather than the full suite, per the trade
commit b1dcd35 made: a CAUGHT here is conclusive, while an ESCAPED only means
these files do not hold the line and has to be re-checked more widely.
"""
import atexit, signal, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VD = ROOT / "app" / "verifydepth.py"
AR = ROOT / "app" / "agent_runner.py"
CF = ROOT / "app" / "config.py"
TP = ROOT / "app" / "templates" / "settings.html"
TESTS = ["tests/test_verify_depth.py", "tests/test_settings_form.py"]

if subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT).returncode != 0:
    sys.exit("REFUSING: tree is dirty. A sweep must start from a committed tree.")

ORIGINAL = {p: p.read_text(encoding="utf-8") for p in (VD, AR, CF, TP)}


def restore():
    for path, text in ORIGINAL.items():
        path.write_text(text, encoding="utf-8")


atexit.register(restore)
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_: sys.exit("killed by signal"))

# (path, label, find, replace). Each is one decision the code makes.
MUTATIONS = [
    # --- app/verifydepth.py: the policy ---
    (VD, "the default becomes the old always-sweep behavior",
     'DEFAULT_DEPTH = "proportionate"',
     'DEFAULT_DEPTH = "thorough"'),
    (VD, "an unknown setting value is used verbatim instead of falling back",
     "    return value if value in DEPTH_CHOICES else DEFAULT_DEPTH",
     "    return value or DEFAULT_DEPTH"),
    (VD, "a broken settings read propagates instead of failing open",
     "    except Exception:  # pragma: no cover - defensive",
     "    except ZeroDivisionError:"),
    (VD, "the caller's explicit depth is ignored",
     "    chosen = depth if depth in DEPTH_CHOICES else current_depth()",
     "    chosen = current_depth()"),
    (VD, "an unknown explicit depth raises instead of falling back",
     "    chosen = depth if depth in DEPTH_CHOICES else current_depth()",
     "    chosen = depth or current_depth()"),
    (VD, "every task gets the section, code or not",
     "    if task not in CODE_TASKS:\n        return \"\"",
     "    if False:\n        return \"\""),
    (VD, "a plan run counts as a code task",
     'CODE_TASKS: frozenset[str] = frozenset({"build"})',
     'CODE_TASKS: frozenset[str] = frozenset({"build", "plan"})'),
    (VD, "the light depth loses its ban on sweeping",
     '    "mutation sweep at all.\\n\\n"',
     '    "mutation sweep unless you feel like one.\\n\\n"'),
    (VD, "the invariants are dropped from the proportionate text",
     '    f"{_INVARIANTS}\\n\\n"\n    f"{_NOT_THE_JOURNAL}"\n)\n\n_THOROUGH',
     '    f"{_NOT_THE_JOURNAL}"\n)\n\n_THOROUGH'),
    (VD, "the invariants are dropped from the thorough text",
     '    f"{_INVARIANTS}"\n)\n\n_LIGHT',
     '    ""\n)\n\n_LIGHT'),
    (VD, "the do-not-copy-the-journal sentence is dropped from light",
     '    f"{_INVARIANTS}\\n\\n"\n    f"{_NOT_THE_JOURNAL}"\n)\n\n_BODIES',
     '    f"{_INVARIANTS}"\n)\n\n_BODIES'),
    (VD, "a choice loses its dropdown label",
     '    ("light", "Only the tests it touches"),\n',
     ""),
    # --- app/agent_runner.py: the placement ---
    (AR, "the section is never added to a build prompt",
     "    if depth_txt:\n        parts.append(depth_txt)",
     "    if False:\n        parts.append(depth_txt)"),
    (AR, "the section lands below the journal it has to outrank",
     "    if depth_txt:\n        parts.append(depth_txt)\n\n    parts.append(_project_section(project, tvars))",
     "    parts.append(_project_section(project, tvars))"),
    # --- app/config.py: the seeded row ---
    (CF, "the seeded default row disagrees with the module's",
     '    "verification_depth": "proportionate",',
     '    "verification_depth": "light",'),
    # --- app/templates/settings.html: the control ---
    (TP, "the field is dropped from _fields, so the dropdown silently never saves",
     "require_build_approval,verification_depth,",
     "require_build_approval,"),
    (TP, "the dropdown forgets what is currently set",
     "{{ 'selected' if value == (settings.verification_depth or 'proportionate') else '' }}",
     ""),
]

caught = escaped = skipped = 0
for path, label, find, repl in MUTATIONS:
    text = ORIGINAL[path]
    if find not in text:
        print(f"SKIP (pattern missing): {label}", flush=True)
        skipped += 1
        continue
    path.write_text(text.replace(find, repl, 1), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-qx", "--no-header", "-p", "no:randomly"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f"ESCAPED: {label}", flush=True)
        escaped += 1
    else:
        print(f"caught:  {label}", flush=True)
        caught += 1
    restore()

print(f"\n{caught} caught, {escaped} escaped, {skipped} skipped, of {len(MUTATIONS)}")
print("SWEEP COMPLETE", flush=True)

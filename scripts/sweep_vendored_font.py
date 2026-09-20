#!/usr/bin/env python3
"""Delete-the-fix sweep for the self-hosted webfont.

Two kinds of defect are being guarded here and they fail very differently.

The **privacy** half - a `<link>` or a `preconnect` to a Google font host
coming back into a template, the CSS header comment or the skill - is invisible
from inside the page. It renders identically, it does not slow anything
perceptibly down on a fast connection, and the only symptom is that a third
party is told the IP and exact URL of every visitor. Nothing but a test that
greps for the hostname will ever notice. Half the mutations below are exactly
that line coming back, in each of the four places it used to live.

The **caching** half is the reverse: a mutation that makes `is_version_pinned`
say yes too often freezes a mutable file in every visitor's browser for a year,
with no way to recall it. `vendor/fira-code` and `vendor/fira-code-v27-scratch`
are the cases that matter, because they read as fine.

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
VS = ROOT / "app" / "vendorstatic.py"
MAIN = ROOT / "app" / "main.py"
BASE = ROOT / "app" / "templates" / "base.html"
GUIDE = ROOT / "app" / "templates" / "style_guide.html"
THEME = ROOT / "app" / "static" / "terminal-theme.css"
SKILL = ROOT / "app" / "skills" / "terminal-style" / "SKILL.md"

SUITE = ["tests/test_webfont.py", "tests/test_style_template.py"]

GOOGLE_LINK = (
    '<link href="https://fonts.googleapis.com/css2?family=Fira+Code'
    ':wght@400;500;600;700&display=swap" rel="stylesheet">'
)

ORIGINAL: dict[Path, str] = {}


def restore_all() -> None:
    for path, text in ORIGINAL.items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"  restored {path.name}", flush=True)


# (file, find, replace, label)
MUTATIONS = [
    # --- the Google link comes back, in each place it used to live -----------
    (BASE,
     '<link rel="stylesheet" href="/static/vendor/fira-code-v27/font.css">',
     GOOGLE_LINK,
     "base.html asks Google for the font again, so every portal page does"),

    (BASE,
     '<link rel="stylesheet" href="/static/vendor/fira-code-v27/font.css">',
     '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
     '<link rel="stylesheet" href="/static/vendor/fira-code-v27/font.css">',
     "only the preconnect comes back - DNS, TCP and TLS to Google, no font"),

    (GUIDE,
     '<link rel="stylesheet" href="/static/vendor/fira-code-v27/font.css">',
     GOOGLE_LINK,
     "the /style gallery asks Google for the font again"),

    (GUIDE,
     '  &lt;link rel="stylesheet" href="vendor/fira-code-v27/font.css"&gt;',
     '  &lt;link href="https://fonts.googleapis.com/css2?family=Fira+Code'
     ':wght@400;500;600;700&amp;display=swap" rel="stylesheet"&gt;',
     "the starter skeleton hands the Google link to every project that copies it"),

    (THEME,
     '     <link rel="stylesheet" href="vendor/fira-code-v27/font.css">',
     f"     {GOOGLE_LINK}",
     "the theme file's header comment tells a project to link Google"),

    (SKILL,
     '<link rel="stylesheet" href="vendor/fira-code-v27/font.css">',
     GOOGLE_LINK,
     "the skill 51 projects read tells them to link Google"),

    (SKILL,
     "**Never link `fonts.googleapis.com` for this,",
     "**Prefer not to link `fonts.googleapis.com` for this,",
     "the skill's prohibition softens into a preference"),

    (SKILL,
     "cp -r $PORTAL_ROOT/app/static/vendor/fira-code-v27 vendor/",
     "cp -r $PORTAL_ROOT/app/static/fira-code-v27 vendor/",
     "the skill's copy recipe names a path that does not exist"),

    # --- the font itself is still whole and still reachable ------------------
    (BASE,
     'href="/static/vendor/fira-code-v27/font.css"',
     'href="/static/vendor/fira-code-v26/font.css"',
     "the font link points at a directory that is not there, so every page 404s it"),

    (BASE,
     '<link rel="stylesheet" href="/static/vendor/fira-code-v27/font.css">',
     '<link rel="stylesheet" href="{{ static_url(\'vendor/fira-code-v27/font.css\') }}">',
     "?v=<mtime> is back on the URL, so a redeploy re-fetches a year-long asset"),

    # --- is_version_pinned: saying yes too often is the expensive direction ---
    (VS,
     "    if len(parts) < 3 or parts[0] != VENDOR_DIR:\n        return False\n"
     "    return bool(_VERSION_PINNED.search(parts[1]))",
     "    return True",
     "everything under /static is frozen for a year, the portal's own CSS included"),

    (VS,
     "    if len(parts) < 3 or parts[0] != VENDOR_DIR:\n        return False",
     "    if len(parts) < 3:\n        return False",
     "any two-deep path is treated as vendored, not just vendor/'s children"),

    (VS,
     "    if len(parts) < 3 or parts[0] != VENDOR_DIR:\n        return False",
     "    if len(parts) < 2 or parts[0] != VENDOR_DIR:\n        return False",
     "a bare file in vendor/ is frozen with no version in any name to pin it"),

    (VS,
     "    return bool(_VERSION_PINNED.search(parts[1]))",
     "    return bool(_VERSION_PINNED.search(parts[-1]))",
     "the FILE name is checked for a version instead of the directory's"),

    (VS,
     r'_VERSION_PINNED = re.compile(r"-v\d+(\.\d+)*$")',
     r'_VERSION_PINNED = re.compile(r"-v\d+(\.\d+)*")',
     "the end anchor goes, so vendor/thing-v27-scratch/ is frozen for a year"),

    (VS,
     r'_VERSION_PINNED = re.compile(r"-v\d+(\.\d+)*$")',
     r'_VERSION_PINNED = re.compile(r"-v.*$")',
     "'-vnext' counts as a version, so an unreleased copy is frozen"),

    (VS,
     r'_VERSION_PINNED = re.compile(r"-v\d+(\.\d+)*$")',
     r'_VERSION_PINNED = re.compile(r"-v\d+$")',
     "a dotted version (pdfjs-v4.10.38) stops being pinned and revalidates forever"),

    # --- the header actually reaching the response ---------------------------
    (VS,
     "        if is_version_pinned(self.get_path(scope)):\n"
     '            response.headers["cache-control"] = IMMUTABLE_CACHE_CONTROL',
     "        pass",
     "the header is never set, so every page load revalidates the font"),

    (VS,
     "        if is_version_pinned(self.get_path(scope)):",
     "        if not is_version_pinned(self.get_path(scope)):",
     "the test is inverted: the font revalidates and style.css freezes"),

    (VS,
     'IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"',
     'IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000"',
     "'immutable' is dropped, so a reload still sends a conditional request"),

    (VS,
     'IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"',
     'IMMUTABLE_CACHE_CONTROL = "public, max-age=3600, immutable"',
     "an hour instead of a year, which is most of the benefit gone"),

    (VS,
     'VENDOR_DIR = "vendor"',
     'VENDOR_DIR = "vendored"',
     "the directory the rule applies to is not the one that exists"),

    # --- the mount, which is what puts any of this in front of a browser -----
    (MAIN,
     "    vendorstatic.VersionedStatic(directory=str(config.BASE_DIR / \"app\" / \"static\")),",
     "    StaticFiles(directory=str(config.BASE_DIR / \"app\" / \"static\")),",
     "the plain StaticFiles is mounted again, so no vendor asset is ever frozen"),

    (VS,
     "from pathlib import PurePath",
     "from pathlib import PurePath  # noqa",
     "(control) a comment-only edit must NOT go red"),
]


def anchors() -> list[tuple[Path, str]]:
    """Every (file, exact string) this sweep mutates, for tests/test_sweep_anchors.py."""
    return [(path, find) for path, find, _repl, _label in MUTATIONS]


def run_suite() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [str(ROOT / "venv" / "bin" / "python"), "-m", "pytest", *SUITE, "-q",
         "--no-header", "--tb=no", "-p", "no:warnings", "-p", "no:randomly"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )
    failures = [
        line.split("::")[-1].split()[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED")
    ]
    return proc.returncode, failures


# A mutation whose label contains this substring is the only one run.
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

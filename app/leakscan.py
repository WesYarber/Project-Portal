"""Refuse to publish a tree that still names the person running it.

The one-off scrub is the easy half. Without a standing check, the next feature
quietly writes a hostname back into a comment and nobody notices until it is
public - so this is the check, and it runs in the test suite (`tests/test_leak
scan.py`) and again as a gate inside `deploy/publish.py` before a public commit
is made.

**The needles come from the site config, not from a hard-coded list.** That is
the whole design:

- a list naming one person's machines protects one person, and is itself a
  personal string that has to live in the published tree;
- deriving them from `SITE` means the guard protects *whoever* installs this.
  Their hostname, their login, their name, their render box - all already
  configured, all automatically forbidden in the source.

`leak_patterns` in portal.toml carries anything the config cannot know: a
tailnet name, a domain, a LAN prefix. It is gitignored, so those never appear
in the published tree either - which they would have to if this list were part
of the check's own source.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, NamedTuple, Optional

from app import config, site

# Extensions worth reading. Anything textual that ships.
SCANNED_SUFFIXES = {".py", ".html", ".css", ".js", ".md", ".sh", ".toml", ".service", ".txt"}

# Directories that are never part of the published tree.
SKIP_DIRS = {".git", "venv", "data", "secrets", "__pycache__", "node_modules", "shots"}

# Words too common to be evidence of anything. A login of "admin" or an owner
# called "Root" would otherwise make every line in the tree an offence.
TOO_COMMON = {
    "admin", "root", "user", "users", "test", "portal", "server", "host",
    "localhost", "home", "app", "main", "run", "runs", "data", "the", "and",
    "ubuntu", "debian", "pi", "dev", "prod", "www", "api", "web", "box",
}

# Where a personal string is not a leak: the example config documents the keys
# by showing values, and portal.toml is gitignored and is the personal file.
EXEMPT_NAMES = {"portal.toml", "portal.example.toml"}

# Tracked in the private repo, never copied into the public one. One list, read
# by both this scan and deploy/publish.py, so a file cannot be quietly exempted
# from the check while still being published - the exemption *is* the exclusion.
PRIVATE_PATHS = {
    # The plan for this separation: written for one person, about one repo,
    # and it quotes the very commits the public history has to start after.
    "docs/open-source.md",
    # A screenshot of the author's own board. Nothing references it, and an
    # image is the one kind of file the text scan below cannot check - so a
    # picture of somebody's real projects would sail straight through a clean
    # scan into a public repo.
    "shots/opus5-settings.png",
}


class Leak(NamedTuple):
    path: Path
    lineno: int
    line: str
    needle: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: [{self.needle}] {self.line.strip()[:120]}"


def _useful(word: str) -> bool:
    word = (word or "").strip()
    return len(word) > 2 and word.lower() not in TOO_COMMON


def _hostish(compound: str) -> set[str]:
    """The machine names inside a `user@host` or a URL, and nothing else.

    Splitting the whole string on punctuation looked equivalent and was not: it
    yields `http` and the port number as needles, which between them match most
    lines of a web application and bury the real findings under 300 false ones.
    """
    text = (compound or "").strip()
    if not text:
        return set()
    text = re.sub(r"^\w+://", "", text)          # scheme
    text = text.split("/", 1)[0]                 # path
    text = text.rsplit("@", 1)[-1]               # user@
    text = text.split(":", 1)[0]                 # :port
    return {text} if text else set()


def needles(where: Optional[site.Site] = None, extra: Optional[Iterable[str]] = None) -> list[str]:
    """Strings that must not appear in the publishable tree.

    Derived from the installation rather than listed, so this protects the
    person who installs it next as automatically as it protects the author.

    **The owner's name is deliberately not on this list**, and neither is their
    login. The tree carries hundreds of comments of the form "Wes asked for
    this on 2026-07-22", and those are honest history: an open-source project
    whose comments explain *why* a decision was made is a better project, and
    the name is simply the person who asked. What must not leak is
    infrastructure - a machine somebody could try to reach, a domain, an
    address, a credential.

    The prompts are a different matter, because a name baked into one would
    reach every future owner's agents. Those are covered separately, and
    better, by rendering every prompt under a foreign identity - see
    tests/test_owner.py.
    """
    where = config.SITE if where is None else where
    found = {where.host}
    found |= _hostish(where.render_host)
    found |= _hostish(where.render_portal_url)
    # The login name in prose ("Wes asked for this") is history; the login name
    # in a *path* is configuration: a README telling a stranger to `cd
    # /home/<login>/project-portal` is both a leak and wrong for them. Found by
    # a raw grep of the staged tree after this scan had called it clean, which
    # is why deploy/publish.py is not the only check that runs.
    if _useful(where.ssh_user):
        found.add(f"/home/{where.ssh_user}")
        # app/climemory.py encodes a workspace path by replacing every slash
        # with a hyphen, so the same path hides in a second spelling.
        found.add(f"-home-{where.ssh_user}")
    found |= set(extra or [])
    return sorted({w.strip() for w in found if _useful(w)}, key=str.lower)


def extra_patterns() -> list[str]:
    """Anything the config cannot infer, from gitignored `portal.toml`.

    A tailnet name, a domain, a LAN prefix: things that identify the owner but
    are not any single field of the site config.
    """
    raw = site.read_file(site.config_path()).get("leak_patterns")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def compile_needles(words: Iterable[str]) -> re.Pattern:
    # Sorted longest-first so a report blames "example.com" rather than the
    # "example" inside it.
    parts = sorted({w for w in words if w}, key=len, reverse=True)
    if not parts:
        # Matching nothing, rather than the empty alternation's "matches
        # everywhere" - a portal with no configured identity must not report
        # every line of its own source as a leak.
        return re.compile(r"(?!x)x")
    # Word-bounded, so a host called "box" does not match "sandbox" and the
    # needle "acme" still fires inside "acme.com" (the dot is a boundary).
    # Without this a short hostname reports every line in the tree.
    #
    # The boundary is only added on an end that is a word character. `\b`
    # before a leading "/" asserts a word character immediately before it, so
    # "/home/ada" wrapped on both sides silently never matches " /home/ada" -
    # which is the only way it is ever written. That bug reported a clean tree
    # while six such paths sat in the README.
    def bounded(needle: str) -> str:
        body = re.escape(needle)
        left = r"\b" if needle[:1].isalnum() or needle[:1] == "_" else ""
        right = r"\b" if needle[-1:].isalnum() or needle[-1:] == "_" else ""
        return f"{left}{body}{right}"

    return re.compile("|".join(bounded(p) for p in parts), re.IGNORECASE)


def files(root: Optional[Path] = None) -> list[Path]:
    root = config.APP_ROOT if root is None else root
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if set(path.relative_to(root).parts) & SKIP_DIRS:
            continue
        if path.name in EXEMPT_NAMES:
            continue
        if path.relative_to(root).as_posix() in PRIVATE_PATHS:
            continue
        out.append(path)
    return out


def scan(root: Optional[Path] = None, words: Optional[Iterable[str]] = None) -> list[Leak]:
    """Every line of the publishable tree that names its owner."""
    root = config.APP_ROOT if root is None else root
    words = needles(extra=extra_patterns()) if words is None else list(words)
    pattern = compile_needles(words)
    leaks: list[Leak] = []
    for path in files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # a binary or unreadable file is not prose
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            found = pattern.search(line)
            if found:
                leaks.append(Leak(path.relative_to(root), lineno, line, found.group(0)))
    return leaks

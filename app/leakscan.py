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

import functools
import ipaddress
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, NamedTuple, Optional

from app import config, site

# Extensions worth reading. Anything textual that ships.
SCANNED_SUFFIXES = {
    ".py", ".html", ".css", ".js", ".mjs", ".md", ".sh", ".toml", ".service",
    ".txt", ".json", ".yml", ".yaml", ".cfg", ".ini", ".conf", ".webmanifest",
}

# Text files that carry no extension at all. `deploy/whisper/Dockerfile` is
# tracked and was read by nothing until this list existed - a Dockerfile is
# exactly where an ARG or an ENV line puts a token, so "no suffix" must not
# mean "not looked at".
SCANNED_NAMES = {"Dockerfile", "Containerfile", "Makefile", ".env", ".envrc"}

# Directories that are never part of the published tree.
SKIP_DIRS = {".git", "venv", "data", "secrets", "__pycache__", "node_modules", "shots"}

# Words too common to be evidence of anything. A login of "admin" or an owner
# called "Root" would otherwise make every line in the tree an offence.
TOO_COMMON = {
    "admin", "root", "user", "users", "test", "portal", "server", "host",
    "localhost", "home", "app", "main", "run", "runs", "data", "the", "and",
    "ubuntu", "debian", "pi", "dev", "prod", "www", "api", "web", "box",
}

# The personal config. Gitignored, never published, and the one file that is
# SUPPOSED to name its owner - so it is not read at all.
SKIP_NAMES = {"portal.toml"}

# Published, and documents each key by showing a value - so `host = "myserver"`
# there is documentation rather than a leak. The exemption is from the IDENTITY
# half only: this file ships, so a key pasted into it while editing would ship
# too, and the credential scan below still reads it.
IDENTITY_EXEMPT_NAMES = {"portal.example.toml"}

# Kept as the union under its old name: `files()` had one exemption list, and
# other callers (and one sweep script) still ask for it.
EXEMPT_NAMES = SKIP_NAMES | IDENTITY_EXEMPT_NAMES

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

# --- the second half of the check: things that are secret by their SHAPE -----
#
# The needles above answer "does this name its owner". They cannot answer "is
# this a live credential", because a key belonging to nobody the config knows
# about - a Slack webhook, an AWS pair, a Telegram bot token pasted into a
# comment - matches no needle at all and publishes cleanly. That is the failure
# this list exists for, and unlike the needles it is deliberately hard-coded:
# the shape of an Anthropic key is a fact about Anthropic, not about whoever
# installs this, so it protects every install identically without being
# configured.
#
# Every threshold below is a LENGTH, chosen so that the real thing matches and
# an obviously-fake test fixture does not. The tree is full of strings like
# `sk-ant-in-a-file` and `sk-ant-oat01-new`, and a check that stopped a publish
# over those would be turned off within a week - so the bar is set above the
# longest of them and below the shortest real credential of each kind.
CREDENTIAL_SHAPES: list[tuple[str, str]] = [
    # sk-ant-api03-<95 chars>, and the CLI's own OAuth pair (sk-ant-oat01- /
    # sk-ant-ort01-), all far past 32. The fixtures top out at 15.
    ("anthropic-key", r"sk-ant-[A-Za-z0-9_-]{32,}"),
    ("openai-key", r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    # ghp_/gho_/ghu_/ghs_/ghr_ are all exactly 36 characters of payload.
    ("github-token", r"gh[pousr]_[A-Za-z0-9]{36}"),
    ("github-pat", r"github_pat_[A-Za-z0-9_]{50,}"),
    ("slack-token", r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    ("slack-webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/+]{20,}"),
    # Both are fixed-length, and both are written WITHOUT a closing boundary on
    # purpose: `\b` after the last character asserts that the next one is not a
    # word character, so a key concatenated into a longer run - a URL, a base64
    # blob, a filename - matches nothing and is published. A false positive here
    # costs a human one glance; a miss costs a live key.
    ("aws-access-key", r"\bAKIA[0-9A-Z]{16}"),
    ("google-api-key", r"\bAIza[A-Za-z0-9_-]{35}"),
    # A bot token is `<numeric bot id>:AA<base64ish>`, and the portal's own
    # Telegram integration means one really could be pasted into a comment.
    ("telegram-bot-token", r"\b\d{8,12}:AA[A-Za-z0-9_-]{30,}"),
    ("tailscale-key", r"\btskey-(?:auth|api|client)-[A-Za-z0-9-]{10,}"),
    ("private-key-block", r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    # Three base64url segments. A JWT in the tree is either a session token or
    # a VAPID assertion, and both are credentials.
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # The catch-all, and the one that has to earn its place rather than cry
    # wolf: a named secret assigned a literal that LOOKS random. Twenty
    # characters, no whitespace, and at least one digit and one letter -
    # `token="wrong-token"` and `password = "hunter2"` are both below the bar
    # on purpose, because a scan nobody trusts is a scan nobody runs.
    (
        "assigned-secret",
        r"(?i)\b(?:pass(?:word|wd)|secret|token|api[_-]?key|access[_-]?key|auth)\b"
        r"\s*[:=]\s*[\"'](?=[^\"'\s]*\d)(?=[^\"'\s]*[A-Za-z])[^\"'\s]{20,}[\"']",
    ),
]

_SHAPES = [(name, re.compile(pattern)) for name, pattern in CREDENTIAL_SHAPES]

# A finding is printed to a terminal and pasted into a report, so echoing the
# line whole would copy the secret one step further out. Identity leaks are
# shown in full - seeing the hostname in context is the point - but a
# credential is reported by its shape and its line number only.
REDACTED = "<redacted>"


class Leak(NamedTuple):
    path: Path
    lineno: int
    line: str
    needle: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: [{self.needle}] {self.line.strip()[:120]}"


def credentials(line: str) -> Optional[str]:
    """The name of the first credential shape in `line`, if any."""
    for name, pattern in _SHAPES:
        if pattern.search(line):
            return name
    return None


def _redact(line: str) -> str:
    """The line with every credential-shaped run replaced.

    Not just the one that matched: a line holding two keys would otherwise
    report the first and print the second.
    """
    for _, pattern in _SHAPES:
        line = pattern.sub(REDACTED, line)
    return line


@functools.lru_cache(maxsize=1)
def local_addresses() -> tuple[str, ...]:
    """Every IP address this machine answers on.

    Derived, not listed, for the same reason as the hostname above - and this
    one was written because the listed version failed. `leak_patterns` in the
    author's own config named the wifi subnet and not the ethernet one, so the
    server's real LAN address sat in a committed test fixture through every
    clean scan. A hand-maintained list of your own addresses is wrong the first
    time an interface changes; the kernel's list never is.

    Loopback and link-local are excluded: they are the same on every machine on
    earth, so they identify nobody and would flag every `127.0.0.1` in the
    tree.
    """
    exe = shutil.which("ip")
    if not exe:  # not Linux, or a stripped container - the needles still work
        return ()
    try:
        out = subprocess.run(
            [exe, "-o", "addr", "show"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    found: set[str] = set()
    for match in re.finditer(r"\binet6?\s+([0-9a-fA-F:.]+)/", out):
        try:
            addr = ipaddress.ip_address(match.group(1))
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local:
            continue
        found.add(str(addr))
    return tuple(sorted(found))


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


def needles(
    where: Optional[site.Site] = None,
    extra: Optional[Iterable[str]] = None,
    addresses: Optional[Iterable[str]] = None,
) -> list[str]:
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

    `addresses` overrides what the kernel reports, so a test can state the
    machine it is describing instead of depending on the one it runs on.
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
    # The machine's own addresses, read from the kernel rather than configured.
    # An address passes `_useful` on its own - it is longer than two characters
    # and is not a word like "admin" - so it needs no exemption from the filter.
    found |= set(local_addresses() if addresses is None else addresses)
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
        if not path.is_file():
            continue
        if path.suffix not in SCANNED_SUFFIXES and path.name not in SCANNED_NAMES:
            continue
        if set(path.relative_to(root).parts) & SKIP_DIRS:
            continue
        if path.name in SKIP_NAMES:
            continue
        if path.relative_to(root).as_posix() in PRIVATE_PATHS:
            continue
        out.append(path)
    return out


def scan(root: Optional[Path] = None, words: Optional[Iterable[str]] = None) -> list[Leak]:
    """Every line of the publishable tree that names its owner or holds a key.

    Two checks over one pass, because they answer different questions and miss
    different things: the needles catch "this is MY machine", the shapes catch
    "this is A credential" regardless of whose. Either one alone reported a
    clean tree that was not.
    """
    root = config.APP_ROOT if root is None else root
    words = needles(extra=extra_patterns()) if words is None else list(words)
    pattern = compile_needles(words)
    leaks: list[Leak] = []
    for path in files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # a binary or unreadable file is not prose
            continue
        rel = path.relative_to(root)
        identity = path.name not in IDENTITY_EXEMPT_NAMES
        for lineno, line in enumerate(text.splitlines(), 1):
            shape = credentials(line)
            if shape:
                # Reported redacted, and reported FIRST: a line holding a key
                # may also hold a hostname, and printing that finding in full
                # would print the key beside it.
                leaks.append(Leak(rel, lineno, _redact(line), shape))
                continue
            found = pattern.search(line) if identity else None
            if found:
                leaks.append(Leak(rel, lineno, line, found.group(0)))
    return leaks

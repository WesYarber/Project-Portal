"""Security floors in `requirements.txt`, and the venv that has to satisfy them.

Site-wide tools' weekly dependency watch grades every installed version on this
estate against OSV.dev and files what it finds on the project that owns it. On
2026-09-20 it filed `cryptography` 49.0.0 here. Acting on one of those findings
is two separate jobs, and only the first is obvious:

- **Upgrade the venv this portal actually runs on.** A one-off
  `pip install -U cryptography==50.0.0`, which fixes exactly one machine.
- **Make the fix survive.** A bare `cryptography` line is satisfied by whatever
  version happens to be installed, so a fresh `pip install -r requirements.txt`
  on the office portal - or a rebuild of this one - is entitled to resolve it
  back to 49.0.0, and reports success either way. A `>=` floor is what turns
  that same command into an upgrade, and `deploy/update.py` reinstalls on the
  other installs precisely when this file moves.

So the floors are the deliverable and these tests guard them from both sides:
every floor declared in `requirements.txt` is met by the interpreter running
the suite (`test_the_running_venv_satisfies_every_floor`), and the one floor we
know the reason for cannot be quietly deleted (`test_cryptography_floor...`).

The generic half matters more than the cryptography half. The next advisory
will name a different package, and the person answering it should be able to
raise a floor in `requirements.txt` and have the suite start enforcing it with
no new test written.

`packaging` is not in `requirements.txt` and does not need to be: pytest
declares it, so it is present wherever this file can run at all.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from app import config

REQUIREMENTS = Path(config.BASE_DIR) / "requirements.txt"


def _requirements() -> list[Requirement]:
    """Every real requirement line, with comments and blanks dropped.

    A `#` only opens a comment at the start of a line or after whitespace, so
    this does not have to cope with one inside a marker or a URL.
    """
    out = []
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        out.append(Requirement(line))
    return out


def _floors() -> list[tuple[Requirement, Version]]:
    """The requirements carrying a `>=` or `==` lower bound, with that bound."""
    out = []
    for req in _requirements():
        bounds = [
            Version(spec.version)
            for spec in req.specifier
            if spec.operator in (">=", "==")
        ]
        if bounds:
            out.append((req, max(bounds)))
    return out


def test_requirements_parses_and_is_not_accidentally_empty():
    """Guards the two tests below from passing on a file this stopped reading.

    Both iterate over what `_requirements` returns, so a parser that silently
    yielded nothing would turn them green while enforcing nothing at all.
    """
    names = {req.name for req in _requirements()}
    assert {"fastapi", "cryptography", "pytest"} <= names, names


def test_the_running_venv_satisfies_every_floor():
    """The interpreter running this suite meets every floor declared.

    This is what makes a raised floor self-enforcing: it fails on the install
    whose `pip install -r` never ran, rather than on the next weekly watch.
    """
    for req, floor in _floors():
        try:
            installed = Version(version(req.name))
        except PackageNotFoundError:  # pragma: no cover - a broken venv
            pytest.fail(f"{req.name} is in requirements.txt but not installed")
        assert installed >= floor, (
            f"{req.name} {installed} is installed, below the "
            f"{floor} floor in requirements.txt - run "
            f"`venv/bin/pip install -r requirements.txt` from the repo root"
        )


def test_cryptography_floor_clears_the_bleichenbacher_advisory():
    """GHSA-g6cj-pr64-35w5, fixed in 50.0.0.

    Pinned by name so that dropping the floor back to a bare `cryptography` is
    a test failure rather than a tidy-up nobody questions. The advisory itself
    was never reachable here - see `test_nothing_here_decrypts_pkcs7` - but
    this process holds every credential on the box, which is the reason the
    floor is worth carrying anyway.
    """
    floors = {req.name: floor for req, floor in _floors()}
    assert "cryptography" in floors, (
        "the cryptography floor is gone; a bare `cryptography` line lets "
        "pip resolve back to a version with GHSA-g6cj-pr64-35w5"
    )
    assert floors["cryptography"] >= Version("50.0.0")


def test_nothing_here_decrypts_pkcs7():
    """The reachability finding the advisory turned on, kept true.

    GHSA-g6cj-pr64-35w5 is an oracle in PKCS#7 EnvelopedData *decryption*, and
    this portal has no PKCS#7 anywhere: `app/webpush.py` uses `cryptography`
    for ECDSA P-256, AES-GCM and HKDF only. That is what let the HIGH be
    reported honestly as not urgent, so it is worth a test - if a later run
    does add PKCS#7 handling, the grading of the next advisory against this
    library changes, and this failing is the notice.

    Deliberately narrow: it looks for the PKCS#7 API surface, not the string
    "enveloped", which this codebase already uses for its own report envelopes.
    """
    root = Path(config.BASE_DIR)
    searched = [
        path
        for folder in ("app", "deploy", "scripts")
        for path in (root / folder).rglob("*.py")
    ]
    assert len(searched) > 20, "the sweep found almost no source files"

    offenders = [
        path.relative_to(root)
        for path in searched
        if "pkcs7" in path.read_text().lower()
    ]
    assert not offenders, (
        f"PKCS#7 handling appeared in {offenders}; re-grade "
        "GHSA-g6cj-pr64-35w5 and anything newer against it"
    )

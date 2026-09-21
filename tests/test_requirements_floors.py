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


def _floors() -> list[tuple[str, Version]]:
    """The requirements carrying a `>=` or `==` lower bound, name and bound."""
    out = []
    for req in _requirements():
        bounds = [
            Version(spec.version)
            for spec in req.specifier
            if spec.operator in (">=", "==")
        ]
        if bounds:
            out.append((req.name, max(bounds)))
    return out


def unmet(floors, lookup) -> list[str]:
    """The floors `lookup` does not satisfy, described. Empty means all met.

    Pure on purpose. The comparison here is the decision the whole file turns
    on, and reading it only through the real venv would make it untestable in
    the one direction that matters: a version of this that always returns `[]`
    - comparing a floor against itself, say - is indistinguishable from a
    correct one on a machine that is already up to date, which is every
    machine right after someone runs the upgrade. Taking `lookup` as an
    argument is what lets the tests below hand it a 49.0.0 that is not there.

    `lookup` returns the installed version string for a name, or None when the
    package is absent.
    """
    complaints = []
    for name, floor in floors:
        raw = lookup(name)
        if raw is None:
            complaints.append(f"{name} is in requirements.txt but not installed")
            continue
        if Version(raw) < floor:
            complaints.append(
                f"{name} {raw} is installed, below the {floor} floor in "
                "requirements.txt - run `venv/bin/pip install -r "
                "requirements.txt` from the repo root"
            )
    return complaints


def _installed(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def test_requirements_parses_and_is_not_accidentally_empty():
    """Guards the tests below from passing on a file this stopped reading.

    They iterate over what `_requirements` returns, so a parser that silently
    yielded nothing would turn them green while enforcing nothing at all.
    """
    names = {req.name for req in _requirements()}
    assert {"fastapi", "cryptography", "pytest"} <= names, names


def test_floors_reads_the_lower_bounds_and_ignores_the_unbounded():
    """`_floors` keeps the `>=` lines and drops the bare ones."""
    floors = dict(_floors())
    assert floors["cryptography"] == Version("50.0.0")
    assert "fastapi" not in floors, "a bare requirement has no floor to enforce"


@pytest.mark.parametrize(
    "installed, complains",
    [
        ("49.0.0", True),  # the version the watch actually reported
        ("50.0.0", False),  # exactly the floor is met
        ("50.0.1", False),  # what the upgrade landed on
        ("51.2.0", False),
        ("9.0.0", True),  # sorts ABOVE "50.0.0" as a string, below as a version
        (None, True),  # absent is not satisfied
    ],
)
def test_unmet_compares_versions_not_strings(installed, complains):
    """The comparison itself, driven with versions this machine does not have.

    `"9.0.0" > "50.0.0"` is True for strings and False for versions, so that
    row is the one that fails a plausible implementation which forgot to parse.
    """
    floors = [("cryptography", Version("50.0.0"))]
    assert bool(unmet(floors, lambda _name: installed)) is complains


def test_unmet_names_the_package_and_the_remedy():
    """A failure has to say what to run, since it fires on a machine mid-setup."""
    (complaint,) = unmet([("cryptography", Version("50.0.0"))], lambda _n: "49.0.0")
    assert "cryptography" in complaint
    assert "49.0.0" in complaint and "50.0.0" in complaint
    assert "pip install -r requirements.txt" in complaint


def test_the_running_venv_satisfies_every_floor():
    """The interpreter running this suite meets every floor declared.

    This is what makes a raised floor self-enforcing: it fails on the install
    whose `pip install -r` never ran, rather than on the next weekly watch.
    """
    assert unmet(_floors(), _installed) == []


def test_cryptography_floor_clears_the_bleichenbacher_advisory():
    """GHSA-g6cj-pr64-35w5, fixed in 50.0.0.

    Pinned by name so that dropping the floor back to a bare `cryptography` is
    a test failure rather than a tidy-up nobody questions. The advisory itself
    was never reachable here - see `test_nothing_here_decrypts_pkcs7` - but
    this process holds every credential on the box, which is the reason the
    floor is worth carrying anyway.
    """
    floors = dict(_floors())
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

    Deliberately narrow in two ways. It looks for the PKCS#7 API surface, not
    the string "enveloped", which this codebase already uses for its own report
    envelopes. And it skips `scripts/sweep_*.py`: a sweep's mutation table
    quotes the code it mutates, so this file's own sweep names the API in a
    label without any code going near it - which it did, and which failed this
    test the first time it ran.
    """
    root = Path(config.BASE_DIR)
    searched = [
        path
        for folder in ("app", "deploy", "scripts")
        for path in (root / folder).rglob("*.py")
        if not path.name.startswith("sweep_")
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

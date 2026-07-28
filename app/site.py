"""Who runs this portal, and where it answers - the one file that is personal.

Everything about *this installation* that another person's installation would
get wrong lives here: the owner's name, the hostname the portal is reachable
at, its ports, and the SSH user. The rest of the codebase reads these instead
of naming a machine, so the tree can be published without publishing anybody.

Three sources, in decreasing precedence:

1. **Environment** - `PORTAL_HOST`, `PORTAL_OWNER`, ... One variable per field,
   so a systemd unit or a container can set any of it without a file.
2. **`portal.toml`** - looked for at `$PORTAL_CONFIG`, then beside the code,
   then in `data/`. Gitignored, because it is exactly the personal part.
3. **What the machine says about itself** - and this is the important one.

The machine defaults are why a fresh clone needs no configuration at all:
`socket.gethostname()` is the name a home server is already reachable by on a
LAN, `getpass.getuser()` is the account you would SSH in as, and the account's
GECOS field is the owner's real name. On the author's own box those three
resolve to exactly the values that used to be hard-coded, so introducing this
file changed no behaviour there - and on someone else's box they resolve to
*their* values without them touching anything.

The one thing that cannot be guessed is a host that is only reachable by an
address the machine does not know it has (a tailnet name, a domain). That is
what `portal.toml` is for, and `warnings()` says so out loud when the guess
looks unusable - see the loopback note on `_default_host`.
"""
from __future__ import annotations

import getpass
import os
import socket
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping, Optional

BASE_DIR = Path(__file__).resolve().parent.parent

CONFIG_FILENAME = "portal.toml"
# Env var for each field is PREFIX + the field name upper-cased.
ENV_PREFIX = "PORTAL_"

# A hostname that only resolves on the machine itself is worse than useless
# here: every URL the portal prints is read on a *different* device (a phone,
# a laptop), so a loopback name is always a dead link. We cannot guess a better
# one, but we can refuse to be quiet about it - see `warnings()`.
LOOPBACK_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", ""}


def _default_host() -> str:
    """The name this machine believes it answers to."""
    try:
        return (socket.gethostname() or "").strip()
    except OSError:  # pragma: no cover - gethostname failing is near-mythical
        return ""


def _default_ssh_user() -> str:
    try:
        return (getpass.getuser() or "").strip()
    except (OSError, KeyError):  # pragma: no cover - no passwd entry for the uid
        return ""


def _default_owner() -> str:
    """The owner's name: the account's real name, else the login name.

    The GECOS field is the closest thing a Unix box has to "what is this
    person called", and it is what `finger`/`passwd` have always meant by it.
    Only the first comma-separated part is a name; the rest is office/phone.
    """
    try:
        import pwd

        gecos = pwd.getpwuid(os.getuid()).pw_gecos or ""
        name = gecos.split(",")[0].strip()
        if name:
            return name
    except (ImportError, KeyError, OSError):  # pragma: no cover - non-Unix / no entry
        pass
    return _default_ssh_user()


# How to refer to the owner in the second half of a sentence. The agent
# contract is written *about* a person - "a hostname he can reach", "only Wes
# can do this" - so de-personalising it means the pronouns too, not just the
# name. Getting this wrong is not a cosmetic bug: an agent that misgenders its
# own user in every prompt it writes is worse than one that says nothing.
#
# `they` is the default because it is the only choice that is never wrong for
# somebody we have not met, and a name tells you nothing about it. An install
# whose owner wants something else sets `pronouns` in portal.toml.
PRONOUNS: dict[str, tuple[str, str, str, str]] = {
    #          subject  object  possessive-adj  possessive-noun
    "they":   ("they",  "them", "their",        "theirs"),
    "he":     ("he",    "him",  "his",          "his"),
    "she":    ("she",   "her",  "her",          "hers"),
}
DEFAULT_PRONOUNS = "they"


def _pronoun_key(raw: str) -> str:
    """Normalise whatever someone typed into a key of PRONOUNS.

    Accepts the shorthand people actually write - `he/him`, `She/Her`,
    `they/them/theirs` - by taking the subject form. Anything unrecognised
    falls back to `they`, on the same never-raise principle as the rest of
    this file: a typo in a config file must not misgender somebody, and it
    certainly must not stop the portal booting.
    """
    first = (raw or "").strip().lower().split("/")[0].strip()
    return first if first in PRONOUNS else DEFAULT_PRONOUNS


# The same normalisation, published. `app/people.py` stores a pronoun per person
# now that more than one person uses the portal, and it must land on exactly the
# same three keys and the same never-raise fallback as the site config does - so
# it calls these rather than keeping a second copy of the rules that could drift.
def pronoun_key(raw: str) -> str:
    """The stored form of a pronoun string: one of `they`, `he`, `she`."""
    return _pronoun_key(raw)


def pronoun_forms(raw: str) -> tuple[str, str, str, str]:
    """(they, them, their, theirs) for a pronoun string, defaulting to they."""
    return PRONOUNS[_pronoun_key(raw)]


@dataclass(frozen=True)
class Site:
    """One installation's identity. Frozen: this is read at import and shared."""

    owner: str
    pronouns: str
    # How spawned runs pay for themselves: "subscription" (the Claude Code
    # login, which bills nothing and is paced against the usage window) or
    # "api_key". Deliberately explicit rather than sniffed from the
    # environment - see the module docstring of app/spawnauth.py, which owns
    # everything that follows from this value. The key itself is NOT kept
    # here: `Site` is a Jinja global on every page, and a secret on it would
    # be one stray `{{ SITE.… }}` from being served to a browser.
    auth_mode: str
    host: str
    port: int
    preview_port: int
    preview_https_port: int
    ssh_user: str
    ntfy_url: str
    ntfy_topic: str
    # `user@host` of a second machine with a browser on it, used by
    # deploy/screenshot.sh and deploy/interact.py to *see* a page rather than
    # infer it from HTML. Empty on an install that has no such machine, which
    # is the honest default - those two scripts then say what to set rather
    # than trying to SSH somewhere nobody configured.
    render_host: str
    # How the render machine reaches the portal, when that is not the same
    # address everybody else uses. Empty means "the same as base_url", which is
    # the normal case; it exists because a tailnet ACL, a VLAN or a split-horizon
    # DNS can leave one machine able to reach the portal only by another route,
    # and a screenshot tool pointed at an unreachable URL fails as a blank page
    # rather than as an error.
    render_portal_url: str
    # RFC 8292 requires a `sub` claim on every VAPID token: an address a push
    # service can use to complain to whoever is sending. Empty falls back to
    # `mailto:portal@<host>`, which is well-formed and inert - better than
    # shipping one person's real address to everybody who clones this, which is
    # what was here until 2026-07-26.
    contact_email: str

    @property
    def base_url(self) -> str:
        """Where a browser on another device should be pointed."""
        return f"http://{self.host}:{self.port}"

    @property
    def handle(self) -> str:
        """`user@host`, the prompt-shaped label the UI chrome wears."""
        return f"{self.ssh_user}@{self.host}"

    @property
    def _pronouns(self) -> tuple[str, str, str, str]:
        return PRONOUNS[_pronoun_key(self.pronouns)]

    @property
    def they(self) -> str:
        """Subject: "...a hostname **they** can reach"."""
        return self._pronouns[0]

    @property
    def them(self) -> str:
        """Object: "...ask **them** rather than guessing"."""
        return self._pronouns[1]

    @property
    def their(self) -> str:
        """Possessive adjective: "...reading it on **their** phone"."""
        return self._pronouns[2]

    @property
    def theirs(self) -> str:
        """Possessive noun: "...the decision is **theirs**"."""
        return self._pronouns[3]

    @property
    def owners(self) -> str:
        """The owner's name in the possessive: "**Ada's** projects".

        Always `'s`, including after a final s ("Jess's"), which is Chicago's
        rule and the one every line of prose in this tree already follows.
        The bare apostrophe is for classical names only, and nothing about a
        spelling tells you which a given name is.
        """
        return self.owner + "'s" if self.owner else self.owner

    @property
    def is_plural_pronoun(self) -> bool:
        """Whether the owner's pronoun takes a plural verb (`they are`)."""
        return _pronoun_key(self.pronouns) == "they"

    def template_vars(self) -> dict[str, str]:
        """Everything a prompt template may substitute, for `string.Template`.

        Every prompt the portal writes is *about* a person and *about* a
        machine, so publishing the tree means templating both out - the
        pronouns as much as the name. Sentences that would need verb agreement
        ("he is" vs "they are") are phrased with a modal or a noun phrase
        instead, so there is no conjugation table here and no template that
        reads correctly for one owner and wrongly for the next.
        """
        return {
            "OWNER": self.owner,
            "OWNERS": self.owners,
            "THEY": self.they,
            "THEM": self.them,
            "THEIR": self.their,
            "THEIRS": self.theirs,
            "HOST": self.host,
            "BASE_URL": self.base_url,
            # Where this checkout lives. A skill that says "copy the stylesheet
            # from <here>" needs it, and hard-coding one person's home
            # directory is exactly what this file exists to stop.
            "PORTAL_ROOT": str(BASE_DIR),
        }


def defaults() -> dict[str, Any]:
    """The zero-configuration answer, derived from the machine itself."""
    return {
        "owner": _default_owner(),
        # Nothing on a Unix box records this, and guessing it from a name is
        # exactly the mistake this field exists to stop. See PRONOUNS.
        "pronouns": DEFAULT_PRONOUNS,
        # The mode that cannot spend money is the one to default to. A fresh
        # clone with the CLI logged in works with no configuration; an
        # API-key install opts in with one line. app/spawnauth.py explains
        # why this is never inferred from a key being present.
        "auth_mode": "subscription",
        "host": _default_host(),
        "port": 8500,
        # The preview server (app/preview.py) gets a *separate port* rather
        # than a path, because a separate port is a separate origin: workspace
        # pages are written by agents and must not run on the origin that has
        # the portal's own POST endpoints on it.
        "preview_port": 8501,
        # The same server over a TLS terminator, where 443 is the portal
        # itself. See deploy/tailscale-https.sh.
        "preview_https_port": 8443,
        "ssh_user": _default_ssh_user(),
        "ntfy_url": "http://127.0.0.1:8095",
        "ntfy_topic": "portal",
        # Deliberately not guessed. There is no way to tell from this machine
        # whether another one exists, and an SSH attempt at an invented host is
        # a hang, not an error.
        "render_host": "",
        "render_portal_url": "",
        "contact_email": "",
    }


def config_path(env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """Where `portal.toml` is, or None if this install has no config file.

    `$PORTAL_CONFIG` wins so a deployment can put it anywhere; otherwise it is
    looked for beside the code and then in `data/`, both of which are already
    gitignored paths.
    """
    env = os.environ if env is None else env
    explicit = (env.get("PORTAL_CONFIG") or "").strip()
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    for candidate in (BASE_DIR / CONFIG_FILENAME, BASE_DIR / "data" / CONFIG_FILENAME):
        if candidate.is_file():
            return candidate
    return None


def read_file(path: Optional[Path]) -> dict[str, Any]:
    """Parse `portal.toml`, tolerating everything.

    A malformed or unreadable config file must not stop the portal booting:
    the machine defaults are always a working installation, and a boot loop is
    a far worse failure than a setting not applying. Unknown keys are ignored
    so a config written for a later version still loads.
    """
    if path is None:
        return {}
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # Both a flat file and a `[portal]` table are accepted - people reach for
    # a section header by habit, and failing silently on that would be unkind.
    section = raw.get("portal")
    if isinstance(section, dict):
        merged = {k: v for k, v in raw.items() if k != "portal"}
        merged.update(section)
        return merged
    return dict(raw)


def _coerce(name: str, value: Any, default: Any) -> Any:
    """A value from TOML or the environment, in the type the field wants.

    A bad value falls back to the default rather than raising, for the same
    reason `read_file` tolerates a broken file: `port = "eight thousand"`
    should be an ignored line, not an unbootable portal.
    """
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default
    text = str(value).strip()
    return text or default


def load(
    env: Optional[Mapping[str, str]] = None,
    path: Optional[Path] = None,
    *,
    use_file: bool = True,
) -> Site:
    """Resolve this installation's identity. Never raises."""
    env = os.environ if env is None else env
    values = defaults()
    if use_file:
        file_values = read_file(path if path is not None else config_path(env))
        for key, value in file_values.items():
            if key in values:
                values[key] = _coerce(key, value, defaults()[key])
    for field in fields(Site):
        raw = env.get(ENV_PREFIX + field.name.upper())
        if raw is not None and str(raw).strip():
            values[field.name] = _coerce(field.name, raw, defaults()[field.name])
    return Site(**values)


def warnings(site: Optional[Site] = None) -> list[str]:
    """Configuration problems worth telling a human about, in plain words.

    Only one so far, and it is the one that silently ruins the portal's most
    basic promise: if the hostname is a loopback name, every link the portal
    and its agents hand out is dead on the phone that reads it.
    """
    site = SITE if site is None else site
    out: list[str] = []
    if site.host.lower() in LOOPBACK_HOSTS or site.host.startswith("127."):
        out.append(
            f"This machine calls itself {site.host!r}, which no other device can reach. "
            f"Set `host` in {CONFIG_FILENAME} (or $PORTAL_HOST) to the name or address "
            "you actually open the portal at, or every link it prints will be a dead one."
        )
    if not site.owner:
        out.append(
            f"No owner name found for this account. Set `owner` in {CONFIG_FILENAME} "
            "(or $PORTAL_OWNER) so agents know who they are working for."
        )
    return out


# Resolved once at import: this is process-wide identity, not per-request state.
# Tests call `load()` with explicit arguments instead of mutating this.
SITE = load()

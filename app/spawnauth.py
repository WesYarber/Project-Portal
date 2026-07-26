"""How a spawned run pays for itself: a subscription, or an API key.

The portal was built against one arrangement - Claude Code logged into a Max
subscription, where `claude -p` bills nothing and the only real budget is the
usage window. Everything downstream assumes it: `app/limits.py` reads the
account's windows, `app/pacing.py` spreads runs across them, and
`agent_runner._extra_env` *strips* `ANTHROPIC_API_KEY` from every spawn so a
stray key in the portal's environment can never quietly turn a free run into a
billed one.

Publishing the portal means supporting the other arrangement too, without
giving up that guard. So the mode is explicit configuration, never a guess:

    auth_mode = "api_key"      # in portal.toml, or PORTAL_AUTH_MODE

**The key is not auto-detected, and that is the whole point.** If merely having
`ANTHROPIC_API_KEY` exported were enough to switch modes, then the guard that
exists to stop exactly that would be gone - a key leaking into the portal's
environment (a sourced `.env`, a CI variable, a shell profile) would silently
start billing somebody's card mid-run. Opting in costs one config line and
cannot happen by accident. In `subscription` mode the key is stripped no matter
where it came from, exactly as before.

Two things follow from the mode, and both are why this is a module rather than
one more field on `Site`:

* **A default spend ceiling.** A subscription run bills $0, so "no ceiling" is
  the right default there. An API-key run bills real money per token, and an
  unattended scheduler with no ceiling is how people wake up to a four-figure
  invoice. So API-key mode carries a per-run default (see
  `DEFAULT_API_KEY_BUDGET_USD`) that the operator can raise, lower or clear -
  but never has to remember to set.

* **Pacing has nothing to pace.** The subscription windows do not describe an
  API key's spending at all. This matters more than "the reading is missing":
  an API-key user almost certainly *does* have the CLI installed and logged in,
  so `~/.claude/.credentials.json` exists and the usage endpoint answers
  happily - with figures about a subscription those runs are not consuming.
  Left alone, the portal would hold scheduled runs because a window it is not
  spending looks full, and would offer to "spend down" headroom that expiring
  costs nothing. So `limits`/`pacing` are switched off by mode, not left to
  degrade on their own.

The key itself is deliberately kept off `Site`. `Site` is a Jinja global on
every rendered page; a secret on it is one `{{ SITE.anthropic_api_key }}` away
from being served to a browser. Nothing here is exposed to templates, and
`status()` reports where a key was found without ever reporting the key.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

from app.site import BASE_DIR, SITE, Site

MODE_SUBSCRIPTION = "subscription"
MODE_API_KEY = "api_key"
MODES = (MODE_SUBSCRIPTION, MODE_API_KEY)

# Spellings people actually write, mapped onto the two real modes. An
# unrecognised value falls back to `subscription` for the same reason a bad
# `port` falls back to 8500 in site.py - a typo must not stop the portal
# booting - and because subscription is the mode that cannot spend money.
_MODE_ALIASES = {
    "subscription": MODE_SUBSCRIPTION,
    "sub": MODE_SUBSCRIPTION,
    "max": MODE_SUBSCRIPTION,
    "oauth": MODE_SUBSCRIPTION,
    "claude_code": MODE_SUBSCRIPTION,
    "api_key": MODE_API_KEY,
    "api-key": MODE_API_KEY,
    "apikey": MODE_API_KEY,
    "api": MODE_API_KEY,
    "anthropic_api_key": MODE_API_KEY,
}

# Where a key may be kept, in the order they are consulted. `secrets/` is
# already gitignored and already holds this portal's other credentials, so it
# is the recommended home; the env vars exist for containers and systemd units.
#
# `ANTHROPIC_API_KEY` is consulted LAST and only in API-key mode - see the
# module docstring. `PORTAL_ANTHROPIC_API_KEY` is offered first of the two so
# an operator can point the portal at one key while leaving the ambient one
# (used by their own shell, their own editor) alone.
KEY_FILE = BASE_DIR / "secrets" / "anthropic_key.txt"
KEY_ENV_VARS = ("PORTAL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")

# Environment variables that would route a spawned run onto pay-as-you-go
# billing or a third-party gateway. Stripped from every spawn in both modes;
# in API-key mode the configured key is put back afterwards, so the only key a
# run can ever see is the one that was configured on purpose.
#
# The gateway variables (Bedrock, Vertex, a custom base URL) stay stripped in
# both modes: they are a different provider story, explicitly out of scope for
# a first release, and a half-supported one is worse than an unsupported one.
BILLING_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)

# The per-run ceiling API-key mode applies when the operator has not set one.
# Generous enough that an ordinary build run finishes inside it, small enough
# that a runaway loop is an annoyance rather than an invoice. A subscription
# install is unaffected: there, unset still means no ceiling, because there is
# nothing to cap.
DEFAULT_API_KEY_BUDGET_USD = 5.0

# Anthropic keys have looked like this since the API launched. Used only to
# *warn*: a key that fails this check is still passed through, because a
# prefix changing is a Tuesday at any vendor and refusing a valid key would be
# a far worse failure than mentioning a suspicious one.
KEY_PREFIX = "sk-ant-"


def mode(site: Optional[Site] = None) -> str:
    """Which arrangement this installation pays with. Never raises."""
    site = SITE if site is None else site
    raw = (getattr(site, "auth_mode", "") or "").strip().lower().replace("-", "_")
    return _MODE_ALIASES.get(raw, MODE_SUBSCRIPTION)


def is_api_key(site: Optional[Site] = None) -> bool:
    return mode(site) == MODE_API_KEY


def paces_on_subscription(site: Optional[Site] = None) -> bool:
    """Whether the subscription usage windows describe this install's spending.

    False in API-key mode - and the caller must check this rather than relying
    on the usage reading being absent, because an API-key user who also has the
    CLI logged in gets a perfectly good reading about a subscription their runs
    are not touching. See the module docstring.
    """
    return mode(site) == MODE_SUBSCRIPTION


def _read_key_file(path: Optional[Path] = None) -> str:
    """The first meaningful line of the key file, or ''. Never raises.

    Blank lines and `#` comments are skipped so the file can explain itself,
    matching how `secrets/cloudflare.txt` is already read.
    """
    path = KEY_FILE if path is None else path
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def key_source(
    env: Optional[Mapping[str, str]] = None,
    path: Optional[Path] = None,
    site: Optional[Site] = None,
) -> tuple[str, str]:
    """`(key, where_it_came_from)`, or `("", "")`.

    Always `("", "")` in subscription mode: not looking is what makes the
    strip in `spawn_env` unconditional there.
    """
    if not is_api_key(site):
        return "", ""
    env = os.environ if env is None else env
    for var in KEY_ENV_VARS:
        value = (env.get(var) or "").strip()
        if value:
            return value, f"${var}"
    value = _read_key_file(path)
    if value:
        return value, str(path or KEY_FILE)
    return "", ""


def api_key(
    env: Optional[Mapping[str, str]] = None,
    path: Optional[Path] = None,
    site: Optional[Site] = None,
) -> str:
    return key_source(env, path, site)[0]


def spawn_env(
    env: dict[str, str],
    *,
    key_env: Optional[Mapping[str, str]] = None,
    path: Optional[Path] = None,
    site: Optional[Site] = None,
) -> dict[str, str]:
    """Turn a copied environment into the one a spawned `claude -p` may see.

    Strips first, then injects. That order is the load-bearing part: in
    API-key mode with nothing configured, a run ends up with *no* key rather
    than inheriting whatever happened to be lying around - flipping the mode
    on its own can never smuggle an ambient credential into a spawn.

    Mutates and returns `env`, which is always a caller's private copy.
    """
    resolved = api_key(key_env, path, site)
    for var in BILLING_ENV_VARS:
        env.pop(var, None)
    if resolved:
        env["ANTHROPIC_API_KEY"] = resolved
    return env


def default_budget_usd(site: Optional[Site] = None) -> Optional[float]:
    """The per-run dollar ceiling to use when the operator has not set one."""
    return DEFAULT_API_KEY_BUDGET_USD if is_api_key(site) else None


def status(
    env: Optional[Mapping[str, str]] = None,
    path: Optional[Path] = None,
    site: Optional[Site] = None,
) -> dict:
    """What to show a human about how runs are paid for. Carries no secret."""
    current = mode(site)
    key, source = key_source(env, path, site)
    return {
        "mode": current,
        "api_key": current == MODE_API_KEY,
        "paced": current == MODE_SUBSCRIPTION,
        "has_key": bool(key),
        # Where it was found, never what it is.
        "key_source": source,
        # What an unset per-run ceiling means here. The Settings page shows it
        # as the placeholder, because "blank" meaning "no cap" on one install
        # and "$5" on another is exactly the sort of thing a form must say out
        # loud rather than leave the operator to infer.
        "default_budget_usd": default_budget_usd(site),
        "label": (
            "Anthropic API key (runs are billed per token)"
            if current == MODE_API_KEY
            else "Claude Code subscription (runs bill nothing; paced against the usage window)"
        ),
        "problems": problems(env, path, site),
    }


def problems(
    env: Optional[Mapping[str, str]] = None,
    path: Optional[Path] = None,
    site: Optional[Site] = None,
) -> list[str]:
    """Misconfigurations worth saying out loud, in plain words.

    Each of these otherwise presents as a run that just fails, with the real
    cause two layers down in the CLI's own error - which is the failure mode
    worth spending a few lines to avoid.
    """
    env = os.environ if env is None else env
    out: list[str] = []
    if is_api_key(site):
        key, _ = key_source(env, path, site)
        if not key:
            out.append(
                "auth_mode is 'api_key' but no key was found. Put one in "
                f"{KEY_FILE}, or set $PORTAL_ANTHROPIC_API_KEY - until then every "
                "run will fail at the CLI with an authentication error."
            )
        elif not key.startswith(KEY_PREFIX):
            out.append(
                f"The configured Anthropic API key does not start with {KEY_PREFIX!r}. "
                "It will still be used, but check it is a key and not an OAuth "
                "token or a truncated paste."
            )
    else:
        stray = [var for var in KEY_ENV_VARS if (env.get(var) or "").strip()]
        if stray:
            out.append(
                f"{stray[0]} is set, but this portal is in subscription mode, so it "
                "is stripped from every run and nothing is billed to it. Set "
                "auth_mode = \"api_key\" in portal.toml if you meant to use it."
            )
    return out

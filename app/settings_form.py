"""One place that knows how to validate and persist every setting.

The Settings page used to be a single giant form bound to a handler with one
named parameter per setting. That coupling caused a real, silent failure: when
the template gained the appearance dropdowns before the running process gained
the matching handler parameters, FastAPI quietly dropped the unknown fields and
still answered 303, so saving *looked* like it worked and nothing was written.

So the handler no longer names fields at all. Each form declares which settings
it owns via a hidden `_fields` input, and this module validates and writes
exactly those. A form can therefore be split into as many sections as the page
needs without any section clobbering another's values, and a field the running
code doesn't know about is reported rather than swallowed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from . import config, daycycle, jumpkeys, runlimit, usage

# The hidden input a section form uses to declare the settings it owns.
FIELDS_INPUT = "_fields"


@dataclass(frozen=True)
class Field:
    """How one setting is read off a submitted form.

    `checkbox` matters: an unchecked box submits nothing at all, so absence has
    to mean "0" for those and "leave alone" for everything else.
    """

    key: str
    clean: Callable[[str], str]
    checkbox: bool = False


def _text(value: str) -> str:
    return value.strip()


def _choice(allowed, default: str) -> Callable[[str], str]:
    allowed = set(allowed)

    def clean(value: str) -> str:
        value = value.strip()
        return value if value in allowed else default

    return clean


def _positive_int(default: str, low: int = 1, high: int = 100_000) -> Callable[[str], str]:
    def clean(value: str) -> str:
        try:
            number = int(value.strip())
        except (TypeError, ValueError):
            return default
        return str(number) if low <= number <= high else default

    return clean


def _decimal_or_blank(default: str = "", high: float = 1000.0) -> Callable[[str], str]:
    """A non-negative dollar amount, or blank for "no ceiling".

    Anything unparseable or out of range falls back to `default` (blank) so a
    bad entry disables the ceiling rather than wedging every run at $0.
    """

    def clean(value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number <= 0 or number > high:
            return default
        return f"{number:g}"

    return clean


def _memory_size(value: str) -> str:
    """A memory ceiling: blank (the derived default), an off-switch, or a size
    like "6G".

    Junk is rejected back to blank rather than stored, because a stored typo
    would sit on the settings page looking like a cap that is in force while
    `runlimit` quietly ignores it - the field is the only place anyone would
    look to find out what the cap is.
    """
    value = value.strip()
    if not value or value.lower() in {"0", "off", "none", "no", "unlimited"}:
        return value.lower()
    parsed = runlimit.parse_size(value)
    return value if parsed and parsed > 0 else ""


def _ratio(default: str, low: float = 0.0, high: float = 1.0) -> Callable[[str], str]:
    """A fraction in (low, high], e.g. the front-load gamma. Junk or an
    out-of-range value falls back to `default` rather than inverting the curve."""

    def clean(value: str) -> str:
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            return default
        return f"{number:g}" if low < number <= high else default

    return clean


def _hour(value: str) -> str:
    try:
        number = int(value.strip())
    except (TypeError, ValueError):
        return str(daycycle.DEFAULT_RESET_HOUR)
    return str(number if 0 <= number <= 23 else daycycle.DEFAULT_RESET_HOUR)


def _appearance_fields() -> list[Field]:
    """Derived from config so adding a look-and-feel option is a one-line
    change there rather than an edit in three files."""
    return [
        Field(key, _choice({v for v, _ in choices}, config.APPEARANCE_DEFAULTS[key]))
        for key, choices in config.APPEARANCE_CHOICES.items()
    ]


def _build_registry() -> dict[str, Field]:
    fields: list[Field] = [
        Field("worker_enabled", _text, checkbox=True),
        Field("worker_model", _choice(config.MODEL_VALUES, config.DEFAULT_MODEL)),
        # 0 is a real, useful value here: it means "no pacing at all", so the
        # worker starts the next run the moment a slot frees up.
        Field("worker_interval_min", _positive_int("10", low=0, high=1440)),
        Field("max_parallel_runs", _positive_int("2", low=1, high=config.MAX_PARALLEL_LIMIT)),
        Field("max_runs_per_day", _positive_int("8", low=0, high=999)),
        Field("run_timeout_min", _positive_int("30", low=1, high=1440)),
        Field("run_max_turns", _positive_int("400", low=10, high=2000)),
        Field("run_max_budget_usd", _decimal_or_blank()),
        Field("run_memory_max", _memory_size),
        Field("learnings_cap_lines", _positive_int("200", low=0, high=5000)),
        Field("limit_hold_percent", _positive_int("90", low=1, high=100)),
        Field("spend_down_session_hold", _positive_int("70", low=1, high=100)),
        Field("spend_front_load", _ratio("0.75")),
        Field("saturation_max_duty", _positive_int("85", low=0, high=100)),
        Field("require_build_approval", _text, checkbox=True),
        Field("day_reset_hour", _hour),
        Field("cost_units", _choice(usage.COST_UNIT_CHOICES, usage.DEFAULT_COST_UNITS)),
        Field("telegram_enabled", _text, checkbox=True),
        Field("telegram_token", _text),
        Field("telegram_chat_id", _text),
        Field("telegram_natural_language", _text, checkbox=True),
        Field("telegram_model", _choice(config.MODEL_VALUES, config.TELEGRAM_MODEL)),
        Field("ask_model", _choice(config.MODEL_VALUES, config.ASK_MODEL)),
        Field("self_review", _text, checkbox=True),
        Field("self_review_model", _choice(config.MODEL_VALUES, config.SELF_REVIEW_MODEL)),
        Field("hook_guardrails", _text, checkbox=True),
        Field("stop_report_nudge", _text, checkbox=True),
        Field("hook_audit", _text, checkbox=True),
        Field("model_watch", _text, checkbox=True),
        Field("research_model", _choice(config.MODEL_VALUES, config.RESEARCH_MODEL)),
        Field("spend_down_model", _choice(config.MODEL_VALUES, config.DEFAULT_MODEL)),
        Field("glados_mode", _text, checkbox=True),
        Field("show_priority", _text, checkbox=True),
        Field("ntfy_url", _text),
        Field("ntfy_topic", _text),
    ]
    # One field per jumpable section, derived from jumpkeys.ACTIONS. `clean`
    # only ever sees one field at a time, so it can validate the letter but not
    # notice that two sections now want the same one - that is what the
    # de-duplication pass at the end of `apply` is for.
    fields.extend(
        Field(jumpkeys.setting_key(name), jumpkeys.clean) for name in jumpkeys.ACTION_NAMES
    )
    fields.extend(_appearance_fields())
    return {field.key: field for field in fields}


REGISTRY: dict[str, Field] = _build_registry()
KNOWN_KEYS = tuple(REGISTRY)

# The settings that belong to a PERSON rather than to the install.
#
# Wes, 2026-07-28: "It would be cool as well if she was able to customize the
# theme of the site for her user to her liking." The appearance layers were one
# global row each, which is right for one person and wrong for two - her
# turning the scanlines off would turn them off on his phone as well.
#
# Validation is unchanged: these still go through the same `Field.clean` as
# everything else, and only the *destination* differs. `split_personal` is the
# one place that knows the difference, so a new appearance option is still a
# one-line change in config.
#
# The keyboard jumps are deliberately NOT here. A jump key is a fact about the
# page - the footer hint prints it, and the letters are the same ones the docs
# name - whereas a typeface is a fact about the reader. Wes asked for the theme
# to be hers, not the keys.
PERSONAL_KEYS: frozenset[str] = frozenset(config.APPEARANCE_CHOICES)


def split_personal(values: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """`(install settings, this person's settings)` from one cleaned form.

    Returned as two dicts rather than written here, for the same reason
    `apply` returns instead of writing: the caller owns persistence, and the
    split is then testable without a database.
    """
    mine = {k: v for k, v in values.items() if k in PERSONAL_KEYS}
    theirs = {k: v for k, v in values.items() if k not in PERSONAL_KEYS}
    return theirs, mine


def declared_fields(raw: Optional[str]) -> list[str]:
    """The settings a submitted form claims to own.

    A missing `_fields` means an older form (or a test) posting everything at
    once, which is the legacy whole-page behavior: every known key is in play.
    Unknown names are dropped here so a stale form can't write junk keys.
    """
    if raw is None:
        return list(KNOWN_KEYS)
    names = [name.strip() for name in raw.split(",")]
    return [name for name in names if name in REGISTRY]


def apply(form: dict[str, str], declared: Optional[str] = None) -> dict[str, str]:
    """Validate and return the settings this form should write.

    Returns the cleaned key/value pairs rather than writing them, so the
    validation is testable without a database and the caller keeps control of
    persistence ordering.
    """
    out: dict[str, str] = {}
    for key in declared_fields(declared):
        field = REGISTRY[key]
        if field.checkbox:
            out[key] = "1" if form.get(key) else "0"
        elif key in form:
            out[key] = field.clean(str(form[key]))
    return _deconflict_jump_keys(out)


def _deconflict_jump_keys(out: dict[str, str]) -> dict[str, str]:
    """Stop two sections being saved onto the same letter.

    The per-field `clean` cannot see its siblings, so this is the only place
    that can - and it has to be a pass over the whole submission rather than a
    smarter Field, because "is this letter free?" is a question about the other
    three fields in the same form.

    The loser is written as blank rather than left alone, deliberately: the
    stored value and the settings row then agree with each other and with what
    the browser will actually do. Leaving the old letter in place would give a
    settings page that shows a binding the page has just refused to honor.
    """
    submitted = {
        name: out[jumpkeys.setting_key(name)]
        for name in jumpkeys.ACTION_NAMES
        if jumpkeys.setting_key(name) in out
    }
    if not submitted:
        return out
    for name, key in jumpkeys.deconflict(submitted).items():
        out[jumpkeys.setting_key(name)] = key
    return out

"""Which key jumps to which section, and how a person changes it.

Wes shipped the jumps and then, the same morning, asked for the obvious next
thing:

  2026-07-27: "Allow these key commands to be reconfigured in settings."

So the letters n / a / j / t / p are now defaults, not facts. This module owns the
whole of that: the list of jumpable sections, the setting key each one lives
under, what a valid binding is, and the `key -> [target]` map app.js consumes.

It deliberately imports nothing from the rest of the app - not `config`, not
`db`. Two reasons, and the first is not style:

- `config.DEFAULT_SETTINGS` has to seed these four settings, so config imports
  *this*. Anything this module imported back from config would be a cycle at
  interpreter start.
- Everything here is a pure function of a settings mapping, which is what makes
  the conflict rules testable without a database or a browser.

Reading is `configured()` / `bindings()`; writing goes through `settings_form`,
which validates with `clean()` and de-duplicates with `deconflict()`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Optional

# The letters a binding may use.
#
# Letters only, and that is a decision rather than laziness:
#
# - Digits would work, but a JS object puts integer-like keys FIRST and in
#   numeric order regardless of insertion order, so the footer hint ("n j t p
#   jump to a section") would silently reorder itself around a digit binding.
# - Punctuation is worse than useless: `/` and `'` open Firefox's quick-find,
#   so binding one gives you a jump that fights a browser feature on one
#   browser and not the others.
#
# Twenty-six letters for five sections is not a constraint anyone will feel.
ALPHABET = "abcdefghijklmnopqrstuvwxyz"

# What "unbound" looks like, in the settings row and in the stored value.
OFF = ""


@dataclass(frozen=True)
class Action:
    """One jumpable thing.

    `targets` are `[data-jump]` names in preference order, and the list is why
    N works on both pages: a project page declares `note`, the dashboard
    declares `idea`, and one key finds whichever exists. app.js picks the first
    match, so order matters - it is the preference, not a set.
    """

    name: str
    label: str
    hint: str
    default: str
    targets: tuple[str, ...]


ACTIONS: tuple[Action, ...] = (
    Action(
        name="note",
        label="add a note",
        hint="or a new idea on the dashboard; the cursor lands in the box",
        default="n",
        targets=("note", "idea"),
    ),
    # Wes, 2026-07-28: "Hitting 'a' should go to the 'ask question' prompt,
    # activate it, and highlight the text field to begin typing."
    #
    # "Activate it" is the word that matters: the ask block is a <details> that
    # sits closed, so unlike the other four this jump has to OPEN its target
    # before the cursor can land in it. jumpTo already opens any <details> on
    # the way to a target, and `closest` counts the element itself - so the
    # attribute goes on the <details> and the opening comes for free.
    Action(
        name="ask",
        label="ask a question",
        hint="opens the ask box at the top of a project and puts the cursor in it",
        default="a",
        targets=("ask",),
    ),
    Action(
        name="journal",
        label="journal",
        hint="the run-by-run account, near the bottom of a project",
        default="j",
        # The scrolling box first, its heading second. Wes asked for the box's
        # own top edge against the top of the window rather than the heading
        # above it - and the fallback is not decoration: a project with no
        # entries yet renders no box, and a key that silently does nothing on
        # an empty project reads as a broken key rather than an empty journal.
        targets=("journal-box", "journal"),
    ),
    Action(
        name="todo",
        label="todo list",
        hint="the working checklist",
        default="t",
        targets=("todo",),
    ),
    Action(
        name="project",
        label="project controls",
        hint="status, agent and the buttons at the top of a project",
        default="p",
        targets=("project",),
    ),
)

ACTION_NAMES: tuple[str, ...] = tuple(action.name for action in ACTIONS)


def setting_key(name: str) -> str:
    """The settings-table key one action's binding is stored under."""
    return f"jump_key_{name}"


SETTING_KEYS: tuple[str, ...] = tuple(setting_key(name) for name in ACTION_NAMES)

# Seeded into config.DEFAULT_SETTINGS, so a fresh install starts on the keys
# Wes asked for rather than on nothing.
DEFAULTS: dict[str, str] = {setting_key(a.name): a.default for a in ACTIONS}


def clean(value: Optional[str]) -> str:
    """One lowercase letter, or `OFF`.

    Anything else - two characters, a digit, a space, None - becomes OFF rather
    than being stored. A stored binding that the browser can never match would
    be a settings row describing a key that does nothing, which is the exact
    kind of quiet lie this project keeps refusing to ship; blank at least says
    "no key" out loud, and the settings page prints that beside the field.
    """
    text = (value or "").strip().lower()
    return text if len(text) == 1 and text in ALPHABET else OFF


def deconflict(keys: Mapping[str, str]) -> dict[str, str]:
    """Resolve two sections claiming the same letter: the earlier one keeps it.

    Earlier means earlier in `ACTIONS`, which is a fixed order, so the outcome
    does not depend on dict iteration or on which field the browser happened to
    submit first.

    Losing the letter is not silent. The loser comes back as OFF, its settings
    row reads "no key - nothing jumps here", and the footer hint on every page
    stops listing it. The alternative - handing app.js two entries for one
    letter - resolves to whichever the JSON serialiser wrote last, which is a
    coin toss nobody can see.

    Only the names present in `keys` are considered, so this is safe to run
    over a partial form submission; the read path calls it again over the full
    stored set, which is what guarantees the live bindings are unambiguous even
    if the database is edited by hand.
    """
    out: dict[str, str] = {}
    taken: set[str] = set()
    for name in ACTION_NAMES:
        if name not in keys:
            continue
        key = clean(keys[name])
        if key and key in taken:
            key = OFF
        if key:
            taken.add(key)
        out[name] = key
    return out


def configured(settings: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Action name -> the letter bound to it (or OFF), for these settings.

    A missing row means the setting has never been written on this install, so
    the default applies. A row holding "" means somebody turned that jump off
    on purpose, and that must survive - which is why this checks for `None`
    rather than for falsiness.
    """
    settings = settings or {}
    raw: dict[str, str] = {}
    for action in ACTIONS:
        stored = settings.get(setting_key(action.name))
        raw[action.name] = action.default if stored is None else stored
    return deconflict(raw)


def bindings(settings: Optional[Mapping[str, str]] = None) -> dict[str, list[str]]:
    """`key -> [data-jump names]` - exactly what app.js needs to do its job.

    Built in `ACTIONS` order so the footer hint reads in a stable order, and
    unbound actions simply do not appear.
    """
    chosen = configured(settings)
    out: dict[str, list[str]] = {}
    for action in ACTIONS:
        key = chosen.get(action.name, OFF)
        if key:
            out[key] = list(action.targets)
    return out


def bindings_json(settings: Optional[Mapping[str, str]] = None) -> str:
    """The bindings as the compact JSON that rides on `<body data-jump-keys>`.

    `sort_keys=False` on purpose: insertion order is ACTIONS order, and that is
    the order the hint lists the keys in.
    """
    return json.dumps(bindings(settings), separators=(",", ":"), sort_keys=False)


def rows(settings: Optional[Mapping[str, str]] = None) -> list[dict[str, str]]:
    """One row per action for the Settings page: the field, and what it does.

    The `state` string is the feedback that makes a rejected duplicate visible
    without a flash message - reload after binding two sections to `j` and the
    loser's row says so in words.
    """
    chosen = configured(settings)
    out: list[dict[str, str]] = []
    for action in ACTIONS:
        key = chosen.get(action.name, OFF)
        out.append(
            {
                "name": action.name,
                "field": setting_key(action.name),
                "label": action.label,
                "hint": action.hint,
                "key": key,
                "state": f"{key} jumps here" if key else "no key - nothing jumps here",
            }
        )
    return out

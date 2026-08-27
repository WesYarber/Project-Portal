"""Where the blocks of a project page sit, and how a person changes that.

Wes, 2026-07-28, describing what a theme should be able to do for Karli:

  "her own theme to where all of the functional pieces are still there, but she
   can change how they appear, where they appear, how they look."

Themes shipped the same day and cover two of those three. They cannot cover the
third, and the reason is a rule rather than an oversight: `themes.css` bans
`display`, `position` and `visibility` outright, because a theme that can hide a
control is a look you cannot get back out of. Moving a panel is a layout change,
so "where they appear" needs its own mechanism, and this is it.

Three decisions worth stating, because each one is a thing this deliberately
does NOT do:

- **Order only. Nothing can be hidden.** His own words were "all of the
  functional pieces are still there" - the point of a personal arrangement is
  that the page is still the same page. A hidden section is also unfindable:
  there would be no control left on screen to bring it back, which is exactly
  the trap the CSS ban exists to avoid. Sections that have nothing in them
  already render as nothing, which is the honest version of hiding.
- **Real markup order, not `order:` in CSS.** Flexbox `order` moves what you
  see and leaves the document alone, so the tab sequence, a screen reader and
  the rail's chapter list would all still be reading the old page. The rail
  builds itself from `[data-jump-label]` in document order, so reordering the
  markup moves the chapters for free - and CSS ordering would have silently
  desynchronized the two.
- **Three blocks do not move**: the title header, the "Since you last looked"
  banner and the danger zone. The first two are the identity of the page and
  the thing you opened it to read; the third is project deletion, which must
  never float up to somewhere a thumb can find it by accident.

Storage is one string in the person's `appearance` JSON, which is why the
values here are names rather than indices: an order stored as positions would
quietly mean something else the moment a section is added or removed.

Like `jumpkeys`, this module imports nothing from the rest of the app. It is a
pure function of a stored string, so every rule below is testable without a
database, a browser or a rendered page.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

# The settings key, in the person's `appearance` blob beside their theme.
SETTING_KEY = "project_sections"

# How the stored string is punctuated. Commas, because the value shares a JSON
# object with the appearance layers and stays readable in a hand-edited row.
SEP = ","


@dataclass(frozen=True)
class Section:
    """One movable block of the project page.

    `name` is the identifier in the stored order AND the key the template
    dispatches on, so the two can never drift apart. Where the block already
    declares a `data-jump` target, the name is that same word on purpose: the
    settings page then labels a row with the same noun the jump key and the
    rail chapter use for it.
    """

    name: str
    label: str
    hint: str


# The default order IS the order the page was written in, so a portal where
# nobody has ever touched this renders byte-for-byte what it rendered before
# the feature existed.
SECTIONS: tuple[Section, ...] = (
    Section("ask", "Ask project", "The folded box that asks a read-only question."),
    Section("project", "Overview", "Stage, model, title and description."),
    Section("console", "Agent console", "The live transcript of the run that is working."),
    Section("subprojects", "Sub-projects", "The children of this project, or its parent."),
    Section("questions", "Questions", "Open questions, plus saved and deleted ones."),
    Section("todo", "Todo", "The working checklist, split by whose it is."),
    Section("note", "Add note", "The box you write a note or drop a file into."),
    Section("files", "Files", "Uploads and the workspace tree."),
    Section("journal", "Journal", "Everything that has happened, newest first."),
)

DEFAULT_ORDER: tuple[str, ...] = tuple(section.name for section in SECTIONS)
SECTION_NAMES: frozenset[str] = frozenset(DEFAULT_ORDER)
BY_NAME: dict[str, Section] = {section.name: section for section in SECTIONS}


def _parse(raw: Optional[str]) -> list[str]:
    """The recognized names in a stored or submitted string, in order.

    Unknown names are dropped and repeats are kept once. Both are things a
    stale form or a hand-edited row can produce, and neither is worth raising
    over: the page has to render.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for token in str(raw).split(SEP):
        name = token.strip()
        if name in SECTION_NAMES and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _restore_missing(names: list[str]) -> list[str]:
    """Put back any section the stored order does not mention.

    This is the whole reason a new section can ship safely. Every order saved
    before today lists nine names; the tenth section to be built is in none of
    them, and the obvious repair - append it - would put it at the very bottom
    of the page, below the journal, for every person who has ever saved an
    arrangement. Nobody would find it, and it would look like the feature had
    not shipped.

    So a missing name goes back beside the neighbor it shipped next to: after
    the nearest section that precedes it in `DEFAULT_ORDER` and is actually
    present, or at the front when there is no such section. A person who has
    only nudged the journal upward therefore gets the new block roughly where
    its designer put it, without losing the one change they made.

    Stated as one rule, because a partly-filled value falls out of it and the
    result surprises people who expect otherwise: **the names that ARE stored
    keep their relative order; every other name returns to its default
    neighborhood.** So `"journal,todo"` does not mean "journal and todo at the
    top" - it means "journal above todo, everything else where it was", which
    lands journal sixth. The UI never writes a partial value (it posts all
    nine), so this governs only an upgrade or a hand-edited row, and for an
    upgrade it is exactly the behavior wanted.
    """
    out = list(names)
    for index, name in enumerate(DEFAULT_ORDER):
        if name in out:
            continue
        anchor = -1
        for earlier in reversed(DEFAULT_ORDER[:index]):
            if earlier in out:
                anchor = out.index(earlier)
                break
        out.insert(anchor + 1, name)
    return out


def order(raw: Optional[str] = None) -> list[str]:
    """The section names to render, top to bottom.

    Always every section exactly once, whatever was stored - a page missing a
    block because a preference was truncated would be a data-loss bug wearing a
    layout bug's clothes.
    """
    return _restore_missing(_parse(raw))


def clean(raw: Optional[str]) -> str:
    """The canonical stored form of a submitted order.

    An order that works out to the default is stored as the empty string rather
    than as the nine names, and that is deliberate rather than tidy-mindedness:
    it is the same distinction `clear_appearance` draws between "I chose the
    shipped value" and "I have chosen nothing". Only the second one keeps
    following the page as it is redesigned, and somebody who dragged a section
    back where it started meant the second one.
    """
    resolved = order(raw)
    if tuple(resolved) == DEFAULT_ORDER:
        return ""
    return SEP.join(resolved)


def sections(raw: Optional[str] = None) -> list[Section]:
    """`order()` as the Section records themselves, for the settings rows."""
    return [BY_NAME[name] for name in order(raw)]


def move(raw: Optional[str], name: str, delta: int) -> str:
    """One section nudged `delta` places, as a value to store.

    Lives here rather than only in the browser so the arrangement can be
    rebuilt, and its edges tested, without a DOM: a nudge off either end is a
    no-op rather than a wrap-around, because a control that teleports the
    journal from the bottom of the page to the top when you press "down" once
    too often is indistinguishable from a bug.
    """
    resolved = order(raw)
    if name not in BY_NAME:
        return clean(SEP.join(resolved))
    at = resolved.index(name)
    target = at + int(delta)
    if not 0 <= target < len(resolved):
        return clean(SEP.join(resolved))
    resolved.insert(target, resolved.pop(at))
    return clean(SEP.join(resolved))


def is_default(raw: Optional[str]) -> bool:
    """Whether this person is following the shipped arrangement."""
    return clean(raw) == ""


def describe(raw: Optional[str]) -> str:
    """A one-line answer to "what have I actually changed?".

    Naively this is "every section not where it shipped", and that reads as
    nonsense: pulling the journal from ninth to first shifts the other eight
    down by one, so the line said `Journal, Ask project, Overview, Agent
    console, Sub-projects...` - a wall naming everything, for one move.

    So a section displaced by a single place is treated as collateral of
    somebody else's move and left out. That isolates the journal exactly in the
    case above, and when nothing has moved further than one place (two
    neighbors swapped) the rule finds nothing, so the fallback names whatever
    did move. Capped at three, because past that the honest summary is a count.
    """
    resolved = order(raw)
    home = {name: index for index, name in enumerate(DEFAULT_ORDER)}
    shifted = [
        (abs(home[name] - position), BY_NAME[name].label)
        for position, name in enumerate(resolved)
        if home[name] != position
    ]
    if not shifted:
        return "the shipped arrangement"
    deliberate = [entry for entry in shifted if entry[0] > 1] or shifted
    deliberate.sort(key=lambda entry: -entry[0])
    labels = [label for _, label in deliberate]
    if len(labels) > 3:
        return ", ".join(labels[:3]) + f" and {len(labels) - 3} more"
    return ", ".join(labels)


def joined(names: Iterable[str]) -> str:
    """A list of names as the stored string, normalized on the way."""
    return clean(SEP.join(names))

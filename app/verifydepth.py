"""How thoroughly a run should verify itself, scaled to what it changed.

Wes called agent spend "a critical issue" on 2026-08-07. The measurement that
followed (app/usage.py, `anatomy` and `turn_trend`) found the cause was not
prompt bloat - the portal's own prompt is ~17% of what a run re-reads, and has
already had two rounds of budgeting. It was run *length*: turns per run up 107%
in a fortnight while weight per turn stayed flat, and cost is `tokens x turns`,
so a longer run is superlinearly more expensive.

Roughly 60% of that length is self-verification. Asked how much of it he wanted
to keep, Wes answered: **"sweep only when logic changed."**

That answer is what this module states, in the prompt, on every build run.

## Why it has to be said out loud

Nothing in the code ever asked for a full 4,000-test suite and a 20-case
mutation sweep on every run. The obligation was *cultural*: the last 20 journal
entries are pasted into every prompt, most of them open with a run reporting its
own sweep, and an agent reading twenty such entries reasonably concludes that is
the standard here. The discipline propagated by imitation, and imitation has no
sense of proportion - a run that fixed a label copied the verification of a run
that rewrote the scheduler.

So the fix is not a checker that scolds afterwards; it is an authoritative line
in the prompt that outranks the journal, and it has to say so explicitly, or the
next twenty entries will out-vote it again. Hence the last paragraph of the
`proportionate` text, which is the load-bearing sentence in this file.

## Why a tier list rather than a number

"Verify proportionately" is advice nobody can act on. The tiers name the change
in terms of the diff an agent is about to commit - docs, tests, code without
decisions, code with decisions - so the question it has to answer is
"which of these is my diff", not "how careful do I feel". `docs/verifying-with
-mutations.md` still holds for the top tier; this only decides *when* to open it.

Deliberately conservative in one direction: the floor is never "run nothing and
claim it works". Every tier below the top still owes the owning tests, and the
two invariants (a green tree before a commit, never report a result you did not
see) hold at every depth. Cutting verification is a spending decision; lying
about verification is a defect, and no setting here can buy one.

## The knob

Three values, because Wes wants the number exposed rather than a policy baked
in - and because his own answer could reasonably be re-tuned once he sees what a
lighter run costs him:

- `proportionate` (default) - the tier list above, his 2026-08-07 answer.
- `thorough` - the old behavior, full suite plus a sweep on every run.
- `light` - the owning tests only, never a sweep. For a stretch where getting
  through the backlog matters more than proving each step.

Only build runs get the section at all. A triage, plan, research, reflect or
compact run writes no code, so telling it how to test would be noise in a prompt
that is already budgeted to the byte.
"""
from __future__ import annotations

import logging

log = logging.getLogger("portal.verifydepth")

SETTING_KEY = "verification_depth"
DEFAULT_DEPTH = "proportionate"
DEPTH_CHOICES: tuple[str, ...] = ("light", "proportionate", "thorough")

# Named for what they do rather than for how hard they try, so the dropdown
# reads as a decision about the work instead of as a dial from 1 to 3.
DEPTH_LABELS: list[tuple[str, str]] = [
    ("light", "Only the tests it touches"),
    ("proportionate", "Scaled to the change"),
    ("thorough", "Full suite and a sweep, always"),
]

# Only tasks that actually write code. `build` is the one project task that
# does; triage/plan/research/reflect/compact are all read-and-write-prose, and a
# one-off runs in a throwaway workspace through its own prompt builder.
CODE_TASKS: frozenset[str] = frozenset({"build"})

_HEADING = "## How much to verify this run"

# Shared by all three depths. The floor, and it is not for sale: the setting
# decides how much proving a change is worth, never whether a claim has to be
# true.
_INVARIANTS = (
    "Two things hold at every depth, and no setting relaxes them: the tree must "
    "be green before you commit, and you must never report a test result you "
    "did not actually watch run."
)

# The sentence this whole module exists for. An agent reads twenty journal
# entries below this line, most of which describe a heavy sweep, and pattern-
# matches on them unless something tells it not to.
_NOT_THE_JOURNAL = (
    "A previous journal entry describing a twenty-case sweep is a record of "
    "what that run needed - it is not an instruction for this one. Do not copy "
    "the verification depth of the last run you read about; derive it from your "
    "own diff."
)

_PROPORTIONATE = (
    "Verifying itself is the largest single thing a run spends - about 60% of "
    "it - so scale it to the change you actually made rather than to the "
    "ceiling. Wes decided this on 2026-08-07, asked directly: **sweep only when "
    "logic changed.**\n\n"
    "Find your diff in this list and do what it says, no more:\n\n"
    "- **Docs, journal, notes, a plan, a screenshot** - nothing is owed. Say in "
    "your report that no test run was needed.\n"
    "- **Tests, fixtures or data only** - run the files you touched.\n"
    "- **Code with no new decision in it** (a string, a label, a constant, a "
    "moved line, a rename) - run the test files that own it.\n"
    "- **Logic** - a new branch, guard, comparison, boundary or default; "
    "anything a flipped operator would break silently - run the owning test "
    "files, then a delete-the-fix mutation sweep over the decision points you "
    "ADDED (see `docs/verifying-with-mutations.md` if this project has it), "
    "then the full suite once before you commit.\n\n"
    f"{_INVARIANTS}\n\n"
    f"{_NOT_THE_JOURNAL}"
)

_THOROUGH = (
    "This project is set to verify thoroughly, whatever the change: run the "
    "full test suite, and prove any code you added with a delete-the-fix "
    "mutation sweep over its decision points (see "
    "`docs/verifying-with-mutations.md` if this project has it). Do it even for "
    "a small diff - the setting says the proof is worth the tokens here.\n\n"
    f"{_INVARIANTS}"
)

_LIGHT = (
    "This project is set to verify lightly right now, because getting through "
    "the work matters more than proving each step of it. Run the test files "
    "that own what you changed and stop there: no full-suite run unless you "
    "have a reason to think you broke something outside your diff, and no "
    "mutation sweep at all.\n\n"
    f"{_INVARIANTS}\n\n"
    f"{_NOT_THE_JOURNAL}"
)

_BODIES: dict[str, str] = {
    "proportionate": _PROPORTIONATE,
    "thorough": _THOROUGH,
    "light": _LIGHT,
}


def current_depth() -> str:
    """The configured depth, falling back to the default on anything odd.

    Never raises: a missing settings row, an unknown value left by an older
    build, or a database that will not open all resolve to `proportionate`. The
    section is guidance, and losing a run over guidance would be absurd.
    """
    try:
        from . import db  # local: db must not import this module

        value = (db.get_setting(SETTING_KEY) or "").strip()
    except Exception:  # pragma: no cover - defensive
        log.exception("Could not read %s; using the default", SETTING_KEY)
        return DEFAULT_DEPTH
    return value if value in DEPTH_CHOICES else DEFAULT_DEPTH


def prompt_section(task: str, depth: str | None = None) -> str:
    """The verification-depth section for a run prompt.

    Empty for every task that writes no code, so a triage or reflect prompt is
    byte-for-byte what it was before this module existed.
    """
    if task not in CODE_TASKS:
        return ""
    chosen = depth if depth in DEPTH_CHOICES else current_depth()
    return f"{_HEADING}\n{_BODIES[chosen]}"

"""American spellings, everywhere, enforced rather than remembered.

The owner has asked for this more than once - most recently 2026-07-28:

    "add some note to a system prompt or something somewhere that I want to
    always use American English spellings rather than British. I want
    'color,' not 'colour.' 'Gray,' not 'grey,' etc. I have seen you use
    'colour' in a few places."

The agent contract already carries the instruction, which stops *new* prose
drifting. This is the other half: a check over the tree itself, so the rule is
a property of the repository rather than a thing each agent has to remember.
It runs over comments, docstrings, templates, stylesheets and test names alike,
because the owner's note said "in code, comments and test names alike".

## Why this is not a `sed`

Three ways a blanket replace does real damage here, all of them found the hard
way:

1. **Stored values.** `"cancelled"` is a run status in the live database and
   the source of a CSS class name (`.run-status-cancelled`). Renaming it is a
   migration, and buys nothing - nobody reads a status key.
2. **Identifiers, including other people's.** `RunResult.cancelled` is a field
   read by name across the worker, and `handle.cancelled()` is asyncio's own
   `Future` API. An earlier sweep renamed the first and turned 23 tests red;
   renaming the second would have been a silent `AttributeError` at runtime.
3. **Text that quotes the rule in order to state it.** The agent contract has
   to be able to say `"color" not "colour"`, and `tests/test_owner.py` asserts
   it does. A sweep that "fixes" those turns the instruction into `"color" not
   "color"` and the guard into a tautology - which is exactly what happened on
   2026-07-28.

So the exemptions below are explicit and each carries its reason. Everything
not exempt is prose, and prose is what the owner actually reads.

Note the words that only *look* British and are correct as they stand:
`analysis`, `emphasis`, `optimistic` and `cancellation` all keep the spelling
they have - the lookaheads in RULES are what keep them out of the report.
"""
from __future__ import annotations

import re
from pathlib import Path

from app import config

ROOT = config.APP_ROOT

# Where the rule applies. `data/` is runtime state, `venv/` is not ours, and
# LICENSE is the AGPL text verbatim - changing a word of it is a licensing
# question, not a spelling one.
SCANNED_DIRS = ("app", "tests", "deploy", "docs")
SCANNED_FILES = ("README.md",)
SUFFIXES = {".py", ".html", ".css", ".js", ".mjs", ".md", ".sh", ".toml", ".txt"}
SKIP_PARTS = {"venv", "node_modules", ".git", "__pycache__", "data", "secrets", "shots"}

# British form -> American form, as a substring rewrite of the whole word, so
# affixes come along for free: `unrecognised` -> `unrecognized`, `greyscale`
# -> `grayscale`, `mislabelled` -> `mislabeled`.
#
# The lookaheads are load-bearing, not decoration. `-ise` verbs take `-ize` in
# American English but the NOUNS `analysis` and `emphasis` are spelled the same
# in both, and `optimistic` merely starts the same way. Matching the bare stem
# would report all three forever, and the only way to get a green suite would
# be to misspell them.
RULES: tuple[tuple[str, str], ...] = (
    ("behaviour", "behavior"),
    ("colouris(?=[eai])", "coloriz"),     # before `colour`, or it leaves `colorise`
    ("colour", "color"),
    ("honour", "honor"),
    ("favour", "favor"),
    ("recognis", "recogniz"),
    ("organis", "organiz"),
    ("normalis", "normaliz"),
    ("initialis", "initializ"),
    ("serialis", "serializ"),
    ("summaris", "summariz"),
    ("customis", "customiz"),
    ("visualis", "visualiz"),
    ("authoris", "authoriz"),
    ("apologis", "apologiz"),
    # Added 2026-07-28 after `generalised` sat in the compaction agent's own
    # guidance in app/agent_runner.py and this suite reported the tree clean.
    # A needle list is only as good as its coverage, so these were taken from
    # the `-ise` verbs a person actually reaches for writing about software.
    ("generalis", "generaliz"),
    ("specialis", "specializ"),
    ("standardis", "standardiz"),
    ("synchronis", "synchroniz"),
    ("categoris", "categoriz"),
    ("prioritis", "prioritiz"),
    ("optimis(?=[eai])", "optimiz"),      # not `optimistic`
    ("utilis(?=[eai])", "utiliz"),
    ("minimis(?=[eai])", "minimiz"),
    ("maximis(?=[eai])", "maximiz"),
    # `-ing` in the lookahead is what separates the verb from the noun:
    # `analysing` takes it, `analysis` does not. A bare `[ei]` matches both and
    # would demand the noun be spelled `analyzis`.
    # Added 2026-08-04, same lesson as the 2026-07-28 batch above: both of
    # these were sitting in the tree while this suite reported it clean.
    # `capitalisation` was in a comment in app/static/app.js and in
    # tests/test_workspace_access.py, `capitalised` four times in
    # app/qdedupe.py, and `dialling` in the settings page's own help text.
    ("capitalis(?=[eai])", "capitaliz"),
    ("emphasis(?=e|ing)", "emphasiz"),    # not the noun `emphasis`
    ("analys(?=e|ing)", "analyz"),        # not the noun `analysis`
    ("practis(?=e|ing)", "practic"),
    # `centred` is `centr` + `ed`, not `centre` + `d`, so the general rule
    # would produce `centerd`. The inflections have to come first, and once
    # one has fired the `centre` rule no longer matches the rewritten word.
    ("centred", "centered"),
    ("centring", "centering"),
    ("centre", "center"),
    ("grey", "gray"),
    # Doubled `l` before a vowel suffix is the British habit; American singles
    # it. `cancellation` keeps both in American English, which is why these
    # stop at `-ed`/`-ing` rather than taking the bare stem.
    ("labell(?=[ei])", "label"),
    ("travell(?=[ei])", "travel"),
    ("modell(?=[ei])", "model"),
    ("signall(?=[ei])", "signal"),
    ("counsell(?=[ei])", "counsel"),
    ("cancell(?=[ei])", "cancel"),
    ("diall(?=[ei])", "dial"),
    ("marvellous", "marvelous"),
    # ...and the reverse: British singles an `l` that American doubles.
    ("fulfil(?!l)", "fulfill"),
    ("enrol(?!l)", "enroll"),
    # Added 2026-07-29: `distil` sat in the /memory compact button's own confirm
    # text and in main.py's docstring for the route behind it, and this suite
    # reported the tree clean. Same family as the two above - British singles an
    # `l` that American doubles - so the rest of that family came with it.
    ("distil(?!l)", "distill"),
    ("instil(?!l)", "instill"),
    ("skilful", "skillful"),
    ("wilful", "willful"),
    ("enthral(?!l)", "enthrall"),
    ("instalment", "installment"),
    ("catalogue", "catalog"),
    ("licence", "license"),
    ("defence", "defense"),
    ("artefact", "artifact"),
    ("programme", "program"),
    ("sceptic", "skeptic"),
    ("manoeuvre", "maneuver"),
    ("smoulder", "smolder"),
    ("whilst", "while"),
)

_WORD = re.compile(
    r"[A-Za-z]*(?:" + "|".join(b for b, _ in RULES) + r")[A-Za-z]*",
    re.IGNORECASE,
)


def americanize(word: str) -> str:
    """The word as it should be spelled. Case of the first letter survives."""
    out = word
    for british, american in RULES:
        out = re.sub(british, american, out, flags=re.IGNORECASE)
    return out[:1].upper() + out[1:] if word[:1].isupper() else out


# --- the exemptions, each with the reason it exists --------------------------

# Names this repository did not choose and cannot change, exempt in any file.
# There is no prose use of either, so matching them anywhere costs nothing.
FOREIGN_NAMES = r"""
      \bCancelledError\b        # asyncio's own exception
    | \.cancelled\(\)           # asyncio Future.cancelled()
    | \bcreateAnalyser\(\)      # the Web Audio API's own spelling
    | \bAnalyserNode\b          # ...and the class that method returns
"""

# `cancelled` as a value or a name rather than as a word. The status string is
# in the live database and is also the source of a CSS class, so renaming it is
# a migration with nothing at the end of it; the field is read by name across
# the worker. Prose on the same line is still reported.
CODE_CANCELLED = r"""
      ["']cancelled["']         # a run status as stored in the database
    | \.cancelled\b             # RunResult.cancelled
    | \bcancelled\s*[:=]        # the dataclass field and the keyword argument
    | -cancelled\b              # the CSS class derived from the status
"""

# A British word inside quotes, as the rule's own counter-example. Only the
# words the instruction actually names, so this cannot become a way to smuggle
# arbitrary British prose past the check by putting quotes round it.
QUOTED_BRITISH = r"""
    ['"](colour|grey|behaviour|recognise|labelled|centre)[\s.,'"]
"""

# (path suffix, a regex the LINE must match, why). A hit is exempt only when
# both halves agree, so an exemption cannot quietly cover a whole file.
EXEMPT: tuple[tuple[str, str, str], ...] = (
    ("", FOREIGN_NAMES, "Python's own API names, which we do not get to spell."),
    ("", r"\brun_cancelled\b", "A persona message key, named after the run status."),
    # The two files that STATE the rule, where a quoted British word is the
    # counter-example rather than the offense. Scoped to a quoted literal so
    # ordinary prose in the same file is still reported - it is, on 11 other
    # lines of test_owner.py.
    (
        "app/agent_runner.py",
        QUOTED_BRITISH,
        "The agent contract has to quote the spelling it bans in order to ban it.",
    ),
    (
        "tests/test_owner.py",
        QUOTED_BRITISH,
        "Asserts the contract still carries those exact counter-examples.",
    ),
    (
        "tests/test_american_english.py",
        r".",
        "This file necessarily spells out every word it is looking for.",
    ),
    *(
        (path, CODE_CANCELLED, "`cancelled` is a stored run status and a field name, not prose.")
        for path in (
            "app/agent_runner.py", "app/worker.py", "app/usage.py", "app/config.py",
            "app/static/style.css", "app/templates/activity.html", "app/preview.py",
            "app/orphans.py", "app/telegram_bot.py", "app/persona.py",
            "tests/test_cancel.py", "tests/test_breakdown.py", "tests/test_oneoffs.py",
            "tests/test_parallel.py", "tests/test_runlimit.py", "tests/test_telegram.py",
            "tests/test_settings_form.py", "tests/test_ui_polish.py",
            "tests/test_restart_survivors.py", "tests/test_stranded_runs.py",
        )
    ),
)

_EXEMPT = tuple(
    (path, re.compile(rx, re.VERBOSE | re.IGNORECASE), why) for path, rx, why in EXEMPT
)


def _is_exempt(rel: str, line: str) -> bool:
    """Note the whole LINE is exempted, not the match - so an exemption written
    for `"cancelled"` also covers a word beside it. That is the safe direction:
    it under-reports on a handful of known lines rather than demanding a rename
    that breaks something."""
    return any(rel.endswith(path) and rx.search(line) for path, rx, _ in _EXEMPT)


def _scanned_paths(root: Path = ROOT) -> list[Path]:
    paths: list[Path] = []
    for name in SCANNED_DIRS:
        paths += sorted(p for p in (root / name).rglob("*") if p.is_file())
    paths += [root / name for name in SCANNED_FILES if (root / name).exists()]
    return [p for p in paths if p.suffix in SUFFIXES and not SKIP_PARTS & set(p.parts)]


def scan(root: Path = ROOT) -> list[tuple[str, int, str, str]]:
    """Every British spelling in the tree that is not exempt.

    Returns (relative path, line number, the word, what it should be).
    """
    hits: list[tuple[str, int, str, str]] = []
    for path in _scanned_paths(root):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if _is_exempt(rel, line):
                continue
            for match in _WORD.finditer(line):
                word = match.group(0)
                fixed = americanize(word)
                if fixed != word:
                    hits.append((rel, number, word, fixed))
    return hits


# --- the check itself --------------------------------------------------------


def test_the_tree_is_written_in_american_english():
    hits = scan()
    listing = "\n".join(
        f"  {rel}:{number}  {word} -> {fixed}" for rel, number, word, fixed in hits[:40]
    )
    more = f"\n  ...and {len(hits) - 40} more" if len(hits) > 40 else ""
    assert not hits, f"{len(hits)} British spelling(s):\n{listing}{more}"


# --- proof the check can fail ------------------------------------------------
#
# A tree-wide scanner that reports zero is indistinguishable from a scanner
# that matches nothing at all, so these pin the machinery rather than the tree.


def test_the_scanner_reports_a_british_spelling(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "deploy").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("The colour of the behaviour.\n")
    (tmp_path / "app" / "x.py").write_text("# a greyed-out label, centred\n")

    found = {(word, fixed) for _, _, word, fixed in scan(tmp_path)}
    assert ("colour", "color") in found
    assert ("behaviour", "behavior") in found
    assert ("greyed", "grayed") in found
    assert ("centred", "centered") in found


def test_words_that_only_look_british_are_left_alone(tmp_path):
    """The lookaheads in RULES, stated as the words they protect.

    Every one of these is spelled identically in American English. A rule
    written as a bare stem would demand they be misspelled to pass.
    """
    for name in ("app", "tests", "deploy", "docs"):
        (tmp_path / name).mkdir()
    (tmp_path / "README.md").write_text(
        "The analysis put emphasis on an optimistic cancellation policy, "
        "and the enrolled devices were fulfilled.\n"
    )
    assert scan(tmp_path) == []


def test_a_stored_run_status_is_not_prose():
    """`cancelled` survives where it is a value or a name, and nowhere else."""
    assert not any(
        word.lower().startswith("cancel") for _, _, word, _ in scan()
    )
    # ...and the exemption is narrow enough that prose still trips it.
    assert not _is_exempt("app/worker.py", "# the run was cancelled by hand")
    assert _is_exempt("app/worker.py", 'db.finish_run(run_id, "cancelled")')
    assert _is_exempt("app/agent_runner.py", "    cancelled: bool = False")
    assert _is_exempt("app/worker.py", "    if result.cancelled:")


def test_an_exemption_covers_only_the_code_on_its_line():
    """The check exempts whole LINES, so prose can ride along beside a status.

    That is not hypothetical. Before this test existed, five lines shaped like

        db.finish_run(run_id, "cancelled", summary="Cancelled from the portal.")

    kept the British spelling in the *summary* - the sentence the owner reads
    on the run - because the status literal beside it carried the exemption.
    The activity page printed `cancelled` as its visible label for the same
    reason. So: strip the code each exemption is actually for, and whatever is
    left on the line has to be American like everything else.
    """
    offenders: list[str] = []
    for path in _scanned_paths(ROOT):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "tests/test_american_english.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not _is_exempt(rel, line):
                continue
            rest = line
            for suffix, rx, _ in _EXEMPT:
                if rel.endswith(suffix):
                    rest = rx.sub(" ", rest)
            left = {m.group(0) for m in _WORD.finditer(rest) if americanize(m.group(0)) != m.group(0)}
            if left:
                offenders.append(f"  {rel}:{number}  {sorted(left)}\n      {line.strip()[:100]}")
    assert not offenders, "British prose on an exempted line:\n" + "\n".join(offenders)


def test_americanize_keeps_affixes_and_capitals():
    assert americanize("unrecognised") == "unrecognized"
    assert americanize("greyscale") == "grayscale"
    assert americanize("mislabelled") == "mislabeled"
    assert americanize("Behaviour") == "Behavior"
    assert americanize("re-enrolment") == "re-enrollment"

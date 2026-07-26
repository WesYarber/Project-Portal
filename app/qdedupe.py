"""Stop the same question being asked twice in two different wordings.

Wes, 2026-07-26:

    I often get multiple questions from projects asking the same thing. I
    should not get the same question asked multiple times in multiple ways.
    Ensure when asking a question that the questions waiting to be answered do
    not ask the same thing.

There are two halves to that, and both are needed:

1. **Prevention.** The run prompt used to carry `## Answered questions` and
   nothing about the ones still *open*, so an agent had no way to know what it
   had already asked - re-asking was the only behaviour available to it.
   `prompt_section()` puts the waiting questions in front of it.
2. **Enforcement.** Prompts are advice; `db.file_question` refuses a near
   duplicate outright, so a run that ignores the advice (or two runs racing on
   the same project) still cannot produce two of the same question.

Everything here was fitted against the live database rather than invented, and
the real corpus killed the obvious design. OpenJournal had **eight open
questions all asking Wes to pick a name** - the exact thing he is complaining
about - and their pairwise word overlap is only 0.34-0.67, because each one
argues the case differently (one leads with the trademark research, one with
the Docker step, one with "it's been open since Friday"). No overlap threshold
separates that set from unrelated questions.

What *is* identical across all eight is the answer space:

    ["Kithlog", "Porchlog", "keep OpenJournal", "I'll think of one"]

So the primary signal is `answer_space()`, not word overlap. A question is the
decision it asks Wes to make, and the one-tap options are that decision with
the argument stripped off - which is why the same eight essays collapse to one
question while two unrelated yes/no questions never do (their options are
generic, so they are not allowed to match on options at all).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# Words that carry no topic. Kept short on purpose: this list only has to stop
# question scaffolding ("do you want me to", "should I") from making every
# question look like every other question, and every word added here is a word
# that can no longer distinguish two questions from each other.
STOPWORDS = frozenset(
    """
    a an and any are as at be been being but by can could did do does doing done
    for from get got had has have how i if in into is it its just like make may
    me might must my need now of on once one only or other our out over own
    please shall should so some such than that the their them then there these
    they this those to too us use used using very want was we were what when
    where whether which while who why will with would you your yours
    """.split()
)

# Answers that say nothing about *which* question they answer. Two questions
# whose only options are these are not shown to be the same by their options -
# "deploy it? [yes] [no]" and "add a leaderboard? [yes] [no]" share an answer
# space and are completely different questions.
GENERIC_ANSWERS = frozenset(
    """
    yes no yeah nope sure ok okay skip later maybe both either neither
    not yet now go ahead hold off leave alone think about later think
    do it don't dont wait stop keep going carry on
    """.split()
)

# Two questions count as the same when their topic words overlap this much.
# Jaccard, not overlap-coefficient: a short question must not be swallowed just
# because its every word appears in a longer one.
#
# 0.6 is where the two real cases sit on opposite sides. "Which colour for the
# header?" vs "...for the footer?" scores 0.33 (different questions that read
# almost identically - which is why this is token-based and not an edit-distance
# ratio, which scores that pair 0.85 and would merge them). "Should I use design
# A or design B?" vs "Which design do you want, A or B?" scores 0.6 - the same
# question in two wordings, which is the thing Wes is complaining about.
THRESHOLD = 0.6

# When two questions offer the *same distinctive answer space*, they only have
# to be about roughly the same subject rather than to read alike. This is the
# bar the eight OpenJournal name questions clear (0.34 at their most different)
# and that unrelated questions sharing a menu do not.
SAME_ANSWERS_FLOOR = 0.3


_URL_RE = re.compile(
    r"(?:https?://)?(?P<host>[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|app|dev|sh|local)"
    # A bare host is only recognised by its port, which the lookahead keeps out
    # of the captured name - a token carrying a digit is dropped downstream, so
    # "myserver:8500" would otherwise vanish entirely instead of collapsing.
    r"|[\w-]+(?=:\d{2,5}))(?::\d{2,5})?(?P<path>[/\w\-.?=&%#]*)",
    re.IGNORECASE,
)


def _collapse_urls(text: str) -> str:
    """An address becomes one token for its host, and its path becomes nothing.

    Two questions about two different features of the same site both say
    "example.com/the-app-dev", and left as words that address is half a
    dozen tokens voting that they are the same question - when it is the one
    thing every question a project asks was always going to have in common.

    The host survives as a single token (and, via `_marks`, as a name), because
    "publish to example.com" and "publish to example.org" really are two
    different decisions.
    """

    def one(match: re.Match) -> str:
        host = re.sub(r"[^a-z0-9]+", "", match.group("host").lower())
        return f" host{host} "

    return _URL_RE.sub(one, text)


def _tokens(text: str) -> frozenset[str]:
    """Topic words of a question, as a set.

    Numbers go entirely rather than becoming a placeholder. The spend-down
    offer differed *only* in its countdown and percentage, and a question is
    almost never distinguished by a bare number - but it is very often
    decorated with one.
    """
    lowered = _collapse_urls((text or "").lower())
    words = re.split(r"[^a-z0-9]+", lowered)
    out = set()
    for word in words:
        if not word or word in STOPWORDS:
            continue
        if any(ch.isdigit() for ch in word):
            continue
        # Crudest possible stemming, and deliberately no more: it only has to
        # make "options"/"option" and "runs"/"run" match. Anything cleverer
        # needs a dictionary to avoid mangling words like "address".
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.add(word)
    return frozenset(out)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _marks(text: str) -> frozenset[str]:
    """The names a question is *about* - capitalised words and `code spans`.

    Overlap alone is not enough for a short question, because two of them can
    share almost every word and still be about different things. The case that
    forced this: the model watcher asks "Opus 6 (`claude-opus-6`) is out. Want
    the portal to move onto it?" and would ask the same sentence about Sonnet 6
    - a 0.67 overlap, above the threshold, and merging them would mean one of
    two real model releases silently never reaching Wes.
    """
    body = text or ""
    # A hostname is a name, and often the only thing separating two otherwise
    # identical questions ("publish to example.com" / "...to example.org").
    marks = {t for t in _tokens(body) if t.startswith("host")}
    for raw in re.findall(r"`([^`]+)`", body):
        marks |= _tokens(raw)
    # A word capitalised because it opens a sentence is capitalised by grammar,
    # not by being a name - "Promote it to production?" is not a question about
    # something called Promote. Dropping those is what keeps two "promote which
    # feature?" questions from looking like they name different subjects.
    for sentence in re.split(r"(?<=[.!?;:])\s+|\n+", body):
        words = re.findall(r"\S+", sentence)
        for word in words[1:]:
            for found in re.findall(r"\b[A-Z][A-Za-z0-9]*\b", word):
                marks |= _tokens(found)
    return frozenset(marks)


# A question long enough to be an argument rather than an ask. Above this, the
# capitalised words are incidental evidence ("American Eagle sued Amazon in
# 2024") rather than the subject, and vetoing on them is what let the eight
# OpenJournal name questions through in the first place.
SHORT_QUESTION_TOKENS = 40


def _about_different_things(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if len(ta) > SHORT_QUESTION_TOKENS or len(tb) > SHORT_QUESTION_TOKENS:
        return False
    ma, mb = _marks(a), _marks(b)
    # Each names something the other does not. A pure subset ("adopt
    # `claude-opus-6`?" vs "adopt the new opus?") is not a disagreement - a
    # rewording is allowed to drop a name, just not to swap one.
    return bool(ma - mb) and bool(mb - ma)


def parse_options(raw: Any) -> list[str]:
    """The one-tap options of a question, however they were stored.

    Never raises: a question with unreadable options is simply a question with
    no options, which costs a signal rather than a run.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        import json

        try:
            loaded = json.loads(text)
        except ValueError:
            return []
        return [str(x) for x in loaded] if isinstance(loaded, list) else []
    return [part for part in re.split(r"[|\n]", text) if part.strip()]


def answer_space(raw: Any, text: str = "") -> frozenset[str]:
    """The answers that *name* what is being decided.

    An option only counts when it names something the question itself names -
    "Kithlog", "Porchlog", "keep OpenJournal" against a body that mentions
    Kithlog, Porchlog and OpenJournal. That is what separates a menu which
    identifies a decision from one that merely describes its shape.

    The pair that forced the rule, both from the live database:

        promote the guided table tour?   [promote it to production] [keep it dev-only]
        promote the Learn-to-play section? [promote it to production] [keep it dev-only]

    Two different features, one reusable menu of verbs. Treating that menu as
    identifying would have meant Wes never being asked about the second one.
    Verbs of action recur across every deployment question a project ever asks;
    the name of the thing does not.
    """
    space = set()
    for option in parse_options(raw):
        tokens = {t for t in _tokens(option) if t not in GENERIC_ANSWERS}
        # Capitalised *in the option itself*. An answer that names a thing
        # spells the thing with a capital - "Kithlog", "keep OpenJournal" - and
        # an answer that describes an action does not: "promote it to
        # production", "keep it dev-only", "adopt it", "not yet". Every menu in
        # the live database follows that split, and the agent contract asks for
        # options phrased as the answer itself ("merge it", "keep both"), which
        # is the lower-case form.
        named = {t for t in _tokens(" ".join(re.findall(r"\b[A-Z][A-Za-z0-9]*\b", option)))}
        if tokens and named - GENERIC_ANSWERS:
            space.add(" ".join(sorted(tokens)))
    return frozenset(space)


def _distinctive(space: frozenset[str]) -> bool:
    """Enough of a menu to identify a question by. Two is the floor: a single
    surviving option is usually one named thing next to a "no", which two
    unrelated questions can easily share."""
    return len(space) >= 2


def similarity(a: str, b: str, a_options: Any = None, b_options: Any = None) -> float:
    """0.0-1.0 likeness of two questions, options included when they have them."""
    ta, tb = _tokens(a), _tokens(b)
    topic = _jaccard(ta, tb)

    sa, sb = answer_space(a_options, a), answer_space(b_options, b)
    if _distinctive(sa) and _distinctive(sb):
        options = _jaccard(sa, sb)
        # The same menu of real answers, asked about roughly the same subject.
        # No mark veto on this path: when the two answer spaces are literally
        # the same choice, the proper nouns scattered through the argument are
        # not what the question is about.
        if options >= THRESHOLD and topic >= SAME_ANSWERS_FLOOR:
            return max(topic, options)
        # A different menu of real answers is positive evidence of a different
        # decision, so it also *blocks* the wording path below.
        if options < SAME_ANSWERS_FLOOR:
            return 0.0

    if _about_different_things(a, b):
        return 0.0
    if not ta or not tb:
        # Nothing to compare on. Identical scaffolding ("Should I?" twice) is
        # still worth catching, so fall back to the normalised strings.
        na = re.sub(r"\W+", " ", (a or "").lower()).strip()
        nb = re.sub(r"\W+", " ", (b or "").lower()).strip()
        return 1.0 if na and na == nb else 0.0
    return topic


def _get(row: Any, field: str) -> Any:
    try:
        return row[field]
    except (TypeError, KeyError, IndexError):
        return getattr(row, field, None)


def find_duplicate(text: str, rows: Iterable[Any], options: Any = None) -> Optional[Any]:
    """The most similar of `rows` that is close enough to be the same question.

    Returns the *best* match rather than the first, so that when a question has
    drifted through several near-wordings the new one is merged into whichever
    it actually restates.
    """
    best, best_score = None, 0.0
    for row in rows:
        score = similarity(
            text, _get(row, "question") or "", options, _get(row, "quick_options")
        )
        if score >= THRESHOLD and score > best_score:
            best, best_score = row, score
    return best


def prompt_section(project_id: int) -> str:
    """The questions already waiting, for the run prompt.

    Empty string when there are none, so a project with a clear board does not
    carry a heading saying so - the instruction only makes sense next to a list.
    """
    from . import db  # local: db imports nothing from here, keep it that way

    rows = db.open_questions(project_id)
    if not rows:
        return ""
    lines = "\n".join(f"- {_get(row, 'question') or ''}" for row in rows)
    return (
        "## Questions already waiting for an answer\n"
        "These are yours, from earlier runs, and they have not been answered yet. "
        "Do NOT ask any of them again in different words - a repeat is not a "
        "reminder, it is a second thing to read and dismiss. Ask only what is "
        "genuinely not on this list; if one of these is now blocking you harder "
        "than when you filed it, say so in your journal entry instead.\n"
        f"{lines}"
    )

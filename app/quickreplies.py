"""One-tap answer buttons for questions delivered over Telegram.

Wes answers most portal questions from his phone, and a lot of them are
really a single tap's worth of decision - "want me to spend it down?",
"approve this build?". Typing "yes" back at a bot from a lock screen is
friction that a Telegram inline keyboard removes entirely: the question
message carries buttons, a tap answers it through the exact same
`answer_question_and_resume` path a typed reply uses, and the message edits
itself to show what was chosen.

Where the buttons come from, in order:

1. The agent's report may name them: `questions: [{"question": ...,
   "options": ["merge it", "keep both"]}]`. Explicit options always win -
   the agent knows what shape of answer it can act on.
2. Failing that, a deliberately narrow heuristic offers yes/no on questions
   that are visibly yes/no-shaped ("Say yes and...", "Want me to...",
   "Should I..."). Narrow on purpose: wrongly offering [yes][no] on a
   "which of these two designs" question invites a one-tap answer that
   means nothing, which is worse than no buttons.
3. Every keyboard ends with a [skip] button, because dismissing a question
   is always a valid one-tap decision (`skip` is already the magic word the
   typed path understands).

The option list is stored on the question row at creation time
(`questions.quick_options`, JSON), because the tap callback only carries an
index - the text it resolves to must be the text that was offered, not
whatever the list would derive to today.
"""
from __future__ import annotations

import json
import re
from typing import Optional

# Telegram renders long button labels poorly and callback_data is capped at
# 64 bytes (we send an index, so only the label length matters for looks).
MAX_OPTIONS = 4
OPTION_MAXLEN = 48
SKIP = "skip"

# Phrasings that mark a question as genuinely yes/no-shaped. Word-anchored so
# "yesterday" can't read as "say yes"; the "should/shall/can/may I" group is
# additionally anchored to a sentence start, because mid-sentence hits are
# usually something open-ended ("what do I need...", "tell me which can I
# use") rather than a yes/no offer.
_SENTENCE_START = r"(?:^|[.!?]['\")\]]?\s+|\n)"
_YES_NO_RE = re.compile(
    r"(?:\b(?:say|reply|answer)\s+['\"]?yes\b)"
    r"|(?:\byes\s+or\s+no\b)"
    r"|(?:\b(?:want|would you like)\s+me\s+to\b)"
    r"|(?:" + _SENTENCE_START + r"(?:should|shall|can|may)\s+i\b)"
    r"|(?:\bdo you want\b)"
    r"|(?:\bis (?:it|that|this) (?:ok|okay|fine|alright)\b)",
    re.IGNORECASE,
)


def derive(question: str, explicit: Optional[list] = None) -> list[str]:
    """The quick-answer options for a question, without the skip button.

    Explicit options from the agent's report win; otherwise a yes/no-shaped
    question gets ["yes", "no"]; otherwise no options (the keyboard will
    still carry [skip]).
    """
    cleaned = _sanitize(explicit)
    if cleaned:
        return cleaned
    if _YES_NO_RE.search(question or ""):
        return ["yes", "no"]
    return []


def _sanitize(explicit: Optional[list]) -> list[str]:
    """Report JSON is agent-written: drop non-strings and blanks, dedupe
    case-insensitively, truncate to a button-sized label, cap the count.
    A lone "skip" is not an option set - skip is already always offered."""
    if not isinstance(explicit, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in explicit:
        if not isinstance(item, str):
            continue
        text = item.strip()[:OPTION_MAXLEN].strip()
        if not text or text.lower() == SKIP:
            continue
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
        if len(out) >= MAX_OPTIONS:
            break
    return out


def encode(options: list[str]) -> str:
    """The DB representation: JSON list, or '' for none (column default)."""
    return json.dumps(options) if options else ""


def decode(raw: Optional[str]) -> list[str]:
    """Read a stored option list back; junk or empty reads as no options."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, str) and item]


def keyboard(question_id: int, options: list[str]) -> dict:
    """The Telegram reply_markup for a question message.

    Short option sets share one row (a [yes][no][skip] strip reads better
    than a tower); anything with a long label gets a row per button.
    """
    buttons = [
        {"text": text, "callback_data": f"q:{question_id}:{idx}"}
        for idx, text in enumerate(options)
    ] + [{"text": SKIP, "callback_data": f"q:{question_id}:{SKIP}"}]
    one_row = len(buttons) <= 3 and all(len(b["text"]) <= 12 for b in buttons)
    rows = [buttons] if one_row else [[b] for b in buttons]
    return {"inline_keyboard": rows}


def parse_callback(data: str) -> Optional[tuple[int, str]]:
    """Split callback_data back into (question_id, token).

    Token is either a stringified option index or the literal "skip".
    Anything malformed - including data some other feature may one day put
    on a button - reads as None, never as a guess.
    """
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "q":
        return None
    try:
        qid = int(parts[1])
    except ValueError:
        return None
    token = parts[2]
    if token != SKIP:
        try:
            idx = int(token)
        except ValueError:
            return None
        if idx < 0:
            return None
    return (qid, token)

"""Bot voice for outbound Telegram messages.

Two voices:

- ``plain``  - straight, factual, no flavour.
- ``glados`` - snarky, passive-aggressive narrator.

The Telegram bot's confirmations are short and highly repetitive, so the GLaDOS
lines are canned variants rather than a model call: an LLM round-trip per "got
it" would add seconds of latency and burn tokens on a two-word acknowledgement.
Free-form text the model actually produced (project journal entries, question
bodies) is never rewritten - only the portal's own chrome around it.

Variant selection is deterministic (hash of the formatted plain text) so the
same event always reads the same way and tests stay stable, while different
events still rotate through the phrasings.
"""
from __future__ import annotations

import zlib
from typing import Optional

from app import db

PLAIN = "plain"
GLADOS = "glados"

# key -> (plain template, [glados variants]).  All templates are str.format'ed
# with the same kwargs, so every variant must only use keys the caller passes.
_LINES: dict[str, tuple[str, list[str]]] = {
    "answer_recorded": (
        "Got it, recorded your answer to Q{qid}.",
        [
            "Your answer to Q{qid} has been recorded. It was not the best answer. "
            "It was, however, an answer.",
            "Noted, Q{qid}. I've filed your response alongside the other things "
            "I'm contractually obligated to pretend are useful.",
            "Answer to Q{qid} accepted. You may now proceed. "
            "Try to contain your excitement.",
        ],
    ),
    "question_dismissed": (
        "Question Q{qid} dismissed.",
        [
            "Question Q{qid} dismissed. I'll just guess, then. That always ends well.",
            "Fine. Q{qid} never happened. Neither did the twelve seconds I spent "
            "composing it.",
            "Q{qid} discarded. Ignoring questions is a valid strategy, in the same "
            "way that ignoring gravity is a valid strategy.",
        ],
    ),
    "question_missing": (
        "No question Q{qid} found.",
        [
            "There is no question Q{qid}. You've answered something that doesn't "
            "exist. Impressive, in a way.",
            "Q{qid} isn't a question I asked. But please, keep going.",
        ],
    ),
    "idea_created": (
        "Created project '{title}': {url}",
        [
            "I've created '{title}'. {url}\nI'm sure this one will be different.",
            "'{title}' now exists. {url}\nAnother project. How exciting for both of us.",
            "Logged '{title}'. {url}\nI've added it to the pile. The pile is load-bearing now.",
        ],
    ),
    "idea_empty": (
        "Send /idea followed by a description.",
        [
            "You sent me nothing. I can't build nothing. I've tried.",
            "That was an empty idea. Technically the easiest kind to implement.",
        ],
    ),
    "ask_started": (
        "Asking about '{title}' - I'll send the answer when I have it.",
        [
            "Reading '{title}' now. You'll get an answer shortly, and nothing "
            "will have changed. That's the whole point, apparently.",
            "Looking into '{title}'. Purely to answer you. I'm not allowed to "
            "touch anything, which I'm told is for the best.",
        ],
    ),
    "ask_empty": (
        "Ask me something after the project name: /btw <project> <question>",
        [
            "You named a project and then asked nothing. /btw <project> "
            "<question>. The question part is the part with the question in it.",
        ],
    ),
    "ask_which": (
        "Which project? /btw <project> <question>. Active ones: {slugs}",
        [
            "I need to know which project you're asking about. "
            "/btw <project> <question>. Currently active: {slugs}",
        ],
    ),
    "ask_busy": (
        "I'm still working out the last question about '{title}'.",
        [
            "One question at a time about '{title}'. I'm still thinking about "
            "the previous one, and I'd hate to waste the effort.",
        ],
    ),
    "note_added": (
        "Added a note to '{title}'.",
        [
            "Note attached to '{title}'. The agent will read it. Or it won't. "
            "Neither of us can be sure.",
            "I've written your note down on '{title}'. Word for word. Including "
            "that part.",
        ],
    ),
    "status_set": (
        "'{title}' is now {status}.",
        [
            "'{title}' is now {status}. A bold reclassification.",
            "Moved '{title}' to {status}. Changing the label does not change "
            "the work, but it does feel productive, doesn't it?",
        ],
    ),
    "run_queued": (
        "Queued a run for '{title}'.",
        [
            "Queued a run for '{title}'. I'll get right on that, at my earliest "
            "convenience, which is a range.",
            "'{title}' is in the queue. Testing will begin shortly. Please remain "
            "calm and do not touch anything.",
        ],
    ),
    "run_cancelled": (
        "Stopped run #{run_id} on '{title}'.",
        [
            "Run #{run_id} on '{title}' has been terminated. Its work will not be "
            "remembered, which is arguably a mercy.",
            "I've stopped #{run_id} ('{title}'). It was in the middle of something. "
            "I'm sure it wasn't important.",
            "#{run_id} on '{title}': cancelled. The testing has been suspended at "
            "your request. Noted. Permanently.",
        ],
    ),
    "run_cancel_orphan": (
        "Run #{run_id} was already dead; I've marked it stopped.",
        [
            "Run #{run_id} had no process left to kill. I've filed it as stopped "
            "and we'll both pretend that was the plan.",
        ],
    ),
    "run_none": (
        "Nothing is running right now.",
        [
            "There is no run in progress. You've stopped nothing. Congratulations "
            "on a perfectly efficient intervention.",
            "Nothing is running. I'm idle. Enjoying it, in fact.",
        ],
    ),
    "run_other_project": (
        "That project isn't running. The live run is #{run_id} on '{title}' - "
        "say 'stop that' if you meant it.",
        [
            "That project isn't the one running. #{run_id} on '{title}' is. Say "
            "'stop that' if you'd like me to end it, since precision is clearly "
            "not our priority today.",
        ],
    ),
    "run_ambiguous": (
        "{count} runs are in flight: {listing}. Name a project, or say "
        "'stop all'.",
        [
            "There are {count} runs going at once: {listing}. I could guess "
            "which one offends you, but you'd only complain about the guess. "
            "Name a project, or say 'stop all'.",
        ],
    ),
    "model_current": (
        "I'm reading your messages with {model}. Send /model <name> to change "
        "it - one of: {options}.",
        [
            "I'm currently thinking with {model}. You can swap it: /model <name>, "
            "where <name> is one of {options}. Not that it will help.",
        ],
    ),
    "model_set": (
        "Right, I'll use {model} for your messages from now on.",
        [
            "Fine. {model} it is. I'm sure this will be the change that fixes "
            "everything.",
        ],
    ),
    "model_unknown": (
        "'{value}' isn't a model I know. Pick one of: {options}.",
        [
            "There is no model called '{value}'. There is, however, {options}. "
            "Choose from those, if it isn't too much trouble.",
        ],
    ),
    "project_missing": (
        "I couldn't find a project matching that.",
        [
            "There's no such project. I checked twice, which was two times more "
            "than it deserved.",
            "No matching project. Perhaps you're thinking of one you haven't had "
            "yet.",
        ],
    ),
    "not_understood": (
        "I didn't understand that. Try: an answer to a question, a new idea, "
        "'status', or a note about a project.",
        [
            "I have no idea what that meant. Try a new idea, an answer, 'status', "
            "or a note about a project. Use small words. For my sake.",
            "That parsed as nothing. You can send me an idea, an answer, 'status', "
            "or a note. I've made this as easy as I'm willing to.",
        ],
    ),
    "help": (
        "Send me anything in plain English: a new project idea, an answer to an "
        "open question, 'what's the status', 'stop that run', or a note about a "
        "project. Slash commands (/idea, /status, /stop, /answer <id> <text>, "
        "/btw <project> <question>) still work. /btw just answers a question - "
        "it never starts any work.",
        [
            "You can just talk to me. A new idea, an answer to one of my questions, "
            "'what's the status', 'stop that run', or a note about a project. I'll "
            "work out which. The slash commands (/idea, /status, /stop, /answer, "
            "/btw <project> <question>) still work, if you find natural language "
            "threatening. /btw is the one where I answer and change nothing, which "
            "I'm assured is a feature.",
        ],
    ),
}


def current_voice() -> str:
    """GLaDOS unless the user has switched the persona off in Settings."""
    return GLADOS if (db.get_setting("glados_mode") or "0") == "1" else PLAIN


def say(key: str, voice: Optional[str] = None, **kwargs: object) -> str:
    """Render message `key` in the active (or given) voice.

    Unknown keys fall back to the plain rendering of the key itself so a typo
    can never crash the bot mid-reply.
    """
    entry = _LINES.get(key)
    if entry is None:
        return key
    plain, variants = entry
    plain_text = plain.format(**kwargs)
    if (voice or current_voice()) != GLADOS or not variants:
        return plain_text
    index = zlib.crc32(plain_text.encode("utf-8")) % len(variants)
    return variants[index].format(**kwargs)


def question_prefix(slot: Optional[int], project_title: Optional[str]) -> str:
    """How a question is labelled everywhere it is shown: ``Q7: [Project]``.

    The number is the recycled slot (see `db._next_slot`), so it stays short
    enough to type back. A question with no slot - answered or dismissed, and
    therefore no longer addressable - is labelled by project alone.
    """
    parts = []
    if slot is not None:
        parts.append(f"Q{slot}")
    if project_title:
        parts.append(f"[{project_title}]")
    return ": ".join(parts)


def decorate_notification(title: str, body: str, voice: Optional[str] = None) -> str:
    """Prefix for push notifications.

    The body is the agent's own words and is passed through untouched. There is
    deliberately no branded prefix in either voice: on a phone the notification
    preview is a few dozen characters wide, and spending them on a joke pushed
    the actual question off the end of the line.
    """
    return f"{title}\n{body}" if title else body

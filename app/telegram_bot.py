"""Telegram long-poll bot.

Message handling is two-layered:

1. Explicit forms are matched first and never hit the model - replying to a
   question message, `#<qid> <answer>`, `/answer <qid> <text>`, `/idea`,
   `/status`, `/help`. These are fast and deterministic.
2. Anything else goes to `nl.classify`, which works out what Wes meant
   (an answer, a new idea, a note on a project, a status request, ...). If the
   NL router is disabled or unavailable, plain text falls back to the v1
   behavior of "treat it as a new idea".

All outbound text goes through `persona.say` so the GLaDOS voice can be
toggled off in Settings.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from typing import Optional

import httpx

from app import ask, config, db, nl, notify, people, persona, quickreplies, routing, worker

log = logging.getLogger("portal.telegram")

POLL_TIMEOUT = 50
ANSWER_RE = re.compile(r"^/answer\s+[#qQ]?(\d+)\s+(.+)$", re.IGNORECASE | re.DOTALL)
# `#7 the answer` and `Q7 the answer` both work - the notification says "Q7",
# and the older messages said "#7".
HASH_RE = re.compile(r"^[#qQ](\d+)[:.\s]\s*(.*)$", re.DOTALL)
# Confidence below this is treated as "I don't know what you meant".
MIN_CONFIDENCE = 0.4


async def telegram_poll_loop() -> None:
    offset: Optional[int] = None
    log.info("Telegram poller started")
    async with httpx.AsyncClient(timeout=POLL_TIMEOUT + 10) as client:
        while True:
            if not db.telegram_enabled():
                # Switched off, or no token configured yet. Re-checked rather
                # than exited so ticking the box in Settings starts the bot
                # without a restart - and, more importantly, so turning it
                # *off* stops it within 15s rather than leaving a poller
                # answering questions that no longer carry a number.
                await asyncio.sleep(15)
                continue
            token = db.get_setting("telegram_token") or ""
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"timeout": POLL_TIMEOUT, **({"offset": offset} if offset is not None else {})},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram poll failed: %s", exc)
                await asyncio.sleep(10)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    await _handle_update(update)
                except Exception:  # noqa: BLE001
                    log.exception("Failed handling telegram update")


async def _handle_update(update: dict) -> None:
    callback = update.get("callback_query")
    if callback is not None:
        await _handle_callback(callback)
        return

    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return

    if not (db.get_setting("telegram_chat_id") or ""):
        db.set_setting("telegram_chat_id", chat_id)
        log.info("Adopted Telegram chat_id %s", chat_id)
    elif chat_id not in routing.telegram_allowlist():
        # Only the install's chat, or a chat somebody has claimed on their own
        # row in settings, may talk to the portal. It was the former alone
        # until 2026-07-28, which meant a second person's phone was silently
        # refused however carefully the rest of the portal had learned to tell
        # the two of them apart.
        log.warning("Ignoring Telegram message from unknown chat %s", chat_id)
        return

    # --- Layer 1: explicit forms ------------------------------------------
    reply_to = message.get("reply_to_message")
    if reply_to is not None:
        qid = db.question_for_telegram_message(chat_id, reply_to.get("message_id"))
        if qid is not None:
            await _answer_question(qid, text, chat_id, by_id=True)
            return

    m = ANSWER_RE.match(text)
    if m:
        await _answer_question(int(m.group(1)), m.group(2).strip(), chat_id)
        return

    m = HASH_RE.match(text)
    if m and m.group(2).strip():
        await _answer_question(int(m.group(1)), m.group(2).strip(), chat_id)
        return

    lowered = text.lower()
    if lowered == "/status":
        await notify.send_telegram_text(chat_id, _status_summary())
        return

    if lowered in ("/stop", "/cancel"):
        # Deliberately layer 1: stopping a run is the one thing you want to
        # still work when the NL router is disabled, slow or unavailable.
        await _cancel_active_run(chat_id)
        return

    if lowered in ("/help", "/start"):
        await notify.send_telegram_text(chat_id, persona.say("help"))
        return

    if lowered == "/model" or lowered.startswith("/model "):
        await _handle_model(text[len("/model"):].strip(), chat_id)
        return

    if lowered.startswith("/idea"):
        await _create_idea(text[len("/idea"):].strip(), chat_id)
        return

    # `/btw <project> <question>` - Claude Code's own gesture for "a question,
    # not a job". Deliberately layer 1: the whole value of an ask is that it
    # cannot set anything in motion, so it must not depend on the NL router
    # guessing right.
    for prefix in ("/btw", "/ask"):
        if lowered == prefix or lowered.startswith(prefix + " "):
            await _handle_btw(text[len(prefix):].strip(), chat_id)
            return

    if text.startswith("/"):
        await notify.send_telegram_text(chat_id, persona.say("help"))
        return

    # --- Layer 2: natural language ----------------------------------------
    if not nl.is_enabled():
        await _create_idea(text, chat_id)
        return

    intent = await nl.classify(text)
    await _dispatch_intent(intent, text, chat_id)


async def _handle_callback(callback: dict) -> None:
    """A tap on one of a question message's inline buttons.

    Routes through the exact same answer/dismiss paths a typed reply uses,
    then edits the message in place - the keyboard disappears (so a second
    tap can't double-answer) and the choice is written into the message, so
    the chat history still reads correctly a month later. Every tap gets an
    answerCallbackQuery, because an unacknowledged tap leaves the Telegram
    client showing a spinner until it times out.
    """
    callback_id = str(callback.get("id", ""))

    async def ack(text: str = "") -> None:
        payload: dict = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        await notify.telegram_call("answerCallbackQuery", payload)

    message = callback.get("message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    if not chat_id or chat_id not in routing.telegram_allowlist():
        # Buttons only exist on messages we sent to a chat we know, so a
        # mismatch is either a forward or a stranger - ignore, but still ack.
        log.warning("Ignoring Telegram callback from unknown chat %s", chat_id or "?")
        await ack()
        return

    parsed = quickreplies.parse_callback(str(callback.get("data") or ""))
    if parsed is None:
        await ack()
        return
    question_id, token = parsed

    question = db.get_question(question_id)
    if question is None or question["status"] != "open":
        # Already answered (web, typed reply, or an earlier tap that edited
        # the message late) - tell the toast, and clear any leftover buttons.
        await ack("Already handled.")
        await _strip_buttons(message)
        return

    if token == quickreplies.SKIP:
        db.dismiss_question_and_resume(question_id)
        await ack("Skipped.")
        await _settle_or_mark(question_id, message, "skipped")
        return

    options = quickreplies.decode(question["quick_options"])
    idx = int(token)
    if idx >= len(options):
        # A stale button from options that no longer decode - never guess.
        await ack("That button no longer maps to an answer - type a reply instead.")
        return
    answer_text = options[idx]
    db.answer_question_and_resume(question_id, answer_text, person_id=_person_id(chat_id))
    await ack(f"Recorded: {answer_text}")
    await _settle_or_mark(question_id, message, f"answered: {answer_text}")
    # A tap from the lock screen is an answer like any other, so it starts a run
    # like any other - see worker.answer_arrived.
    await worker.answer_arrived(question)


async def _settle_or_mark(question_id: int, message: dict, verdict: str) -> None:
    """Settle every chat's copy of the question; for one sent before the
    copies table existed, fall back to editing the message the tap rode in
    on - the one copy that certainly exists."""
    if await notify.settle_question_copies(question_id, verdict) == 0:
        await _mark_answered(message, verdict)


async def _mark_answered(message: dict, verdict: str) -> None:
    """Rewrite the question message to carry the outcome and drop the
    keyboard. Best-effort: the answer is already recorded either way."""
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    original = message.get("text") or ""
    if chat_id is None or message_id is None or not original:
        await _strip_buttons(message)
        return
    await notify.telegram_call(
        "editMessageText",
        {"chat_id": chat_id, "message_id": message_id, "text": f"{original}\n\n[{verdict}]"},
    )


async def _strip_buttons(message: dict) -> None:
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        return
    await notify.telegram_call(
        "editMessageReplyMarkup",
        {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
    )


async def _dispatch_intent(intent: dict, original_text: str, chat_id: str) -> None:
    """Execute a classified intent. Low-confidence or unresolvable intents fall
    back to creating an idea, which is the least destructive outcome and matches
    what most unclassifiable messages actually are."""
    name = intent.get("intent", "unknown")
    if name == "unknown" or intent.get("confidence", 0.0) < MIN_CONFIDENCE:
        await _create_idea(original_text, chat_id)
        return

    if name == "answer":
        # The router is given real row ids, so this must not be read as a slot.
        await _answer_question(
            int(intent["question_id"]), intent.get("text") or original_text, chat_id, by_id=True
        )
        return

    if name == "idea":
        await _create_idea(intent.get("text") or original_text, chat_id)
        return

    if name == "status":
        await notify.send_telegram_text(chat_id, _status_summary())
        return

    if name == "help":
        await notify.send_telegram_text(chat_id, persona.say("help"))
        return

    if name == "cancel":
        # A cancel targets whatever is live, so an unresolved slug is fine here
        # - unlike note/set_status/run, which parse_intent rejects without one.
        await _cancel_active_run(chat_id, intent.get("project_slug"))
        return

    project = db.get_project_by_slug(intent.get("project_slug") or "")
    if project is None:
        await notify.send_telegram_text(chat_id, persona.say("project_missing"))
        return

    if name == "note":
        db.add_journal(
            project["id"], "user", "note", intent.get("text") or original_text,
            person_id=_person_id(chat_id),
        )
        # Same rule as the web note box: a note on a put-down project wakes it
        # up and puts an agent on it.
        await worker.reactivate_on_note(project)
        await notify.send_telegram_text(chat_id, persona.say("note_added", title=project["title"]))
        return

    if name == "ask":
        await _start_ask(project, intent.get("text") or original_text, chat_id)
        return

    if name == "set_status":
        # The same shared writer as the picker and the dashboard drag, so
        # "start building X" over Telegram approves the build and "pause X" is
        # a real pause, exactly like the web UI.
        new_state = intent["status"]
        db.set_user_state(project, new_state, via=" via Telegram")
        await notify.send_telegram_text(
            chat_id, persona.say("status_set", title=project["title"], status=new_state)
        )
        return

    if name == "run":
        # The same gesture as the web button: the run starts now and the
        # project is active again from whatever shelf it was on.
        await worker.run_now(project, via=" via Telegram")
        await notify.send_telegram_text(chat_id, persona.say("run_queued", title=project["title"]))
        return

    await _create_idea(original_text, chat_id)


async def _cancel_active_run(chat_id: str, project_slug: Optional[str] = None) -> None:
    """Stop the run currently in flight.

    If Wes named a project, it must be one that is actually running - killing a
    different run than the one he asked about would be a nasty surprise from a
    phone, so that case reports what is really live instead.

    Runs are parallel now, so a bare "stop" with several in flight is genuinely
    ambiguous and is answered with the list rather than by picking one.
    """
    runs = db.active_runs()
    if not runs:
        await notify.send_telegram_text(chat_id, persona.say("run_none"))
        return

    if project_slug:
        mine = [r for r in runs if (r["project_slug"] or "") == project_slug]
        if not mine:
            newest = runs[0]
            await notify.send_telegram_text(
                chat_id,
                persona.say(
                    "run_other_project",
                    run_id=newest["id"],
                    title=newest["project_title"] or "memory / reflect",
                ),
            )
            return
        run = mine[0]
    elif len(runs) > 1:
        listing = ", ".join(
            f"#{r['id']} {r['task']} on '{r['project_title'] or 'memory / reflect'}'"
            for r in runs
        )
        await notify.send_telegram_text(
            chat_id, persona.say("run_ambiguous", count=len(runs), listing=listing)
        )
        return
    else:
        run = runs[0]

    title = run["project_title"] or "memory / reflect"

    outcome = worker.cancel_run(int(run["id"]))
    if outcome == "orphaned":
        await notify.send_telegram_text(
            chat_id, persona.say("run_cancel_orphan", run_id=run["id"], title=title)
        )
    elif outcome == "cancelled":
        await notify.send_telegram_text(
            chat_id, persona.say("run_cancelled", run_id=run["id"], title=title)
        )
    else:
        # It finished between the lookup and the kill - nothing to stop.
        await notify.send_telegram_text(chat_id, persona.say("run_none"))


def _person_id(chat_id: str) -> Optional[int]:
    """Who is typing, if the portal can say - the person who has claimed this
    Telegram chat on their own row in settings, else None.

    None is a real answer and is left alone. The install's own
    `telegram_chat_id` is not treated as the owner's even though on a
    single-person portal it plainly is: whoever pasted it in has a person row
    they can paste it into as well, and the alternative is a rule that quietly
    stops being true the day a second person arrives. See
    `people.by_telegram_chat_id`.
    """
    person = people.by_telegram_chat_id(chat_id)
    return int(person["id"]) if person is not None else None


async def _answer_question(ref: int, answer_text: str, chat_id: str, by_id: bool = False) -> None:
    """Answer the question Wes referred to.

    `ref` is whatever number he typed, which is normally a *slot* ("Q7") rather
    than a row id - see `db.resolve_question`. `by_id=True` is for callers that
    already hold a real row id (a Telegram reply, the NL router) and must not be
    re-interpreted as a slot.
    """
    question = db.get_question(ref) if by_id else db.resolve_question(ref)
    if question is None:
        await notify.send_telegram_text(chat_id, persona.say("question_missing", qid=ref))
        return
    # Echo back the number he used, not the internal id.
    shown = question["slot"] if question["slot"] is not None else question["id"]
    question_id = int(question["id"])
    if answer_text.lower() == "skip":
        db.dismiss_question_and_resume(question_id)
        await notify.send_telegram_text(chat_id, persona.say("question_dismissed", qid=shown))
        await notify.settle_question_copies(question_id, "skipped")
        return
    db.answer_question_and_resume(question_id, answer_text, person_id=_person_id(chat_id))
    await notify.send_telegram_text(chat_id, persona.say("answer_recorded", qid=shown))
    await notify.settle_question_copies(question_id, f"answered: {answer_text}")
    # A typed reply is an answer like any other - see worker.answer_arrived.
    await worker.answer_arrived(question)


async def _handle_model(arg: str, chat_id: str) -> None:
    """`/model` reports which model is reading Telegram messages; `/model
    <name>` switches it. This is the router model only - the agent that does
    the actual work keeps its own setting."""
    options = ", ".join(config.MODEL_VALUES)
    if not arg:
        await notify.send_telegram_text(
            chat_id, persona.say("model_current", model=nl.router_model(), options=options)
        )
        return
    chosen = nl.set_router_model(arg)
    if chosen is None:
        await notify.send_telegram_text(
            chat_id, persona.say("model_unknown", value=arg, options=options)
        )
        return
    await notify.send_telegram_text(chat_id, persona.say("model_set", model=chosen))


def resolve_project_token(token: str) -> Optional[sqlite3.Row]:
    """Find the project a `/btw` names by its first word.

    Exact slug first, then an unambiguous slug prefix, then an unambiguous
    title prefix - typing the whole slug from a phone is exactly the friction
    this feature exists to avoid. Ambiguity resolves to None rather than to a
    guess: asking the wrong project a question wastes a minute and reads as the
    portal being broken.
    """
    token = (token or "").strip().lower().strip(":,")
    if not token:
        return None
    exact = db.get_project_by_slug(token)
    if exact is not None:
        return exact
    projects = db.list_projects()
    for candidates in (
        [p for p in projects if p["slug"].startswith(token)],
        [p for p in projects if (p["title"] or "").lower().startswith(token)],
    ):
        if len(candidates) == 1:
            return candidates[0]
    return None


async def _start_ask(project: sqlite3.Row, question: str, chat_id: str) -> None:
    """Kick off a read-only ask and tell Wes it's running.

    The answer arrives as its own message minutes later (ask.answer sends it),
    so this reply exists purely so the message doesn't look ignored.
    """
    if not question:
        await notify.send_telegram_text(chat_id, persona.say("ask_empty"))
        return
    if ask.pending(project["id"]):
        await notify.send_telegram_text(chat_id, persona.say("ask_busy", title=project["title"]))
        return
    ask.start(project["id"], question, reply_chat_id=chat_id)
    await notify.send_telegram_text(chat_id, persona.say("ask_started", title=project["title"]))


async def _handle_btw(arg: str, chat_id: str) -> None:
    """`/btw <project> <question>`."""
    token, _, question = arg.partition(" ")
    project = resolve_project_token(token)
    if project is None:
        slugs = ", ".join(p["slug"] for p in db.list_projects_by_stage(config.OPEN_STAGES))
        await notify.send_telegram_text(
            chat_id, persona.say("ask_which", slugs=slugs or "(no active projects)")
        )
        return
    await _start_ask(project, question.strip(), chat_id)


async def _create_idea(idea_text: str, chat_id: str) -> None:
    if not idea_text:
        await notify.send_telegram_text(chat_id, persona.say("idea_empty"))
        return
    title = idea_text.split("\n", 1)[0][:80]
    # Whoever's chat this is owns the idea - same rule as the web form. A chat
    # nobody has claimed resolves to None, and create_project falls back to
    # the owner, which is the only defensible guess for an unclaimed chat.
    person_id = _person_id(chat_id)
    project = db.create_project(
        title=title, description=idea_text, kind="unknown", stage="backlog",
        person_id=person_id,
    )
    db.add_journal(project["id"], "user", "note", idea_text, person_id=person_id)
    url = f"http://{config.HOST_LABEL}:{config.PORT}/project/{project['slug']}"
    await notify.send_telegram_text(
        chat_id, persona.say("idea_created", title=project["title"], url=url)
    )


def _status_summary() -> str:
    projects = db.list_projects_by_stage(config.OPEN_STAGES)
    open_qs = db.open_questions()

    # Lead with what's happening *now* - that's what "how's it going" means
    # when you're asking from a phone, and it's what a 'stop' would target.
    lines: list[str] = []
    runs = db.active_runs()
    if not runs:
        reason = worker.idle_reason()
        lines.append(f"Nothing running - {reason}." if reason else "Nothing running.")
    else:
        for run in runs:
            lines.append(
                f"Running: #{run['id']} {run['task']} on "
                f"{run['project_title'] or 'memory / reflect'}"
            )
            said = run["last_said"] if "last_said" in run.keys() else None
            if said:
                lines.append(f"  {said}")
            lines.append(f"  {run['last_activity'] or 'starting up...'}")
        lines.append(
            "  Send 'stop' to end it." if len(runs) == 1
            else "  Send 'stop <project>' to end one."
        )
    used, cap = db.count_runs_today(), db.effective_max_runs()
    lines.append(f"Runs today: {used}/{cap} ({max(0, cap - used)} left)")
    lines.append("")

    lines.append("Active projects:")
    if not projects:
        lines.append("  (none)")
    for p in projects[:15]:
        lines.append(f"  - {p['title']} [{db.display_state(p)}]")
    lines.append("")
    lines.append(f"Open questions: {len(open_qs)}")
    for q in open_qs[:10]:
        label = persona.question_prefix(q["slot"], q["project_title"])
        lines.append(f"  - {label}: {q['question'][:100]}")
    if persona.current_voice() == persona.GLADOS:
        lines.append("")
        lines.append("Progress remains within acceptable parameters. Barely.")
    return "\n".join(lines)

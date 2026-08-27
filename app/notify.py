"""Best-effort outbound notifications (web push + Telegram + ntfy).

Web push is the default method: a person with an enrolled device is reached
there, and the install's ntfy/Telegram channels are the fallback for anyone
who has not enrolled one. See app/routing.py.

Never raises: notification failures are logged and swallowed so they can
never crash the worker or a request handler.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app import db, people, persona, quickreplies, routing, scope, webpush

log = logging.getLogger("portal.notify")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


async def notify(
    title: str,
    message: str,
    question_id: Optional[int] = None,
    project_title: Optional[str] = None,
    question_slot: Optional[int] = None,
    project_id: Optional[int] = None,
) -> None:
    """Send one notification to everybody it is addressed to.

    `project_id` is what makes it addressed to anybody in particular: with it,
    this reaches that project's members; without it, everybody. Which concrete
    topics, chats and devices that means - and why a person with no channels of
    their own still hears about it - is app/routing.py.
    """
    settings = db.get_all_settings()
    telegram_on = db.telegram_enabled()
    people_rows = routing.recipients(project_id)
    all_subs = db.list_push_subscriptions()
    # Web push is the default method: a recipient with an enrolled device of
    # their own is reached there and stops pulling the install's ntfy topic
    # and Telegram chat in as a fallback. See routing.push_covered.
    covered = routing.push_covered(people_rows, all_subs)
    text = message
    if question_id is not None:
        # "Q7: [Project]: <question>" - the number Wes types back, then which
        # project is asking, then the question itself. With no bot to type it
        # back at, the number addresses nothing and is spending characters
        # from a notification preview that is only a line wide, so it goes.
        prefix = persona.question_prefix(
            question_slot if telegram_on else None, project_title
        )
        text = f"{prefix}: {message}" if prefix else message

    if telegram_on:
        token = settings.get("telegram_token", "")
        decorated = persona.decorate_notification(title, text)
        chats = routing.telegram_chats(
            people_rows, settings.get("telegram_chat_id", ""), covered
        )
        for index, chat_id in enumerate(chats):
            # Every chat's copy is recorded in question_messages, so answering
            # from anywhere settles all of them (settle_question_copies). The
            # legacy telegram_msg_id column still gets the first copy's id -
            # it is what old code paths and pre-table rows key on, and writing
            # it costs nothing. First rather than last is only determinism.
            await _send_telegram(
                token, chat_id, decorated, question_id, record_msg_id=(index == 0)
            )

    ntfy_url = settings.get("ntfy_url", "")
    for topic in routing.ntfy_topics(people_rows, settings.get("ntfy_topic", ""), covered):
        await _send_ntfy(ntfy_url, topic, title, text)

    # Enrolled phones. A question deserves the lock screen now; everything
    # else can wait for the OS's normal batching.
    subs = routing.push_subscriptions(people_rows, all_subs)
    # The badge and the buttons are worked out only when there is a phone to
    # put them on: an install with nobody enrolled sends everything over
    # Telegram and ntfy, and should not pay for a per-person question count on
    # each one. `push_to` is still called either way - it is the one line that
    # says a notification was routed to web push at all.
    await webpush.push_to(
        subs,
        title,
        text,
        urgency="high" if question_id is not None else "normal",
        badges=question_badges() if subs else None,
        actions=push_actions(question_id) if subs else None,
    )


def question_badges() -> dict:
    """How many questions each person has waiting, for the home-screen icon.

    Counted for everybody rather than only the recipients, and on every
    notification rather than only on a question: the badge is a running total,
    not news about this one message, so the send that finally corrects a stale
    icon is often a notification about something else entirely.

    Best-effort like the rest of this module - a badge that could not be
    counted is simply not sent, and the notification goes out without one.
    """
    try:
        return {
            int(person["id"]): len(scope.pending_questions(person))
            for person in people.everyone()
        }
    except Exception:  # noqa: BLE001 - never break a notification over a count
        log.warning("Could not count pending questions for the app badge", exc_info=True)
        return {}


def push_actions(question_id: Optional[int]) -> list:
    """The answer buttons for this notification, if it is a question with
    options small enough to fit on a lock screen. See quickreplies."""
    if question_id is None:
        return []
    try:
        row = db.get_question(question_id)
        options = quickreplies.decode(row["quick_options"] if row else "")
        return quickreplies.push_actions(question_id, options, webpush.portal_url())
    except Exception:  # noqa: BLE001
        log.warning("Could not build push answer buttons", exc_info=True)
        return []


async def _send_telegram(
    token: str,
    chat_id: str,
    text: str,
    question_id: Optional[int],
    record_msg_id: bool = True,
) -> None:
    try:
        payload: dict = {"chat_id": chat_id, "text": text}
        if question_id is not None:
            # Question messages carry one-tap answer buttons; the option list
            # was frozen on the row at creation. See app/quickreplies.
            row = db.get_question(question_id)
            options = quickreplies.decode(row["quick_options"] if row else "")
            payload["reply_markup"] = quickreplies.keyboard(question_id, options)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                TELEGRAM_API.format(token=token, method="sendMessage"),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            if question_id is not None and data.get("ok"):
                msg_id = data.get("result", {}).get("message_id")
                if msg_id is not None:
                    db.record_question_message(question_id, chat_id, int(msg_id), text)
                    if record_msg_id:
                        db.set_question_telegram_msg_id(question_id, int(msg_id))
    except Exception as exc:  # noqa: BLE001 - best effort, never raise
        log.warning("Telegram notify failed: %s", exc)


async def settle_question_copies(question_id: int, verdict: str) -> int:
    """Rewrite every Telegram copy of a question to carry its outcome.

    editMessageText without a reply_markup drops the inline keyboard, so a
    settled copy has nothing left to tap. Called from every path that closes
    a question - a button tap, a typed reply, the web routes - so the person
    who did NOT answer sees "[answered: ...]" instead of a question that
    looks open forever. Returns how many copies it knew about; zero means a
    question from before the copies table existed, and the caller may fall
    back to editing whatever message it has in hand."""
    rows = db.question_messages(question_id)
    for row in rows:
        text = row["text"] or ""
        await telegram_call(
            "editMessageText",
            {
                "chat_id": row["chat_id"],
                "message_id": row["message_id"],
                "text": f"{text}\n\n[{verdict}]" if text else f"[{verdict}]",
            },
        )
    return len(rows)


async def telegram_call(method: str, payload: dict) -> None:
    """Fire a Telegram Bot API method best-effort (answerCallbackQuery,
    editMessageText, ...). Failures are logged and swallowed - a missed
    message edit must never break the answer it decorates."""
    if not db.telegram_enabled():
        return
    token = db.get_setting("telegram_token") or ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(TELEGRAM_API.format(token=token, method=method), json=payload)
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram %s failed: %s", method, exc)


async def _send_ntfy(ntfy_url: str, topic: str, title: str, message: str) -> None:
    if not ntfy_url or not topic:
        return
    try:
        url = f"{ntfy_url.rstrip('/')}/{topic}"
        headers = {}
        if title:
            headers["Title"] = title.encode("ascii", errors="ignore").decode() or "Project Portal"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, content=message.encode("utf-8"), headers=headers)
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - best effort, never raise
        log.warning("ntfy notify failed: %s", exc)


async def send_telegram_text(chat_id: str, text: str) -> None:
    """Send a plain reply from the bot poller (confirmations etc.)."""
    if not db.telegram_enabled():
        return
    settings = db.get_all_settings()
    token = settings.get("telegram_token", "")
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                TELEGRAM_API.format(token=token, method="sendMessage"),
                json={"chat_id": chat_id, "text": text},
            )
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram send failed: %s", exc)

"""Best-effort outbound notifications (Telegram + ntfy + web push).

Never raises: notification failures are logged and swallowed so they can
never crash the worker or a request handler.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app import db, persona, quickreplies, webpush

log = logging.getLogger("portal.notify")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


async def notify(
    title: str,
    message: str,
    question_id: Optional[int] = None,
    project_title: Optional[str] = None,
    question_slot: Optional[int] = None,
) -> None:
    settings = db.get_all_settings()
    telegram_on = db.telegram_enabled()
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

    chat_id = settings.get("telegram_chat_id", "")
    if telegram_on and chat_id:
        token = settings.get("telegram_token", "")
        await _send_telegram(token, chat_id, persona.decorate_notification(title, text), question_id)

    await _send_ntfy(settings.get("ntfy_url", ""), settings.get("ntfy_topic", ""), title, text)

    # Enrolled phones. A question deserves the lock screen now; everything
    # else can wait for the OS's normal batching.
    await webpush.push_all(title, text, urgency="high" if question_id is not None else "normal")


async def _send_telegram(token: str, chat_id: str, text: str, question_id: Optional[int]) -> None:
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
                    db.set_question_telegram_msg_id(question_id, int(msg_id))
    except Exception as exc:  # noqa: BLE001 - best effort, never raise
        log.warning("Telegram notify failed: %s", exc)


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

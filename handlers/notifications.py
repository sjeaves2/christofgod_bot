"""Event reminder delivery: rendering, media attachments, recipient selection,
idempotent broadcast, catch-up retries, and job scheduling.

The media helpers (_send_media, CAPTION_LIMIT, …) also serve /broadcast.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytz
from telegram import InputFile
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

from common import (
    md,
    _answer_cb,  # noqa: F401  (re-exported for handlers importing from here)
    _event_category,
    format_dt,
    now_tz,
    user_lang_of,
    user_notif_prefs,
    user_tz_of,
)
from events import all_upcoming
from localization import DEFAULT_LANG, t
from settings import BASE_DIR, TZ, activity
import storage

logger = logging.getLogger(__name__)

def _render_notification(event: dict[str, Any], tz: "pytz.BaseTzInfo", lang: str) -> str:
    """Build a reminder message localized and time-zoned for one recipient."""
    lines = [
        t("notif_reminder_title", lang, name=md(event["name"])),
        t("notif_service_begins", lang, when=format_dt(event["service_time"], tz, lang)),
    ]
    if event.get("description"):
        lines.append(f"\n_{md(event['description'])}_")
    if event.get("url"):
        lines.append("\n" + t("notif_join", lang, url=event["url"]))
    if event.get("announcements"):
        lines.append("\n" + t("notif_announcements_header", lang))
        lines.extend(f"• {md(a)}" for a in event["announcements"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Media attachments (photos / documents) for notifications and broadcasts
# ---------------------------------------------------------------------------

CAPTION_LIMIT = 1024  # Telegram's caption length limit for photos/documents


def _is_media_url(src: "str | None") -> bool:
    return isinstance(src, str) and src.lower().startswith(("http://", "https://"))


def _looks_like_path(src: str) -> bool:
    """Heuristic: a local path has a separator or a file extension; a Telegram
    file_id has neither."""
    return "/" in src or "\\" in src or "." in src


def _resolve_local_media(src: "str | None") -> "Path | None":
    """Resolve a local media path (relative to the project root) to an existing file."""
    if not src:
        return None
    p = Path(src)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p if p.is_file() else None


async def _send_media(bot, chat_id, kind: str, source: str,
                      caption: "str | None" = None, cache: "dict | None" = None):
    """Send a photo or document to one chat.

    *source* may be an https URL, a local file path, or a Telegram file_id.
    Local files are uploaded and the returned file_id is stored in *cache* so
    the same file is only uploaded once across many recipients. Returns the
    file_id, or None. Raises TelegramError on send failure (caller retries).
    """
    parse_mode = ParseMode.MARKDOWN if caption else None
    opened = None
    if cache and cache.get("file_id"):
        media = cache["file_id"]
    elif _is_media_url(source):
        media = source
    else:
        local = _resolve_local_media(source)
        if local is not None:
            opened = local.open("rb")
            media = InputFile(opened, filename=local.name)
        else:
            media = source  # assume it's already a Telegram file_id
    try:
        if kind == "photo":
            msg = await bot.send_photo(chat_id, media, caption=caption, parse_mode=parse_mode)
            fid = msg.photo[-1].file_id if getattr(msg, "photo", None) else None
        else:
            msg = await bot.send_document(chat_id, media, caption=caption, parse_mode=parse_mode)
            fid = msg.document.file_id if getattr(msg, "document", None) else None
    finally:
        if opened:
            opened.close()
    if cache is not None and fid:
        cache["file_id"] = fid
    return fid


async def _send_notification_payload(bot, chat_id, media: dict, text: str, caches: dict) -> None:
    """Deliver a notification to one chat: image/document with the reminder text as
    a caption, plus the text as its own message if it exceeds the caption limit."""
    image = media.get("image")
    document = media.get("document")
    if not image and not document:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
        return
    caption = text if len(text) <= CAPTION_LIMIT else None
    caption_used = False
    if image:
        await _send_media(bot, chat_id, "photo", image, caption=caption, cache=caches["image"])
        caption_used = caption is not None
    if document:
        cap = None if caption_used else caption
        await _send_media(bot, chat_id, "document", document, caption=cap, cache=caches["document"])
        caption_used = caption_used or (cap is not None)
    if caption is None:  # text too long to be a caption — send separately
        await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)


async def _notification_recipients(event: dict[str, Any]) -> dict:
    """Map every recipient chat_id -> (tz, lang) for this event.

    Recipients are the configured group/channel targets (rendered in the church
    default tz/language) plus any individual subscribers who opted into this
    event's category (rendered in their own tz/language).
    """
    recipients: dict = {}
    for cid in event.get("target_chat_ids") or []:
        recipients.setdefault(cid, (TZ, DEFAULT_LANG))
    category = _event_category(event)
    for u in await storage.get_all_users():
        if category in user_notif_prefs(u):
            recipients[u["chat_id"]] = (user_tz_of(u), user_lang_of(u))
    return recipients


async def deliver_event_notifications(bot, event: dict[str, Any]) -> int:
    """Post an event reminder to each recipient not yet notified.

    Recipients are the configured groups/channels plus individual subscribers
    who opted in (via /notifications) to this event's category. Idempotent:
    tracks delivered chats in notification_state so a missed or partially-failed
    broadcast can be retried later without duplicates. Does nothing once the
    event's service time has passed. Returns the number of messages sent.
    """
    key = event["key"]
    service_time = event["service_time"]
    now = now_tz()
    if now >= service_time:
        return 0  # too late — the event has already started

    recipients = await _notification_recipients(event)
    if not recipients:
        return 0  # no groups configured and nobody opted in

    states = await storage._load_notif_state()
    state = states.setdefault(
        key,
        {"name": event["name"], "service_time": service_time.isoformat(), "notified": []},
    )
    notified = set(state["notified"])
    pending = [c for c in recipients if c not in notified]
    if not pending:
        return 0

    # Configured media (image/document): URL or local path. Drop a local file
    # that's missing so the reminder still goes out as text.
    media = {"image": event.get("image") or "", "document": event.get("document") or ""}
    for field, src in list(media.items()):
        if src and not _is_media_url(src) and _looks_like_path(src) \
                and _resolve_local_media(src) is None:
            logger.warning("Event %r: media file not found, skipping: %s", event["name"], src)
            media[field] = ""
    caches = {"image": {}, "document": {}}

    sent = 0
    failed = 0
    for chat_id in pending:
        tz, lang = recipients[chat_id]
        text = _render_notification(event, tz, lang)
        try:
            await _send_notification_payload(bot, chat_id, media, text, caches)
            notified.add(chat_id)
            sent += 1
        except TelegramError as exc:
            # Includes Forbidden (bot not in group) — leave pending for retry.
            failed += 1
            logger.warning("Notification post error for chat %s: %s", chat_id, exc)

    state["notified"] = sorted(notified, key=lambda c: str(c))
    states[key] = state
    await storage._save_notif_state(states)

    if sent:
        activity.log_notification_sent(event["name"], sent)
    logger.info(
        "Notification broadcast %r: posted=%d, will_retry=%d",
        event["name"], sent, failed,
    )
    return sent


async def send_notification(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled one-shot job at an event's notification time."""
    event: dict[str, Any] = context.job.data  # type: ignore[attr-defined]
    await deliver_event_notifications(context.bot, event)


async def notification_catchup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Safety net: retry any due-but-undelivered notifications.

    Covers reminders missed entirely (process asleep/offline at fire time) and
    partial failures (network errors). Retries every recipient still pending
    for any event currently inside its [notification_time, service_time) window,
    then prunes state for events whose service time has passed.
    """
    now = now_tz()
    events = await all_upcoming(days_ahead=3)
    for ev in events:
        if ev["notification_time"] <= now < ev["service_time"]:
            await deliver_event_notifications(context.bot, ev)

    # Prune state for events that have started or fallen out of the window.
    states = await storage._load_notif_state()
    live_keys = {ev["key"] for ev in events if now < ev["service_time"]}
    pruned = {k: v for k, v in states.items() if k in live_keys}
    if len(pruned) != len(states):
        await storage._save_notif_state(pruned)


def schedule_event_notification(app: Application, event: dict[str, Any]) -> None:
    notif_time = event["notification_time"]
    if notif_time <= now_tz():
        return
    job_id = f"notif_{event['key']}"
    # Remove existing job with same id (if rescheduled)
    existing = app.job_queue.get_jobs_by_name(job_id)
    for j in existing:
        j.schedule_removal()
    app.job_queue.run_once(send_notification, when=notif_time, data=event, name=job_id)


async def schedule_all_upcoming(app: Application) -> None:
    events = await all_upcoming(days_ahead=400)
    for ev in events:
        schedule_event_notification(app, ev)
    logger.info("Scheduled %d upcoming event notifications.", len(events))

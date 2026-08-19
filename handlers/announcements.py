"""/announcements — congregation announcements.

Users see the active ones (machine-translated into their language on first
view and cached); admins create them (with an optional broadcast on creation),
list them, and expire them early. Expired announcements linger for
ANNOUNCEMENT_PURGE_AFTER_DAYS so admins can still see them, then are deleted.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

import storage
import translation
from common import _is_affirmative, get_user_prefs, now_tz
from handlers.broadcast import (
    BC_SELECT,
    _append_sender,
    _bc_keyboard,
    _broadcast_target_options,
)
from localization import t
from permissions import admin_only, user_info
from settings import TZ, activity
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler
from telegram.helpers import escape_markdown

logger = logging.getLogger(__name__)

# Expired announcements stay in the file (hidden from /announcements) for this
# many days, then the daily maintenance job deletes them.
ANNOUNCEMENT_PURGE_AFTER_DAYS = 30


def _ann_expiry_dt(ann: dict[str, Any]) -> "datetime | None":
    """An announcement expires at the END of its `expires` day, church time."""
    try:
        d = datetime.strptime(str(ann.get("expires", "")), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return TZ.localize(d.replace(hour=23, minute=59, second=59))


def _ann_is_active(ann: dict[str, Any], now: "datetime | None" = None) -> bool:
    exp = _ann_expiry_dt(ann)
    return exp is not None and (now or now_tz()) <= exp


def active_announcements(anns: list[dict[str, Any]],
                         now: "datetime | None" = None) -> list[dict[str, Any]]:
    """Active announcements, newest first."""
    now = now or now_tz()
    live = [a for a in anns if _ann_is_active(a, now)]
    return sorted(live, key=lambda a: str(a.get("created", "")), reverse=True)


async def purge_old_announcements() -> int:
    """Delete announcements expired more than the retention window ago.

    Records with unparseable expiry dates are kept (never silently discarded).
    """
    cutoff = now_tz() - timedelta(days=ANNOUNCEMENT_PURGE_AFTER_DAYS)
    anns = await storage.get_announcements()
    keep = []
    for a in anns:
        exp = _ann_expiry_dt(a)
        if exp is None or exp >= cutoff:
            keep.append(a)
    purged = len(anns) - len(keep)
    if purged:
        await storage.save_announcements(keep)
        logger.info("Purged %d announcement(s) expired more than %d days ago.",
                    purged, ANNOUNCEMENT_PURGE_AFTER_DAYS)
    return purged



ANN_TITLE_MAX = 64
ANN_BODY_MAX = 1024

# Conversation states. Offset past the BC_* constants (0-2) because the
# add-announcement conversation re-enters the broadcast target-selection
# states after saving.
AN_TITLE, AN_BODY, AN_EXPIRES, AN_CONFIRM = range(3, 7)
DA_SELECT = 7


def _render_announcement(ann: dict[str, Any]) -> str:
    """Markdown-safe '📢 title + body' block (admin-typed text is escaped)."""
    title = escape_markdown(str(ann.get("title", "")), version=1)
    body = escape_markdown(str(ann.get("body", "")), version=1)
    return f"📢 *{title}*\n\n{body}"


async def _announcement_for_lang(ann: dict[str, Any], lang: str) -> dict[str, Any]:
    """The announcement with title/body in *lang*, machine-translating and
    caching on first request.

    Returns the record itself when no translation is needed (same language, or
    a legacy record without a source lang), and falls back to the original text
    whenever translation fails — announcements must never break because the
    translator did.
    """
    src = ann.get("lang")
    if not src or src == lang:
        return ann
    cached = (ann.get("translations") or {}).get(lang)
    if cached:
        return {**ann, "title": cached.get("title") or ann.get("title"),
                "body": cached.get("body") or ann.get("body")}

    loop = asyncio.get_event_loop()
    title = await loop.run_in_executor(
        None, translation.translate, str(ann.get("title", "")), src, lang)
    body = await loop.run_in_executor(
        None, translation.translate, str(ann.get("body", "")), src, lang)
    if title is None and body is None:
        return ann  # translator unavailable — show the original

    entry = {"title": title or ann.get("title"), "body": body or ann.get("body")}
    # Persist the translation on the stored record so it's done once per language.
    anns = await storage.get_announcements()
    for a in anns:
        if a.get("id") == ann.get("id"):
            a.setdefault("translations", {})[lang] = entry
            await storage.save_announcements(anns)
            break
    return {**ann, **entry}


async def cmd_announcements(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("announcements", uid, uname, dname)
    _, lang = await get_user_prefs(uid)
    live = active_announcements(await storage.get_announcements())
    if not live:
        await update.message.reply_text(t("ann_none", lang))
        return
    parts = [t("ann_header", lang)]
    for a in live:
        localized = await _announcement_for_lang(a, lang)
        parts.append(
            _render_announcement(localized) + "\n"
            + t("ann_until", lang, date=a.get("expires", "?"))
        )
    await update.message.reply_text("\n\n".join(parts), parse_mode=ParseMode.MARKDOWN)


@admin_only
async def cmd_addannouncement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("addannouncement", uid, uname, dname)
    context.user_data.clear()
    await update.message.reply_text(
        f"📢 *Add Announcement*\n\nTitle (max {ANN_TITLE_MAX} characters):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return AN_TITLE


async def an_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = update.message.text.strip()
    if not title or len(title) > ANN_TITLE_MAX:
        await update.message.reply_text(
            f"Please send a title of 1–{ANN_TITLE_MAX} characters:")
        return AN_TITLE
    context.user_data["an_title"] = title
    await update.message.reply_text(f"Body (max {ANN_BODY_MAX} characters):")
    return AN_BODY


async def an_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    body = update.message.text.strip()
    if not body or len(body) > ANN_BODY_MAX:
        await update.message.reply_text(
            f"Please send a body of 1–{ANN_BODY_MAX} characters:")
        return AN_BODY
    context.user_data["an_body"] = body
    await update.message.reply_text("Expiration date (YYYY-MM-DD) — the announcement "
                                    "shows through the end of that day:")
    return AN_EXPIRES


async def an_expires(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        d = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("Please use YYYY-MM-DD format:")
        return AN_EXPIRES
    if TZ.localize(d.replace(hour=23, minute=59, second=59)) < now_tz():
        await update.message.reply_text(
            "That date is in the past. Please enter today or a future date (YYYY-MM-DD):")
        return AN_EXPIRES
    context.user_data["an_expires"] = text
    preview = _render_announcement(
        {"title": context.user_data["an_title"], "body": context.user_data["an_body"]})
    await update.message.reply_text(
        f"{preview}\n\n_Expires: {text} (end of day)_\n\nSave this announcement? (yes/no)",
        parse_mode=ParseMode.MARKDOWN,
    )
    return AN_CONFIRM


async def an_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    _, lang = await get_user_prefs(uid)
    if not _is_affirmative(update.message.text):
        await update.message.reply_text("Announcement discarded.")
        return ConversationHandler.END

    ann = {
        "id": uuid.uuid4().hex[:8].upper(),
        "title": context.user_data["an_title"],
        "body": context.user_data["an_body"],
        "lang": lang,  # source language; viewers in other languages get a
                       # cached machine translation (see _announcement_for_lang)
        "created": now_tz().isoformat(),
        "created_by": dname,
        "expires": context.user_data["an_expires"],
    }
    anns = await storage.get_announcements()
    anns.append(ann)
    await storage.save_announcements(anns)
    activity.log_command("addannouncement", uid, uname, dname,
                         details=f"Created announcement {ann['id']}")

    # Hand off to the broadcast target-selection flow so the new announcement
    # can be pushed out immediately (Cancel skips the push; it's already saved).
    context.user_data["bc_message"] = _append_sender(_render_announcement(ann), dname)
    context.user_data.pop("bc_media", None)
    options = await _broadcast_target_options(context.bot, lang)
    context.user_data["bc_options"] = options
    context.user_data["bc_selected"] = set()
    await update.message.reply_text(
        f"✅ Announcement saved (ID: `{ann['id']}`).\n\n"
        "Now choose where to broadcast it, then tap *Send* "
        "(or *Cancel* to skip broadcasting — it will still appear in /announcements):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_bc_keyboard(options, set()),
    )
    return BC_SELECT


@admin_only
async def cmd_listannouncements(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("listannouncements", uid, uname, dname)
    anns = await storage.get_announcements()
    if not anns:
        await update.message.reply_text("No announcements on record.")
        return
    now = now_tz()
    lines = ["*Announcements (admin view):*\n"]
    for a in sorted(anns, key=lambda x: str(x.get("created", "")), reverse=True):
        mark = "🟢" if _ann_is_active(a, now) else "⚪️ expired"
        title = escape_markdown(str(a.get("title", "?")), version=1)
        lines.append(f"{mark} *{title}* — until {a.get('expires', '?')} (ID: `{a.get('id')}`)")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@admin_only
async def cmd_delannouncement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("delannouncement", uid, uname, dname)
    live = active_announcements(await storage.get_announcements())
    if not live:
        await update.message.reply_text("There are no active announcements to remove.")
        return ConversationHandler.END
    context.user_data["da_anns"] = live
    lines = ["*Remove an Announcement*\nReply with the number to expire it now:\n"]
    for i, a in enumerate(live, 1):
        title = escape_markdown(str(a.get("title", "?")), version=1)
        lines.append(f"{i}. *{title}* — until {a.get('expires', '?')}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    return DA_SELECT


async def da_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    live: list = context.user_data.get("da_anns", [])
    text = update.message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= len(live)):
        await update.message.reply_text("Please reply with one of the listed numbers:")
        return DA_SELECT
    target = live[int(text) - 1]
    anns = await storage.get_announcements()
    yesterday = (now_tz() - timedelta(days=1)).strftime("%Y-%m-%d")
    for a in anns:
        if a.get("id") == target.get("id"):
            a["expires"] = yesterday  # expires immediately; purged after 30 days
            break
    await storage.save_announcements(anns)
    activity.log_command("delannouncement", uid, uname, dname,
                         details=f"Expired announcement {target.get('id')}")
    await update.message.reply_text(
        f"✅ Announcement `{target.get('id')}` is no longer shown. "
        f"It will be permanently deleted after {ANNOUNCEMENT_PURGE_AFTER_DAYS} days.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END



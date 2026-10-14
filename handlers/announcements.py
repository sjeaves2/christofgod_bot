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
from common import (
    _is_affirmative,
    get_user_prefs,
    is_button_url,
    now_tz,
    reply_markdown,
    validate_markdown,
)
from handlers.notifications import CAPTION_LIMIT
from handlers.broadcast import (
    BC_SELECT,
    _append_sender,
    _bc_keyboard,
    _broadcast_target_options,
)
from localization import t
from permissions import admin_only, user_info
from settings import TZ, activity
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler
from telegram.helpers import escape_markdown

logger = logging.getLogger(__name__)

# Expired announcements stay in the file (hidden from /announcements) for this
# many days, then the daily maintenance job deletes them.
ANNOUNCEMENT_PURGE_AFTER_DAYS = 30


def _ann_expiry_dt(ann: dict[str, Any]) -> datetime | None:
    """An announcement expires at the END of its `expires` day, church time."""
    try:
        d = datetime.strptime(str(ann.get("expires", "")), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return TZ.localize(d.replace(hour=23, minute=59, second=59))


def _ann_is_active(ann: dict[str, Any], now: datetime | None = None) -> bool:
    exp = _ann_expiry_dt(ann)
    return exp is not None and (now or now_tz()) <= exp


def active_announcements(anns: list[dict[str, Any]],
                         now: datetime | None = None) -> list[dict[str, Any]]:
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
AN_TITLE, AN_BODY, AN_MEDIA, AN_LINK, AN_EXPIRES, AN_CONFIRM = range(3, 9)
DA_SELECT = 8


def announcement_markup(ann: dict[str, Any], lang: str) -> InlineKeyboardMarkup | None:
    """A tappable button for an announcement's link, or None if it has none.

    The url was validated when the admin entered it (an_link), so this is
    belt-and-braces: an older record predates the field, and a hand-edited
    announcements.yaml could hold anything. Telegram rejects the whole message
    over a bad button url, so never build one from an unchecked value.
    """
    url = is_button_url(ann.get("link"))
    if not url:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("ann_link_button", lang), url=url)]])


def _render_announcement(ann: dict[str, Any], escape: bool = True,
                        show_link: bool = True) -> str:
    """'📢 title + body' block, with the link as text unless a button carries it.

    show_link=False is for the push, where announcement_markup() puts the link
    on a tappable button. /announcements renders several announcements into ONE
    message, so it keeps the links as text: a column of buttons there would be
    noise, and a button Telegram refused would cost the whole list.

    escape=False passes the admin's own Markdown straight through, so *bold* and
    _italic_ work as typed. That is only safe for text the admin wrote and we
    validated at entry (see an_body): Telegram rejects an entire message over one
    unbalanced marker.

    escape=True is used for MACHINE-TRANSLATED copies. A translator treats * and
    _ as ordinary punctuation and may move, drop or duplicate them — and in an
    agglutinative language like isiZulu an emphasised English phrase often maps
    onto a single prefixed word, so the span has nowhere sensible to land.
    Escaping means a translated announcement arrives correct but unemphasised,
    which beats arriving broken or not at all.
    """
    title = str(ann.get("title", ""))
    body = str(ann.get("body", ""))
    if escape:
        title = escape_markdown(title, version=1)
        body = escape_markdown(body, version=1)
    out = f"📢 *{title}*\n\n{body}"
    link = str(ann.get("link") or "").strip()
    if link and show_link:
        # Always escaped, whatever `escape` says: a url is never the admin's
        # formatting, and one underscore in it would reject the whole message.
        out += f"\n\n🔗 {escape_markdown(link, version=1)}"
    return out




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

    # to_thread rather than get_event_loop().run_in_executor(): translate() is a
    # blocking network call, and this says so in one line without a loop handle.
    # get_event_loop() is also the loose idiom — it works inside a coroutine but
    # is deprecated where no loop is running.
    title = await asyncio.to_thread(
        translation.translate, str(ann.get("title", "")), src, lang)
    body = await asyncio.to_thread(
        translation.translate, str(ann.get("body", "")), src, lang)
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
    def _build(force_escape: bool) -> str:
        parts = [t("ann_header", lang)]
        for a, localized in rendered:
            # Show the admin's own formatting only in the language they wrote in.
            # A machine-translated copy has been through a translator that treats
            # * and _ as punctuation, so its markers can no longer be trusted.
            translated = bool(a.get("lang")) and a.get("lang") != lang
            parts.append(
                _render_announcement(localized, escape=force_escape or translated) + "\n"
                + t("ann_until", lang, date=a.get("expires", "?"))
            )
        return "\n\n".join(parts)

    rendered = [(a, await _announcement_for_lang(a, lang)) for a in live]
    # NB: raw on purpose. This has a BETTER fallback than plain text — it
    # re-renders with everything escaped, so the list keeps its structure. A
    # generic plain-text retry would swallow the BadRequest and lose that.
    try:
        await update.message.reply_text(_build(False), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        # One announcement's markup is unparseable — most likely a record created
        # before formatting was supported, whose literal asterisks were stored
        # escaped-on-render and are now taken as markup. Escaping everything
        # loses emphasis but is far better than showing nobody anything.
        logger.warning("Announcement Markdown failed to render; retrying escaped.")
        await reply_markdown(update.message, _build(True))


@admin_only
async def cmd_addannouncement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("addannouncement", uid, uname, dname)
    context.user_data.clear()
    await reply_markdown(update.message,
        f"📢 *Add Announcement*\n\nTitle (max {ANN_TITLE_MAX} characters):")
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
    # Validate the admin's Markdown now, by rendering it back to them, rather
    # than at send time. One unbalanced * or _ makes Telegram reject the whole
    # message, and finding that out mid-broadcast is far worse than here.
    # NB: this send must stay raw. A plain-text fallback here would swallow the
    # BadRequest that IS the validation, and every body would be accepted.
    if not await validate_markdown(
            update.message,
            _render_announcement({"title": context.user_data["an_title"], "body": body},
                                 escape=False)):
        return AN_BODY

    context.user_data["an_body"] = body
    await update.message.reply_text(
        "Attach a photo or document now, or send /skip for a text-only announcement:")
    return AN_MEDIA


_EXPIRES_PROMPT = ("Expiration date (YYYY-MM-DD) — the announcement "
                   "shows through the end of that day:")


_LINK_PROMPT = ("Send a link for this announcement and members get a tappable "
                "button, or /skip for none:")


async def an_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Optional link, shown to members as a button rather than a raw URL.

    Validated here rather than at send time: Telegram rejects the ENTIRE
    message if a button's url is unacceptable, so a bad one would silently cost
    the whole announcement. Better to say so now, while the admin is still here
    and can paste a different one.
    """
    text = (update.message.text or "").strip()
    url = is_button_url(text)
    if not url:
        await update.message.reply_text(
            "That does not look like a link Telegram will accept — it needs to "
            "start with http:// or https:// and contain no spaces.\n\n"
            "Send the link again, or /skip for none:")
        return AN_LINK
    context.user_data["an_link"] = url
    await update.message.reply_text(_EXPIRES_PROMPT)
    return AN_EXPIRES


async def an_skip_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["an_link"] = ""
    await update.message.reply_text(_EXPIRES_PROMPT)
    return AN_EXPIRES


async def an_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Optional photo/document for the announcement push.

    Telegram sends media *with* a caption rather than as a separate message, so
    the whole announcement has to fit CAPTION_LIMIT. Rather than silently
    truncating the body, say so and let the admin shorten it or skip the media.
    """
    msg = update.message
    if msg.photo:
        kind, file_id = "photo", msg.photo[-1].file_id
    elif msg.document:
        kind, file_id = "document", msg.document.file_id
    else:
        await msg.reply_text("Please send a photo or document, or /skip:")
        return AN_MEDIA

    rendered = _render_announcement(
        {"title": context.user_data["an_title"], "body": context.user_data["an_body"]},
        escape=False)
    if len(rendered) > CAPTION_LIMIT:
        await msg.reply_text(
            f"⚠️ With an attachment the whole announcement becomes the caption, "
            f"and Telegram caps that at {CAPTION_LIMIT} characters "
            f"(yours is {len(rendered)}).\n\n"
            "Send /skip to post it as text without the attachment, or /cancel and "
            "start again with a shorter body:"
        )
        return AN_MEDIA

    context.user_data["an_media"] = {"kind": kind, "file_id": file_id}
    await msg.reply_text(_LINK_PROMPT)
    return AN_LINK


async def an_skip_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/skip — post the announcement as text only."""
    context.user_data.pop("an_media", None)
    await update.message.reply_text(_LINK_PROMPT)
    return AN_LINK


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
        {"title": context.user_data["an_title"], "body": context.user_data["an_body"]},
        escape=False)
    await reply_markdown(update.message,
        f"{preview}\n\n_Expires: {text} (end of day)_\n\nSave this announcement? (yes/no)")
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
        "link": context.user_data.get("an_link", ""),
    }
    anns = await storage.get_announcements()
    anns.append(ann)
    await storage.save_announcements(anns)
    activity.log_command("addannouncement", uid, uname, dname,
                         details=f"Created announcement {ann['id']}")

    # Hand off to the broadcast target-selection flow so the new announcement
    # can be pushed out immediately (Cancel skips the push; it's already saved).
    # The stored body keeps the admin's Markdown; escape=False so *bold* reaches
    # the congregation as typed (it was validated in an_body).
    has_button = announcement_markup(ann, lang) is not None
    rendered = _append_sender(
        _render_announcement(ann, escape=False, show_link=not has_button), dname)
    media = context.user_data.get("an_media")
    if media:
        # Media carries the text as its caption — Telegram has no "photo plus
        # separate message" primitive in a single send.
        caption = rendered if len(rendered) <= CAPTION_LIMIT else None
        context.user_data["bc_media"] = {**media, "caption": caption}
        context.user_data.pop("bc_message", None)
    else:
        context.user_data["bc_message"] = rendered
        context.user_data.pop("bc_media", None)
    # The link rides as a button on the pushed message. Recipients are the
    # configured group targets, rendered in the church default language, so one
    # markup serves them all.
    context.user_data["bc_markup"] = announcement_markup(ann, lang)
    options = await _broadcast_target_options(context.bot, lang)
    context.user_data["bc_options"] = options
    context.user_data["bc_selected"] = set()
    await reply_markdown(update.message, f"✅ Announcement saved (ID: `{ann['id']}`).\n\n"
        "Now choose where to broadcast it, then tap *Send* "
        "(or *Cancel* to skip broadcasting — it will still appear in /announcements):",
        reply_markup=_bc_keyboard(options, set()))
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
    await reply_markdown(update.message, "\n".join(lines))


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
    await reply_markdown(update.message, "\n".join(lines))
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
    await reply_markdown(update.message,
        f"✅ Announcement `{target.get('id')}` is no longer shown. "
        f"It will be permanently deleted after {ANNOUNCEMENT_PURGE_AFTER_DAYS} days.")
    return ConversationHandler.END



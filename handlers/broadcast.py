"""/broadcast — admin sends an ad-hoc message (text or media) to selected
groups/channels and/or every individual subscriber, with retry on failures.

The target-selection states (BC_SELECT/BC_RETRY) are also re-entered by the
announcements flow so a new announcement can be pushed out on creation.
"""

from __future__ import annotations

import logging
from typing import Any

from permissions import admin_only, user_info
import storage
from common import _answer_cb, get_user_prefs
from handlers.notifications import CAPTION_LIMIT, _send_media
from localization import DEFAULT_LANG, t
from settings import activity
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes, ConversationHandler
from telegram.helpers import escape_markdown

logger = logging.getLogger(__name__)

BC_MESSAGE, BC_SELECT, BC_RETRY = range(3)

CB_BC_PREFIX = "bc:"
BC_MAX_RETRIES = 3


async def _reconcile_registry_into_known_groups(bot) -> dict[str, Any]:
    """Ensure every valid notification_targets chat is recorded in known_groups.

    For each registry target not already tracked, fetch its real chat (title,
    type) via the API and add it to known_groups so broadcasts can show the
    group's pretty name. Invalid/unreachable ids are skipped. Returns the
    (possibly updated) known_groups map.
    """
    groups = await storage._load_known_groups()
    evdata = await storage.get_all_events_data()
    registry: dict = evdata.get("notification_targets", {}) or {}

    added = False
    for name, cid in registry.items():
        if str(cid) in groups:
            continue
        try:
            chat = await bot.get_chat(cid)
        except TelegramError as exc:
            logger.warning("notification_targets '%s' (%s) not reachable: %s", name, cid, exc)
            continue
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
            groups[str(chat.id)] = {
                "chat_id": chat.id,
                "title": chat.title or name,
                "type": str(chat.type),
                "status": "member",
            }
            added = True
    if added:
        await storage._save_known_groups(groups)
    return groups


async def _broadcast_target_options(bot, lang: str = DEFAULT_LANG) -> list[dict[str, Any]]:
    """Build the selectable target list from known groups (using their pretty
    titles), plus any registry target we couldn't resolve, plus individual subscribers.

    Each option: {"key": str, "kind": "all"|"group", "chat_id": ..., "label": str}.
    """
    options: list[dict[str, Any]] = [
        {"key": "all", "kind": "all", "chat_id": None,
         "label": t("bcast_individual_subscribers", lang)}
    ]
    seen: set[str] = set()

    # Pull titles for any configured targets we haven't recorded yet.
    groups = await _reconcile_registry_into_known_groups(bot)
    for g in groups.values():
        cid = g.get("chat_id")
        k = str(cid)
        if k in seen:
            continue
        seen.add(k)
        options.append({"key": k, "kind": "group", "chat_id": cid,
                        "label": g.get("title") or k})

    # Registry targets that couldn't be resolved to a title — still offer them,
    # labelled with the registry name so they remain selectable.
    evdata = await storage.get_all_events_data()
    registry: dict = evdata.get("notification_targets", {}) or {}
    for name, cid in registry.items():
        k = str(cid)
        if k in seen:
            continue
        seen.add(k)
        options.append({"key": k, "kind": "group", "chat_id": cid, "label": name})

    return options


def _append_sender(body: str, sender_name: str) -> str:
    """Append a '— posted by <name>' attribution line to a broadcast body/caption."""
    footer = f"— posted by {escape_markdown(sender_name, version=1)}"
    return f"{body}\n\n{footer}" if body else footer


def _bc_keyboard(options: list[dict], selected: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for opt in options:
        mark = "✅ " if opt["key"] in selected else "▫️ "
        rows.append([InlineKeyboardButton(
            f"{mark}{opt['label']}", callback_data=f"{CB_BC_PREFIX}toggle:{opt['key']}"
        )])
    rows.append([InlineKeyboardButton("📤 Send", callback_data=f"{CB_BC_PREFIX}send")])
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"{CB_BC_PREFIX}cancel")])
    return InlineKeyboardMarkup(rows)


@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("broadcast", uid, uname, dname)
    context.user_data.clear()
    await update.message.reply_text(
        "📣 *Broadcast*\n\nSend me the message to broadcast — plain text, or a "
        "*photo* or *document* (with an optional caption). Markdown is supported; "
        "I'll show you a preview before sending.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return BC_MESSAGE


async def bc_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, _, dname = user_info(update)
    _, lang = await get_user_prefs(uid)
    text = _append_sender(update.message.text, dname)
    # Validate Markdown by rendering a preview (with the attribution) back to the admin.
    try:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except BadRequest as exc:
        await update.message.reply_text(
            f"⚠️ I couldn't render that as Markdown ({exc.message}). "
            "Please edit and re-send your message."
        )
        return BC_MESSAGE

    context.user_data["bc_message"] = text
    context.user_data.pop("bc_media", None)
    options = await _broadcast_target_options(context.bot, lang)
    context.user_data["bc_options"] = options
    context.user_data["bc_selected"] = set()
    await update.message.reply_text(
        "👆 *Preview above.* Choose where to send it, then tap *Send*:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_bc_keyboard(options, set()),
    )
    return BC_SELECT


async def bc_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Broadcast a photo or document (with an optional caption) instead of text."""
    msg = update.message
    if msg.photo:
        kind, file_id = "photo", msg.photo[-1].file_id
    elif msg.document:
        kind, file_id = "document", msg.document.file_id
    else:
        return BC_MESSAGE

    uid, _, dname = user_info(update)
    _, lang = await get_user_prefs(uid)
    caption = msg.caption or ""
    # Append the sender attribution if it still fits within the caption limit.
    with_sender = _append_sender(caption, dname)
    caption = with_sender if len(with_sender) <= CAPTION_LIMIT else caption

    # Preview it back (validates any Markdown in the caption).
    try:
        await _send_media(context.bot, msg.chat_id, kind, file_id, caption=caption or None)
    except BadRequest as exc:
        await msg.reply_text(
            f"⚠️ I couldn't render that caption as Markdown ({exc.message}). "
            "Please fix the caption and re-send."
        )
        return BC_MESSAGE

    context.user_data["bc_media"] = {"kind": kind, "file_id": file_id, "caption": caption}
    context.user_data.pop("bc_message", None)
    options = await _broadcast_target_options(context.bot, lang)
    context.user_data["bc_options"] = options
    context.user_data["bc_selected"] = set()
    await msg.reply_text(
        "👆 *Preview above.* Choose where to send it, then tap *Send*:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_bc_keyboard(options, set()),
    )
    return BC_SELECT


async def bc_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _answer_cb(query)
    action = query.data[len(CB_BC_PREFIX):]
    options: list[dict] = context.user_data.get("bc_options", [])
    selected: set[str] = context.user_data.get("bc_selected", set())

    if action == "cancel":
        await query.edit_message_text("Broadcast cancelled.")
        return ConversationHandler.END

    if action.startswith("toggle:"):
        key = action.split(":", 1)[1]
        if key in selected:
            selected.discard(key)
        else:
            selected.add(key)
        context.user_data["bc_selected"] = selected
        await query.edit_message_reply_markup(reply_markup=_bc_keyboard(options, selected))
        return BC_SELECT

    if action == "send":
        if not selected:
            await query.answer("Select at least one target first.", show_alert=True)
            return BC_SELECT
        # Expand selection into a concrete recipient list.
        recipients = await _bc_expand_recipients(options, selected)
        context.user_data["bc_recipients"] = recipients
        context.user_data["bc_done"] = set()
        context.user_data["bc_retries"] = 0
        await query.edit_message_text(f"Sending to {len(recipients)} recipient(s)…")
        return await _bc_attempt_and_prompt(update, context)

    return BC_SELECT


async def _bc_expand_recipients(options: list[dict], selected: set[str]) -> list[dict]:
    """Turn the selected option keys into concrete (kind, chat_id, label) recipients."""
    recipients: list[dict] = []
    seen: set = set()
    opt_by_key = {o["key"]: o for o in options}
    for key in selected:
        opt = opt_by_key.get(key)
        if not opt:
            continue
        if opt["kind"] == "all":
            for u in await storage.get_all_users():
                cid = u["chat_id"]
                if cid in seen:
                    continue
                seen.add(cid)
                recipients.append({"kind": "user", "chat_id": cid,
                                   "label": u.get("display_name") or str(cid)})
        else:
            cid = opt["chat_id"]
            if cid in seen:
                continue
            seen.add(cid)
            recipients.append({"kind": "group", "chat_id": cid, "label": opt["label"]})
    return recipients


async def _bc_send_pending(bot, context) -> list[dict]:
    """Send the message/media to all recipients not yet delivered. Returns failures."""
    media: "dict | None" = context.user_data.get("bc_media")
    message: str = context.user_data.get("bc_message", "")
    recipients: list[dict] = context.user_data["bc_recipients"]
    done: set = context.user_data["bc_done"]
    failures: list[dict] = []
    for r in recipients:
        if r["chat_id"] in done:
            continue
        try:
            if media:
                await _send_media(bot, r["chat_id"], media["kind"], media["file_id"],
                                  caption=media["caption"] or None)
            else:
                await bot.send_message(r["chat_id"], message, parse_mode=ParseMode.MARKDOWN)
            done.add(r["chat_id"])
        except TelegramError as exc:
            failures.append(r)
            logger.warning("Broadcast send failed for %s (%s): %s",
                           r["label"], r["chat_id"], exc)
    context.user_data["bc_done"] = done
    return failures


async def _bc_attempt_and_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send the pending recipients once, then report / prompt for retry."""
    bot = context.application.bot
    failures = await _bc_send_pending(bot, context)
    total = len(context.user_data["bc_recipients"])
    sent = len(context.user_data["bc_done"])
    chat_id = update.effective_chat.id

    uid, uname, dname = user_info(update)
    activity.log_command(
        "broadcast", uid, uname, dname,
        details=f"sent={sent}/{total}, failures={len(failures)}",
    )
    logger.info("Broadcast: delivered=%d/%d, failures=%d", sent, total, len(failures))

    if not failures:
        await bot.send_message(chat_id, f"✅ Broadcast delivered to all {total} recipient(s).")
        return ConversationHandler.END

    retries = context.user_data["bc_retries"]
    failed_labels = ", ".join(f["label"] for f in failures[:10])
    more = "" if len(failures) <= 10 else f" (+{len(failures) - 10} more)"
    summary = (
        f"⚠️ Delivered to {sent}/{total}. "
        f"{len(failures)} failed: {failed_labels}{more}."
    )
    if retries >= BC_MAX_RETRIES:
        await bot.send_message(
            chat_id, summary + f"\n\nRetry limit ({BC_MAX_RETRIES}) reached. Stopping."
        )
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Retry", callback_data=f"{CB_BC_PREFIX}retry:yes"),
        InlineKeyboardButton("🛑 Stop", callback_data=f"{CB_BC_PREFIX}retry:no"),
    ]])
    await bot.send_message(
        chat_id,
        summary + f"\n\nRetry the {len(failures)} failed recipient(s)? "
        f"(attempt {retries + 1} of {BC_MAX_RETRIES})",
        reply_markup=kb,
    )
    return BC_RETRY


async def bc_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _answer_cb(query)
    action = query.data[len(CB_BC_PREFIX):]
    if action == "retry:no":
        sent = len(context.user_data["bc_done"])
        total = len(context.user_data["bc_recipients"])
        await query.edit_message_text(
            f"Stopped. Broadcast delivered to {sent}/{total} recipient(s)."
        )
        return ConversationHandler.END

    # retry:yes
    context.user_data["bc_retries"] += 1
    await query.edit_message_text(
        f"Retrying… (attempt {context.user_data['bc_retries']} of {BC_MAX_RETRIES})"
    )
    return await _bc_attempt_and_prompt(update, context)



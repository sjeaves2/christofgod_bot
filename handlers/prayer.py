"""/prayer — members submit prayer requests; prayer admins answer them.

This is the most sensitive data the bot handles: health, finances, bereavement
and family crises, written by members in their own words. Three decisions follow
from that, all made deliberately with the user on 2026-09-16:

* **The request text is deleted the moment it is answered or dismissed.** Only a
  stub survives — id, date, requester, status, who acted — so a follow-up can be
  traced and /stats can count, without the ministry accumulating a permanent
  record of what people confided. The stub itself is purged after 30 days.

* **Members are told their request is "shared only with the ministry's
  leadership", never "confidential".** The file lives in data/, which is zipped
  nightly and direct-messaged to ops admins, so a stronger promise would not be
  true. PRIVACY.md says the same.

* **Only admins with `prayer: true` in admins.yaml may read, answer or dismiss
  a request.** Being an administrator is not enough.

A request is never silently lost: if no prayer admin can be reached it is still
stored and still confirmed to the member, and the failure is logged so it
surfaces in /stats and alerts ops.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

import permissions
import storage
from common import _is_affirmative, get_user_prefs, now_tz
from localization import t
from permissions import user_info
from settings import activity
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler
from telegram.helpers import escape_markdown

logger = logging.getLogger(__name__)

# Telegram messages cap at 4096 characters; 2048 leaves ample room for the
# preamble, the requester's name and the id when the request is forwarded.
PRAYER_MAX = 2048

# How long the stub survives after the text has been removed.
STUB_RETENTION_DAYS = 30

# Conversation states. Offset clear of the broadcast/announcement ranges.
PR_TEXT, PR_CONFIRM = range(40, 42)
RESP_SELECT, RESP_TEXT = range(42, 44)

STATUS_PENDING = "pending"
STATUS_ANSWERED = "answered"
STATUS_DISMISSED = "dismissed"


def new_request_id() -> str:
    """Short, typable identifier — an admin quotes it to answer a request."""
    return f"PR-{uuid.uuid4().hex[:6].upper()}"


def pending_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unanswered requests, oldest first — the order they should be answered."""
    live = [r for r in requests if r.get("status") == STATUS_PENDING]
    return sorted(live, key=lambda r: str(r.get("created", "")))


def find_request(requests: list[dict[str, Any]], req_id: str) -> "dict | None":
    """Look up by id, case-insensitively and tolerant of a missing PR- prefix."""
    wanted = (req_id or "").strip().upper()
    if not wanted:
        return None
    if not wanted.startswith("PR-"):
        wanted = f"PR-{wanted}"
    for r in requests:
        if str(r.get("id", "")).upper() == wanted:
            return r
    return None


def _created_dt(req: dict[str, Any]) -> "datetime | None":
    try:
        return datetime.fromisoformat(str(req.get("created", "")))
    except (TypeError, ValueError):
        return None


def _closed_dt(req: dict[str, Any]) -> "datetime | None":
    try:
        return datetime.fromisoformat(str(req.get("closed", "")))
    except (TypeError, ValueError):
        return None


def redact(req: dict[str, Any], status: str, actor: str,
           now: "datetime | None" = None) -> dict[str, Any]:
    """Strip the request text, leaving the stub.

    Called the moment a request is answered or dismissed. What a member shared
    should not outlive the reason it was shared, so the words go immediately
    and only the fact of the request remains.
    """
    now = now or now_tz()
    req["status"] = status
    req["closed"] = now.isoformat()
    req["closed_by"] = actor
    req.pop("text", None)
    return req


async def purge_old_stubs(now: "datetime | None" = None) -> int:
    """Delete stubs closed more than STUB_RETENTION_DAYS ago.

    Stubs with an unreadable closed date are kept, never silently discarded.
    """
    now = now or now_tz()
    cutoff = now - timedelta(days=STUB_RETENTION_DAYS)
    requests = await storage.get_prayer_requests()
    keep = []
    for r in requests:
        if r.get("status") == STATUS_PENDING:
            keep.append(r)
            continue
        closed = _closed_dt(r)
        if closed is None or closed >= cutoff:
            keep.append(r)
    purged = len(requests) - len(keep)
    if purged:
        await storage.save_prayer_requests(keep)
        logger.info("Purged %d prayer stub(s) closed over %d days ago.",
                    purged, STUB_RETENTION_DAYS)
    return purged


async def prayer_admin_chat_ids() -> set:
    """Chat ids of admins with `prayer: true` who have started the bot."""
    ids = set(permissions._prayer_chat_ids)
    for u in await storage.get_all_users():
        uname = (u.get("username") or "").lstrip("@").lower()
        if uname and uname in permissions.PRAYER_USERNAMES and u.get("chat_id"):
            ids.add(u["chat_id"])
    return ids


def format_for_admin(req: dict[str, Any]) -> str:
    """The request as a prayer admin sees it.

    Everything the member typed is escaped: an unbalanced marker in a moment of
    distress must not stop the request reaching anyone.
    """
    created = _created_dt(req)
    when = created.strftime("%Y-%m-%d %H:%M %Z") if created else "?"
    lang = str(req.get("lang", "en"))
    header = (f"🙏 *Prayer request* `{req.get('id', '?')}`\n"
              f"*From:* {escape_markdown(str(req.get('requester_name', '?')), version=1)}\n"
              f"*Sent:* {when}")
    # The member writes in their own language; say which, because the bot does
    # not translate prayer requests and the reader may not share it.
    if lang != "en":
        header += f"  ·  _language: {lang}_"
    body = escape_markdown(str(req.get("text", "")), version=1)
    return f"{header}\n\n{body}\n\n_Reply with_ `/respondprayer {req.get('id', '')}`"


# ---------------------------------------------------------------------------
# Member: /prayer
# ---------------------------------------------------------------------------

async def cmd_prayer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("prayer", uid, uname, dname)
    _, lang = await get_user_prefs(uid)
    context.user_data.pop("pr_text", None)
    await update.message.reply_text(t("prayer_prompt", lang, limit=PRAYER_MAX),
                                    parse_mode=ParseMode.MARKDOWN)
    return PR_TEXT


async def pr_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, _, _ = user_info(update)
    _, lang = await get_user_prefs(uid)
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text(t("prayer_empty", lang))
        return PR_TEXT
    if len(text) > PRAYER_MAX:
        await update.message.reply_text(
            t("prayer_too_long", lang, limit=PRAYER_MAX, actual=len(text)))
        return PR_TEXT

    context.user_data["pr_text"] = text
    await update.message.reply_text(
        t("prayer_confirm", lang, request=escape_markdown(text, version=1)),
        parse_mode=ParseMode.MARKDOWN)
    return PR_CONFIRM


async def pr_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    _, lang = await get_user_prefs(uid)
    answer = (update.message.text or "").strip().lower()
    text = context.user_data.get("pr_text", "")

    if answer in ("cancel", "no", "n", "/cancel"):
        context.user_data.pop("pr_text", None)
        await update.message.reply_text(t("prayer_cancelled", lang))
        return ConversationHandler.END

    if answer in ("modify", "edit", "change"):
        # A bot cannot pre-fill the compose box, so the next best thing is to
        # hand the text back in a monospace block — tap-to-copy on mobile —
        # and let them paste, edit and resend.
        await update.message.reply_text(
            t("prayer_modify", lang, request=text), parse_mode=ParseMode.MARKDOWN)
        return PR_TEXT

    if not _is_affirmative(answer):
        await update.message.reply_text(t("prayer_confirm_unclear", lang))
        return PR_CONFIRM

    req = {
        "id": new_request_id(),
        "requester_chat_id": uid,
        "requester_name": dname,
        "requester_username": uname,
        "lang": lang,
        "text": text,
        "created": now_tz().isoformat(),
        "status": STATUS_PENDING,
    }
    requests = await storage.get_prayer_requests()
    requests.append(req)
    await storage.save_prayer_requests(requests)
    context.user_data.pop("pr_text", None)
    activity.log_command("prayer", uid, uname, dname, details=f"submitted {req['id']}")

    sent = await _deliver_to_prayer_admins(context.bot, req)
    if not sent:
        # Stored and acknowledged regardless — a request must never be lost
        # because nobody was reachable — but this needs someone's attention.
        logger.error("Prayer request %s stored but no prayer admin was reachable.",
                     req["id"])
        activity.log_error(
            f"Prayer request {req['id']} stored but no prayer admin was reachable")

    await update.message.reply_text(
        t("prayer_received", lang, request_id=req["id"]),
        parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


async def _deliver_to_prayer_admins(bot, req: dict[str, Any]) -> int:
    """Forward a new request. Returns how many admins received it."""
    text = format_for_admin(req)
    sent = 0
    for chat_id in await prayer_admin_chat_ids():
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except TelegramError as exc:
            logger.warning("Could not send prayer request %s to %s: %s",
                           req.get("id"), chat_id, exc)
    return sent


# ---------------------------------------------------------------------------
# Prayer admin: listing, answering, dismissing
# ---------------------------------------------------------------------------

@permissions.prayer_admin_only
async def cmd_prayerrequests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List pending requests, newest last, with their ids."""
    uid, uname, dname = user_info(update)
    activity.log_command("prayerrequests", uid, uname, dname)
    pending = pending_requests(await storage.get_prayer_requests())
    if not pending:
        await update.message.reply_text("🙏 No prayer requests are waiting.")
        return

    parts = [f"🙏 *{len(pending)} prayer request(s) waiting*\n"]
    for r in pending:
        created = _created_dt(r)
        when = created.strftime("%Y-%m-%d %H:%M") if created else "?"
        who = escape_markdown(str(r.get("requester_name", "?")), version=1)
        preview = str(r.get("text", ""))
        if len(preview) > 160:
            preview = preview[:157] + "…"
        parts.append(
            f"`{r.get('id')}` · {when} · {who}\n{escape_markdown(preview, version=1)}")
    parts.append("_Answer with_ `/respondprayer <id>` _· dismiss with_ "
                 "`/dismissprayer <id>`")
    await update.message.reply_text("\n\n".join(parts), parse_mode=ParseMode.MARKDOWN)


@permissions.prayer_admin_only
async def cmd_respondprayer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin answering a request: /respondprayer PR-4A7C2E"""
    uid, uname, dname = user_info(update)
    activity.log_command("respondprayer", uid, uname, dname)
    requests = await storage.get_prayer_requests()
    pending = pending_requests(requests)

    arg = (context.args[0] if context.args else "").strip()
    if not arg:
        if not pending:
            await update.message.reply_text("🙏 No prayer requests are waiting.")
            return ConversationHandler.END
        ids = "\n".join(f"  `{r.get('id')}` — "
                        f"{escape_markdown(str(r.get('requester_name', '?')), version=1)}"
                        for r in pending)
        await update.message.reply_text(
            f"Which request? Send `/respondprayer <id>`\n\n{ids}",
            parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    req = find_request(requests, arg)
    if req is None:
        await update.message.reply_text(
            f"No prayer request with id `{escape_markdown(arg, version=1)}`. "
            "Use /prayerrequests to see what is waiting.",
            parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    if req.get("status") != STATUS_PENDING:
        closed_by = escape_markdown(str(req.get("closed_by", "someone")), version=1)
        await update.message.reply_text(
            f"`{req['id']}` was already {req.get('status')} by {closed_by}.",
            parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    context.user_data["pr_respond_id"] = req["id"]
    await update.message.reply_text(
        f"Replying to `{req['id']}`. Send your response — it will be "
        "delivered to the member, and the request text will then be deleted.\n\n"
        "Send /cancel to stop.",
        parse_mode=ParseMode.MARKDOWN)
    return RESP_TEXT


async def resp_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Deliver the response, then redact the request and tell the other admins."""
    uid, uname, dname = user_info(update)
    response = (update.message.text or "").strip()
    if not response:
        await update.message.reply_text("Please send the text of your response, "
                                        "or /cancel.")
        return RESP_TEXT

    req_id = context.user_data.get("pr_respond_id")
    requests = await storage.get_prayer_requests()
    req = find_request(requests, req_id or "")
    if req is None or req.get("status") != STATUS_PENDING:
        await update.message.reply_text("That request is no longer waiting.")
        context.user_data.pop("pr_respond_id", None)
        return ConversationHandler.END

    created = _created_dt(req)
    member_lang = str(req.get("lang", "en"))
    when = created.strftime("%-d %B %Y") if created else "?"
    body = t("prayer_response_intro", member_lang, date=when,
             request_id=req["id"]) + "\n\n" + escape_markdown(response, version=1)

    delivered = False
    try:
        await context.bot.send_message(req["requester_chat_id"], body,
                                       parse_mode=ParseMode.MARKDOWN)
        delivered = True
    except TelegramError as exc:
        logger.warning("Could not deliver prayer response %s: %s", req["id"], exc)

    if not delivered:
        # Leave it pending: an undelivered response is not an answered request.
        await update.message.reply_text(
            f"⚠️ Could not deliver the response to `{req['id']}` — the member may "
            "have blocked the bot. The request is still waiting.",
            parse_mode=ParseMode.MARKDOWN)
        context.user_data.pop("pr_respond_id", None)
        return ConversationHandler.END

    redact(req, STATUS_ANSWERED, dname)
    await storage.save_prayer_requests(requests)
    activity.log_command("respondprayer", uid, uname, dname,
                         details=f"answered {req['id']}")
    await update.message.reply_text(
        f"✅ Response sent for `{req['id']}`. The request text has been deleted.",
        parse_mode=ParseMode.MARKDOWN)
    await _notify_other_prayer_admins(
        context.bot, req, uid,
        f"🙏 `{req['id']}` has been answered by "
        f"{escape_markdown(dname, version=1)}.")
    context.user_data.pop("pr_respond_id", None)
    return ConversationHandler.END


@permissions.prayer_admin_only
async def cmd_dismissprayer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Discard a request without responding. The member is NOT told.

    Deliberate: the confirmation they already received stands on its own, so
    silence is not a broken promise.
    """
    uid, uname, dname = user_info(update)
    activity.log_command("dismissprayer", uid, uname, dname)
    arg = (context.args[0] if context.args else "").strip()
    if not arg:
        await update.message.reply_text(
            "Send `/dismissprayer <id>` — see /prayerrequests for the ids.",
            parse_mode=ParseMode.MARKDOWN)
        return

    requests = await storage.get_prayer_requests()
    req = find_request(requests, arg)
    if req is None:
        await update.message.reply_text(
            f"No prayer request with id `{escape_markdown(arg, version=1)}`.",
            parse_mode=ParseMode.MARKDOWN)
        return
    if req.get("status") != STATUS_PENDING:
        await update.message.reply_text(
            f"`{req['id']}` was already {req.get('status')}.",
            parse_mode=ParseMode.MARKDOWN)
        return

    redact(req, STATUS_DISMISSED, dname)
    await storage.save_prayer_requests(requests)
    activity.log_command("dismissprayer", uid, uname, dname,
                         details=f"dismissed {req['id']}")
    await update.message.reply_text(
        f"`{req['id']}` dismissed and its text deleted. The member was not "
        "notified.", parse_mode=ParseMode.MARKDOWN)
    await _notify_other_prayer_admins(
        context.bot, req, uid,
        f"🙏 `{req['id']}` was dismissed by {escape_markdown(dname, version=1)}.")


async def _notify_other_prayer_admins(bot, req: dict[str, Any], actor_id: int,
                                      message: str) -> int:
    """Tell the other prayer admins, so nobody answers the same request twice."""
    sent = 0
    for chat_id in await prayer_admin_chat_ids():
        if chat_id == actor_id:
            continue
        try:
            await bot.send_message(chat_id, message, parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except TelegramError as exc:
            logger.warning("Could not notify prayer admin %s: %s", chat_id, exc)
    return sent

"""User-facing basics and small admin listings.

/start registration and contact sharing, /help (with per-command topics),
/events, /exportcalendar, /donate, the per-user /settimezone and /language
preferences, /notifications opt-ins, /stop, and the admin /adminhelp,
/usercount, /userlist, /listevents views.
"""

from __future__ import annotations

import io
import logging
import re

import permissions
import pytz
import storage
from common import (
    NOTIF_CATEGORIES,
    _NOTIF_CATEGORY_KEYS,
    _answer_cb,
    format_dt,
    get_user_prefs,
    now_tz,
    user_notif_prefs,
)
from events import all_upcoming
from ics_generator import events_to_ics
from localization import AVAILABLE_LANGUAGES, DEFAULT_LANG, t
from pdf_generator import generate_user_list_pdf
from permissions import admin_only, user_info
from settings import BOT_DISPLAY_NAME, DONATION_URL, activity
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler
from telegram.helpers import escape_markdown

logger = logging.getLogger(__name__)

ADMIN_COMMANDS_TEXT = """\
*Admin commands:*
/addevent — add a special event
/modifyevent — modify an event
/deleteevent — remove or annotate an event
/setservicelink — set the join link for a convocation/Sabbath service
/broadcast — send a message to groups and/or all subscribers
/addannouncement — create an announcement (title, body, expiration)
/listannouncements — list active and recently-expired announcements
/delannouncement — expire an active announcement now
/listevents — events in the next 30 days (admin view)
/usercount — number of registered users
/userlist — list registered users
/stats — usage and system stats (/stats usage, /stats 30)
/backup — send a data backup now (also runs nightly at 3am)
/adminhelp — show this list"""


def _commands_text(lang: str, is_adm: bool) -> str:
    """Localized user command list, with admin commands appended if applicable."""
    text = t("user_commands", lang)
    if is_adm:
        text += "\n\n" + ADMIN_COMMANDS_TEXT
    return text


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    users = await storage.get_all_users()

    is_new = not any(u["chat_id"] == uid for u in users)
    if is_new:
        users.append({
            "chat_id": uid,
            "username": uname,
            "display_name": dname,
            "joined": now_tz().isoformat(),
        })
        await storage.save_users(users)
        activity.log_user_joined(uid, uname, dname)

    # Try to match by username first (no extra step needed)
    await _register_official_if_known(uid, uname)
    await permissions._register_admin_by_username(uid, uname)

    _, lang = await get_user_prefs(uid)

    # If not yet identified as admin/official by username, request contact share
    # so phone-number-only admins/officials can be recognised.
    already_known = permissions.is_admin(update) or permissions._is_known_official(uid, uname)
    if not already_known:
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton(t("share_contact_button", lang), request_contact=True)]],
            one_time_keyboard=True,
            resize_keyboard=True,
        )
        await update.message.reply_text(
            t("share_contact_prompt", lang),
            reply_markup=kb,
        )

    is_adm = permissions.is_admin(update)
    cmd_text = _commands_text(lang, is_adm)

    await update.message.reply_text(
        t("welcome", lang, bot_name=BOT_DISPLAY_NAME, commands=cmd_text),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove() if already_known else None,
    )
    activity.log_command("start", uid, uname, dname)


async def _register_official_if_known(
    user_id: int, username: str | None, phone: str | None = None
) -> None:
    """Store chat_id for officials/admins who have started the bot.

    Matches on telegram_username or phone (digits-only comparison).
    """
    uname_lower = (username or "").lstrip("@").lower()
    phone_norm = re.sub(r"\D", "", phone or "")

    def _match_and_set(rec: dict) -> bool:
        matched = False
        if uname_lower:
            rname = (rec.get("telegram_username") or "").lstrip("@").lower()
            if rname and rname == uname_lower:
                matched = True
        if not matched and phone_norm:
            rphone = re.sub(r"\D", "", rec.get("phone") or "")
            if rphone and rphone == phone_norm:
                matched = True
        if matched and rec.get("chat_id") != user_id:
            rec["chat_id"] = user_id
            return True
        return False

    changed = False
    for off in permissions.OFFICIALS:
        if _match_and_set(off):
            changed = True
        for proxy in off.get("proxies") or []:
            if _match_and_set(proxy):
                changed = True
    if changed:
        permissions._save_officials()


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    _, lang = await get_user_prefs(uid)
    users = await storage.get_all_users()
    users = [u for u in users if u["chat_id"] != uid]
    await storage.save_users(users)
    activity.log_user_left(uid, uname, dname)
    activity.log_command("stop", uid, uname, dname)
    await update.message.reply_text(t("unsubscribed", lang))


# ---------------------------------------------------------------------------
# /help  /events  /exportcalendar
# ---------------------------------------------------------------------------

# /help subtopics -> catalog key with a detailed explanation.
HELP_TOPICS = {
    "appointment": "help_appointment",
    "myappointments": "help_myappointments",
    "cancelappointment": "help_cancelappointment",
    "reschedule": "help_reschedule",
    "events": "help_events",
    "exportcalendar": "help_exportcalendar",
    "settimezone": "help_settimezone",
    "language": "help_language",
    "notifications": "help_notifications",
    "donate": "help_donate",
    "announcements": "help_announcements",
}


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    _, lang = await get_user_prefs(uid)

    topic = (context.args[0].lstrip("/").lower() if context.args else None)
    if topic:
        activity.log_command("help", uid, uname, dname, details=f"topic={topic}")
        if topic in HELP_TOPICS:
            await update.message.reply_text(t(HELP_TOPICS[topic], lang),
                                            parse_mode=ParseMode.MARKDOWN)
        else:
            topics = ", ".join(f"`{x}`" for x in HELP_TOPICS)
            await update.message.reply_text(
                t("help_unknown_topic", lang, topics=topics),
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    activity.log_command("help", uid, uname, dname)
    text = _commands_text(lang, permissions.is_admin(update))
    # Hint that per-command help is available.
    text += "\n\n" + t("help_topic_hint", lang)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_donate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("donate", uid, uname, dname)
    _, lang = await get_user_prefs(uid)
    if not DONATION_URL:
        await update.message.reply_text(t("donate_not_configured", lang))
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(t("donate_button", lang), url=DONATION_URL)
    ]])
    await update.message.reply_text(
        t("donate_message", lang),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("events", uid, uname, dname)
    tz, lang = await get_user_prefs(uid)
    events = await all_upcoming(days_ahead=30)
    if not events:
        await update.message.reply_text(t("events_none", lang))
        return
    lines = [t("events_header", lang)]
    for ev in events:
        dt_str = format_dt(ev["service_time"], tz, lang)
        lines.append(f"📅 *{ev['name']}*\n   {dt_str}")
        if ev.get("url"):
            lines.append(f"   🔗 {ev['url']}")
        if ev.get("announcements"):
            for a in ev["announcements"]:
                lines.append(f"   ⚠️ {a}")
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_export_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("exportcalendar", uid, uname, dname)
    events = await all_upcoming(days_ahead=365)
    ics_bytes = events_to_ics(events, calendar_name=BOT_DISPLAY_NAME)
    bio = io.BytesIO(ics_bytes)
    bio.name = "kingdom_events.ics"
    await update.message.reply_document(
        document=InputFile(bio, filename="kingdom_events.ics"),
        caption=f"📅 {BOT_DISPLAY_NAME} — upcoming events calendar",
    )


# ---------------------------------------------------------------------------
# Admin guard decorator
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# /announcements — general announcements (user view + admin management)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# /adminhelp  /usercount  /userlist  /listevents
# ---------------------------------------------------------------------------

@admin_only
async def cmd_adminhelp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("adminhelp", uid, uname, dname)
    await update.message.reply_text(ADMIN_COMMANDS_TEXT, parse_mode=ParseMode.MARKDOWN)


@admin_only
async def cmd_usercount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("usercount", uid, uname, dname)
    users = await storage.get_all_users()
    await update.message.reply_text(f"👥 Total registered users: *{len(users)}*",
                                    parse_mode=ParseMode.MARKDOWN)


@admin_only
async def cmd_userlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("userlist", uid, uname, dname)
    users = await storage.get_all_users()
    if not users:
        await update.message.reply_text("No registered users.")
        return
    if len(users) > 100:
        pdf_buf = generate_user_list_pdf(users)
        await update.message.reply_document(
            document=InputFile(pdf_buf, filename="user_list.pdf"),
            caption=f"User list ({len(users)} users)",
        )
        return
    lines = [f"👥 *Registered Users ({len(users)}):*\n"]
    for i, u in enumerate(users, 1):
        dn = escape_markdown(u.get("display_name") or "—", version=1)
        un = ("@" + escape_markdown(u["username"], version=1)) if u.get("username") else "—"
        lines.append(f"{i}. {dn} ({un})")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@admin_only
async def cmd_listevents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("listevents", uid, uname, dname)
    events = await all_upcoming(days_ahead=30)
    if not events:
        await update.message.reply_text("No events in the next 30 days.")
        return
    lines = ["*Events — Next 30 Days (admin view):*\n"]
    for ev in events:
        dt_str = format_dt(ev["service_time"])
        notif_str = format_dt(ev["notification_time"])
        etype = ev.get("type", "?")
        lines.append(
            f"📅 *{ev['name']}*\n"
            f"   Service: {dt_str}\n"
            f"   Notify: {notif_str}\n"
            f"   Type: {etype}"
        )
        if ev.get("announcements"):
            for a in ev["announcements"]:
                lines.append(f"   ⚠️ {a}")
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# /addevent — multi-step conversation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /broadcast — admin sends an ad-hoc message to groups and/or all subscribers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# /settimezone — per-user time zone preference
# ---------------------------------------------------------------------------

TZ_SELECT = 0

# A short menu of common zones; users may also type any IANA name.
COMMON_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "Europe/London",
    "Africa/Lagos",
]


async def _set_user_field(chat_id: int, field: str, value: str) -> None:
    """Persist a single preference field on the user's record."""
    users = await storage.get_all_users()
    for u in users:
        if u.get("chat_id") == chat_id:
            u[field] = value
            await storage.save_users(users)
            return


CB_TZ_PREFIX = "tz:"


async def _apply_timezone(uid: int, lang: str, tz_name: str) -> "str | None":
    """Validate and persist a timezone; return the confirmation text, or None if invalid."""
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        return None
    await _set_user_field(uid, "timezone", tz_name)
    return t("tz_set", lang, tz=tz_name, now=format_dt(now_tz(), tz, lang))


async def cmd_settimezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("settimezone", uid, uname, dname)
    _, lang = await get_user_prefs(uid)
    rows = [
        [InlineKeyboardButton(name, callback_data=f"{CB_TZ_PREFIX}{i}")]
        for i, name in enumerate(COMMON_TIMEZONES)
    ]
    await update.message.reply_text(
        t("tz_prompt", lang),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return TZ_SELECT


async def tz_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A common-zone button was tapped."""
    query = update.callback_query
    await _answer_cb(query)
    uid, uname, dname = user_info(update)
    _, lang = await get_user_prefs(uid)
    idx = query.data[len(CB_TZ_PREFIX):]
    if not idx.isdigit() or not (0 <= int(idx) < len(COMMON_TIMEZONES)):
        await query.edit_message_text(t("tz_invalid", lang))
        return ConversationHandler.END
    tz_name = COMMON_TIMEZONES[int(idx)]
    msg = await _apply_timezone(uid, lang, tz_name)
    activity.log_command("settimezone", uid, uname, dname, details=f"tz={tz_name}")
    await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


async def tz_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A timezone (or list number) was typed instead of tapped."""
    uid, uname, dname = user_info(update)
    _, lang = await get_user_prefs(uid)
    text = update.message.text.strip()
    if text.isdigit() and 1 <= int(text) <= len(COMMON_TIMEZONES):
        tz_name = COMMON_TIMEZONES[int(text) - 1]
    else:
        tz_name = text
    msg = await _apply_timezone(uid, lang, tz_name)
    if msg is None:
        await update.message.reply_text(t("tz_invalid", lang))
        return TZ_SELECT
    activity.log_command("settimezone", uid, uname, dname, details=f"tz={tz_name}")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /language — per-user language preference
# ---------------------------------------------------------------------------

LANG_SELECT = 0

CB_LANG_PREFIX = "lang:"


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("language", uid, uname, dname)
    _, lang = await get_user_prefs(uid)
    rows = [
        [InlineKeyboardButton(name, callback_data=f"{CB_LANG_PREFIX}{code}")]
        for code, name in AVAILABLE_LANGUAGES.items()
    ]
    await update.message.reply_text(
        t("lang_prompt", lang),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return LANG_SELECT


async def lang_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _answer_cb(query)
    uid, uname, dname = user_info(update)
    code = query.data[len(CB_LANG_PREFIX):]
    if code not in AVAILABLE_LANGUAGES:
        await query.edit_message_text(t("lang_set", DEFAULT_LANG,
                                        language=AVAILABLE_LANGUAGES[DEFAULT_LANG]))
        return ConversationHandler.END
    await _set_user_field(uid, "language", code)
    activity.log_command("language", uid, uname, dname, details=f"lang={code}")
    await query.edit_message_text(
        t("lang_set", code, language=AVAILABLE_LANGUAGES[code]),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /notifications — opt-in personal reminders by event category
# ---------------------------------------------------------------------------

CB_NOTIFPREF_PREFIX = "np:"


def _notif_prefs_keyboard(prefs: set, lang: str) -> InlineKeyboardMarkup:
    rows = []
    for key, label_key in NOTIF_CATEGORIES:
        mark = "✅ " if key in prefs else "▫️ "
        rows.append([InlineKeyboardButton(
            mark + t(label_key, lang), callback_data=f"{CB_NOTIFPREF_PREFIX}toggle:{key}"
        )])
    rows.append([InlineKeyboardButton(
        t("notif_prefs_done", lang), callback_data=f"{CB_NOTIFPREF_PREFIX}done"
    )])
    return InlineKeyboardMarkup(rows)


def _notif_prefs_summary(prefs: set, lang: str) -> str:
    if not prefs:
        return t("notif_prefs_none", lang)
    lines = [t(lk, lang) for k, lk in NOTIF_CATEGORIES if k in prefs]
    return t("notif_prefs_saved", lang, list="\n".join(f"• {x}" for x in lines))


async def _get_user_notif_prefs(chat_id: int) -> set:
    users = await storage.get_all_users()
    rec = next((u for u in users if u.get("chat_id") == chat_id), None)
    return user_notif_prefs(rec)


async def _set_user_notif_prefs(chat_id: int, uname: "str | None",
                                dname: str, prefs: set) -> None:
    """Persist a user's notification categories, creating a record if needed
    (so friends-of-the-congregation can opt in without a full /start first)."""
    users = await storage.get_all_users()
    rec = next((u for u in users if u.get("chat_id") == chat_id), None)
    if rec is None:
        rec = {"chat_id": chat_id, "username": uname, "display_name": dname,
               "joined": now_tz().isoformat()}
        users.append(rec)
    rec["notif_prefs"] = sorted(prefs)
    await storage.save_users(users)


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("notifications", uid, uname, dname)
    _, lang = await get_user_prefs(uid)
    prefs = await _get_user_notif_prefs(uid)
    await update.message.reply_text(
        t("notif_prefs_prompt", lang),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_notif_prefs_keyboard(prefs, lang),
    )


async def notif_prefs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _answer_cb(query)
    uid, uname, dname = user_info(update)
    _, lang = await get_user_prefs(uid)
    action = query.data[len(CB_NOTIFPREF_PREFIX):]

    if action == "done":
        prefs = await _get_user_notif_prefs(uid)
        await query.edit_message_text(_notif_prefs_summary(prefs, lang),
                                      parse_mode=ParseMode.MARKDOWN)
        return

    if action.startswith("toggle:"):
        cat = action.split(":", 1)[1]
        if cat not in _NOTIF_CATEGORY_KEYS:
            return
        prefs = await _get_user_notif_prefs(uid)
        prefs.discard(cat) if cat in prefs else prefs.add(cat)
        await _set_user_notif_prefs(uid, uname, dname, prefs)
        activity.log_command("notifications", uid, uname, dname,
                             details=f"{cat}={'on' if cat in prefs else 'off'}")
        await query.edit_message_reply_markup(reply_markup=_notif_prefs_keyboard(prefs, lang))



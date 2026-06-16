"""
Kingdom Events Telegram Bot
===========================
Sends notifications for Hebrew-calendar convocations and special events,
manages appointment requests between congregants and officials, and provides
admin tools for event management.

Usage:
  1. Copy config/config.yaml and set bot.token
  2. Add admin usernames to config/admins.yaml
  3. pip install -r requirements.txt
  4. python bot.py
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytz
import yaml
from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from activity_logger import ActivityLogger
from cache import FileCache
from hebrew_calendar import all_upcoming_events, upcoming_convocation_events, sabbath_events
from ics_generator import appointment_to_ics, events_to_ics
from pdf_generator import generate_user_list_pdf

# ---------------------------------------------------------------------------
# Boot-time configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"

with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as _f:
    _CFG = yaml.safe_load(_f)

BOT_TOKEN: str = _CFG["bot"]["token"]
TZ = pytz.timezone(_CFG["bot"]["timezone"])
BOT_DISPLAY_NAME: str = _CFG["bot"].get("display_name", "Kingdom Events Bot")
DATA_DIR = BASE_DIR / _CFG["paths"]["data_dir"]
LOGS_DIR = BASE_DIR / _CFG["paths"]["logs_dir"]
GEN_DIR = BASE_DIR / _CFG["paths"]["generated_dir"]
LOG_RETENTION = _CFG["log"]["retention_days"]
DEFAULT_NOTIF_MIN: int = _CFG["notifications"]["default_minutes_before"]

for _d in (DATA_DIR, LOGS_DIR, GEN_DIR):
    _d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class _GetUpdatesFilter(logging.Filter):
    """Suppress repetitive successful getUpdates log lines from httpx.

    Allows through:
      - The very first getUpdates request (replaced with a friendlier message)
      - Any response with an HTTP 4xx or 5xx status code
    Suppresses everything else that mentions getUpdates.
    """

    def __init__(self) -> None:
        super().__init__()
        self._seen_first = False

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "getUpdates" not in msg:
            return True  # unrelated record — pass through unchanged

        # Check for error status codes (4xx / 5xx) in the httpx message format:
        # 'HTTP Request: POST ... getUpdates "HTTP/1.1 4xx ..."'
        import re
        status_match = re.search(r'"HTTP/[\d.]+ (\d{3})', msg)
        if status_match and int(status_match.group(1)) >= 400:
            return True  # always log errors

        if not self._seen_first:
            self._seen_first = True
            # Replace the raw httpx message with a friendlier one-time notice
            record.msg = (
                "Long polling started — using getUpdates to check for incoming messages"
            )
            record.args = ()
            return True

        return False  # suppress subsequent successful getUpdates calls


_get_updates_filter = _GetUpdatesFilter()
logging.getLogger("httpx").addFilter(_get_updates_filter)

activity = ActivityLogger(LOGS_DIR, retention_days=LOG_RETENTION, tz=TZ)

# ---------------------------------------------------------------------------
# File caches
# ---------------------------------------------------------------------------

events_cache = FileCache(DATA_DIR / "events.yaml")
users_cache = FileCache(DATA_DIR / "users.yaml")
appts_cache = FileCache(DATA_DIR / "appointments.yaml")

# Admins are loaded once at startup and kept in memory.
# Each entry may carry a `username`, a `phone`, or both.
_admins_raw = yaml.safe_load((CONFIG_DIR / "admins.yaml").read_text()) or {}
ADMIN_USERNAMES: set[str] = {
    a["username"].lstrip("@").lower()
    for a in _admins_raw.get("admins", [])
    if a.get("username")
}
ADMIN_PHONES: set[str] = {
    re.sub(r"\D", "", a["phone"])
    for a in _admins_raw.get("admins", [])
    if a.get("phone")
}
# chat_id → True for admins identified by phone after they /start the bot
_admin_chat_ids: set[int] = set()

# Officials
_officials_raw = yaml.safe_load((CONFIG_DIR / "officials.yaml").read_text()) or {}
OFFICIALS: list[dict[str, Any]] = _officials_raw.get("officials", [])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_admin(update: Update) -> bool:
    """Return True if the user is listed in admins.yaml by username or phone."""
    u = update.effective_user
    if (u.username or "").lower() in ADMIN_USERNAMES:
        return True
    if u.id in _admin_chat_ids:
        return True
    return False


async def _register_admin_by_phone(user_id: int, phone: str | None) -> None:
    """Cache chat_id when a phone-number-only admin shares their contact."""
    if not phone:
        return
    normalized = re.sub(r"\D", "", phone)
    if normalized in ADMIN_PHONES:
        _admin_chat_ids.add(user_id)


async def _register_admin_by_username(user_id: int, username: str | None) -> None:
    """Cache chat_id for username-based admins (no-op if already in set)."""
    if (username or "").lower() in ADMIN_USERNAMES:
        _admin_chat_ids.add(user_id)


def _is_known_official(user_id: int, username: str | None) -> bool:
    """Return True if this user is already linked to an official entry."""
    uname_lower = (username or "").lstrip("@").lower()
    for off in OFFICIALS:
        if off.get("chat_id") == user_id:
            return True
        if uname_lower:
            oname = (off.get("telegram_username") or "").lstrip("@").lower()
            if oname and oname == uname_lower:
                return True
    return False


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a shared contact — used to identify phone-number-only admins/officials."""
    contact = update.message.contact
    # Only process contacts the user shares about themselves
    if contact.user_id != update.effective_user.id:
        await update.message.reply_text(
            "Please share your own contact, not someone else's.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    uid, uname, dname = user_info(update)
    phone = contact.phone_number  # e.g. "+17572863574" or "17572863574"

    await _register_admin_by_phone(uid, phone)
    await _register_official_if_known(uid, uname, phone)

    # Store phone in user record
    users = await get_all_users()
    for u in users:
        if u["chat_id"] == uid and not u.get("phone"):
            u["phone"] = re.sub(r"\D", "", phone)
            await save_users(users)
            break

    is_adm = is_admin(update)
    if is_adm:
        reply = "✅ Contact received. You have been recognised as an administrator."
        cmd_text = USER_COMMANDS_TEXT + "\n\n" + ADMIN_COMMANDS_TEXT
    elif _is_known_official(uid, uname):
        reply = "✅ Contact received. You have been recognised as an official."
        cmd_text = USER_COMMANDS_TEXT
    else:
        reply = "✅ Contact received. Thank you!"
        cmd_text = USER_COMMANDS_TEXT

    await update.message.reply_text(reply, reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(cmd_text, parse_mode=ParseMode.MARKDOWN)
    phone_digits = re.sub(r"\D", "", phone)
    activity.log_command("contact_share", uid, uname, dname, details=f"phone={phone_digits}")


def user_info(update: Update) -> tuple[int, str | None, str]:
    u = update.effective_user
    return u.id, u.username, u.full_name or u.first_name or str(u.id)


async def get_all_users() -> list[dict[str, Any]]:
    data = await users_cache.get()
    return data.get("users", []) if data else []


async def save_users(users: list[dict[str, Any]]) -> None:
    data = users_cache._data or {}
    data["users"] = users
    await users_cache.save(data)


async def get_all_events_data() -> dict[str, Any]:
    data = await events_cache.get()
    return data or {}


async def save_events_data(data: dict[str, Any]) -> None:
    await events_cache.save(data)


async def get_appointments() -> list[dict[str, Any]]:
    data = await appts_cache.get()
    return data.get("appointments", []) if data else []


async def save_appointments(appts: list[dict[str, Any]]) -> None:
    data = appts_cache._data or {}
    data["appointments"] = appts
    await appts_cache.save(data)


def format_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%A, %B %d, %Y at %I:%M %p %Z")


def now_tz() -> datetime:
    return datetime.now(TZ)


def _merge_special_events(
    special_defs: list[dict[str, Any]],
    announcements_map: dict[str, list[str]],
    days_ahead: int = 90,
) -> list[dict[str, Any]]:
    """Expand special_events definitions into concrete upcoming event dicts.

    sunday_morning_prayer is excluded here because hebrew_calendar.py already
    generates it via sunday_prayer_events(), avoiding duplicates.
    """
    now = now_tz()
    cutoff = now + timedelta(days=days_ahead)
    results: list[dict[str, Any]] = []
    for defn in special_defs:
        if defn.get("id") == "sunday_morning_prayer":
            continue
        if not defn.get("active", True):
            continue
        etype = defn.get("type", "once")
        if etype == "weekly":
            wd_target = int(defn["weekday"])
            current = now.date()
            end = cutoff.date()
            while current <= end:
                if current.weekday() == wd_target:
                    h, m = [int(x) for x in defn["time"].split(":")]
                    svc_dt = TZ.localize(datetime(current.year, current.month, current.day, h, m))
                    notif_dt = svc_dt - timedelta(minutes=int(defn.get("notification_minutes", DEFAULT_NOTIF_MIN)))
                    if svc_dt > now:
                        key = f"{defn['id']}_{current.isoformat()}"
                        results.append({
                            "key": key,
                            "name": defn["name"],
                            "type": "special",
                            "service_time": svc_dt,
                            "notification_time": notif_dt,
                            "duration_minutes": defn.get("duration_minutes", 60),
                            "description": defn.get("description", ""),
                            "announcements": announcements_map.get(key, []),
                        })
                current += timedelta(days=1)
        elif etype == "once":
            date_str = defn.get("date")
            if not date_str:
                continue
            h, m = [int(x) for x in defn["time"].split(":")]
            from datetime import date as _date
            parts = [int(x) for x in date_str.split("-")]
            svc_dt = TZ.localize(datetime(parts[0], parts[1], parts[2], h, m))
            notif_dt = svc_dt - timedelta(minutes=int(defn.get("notification_minutes", DEFAULT_NOTIF_MIN)))
            if now <= svc_dt <= cutoff:
                results.append({
                    "key": defn["id"],
                    "name": defn["name"],
                    "type": "special",
                    "service_time": svc_dt,
                    "notification_time": notif_dt,
                    "duration_minutes": defn.get("duration_minutes", 60),
                    "description": defn.get("description", ""),
                    "announcements": announcements_map.get(defn["id"], []),
                })
    return results


async def all_upcoming(days_ahead: int = 90) -> list[dict[str, Any]]:
    """Return all events (convocations + special) sorted by service_time."""
    evdata = await get_all_events_data()
    announcements_map: dict[str, list[str]] = evdata.get("convocation_announcements", {})
    special_defs: list[dict[str, Any]] = evdata.get("special_events", [])

    convocations = all_upcoming_events(TZ, days_ahead)
    # Attach any urgent announcements to convocations
    for ev in convocations:
        ev["announcements"] = announcements_map.get(ev["key"], [])

    specials = _merge_special_events(special_defs, announcements_map, days_ahead)

    merged = convocations + specials
    merged.sort(key=lambda e: e["service_time"])
    return merged


# ---------------------------------------------------------------------------
# Notification sender
# ---------------------------------------------------------------------------

async def send_notification(context: ContextTypes.DEFAULT_TYPE) -> None:
    event: dict[str, Any] = context.job.data  # type: ignore[attr-defined]
    name = event["name"]
    svc_time = format_dt(event["service_time"])
    lines = [f"🔔 *Reminder: {name}*", f"Service begins: {svc_time}"]
    if event.get("description"):
        lines.append(f"\n_{event['description']}_")
    if event.get("announcements"):
        lines.append("\n⚠️ *Announcements:*")
        lines.extend(f"• {a}" for a in event["announcements"])
    text = "\n".join(lines)

    users = await get_all_users()
    sent = 0
    blocked: list[int] = []
    for u in users:
        try:
            await context.bot.send_message(u["chat_id"], text, parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except Forbidden:
            blocked.append(u["chat_id"])
        except TelegramError as exc:
            logger.warning("Notification send error for %s: %s", u.get("chat_id"), exc)

    activity.log_notification_sent(name, sent)

    if blocked:
        # Remove users who have blocked the bot
        remaining = [u for u in users if u["chat_id"] not in blocked]
        await save_users(remaining)
        for cid in blocked:
            activity.log_user_left(cid, None, None)


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


# ---------------------------------------------------------------------------
# /start — user registration
# ---------------------------------------------------------------------------

WELCOME = """👋 Welcome to *{bot_name}*!

I send reminders for God's Holy Convocations, special services, and events.

{commands}"""

USER_COMMANDS_TEXT = """\
*Available commands:*
/help — show this message
/events — upcoming events (next 30 days)
/exportcalendar — download an ICS calendar file
/appointment — request a meeting with a church official
/stop — unsubscribe from notifications"""

ADMIN_COMMANDS_TEXT = """\
*Admin commands:*
/addevent — add a special event
/modifyevent — modify an event
/deleteevent — remove or annotate an event
/listevents — events in the next 30 days (admin view)
/usercount — number of registered users
/userlist — list registered users
/adminhelp — show this list"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    users = await get_all_users()

    is_new = not any(u["chat_id"] == uid for u in users)
    if is_new:
        users.append({
            "chat_id": uid,
            "username": uname,
            "display_name": dname,
            "joined": now_tz().isoformat(),
        })
        await save_users(users)
        activity.log_user_joined(uid, uname, dname)

    # Try to match by username first (no extra step needed)
    await _register_official_if_known(uid, uname)
    await _register_admin_by_username(uid, uname)

    # If not yet identified as admin/official by username, request contact share
    # so phone-number-only admins/officials can be recognised.
    already_known = is_admin(update) or _is_known_official(uid, uname)
    if not already_known:
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📱 Share my contact", request_contact=True)]],
            one_time_keyboard=True,
            resize_keyboard=True,
        )
        await update.message.reply_text(
            "To personalise your experience, please share your contact "
            "(tap the button below). You can tap Skip if you prefer not to.",
            reply_markup=kb,
        )

    is_adm = is_admin(update)
    cmd_text = USER_COMMANDS_TEXT
    if is_adm:
        cmd_text += "\n\n" + ADMIN_COMMANDS_TEXT

    await update.message.reply_text(
        WELCOME.format(bot_name=BOT_DISPLAY_NAME, commands=cmd_text),
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
    changed = False
    for off in OFFICIALS:
        matched = False
        if uname_lower:
            oname = (off.get("telegram_username") or "").lstrip("@").lower()
            if oname and oname == uname_lower:
                matched = True
        if not matched and phone_norm:
            ophone = re.sub(r"\D", "", off.get("phone") or "")
            if ophone and ophone == phone_norm:
                matched = True
        if matched and off.get("chat_id") != user_id:
            off["chat_id"] = user_id
            changed = True
    if changed:
        raw = {"officials": OFFICIALS}
        with open(CONFIG_DIR / "officials.yaml", "w", encoding="utf-8") as fh:
            yaml.dump(raw, fh, default_flow_style=False, allow_unicode=True)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    users = await get_all_users()
    users = [u for u in users if u["chat_id"] != uid]
    await save_users(users)
    activity.log_user_left(uid, uname, dname)
    activity.log_command("stop", uid, uname, dname)
    await update.message.reply_text("You have been unsubscribed. Send /start to re-subscribe.")


# ---------------------------------------------------------------------------
# /help  /events  /exportcalendar
# ---------------------------------------------------------------------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("help", uid, uname, dname)
    is_adm = is_admin(update)
    text = USER_COMMANDS_TEXT
    if is_adm:
        text += "\n\n" + ADMIN_COMMANDS_TEXT
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("events", uid, uname, dname)
    events = await all_upcoming(days_ahead=30)
    if not events:
        await update.message.reply_text("No events in the next 30 days.")
        return
    lines = ["*Upcoming Events (next 30 days):*\n"]
    for ev in events:
        dt_str = format_dt(ev["service_time"])
        lines.append(f"📅 *{ev['name']}*\n   {dt_str}")
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

def admin_only(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            await update.message.reply_text("⛔ Unknown command.")
            return ConversationHandler.END
        return await handler(update, context)
    wrapper.__name__ = handler.__name__
    return wrapper


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
    users = await get_all_users()
    await update.message.reply_text(f"👥 Total registered users: *{len(users)}*", parse_mode=ParseMode.MARKDOWN)


@admin_only
async def cmd_userlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("userlist", uid, uname, dname)
    users = await get_all_users()
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
        dn = u.get("display_name") or "—"
        un = ("@" + u["username"]) if u.get("username") else "—"
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

(
    AE_NAME,
    AE_DATE,
    AE_TIME,
    AE_DURATION,
    AE_DESC,
    AE_NOTIF,
    AE_CONFIRM,
) = range(7)


@admin_only
async def cmd_addevent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("addevent", uid, uname, dname)
    context.user_data.clear()
    await update.message.reply_text("➕ *Add Special Event*\n\nEvent name:", parse_mode=ParseMode.MARKDOWN)
    return AE_NAME


async def ae_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["ae_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Date (YYYY-MM-DD) for a one-time event, or 'weekly:N' where N=0 Mon … 6 Sun:"
    )
    return AE_DATE


async def ae_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["ae_date"] = text
    await update.message.reply_text("Time (HH:MM, 24-hour):")
    return AE_TIME


async def ae_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = update.message.text.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", t):
        await update.message.reply_text("Please use HH:MM format (e.g. 19:00):")
        return AE_TIME
    context.user_data["ae_time"] = t
    await update.message.reply_text("Duration in minutes (press Enter/0 to skip):")
    return AE_DURATION


async def ae_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["ae_duration"] = int(text) if text.isdigit() else 60
    await update.message.reply_text("Description (or '-' to skip):")
    return AE_DESC


async def ae_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["ae_desc"] = "" if text == "-" else text
    await update.message.reply_text(
        f"Notification minutes before event (default {DEFAULT_NOTIF_MIN}):"
    )
    return AE_NOTIF


async def ae_notif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    notif = int(text) if text.isdigit() else DEFAULT_NOTIF_MIN
    context.user_data["ae_notif"] = notif
    d = context.user_data
    summary = (
        f"*New event summary:*\n"
        f"Name: {d['ae_name']}\n"
        f"Schedule: {d['ae_date']} at {d['ae_time']}\n"
        f"Duration: {d['ae_duration']} min\n"
        f"Description: {d.get('ae_desc') or '—'}\n"
        f"Notify: {notif} min before\n\n"
        f"Confirm? (yes/no)"
    )
    await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
    return AE_CONFIRM


async def ae_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.strip().lower() not in ("yes", "y"):
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    d = context.user_data
    date_raw: str = d["ae_date"]
    new_id = f"special_{uuid.uuid4().hex[:8]}"

    if date_raw.startswith("weekly:"):
        wd = int(date_raw.split(":")[1])
        new_defn = {
            "id": new_id,
            "name": d["ae_name"],
            "type": "weekly",
            "weekday": wd,
            "time": d["ae_time"],
            "duration_minutes": d["ae_duration"],
            "notification_minutes": d["ae_notif"],
            "description": d.get("ae_desc", ""),
            "active": True,
        }
    else:
        new_defn = {
            "id": new_id,
            "name": d["ae_name"],
            "type": "once",
            "date": date_raw,
            "time": d["ae_time"],
            "duration_minutes": d["ae_duration"],
            "notification_minutes": d["ae_notif"],
            "description": d.get("ae_desc", ""),
            "active": True,
        }

    evdata = await get_all_events_data()
    specials = evdata.get("special_events", [])
    specials.append(new_defn)
    evdata["special_events"] = specials
    await save_events_data(evdata)

    # Schedule notification for the new event
    app: Application = context.application
    fake_events = _merge_special_events([new_defn], {}, days_ahead=400)
    for ev in fake_events:
        schedule_event_notification(app, ev)

    uid, uname, dname = user_info(update)
    activity.log_command("addevent", uid, uname, dname, details=f"Added '{d['ae_name']}' (id:{new_id})")
    await update.message.reply_text(f"✅ Event added (ID: `{new_id}`)", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /modifyevent
# ---------------------------------------------------------------------------

ME_SELECT, ME_FIELD, ME_VALUE = range(3)


@admin_only
async def cmd_modifyevent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("modifyevent", uid, uname, dname)
    evdata = await get_all_events_data()
    specials = evdata.get("special_events", [])
    if not specials:
        await update.message.reply_text("No special events to modify.")
        return ConversationHandler.END
    lines = ["*Special Events:*\n"]
    for i, ev in enumerate(specials):
        lines.append(f"{i+1}. [{ev['id']}] {ev['name']}")
    lines.append("\nEnter the event number or ID to modify:")
    context.user_data["me_specials"] = specials
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    return ME_SELECT


async def me_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    specials: list[dict] = context.user_data["me_specials"]
    ev = None
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(specials):
            ev = specials[idx]
    else:
        ev = next((e for e in specials if e["id"] == text), None)
    if not ev:
        await update.message.reply_text("Event not found. Please try again:")
        return ME_SELECT
    context.user_data["me_event"] = ev
    await update.message.reply_text(
        f"Modifying: *{ev['name']}*\n\n"
        "Which field to change?\n"
        "date | time | duration | description | notification | name | active",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ME_FIELD


async def me_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field = update.message.text.strip().lower()
    valid = {"date", "time", "duration", "description", "notification", "name", "active"}
    if field not in valid:
        await update.message.reply_text(f"Invalid field. Choose from: {', '.join(sorted(valid))}:")
        return ME_FIELD
    context.user_data["me_field"] = field
    await update.message.reply_text(f"New value for *{field}*:", parse_mode=ParseMode.MARKDOWN)
    return ME_VALUE


async def me_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.message.text.strip()
    field = context.user_data["me_field"]
    ev: dict = context.user_data["me_event"]
    field_map = {
        "date": "date", "time": "time", "duration": "duration_minutes",
        "description": "description", "notification": "notification_minutes",
        "name": "name", "active": "active",
    }
    yaml_field = field_map[field]
    if field in ("duration", "notification"):
        ev[yaml_field] = int(value)
    elif field == "active":
        ev[yaml_field] = value.lower() in ("true", "yes", "1")
    else:
        ev[yaml_field] = value

    evdata = await get_all_events_data()
    specials = evdata.get("special_events", [])
    for i, e in enumerate(specials):
        if e["id"] == ev["id"]:
            specials[i] = ev
            break
    evdata["special_events"] = specials
    await save_events_data(evdata)

    # Reschedule
    app: Application = context.application
    new_events = _merge_special_events([ev], {}, days_ahead=400)
    for e in new_events:
        schedule_event_notification(app, e)

    uid, uname, dname = user_info(update)
    activity.log_command("modifyevent", uid, uname, dname, details=f"Modified '{ev['name']}' field={field}")
    await update.message.reply_text(f"✅ Updated *{field}* for *{ev['name']}*.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /deleteevent — delete special event or annotate convocation
# ---------------------------------------------------------------------------

DE_SELECT, DE_CONFIRM, DE_ANNOT = range(3)


@admin_only
async def cmd_deleteevent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("deleteevent", uid, uname, dname)
    events = await all_upcoming(days_ahead=30)
    lines = ["*Events in next 30 days:*\n"]
    context.user_data["de_events"] = events
    for i, ev in enumerate(events):
        lines.append(f"{i+1}. [{ev['type'][0].upper()}] {ev['name']}  ({format_dt(ev['service_time'])})")
    lines.append(
        "\nEnter number to select.\n"
        "_Special events can be deleted; convocations get an urgent announcement added._"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    return DE_SELECT


async def de_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    events: list[dict] = context.user_data["de_events"]
    if not text.isdigit() or not (1 <= int(text) <= len(events)):
        await update.message.reply_text("Invalid selection. Enter a number:")
        return DE_SELECT
    ev = events[int(text) - 1]
    context.user_data["de_ev"] = ev
    if ev["type"] == "special":
        await update.message.reply_text(
            f"Delete *{ev['name']}*? (yes/no)", parse_mode=ParseMode.MARKDOWN
        )
        return DE_CONFIRM
    else:
        await update.message.reply_text(
            f"*{ev['name']}* is a convocation (cannot be deleted).\n"
            "Enter an urgent announcement to add (or '-' to cancel):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return DE_ANNOT


async def de_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.strip().lower() not in ("yes", "y"):
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END
    ev: dict = context.user_data["de_ev"]
    evdata = await get_all_events_data()
    specials = [e for e in evdata.get("special_events", []) if e["id"] != ev["key"]]
    evdata["special_events"] = specials
    await save_events_data(evdata)
    uid, uname, dname = user_info(update)
    activity.log_command("deleteevent", uid, uname, dname, details=f"Deleted '{ev['name']}'")
    await update.message.reply_text(f"✅ *{ev['name']}* deleted.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


async def de_annot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == "-":
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END
    ev: dict = context.user_data["de_ev"]
    evdata = await get_all_events_data()
    ann_map: dict = evdata.setdefault("convocation_announcements", {})
    ann_map.setdefault(ev["key"], []).append(text)
    await save_events_data(evdata)
    uid, uname, dname = user_info(update)
    activity.log_command(
        "deleteevent", uid, uname, dname,
        details=f"Added announcement to '{ev['name']}': {text}"
    )
    await update.message.reply_text("⚠️ Announcement added to the convocation notification.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /appointment — multi-step user flow
# ---------------------------------------------------------------------------

(
    AP_OFFICIAL,
    AP_DATE,
    AP_TIME,
    AP_DESC,
    AP_CONFIRM,
) = range(5)

# Official response states (handled via callback queries)
CB_APPT_PREFIX = "appt:"


@admin_only
async def admin_check_pass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder — used only to block unknown admin commands from non-admins."""
    pass


async def cmd_appointment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("appointment", uid, uname, dname)
    context.user_data.clear()

    lines = ["*Request an Appointment*\n", "Who would you like to meet with?\n"]
    for i, off in enumerate(OFFICIALS, 1):
        lines.append(f"{i}. {off['name']}")
    lines.append("\nEnter the number:")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    return AP_OFFICIAL


async def ap_official(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= len(OFFICIALS)):
        await update.message.reply_text("Please enter a valid number:")
        return AP_OFFICIAL
    context.user_data["ap_official"] = OFFICIALS[int(text) - 1]
    await update.message.reply_text("Desired date (YYYY-MM-DD):")
    return AP_DATE


async def ap_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        await update.message.reply_text("Please use YYYY-MM-DD format:")
        return AP_DATE
    context.user_data["ap_date"] = text
    await update.message.reply_text("Desired time (HH:MM, 24-hour):")
    return AP_TIME


async def ap_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", text):
        await update.message.reply_text("Please use HH:MM format:")
        return AP_TIME
    context.user_data["ap_time"] = text
    await update.message.reply_text(
        "Brief description of the meeting purpose (128 characters max):"
    )
    return AP_DESC


async def ap_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()[:128]
    context.user_data["ap_desc"] = text
    off = context.user_data["ap_official"]
    d = context.user_data
    summary = (
        f"*Appointment Request Summary:*\n"
        f"With: {off['name']}\n"
        f"Date: {d['ap_date']} at {d['ap_time']}\n"
        f"Description: {text}\n\n"
        f"Submit? (yes/no)"
    )
    await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
    return AP_CONFIRM


async def ap_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.strip().lower() not in ("yes", "y"):
        await update.message.reply_text("Request cancelled.")
        return ConversationHandler.END

    uid, uname, dname = user_info(update)
    d = context.user_data
    off: dict = d["ap_official"]
    appt_id = uuid.uuid4().hex[:10].upper()

    # Parse requested datetime
    parts_d = [int(x) for x in d["ap_date"].split("-")]
    parts_t = [int(x) for x in d["ap_time"].split(":")]
    req_dt = TZ.localize(datetime(parts_d[0], parts_d[1], parts_d[2], parts_t[0], parts_t[1]))

    appt = {
        "id": appt_id,
        "user_chat_id": uid,
        "user_username": uname,
        "user_display_name": dname,
        "official_id": off["id"],
        "official_name": off["name"],
        "requested_datetime": req_dt.isoformat(),
        "confirmed_datetime": None,
        "description": d["ap_desc"],
        "status": "pending",
        "duration_minutes": 30,
    }
    appts = await get_appointments()
    appts.append(appt)
    await save_appointments(appts)

    await update.message.reply_text(
        f"✅ *Request submitted!* (ID: `{appt_id}`)\n"
        "You will be notified when your request is accepted, declined, or a new time is suggested.",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Notify the official
    await _notify_official_of_request(context, appt, update)

    activity.log_command("appointment", uid, uname, dname, details=f"New appt request {appt_id}")
    return ConversationHandler.END


async def _notify_official_of_request(
    context: ContextTypes.DEFAULT_TYPE, appt: dict, update: Update
) -> None:
    off_id = appt["official_id"]
    off = next((o for o in OFFICIALS if o["id"] == off_id), None)
    if not off:
        return
    chat_id = off.get("chat_id")
    if not chat_id:
        logger.warning("Official %s has no chat_id — they need to /start the bot.", off_id)
        return

    req_dt_str = format_dt(datetime.fromisoformat(appt["requested_datetime"]))
    caption = (
        f"📅 *Appointment Request* (ID: `{appt['id']}`)\n\n"
        f"From: {appt['user_display_name']}"
        + (f" (@{appt['user_username']})" if appt.get("user_username") else "")
        + f"\nRequested: {req_dt_str}\n"
        f"Purpose: {appt['description']}"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"{CB_APPT_PREFIX}confirm:{appt['id']}"),
            InlineKeyboardButton("📅 Suggest time", callback_data=f"{CB_APPT_PREFIX}counter:{appt['id']}"),
            InlineKeyboardButton("❌ Decline", callback_data=f"{CB_APPT_PREFIX}decline:{appt['id']}"),
        ]
    ])

    # Try to include user's profile photo
    try:
        photos = await context.bot.get_user_profile_photos(appt["user_chat_id"], limit=1)
        if photos.photos:
            photo = photos.photos[0][-1]
            await context.bot.send_photo(chat_id, photo.file_id, caption=caption,
                                         parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            return
    except TelegramError:
        pass
    await context.bot.send_message(chat_id, caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# ---------------------------------------------------------------------------
# Appointment callback handler (official responses)
# ---------------------------------------------------------------------------

# Store pending counter-propose state outside conversation
_counter_propose_state: dict[str, Any] = {}  # appt_id -> {"chat_id": ..., "role": "official"|"user"}


async def appt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data: str = query.data
    parts = data[len(CB_APPT_PREFIX):].split(":")
    action, appt_id = parts[0], parts[1]

    appts = await get_appointments()
    appt = next((a for a in appts if a["id"] == appt_id), None)
    if not appt:
        await query.edit_message_text("⚠️ Appointment not found.")
        return

    user_chat_id = appt["user_chat_id"]

    if action == "confirm":
        appt["status"] = "confirmed"
        appt["confirmed_datetime"] = appt["requested_datetime"]
        await _finalize_appointment(context, appt, appts)
        await query.edit_message_text(f"✅ You confirmed the appointment (ID: {appt_id}).")

    elif action == "decline":
        appt["status"] = "declined"
        for i, a in enumerate(appts):
            if a["id"] == appt_id:
                appts[i] = appt
        await save_appointments(appts)
        await query.edit_message_text(f"❌ You declined the appointment (ID: {appt_id}).")
        await context.bot.send_message(user_chat_id,
            f"❌ Your appointment request (ID: `{appt_id}`) has been declined.",
            parse_mode=ParseMode.MARKDOWN)

    elif action == "counter":
        # Official wants to suggest a different time
        _counter_propose_state[appt_id] = {
            "chat_id": query.message.chat_id,
            "role": "official",
        }
        context.user_data[f"cp_appt_{appt_id}"] = True
        await query.edit_message_text(
            f"Suggest a new date/time for appointment {appt_id}.\n"
            "Reply with: YYYY-MM-DD HH:MM"
        )
        # We handle the next message in a fallback handler

    elif action == "accept_counter":
        # User accepts counter-proposed time
        appt["status"] = "confirmed"
        appt["confirmed_datetime"] = appt.get("counter_datetime", appt["requested_datetime"])
        await _finalize_appointment(context, appt, appts)
        await query.edit_message_text(f"✅ You accepted the suggested time (ID: {appt_id}).")

    elif action == "decline_counter":
        # User declines the counter-proposal → let them suggest a new time
        _counter_propose_state[appt_id] = {
            "chat_id": query.message.chat_id,
            "role": "user",
        }
        await query.edit_message_text(
            f"Suggest a different date/time (or type 'cancel' to cancel the request):\n"
            "YYYY-MM-DD HH:MM"
        )

    elif action == "accept_user_counter":
        # Official accepts user's counter-proposed time
        appt["status"] = "confirmed"
        appt["confirmed_datetime"] = appt.get("user_counter_datetime", appt["requested_datetime"])
        await _finalize_appointment(context, appt, appts)
        await query.edit_message_text(f"✅ You confirmed the appointment with the user's suggested time.")

    elif action == "decline_user_counter":
        appt["status"] = "declined"
        for i, a in enumerate(appts):
            if a["id"] == appt_id:
                appts[i] = appt
        await save_appointments(appts)
        await query.edit_message_text(f"❌ Request cancelled.")
        await context.bot.send_message(user_chat_id,
            f"Your appointment request (ID: `{appt_id}`) has been cancelled.",
            parse_mode=ParseMode.MARKDOWN)


async def _finalize_appointment(
    context: ContextTypes.DEFAULT_TYPE, appt: dict, appts: list
) -> None:
    """Save confirmed appointment and send ICS to user."""
    for i, a in enumerate(appts):
        if a["id"] == appt["id"]:
            appts[i] = appt
    await save_appointments(appts)

    confirmed_dt = datetime.fromisoformat(appt["confirmed_datetime"])
    if confirmed_dt.tzinfo is None:
        confirmed_dt = TZ.localize(confirmed_dt)

    ics_bytes = appointment_to_ics(
        {**appt, "confirmed_datetime": confirmed_dt},
        TZ,
    )
    bio = io.BytesIO(ics_bytes)
    bio.name = "appointment.ics"
    await context.bot.send_message(
        appt["user_chat_id"],
        f"✅ *Your appointment (ID: `{appt['id']}`) has been confirmed!*\n"
        f"With: {appt['official_name']}\n"
        f"When: {format_dt(confirmed_dt)}\n\n"
        "An ICS calendar file is attached.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await context.bot.send_document(
        appt["user_chat_id"],
        document=InputFile(bio, filename="appointment.ics"),
        caption="Import this file into your calendar app.",
    )


async def handle_counter_propose_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle free-text messages from officials/users suggesting a new date/time."""
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # Find if this chat_id has a pending counter-propose
    appt_id = next(
        (aid for aid, st in _counter_propose_state.items() if st["chat_id"] == chat_id),
        None,
    )
    if not appt_id:
        return  # Not a counter-propose message

    appts = await get_appointments()
    appt = next((a for a in appts if a["id"] == appt_id), None)
    if not appt:
        return

    role = _counter_propose_state[appt_id]["role"]
    del _counter_propose_state[appt_id]

    if role == "user" and text.lower() == "cancel":
        appt["status"] = "cancelled"
        for i, a in enumerate(appts):
            if a["id"] == appt_id:
                appts[i] = appt
        await save_appointments(appts)
        await update.message.reply_text("Request cancelled.")
        return

    # Parse "YYYY-MM-DD HH:MM"
    m = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", text)
    if not m:
        await update.message.reply_text(
            "Couldn't parse date/time. Please use: YYYY-MM-DD HH:MM"
        )
        _counter_propose_state[appt_id] = {"chat_id": chat_id, "role": role}
        return

    date_s, time_s = m.group(1), m.group(2)
    parts_d = [int(x) for x in date_s.split("-")]
    parts_t = [int(x) for x in time_s.split(":")]
    new_dt = TZ.localize(datetime(parts_d[0], parts_d[1], parts_d[2], parts_t[0], parts_t[1]))
    new_dt_str = format_dt(new_dt)

    if role == "official":
        # Official suggests new time → notify user
        appt["counter_datetime"] = new_dt.isoformat()
        appt["status"] = "counter_proposed"
        for i, a in enumerate(appts):
            if a["id"] == appt_id:
                appts[i] = appt
        await save_appointments(appts)
        await update.message.reply_text(f"✅ Suggested time sent to the user: {new_dt_str}")
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Accept", callback_data=f"{CB_APPT_PREFIX}accept_counter:{appt_id}"),
                InlineKeyboardButton("📅 Suggest different", callback_data=f"{CB_APPT_PREFIX}decline_counter:{appt_id}"),
            ]
        ])
        await context.bot.send_message(
            appt["user_chat_id"],
            f"📅 *New time suggested for appointment `{appt_id}`*\n"
            f"With: {appt['official_name']}\n"
            f"Suggested: {new_dt_str}\n"
            f"Purpose: {appt['description']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    else:
        # User suggests alternative → notify official
        appt["user_counter_datetime"] = new_dt.isoformat()
        for i, a in enumerate(appts):
            if a["id"] == appt_id:
                appts[i] = appt
        await save_appointments(appts)
        await update.message.reply_text(f"✅ Your suggested time has been forwarded: {new_dt_str}")
        off = next((o for o in OFFICIALS if o["id"] == appt["official_id"]), None)
        if off and off.get("chat_id"):
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Accept", callback_data=f"{CB_APPT_PREFIX}accept_user_counter:{appt_id}"),
                    InlineKeyboardButton("❌ Decline", callback_data=f"{CB_APPT_PREFIX}decline_user_counter:{appt_id}"),
                ]
            ])
            await context.bot.send_message(
                off["chat_id"],
                f"The user has suggested a new time for appointment `{appt_id}`:\n{new_dt_str}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
            )


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception:", exc_info=context.error)
    activity.log_error(str(context.error))


# ---------------------------------------------------------------------------
# Post-init: schedule all notifications
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    await schedule_all_upcoming(app)

    # Set bot commands
    await app.bot.set_my_commands([
        BotCommand("start", "Register and show welcome"),
        BotCommand("help", "Show available commands"),
        BotCommand("events", "Upcoming events (next 30 days)"),
        BotCommand("exportcalendar", "Download ICS calendar file"),
        BotCommand("appointment", "Request a meeting with an official"),
        BotCommand("stop", "Unsubscribe from notifications"),
    ])

    # Weekly reschedule job — runs every Sunday at 00:05 to extend notification window
    app.job_queue.run_repeating(
        reschedule_job,
        interval=timedelta(days=7),
        first=timedelta(seconds=10),
        name="weekly_reschedule",
    )


async def reschedule_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await schedule_all_upcoming(context.application)
    logger.info("Weekly reschedule completed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # --- Conversations ---
    add_event_conv = ConversationHandler(
        entry_points=[CommandHandler("addevent", cmd_addevent)],
        states={
            AE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ae_name)],
            AE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ae_date)],
            AE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ae_time)],
            AE_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ae_duration)],
            AE_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, ae_desc)],
            AE_NOTIF: [MessageHandler(filters.TEXT & ~filters.COMMAND, ae_notif)],
            AE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ae_confirm)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    modify_event_conv = ConversationHandler(
        entry_points=[CommandHandler("modifyevent", cmd_modifyevent)],
        states={
            ME_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, me_select)],
            ME_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, me_field)],
            ME_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, me_value)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    delete_event_conv = ConversationHandler(
        entry_points=[CommandHandler("deleteevent", cmd_deleteevent)],
        states={
            DE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, de_select)],
            DE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, de_confirm)],
            DE_ANNOT: [MessageHandler(filters.TEXT & ~filters.COMMAND, de_annot)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    appointment_conv = ConversationHandler(
        entry_points=[CommandHandler("appointment", cmd_appointment)],
        states={
            AP_OFFICIAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_official)],
            AP_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_date)],
            AP_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_time)],
            AP_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_desc)],
            AP_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_confirm)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    # --- Register handlers ---
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("exportcalendar", cmd_export_calendar))
    app.add_handler(CommandHandler("adminhelp", cmd_adminhelp))
    app.add_handler(CommandHandler("usercount", cmd_usercount))
    app.add_handler(CommandHandler("userlist", cmd_userlist))
    app.add_handler(CommandHandler("listevents", cmd_listevents))

    app.add_handler(add_event_conv)
    app.add_handler(modify_event_conv)
    app.add_handler(delete_event_conv)
    app.add_handler(appointment_conv)

    app.add_handler(CallbackQueryHandler(appt_callback, pattern=f"^{re.escape(CB_APPT_PREFIX)}"))

    # Free-text handler for counter-propose date/time responses
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_counter_propose_message,
        )
    )

    app.add_error_handler(error_handler)

    logger.info("Starting %s…", BOT_DISPLAY_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

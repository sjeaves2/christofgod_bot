"""
Christ of God Ministries Telegram Bot
=====================================
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
import calendar
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
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from activity_logger import ActivityLogger
from cache import FileCache
from hebrew_calendar import (
    all_upcoming_events,
    upcoming_convocation_events,
    sabbath_events,
    service_phases,
)
from localization import (
    AVAILABLE_LANGUAGES,
    CATALOG,
    DEFAULT_LANG,
    localized_datetime,
    status_label,
    t,
)
from ics_generator import (
    appointment_cancellation_to_ics,
    appointment_to_ics,
    events_to_ics,
)
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
# Console/file log verbosity. INFO shows command execution + notification
# broadcasts; DEBUG additionally shows the underlying Telegram API calls.
LOG_LEVEL = getattr(logging, str(_CFG["log"].get("level", "INFO")).upper(), logging.INFO)
DEFAULT_NOTIF_MIN: int = _CFG["notifications"]["default_minutes_before"]

for _d in (DATA_DIR, LOGS_DIR, GEN_DIR):
    _d.mkdir(parents=True, exist_ok=True)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_file = LOGS_DIR / "bot.log"

# Console handler — always on
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_console_handler.setLevel(LOG_LEVEL)

# File handler — appends across restarts
_file_handler = logging.FileHandler(_log_file, mode="a", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_file_handler.setLevel(LOG_LEVEL)

# Root at DEBUG so demoted-to-DEBUG records can reach handlers; the handlers'
# own levels (LOG_LEVEL) decide what is actually emitted.
logging.basicConfig(level=logging.DEBUG, handlers=[_console_handler, _file_handler])
logger = logging.getLogger(__name__)
logger.info("---- Bot process started ----")


class _HttpxApiLogFilter(logging.Filter):
    """Keep the Telegram API call logs out of the way at INFO level.

    - The very first getUpdates poll is replaced with a friendly INFO notice.
    - Any HTTP 4xx/5xx response is left at its original level so API errors
      always surface.
    - Every other successful API request line (getUpdates polls, sendMessage,
      etc.) is demoted to DEBUG, so it only appears when LOG_LEVEL=DEBUG.
      This also keeps the bot token (embedded in request URLs) out of the
      INFO-level logs.
    """

    def __init__(self) -> None:
        super().__init__()
        self._seen_first = False

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "HTTP Request" not in msg and "getUpdates" not in msg:
            return True  # unrelated record — pass through unchanged

        status_match = re.search(r'"HTTP/[\d.]+ (\d{3})', msg)
        if status_match and int(status_match.group(1)) >= 400:
            return True  # always surface API errors at their original level

        if "getUpdates" in msg and not self._seen_first:
            self._seen_first = True
            record.msg = (
                "Long polling started — using getUpdates to check for incoming messages"
            )
            record.args = ()
            return True  # friendly one-time INFO notice

        # Successful API calls → DEBUG (hidden unless LOG_LEVEL=DEBUG)
        record.levelno = logging.DEBUG
        record.levelname = "DEBUG"
        return True


_httpx_api_filter = _HttpxApiLogFilter()
logging.getLogger("httpx").addFilter(_httpx_api_filter)

activity = ActivityLogger(LOGS_DIR, retention_days=LOG_RETENTION, tz=TZ)

# ---------------------------------------------------------------------------
# File caches
# ---------------------------------------------------------------------------

events_cache = FileCache(DATA_DIR / "events.yaml")
users_cache = FileCache(DATA_DIR / "users.yaml")
appts_cache = FileCache(DATA_DIR / "appointments.yaml")
# Tracks which recipients have already been notified for each event, so a
# missed/partial broadcast can be retried later without duplicate sends.
notif_state_cache = FileCache(DATA_DIR / "notification_state.yaml")
# Groups/channels the bot has been added to (discovered via membership events),
# used to populate the /broadcast target list.
groups_cache = FileCache(DATA_DIR / "known_groups.yaml")

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

    _, lang = await get_user_prefs(uid)
    is_adm = is_admin(update)
    if is_adm:
        reply = "✅ Contact received. You have been recognised as an administrator."
    elif _is_known_official(uid, uname):
        reply = "✅ Contact received. You have been recognised as an official."
    else:
        reply = "✅ Contact received. Thank you!"
    cmd_text = _commands_text(lang, is_adm)

    await update.message.reply_text(reply, reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(cmd_text, parse_mode=ParseMode.MARKDOWN)
    phone_digits = re.sub(r"\D", "", phone)
    activity.log_command("contact_share", uid, uname, dname, details=f"phone={phone_digits}")


def user_info(update: Update) -> tuple[int, str | None, str]:
    u = update.effective_user
    return u.id, u.username, u.full_name or u.first_name or str(u.id)


async def get_all_users() -> list[dict[str, Any]]:
    data = await users_cache.get()
    return data.get("users") or [] if data else []


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
    return data.get("appointments") or [] if data else []


async def save_appointments(appts: list[dict[str, Any]]) -> None:
    data = appts_cache._data or {}
    data["appointments"] = appts
    await appts_cache.save(data)


def format_dt(dt: datetime, tz: "pytz.BaseTzInfo | None" = None, lang: "str | None" = None) -> str:
    return localized_datetime(dt.astimezone(tz or TZ), lang or DEFAULT_LANG)


def _coerce_tz(name: "str | None") -> "pytz.BaseTzInfo":
    """Return a pytz timezone for *name*, falling back to the church timezone."""
    if name:
        try:
            return pytz.timezone(name)
        except Exception:
            pass
    return TZ


def user_tz_of(record: "dict | None") -> "pytz.BaseTzInfo":
    """Timezone for a user record, defaulting to the configured church timezone."""
    return _coerce_tz((record or {}).get("timezone"))


def user_lang_of(record: "dict | None") -> str:
    """Language code for a user record, defaulting to the catalog default."""
    lang = (record or {}).get("language")
    return lang if lang in CATALOG else DEFAULT_LANG


async def get_user_prefs(chat_id: int) -> "tuple[pytz.BaseTzInfo, str]":
    """Return (timezone, language) preferences for a user (with safe defaults)."""
    users = await get_all_users()
    record = next((u for u in users if u.get("chat_id") == chat_id), None)
    return user_tz_of(record), user_lang_of(record)


def now_tz() -> datetime:
    return datetime.now(TZ)


async def _answer_cb(query) -> None:
    """Acknowledge a callback query, tolerating a stale/expired one.

    If the bot was briefly offline when the button was tapped, Telegram expires
    the callback query and answer() raises BadRequest ("query is too old").
    That must not abort the handler — edit_message_text and the real work that
    follow are not subject to the callback's ~15s timeout, so we swallow it and
    let the handler complete (graceful recovery on wake).
    """
    try:
        await query.answer()
    except BadRequest:
        pass


def _appt_datetime(appt: dict[str, Any]) -> "datetime | None":
    """Best-effort tz-aware datetime for an appointment (confirmed, else requested)."""
    dt_raw = appt.get("confirmed_datetime") or appt.get("requested_datetime", "")
    try:
        dt_obj = datetime.fromisoformat(dt_raw)
    except (ValueError, TypeError):
        return None
    if dt_obj.tzinfo is None:
        dt_obj = TZ.localize(dt_obj)
    return dt_obj


def _appt_dt_label(appt: dict[str, Any], tz: "pytz.BaseTzInfo | None" = None,
                   lang: "str | None" = None) -> str:
    """Human-readable date/time for an appointment, falling back to the raw value."""
    dt_obj = _appt_datetime(appt)
    if dt_obj is None:
        return appt.get("confirmed_datetime") or appt.get("requested_datetime") or "—"
    return format_dt(dt_obj, tz, lang)


def _user_is_appt_official(appt: dict[str, Any], user_id: int, username: "str | None") -> bool:
    """True if this user is the official assigned to the given appointment."""
    off = next((o for o in OFFICIALS if o.get("id") == appt.get("official_id")), None)
    if not off:
        return False
    if off.get("chat_id") == user_id:
        return True
    uname_lower = (username or "").lstrip("@").lower()
    oname = (off.get("telegram_username") or "").lstrip("@").lower()
    return bool(uname_lower) and oname == uname_lower


# Affirmative replies accepted for typed yes/no prompts, across supported languages.
_AFFIRMATIVE_WORDS = {"yes", "y", "sí", "si", "s", "oui", "o"}


def _is_affirmative(text: "str | None") -> bool:
    return (text or "").strip().lower() in _AFFIRMATIVE_WORDS


# Statuses that count as an appointment still "in play".
ACTIVE_APPT_STATUSES = ("pending", "confirmed", "counter_proposed")
# Once an appointment reaches one of these, callback actions on it are no-ops
# (prevents duplicate confirmations/declines from repeated or replayed taps).
TERMINAL_APPT_STATUSES = ("confirmed", "declined", "cancelled")

# How far ahead an appointment may be requested.
APPOINTMENT_HORIZON_MONTHS = 6

# Default length of an appointment, used for overlap checks and new requests.
DEFAULT_APPT_DURATION_MIN = 30


def _overlapping_appt(
    appts: list[dict[str, Any]],
    user_id: int,
    start: datetime,
    duration_minutes: int,
    exclude_id: "str | None" = None,
) -> "dict | None":
    """Return the user's active appointment whose time overlaps [start, start+duration)."""
    end = start + timedelta(minutes=duration_minutes)
    for a in appts:
        if a.get("user_chat_id") != user_id:
            continue
        if a.get("status") not in ACTIVE_APPT_STATUSES:
            continue
        if exclude_id and a.get("id") == exclude_id:
            continue
        a_start = _appt_datetime(a)
        if a_start is None:
            continue
        a_end = a_start + timedelta(minutes=int(a.get("duration_minutes", DEFAULT_APPT_DURATION_MIN)))
        # Half-open intervals overlap when each starts before the other ends.
        if start < a_end and a_start < end:
            return a
    return None


def _confirmed_overlap(
    appts: list[dict[str, Any]], appt: dict[str, Any], confirmed_iso: str
) -> "dict | None":
    """Check a to-be-confirmed time against the requester's *other* active appointments."""
    start = datetime.fromisoformat(confirmed_iso)
    if start.tzinfo is None:
        start = TZ.localize(start)
    return _overlapping_appt(
        appts,
        appt["user_chat_id"],
        start,
        int(appt.get("duration_minutes", DEFAULT_APPT_DURATION_MIN)),
        exclude_id=appt["id"],
    )


def _max_request_datetime() -> datetime:
    """Latest datetime an appointment may be requested for (6 calendar months out)."""
    now = now_tz()
    month_index = now.month - 1 + APPOINTMENT_HORIZON_MONTHS
    year = now.year + month_index // 12
    month = month_index % 12 + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    naive = datetime(year, month, day, now.hour, now.minute, now.second)
    return TZ.localize(naive)


# A user may hold at most this many active appointments with a given official
# whose scheduled time falls within ±APPOINTMENT_WINDOW_HALF_DAYS of *now*
# (a symmetric, now-anchored 30-day window).
APPOINTMENT_MAX_PER_WINDOW = 4
APPOINTMENT_WINDOW_HALF_DAYS = 15


def _count_active_appts_with_official(
    appts: list[dict[str, Any]],
    user_id: int,
    official_id: str,
    center_dt: datetime,
    half_days: int = APPOINTMENT_WINDOW_HALF_DAYS,
) -> int:
    """Count the user's active appointments with this official whose scheduled
    time falls within *half_days* days before or after *center_dt*."""
    start_dt = center_dt - timedelta(days=half_days)
    end_dt = center_dt + timedelta(days=half_days)
    count = 0
    for a in appts:
        if a.get("user_chat_id") != user_id:
            continue
        if a.get("official_id") != official_id:
            continue
        if a.get("status") not in ACTIVE_APPT_STATUSES:
            continue
        dt = _appt_datetime(a)
        if dt is None:
            continue
        if start_dt <= dt <= end_dt:
            count += 1
    return count


def _merge_special_events(
    special_defs: list[dict[str, Any]],
    announcements_map: dict[str, list[str]],
    days_ahead: int = 90,
) -> list[dict[str, Any]]:
    """Expand special_events definitions into concrete upcoming event dicts.

    All special events (including the weekly Sunday Morning Prayer) are driven
    from events.yaml here; convocations/Sabbath come from hebrew_calendar.py.
    """
    now = now_tz()
    cutoff = now + timedelta(days=days_ahead)
    results: list[dict[str, Any]] = []
    for defn in special_defs:
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
                            "url": defn.get("url", ""),
                            "targets": defn.get("targets", []),
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
                    "url": defn.get("url", ""),
                    "targets": defn.get("targets", []),
                    "announcements": announcements_map.get(defn["id"], []),
                })
    return results


def _resolve_targets(names: "list", registry: dict) -> list[int]:
    """Map target names to chat_ids via the registry; pass through raw ids.

    Accepts a list of registry names and/or literal chat ids (int, or a string
    like a "@channelusername" / numeric id). Unknown names are dropped.
    """
    resolved: list = []
    for n in names or []:
        if isinstance(n, int):
            resolved.append(n)
        elif n in registry:
            resolved.append(registry[n])
        elif isinstance(n, str) and (n.startswith("@") or n.lstrip("-").isdigit()):
            resolved.append(int(n) if n.lstrip("-").isdigit() else n)
        # else: unknown name with no registry entry — skip
    # De-duplicate, preserving order
    seen = set()
    out = []
    for c in resolved:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


async def all_upcoming(days_ahead: int = 90) -> list[dict[str, Any]]:
    """Return all events (convocations + special) sorted by service_time."""
    evdata = await get_all_events_data()
    announcements_map: dict[str, list[str]] = evdata.get("convocation_announcements", {})
    urls_map: dict[str, str] = evdata.get("convocation_urls", {})
    special_defs: list[dict[str, Any]] = evdata.get("special_events", [])

    # Notification target registry + convocation target assignments
    registry: dict = evdata.get("notification_targets", {}) or {}
    convo_targets: dict = evdata.get("convocation_targets", {}) or {}
    convo_targets_default: list = evdata.get("convocation_targets_default", []) or []

    convocations = all_upcoming_events(TZ, days_ahead)
    # Attach announcements, per-service join link, and notification target chats.
    for ev in convocations:
        ev["announcements"] = announcements_map.get(ev["key"], [])
        phase_key = ev.get("phase_key")
        if phase_key and urls_map.get(phase_key):
            ev["url"] = urls_map[phase_key]
        names = convo_targets.get(phase_key) if phase_key else None
        if names is None:
            names = convo_targets_default
        ev["target_chat_ids"] = _resolve_targets(names, registry)

    specials = _merge_special_events(special_defs, announcements_map, days_ahead)
    for ev in specials:
        ev["target_chat_ids"] = _resolve_targets(ev.get("targets", []), registry)

    merged = convocations + specials
    merged.sort(key=lambda e: e["service_time"])
    return merged


# ---------------------------------------------------------------------------
# Notification sender
# ---------------------------------------------------------------------------

def _render_notification(event: dict[str, Any], tz: "pytz.BaseTzInfo", lang: str) -> str:
    """Build a reminder message localized and time-zoned for one recipient."""
    lines = [
        t("notif_reminder_title", lang, name=event["name"]),
        t("notif_service_begins", lang, when=format_dt(event["service_time"], tz, lang)),
    ]
    if event.get("description"):
        lines.append(f"\n_{event['description']}_")
    if event.get("url"):
        lines.append("\n" + t("notif_join", lang, url=event["url"]))
    if event.get("announcements"):
        lines.append("\n" + t("notif_announcements_header", lang))
        lines.extend(f"• {a}" for a in event["announcements"])
    return "\n".join(lines)


async def _load_notif_state() -> dict[str, Any]:
    data = await notif_state_cache.get()
    return (data or {}).get("states") or {}


async def _save_notif_state(states: dict[str, Any]) -> None:
    await notif_state_cache.save({"states": states})


async def deliver_event_notifications(bot, event: dict[str, Any]) -> int:
    """Post an event reminder to each configured group/channel not yet notified.

    Notifications are broadcast once per target chat (not per individual user).
    Idempotent: tracks delivered target chats in notification_state so a missed
    or partially-failed broadcast can be retried later without duplicate posts.
    Does nothing once the event's service time has passed. Returns the number of
    messages posted on this call.
    """
    key = event["key"]
    service_time = event["service_time"]
    now = now_tz()
    if now >= service_time:
        return 0  # too late — the event has already started

    targets = event.get("target_chat_ids") or []
    if not targets:
        return 0  # no group/channel configured for this event

    states = await _load_notif_state()
    state = states.setdefault(
        key,
        {"name": event["name"], "service_time": service_time.isoformat(), "notified": []},
    )
    notified = set(state["notified"])
    pending = [c for c in targets if c not in notified]
    if not pending:
        return 0

    # Groups/channels get a single rendering in the church's default tz/language.
    text = _render_notification(event, TZ, DEFAULT_LANG)

    sent = 0
    failed = 0
    for chat_id in pending:
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
            notified.add(chat_id)
            sent += 1
        except TelegramError as exc:
            # Includes Forbidden (bot not in group) — leave pending for retry.
            failed += 1
            logger.warning("Notification post error for chat %s: %s", chat_id, exc)

    state["notified"] = sorted(notified, key=lambda c: str(c))
    states[key] = state
    await _save_notif_state(states)

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
    states = await _load_notif_state()
    live_keys = {ev["key"] for ev in events if now < ev["service_time"]}
    pruned = {k: v for k, v in states.items() if k in live_keys}
    if len(pruned) != len(states):
        await _save_notif_state(pruned)


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

ADMIN_COMMANDS_TEXT = """\
*Admin commands:*
/addevent — add a special event
/modifyevent — modify an event
/deleteevent — remove or annotate an event
/setservicelink — set the join link for a convocation/Sabbath service
/broadcast — send a message to groups and/or all subscribers
/listevents — events in the next 30 days (admin view)
/usercount — number of registered users
/userlist — list registered users
/adminhelp — show this list"""


def _commands_text(lang: str, is_adm: bool) -> str:
    """Localized user command list, with admin commands appended if applicable."""
    text = t("user_commands", lang)
    if is_adm:
        text += "\n\n" + ADMIN_COMMANDS_TEXT
    return text


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

    _, lang = await get_user_prefs(uid)

    # If not yet identified as admin/official by username, request contact share
    # so phone-number-only admins/officials can be recognised.
    already_known = is_admin(update) or _is_known_official(uid, uname)
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

    is_adm = is_admin(update)
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
    _, lang = await get_user_prefs(uid)
    users = await get_all_users()
    users = [u for u in users if u["chat_id"] != uid]
    await save_users(users)
    activity.log_user_left(uid, uname, dname)
    activity.log_command("stop", uid, uname, dname)
    await update.message.reply_text(t("unsubscribed", lang))


# ---------------------------------------------------------------------------
# /help  /events  /exportcalendar
# ---------------------------------------------------------------------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("help", uid, uname, dname)
    _, lang = await get_user_prefs(uid)
    text = _commands_text(lang, is_admin(update))
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


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
    AE_URL,
    AE_NOTIF,
    AE_CONFIRM,
) = range(8)


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
    await update.message.reply_text("Zoom / join URL (or '-' to skip):")
    return AE_URL


async def ae_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["ae_url"] = "" if text == "-" else text
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
        f"URL: {d.get('ae_url') or '—'}\n"
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
            "url": d.get("ae_url", ""),
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
            "url": d.get("ae_url", ""),
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
        "date | time | duration | description | url | notification | name | active",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ME_FIELD


async def me_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field = update.message.text.strip().lower()
    valid = {"date", "time", "duration", "description", "notification", "name", "active", "url"}
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
        "name": "name", "active": "active", "url": "url",
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
# /setservicelink — set a per-service (per-phase) join link for convocations
# ---------------------------------------------------------------------------

SL_SELECT, SL_URL = range(2)


@admin_only
async def cmd_setservicelink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("setservicelink", uid, uname, dname)
    context.user_data.clear()

    evdata = await get_all_events_data()
    urls_map: dict[str, str] = evdata.get("convocation_urls", {})
    phases = service_phases()
    context.user_data["sl_phases"] = phases

    lines = ["*Set a Service Join Link*\n", "Each service (phase) can have its own link.\n"]
    for i, ph in enumerate(phases, 1):
        current = urls_map.get(ph["phase_key"])
        suffix = f"  🔗 {current}" if current else ""
        lines.append(f"{i}. {ph['display']}{suffix}")
    lines.append("\nEnter the number of the service to set (or /cancel):")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    return SL_SELECT


async def sl_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    phases: list[dict] = context.user_data.get("sl_phases", [])
    if not text.isdigit() or not (1 <= int(text) <= len(phases)):
        await update.message.reply_text(f"Please enter a number between 1 and {len(phases)}:")
        return SL_SELECT
    ph = phases[int(text) - 1]
    context.user_data["sl_phase"] = ph
    await update.message.reply_text(
        f"Enter the join link (URL) for *{ph['display']}*,\n"
        "or '-' to clear the existing link:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return SL_URL


async def sl_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    ph: dict = context.user_data["sl_phase"]
    uid, uname, dname = user_info(update)

    evdata = await get_all_events_data()
    urls_map: dict[str, str] = evdata.setdefault("convocation_urls", {})
    if text == "-":
        urls_map.pop(ph["phase_key"], None)
        action_msg = f"🔗 Cleared the link for *{ph['display']}*."
        detail = f"Cleared link for {ph['phase_key']}"
    else:
        urls_map[ph["phase_key"]] = text
        action_msg = f"🔗 Link set for *{ph['display']}*."
        detail = f"Set link for {ph['phase_key']}"
    await save_events_data(evdata)

    # Reschedule upcoming notifications so they carry the updated link.
    await schedule_all_upcoming(context.application)

    activity.log_command("setservicelink", uid, uname, dname, details=detail)
    await update.message.reply_text(action_msg, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /broadcast — admin sends an ad-hoc message to groups and/or all subscribers
# ---------------------------------------------------------------------------

BC_MESSAGE, BC_SELECT, BC_RETRY = range(3)

CB_BC_PREFIX = "bc:"
BC_MAX_RETRIES = 3


async def _broadcast_target_options() -> list[dict[str, Any]]:
    """Build the selectable target list: tracked groups ∪ registry, plus 'All'.

    Each option: {"key": str, "kind": "all"|"group", "chat_id": ..., "label": str}.
    """
    options: list[dict[str, Any]] = [
        {"key": "all", "kind": "all", "chat_id": None, "label": "All subscribers"}
    ]
    seen: set[str] = set()

    groups = await _load_known_groups()
    for g in groups.values():
        cid = g.get("chat_id")
        k = str(cid)
        if k in seen:
            continue
        seen.add(k)
        options.append({"key": k, "kind": "group", "chat_id": cid,
                        "label": g.get("title") or k})

    evdata = await get_all_events_data()
    registry: dict = evdata.get("notification_targets", {}) or {}
    for name, cid in registry.items():
        k = str(cid)
        if k in seen:
            continue
        seen.add(k)
        options.append({"key": k, "kind": "group", "chat_id": cid, "label": name})

    return options


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
        "📣 *Broadcast*\n\nSend me the message to broadcast. "
        "Markdown formatting is supported; I'll show you a preview before sending.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return BC_MESSAGE


async def bc_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    # Validate Markdown by rendering a preview back to the admin.
    try:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except BadRequest as exc:
        await update.message.reply_text(
            f"⚠️ I couldn't render that as Markdown ({exc.message}). "
            "Please edit and re-send your message."
        )
        return BC_MESSAGE

    context.user_data["bc_message"] = text
    options = await _broadcast_target_options()
    context.user_data["bc_options"] = options
    context.user_data["bc_selected"] = set()
    await update.message.reply_text(
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
            for u in await get_all_users():
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
    """Send the message to all recipients not yet delivered. Returns failures."""
    message: str = context.user_data["bc_message"]
    recipients: list[dict] = context.user_data["bc_recipients"]
    done: set = context.user_data["bc_done"]
    failures: list[dict] = []
    for r in recipients:
        if r["chat_id"] in done:
            continue
        try:
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
# Official picker for the /appointment request flow (distinct from CB_APPT_PREFIX).
CB_APSEL_PREFIX = "apsel:"


@admin_only
async def admin_check_pass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder — used only to block unknown admin commands from non-admins."""
    pass


async def cmd_appointment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("appointment", uid, uname, dname)
    context.user_data.clear()
    _, lang = await get_user_prefs(uid)

    rows = [
        [InlineKeyboardButton(off["name"], callback_data=f"{CB_APSEL_PREFIX}{i}")]
        for i, off in enumerate(OFFICIALS)
    ]
    rows.append([InlineKeyboardButton(
        "✖️ " + t("appt_request_cancelled", lang).rstrip("."),
        callback_data=f"{CB_APSEL_PREFIX}cancel",
    )])
    await update.message.reply_text(
        t("appt_choose_official", lang),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return AP_OFFICIAL


async def ap_official(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _answer_cb(query)
    uid = query.from_user.id
    _, lang = await get_user_prefs(uid)
    data = query.data[len(CB_APSEL_PREFIX):]

    if data == "cancel":
        await query.edit_message_text(t("appt_request_cancelled", lang))
        return ConversationHandler.END
    if not data.isdigit() or not (0 <= int(data) < len(OFFICIALS)):
        await query.edit_message_text(t("appt_invalid_number", lang))
        return ConversationHandler.END
    off = OFFICIALS[int(data)]

    # Per-official frequency limit: at most APPOINTMENT_MAX_PER_WINDOW active
    # appointments within ±APPOINTMENT_WINDOW_HALF_DAYS of now.
    appts = await get_appointments()
    if _count_active_appts_with_official(appts, uid, off["id"], now_tz()) >= APPOINTMENT_MAX_PER_WINDOW:
        await query.edit_message_text(
            t("appt_limit_reached", lang, official=off["name"],
              max=APPOINTMENT_MAX_PER_WINDOW, days=APPOINTMENT_WINDOW_HALF_DAYS * 2),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    context.user_data["ap_official"] = off
    await query.edit_message_text(
        f"*{off['name']}*\n\n" + t("appt_ask_date", lang),
        parse_mode=ParseMode.MARKDOWN,
    )
    return AP_DATE


async def ap_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, _, _ = user_info(update)
    _, lang = await get_user_prefs(uid)
    text = update.message.text.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        await update.message.reply_text(t("appt_bad_date", lang))
        return AP_DATE
    context.user_data["ap_date"] = text
    await update.message.reply_text(t("appt_ask_time", lang))
    return AP_TIME


async def ap_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, _, _ = user_info(update)
    tz, lang = await get_user_prefs(uid)
    text = update.message.text.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", text):
        await update.message.reply_text(t("appt_bad_time", lang))
        return AP_TIME

    # Build the full datetime now that we have both date and time, and validate it.
    parts_d = [int(x) for x in context.user_data["ap_date"].split("-")]
    parts_t = [int(x) for x in text.split(":")]
    try:
        req_dt = TZ.localize(datetime(parts_d[0], parts_d[1], parts_d[2], parts_t[0], parts_t[1]))
    except ValueError:
        await update.message.reply_text(t("appt_bad_datetime", lang))
        return AP_DATE

    now = now_tz()
    if req_dt <= now:
        await update.message.reply_text(t("appt_past", lang))
        return AP_DATE

    max_dt = _max_request_datetime()
    if req_dt > max_dt:
        await update.message.reply_text(
            t("appt_too_far", lang, months=APPOINTMENT_HORIZON_MONTHS,
              until=max_dt.strftime("%B %d, %Y"))
        )
        return AP_DATE

    # No overlap with the user's other active appointments.
    appts = await get_appointments()
    clash = _overlapping_appt(appts, uid, req_dt, DEFAULT_APPT_DURATION_MIN)
    if clash:
        await update.message.reply_text(
            t("appt_overlap", lang, official=clash["official_name"],
              when=_appt_dt_label(clash, tz, lang), id=clash["id"]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return AP_DATE

    context.user_data["ap_time"] = text
    await update.message.reply_text(t("appt_ask_desc", lang))
    return AP_DESC


async def ap_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, _, _ = user_info(update)
    tz, lang = await get_user_prefs(uid)
    text = update.message.text.strip()[:128]
    context.user_data["ap_desc"] = text
    off = context.user_data["ap_official"]
    d = context.user_data
    parts_d = [int(x) for x in d["ap_date"].split("-")]
    parts_t = [int(x) for x in d["ap_time"].split(":")]
    req_dt = TZ.localize(datetime(parts_d[0], parts_d[1], parts_d[2], parts_t[0], parts_t[1]))
    summary = t("appt_summary", lang, official=off["name"],
                when=format_dt(req_dt, tz, lang), desc=text)
    await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
    return AP_CONFIRM


async def ap_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    _, lang = await get_user_prefs(uid)
    if not _is_affirmative(update.message.text):
        await update.message.reply_text(t("appt_request_cancelled", lang))
        return ConversationHandler.END

    d = context.user_data
    off: dict = d["ap_official"]
    appt_id = uuid.uuid4().hex[:10].upper()

    # Parse requested datetime
    parts_d = [int(x) for x in d["ap_date"].split("-")]
    parts_t = [int(x) for x in d["ap_time"].split(":")]
    req_dt = TZ.localize(datetime(parts_d[0], parts_d[1], parts_d[2], parts_t[0], parts_t[1]))

    appts = await get_appointments()

    # Final guard: per-official frequency limit within ±15 days of now.
    if _count_active_appts_with_official(appts, uid, off["id"], now_tz()) >= APPOINTMENT_MAX_PER_WINDOW:
        await update.message.reply_text(
            t("appt_limit_not_submitted", lang, official=off["name"],
              max=APPOINTMENT_MAX_PER_WINDOW, days=APPOINTMENT_WINDOW_HALF_DAYS * 2),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    # Final guard: ensure the requested time doesn't overlap another appointment.
    clash = _overlapping_appt(appts, uid, req_dt, DEFAULT_APPT_DURATION_MIN)
    if clash:
        await update.message.reply_text(
            t("appt_overlap_not_submitted", lang, official=clash["official_name"], id=clash["id"]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

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
        "duration_minutes": DEFAULT_APPT_DURATION_MIN,
    }
    appts.append(appt)
    await save_appointments(appts)

    await update.message.reply_text(
        t("appt_submitted", lang, id=appt_id),
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
    await _answer_cb(query)
    data: str = query.data
    parts = data[len(CB_APPT_PREFIX):].split(":")
    action, appt_id = parts[0], parts[1]

    appts = await get_appointments()
    appt = next((a for a in appts if a["id"] == appt_id), None)
    if not appt:
        await query.edit_message_text("⚠️ Appointment not found.")
        return

    # Idempotency guard: if this appointment is already in a terminal state,
    # a repeated/replayed tap (e.g. multiple taps while the bot was offline)
    # must not re-run confirmation/decline side effects.
    if appt.get("status") in TERMINAL_APPT_STATUSES:
        await query.edit_message_text(
            f"ℹ️ Appointment {appt_id} has already been {appt['status']}. "
            "No further action taken."
        )
        return

    user_chat_id = appt["user_chat_id"]

    if action == "confirm":
        clash = _confirmed_overlap(appts, appt, appt["requested_datetime"])
        if clash:
            await query.edit_message_text(
                f"⚠️ That time overlaps the requester's appointment with "
                f"{clash['official_name']} (ID: {clash['id']}). Not confirmed — "
                "suggest a different time instead."
            )
            return
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
        proposed = appt.get("counter_datetime", appt["requested_datetime"])
        clash = _confirmed_overlap(appts, appt, proposed)
        if clash:
            await query.edit_message_text(
                f"⚠️ That time overlaps your appointment with {clash['official_name']} "
                f"(ID: {clash['id']}). It was not confirmed — please suggest a different time."
            )
            return
        appt["status"] = "confirmed"
        appt["confirmed_datetime"] = proposed
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
        proposed = appt.get("user_counter_datetime", appt["requested_datetime"])
        clash = _confirmed_overlap(appts, appt, proposed)
        if clash:
            await query.edit_message_text(
                f"⚠️ That time overlaps the requester's appointment with "
                f"{clash['official_name']} (ID: {clash['id']}). Not confirmed."
            )
            return
        appt["status"] = "confirmed"
        appt["confirmed_datetime"] = proposed
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
    """Save confirmed appointment and send ICS to both the user and the official."""
    for i, a in enumerate(appts):
        if a["id"] == appt["id"]:
            appts[i] = appt
    await save_appointments(appts)

    confirmed_dt = datetime.fromisoformat(appt["confirmed_datetime"])
    if confirmed_dt.tzinfo is None:
        confirmed_dt = TZ.localize(confirmed_dt)

    appt_with_dt = {**appt, "confirmed_datetime": confirmed_dt}

    # --- Notify and send ICS to the user (their timezone + language) ---
    user_tz, user_lang = await get_user_prefs(appt["user_chat_id"])
    ics_bytes = appointment_to_ics(appt_with_dt, TZ)
    user_bio = io.BytesIO(ics_bytes)
    await context.bot.send_message(
        appt["user_chat_id"],
        t("appt_confirmed_user", user_lang, id=appt["id"],
          official=appt["official_name"], when=format_dt(confirmed_dt, user_tz, user_lang)),
        parse_mode=ParseMode.MARKDOWN,
    )
    await context.bot.send_document(
        appt["user_chat_id"],
        document=InputFile(user_bio, filename="appointment.ics"),
        caption=t("appt_ics_caption", user_lang),
    )

    # --- Notify and send ICS to the official (their timezone) ---
    off = next((o for o in OFFICIALS if o["id"] == appt["official_id"]), None)
    if off and off.get("chat_id"):
        off_tz, _ = await get_user_prefs(off["chat_id"])
        off_dt_str = format_dt(confirmed_dt, off_tz)
        user_display = appt.get("user_display_name") or appt.get("user_username") or "The requester"
        ics_bytes_off = appointment_to_ics(appt_with_dt, TZ)
        off_bio = io.BytesIO(ics_bytes_off)
        await context.bot.send_message(
            off["chat_id"],
            f"✅ *Appointment confirmed (ID: `{appt['id']}`)*\n"
            f"With: {user_display}"
            + (f" (@{appt['user_username']})" if appt.get("user_username") else "") + "\n"
            f"When: {off_dt_str}\n"
            f"Purpose: {appt.get('description', '')}\n\n"
            "An ICS calendar file is attached.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await context.bot.send_document(
            off["chat_id"],
            document=InputFile(off_bio, filename="appointment.ics"),
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
# /myappointments — list the appointments the user is a party to
# ---------------------------------------------------------------------------

def _counterparty_label(appt: dict, viewer_is_official: bool) -> str:
    """Who the appointment is *with*, from the viewer's perspective."""
    if viewer_is_official:
        name = appt.get("user_display_name") or appt.get("user_username") or "Unknown requester"
        if appt.get("user_username"):
            return f"{name} (@{appt['user_username']})"
        return name
    return appt.get("official_name", "Unknown official")


async def cmd_myappointments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("myappointments", uid, uname, dname)
    tz, lang = await get_user_prefs(uid)

    appts = await get_appointments()
    mine: list[tuple[dict, bool]] = []  # (appointment, viewer_is_official)
    for a in appts:
        as_official = _user_is_appt_official(a, uid, uname)
        as_requester = a.get("user_chat_id") == uid
        if as_official or as_requester:
            # Prefer the official view when the viewer is the assigned official.
            mine.append((a, as_official))

    if not mine:
        await update.message.reply_text(t("myappts_none", lang))
        return

    now = now_tz()
    upcoming = [it for it in mine if (_appt_datetime(it[0]) or now) >= now]
    past = [it for it in mine if (_appt_datetime(it[0]) or now) < now]
    upcoming.sort(key=lambda it: _appt_datetime(it[0]) or now)
    past.sort(key=lambda it: _appt_datetime(it[0]) or now, reverse=True)

    def _render(appt: dict, viewer_is_official: bool) -> str:
        return t(
            "appt_line", lang,
            counterparty=_counterparty_label(appt, viewer_is_official),
            when=_appt_dt_label(appt, tz, lang),
            status=status_label(appt.get("status"), lang),
            id=appt["id"],
        )

    lines = [t("myappts_header", lang)]
    if upcoming:
        lines.append(t("section_upcoming", lang))
        lines.extend(_render(a, is_off) for a, is_off in upcoming)
    if past:
        lines.append(t("section_past", lang))
        lines.extend(_render(a, is_off) for a, is_off in past)

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# /cancelappointment — either party can cancel a pending or confirmed appointment
# ---------------------------------------------------------------------------

CA_SELECT, CA_CONFIRM = range(2)

CB_CANCEL_PREFIX = "ca:"


async def cmd_cancelappointment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("cancelappointment", uid, uname, dname)
    context.user_data.clear()
    tz, lang = await get_user_prefs(uid)

    appts = await get_appointments()
    is_off = _is_known_official(uid, uname)

    # Build list of appointments this user is party to that are still active
    active = []
    for a in appts:
        if a.get("status") not in ACTIVE_APPT_STATUSES:
            continue
        if is_off and _user_is_appt_official(a, uid, uname):
            active.append(a)
        if a.get("user_chat_id") == uid:
            # Avoid duplicates if official is also the requester (edge case)
            if not any(x["id"] == a["id"] for x in active):
                active.append(a)

    if not active:
        await update.message.reply_text(t("cancel_none", lang))
        return ConversationHandler.END

    context.user_data["ca_appts"] = active
    rows = []
    for i, a in enumerate(active):
        label = f"{a['official_name']} — {_appt_dt_label(a, tz, lang)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{CB_CANCEL_PREFIX}sel:{i}")])
    rows.append([InlineKeyboardButton(
        "✖️ " + t("cancel_aborted", lang).rstrip("."),
        callback_data=f"{CB_CANCEL_PREFIX}abort",
    )])
    await update.message.reply_text(
        t("cancel_list_header", lang),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return CA_SELECT


async def ca_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _answer_cb(query)
    uid = query.from_user.id
    tz, lang = await get_user_prefs(uid)
    data = query.data[len(CB_CANCEL_PREFIX):]
    active: list[dict] = context.user_data.get("ca_appts", [])

    if data == "abort":
        await query.edit_message_text(t("cancel_aborted", lang))
        return ConversationHandler.END

    idx = data.split(":", 1)[1] if data.startswith("sel:") else ""
    if not idx.isdigit() or not (0 <= int(idx) < len(active)):
        await query.edit_message_text(t("cancel_aborted", lang))
        return ConversationHandler.END

    appt = active[int(idx)]
    context.user_data["ca_appt"] = appt
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"{CB_CANCEL_PREFIX}yes"),
        InlineKeyboardButton("✖️ No", callback_data=f"{CB_CANCEL_PREFIX}no"),
    ]])
    await query.edit_message_text(
        t("cancel_confirm_prompt", lang, official=appt["official_name"],
          when=_appt_dt_label(appt, tz, lang)),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    return CA_CONFIRM


async def ca_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _answer_cb(query)
    uid, uname, dname = user_info(update)
    _, lang = await get_user_prefs(uid)
    if query.data != f"{CB_CANCEL_PREFIX}yes":
        await query.edit_message_text(t("cancel_aborted", lang))
        return ConversationHandler.END

    appt: dict = context.user_data["ca_appt"]
    appts = await get_appointments()
    for i, a in enumerate(appts):
        if a["id"] == appt["id"]:
            appts[i]["status"] = "cancelled"
            break
    await save_appointments(appts)

    activity.log_command(
        "cancelappointment", uid, uname, dname,
        details=f"Cancelled appointment {appt['id']}"
    )

    # Determine who cancelled so we can notify the other party
    is_off = _is_known_official(uid, uname)
    off = next((o for o in OFFICIALS if o.get("id") == appt.get("official_id")), None)
    user_chat_id = appt.get("user_chat_id")

    if is_off:
        # Official cancelled → notify the requester (in their language)
        if user_chat_id:
            _, req_lang = await get_user_prefs(user_chat_id)
            await context.bot.send_message(
                user_chat_id,
                t("cancel_done_by_official_to_user", req_lang,
                  id=appt["id"], official=appt["official_name"]),
                parse_mode=ParseMode.MARKDOWN,
            )
            await _send_cancellation_ics(context, user_chat_id, appt)
        await query.edit_message_text(
            t("cancel_done_official_ack", lang, id=appt["id"]),
            parse_mode=ParseMode.MARKDOWN,
        )
        # Also remove it from the official's own calendar
        if off and off.get("chat_id"):
            await _send_cancellation_ics(context, off["chat_id"], appt)
    else:
        # Requester cancelled → notify the official if we know their chat_id
        if off and off.get("chat_id"):
            user_display = appt.get("user_display_name") or appt.get("user_username") or "The requester"
            await context.bot.send_message(
                off["chat_id"],
                f"❌ Appointment (ID: `{appt['id']}`) with "
                + (f"*{user_display}*" if user_display else "a congregant")
                + (f" (@{appt['user_username']})" if appt.get("user_username") else "")
                + " has been cancelled by the requester.",
                parse_mode=ParseMode.MARKDOWN,
            )
            await _send_cancellation_ics(context, off["chat_id"], appt)
        notified = bool(off and off.get("chat_id"))
        await query.edit_message_text(
            t("cancel_done_requester_ack_notified" if notified else "cancel_done_requester_ack",
              lang, id=appt["id"]),
            parse_mode=ParseMode.MARKDOWN,
        )
        # Also remove it from the requester's own calendar
        if user_chat_id:
            await _send_cancellation_ics(context, user_chat_id, appt)

    return ConversationHandler.END


async def _send_cancellation_ics(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, appt: dict
) -> None:
    """Send a METHOD:CANCEL ICS so the recipient's calendar removes the event."""
    ics_bytes = appointment_cancellation_to_ics(appt, TZ)
    bio = io.BytesIO(ics_bytes)
    await context.bot.send_document(
        chat_id,
        document=InputFile(bio, filename="appointment-cancelled.ics"),
        caption="Import this file to remove the appointment from your calendar.",
    )


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
    users = await get_all_users()
    for u in users:
        if u.get("chat_id") == chat_id:
            u[field] = value
            await save_users(users)
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
# Command-execution logging (INFO)
# ---------------------------------------------------------------------------

async def _ignore_group_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop any message from a group/channel — the bot only serves private chats.

    Raising ApplicationHandlerStop prevents all later handlers (commands,
    conversations, free-text) from acting on group/channel messages.
    """
    raise ApplicationHandlerStop


async def _log_command_invocation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log every private command at INFO. Runs in group -1 before real handlers."""
    msg = update.effective_message
    if not msg or not msg.text:
        return
    command = msg.text.split()[0]
    u = update.effective_user
    who = (u.full_name if u else None) or "unknown"
    uid = u.id if u else "?"
    logger.info("Command %s executed by %s (id=%s)", command, who, uid)


_ACTIVE_MEMBER_STATUSES = ("member", "administrator", "creator")


async def _load_known_groups() -> dict[str, Any]:
    data = await groups_cache.get()
    return (data or {}).get("groups") or {}


async def _save_known_groups(groups: dict[str, Any]) -> None:
    await groups_cache.save({"groups": groups})


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record (and log) when the bot is added to / removed from a group or channel.

    Maintains data/known_groups.yaml so /broadcast can list the groups the bot
    currently belongs to, and surfaces the chat_id at INFO.
    """
    cmu = update.my_chat_member
    if not cmu:
        return
    chat = cmu.chat
    new_status = cmu.new_chat_member.status
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        return

    logger.info(
        "Bot membership change in %s '%s' (chat_id=%s): status=%s",
        chat.type, chat.title, chat.id, new_status,
    )

    groups = await _load_known_groups()
    key = str(chat.id)
    if new_status in _ACTIVE_MEMBER_STATUSES:
        groups[key] = {
            "chat_id": chat.id,
            "title": chat.title or key,
            "type": str(chat.type),
            "status": new_status,
        }
    else:
        # Left/kicked/restricted — drop it from the broadcast list.
        groups.pop(key, None)
    await _save_known_groups(groups)


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
        BotCommand("myappointments", "List your appointments"),
        BotCommand("cancelappointment", "Cancel a pending or confirmed appointment"),
        BotCommand("settimezone", "Set your time zone for displayed times"),
        BotCommand("language", "Choose your language"),
        BotCommand("stop", "Unsubscribe from notifications"),
    ])

    # Weekly reschedule job — runs every Sunday at 00:05 to extend notification window
    app.job_queue.run_repeating(
        reschedule_job,
        interval=timedelta(days=7),
        first=timedelta(seconds=10),
        name="weekly_reschedule",
    )

    # Catch-up job — retries any due-but-undelivered notifications (missed while
    # offline/asleep, or partially failed) until delivered or the event starts.
    app.job_queue.run_repeating(
        notification_catchup_job,
        interval=timedelta(minutes=2),
        first=timedelta(seconds=20),
        name="notification_catchup",
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
            AE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ae_url)],
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

    set_service_link_conv = ConversationHandler(
        entry_points=[CommandHandler("setservicelink", cmd_setservicelink)],
        states={
            SL_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sl_select)],
            SL_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, sl_url)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", cmd_broadcast)],
        states={
            BC_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_message)],
            BC_SELECT: [CallbackQueryHandler(bc_select, pattern=f"^{re.escape(CB_BC_PREFIX)}")],
            BC_RETRY: [CallbackQueryHandler(bc_retry, pattern=f"^{re.escape(CB_BC_PREFIX)}retry:")],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    appointment_conv = ConversationHandler(
        entry_points=[CommandHandler("appointment", cmd_appointment)],
        states={
            AP_OFFICIAL: [CallbackQueryHandler(ap_official, pattern=f"^{re.escape(CB_APSEL_PREFIX)}")],
            AP_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_date)],
            AP_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_time)],
            AP_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_desc)],
            AP_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_confirm)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    cancel_appt_conv = ConversationHandler(
        entry_points=[CommandHandler("cancelappointment", cmd_cancelappointment)],
        states={
            CA_SELECT: [CallbackQueryHandler(ca_select, pattern=f"^{re.escape(CB_CANCEL_PREFIX)}")],
            CA_CONFIRM: [CallbackQueryHandler(ca_confirm, pattern=f"^{re.escape(CB_CANCEL_PREFIX)}")],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    settimezone_conv = ConversationHandler(
        entry_points=[CommandHandler("settimezone", cmd_settimezone)],
        states={TZ_SELECT: [
            CallbackQueryHandler(tz_button, pattern=f"^{re.escape(CB_TZ_PREFIX)}"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, tz_typed),
        ]},
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    language_conv = ConversationHandler(
        entry_points=[CommandHandler("language", cmd_language)],
        states={LANG_SELECT: [CallbackQueryHandler(lang_select, pattern=f"^{re.escape(CB_LANG_PREFIX)}")]},
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    # --- Register handlers ---
    # Log/observe the bot being added to or removed from groups/channels.
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Group -1 runs before the real handlers in group 0:
    #   1. Drop anything sent from a group/channel (bot serves private chats only)
    #   2. Log private command execution at INFO
    app.add_handler(
        MessageHandler(~filters.ChatType.PRIVATE, _ignore_group_messages), group=-1
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.COMMAND, _log_command_invocation
        ),
        group=-1,
    )

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
    app.add_handler(set_service_link_conv)
    app.add_handler(broadcast_conv)
    app.add_handler(CommandHandler("myappointments", cmd_myappointments))
    app.add_handler(appointment_conv)
    app.add_handler(cancel_appt_conv)
    app.add_handler(settimezone_conv)
    app.add_handler(language_conv)

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

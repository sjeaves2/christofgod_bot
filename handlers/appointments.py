"""Appointments end-to-end: request flow, official/proxy responses, listing,
cancellation, rescheduling, reminder DMs, request-rate limits, and archiving.

Also owns the appointment lifecycle helpers (status sets, overlap detection,
date parsing) shared by those flows.
"""

from __future__ import annotations

import calendar
import io
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

import permissions
import pytz
import storage
from common import (
    _answer_cb,
    md,
    _is_affirmative,
    format_dt,
    get_user_prefs,
    now_tz,
)
from ics_generator import appointment_cancellation_to_ics, appointment_to_ics
from localization import status_label, t
from permissions import admin_only, user_info
from settings import TZ, activity
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler

logger = logging.getLogger(__name__)

# Appointments whose date passed this many days ago are moved out of the live
# file into data/appointments_archive.yaml (any status — a request still
# "pending" 90 days after its date is dead).
APPT_ARCHIVE_AFTER_DAYS = 90


async def archive_old_appointments() -> int:
    """Move long-past appointments to the archive file. Returns how many moved.

    Records with unparseable dates are kept in the live file (never silently
    discarded). The archive is append-only.
    """
    cutoff = now_tz() - timedelta(days=APPT_ARCHIVE_AFTER_DAYS)
    appts = await storage.get_appointments()
    keep: list[dict[str, Any]] = []
    old: list[dict[str, Any]] = []
    for a in appts:
        dt = _appt_datetime(a)
        (old if dt is not None and dt < cutoff else keep).append(a)
    if not old:
        return 0
    archive_data = await storage.appts_archive_cache.get() or {}
    archive = archive_data.get("appointments") or []
    archive.extend(old)
    archive_data["appointments"] = archive
    await storage.appts_archive_cache.save(archive_data)
    await storage.save_appointments(keep)
    logger.info("Archived %d appointment(s) older than %d days (%d remain live).",
                len(old), APPT_ARCHIVE_AFTER_DAYS, len(keep))
    return len(old)


# Archived appointments are permanently deleted once their date is this far in
# the past, bounding the archive's growth (and how long congregants' meeting
# records are retained).
APPT_ARCHIVE_RETENTION_DAYS = 2 * 365


async def purge_archived_appointments() -> int:
    """Delete archived appointments past the retention window. Returns count.

    Records with unparseable dates are kept (never silently discarded).
    """
    cutoff = now_tz() - timedelta(days=APPT_ARCHIVE_RETENTION_DAYS)
    data = await storage.appts_archive_cache.get() or {}
    archive = data.get("appointments") or []
    keep = []
    for a in archive:
        dt = _appt_datetime(a)
        if dt is None or dt >= cutoff:
            keep.append(a)
    purged = len(archive) - len(keep)
    if purged:
        data["appointments"] = keep
        await storage.appts_archive_cache.save(data)
        logger.info("Purged %d archived appointment(s) older than %d days.",
                    purged, APPT_ARCHIVE_RETENTION_DAYS)
    return purged




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


# Reminder DMs before a confirmed appointment, farthest first: (stage, minutes).
APPT_REMINDER_STAGES: list[tuple[str, int]] = [("24h", 24 * 60), ("2h", 2 * 60)]


async def _appt_reminder_recipients(appt: dict[str, Any]) -> dict:
    """Both parties of an appointment as {chat_id: (tz, lang)}."""
    recipients: dict = {}
    user_cid = appt.get("user_chat_id")
    if user_cid:
        recipients[user_cid] = await get_user_prefs(user_cid)
    off = permissions._official_by_id(appt.get("official_id"))
    off_cid = (off or {}).get("chat_id")
    if off_cid:
        recipients[off_cid] = await get_user_prefs(off_cid)
    return recipients


async def appointment_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send reminder DMs for confirmed appointments (24h and 2h before).

    Runs on a short repeating interval. Idempotent: delivered chat_ids are
    recorded per stage on the appointment record itself, so restarts and
    partial failures retry only what's missing. If more than one stage is due
    at once (e.g. the bot was offline, or the appointment was booked close in),
    only the closest stage is sent — the farther ones are marked superseded.
    Rescheduling re-arms reminders (state is cleared on re-confirmation).
    """
    now = now_tz()
    appts = await storage.get_appointments()
    changed = False
    for appt in appts:
        if appt.get("status") != "confirmed":
            continue
        dt = _appt_datetime(appt)
        if dt is None or dt <= now:
            continue
        due = [s for s, mins in APPT_REMINDER_STAGES
               if now >= dt - timedelta(minutes=mins)]
        if not due:
            continue
        closest = due[-1]  # stages are ordered farthest-first
        recipients = await _appt_reminder_recipients(appt)
        sent_map: dict = appt.setdefault("reminders_sent", {})
        for stage in due:
            done = set(sent_map.get(stage, []))
            for chat_id, (u_tz, u_lang) in recipients.items():
                if chat_id in done:
                    continue
                if stage != closest:
                    done.add(chat_id)  # superseded by a closer stage
                    changed = True
                    continue
                is_requester = chat_id == appt.get("user_chat_id")
                key = "appt_reminder_user" if is_requester else "appt_reminder_official"
                counterparty = md(appt.get("official_name") if is_requester
                                else appt.get("user_display_name")
                                or appt.get("user_username") or "the requester")
                try:
                    await context.bot.send_message(
                        chat_id,
                        t(key, u_lang, id=appt["id"], counterparty=counterparty,
                          when=format_dt(dt, u_tz, u_lang)),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    done.add(chat_id)
                    changed = True
                except TelegramError as exc:
                    logger.warning("Appointment reminder failed for chat %s (appt %s): %s",
                                   chat_id, appt["id"], exc)
            sent_map[stage] = sorted(done)
    if changed:
        await storage.save_appointments(appts)


def _appt_dt_label(appt: dict[str, Any], tz: "pytz.BaseTzInfo | None" = None,
                   lang: "str | None" = None) -> str:
    """Human-readable date/time for an appointment, falling back to the raw value."""
    dt_obj = _appt_datetime(appt)
    if dt_obj is None:
        return appt.get("confirmed_datetime") or appt.get("requested_datetime") or "—"
    return format_dt(dt_obj, tz, lang)


def _datetime_is_past(value) -> bool:
    """True if an ISO string / datetime is in the past (naive values are localized)."""
    if value is None:
        return False
    try:
        dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return False
    if dt.tzinfo is None:
        dt = TZ.localize(dt)
    return dt <= now_tz()


def _appt_is_past(appt: dict[str, Any]) -> bool:
    """True if the appointment's scheduled time is in the past."""
    dt_obj = _appt_datetime(appt)
    return dt_obj is not None and dt_obj <= now_tz()


def _user_is_appt_official(appt: dict[str, Any], user_id: int, username: "str | None") -> bool:
    """True if this user is the official assigned to the given appointment."""
    off = next((o for o in permissions.OFFICIALS if o.get("id") == appt.get("official_id")), None)
    if not off:
        return False
    if off.get("chat_id") == user_id:
        return True
    uname_lower = (username or "").lstrip("@").lower()
    oname = (off.get("telegram_username") or "").lstrip("@").lower()
    return bool(uname_lower) and oname == uname_lower


# ---------------------------------------------------------------------------
# Officials' appointment proxies (secretaries who can negotiate on their behalf)
# ---------------------------------------------------------------------------

async def cmd_enable_appt_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Officer-only: enable/disable their appointment proxies. Usage: /enable_appt_proxies yes|no"""
    uid, uname, dname = user_info(update)
    uname_lower = (uname or "").lstrip("@").lower()
    off = next((o for o in permissions.OFFICIALS if permissions._person_matches(o, uid, uname_lower)), None)
    if not off:
        await update.message.reply_text("Only a church official can manage appointment proxies.")
        return

    arg = (context.args[0].strip().lower() if context.args else "")
    if arg in ("yes", "y", "true", "on", "1", "sí", "si", "oui"):
        enabled = True
    elif arg in ("no", "n", "false", "off", "0", "non"):
        enabled = False
    else:
        await update.message.reply_text("Usage: /enable_appt_proxies yes|no")
        return

    off["proxies_enabled"] = enabled
    permissions._save_officials()
    activity.log_command("enable_appt_proxies", uid, uname, dname,
                         details=f"{off.get('id')}={enabled}")

    proxies = off.get("proxies") or []
    if enabled:
        names = ", ".join(p.get("name", "?") for p in proxies) or "none configured yet"
        await update.message.reply_text(
            f"✅ Appointment proxies *enabled*.\nYour proxies: {md(names)}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("✅ Appointment proxies *disabled*.",
                                        parse_mode=ParseMode.MARKDOWN)


# Affirmative replies accepted for typed yes/no prompts, across supported languages.
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

# Request-rate limiting on /appointment (option E): a short cooldown between
# actions plus a cap on outstanding pending requests. Admins are exempt.
APPOINTMENT_COOLDOWN_SECONDS = 120
APPOINTMENT_MAX_PENDING = 5


def _stamp_appt_action(appt: dict[str, Any]) -> None:
    """Record the time of the latest action on an appointment (create, confirm,
    cancel, reschedule) so the per-user request cooldown can be measured."""
    appt["last_action_at"] = now_tz().isoformat()


def _user_last_action_at(
    appts: list[dict[str, Any]], user_id: int
) -> "datetime | None":
    """Most recent last_action_at across this user's appointments, or None."""
    latest: "datetime | None" = None
    for a in appts:
        if a.get("user_chat_id") != user_id:
            continue
        ts = a.get("last_action_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = TZ.localize(dt)
        if latest is None or dt > latest:
            latest = dt
    return latest


def _count_pending_appts(appts: list[dict[str, Any]], user_id: int) -> int:
    """How many of the user's requests are still awaiting a response."""
    return sum(
        1 for a in appts
        if a.get("user_chat_id") == user_id and a.get("status") == "pending"
    )


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
        for i, off in enumerate(permissions.OFFICIALS)
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
    if not data.isdigit() or not (0 <= int(data) < len(permissions.OFFICIALS)):
        await query.edit_message_text(t("appt_invalid_number", lang))
        return ConversationHandler.END
    off = permissions.OFFICIALS[int(data)]

    # Per-official frequency limit: at most APPOINTMENT_MAX_PER_WINDOW active
    # appointments within ±APPOINTMENT_WINDOW_HALF_DAYS of now.
    appts = await storage.get_appointments()
    if _count_active_appts_with_official(appts, uid, off["id"], now_tz()) >= APPOINTMENT_MAX_PER_WINDOW:
        await query.edit_message_text(
            t("appt_limit_reached", lang, official=md(off["name"]),
              max=APPOINTMENT_MAX_PER_WINDOW, days=APPOINTMENT_WINDOW_HALF_DAYS * 2),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    context.user_data["ap_official"] = off
    await query.edit_message_text(
        f"*{md(off['name'])}*\n\n" + t("appt_ask_date", lang),
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
    appts = await storage.get_appointments()
    clash = _overlapping_appt(appts, uid, req_dt, DEFAULT_APPT_DURATION_MIN)
    if clash:
        await update.message.reply_text(
            t("appt_overlap", lang, official=md(clash["official_name"]),
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
    summary = t("appt_summary", lang, official=md(off["name"]),
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

    appts = await storage.get_appointments()

    # Request-rate limits (admins exempt): a short cooldown after any recent
    # appointment action, and a cap on outstanding pending requests.
    if not permissions.is_admin(update):
        last_action = _user_last_action_at(appts, uid)
        if last_action is not None:
            elapsed = (now_tz() - last_action).total_seconds()
            if elapsed < APPOINTMENT_COOLDOWN_SECONDS:
                wait = int(APPOINTMENT_COOLDOWN_SECONDS - elapsed) or 1
                await update.message.reply_text(
                    t("appt_cooldown", lang, seconds=wait),
                    parse_mode=ParseMode.MARKDOWN,
                )
                return ConversationHandler.END
        if _count_pending_appts(appts, uid) >= APPOINTMENT_MAX_PENDING:
            await update.message.reply_text(
                t("appt_too_many_pending", lang, max=APPOINTMENT_MAX_PENDING),
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END

    # Final guard: per-official frequency limit within ±15 days of now.
    if _count_active_appts_with_official(appts, uid, off["id"], now_tz()) >= APPOINTMENT_MAX_PER_WINDOW:
        await update.message.reply_text(
            t("appt_limit_not_submitted", lang, official=md(off["name"]),
              max=APPOINTMENT_MAX_PER_WINDOW, days=APPOINTMENT_WINDOW_HALF_DAYS * 2),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    # Final guard: ensure the requested time doesn't overlap another appointment.
    clash = _overlapping_appt(appts, uid, req_dt, DEFAULT_APPT_DURATION_MIN)
    if clash:
        await update.message.reply_text(
            t("appt_overlap_not_submitted", lang, official=md(clash["official_name"]), id=clash["id"]),
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
    _stamp_appt_action(appt)
    appts.append(appt)
    await storage.save_appointments(appts)

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
    off = permissions._official_by_id(appt["official_id"])
    if not off:
        return
    recipients = permissions._official_side_recipients(off)
    if not recipients:
        logger.warning("Official %s (and any proxies) have no chat_id — they need to /start.",
                       appt["official_id"])
        return

    req_dt_str = format_dt(datetime.fromisoformat(appt["requested_datetime"]))
    base = (
        f"📅 *Appointment Request* (ID: `{appt['id']}`)\n\n"
        f"From: {md(appt['user_display_name'])}"
        + (f" (@{md(appt['user_username'])})" if appt.get("user_username") else "")
        + f"\nRequested: {req_dt_str}\n"
        f"Purpose: {md(appt['description'])}"
    )
    proxy_note = f"\n\n_You're receiving this as {md(off.get('name'))}'s proxy._"

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"{CB_APPT_PREFIX}confirm:{appt['id']}"),
            InlineKeyboardButton("📅 Suggest time", callback_data=f"{CB_APPT_PREFIX}counter:{appt['id']}"),
            InlineKeyboardButton("❌ Decline", callback_data=f"{CB_APPT_PREFIX}decline:{appt['id']}"),
        ]
    ])

    # Fetch the requester's profile photo once and reuse it for every recipient.
    photo_id = None
    try:
        photos = await context.bot.get_user_profile_photos(appt["user_chat_id"], limit=1)
        if photos.photos:
            photo_id = photos.photos[0][-1].file_id
    except TelegramError:
        pass

    for r in recipients:
        caption = base + (proxy_note if r["is_proxy"] else "")
        try:
            if photo_id:
                await context.bot.send_photo(r["chat_id"], photo_id, caption=caption,
                                             parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            else:
                await context.bot.send_message(r["chat_id"], caption,
                                               parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except TelegramError as exc:
            logger.warning("Couldn't send appointment request to %s: %s", r["chat_id"], exc)


# ---------------------------------------------------------------------------
# Appointment callback handler (official responses)
# ---------------------------------------------------------------------------

# Store pending counter-propose state outside conversation
_counter_propose_state: dict[str, Any] = {}  # appt_id -> {"chat_id": ..., "role": "official"|"user"}

# Actions taken by the official side (official or an enabled proxy). The rest
# (accept_counter / decline_counter) are taken by the requester.
OFFICIAL_SIDE_ACTIONS = {"confirm", "decline", "counter",
                         "accept_user_counter", "decline_user_counter"}


def _requester_proxy_note(appt: dict) -> str:
    """A note for the requester that a proxy is handling this on the official's behalf."""
    if appt.get("negotiator_is_proxy"):
        return f"\n\n_Handled by {appt.get('official_name')}'s office on their behalf._"
    return ""


async def _notify_negotiation_started(
    context: ContextTypes.DEFAULT_TYPE, off: dict, appt: dict, claimant_chat_id: int
) -> None:
    """Tell the official and any other proxies that someone has taken up the request."""
    requester = md(appt.get("user_display_name") or appt.get("user_username") or "the requester")
    claimant = md(appt.get("negotiator_name") or "Someone")
    by_proxy = appt.get("negotiator_is_proxy")
    for r in permissions._official_side_recipients(off):
        if r["chat_id"] == claimant_chat_id:
            continue
        if by_proxy and not r["is_proxy"]:
            msg = (f"🔔 {claimant} is negotiating an appointment on your behalf "
                   f"with {requester} (ID: `{appt['id']}`).")
        else:
            msg = (f"🔔 {claimant} has started handling the appointment request from "
                   f"{requester} (ID: `{appt['id']}`).")
        try:
            await context.bot.send_message(r["chat_id"], msg, parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            pass


async def appt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _answer_cb(query)
    data: str = query.data
    parts = data[len(CB_APPT_PREFIX):].split(":")
    action, appt_id = parts[0], parts[1]

    appts = await storage.get_appointments()
    appt = next((a for a in appts if a["id"] == appt_id), None)
    if not appt:
        await query.edit_message_text("⚠️ Appointment not found.")
        return

    # Reschedule responses act on an already-confirmed (i.e. "terminal") appointment,
    # so they are handled before the terminal-status guard below.
    if action in ("rs_accept", "rs_decline"):
        await _handle_reschedule_response(context, query, appt, appts, action)
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

    # Can't start rescheduling an appointment whose time has already passed.
    if action in ("counter", "decline_counter") and _appt_is_past(appt):
        await query.edit_message_text(
            "⚠️ This appointment's time has already passed; it can no longer be rescheduled."
        )
        return

    # Official-side actions may come from the official OR an enabled proxy.
    # The first to act "claims" the appointment and must see it through; anyone
    # else who taps is told it's already being handled.
    if action in OFFICIAL_SIDE_ACTIONS:
        off = permissions._official_by_id(appt.get("official_id"))
        actor_id = query.from_user.id
        actor_uname = query.from_user.username
        if not permissions._user_can_act_for_official(off, actor_id, actor_uname):
            await query.edit_message_text("⚠️ You're not authorized to act on this appointment.")
            return
        negotiator = appt.get("negotiator_chat_id")
        if negotiator is None:
            name, is_proxy = permissions._acting_identity(off, actor_id, actor_uname)
            appt["negotiator_chat_id"] = actor_id
            appt["negotiator_name"] = name or "The official"
            appt["negotiator_is_proxy"] = is_proxy
            for i, a in enumerate(appts):
                if a["id"] == appt_id:
                    appts[i] = appt
            await storage.save_appointments(appts)
            await _notify_negotiation_started(context, off, appt, actor_id)
        elif negotiator != actor_id:
            await query.edit_message_text(
                f"ℹ️ This appointment is already being handled by "
                f"{md(appt.get('negotiator_name', 'someone'))}."
            )
            return

    if action == "confirm":
        clash = _confirmed_overlap(appts, appt, appt["requested_datetime"])
        if clash:
            await query.edit_message_text(
                f"⚠️ That time overlaps the requester's appointment with "
                f"{md(clash['official_name'])} (ID: {clash['id']}). Not confirmed — "
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
        await storage.save_appointments(appts)
        await query.edit_message_text(f"❌ You declined the appointment (ID: {appt_id}).")
        await context.bot.send_message(user_chat_id,
            f"❌ Your appointment request (ID: `{appt_id}`) has been declined."
            + _requester_proxy_note(appt),
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
        if _datetime_is_past(proposed):
            await query.edit_message_text(
                "⚠️ That suggested time has already passed. Please suggest a new time."
            )
            return
        clash = _confirmed_overlap(appts, appt, proposed)
        if clash:
            await query.edit_message_text(
                f"⚠️ That time overlaps your appointment with {md(clash['official_name'])} "
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
            "Suggest a different date/time (or type 'cancel' to cancel the request):\n"
            "YYYY-MM-DD HH:MM"
        )

    elif action == "accept_user_counter":
        # Official accepts user's counter-proposed time
        proposed = appt.get("user_counter_datetime", appt["requested_datetime"])
        if _datetime_is_past(proposed):
            await query.edit_message_text(
                "⚠️ That suggested time has already passed. Please suggest a new time."
            )
            return
        clash = _confirmed_overlap(appts, appt, proposed)
        if clash:
            await query.edit_message_text(
                f"⚠️ That time overlaps the requester's appointment with "
                f"{md(clash['official_name'])} (ID: {clash['id']}). Not confirmed."
            )
            return
        appt["status"] = "confirmed"
        appt["confirmed_datetime"] = proposed
        await _finalize_appointment(context, appt, appts)
        await query.edit_message_text("✅ You confirmed the appointment with the user's suggested time.")

    elif action == "decline_user_counter":
        appt["status"] = "declined"
        for i, a in enumerate(appts):
            if a["id"] == appt_id:
                appts[i] = appt
        await storage.save_appointments(appts)
        await query.edit_message_text("❌ Request cancelled.")
        await context.bot.send_message(user_chat_id,
            f"Your appointment request (ID: `{appt_id}`) has been cancelled.",
            parse_mode=ParseMode.MARKDOWN)


async def _finalize_appointment(
    context: ContextTypes.DEFAULT_TYPE, appt: dict, appts: list
) -> None:
    """Save confirmed appointment and send ICS to both the user and the official."""
    _stamp_appt_action(appt)
    appt.pop("reminders_sent", None)  # re-arm reminder DMs for the (new) time
    for i, a in enumerate(appts):
        if a["id"] == appt["id"]:
            appts[i] = appt
    await storage.save_appointments(appts)

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
          official=md(appt["official_name"]), when=format_dt(confirmed_dt, user_tz, user_lang)),
        parse_mode=ParseMode.MARKDOWN,
    )
    await context.bot.send_document(
        appt["user_chat_id"],
        document=InputFile(user_bio, filename="appointment.ics"),
        caption=t("appt_ics_caption", user_lang),
    )

    user_display = md(appt.get("user_display_name") or appt.get("user_username") or "The requester")

    # --- Notify and send ICS to the official (their timezone) ---
    off = next((o for o in permissions.OFFICIALS if o["id"] == appt["official_id"]), None)
    if off and off.get("chat_id"):
        off_tz, _ = await get_user_prefs(off["chat_id"])
        off_dt_str = format_dt(confirmed_dt, off_tz)
        ics_bytes_off = appointment_to_ics(appt_with_dt, TZ)
        off_bio = io.BytesIO(ics_bytes_off)
        await context.bot.send_message(
            off["chat_id"],
            f"✅ *Appointment confirmed (ID: `{appt['id']}`)*\n"
            f"With: {user_display}"
            + (f" (@{md(appt['user_username'])})" if appt.get("user_username") else "") + "\n"
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

    # --- Note to the negotiating proxy (if a proxy arranged this) ---
    if appt.get("negotiator_is_proxy") and appt.get("negotiator_chat_id"):
        await context.bot.send_message(
            appt["negotiator_chat_id"],
            f"✅ Appointment confirmed: *{md(appt['official_name'])}* is scheduled with "
            f"{user_display} on {format_dt(confirmed_dt)} (ID: `{appt['id']}`).",
            parse_mode=ParseMode.MARKDOWN,
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

    appts = await storage.get_appointments()
    appt = next((a for a in appts if a["id"] == appt_id), None)
    if not appt:
        return

    role = _counter_propose_state[appt_id]["role"]
    del _counter_propose_state[appt_id]

    if role == "user" and text.lower() == "cancel":
        appt["status"] = "cancelled"
        _stamp_appt_action(appt)
        for i, a in enumerate(appts):
            if a["id"] == appt_id:
                appts[i] = appt
        await storage.save_appointments(appts)
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
    try:
        new_dt = TZ.localize(datetime(parts_d[0], parts_d[1], parts_d[2], parts_t[0], parts_t[1]))
    except ValueError:
        await update.message.reply_text(
            "That date/time isn't valid. Please use: YYYY-MM-DD HH:MM"
        )
        _counter_propose_state[appt_id] = {"chat_id": chat_id, "role": role}
        return
    if new_dt <= now_tz():
        await update.message.reply_text(
            "That date/time is in the past. Please suggest a future time: YYYY-MM-DD HH:MM"
        )
        _counter_propose_state[appt_id] = {"chat_id": chat_id, "role": role}
        return
    new_dt_str = format_dt(new_dt)

    if role == "official":
        # Official suggests new time → notify user
        appt["counter_datetime"] = new_dt.isoformat()
        appt["status"] = "counter_proposed"
        for i, a in enumerate(appts):
            if a["id"] == appt_id:
                appts[i] = appt
        await storage.save_appointments(appts)
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
            f"With: {md(appt['official_name'])}\n"
            f"Suggested: {new_dt_str}\n"
            f"Purpose: {md(appt['description'])}"
            + _requester_proxy_note(appt),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    else:
        # User suggests alternative → notify the negotiator (official or their proxy)
        appt["user_counter_datetime"] = new_dt.isoformat()
        for i, a in enumerate(appts):
            if a["id"] == appt_id:
                appts[i] = appt
        await storage.save_appointments(appts)
        await update.message.reply_text(f"✅ Your suggested time has been forwarded: {new_dt_str}")
        off = permissions._official_by_id(appt["official_id"])
        target_chat = appt.get("negotiator_chat_id") or (off.get("chat_id") if off else None)
        if target_chat:
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Accept", callback_data=f"{CB_APPT_PREFIX}accept_user_counter:{appt_id}"),
                    InlineKeyboardButton("❌ Decline", callback_data=f"{CB_APPT_PREFIX}decline_user_counter:{appt_id}"),
                ]
            ])
            await context.bot.send_message(
                target_chat,
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
        name = md(appt.get("user_display_name") or appt.get("user_username") or "Unknown requester")
        if appt.get("user_username"):
            return f"{name} (@{appt['user_username']})"
        return name
    return appt.get("official_name", "Unknown official")


async def cmd_myappointments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid, uname, dname = user_info(update)
    activity.log_command("myappointments", uid, uname, dname)
    tz, lang = await get_user_prefs(uid)

    appts = await storage.get_appointments()
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

    appts = await storage.get_appointments()
    # Build list of appointments this user is party to that are still active
    # and have not already taken place (you can't cancel a past appointment).
    # "Party to" = the requester, the official, or an enabled proxy.
    active = []
    for a in appts:
        if a.get("status") not in ACTIVE_APPT_STATUSES:
            continue
        if _appt_is_past(a):
            continue
        if permissions._user_can_act_for_appt(a, uid, uname):
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
        t("cancel_confirm_prompt", lang, official=md(appt["official_name"]),
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
    # Defensive: the appointment must still be in the future to cancel it.
    if _appt_is_past(appt):
        await query.edit_message_text(t("cancel_past", lang))
        return ConversationHandler.END

    appts = await storage.get_appointments()
    for i, a in enumerate(appts):
        if a["id"] == appt["id"]:
            appts[i]["status"] = "cancelled"
            _stamp_appt_action(appts[i])
            break
    await storage.save_appointments(appts)

    activity.log_command(
        "cancelappointment", uid, uname, dname,
        details=f"Cancelled appointment {appt['id']}"
    )

    # Determine who cancelled so we can notify the other party. An official OR
    # an enabled proxy counts as the "official side".
    is_off = permissions._user_can_act_for_appt(appt, uid, uname)
    off = next((o for o in permissions.OFFICIALS if o.get("id") == appt.get("official_id")), None)
    user_chat_id = appt.get("user_chat_id")

    if is_off:
        # Official cancelled → notify the requester (in their language)
        if user_chat_id:
            _, req_lang = await get_user_prefs(user_chat_id)
            await context.bot.send_message(
                user_chat_id,
                t("cancel_done_by_official_to_user", req_lang,
                  id=appt["id"], official=md(appt["official_name"])),
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
            user_display = md(appt.get("user_display_name") or appt.get("user_username") or "The requester")
            await context.bot.send_message(
                off["chat_id"],
                f"❌ Appointment (ID: `{appt['id']}`) with "
                + (f"*{user_display}*" if user_display else "a congregant")
                + (f" (@{md(appt['user_username'])})" if appt.get("user_username") else "")
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
# /reschedule — propose a new time for an upcoming appointment
# ---------------------------------------------------------------------------
#
# The original appointment stays confirmed at its current time until the other
# party accepts the proposed new time. A decline keeps the original.
# Reschedule actions ("rs_accept"/"rs_decline") ride on CB_APPT_PREFIX and are
# handled early in appt_callback (before the terminal-status guard, since a
# confirmed appointment is "terminal").

RS_SELECT, RS_NEWTIME = range(2)

CB_RESCHED_PREFIX = "rs:"


async def cmd_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("reschedule", uid, uname, dname)
    context.user_data.clear()
    tz, lang = await get_user_prefs(uid)

    appts = await storage.get_appointments()
    # Future, active appointments the user is party to (requester, official, or
    # an enabled proxy).
    reschedulable: list[dict] = []
    for a in appts:
        if a.get("status") not in ACTIVE_APPT_STATUSES:
            continue
        if _appt_is_past(a):
            continue
        if permissions._user_can_act_for_appt(a, uid, uname):
            reschedulable.append(a)
        if a.get("user_chat_id") == uid and not any(x["id"] == a["id"] for x in reschedulable):
            reschedulable.append(a)

    if not reschedulable:
        await update.message.reply_text(t("resched_none", lang))
        return ConversationHandler.END

    context.user_data["rs_appts"] = reschedulable
    rows = []
    for i, a in enumerate(reschedulable):
        label = f"{a['official_name']} — {_appt_dt_label(a, tz, lang)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{CB_RESCHED_PREFIX}sel:{i}")])
    rows.append([InlineKeyboardButton(
        "✖️ " + t("cancel_aborted", lang).rstrip("."),
        callback_data=f"{CB_RESCHED_PREFIX}abort",
    )])
    await update.message.reply_text(
        t("resched_list_header", lang),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return RS_SELECT


async def rs_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _answer_cb(query)
    uid = query.from_user.id
    _, lang = await get_user_prefs(uid)
    data = query.data[len(CB_RESCHED_PREFIX):]
    reschedulable: list[dict] = context.user_data.get("rs_appts", [])

    if data == "abort":
        await query.edit_message_text(t("cancel_aborted", lang))
        return ConversationHandler.END

    idx = data.split(":", 1)[1] if data.startswith("sel:") else ""
    if not idx.isdigit() or not (0 <= int(idx) < len(reschedulable)):
        await query.edit_message_text(t("cancel_aborted", lang))
        return ConversationHandler.END

    appt = reschedulable[int(idx)]
    # Role of the person rescheduling: the requester, or the official side.
    context.user_data["rs_appt_id"] = appt["id"]
    context.user_data["rs_role"] = "user" if appt.get("user_chat_id") == uid else "official"
    await query.edit_message_text(t("resched_ask_time", lang))
    return RS_NEWTIME


async def rs_newtime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    tz, lang = await get_user_prefs(uid)
    text = update.message.text.strip()
    appt_id = context.user_data.get("rs_appt_id")
    role = context.user_data.get("rs_role")

    appts = await storage.get_appointments()
    appt = next((a for a in appts if a["id"] == appt_id), None)
    if not appt or appt.get("status") not in ACTIVE_APPT_STATUSES or _appt_is_past(appt):
        await update.message.reply_text(t("resched_no_longer", lang))
        return ConversationHandler.END

    m = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", text)
    if not m:
        await update.message.reply_text(t("resched_bad_format", lang))
        return RS_NEWTIME
    parts_d = [int(x) for x in m.group(1).split("-")]
    parts_t = [int(x) for x in m.group(2).split(":")]
    try:
        new_dt = TZ.localize(datetime(parts_d[0], parts_d[1], parts_d[2], parts_t[0], parts_t[1]))
    except ValueError:
        await update.message.reply_text(t("resched_bad_format", lang))
        return RS_NEWTIME
    if new_dt <= now_tz():
        await update.message.reply_text(t("resched_past", lang))
        return RS_NEWTIME
    # No overlap with the requester's other active appointments.
    if _overlapping_appt(appts, appt["user_chat_id"], new_dt,
                         DEFAULT_APPT_DURATION_MIN, exclude_id=appt["id"]):
        await update.message.reply_text(t("resched_overlap", lang))
        return RS_NEWTIME

    # Record the proposal WITHOUT touching the confirmed status/time.
    appt["reschedule_proposed_datetime"] = new_dt.isoformat()
    appt["reschedule_proposed_by"] = role
    _stamp_appt_action(appt)
    off = permissions._official_by_id(appt["official_id"])
    if role == "official" and off:
        name, is_proxy = permissions._acting_identity(off, uid, uname)
        appt["negotiator_chat_id"] = uid
        appt["negotiator_name"] = name or "The official"
        appt["negotiator_is_proxy"] = is_proxy
    for i, a in enumerate(appts):
        if a["id"] == appt_id:
            appts[i] = appt
    await storage.save_appointments(appts)

    new_dt_str = format_dt(new_dt)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"{CB_APPT_PREFIX}rs_accept:{appt_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"{CB_APPT_PREFIX}rs_decline:{appt_id}"),
    ]])

    if role == "official":
        # Notify the requester.
        await context.bot.send_message(
            appt["user_chat_id"],
            f"📅 *Reschedule requested for appointment `{appt_id}`*\n"
            f"With: {md(appt['official_name'])}\n"
            f"New time: {new_dt_str}\n"
            f"Purpose: {appt.get('description', '')}"
            + _requester_proxy_note(appt),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    else:
        # Requester is rescheduling → notify the official side (officer + proxies).
        requester = md(appt.get("user_display_name") or appt.get("user_username") or "The requester")
        for r in permissions._official_side_recipients(off) if off else []:
            try:
                await context.bot.send_message(
                    r["chat_id"],
                    f"📅 *Reschedule requested for appointment `{appt_id}`*\n"
                    f"From: {requester}\n"
                    f"New time: {new_dt_str}\n"
                    f"Purpose: {appt.get('description', '')}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb,
                )
            except TelegramError:
                pass

    await update.message.reply_text(t("resched_sent", lang))
    return ConversationHandler.END


async def _handle_reschedule_response(
    context: ContextTypes.DEFAULT_TYPE, query, appt: dict, appts: list, action: str
) -> None:
    """Handle the other party accepting/declining a proposed reschedule."""
    appt_id = appt["id"]
    proposed = appt.get("reschedule_proposed_datetime")
    if not proposed:
        await query.edit_message_text("ℹ️ This reschedule request has already been handled.")
        return

    proposed_by = appt.get("reschedule_proposed_by")
    actor_id = query.from_user.id
    actor_uname = query.from_user.username
    # The party who did NOT propose responds.
    if proposed_by == "official":
        authorized = actor_id == appt.get("user_chat_id")
    else:  # proposed by the requester → official side responds
        authorized = permissions._user_can_act_for_appt(appt, actor_id, actor_uname)
    if not authorized:
        await query.edit_message_text("⚠️ You're not authorized to respond to this reschedule.")
        return

    def _clear_and_store():
        appt.pop("reschedule_proposed_datetime", None)
        appt.pop("reschedule_proposed_by", None)
        for i, a in enumerate(appts):
            if a["id"] == appt_id:
                appts[i] = appt

    proposer_chat = (appt["user_chat_id"] if proposed_by == "user"
                     else appt.get("negotiator_chat_id")
                     or (permissions._official_by_id(appt["official_id"]) or {}).get("chat_id"))

    if action == "rs_decline":
        _clear_and_store()
        await storage.save_appointments(appts)
        await query.edit_message_text("❌ Reschedule declined; the original time stands.")
        if proposer_chat:
            await context.bot.send_message(
                proposer_chat,
                f"❌ Your reschedule request for appointment `{appt_id}` was declined; "
                "the original time stands.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # rs_accept
    if _datetime_is_past(proposed):
        await query.edit_message_text(
            "⚠️ That proposed time has already passed; no change was made."
        )
        _clear_and_store()
        await storage.save_appointments(appts)
        return
    clash = _confirmed_overlap(appts, appt, proposed)
    if clash:
        await query.edit_message_text(
            f"⚠️ That time now overlaps another appointment (ID: {clash['id']}); "
            "not rescheduled."
        )
        return
    appt["confirmed_datetime"] = proposed
    appt["status"] = "confirmed"
    _clear_and_store()
    await _finalize_appointment(context, appt, appts)  # new ICS to both, proxy note if any
    await query.edit_message_text("✅ Reschedule accepted; the new time is confirmed.")



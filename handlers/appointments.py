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
from datetime import date, datetime, timedelta
from typing import Any

import appointment_slots as slots
import permissions
import pytz
import storage
from babel import Locale
from babel.dates import format_date, format_time, get_day_names
from common import (
    edit_markdown,
    format_dt,
    get_user_prefs,
    md,
    now_tz,
    reply_markdown,
    send_markdown,
    _answer_cb,
    _is_affirmative,
)
from ics_generator import appointment_cancellation_to_ics, appointment_to_ics
from localization import AVAILABLE_LANGUAGES, DEFAULT_LANG, status_label, t
from permissions import user_info
from settings import APPOINTMENT_HOURS_RAW, TZ, activity
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from handlers.shared import cancel_conversation

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




def _appt_datetime(appt: dict[str, Any]) -> datetime | None:
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
                    await send_markdown(context.bot, chat_id,
                        t(key, u_lang, id=appt["id"], counterparty=counterparty,
                          when=format_dt(dt, u_tz, u_lang)))
                    done.add(chat_id)
                    changed = True
                except TelegramError as exc:
                    logger.warning("Appointment reminder failed for chat %s (appt %s): %s",
                                   chat_id, appt["id"], exc)
            sent_map[stage] = sorted(done)
    if changed:
        await storage.save_appointments(appts)


def _appt_dt_label(appt: dict[str, Any], tz: pytz.BaseTzInfo | None = None,
                   lang: str | None = None) -> str:
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


def _appt_window_has_ended(appt: dict[str, Any]) -> bool:
    """True once the appointment's whole SLOT is over, not merely begun.

    Deliberately separate from `_appt_is_past`, which is `start <= now` with
    NO grace. That is right for listing and reminders — an appointment that
    started a minute ago is no longer "upcoming" — but wrong as a gate on an
    official's action: an official tapping Confirm at 10:03 on a 10:00
    appointment is sitting down to the meeting, not confirming a dead
    request.

    `_appt_is_past` is NOT changed. Its other call sites are the "can the
    user still act on this?" filters in the cancel and reschedule flows
    (cmd_cancelappointment, ca_confirm, cmd_reschedule, _picker_refusal,
    _can_change_appt), where no grace is what you want.

    No COUNT of them here on purpose. This comment said "eleven other
    callers", which was wrong — a grep that also matched `_appt_datetime` —
    and the figure reached the v0.28.4 tag before anyone checked it. v0.27.1
    removed a count from a comment in handlers/shared.py for the same reason
    rather than correcting it: a number in a comment goes stale silently,
    while a list of names is at least wrong in a way a reader can see.

    The grace is the appointment's own `duration_minutes`, so the window
    closes when the meeting would have finished, rather than at some constant
    that would be wrong for both a 15-minute call and a 2-hour session.
    """
    start = _appt_datetime(appt)
    if start is None:
        return False
    minutes = int(appt.get("duration_minutes", DEFAULT_APPT_DURATION_MIN))
    return start + timedelta(minutes=minutes) <= now_tz()


def _user_is_appt_official(appt: dict[str, Any], user_id: int, username: str | None) -> bool:
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
    off = next((o for o in permissions.OFFICIALS
                if permissions._person_matches(o, uid, uname_lower)), None)
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
        await reply_markdown(update.message,
            f"✅ Appointment proxies *enabled*.\nYour proxies: {md(names)}")
    else:
        await reply_markdown(update.message, "✅ Appointment proxies *disabled*.")


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
    exclude_id: str | None = None,
) -> dict | None:
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
        a_end = a_start + timedelta(
            minutes=int(a.get("duration_minutes", DEFAULT_APPT_DURATION_MIN)))
        # Half-open intervals overlap when each starts before the other ends.
        if start < a_end and a_start < end:
            return a
    return None


def _confirmed_overlap(
    appts: list[dict[str, Any]], appt: dict[str, Any], confirmed_iso: str
) -> dict | None:
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


def _official_overlap(
    appts: list[dict[str, Any]], appt: dict[str, Any], start_iso: str
) -> dict | None:
    """The official's OTHER confirmed appointment overlapping *appt* at *start_iso*.

    The official-side twin of _confirmed_overlap, which only looks at the
    requester's calendar. Without this, two members' requests for the same
    hour could both be confirmed (asked for by the user 2026-11-23). Only
    CONFIRMED appointments count: a pending request is not yet a commitment,
    and treating it as one would let an unanswered request block a real one.
    """
    start = datetime.fromisoformat(start_iso)
    if start.tzinfo is None:
        start = TZ.localize(start)
    end = start + timedelta(minutes=int(appt.get("duration_minutes",
                                                  DEFAULT_APPT_DURATION_MIN)))
    for a in appts:
        if a.get("id") == appt.get("id"):
            continue
        if a.get("official_id") != appt.get("official_id"):
            continue
        if a.get("status") != "confirmed":
            continue
        a_start = _appt_datetime(a)
        if a_start is None:
            continue
        a_end = a_start + timedelta(
            minutes=int(a.get("duration_minutes", DEFAULT_APPT_DURATION_MIN)))
        if start < a_end and a_start < end:
            return a
    return None


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
) -> datetime | None:
    """Most recent last_action_at across this user's appointments, or None."""
    latest: datetime | None = None
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
    await reply_markdown(update.message, t("appt_choose_official", lang),
        reply_markup=InlineKeyboardMarkup(rows))
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
    if (_count_active_appts_with_official(appts, uid, off["id"], now_tz())
            >= APPOINTMENT_MAX_PER_WINDOW):
        await edit_markdown(query, t("appt_limit_reached", lang, official=md(off["name"]),
              max=APPOINTMENT_MAX_PER_WINDOW, days=APPOINTMENT_WINDOW_HALF_DAYS * 2))
        return ConversationHandler.END

    context.user_data["ap_official"] = off
    tz, _ = await get_user_prefs(uid)
    rules = _slot_rules(appts, off["id"], uid)
    month = _first_bookable_month(tz, rules)
    if month is None:
        await edit_markdown(query, t("appt_no_days", lang, official=md(off["name"]),
                                     months=APPOINTMENT_HORIZON_MONTHS))
        return ConversationHandler.END
    await _show_calendar(_edit_render(query), off["name"], month, tz, lang, rules)
    return AP_DATE


# ---------------------------------------------------------------------------
# The day and time picker (agreed with the user 2026-11-20)
# ---------------------------------------------------------------------------
#
# Replaces typing YYYY-MM-DD and then a 24-hour HH:MM. Which times are offered
# is appointment_slots' job; this part only draws the buttons and checks a tap
# against the same rules again, since a keyboard can sit on screen while the
# slot passes or someone else's appointment is confirmed into it.
#
# The tapped slot is stored as ap_date / ap_time in the CHURCH timezone, the
# shape the typed steps produced, so ap_desc and ap_confirm are unchanged.
# Note what that fixes: the typed time was read in the church timezone even
# though the member was shown times in their own, so a member abroad who typed
# 10:00 got 10:00 New York. The picker shows their time and stores the
# church's.

OFFICE_HOURS = slots.configured_hours(APPOINTMENT_HOURS_RAW)

CB_APCAL_PREFIX = "apcal:"


def _busy_intervals(appts: list[dict], official_id: str, user_id: int,
                    exclude_id: str | None = None
                    ) -> list[tuple[datetime, datetime]]:
    """Times that must not be offered to *user_id* booking *official_id*.

    The official's CONFIRMED appointments, and every active appointment of the
    requester's own (the overlap rule ap_confirm enforces anyway). Another
    member's PENDING request does not block a slot: it is only a request, and
    hiding it would let one unanswered request close a slot to everyone.

    *exclude_id* leaves out the appointment being re-timed, so its current
    slot — and the ones beside it — stay on offer.
    """
    busy = []
    for a in appts:
        if exclude_id and a.get("id") == exclude_id:
            continue
        status = a.get("status")
        own = a.get("user_chat_id") == user_id and status in ACTIVE_APPT_STATUSES
        officials = a.get("official_id") == official_id and status == "confirmed"
        if not (own or officials):
            continue
        start = _appt_datetime(a)
        if start is None:
            continue
        minutes = int(a.get("duration_minutes", DEFAULT_APPT_DURATION_MIN))
        busy.append((start, start + timedelta(minutes=minutes)))
    return busy


def _slot_rules(appts: list[dict], official_id: str, user_id: int,
                exclude_id: str | None = None) -> dict:
    """Keyword arguments for appointment_slots' functions, as of now."""
    now = now_tz()
    return {
        "church_tz": TZ,
        "hours": OFFICE_HOURS,
        "closed": slots.closed_dates(TZ, now.date()),
        "busy": _busy_intervals(appts, official_id, user_id, exclude_id),
        "now": now,
        "latest": _max_request_datetime(),
    }


def _locale(lang: str) -> str:
    return lang if lang in AVAILABLE_LANGUAGES else DEFAULT_LANG


def _plain_spaces(text: str) -> str:
    """CLDR's narrow/no-break spaces as plain ones, as localized_datetime does."""
    return text.translate({0x202F: " ", 0x00A0: " "})


def _month_bounds(viewer_tz, latest: datetime) -> tuple[tuple[int, int], tuple[int, int]]:
    """(year, month) of today and of the booking horizon, in the viewer's dates."""
    today = now_tz().astimezone(viewer_tz)
    last = latest.astimezone(viewer_tz)
    return (today.year, today.month), (last.year, last.month)


def _months(first: tuple[int, int], last: tuple[int, int]):
    year, month = first
    while (year, month) <= last:
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _calendar_keyboard(year: int, month: int, bookable: set[date], lang: str,
                       first: tuple[int, int], last: tuple[int, int],
                       prefix: str = CB_APCAL_PREFIX) -> InlineKeyboardMarkup:
    """A month grid: bookable days are buttons, the rest are inert dots.

    Every key needs callback data, so the inert ones carry `<prefix>n`. The
    prefix is the conversation's for /appointment and carries the appointment
    for the stateless picker (see appt_picker_callback). The week
    starts on the locale's own first day — Sunday in (American) English,
    Monday in Spanish and French — because that is how each reader's
    calendar looks.
    """
    loc = _locale(lang)
    names = get_day_names("short", locale=loc)  # keyed 0 = Monday
    # Plain "en" in CLDR is the world default and starts on Monday; the
    # congregation's English readers are American, so use en_US's Sunday.
    week_start = Locale.parse("en_US" if loc == "en" else loc).first_week_day
    title = format_date(date(year, month, 1), "LLLL y", locale=loc)
    title = title[:1].upper() + title[1:]

    def key(text: str, data: str = f"{prefix}n") -> InlineKeyboardButton:
        return InlineKeyboardButton(text, callback_data=data)

    rows = [[key(title)],
            [key(names[(week_start + i) % 7]) for i in range(7)]]
    for week in calendar.Calendar(firstweekday=week_start).monthdatescalendar(year, month):
        rows.append([
            key(" ") if d.month != month
            else key(str(d.day), f"{prefix}d:{d.isoformat()}") if d in bookable
            else key("·")
            for d in week
        ])
    def month_key(label: str, delta: int) -> InlineKeyboardButton:
        y, m = _shift_month(year, month, delta)
        return key(label, f"{prefix}m:{y}-{m:02d}")

    nav = []
    if (year, month) > first:
        nav.append(month_key("◀️", -1))
    if (year, month) < last:
        nav.append(month_key("▶️", 1))
    if nav:
        rows.append(nav)
    rows.append([key(t("appt_btn_cancel", lang), f"{prefix}x")])
    return InlineKeyboardMarkup(rows)


def _time_keyboard(free: list[datetime], viewer_tz, lang: str,
                   prefix: str = CB_APCAL_PREFIX) -> InlineKeyboardMarkup:
    """The free slots of one day, three to a row, in the viewer's time."""
    loc = _locale(lang)
    keys = [InlineKeyboardButton(
                _plain_spaces(format_time(s.astimezone(viewer_tz), "short", locale=loc)),
                callback_data=f"{prefix}t:{int(s.timestamp())}")
            for s in free]
    rows = [keys[i:i + 3] for i in range(0, len(keys), 3)]
    day = free[0].astimezone(viewer_tz)
    rows.append([
        InlineKeyboardButton(t("appt_btn_calendar", lang),
                             callback_data=f"{prefix}m:{day.year}-{day.month:02d}"),
        InlineKeyboardButton(t("appt_btn_cancel", lang),
                             callback_data=f"{prefix}x"),
    ])
    return InlineKeyboardMarkup(rows)


def _edit_render(query):
    """Draw the picker by editing the message that was tapped."""
    async def render(text: str, markup) -> None:
        await edit_markdown(query, text, reply_markup=markup)
    return render


def _reply_render(message):
    """Draw the picker as a NEW message under *message*.

    Used where the tapped message must keep its own buttons — and where it
    cannot be edited as text at all: an appointment request is sent as a
    PHOTO when the requester has a profile picture, and a photo has a caption,
    not text.
    """
    async def render(text: str, markup) -> None:
        await reply_markdown(message, text, reply_markup=markup)
    return render


async def _show_calendar(render, name: str, month: tuple[int, int], viewer_tz,
                         lang: str, rules: dict, notice: str = "",
                         prefix: str = CB_APCAL_PREFIX) -> None:
    first, last = _month_bounds(viewer_tz, rules["latest"])
    month = min(max(month, first), last)
    bookable = slots.bookable_viewer_days(*month, viewer_tz, **rules)
    tz_abbr = now_tz().astimezone(viewer_tz).strftime("%Z")
    text = (notice + "\n\n" if notice else "") + t(
        "appt_pick_day", lang, official=md(name), tz=tz_abbr)
    await render(text, _calendar_keyboard(*month, bookable, lang, first, last, prefix))


async def _show_times(render, name: str, day: date, free: list[datetime],
                      viewer_tz, lang: str, prefix: str = CB_APCAL_PREFIX) -> None:
    loc = _locale(lang)
    tz_abbr = free[0].astimezone(viewer_tz).strftime("%Z")
    await render(t("appt_pick_time", lang, official=md(name),
                   day=_plain_spaces(format_date(day, "full", locale=loc)), tz=tz_abbr),
                 _time_keyboard(free, viewer_tz, lang, prefix))


def _first_bookable_month(viewer_tz, rules: dict) -> tuple[int, int] | None:
    first, last = _month_bounds(viewer_tz, rules["latest"])
    return next((m for m in _months(first, last)
                 if slots.bookable_viewer_days(*m, viewer_tz, **rules)), None)


async def _picker_step(query, verb: str, arg: str, name: str, viewer_tz,
                       lang: str, rules: dict, prefix: str):
    """The navigation every picker shares: m(onth), d(ay), t(ime).

    Returns ("calendar" | "times", None) after drawing that screen, or
    ("chosen", slot) for a tapped slot that is STILL free — re-checked here,
    since the keyboard may have sat on screen while it passed or was taken.
    """
    render = _edit_render(query)
    today = now_tz().astimezone(viewer_tz)

    async def month_view(year, month, notice=""):
        await _show_calendar(render, name, (year, month), viewer_tz, lang, rules,
                             notice=notice, prefix=prefix)
        return "calendar", None

    if verb == "m":
        try:
            year, month = (int(x) for x in arg.split("-"))
        except ValueError:
            year, month = today.year, today.month
        return await month_view(year, month)

    if verb == "d":
        try:
            day = date.fromisoformat(arg)
        except ValueError:
            day = today.date()
        free = slots.free_slots_for_viewer_day(day, viewer_tz, **rules)
        if not free:
            return await month_view(day.year, day.month, t("appt_slot_gone", lang))
        await _show_times(render, name, day, free, viewer_tz, lang, prefix)
        return "times", None

    if verb == "t" and arg.isdigit():
        slot = datetime.fromtimestamp(int(arg), TZ)
        day = slot.astimezone(viewer_tz).date()
        free = slots.free_slots_for_viewer_day(day, viewer_tz, **rules)
        if slot in free:
            return "chosen", slot
        # Passed, or taken, since the keyboard was drawn. Offer what is left
        # on that day, or the month if nothing is.
        if free:
            await _show_times(render, name, day, free, viewer_tz, lang, prefix)
            await query.message.reply_text(t("appt_slot_gone", lang))
            return "times", None
        return await month_view(day.year, day.month, t("appt_slot_gone", lang))

    logger.warning("Unrecognised appointment picker callback %r", query.data)
    return await month_view(today.year, today.month)


async def ap_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Every tap on the day/time picker, in both of its states.

    One function for both because the verbs cross over — "◀️ Calendar" on the
    time screen goes back to the month — so the state each tap leads to is
    returned rather than implied by which handler caught it. None keeps the
    current state (an inert key).
    """
    query = update.callback_query
    await _answer_cb(query)
    uid = query.from_user.id
    tz, lang = await get_user_prefs(uid)
    off = context.user_data.get("ap_official")
    verb, _, arg = query.data[len(CB_APCAL_PREFIX):].partition(":")

    if verb == "n":
        return None
    if verb == "x" or not off:
        await query.edit_message_text(t("appt_request_cancelled", lang))
        return ConversationHandler.END

    appts = await storage.get_appointments()
    rules = _slot_rules(appts, off["id"], uid)
    outcome, slot = await _picker_step(query, verb, arg, off["name"], tz, lang,
                                       rules, CB_APCAL_PREFIX)
    if outcome != "chosen":
        return AP_TIME if outcome == "times" else AP_DATE
    context.user_data["ap_date"] = slot.strftime("%Y-%m-%d")
    context.user_data["ap_time"] = slot.strftime("%H:%M")
    await edit_markdown(query, t("appt_chosen", lang, official=md(off["name"]),
                                 when=format_dt(slot, tz, lang))
                        + "\n\n" + t("appt_ask_desc", lang))
    return AP_DESC


async def ap_typed_instead(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Text sent while the picker is waiting for a tap. Points at the buttons."""
    uid, _, _ = user_info(update)
    _, lang = await get_user_prefs(uid)
    await update.message.reply_text(t("appt_tap_hint", lang))


async def ap_stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A picker tap the conversation did not take.

    Registered OUTSIDE appointment_conversation(), in bot.py, AFTER it — the
    same arrangement, for the same reason, as worship.stale_button_handler():
    with allow_reentry=True an entry point would be checked before the state
    handlers and swallow every live tap. Reached after a restart (user_data is
    gone) or from an older keyboard once the request has moved on.
    """
    query = update.callback_query
    await _answer_cb(query)
    _, lang = await get_user_prefs(query.from_user.id)
    await query.edit_message_text(t("appt_stale_buttons", lang))
    return ConversationHandler.END


def picker_stale_button_handler() -> CallbackQueryHandler:
    """MUST be registered after appointment_conversation(); see ap_stale_button."""
    return CallbackQueryHandler(ap_stale_button,
                                pattern=f"^{re.escape(CB_APCAL_PREFIX)}")


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
    await reply_markdown(update.message, summary)
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
                await reply_markdown(update.message, t("appt_cooldown", lang, seconds=wait))
                return ConversationHandler.END
        if _count_pending_appts(appts, uid) >= APPOINTMENT_MAX_PENDING:
            await reply_markdown(update.message, t("appt_too_many_pending", lang,
                max=APPOINTMENT_MAX_PENDING))
            return ConversationHandler.END

    # Final guard: per-official frequency limit within ±15 days of now.
    if (_count_active_appts_with_official(appts, uid, off["id"], now_tz())
            >= APPOINTMENT_MAX_PER_WINDOW):
        await reply_markdown(update.message, t("appt_limit_not_submitted", lang,
            official=md(off["name"]),
              max=APPOINTMENT_MAX_PER_WINDOW, days=APPOINTMENT_WINDOW_HALF_DAYS * 2))
        return ConversationHandler.END

    # Final guard: ensure the requested time doesn't overlap another appointment.
    clash = _overlapping_appt(appts, uid, req_dt, DEFAULT_APPT_DURATION_MIN)
    if clash:
        await reply_markdown(update.message, t("appt_overlap_not_submitted", lang,
              official=md(clash["official_name"]), id=clash["id"]))
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

    await reply_markdown(update.message, t("appt_submitted", lang, id=appt_id))

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
            InlineKeyboardButton("✅ Confirm",
                                 callback_data=f"{CB_APPT_PREFIX}confirm:{appt['id']}"),
            InlineKeyboardButton("📅 Suggest time",
                                 callback_data=f"{CB_APPT_PREFIX}counter:{appt['id']}"),
            InlineKeyboardButton("❌ Decline",
                                 callback_data=f"{CB_APPT_PREFIX}decline:{appt['id']}"),
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
                await send_markdown(context.bot, r["chat_id"], caption,  reply_markup=kb)
        except TelegramError as exc:
            logger.warning("Couldn't send appointment request to %s: %s", r["chat_id"], exc)


# ---------------------------------------------------------------------------
# Appointment callback handler (official responses)
# ---------------------------------------------------------------------------

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
            await send_markdown(context.bot, r["chat_id"], msg)
        except TelegramError:
            pass


def _replace_appt(appts: list[dict], appt: dict) -> None:
    """Write *appt* back over its entry in *appts*, in place.

    AT EVERY CALL SITE HERE THIS IS A NO-OP, and saying so is more useful than
    pretending otherwise. `appt` comes from
    `next(a for a in appts if a["id"] == appt_id)`, so it IS the object in the
    list: mutating it is already visible there and the assignment changes
    nothing. Gutting the loop survives the entire dispatch snapshot, because
    there is no behaviour to observe.

    It is kept, and extracted rather than deleted, because it is the contract
    the save path assumes: records are loaded as a list and saved as a list,
    so a caller holding a COPY must put it back. That case is what the direct
    tests in tests/test_appt_callback_dispatch.py pin
    (test_replace_appt_writes_a_copy_back), since no test driving the router
    can — through the router it is a no-op either way.

    As of v0.28.13 this is the ONLY copy of the loop. It was written out eight
    times; the six inline copies in _finalize_appointment,
    handle_counter_propose_message (three branches), rs_newtime and
    _handle_reschedule_response now call this instead. That was a pure move —
    every one of them was a no-op for the same aliasing reason, verified by
    deleting all six and seeing 2303/2303 still pass.
    (On 2026-11-23 the typed prompts became the picker, and those call sites
    moved with them: handle_counter_propose_message's into
    _submit_official_counter and _submit_user_counter, and rs_newtime's into
    _submit_reschedule. Its third branch, a typed 'cancel', went with it.)

    They were consolidated rather than deleted deliberately. **The suite
    cannot tell this loop live from dead**, so if some future caller does hold
    a copy, deleting the write-back would break persistence with nothing
    failing. One tested place to answer that question is worth more than six
    silent assumptions that `next()` returned a live reference.

    _cancel_and_notify (ca_confirm's body until 2026-11-23, when the
    /myappointments Cancel button came to share it) keeps its own loop and is
    deliberately NOT this function: it sets `appts[i]["status"]` and stamps
    the action by index rather than replacing the record, and it breaks on
    the first match.
    """
    for index, existing in enumerate(appts):
        if existing["id"] == appt["id"]:
            appts[index] = appt


async def _decline_appt(context: ContextTypes.DEFAULT_TYPE, query,
                        appt: dict, appts: list[dict], *,
                        official_text: str, requester_text: str) -> None:
    """Mark *appt* declined, persist it, and tell both sides.

    Shared by `decline` (the official refuses the requested time) and
    `decline_user_counter` (the official refuses the time the requester
    suggested). Those two differed only in their wording, and the wording is
    the parameter.
    """
    appt["status"] = "declined"
    _replace_appt(appts, appt)
    await storage.save_appointments(appts)
    await _edit_tapped(query, official_text)
    await send_markdown(context.bot, appt["user_chat_id"], requester_text)


async def _confirm_proposed_time(context: ContextTypes.DEFAULT_TYPE, query,
                                 appt: dict, appts: list[dict],
                                 proposed: str, *, check_past: bool,
                                 clash_text, busy_text, ok_text: str) -> None:
    """Confirm *appt* at *proposed*, unless it is past or clashes.

    Shared by the three branches that settle on a time: `confirm` (the
    requested time), `accept_counter` (the official's suggestion, accepted by
    the requester) and `accept_user_counter` (the requester's suggestion,
    accepted by the official). They differed in which field holds the time,
    the wording, and one guard — so those are the parameters.

    check_past is False for `confirm` alone, and still is — but no longer
    because confirm accepts a past time. Since v0.28.4 `appt_callback` refuses
    a past `confirm` upstream, with `_appt_window_has_ended`, which allows a
    grace period of the appointment's duration; this check has no grace at
    all, so routing confirm through it would refuse an official tapping
    Confirm three minutes into the meeting. The two checks answer different
    questions: this one is "is the time THEY SUGGESTED in the past", the
    upstream one is "has the booked slot finished".

    clash_text is a callable rather than a template because the three
    messages differ in more than the values — one says "your appointment" and
    the others "the requester's appointment". busy_text is the same for the
    OFFICIAL's calendar (_official_overlap); it says less to a requester, who
    is not told another member's appointment id.
    """
    if check_past and _datetime_is_past(proposed):
        await _edit_tapped(query,
            "⚠️ That suggested time has already passed. "
            "Please suggest a new time."
        )
        return
    clash = _confirmed_overlap(appts, appt, proposed)
    if clash:
        await _edit_tapped(query, clash_text(clash))
        return
    busy = _official_overlap(appts, appt, proposed)
    if busy:
        await _edit_tapped(query, busy_text(busy))
        return
    appt["status"] = "confirmed"
    appt["confirmed_datetime"] = proposed
    await _finalize_appointment(context, appt, appts)
    await _edit_tapped(query, ok_text)


async def _edit_tapped(query, text: str) -> None:
    """Replace the tapped message's words with *text* — its caption if it is
    a photo, its text otherwise.

    An appointment request reaches the official side as a PHOTO when the
    requester has a profile picture (_notify_official_of_request), and
    Telegram refuses edit_message_text on a photo: "There is no text in the
    message to edit". Every reply to a tap on that request went through
    edit_message_text, so on a photo the action was saved and then the bot
    raised, leaving the buttons on screen and sending an error alert. Found
    2026-11-23 while building the time picker; fixed at the user's request.

    Plain text either way, as before: the callers' messages are not Markdown.
    """
    message = query.message
    photo = getattr(message, "photo", None)
    if isinstance(photo, (list, tuple)) and photo:
        await query.edit_message_caption(caption=text)
    else:
        await query.edit_message_text(text)


async def _unknown_appt_callback(query, data: str) -> None:
    """Answer a tap that no branch handles, and leave a trace.

    Until 2026-11-03 appt_callback had no else: the if/elif chain simply ran
    off the end. That is invisible rather than harmless — _answer_cb has
    already cleared the user's spinner, so the tap looks accepted. Someone
    tapping "✅ Confirm" on a stale keyboard would reasonably believe the
    appointment was confirmed when nothing had happened, and no log line was
    written either, so there was nothing to find afterwards.
    (board_callback has logged this case all along; appointments did not.)

    The wording must cover both causes, because from here they are
    indistinguishable: a keyboard older than a deploy that changed the verbs,
    or a bug in the bot. So it says what is true — the button no longer works
    — and points at /myappointments, which shows the real current state. It
    does NOT say "try again": the button will never work.
    """
    logger.warning("Unrecognised appointment callback %r", data)
    await _edit_tapped(query,
        "⚠️ That button no longer works.\n\n"
        "It is probably from an older message. Send /myappointments to see "
        "your appointments and their current status."
    )


async def appt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _answer_cb(query)
    data: str = query.data
    # partition, not split(":")[1], which raised IndexError on a verb carrying
    # no id. Nothing builds such a callback today — every button here appends
    # ":{appt_id}" — so this is not a live fault. It is the shape of one:
    # board_callback already has id-less verbs (close, list, completed) and
    # parses with partition for exactly this reason, so the first id-less verb
    # added here would have crashed rather than routed.
    action, _, appt_id = data[len(CB_APPT_PREFIX):].partition(":")
    if not action or not appt_id:
        await _unknown_appt_callback(query, data)
        return

    appts = await storage.get_appointments()
    appt = next((a for a in appts if a["id"] == appt_id), None)
    if not appt:
        await _edit_tapped(query, "⚠️ Appointment not found.")
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
        await _edit_tapped(query,
            f"ℹ️ Appointment {appt_id} has already been {appt['status']}. "
            "No further action taken."
        )
        return

    # A slot that has come and gone can no longer be CONFIRMED — doing so
    # would produce a confirmed appointment in the past and send both parties
    # an ICS for a meeting that already finished.
    #
    # The reverse of what this guard used to say. Until v0.28.4 it blocked
    # `counter` and `decline_counter` and let `confirm` through, which was an
    # omission rather than a decision: the verb list dates to a bulk commit
    # (2026-08-18) and nothing recorded a choice. The effect was backwards —
    # the one action that cannot be right was allowed, and both ways out were
    # blocked, so a stale request could only be DECLINED. Worse, the refusal
    # told the official to "suggest a new time", which was the very thing it
    # had just blocked.
    #
    # Unblocking both re-propose verbs is safe because neither writes a time:
    # they open the picker, which offers only future slots and re-checks the
    # one tapped (_picker_step). So the official is forced onto a future time
    # regardless, and the old block bought no safety while removing the only
    # useful action. `decline_counter` comes along because it is not a
    # terminal decline — it opens the SAME picker for the requester.
    if action == "confirm" and _appt_window_has_ended(appt):
        await _edit_tapped(query,
            "⚠️ This appointment's time has already passed, so it can no "
            "longer be confirmed. Suggest a new time instead, or decline it."
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
            await _edit_tapped(query, "⚠️ You're not authorized to act on this appointment.")
            return
        negotiator = appt.get("negotiator_chat_id")
        if negotiator is None:
            name, is_proxy = permissions._acting_identity(off, actor_id, actor_uname)
            appt["negotiator_chat_id"] = actor_id
            appt["negotiator_name"] = name or "The official"
            appt["negotiator_is_proxy"] = is_proxy
            _replace_appt(appts, appt)
            await storage.save_appointments(appts)
            await _notify_negotiation_started(context, off, appt, actor_id)
        elif negotiator != actor_id:
            await _edit_tapped(query,
                f"ℹ️ This appointment is already being handled by "
                f"{md(appt.get('negotiator_name', 'someone'))}."
            )
            return

    if action == "confirm":
        await _confirm_proposed_time(
            context, query, appt, appts, appt["requested_datetime"],
            check_past=False,
            clash_text=lambda clash: (
                f"⚠️ That time overlaps the requester's appointment with "
                f"{md(clash['official_name'])} (ID: {clash['id']}). "
                "Not confirmed — suggest a different time instead."),
            busy_text=lambda busy: (
                f"⚠️ {appt['official_name']} already has a confirmed "
                f"appointment at that time (ID: {busy['id']}). Not confirmed "
                "— suggest a different time instead."),
            ok_text=f"✅ You confirmed the appointment (ID: {appt_id}).")

    elif action == "decline":
        await _decline_appt(
            context, query, appt, appts,
            official_text=f"❌ You declined the appointment (ID: {appt_id}).",
            requester_text=(
                f"❌ Your appointment request (ID: `{appt_id}`) has been "
                "declined." + _requester_proxy_note(appt)))

    elif action == "counter":
        # The official side suggests a different time: a picker, sent as a new
        # message so the request keeps its buttons (see _reply_render).
        await _open_appt_picker(_reply_render(query.message), PICK_OFFICIAL,
                                appt, appts, query.from_user.id)

    elif action == "accept_counter":
        # The requester accepts the time the official suggested.
        await _confirm_proposed_time(
            context, query, appt, appts,
            appt.get("counter_datetime", appt["requested_datetime"]),
            check_past=True,
            clash_text=lambda clash: (
                f"⚠️ That time overlaps your appointment with "
                f"{md(clash['official_name'])} (ID: {clash['id']}). It was "
                "not confirmed — please suggest a different time."),
            busy_text=lambda busy: (
                f"⚠️ {appt['official_name']} is no longer free at that time, "
                "so it was not confirmed — please suggest a different time."),
            ok_text=f"✅ You accepted the suggested time (ID: {appt_id}).")

    elif action == "decline_counter":
        # The requester turns down the suggested time and offers their own.
        await _open_appt_picker(_reply_render(query.message), PICK_REQUESTER,
                                appt, appts, query.from_user.id)

    elif action == "accept_user_counter":
        # The official accepts the time the requester suggested.
        await _confirm_proposed_time(
            context, query, appt, appts,
            appt.get("user_counter_datetime", appt["requested_datetime"]),
            check_past=True,
            clash_text=lambda clash: (
                f"⚠️ That time overlaps the requester's appointment with "
                f"{md(clash['official_name'])} (ID: {clash['id']}). "
                "Not confirmed."),
            busy_text=lambda busy: (
                f"⚠️ {appt['official_name']} already has a confirmed "
                f"appointment at that time (ID: {busy['id']}). Not confirmed "
                "— suggest a different time instead."),
            ok_text=("✅ You confirmed the appointment with the user's "
                     "suggested time."))

    elif action == "decline_user_counter":
        await _decline_appt(
            context, query, appt, appts,
            official_text="❌ Request cancelled.",
            requester_text=(f"Your appointment request (ID: `{appt_id}`) "
                            "has been cancelled."))

    else:
        # Reached only for a verb no branch claims. Note where this sits: the
        # guards above run FIRST, so an unknown verb on an already-confirmed
        # appointment is answered by the terminal-status guard, not here.
        await _unknown_appt_callback(query, data)


async def _finalize_appointment(
    context: ContextTypes.DEFAULT_TYPE, appt: dict, appts: list
) -> None:
    """Save confirmed appointment and send ICS to both the user and the official."""
    _stamp_appt_action(appt)
    appt.pop("reminders_sent", None)  # re-arm reminder DMs for the (new) time
    _replace_appt(appts, appt)
    await storage.save_appointments(appts)

    confirmed_dt = datetime.fromisoformat(appt["confirmed_datetime"])
    if confirmed_dt.tzinfo is None:
        confirmed_dt = TZ.localize(confirmed_dt)

    appt_with_dt = {**appt, "confirmed_datetime": confirmed_dt}

    # --- Notify and send ICS to the user (their timezone + language) ---
    user_tz, user_lang = await get_user_prefs(appt["user_chat_id"])
    ics_bytes = appointment_to_ics(appt_with_dt, TZ)
    user_bio = io.BytesIO(ics_bytes)
    await send_markdown(context.bot, appt["user_chat_id"],
        t("appt_confirmed_user", user_lang, id=appt["id"],
          official=md(appt["official_name"]), when=format_dt(confirmed_dt, user_tz, user_lang)))
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
        await send_markdown(context.bot, off["chat_id"],
            f"✅ *Appointment confirmed (ID: `{appt['id']}`)*\n"
            f"With: {user_display}"
            + (f" (@{md(appt['user_username'])})" if appt.get("user_username") else "") + "\n"
            f"When: {off_dt_str}\n"
            f"Purpose: {md(appt.get('description', ''))}\n\n"
            "An ICS calendar file is attached.")
        await context.bot.send_document(
            off["chat_id"],
            document=InputFile(off_bio, filename="appointment.ics"),
            caption="Import this file into your calendar app.",
        )

    # --- Note to the negotiating proxy (if a proxy arranged this) ---
    if appt.get("negotiator_is_proxy") and appt.get("negotiator_chat_id"):
        await send_markdown(context.bot, appt["negotiator_chat_id"],
            f"✅ Appointment confirmed: *{md(appt['official_name'])}* is scheduled with "
            f"{user_display} on {format_dt(confirmed_dt)} (ID: `{appt['id']}`).")


async def _submit_official_counter(context: ContextTypes.DEFAULT_TYPE,
                                   appt: dict, appts: list, new_dt: datetime) -> None:
    """The official side suggests *new_dt*: record it and ask the requester."""
    appt_id = appt["id"]
    appt["counter_datetime"] = new_dt.isoformat()
    appt["status"] = "counter_proposed"
    _replace_appt(appts, appt)
    await storage.save_appointments(appts)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Accept", callback_data=f"{CB_APPT_PREFIX}accept_counter:{appt_id}"),
            InlineKeyboardButton(
                "📅 Suggest different",
                callback_data=f"{CB_APPT_PREFIX}decline_counter:{appt_id}"),
        ]
    ])
    await send_markdown(context.bot, appt["user_chat_id"],
        f"📅 *New time suggested for appointment `{appt_id}`*\n"
        f"With: {md(appt['official_name'])}\n"
        f"Suggested: {format_dt(new_dt)}\n"
        f"Purpose: {md(appt['description'])}"
        + _requester_proxy_note(appt),
        reply_markup=kb)


async def _submit_user_counter(context: ContextTypes.DEFAULT_TYPE,
                               appt: dict, appts: list, new_dt: datetime) -> None:
    """The requester suggests *new_dt*: record it and ask the negotiator."""
    appt_id = appt["id"]
    appt["user_counter_datetime"] = new_dt.isoformat()
    _replace_appt(appts, appt)
    await storage.save_appointments(appts)
    off = permissions._official_by_id(appt["official_id"])
    target_chat = appt.get("negotiator_chat_id") or (off.get("chat_id") if off else None)
    if not target_chat:
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Accept",
                callback_data=f"{CB_APPT_PREFIX}accept_user_counter:{appt_id}"),
            InlineKeyboardButton(
                "❌ Decline",
                callback_data=f"{CB_APPT_PREFIX}decline_user_counter:{appt_id}"),
        ]
    ])
    await send_markdown(context.bot, target_chat,
        f"The user has suggested a new time for appointment `{appt_id}`:\n"
        f"{format_dt(new_dt)}",
        reply_markup=kb)


# ---------------------------------------------------------------------------
# The picker for an EXISTING appointment: suggest a time, or reschedule
# ---------------------------------------------------------------------------
#
# Replaced three typed "YYYY-MM-DD HH:MM" prompts (2026-11-23): the official's
# "Suggest time", the requester's "Suggest different", and /reschedule.
#
# STATELESS, unlike /appointment's picker. Every button carries the mode and
# the appointment id — appick:<mode>:<id>:<verb>[:<arg>] — so a picker keeps
# working across a restart, and nothing is held in memory between taps. The
# typed prompts kept a module-level dict of who was mid-reply, which a deploy
# emptied silently; the reply then fell through to nothing.
#
# Everything is re-checked on every tap from the stored record: who may act,
# whether the appointment can still change, and whether the slot is free.

CB_APPICK_PREFIX = "appick:"
PICK_OFFICIAL = "o"     # the official side suggests a time (from "counter")
PICK_REQUESTER = "u"    # the requester suggests one (from "decline_counter")
PICK_RESCHEDULE = "r"   # /reschedule, by either party


def _pick_prefix(mode: str, appt_id: str) -> str:
    return f"{CB_APPICK_PREFIX}{mode}:{appt_id}:"


def _picker_counterparty(appt: dict, actor_id: int) -> str:
    """Who the meeting is WITH, seen by the person picking."""
    if actor_id == appt.get("user_chat_id"):
        return appt.get("official_name") or "the official"
    return appt.get("user_display_name") or appt.get("user_username") or "the requester"


def _picker_refusal(mode: str, appt: dict, actor_id: int, actor_uname: str | None,
                    lang: str) -> str | None:
    """Why this person may not pick a time for *appt* now, or None if they may."""
    status = appt.get("status")
    if mode == PICK_RESCHEDULE:
        if status not in ACTIVE_APPT_STATUSES or _appt_is_past(appt):
            return t("resched_no_longer", lang)
        if (actor_id != appt.get("user_chat_id")
                and not permissions._user_can_act_for_appt(appt, actor_id, actor_uname)):
            return "⚠️ You're not authorized to act on this appointment."
        return None
    if status in TERMINAL_APPT_STATUSES:
        return (f"ℹ️ Appointment {appt['id']} has already been {status}. "
                "No further action taken.")
    if mode == PICK_OFFICIAL:
        off = permissions._official_by_id(appt.get("official_id"))
        if not permissions._user_can_act_for_official(off, actor_id, actor_uname):
            return "⚠️ You're not authorized to act on this appointment."
        negotiator = appt.get("negotiator_chat_id")
        if negotiator is not None and negotiator != actor_id:
            return (f"ℹ️ This appointment is already being handled by "
                    f"{appt.get('negotiator_name', 'someone')}.")
        return None
    if mode == PICK_REQUESTER:
        if actor_id != appt.get("user_chat_id"):
            return "⚠️ You're not authorized to act on this appointment."
        return None
    return "⚠️ That button no longer works."


async def _open_appt_picker(render, mode: str, appt: dict, appts: list,
                            actor_id: int) -> None:
    """Show the month calendar for re-timing *appt*.

    The busy times are the official's and the requester's, LESS this
    appointment itself, so its current slot stays on offer.
    """
    tz, lang = await get_user_prefs(actor_id)
    rules = _slot_rules(appts, appt["official_id"], appt["user_chat_id"],
                        exclude_id=appt["id"])
    name = _picker_counterparty(appt, actor_id)
    month = _first_bookable_month(tz, rules)
    if month is None:
        await render(t("appt_no_days", lang, official=md(name),
                       months=APPOINTMENT_HORIZON_MONTHS), None)
        return
    await _show_calendar(render, name, month, tz, lang, rules,
                         prefix=_pick_prefix(mode, appt["id"]))


async def appt_picker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every tap on an existing appointment's picker. See the section comment."""
    query = update.callback_query
    await _answer_cb(query)
    actor_id = query.from_user.id
    actor_uname = query.from_user.username
    tz, lang = await get_user_prefs(actor_id)

    parts = query.data[len(CB_APPICK_PREFIX):].split(":", 3)
    if len(parts) < 3:
        await _unknown_appt_callback(query, query.data)
        return
    mode, appt_id, verb = parts[:3]
    arg = parts[3] if len(parts) > 3 else ""

    if verb == "n":
        return
    if verb == "x":
        await query.edit_message_text(t("appt_picker_closed", lang))
        return

    appts = await storage.get_appointments()
    appt = next((a for a in appts if a["id"] == appt_id), None)
    if not appt:
        await query.edit_message_text("⚠️ Appointment not found.")
        return
    refusal = _picker_refusal(mode, appt, actor_id, actor_uname, lang)
    if refusal:
        await query.edit_message_text(refusal)
        return

    if verb == "open":
        # From /myappointments: a new message, so the list stays on screen.
        await _open_appt_picker(_reply_render(query.message), mode, appt, appts,
                                actor_id)
        return

    rules = _slot_rules(appts, appt["official_id"], appt["user_chat_id"],
                        exclude_id=appt["id"])
    outcome, slot = await _picker_step(
        query, verb, arg, _picker_counterparty(appt, actor_id), tz, lang, rules,
        _pick_prefix(mode, appt_id))
    if outcome != "chosen":
        return

    when = format_dt(slot, tz, lang)
    if mode == PICK_OFFICIAL:
        await _submit_official_counter(context, appt, appts, slot)
        done = f"✅ Suggested time sent to the user: {when}"
    elif mode == PICK_REQUESTER:
        await _submit_user_counter(context, appt, appts, slot)
        done = f"✅ Your suggested time has been forwarded: {when}"
    else:
        role = "user" if actor_id == appt.get("user_chat_id") else "official"
        await _submit_reschedule(context, appt, appts, slot, role, actor_id, actor_uname)
        done = t("resched_sent", lang)
    # Logged under the commands members know, not a new name: /stats counts
    # COMMAND entries by name, and "appointment_time" would show up there as
    # a command nobody can type.
    activity.log_command("reschedule" if mode == PICK_RESCHEDULE else "appointment",
                         actor_id, actor_uname, None,
                         details=f"time picked ({mode}) for {appt_id}")
    await edit_markdown(query, done)


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

    # Upcoming appointments the viewer can still change are numbered, and get
    # a Reschedule and a Cancel button each (2026-11-23), so nobody has to
    # know /reschedule or /cancelappointment exist. Those commands still work.
    rows = []
    rendered_upcoming = []
    for a, is_off in upcoming:
        line = _render(a, is_off)
        if len(rows) < MYAPPTS_MAX_BUTTONS and _can_change_appt(a, uid, uname):
            n = len(rows) + 1
            # "• With: …" → "*1.* With: …", so the number matches the buttons.
            line = f"*{n}.*{line[1:]}" if line.startswith("•") else f"*{n}.* {line}"
            rows.append([
                InlineKeyboardButton(t("myappts_btn_reschedule", lang, n=n),
                                     callback_data=f"{_pick_prefix(PICK_RESCHEDULE, a['id'])}open"),
                InlineKeyboardButton(t("myappts_btn_cancel", lang, n=n),
                                     callback_data=f"{CB_MYAPPT_PREFIX}c:{a['id']}"),
            ])
        rendered_upcoming.append(line)

    lines = [t("myappts_header", lang)]
    if upcoming:
        lines.append(t("section_upcoming", lang))
        lines.extend(rendered_upcoming)
    if past:
        lines.append(t("section_past", lang))
        lines.extend(_render(a, is_off) for a, is_off in past)
    if rows:
        lines.append(t("myappts_actions_hint", lang))

    await reply_markdown(update.message, "\n".join(lines),
                         reply_markup=InlineKeyboardMarkup(rows) if rows else None)


# The buttons on /myappointments. Reschedule opens the stateless picker
# (appick:r:<id>:open); Cancel is handled here. Stateless too — the id rides in
# the button, and everything is re-checked from the store on each tap, since
# the list can sit on screen for days.
CB_MYAPPT_PREFIX = "apmy:"
MYAPPTS_MAX_BUTTONS = 8   # rows of buttons; a longer list still shows in full


def _can_change_appt(appt: dict, uid: int, uname: str | None) -> bool:
    """Still upcoming, still active, and this person is a party to it — the
    same test /cancelappointment and /reschedule apply to their lists."""
    if appt.get("status") not in ACTIVE_APPT_STATUSES or _appt_is_past(appt):
        return False
    return (appt.get("user_chat_id") == uid
            or permissions._user_can_act_for_appt(appt, uid, uname))


async def myappt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel from /myappointments: c = ask (a new message, so the list
    stays), y = cancel, k = keep."""
    query = update.callback_query
    await _answer_cb(query)
    uid, uname, dname = user_info(update)
    tz, lang = await get_user_prefs(uid)
    verb, _, appt_id = query.data[len(CB_MYAPPT_PREFIX):].partition(":")

    if verb == "k":
        await query.edit_message_text(t("cancel_aborted", lang))
        return
    appts = await storage.get_appointments()
    appt = next((a for a in appts if a["id"] == appt_id), None)
    if verb not in ("c", "y") or not appt:
        await _unknown_appt_callback(query, query.data)
        return
    if not _can_change_appt(appt, uid, uname):
        await query.edit_message_text(t("myappts_no_longer", lang)) if verb == "y" \
            else await query.message.reply_text(t("myappts_no_longer", lang))
        return

    if verb == "c":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ " + t("myappts_yes_cancel", lang),
                                 callback_data=f"{CB_MYAPPT_PREFIX}y:{appt_id}"),
            InlineKeyboardButton("✖️ " + t("myappts_no_keep", lang),
                                 callback_data=f"{CB_MYAPPT_PREFIX}k:{appt_id}"),
        ]])
        await reply_markdown(query.message, t(
            "cancel_confirm_prompt", lang, official=md(appt["official_name"]),
            when=_appt_dt_label(appt, tz, lang)), reply_markup=kb)
        return

    await _cancel_and_notify(context, query, appt, uid, uname, dname, lang)


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
    await reply_markdown(update.message, t("cancel_list_header", lang),
        reply_markup=InlineKeyboardMarkup(rows))
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
    await edit_markdown(query, t("cancel_confirm_prompt", lang, official=md(appt["official_name"]),
          when=_appt_dt_label(appt, tz, lang)),
        reply_markup=kb)
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

    await _cancel_and_notify(context, query, appt, uid, uname, dname, lang)
    return ConversationHandler.END


async def _cancel_and_notify(context: ContextTypes.DEFAULT_TYPE, query, appt: dict,
                             uid: int, uname: str | None, dname: str | None,
                             lang: str) -> None:
    """Cancel *appt*, tell the other party, send both a removal ICS, and
    acknowledge by editing the tapped message.

    Shared by /cancelappointment's confirm step and the Cancel button on
    /myappointments. Callers check first that the appointment may still be
    cancelled; this only carries it out.
    """
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
            await send_markdown(context.bot, user_chat_id,
                t("cancel_done_by_official_to_user", req_lang,
                  id=appt["id"], official=md(appt["official_name"])))
            await _send_cancellation_ics(context, user_chat_id, appt)
        await edit_markdown(query, t("cancel_done_official_ack", lang, id=appt["id"]))
        # Also remove it from the official's own calendar
        if off and off.get("chat_id"):
            await _send_cancellation_ics(context, off["chat_id"], appt)
    else:
        # Requester cancelled → notify the official if we know their chat_id
        if off and off.get("chat_id"):
            user_display = md(appt.get("user_display_name")
                              or appt.get("user_username") or "The requester")
            await send_markdown(context.bot, off["chat_id"],
                f"❌ Appointment (ID: `{appt['id']}`) with "
                + (f"*{user_display}*" if user_display else "a congregant")
                + (f" (@{md(appt['user_username'])})" if appt.get("user_username") else "")
                + " has been cancelled by the requester.")
            await _send_cancellation_ics(context, off["chat_id"], appt)
        notified = bool(off and off.get("chat_id"))
        await edit_markdown(query,
            t("cancel_done_requester_ack_notified" if notified else "cancel_done_requester_ack",
              lang, id=appt["id"]))
        # Also remove it from the requester's own calendar
        if user_chat_id:
            await _send_cancellation_ics(context, user_chat_id, appt)


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

RS_SELECT = 0

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
    await reply_markdown(update.message, t("resched_list_header", lang),
        reply_markup=InlineKeyboardMarkup(rows))
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

    # The picker takes over, and it is stateless (appt_picker_callback), so
    # the conversation ends here. The record is re-read: the list was built
    # when /reschedule was sent, and may be stale by the time of this tap.
    appts = await storage.get_appointments()
    appt = next((a for a in appts if a["id"] == reschedulable[int(idx)]["id"]), None)
    refusal = (_picker_refusal(PICK_RESCHEDULE, appt, uid, query.from_user.username, lang)
               if appt else t("resched_no_longer", lang))
    if refusal:
        await query.edit_message_text(refusal)
        return ConversationHandler.END
    await _open_appt_picker(_edit_render(query), PICK_RESCHEDULE, appt, appts, uid)
    return ConversationHandler.END


async def _submit_reschedule(context: ContextTypes.DEFAULT_TYPE, appt: dict,
                             appts: list, new_dt: datetime, role: str,
                             actor_id: int, actor_uname: str | None) -> None:
    """Record a proposed new time WITHOUT touching the confirmed one, and ask
    the other party. The original stands until they accept."""
    appt_id = appt["id"]
    appt["reschedule_proposed_datetime"] = new_dt.isoformat()
    appt["reschedule_proposed_by"] = role
    _stamp_appt_action(appt)
    off = permissions._official_by_id(appt["official_id"])
    if role == "official" and off:
        name, is_proxy = permissions._acting_identity(off, actor_id, actor_uname)
        appt["negotiator_chat_id"] = actor_id
        appt["negotiator_name"] = name or "The official"
        appt["negotiator_is_proxy"] = is_proxy
    _replace_appt(appts, appt)
    await storage.save_appointments(appts)

    new_dt_str = format_dt(new_dt)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"{CB_APPT_PREFIX}rs_accept:{appt_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"{CB_APPT_PREFIX}rs_decline:{appt_id}"),
    ]])

    if role == "official":
        # Notify the requester.
        await send_markdown(context.bot, appt["user_chat_id"],
            f"📅 *Reschedule requested for appointment `{appt_id}`*\n"
            f"With: {md(appt['official_name'])}\n"
            f"New time: {new_dt_str}\n"
            f"Purpose: {md(appt.get('description', ''))}"
            + _requester_proxy_note(appt),
            reply_markup=kb)
    else:
        # Requester is rescheduling → notify the official side (officer + proxies).
        requester = md(appt.get("user_display_name")
                       or appt.get("user_username") or "The requester")
        for r in permissions._official_side_recipients(off) if off else []:
            try:
                await send_markdown(context.bot, r["chat_id"],
                    f"📅 *Reschedule requested for appointment `{appt_id}`*\n"
                    f"From: {requester}\n"
                    f"New time: {new_dt_str}\n"
                    f"Purpose: {md(appt.get('description', ''))}",
                    reply_markup=kb)
            except TelegramError:
                pass


async def _handle_reschedule_response(
    context: ContextTypes.DEFAULT_TYPE, query, appt: dict, appts: list, action: str
) -> None:
    """Handle the other party accepting/declining a proposed reschedule."""
    appt_id = appt["id"]
    proposed = appt.get("reschedule_proposed_datetime")
    if not proposed:
        await _edit_tapped(query, "ℹ️ This reschedule request has already been handled.")
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
        await _edit_tapped(query, "⚠️ You're not authorized to respond to this reschedule.")
        return

    def _clear_and_store():
        appt.pop("reschedule_proposed_datetime", None)
        appt.pop("reschedule_proposed_by", None)
        _replace_appt(appts, appt)

    proposer_chat = (appt["user_chat_id"] if proposed_by == "user"
                     else appt.get("negotiator_chat_id")
                     or (permissions._official_by_id(appt["official_id"]) or {}).get("chat_id"))

    if action == "rs_decline":
        _clear_and_store()
        await storage.save_appointments(appts)
        await _edit_tapped(query, "❌ Reschedule declined; the original time stands.")
        if proposer_chat:
            await send_markdown(context.bot, proposer_chat,
                f"❌ Your reschedule request for appointment `{appt_id}` was declined; "
                "the original time stands.")
        return

    # rs_accept
    if _datetime_is_past(proposed):
        await _edit_tapped(query,
            "⚠️ That proposed time has already passed; no change was made."
        )
        _clear_and_store()
        await storage.save_appointments(appts)
        return
    clash = _confirmed_overlap(appts, appt, proposed)
    if clash:
        await _edit_tapped(query,
            f"⚠️ That time now overlaps another appointment (ID: {clash['id']}); "
            "not rescheduled."
        )
        return
    if _official_overlap(appts, appt, proposed):
        # No id: the one tapping may be the requester, and the clash is
        # another member's appointment.
        await _edit_tapped(query,
            f"⚠️ {appt.get('official_name')} already has a confirmed appointment "
            "at that time; not rescheduled."
        )
        return
    appt["confirmed_datetime"] = proposed
    appt["status"] = "confirmed"
    _clear_and_store()
    await _finalize_appointment(context, appt, appts)  # new ICS to both, proxy note if any
    await _edit_tapped(query, "✅ Reschedule accepted; the new time is confirmed.")




# ---------------------------------------------------------------------------
# Conversation assembly
# ---------------------------------------------------------------------------
#
# Built here rather than in bot.main(). See handlers/events_admin.py for why
# these are functions and not module-level constants, and why allow_reentry is
# set on every one.
#
# Unlike the event-admin conversations, these mix step kinds: a tap on an
# inline keyboard (CallbackQueryHandler, gated by the CB_* prefix the keyboard
# was built with) and typed replies (MessageHandler). The prefixes are what
# route a tap to the right conversation, so a wrong one here sends an officer's
# tap into a different flow — tests/test_handler_registration_order.py pins
# each pattern for that reason.


def _typed(callback) -> list:
    """A typed free-text step."""
    return [MessageHandler(filters.TEXT & ~filters.COMMAND, callback)]


def _tapped(callback, prefix: str) -> list:
    """A step driven by an inline-keyboard tap carrying `prefix`."""
    return [CallbackQueryHandler(callback, pattern=f"^{re.escape(prefix)}")]


def appointment_conversation() -> ConversationHandler:
    """/appointment — pick an official, a day, a time, the purpose, confirm.

    The day and time are taps on one picker (ap_pick); typing there gets a
    pointer to the buttons. Stale picker taps are answered by
    picker_stale_button_handler(), registered after this in bot.py.
    """
    return ConversationHandler(
        entry_points=[CommandHandler("appointment", cmd_appointment)],
        states={
            AP_OFFICIAL: _tapped(ap_official, CB_APSEL_PREFIX),
            AP_DATE: _tapped(ap_pick, CB_APCAL_PREFIX) + _typed(ap_typed_instead),
            AP_TIME: _tapped(ap_pick, CB_APCAL_PREFIX) + _typed(ap_typed_instead),
            AP_DESC: _typed(ap_desc),
            AP_CONFIRM: _typed(ap_confirm),
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=True,
    )


def cancel_appointment_conversation() -> ConversationHandler:
    """/cancelappointment — pick one, then confirm. Both steps are taps.

    Both states use the SAME prefix, so the conversation's state is what
    distinguishes "which appointment" from "yes, really".
    """
    return ConversationHandler(
        entry_points=[CommandHandler("cancelappointment", cmd_cancelappointment)],
        states={
            CA_SELECT: _tapped(ca_select, CB_CANCEL_PREFIX),
            CA_CONFIRM: _tapped(ca_confirm, CB_CANCEL_PREFIX),
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=True,
    )


def reschedule_conversation() -> ConversationHandler:
    """/reschedule — tap the appointment; the stateless picker does the rest."""
    return ConversationHandler(
        entry_points=[CommandHandler("reschedule", cmd_reschedule)],
        states={
            RS_SELECT: _tapped(rs_select, CB_RESCHED_PREFIX),
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=True,
    )

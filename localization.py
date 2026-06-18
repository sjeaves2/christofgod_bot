"""Lightweight localization (i18n) for user-facing strings.

Adding a language later means adding a new entry to CATALOG (and listing it in
AVAILABLE_LANGUAGES) — no code changes elsewhere. Slash-command names inside a
string (e.g. "/events") are intentionally left untranslated.

Usage:
    from localization import t
    t("events_none", lang)                      # simple lookup
    t("appt_confirmed_user", lang, id="ABC", when="...")  # with placeholders
"""

from __future__ import annotations

DEFAULT_LANG = "en"

# Languages offered to users via /language (code -> display name).
AVAILABLE_LANGUAGES: dict[str, str] = {
    "en": "English",
}

CATALOG: dict[str, dict[str, str]] = {
    "en": {
        # -- welcome / help --
        "welcome": (
            "👋 Welcome to *{bot_name}*!\n\n"
            "I send reminders for God's Holy Convocations, special services, "
            "and events.\n\n{commands}"
        ),
        "user_commands": (
            "*Available commands:*\n"
            "/help — show this message\n"
            "/events — upcoming events (next 30 days)\n"
            "/exportcalendar — download an ICS calendar file\n"
            "/appointment — request a meeting with a church official\n"
            "/myappointments — list your appointments\n"
            "/cancelappointment — cancel a pending or confirmed appointment\n"
            "/settimezone — set your time zone for displayed times\n"
            "/language — choose your language\n"
            "/stop — unsubscribe from notifications"
        ),
        "share_contact_prompt": (
            "To personalise your experience, please share your contact "
            "(tap the button below). You can tap Skip if you prefer not to."
        ),
        "share_contact_button": "📱 Share my contact",
        "unsubscribed": "You have been unsubscribed. Send /start to re-subscribe.",

        # -- events --
        "events_header": "*Upcoming Events (next 30 days):*\n",
        "events_none": "No events in the next 30 days.",

        # -- my appointments --
        "myappts_header": "*Your Appointments:*",
        "myappts_none": "You have no appointments on record.",
        "section_upcoming": "\n*Upcoming:*",
        "section_past": "\n*Past:*",
        "appt_line": "• With: {counterparty}\n   {when} — *{status}*\n   _ID: {id}_",

        # -- cancel appointment --
        "cancel_none": "You have no active appointments to cancel.",
        "cancel_list_header": "*Your Active Appointments:*\n",
        "cancel_list_line": "{n}. [{id}] {official}\n   {when} — *{status}*",
        "cancel_select_prompt": "\nEnter the number to cancel (or /cancel to abort):",
        "cancel_pick_number": "Please enter a number between 1 and {max}:",
        "cancel_confirm_prompt": (
            "Cancel appointment with *{official}* on {when}?\n\n"
            "Type *yes* to confirm cancellation, or anything else to abort."
        ),
        "cancel_aborted": "Cancellation aborted.",
        "cancel_done_by_official_to_user": (
            "❌ Your appointment (ID: `{id}`) with *{official}* "
            "has been cancelled by the official."
        ),
        "cancel_done_official_ack": (
            "✅ Appointment `{id}` cancelled. The requester has been notified."
        ),
        "cancel_done_requester_ack": "✅ Appointment `{id}` cancelled.",
        "cancel_done_requester_ack_notified": (
            "✅ Appointment `{id}` cancelled. The official has been notified."
        ),

        # -- appointment request flow --
        "appt_choose_official": "*Request an Appointment*\n\nWho would you like to meet with?\n",
        "appt_enter_number": "\nEnter the number:",
        "appt_invalid_number": "Please enter a valid number:",
        "appt_already_with_official": (
            "You already have an appointment with {official} "
            "(ID: `{id}`, {status}).\n\n"
            "Please cancel it with /cancelappointment before requesting another, "
            "or use /myappointments to review it."
        ),
        "appt_ask_date": "Desired date (YYYY-MM-DD):",
        "appt_bad_date": "Please use YYYY-MM-DD format:",
        "appt_ask_time": "Desired time (HH:MM, 24-hour):",
        "appt_bad_time": "Please use HH:MM format:",
        "appt_bad_datetime": "That date/time isn't valid. Please re-enter the date (YYYY-MM-DD):",
        "appt_past": "That date/time is in the past. Please enter a future date (YYYY-MM-DD):",
        "appt_too_far": (
            "Appointments can be booked at most {months} months ahead "
            "(through {until}). Please enter an earlier date (YYYY-MM-DD):"
        ),
        "appt_overlap": (
            "That time overlaps your existing appointment with {official} "
            "on {when} (ID: `{id}`).\n\nPlease choose a different date/time (YYYY-MM-DD):"
        ),
        "appt_ask_desc": "Brief description of the meeting purpose (128 characters max):",
        "appt_summary": (
            "*Appointment Request Summary:*\n"
            "With: {official}\n"
            "When: {when}\n"
            "Description: {desc}\n\n"
            "Submit? (yes/no)"
        ),
        "appt_request_cancelled": "Request cancelled.",
        "appt_overlap_not_submitted": (
            "That time overlaps your appointment with {official} "
            "(ID: `{id}`). Request not submitted."
        ),
        "appt_already_not_submitted": (
            "You already have an appointment with {official} "
            "(ID: `{id}`). Request not submitted."
        ),
        "appt_submitted": (
            "✅ *Request submitted!* (ID: `{id}`)\n"
            "I will notify you when your request is accepted, declined, "
            "or a new time is suggested."
        ),
        "appt_confirmed_user": (
            "✅ *Your appointment (ID: `{id}`) has been confirmed!*\n"
            "With: {official}\n"
            "When: {when}\n\n"
            "An ICS calendar file is attached."
        ),
        "appt_ics_caption": "Import this file into your calendar app.",

        # -- notifications --
        "notif_reminder_title": "🔔 *Reminder: {name}*",
        "notif_service_begins": "Service begins: {when}",
        "notif_join": "🔗 Join: {url}",
        "notif_announcements_header": "⚠️ *Announcements:*",

        # -- /settimezone --
        "tz_prompt": (
            "*Set Your Time Zone*\n\n"
            "Choose a number, or type any IANA zone name "
            "(e.g. `America/New_York`):\n"
        ),
        "tz_invalid": "That isn't a recognised time zone. Please try again (or /cancel):",
        "tz_set": "✅ Your time zone is set to *{tz}*.\nCurrent local time: {now}",

        # -- /language --
        "lang_prompt": "*Choose Your Language*\n",
        "lang_pick_number": "Please enter a number between 1 and {max}:",
        "lang_set": "✅ Language set to *{language}*.",
    },
}


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """Translate ``key`` into ``lang`` (falling back to English), then format."""
    if lang not in CATALOG:
        lang = DEFAULT_LANG
    text = CATALOG.get(lang, {}).get(key)
    if text is None:
        text = CATALOG[DEFAULT_LANG].get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text

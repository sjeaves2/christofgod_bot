"""English (en) strings."""

STRINGS: dict[str, str] = {
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
        "/reschedule — propose a new time for an upcoming appointment\n"
        "/settimezone — set your time zone for displayed times\n"
        "/language — choose your language\n"
        "/notifications — choose which reminders you receive\n"
        "/donate — support the congregation with a gift\n"
        "/stop — unsubscribe from notifications"
    ),
    "share_contact_prompt": (
        "To personalise your experience, please share your contact "
        "(tap the button below). You can tap Skip if you prefer not to."
    ),
    "share_contact_button": "📱 Share my contact",
    "unsubscribed": "You have been unsubscribed. Send /start to re-subscribe.",
    "bcast_individual_subscribers": "Individual subscribers",

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
    "cancel_list_header": "*Your Active Appointments:*\nChoose one to cancel:",
    "cancel_confirm_prompt": (
        "Cancel appointment with *{official}* on {when}?\n\n"
        "Tap ✅ Yes to confirm the cancellation, or ✖️ No to keep it."
    ),
    "cancel_aborted": "Cancellation aborted.",
    "cancel_past": "That appointment has already taken place and can't be cancelled.",

    # -- reschedule appointment --
    "resched_none": "You have no upcoming appointments to reschedule.",
    "resched_list_header": "*Reschedule an Appointment*\nChoose one to reschedule:",
    "resched_ask_time": "Enter the new date and time (YYYY-MM-DD HH:MM):",
    "resched_bad_format": "Please use the format YYYY-MM-DD HH:MM:",
    "resched_past": "That date/time is in the past. Please enter a future time (YYYY-MM-DD HH:MM):",
    "resched_overlap": "That time overlaps another of your appointments. Please choose a different time (YYYY-MM-DD HH:MM):",
    "resched_no_longer": "That appointment can no longer be rescheduled.",
    "resched_sent": "✅ Your reschedule request has been sent. You'll be notified when it's accepted or declined.",
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
    "appt_choose_official": "*Request an Appointment*\n\nWho would you like to meet with?",
    "appt_invalid_number": "Invalid selection.",
    "appt_limit_reached": (
        "You've reached the limit of {max} appointments with {official} "
        "in any {days}-day period.\n\n"
        "Please choose a date outside that window, or cancel an existing "
        "appointment with /cancelappointment."
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
    "appt_limit_not_submitted": (
        "You've reached the limit of {max} appointments with {official} "
        "in any {days}-day period. Request not submitted."
    ),
    "appt_cooldown": (
        "⏳ You've just made an appointment change. Please wait about "
        "{seconds} more second(s) before submitting another request."
    ),
    "appt_too_many_pending": (
        "📋 You already have {max} pending request(s) awaiting a response. "
        "Please wait for one to be confirmed or declined before submitting another."
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
        "Tap a zone below, or type any IANA zone name "
        "(e.g. `America/New_York`):"
    ),
    "tz_invalid": "That isn't a recognised time zone. Please try again (or /cancel):",
    "tz_set": "✅ Your time zone is set to *{tz}*.\nCurrent local time: {now}",

    # -- /language --
    "lang_prompt": "*Choose Your Language*",
    "lang_set": "✅ Language set to *{language}*.",

    # -- /notifications (opt-in personal reminders) --
    "notif_cat_convocations": "Sabbath and other Holy Convocations",
    "notif_cat_sunday_prayer": "Sunday Morning Prayer",
    "notif_cat_special": "Special events",
    "notif_prefs_prompt": (
        "*Personal Reminders*\n\n"
        "Choose which reminders you'd like sent to you directly. "
        "This is handy if you're not in a church group chat. "
        "Tap to turn each on/off, then tap Done."
    ),
    "notif_prefs_done": "✔️ Done",
    "notif_prefs_none": (
        "You've turned off all personal reminders. "
        "You can turn them back on any time with /notifications."
    ),
    "notif_prefs_saved": "✅ You'll receive personal reminders for:\n{list}",

    # -- /help topics --
    "help_topic_hint": "For details on a command, send `/help <command>` (e.g. `/help appointment`).",
    "help_unknown_topic": "I don't have help for that. Try one of: {topics}",
    "help_appointment": (
        "*/appointment* — Request a meeting with a church official.\n\n"
        "Pick the official, then enter a date and time. They (or their proxy) "
        "will confirm, decline, or suggest a different time. Once confirmed you "
        "get a calendar file."
    ),
    "help_myappointments": (
        "*/myappointments* — List your upcoming and past appointments and their status."
    ),
    "help_cancelappointment": (
        "*/cancelappointment* — Cancel an upcoming appointment. Pick it from the "
        "list and confirm. The other party is notified. Past appointments can't be cancelled."
    ),
    "help_reschedule": (
        "*/reschedule* — Propose a new time for an upcoming appointment. Pick it, "
        "enter the new date/time, and the other party accepts or declines. If declined, "
        "the original time is kept. New times can't be in the past."
    ),
    "help_events": (
        "*/events* — Show upcoming convocations, services and events for the next 30 days, "
        "with any join links."
    ),
    "help_exportcalendar": (
        "*/exportcalendar* — Download an ICS calendar file of upcoming events to import "
        "into your calendar app."
    ),
    "help_settimezone": (
        "*/settimezone* — Set your time zone so dates and times are shown in your local time. "
        "Tap a common zone or type any IANA name (e.g. `America/New_York`)."
    ),
    "help_language": "*/language* — Choose the language the bot uses when talking to you.",
    "help_notifications": (
        "*/notifications* — Choose which reminders you receive as personal messages "
        "(Sabbath and Holy Convocations, Sunday Morning Prayer, Special events). Useful "
        "if you're not in a church group chat."
    ),
    "help_donate": (
        "*/donate* — Support the congregation with a gift. Opens a secure giving "
        "page where you can contribute."
    ),

    # -- /donate --
    "donate_message": (
        "🙏 *Support Christ of God Ministries*\n\n"
        "Your generosity helps sustain our ministry and outreach. "
        "If you'd like to contribute, tap the button below — "
        "thank you, and may The LORD bless you!"
    ),
    "donate_button": "💝 Donate now",
    "donate_not_configured": (
        "Online giving isn't set up yet. Please speak with Elder L. "
        "Williams about ways to contribute."
    ),

    # -- appointment reminder DMs --
    "appt_reminder_user": (
        "⏰ *Appointment reminder*\n\n"
        "Your appointment with *{counterparty}* is {when}.\n_ID: {id}_"
    ),
    "appt_reminder_official": (
        "⏰ *Appointment reminder*\n\n"
        "Your appointment with *{counterparty}* is {when}.\n_ID: {id}_"
    ),

    # -- appointment statuses --
    "status_pending": "pending",
    "status_confirmed": "confirmed",
    "status_counter_proposed": "time proposed",
    "status_cancelled": "cancelled",
    "status_declined": "declined",
}

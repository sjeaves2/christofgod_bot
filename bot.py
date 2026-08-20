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

import logging
import re
from datetime import timedelta

from telegram import (
    BotCommand,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
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


# ---------------------------------------------------------------------------
# Application assembly
#
# bot.py is the composition root: it owns the few cross-cutting handlers below
# (contact sharing, command logging, maintenance jobs, the error handler) and
# wires every feature module's handlers into the Application in main().
#
# The imports that follow are the handlers, conversation states and callback
# prefixes main() registers. F401 is silenced on them because ruff cannot see
# that they are used inside main()'s handler construction.
# ---------------------------------------------------------------------------

from settings import (  # noqa: F401
    BOT_DISPLAY_NAME,
    BOT_TOKEN,
    activity,
)
import permissions
import storage

from permissions import (  # noqa: F401
    user_info,
)
from handlers.broadcast import (  # noqa: F401
    BC_MESSAGE,
    BC_RETRY,
    BC_SELECT,
    CB_BC_PREFIX,
    bc_media,
    bc_message,
    bc_retry,
    bc_select,
    cmd_broadcast,
)
from handlers.announcements import (  # noqa: F401
    AN_BODY,
    AN_CONFIRM,
    AN_EXPIRES,
    AN_TITLE,
    DA_SELECT,
    an_body,
    an_confirm,
    an_expires,
    an_title,
    cmd_addannouncement,
    cmd_announcements,
    cmd_delannouncement,
    cmd_listannouncements,
    da_select,
    purge_old_announcements,
)
from handlers.events_admin import (  # noqa: F401
    AE_CONFIRM,
    AE_DATE,
    AE_DESC,
    AE_DURATION,
    AE_NAME,
    AE_NOTIF,
    AE_TIME,
    AE_URL,
    DE_ANNOT,
    DE_CONFIRM,
    DE_SELECT,
    ME_FIELD,
    ME_SELECT,
    ME_VALUE,
    SL_SELECT,
    SL_URL,
    ae_confirm,
    ae_date,
    ae_desc,
    ae_duration,
    ae_name,
    ae_notif,
    ae_time,
    ae_url,
    cmd_addevent,
    cmd_deleteevent,
    cmd_modifyevent,
    cmd_setservicelink,
    de_annot,
    de_confirm,
    de_select,
    me_field,
    me_select,
    me_value,
    sl_select,
    sl_url,
)
from handlers.appointments import (  # noqa: F401
    CB_APSEL_PREFIX,
    CB_CANCEL_PREFIX,
    CB_RESCHED_PREFIX,
    AP_CONFIRM,
    AP_DATE,
    AP_DESC,
    AP_OFFICIAL,
    AP_TIME,
    CA_CONFIRM,
    CA_SELECT,
    CB_APPT_PREFIX,
    RS_NEWTIME,
    RS_SELECT,
    ap_confirm,
    ap_date,
    ap_desc,
    ap_official,
    ap_time,
    appointment_reminder_job,
    appt_callback,
    archive_old_appointments,
    ca_confirm,
    ca_select,
    cmd_appointment,
    cmd_cancelappointment,
    cmd_enable_appt_proxies,
    cmd_myappointments,
    cmd_reschedule,
    handle_counter_propose_message,
    purge_archived_appointments,
    rs_newtime,
    rs_select,
)
from handlers.user_basics import (  # noqa: F401
    CB_LANG_PREFIX,
    CB_NOTIFPREF_PREFIX,
    CB_TZ_PREFIX,
    LANG_SELECT,
    TZ_SELECT,
    _commands_text,
    _register_official_if_known,
    cmd_adminhelp,
    cmd_donate,
    cmd_events,
    cmd_export_calendar,
    cmd_help,
    cmd_language,
    cmd_listevents,
    cmd_notifications,
    cmd_settimezone,
    cmd_start,
    cmd_stop,
    cmd_usercount,
    cmd_userlist,
    lang_select,
    notif_prefs_callback,
    tz_button,
    tz_typed,
)
from common import (  # noqa: F401
    md,
    get_user_prefs,
)
from events import _merge_special_events, _resolve_targets, all_upcoming  # noqa: F401
from handlers.notifications import (  # noqa: F401
    notification_catchup_job,
    schedule_all_upcoming,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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

    await permissions._register_admin_by_phone(uid, phone)
    await _register_official_if_known(uid, uname, phone)

    # Store phone in user record
    users = await storage.get_all_users()
    for u in users:
        if u["chat_id"] == uid and not u.get("phone"):
            u["phone"] = re.sub(r"\D", "", phone)
            await storage.save_users(users)
            break

    _, lang = await get_user_prefs(uid)
    is_adm = permissions.is_admin(update)
    if is_adm:
        reply = "✅ Contact received. You have been recognised as an administrator."
    elif permissions._is_known_official(uid, uname):
        reply = "✅ Contact received. You have been recognised as an official."
    else:
        reply = "✅ Contact received. Thank you!"
    cmd_text = _commands_text(lang, is_adm)

    await update.message.reply_text(reply, reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(cmd_text, parse_mode=ParseMode.MARKDOWN)
    phone_digits = re.sub(r"\D", "", phone)
    activity.log_command("contact_share", uid, uname, dname, details=f"phone={phone_digits}")


async def daily_maintenance_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily: archive long-past appointments, purge the appointment archive,
    and purge long-expired announcements."""
    await archive_old_appointments()
    await purge_archived_appointments()
    await purge_old_announcements()


# ---------------------------------------------------------------------------
# Command-execution logging, identity refresh, and config-drift alerts
# ---------------------------------------------------------------------------

async def _ignore_group_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop any message from a group/channel — the bot only serves private chats.

    Raising ApplicationHandlerStop prevents all later handlers (commands,
    conversations, free-text) from acting on group/channel messages.
    """
    raise ApplicationHandlerStop


async def _refresh_user_identity(uid: int, uname: "str | None", dname: str) -> None:
    """Keep the stored user record's username/display_name in sync with Telegram.

    A user may create, change, or remove their @username (or rename themselves)
    after registering; without this, the record written at /start goes stale
    forever (e.g. /userlist showing '—'). No-op if the user has no record yet —
    registration remains /start's job.
    """
    users = await storage.get_all_users()
    rec = next((u for u in users if u.get("chat_id") == uid), None)
    if rec is None:
        return
    if rec.get("username") == uname and rec.get("display_name") == dname:
        return
    rec["username"] = uname
    rec["display_name"] = dname
    await storage.save_users(users)
    logger.info("Refreshed identity for user %s: username=%s, display_name=%s",
                uid, uname, dname)


async def _known_admin_chat_ids() -> set:
    """Chat ids we can message admins on: phone-registered admins plus any
    registered user whose current username is listed in admins.yaml."""
    ids = set(permissions._admin_chat_ids)
    for u in await storage.get_all_users():
        uname = (u.get("username") or "").lstrip("@").lower()
        if uname and uname in permissions.ADMIN_USERNAMES and u.get("chat_id"):
            ids.add(u["chat_id"])
    return ids


# (official_id, kind, new_username) already reported — one alert per change,
# not one per command. Cleared on restart, which re-alerts if still unfixed.
_reported_username_drift: set = set()


async def _warn_official_username_drift(context: ContextTypes.DEFAULT_TYPE,
                                        uid: int, uname: "str | None") -> None:
    """Alert admins when an official's/proxy's configured username goes stale.

    Their chat_id still identifies them, but any username-based match in
    officials.yaml now fails, so appointment routing can silently misbehave
    until an admin updates the file.
    """
    drift = permissions.official_username_drift(uid, uname)
    if not drift:
        return
    key = (drift["official_id"], drift["kind"], (uname or "").lower())
    if key in _reported_username_drift:
        return
    _reported_username_drift.add(key)

    logger.warning(
        "Username drift for %s '%s' (official=%s): officials.yaml has @%s, "
        "Telegram now reports @%s — update config/officials.yaml.",
        drift["kind"], drift["name"], drift["official_id"],
        drift["configured"], drift["current"] or "(none)",
    )
    activity.log_command("username_drift", uid, uname, drift["name"],
                         details=f"{drift['configured']} -> {drift['current']}")

    now_txt = f"@{md(drift['current'])}" if drift["current"] else "_(no username set)_"
    text = (
        "⚠️ *Official username changed*\n\n"
        f"{md(drift['name'])} ({md(drift['kind'])} for `{md(drift['official_id'])}`) "
        f"is configured in officials.yaml as @{md(drift['configured'])}, "
        f"but Telegram now reports {now_txt}.\n\n"
        "Appointment permissions matched by username will fail until "
        "`config/officials.yaml` is updated."
    )
    for chat_id in await _known_admin_chat_ids():
        if chat_id == uid:
            continue  # tell the other admins, not the person who changed it
        try:
            await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
        except TelegramError as exc:
            logger.warning("Could not alert admin %s about username drift: %s", chat_id, exc)


async def _log_command_invocation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log every private command at INFO, keep the sender's stored
    username/display_name fresh, and flag officials whose configured username
    has gone stale. Runs in group -1 before real handlers."""
    msg = update.effective_message
    if not msg or not msg.text:
        return
    command = msg.text.split()[0]
    u = update.effective_user
    who = (u.full_name if u else None) or "unknown"
    uid = u.id if u else "?"
    logger.info("Command %s executed by %s (id=%s)", command, who, uid)
    if u:
        uid_i, uname, dname = user_info(update)
        await _refresh_user_identity(uid_i, uname, dname)
        await _warn_official_username_drift(context, uid_i, uname)


_ACTIVE_MEMBER_STATUSES = ("member", "administrator", "creator")


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

    groups = await storage._load_known_groups()
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
    await storage._save_known_groups(groups)


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
        BotCommand("reschedule", "Propose a new time for an upcoming appointment"),
        BotCommand("settimezone", "Set your time zone for displayed times"),
        BotCommand("language", "Choose your language"),
        BotCommand("notifications", "Choose which reminders you receive"),
        BotCommand("donate", "Support the congregation with a gift"),
        BotCommand("announcements", "View current announcements"),
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

    # Appointment reminder DMs (24h and 2h before each confirmed appointment).
    app.job_queue.run_repeating(
        appointment_reminder_job,
        interval=timedelta(minutes=5),
        first=timedelta(seconds=30),
        name="appointment_reminders",
    )

    # Daily maintenance: archive/purge old appointments and purge long-expired
    # announcements (runs shortly after each startup, then every 24h).
    app.job_queue.run_repeating(
        daily_maintenance_job,
        interval=timedelta(days=1),
        first=timedelta(seconds=60),
        name="daily_maintenance",
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
            BC_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bc_message),
                MessageHandler(filters.PHOTO | filters.Document.ALL, bc_media),
            ],
            BC_SELECT: [CallbackQueryHandler(bc_select, pattern=f"^{re.escape(CB_BC_PREFIX)}")],
            BC_RETRY: [CallbackQueryHandler(bc_retry, pattern=f"^{re.escape(CB_BC_PREFIX)}retry:")],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    appointment_conv = ConversationHandler(
        entry_points=[CommandHandler("appointment", cmd_appointment)],
        states={
            AP_OFFICIAL: [CallbackQueryHandler(
                ap_official, pattern=f"^{re.escape(CB_APSEL_PREFIX)}")],
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
            CA_CONFIRM: [CallbackQueryHandler(
                ca_confirm, pattern=f"^{re.escape(CB_CANCEL_PREFIX)}")],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )

    reschedule_conv = ConversationHandler(
        entry_points=[CommandHandler("reschedule", cmd_reschedule)],
        states={
            RS_SELECT: [CallbackQueryHandler(
                rs_select, pattern=f"^{re.escape(CB_RESCHED_PREFIX)}")],
            RS_NEWTIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, rs_newtime)],
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
        states={LANG_SELECT: [CallbackQueryHandler(
            lang_select, pattern=f"^{re.escape(CB_LANG_PREFIX)}")]},
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
    app.add_handler(CommandHandler("enable_appt_proxies", cmd_enable_appt_proxies))
    app.add_handler(appointment_conv)
    app.add_handler(cancel_appt_conv)
    app.add_handler(reschedule_conv)
    app.add_handler(settimezone_conv)
    app.add_handler(language_conv)
    app.add_handler(CommandHandler("notifications", cmd_notifications))
    app.add_handler(CommandHandler("donate", cmd_donate))
    app.add_handler(CommandHandler("announcements", cmd_announcements))
    app.add_handler(CommandHandler("listannouncements", cmd_listannouncements))

    add_announcement_conv = ConversationHandler(
        entry_points=[CommandHandler("addannouncement", cmd_addannouncement)],
        states={
            AN_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, an_title)],
            AN_BODY: [MessageHandler(filters.TEXT & ~filters.COMMAND, an_body)],
            AN_EXPIRES: [MessageHandler(filters.TEXT & ~filters.COMMAND, an_expires)],
            AN_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, an_confirm)],
            # After saving, the flow re-uses the broadcast target selection.
            BC_SELECT: [CallbackQueryHandler(bc_select, pattern=f"^{re.escape(CB_BC_PREFIX)}")],
            BC_RETRY: [CallbackQueryHandler(bc_retry, pattern=f"^{re.escape(CB_BC_PREFIX)}retry:")],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )
    app.add_handler(add_announcement_conv)

    del_announcement_conv = ConversationHandler(
        entry_points=[CommandHandler("delannouncement", cmd_delannouncement)],
        states={
            DA_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, da_select)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )
    app.add_handler(del_announcement_conv)
    app.add_handler(CallbackQueryHandler(
        notif_prefs_callback, pattern=f"^{re.escape(CB_NOTIFPREF_PREFIX)}"))

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

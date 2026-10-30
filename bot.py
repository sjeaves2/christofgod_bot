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
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
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

from settings import BOT_DISPLAY_NAME, BOT_TOKEN, activity
import error_reporting
import permissions
import storage

from network_watchdog import CHECK_INTERVAL, watchdog_job

from permissions import (
    user_info,
)
# The /broadcast command was retired in favour of /addannouncement, which does
# the same push plus a stored, translatable, expiring record. The delivery
# engine below (target selection, send, retry) is still very much in use — it
# is what actually pushes an announcement out.
from handlers.announcements import (
    add_announcement_conversation,
    delete_announcement_conversation,
    cmd_announcements,
    cmd_listannouncements,
    purge_old_announcements,
)
from handlers.events_admin import (
    add_event_conversation,
    delete_event_conversation,
    modify_event_conversation,
    set_service_link_conversation,
)
from handlers.appointments import (
    appointment_conversation,
    cancel_appointment_conversation,
    reschedule_conversation,
    CB_APPT_PREFIX,
    appointment_reminder_job,
    appt_callback,
    archive_old_appointments,
    cmd_enable_appt_proxies,
    cmd_myappointments,
    handle_counter_propose_message,
    purge_archived_appointments,
)
from handlers.user_basics import (
    language_conversation,
    set_timezone_conversation,
    CB_NOTIFPREF_PREFIX,
    _commands_text,
    _register_official_if_known,
    cmd_adminhelp,
    cmd_donate,
    cmd_events,
    cmd_export_calendar,
    cmd_help,
    cmd_listevents,
    cmd_notifications,
    cmd_privacy,
    cmd_start,
    cmd_stop,
    cmd_unknown,
    nudge_group_to_private,
    ignore_edited_messages,
    cmd_usercount,
    cmd_userlist,
    notif_prefs_callback,
)
from common import (
    get_user_prefs,
    md,
    reply_markdown,
    send_markdown,
)
from events import check_service_links
from handlers.notifications import (
    notification_catchup_job,
    schedule_all_upcoming,
)
from handlers.backup import (
    BACKUP_TIME,
    cmd_backup,
    nightly_backup_job,
)
from handlers.prayer import (
    prayer_conversation,
    respond_prayer_conversation,
    cmd_dismissprayer,
    cmd_prayerrequests,
    purge_old_stubs,
)
from handlers.board import (
    add_board_item_conversation,
    board_items_conversation,
    BOARD_NOTICE_TIME,
    CB_BOFF_PREFIX,
    board_notice_job,
    board_officer_callback,
    cmd_boardcalendar,
    cmd_setboardofficer,
    purge_deleted_board_items,
)
from handlers.stats import cmd_stats

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
    cmd_text = _commands_text(lang, is_adm, permissions.is_prayer_admin(update),
                              await permissions.is_board_officer(update))

    await update.message.reply_text(reply, reply_markup=ReplyKeyboardRemove())
    await reply_markdown(update.message, cmd_text)
    phone_digits = re.sub(r"\D", "", phone)
    activity.log_command("contact_share", uid, uname, dname, details=f"phone={phone_digits}")


async def daily_maintenance_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily: archive long-past appointments, purge the appointment archive,
    and purge long-expired announcements, prayer stubs and soft-deleted board
    items.

    bot.log is NOT handled here any more. It rotates daily and drops expired
    files on its own (see settings.py), which replaced a pruner that read the
    whole file — 9 seconds of every startup on a 45 MB log.
    """
    await archive_old_appointments()
    await purge_archived_appointments()
    await purge_old_announcements()
    await purge_old_stubs()
    await purge_deleted_board_items()


# ---------------------------------------------------------------------------
# Command-execution logging, identity refresh, and config-drift alerts
# ---------------------------------------------------------------------------

async def _refresh_user_identity(uid: int, uname: str | None, dname: str) -> None:
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
                                        uid: int, uname: str | None) -> None:
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
            await send_markdown(context.bot, chat_id, text)
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
    """Log every unhandled exception and alert ops admins.

    error_reporting assigns an error id, writes the full traceback to
    logs/errors.log, and applies the de-duplication and transient-network
    rules before messaging anyone.
    """
    logger.error("Unhandled exception:", exc_info=context.error)
    exc = context.error
    if not isinstance(exc, BaseException):
        # PTB normally supplies an exception; be tolerant of anything else.
        activity.log_error(str(exc))
        return
    note = _error_context_note(update)
    try:
        await error_reporting.report_exception(context.bot, exc, note)
    except Exception:  # noqa: BLE001 - reporting must never mask the original
        logger.exception("Error reporting itself failed")


def _error_context_note(update: object) -> str:
    """Short description of what the user was doing when the error happened."""
    user = getattr(update, "effective_user", None)
    message = getattr(update, "effective_message", None)
    parts = []
    if user is not None and getattr(user, "id", None):
        parts.append(f"user {user.id}")
    text = getattr(message, "text", None)
    if text:
        parts.append(f"input {text.split()[0]!r}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Post-init: schedule all notifications
# ---------------------------------------------------------------------------

#: How many bad links to name in the startup alert before summarising the rest.
LINK_ALERT_LIMIT = 6


async def alert_on_bad_service_links(app: Application) -> None:
    """Warn ops at startup when upcoming services have unusable join links.

    A restart is exactly when a re-seeded config appears, and nothing else in
    the bot reads those links until a reminder fires — by which point the
    congregation has already received the bad one. See check_service_links().

    Never allowed to prevent startup: a bot that runs with bad links is far
    better than a bot that does not run.
    """
    try:
        await _alert_on_bad_service_links(app)
    except Exception:
        # Deliberately broad. This is a convenience warning; startup is not.
        logger.exception("Service-link check failed; continuing startup.")


async def _alert_on_bad_service_links(app: Application) -> None:
    problems = await check_service_links()
    if not problems:
        return

    logger.warning("%d upcoming service(s) have an unusable join link.", len(problems))
    shown = problems[:LINK_ALERT_LIMIT]
    lines = [f"⚠️ *{len(problems)} upcoming service(s)* have an unusable join link:", ""]
    lines += [f"• {md(label)} — {reason}" for label, reason in shown]
    if len(problems) > len(shown):
        lines.append(f"• …and {len(problems) - len(shown)} more")
    lines += ["", "Fix each one with /setservicelink, then run /backup."]
    text = "\n".join(lines)

    for chat_id in await error_reporting.ops_chat_ids():
        try:
            await send_markdown(app.bot, chat_id, text)
        except TelegramError:
            logger.warning("Could not alert %s about service links.", chat_id, exc_info=True)


#: The command menu Telegram shows every user. Admin, prayer and board
#: commands are deliberately ABSENT: each is gated and answers "Unknown
#: command" to everyone else, so listing them here would advertise queues most
#: people cannot open. /help assembles the per-role lists instead.
PUBLIC_COMMANDS: list[BotCommand] = [
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
    BotCommand("prayer", "Send a prayer request"),
    BotCommand("privacy", "What the bot stores about you"),
    BotCommand("donate", "Support the congregation with a gift"),
    BotCommand("announcements", "View current announcements"),
    BotCommand("stop", "Unsubscribe from notifications"),
]


def _register_jobs(app: Application) -> None:
    """Attach the recurring jobs. Called from post_init.

    Lifted out of post_init so that function reads as the four things startup
    does rather than as one 114-line block. The comments here are the record of
    why each cadence is what it is — several were written after an incident.
    """
    # Weekly reschedule — extends the notification window as events enter the
    # 400-day horizon that schedule_all_upcoming() looks at.
    #
    # NO `first=`, so the first run is one interval (7 days) from startup;
    # that is PTB's documented default. It used to be `first=10 seconds`, which
    # meant that ten seconds after every start this job removed and re-added
    # all ~180 notification jobs that the schedule_all_upcoming() call above had
    # just created. Measured on the 2026-10-29 restart:
    #
    #     12:43:07.196  Scheduled 180 upcoming event notifications   <- above
    #     12:43:17.484  Scheduled 180 upcoming event notifications   <- this job
    #
    # Pure duplication, and the source of the 32,592 "Added job"/"Removed job"
    # lines that were a third of a 45 MB bot.log.
    #
    # The premise was checked rather than assumed, because removing it would be
    # wrong if post_init's jobs did not survive: APScheduler holds jobs added
    # before its scheduler starts and flushes them on start, so the pass above
    # — which runs before Application.start() — does take effect. Verified
    # empirically in tests/test_reschedule_startup.py, not just read from docs.
    #
    # An unrestarted process still gets its weekly pass; a restarted one gets
    # post_init's. Nothing is left unscheduled either way.
    #
    # (The comment here used to claim "runs every Sunday at 00:05". It never
    # did: run_repeating anchors to process start, not to a wall-clock day.)
    app.job_queue.run_repeating(
        reschedule_job,
        interval=timedelta(days=7),
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

    # Nightly backup of data/ — DM'd to ops admins at 3am church time. The YAML
    # stores are the only copy of registrations and appointments.
    app.job_queue.run_daily(
        nightly_backup_job,
        time=BACKUP_TIME,
        name="nightly_backup",
    )

    # Board compliance notices — DM'd to board officers at 6am church time.
    #
    # run_daily, not run_repeating: these are calendar-day decisions (is today
    # inside the lead time? has a notice already gone out today?), and a
    # repeating interval anchored to process start would drift an hour earlier
    # every restart until the 6am notice arrived at 2am. The schedule itself
    # is self-healing, so a day missed entirely is caught the next morning.
    app.job_queue.run_daily(
        board_notice_job,
        time=BOARD_NOTICE_TIME,
        name="board_notices",
    )

    # Daily maintenance: archive/purge old appointments and purge long-expired
    # announcements (runs shortly after each startup, then every 24h).
    app.job_queue.run_repeating(
        daily_maintenance_job,
        interval=timedelta(days=1),
        first=timedelta(seconds=60),
        name="daily_maintenance",
    )

    # Network watchdog — exits the process after a sustained Telegram outage so
    # systemd restarts it. Without this the bot can sit wedged but "active"
    # indefinitely, which is exactly what happened on 2026-09-04.
    app.job_queue.run_repeating(
        watchdog_job,
        interval=CHECK_INTERVAL,
        first=CHECK_INTERVAL,
        name="network_watchdog",
    )


async def post_init(app: Application) -> None:
    # A restart usually means new code, so last run's traces are stale.
    error_reporting.reset_error_log()
    await schedule_all_upcoming(app)
    await alert_on_bad_service_links(app)

    await app.bot.set_my_commands(PUBLIC_COMMANDS)
    _register_jobs(app)


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

    # --- Register handlers ---
    #
    # ORDER IS LOAD-BEARING. PTB runs only the FIRST matching handler per
    # group, and group 0 ends with two handlers that match almost anything, so
    # anything registered after them is dead code that still looks registered.
    # /prayer was silently broken this way once. The conversations are built in
    # their own feature modules; this sequence is the part that decides which
    # handler receives an update, and tests/test_handler_registration_order.py
    # pins it.

    # Log/observe the bot being added to or removed from groups/channels.
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Group -1 runs before the real handlers in group 0:
    #   1. Group/channel messages: a COMMAND gets a nudge to DM the bot,
    #      anything else is dropped (the bot serves private chats only)
    #   2. Drop edited messages (update.message is None on those — see below)
    #   3. Log private command execution at INFO
    app.add_handler(
        MessageHandler(~filters.ChatType.PRIVATE, nudge_group_to_private), group=-1
    )
    app.add_handler(
        MessageHandler(filters.UpdateType.EDITED_MESSAGE, ignore_edited_messages),
        group=-1,
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
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("backup", cmd_backup))

    app.add_handler(add_event_conversation())
    app.add_handler(modify_event_conversation())
    app.add_handler(delete_event_conversation())
    app.add_handler(set_service_link_conversation())
    app.add_handler(CommandHandler("myappointments", cmd_myappointments))
    app.add_handler(CommandHandler("enable_appt_proxies", cmd_enable_appt_proxies))
    app.add_handler(appointment_conversation())
    app.add_handler(cancel_appointment_conversation())
    app.add_handler(reschedule_conversation())
    app.add_handler(set_timezone_conversation())
    app.add_handler(language_conversation())
    app.add_handler(CommandHandler("notifications", cmd_notifications))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("donate", cmd_donate))
    app.add_handler(CommandHandler("announcements", cmd_announcements))
    app.add_handler(CommandHandler("listannouncements", cmd_listannouncements))

    app.add_handler(add_announcement_conversation())
    app.add_handler(delete_announcement_conversation())
    app.add_handler(CallbackQueryHandler(
        notif_prefs_callback, pattern=f"^{re.escape(CB_NOTIFPREF_PREFIX)}"))

    app.add_handler(CallbackQueryHandler(appt_callback, pattern=f"^{re.escape(CB_APPT_PREFIX)}"))

    app.add_handler(prayer_conversation())
    app.add_handler(respond_prayer_conversation())
    app.add_handler(CommandHandler("prayerrequests", cmd_prayerrequests))
    app.add_handler(CommandHandler("dismissprayer", cmd_dismissprayer))

    app.add_handler(board_items_conversation())
    app.add_handler(add_board_item_conversation())
    app.add_handler(CommandHandler("boardcalendar", cmd_boardcalendar))
    app.add_handler(CommandHandler("setboardofficer", cmd_setboardofficer))
    app.add_handler(CallbackQueryHandler(
        board_officer_callback, pattern=f"^{re.escape(CB_BOFF_PREFIX)}"))

    # Catch-all free-text handler. MUST come after every ConversationHandler:
    # PTB runs only the first matching handler per group, and this one matches
    # ANY text, so a conversation registered below it would never receive its
    # own replies. (/prayer was briefly broken exactly this way — the member's
    # request was swallowed here and silently discarded, with no error to show
    # for it, because this handler returns quietly when no counter-propose is
    # pending.)
    # Free-text handler for counter-propose date/time responses
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_counter_propose_message,
        )
    )


    # LAST in group 0, deliberately: PTB runs only the first matching handler
    # per group, so this catches commands nothing above recognised. In a later
    # group it would instead fire on every valid command as well. Scoped to
    # private chats — group/channel traffic is already dropped in group -1, but
    # this should not depend on a handler registered far away.
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.COMMAND, cmd_unknown)
    )

    app.add_error_handler(error_handler)

    logger.info("Starting %s…", BOT_DISPLAY_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

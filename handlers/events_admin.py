"""Admin event management: /addevent, /modifyevent, /deleteevent and
/setservicelink (per-service join links for convocation phases).

All four are admin-only multi-step conversations that read and write the
special_events / convocation_* maps in events.yaml.
"""

from __future__ import annotations

import logging
import re
import uuid

import storage
from common import format_dt, md
from events import _merge_special_events, all_upcoming
from handlers.notifications import schedule_all_upcoming, schedule_event_notification
from hebrew_calendar import service_phases
from permissions import admin_only, user_info
from settings import DEFAULT_NOTIF_MIN, activity
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, ConversationHandler

logger = logging.getLogger(__name__)

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
    await update.message.reply_text("➕ *Add Special Event*\n\nEvent name:",
                                    parse_mode=ParseMode.MARKDOWN)
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

    evdata = await storage.get_all_events_data()
    specials = evdata.get("special_events", [])
    specials.append(new_defn)
    evdata["special_events"] = specials
    await storage.save_events_data(evdata)

    # Schedule notification for the new event
    app: Application = context.application
    fake_events = _merge_special_events([new_defn], {}, days_ahead=400)
    for ev in fake_events:
        schedule_event_notification(app, ev)

    uid, uname, dname = user_info(update)
    activity.log_command("addevent", uid, uname, dname,
                         details=f"Added '{d['ae_name']}' (id:{new_id})")
    await update.message.reply_text(f"✅ Event added (ID: `{new_id}`)",
                                    parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /modifyevent
# ---------------------------------------------------------------------------

ME_SELECT, ME_FIELD, ME_VALUE = range(3)


@admin_only
async def cmd_modifyevent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("modifyevent", uid, uname, dname)
    evdata = await storage.get_all_events_data()
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
        f"Modifying: *{md(ev['name'])}*\n\n"
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

    evdata = await storage.get_all_events_data()
    specials = evdata.get("special_events", [])
    for i, e in enumerate(specials):
        if e["id"] == ev["id"]:
            specials[i] = ev
            break
    evdata["special_events"] = specials
    await storage.save_events_data(evdata)

    # Reschedule
    app: Application = context.application
    new_events = _merge_special_events([ev], {}, days_ahead=400)
    for e in new_events:
        schedule_event_notification(app, e)

    uid, uname, dname = user_info(update)
    activity.log_command("modifyevent", uid, uname, dname,
                         details=f"Modified '{ev['name']}' field={field}")
    await update.message.reply_text(f"✅ Updated *{md(field)}* for *{md(ev['name'])}*.",
                                    parse_mode=ParseMode.MARKDOWN)
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
        lines.append(f"{i+1}. [{ev['type'][0].upper()}] {ev['name']}  "
                     f"({format_dt(ev['service_time'])})")
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
            f"Delete *{md(ev['name'])}*? (yes/no)", parse_mode=ParseMode.MARKDOWN
        )
        return DE_CONFIRM
    else:
        await update.message.reply_text(
            f"*{md(ev['name'])}* is a convocation (cannot be deleted).\n"
            "Enter an urgent announcement to add (or '-' to cancel):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return DE_ANNOT


async def de_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.strip().lower() not in ("yes", "y"):
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END
    ev: dict = context.user_data["de_ev"]
    evdata = await storage.get_all_events_data()
    specials = [e for e in evdata.get("special_events", []) if e["id"] != ev["key"]]
    evdata["special_events"] = specials
    await storage.save_events_data(evdata)
    uid, uname, dname = user_info(update)
    activity.log_command("deleteevent", uid, uname, dname, details=f"Deleted '{ev['name']}'")
    await update.message.reply_text(f"✅ *{md(ev['name'])}* deleted.",
                                    parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


async def de_annot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == "-":
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END
    ev: dict = context.user_data["de_ev"]
    evdata = await storage.get_all_events_data()
    ann_map: dict = evdata.setdefault("convocation_announcements", {})
    ann_map.setdefault(ev["key"], []).append(text)
    await storage.save_events_data(evdata)
    uid, uname, dname = user_info(update)
    activity.log_command(
        "deleteevent", uid, uname, dname,
        details=f"Added announcement to '{ev['name']}': {text}"
    )
    await update.message.reply_text("⚠️ Announcement added to the convocation notification.",
                                    parse_mode=ParseMode.MARKDOWN)
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

    evdata = await storage.get_all_events_data()
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

    evdata = await storage.get_all_events_data()
    urls_map: dict[str, str] = evdata.setdefault("convocation_urls", {})
    if text == "-":
        urls_map.pop(ph["phase_key"], None)
        action_msg = f"🔗 Cleared the link for *{ph['display']}*."
        detail = f"Cleared link for {ph['phase_key']}"
    else:
        urls_map[ph["phase_key"]] = text
        action_msg = f"🔗 Link set for *{ph['display']}*."
        detail = f"Set link for {ph['phase_key']}"
    await storage.save_events_data(evdata)

    # Reschedule upcoming notifications so they carry the updated link.
    await schedule_all_upcoming(context.application)

    activity.log_command("setservicelink", uid, uname, dname, details=detail)
    await update.message.reply_text(action_msg, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

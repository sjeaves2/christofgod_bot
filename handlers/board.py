"""Board compliance items — the officer-facing commands.

Four things board officers do here:

* ``/boarditems``  — the queue in due-date order, and the way into every other
  action. Drill down to an item, then Complete / Edit / Delete it.
* ``/addboarditem`` — add an obligation.
* ``/setboardofficer`` — turn the flag on or off for a registered user. Gated on
  ``admin_only``, NOT ``board_officer_only``, and that is not an oversight: the
  officer gate would need an officer to already exist, so nobody could ever
  create the first one. An administrator seeds the board; after that the
  officers manage the flag themselves.

The domain logic — the schedule, the ordering, the lifecycle — lives in
``board_items``. This module is conversation and presentation only.

Every action is reached by TWO taps, not one: the list gives a detail view, and
the detail view's destructive buttons confirm. These are corporate records,
there are four people with access, and a mis-tapped "Delete" on the tax
exemption renewal is not a mistake worth optimising keystrokes for.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date, time, timedelta
from typing import Any

import board_items as bi
import permissions
import storage
from common import (
    _answer_cb,
    _is_affirmative,
    edit_markdown,
    is_button_url,
    md,
    now_tz,
    reply_markdown,
)
from common import send_markdown
from ics_generator import board_items_to_ics
from permissions import admin_only, board_officer_only, user_info
from settings import TZ, activity
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
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

#: 6am church time, as the user chose. Early enough that a deadline notice is
#: the first thing an officer sees, and three hours clear of the 3am backup so
#: the two jobs never contend for the same files.
BOARD_NOTICE_HOUR = 6
BOARD_NOTICE_MINUTE = 0
BOARD_NOTICE_TIME = time(hour=BOARD_NOTICE_HOUR, minute=BOARD_NOTICE_MINUTE, tzinfo=TZ)

#: What the exported calendar is called in the officer's client, and the
#: filename they receive.
BOARD_CALENDAR_NAME = "Board Deadlines"
BOARD_CALENDAR_FILENAME = "board_deadlines.ics"

CB_BOARD_PREFIX = "bi:"
CB_BOFF_PREFIX = "bo:"

# Conversation states. Offset clear of the prayer range (40-41).
BI_MENU, BI_EDIT_VALUE, BI_NEXT_DUE = range(60, 63)

# Inline keyboards are capped by Telegram, and a hundred-row list is unusable
# long before that. The congregation has twelve registered users today; this is
# here so a grown roster degrades with a message rather than a failed send.
OFFICER_PICKER_LIMIT = 50

#: Which fields /boarditems can edit, and how each is prompted. Order is the
#: order of the buttons.
EDIT_FIELDS: list[tuple[str, str]] = [
    ("action", "What has to be done"),
    ("due", "Due date"),
    ("notify_days", "Lead time"),
    ("description", "Details"),
    ("url", "Link"),
]
_EDIT_LABELS = dict(EDIT_FIELDS)

MAX_NOTIFY_DAYS = 3650   # ten years; beyond this it is a typo, not a lead time


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _urgency(item: dict[str, Any], today: date) -> str:
    """A glyph for how close this item is, by the same thresholds the bot
    notifies on — so the list and the notices never disagree."""
    days = bi.days_until(item, today)
    if days is None:
        return "❔"
    if days < 0 or days <= bi.DAILY_WINDOW_DAYS:
        return "🔴"
    return "🟡" if days <= bi.notify_days(item) else "⚪️"


def _when(item: dict[str, Any], today: date) -> str:
    """Human phrasing for the due date's distance, overdue included."""
    days = bi.days_until(item, today)
    if days is None:
        return "no due date set"
    # Single asterisks: Telegram's LEGACY Markdown has no `**bold**`, so a
    # doubled marker is parsed as an empty emphasis followed by an unpaired
    # one — and one unpaired marker makes Telegram reject the whole message.
    if days < 0:
        return f"*OVERDUE by {-days} day{'s' if days != -1 else ''}*"
    if days == 0:
        return "*due today*"
    return f"in {days} day{'s' if days != 1 else ''}"


def _when_notices_start(item: dict[str, Any], today: date) -> str:
    """One sentence telling the officer when the bot will start nagging.

    Worth saying out loud at creation time: an item added 90 days out with a
    60-day lead time stays silent for a month, and without this line that looks
    indistinguishable from a feature that does not work.
    """
    due = bi.due_date(item)
    lead = bi.notify_days(item)
    if due is None:
        return "No due date, so nothing will be sent."
    starts = due - timedelta(days=lead)
    if starts <= today:
        return "Notices start today."
    return f"Notices start on {starts.isoformat()}, {lead} days before it is due."


def _short(text: Any, limit: int = 40) -> str:
    """Trim an action for a button label (Telegram caps button text)."""
    value = str(text or "(untitled)").strip().replace("\n", " ")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def render_queue(items: list[dict[str, Any]], today: date) -> str:
    """The overview message: pending items, soonest first."""
    pending = bi.pending(items)
    if not pending:
        return ("🗂 *Board Items*\n\nNothing pending. "
                "Add one with /addboarditem.")
    lines = ["🗂 *Board Items* — pending, soonest first\n"]
    for n, item in enumerate(pending, 1):
        due = bi.due_date(item)
        lines.append(
            f"{_urgency(item, today)} *{n}. {md(item.get('action'))}*\n"
            f"    {due.isoformat() if due else '?'} — {_when(item, today)}"
        )
    return "\n".join(lines)


def queue_keyboard(items: list[dict[str, Any]], today: date) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{_urgency(item, today)} {n}. {_short(item.get('action'))}",
                              callback_data=f"{CB_BOARD_PREFIX}show:{item.get('id')}")]
        for n, item in enumerate(bi.pending(items), 1)
    ]
    rows.append([InlineKeyboardButton("📋 Completed items",
                                      callback_data=f"{CB_BOARD_PREFIX}completed")])
    rows.append([InlineKeyboardButton("Close", callback_data=f"{CB_BOARD_PREFIX}close")])
    return InlineKeyboardMarkup(rows)


def render_item(item: dict[str, Any], today: date) -> str:
    """The detail view for one item."""
    due = bi.due_date(item)
    done = bi.status_of(item) == bi.STATUS_COMPLETED
    head = "✅ *Completed*" if done else f"{_urgency(item, today)} *Pending*"
    lines = [f"{head}\n", f"*{md(item.get('action'))}*\n"]
    if done:
        lines.append(f"Completed: {md(item.get('completed_at') or '?')}"
                     f" by {md(item.get('completed_by') or '?')}")
        lines.append(f"Was due: {due.isoformat() if due else '?'}")
    else:
        lines.append(f"Due: {due.isoformat() if due else '?'} — {_when(item, today)}")
        lines.append(f"First notice: {bi.notify_days(item)} days before, "
                     f"then weekly, then daily inside {bi.DAILY_WINDOW_DAYS} days")
        last = item.get("last_notified")
        lines.append(f"Last notice sent: {md(last) if last else 'none yet'}")
    if item.get("description"):
        lines.append(f"\n{md(item.get('description'))}")
    url = is_button_url(item.get("url"))
    if item.get("url") and not url:
        # Not a link Telegram will put on a button. Show it as text rather than
        # dropping it — an officer pasted it for a reason.
        lines.append(f"\nLink (not tappable): {md(item.get('url'))}")
    history = item.get("history") or []
    if history:
        lines.append(f"\n_Previous cycles: "
                     f"{md(', '.join(str(h.get('completed_at')) for h in history))}_")
    lines.append(f"\n`{md(item.get('id'))}`")
    return "\n".join(lines)


def item_keyboard(item: dict[str, Any]) -> InlineKeyboardMarkup:
    item_id = item.get("id")
    rows: list[list[InlineKeyboardButton]] = []
    if bi.status_of(item) == bi.STATUS_COMPLETED:
        rows.append([InlineKeyboardButton(
            "♻️ Reopen with a new date",
            callback_data=f"{CB_BOARD_PREFIX}reopen:{item_id}")])
    else:
        rows.append([
            InlineKeyboardButton("✅ Mark completed",
                                 callback_data=f"{CB_BOARD_PREFIX}done:{item_id}"),
            InlineKeyboardButton("✏️ Edit",
                                 callback_data=f"{CB_BOARD_PREFIX}edit:{item_id}"),
        ])
    rows.append([InlineKeyboardButton("🗑 Delete",
                                      callback_data=f"{CB_BOARD_PREFIX}del:{item_id}")])
    url = is_button_url(item.get("url"))
    if url:
        rows.append([InlineKeyboardButton("🔗 Open link", url=url)])
    rows.append([InlineKeyboardButton("← Back",
                                      callback_data=f"{CB_BOARD_PREFIX}list")])
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard(verb: str, item_id: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"{CB_BOARD_PREFIX}{verb}:{item_id}")],
        [InlineKeyboardButton("Cancel", callback_data=f"{CB_BOARD_PREFIX}show:{item_id}")],
    ])


# ---------------------------------------------------------------------------
# /boarditems
# ---------------------------------------------------------------------------


@board_officer_only
async def cmd_boarditems(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("boarditems", uid, uname, dname)
    items = await storage.get_board_items()
    today = now_tz().date()
    await reply_markdown(update.message, render_queue(items, today),
                         reply_markup=queue_keyboard(items, today))
    return BI_MENU


async def _show_queue(query, items: list[dict[str, Any]], today: date) -> int:
    await edit_markdown(query, render_queue(items, today),
                        reply_markup=queue_keyboard(items, today))
    return BI_MENU


async def _show_item(query, item: dict[str, Any], today: date) -> int:
    await edit_markdown(query, render_item(item, today),
                        reply_markup=item_keyboard(item))
    return BI_MENU


async def board_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Router for every button in the /boarditems flow.

    Re-checks the officer flag. The entry point is gated, but a button from a
    session days ago is still tappable, and the board can change between the
    two — the flag is a runtime value precisely so it can.
    """
    query = update.callback_query
    await _answer_cb(query)
    if not await permissions.is_board_officer(update):
        await edit_markdown(query, "⛔ Unknown command.")
        return ConversationHandler.END

    uid, uname, dname = user_info(update)
    raw = query.data[len(CB_BOARD_PREFIX):]
    verb, _, item_id = raw.partition(":")
    items = await storage.get_board_items()
    today = now_tz().date()

    if verb == "close":
        await edit_markdown(query, "🗂 Closed. /boarditems to reopen.")
        return ConversationHandler.END

    if verb == "list":
        return await _show_queue(query, items, today)

    if verb == "completed":
        done = bi.completed(items)
        if not done:
            await edit_markdown(
                query, "No completed items yet.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "← Back", callback_data=f"{CB_BOARD_PREFIX}list")]]))
            return BI_MENU
        rows = [[InlineKeyboardButton(f"✅ {_short(i.get('action'))}",
                                      callback_data=f"{CB_BOARD_PREFIX}show:{i.get('id')}")]
                for i in done]
        rows.append([InlineKeyboardButton("← Back",
                                          callback_data=f"{CB_BOARD_PREFIX}list")])
        await edit_markdown(
            query,
            "📋 *Completed items*\n\nReopen one with a new due date rather than "
            "adding a fresh item — it keeps the renewal history.",
            reply_markup=InlineKeyboardMarkup(rows))
        return BI_MENU

    item = bi.by_id(items, item_id)
    if item is None:
        # The item was deleted or completed from another officer's phone while
        # this keyboard sat on screen. Say so instead of failing silently.
        await edit_markdown(
            query, "That item is no longer on the list — it may have been "
                   "changed by another officer.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "← Back", callback_data=f"{CB_BOARD_PREFIX}list")]]))
        return BI_MENU

    if verb == "show":
        return await _show_item(query, item, today)

    if verb == "done":
        await edit_markdown(
            query,
            f"Mark *{md(item.get('action'))}* as completed?",
            reply_markup=_confirm_keyboard("done_yes", item_id, "✅ Yes, it is done"))
        return BI_MENU

    if verb == "done_yes":
        bi.complete(item, today, dname)
        await storage.save_board_items(items)
        activity.log_command("boarditems", uid, uname, dname,
                             details=f"completed {item_id}")
        context.user_data["bi_next_due_id"] = item_id
        await edit_markdown(
            query,
            f"✅ *{md(item.get('action'))}* marked completed.\n\n"
            "Most of these come round again. Send the NEXT due date "
            "(YYYY-MM-DD) to queue it up now, or /skip to leave it retired — "
            "you can reopen it any time from *Completed items*.")
        return BI_NEXT_DUE

    if verb == "reopen":
        context.user_data["bi_next_due_id"] = item_id
        await edit_markdown(
            query,
            f"♻️ Reopening *{md(item.get('action'))}*.\n\n"
            "Send the new due date (YYYY-MM-DD), or /skip to leave it retired:")
        return BI_NEXT_DUE

    if verb == "del":
        await edit_markdown(
            query,
            f"Delete *{md(item.get('action'))}*?\n\n"
            f"It stops appearing and stops notifying. Recoverable by hand for "
            f"{bi.DELETED_PURGE_AFTER_DAYS} days, then gone.\n\n"
            "If the obligation was *met*, use ✅ Mark completed instead — that "
            "keeps the renewal history.",
            reply_markup=_confirm_keyboard("del_yes", item_id, "🗑 Yes, delete it"))
        return BI_MENU

    if verb == "del_yes":
        bi.soft_delete(item, today, dname)
        await storage.save_board_items(items)
        activity.log_command("boarditems", uid, uname, dname,
                             details=f"deleted {item_id}")
        items = await storage.get_board_items()
        await edit_markdown(
            query, f"🗑 Deleted *{md(item.get('action'))}*.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "← Back to the list", callback_data=f"{CB_BOARD_PREFIX}list")]]))
        return BI_MENU

    if verb == "edit":
        rows = [[InlineKeyboardButton(
            label, callback_data=f"{CB_BOARD_PREFIX}ef_{field}:{item_id}")]
            for field, label in EDIT_FIELDS]
        rows.append([InlineKeyboardButton(
            "← Back", callback_data=f"{CB_BOARD_PREFIX}show:{item_id}")])
        await edit_markdown(query, f"✏️ Editing *{md(item.get('action'))}*\n\n"
                                   "Which field?",
                            reply_markup=InlineKeyboardMarkup(rows))
        return BI_MENU

    if verb.startswith("ef_"):
        field = verb[3:]
        if field not in _EDIT_LABELS:
            return await _show_item(query, item, today)
        context.user_data["bi_edit"] = {"id": item_id, "field": field}
        await edit_markdown(query, _edit_prompt(field, item))
        return BI_EDIT_VALUE

    logger.warning("Unrecognised board callback %r", query.data)
    return BI_MENU


def _edit_prompt(field: str, item: dict[str, Any]) -> str:
    current = item.get(field)
    prompts = {
        "action": f"Send the new wording (max {bi.ACTION_MAX} characters):",
        "due": "Send the new due date (YYYY-MM-DD):",
        "notify_days": ("Send the lead time in days — how far ahead the first "
                        "notice goes out:"),
        "description": (f"Send the new details (max {bi.DESCRIPTION_MAX} "
                        "characters), or /skip to clear them:"),
        "url": "Send the new link, or /skip to remove it:",
    }
    shown = f"\n\nCurrently: {md(current)}" if current not in (None, "") else ""
    return f"*{_EDIT_LABELS[field]}*{shown}\n\n{prompts[field]}"


# ---------------------------------------------------------------------------
# Applying an edit
# ---------------------------------------------------------------------------


def _validate(field: str, text: str) -> tuple[Any, str | None]:
    """Return (value, error). A non-None error is shown and the state repeats."""
    if field == "action":
        if not text or len(text) > bi.ACTION_MAX:
            return None, f"Please send 1–{bi.ACTION_MAX} characters:"
        return text, None
    if field == "due":
        parsed = bi.parse_date(text)
        if parsed is None:
            return None, "Please use YYYY-MM-DD:"
        return parsed.isoformat(), None
    if field == "notify_days":
        try:
            days = int(text)
        except ValueError:
            return None, "Please send a whole number of days:"
        if not 1 <= days <= MAX_NOTIFY_DAYS:
            return None, f"Please send a number between 1 and {MAX_NOTIFY_DAYS}:"
        return days, None
    if field == "description":
        if len(text) > bi.DESCRIPTION_MAX:
            return None, f"Too long — max {bi.DESCRIPTION_MAX} characters:"
        return text, None
    if field == "url":
        url = is_button_url(text)
        if not url:
            return None, ("That is not a link Telegram will accept — it needs "
                          "http:// or https:// and no spaces. Send another, or "
                          "/skip to remove it:")
        return url, None
    return None, "Unknown field."


async def bi_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pending_edit = context.user_data.get("bi_edit") or {}
    field, item_id = pending_edit.get("field"), pending_edit.get("id")
    if not field or not item_id:
        await update.message.reply_text("Nothing to edit. /boarditems to start again.")
        return ConversationHandler.END

    value, error = _validate(field, (update.message.text or "").strip())
    if error:
        await update.message.reply_text(error)
        return BI_EDIT_VALUE
    return await _apply_edit(update, context, item_id, field, value)


async def bi_skip_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/skip — clear the field, for the two that may legitimately be empty."""
    pending_edit = context.user_data.get("bi_edit") or {}
    field, item_id = pending_edit.get("field"), pending_edit.get("id")
    if field not in ("description", "url"):
        await update.message.reply_text(
            "This field cannot be left empty. Send a value, or /cancel:")
        return BI_EDIT_VALUE
    return await _apply_edit(update, context, item_id, field, "")


async def _apply_edit(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      item_id: str, field: str, value: Any) -> int:
    uid, uname, dname = user_info(update)
    items = await storage.get_board_items()
    item = bi.by_id(items, item_id)
    if item is None:
        await update.message.reply_text(
            "That item is no longer on the list. /boarditems to start again.")
        return ConversationHandler.END

    item[field] = value
    if field == "due":
        # A calendar client that already holds this item ignores an incoming
        # copy with the same SEQUENCE, so without this an officer who
        # re-imports after a date change keeps seeing the old date.
        bi.bump_sequence(item)
    if field in ("due", "notify_days"):
        # The schedule is recomputed from the due date and the lead time, and
        # last_notified is what suppresses a repeat within the same day. Moving
        # either without clearing it would hold the first notice of the NEW
        # schedule back by up to a week.
        item.pop("last_notified", None)
    await storage.save_board_items(items)
    activity.log_command("boarditems", uid, uname, dname,
                         details=f"edited {item_id}.{field}")
    context.user_data.pop("bi_edit", None)
    today = now_tz().date()
    await reply_markdown(
        update.message,
        f"✅ Updated *{md(_EDIT_LABELS[field])}*.\n\n{render_item(item, today)}",
        reply_markup=item_keyboard(item))
    return BI_MENU


# ---------------------------------------------------------------------------
# The next due date (after completing, or when reopening)
# ---------------------------------------------------------------------------


async def bi_next_due(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    item_id = context.user_data.get("bi_next_due_id")
    new_due = bi.parse_date((update.message.text or "").strip())
    if new_due is None:
        await update.message.reply_text(
            "Please use YYYY-MM-DD, or /skip to leave it retired:")
        return BI_NEXT_DUE

    uid, uname, dname = user_info(update)
    items = await storage.get_board_items()
    item = bi.by_id(items, item_id)
    if item is None:
        await update.message.reply_text(
            "That item is no longer on the list. /boarditems to start again.")
        return ConversationHandler.END

    bi.reopen(item, new_due)
    await storage.save_board_items(items)
    activity.log_command("boarditems", uid, uname, dname,
                         details=f"reopened {item_id} due {new_due.isoformat()}")
    context.user_data.pop("bi_next_due_id", None)
    today = now_tz().date()
    note = ""
    if new_due < today:
        note = ("\n\n⚠️ That date is in the past, so this will notify every day "
                "from now. Edit the due date if that was not intended.")
    await reply_markdown(
        update.message,
        f"♻️ *{md(item.get('action'))}* is queued again for "
        f"{new_due.isoformat()} ({_when(item, today)}).{note}",
        reply_markup=item_keyboard(item))
    return BI_MENU


async def bi_skip_next_due(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("bi_next_due_id", None)
    await update.message.reply_text(
        "Left retired. Reopen it any time from /boarditems → Completed items.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /addboarditem
# ---------------------------------------------------------------------------

AB_ACTION, AB_DUE, AB_NOTIFY, AB_DESC, AB_URL, AB_CONFIRM = range(63, 69)


@board_officer_only
async def cmd_addboarditem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("addboarditem", uid, uname, dname)
    context.user_data.clear()
    await reply_markdown(
        update.message,
        "🗂 *Add a board item*\n\nWhat has to be done or renewed? "
        f"(max {bi.ACTION_MAX} characters)")
    return AB_ACTION


async def ab_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text or len(text) > bi.ACTION_MAX:
        await update.message.reply_text(f"Please send 1–{bi.ACTION_MAX} characters:")
        return AB_ACTION
    context.user_data["ab_action"] = text
    await update.message.reply_text("Due date (YYYY-MM-DD):")
    return AB_DUE


async def ab_due(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed = bi.parse_date((update.message.text or "").strip())
    if parsed is None:
        await update.message.reply_text("Please use YYYY-MM-DD:")
        return AB_DUE
    context.user_data["ab_due"] = parsed.isoformat()
    # A past date is accepted on purpose: an obligation already overdue is
    # exactly the kind of thing the board needs on this list. Say what it will
    # do, rather than refusing it.
    if parsed < now_tz().date():
        await update.message.reply_text(
            "⚠️ Noted — that date has passed, so this will notify every day "
            "until it is marked completed.")
    await reply_markdown(
        update.message,
        "How many days ahead should the first notice go out?\n\n"
        f"Send a number, or /skip for the usual {bi.NOTIFY_DAYS_DEFAULT}. "
        f"After the first notice it repeats weekly, then every day once "
        f"inside {bi.DAILY_WINDOW_DAYS} days.")
    return AB_NOTIFY


async def ab_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value, error = _validate("notify_days", (update.message.text or "").strip())
    if error:
        await update.message.reply_text(error)
        return AB_NOTIFY
    context.user_data["ab_notify"] = value
    return await _prompt_description(update)


async def ab_skip_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["ab_notify"] = bi.NOTIFY_DAYS_DEFAULT
    return await _prompt_description(update)


async def _prompt_description(update: Update) -> int:
    await update.message.reply_text(
        "Details — registration numbers, who files it, what it costs. "
        "Send /skip for none:")
    return AB_DESC


async def ab_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value, error = _validate("description", (update.message.text or "").strip())
    if error:
        await update.message.reply_text(error)
        return AB_DESC
    context.user_data["ab_desc"] = value
    return await _prompt_url(update)


async def ab_skip_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["ab_desc"] = ""
    return await _prompt_url(update)


async def _prompt_url(update: Update) -> int:
    await update.message.reply_text(
        "A link for this item — the renewal portal or the filing page — and "
        "every notice gets a tappable button. Send /skip for none:")
    return AB_URL


async def ab_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value, error = _validate("url", (update.message.text or "").strip())
    if error:
        # Validated here rather than at send time: Telegram rejects the ENTIRE
        # message when a button's url is unacceptable, so a bad one would cost
        # the whole notice — months later, with nobody watching.
        await update.message.reply_text(error)
        return AB_URL
    context.user_data["ab_url"] = value
    return await _prompt_confirm(update, context)


async def ab_skip_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["ab_url"] = ""
    return await _prompt_confirm(update, context)


def _draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return {
        "action": context.user_data.get("ab_action", ""),
        "due": context.user_data.get("ab_due", ""),
        "notify_days": context.user_data.get("ab_notify", bi.NOTIFY_DAYS_DEFAULT),
        "description": context.user_data.get("ab_desc", ""),
        "url": context.user_data.get("ab_url", ""),
        "status": bi.STATUS_PENDING,
    }


async def _prompt_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    today = now_tz().date()
    await reply_markdown(
        update.message,
        f"{render_item(_draft(context), today)}\n\nSave this item? (yes/no)")
    return AB_CONFIRM


async def ab_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    if not _is_affirmative(update.message.text):
        await update.message.reply_text("Discarded — nothing was saved.")
        return ConversationHandler.END

    item = {**_draft(context), "id": bi.new_id(),
            "created": now_tz().date().isoformat(), "created_by": dname,
            "history": []}
    items = await storage.get_board_items()
    items.append(item)
    await storage.save_board_items(items)
    activity.log_command("addboarditem", uid, uname, dname,
                         details=f"created {item['id']} due {item['due']}")
    today = now_tz().date()
    when = _when_notices_start(item, today)
    await reply_markdown(
        update.message,
        f"✅ Saved *{md(item['action'])}* (`{item['id']}`).\n\n{when}\n\n"
        "/boarditems to see the queue.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /boardcalendar
# ---------------------------------------------------------------------------


@board_officer_only
async def cmd_boardcalendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the pending items as an .ics file the officer can import.

    Pending only. A completed item has no deadline left to warn about, and
    exporting it would put a dead entry in the officer's calendar — the entry
    they already have simply stops being updated, which is right.
    """
    uid, uname, dname = user_info(update)
    activity.log_command("boardcalendar", uid, uname, dname)
    items = bi.pending(await storage.get_board_items())
    datable = [i for i in items if bi.due_date(i) is not None]
    if not datable:
        await update.message.reply_text(
            "Nothing pending to put on a calendar. /addboarditem to add one.")
        return

    ics_bytes = board_items_to_ics(datable, calendar_name=BOARD_CALENDAR_NAME)
    bio = io.BytesIO(ics_bytes)
    bio.name = BOARD_CALENDAR_FILENAME
    await update.message.reply_document(
        document=InputFile(bio, filename=BOARD_CALENDAR_FILENAME),
        caption=(f"🏛 {len(datable)} pending board deadline"
                 f"{'s' if len(datable) != 1 else ''}, as all-day entries with "
                 "reminders. Re-import after a change and your calendar "
                 "updates in place rather than duplicating."),
    )


# ---------------------------------------------------------------------------
# The daily notice
# ---------------------------------------------------------------------------


def render_notice(item: dict[str, Any], today: date) -> str:
    """The message a board officer receives about one item.

    Leads with the deadline, not the action: an officer reading four of these
    at 6am is deciding what is urgent, and the date is what decides it.
    """
    due = bi.due_date(item)
    lines = [
        "🏛 *Board deadline*\n",
        f"*{md(item.get('action'))}*\n",
        f"Due {due.isoformat() if due else '?'} — {_when(item, today)}",
    ]
    if item.get("description"):
        lines.append(f"\n{md(item.get('description'))}")
    url = is_button_url(item.get("url"))
    if item.get("url") and not url:
        lines.append(f"\nLink: {md(item.get('url'))}")
    lines.append("\n_Mark it done with /boarditems._")
    return "\n".join(lines)


def notice_markup(item: dict[str, Any]) -> InlineKeyboardMarkup | None:
    """A tappable button for the item's link, or None if it has none.

    Built only from a url that passed is_button_url. Telegram rejects the
    ENTIRE message over an unacceptable button url, so a notice carrying a
    bad one would simply never arrive.
    """
    url = is_button_url(item.get("url"))
    return None if not url else InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔗 Open", url=url)]])


async def _deliver_notice(bot, chat_ids: list[int], item: dict[str, Any],
                          today: date) -> int:
    """Send one item's notice to every officer. Returns how many arrived.

    One officer blocking the bot, or deleting the chat, must not cost the other
    three their notice — so every send is isolated. The count is what decides
    whether the item is marked as notified.
    """
    text = render_notice(item, today)
    markup = notice_markup(item)
    delivered = 0
    for chat_id in chat_ids:
        try:
            try:
                await send_markdown(bot, chat_id, text, reply_markup=markup)
            except TelegramError:
                if markup is None:
                    raise
                # The button was refused (a url Telegram dislikes despite
                # passing our check). The deadline matters more than the
                # button, so resend without it.
                logger.warning("Board notice button rejected for chat %s; "
                               "resending without it.", chat_id)
                await send_markdown(bot, chat_id, text)
            delivered += 1
        except TelegramError as exc:
            logger.warning("Board notice to %s failed: %s", chat_id, exc)
    return delivered


async def board_notice_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily at 6am: notify board officers about every item that is due a notice.

    The schedule itself lives in board_items.notice_due(). This function's only
    judgements are about delivery.

    **An item is marked notified only if at least one officer received it.** If
    every send fails — Telegram unreachable, which has happened here for 87
    minutes — the item keeps its old `last_notified` and is tried again
    tomorrow, rather than being recorded as delivered and going quiet. The cost
    of the other choice is a notice silently lost; the cost of this one is a
    possible duplicate, which is the right way round for a legal deadline.
    """
    items = await storage.get_board_items()
    if not items:
        return
    today = now_tz().date()
    due_now = [i for i in bi.pending(items) if bi.notice_due(i, today)]
    if not due_now:
        return

    chat_ids = await permissions.board_officer_chat_ids()
    if not chat_ids:
        # Nothing can be delivered, so nothing is marked. This is a real
        # configuration fault — obligations are being tracked and nobody is
        # being told — so it is logged at WARNING, where /stats and the ops
        # error path will surface it.
        logger.warning("%d board item(s) are due a notice but no user has "
                       "board_officer: true. Set it with /setboardofficer.",
                       len(due_now))
        return

    sent = 0
    for item in due_now:
        delivered = await _deliver_notice(context.bot, chat_ids, item, today)
        if delivered:
            bi.mark_notified(item, today)
            sent += 1
        else:
            logger.warning("Board notice for %s reached nobody; will retry "
                           "tomorrow.", item.get("id"))
    if sent:
        # Saved once for the whole run, not per item: the file is the same
        # document and a save per item would multiply writes for no gain.
        await storage.save_board_items(items)
    activity.log_command("board_notices", 0, None, "system",
                         details=f"{sent}/{len(due_now)} item(s) to "
                                 f"{len(chat_ids)} officer(s)")
    logger.info("Board notices: %d of %d due item(s) delivered to %d officer(s).",
                sent, len(due_now), len(chat_ids))


async def purge_deleted_board_items() -> int:
    """Drop soft-deleted items past their retention window. Called daily."""
    items = await storage.get_board_items()
    keep = bi.purge_deleted(items, now_tz().date())
    removed = len(items) - len(keep)
    if removed:
        await storage.save_board_items(keep)
        logger.info("Purged %d board item(s) deleted more than %d days ago.",
                    removed, bi.DELETED_PURGE_AFTER_DAYS)
    return removed


# ---------------------------------------------------------------------------
# /setboardofficer
# ---------------------------------------------------------------------------


def _officer_keyboard(users: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for user in users[:OFFICER_PICKER_LIMIT]:
        mark = "✅ " if user.get("board_officer") is True else "▫️ "
        name = _short(user.get("display_name") or user.get("username")
                      or user.get("chat_id"), 50)
        rows.append([InlineKeyboardButton(
            mark + name,
            callback_data=f"{CB_BOFF_PREFIX}toggle:{user.get('chat_id')}")])
    rows.append([InlineKeyboardButton("Done", callback_data=f"{CB_BOFF_PREFIX}done")])
    return InlineKeyboardMarkup(rows)


def _sorted_users(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(users, key=lambda u: str(u.get("display_name")
                                           or u.get("username") or "").lower())


def _officer_prompt(users: list[dict[str, Any]]) -> str:
    text = ("🏛 *Board officers*\n\nTap a name to turn the flag on or off. "
            "Officers can see and manage board items; nobody else can.")
    if len(users) > OFFICER_PICKER_LIMIT:
        text += (f"\n\n⚠️ Showing the first {OFFICER_PICKER_LIMIT} of "
                 f"{len(users)} registered users.")
    return text


@admin_only
async def cmd_setboardofficer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle the board_officer flag. ADMIN-gated, deliberately — see module docstring."""
    uid, uname, dname = user_info(update)
    activity.log_command("setboardofficer", uid, uname, dname)
    users = _sorted_users(await storage.get_all_users())
    if not users:
        await update.message.reply_text("No registered users yet.")
        return
    await reply_markdown(update.message, _officer_prompt(users),
                         reply_markup=_officer_keyboard(users))


async def board_officer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _answer_cb(query)
    if not permissions.is_admin(update):
        await edit_markdown(query, "⛔ Unknown command.")
        return
    uid, uname, dname = user_info(update)
    action = query.data[len(CB_BOFF_PREFIX):]

    if action == "done":
        names = await permissions.board_officer_names()
        listed = "\n".join(f"• {md(n)}" for n in sorted(names)) or "_nobody_"
        await edit_markdown(query, f"🏛 *Board officers*\n\n{listed}")
        return

    if not action.startswith("toggle:"):
        return
    try:
        target = int(action.split(":", 1)[1])
    except ValueError:
        return

    users = await storage.get_all_users()
    record = next((u for u in users if u.get("chat_id") == target), None)
    if record is None:
        return
    # Written as a real boolean, never a string: is_board_officer() checks
    # `is True`, so a "true" stored here would read as not-an-officer.
    now_officer = record.get("board_officer") is not True
    if now_officer:
        record["board_officer"] = True
    else:
        # Removed rather than set to False, so a user record carries the flag
        # only while it means something.
        record.pop("board_officer", None)
    await storage.save_users(users)
    activity.log_command("setboardofficer", uid, uname, dname,
                         details=f"{target} board_officer="
                                 f"{'on' if now_officer else 'off'}")
    await query.edit_message_reply_markup(
        reply_markup=_officer_keyboard(_sorted_users(users)))


# ---------------------------------------------------------------------------
# Conversation assembly
# ---------------------------------------------------------------------------
#
# Built here rather than in bot.main(). See handlers/events_admin.py for why
# these are functions and not module-level constants, and why allow_reentry is
# set on both.


def _skippable(skip_callback, value_callback) -> list:
    """An optional step: /skip to leave it unset, or type a value.

    The /skip CommandHandler must come FIRST. The message handler is
    TEXT & ~COMMAND so it would not match /skip anyway, but relying on that
    puts the behaviour of this step in the filter rather than in the order,
    and the order is what reads correctly.
    """
    return [
        CommandHandler("skip", skip_callback),
        MessageHandler(filters.TEXT & ~filters.COMMAND, value_callback),
    ]


def board_items_conversation() -> ConversationHandler:
    """/boarditems — the pending queue, with drill-down and edit actions.

    The callback router is an ENTRY POINT as well as a state handler.
    user_data does not survive a restart and every deploy restarts the bot, so
    without this a keyboard already on an officer's screen would go dead:
    tapping it would do nothing at all — no error, no reply. As an entry point
    the tap re-enters the conversation at the right place instead.
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("boarditems", cmd_boarditems),
            CallbackQueryHandler(board_callback,
                                 pattern=f"^{re.escape(CB_BOARD_PREFIX)}"),
        ],
        states={
            BI_MENU: [CallbackQueryHandler(
                board_callback, pattern=f"^{re.escape(CB_BOARD_PREFIX)}")],
            BI_EDIT_VALUE: _skippable(bi_skip_edit, bi_edit_value),
            BI_NEXT_DUE: _skippable(bi_skip_next_due, bi_next_due),
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=True,
    )


def add_board_item_conversation() -> ConversationHandler:
    """/addboarditem — action and due date are required; the rest are optional.

    Notify-days, description and URL are each skippable, so an officer can add
    a bare deadline in two answers and fill the detail in later via /boarditems.
    """
    return ConversationHandler(
        entry_points=[CommandHandler("addboarditem", cmd_addboarditem)],
        states={
            AB_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ab_action)],
            AB_DUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ab_due)],
            AB_NOTIFY: _skippable(ab_skip_notify, ab_notify),
            AB_DESC: _skippable(ab_skip_desc, ab_desc),
            AB_URL: _skippable(ab_skip_url, ab_url),
            AB_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ab_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=True,
    )

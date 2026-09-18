"""Shared primitives used across every feature area.

The bottom of the dependency graph: depends only on settings, storage, and
localization — never on feature modules — so anything may import it freely.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pytz
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.helpers import escape_markdown

from localization import CATALOG, DEFAULT_LANG, localized_datetime
from settings import TZ
import storage

logger = logging.getLogger(__name__)


def now_tz() -> datetime:
    return datetime.now(TZ)


def format_dt(dt: datetime, tz: pytz.BaseTzInfo | None = None, lang: str | None = None) -> str:
    return localized_datetime(dt.astimezone(tz or TZ), lang or DEFAULT_LANG)


def _coerce_tz(name: str | None) -> pytz.BaseTzInfo:
    """Return a pytz timezone for *name*, falling back to the church timezone."""
    if name:
        try:
            return pytz.timezone(name)
        except Exception:
            pass
    return TZ


def user_tz_of(record: dict | None) -> pytz.BaseTzInfo:
    """Timezone for a user record, defaulting to the configured church timezone."""
    return _coerce_tz((record or {}).get("timezone"))


def user_lang_of(record: dict | None) -> str:
    """Language code for a user record, defaulting to the catalog default."""
    lang = (record or {}).get("language")
    return lang if lang in CATALOG else DEFAULT_LANG


async def get_user_prefs(chat_id: int) -> tuple[pytz.BaseTzInfo, str]:
    """Return (timezone, language) preferences for a user (with safe defaults)."""
    users = await storage.get_all_users()
    record = next((u for u in users if u.get("chat_id") == chat_id), None)
    return user_tz_of(record), user_lang_of(record)


# ---------------------------------------------------------------------------
# Opt-in personal notification preferences (by event category)
# ---------------------------------------------------------------------------

# Category key -> catalog label key. Order defines the /notifications layout.
NOTIF_CATEGORIES: list[tuple[str, str]] = [
    ("convocations", "notif_cat_convocations"),   # Sabbath + the annual feasts
    ("sunday_prayer", "notif_cat_sunday_prayer"),
    ("special", "notif_cat_special"),
]
_NOTIF_CATEGORY_KEYS = {k for k, _ in NOTIF_CATEGORIES}


def _event_category(event: dict[str, Any]) -> str:
    """Classify an event into a notification category (see NOTIF_CATEGORIES)."""
    if event.get("type") == "convocation":
        return "convocations"
    if event.get("special_id") == "sunday_morning_prayer":
        return "sunday_prayer"
    return "special"


def user_notif_prefs(record: dict | None) -> set:
    """Set of event categories a user has opted into for personal reminders."""
    return {c for c in ((record or {}).get("notif_prefs") or []) if c in _NOTIF_CATEGORY_KEYS}


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


_AFFIRMATIVE_WORDS = {"yes", "y", "sí", "si", "s", "oui", "o"}


def _is_affirmative(text: str | None) -> bool:
    return (text or "").strip().lower() in _AFFIRMATIVE_WORDS


def md(value: Any) -> str:
    """Escape runtime text for Telegram's legacy Markdown parse mode.

    Any value authored by a user or admin — display names, appointment
    purposes, event/announcement titles — must pass through this before being
    interpolated into a Markdown message. An unescaped '_', '*', '`' or '['
    makes Telegram reject the whole message ("Can't parse entities"), so the
    message never arrives. See the 2026-07-12 /userlist incident.
    """
    return escape_markdown("" if value is None else str(value), version=1)


async def validate_markdown(message, text: str) -> bool:
    """Render *text* back to its author to prove Telegram accepts the markup.

    The counterpart to md(). Where escaping guarantees delivery at the cost of
    formatting, this keeps the formatting and moves the risk to a moment where
    a human can fix it: the author sees the result immediately and is asked to
    edit if a marker is unpaired. Telegram rejects an entire message over one
    stray marker, so learning that mid-broadcast is far worse than learning it
    at the prompt.

    Use it only for text the AUTHOR can re-send. Returns True when the markup
    is valid; on False the caller re-prompts.
    """
    try:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return True
    except BadRequest as exc:
        await message.reply_text(
            f"⚠️ I couldn't render that as Markdown ({exc.message}).\n\n"
            "Use *bold*, _italic_, `code` — each marker needs a matching pair. "
            "A literal _ or * in a link needs a backslash: pwd=aB\\_cD.\n\n"
            "Please edit and re-send:"
        )
        return False


#: Telegram's wording for a Markdown parse failure. Everything else a
#: BadRequest can mean — chat not found, message too long, bot blocked — is not
#: something a plain-text retry can fix.
_PARSE_ERROR_MARKERS = ("can't parse entities", "can't find end of the entity",
                        "unsupported start tag", "reserved and must be escaped")


def _is_parse_error(exc: BadRequest) -> bool:
    return any(m in str(exc).lower() for m in _PARSE_ERROR_MARKERS)


async def _markdown_or_plain(send, text: str, kwargs: dict, where: str):
    """Send *text* as Markdown; on rejection, send it again as plain text.

    The seatbelt beside md(). Escaping is the fix — this exists because
    escaping is per-call-site and easy to forget, and the failure mode when it
    IS forgotten is total: Telegram rejects the whole message over one unpaired
    marker and it never arrives, usually with nobody to see an error.

    Between a message that loses its bold and a message that is never
    delivered, the second is far worse. But the first is still a defect, which
    is why every fallback logs. A quiet fallback would hide the escaping bug it
    compensates for — if this warning appears, fix the escaping; do not treat
    the fallback as the answer.
    """
    try:
        return await send(text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except BadRequest as exc:
        if not _is_parse_error(exc):
            # Not a formatting problem, so plain text will not help and the
            # retry is a second doomed API call. Worse, it used to be logged as
            # "a value was interpolated unescaped", which sent the reader
            # looking for an escaping bug: on 2026-09-18 the real fault was a
            # placeholder chat id, and the log said nothing about it.
            raise
        logger.warning("Markdown rejected (%s): %s — resending as plain text. "
                       "A value was interpolated unescaped.", where, exc.message)
        return await send(text, **kwargs)


async def reply_markdown(message, text: str, **kwargs):
    """message.reply_text in Markdown, falling back to plain text."""
    return await _markdown_or_plain(
        lambda t, **kw: message.reply_text(t, **kw), text, kwargs, "reply")


async def send_markdown(bot, chat_id, text: str, **kwargs):
    """bot.send_message in Markdown, falling back to plain text."""
    return await _markdown_or_plain(
        lambda t, **kw: bot.send_message(chat_id, t, **kw), text, kwargs,
        f"send to {chat_id}")


async def edit_markdown(query, text: str, **kwargs):
    """query.edit_message_text in Markdown, falling back to plain text.

    Telegram also rejects an edit whose result is identical to the current
    text. That is not an escaping fault and there is nothing to retry, so it is
    swallowed rather than reported as one.
    """
    try:
        return await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return None
        if not _is_parse_error(exc):
            # An edit can fail for reasons no retry fixes — the message is too
            # old, or was deleted. Losing one edit must not take down the
            # handler around it, but it is not an escaping fault either.
            logger.warning("Edit failed (%s); not a formatting problem.", exc.message)
            return None
        logger.warning("Markdown rejected (edit): %s — resending as plain text. "
                       "A value was interpolated unescaped.", exc.message)
        try:
            return await query.edit_message_text(text, **kwargs)
        except BadRequest:
            return None

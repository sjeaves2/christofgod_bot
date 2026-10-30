"""Pieces every conversation needs, in a module that imports no feature code.

This exists to be a LEAF. Each feature module assembles its own
ConversationHandlers (see handlers/events_admin.py and friends), and every one
of them needs the shared /cancel fallback. Keeping that fallback in
handlers/user_basics.py made it unreachable for handlers/announcements.py,
because user_basics imports cmd_announcements from announcements — so an
announcements factory reaching back for it would close the cycle
announcements → user_basics → announcements.

Nothing here may import a handlers.* module. That rule is what keeps the
feature modules free to import this one.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from common import get_user_prefs
from localization import t
from permissions import user_info
from settings import activity


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Shared /cancel fallback for every ConversationHandler.

    Replaces `lambda u, c: ConversationHandler.END`, which looked harmless but
    never worked: PTB awaits every callback, and awaiting the integer END raises
    TypeError. So /cancel errored in all 15 conversations and — because the
    exception meant the state was never returned — left the user stuck in the
    conversation they were trying to leave. Several prompts tell people to use
    it, so this was a promise the bot could not keep.

    Clearing user_data matters too: it holds only conversation scratch here, and
    leaving a half-finished draft behind would leak it into the next one.
    """
    uid, uname, dname = user_info(update)
    _, lang = await get_user_prefs(uid)
    context.user_data.clear()
    activity.log_command("cancel", uid, uname, dname)
    await update.message.reply_text(t("action_cancelled", lang))
    return ConversationHandler.END

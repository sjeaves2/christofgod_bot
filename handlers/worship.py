"""/songlist — build a worship song list and send it to another user.

An admin types a song — name and, if known, artist, in ONE message — and picks
from Apple Music search results. Repeat until /end, confirm the list, choose a
recipient, send. Typically ten or eleven songs.

ONE PROMPT, NOT TWO
-------------------
The first version asked for the title and then the artist separately, on the
assumption that the search needed the two apart. It does not: the iTunes
Search API's `term` is a general search across title, artist and album, the
same as the Music app's search box. So the second prompt bought nothing and
cost a round trip per song. It was removed on 2026-11-03 along with the
title/artist concatenation it existed to feed.

ENGLISH ONLY, like the rest of the admin surface
------------------------------------------------
board.py and events_admin.py — the two admin/officer-facing modules — use no
localization at all, while member-facing flows (appointments, user_basics,
prayer) do. This follows them.

The one consequence worth naming: the message the RECIPIENT gets has an
English header, and a recipient may be an isiZulu speaker. Everything below
the header is data — an index, a title, "by", an artist, a link — so the
header is the whole of it. If that matters, it is one t() key away.

WHY A HANDLER PER BUTTON RATHER THAN ONE ROUTER
-----------------------------------------------
board_callback (133 lines) and appt_callback (165) are single routers that
switch on a verb, and both are on the list to be rewritten as dispatch tables
precisely because that shape got hard to follow. New code should not add a
third. Each button here gets its own small CallbackQueryHandler with its own
pattern, so PTB does the dispatch and the wiring states which tap is legal in
which state — the thing a router hides.

STALE BUTTONS AFTER A RESTART
-----------------------------
user_data does not survive a restart and every deploy restarts the bot, so a
keyboard already on an admin's screen goes dead. A broad `^sl:` handler answers
those taps instead of leaving them silent — but it is registered in bot.py
AFTER this conversation, OUTSIDE it, and must never be moved into it as an
entry point. board_items_conversation does exactly that and is fine; copying it
here caused a production bug, because with allow_reentry=True PTB checks entry
points BEFORE state handlers, so the broad pattern swallowed every live tap.
See song_list_conversation() and stale_button_handler() for the full reason.

A part-built list is NOT persisted. That was a deliberate choice: the
alternative is a tenth YAML store to create, gitignore, keep out of the backup
globs and garbage-collect, for something a restart interrupts a few times a
year. The cost is a lost list, so the first message says so plainly.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import apple_music
import storage
from apple_music import Song
from handlers.shared import cancel_conversation
from permissions import admin_only, user_info
from settings import activity
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
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

logger = logging.getLogger(__name__)

SL_QUERY, SL_PICK, SL_CONFIRM, SL_REMOVE, SL_RECIPIENT = range(5)

CB_SONG_PREFIX = "sl:"

#: Namespace inside user_data, so this conversation cannot tread on another's.
STATE_KEY = "songlist"

# Eleven links would otherwise draw eleven link previews, burying the list.
#
# This is the CURRENT way to do it. The old boolean keyword was REMOVED in
# python-telegram-bot 22.0, so passing it raises TypeError at call time — the
# first draft of this module used it, and tests/test_dependency_pins.py
# rejected it before anything ran. That test matches on source TEXT, so the
# retired keyword is not spelled out anywhere here, not even in a comment.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Telegram rejects a message over 4096 characters. A long entry runs to about
# 170 (title, artist, a music.apple.com link, the <b> tags, a blank line), so
# twenty-four songs would exceed it. The list is split instead of capped:
# refusing a twenty-fifth song would be an arbitrary limit, while a rejected
# send would lose the whole list at the last step.
#
# The margin is larger than it looks: Telegram counts the message AFTER
# parsing, so the <b></b> tags do not count against 4096 while they do count
# here. Measuring the markup is the conservative direction, so it is left as
# it is rather than made clever.
MAX_MESSAGE_CHARS = 3900


def _state(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return context.user_data.setdefault(STATE_KEY, {"songs": []})


def _songs(context: ContextTypes.DEFAULT_TYPE) -> list[Song]:
    return _state(context)["songs"]


def render_list(songs: list[Song]) -> str:
    """The list as the recipient sees it, exactly as specified.

    HTML, not plain text — Song.enumerated bolds the title. Every caller must
    send with parse_mode=ParseMode.HTML.

    One blank line between entries, because without it the link of one song
    and the title of the next form a solid block that is hard to read on a
    phone. That separator is also what keeps the markup safe under splitting:
    split_for_telegram breaks BETWEEN entries, so a <b> can never be parted
    from its </b>.
    """
    return "\n\n".join(song.enumerated(n) for n, song in enumerate(songs, 1))


def split_for_telegram(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """*text* as one or more messages, each within Telegram's size limit.

    Split on the blank line between entries, so a song and its link never land
    in different messages — a link without its title is useless.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for entry in text.split("\n\n"):
        candidate = f"{current}\n\n{entry}" if current else entry
        if current and len(candidate) > limit:
            chunks.append(current)
            current = entry
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def results_keyboard(results: list[Song]) -> InlineKeyboardMarkup:
    """One button per candidate, plus a way out when none of them is right.

    The album is in the label because it is sometimes the ONLY difference: a
    search for "Great Is Thy Faithfulness" returns CeCe Winans twice, from
    The Hymns and from Alone in His Presence.
    """
    rows = [
        [InlineKeyboardButton(_result_label(song),
                              callback_data=f"{CB_SONG_PREFIX}pick:{n}")]
        for n, song in enumerate(results)
    ]
    rows.append([InlineKeyboardButton("🔁 None of these — search again",
                                      callback_data=f"{CB_SONG_PREFIX}none")])
    rows.append([InlineKeyboardButton("✖ Cancel the whole list",
                                      callback_data=f"{CB_SONG_PREFIX}cancel")])
    return InlineKeyboardMarkup(rows)


def _result_label(song: Song) -> str:
    label = f"{song.title} — {song.artist}"
    if song.album:
        label = f"{label} ({song.album})"
    # Telegram truncates a long button label mid-word with no ellipsis, which
    # reads like a corrupted entry; trim it deliberately instead.
    return label if len(label) <= 60 else label[:57] + "…"


def confirm_keyboard(count: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("✅ Send this list",
                                  callback_data=f"{CB_SONG_PREFIX}send")]]
    if count:
        rows.append([InlineKeyboardButton("🗑 Remove a song",
                                          callback_data=f"{CB_SONG_PREFIX}remove")])
    rows.append([InlineKeyboardButton("✖ Cancel",
                                      callback_data=f"{CB_SONG_PREFIX}cancel")])
    return InlineKeyboardMarkup(rows)


def remove_keyboard(songs: list[Song]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{n}. {_result_label(song)}",
                              callback_data=f"{CB_SONG_PREFIX}rm:{n - 1}")]
        for n, song in enumerate(songs, 1)
    ]
    rows.append([InlineKeyboardButton("⬅ Back",
                                      callback_data=f"{CB_SONG_PREFIX}back")])
    return InlineKeyboardMarkup(rows)


async def recipient_keyboard() -> InlineKeyboardMarkup | None:
    """Registered users, or None when there is nobody to send to.

    A bot cannot message someone by username — it needs a chat_id, which only
    exists once that person has used the bot. So the recipient is chosen from
    data/users.yaml rather than typed, and a typo cannot silently send the
    list nowhere.
    """
    seen: set[int] = set()
    rows = []
    for user in sorted(await storage.get_all_users(),
                       key=lambda u: (u.get("display_name") or "").lower()):
        chat_id = user.get("chat_id")
        if not chat_id or chat_id in seen:
            continue
        seen.add(chat_id)
        label = user.get("display_name") or str(chat_id)
        rows.append([InlineKeyboardButton(
            label, callback_data=f"{CB_SONG_PREFIX}to:{chat_id}")])
    if not rows:
        return None
    rows.append([InlineKeyboardButton("✖ Cancel",
                                      callback_data=f"{CB_SONG_PREFIX}cancel")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Building the list
# ---------------------------------------------------------------------------

@admin_only
async def cmd_songlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid, uname, dname = user_info(update)
    activity.log_command("songlist", uid, uname, dname, details="started")
    context.user_data[STATE_KEY] = {"songs": []}
    await update.message.reply_text(
        "🎵 Building a worship song list.\n\n"
        "Send me the first song — the name, and the artist too if you know "
        "it, in one message. For example:\n\n"
        "    Amazing Grace CeCe Winans\n\n"
        "The artist is optional, but it helps: a hymn is often recorded by "
        "several people. I will look it up on Apple Music and show you what "
        "I find.\n\n"
        "/end when the list is complete\n"
        "/undo to drop the last song\n"
        "/cancel to abandon the list\n\n"
        "Note: if the bot restarts while you are building the list, the list "
        "is lost and you will need to start again."
    )
    return SL_QUERY


async def sl_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Search on whatever the admin typed, and offer the matches.

    One prompt, not two. The Search API's `term` is a general search over
    title, artist and album together — the same thing the Music app's own
    search box does — so "Amazing Grace CeCe Winans" needs no splitting, no
    rejoining, and no second question. See apple_music.search_songs.

    The example in cmd_songlist's prompt is therefore load-bearing: it shows
    the song and artist side by side with NO "by" between them, because the
    API scores "by" as a search term like any other and it measurably moves
    the ranking.
    """
    query = (update.message.text or "").strip()
    if not query:
        await update.message.reply_text("Send me a song name, or /end.")
        return SL_QUERY

    results = await apple_music.search_songs(query)
    if not results:
        # search_songs never raises and returns [] for "nothing found" and for
        # "search is broken" alike, so this message must cover both without
        # claiming to know which it was.
        await update.message.reply_text(
            f"🔍 I could not find anything for “{query}”.\n\n"
            "Check the spelling and send it again, or /end to finish the list."
        )
        return SL_QUERY

    _state(context)["results"] = results
    await update.message.reply_text(
        f"Found {len(results)} match(es) for “{query}”. Which one?",
        reply_markup=results_keyboard(results),
    )
    return SL_PICK


async def sl_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    state = _state(context)
    results: list[Song] = state.get("results") or []
    index = int(query.data.rsplit(":", 1)[1])
    if index >= len(results):
        # Only reachable from a keyboard older than the current search.
        await query.edit_message_text(
            "That result is no longer on offer. Send the song name again.")
        return SL_QUERY
    song = results[index]
    state["songs"].append(song)
    state.pop("results", None)
    count = len(state["songs"])
    await query.edit_message_text(
        f"✅ Added {count}. {song.title} by {song.artist}\n\n"
        "Send the next song name, /undo to drop this one, or /end to finish."
    )
    return SL_QUERY


async def sl_search_again(update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _state(context).pop("results", None)
    await query.edit_message_text(
        "No problem. Send the song name again — adding the artist, or part of "
        "the title, usually helps."
    )
    return SL_QUERY


async def sl_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    songs = _songs(context)
    if not songs:
        await update.message.reply_text(
            "Nothing to undo — the list is empty. Send a song name, or /cancel."
        )
        return SL_QUERY
    dropped = songs.pop()
    await update.message.reply_text(
        f"↩️ Removed “{dropped.title}” by {dropped.artist}. "
        f"{len(songs)} song(s) left.\n\nSend the next song name, or /end."
    )
    return SL_QUERY


async def sl_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    songs = _songs(context)
    if not songs:
        await update.message.reply_text(
            "The list is empty, so there is nothing to send. "
            "Start again with /songlist when you are ready."
        )
        context.user_data.pop(STATE_KEY, None)
        return ConversationHandler.END
    await _show_confirmation(update.message.reply_text, songs)
    return SL_CONFIRM


async def _show_confirmation(send, songs: list[Song]) -> None:
    """Show the finished list exactly as the recipient will receive it.

    Deliberately the same renderer: a confirmation that formats the list
    differently from the real message confirms something the recipient will
    never see.

    Which is why parse_mode here must stay in step with sl_send's. Rendering
    the preview as plain text would show the admin the raw <b> tags and have
    them approve something nobody receives; rendering it as HTML while the
    real send is plain would do the reverse. The two are checked together in
    tests/test_worship_songlist.py.
    """
    body = render_list(songs)
    header = f"📋 {len(songs)} song(s) — this is exactly what will be sent:\n\n"
    chunks = split_for_telegram(body)
    for n, chunk in enumerate(chunks):
        text = header + chunk if n == 0 else chunk
        is_last = n == len(chunks) - 1
        await send(text,
                   reply_markup=confirm_keyboard(len(songs)) if is_last else None,
                   parse_mode=ParseMode.HTML,
                   link_preview_options=NO_PREVIEW)


# ---------------------------------------------------------------------------
# Confirming, editing, sending
# ---------------------------------------------------------------------------

async def sl_remove_menu(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    songs = _songs(context)
    if not songs:
        await query.edit_message_text("The list is empty. /songlist to start again.")
        return ConversationHandler.END
    await query.edit_message_text("Which song should come off the list?",
                                  reply_markup=remove_keyboard(songs))
    return SL_REMOVE


async def sl_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    songs = _songs(context)
    index = int(query.data.rsplit(":", 1)[1])
    if index >= len(songs):
        await query.edit_message_text("That song is no longer on the list.")
        return await _back_to_confirm(query, songs)
    dropped = songs.pop(index)
    await query.edit_message_text(
        f"🗑 Removed “{dropped.title}” by {dropped.artist}.")
    return await _back_to_confirm(query, songs)


async def sl_back_to_confirm(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await _back_to_confirm(query, _songs(context))


async def _back_to_confirm(query, songs: list[Song]) -> int:
    if not songs:
        await query.message.reply_text(
            "Every song has been removed, so there is nothing to send. "
            "/songlist to start again."
        )
        return ConversationHandler.END
    await _show_confirmation(query.message.reply_text, songs)
    return SL_CONFIRM


async def sl_choose_recipient(update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not _songs(context):
        await query.edit_message_text("The list is empty. /songlist to start again.")
        return ConversationHandler.END
    keyboard = await recipient_keyboard()
    if keyboard is None:
        await query.edit_message_text(
            "⚠️ There is nobody to send this to: the bot has no registered "
            "users with a chat it can open. A recipient has to have used the "
            "bot at least once."
        )
        return ConversationHandler.END
    await query.edit_message_text("Who should receive the list?",
                                  reply_markup=keyboard)
    return SL_RECIPIENT


async def sl_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    songs = _songs(context)
    chat_id = int(query.data.rsplit(":", 1)[1])
    uid, uname, dname = user_info(update)

    body = render_list(songs)
    header = f"🎵 Worship song list — {len(songs)} song(s)\n\n"
    chunks = split_for_telegram(body)
    sent = 0
    for n, chunk in enumerate(chunks):
        text = header + chunk if n == 0 else chunk
        try:
            # HTML, not Markdown: Song.enumerated bolds the title and escapes
            # the three characters HTML mode cares about. Markdown would need
            # eighteen escaped, most of them common in song titles.
            await context.bot.send_message(chat_id, text,
                                           parse_mode=ParseMode.HTML,
                                           link_preview_options=NO_PREVIEW)
            sent += 1
        except TelegramError as exc:
            logger.warning("Could not send song list to %s: %s", chat_id, exc)
            await query.edit_message_text(
                f"⚠️ The list could not be delivered: {exc}\n\n"
                + ("Nothing was sent." if sent == 0 else
                   f"{sent} of {len(chunks)} message(s) went through, so the "
                   "recipient has a partial list.")
                + "\n\nThe most likely cause is that the recipient has "
                  "blocked the bot or never started a chat with it."
            )
            activity.log_error(
                f"Song list to {chat_id} failed after {sent} message(s): {exc}")
            context.user_data.pop(STATE_KEY, None)
            return ConversationHandler.END

    activity.log_command("songlist", uid, uname, dname,
                         details=f"{len(songs)} song(s) sent to {chat_id}")
    await query.edit_message_text(
        f"✅ Sent {len(songs)} song(s)."
        + ("" if len(chunks) == 1 else f" ({len(chunks)} messages.)")
    )
    context.user_data.pop(STATE_KEY, None)
    return ConversationHandler.END


async def sl_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop(STATE_KEY, None)
    await query.edit_message_text("Abandoned. Nothing was sent.")
    return ConversationHandler.END


async def sl_stale_button(update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> int:
    """A tap the conversation itself declined.

    Reached only when song_list_conversation() does NOT handle the update,
    which covers two cases that look identical from here:

      * the bot restarted, so the list is gone (user_data does not survive);
      * a list IS in progress and this is an OLDER keyboard, from a step the
        conversation has already moved past.

    The wording must be true in both. It deliberately does not say "the bot
    restarted", which was wrong half the time, and does not tell an admin
    mid-list to start again, which would throw away work.

    Registered OUTSIDE the conversation, in bot.py, AFTER it. It cannot be an
    entry point: with allow_reentry=True, PTB checks entry points BEFORE state
    handlers, so a broad `^sl:` entry point hijacks every live tap. That was a
    real production bug — the admin tapped a song and was told the bot had
    restarted.
    """
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "⚠️ Those buttons are out of date.\n\n"
        "If you are still building a list, carry on in the most recent "
        "message. Otherwise /songlist starts a new one — note that a "
        "part-built list is not saved across a restart."
    )
    return ConversationHandler.END


def _cb(callback, verb: str) -> CallbackQueryHandler:
    """A handler for one exact button, anchored at BOTH ends.

    The trailing `$` is the point. Without it a verb that is a prefix of
    another verb swallows it: `^sl:cancel` matches `sl:cancelall`, and
    `^sl:send` matches `sl:sendall`. No such pair exists today, which is
    exactly why the next person to add a verb would not think about it — and
    PTB runs only the first matching handler, so the symptom would be a button
    that does the wrong thing rather than an error.

    (`sl:rm:` and `sl:remove` are NOT such a pair, despite looking like one:
    they diverge at the third character, `sl:rm` against `sl:re`. An earlier
    version of this docstring claimed otherwise.)
    """
    return CallbackQueryHandler(
        callback, pattern=f"^{re.escape(CB_SONG_PREFIX + verb)}$")


def _cb_indexed(callback, verb: str) -> CallbackQueryHandler:
    """A handler for a button carrying an index, e.g. `sl:pick:3`."""
    return CallbackQueryHandler(
        callback, pattern=f"^{re.escape(CB_SONG_PREFIX + verb + ':')}\\d+$")


def stale_button_handler() -> CallbackQueryHandler:
    """Replies to any `sl:` tap the conversation did not handle.

    MUST be registered AFTER song_list_conversation(). Before it, it would
    shadow every live tap — the same symptom as the entry-point bug, by a
    different route.
    """
    return CallbackQueryHandler(
        sl_stale_button, pattern=f"^{re.escape(CB_SONG_PREFIX)}")


def song_list_conversation() -> ConversationHandler:
    """/songlist — compile a worship list and send it to a registered user.

    The ONLY entry point is the command. A CallbackQueryHandler must never be
    added here: with allow_reentry=True, PTB checks entry points BEFORE the
    state handlers —

        if state is None or self.allow_reentry:
            for entry_point in self.entry_points:     # first
        ...
        for candidate in self.states.get(state, []):  # only after

    — so a broad `^sl:` entry point swallows every live tap. It did: the admin
    tapped a song and was told the bot had restarted.

    board_items_conversation does have a CallbackQueryHandler entry point and
    is fine, but only because its entry-point callback IS its state callback,
    so whichever branch wins the same function runs. That identity is the
    load-bearing part of that pattern and it does not survive being copied to
    a conversation with per-button handlers.

    Dead taps are handled by stale_button_handler(), registered after this
    conversation in bot.py, where the conversation declining an update lets it
    fall through — which is what PTB's per-group ordering is for.
    """
    return ConversationHandler(
        entry_points=[CommandHandler("songlist", cmd_songlist)],
        states={
            SL_QUERY: [
                CommandHandler("end", sl_end),
                CommandHandler("undo", sl_undo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sl_query),
            ],
            SL_PICK: [
                _cb_indexed(sl_pick, "pick"),
                _cb(sl_search_again, "none"),
                _cb(sl_cancel, "cancel"),
            ],
            SL_CONFIRM: [
                _cb(sl_choose_recipient, "send"),
                _cb(sl_remove_menu, "remove"),
                _cb(sl_cancel, "cancel"),
            ],
            SL_REMOVE: [
                _cb_indexed(sl_remove, "rm"),
                _cb(sl_back_to_confirm, "back"),
                _cb(sl_cancel, "cancel"),
            ],
            SL_RECIPIENT: [
                _cb_indexed(sl_send, "to"),
                _cb(sl_cancel, "cancel"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=True,
    )

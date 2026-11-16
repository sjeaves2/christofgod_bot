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

#: Message ids of the preview clips sent so far, inside the conversation state.
#:
#: They accumulate across searches and are cleared when the list is sent or
#: abandoned, not when a song is picked — the admin asked for it that way, so
#: a clip stays available for comparison while the list is still being built.
#: A list that previews several songs can therefore hold a few dozen ids,
#: which is why the delete is bulk.
PREVIEW_IDS_KEY = "preview_message_ids"

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

# How long Apple's preview clips run. Sent to Telegram as the audio duration so
# the player draws a scrubber the right length; the full track time would draw
# a four-minute bar over a thirty-second file.
#
# A LABEL, NOT A LIMIT. This does not fetch, trim or truncate anything — it
# describes a file Telegram downloads whole and plays whole. Lowering it does
# not shorten the clip, it only makes the player misreport a file it will
# still play to the end. It was briefly set to 15 on 2026-11-16 in the belief
# that it capped playback; it does not, and there is no parameter that does.
#
# Apple serves a fixed clip and offers no way to ask for a shorter one, so
# genuinely delivering fifteen seconds would mean the BOT downloading each
# clip, trimming it with a tool the server does not have, and uploading the
# bytes — which would also forfeit this path's immunity to the AWS-IP failure
# described in _send_previews. Nobody has to listen for thirty seconds; the
# listener stops whenever they have recognised it.
#
# Apple does not promise the length and does not report it in the API — it is
# a property of the m4a. Measured from the container's mvhd box on 2026-11-16,
# all five hits for "Amazing Grace" ran 30.02s (949-1050 KB). Rounded down to
# 30 because the duration field is whole seconds. If it is ever wrong the
# scrubber is wrong and nothing else is, which is why a value is hardcoded
# rather than the file downloaded to measure it.
PREVIEW_SECONDS = 30


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

def render_candidates(results: list[Song]) -> str:
    """The candidates as a numbered block, each with what tells it apart.

    HTML, like render_list — Song.detail bolds the title and escapes for HTML.

    This exists because the BUTTON is the wrong place for this. Telegram
    truncates a long label (see _result_label), and 60 characters cannot hold
    a title, an artist, an album, a running time and a year — "Great Is Thy
    Faithfulness — CeCe Winans (Alone in His Presence)" is 50 on its own. In
    the message body there is no such limit, so the facts go here and the
    buttons shrink to their numbers.
    """
    return "\n\n".join(song.detail(n) for n, song in enumerate(results, 1))


def results_keyboard(results: list[Song]) -> InlineKeyboardMarkup:
    """A numbered button per candidate, plus a way out when none is right.

    Numbers, not labels: render_candidates carries the identifying detail in
    the message body, where it is not truncated. The buttons are packed onto
    one row because five "3"s side by side are easier to hit than five
    full-width rows, and the number is all the target needs to be.

    The preview row is only offered when at least one candidate actually has
    a clip — a button that silently does nothing is worse than no button.
    """
    rows = [[
        InlineKeyboardButton(str(n + 1),
                             callback_data=f"{CB_SONG_PREFIX}pick:{n}")
        for n, _ in enumerate(results)
    ]]
    if any(song.preview_url for song in results):
        rows.append([InlineKeyboardButton(
            "🔊 Play me a bit of each",
            callback_data=f"{CB_SONG_PREFIX}preview")])
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

    state = _state(context)
    state["results"] = results
    # Kept so sl_preview can re-post the picker with the same heading. Cleared
    # alongside `results` wherever those are dropped.
    state["query"] = query
    await _show_candidates(update.message.reply_text, query, results)
    return SL_PICK


async def _show_candidates(send, query: str, results: list[Song]) -> None:
    """The picker: the numbered detail block and its keyboard.

    One function because sl_preview re-posts the same screen BELOW the clips
    it just sent, and a picker that differed between the two routes would be
    two things to keep in step. parse_mode must stay HTML: Song.detail emits
    <b> tags and escapes for HTML, so plain text would show the admin the raw
    markup.
    """
    await send(
        f"🔍 {len(results)} match(es) for “{query}”. Which one?\n\n"
        f"{render_candidates(results)}",
        reply_markup=results_keyboard(results),
        parse_mode=ParseMode.HTML,
        link_preview_options=NO_PREVIEW,
    )


async def sl_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send a thirty-second clip of each candidate, then re-post the picker.

    WHY A BUTTON AND NOT AUTOMATIC
    ------------------------------
    Five clips are five messages and about 5 MB that Telegram fetches from
    Apple. Most searches are not ambiguous — the admin types a title, knows
    the recording, and taps 1. Paying that on every search to help the
    minority of searches that need it would make the common case worse, so
    the clips are asked for.

    WHY THE PICKER IS RE-POSTED
    ---------------------------
    The clips push the original picker up out of view, and an admin who has
    just listened to five songs wants the buttons under the one they heard
    last. The old keyboard is stripped rather than left live, so the preview
    button cannot be tapped a second time and produce ten clips. The state
    does not change: `results` is untouched, so the indices behind the new
    buttons mean exactly what the old ones meant.
    """
    query = update.callback_query
    await query.answer()
    state = _state(context)
    results: list[Song] = state.get("results") or []
    if not results:
        # Only reachable from a keyboard that outlived its search.
        await query.edit_message_text(
            "Those results are no longer on offer. Send the song name again.")
        return SL_QUERY

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError as exc:
        # Losing the race to strip the old keyboard is not worth abandoning
        # the previews for; the worst case is a second set of clips.
        logger.warning("Could not clear the picker keyboard: %s", exc)

    sent = await _send_previews(query.message.chat_id, context, results,
                                state)
    if not sent:
        await query.message.reply_text(
            "⚠️ I could not fetch any previews just now. "
            "The details below should still help, or search again."
        )
    await _show_candidates(query.message.reply_text,
                           state.get("query") or "those", results)
    return SL_PICK


async def _send_previews(chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                         results: list[Song],
                         state: dict[str, Any]) -> int:
    """Clips for every candidate that has one. Returns how many were sent.

    Sequential, not gathered: five concurrent sends to one chat invite
    Telegram's flood limits, and the admin is going to listen to them one at
    a time anyway.

    One failure must not cost the others, so each send is caught on its own —
    the same reasoning as the per-recipient handling in sl_send. The caller
    reports nothing being sent; a single clip missing from five is visible in
    the numbering without being announced.

    Every clip's message id is recorded in *state* so the conversation can
    delete them when it finishes. See _clear_previews.

    THE TITLE IS OURS TO SET; THE THUMBNAIL HAD TO BE FETCHED
    ---------------------------------------------------------
    Telegram prefers an audio file's OWN tags over the title and performer a
    caller passes, which would have made those arguments decorative. They are
    not: a preview clip was downloaded and inspected on 2026-11-16 and its
    MPEG-4 metadata box contains no ©nam, ©ART, ©alb or covr atom at all, so
    there is nothing for Telegram to prefer. Should Apple start tagging the
    clips, the symptom is the numbering disappearing from the player — the
    clips would still play, and would no longer be matched to their buttons.

    The thumbnail is the exception, and v0.29.0 shipped it broken. `thumbnail`
    is the one sendAudio field that will NOT take a URL: Telegram treats it as
    a file_id and accepts only a freshly uploaded file, so PTB passed our URL
    through as a plain string and the artwork was silently dropped. Nothing
    raised; the user found it on the first live run. The bytes are fetched by
    apple_music.fetch_artwork and handed over as bytes, which PTB wraps in an
    InputFile. `audio` is unaffected — URLs are fine there, which is why the
    clips themselves always worked.

    ONE CONSEQUENCE: THIS PATH NOW FETCHES
    --------------------------------------
    Until the thumbnail fix, every fetch on this path was Telegram's, which
    made it immune to the deep-translator failure — an endpoint that refused
    AWS addresses, silently, in production only, for months. It is not immune
    any more, so mzstatic was checked from 44.214.123.92 itself (200, 22442
    bytes, 50ms, 2026-11-16). Keep that check in mind for any change here. The
    artwork is about 22 kB per clip, so five candidates cost ~110 kB, against
    the ~5 MB of audio Telegram still fetches for itself.
    """
    sent = 0
    for index, song in enumerate(results, 1):
        if not song.preview_url:
            continue
        # Fetched before the send rather than inside it: fetch_artwork never
        # raises and returns None on any failure, so a thumbnail that cannot
        # be had costs the thumbnail, never the clip.
        artwork = await apple_music.fetch_artwork(song.artwork_url)
        try:
            message = await context.bot.send_audio(
                chat_id=chat_id,
                audio=song.preview_url,
                # Telegram shows title and performer in the player. The
                # number leads the title so a clip is matched to its button
                # without reading the caption.
                title=f"{index}. {song.title}",
                performer=song.artist,
                caption=song.detail(index),
                parse_mode=ParseMode.HTML,
                thumbnail=artwork,
                # The clip, NOT the track. Apple's previews run about thirty
                # seconds; song.duration_ms is the full recording and would
                # tell the player to draw a four-minute scrubber over a
                # thirty-second file.
                duration=PREVIEW_SECONDS,
            )
            sent += 1
            message_id = getattr(message, "message_id", None)
            if message_id is not None:
                state.setdefault(PREVIEW_IDS_KEY, []).append(message_id)
        except TelegramError as exc:
            logger.warning("Preview %d (%r) failed: %s: %s",
                           index, song.title, type(exc).__name__, exc)
    logger.info("Sent %d of %d preview clip(s).", sent, len(results))
    return sent


async def _clear_previews(chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                          state: dict[str, Any]) -> int:
    """Delete every preview clip this conversation sent. Returns the count.

    The clips are scaffolding. Once the list has been sent or abandoned they
    are a wall of audio in the admin's chat with nothing left to decide, so
    the conversation cleans up after itself.

    The ids are forgotten whether or not the delete succeeded, which is
    deliberate: a failure here is permanent, not transient. A bot may only
    delete its own message within 48 HOURS of sending it, and a conversation
    left open past that — or a clip the admin deleted by hand — will fail the
    same way on every retry. Keeping the id would mean trying forever.

    Bulk, not one call per clip: delete_messages takes up to 100 ids and
    costs one request. It is all-or-nothing, though, so a single undeletable
    clip would strand the rest — hence the per-id fallback.
    """
    ids = state.pop(PREVIEW_IDS_KEY, None) or []
    if not ids:
        return 0
    try:
        await context.bot.delete_messages(chat_id=chat_id, message_ids=ids)
        logger.info("Deleted %d preview clip(s) in one call.", len(ids))
        return len(ids)
    except TelegramError as exc:
        logger.warning("Bulk delete of %d preview clip(s) failed: %s; "
                       "falling back to one at a time.", len(ids), exc)

    deleted = 0
    for message_id in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id,
                                             message_id=message_id)
            deleted += 1
        except TelegramError as exc:
            logger.warning("Could not delete preview clip %s: %s",
                           message_id, exc)
    logger.info("Deleted %d of %d preview clip(s) individually.",
                deleted, len(ids))
    return deleted


async def _finish(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    """End the conversation: delete the preview clips, then drop the state.

    THE ONLY WAY THIS CONVERSATION SHOULD END. Eight code paths reach
    ConversationHandler.END and three of them did not even clear the state
    before this existed, so patching them one at a time was never going to
    hold — that is precisely the mistake that cost v0.28.16 and v0.28.17,
    where a guard covered the cases someone remembered. TestEveryExitCleansUp
    walks this module's AST and fails if any function that returns END does
    not come through here, so a ninth exit added later cannot quietly skip it.

    sl_stale_button is the one deliberate exemption and must stay exempt: it
    is registered OUTSIDE the conversation and fires for taps the conversation
    declined, which includes an admin tapping an old keyboard while a list is
    still being built. Tearing down there would delete the clips and the list
    of someone who is mid-task.

    Returns END so callers can `return await _finish(...)` and not restate it.
    """
    await _clear_previews(chat_id, context, _state(context))
    context.user_data.pop(STATE_KEY, None)
    return ConversationHandler.END


async def sl_cancel_command(update: Update,
                            context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel for this conversation only.

    handlers.shared.cancel_conversation is the shared fallback and clears all
    of user_data, which is right for every other conversation and not enough
    for this one: the clips live in the CHAT, not in user_data, so dropping
    the state would orphan them permanently — nothing would remember the ids.

    So the clips go first and the shared handler still does the rest, keeping
    the cancellation message and the activity log identical to every other
    conversation's /cancel rather than forking that behaviour here.
    """
    await _clear_previews(update.effective_chat.id, context, _state(context))
    return await cancel_conversation(update, context)


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
    state.pop("query", None)
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
    state = _state(context)
    state.pop("results", None)
    state.pop("query", None)
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
        return await _finish(context, update.effective_chat.id)
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
        return await _finish(context, query.message.chat_id)
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
        return await _back_to_confirm(query, context, songs)
    dropped = songs.pop(index)
    await query.edit_message_text(
        f"🗑 Removed “{dropped.title}” by {dropped.artist}.")
    return await _back_to_confirm(query, context, songs)


async def sl_back_to_confirm(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await _back_to_confirm(query, context, _songs(context))


async def _back_to_confirm(query, context: ContextTypes.DEFAULT_TYPE,
                           songs: list[Song]) -> int:
    if not songs:
        await query.message.reply_text(
            "Every song has been removed, so there is nothing to send. "
            "/songlist to start again."
        )
        return await _finish(context, query.message.chat_id)
    await _show_confirmation(query.message.reply_text, songs)
    return SL_CONFIRM


async def sl_choose_recipient(update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not _songs(context):
        await query.edit_message_text("The list is empty. /songlist to start again.")
        return await _finish(context, query.message.chat_id)
    keyboard = await recipient_keyboard()
    if keyboard is None:
        await query.edit_message_text(
            "⚠️ There is nobody to send this to: the bot has no registered "
            "users with a chat it can open. A recipient has to have used the "
            "bot at least once."
        )
        return await _finish(context, query.message.chat_id)
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
            return await _finish(context, query.message.chat_id)

    activity.log_command("songlist", uid, uname, dname,
                         details=f"{len(songs)} song(s) sent to {chat_id}")
    await query.edit_message_text(
        f"✅ Sent {len(songs)} song(s)."
        + ("" if len(chunks) == 1 else f" ({len(chunks)} messages.)")
    )
    return await _finish(context, query.message.chat_id)


async def sl_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Abandoned. Nothing was sent.")
    return await _finish(context, query.message.chat_id)


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
                _cb(sl_preview, "preview"),
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
        fallbacks=[CommandHandler("cancel", sl_cancel_command)],
        allow_reentry=True,
    )

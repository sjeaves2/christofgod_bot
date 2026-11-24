"""/songlist — build a worship song list and send it to another user.

An admin types a song — name and, if known, artist, in ONE message — and picks
from Apple Music search results. Repeat until /end, confirm the list, choose a
recipient, send. Typically ten or eleven songs.

Or they paste the whole list, one song per line, and check every match on ONE
review screen. See "PASTING A LIST" below.

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

PASTING A LIST
--------------
One song at a time cost about twenty-two messages and taps for eleven songs,
most of them confirming a top result that was right. A message of two or more
lines is searched line by line, the top result is taken PROVISIONALLY, and one
review screen shows what was typed beside what was found. Only the wrong ones
need a tap. song_matching.py decides which matches are flagged.

Nothing provisional can reach a recipient: the review leads to the same
confirm, recipient and send steps as a list built one song at a time.

THE SEARCHES RUN IN A BACKGROUND TASK, and must. bot.py does not enable
concurrent updates, so PTB runs one handler at a time for the WHOLE bot: a
handler awaiting eleven searches, or a minute of throttling, would leave every
member unanswered and the admin's own /cancel waiting behind it. The handler
returns SL_REVIEW at once and the task posts the review when it is ready.

A task outlives the handler, so it can also outlive the conversation. Every
step asks _still_wanted() first: a cancelled or restarted list must not go on
searching, or post a review or a clip nobody will clean up. It also stops when
the bot is shutting down, because Application.stop() WAITS for these tasks and
a deploy must not sit out a throttled search.

A part-built list is NOT persisted. That was a deliberate choice: the
alternative is a tenth YAML store to create, gitignore, keep out of the backup
globs and garbage-collect, for something a restart interrupts a few times a
year. The cost is a lost list, so the first message says so plainly.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import apple_music
import song_matching
import storage
from apple_music import Song
from handlers.shared import cancel_conversation
from permissions import admin_only, user_info
from settings import activity
from song_matching import Line
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

(SL_QUERY, SL_PICK, SL_CONFIRM, SL_REMOVE, SL_RECIPIENT,
 SL_REVIEW, SL_FIX, SL_RETYPE) = range(8)

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

# Least time between edits of the "Searching 3 of 11" message. Telegram limits
# how fast one chat may be written to, and the search outruns anyone's reading.
PROGRESS_SECONDS = 1.5

# Telegram allows about a hundred buttons on one keyboard and rejects the whole
# message past that, which would lose the review. Ten are kept back for the
# rows underneath the numbers.
MAX_NUMBER_BUTTONS = 90
NUMBERS_PER_ROW = 5

#: How much of a typed line the review echoes back.
MAX_QUERY_SHOWN = 80


@dataclass
class Batch:
    """A pasted list under review.

    `busy` says what the background task is doing, or is "" when it is idle.
    `live_id` is the one message currently carrying this batch's buttons, the
    review or a picker opened from it; see _take_off_buttons.
    """

    lines: list[Line]
    busy: str = ""
    live_id: int | None = None


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


def results_keyboard(results: list[Song],
                     fixing: bool = False) -> InlineKeyboardMarkup:
    """A numbered button per candidate, plus a way out when none is right.

    Numbers, not labels: render_candidates carries the identifying detail in
    the message body, where it is not truncated. The buttons are packed onto
    one row because five "3"s side by side are easier to hit than five
    full-width rows, and the number is all the target needs to be.

    The preview row is only offered when at least one candidate actually has
    a clip — a button that silently does nothing is worse than no button.

    *fixing* is the same screen opened from the review of a pasted list. Its
    way out is back to that review: "cancel the whole list" beside a question
    about one song of eleven would be a costly mis-tap.
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
    if fixing:
        rows += _fix_rows()
    else:
        rows.append([InlineKeyboardButton(
            "✖ Cancel the whole list",
            callback_data=f"{CB_SONG_PREFIX}cancel")])
    return InlineKeyboardMarkup(rows)


def _fix_rows() -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton("🚫 Leave this song out",
                              callback_data=f"{CB_SONG_PREFIX}drop")],
        [InlineKeyboardButton("⬅ Back to the review",
                              callback_data=f"{CB_SONG_PREFIX}review")],
    ]


def review_keyboard(lines: list[Line], already: list[Song]) -> InlineKeyboardMarkup:
    """A number per song, then what can be done with the whole list.

    The ✅ label states how many songs go in AND how many do not, so a song
    that was never found cannot be dropped without being read. It is absent
    when there is nothing to use.
    """
    icons = [icon for icon, _ in song_matching.flags(lines, already)]
    shown = list(range(len(lines)))
    if len(shown) > MAX_NUMBER_BUTTONS:
        shown = [n for n in shown if icons[n] != song_matching.FINE]
        shown = shown[:MAX_NUMBER_BUTTONS]
    buttons = [
        InlineKeyboardButton(
            str(n + 1) if icons[n] == song_matching.FINE
            else f"{icons[n]} {n + 1}",
            callback_data=f"{CB_SONG_PREFIX}fix:{n}")
        for n in shown
    ]
    rows = [buttons[n:n + NUMBERS_PER_ROW]
            for n in range(0, len(buttons), NUMBERS_PER_ROW)]

    if any(line.missing or (not line.searched and not line.dropped)
           for line in lines):
        rows.append([InlineKeyboardButton(
            f"🔄 Search again for the {song_matching.MISSING} ones",
            callback_data=f"{CB_SONG_PREFIX}retry")])
    if any(line.song and line.song.preview_url for line in lines):
        rows.append([InlineKeyboardButton(
            "🔊 Hear my picks", callback_data=f"{CB_SONG_PREFIX}hear")])
    used = sum(1 for line in lines if line.song)
    if used:
        skipped = len(lines) - used
        rows.append([InlineKeyboardButton(
            f"✅ Use {used} song(s)" + (f", skip {skipped}" if skipped else ""),
            callback_data=f"{CB_SONG_PREFIX}use")])
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
    # Starting again abandons whatever was being built, so its clips go too —
    # their ids live in the state this replaces.
    await _clear_previews(update.effective_chat.id, context, _state(context))
    context.user_data[STATE_KEY] = {"songs": []}
    await update.message.reply_text(
        "🎵 Building a worship song list.\n\n"
        "Paste the whole list, one song per line, and check every match on "
        "one screen — or send one song at a time.\n\n"
        "Give each song's name, and the artist too if you know it, side by "
        "side. For example:\n\n"
        "    Amazing Grace CeCe Winans\n\n"
        "The artist is optional, but it helps: a hymn is often recorded by "
        "several people. I will look it up on Apple Music and show you what "
        "I find.\n\n"
        "/list to see the songs chosen so far\n"
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

    if song_matching.is_list(query):
        return await _start_batch(update, context, query)

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


async def _show_candidates(send, query: str, results: list[Song],
                           fixing: bool = False):
    """The picker: the numbered detail block and its keyboard.

    One function because sl_preview re-posts the same screen BELOW the clips
    it just sent, and a picker that differed between the two routes would be
    two things to keep in step. parse_mode must stay HTML: Song.detail emits
    <b> tags and escapes for HTML, so plain text would show the admin the raw
    markup. The query is escaped for the same reason: a stray "<" in it would
    make Telegram reject the whole picker.

    Returns the message sent, so a review can take its buttons off later.
    """
    return await send(
        f"🔍 {len(results)} match(es) for "
        f"“{html.escape(query, quote=False)}”. Which one?\n\n"
        f"{render_candidates(results)}",
        reply_markup=results_keyboard(results, fixing),
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
    results: list[Song] = _state(context).get("results") or []
    if not results:
        # Only reachable from a keyboard that outlived its search.
        await query.edit_message_text(
            "Those results are no longer on offer. Send the song name again.")
        return SL_QUERY
    await _preview_and_repost(query, context, results)
    return SL_PICK


async def _preview_and_repost(query, context: ContextTypes.DEFAULT_TYPE,
                              results: list[Song],
                              fixing: bool = False):
    """The work of sl_preview, shared with the review's picker.

    Returns the re-posted picker.
    """
    state = _state(context)
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
    return await _show_candidates(query.message.reply_text,
                                  state.get("query") or "those", results,
                                  fixing)


async def _send_previews(chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                         results: list[Song],
                         state: dict[str, Any],
                         numbers: list[int] | None = None,
                         wanted=lambda: True) -> int:
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

    THE THUMBNAIL TOOK THREE RELEASES, AND HERE IS THE WHOLE RULE
    -------------------------------------------------------------
    `thumbnail` has two constraints and missing either one loses the artwork
    silently — no error, no warning, nothing in errors.log:

      1. It will NOT take a URL. Telegram reads it as a file_id and accepts
         only a freshly uploaded file. v0.29.0 passed a URL, which PTB
         forwarded as a plain string, and Telegram dropped it.
      2. "Ignored if the file is not uploaded using multipart/form-data" —
         where *the file* is the AUDIO, not the thumbnail. v0.29.1 fixed (1)
         and still showed nothing, because `audio` was a URL.

    Both sentences are in that one field's own documentation, two apart. The
    first explained the v0.29.0 bug, which is exactly why the second went
    unread. **When one field of an API call turns out to have a rule, read
    the rest of its paragraph** — a lesson stated in the v0.29.1 tag message
    and then not applied to the sentence directly below the one it came from.

    So the clip is UPLOADED. `audio` accepts a URL and that is still the
    cheaper call, but it costs the thumbnail, so it is only the fallback for
    a download that failed: a clip without artwork beats no clip.

    THIS PATH NOW FETCHES, WHERE ONCE IT FETCHED NOTHING
    ----------------------------------------------------
    Every fetch here used to be Telegram's, which made the path immune to the
    deep-translator failure — an endpoint that refused AWS addresses,
    silently, in production only, for months. It is not immune any more, and
    now for the audio as well as the artwork, so BOTH were verified from
    44.214.123.92 itself on 2026-11-16:

        is1-ssl.mzstatic.com         200, 22442 bytes, 0.05s
        audio-ssl.itunes.apple.com   200, 1075543 bytes, 1.01s

    *numbers* labels the clips when they are not simply 1, 2, 3: a pasted list
    with a song missing from the middle must still match its review numbers.

    *wanted* is asked before each clip and again after it. When this runs as a
    background task the admin can cancel mid-send, and _clear_previews has
    then already been and gone: a clip sent after that would stay in the chat
    for good, so the one in flight is deleted here and the rest are not sent.

    Keep that check in any change here. Five candidates now cost about 5 MB
    down and 5 MB up, one clip held in memory at a time; the box had 189 MB
    free. The $5 plan's transfer allowance makes 10 MB irrelevant.
    """
    sent = 0
    uploaded = 0
    with_artwork = 0
    for index, song in zip(numbers or range(1, len(results) + 1), results):
        if not song.preview_url:
            continue
        if not wanted():
            break
        # Clip FIRST, then artwork only if the clip arrived. Telegram ignores
        # a thumbnail sent beside a linked file, so artwork fetched for a
        # fallback send would be downloaded and then discarded — 22 kB of
        # pointless traffic, and a log line implying a thumbnail that cannot
        # appear.
        #
        # Neither fetch raises; both return None on any failure. A clip that
        # cannot be downloaded falls back to its URL, which still plays.
        clip = await apple_music.fetch_preview(song.preview_url)
        artwork = (await apple_music.fetch_artwork(song.artwork_url)
                   if clip else None)
        try:
            message = await context.bot.send_audio(
                chat_id=chat_id,
                audio=clip or song.preview_url,
                # Only meaningful for an upload; Telegram names a linked file
                # itself. Without it PTB would send the bytes as
                # "application.octet-stream" and some clients show that
                # instead of the title.
                filename=f"{index}.m4a",
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
            if clip:
                uploaded += 1
            if artwork:
                with_artwork += 1
            message_id = getattr(message, "message_id", None)
            if not wanted():
                await context.bot.delete_message(chat_id=chat_id,
                                                 message_id=message_id)
                break
            if message_id is not None:
                state.setdefault(PREVIEW_IDS_KEY, []).append(message_id)
        except TelegramError as exc:
            logger.warning("Preview %d (%r) failed: %s: %s",
                           index, song.title, type(exc).__name__, exc)

    # Says UPLOADED and WITH ARTWORK, not just "sent". Two releases of this
    # feature shipped broken in a way no log could show: v0.29.0 sent an
    # invalid thumbnail and v0.29.1 sent a valid one beside a linked file,
    # and in both cases Telegram discarded it silently while every log line
    # read "Sent 5 of 5 preview clip(s)." A thumbnail that never appears is
    # now distinguishable from one that did, from the log alone.
    logger.info("Sent %d of %d preview clip(s); %d uploaded (so eligible for "
                "a thumbnail), %d with artwork attached.",
                sent, len(results), uploaded, with_artwork)
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
        "Send the next song name, /list to see them all, /undo to drop this "
        "one, or /end to finish."
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


async def sl_list(update: Update,
                  context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """/list — the songs chosen so far, without disturbing anything.

    WHY IT RETURNS None
    -------------------
    None means "leave the conversation state alone": PTB's _update_state ends
    in `elif new_state is not None`, so a None return changes nothing. That
    is exactly right for a read-only peek, and it is what lets ONE handler
    serve several states without being told which one it is in. Returning a
    literal state instead would mean either a wrapper per state or a handler
    that silently moves the admin somewhere they were not.

    So /list works mid-search and mid-pick alike, and after it the live
    keyboard above is still the live keyboard.

    WHY THIS SHOWS THE DETAIL AND NOT THE DELIVERED FORMAT
    -----------------------------------------------------
    Song.detail, the same renderer the picker uses — album, running time and
    year — rather than Song.enumerated, which is what the recipient gets.
    Two reasons. The admin asked for this because five preview clips push the
    list off screen and they lose their place, so the useful thing is
    confirmation that the RIGHT RECORDING is in the list, which is precisely
    what those three fields are for. And /end already shows the delivered
    format, under a header promising it is exactly what will be sent; making
    /list a second, subtly different claim about the outgoing message would
    devalue that one.

    Reported by the user on 2026-11-16: "one can become lost in the list...
    there is no sub-command to let me see all of the list thus far."
    """
    songs = _songs(context)
    uid, uname, dname = user_info(update)
    activity.log_command("songlist", uid, uname, dname,
                         details=f"listed {len(songs)} song(s) so far")
    if not songs:
        await update.message.reply_text(
            "📋 The list is empty so far.\n\n"
            "Send a song name to add the first one, or /cancel."
        )
        return None

    body = render_candidates(songs)
    header = (f"📋 {len(songs)} song(s) so far — still building:\n\n")
    # Split for the same reason sl_send does: a long list exceeds Telegram's
    # message limit, and /list is the one view whose length the admin
    # controls directly by adding songs.
    chunks = split_for_telegram(body)
    for n, chunk in enumerate(chunks):
        await update.message.reply_text(
            header + chunk if n == 0 else chunk,
            parse_mode=ParseMode.HTML,
            link_preview_options=NO_PREVIEW,
        )
    return None


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
        f"{len(songs)} song(s) left.\n\nSend the next song name, /list to see "
        f"them all, or /end."
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
# A pasted list: search it, review it, fix a line
# ---------------------------------------------------------------------------

def _batch(context: ContextTypes.DEFAULT_TYPE) -> Batch:
    return _state(context).setdefault("batch", Batch([]))


def _still_wanted(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any],
                  batch: Batch) -> bool:
    """Whether a background task's list is still the one being built.

    Compared by identity: /cancel and /songlist replace or drop the state, so
    a task holding the old one must stop. `running` goes False at the START of
    Application.stop(), which then waits for these tasks.
    """
    return (context.application.running
            and context.user_data.get(STATE_KEY) is state
            and state.get("batch") is batch)


async def _take_off_buttons(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                            batch: Batch) -> None:
    """Strip the buttons from whichever review or picker last carried them.

    Only ONE keyboard of a batch is live at a time. The buttons say `pick:2`
    and `drop`, not which song they were drawn for, so a leftover keyboard
    from song 3 would otherwise act on song 7.
    """
    message_id, batch.live_id = batch.live_id, None
    if message_id is None:
        return
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramError as exc:
        logger.warning("Could not clear a song-list keyboard: %s", exc)


async def _start_batch(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       text: str) -> int:
    """Begin reviewing a pasted list. Songs already chosen are kept."""
    lines = [Line(query) for query in song_matching.parse_lines(text)]
    if not lines:
        await update.message.reply_text(
            "I could not find a song name in that. Send a song name, or /end.")
        return SL_QUERY
    uid, uname, dname = user_info(update)
    activity.log_command("songlist", uid, uname, dname,
                         details=f"pasted {len(lines)} line(s)")
    _state(context)["batch"] = Batch(lines)
    await _begin_search(update, context, update.message.reply_text)
    return SL_REVIEW


async def _begin_search(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        send) -> None:
    """Search the batch's unsearched lines, in the background."""
    state, batch = _state(context), _batch(context)
    chat_id = update.effective_chat.id
    await _take_off_buttons(context, chat_id, batch)
    total = sum(1 for line in batch.lines if _is_pending(line))
    progress = await send(_progress_text(1, total))
    # After the send: if that raised, nothing is running and nothing must
    # claim to be.
    batch.busy = "searching"
    context.application.create_task(
        _search_pending(chat_id, context, state, batch, progress),
        update=update)


def _is_pending(line: Line) -> bool:
    return not line.searched and not line.dropped


def _progress_text(n: int, total: int, waiting: float = 0.0) -> str:
    text = f"🔍 Searching {n} of {total}…"
    if waiting:
        text += (f"\n⏳ Waiting about {max(1, round(waiting))} seconds: Apple "
                 "allows about 20 searches a minute.")
    return text + "\n\n/cancel to abandon the list."


async def _search_pending(chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                          state: dict[str, Any], batch: Batch,
                          progress) -> None:
    """The background task: search each pending line, then post the review.

    One at a time, never gathered, so the throttle means something.
    """
    def wanted() -> bool:
        return _still_wanted(context, state, batch)

    try:
        pending = [line for line in batch.lines if _is_pending(line)]
        shown_at = time.monotonic()
        for n, line in enumerate(pending, 1):
            told = False
            while wanted() and (delay := apple_music.search_delay()) > 0:
                if not told:
                    await _edit_quietly(
                        progress, _progress_text(n, len(pending), delay))
                    told = True
                # Short naps, so a cancel or a shutdown is noticed promptly.
                await asyncio.sleep(min(delay, 1.0))
            if not wanted():
                return
            now = time.monotonic()
            if told or now - shown_at >= PROGRESS_SECONDS:
                shown_at = now
                await _edit_quietly(progress, _progress_text(n, len(pending)))
            line.record(await apple_music.search_songs(line.query))
    finally:
        batch.busy = ""
        try:
            await progress.delete()
        except TelegramError as exc:
            logger.warning("Could not delete the progress message: %s", exc)

    if wanted():
        await _post_review(chat_id, context)


async def _edit_quietly(message, text: str) -> None:
    """Edit a progress message; a failure is not worth stopping a search for."""
    try:
        await message.edit_text(text)
    except TelegramError as exc:
        logger.warning("Could not update the progress message: %s", exc)


def render_review(lines: list[Line], already: list[Song]) -> str:
    """What was typed beside what was found, one entry per line. HTML.

    Every entry carries the same detail as the picker, flagged or not: a ✓
    means the WORDS agree, and only the album, length and year can tell the
    admin whether it is the recording they meant.
    """
    entries = []
    marks = song_matching.flags(lines, already)
    for n, (line, (icon, note)) in enumerate(zip(lines, marks), 1):
        song = line.song
        if song:
            text = f"{icon} {song.detail(n)}"
        else:
            text = f"{icon} {n}. {html.escape(note, quote=False).capitalize()}"
            note = ""
        typed = line.query
        if len(typed) > MAX_QUERY_SHOWN:
            typed = typed[:MAX_QUERY_SHOWN - 1] + "…"
        text += f"\n    you typed: {html.escape(typed, quote=False)}"
        if note:
            text += f"\n    {html.escape(note, quote=False)}"
        entries.append(text)
    return "\n\n".join(entries)


def review_heading(lines: list[Line], already: list[Song]) -> str:
    """The instructions above the review, saying only what applies."""
    kept = [line for line in lines if not line.dropped]
    if kept and all(line.missing for line in kept):
        # search_songs returns [] for "nothing found" and "broken" alike. One
        # line missing is a typo; every line missing is probably not.
        return ("⚠️ I found nothing for any of these. The search itself may be "
                "down: try 🔄 in a few minutes, or tap a number to type that "
                "song again.")
    parts = ["📋 Please check these matches. Nothing has been sent."]
    if already:
        parts.append(f"They follow the {len(already)} song(s) already chosen.")
    parts.append("Tap a number to change that song. ✓ means the words match, "
                 "so check the album, length and year too.")
    if len(lines) > MAX_NUMBER_BUTTONS:
        parts.append("In a list this long, only songs with a mark other than "
                     "✓ have a button.")
    parts.append("Send more songs to add them.")
    return "\n".join(parts)


async def _post_review(chat_id: int,
                       context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the review, with the buttons under its last message."""
    batch, already = _batch(context), _songs(context)
    await _take_off_buttons(context, chat_id, batch)
    body = (review_heading(batch.lines, already) + "\n\n"
            + render_review(batch.lines, already))
    chunks = split_for_telegram(body)
    for n, chunk in enumerate(chunks):
        is_last = n == len(chunks) - 1
        message = await context.bot.send_message(
            chat_id, chunk,
            reply_markup=(review_keyboard(batch.lines, already)
                          if is_last else None),
            parse_mode=ParseMode.HTML,
            link_preview_options=NO_PREVIEW)
        if is_last:
            batch.live_id = message.message_id


def _busy_reply(batch: Batch) -> str:
    return (f"Still {batch.busy}. The review will appear here when that is "
            "done, or /cancel to abandon the list.")


async def sl_review_text(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
    """More songs sent while reviewing: add them to the review."""
    batch = _batch(context)
    if batch.busy:
        await update.message.reply_text(
            _busy_reply(batch) + "\n\nSend that again afterwards.")
        return SL_REVIEW
    text = (update.message.text or "").strip()
    queries = (song_matching.parse_lines(text)
               if song_matching.is_list(text) else [text])
    batch.lines += [Line(query) for query in queries if query]
    await _begin_search(update, context, update.message.reply_text)
    return SL_REVIEW


async def sl_review_list(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
    """/list while a pasted list is open: show the review again."""
    batch = _batch(context)
    if batch.busy:
        await update.message.reply_text(_busy_reply(batch))
        return SL_REVIEW
    _leave_fix(context)
    await _post_review(update.effective_chat.id, context)
    return SL_REVIEW


async def sl_review_retry(update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> int:
    """Search again for the lines that came back empty."""
    query = update.callback_query
    batch = _batch(context)
    if batch.busy:
        await query.answer(_busy_reply(batch))
        return SL_REVIEW
    await query.answer()
    for line in batch.lines:
        if line.missing:
            line.searched = False
    await _begin_search(update, context, query.message.reply_text)
    return SL_REVIEW


async def sl_review_hear(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
    """One clip per chosen song, in the background like the search.

    About a second and a megabyte each, so eleven of them inside a handler
    would hold up the whole bot.
    """
    query = update.callback_query
    state, batch = _state(context), _batch(context)
    if batch.busy:
        await query.answer(_busy_reply(batch))
        return SL_REVIEW
    await query.answer()
    chat_id = update.effective_chat.id
    await _take_off_buttons(context, chat_id, batch)
    progress = await query.message.reply_text(
        "🔊 Fetching a clip of each song…\n\n/cancel to abandon the list.")
    batch.busy = "fetching the clips"
    context.application.create_task(
        _play_picks(chat_id, context, state, batch, progress), update=update)
    return SL_REVIEW


async def _play_picks(chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                      state: dict[str, Any], batch: Batch, progress) -> None:
    def wanted() -> bool:
        return _still_wanted(context, state, batch)

    picks = [(n, line.song) for n, line in enumerate(batch.lines, 1)
             if line.song]
    try:
        sent = await _send_previews(
            chat_id, context, [song for _, song in picks], state,
            numbers=[n for n, _ in picks], wanted=wanted)
    finally:
        batch.busy = ""
        try:
            await progress.delete()
        except TelegramError as exc:
            logger.warning("Could not delete the progress message: %s", exc)

    if not wanted():
        return
    if sent < len(picks):
        await context.bot.send_message(
            chat_id, f"⚠️ {len(picks) - sent} of {len(picks)} song(s) had no "
                     "clip I could fetch.")
    await _post_review(chat_id, context)


async def sl_review_use(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> int:
    """✅ — the review is right, so go on to the usual confirmation."""
    query = update.callback_query
    if _batch(context).busy:
        await query.answer(_busy_reply(_batch(context)))
        return SL_REVIEW
    await query.answer()
    return await _use_batch(update, context, query.message.reply_text)


async def sl_review_end(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> int:
    """/end, which an admin will type here from habit, means the same."""
    if _batch(context).busy:
        await update.message.reply_text(_busy_reply(_batch(context)))
        return SL_REVIEW
    return await _use_batch(update, context, update.message.reply_text)


async def _use_batch(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     send) -> int:
    state, batch = _state(context), _batch(context)
    chosen = [line.song for line in batch.lines if line.song]
    if not chosen and not state["songs"]:
        await send("None of these lines has a song yet. Tap a number in the "
                   "review to type one again, or /cancel. /list shows the "
                   "review.")
        return SL_REVIEW
    await _take_off_buttons(context, update.effective_chat.id, batch)
    state["songs"].extend(chosen)
    state.pop("batch")
    await _show_confirmation(send, state["songs"])
    return SL_CONFIRM


# -- Fixing one line ---------------------------------------------------------
#
# The picker and its previews are the ones used for a single song. `fixing`
# holds which line is open, beside the `results` and `query` the picker reads.

def _leave_fix(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    for key in ("fixing", "results", "query"):
        state.pop(key, None)


def _fixing(context: ContextTypes.DEFAULT_TYPE) -> tuple[int, Line]:
    index = _state(context)["fixing"]
    return index + 1, _batch(context).lines[index]


async def _back_to_review(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          said: str) -> int:
    """Close the picker with a word on what happened, and show the review."""
    await update.callback_query.edit_message_text(said)
    # The edit above already removed that message's buttons.
    _batch(context).live_id = None
    _leave_fix(context)
    await _post_review(update.effective_chat.id, context)
    return SL_REVIEW


async def sl_review_fix(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> int:
    """A number in the review: open that song's results, or ask for it again."""
    query = update.callback_query
    batch = _batch(context)
    if batch.busy:
        await query.answer(_busy_reply(batch))
        return SL_REVIEW
    await query.answer()
    index = int(query.data.rsplit(":", 1)[1])
    if index >= len(batch.lines):
        # Only reachable from the review of an earlier, longer list.
        await query.message.reply_text(
            "That song is not in this list. /list shows the current review.")
        return SL_REVIEW
    await _take_off_buttons(context, update.effective_chat.id, batch)
    state, line = _state(context), batch.lines[index]
    state["fixing"] = index
    if not line.results:
        return await _ask_again(context, query.message.reply_text)
    state["results"], state["query"] = line.results, line.query
    message = await _show_candidates(query.message.reply_text, line.query,
                                     line.results, fixing=True)
    batch.live_id = message.message_id
    return SL_FIX


async def _ask_again(context: ContextTypes.DEFAULT_TYPE, send) -> int:
    number, line = _fixing(context)
    message = await send(
        f"Send song {number} again — adding the artist, or part of the title, "
        f"usually helps.\n\nYou typed: {line.query[:MAX_QUERY_SHOWN]}",
        reply_markup=InlineKeyboardMarkup(_fix_rows()))
    _batch(context).live_id = message.message_id
    return SL_RETYPE


async def sl_fix_pick(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    state = _state(context)
    results: list[Song] = state.get("results") or []
    index = int(query.data.rsplit(":", 1)[1])
    if index >= len(results):
        return await _back_to_review(
            update, context, "That result is no longer on offer.")
    number, line = _fixing(context)
    line.settle(state["query"], results, index)
    song = results[index]
    return await _back_to_review(
        update, context, f"✅ Song {number} is {song.title} by {song.artist}")


async def sl_fix_preview(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    message = await _preview_and_repost(
        query, context, _state(context).get("results") or [], fixing=True)
    _batch(context).live_id = message.message_id
    return SL_FIX


async def sl_fix_retype(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> int:
    """None of these: ask for this one song again."""
    query = update.callback_query
    await query.answer()
    state = _state(context)
    state.pop("results", None)
    state.pop("query", None)
    await _take_off_buttons(context, update.effective_chat.id, _batch(context))
    return await _ask_again(context, query.message.reply_text)


async def sl_fix_text(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """The retyped song. Nothing changes until one of its results is picked."""
    text = (update.message.text or "").strip()
    if song_matching.is_list(text):
        await update.message.reply_text(
            f"Just song {_fixing(context)[0]} here, please, on one line. "
            "More songs can be added from the review.")
        return SL_RETYPE
    results = await apple_music.search_songs(text)
    if not results:
        await update.message.reply_text(
            f"🔍 I could not find anything for “{text}”.\n\n"
            "Check the spelling and send it again, or use the buttons above.")
        return SL_RETYPE
    batch = _batch(context)
    await _take_off_buttons(context, update.effective_chat.id, batch)
    state = _state(context)
    state["results"], state["query"] = results, text
    message = await _show_candidates(update.message.reply_text, text, results,
                                     fixing=True)
    batch.live_id = message.message_id
    return SL_FIX


async def sl_fix_drop(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """Leave this song out. Its results are kept, so it can be put back."""
    await update.callback_query.answer()
    number, line = _fixing(context)
    line.dropped = True
    return await _back_to_review(
        update, context, f"🚫 Song {number} is left out. Tap its number in "
                         "the review to put it back.")


async def sl_fix_back(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    number, _ = _fixing(context)
    return await _back_to_review(update, context,
                                 f"Song {number} is unchanged.")


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
                CommandHandler("list", sl_list),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sl_query),
            ],
            SL_PICK: [
                # /list here too, because this is where the admin actually
                # gets lost: five preview clips have just pushed the list off
                # screen and a song still has to be chosen. It returns None,
                # so the picker keyboard above stays live.
                CommandHandler("list", sl_list),
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
            # A pasted list. SL_FIX and SL_RETYPE have no "cancel" button;
            # they go back to the review, which does.
            SL_REVIEW: [
                CommandHandler("end", sl_review_end),
                CommandHandler("list", sl_review_list),
                _cb_indexed(sl_review_fix, "fix"),
                _cb(sl_review_retry, "retry"),
                _cb(sl_review_hear, "hear"),
                _cb(sl_review_use, "use"),
                _cb(sl_cancel, "cancel"),
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               sl_review_text),
            ],
            SL_FIX: [
                CommandHandler("list", sl_review_list),
                _cb_indexed(sl_fix_pick, "pick"),
                _cb(sl_fix_preview, "preview"),
                _cb(sl_fix_retype, "none"),
                _cb(sl_fix_drop, "drop"),
                _cb(sl_fix_back, "review"),
            ],
            SL_RETYPE: [
                CommandHandler("list", sl_review_list),
                _cb(sl_fix_drop, "drop"),
                _cb(sl_fix_back, "review"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sl_fix_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", sl_cancel_command)],
        allow_reentry=True,
    )

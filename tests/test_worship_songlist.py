"""/songlist — building a worship list and sending it to a registered user.

The format of the message the RECIPIENT gets is the deliverable, so it is
tested exactly: index, title, "by Artist", newline, link. Everything else here
exists to get that message right and to make sure it actually leaves.

No test touches the network. apple_music.search_songs is substituted
throughout; tests/test_apple_music.py owns the search itself.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.worship as w
from apple_music import Song
from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, ConversationHandler

SONGS = [
    Song("Amazing Grace", "CeCe Winans", "The Hymns",
         "https://music.apple.com/us/album/amazing-grace/1?i=11"),
    Song("Great Is Thy Faithfulness", "Michael W. Smith", "The Hymns",
         "https://music.apple.com/us/album/great-is-thy/2?i=22"),
    Song("Amazing Grace (My Chains Are Gone)", "Chris Tomlin", "How Great",
         "https://music.apple.com/us/album/chains/3?i=33"),
]


def _context(songs=None, **state):
    ctx = MagicMock()
    ctx.user_data = {}
    if songs is not None or state:
        ctx.user_data[w.STATE_KEY] = {"songs": list(songs or []), **state}
    ctx.bot.send_message = AsyncMock()
    return ctx


def _message_update(text="", uid=7):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_user.id = uid
    update.effective_user.username = "bishop"
    update.effective_user.full_name = "The Bishop"
    return update


def _callback_update(data, uid=7):
    update = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.effective_user.id = uid
    update.effective_user.username = "bishop"
    update.effective_user.full_name = "The Bishop"
    return update


def _said(mock) -> str:
    """Everything a reply/edit mock was called with, joined."""
    return "\n".join(str(c[0][0]) for c in mock.call_args_list if c[0])


class TestTheRecipientFormat:
    """The specified shape: index, name of song, "by <Artist>", newline, link."""

    def test_one_song(self):
        assert w.render_list(SONGS[:1]) == (
            "1. Amazing Grace by CeCe Winans\n"
            "https://music.apple.com/us/album/amazing-grace/1?i=11")

    def test_indices_count_from_one(self):
        rendered = w.render_list(SONGS)
        assert rendered.startswith("1. ")
        assert "\n2. Great Is Thy Faithfulness by Michael W. Smith\n" in rendered
        assert "\n3. Amazing Grace (My Chains Are Gone) by Chris Tomlin\n" in rendered

    def test_every_link_sits_on_the_line_below_its_title(self):
        lines = w.render_list(SONGS).split("\n")
        for n, song in enumerate(SONGS, 1):
            i = lines.index(f"{n}. {song.title} by {song.artist}")
            assert lines[i + 1] == song.url

    def test_entries_are_separated_by_a_blank_line(self):
        assert "\n\n" in w.render_list(SONGS)

    def test_an_eleven_song_list_is_numbered_one_to_eleven(self):
        """Eleven is the size the user said these lists usually are."""
        many = [Song(f"Song {i}", f"Artist {i}", "", f"https://music.apple.com/{i}")
                for i in range(1, 12)]
        rendered = w.render_list(many)
        for n in range(1, 12):
            assert f"{n}. Song {n} by Artist {n}" in rendered

    def test_nothing_is_escaped_or_marked_up(self):
        """Sent as plain text, so a backslash or asterisk would be literal."""
        rendered = w.render_list(SONGS)
        assert "\\" not in rendered
        assert "*" not in rendered and "_" not in rendered
        assert "Amazing Grace (My Chains Are Gone)" in rendered


class TestSplittingLongLists:
    def test_a_short_list_is_one_message(self):
        assert w.split_for_telegram(w.render_list(SONGS)) == [w.render_list(SONGS)]

    def test_a_long_list_is_split(self):
        many = [Song(f"Song {i}", f"Artist {i}", "",
                     f"https://music.apple.com/us/album/song-{i}/{i}?i={i}")
                for i in range(80)]
        chunks = w.split_for_telegram(w.render_list(many))
        assert len(chunks) > 1

    def test_every_chunk_is_within_the_limit(self):
        many = [Song(f"Song {i}", f"Artist {i}", "",
                     f"https://music.apple.com/us/album/song-{i}/{i}?i={i}")
                for i in range(80)]
        for chunk in w.split_for_telegram(w.render_list(many)):
            assert len(chunk) <= w.MAX_MESSAGE_CHARS

    def test_a_song_is_never_separated_from_its_link(self):
        """The whole reason the split is on blank lines. A chunk ending on a
        title, or starting with a bare URL, would be useless to the reader."""
        many = [Song(f"Song {i}", f"Artist {i}", "",
                     f"https://music.apple.com/us/album/song-{i}/{i}?i={i}")
                for i in range(80)]
        for chunk in w.split_for_telegram(w.render_list(many)):
            lines = [ln for ln in chunk.split("\n") if ln.strip()]
            assert len(lines) % 2 == 0, chunk[:120]
            for i in range(0, len(lines), 2):
                assert not lines[i].startswith("http")
                assert lines[i + 1].startswith("http")

    def test_nothing_is_lost_in_the_split(self):
        many = [Song(f"Song {i}", f"Artist {i}", "",
                     f"https://music.apple.com/us/album/song-{i}/{i}?i={i}")
                for i in range(80)]
        rendered = w.render_list(many)
        chunks = w.split_for_telegram(rendered)
        for song in many:
            assert any(song.url in c for c in chunks), song.url
        assert sum(c.count("http") for c in chunks) == 80


class TestKeyboards:
    def test_one_button_per_search_result(self):
        kb = w.results_keyboard(SONGS)
        picks = [b for row in kb.inline_keyboard for b in row
                 if b.callback_data.startswith(f"{w.CB_SONG_PREFIX}pick:")]
        assert len(picks) == 3
        assert [b.callback_data for b in picks] == [
            "sl:pick:0", "sl:pick:1", "sl:pick:2"]

    def test_the_album_is_in_the_label(self):
        """Sometimes the only difference between two results."""
        kb = w.results_keyboard(SONGS[:1])
        assert "The Hymns" in kb.inline_keyboard[0][0].text

    def test_there_is_a_way_out_when_no_result_fits(self):
        data = {b.callback_data for row in w.results_keyboard(SONGS).inline_keyboard
                for b in row}
        assert "sl:none" in data and "sl:cancel" in data

    def test_a_long_label_is_trimmed_deliberately(self):
        long = Song("A" * 90, "B" * 40, "C" * 40, "https://music.apple.com/1")
        label = w._result_label(long)
        assert len(label) <= 60 and label.endswith("…")

    def test_remove_buttons_show_one_based_numbers_but_carry_indices(self):
        """The admin sees "2." and the callback must remove element 1."""
        kb = w.remove_keyboard(SONGS)
        first = kb.inline_keyboard[0][0]
        second = kb.inline_keyboard[1][0]
        assert first.text.startswith("1. ") and first.callback_data == "sl:rm:0"
        assert second.text.startswith("2. ") and second.callback_data == "sl:rm:1"

    def test_remove_is_offered_only_when_there_is_something_to_remove(self):
        with_songs = {b.callback_data for row in w.confirm_keyboard(3).inline_keyboard
                      for b in row}
        empty = {b.callback_data for row in w.confirm_keyboard(0).inline_keyboard
                 for b in row}
        assert "sl:remove" in with_songs
        assert "sl:remove" not in empty

    async def test_recipients_come_from_registered_users(self):
        async def _users():
            return [{"chat_id": 2, "display_name": "Zeb"},
                    {"chat_id": 1, "display_name": "Abel"}]
        with patch("storage.get_all_users", side_effect=_users):
            kb = await w.recipient_keyboard()
        buttons = [b for row in kb.inline_keyboard for b in row
                   if b.callback_data.startswith("sl:to:")]
        assert [b.text for b in buttons] == ["Abel", "Zeb"], "sorted by name"
        assert [b.callback_data for b in buttons] == ["sl:to:1", "sl:to:2"]

    async def test_users_without_a_chat_id_are_skipped(self):
        """A bot cannot open a chat it has never had; such a button would
        silently send the list nowhere."""
        async def _users():
            return [{"display_name": "No Chat"}, {"chat_id": 0, "display_name": "Zero"},
                    {"chat_id": 5, "display_name": "Real"}]
        with patch("storage.get_all_users", side_effect=_users):
            kb = await w.recipient_keyboard()
        assert [b.callback_data for row in kb.inline_keyboard for b in row
                if b.callback_data.startswith("sl:to:")] == ["sl:to:5"]

    async def test_duplicate_chat_ids_appear_once(self):
        async def _users():
            return [{"chat_id": 5, "display_name": "A"},
                    {"chat_id": 5, "display_name": "A again"}]
        with patch("storage.get_all_users", side_effect=_users):
            kb = await w.recipient_keyboard()
        assert len([b for row in kb.inline_keyboard for b in row
                    if b.callback_data.startswith("sl:to:")]) == 1

    async def test_no_registered_users_yields_no_keyboard(self):
        async def _users():
            return []
        with patch("storage.get_all_users", side_effect=_users):
            assert await w.recipient_keyboard() is None


class TestBuildingTheList:
    async def test_the_command_is_admin_only(self):
        """A member must not be able to start this, and must not learn that
        the command exists."""
        update = _message_update()
        with patch("permissions.is_admin", return_value=False):
            result = await w.cmd_songlist(update, _context())
        assert result == ConversationHandler.END
        assert "Unknown command" in _said(update.message.reply_text)

    async def test_starting_warns_that_a_restart_loses_the_list(self):
        """The user accepted that trade, so the admin has to be told."""
        update = _message_update()
        ctx = _context()
        with patch("permissions.is_admin", return_value=True), \
             patch.object(w.activity, "log_command"):
            state = await w.cmd_songlist(update, ctx)
        said = _said(update.message.reply_text)
        assert state == w.SL_QUERY
        assert "restarts" in said and "lost" in said
        assert "/end" in said and "/undo" in said

    async def test_the_first_prompt_asks_for_song_and_artist_together(self):
        """One message, not two — and the example shows the exact form.

        The example is load-bearing, not decoration. The API scores "by" as a
        search term like any other: measured over twelve title/artist pairs,
        putting it between them moved the top hit in five and in one case
        returned a DIFFERENT ARTIST. An example written "Amazing Grace by
        CeCe Winans" would teach the admin the worse of the two inputs.
        """
        update = _message_update()
        ctx = _context()
        with patch("permissions.is_admin", return_value=True), \
             patch.object(w.activity, "log_command"):
            await w.cmd_songlist(update, ctx)
        said = _said(update.message.reply_text)
        assert "artist" in said
        assert "Amazing Grace CeCe Winans" in said
        assert "Amazing Grace by CeCe Winans" not in said

    async def test_the_whole_message_goes_to_the_search_verbatim(self):
        """No parsing, no splitting, no rejoining.

        `term` is a general search over title, artist and album at once, so
        the artist needs no separate handling. Pinned because the obvious
        "improvement" is to split on " by " and pass two fields — there is no
        second field to pass them to.
        """
        update = _message_update("Amazing Grace CeCe Winans")
        ctx = _context(songs=[])
        search = AsyncMock(return_value=SONGS)
        with patch.object(w.apple_music, "search_songs", search):
            assert await w.sl_query(update, ctx) == w.SL_PICK
        search.assert_awaited_once_with("Amazing Grace CeCe Winans")

    async def test_the_artist_is_optional(self):
        """The admin may not know it — that was explicit in the request."""
        update = _message_update("Amazing Grace")
        ctx = _context(songs=[])
        search = AsyncMock(return_value=SONGS)
        with patch.object(w.apple_music, "search_songs", search):
            assert await w.sl_query(update, ctx) == w.SL_PICK
        search.assert_awaited_once_with("Amazing Grace")

    async def test_a_title_containing_by_is_not_split(self):
        """"Saved By Grace" is a song, not a song and an artist.

        A last-" by " split would send "Saved Grace", which does not fail
        loudly — it returns five confident, wrong results (Tom Petty's
        "Saving Grace" topped the list when this was measured). So nothing may
        touch the admin's text on its way to the search.
        """
        update = _message_update("Saved By Grace Israel Houghton")
        ctx = _context(songs=[])
        search = AsyncMock(return_value=SONGS)
        with patch.object(w.apple_music, "search_songs", search):
            await w.sl_query(update, ctx)
        search.assert_awaited_once_with("Saved By Grace Israel Houghton")

    async def test_an_admin_who_types_by_anyway_still_gets_it_verbatim(self):
        """The prompt discourages "by", but nothing strips it.

        This is the mutation-killing case: every other test here uses input
        with no " by " in it, so a re-added `query.rsplit(" by ", 1)` would
        pass them all. Stripping it was considered and rejected — the measured
        cost is a slightly worse ranking, and the policy chosen was that the
        admin's text reaches the API untouched. A split here would instead
        send "Amazing Grace" alone and silently drop the artist.
        """
        update = _message_update("Amazing Grace by CeCe Winans")
        ctx = _context(songs=[])
        search = AsyncMock(return_value=SONGS)
        with patch.object(w.apple_music, "search_songs", search):
            await w.sl_query(update, ctx)
        search.assert_awaited_once_with("Amazing Grace by CeCe Winans")

    async def test_no_results_asks_for_the_song_again(self):
        update = _message_update("Nonexistent Hymn")
        ctx = _context(songs=[])
        with patch.object(w.apple_music, "search_songs",
                          AsyncMock(return_value=[])):
            assert await w.sl_query(update, ctx) == w.SL_QUERY
        said = _said(update.message.reply_text)
        assert "could not find" in said and "Nonexistent Hymn" in said

    async def test_a_blank_message_does_not_reach_the_search(self):
        """Whitespace is not a song. The API would return [] for it anyway,
        but the admin should be asked again rather than told nothing matched."""
        update = _message_update("   ")
        ctx = _context(songs=[])
        search = AsyncMock(return_value=SONGS)
        with patch.object(w.apple_music, "search_songs", search):
            assert await w.sl_query(update, ctx) == w.SL_QUERY
        search.assert_not_awaited()

    async def test_picking_a_result_adds_it_to_the_list(self):
        update = _callback_update("sl:pick:1")
        ctx = _context(songs=[], results=SONGS)
        assert await w.sl_pick(update, ctx) == w.SL_QUERY
        assert ctx.user_data[w.STATE_KEY]["songs"] == [SONGS[1]]
        assert "Great Is Thy Faithfulness" in _said(
            update.callback_query.edit_message_text)

    async def test_picking_clears_the_previous_search(self):
        """Otherwise a stale `results` list could be indexed by the next pick."""
        update = _callback_update("sl:pick:0")
        ctx = _context(songs=[], results=SONGS)
        await w.sl_pick(update, ctx)
        assert "results" not in ctx.user_data[w.STATE_KEY]

    async def test_a_pick_beyond_the_results_is_handled(self):
        update = _callback_update("sl:pick:9")
        ctx = _context(songs=[], results=SONGS[:1])
        assert await w.sl_pick(update, ctx) == w.SL_QUERY
        assert ctx.user_data[w.STATE_KEY]["songs"] == []

    async def test_search_again_keeps_the_list_and_asks_for_a_title(self):
        update = _callback_update("sl:none")
        ctx = _context(songs=[SONGS[0]], results=SONGS)
        assert await w.sl_search_again(update, ctx) == w.SL_QUERY
        assert ctx.user_data[w.STATE_KEY]["songs"] == [SONGS[0]]

    async def test_undo_drops_the_last_song(self):
        update = _message_update("/undo")
        ctx = _context(songs=list(SONGS))
        assert await w.sl_undo(update, ctx) == w.SL_QUERY
        assert ctx.user_data[w.STATE_KEY]["songs"] == SONGS[:2]
        assert "Amazing Grace (My Chains Are Gone)" in _said(
            update.message.reply_text)

    async def test_undo_on_an_empty_list_says_so(self):
        update = _message_update("/undo")
        ctx = _context(songs=[])
        assert await w.sl_undo(update, ctx) == w.SL_QUERY
        assert "Nothing to undo" in _said(update.message.reply_text)


class TestConfirming:
    async def test_end_shows_the_list_and_asks_for_confirmation(self):
        update = _message_update("/end")
        ctx = _context(songs=list(SONGS))
        assert await w.sl_end(update, ctx) == w.SL_CONFIRM
        said = _said(update.message.reply_text)
        assert "1. Amazing Grace by CeCe Winans" in said

    async def test_the_confirmation_shows_exactly_what_will_be_sent(self):
        """A preview formatted differently from the real message confirms
        something the recipient never sees."""
        update = _message_update("/end")
        ctx = _context(songs=list(SONGS))
        await w.sl_end(update, ctx)
        assert w.render_list(SONGS) in _said(update.message.reply_text)

    async def test_end_on_an_empty_list_ends_without_sending(self):
        update = _message_update("/end")
        ctx = _context(songs=[])
        assert await w.sl_end(update, ctx) == ConversationHandler.END
        assert "empty" in _said(update.message.reply_text)
        assert w.STATE_KEY not in ctx.user_data

    async def test_removing_a_song_returns_to_the_confirmation(self):
        update = _callback_update("sl:rm:1")
        ctx = _context(songs=list(SONGS))
        assert await w.sl_remove(update, ctx) == w.SL_CONFIRM
        assert ctx.user_data[w.STATE_KEY]["songs"] == [SONGS[0], SONGS[2]]

    async def test_the_list_is_renumbered_after_a_removal(self):
        """Removing song 1 of 3 must leave 1, 2 — not 1, 3."""
        update = _callback_update("sl:rm:0")
        ctx = _context(songs=list(SONGS))
        await w.sl_remove(update, ctx)
        shown = _said(update.callback_query.message.reply_text)
        assert "1. Great Is Thy Faithfulness" in shown
        assert "2. Amazing Grace (My Chains Are Gone)" in shown
        assert "3. " not in shown

    async def test_removing_the_last_remaining_song_ends_the_conversation(self):
        update = _callback_update("sl:rm:0")
        ctx = _context(songs=[SONGS[0]])
        assert await w.sl_remove(update, ctx) == ConversationHandler.END

    async def test_cancel_sends_nothing_and_clears_the_list(self):
        update = _callback_update("sl:cancel")
        ctx = _context(songs=list(SONGS))
        assert await w.sl_cancel(update, ctx) == ConversationHandler.END
        assert w.STATE_KEY not in ctx.user_data
        ctx.bot.send_message.assert_not_awaited()
        assert "Nothing was sent" in _said(
            update.callback_query.edit_message_text)


class TestSending:
    async def _send(self, songs=None, side_effect=None, chat_id=99):
        update = _callback_update(f"sl:to:{chat_id}")
        ctx = _context(songs=songs if songs is not None else list(SONGS))
        ctx.bot.send_message = AsyncMock(side_effect=side_effect)
        with patch.object(w.activity, "log_command"), \
             patch.object(w.activity, "log_error") as log_error:
            result = await w.sl_send(update, ctx)
        return result, update, ctx, log_error

    async def test_the_list_reaches_the_chosen_recipient(self):
        result, update, ctx, _ = await self._send()
        assert result == ConversationHandler.END
        ctx.bot.send_message.assert_awaited_once()
        assert ctx.bot.send_message.await_args[0][0] == 99
        assert w.render_list(SONGS) in ctx.bot.send_message.await_args[0][1]

    async def test_the_message_is_plain_text(self):
        """This project has lost messages to Markdown parse failures. Song
        titles carry parentheses and ampersands, and Telegram links a bare URL
        on its own."""
        _result, _update, ctx, _ = await self._send()
        assert "parse_mode" not in ctx.bot.send_message.await_args.kwargs

    async def test_link_previews_are_suppressed(self):
        """Eleven links would otherwise draw eleven previews."""
        _result, _update, ctx, _ = await self._send()
        assert ctx.bot.send_message.await_args.kwargs["link_preview_options"] \
            is w.NO_PREVIEW

    async def test_the_admin_is_told_it_went(self):
        _result, update, _ctx, _ = await self._send()
        assert "Sent 3 song(s)" in _said(
            update.callback_query.edit_message_text)

    async def test_the_list_is_cleared_after_sending(self):
        """Otherwise the next /songlist would start with the old songs."""
        _result, _update, ctx, _ = await self._send()
        assert w.STATE_KEY not in ctx.user_data

    async def test_a_failed_send_is_reported_to_the_admin(self):
        """The admin is standing there waiting; silence would leave them
        believing the worship leader has the list."""
        result, update, _ctx, log_error = await self._send(
            side_effect=TelegramError("bot was blocked by the user"))
        assert result == ConversationHandler.END
        said = _said(update.callback_query.edit_message_text)
        assert "could not be delivered" in said
        assert "blocked" in said
        assert log_error.called

    async def test_a_failure_on_the_first_message_says_nothing_was_sent(self):
        _result, update, _ctx, _ = await self._send(
            side_effect=TelegramError("nope"))
        assert "Nothing was sent" in _said(
            update.callback_query.edit_message_text)

    async def test_a_partial_delivery_is_reported_as_partial(self):
        """A long list is several messages. If the third fails, the recipient
        has part of the list and the admin must know that."""
        many = [Song(f"Song {i}", f"Artist {i}", "",
                     f"https://music.apple.com/us/album/song-{i}/{i}?i={i}")
                for i in range(80)]
        calls = {"n": 0}

        async def _flaky(chat_id, text, **kw):
            calls["n"] += 1
            if calls["n"] > 1:
                raise TelegramError("timeout")

        _result, update, _ctx, _ = await self._send(songs=many,
                                                   side_effect=_flaky)
        said = _said(update.callback_query.edit_message_text)
        assert "partial" in said

    async def test_sending_is_recorded_for_stats(self):
        update = _callback_update("sl:to:99")
        ctx = _context(songs=list(SONGS))
        with patch.object(w.activity, "log_command") as log_command:
            await w.sl_send(update, ctx)
        assert log_command.called
        details = str(log_command.call_args.kwargs.get("details", ""))
        assert "3 song(s)" in details and "99" in details

    async def test_an_empty_list_cannot_reach_the_recipient_step(self):
        update = _callback_update("sl:send")
        ctx = _context(songs=[])
        assert await w.sl_choose_recipient(update, ctx) == ConversationHandler.END

    async def test_no_reachable_recipient_is_explained(self):
        update = _callback_update("sl:send")
        ctx = _context(songs=list(SONGS))
        with patch.object(w, "recipient_keyboard", AsyncMock(return_value=None)):
            assert await w.sl_choose_recipient(update, ctx) == \
                ConversationHandler.END
        assert "nobody to send this to" in _said(
            update.callback_query.edit_message_text)


class TestTheWiring:
    """PTB runs only the first matching handler per group, and these patterns
    share prefixes, so the anchoring is load-bearing.
    """

    @staticmethod
    def _conv():
        return w.song_list_conversation()

    def test_rm_and_remove_route_to_different_handlers(self):
        """Two similar-looking verbs must reach the right callbacks.

        They do NOT in fact collide — `sl:rm` and `sl:re` diverge at the third
        character — and an earlier version of this test claimed they did. The
        routing is still worth pinning; the anchoring hazard it was supposed to
        demonstrate is covered by test_an_exact_button_does_not_match_a_longer_verb.
        """
        conv = self._conv()
        remove_menu = [h for h in conv.states[w.SL_CONFIRM]
                       if h.pattern.match("sl:remove")]
        assert len(remove_menu) == 1
        assert remove_menu[0].callback is w.sl_remove_menu
        indexed = [h for h in conv.states[w.SL_REMOVE]
                   if h.pattern.match("sl:rm:0")]
        assert len(indexed) == 1 and indexed[0].callback is w.sl_remove
        assert not any(h.pattern.match("sl:remove")
                       for h in conv.states[w.SL_REMOVE])

    def test_an_indexed_pattern_requires_digits(self):
        """`sl:pick:` with nothing after it must not reach int()."""
        conv = self._conv()
        # Selected by what it MATCHES, not by its pattern text: re.escape
        # leaves ":" alone on Python 3.7+, so asserting on the source regex
        # tests the escaping rules rather than the wiring.
        pick = [h for h in conv.states[w.SL_PICK]
                if h.pattern.match("sl:pick:3")]
        assert len(pick) == 1, "no single handler matches sl:pick:3"
        assert pick[0].callback is w.sl_pick
        assert not pick[0].pattern.match("sl:pick:")
        assert not pick[0].pattern.match("sl:pick:x")
        assert not pick[0].pattern.match("sl:pick:3x")

    def test_an_exact_button_does_not_match_a_longer_verb(self):
        """The real anchoring hazard. `^sl:cancel` without a trailing `$`
        matches `sl:cancelall`, and PTB runs only the first match — so a new
        verb whose name extends an existing one would silently be handled by
        the wrong callback. No such pair exists today, which is precisely why
        the next person to add a verb would not think of it.
        """
        conv = self._conv()
        cancel = [h for h in conv.states[w.SL_PICK]
                  if h.pattern.match("sl:cancel")][0]
        for longer in ("sl:cancelall", "sl:cancelled", "sl:cancel:1"):
            assert not cancel.pattern.match(longer), longer

    def test_every_exact_verb_handler_is_terminated(self):
        """Checked across all states rather than one sample, since the trap is
        a verb added later in a state nobody revisits."""
        conv = self._conv()
        for state, handlers in conv.states.items():
            for handler in handlers:
                pattern = getattr(handler, "pattern", None)
                if pattern is None:
                    continue
                assert pattern.pattern.endswith(("$", "\\d+$")), (
                    f"state {state}: {pattern.pattern} is open-ended")

    def test_the_stale_button_handler_covers_every_verb(self):
        """It is registered standalone, after the conversation, so its pattern
        must be broad enough to catch any dead tap.

        This replaces a test that asserted the handler was an ENTRY POINT —
        which is precisely what caused the production bug, since PTB checks
        entry points before state handlers when allow_reentry is true. The
        test encoded the defect as the requirement, so it passed throughout.
        """
        handler = w.stale_button_handler()
        assert handler.callback is w.sl_stale_button
        for data in ("sl:pick:0", "sl:send", "sl:remove", "sl:rm:2",
                     "sl:to:7", "sl:none", "sl:back", "sl:cancel"):
            assert handler.pattern.match(data), data

    async def test_the_stale_button_explains_rather_than_failing_silently(self):
        update = _callback_update("sl:pick:0")
        assert await w.sl_stale_button(update, _context()) == \
            ConversationHandler.END
        said = _said(update.callback_query.edit_message_text)
        assert "out of date" in said and "/songlist" in said
        # Must NOT claim a restart: the conversation also declines a tap on an
        # older keyboard while a list IS in progress, and saying "the bot
        # restarted" was wrong half the time.
        assert "restarted since" not in said
        # Must not tell an admin mid-list to start again, which would throw
        # away their work.
        assert "carry on" in said

    def test_the_command_is_the_other_entry_point(self):
        commands = [h for h in self._conv().entry_points
                    if hasattr(h, "commands")]
        assert len(commands) == 1
        assert commands[0].commands == frozenset({"songlist"})

    def test_cancel_is_a_fallback_in_every_state(self):
        conv = self._conv()
        assert any(getattr(h, "commands", None) == frozenset({"cancel"})
                   for h in conv.fallbacks)

    def test_reentry_is_allowed(self):
        """An admin who starts a second list should get one, not silence."""
        assert self._conv().allow_reentry is True

    def test_end_and_undo_are_commands_not_free_text(self):
        """A song called "end" must not finish the list."""
        handlers = w.song_list_conversation().states[w.SL_QUERY]
        commands = {c for h in handlers
                    for c in getattr(h, "commands", None) or ()}
        assert {"end", "undo"} <= commands

    def test_the_conversation_is_registered_before_the_catch_alls(self):
        """A conversation registered after the free-text handler is dead code
        that still looks registered — the /prayer bug."""
        from tests.test_board_wiring import _handlers
        descriptors = [(group, h) for group, h in _handlers()]
        group0 = [h for group, h in descriptors if group == 0]
        names = [getattr(getattr(h, "callback", None), "__name__", "")
                 for h in group0]
        songlist_at = next(
            i for i, h in enumerate(group0)
            if any(getattr(getattr(e, "callback", None), "__name__", "")
                   == "cmd_songlist"
                   for e in getattr(h, "entry_points", []) or []))
        catch_all_at = names.index("handle_counter_propose_message")
        assert songlist_at < catch_all_at


class TestPtbPicksTheRightHandler:
    """Which handler PTB ACTUALLY selects, driven through check_update.

    THE GAP THIS FILLS, AND WHY IT COST A PRODUCTION BUG
    ----------------------------------------------------
    Every other test in this file calls a callback directly — w.sl_pick(...) —
    so all 62 of them passed while the feature was broken for real users: the
    admin tapped a song and was told the bot had restarted.

    The cause was wiring, not logic. With allow_reentry=True, PTB checks
    ENTRY POINTS BEFORE state handlers, even mid-conversation:

        if state is None or self.allow_reentry:
            for entry_point in self.entry_points:   # first
        ...
        for candidate in self.states.get(state, []):  # only after

    A broad `^sl:` CallbackQueryHandler among the entry points therefore
    hijacked every live tap. board_items_conversation has the same shape and
    is safe only by accident — its entry-point callback IS its state callback,
    so whichever wins, the same function runs. That identity was the load-
    bearing part of the pattern, and it did not survive being copied.

    The snapshot test pins the ORDER of registrations; it cannot say which
    handler a given update reaches. Only check_update can.
    """

    USER = User(id=7, first_name="Bishop", is_bot=False)
    CHAT = Chat(id=7, type="private")

    @classmethod
    def _tap(cls, data: str) -> Update:
        message = Message(message_id=1, date=None, chat=cls.CHAT,
                          from_user=cls.USER)
        query = CallbackQuery(id="1", from_user=cls.USER, chat_instance="ci",
                              data=data, message=message)
        return Update(update_id=1, callback_query=query)

    @classmethod
    def _chosen(cls, conv, update):
        """The name of the callback PTB would run, or None."""
        result = conv.check_update(update)
        if not result:
            return None
        for part in result:
            name = getattr(getattr(part, "callback", None), "__name__", None)
            if name:
                return name
        return "?"

    @classmethod
    def _mid_conversation(cls, state):
        conv = w.song_list_conversation()
        conv._conversations[(cls.CHAT.id, cls.USER.id)] = state
        return conv

    def test_a_live_pick_reaches_sl_pick(self):
        """The reported bug: tapping a song said the bot had restarted."""
        conv = self._mid_conversation(w.SL_PICK)
        assert self._chosen(conv, self._tap("sl:pick:0")) == "sl_pick"

    def test_a_live_search_again_reaches_sl_search_again(self):
        conv = self._mid_conversation(w.SL_PICK)
        assert self._chosen(conv, self._tap("sl:none")) == "sl_search_again"

    def test_a_live_send_reaches_the_recipient_step(self):
        conv = self._mid_conversation(w.SL_CONFIRM)
        assert self._chosen(conv, self._tap("sl:send")) == "sl_choose_recipient"

    def test_a_live_remove_menu_tap_reaches_the_remove_menu(self):
        conv = self._mid_conversation(w.SL_CONFIRM)
        assert self._chosen(conv, self._tap("sl:remove")) == "sl_remove_menu"

    def test_a_live_removal_reaches_sl_remove(self):
        conv = self._mid_conversation(w.SL_REMOVE)
        assert self._chosen(conv, self._tap("sl:rm:1")) == "sl_remove"

    def test_a_live_recipient_choice_reaches_sl_send(self):
        conv = self._mid_conversation(w.SL_RECIPIENT)
        assert self._chosen(conv, self._tap("sl:to:424242")) == "sl_send"

    def test_a_live_cancel_reaches_sl_cancel(self):
        conv = self._mid_conversation(w.SL_PICK)
        assert self._chosen(conv, self._tap("sl:cancel")) == "sl_cancel"

    def test_no_live_tap_is_ever_treated_as_stale(self):
        """Swept across every state and button, because the bug hit ALL of
        them at once and a per-button test could have missed a state."""
        taps = {
            w.SL_PICK: ["sl:pick:0", "sl:pick:4", "sl:none", "sl:cancel"],
            w.SL_CONFIRM: ["sl:send", "sl:remove", "sl:cancel"],
            w.SL_REMOVE: ["sl:rm:0", "sl:rm:10", "sl:back", "sl:cancel"],
            w.SL_RECIPIENT: ["sl:to:1", "sl:to:474108062", "sl:cancel"],
        }
        for state, datas in taps.items():
            conv = self._mid_conversation(state)
            for data in datas:
                chosen = self._chosen(conv, self._tap(data))
                assert chosen is not None, f"state {state}: {data} unhandled"
                assert chosen != "sl_stale_button", (
                    f"state {state}: {data} was treated as a stale button")

    def test_the_command_still_re_enters_mid_conversation(self):
        """allow_reentry's actual purpose, which the fix must not cost: an
        admin who types /songlist again gets a fresh list, not silence."""
        conv = self._mid_conversation(w.SL_PICK)
        message = Message(message_id=2, date=None, chat=self.CHAT,
                          from_user=self.USER, text="/songlist",
                          entities=[MessageEntity(type="bot_command", offset=0,
                                                  length=9)])
        # CommandHandler resolves "/cmd@botname" via message.get_bot(), which
        # raises without one. Nothing here talks to Telegram.
        bot = MagicMock()
        bot.username = "christofgod_bot"
        message.set_bot(bot)
        update = Update(update_id=2, message=message)
        assert self._chosen(conv, update) == "cmd_songlist"

    def test_a_tap_with_no_conversation_is_not_handled_by_the_conversation(self):
        """The stale-button case now belongs OUTSIDE the conversation, so the
        conversation itself must decline it and let the standalone handler
        registered after it in bot.py reply."""
        conv = w.song_list_conversation()      # nothing in progress
        assert self._chosen(conv, self._tap("sl:pick:0")) is None

    def test_the_stale_button_handler_is_registered_after_the_conversation(self):
        """It must come AFTER, or it would shadow every live tap again — in a
        different way, but with the same symptom."""
        from tests.test_board_wiring import _handlers
        group0 = [h for group, h in _handlers() if group == 0]

        def index_of(predicate):
            return next(i for i, h in enumerate(group0) if predicate(h))

        conv_at = index_of(lambda h: any(
            getattr(getattr(e, "callback", None), "__name__", "") == "cmd_songlist"
            for e in getattr(h, "entry_points", []) or []))
        stale_at = index_of(lambda h: getattr(
            getattr(h, "callback", None), "__name__", "") == "sl_stale_button")
        assert conv_at < stale_at, (
            "the stale-button handler must be registered after the "
            "conversation, or it hijacks live taps")

    def test_the_conversation_has_no_callback_entry_point(self):
        """The direct guard on the regression. A CallbackQueryHandler among
        the entry points is checked BEFORE the state handlers whenever
        allow_reentry is true, which is what caused the bug."""
        conv = w.song_list_conversation()
        callback_entries = [h for h in conv.entry_points
                            if isinstance(h, CallbackQueryHandler)]
        assert callback_entries == [], (
            "a CallbackQueryHandler entry point hijacks live taps while "
            "allow_reentry is True")

"""/songlist — adding and moving songs from the confirm screen.

Offline: the search and the lookup are substituted. The tests that matter most
are the refusals: these buttons carry positions, and a position means another
song once the list has changed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import apple_music
import handlers.worship as w
from apple_music import Song
from song_matching import Line
from telegram import Message, Update
from telegram.error import BadRequest, TelegramError
from tests import test_worship_songlist as one_by_one
from tests.test_songlist_paste import (
    _buttons,
    _Context,
    _no_network,
    _tap,
    _text,
)

A, B, C, D = SONGS = [
    Song(f"Song {name}", f"Artist {name}", "Album",
         f"https://music.apple.com/us/album/x/1?i={n}",
         preview_url=f"https://audio-ssl.itunes.apple.com/{n}.m4a")
    for n, name in enumerate("ABCD", 1)
]
NEW = Song("New Song", "New Artist", "Album",
           "https://music.apple.com/us/album/x/1?i=9",
           preview_url="https://audio-ssl.itunes.apple.com/9.m4a")
MENU = 70       # the message the open menu lives in


@pytest.fixture(autouse=True)
def _apple():
    apple_music._recent_searches.clear()

    async def search(query, limit=5):
        return [NEW, D] if query.startswith("New") else []

    with patch.object(apple_music, "search_songs",
                      AsyncMock(side_effect=search)) as found, \
         patch.object(apple_music, "lookup_songs",
                      AsyncMock(return_value={"9": NEW})):
        yield found
    apple_music._recent_searches.clear()


def _ctx(*songs, **state):
    return _Context(songs=songs or SONGS, **state)


def _tapped(data, message_id=MENU):
    update = _tap(data)
    update.callback_query.message.message_id = message_id
    return update


def _edited(update):
    return update.callback_query.edit_message_text.call_args


def _confirmed(update):
    """The list as the confirm screen showed it."""
    call = update.callback_query.message.reply_text.call_args
    assert "exactly what will be sent" in call.args[0]
    return call.args[0]


class TestTheConfirmKeyboard:
    def test_add_and_move_are_offered(self):
        data = _buttons(w.confirm_keyboard(2))
        assert {"sl:send", "sl:add", "sl:move", "sl:remove", "sl:cancel"} == set(data)

    def test_one_song_has_nowhere_to_move(self):
        data = _buttons(w.confirm_keyboard(1))
        assert "sl:move" not in data and "sl:add" in data


class TestMoving:
    async def _open(self, ctx=None):
        ctx = ctx or _ctx()
        update = _tapped("sl:move")
        return ctx, update, await w.sl_move_menu(update, ctx)

    async def test_every_song_can_be_chosen(self):
        _, update, state = await self._open()
        assert state == w.SL_MOVE
        data = _buttons(_edited(update).kwargs["reply_markup"])
        assert [d for d in data if d.startswith("sl:mv:")] == [
            f"sl:mv:{n}" for n in range(4)]
        assert data["sl:mv:1"].startswith("2. Song B") and "sl:back" in data

    async def test_every_position_but_its_own_is_offered(self):
        ctx, _, _ = await self._open()
        update = _tapped("sl:mv:1")
        assert await w.sl_move_pick(update, ctx) == w.SL_MOVE
        call = _edited(update)
        assert "Song 2 is “Song B”" in call.args[0]
        assert "3. Song C" in call.args[0], "the list to count against"
        data = _buttons(call.kwargs["reply_markup"])
        assert {d: t for d, t in data.items() if "mvto" in d} == {
            "sl:mvto:1:0": "1", "sl:mvto:1:2": "3", "sl:mvto:1:3": "4"}
        assert "sl:move" in data and "sl:back" in data

    @pytest.mark.parametrize("start,end,expected", [
        (0, 3, [B, C, D, A]), (3, 0, [D, A, B, C]), (1, 2, [A, C, B, D]),
        (2, 1, [A, C, B, D]), (0, 1, [B, A, C, D]), (3, 2, [A, B, D, C]),
    ])
    async def test_it_ends_up_at_the_number_tapped(self, start, end, expected):
        ctx, _, _ = await self._open()
        update = _tapped(f"sl:mvto:{start}:{end}")
        assert await w.sl_move_to(update, ctx) == w.SL_CONFIRM
        assert ctx.state["songs"] == expected
        assert f"is now number {end + 1}" in _edited(update).args[0]
        assert _edited(update).kwargs.get("reply_markup") is None
        shown = _confirmed(update)
        assert [shown.index(s.title) for s in expected] == sorted(
            shown.index(s.title) for s in expected)

    async def test_a_different_song_can_be_chosen_instead(self):
        ctx, _, _ = await self._open()
        await w.sl_move_pick(_tapped("sl:mv:1"), ctx)
        update = _tapped("sl:move")
        assert await w.sl_move_again(update, ctx) == w.SL_MOVE
        assert "Which song should move" in _edited(update).args[0]

    async def test_back_changes_nothing_and_takes_the_buttons_away(self):
        ctx, _, _ = await self._open()
        update = _tapped("sl:back")
        assert await w.sl_back_to_confirm(update, ctx) == w.SL_CONFIRM
        assert ctx.state["songs"] == SONGS
        assert _edited(update).kwargs.get("reply_markup") is None
        _confirmed(update)

    async def test_one_song_cannot_be_moved(self):
        update = _tapped("sl:move")
        assert await w.sl_move_menu(update, _ctx(A)) is None
        update.callback_query.edit_message_text.assert_not_awaited()

    async def test_a_double_tap_is_harmless(self):
        ctx, _, _ = await self._open()
        update = _tapped("sl:mv:1")
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message is not modified: same content"))
        assert await w.sl_move_pick(update, ctx) == w.SL_MOVE

    async def test_any_other_refusal_is_not_hidden(self):
        ctx, _, _ = await self._open()
        update = _tapped("sl:mv:1")
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message to edit not found"))
        with pytest.raises(BadRequest):
            await w.sl_move_pick(update, ctx)

    async def test_a_list_too_long_to_show_still_asks(self):
        many = [Song("S" * 50, "A" * 40, "", f"https://music.apple.com/x?i={n}")
                for n in range(80)]
        ctx, _, _ = await self._open(_ctx(*many))
        update = _tapped("sl:mv:5")
        await w.sl_move_pick(update, ctx)
        text = _edited(update).args[0]
        assert len(text) <= w.MAX_MESSAGE_CHARS and "Which number" in text
        assert "1. " not in text

    def test_positions_are_five_to_a_row(self):
        rows = w.position_keyboard(12, 0).inline_keyboard
        assert [len(row) for row in rows] == [5, 5, 1, 1, 1]

    def test_callback_data_fits(self):
        assert all(len(d) <= 64 for d in _buttons(w.position_keyboard(999, 998)))


class TestOldButtonsAreRefused:
    """A position in an old keyboard may now be a different song."""

    async def _after_menu(self, opener, data):
        ctx = _ctx(results=[NEW, D], query="New")
        await opener(_tapped(data), ctx)
        return ctx

    @pytest.mark.parametrize("opener,opening,handler,data", [
        (w.sl_move_menu, "sl:move", w.sl_move_pick, "sl:mv:0"),
        (w.sl_move_menu, "sl:move", w.sl_move_to, "sl:mvto:0:3"),
        (w.sl_move_menu, "sl:move", w.sl_move_again, "sl:move"),
        (w.sl_remove_menu, "sl:remove", w.sl_remove, "sl:rm:0"),
        (w.sl_add, "sl:add", w.sl_add_pick, "sl:pick:0"),
        (w.sl_add, "sl:add", w.sl_add_preview, "sl:preview"),
        (w.sl_add, "sl:add", w.sl_add_again, "sl:none"),
    ])
    async def test_another_message_changes_nothing(
            self, opener, opening, handler, data):
        ctx = await self._after_menu(opener, opening)
        update = _tapped(data, message_id=MENU - 1)
        assert await handler(update, ctx) is None, "the state must not move"
        assert ctx.state["songs"] == SONGS and ctx.state["results"]
        update.callback_query.answer.assert_awaited_once_with(w.OUT_OF_DATE)
        update.callback_query.edit_message_text.assert_not_awaited()
        update.callback_query.message.reply_text.assert_not_awaited()
        ctx.bot.send_audio.assert_not_awaited()

    async def test_the_open_one_still_works(self):
        ctx = await self._after_menu(w.sl_remove_menu, "sl:remove")
        await w.sl_remove(_tapped("sl:rm:0"), ctx)
        assert ctx.state["songs"] == [B, C, D]

    @pytest.mark.parametrize("handler,data", [
        (w.sl_pick, "sl:pick:0"), (w.sl_preview, "sl:preview"),
        (w.sl_search_again, "sl:none"),
    ])
    async def test_nor_does_an_old_one_song_picker(self, handler, data):
        """A picker left on screen by an earlier search, e.g. by the admin
        who scrolled up: its "1" would be the newest search's first result."""
        ctx = _ctx(A, B)
        update = _text("New Song")
        update.message.reply_text = AsyncMock(
            return_value=MagicMock(message_id=71))
        assert await w.sl_query(update, ctx) == w.SL_PICK
        old = _tapped(data, message_id=70)
        assert await handler(old, ctx) is None
        assert ctx.state["songs"] == [A, B] and ctx.state["results"]
        old.callback_query.answer.assert_awaited_once_with(w.OUT_OF_DATE)
        old.callback_query.edit_message_text.assert_not_awaited()
        ctx.bot.send_audio.assert_not_awaited()

    async def test_the_newest_one_song_picker_works_as_before(self):
        ctx = _ctx(A, B)
        update = _text("New Song")
        update.message.reply_text = AsyncMock(
            return_value=MagicMock(message_id=71))
        await w.sl_query(update, ctx)
        tap = _tapped("sl:preview", 71)
        tap.callback_query.message.reply_text = AsyncMock(
            return_value=MagicMock(message_id=72))
        with _no_network():
            assert await w.sl_preview(tap, ctx) == w.SL_PICK
        assert await w.sl_pick(_tapped("sl:pick:0", 71), ctx) is None
        assert await w.sl_pick(_tapped("sl:pick:0", 72), ctx) == w.SL_QUERY
        assert ctx.state["songs"] == [A, B, NEW]

    @pytest.mark.parametrize("handler,data", [
        (w.sl_move_pick, "sl:mv:4"), (w.sl_move_to, "sl:mvto:4:0"),
        (w.sl_move_to, "sl:mvto:0:4"), (w.sl_add_pick, "sl:pick:2"),
    ])
    async def test_a_position_past_the_end_changes_nothing(self, handler, data):
        ctx = _ctx(results=[NEW, D], **{w.MENU_ID_KEY: MENU})
        update = _tapped(data)
        assert await handler(update, ctx) is None
        assert ctx.state["songs"] == SONGS
        update.callback_query.answer.assert_awaited_once_with(w.OUT_OF_DATE)

    async def test_a_pick_with_no_results_changes_nothing(self):
        ctx = _ctx(**{w.MENU_ID_KEY: MENU})
        assert await w.sl_add_pick(_tapped("sl:pick:0"), ctx) is None
        assert ctx.state["songs"] == SONGS

    def test_the_toast_fits(self):
        assert len(w.OUT_OF_DATE) <= 200


class TestAddingOne:
    async def _adding(self):
        ctx = _ctx(A, B)
        update = _tapped("sl:add")
        assert await w.sl_add(update, ctx) == w.SL_ADD
        return ctx, update

    async def _picker(self):
        ctx, _ = await self._adding()
        update = _text("New Song")
        update.message.reply_text = AsyncMock(
            return_value=MagicMock(message_id=71))
        assert await w.sl_add_text(update, ctx) == w.SL_ADD_PICK
        return ctx, update

    async def test_the_prompt_replaces_the_confirm_screen(self):
        """So its Send cannot be tapped for a list about to change."""
        _, update = await self._adding()
        assert set(_buttons(_edited(update).kwargs["reply_markup"])) == {"sl:back"}

    async def test_the_usual_picker_with_a_way_back_to_the_list(self):
        ctx, update = await self._picker()
        data = _buttons(update.message.reply_text.call_args.kwargs["reply_markup"])
        assert set(data) == {"sl:pick:0", "sl:pick:1", "sl:preview", "sl:none",
                             "sl:back"}
        assert ctx.state[w.MENU_ID_KEY] == 71

    async def test_picking_adds_it_last_and_returns_to_the_list(self):
        ctx, _ = await self._picker()
        update = _tapped("sl:pick:0", 71)
        assert await w.sl_add_pick(update, ctx) == w.SL_CONFIRM
        assert ctx.state["songs"] == [A, B, NEW]
        assert "Added 3. New Song" in _edited(update).args[0]
        assert "New Song" in _confirmed(update)
        assert not {"results", "query"} & set(ctx.state)

    async def test_previews_keep_the_way_back_and_move_the_live_message(self):
        ctx, _ = await self._picker()
        update = _tapped("sl:preview", 71)
        update.callback_query.message.reply_text = AsyncMock(
            return_value=MagicMock(message_id=72))
        with _no_network():
            assert await w.sl_add_preview(update, ctx) == w.SL_ADD_PICK
        data = _buttons(update.callback_query.message.reply_text
                        .call_args.kwargs["reply_markup"])
        assert "sl:back" in data and "sl:cancel" not in data
        assert ctx.state[w.MENU_ID_KEY] == 72
        assert len(ctx.state[w.PREVIEW_IDS_KEY]) == 2

    async def test_none_of_these_asks_again(self):
        ctx, _ = await self._picker()
        update = _tapped("sl:none", 71)
        assert await w.sl_add_again(update, ctx) == w.SL_ADD
        assert "Send the song to add" in _edited(update).args[0]
        assert "results" not in ctx.state

    async def test_not_found_stays_put(self):
        ctx, _ = await self._adding()
        update = _text("zzqq")
        assert await w.sl_add_text(update, ctx) == w.SL_ADD
        assert "could not find" in update.message.reply_text.call_args[0][0]
        assert ctx.state["songs"] == [A, B]

    async def test_back_from_the_picker_drops_the_search(self):
        ctx, _ = await self._picker()
        await w.sl_back_to_confirm(_tapped("sl:back", 71), ctx)
        assert ctx.state["songs"] == [A, B] and "results" not in ctx.state

    async def test_slash_end_goes_back_to_the_list(self):
        ctx, _ = await self._picker()
        update = _text("/end")
        assert await w.sl_add_end(update, ctx) == w.SL_CONFIRM
        assert "exactly what will be sent" in update.message.reply_text.call_args[0][0]
        assert "results" not in ctx.state and ctx.state["songs"] == [A, B]


class TestAddingSeveral:
    async def _review(self, text):
        ctx = _ctx(A, B)
        await w.sl_add(_tapped("sl:add"), ctx)
        state = await w.sl_add_text(_text(text), ctx)
        await ctx.run()
        return ctx, state

    @pytest.mark.parametrize("text", [
        "New Song\nzzqq", "https://music.apple.com/us/song/x/9"])
    async def test_a_paste_or_a_link_opens_the_review(self, text):
        ctx, state = await self._review(text)
        assert state == w.SL_REVIEW
        assert "follow the 2 song(s)" in ctx.said

    async def test_using_it_appends_and_returns_to_the_list(self):
        ctx, _ = await self._review("New Song\nzzqq")
        update = _tapped("sl:use")
        assert await w.sl_review_use(update, ctx) == w.SL_CONFIRM
        assert ctx.state["songs"] == [A, B, NEW]
        _confirmed(update)

    async def test_a_message_with_no_songs_stays_put(self):
        ctx, state = await self._review("•\n-")
        assert state == w.SL_ADD and ctx.state["songs"] == [A, B]


class TestDiscardingAPaste:
    async def _reviewing(self, songs):
        ctx = _Context(songs=songs)
        await w.sl_query(_text("New Song\nzzqq"), ctx)
        await ctx.run()
        return ctx

    async def test_offered_only_when_there_is_something_to_keep(self):
        line = Line("x", searched=True)
        assert "sl:discard" not in _buttons(w.review_keyboard([line], []))
        data = _buttons(w.review_keyboard([line], [A, B]))
        assert "keep the 2" in data["sl:discard"]

    async def test_the_earlier_songs_survive_and_the_paste_does_not(self):
        ctx = await self._reviewing([A, B])
        update = _tapped("sl:discard")
        assert await w.sl_review_discard(update, ctx) == w.SL_CONFIRM
        assert ctx.state["songs"] == [A, B] and "batch" not in ctx.state
        ctx.bot.edit_message_reply_markup.assert_awaited_once()
        _confirmed(update)

    async def test_refused_while_searching(self):
        ctx = _Context(songs=[A])
        await w.sl_query(_text("New Song\nzzqq"), ctx)
        update = _tapped("sl:discard")
        assert await w.sl_review_discard(update, ctx) == w.SL_REVIEW
        assert "Still searching" in update.callback_query.answer.call_args[0][0]
        assert "batch" in ctx.state
        ctx.drop_tasks()

    async def test_refused_when_there_is_nothing_to_go_back_to(self):
        ctx = await self._reviewing([])
        update = _tapped("sl:discard")
        assert await w.sl_review_discard(update, ctx) == w.SL_REVIEW
        assert "no other songs" in update.callback_query.answer.call_args[0][0]
        assert "batch" in ctx.state


class TestEndToEnd:
    async def test_add_then_move_then_send(self):
        ctx = _ctx(A, B)
        await w.sl_add(_tapped("sl:add"), ctx)
        update = _text("New Song")
        update.message.reply_text = AsyncMock(
            return_value=MagicMock(message_id=71))
        await w.sl_add_text(update, ctx)
        await w.sl_add_pick(_tapped("sl:pick:0", 71), ctx)
        await w.sl_move_menu(_tapped("sl:move", 72), ctx)
        await w.sl_move_pick(_tapped("sl:mv:2", 72), ctx)
        await w.sl_move_to(_tapped("sl:mvto:2:0", 72), ctx)
        await w.sl_send(_tapped("sl:to:999000001"), ctx)
        assert ctx.bot.send_message.await_args_list[0].args[1].endswith(
            w.render_list([NEW, A, B]))

    async def test_an_old_remove_screen_cannot_take_off_the_wrong_song(self):
        """Back used to leave its buttons live; Move made that matter."""
        ctx = _ctx()
        await w.sl_remove_menu(_tapped("sl:remove", 80), ctx)
        await w.sl_back_to_confirm(_tapped("sl:back", 80), ctx)
        await w.sl_move_menu(_tapped("sl:move", 81), ctx)
        await w.sl_move_to(_tapped("sl:mvto:0:3", 81), ctx)
        await w.sl_remove_menu(_tapped("sl:remove", 82), ctx)
        assert await w.sl_remove(_tapped("sl:rm:0", 80), ctx) is None
        assert ctx.state["songs"] == [B, C, D, A]


class TestPtbReachesTheHandlers:
    """check_update, not direct calls; see TestPtbPicksTheRightHandler."""

    _ptb = one_by_one.TestPtbPicksTheRightHandler
    USER, CHAT = _ptb.USER, _ptb.CHAT
    _tap, _chosen = staticmethod(_ptb._tap), staticmethod(_ptb._chosen)
    _mid_conversation = staticmethod(_ptb._mid_conversation)

    ROUTES = {
        w.SL_CONFIRM: {"sl:add": "sl_add", "sl:move": "sl_move_menu",
                       "sl:remove": "sl_remove_menu",
                       "sl:send": "sl_choose_recipient"},
        w.SL_ADD: {"sl:back": "sl_back_to_confirm"},
        w.SL_ADD_PICK: {"sl:pick:1": "sl_add_pick", "sl:none": "sl_add_again",
                        "sl:preview": "sl_add_preview",
                        "sl:back": "sl_back_to_confirm"},
        w.SL_MOVE: {"sl:mv:10": "sl_move_pick", "sl:mvto:10:1": "sl_move_to",
                    "sl:move": "sl_move_again", "sl:back": "sl_back_to_confirm"},
        w.SL_REVIEW: {"sl:discard": "sl_review_discard"},
    }

    def test_every_button_reaches_its_handler(self):
        for state, routes in self.ROUTES.items():
            conv = self._mid_conversation(state)
            for data, name in routes.items():
                assert self._chosen(conv, self._tap(data)) == name, (state, data)

    def test_no_button_is_offered_that_its_state_cannot_handle(self):
        for state, markup in (
                (w.SL_CONFIRM, w.confirm_keyboard(2)),
                (w.SL_ADD_PICK, w.results_keyboard([NEW], w._add_rows())),
                (w.SL_MOVE, w.move_keyboard(SONGS)),
                (w.SL_MOVE, w.position_keyboard(4, 1)),
                (w.SL_REVIEW, w.review_keyboard([Line("x")], SONGS))):
            conv = self._mid_conversation(state)
            for data in _buttons(markup):
                assert self._chosen(conv, self._tap(data)), (state, data)

    @pytest.mark.parametrize("data", [
        "sl:mvto:1", "sl:mvto:1:", "sl:mvto::1", "sl:mvto:1:2:3", "sl:mvto:a:1",
        "sl:mv:", "sl:mv:1:2"])
    def test_malformed_data_reaches_nothing(self, data):
        assert self._chosen(self._mid_conversation(w.SL_MOVE),
                            self._tap(data)) is None

    @pytest.mark.parametrize("state", [w.SL_QUERY, w.SL_PICK, w.SL_CONFIRM,
                                       w.SL_RECIPIENT, w.SL_REMOVE])
    def test_move_buttons_are_dead_elsewhere(self, state):
        conv = self._mid_conversation(state)
        for data in ("sl:mv:0", "sl:mvto:0:1"):
            assert self._chosen(conv, self._tap(data)) is None

    @pytest.mark.parametrize("state,name", [
        (w.SL_ADD, "sl_add_text"), (w.SL_ADD_PICK, None), (w.SL_MOVE, None),
        (w.SL_CONFIRM, None)])
    def test_typed_text(self, state, name):
        message = Message(message_id=2, date=None, chat=self.CHAT,
                          from_user=self.USER, text="New Song")
        assert self._chosen(self._mid_conversation(state),
                            Update(update_id=2, message=message)) == name

    def test_the_commands_are_wired(self):
        states = w.song_list_conversation().states
        for state in (w.SL_ADD, w.SL_ADD_PICK):
            assert any("end" in getattr(h, "commands", ())
                       and h.callback is w.sl_add_end for h in states[state])
        for state in (w.SL_ADD, w.SL_ADD_PICK, w.SL_MOVE):
            assert any("list" in getattr(h, "commands", ())
                       and h.callback is w.sl_list for h in states[state])

    def test_the_stale_handler_covers_the_new_verbs(self):
        pattern = w.stale_button_handler().pattern
        assert all(pattern.match(data) for routes in self.ROUTES.values()
                   for data in routes)


async def test_a_failed_prompt_leaves_the_conversation_where_it_was():
    """An exception means no state change, so nothing may have been opened."""
    ctx = _ctx()
    for handler, data in ((w.sl_add, "sl:add"), (w.sl_move_menu, "sl:move"),
                          (w.sl_remove_menu, "sl:remove")):
        update = _tapped(data)
        update.callback_query.edit_message_text = AsyncMock(
            side_effect=TelegramError("no"))
        with pytest.raises(TelegramError):
            await handler(update, ctx)
        assert w.MENU_ID_KEY not in ctx.state

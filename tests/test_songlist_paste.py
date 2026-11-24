"""/songlist — pasting a whole list, reviewing it, fixing a line.

Offline: apple_music.search_songs is substituted throughout. The background
tasks are captured rather than scheduled, so a test decides when they run and
what has happened meanwhile — the interleavings are the point.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import apple_music
import handlers.worship as w
import song_matching
from apple_music import Song
from song_matching import Line
from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, ConversationHandler
from tests import test_worship_songlist as one_by_one
from tests.test_worship_songlist import (
    _callback_update,
    _message_update,
    _no_network,
)

CHAT = 4242


def _song(title, artist="CeCe Winans", n=1, clip=True):
    return Song(title, artist, "The Hymns",
                f"https://music.apple.com/us/album/x/{n}?i={n}",
                preview_url=(f"https://audio-ssl.itunes.apple.com/{n}.m4a"
                             if clip else ""),
                duration_ms=240000, release_year=2021)


GRACE = _song("Amazing Grace", n=1)
GRACE2 = _song("Amazing Grace", "Alan Jackson", n=2)
WAY = _song("Way Maker", "Sinach", n=3)
KNOWN = {"amazing grace winans": [GRACE, GRACE2], "way maker sinach": [WAY]}


async def _search(query, limit=5):
    return list(KNOWN.get(query.casefold(), []))


@pytest.fixture(autouse=True)
def _searching():
    apple_music._recent_searches.clear()
    with patch.object(w.apple_music, "search_songs",
                      AsyncMock(side_effect=_search)) as search:
        yield search
    apple_music._recent_searches.clear()


class _Context:
    """A context whose background tasks wait until `run()`."""

    def __init__(self, songs=None, **state):
        self.user_data = {w.STATE_KEY: {"songs": list(songs or []), **state}}
        self.tasks = []
        self.application = MagicMock(running=True)
        self.application.create_task = (
            lambda coro, update=None, **kw: self.tasks.append(coro))
        self.bot = MagicMock()
        ids = iter(range(9000, 9999))
        for name in ("send_message", "send_audio"):
            setattr(self.bot, name, AsyncMock(
                side_effect=lambda *a, **k: MagicMock(message_id=next(ids))))
        for name in ("edit_message_reply_markup", "delete_messages",
                     "delete_message"):
            setattr(self.bot, name, AsyncMock())

    @property
    def state(self):
        return self.user_data[w.STATE_KEY]

    @property
    def batch(self):
        return self.state["batch"]

    async def run(self):
        while self.tasks:
            await self.tasks.pop(0)

    def drop_tasks(self):
        for coro in self.tasks:
            coro.close()
        self.tasks.clear()

    @property
    def said(self):
        return "\n".join(c.args[1] for c in self.bot.send_message.call_args_list)

    @property
    def last(self):
        return self.bot.send_message.call_args


def _progress():
    return MagicMock(message_id=1, delete=AsyncMock(), edit_text=AsyncMock())


def _text(text):
    update = _message_update(text)
    update.effective_chat.id = CHAT
    update.message.reply_text = AsyncMock(side_effect=lambda *a, **k: _progress())
    return update


def _tap(data):
    update = _callback_update(data)
    update.effective_chat.id = CHAT
    update.callback_query.message.chat_id = CHAT
    update.callback_query.message.reply_text = AsyncMock(
        side_effect=lambda *a, **k: _progress())
    update.callback_query.edit_message_reply_markup = AsyncMock()
    return update


def _buttons(markup):
    return {b.callback_data: b.text for row in markup.inline_keyboard for b in row}


async def _reviewing(text="Amazing Grace Winans\nWay Maker Sinach\nzzqqx",
                     ctx=None):
    ctx = ctx or _Context()
    assert await w.sl_query(_text(text), ctx) == w.SL_REVIEW
    await ctx.run()
    return ctx


class TestWhatCountsAsAPaste:
    async def test_one_line_behaves_as_before(self):
        ctx = _Context()
        assert await w.sl_query(_text("Amazing Grace Winans"), ctx) == w.SL_PICK
        assert "batch" not in ctx.state and not ctx.tasks

    async def test_two_lines_start_a_review(self):
        ctx = _Context()
        assert await w.sl_query(_text("a\nb"), ctx) == w.SL_REVIEW
        assert [line.query for line in ctx.batch.lines] == ["a", "b"]
        ctx.drop_tasks()

    async def test_nothing_but_bullets_asks_again(self):
        ctx = _Context()
        update = _text("•\n-")
        assert await w.sl_query(update, ctx) == w.SL_QUERY
        assert "batch" not in ctx.state and not ctx.tasks
        assert "could not find a song name" in update.message.reply_text.call_args[0][0]

    async def test_the_opening_prompt_offers_it(self):
        tree = ast.parse(inspect.getsource(w))
        body = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "cmd_songlist")
        assert any("Paste the whole list" in n.value for n in ast.walk(body)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))


class TestTheSearchIsInTheBackground:
    async def test_the_handler_searches_nothing_itself(self, _searching):
        """One handler runs at a time for the whole bot, so a handler that
        awaited the searches would leave every member unanswered."""
        ctx = _Context()
        await w.sl_query(_text("a\nb"), ctx)
        _searching.assert_not_awaited()
        assert len(ctx.tasks) == 1 and ctx.batch.busy
        ctx.drop_tasks()

    async def test_every_line_is_searched_in_order(self, _searching):
        await _reviewing()
        assert [c.args[0] for c in _searching.await_args_list] == [
            "Amazing Grace Winans", "Way Maker Sinach", "zzqqx"]

    async def test_it_is_idle_afterwards(self):
        assert (await _reviewing()).batch.busy == ""

    async def test_it_is_idle_even_if_the_search_breaks(self, _searching):
        _searching.side_effect = RuntimeError("boom")
        ctx = _Context()
        await w.sl_query(_text("a\nb"), ctx)
        with pytest.raises(RuntimeError):
            await ctx.run()
        assert ctx.batch.busy == ""

    async def test_a_failed_first_message_leaves_nothing_busy(self):
        ctx = _Context()
        update = _text("a\nb")
        update.message.reply_text = AsyncMock(side_effect=TelegramError("no"))
        with pytest.raises(TelegramError):
            await w.sl_query(update, ctx)
        assert ctx.batch.busy == "" and not ctx.tasks

    async def test_the_progress_message_goes_away(self):
        ctx, update = _Context(), _text("a\nb")
        shown = _progress()
        update.message.reply_text = AsyncMock(return_value=shown)
        await w.sl_query(update, ctx)
        await ctx.run()
        shown.delete.assert_awaited_once()

    async def test_failing_to_delete_it_does_not_cost_the_review(self):
        ctx, update = _Context(), _text("a\nb")
        shown = _progress()
        shown.delete = AsyncMock(side_effect=TelegramError("gone"))
        update.message.reply_text = AsyncMock(return_value=shown)
        await w.sl_query(update, ctx)
        await ctx.run()
        assert "Please check" in ctx.said or "found nothing" in ctx.said

    async def test_progress_is_edited_but_not_on_every_line(self):
        ctx, update = _Context(), _text("\n".join("abcdefgh"))
        shown = _progress()
        update.message.reply_text = AsyncMock(return_value=shown)
        await w.sl_query(update, ctx)
        clock = iter(range(1000))
        with patch.object(w.time, "monotonic", lambda: next(clock) * 1.0), \
             patch.object(w.apple_music, "search_delay", lambda: 0.0):
            await ctx.run()
        # A second per line against PROGRESS_SECONDS = 1.5: every other line.
        assert shown.edit_text.await_count == 4

    async def test_a_failed_edit_does_not_stop_the_search(self, _searching):
        ctx, update = _Context(), _text("a\nb")
        shown = _progress()
        shown.edit_text = AsyncMock(side_effect=TelegramError("slow down"))
        update.message.reply_text = AsyncMock(return_value=shown)
        await w.sl_query(update, ctx)
        with patch.object(w, "PROGRESS_SECONDS", 0):
            await ctx.run()
        assert _searching.await_count == 2


class TestTheSearchStopsWhenUnwanted:
    async def _stops(self, _searching, interrupt):
        ctx = _Context()
        await w.sl_query(_text("a\nb\nc"), ctx)

        async def once(query, limit=5):
            interrupt(ctx)
            return [GRACE]

        _searching.side_effect = once
        await ctx.run()
        assert _searching.await_count == 1
        ctx.bot.send_message.assert_not_awaited()

    async def test_slash_cancel(self, _searching):
        await self._stops(_searching, lambda c: c.user_data.clear())

    async def test_starting_again(self, _searching):
        def again(c):
            c.user_data[w.STATE_KEY] = {"songs": []}
        await self._stops(_searching, again)

    async def test_starting_again_with_another_paste(self, _searching):
        def again(c):
            c.user_data[w.STATE_KEY] = {"songs": [], "batch": w.Batch([])}
        await self._stops(_searching, again)

    async def test_the_batch_being_used(self, _searching):
        await self._stops(_searching, lambda c: c.state.pop("batch"))

    async def test_the_bot_shutting_down(self, _searching):
        """Application.stop() waits for these tasks."""
        def stop(c):
            c.application.running = False
        await self._stops(_searching, stop)

    async def test_a_cancel_during_the_last_search_posts_nothing(self, _searching):
        ctx = _Context()
        await w.sl_query(_text("a\nb"), ctx)

        async def cancel_on_b(query, limit=5):
            if query == "b":
                ctx.user_data.clear()
            return []

        _searching.side_effect = cancel_on_b
        await ctx.run()
        ctx.bot.send_message.assert_not_awaited()

    async def test_cancelling_during_a_throttled_wait(self, _searching):
        ctx = _Context()
        await w.sl_query(_text("a\nb"), ctx)

        async def nap(seconds):
            assert seconds <= 1.0, "a long nap would hold up a shutdown"
            ctx.user_data.clear()

        with patch.object(w.apple_music, "search_delay", lambda: 30.0), \
             patch.object(w.asyncio, "sleep", nap):
            await ctx.run()
        _searching.assert_not_awaited()


class TestTheThrottle:
    async def test_the_wait_is_shown_once_and_then_honoured(self, _searching):
        ctx, update = _Context(), _text("a\nb")
        shown = _progress()
        update.message.reply_text = AsyncMock(return_value=shown)
        await w.sl_query(update, ctx)
        delays = iter([2.4, 1.4, 0.4, 0.0, 0.0])
        naps = []

        async def nap(seconds):
            naps.append(seconds)
            assert _searching.await_count == 0

        with patch.object(w.apple_music, "search_delay", lambda: next(delays)), \
             patch.object(w.asyncio, "sleep", nap):
            await ctx.run()
        assert naps == [1.0, 1.0, 0.4]
        waits = [c.args[0] for c in shown.edit_text.await_args_list
                 if "Waiting" in c.args[0]]
        assert len(waits) == 1 and "2 seconds" in waits[0]
        assert "1 of 2" in waits[0]
        assert _searching.await_count == 2

    def test_under_the_limit_there_is_no_wait(self):
        with patch.object(apple_music.time, "monotonic", lambda: 100.0):
            apple_music._recent_searches.extend([99.0] * 17)
            assert apple_music.search_delay() == 0.0

    def test_at_the_limit_it_waits_for_the_oldest(self):
        apple_music._recent_searches.extend([50.0] + [99.0] * 17)
        with patch.object(apple_music.time, "monotonic", lambda: 100.0):
            assert apple_music.search_delay() == 10.0

    def test_over_the_limit_the_oldest_is_not_enough(self):
        """Typed searches are never held back, so the window can overflow."""
        apple_music._recent_searches.extend([50.0, 70.0] + [99.0] * 17)
        with patch.object(apple_music.time, "monotonic", lambda: 100.0):
            assert apple_music.search_delay() == 30.0

    def test_old_searches_are_forgotten(self):
        apple_music._recent_searches.extend([1.0] * 30 + [40.0])
        with patch.object(apple_music.time, "monotonic", lambda: 99.0):
            assert apple_music.search_delay() == 0.0
            assert list(apple_music._recent_searches) == [40.0]

    async def test_a_typed_search_is_counted_but_never_waits(self):
        apple_music._recent_searches.extend([99.0] * 50)
        client = MagicMock()
        client.return_value.__aenter__ = AsyncMock(side_effect=OSError("offline"))
        client.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(apple_music.httpx, "AsyncClient", client), \
             patch.object(apple_music.time, "monotonic", lambda: 100.0):
            assert await _REAL_SEARCH("amazing grace") == []
        assert len(apple_music._recent_searches) == 51

    async def test_an_empty_search_is_not_counted(self):
        assert await _REAL_SEARCH("  ") == []
        assert not apple_music._recent_searches

    def test_it_leaves_room_under_apples_limit(self):
        assert apple_music.PASTED_SEARCHES_PER_MINUTE < 20


_REAL_SEARCH = apple_music.search_songs


class TestTheReview:
    async def test_typed_and_found_side_by_side(self):
        ctx = await _reviewing()
        assert "✓ 1. <b>Amazing Grace</b> — CeCe Winans" in ctx.said
        assert "The Hymns · 4:00 · 2021" in ctx.said
        assert "you typed: Amazing Grace Winans" in ctx.said

    async def test_a_missing_song_is_numbered_in_place(self):
        ctx = await _reviewing("zzqqx\nWay Maker Sinach")
        assert "⚠️ 1. Not found" in ctx.said
        assert "you typed: zzqqx" in ctx.said and "✓ 2. <b>Way Maker" in ctx.said

    async def test_a_doubt_is_spelled_out(self, _searching):
        _searching.side_effect = AsyncMock(return_value=[GRACE])
        ctx = await _reviewing("amazing grace tomlin\nx")
        assert "❓ 1." in ctx.said
        assert "“tomlin” is not in this match" in ctx.said

    async def test_it_is_html_with_no_link_previews(self):
        kwargs = (await _reviewing()).last.kwargs
        assert kwargs["parse_mode"] == ParseMode.HTML
        assert kwargs["link_preview_options"] is w.NO_PREVIEW

    async def test_what_was_typed_is_escaped(self):
        ctx = await _reviewing("Rock & <b>Roll\nx")
        assert "Rock &amp; &lt;b&gt;Roll" in ctx.said

    async def test_a_note_is_escaped(self, _searching):
        odd = _song("Grace (feat. Winans)", "Tom & <i>Co")
        _searching.side_effect = AsyncMock(return_value=[odd])
        ctx = await _reviewing("grace winans\nx")
        assert "the artist is Tom &amp; &lt;i&gt;Co" in ctx.said

    async def test_a_very_long_line_is_shortened_on_screen_only(self, _searching):
        long = "grace " * 60
        ctx = await _reviewing(long + "\nx")
        assert "…" in ctx.said and long.strip() not in ctx.said
        assert _searching.await_args_list[0].args[0] == long.strip()

    async def test_nothing_is_added_to_the_list_yet(self):
        assert (await _reviewing()).state["songs"] == []

    async def test_all_missing_says_the_search_may_be_down(self):
        ctx = await _reviewing("qq\nzz")
        assert "may be down" in ctx.said and "sl:retry" in _buttons(
            ctx.last.kwargs["reply_markup"])
        assert "sl:use" not in _buttons(ctx.last.kwargs["reply_markup"])

    async def test_one_missing_does_not(self):
        assert "may be down" not in (await _reviewing()).said

    def test_dropped_lines_do_not_hide_an_outage(self):
        lines = [Line("a", dropped=True), Line("b", searched=True)]
        assert "may be down" in w.review_heading(lines, [])

    def test_all_dropped_is_not_an_outage(self):
        assert "may be down" not in w.review_heading(
            [Line("a", dropped=True)], [])

    async def test_songs_already_chosen_are_mentioned(self):
        ctx = await _reviewing(ctx=_Context(songs=[WAY]))
        assert "follow the 1 song(s)" in ctx.said
        assert "already in your list" in ctx.said

    async def test_a_long_review_is_split_with_buttons_on_the_last(self):
        ctx = await _reviewing("\n".join(f"unknown song {n}" + "x" * 60
                                         for n in range(60)))
        calls = ctx.bot.send_message.await_args_list
        assert len(calls) > 1
        assert all(len(c.args[1]) <= w.MAX_MESSAGE_CHARS for c in calls)
        assert [c.kwargs["reply_markup"] is not None for c in calls] == (
            [False] * (len(calls) - 1) + [True])

    async def test_the_message_with_the_buttons_is_remembered(self):
        ctx = await _reviewing()
        assert ctx.batch.live_id == 9000


class TestTheReviewKeyboard:
    @staticmethod
    def _line(song=None, **kw):
        line = Line("q")
        line.record([song] if song else [])
        line.confirmed = True
        for key, value in kw.items():
            setattr(line, key, value)
        return line

    def test_a_number_per_song_marked_where_it_matters(self):
        data = _buttons(w.review_keyboard([self._line(GRACE), self._line()], []))
        assert data["sl:fix:0"] == "1" and data["sl:fix:1"] == "⚠️ 2"

    def test_use_counts_what_goes_in_and_what_does_not(self):
        lines = [self._line(GRACE), self._line(), self._line(WAY, dropped=True)]
        assert _buttons(w.review_keyboard(lines, []))["sl:use"] == \
            "✅ Use 1 song(s), skip 2"

    def test_use_does_not_mention_skipping_when_nothing_is(self):
        assert _buttons(w.review_keyboard([self._line(GRACE)], []))["sl:use"] == \
            "✅ Use 1 song(s)"

    def test_no_retry_when_nothing_is_missing(self):
        lines = [self._line(GRACE), self._line(dropped=True)]
        assert "sl:retry" not in _buttons(w.review_keyboard(lines, []))

    def test_retry_covers_a_search_that_was_interrupted(self):
        assert "sl:retry" in _buttons(
            w.review_keyboard([self._line(GRACE), Line("x")], []))

    def test_hear_needs_a_clip(self):
        silent = _song("A", clip=False)
        assert "sl:hear" not in _buttons(
            w.review_keyboard([self._line(silent)], []))
        assert "sl:hear" in _buttons(w.review_keyboard([self._line(GRACE)], []))

    def test_cancel_is_always_there(self):
        assert "sl:cancel" in _buttons(w.review_keyboard([self._line()], []))

    def test_rows_are_five_wide(self):
        rows = w.review_keyboard([self._line() for _ in range(11)], [])
        widths = [len(r) for r in rows.inline_keyboard]
        assert widths[:3] == [5, 5, 1]

    @pytest.mark.parametrize("missing", [0, 3, 200])
    def test_never_more_buttons_than_telegram_takes(self, missing):
        lines = [self._line(_song(f"S{n}", n=n)) for n in range(150)]
        lines += [self._line() for _ in range(missing)]
        data = _buttons(w.review_keyboard(lines, []))
        assert len(data) <= 100
        fixes = [d for d in data if d.startswith("sl:fix:")]
        assert len(fixes) == min(missing, w.MAX_NUMBER_BUTTONS)

    def test_a_list_at_the_limit_keeps_every_button(self):
        lines = [self._line(_song(f"S{n}", n=n))
                 for n in range(w.MAX_NUMBER_BUTTONS)]
        data = _buttons(w.review_keyboard(lines, []))
        assert sum(d.startswith("sl:fix:") for d in data) == w.MAX_NUMBER_BUTTONS
        assert "have a button" not in w.review_heading(lines, [])

    def test_a_list_past_it_says_so(self):
        lines = [self._line(GRACE)] * (w.MAX_NUMBER_BUTTONS + 1)
        assert "have a button" in w.review_heading(lines, [])

    def test_callback_data_fits(self):
        lines = [self._line()] * 4096
        assert all(len(d.encode()) <= 64
                   for d in _buttons(w.review_keyboard(lines, [])))


class TestUsingTheReview:
    async def test_the_button_goes_to_the_usual_confirmation(self):
        ctx = await _reviewing()
        update = _tap("sl:use")
        assert await w.sl_review_use(update, ctx) == w.SL_CONFIRM
        assert ctx.state["songs"] == [GRACE, WAY]
        assert "exactly what will be sent" in \
            update.callback_query.message.reply_text.call_args[0][0]

    async def test_it_appends_to_songs_already_chosen(self):
        ctx = await _reviewing(ctx=_Context(songs=[GRACE2]))
        await w.sl_review_use(_tap("sl:use"), ctx)
        assert ctx.state["songs"] == [GRACE2, GRACE, WAY]

    async def test_slash_end_means_the_same(self):
        ctx = await _reviewing()
        assert await w.sl_review_end(_text("/end"), ctx) == w.SL_CONFIRM
        assert ctx.state["songs"] == [GRACE, WAY]

    async def test_the_batch_is_finished_with_and_its_buttons_removed(self):
        ctx = await _reviewing()
        await w.sl_review_use(_tap("sl:use"), ctx)
        assert "batch" not in ctx.state
        ctx.bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=CHAT, message_id=9000, reply_markup=None)

    async def test_nothing_to_use_stays_put(self):
        ctx = await _reviewing("qq\nzz")
        update = _text("/end")
        assert await w.sl_review_end(update, ctx) == w.SL_REVIEW
        assert "None of these" in update.message.reply_text.call_args[0][0]
        assert ctx.batch.lines

    async def test_nothing_new_but_songs_already_chosen_goes_on(self):
        ctx = await _reviewing("qq\nzz", ctx=_Context(songs=[WAY]))
        assert await w.sl_review_end(_text("/end"), ctx) == w.SL_CONFIRM
        assert ctx.state["songs"] == [WAY]


class TestWhileItIsBusy:
    @pytest.fixture
    async def ctx(self):
        ctx = _Context()
        await w.sl_query(_text("a\nb"), ctx)
        yield ctx
        ctx.drop_tasks()

    @pytest.mark.parametrize("handler,data", [
        (w.sl_review_use, "sl:use"), (w.sl_review_fix, "sl:fix:0"),
        (w.sl_review_retry, "sl:retry"), (w.sl_review_hear, "sl:hear"),
    ])
    async def test_a_tap_changes_nothing(self, ctx, handler, data):
        update = _tap(data)
        assert await handler(update, ctx) == w.SL_REVIEW
        assert "searching" in update.callback_query.answer.call_args[0][0]
        assert len(update.callback_query.answer.call_args[0][0]) <= 200
        assert len(ctx.tasks) == 1 and "fixing" not in ctx.state
        assert ctx.state["songs"] == [] and "batch" in ctx.state

    async def test_text_is_not_swallowed_silently(self, ctx):
        update = _text("more")
        assert await w.sl_review_text(update, ctx) == w.SL_REVIEW
        assert "Send that again" in update.message.reply_text.call_args[0][0]
        assert len(ctx.batch.lines) == 2 and len(ctx.tasks) == 1

    @pytest.mark.parametrize("handler", [w.sl_review_end, w.sl_review_list])
    async def test_the_commands_wait_too(self, ctx, handler):
        update = _text("/x")
        assert await handler(update, ctx) == w.SL_REVIEW
        assert "Still searching" in update.message.reply_text.call_args[0][0]
        assert ctx.state["songs"] == [] and "batch" in ctx.state


class TestAddingFromTheReview:
    async def test_one_line_is_added_and_only_it_is_searched(self, _searching):
        ctx = await _reviewing("qq\nzz")
        _searching.reset_mock()
        assert await w.sl_review_text(_text("Way Maker Sinach"), ctx) == w.SL_REVIEW
        await ctx.run()
        assert [c.args[0] for c in _searching.await_args_list] == [
            "Way Maker Sinach"]
        assert ctx.batch.lines[2].song is WAY

    async def test_a_paste_is_added_too(self):
        ctx = await _reviewing("qq\nzz")
        await w.sl_review_text(_text("1. Way Maker Sinach\n2. x"), ctx)
        await ctx.run()
        assert [line.query for line in ctx.batch.lines] == [
            "qq", "zz", "Way Maker Sinach", "x"]

    async def test_the_old_review_loses_its_buttons(self):
        ctx = await _reviewing()
        await w.sl_review_text(_text("x"), ctx)
        ctx.bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=CHAT, message_id=9000, reply_markup=None)
        await ctx.run()
        assert ctx.batch.live_id == 9001


class TestRetrying:
    async def test_only_the_missing_are_searched_again(self, _searching):
        ctx = await _reviewing()
        _searching.reset_mock()
        _searching.side_effect = AsyncMock(return_value=[GRACE2])
        assert await w.sl_review_retry(_tap("sl:retry"), ctx) == w.SL_REVIEW
        await ctx.run()
        assert [c.args[0] for c in _searching.await_args_list] == ["zzqqx"]
        assert ctx.batch.lines[2].song is GRACE2
        assert ctx.batch.lines[0].song is GRACE

    async def test_a_dropped_line_is_not_searched_again(self, _searching):
        ctx = await _reviewing()
        ctx.batch.lines[2].dropped = True
        _searching.reset_mock()
        await w.sl_review_retry(_tap("sl:retry"), ctx)
        await ctx.run()
        _searching.assert_not_awaited()

    async def test_nor_is_one_dropped_before_it_was_ever_searched(self, _searching):
        ctx = await _reviewing()
        ctx.batch.lines.append(Line("interrupted", dropped=True))
        _searching.reset_mock()
        await w.sl_review_retry(_tap("sl:retry"), ctx)
        await ctx.run()
        assert [c.args[0] for c in _searching.await_args_list] == ["zzqqx"]


class TestFixingALine:
    async def _open(self, index=0, ctx=None):
        ctx = ctx or await _reviewing()
        update = _tap(f"sl:fix:{index}")
        return ctx, update, await w.sl_review_fix(update, ctx)

    async def test_the_usual_picker_opens(self):
        ctx, update, state = await self._open()
        assert state == w.SL_FIX
        call = update.callback_query.message.reply_text.call_args
        assert "2 match(es)" in call.args[0]
        data = _buttons(call.kwargs["reply_markup"])
        assert {"sl:pick:0", "sl:pick:1", "sl:preview", "sl:none", "sl:drop",
                "sl:review"} == set(data)

    async def test_there_is_no_cancel_the_whole_list_beside_one_song(self):
        assert "sl:cancel" not in _buttons(w.results_keyboard([GRACE], w._fix_rows()))
        assert "sl:cancel" in _buttons(w.results_keyboard([GRACE]))

    async def test_picking_replaces_only_that_line(self):
        ctx, _, _ = await self._open()
        assert await w.sl_fix_pick(_tap("sl:pick:1"), ctx) == w.SL_REVIEW
        assert [line.song for line in ctx.batch.lines] == [GRACE2, WAY, None]
        assert ctx.batch.lines[0].confirmed
        assert not ctx.batch.lines[1].confirmed

    async def test_picking_reposts_the_review(self):
        ctx, _, _ = await self._open()
        ctx.bot.send_message.reset_mock()
        await w.sl_fix_pick(_tap("sl:pick:1"), ctx)
        assert "Alan Jackson" in ctx.said and "sl:use" in _buttons(
            ctx.last.kwargs["reply_markup"])

    async def test_picking_clears_the_pickers_state(self):
        ctx, _, _ = await self._open()
        await w.sl_fix_pick(_tap("sl:pick:1"), ctx)
        assert not {"fixing", "results", "query"} & set(ctx.state)

    async def test_an_impossible_pick_changes_nothing(self):
        ctx, _, _ = await self._open()
        assert await w.sl_fix_pick(_tap("sl:pick:9"), ctx) == w.SL_REVIEW
        assert ctx.batch.lines[0].song is GRACE

    async def test_an_impossible_number_changes_nothing(self):
        ctx = await _reviewing()
        for index in (3, 99):
            update = _tap(f"sl:fix:{index}")
            assert await w.sl_review_fix(update, ctx) == w.SL_REVIEW
            assert "not in this list" in \
                update.callback_query.message.reply_text.call_args[0][0]
        assert "fixing" not in ctx.state

    async def test_only_one_keyboard_is_ever_live(self):
        """The buttons say `drop`, not which song they were drawn for."""
        ctx, update, _ = await self._open()
        ctx.bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=CHAT, message_id=9000, reply_markup=None)
        assert ctx.batch.live_id == 1           # the picker just posted
        await w.sl_review_list(_text("/list"), ctx)
        assert ctx.bot.edit_message_reply_markup.await_args.kwargs[
            "message_id"] == 1

    async def test_a_failure_to_strip_a_keyboard_is_survived(self):
        ctx = await _reviewing()
        ctx.bot.edit_message_reply_markup = AsyncMock(
            side_effect=TelegramError("too old"))
        _, _, state = await self._open(ctx=ctx)
        assert state == w.SL_FIX

    async def test_a_reposted_picker_becomes_the_live_keyboard(self):
        ctx, _, _ = await self._open()
        update = _tap("sl:preview")
        update.callback_query.message.reply_text = AsyncMock(
            return_value=MagicMock(message_id=55))
        with _no_network():
            await w.sl_fix_preview(update, ctx)
        assert ctx.batch.live_id == 55

    async def test_a_picker_already_edited_away_is_not_stripped_again(self):
        """Telegram refuses that edit, which would log a false warning."""
        ctx, _, _ = await self._open()
        ctx.bot.edit_message_reply_markup.reset_mock()
        await w.sl_fix_pick(_tap("sl:pick:1"), ctx)
        ctx.bot.edit_message_reply_markup.assert_not_awaited()

    async def test_back_changes_nothing(self):
        ctx, _, _ = await self._open()
        assert await w.sl_fix_back(_tap("sl:review"), ctx) == w.SL_REVIEW
        assert ctx.batch.lines[0].song is GRACE
        assert not ctx.batch.lines[0].confirmed

    async def test_leaving_out_and_putting_back(self):
        ctx, _, _ = await self._open()
        assert await w.sl_fix_drop(_tap("sl:drop"), ctx) == w.SL_REVIEW
        assert "🚫 1. Left out" in ctx.said
        assert ctx.batch.lines[0].song is None
        _, _, state = await self._open(ctx=ctx)
        assert state == w.SL_FIX
        await w.sl_fix_pick(_tap("sl:pick:0"), ctx)
        assert ctx.batch.lines[0].song is GRACE

    async def test_list_returns_to_the_review(self):
        ctx, _, _ = await self._open()
        assert await w.sl_review_list(_text("/list"), ctx) == w.SL_REVIEW
        assert "fixing" not in ctx.state

    async def test_previews_reuse_the_clips_and_keep_the_fix_buttons(self):
        ctx, _, _ = await self._open()
        update = _tap("sl:preview")
        with _no_network():
            assert await w.sl_fix_preview(update, ctx) == w.SL_FIX
        assert ctx.bot.send_audio.await_count == 2
        assert len(ctx.state[w.PREVIEW_IDS_KEY]) == 2
        data = _buttons(update.callback_query.message.reply_text
                        .call_args.kwargs["reply_markup"])
        assert "sl:drop" in data and "sl:cancel" not in data


class TestRetypingALine:
    async def _retyping(self):
        ctx = await _reviewing()
        update = _tap("sl:fix:2")
        assert await w.sl_review_fix(update, ctx) == w.SL_RETYPE
        return ctx, update

    async def test_a_missing_line_is_asked_for_again(self):
        _, update = await self._retyping()
        call = update.callback_query.message.reply_text.call_args
        assert "Send song 3 again" in call.args[0] and "zzqqx" in call.args[0]
        assert set(_buttons(call.kwargs["reply_markup"])) == {
            "sl:drop", "sl:review"}

    async def test_the_prompt_is_the_live_keyboard(self):
        ctx, _ = await self._retyping()
        assert ctx.batch.live_id == 1

    async def test_the_next_message_opens_the_picker_for_that_line(self):
        ctx, _ = await self._retyping()
        assert await w.sl_fix_text(_text("Way Maker Sinach"), ctx) == w.SL_FIX
        assert ctx.batch.lines[2].query == "zzqqx", "not until one is picked"
        await w.sl_fix_pick(_tap("sl:pick:0"), ctx)
        assert ctx.batch.lines[2].query == "Way Maker Sinach"
        assert [line.song for line in ctx.batch.lines] == [GRACE, WAY, WAY]

    async def test_the_prompt_loses_its_buttons_when_answered(self):
        ctx, _ = await self._retyping()
        ctx.bot.edit_message_reply_markup.reset_mock()
        await w.sl_fix_text(_text("Way Maker Sinach"), ctx)
        ctx.bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=CHAT, message_id=1, reply_markup=None)

    async def test_its_picker_becomes_the_live_keyboard(self):
        ctx, _ = await self._retyping()
        update = _text("Way Maker Sinach")
        update.message.reply_text = AsyncMock(
            return_value=MagicMock(message_id=56))
        await w.sl_fix_text(update, ctx)
        assert ctx.batch.live_id == 56

    async def test_still_not_found_asks_again(self):
        ctx, _ = await self._retyping()
        update = _text("nope")
        assert await w.sl_fix_text(update, ctx) == w.SL_RETYPE
        assert "could not find" in update.message.reply_text.call_args[0][0]
        assert ctx.state["fixing"] == 2

    async def test_a_paste_is_refused_here(self, _searching):
        ctx, _ = await self._retyping()
        _searching.reset_mock()
        update = _text("a\nb")
        assert await w.sl_fix_text(update, ctx) == w.SL_RETYPE
        assert "Just song 3" in update.message.reply_text.call_args[0][0]
        _searching.assert_not_awaited()
        assert len(ctx.batch.lines) == 3

    async def test_none_of_these_leads_here(self):
        ctx = await _reviewing()
        await w.sl_review_fix(_tap("sl:fix:0"), ctx)
        update = _tap("sl:none")
        assert await w.sl_fix_retype(update, ctx) == w.SL_RETYPE
        assert "results" not in ctx.state and "query" not in ctx.state
        assert "Send song 1 again" in \
            update.callback_query.message.reply_text.call_args[0][0]

    async def test_backing_out_after_none_keeps_the_match(self):
        ctx = await _reviewing()
        await w.sl_review_fix(_tap("sl:fix:0"), ctx)
        await w.sl_fix_retype(_tap("sl:none"), ctx)
        await w.sl_fix_back(_tap("sl:review"), ctx)
        assert ctx.batch.lines[0].song is GRACE


class TestHearMyPicks:
    async def _hear(self, ctx):
        with _no_network():
            assert await w.sl_review_hear(_tap("sl:hear"), ctx) == w.SL_REVIEW
            assert ctx.batch.busy and len(ctx.tasks) == 1
            ctx.bot.send_audio.assert_not_awaited()
            await ctx.run()

    async def test_one_clip_per_song_numbered_as_in_the_review(self):
        ctx = await _reviewing("Amazing Grace Winans\nzz\nWay Maker Sinach")
        await self._hear(ctx)
        assert [c.kwargs["title"] for c in ctx.bot.send_audio.await_args_list] == [
            "1. Amazing Grace", "3. Way Maker"]

    async def test_the_review_is_posted_again_below(self):
        ctx = await _reviewing()
        ctx.bot.send_message.reset_mock()
        await self._hear(ctx)
        assert "Please check" in ctx.said and ctx.batch.busy == ""

    async def test_a_song_without_a_clip_is_reported(self):
        ctx = await _reviewing()
        ctx.batch.lines[1].results = [_song("Way Maker", "Sinach", 3, clip=False)]
        await self._hear(ctx)
        assert "1 of 2 song(s) had no clip" in ctx.said

    async def test_nothing_is_reported_when_all_played(self):
        ctx = await _reviewing()
        await self._hear(ctx)
        assert "had no clip" not in ctx.said

    async def test_the_clips_are_deleted_with_the_list(self):
        ctx = await _reviewing()
        await self._hear(ctx)
        ids = list(ctx.state[w.PREVIEW_IDS_KEY])
        assert len(ids) == 2
        await w.sl_cancel(_tap("sl:cancel"), ctx)
        assert ctx.bot.delete_messages.await_args.kwargs["message_ids"] == ids

    async def test_a_cancel_mid_send_leaves_no_clip_behind(self):
        """_clear_previews has already been and gone by then."""
        ctx = await _reviewing()
        ctx.bot.send_message.reset_mock()

        async def send(**kw):
            await w.sl_cancel(_tap("sl:cancel"), ctx)
            return MagicMock(message_id=777)

        ctx.bot.send_audio = AsyncMock(side_effect=send)
        with _no_network():
            await w.sl_review_hear(_tap("sl:hear"), ctx)
            await ctx.run()
        assert ctx.bot.send_audio.await_count == 1
        ctx.bot.delete_message.assert_awaited_once_with(
            chat_id=CHAT, message_id=777)
        ctx.bot.send_message.assert_not_awaited()

    async def test_a_cancel_before_the_first_clip_sends_none(self):
        ctx = await _reviewing()
        with _no_network():
            await w.sl_review_hear(_tap("sl:hear"), ctx)
            await w.sl_cancel(_tap("sl:cancel"), ctx)
            await ctx.run()
        ctx.bot.send_audio.assert_not_awaited()

    async def test_the_progress_message_goes_away(self):
        ctx = await _reviewing()
        update, shown = _tap("sl:hear"), _progress()
        update.callback_query.message.reply_text = AsyncMock(return_value=shown)
        with _no_network():
            await w.sl_review_hear(update, ctx)
            await ctx.run()
        shown.delete.assert_awaited_once()


class TestStartingAgain:
    async def test_the_old_lists_clips_are_deleted(self):
        """Their ids live in the state that /songlist replaces."""
        ctx = _Context(**{w.PREVIEW_IDS_KEY: [11, 12]})
        update = _text("/songlist")
        await _undecorated(w.cmd_songlist)(update, ctx)
        ctx.bot.delete_messages.assert_awaited_once_with(
            chat_id=CHAT, message_ids=[11, 12])
        assert ctx.state == {"songs": []}


def _undecorated(decorated):
    """The function inside @admin_only, which sets no __wrapped__."""
    return next(cell.cell_contents for cell in decorated.__closure__
                if inspect.iscoroutinefunction(cell.cell_contents))


class TestThePickerEscapesTheQuery:
    async def test_a_stray_bracket_cannot_lose_the_picker(self):
        send = AsyncMock()
        await w._show_candidates(send, "Rock & <3", [GRACE])
        assert "“Rock &amp; &lt;3”" in send.call_args[0][0]


class TestPtbReachesTheReviewHandlers:
    """check_update, not direct calls.

    See TestPtbPicksTheRightHandler for why that matters. Its helpers are
    borrowed rather than inherited, so its tests do not run twice.
    """

    _ptb = one_by_one.TestPtbPicksTheRightHandler
    USER, CHAT = _ptb.USER, _ptb.CHAT
    _tap, _chosen = staticmethod(_ptb._tap), staticmethod(_ptb._chosen)
    _mid_conversation = staticmethod(_ptb._mid_conversation)

    ROUTES = {
        w.SL_REVIEW: {"sl:fix:0": "sl_review_fix", "sl:fix:10": "sl_review_fix",
                      "sl:retry": "sl_review_retry", "sl:hear": "sl_review_hear",
                      "sl:use": "sl_review_use", "sl:cancel": "sl_cancel"},
        w.SL_FIX: {"sl:pick:4": "sl_fix_pick", "sl:preview": "sl_fix_preview",
                   "sl:none": "sl_fix_retype", "sl:drop": "sl_fix_drop",
                   "sl:review": "sl_fix_back"},
        w.SL_RETYPE: {"sl:drop": "sl_fix_drop", "sl:review": "sl_fix_back"},
    }

    def test_every_button_reaches_its_handler(self):
        for state, routes in self.ROUTES.items():
            conv = self._mid_conversation(state)
            for data, name in routes.items():
                assert self._chosen(conv, self._tap(data)) == name, (state, data)

    def test_no_button_is_offered_that_its_state_cannot_handle(self):
        """DERIVED from the keyboards, so a new button cannot be forgotten."""
        missing, found = Line("x", searched=True), Line("y")
        found.record([GRACE])
        for state, markup in (
                (w.SL_REVIEW, w.review_keyboard([missing, found], [])),
                (w.SL_FIX, w.results_keyboard([GRACE], w._fix_rows()))):
            conv = self._mid_conversation(state)
            for data in _buttons(markup):
                assert self._chosen(conv, self._tap(data)), (state, data)

    @pytest.mark.parametrize("state,name", [
        (w.SL_REVIEW, "sl_review_text"), (w.SL_RETYPE, "sl_fix_text"),
        (w.SL_FIX, None),
    ])
    def test_typed_text(self, state, name):
        message = Message(message_id=2, date=None, chat=self.CHAT,
                          from_user=self.USER, text="Way Maker")
        conv = self._mid_conversation(state)
        assert self._chosen(conv, Update(update_id=2, message=message)) == name

    def test_review_buttons_are_dead_outside_their_states(self):
        """They belong to the stale-button handler there."""
        for state in (w.SL_QUERY, w.SL_PICK, w.SL_CONFIRM):
            conv = self._mid_conversation(state)
            for data in ("sl:use", "sl:fix:0", "sl:drop", "sl:review"):
                assert self._chosen(conv, self._tap(data)) is None

    def test_the_stale_handler_covers_the_new_verbs(self):
        pattern = w.stale_button_handler().pattern
        assert all(pattern.match(data) for routes in self.ROUTES.values()
                   for data in routes)

    def test_list_and_end_are_wired(self):
        conv = w.song_list_conversation()
        for state in (w.SL_REVIEW, w.SL_FIX, w.SL_RETYPE):
            assert any("list" in getattr(h, "commands", ())
                       and h.callback is w.sl_review_list
                       for h in conv.states[state])
        assert any("end" in getattr(h, "commands", ())
                   and h.callback is w.sl_review_end
                   for h in conv.states[w.SL_REVIEW])

    def test_the_conversation_still_has_one_entry_point(self):
        conv = w.song_list_conversation()
        assert len(conv.entry_points) == 1
        assert not isinstance(conv.entry_points[0], CallbackQueryHandler)


class TestEndToEnd:
    async def test_a_paste_reaches_the_recipient_in_order(self):
        ctx = await _reviewing("1. Amazing Grace Winans\n2. zz\n"
                               "3. Way Maker - Sinach")
        await w.sl_review_fix(_tap("sl:fix:0"), ctx)
        await w.sl_fix_pick(_tap("sl:pick:1"), ctx)
        await w.sl_review_use(_tap("sl:use"), ctx)
        ctx.bot.send_message.reset_mock()
        result = await w.sl_send(_tap("sl:to:999000001"), ctx)
        assert result == ConversationHandler.END
        sent, copy = ctx.bot.send_message.await_args_list
        assert sent.args[0] == 999000001 and copy.args[0] == 7
        for call in (sent, copy):
            assert call.args[1].endswith(w.render_list([GRACE2, WAY]))
        assert w.STATE_KEY not in ctx.user_data


def test_every_icon_is_distinct():
    icons = [song_matching.MISSING, song_matching.DOUBTFUL,
             song_matching.SEVERAL, song_matching.DUPLICATE,
             song_matching.DROPPED, song_matching.FINE, song_matching.PENDING]
    assert len(set(icons)) == len(icons)

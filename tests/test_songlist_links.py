"""/songlist — building from Apple Music links and from earlier lists.

Offline: the lookup and the search are substituted. What they return was
checked against Apple, from the laptop and the server, on 2026-11-24.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import apple_music as a
import handlers.worship as w
import song_matching as m
from apple_music import Song
from song_matching import Line
from telegram.error import TelegramError
from tests.test_songlist_paste import (
    _buttons,
    _Context,
    _progress,
    _tap,
    _text,
)


def _song(title, artist, n, album="The Hymns"):
    return Song(title, artist, album,
                f"https://music.apple.com/us/album/x/1?i={n}",
                duration_ms=240000, release_year=2021)


SONGS = [
    _song("Amazing Grace", "CeCe Winans", 11),
    _song("Stand By Me", "Ben E. King", 22),
    _song("Grace & Truth (feat. Tom) [Live]", "A & B", 33, album=""),
    _song("Way Maker", "Sinach", 44, album="Rock & Roll by Night"),
]
STORE = {"US": {str(n): song for n, song in zip((11, 22, 33, 44), SONGS)},
         "ZA": {"55": _song("Siyabonga", "Choir", 55)}}


async def _lookup(ids, country="US"):
    return {i: STORE[country][i] for i in ids if i in STORE.get(country, {})}


async def _search(query, limit=5):
    return [s for s in SONGS if query.casefold().startswith(s.title.casefold())]


@pytest.fixture(autouse=True)
def _apple():
    a._recent_searches.clear()
    with patch.object(a, "lookup_songs", AsyncMock(side_effect=_lookup)) as look, \
         patch.object(a, "search_songs", AsyncMock(side_effect=_search)) as search:
        yield MagicMock(look=look, search=search)
    a._recent_searches.clear()


async def _review(text, ctx=None):
    ctx = ctx or _Context()
    assert await w.sl_query(_text(text), ctx) == w.SL_REVIEW
    await ctx.run()
    return ctx


def _picked(ctx):
    return [line.song for line in ctx.batch.lines]


def _as_read(markup):
    """What Telegram puts on the clipboard: no tags, entities decoded."""
    return html.unescape(re.sub(r"</?b>", "", markup))


class TestFindingLinks:
    @pytest.mark.parametrize("url,track,country", [
        ("https://music.apple.com/us/album/amazing-grace/9?i=11", "11", "US"),
        ("https://music.apple.com/za/album/x/9?i=11&uo=4&ls=1", "11", "ZA"),
        ("https://music.apple.com/us/album/9?i=11", "11", "US"),
        ("https://music.apple.com/us/song/amazing-grace/11", "11", "US"),
        ("https://music.apple.com/us/song/11", "11", "US"),
        ("https://music.apple.com/song/11", "11", "US"),
        ("http://music.apple.com/gb/song/x/11?l=en", "11", "GB"),
        ("https://geo.music.apple.com/us/album/x/9?i=11&at=z", "11", "US"),
        ("https://itunes.apple.com/us/album/x/id9?i=11", "11", "US"),
        ("music.apple.com/us/song/x/11", "11", "US"),
        ("HTTPS://Music.Apple.com/US/song/x/11", "11", "US"),
    ])
    def test_a_song(self, url, track, country):
        link, = a.find_links(url)
        assert (link.kind, link.track_id, link.country) == ("song", track, country)

    @pytest.mark.parametrize("url,kind", [
        ("https://music.apple.com/us/album/x/9", "album"),
        ("https://music.apple.com/us/playlist/x/pl.u-abc", "playlist"),
        ("https://music.apple.com/us/artist/x/9", "artist"),
        ("https://music.apple.com/us/station/x/ra.9", "other"),
        ("https://music.apple.com/us/song/x", "other"),
        ("https://music.apple.com/us/album/x/9?i=abc", "album"),
        ("https://music.apple.com/us/album/x/9?i=" + "1" * 16, "album"),
        ("https://music.apple.com/us/album/x/9?i=1,2", "album"),
        ("https://music.apple.com/us/album/x/9?i=١٢", "album"),
        ("https://music.apple.com/us/", "other"),
    ])
    def test_not_a_song(self, url, kind):
        link, = a.find_links(url)
        assert link.kind == kind and link.track_id == ""

    @pytest.mark.parametrize("text", [
        "", None, "Amazing Grace", "https://example.test/us/song/x/11",
        "https://music.apple.com.example.test/us/song/x/11",
        "https://notmusic.apple.com/us/song/x/11",
        "https://x.music.apple.com/us/song/x/11",
        "me@music.apple.com/us/song/11", "music.apple.com",
    ])
    def test_not_a_link(self, text):
        assert a.find_links(text) == []

    @pytest.mark.parametrize("after", [".", ")", ",", "!", "…", "]."])
    def test_punctuation_after_it_is_not_part_of_it(self, after):
        link, = a.find_links("see https://music.apple.com/us/song/x/11" + after)
        assert link.track_id == "11" and link.url.endswith("/11")

    def test_several_in_order(self):
        found = a.find_links("music.apple.com/us/song/2 x music.apple.com/us/song/1")
        assert [link.track_id for link in found] == ["2", "1"]

    def test_they_can_be_taken_out(self):
        assert a.without_links("A music.apple.com/us/song/2 B").split() == ["A", "B"]


class TestTheLookup:
    """The real function, with only HTTP substituted."""

    _real = staticmethod(a.lookup_songs)

    @staticmethod
    def _client(payload=None, error=None):
        response = MagicMock()
        response.json.return_value = payload
        client = MagicMock()
        client.get = AsyncMock(return_value=response, side_effect=error)
        manager = MagicMock()
        manager.return_value.__aenter__ = AsyncMock(return_value=client)
        manager.return_value.__aexit__ = AsyncMock(return_value=False)
        return manager, client

    RESULT = {"kind": "song", "trackId": 11, "trackName": "Amazing Grace",
              "artistName": "CeCe Winans", "collectionName": "The Hymns",
              "trackViewUrl": "https://music.apple.com/us/album/x/1?i=11&uo=4",
              "trackTimeMillis": 240000, "releaseDate": "2021-01-01T00:00:00Z"}

    async def _call(self, payload=None, error=None, ids=("11", "22"), country="ZA"):
        manager, client = self._client(payload, error)
        with patch.object(a.httpx, "AsyncClient", manager):
            return await self._real(list(ids), country), client

    async def test_by_id_with_the_same_metadata_as_a_search(self):
        found, _ = await self._call({"results": [self.RESULT]})
        assert found == {"11": SONGS[0]}

    async def test_all_ids_go_in_one_request_to_the_links_country(self):
        _, client = await self._call({"results": []})
        client.get.assert_awaited_once()
        assert client.get.await_args.args[0] == a.LOOKUP_URL
        assert client.get.await_args.kwargs["params"] == {
            "id": "11,22", "country": "ZA"}

    async def test_an_album_in_the_answer_is_ignored(self):
        found, _ = await self._call(
            {"results": [{"wrapperType": "collection"}, "junk", self.RESULT]})
        assert list(found) == ["11"]

    async def test_nothing_found_is_not_the_same_as_no_answer(self):
        assert (await self._call({"results": []}))[0] == {}
        assert (await self._call(error=OSError("down")))[0] is None
        assert (await self._call(["odd"]))[0] is None
        assert (await self._call({"results": None}))[0] is None

    async def test_no_ids_asks_nothing(self):
        found, client = await self._call(ids=())
        assert found == {} and client.get.await_count == 0
        assert not a._recent_searches

    async def test_it_counts_toward_apples_limit(self):
        await self._call({"results": []})
        assert len(a._recent_searches) == 1


class TestReadingAMessage:
    def test_a_bare_link(self):
        line, = m.parse_entries("https://music.apple.com/us/song/x/11")
        assert line.link.track_id == "11" and not line.labelled

    def test_links_one_per_line(self):
        lines = m.parse_entries("music.apple.com/us/song/1\nmusic.apple.com/us/song/2")
        assert [line.link.track_id for line in lines] == ["1", "2"]

    def test_links_on_one_line(self):
        lines = m.parse_entries("x music.apple.com/us/song/1 music.apple.com/us/song/2")
        assert [line.link.track_id for line in lines] == ["1", "2"]
        assert not any(line.labelled for line in lines)

    @pytest.mark.parametrize("text", [
        "Way Maker - Sinach\nmusic.apple.com/us/song/1",
        "Way Maker - Sinach music.apple.com/us/song/1",
        "music.apple.com/us/song/1 Way Maker Sinach",
        "3. Way Maker by Sinach\nmusic.apple.com/us/song/1",
        "3. Way Maker — Sinach\n    Album · 4:00 · 2021\nmusic.apple.com/us/song/1",
        "🔗 3. Way Maker — Sinach\n    ↳ some note\nmusic.apple.com/us/song/1",
    ])
    def test_the_text_with_a_link_is_its_label(self, text):
        line, = m.parse_entries(text)
        assert line.query == "Way Maker Sinach"
        assert line.labelled and line.link.track_id == "1"

    def test_only_the_line_directly_above_is_a_label(self):
        one, two = m.parse_entries("Amazing Grace\nWay Maker\nmusic.apple.com/us/song/1")
        assert one.query == "Amazing Grace" and one.link is None
        assert two.query == "Way Maker" and two.link

    def test_a_blank_line_ends_a_label(self):
        one, two = m.parse_entries("Amazing Grace\n\nmusic.apple.com/us/song/1")
        assert one.link is None and not two.labelled

    def test_a_label_is_used_once(self):
        lines = m.parse_entries(
            "Way Maker\nmusic.apple.com/us/song/1\nmusic.apple.com/us/song/2")
        assert [line.labelled for line in lines] == [True, False]

    def test_a_typed_song_under_a_link_is_a_song(self):
        one, two = m.parse_entries("music.apple.com/us/song/1\nWay Maker")
        assert one.link and two.query == "Way Maker" and two.link is None

    def test_a_numbered_typed_list_is_not_swallowed_by_a_link(self):
        lines = m.parse_entries(
            "1. Amazing Grace\n2. Way Maker\n3. Total Praise\nmusic.apple.com/us/song/1")
        assert [line.query for line in lines] == [
            "Amazing Grace", "Way Maker", "Total Praise"]

    def test_an_indented_numbered_list_is_not_swallowed_either(self):
        lines = m.parse_entries(
            "1. Amazing Grace\n  2. Way Maker\nmusic.apple.com/us/song/1")
        assert [line.query for line in lines] == ["Amazing Grace", "Way Maker"]

    @pytest.mark.parametrize("text", [
        "1. Way Maker\n  music.apple.com/us/song/1\nmusic.apple.com/us/song/2",
        "1. Way Maker music.apple.com/us/song/1\nmusic.apple.com/us/song/2",
        "1. Way Maker\nmusic.apple.com/us/song/1 music.apple.com/us/song/2",
    ])
    def test_two_links_under_one_title_are_both_kept(self, text):
        lines = m.parse_entries(text)
        assert [line.link.track_id for line in lines if line.link] == ["1", "2"]

    def test_our_by_is_removed_among_other_numbered_lines_too(self):
        _, two = m.parse_entries(
            "1. Amazing Grace\n2. Way Maker by Sinach\nmusic.apple.com/us/song/1")
        assert two.query == "Way Maker Sinach" and two.link

    def test_only_our_by_is_removed_and_only_the_last(self):
        line, = m.parse_entries("1. Stand by Me by Ben E. King\nmusic.apple.com/us/song/1")
        assert line.query == "Stand by Me Ben E. King"
        line, = m.parse_entries("1. Stand by Me — Ben E. King\nmusic.apple.com/us/song/1")
        assert line.query == "Stand by Me Ben E. King"

    def test_without_links_it_is_what_it_was(self):
        text = "1. Stand By Me by Ben E King\n• Way Maker - Sinach\n\nGrace"
        assert [line.query for line in m.parse_entries(text)] == m.parse_lines(text)

    @pytest.mark.parametrize("url,word", [
        ("music.apple.com/us/album/x/9", "album"),
        ("music.apple.com/us/playlist/x/pl.1", "playlist"),
        ("music.apple.com/us/artist/x/9", "artist"),
        ("music.apple.com/us/station/9", "cannot tell"),
    ])
    def test_a_link_to_something_else_is_explained_and_final(self, url, word):
        line, = m.parse_entries(url)
        assert word in line.note and line.missing and not line.retryable

    def test_a_decorated_song_is_not_taken_for_our_header(self):
        assert [line.query for line in m.parse_entries("🎵 Amazing Grace\n📋 Way Maker")
                ] == ["🎵 Amazing Grace", "📋 Way Maker"]

    def test_a_header_with_no_songs_under_it_is_nothing(self):
        assert m.parse_entries("🎵 Worship song list — 3 song(s)") == []
        assert m.parse_entries("") == [] and m.parse_entries(None) == []

    def test_songs_not_found_in_an_old_review_are_searched_again(self):
        line, = m.parse_entries("⚠️ 3. Not found\n    you typed: waymakr")
        assert line.query == "waymakr" and line.link is None

    def test_songs_left_out_of_an_old_review_stay_out(self):
        assert m.parse_entries("🚫 3. Left out\n    you typed: waymaker") == []


class TestEveryListComesBack:
    """Whatever this bot shows with links can be pasted back as it stands."""

    @staticmethod
    def _lines():
        typed = [Line("Amazing Grace CeCe Winans"), Line("Stand By Me Somebody"),
                 Line("grace & truth tom live"), Line("way maker sinach")]
        for line, song in zip(typed, SONGS):
            line.record([song])
        return typed

    def _forms(self):
        lines = self._lines()
        n = len(SONGS)
        return {
            "sent": f"🎵 Worship song list — {n} song(s)\n\n{w.render_list(SONGS)}",
            "confirm": f"📋 {n} song(s) — this is exactly what will be sent:\n\n"
                       + w.render_list(SONGS),
            "list": f"📋 {n} song(s) so far — still building:\n\n"
                    + w.render_with_links(SONGS),
            "review": w.review_heading(lines, []) + "\n\n"
                      + w.render_review(lines, []),
            "body only": w.render_list(SONGS),
        }

    @pytest.mark.parametrize("form", ["sent", "confirm", "list", "review", "body only"])
    @pytest.mark.parametrize("damage", [
        lambda t: t, lambda t: t.replace("\n\n", "\n"),
        lambda t: t.replace("\n", "\r\n"),
    ], ids=["intact", "no blank lines", "crlf"])
    async def test_the_same_songs_in_the_same_order(self, form, damage, _apple):
        ctx = await _review(damage(_as_read(self._forms()[form])))
        assert _picked(ctx) == SONGS
        assert {icon for icon, _ in m.flags(ctx.batch.lines)} == {m.LINKED}
        _apple.search.assert_not_awaited()

    async def test_the_headers_are_the_ones_really_sent(self):
        """The forms above quote the headers; this reads them from the code."""
        ctx = _Context(songs=SONGS)
        update = _text("/list")
        await w.sl_list(update, ctx)
        shown = [update.message.reply_text.call_args[0][0]]
        update = _text("/end")
        await w.sl_end(update, ctx)
        shown.append(update.message.reply_text.call_args[0][0])
        await w.sl_send(_tap("sl:to:999000001"), ctx)
        shown += [c.args[1] for c in ctx.bot.send_message.await_args_list]
        assert len(shown) == 4
        for text in shown:
            assert _picked(await _review(_as_read(text))) == SONGS

    async def test_part_of_a_list(self):
        part = "\n\n".join(_as_read(w.render_list(SONGS)).split("\n\n")[1:3])
        assert _picked(await _review(part)) == SONGS[1:3]

    @pytest.mark.parametrize("gap", ["\n\n", "\n"])
    async def test_a_review_with_a_hole_in_it(self, gap):
        lines = self._lines()
        lines[1] = Line("way maker please", searched=True)
        lines[2].dropped = True
        text = _as_read(w.render_review(lines, [])).replace("\n\n", gap)
        assert _picked(await _review(text)) == [SONGS[0], SONGS[3], SONGS[3]]


class TestResolving:
    async def test_links_take_one_lookup_and_no_searches(self, _apple):
        ctx = await _review("music.apple.com/us/song/11\nmusic.apple.com/us/song/44")
        _apple.look.assert_awaited_once_with(["11", "44"], "US")
        _apple.search.assert_not_awaited()
        assert _picked(ctx) == [SONGS[0], SONGS[3]]

    async def test_one_link_alone_is_enough(self):
        ctx = await _review("https://music.apple.com/us/song/x/11")
        assert _picked(ctx) == [SONGS[0]]

    async def test_each_country_is_asked_about_its_own(self, _apple):
        ctx = await _review("music.apple.com/za/song/55\nmusic.apple.com/us/song/11\n"
                            "music.apple.com/za/song/55")
        assert [c.args for c in _apple.look.await_args_list] == [
            (["55"], "ZA"), (["11"], "US")]
        assert [s.title for s in _picked(ctx)] == [
            "Siyabonga", "Amazing Grace", "Siyabonga"]

    async def test_a_very_long_list_of_links_is_split(self, _apple):
        text = "\n".join(f"music.apple.com/us/song/{n}" for n in range(1, 151))
        with patch.object(a, "LOOKUP_BATCH", 100):
            await _review(text)
        assert [len(c.args[0]) for c in _apple.look.await_args_list] == [100, 50]

    async def test_a_mix_keeps_its_order(self, _apple):
        ctx = await _review("Way Maker\n\nmusic.apple.com/us/song/11\n\nStand By Me")
        assert _picked(ctx) == [SONGS[3], SONGS[0], SONGS[1]]

    async def test_lookups_wait_for_the_limit_too(self, _apple):
        delays = iter([0.5, 0.0])
        naps = []

        async def nap(seconds):
            naps.append(seconds)
            _apple.look.assert_not_awaited()

        with patch.object(a, "search_delay", lambda: next(delays)), \
             patch.object(w.asyncio, "sleep", nap):
            await _review("music.apple.com/us/song/11")
        assert naps == [0.5]
        _apple.look.assert_awaited_once()

    async def test_a_cancelled_list_is_not_looked_up(self, _apple):
        ctx = _Context()
        await w.sl_query(_text("music.apple.com/us/song/11"), ctx)
        ctx.user_data.clear()
        await ctx.run()
        _apple.look.assert_not_awaited()

    async def test_a_dead_link_with_a_title_is_searched_for(self, _apple):
        ctx = await _review("1. Way Maker by Sinach\nmusic.apple.com/us/song/99")
        assert _picked(ctx) == [SONGS[3]]
        _apple.search.assert_awaited_once_with("Way Maker Sinach")
        icon, note = m.flags(ctx.batch.lines)[0]
        assert icon == "❓" and "not in Apple's US store" in note
        assert "search for its title" in note

    async def test_a_dead_link_without_one_is_explained(self, _apple):
        ctx = await _review("music.apple.com/za/song/11")
        assert m.flags(ctx.batch.lines) == [
            ("⚠️", "its link is not in Apple's ZA store")]
        _apple.search.assert_not_awaited()
        assert "sl:retry" not in _buttons(ctx.last.kwargs["reply_markup"])

    async def test_no_answer_keeps_the_link_for_another_try(self, _apple):
        _apple.look.side_effect = AsyncMock(return_value=None)
        ctx = await _review("Way Maker\nmusic.apple.com/us/song/44")
        assert m.flags(ctx.batch.lines) == [("⚠️", m.UNANSWERED)]
        assert "may be down" in ctx.said
        _apple.search.assert_not_awaited()

        _apple.look.side_effect = _lookup
        await w.sl_review_retry(_tap("sl:retry"), ctx)
        await ctx.run()
        assert _picked(ctx) == [SONGS[3]]
        assert m.flags(ctx.batch.lines) == [("🔗", "")]
        assert ctx.batch.lines[0].note == ""

    async def test_a_dead_link_is_not_looked_up_again(self, _apple):
        ctx = await _review("Zzqq\nmusic.apple.com/us/song/99")
        assert ctx.batch.lines[0].missing
        _apple.look.reset_mock()
        await w.sl_review_retry(_tap("sl:retry"), ctx)
        await ctx.run()
        _apple.look.assert_not_awaited()
        assert _apple.search.await_count == 2

    async def test_links_to_albums_alone_are_not_an_outage(self):
        ctx = await _review("music.apple.com/us/album/x/9")
        assert "may be down" not in ctx.said
        assert "album" in ctx.said

    async def test_a_final_line_is_not_retried_with_the_others(self, _apple):
        ctx = await _review("music.apple.com/us/album/x/9\nzzqq")
        _apple.search.reset_mock()
        await w.sl_review_retry(_tap("sl:retry"), ctx)
        await ctx.run()
        _apple.search.assert_awaited_once_with("zzqq")


class TestFlaggingLinks:
    @staticmethod
    def _linked(song, label=""):
        line = Line(label or song.url, labelled=bool(label))
        line.record([song])
        line.linked = True
        return line

    def test_a_link_is_not_a_guess(self):
        assert m.flags([self._linked(SONGS[2])]) == [("🔗", "")]
        assert m.flags([self._linked(SONGS[0], "Amazing Grace Tomlin")]) == [("🔗", "")]

    def test_a_label_for_another_song_is_doubtful(self):
        icon, note = m.flags([self._linked(SONGS[0], "Way Maker")])[0]
        assert icon == "❓" and "“Way Maker”" in note

    def test_brackets_in_the_title_need_not_be_in_the_label(self):
        assert m.label_agrees("grace and & truth", SONGS[2])

    def test_a_title_that_is_all_brackets_is_still_compared(self):
        odd = _song("(Untitled)", "X", 9)
        assert m.label_agrees("untitled", odd)
        assert not m.label_agrees("something", odd)

    def test_a_duplicate_is_still_a_duplicate(self):
        lines = [self._linked(SONGS[0]), self._linked(SONGS[0])]
        assert [icon for icon, _ in m.flags(lines)] == ["🔗", "🔁"]

    def test_a_pick_makes_it_an_ordinary_line(self):
        line = self._linked(SONGS[0], "Way Maker")
        line.link, = a.find_links("music.apple.com/us/song/11")
        line.note, line.final = "x", True
        line.settle("way maker", [SONGS[3]], 0)
        assert m.flags([line]) == [("✓", "")]
        assert not (line.linked or line.labelled or line.final or line.note
                    or line.link)

    def test_a_link_counts_as_settled_on_the_keyboard(self):
        data = _buttons(w.review_keyboard([self._linked(SONGS[0])], []))
        assert data["sl:fix:0"] == "1"
        many = [self._linked(_song(f"S{n}", "X", n))
                for n in range(w.MAX_NUMBER_BUTTONS + 1)]
        assert not any(d.startswith("sl:fix") for d in
                       _buttons(w.review_keyboard(many, [])))

    def test_the_heading_explains_the_icon_only_when_it_is_used(self):
        line = Line("x")
        line.record([SONGS[0]])
        assert "🔗" not in w.review_heading([line], [])
        assert "🔗" in w.review_heading([self._linked(SONGS[0])], [])

    def test_the_label_is_not_shown_as_typed(self):
        assert "you typed" not in w.render_review([self._linked(SONGS[0])], [])


class TestElsewhere:
    async def test_links_sent_during_a_review_are_added(self, _apple):
        ctx = await _review("music.apple.com/us/song/44\nStand By Me")
        _apple.look.reset_mock()
        await w.sl_review_text(_text("music.apple.com/us/song/11"), ctx)
        await ctx.run()
        assert _picked(ctx) == [SONGS[3], SONGS[1], SONGS[0]]
        _apple.look.assert_awaited_once_with(["11"], "US")

    async def test_a_list_added_mid_list_keeps_what_was_chosen(self):
        ctx = await _review(_as_read(w.render_list(SONGS[1:])),
                            ctx=_Context(songs=SONGS[:1]))
        await w.sl_review_use(_tap("sl:use"), ctx)
        assert ctx.state["songs"] == SONGS

    async def _retyping(self):
        ctx = await _review("zzqq\nyyqq")
        await w.sl_review_fix(_tap("sl:fix:0"), ctx)
        return ctx

    @pytest.mark.parametrize("text", [
        "https://music.apple.com/us/song/x/11",
        "Listen to this\nhttps://music.apple.com/us/song/x/11",
    ])
    async def test_a_line_can_be_replaced_with_a_link(self, text, _apple):
        ctx = await self._retyping()
        assert await w.sl_fix_text(_text(text), ctx) == w.SL_FIX
        assert ctx.state["results"] == [SONGS[0]]
        await w.sl_fix_pick(_tap("sl:pick:0"), ctx)
        assert _picked(ctx)[0] is SONGS[0]

    @pytest.mark.parametrize("url", ["music.apple.com/us/album/x/9",
                                     "music.apple.com/us/song/99"])
    async def test_a_useless_link_says_so_and_asks_again(self, url, _apple):
        ctx = await self._retyping()
        update = _text(url)
        assert await w.sl_fix_text(update, ctx) == w.SL_RETYPE
        assert "own link" in update.message.reply_text.call_args[0][0]
        assert _apple.look.await_count == ("song" in url)

    async def test_no_answer_while_retyping_asks_again(self, _apple):
        ctx = await self._retyping()
        _apple.look.side_effect = AsyncMock(return_value=None)
        assert await w.sl_fix_text(_text("music.apple.com/us/song/11"),
                                   ctx) == w.SL_RETYPE

    async def test_two_links_for_one_line_are_refused(self, _apple):
        ctx = await self._retyping()
        update = _text("music.apple.com/us/song/11 music.apple.com/us/song/22")
        assert await w.sl_fix_text(update, ctx) == w.SL_RETYPE
        assert "Just song 1" in update.message.reply_text.call_args[0][0]
        _apple.look.assert_not_awaited()

    async def test_the_progress_message_survives_having_nothing_to_search(self):
        ctx, update = _Context(), _text("music.apple.com/us/album/x/9")
        update.message.reply_text = AsyncMock(return_value=_progress())
        await w.sl_query(update, ctx)
        assert "Checking the list" in update.message.reply_text.call_args[0][0]
        await ctx.run()


    async def test_the_first_message_counts_links_and_songs_alike(self):
        ctx, update = _Context(), _text("music.apple.com/us/song/11\nWay Maker")
        await w.sl_query(update, ctx)
        assert "Looking up 2 song(s)" in update.message.reply_text.call_args[0][0]
        await ctx.run()


class TestTheSendersCopy:
    """Asked for on 2026-11-24, so a past list can start a future one."""

    async def _send(self, to=999000001, songs=SONGS, fail=()):
        ctx, update = _Context(songs=songs), _tap(f"sl:to:{to}")

        async def send(chat_id, text, **kw):
            if chat_id in fail:
                raise TelegramError("blocked")
            return MagicMock(message_id=1)

        ctx.bot.send_message = AsyncMock(side_effect=send)
        await w.sl_send(update, ctx)
        said = update.callback_query.edit_message_text.call_args[0][0]
        return [c for c in ctx.bot.send_message.await_args_list
                if c.args[0] == 7], said, ctx

    async def test_it_goes_to_the_sender_privately(self):
        """Not to the chat /songlist was run in, which may be a group."""
        copies, said, ctx = await self._send()
        assert len(copies) == 1
        assert {c.args[0] for c in ctx.bot.send_message.await_args_list} == {
            7, 999000001}
        assert "copy could not" not in said

    async def test_it_holds_the_same_list_and_says_how_to_use_it(self):
        (copy,), _, _ = await self._send()
        assert copy.args[1].endswith(w.render_list(SONGS))
        assert "Your copy" in copy.args[1] and "/songlist" in copy.args[1]
        assert "as sent" in copy.args[1]
        assert copy.kwargs["parse_mode"] == w.ParseMode.HTML
        assert copy.kwargs["link_preview_options"] is w.NO_PREVIEW

    async def test_the_recipients_message_is_unchanged(self):
        _, _, ctx = await self._send()
        assert ctx.bot.send_message.await_args_list[0].args[1] == (
            f"🎵 Worship song list — {len(SONGS)} song(s)\n\n{w.render_list(SONGS)}")

    async def test_no_second_copy_of_a_list_sent_to_oneself(self):
        copies, said, _ = await self._send(to=7)
        assert len(copies) == 1 and "Your copy" not in copies[0].args[1]
        assert "copy could not" not in said

    async def test_a_failed_copy_does_not_look_like_a_failed_send(self):
        _, said, _ = await self._send(fail={7})
        assert said.startswith("✅ Sent") and "copy could not" in said

    async def test_an_undeliverable_list_is_not_lost(self):
        copies, said, ctx = await self._send(fail={999000001})
        assert "could not be delivered" in said
        assert len(copies) == 1 and "not delivered" in copies[0].args[1]
        assert w.STATE_KEY not in ctx.user_data

    async def test_nothing_is_raised_when_neither_arrives(self):
        _, said, ctx = await self._send(fail={7, 999000001})
        assert "could not be delivered" in said and w.STATE_KEY not in ctx.user_data

    async def test_a_long_copy_is_split_and_fits(self):
        many = [_song(f"Song {n} " + "x" * 60, "Y" * 40, n) for n in range(60)]
        copies, _, _ = await self._send(songs=many)
        assert len(copies) > 1
        assert all(len(c.args[1]) <= 4096 for c in copies)
        assert sum("Your copy" in c.args[1] for c in copies) == 1


class TestThePromptSaysSo:
    def test_links_and_earlier_lists_are_offered(self):
        import ast
        import inspect
        body = next(n for n in ast.walk(ast.parse(inspect.getsource(w)))
                    if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "cmd_songlist")
        text = "".join(n.value for n in ast.walk(body)
                       if isinstance(n, ast.Constant) and isinstance(n.value, str))
        assert "song links work" in text and "earlier list" in text

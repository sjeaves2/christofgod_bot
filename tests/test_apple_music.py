"""Song search: the never-raises contract, result filtering, and the format.

NOTHING HERE TOUCHES THE NETWORK. Every test substitutes the HTTP client, so
the suite stays offline and deterministic — a test that really called Apple
would be slow, would fail on a train, and would start failing the day a
catalogue entry changed.

The one thing a fake cannot establish is that the real endpoint answers from
the production IP, which matters because deep-translator's Google endpoint
refused AWS addresses and nobody noticed for months. That was checked by hand
from 44.214.123.92 on 2026-11-02 and is recorded in the module docstring; it
is not something a unit test can own.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import apple_music as am


def _result(track="Amazing Grace", artist="CeCe Winans", album="The Hymns",
            url="https://music.apple.com/us/album/amazing-grace/1?i=2&uo=4",
            kind="song", **extra):
    """One iTunes Search API result, shaped as the real API shapes it."""
    payload = {"kind": kind, "trackName": track, "artistName": artist,
               "collectionName": album, "trackViewUrl": url}
    payload.update(extra)
    return payload


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self, payload=None, status=200, raises=None):
        self._payload = payload if payload is not None else {"results": []}
        self._status = status
        self._raises = raises
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        self.calls.append({"url": url, "params": params})
        if self._raises is not None:
            raise self._raises
        return _FakeResponse(self._payload, self._status)


def _patched(client):
    """Patch the constructor so `async with httpx.AsyncClient(...)` yields it."""
    return patch.object(am.httpx, "AsyncClient", lambda *a, **k: client)


class TestTheSearchTerm:
    """The query reaches the API untouched.

    There used to be a search_term() that joined a title and an artist. The
    caller now asks for both in one message, because `term` is a general
    search across title, artist and album — so there is nothing left to join,
    and anything this module did to the admin's text would be a bug.
    """

    async def test_the_query_is_sent_verbatim(self):
        client = _FakeClient({"results": []})
        with _patched(client):
            await am.search_songs("Amazing Grace CeCe Winans")
        assert client.calls[0]["params"]["term"] == "Amazing Grace CeCe Winans"

    async def test_a_query_containing_by_is_not_split(self):
        """"Saved By Grace" is one song title. Splitting it would send
        "Saved Grace", which returns confident, wrong results."""
        client = _FakeClient({"results": []})
        with _patched(client):
            await am.search_songs("Saved By Grace Israel Houghton")
        assert client.calls[0]["params"]["term"] == \
            "Saved By Grace Israel Houghton"

    async def test_surrounding_whitespace_is_dropped(self):
        client = _FakeClient({"results": []})
        with _patched(client):
            await am.search_songs("  Amazing Grace  ")
        assert client.calls[0]["params"]["term"] == "Amazing Grace"

    async def test_inner_spacing_is_left_alone(self):
        """Only the ends are stripped. Collapsing runs of spaces would be
        another transformation of the admin's text, with nothing to gain."""
        client = _FakeClient({"results": []})
        with _patched(client):
            await am.search_songs(" Amazing  Grace ")
        assert client.calls[0]["params"]["term"] == "Amazing  Grace"

    def test_it_takes_no_artist_argument(self):
        """The API has no artist parameter for an entity=song query, so this
        one must not grow a second field that silently goes nowhere."""
        import inspect
        params = inspect.signature(am.search_songs).parameters
        assert list(params) == ["query", "limit"]


class TestTheRequest:
    async def test_it_asks_for_songs_in_the_configured_storefront(self):
        client = _FakeClient({"results": [_result()]})
        with _patched(client):
            await am.search_songs("Amazing Grace")
        params = client.calls[0]["params"]
        assert client.calls[0]["url"] == am.SEARCH_URL
        assert params["entity"] == "song"
        assert params["country"] == am.STOREFRONT
        assert params["term"] == "Amazing Grace"

    async def test_the_limit_is_passed_through(self):
        client = _FakeClient({"results": []})
        with _patched(client):
            await am.search_songs("x", limit=3)
        assert client.calls[0]["params"]["limit"] == 3

    async def test_an_empty_title_never_calls_the_api(self):
        """Typing only whitespace must not spend a request, nor count against
        Apple's rate limit."""
        client = _FakeClient({"results": [_result()]})
        with _patched(client):
            assert await am.search_songs("   ") == []
        assert client.calls == []


class TestResultParsing:
    async def test_a_result_becomes_a_song(self):
        client = _FakeClient({"results": [_result()]})
        with _patched(client):
            songs = await am.search_songs("Amazing Grace")
        assert len(songs) == 1
        song = songs[0]
        assert song.title == "Amazing Grace"
        assert song.artist == "CeCe Winans"
        assert song.album == "The Hymns"
        assert song.url.startswith("https://music.apple.com/")

    async def test_the_same_hymn_by_several_artists_is_preserved(self):
        """The case this screen exists for. Collapsing these would make the
        picker useless."""
        client = _FakeClient({"results": [
            _result(artist="CeCe Winans", url="https://music.apple.com/1"),
            _result(artist="Alan Jackson", url="https://music.apple.com/2"),
            _result(artist="Carrie Underwood", url="https://music.apple.com/3"),
        ]})
        with _patched(client):
            songs = await am.search_songs("Amazing Grace")
        assert [s.artist for s in songs] == \
            ["CeCe Winans", "Alan Jackson", "Carrie Underwood"]

    async def test_two_recordings_by_one_artist_are_both_kept(self):
        """A real result: CeCe Winans appears twice for "Great Is Thy
        Faithfulness", from The Hymns and from Alone in His Presence. They are
        different recordings, so both stay — and the album is what tells them
        apart in the picker."""
        client = _FakeClient({"results": [
            _result(track="Great Is Thy Faithfulness", album="The Hymns",
                    url="https://music.apple.com/1"),
            _result(track="Great Is Thy Faithfulness",
                    album="Alone in His Presence",
                    url="https://music.apple.com/2"),
        ]})
        with _patched(client):
            songs = await am.search_songs("Great Is Thy Faithfulness")
        assert len(songs) == 2
        assert {s.album for s in songs} == {"The Hymns", "Alone in His Presence"}

    async def test_non_song_results_are_dropped(self):
        """entity=song still returns the occasional music-video or podcast
        row, and those links do not belong in a worship list."""
        client = _FakeClient({"results": [
            _result(kind="music-video"), _result(kind="podcast"),
            _result(kind="song"),
        ]})
        with _patched(client):
            songs = await am.search_songs("x")
        assert len(songs) == 1

    async def test_a_result_with_no_link_is_dropped(self):
        """A row without trackViewUrl is useless: the link IS the deliverable."""
        client = _FakeClient({"results": [_result(url=""), _result()]})
        with _patched(client):
            assert len(await am.search_songs("x")) == 1

    async def test_a_result_with_no_title_or_artist_is_dropped(self):
        client = _FakeClient({"results": [
            _result(track=""), _result(artist=""), _result(),
        ]})
        with _patched(client):
            assert len(await am.search_songs("x")) == 1

    async def test_a_missing_album_is_tolerated(self):
        """Singles sometimes have no collectionName. That is not a reason to
        discard an otherwise good link."""
        client = _FakeClient({"results": [_result(album=None)]})
        with _patched(client):
            songs = await am.search_songs("x")
        assert len(songs) == 1 and songs[0].album == ""

    async def test_more_results_than_asked_for_are_truncated(self):
        client = _FakeClient({"results": [
            _result(url=f"https://music.apple.com/{i}") for i in range(10)]})
        with _patched(client):
            assert len(await am.search_songs("x", limit=4)) == 4


class TestItNeverRaises:
    """The contract. Every one of these must return [], not propagate — a
    conversation step has to be able to say "no matches" without a traceback.
    """

    async def test_a_network_error_returns_empty(self):
        with _patched(_FakeClient(raises=OSError("dns is gone"))):
            assert await am.search_songs("x") == []

    async def test_a_timeout_returns_empty(self):
        import httpx
        with _patched(_FakeClient(raises=httpx.ReadTimeout("slow"))):
            assert await am.search_songs("x") == []

    async def test_an_http_error_returns_empty(self):
        """A 403 is what a blocked IP looks like — the deep-translator
        failure mode, which this must survive visibly rather than crash on."""
        with _patched(_FakeClient({"results": [_result()]}, status=403)):
            assert await am.search_songs("x") == []

    async def test_malformed_json_returns_empty(self):
        with _patched(_FakeClient(ValueError("not json"))):
            assert await am.search_songs("x") == []

    async def test_a_payload_that_is_not_a_dict_returns_empty(self):
        with _patched(_FakeClient(["unexpected"])):
            assert await am.search_songs("x") == []

    async def test_a_missing_results_field_returns_empty(self):
        with _patched(_FakeClient({"resultCount": 0})):
            assert await am.search_songs("x") == []

    async def test_results_that_are_not_a_list_returns_empty(self):
        with _patched(_FakeClient({"results": "nope"})):
            assert await am.search_songs("x") == []

    async def test_results_that_cannot_be_iterated_returns_empty(self):
        """The case that actually needs the isinstance(results, list) guard.

        A STRING passes the test above whether the guard is there or not,
        because the per-entry isinstance(r, dict) filter discards its
        characters — so that test alone does not prove the guard does
        anything. An int cannot be iterated at all, and the loop over results
        runs OUTSIDE the try block, so without the guard this raises
        TypeError and breaks the never-raises contract.

        Found by mutating the guard away and watching the suite stay green.
        """
        with _patched(_FakeClient({"results": 5})):
            assert await am.search_songs("x") == []

    async def test_a_results_field_of_none_returns_empty(self):
        with _patched(_FakeClient({"results": None})):
            assert await am.search_songs("x") == []

    async def test_a_junk_entry_among_good_ones_does_not_lose_the_rest(self):
        with _patched(_FakeClient({"results": ["junk", None, _result()]})):
            assert len(await am.search_songs("x")) == 1

    async def test_a_failure_is_logged_not_swallowed_silently(self):
        """Returning [] must not mean nobody can find out why."""
        with _patched(_FakeClient(raises=OSError("dns is gone"))), \
             patch.object(am.logger, "warning") as warn:
            await am.search_songs("x")
        assert warn.called
        assert "dns is gone" in " ".join(str(a) for a in warn.call_args[0])


class TestTheEnumeratedFormat:
    """The exact shape asked for: index, title, "by Artist", newline, link."""

    SONG = am.Song(title="Amazing Grace", artist="CeCe Winans",
                   album="The Hymns",
                   url="https://music.apple.com/us/album/amazing-grace/1?i=2")

    def test_index_title_artist_then_link_on_its_own_line(self):
        assert self.SONG.enumerated(1) == (
            "1. Amazing Grace by CeCe Winans\n"
            "https://music.apple.com/us/album/amazing-grace/1?i=2")

    def test_the_index_is_the_one_given(self):
        assert self.SONG.enumerated(11).startswith("11. ")

    def test_the_link_is_on_the_second_line(self):
        first, second = self.SONG.enumerated(3).split("\n")
        assert first == "3. Amazing Grace by CeCe Winans"
        assert second == self.SONG.url

    def test_nothing_is_markdown_escaped(self):
        """Sent as PLAIN text, so escaping would show literal backslashes to
        the recipient. A title like "Amazing Grace (My Chains Are Gone)" must
        appear exactly as Apple spells it."""
        song = am.Song(title="Amazing Grace (My Chains Are Gone)",
                       artist="Chris Tomlin", album="x",
                       url="https://music.apple.com/1")
        line = song.enumerated(2)
        assert "\\" not in line
        assert "Amazing Grace (My Chains Are Gone)" in line

    async def test_the_link_carries_no_tracking_parameter(self):
        """The link reaching the recipient must be the cleaned one.

        This test previously asserted the OPPOSITE — that uo=4 was passed
        through untouched — and kept passing after the policy changed, because
        it built a Song directly and so never exercised the cleaning. A test
        that constructs its own input cannot notice a change upstream of it,
        so this one goes through the parser.

        Plain `async def`, no marker and no run_until_complete: pyproject sets
        asyncio_mode = "auto" precisely because hand-driven event loops in
        test files once broke 493 unrelated tests.
        """
        client = _FakeClient({"results": [_result(
            url="https://music.apple.com/us/album/x/1?i=2&uo=4")]})
        with _patched(client):
            songs = await am.search_songs("x")
        assert "uo=" not in songs[0].enumerated(1)
        assert "i=2" in songs[0].enumerated(1)


class TestStrippingTrackingParameters:
    """`uo=4` is Apple's tracking marker and is removed. `i=` is the TRACK id
    and is kept — without it the link opens the ALBUM, which was confirmed by
    fetching all three variants of a real link on 2026-11-02:

        ...?i=6767277651&uo=4 -> "Amazing Grace - Song"
        ...?i=6767277651      -> "Amazing Grace - Song"
        ...(no query)         -> "The Hymns - Album"
    """

    FULL = "https://music.apple.com/us/album/amazing-grace/1896455498?i=6767277651&uo=4"

    def test_the_tracking_parameter_is_removed(self):
        assert "uo=" not in am.clean_url(self.FULL)

    def test_the_track_id_is_kept(self):
        """Dropping it would hand the recipient a fifteen-track album."""
        assert "i=6767277651" in am.clean_url(self.FULL)

    def test_the_path_is_untouched(self):
        assert am.clean_url(self.FULL).startswith(
            "https://music.apple.com/us/album/amazing-grace/1896455498?")

    def test_the_result_is_exactly_the_song_link(self):
        assert am.clean_url(self.FULL) == (
            "https://music.apple.com/us/album/amazing-grace/1896455498"
            "?i=6767277651")

    def test_an_unknown_future_parameter_is_also_dropped(self):
        """A whitelist, not a blacklist: the point is to send NO tracking
        data, including whatever Apple adds next."""
        url = self.FULL + "&at=1001lKQU&ct=newsletter&ls=1"
        cleaned = am.clean_url(url)
        for unwanted in ("at=", "ct=", "ls=", "uo="):
            assert unwanted not in cleaned, unwanted
        assert "i=6767277651" in cleaned

    def test_a_url_with_no_query_string_is_unchanged(self):
        bare = "https://music.apple.com/us/album/the-hymns/1896455498"
        assert am.clean_url(bare) == bare

    def test_a_fragment_is_dropped(self):
        assert "#" not in am.clean_url(self.FULL + "#play")

    def test_a_url_with_only_tracking_loses_its_query_entirely(self):
        assert am.clean_url("https://music.apple.com/us/album/x/1?uo=4") == \
            "https://music.apple.com/us/album/x/1"

    def test_cleaning_is_idempotent(self):
        once = am.clean_url(self.FULL)
        assert am.clean_url(once) == once

    def test_it_never_raises_on_junk(self):
        """The contract of this module. A link that cannot be parsed is worse
        kept than lost, so junk comes back unchanged rather than crashing."""
        for junk in ("", "not a url", "://", "https://"):
            assert isinstance(am.clean_url(junk), str)

"""Song search against Apple's public iTunes Search API.

Used by the worship song-list builder to turn "Amazing Grace CeCe Winans"
into a music.apple.com link an admin can hand to a worship leader.

WHY THIS API AND NOT THE APPLE MUSIC API
----------------------------------------
api.music.apple.com needs a developer token: a JWT signed with a private key
from a paid Apple Developer account. The iTunes Search API needs nothing at
all, and returns the one field that matters — trackViewUrl, which is already a
music.apple.com address.

NO NEW DEPENDENCY
-----------------
httpx ships with python-telegram-bot, so this adds no package. That is not a
detail: deep-translator was removed on 2026-10-13 as the sole source of four
security advisories, and the lesson recorded then was that a dependency has to
earn its place. This one costs nothing.

VERIFIED FROM THE SERVER, NOT JUST THE LAPTOP
---------------------------------------------
deep-translator's free Google endpoint refuses requests from AWS IPs, so every
call from the Lightsail box had been silently returning None long before anyone
noticed. This API was therefore tested from 44.214.123.92 itself on 2026-11-02
before any of this was written, and it answers normally.

DESIGN CONTRACT: search_songs() NEVER RAISES
--------------------------------------------
Any failure — no network, a timeout, an HTTP error, malformed JSON, a blocked
IP — returns an EMPTY LIST. The same contract translation.translate() has, and
for the same reason: a conversation step must be able to say "no matches, try
again" without a traceback reaching the member. Callers distinguish "nothing
found" from "search is broken" by nothing at all, deliberately; both mean the
admin should retype the title.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

SEARCH_URL = "https://itunes.apple.com/search"

# Which Apple Music storefront to search. US matches the bot's timezone, and a
# music.apple.com/us/... link normally redirects a viewer to their own
# storefront. Availability still differs by country, so if members report a
# dead link this is the first thing to change.
STOREFRONT = "US"

# How many candidates to offer. Enough to cover the common case — one hymn
# recorded by several artists — while still fitting an inline keyboard without
# scrolling. "Amazing Grace" alone returns CeCe Winans, Alan Jackson and
# Carrie Underwood, which is exactly the ambiguity this screen exists for.
MAX_RESULTS = 5

# Apple documents a soft limit around 20 calls a minute. One admin typing song
# titles cannot approach that, so there is no throttling here; if this is ever
# driven by anything other than a human, add some.
TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Song:
    """One search hit.

    A dataclass rather than the dict the YAML-backed records use, because
    nothing here is ever written to disk — the list lives in user_data for the
    few minutes a conversation lasts and is gone after a restart, which the
    user chose knowingly.

    `album` is carried only to disambiguate the picker: a search for "Great Is
    Thy Faithfulness" returns CeCe Winans twice, from The Hymns and from Alone
    in His Presence. Two identical-looking buttons would be a coin toss.
    """

    title: str
    artist: str
    album: str
    url: str

    def enumerated(self, index: int) -> str:
        """The exact shape asked for: index, title, "by Artist", link below.

        Plain text, never Markdown. Song titles are full of parentheses,
        ampersands and apostrophes — "Amazing Grace (My Chains Are Gone)" —
        and this project has already lost messages to Markdown parse failures
        once. Telegram turns a bare URL into a link by itself, so nothing is
        gained by marking it up and a whole class of failure is avoided.

        `url` has already been through clean_url(), so no tracking parameter
        reaches the recipient.
        """
        return f"{index}. {self.title} by {self.artist}\n{self.url}"


#: The only query parameter kept on a track link. `i` is the TRACK id, not
#: tracking: it is what makes the URL open the song instead of the album it
#: sits on. Verified 2026-11-02 by fetching all three variants of a real link:
#:
#:   .../amazing-grace/1896455498?i=6767277651&uo=4  -> "Amazing Grace - Song"
#:   .../amazing-grace/1896455498?i=6767277651       -> "Amazing Grace - Song"
#:   .../amazing-grace/1896455498                    -> "The Hymns - Album"
#:
#: So dropping the whole query string would hand the recipient a fifteen-track
#: album and leave them to find the song. `uo=4` is Apple's tracking marker
#: and makes no difference to the page.
#:
#: A WHITELIST rather than dropping `uo` by name: a survey of 225 real track
#: URLs on 2026-11-02 found exactly two parameters, `i` and `uo`, on 100% of
#: them — but a blacklist would silently start passing through whatever Apple
#: adds next, and the point of this is to send no tracking data at all.
KEPT_URL_PARAMS = ("i",)


def clean_url(url: str) -> str:
    """*url* with every query parameter except KEPT_URL_PARAMS removed.

    Also drops any fragment. Never raises: a URL it cannot parse is returned
    unchanged, because a working link with a tracking parameter beats no link.
    """
    try:
        parts = urlsplit(url)
        existing = parse_qs(parts.query)
        kept = [(name, value)
                for name in KEPT_URL_PARAMS
                for value in existing.get(name, [])]
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           urlencode(kept), ""))
    except ValueError as exc:
        logger.warning("Could not clean URL %r: %s", url, exc)
        return url


def _as_song(result: dict) -> Song | None:
    """One API result as a Song, or None when it is not a usable track."""
    if result.get("kind") != "song":
        return None
    title = (result.get("trackName") or "").strip()
    artist = (result.get("artistName") or "").strip()
    url = (result.get("trackViewUrl") or "").strip()
    if not (title and artist and url):
        return None
    return Song(title=title, artist=artist,
                album=(result.get("collectionName") or "").strip(),
                url=clean_url(url))


async def search_songs(query: str,
                       limit: int = MAX_RESULTS) -> list[Song]:
    """Candidate songs for the free-text *query*.

    *query* goes to the API verbatim — it is the same general search the Music
    app's own search box performs, matching title, artist and album at once.
    So "Amazing Grace CeCe Winans" narrows by artist with no parsing here and
    no separate parameter: there ISN'T one. For an entity=song query the
    Search API offers only `attribute=artistTerm`, which searches the artist
    ALONE and ignores the title entirely. Do not add a second field hoping to
    pass the artist separately; there is nowhere for it to go.

    One consequence is worth knowing before anyone adds "helpful" parsing:
    measured on 2026-11-03 over twelve title/artist pairs, sending the word
    "by" between them ("Great Is Thy Faithfulness by CeCe Winans") moved the
    top hit in five, and in one case to a DIFFERENT ARTIST — Carrie
    Underwood's recording outranked the one asked for. The word is treated as
    a search term like any other. The caller's prompt therefore asks for a
    song and artist without suggesting "by", which is cheaper and more
    reliable than stripping it afterwards.

    Never raises; returns [] on any failure. See the module docstring.
    """
    term = (query or "").strip()
    if not term:
        return []

    params = {"term": term, "entity": "song", "limit": limit,
              "country": STOREFRONT}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(SEARCH_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:   # noqa: BLE001 - the contract is never to raise
        logger.warning("Song search for %r failed: %s: %s",
                       term, type(exc).__name__, exc)
        return []

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        logger.warning("Song search for %r returned no usable results field.",
                       term)
        return []

    songs = [song for song in (_as_song(r) for r in results
                               if isinstance(r, dict)) if song is not None]
    logger.info("Song search for %r returned %d usable result(s).",
                term, len(songs))
    return songs[:limit]

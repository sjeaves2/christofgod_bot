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

import html
import logging
import re
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

# Artwork size to request, in pixels. The API only ever returns 30, 60 and 100,
# all far too small to recognise an album by — but the size is encoded in the
# URL PATH, so asking for another one costs nothing. Verified 2026-11-16:
#
#   .../196874328200.jpg/100x100bb.jpg ->   4 KB
#   .../196874328200.jpg/300x300bb.jpg ->  22 KB
#   .../196874328200.jpg/600x600bb.jpg ->  83 KB
#
# 300 is the largest that still fits Telegram's sendAudio thumbnail limits
# (JPEG, at most 320x320 and 200 kB) — which is why it is this and not 600.
# A larger value here would be accepted by Apple and then silently rejected by
# Telegram, costing the thumbnail rather than raising anything.
ARTWORK_SIZE = 300

#: Matches the size segment at the end of an artwork URL: `100x100bb.jpg`.
#: Anchored at the end so it can only ever rewrite the final path segment, and
#: requires the two numbers to match each other — Apple's artwork is square,
#: and a non-square segment is something this function does not understand and
#: must therefore leave alone.
_ARTWORK_SIZE_RE = re.compile(r"/(\d+)x\1bb\.jpg$")

#: Host suffixes whose media we are willing to hand to Telegram.
#:
#: NOT decoration. `preview_url` and `artwork_url` come from a third-party
#: response and are passed to sendAudio, which makes TELEGRAM fetch them —
#: so an attacker-controlled or merely wrong value turns the bot into a
#: fetch-this-for-me service pointed at a host of someone else's choosing.
#: `trackViewUrl` has always been trusted because it is only ever shown to a
#: human as text; these two are different in kind, because something
#: automatically dereferences them.
#:
#: Checked as a suffix after a leading dot, so `mzstatic.com.evil.test` cannot
#: match, and `is1-ssl.mzstatic.com` through `is5-ssl...` all can — Apple
#: rotates that prefix and pinning the full host would break on their whim.
MEDIA_HOST_SUFFIXES = (".apple.com", ".mzstatic.com")


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

    Everything after `url` is for IDENTIFYING the song, not for recording it.
    Members reported on 2026-11-16 that a title and an artist are often not
    enough to tell two recordings apart — "Amazing Grace" returns CeCe Winans
    from The Hymns and a 2:36 track off a coffee-shop jazz compilation
    released the day before. The album, the duration and the year separate
    those two at a glance and cost nothing: all three were already in the
    response and were being discarded.

    The four new fields carry defaults because the API omits fields.
    `_as_song` requires only the title/artist/url trio, and a result missing
    its preview or its artwork is still a perfectly good search hit — so
    every consumer must treat these as optional rather than assume the API is
    generous.
    """

    title: str
    artist: str
    album: str
    url: str
    preview_url: str = ""
    artwork_url: str = ""
    duration_ms: int | None = None
    release_year: int | None = None

    @property
    def duration(self) -> str:
        """The running time as `M:SS`, or "" when the API did not say.

        Seconds are FLOORED, matching what a music player shows: 253338 ms is
        4:13 in the Music app, not 4:14.
        """
        if not self.duration_ms or self.duration_ms < 0:
            return ""
        total = self.duration_ms // 1000
        return f"{total // 60}:{total % 60:02d}"

    def detail(self, index: int) -> str:
        """One candidate as a line for the picker: what distinguishes it.

        Separate from `enumerated` on purpose. `enumerated` is the RECORD the
        recipient keeps and must stay stable — its shape is asserted against
        the delivered message. This is the CHOOSING aid, shown only to the
        admin mid-search, and is free to carry whatever helps them decide.

        Escaped for HTML on the same grounds as `enumerated`: album names
        carry ampersands ("Shout to the Lord & Other Songs") far more often
        than track titles do.
        """
        title = html.escape(self.title, quote=False)
        artist = html.escape(self.artist, quote=False)
        line = f"{index}. <b>{title}</b> — {artist}"
        # Only the facts the API actually gave us. A trailing " · · 2026" from
        # blindly joining placeholders reads like a bug to the one person this
        # screen exists to help.
        facts = [html.escape(self.album, quote=False) if self.album else "",
                 self.duration,
                 str(self.release_year) if self.release_year else ""]
        kept = [fact for fact in facts if fact]
        if kept:
            line += "\n    " + " · ".join(kept)
        return line

    def enumerated(self, index: int) -> str:
        """The exact shape asked for: index, title, "by Artist", link below.

        The title is bold, so the message must be sent with HTML parse_mode.

        HTML AND NOT MARKDOWN, DELIBERATELY
        -----------------------------------
        This was plain text until 2026-11-03, because the project had lost
        messages to Markdown parse failures and song titles are full of the
        characters that cause them: "Amazing Grace (My Chains Are Gone)",
        "10,000 Reasons (Bless the Lord) [Radio Version] [Live]". MarkdownV2
        would need eighteen characters escaped, several of which appear in
        almost every title.

        HTML needs exactly three — & < > — which html.escape puts beyond
        doubt. Parentheses, brackets, commas, dots and apostrophes are all
        literal, so the titles that made Markdown dangerous are inert here.

        quote=False on purpose: Telegram's HTML mode documents only those
        three entities, so escaping apostrophes to &#x27; would be trusting
        it to unescape something it never promised to. Titles like
        "Ain't No Grave" are common enough for that to matter.

        The URL is escaped too. Today clean_url leaves at most the single `i`
        parameter, so there is never an & to escape — but that is a property
        of KEPT_URL_PARAMS, not a law, and a second kept parameter would join
        with & and silently break every link.

        Telegram still turns the bare URL into a link by itself, so it is left
        unmarked. `url` has been through clean_url(), so no tracking parameter
        reaches the recipient.
        """
        title = html.escape(self.title, quote=False)
        artist = html.escape(self.artist, quote=False)
        url = html.escape(self.url, quote=False)
        return f"{index}. <b>{title}</b> by {artist}\n{url}"


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


def media_url(url: str) -> str:
    """*url* if it is an https Apple address we may hand to Telegram, else "".

    Returning "" rather than raising keeps the never-raises contract: a hit
    whose preview fails this check is still shown and still pickable, just
    without a clip. Silently passing it through is the one option that is not
    available, because something downstream dereferences it.
    """
    try:
        parts = urlsplit((url or "").strip())
    except ValueError as exc:
        logger.warning("Could not parse media URL %r: %s", url, exc)
        return ""
    if parts.scheme != "https":
        return ""
    # .hostname, not .netloc, and lowercased by urlsplit: the suffix test is
    # a statement about the HOST, so it is given the host alone, with any
    # user:password@ and :port already removed.
    #
    # NOT for the reason it looks like. A first version of this comment said a
    # netloc check would let `https://trusted.mzstatic.com@evil.test/` through;
    # it would not — that netloc ends in `evil.test` and is refused either way.
    # Measured, both directions, 2026-11-16:
    #
    #   https://is1-ssl.mzstatic.com@evil.test/  netloc …@evil.test  host evil.test
    #   https://evil.test@is1-ssl.mzstatic.com/  netloc …mzstatic.com host mzstatic
    #   https://is1-ssl.mzstatic.com:8443/       netloc …com:8443     host mzstatic
    #
    # So netloc is STRICTER, never looser: it would refuse a genuine Apple URL
    # carrying a port. .hostname is right because it is the host that decides,
    # and the test below should not also be a test of the port.
    host = (parts.hostname or "").lower()
    if not any(host == suffix[1:] or host.endswith(suffix)
               for suffix in MEDIA_HOST_SUFFIXES):
        logger.warning("Refusing media URL from unexpected host %r.", host)
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       parts.query, ""))


def upsize_artwork(url: str, size: int = ARTWORK_SIZE) -> str:
    """*url* asking for a *size*x*size* image instead of whatever it asked for.

    A URL whose final segment is not Apple's `NNNxNNNbb.jpg` is returned
    unchanged — a recognisable small image beats no image, and a shape this
    does not understand is not one to start rewriting.
    """
    return _ARTWORK_SIZE_RE.sub(f"/{size}x{size}bb.jpg", url, count=1)


def _release_year(raw: object) -> int | None:
    """The year out of an API `releaseDate`, or None.

    The API sends ISO-8601 with a Z suffix ("2026-06-05T12:00:00Z"). Only the
    leading four digits are taken, deliberately: a full datetime parse would
    be a second thing that can fail for a field used for nothing but display.
    Anything that is not four plausible digits becomes None and the year is
    simply not shown.
    """
    if not isinstance(raw, str):
        return None
    head = raw.strip()[:4]
    if not head.isdigit():
        return None
    year = int(head)
    return year if 1860 <= year <= 2200 else None


def _duration_ms(raw: object) -> int | None:
    """`trackTimeMillis` as a positive int, or None.

    The API has been seen to send this as a string. bool is excluded because
    it is an int subclass and True would become a one-millisecond track.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        return value or None
    return None


#: Telegram rejects a sendAudio thumbnail over 200 kB. Artwork at
#: ARTWORK_SIZE measures about 22 kB, so this is a sanity bound on a
#: third-party response rather than a real expectation — but without it a URL
#: that unexpectedly serves something enormous would be read into memory on a
#: 412 MB box before Telegram threw it away.
MAX_ARTWORK_BYTES = 200 * 1024

#: JPEG's magic number. Telegram requires the thumbnail to be JPEG and will
#: reject anything else, so a PNG or an HTML error page dressed as an image is
#: better caught here, where it costs a missing thumbnail, than there, where it
#: costs the whole clip.
_JPEG_MAGIC = b"\xff\xd8\xff"

#: Upper bound on a preview clip, for the same reason as MAX_ARTWORK_BYTES: a
#: sanity bound on a third-party response, not an expectation. Every clip
#: measured on 2026-11-16 was 949-1050 kB, and Telegram's own ceiling for an
#: audio upload is 50 MB, so 8 MB sits well clear of the real sizes while
#: still refusing to read something absurd into 412 MB of RAM.
MAX_PREVIEW_BYTES = 8 * 1024 * 1024

#: An MPEG-4 container announces itself with the box type `ftyp` at offset 4 —
#: the first four bytes are that box's length. Verified against a real clip on
#: 2026-11-16: `00 00 00 18 66 74 79 70 4d 34 41 20`, i.e. `ftyp` then the
#: brand `M4A `. Checked so an HTML error page served with a 200 cannot be
#: uploaded to Telegram as audio.
_MP4_BOX_TYPE = b"ftyp"
_MP4_BOX_TYPE_OFFSET = 4


async def fetch_artwork(url: str) -> bytes | None:
    """The JPEG at *url* as bytes, or None.

    WHY THIS EXISTS AT ALL — THE BOT DID NOT USED TO FETCH ANYTHING
    ---------------------------------------------------------------
    sendAudio's `thumbnail` will NOT take a URL: Telegram treats it as a
    file_id and accepts only a freshly uploaded file, so python-telegram-bot
    handing our URL through as a plain string meant the artwork was silently
    discarded — shipped that way in v0.29.0 and spotted by the user on the
    first live run, because a dropped thumbnail raises nothing.

    Sending bytes is NECESSARY AND NOT SUFFICIENT. The same field's docs add
    "Ignored if the file is not uploaded using multipart/form-data", where
    *the file* is the AUDIO — so a correct thumbnail beside a LINKED clip is
    still discarded, which is what v0.29.1 shipped. See fetch_preview.

    So the bytes have to come from here. That is a real change in kind: this
    path previously had Telegram do every fetch, and therefore could not
    suffer the deep-translator failure where an endpoint refused AWS addresses
    and nothing in production worked for months. It can now, so it was
    verified from 44.214.123.92 itself on 2026-11-16 — 200, 22442 bytes, 50ms
    — and that check belongs in any future change to this function.

    Never raises, like search_songs: a missing thumbnail must cost the
    thumbnail and nothing else. Returns None on any failure, and the caller
    sends the clip without one.
    """
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.content
    except Exception as exc:   # noqa: BLE001 - the contract is never to raise
        logger.warning("Artwork fetch for %r failed: %s: %s",
                       url, type(exc).__name__, exc)
        return None

    if len(data) > MAX_ARTWORK_BYTES:
        logger.warning("Artwork at %r is %d bytes, over the %d limit.",
                       url, len(data), MAX_ARTWORK_BYTES)
        return None
    if not data.startswith(_JPEG_MAGIC):
        logger.warning("Artwork at %r is not a JPEG (%r).", url, data[:4])
        return None
    return data


async def fetch_preview(url: str) -> bytes | None:
    """The preview clip at *url* as bytes, or None.

    WHY THE BOT DOWNLOADS AUDIO IT USED TO JUST LINK
    ------------------------------------------------
    sendAudio's `thumbnail` says, two sentences into its own documentation,
    "Ignored if the file is not uploaded using multipart/form-data." *The
    file* means the AUDIO. So while `audio` is an HTTP URL — which that field
    explicitly allows — Telegram discards the thumbnail no matter how
    correctly it is supplied. Silently: no error, no warning, nothing in
    errors.log.

    That is why v0.29.1 did not work. v0.29.0 sent the thumbnail as a URL,
    which is invalid for that field; v0.29.1 correctly sent bytes, and
    Telegram ignored them anyway because the audio beside them was a link.
    Both releases looked right and shipped broken, and both were invisible
    to the suite and the logs — only a person looking at the chat could tell.

    So the clip is uploaded rather than linked. The caller falls back to the
    URL when this returns None: a clip with no thumbnail is worth far more
    than no clip, and that fallback is the whole reason this never raises.

    COST, MEASURED RATHER THAN GUESSED
    ----------------------------------
    About 1 MB and 1.0s per clip from 44.214.123.92 (200, 1075543 bytes,
    2026-11-16), so five candidates cost roughly 5 MB down and 5 MB up where
    they previously cost the bot nothing. The $5 plan's allowance makes that
    irrelevant, and the box had 189 MB free with one clip held at a time.

    Like fetch_artwork, this means the AWS-IP failure mode now reaches the
    audio CDN too — audio-ssl.itunes.apple.com was therefore verified from
    the server itself, not just a laptop.
    """
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.content
    except Exception as exc:   # noqa: BLE001 - the contract is never to raise
        logger.warning("Preview fetch for %r failed: %s: %s",
                       url, type(exc).__name__, exc)
        return None

    if len(data) > MAX_PREVIEW_BYTES:
        logger.warning("Preview at %r is %d bytes, over the %d limit.",
                       url, len(data), MAX_PREVIEW_BYTES)
        return None
    end = _MP4_BOX_TYPE_OFFSET + len(_MP4_BOX_TYPE)
    if data[_MP4_BOX_TYPE_OFFSET:end] != _MP4_BOX_TYPE:
        logger.warning("Preview at %r is not an MPEG-4 container (%r).",
                       url, data[:12])
        return None
    return data


def _as_song(result: dict) -> Song | None:
    """One API result as a Song, or None when it is not a usable track."""
    if result.get("kind") != "song":
        return None
    title = (result.get("trackName") or "").strip()
    artist = (result.get("artistName") or "").strip()
    url = (result.get("trackViewUrl") or "").strip()
    if not (title and artist and url):
        return None
    artwork = media_url(result.get("artworkUrl100")
                        or result.get("artworkUrl60") or "")
    return Song(title=title, artist=artist,
                album=(result.get("collectionName") or "").strip(),
                url=clean_url(url),
                preview_url=media_url(result.get("previewUrl") or ""),
                artwork_url=upsize_artwork(artwork) if artwork else "",
                duration_ms=_duration_ms(result.get("trackTimeMillis")),
                release_year=_release_year(result.get("releaseDate")))


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

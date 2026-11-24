"""Reading a pasted song list, and deciding which matches deserve a second look.

Pure functions over text and Song objects: no Telegram, no network, no state.
handlers/worship.py drives them; everything here can be tested by calling it.

WHAT A MATCH CAN AND CANNOT TELL ANYONE
---------------------------------------
Apple returns SOMETHING for almost any search, so the top result being present
says nothing about its being right. These checks compare the words the admin
typed with the words Apple returned. They catch a different song and a
different artist. They cannot tell a studio recording from a live one unless
somebody typed "live", so no flag means "the words agree" and never
"this is the recording you wanted". The review screen therefore shows every
match with its album, length and year, flagged or not.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from apple_music import Song

#: List numbering and bullets at the start of a pasted line.
#:
#: The lookahead after "1." matters: "4.2" or "10.000" must not lose its front.
#: A bare number with no punctuation is NOT stripped, because it is as likely
#: to be the title ("10,000 Reasons", "99 and a Half") as a position.
_LEADER_RE = re.compile(
    r"""^\s*(?:
        \d{1,3}️?⃣\s*        # keycap emoji: 1️⃣
      | \d{1,3}\s*[.)\]:]\s*(?=\D)     # 1.  1)  1]  1:
      | \d{1,3}\s+[-–—]\s+             # 1 - Title
      | [-–—*•·▪●]\s*                  # bullets
    )""", re.VERBOSE)

#: A dash used as a separator. Spaces on BOTH sides, so "Yah-weh" survives.
_DASH_RE = re.compile(r"\s+[-–—]\s+")

#: Words that say nothing about WHICH song this is, so a match lacking one is
#: not suspicious. Kept short on purpose: every word here is a word whose
#: absence goes unflagged. "live", "acoustic" and the like must never join it
#: — they are exactly what an admin types to get one recording over another.
FILLER = frozenset({"a", "an", "and", "by", "feat", "featuring", "ft", "the",
                    "with"})


#: A bracketed credit in a title: somebody NAMED there but not the artist.
#:
#: Measured against Apple on 2026-11-24. "Great Is Thy Faithfulness by CeCe
#: Winans" returns Carrie Underwood's recording, titled "... (feat. CeCe
#: Winans)", and "Total Praise Richard Smallwood live" returns Donnie
#: McClurkin's. Every typed word is present and the artist is still wrong.
#: Karaoke and tribute tracks name the real artist the same way.
_CREDIT_RE = re.compile(
    r"""[(\[]\s*(?:
        with\b[^)\]]*
      | [^)\]]*\b(?:feat|ft|featuring|famous|originally|tribute|style\s+of)\b
        [^)\]]*
    )[)\]]""", re.VERBOSE | re.IGNORECASE)

#: Typed by an admin who wants the featured recording, so it is no surprise.
_CREDIT_WORDS = frozenset({"feat", "featuring", "ft", "with"})


def parse_lines(text: str) -> list[str]:
    """The songs in a pasted message, one per non-blank line.

    Numbering and bullets are dropped and a spaced dash becomes a space,
    because Apple's search reads every token as something to match.

    "by" is deliberately left alone (the user's decision, 2026-11-23): it
    does hurt the ranking, but it is also part of titles such as "Stand By
    Me", and the ❓ flag catches a match it spoiled.
    """
    songs = []
    for line in (text or "").splitlines():
        line = _LEADER_RE.sub("", line)
        line = " ".join(_DASH_RE.sub(" ", line).split())
        if line:
            songs.append(line)
    return songs


def is_list(text: str) -> bool:
    """Whether a message is a pasted list rather than one song.

    Counted BEFORE numbering is stripped, so this is a plain question about
    the message: more than one line with something on it.
    """
    return sum(1 for line in (text or "").splitlines() if line.strip()) > 1


def words(text: str) -> list[str]:
    """*text* as comparable words: no case, accents, or punctuation.

    Apostrophes are removed rather than split on, so "Ain't" typed as "aint"
    still agrees. Commas inside a number go too: "10,000" must equal "10000".
    """
    text = unicodedata.normalize("NFKD", text or "").casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"['’‘ʼ`]", "", text)
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    return re.findall(r"[^\W_]+", text)


def _joined_pairs(tokens: list[str]) -> set[str]:
    return {a + b for a, b in zip(tokens, tokens[1:])}


def _absent(typed: list[str], found: list[str]) -> list[str]:
    """The words of *typed* that *found* does not contain.

    Two words run together count as their parts and the reverse: "waymaker"
    agrees with "Way Maker", and "way maker" with "Waymaker". Both spellings
    are common for worship titles, and a flag on them would teach the reader
    to ignore flags.
    """
    have = set(found)
    joined = _joined_pairs(found)
    absent = []
    for n, word in enumerate(typed):
        if word in have or word in joined:
            continue
        after = word + typed[n + 1] if n + 1 < len(typed) else ""
        before = typed[n - 1] + word if n else ""
        if after in have or before in have:
            continue
        absent.append(word)
    return absent


def _significant(query: str) -> list[str]:
    """The words of *query* worth checking; all of them if only filler."""
    typed = words(query)
    return [word for word in typed if word not in FILLER] or typed


def missing_words(query: str, song: Song) -> list[str]:
    """Words the admin typed that this match has nowhere.

    Not in the title, the artist, or the album. A wrong artist shows up here
    by name, which "does the title share a word?" would have let through.
    """
    found = words(song.title) + words(song.artist) + words(song.album)
    return list(dict.fromkeys(_absent(_significant(query), found)))


def credited_words(query: str, song: Song) -> list[str]:
    """Words the admin typed that appear only in a bracketed credit.

    See _CREDIT_RE. Empty when the admin typed "feat" or "with" themselves.
    """
    if _CREDIT_WORDS & set(words(query)):
        return []
    found = (words(_CREDIT_RE.sub(" ", song.title)) + words(song.artist)
             + words(song.album))
    nowhere = set(missing_words(query, song))
    return [word for word in
            dict.fromkeys(_absent(_significant(query), found))
            if word not in nowhere]


def several_artists(query: str, chosen: Song, results: list[Song]) -> bool:
    """Whether *query* names a title that more than one artist recorded.

    True only when the artist was not typed: every word typed is already in
    the title, and none is in the artist. Then the top result is merely
    Apple's favourite, and the admin should know there was a choice.
    """
    typed = _significant(query)
    if not typed or _absent(typed, words(chosen.title)):
        return False
    if set(typed) & set(words(chosen.artist)):
        return False
    artists = {tuple(words(song.artist)) for song in results
               if not _absent(typed, words(song.title))}
    return len(artists) > 1


@dataclass
class Line:
    """One pasted line and what has been decided about it.

    `confirmed` means a person picked this result themselves, which answers
    the ❓ and 👥 questions: repeating them afterwards would be nagging.
    """

    query: str
    results: list[Song] = field(default_factory=list)
    chosen: int | None = None
    searched: bool = False
    confirmed: bool = False
    dropped: bool = False

    @property
    def song(self) -> Song | None:
        """The match this line will contribute, or None."""
        if self.dropped or self.chosen is None:
            return None
        return self.results[self.chosen]

    @property
    def missing(self) -> bool:
        """Searched, and nothing came back."""
        return self.searched and not self.dropped and self.chosen is None

    def record(self, results: list[Song]) -> None:
        """Store a search's answer, provisionally taking the top result."""
        self.results = results
        self.searched = True
        self.chosen = 0 if results else None

    def settle(self, query: str, results: list[Song], index: int) -> None:
        """Store a result a person picked."""
        self.query = query
        self.results = results
        self.chosen = index
        self.searched = True
        self.confirmed = True
        self.dropped = False


MISSING = "⚠️"
DOUBTFUL = "❓"
SEVERAL = "👥"
DUPLICATE = "🔁"
DROPPED = "🚫"
FINE = "✓"
PENDING = "⏳"


def _quoted(some: list[str]) -> str:
    return ", ".join(f"“{word}”" for word in some)


def flags(lines: list[Line],
          already: list[Song] = ()) -> list[tuple[str, str]]:
    """An (icon, note) for every line, in order.

    One icon each, most serious first: a line with no song at all matters
    more than a doubtful one, and a doubtful one more than a choice of
    artists. *already* is the songs chosen before this paste.
    """
    seen: dict[str, str] = {song.url: "already in your list"
                            for song in already}
    out = []
    for number, line in enumerate(lines, 1):
        song = line.song
        if line.dropped:
            out.append((DROPPED, "left out"))
            continue
        if not line.searched:
            out.append((PENDING, "not searched yet"))
            continue
        if song is None:
            out.append((MISSING, "not found"))
            continue

        earlier = seen.get(song.url)
        seen.setdefault(song.url, f"the same recording as number {number}")
        absent = [] if line.confirmed else missing_words(line.query, song)
        credited = [] if line.confirmed else credited_words(line.query, song)
        notes = []
        icon = FINE
        if earlier:
            icon, notes = DUPLICATE, [earlier]
        if credited:
            icon = DOUBTFUL
            notes.insert(0, f"{_quoted(credited)} credited in the title, but "
                            f"the artist is {song.artist}")
        if absent:
            icon = DOUBTFUL
            verb = "is" if len(absent) == 1 else "are"
            notes.insert(0, f"{_quoted(absent)} {verb} not in this match")
        if (icon != DOUBTFUL and not line.confirmed
                and several_artists(line.query, song, line.results)):
            if not earlier:
                icon = SEVERAL
            notes.append("several artists recorded this")
        out.append((icon, "; ".join(notes)))
    return out

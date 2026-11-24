"""Reading a pasted song list, and flagging matches that need a second look.

song_matching is pure, so everything here is a plain call.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import song_matching as m
from apple_music import Song
from song_matching import Line


def _song(title, artist="CeCe Winans", album="The Hymns", n=1):
    return Song(title, artist, album, f"https://music.apple.com/us/album/x/{n}?i={n}")


def _line(query, *results, **kw):
    line = Line(query)
    line.record(list(results))
    for key, value in kw.items():
        setattr(line, key, value)
    return line


class TestParsing:
    @pytest.mark.parametrize("raw", [
        "1. Amazing Grace", "1) Amazing Grace", "1.Amazing Grace",
        "12. Amazing Grace", "1: Amazing Grace", "1 - Amazing Grace",
        "- Amazing Grace", "• Amazing Grace", "* Amazing Grace",
        "–Amazing Grace", "1️⃣ Amazing Grace", "   Amazing   Grace  ",
    ])
    def test_numbering_and_bullets_go(self, raw):
        assert m.parse_lines(raw) == ["Amazing Grace"]

    @pytest.mark.parametrize("raw", [
        "10,000 Reasons", "10000 Reasons", "99 and a Half", "4.2 Degrees",
        "3:16 Song", "Yah-weh",
    ])
    def test_a_title_is_not_mistaken_for_numbering(self, raw):
        assert m.parse_lines(raw) == [raw]

    @pytest.mark.parametrize("dash", ["-", "–", "—"])
    def test_a_spaced_dash_becomes_a_space(self, dash):
        assert m.parse_lines(f"Way Maker {dash} Sinach") == ["Way Maker Sinach"]

    def test_only_the_first_number_goes(self):
        assert m.parse_lines("1. 24. Psalm") == ["24. Psalm"]

    def test_by_is_left_alone(self):
        """The user's decision, 2026-11-23."""
        assert m.parse_lines("Stand By Me by Ben E King") == [
            "Stand By Me by Ben E King"]

    def test_blank_lines_and_bare_bullets_are_not_songs(self):
        assert m.parse_lines("A\n\n  \n•\nB\r\nC") == ["A", "B", "C"]

    def test_nothing_is_nothing(self):
        assert m.parse_lines("") == [] and m.parse_lines(None) == []

    @pytest.mark.parametrize("text,expected", [
        ("Amazing Grace", False), ("\nAmazing Grace\n\n", False), ("", False),
        ("A\nB", True), ("A\n\n\nB", True), (None, False),
    ])
    def test_a_list_is_two_lines_or_more(self, text, expected):
        assert m.is_list(text) is expected


class TestWords:
    def test_case_accents_and_punctuation_do_not_matter(self):
        assert m.words("Señor, HOLY!") == ["senor", "holy"]

    @pytest.mark.parametrize("apostrophe", ["'", "’", "‘", "ʼ", "`"])
    def test_an_apostrophe_is_removed_not_split_on(self, apostrophe):
        assert m.words(f"Ain{apostrophe}t") == ["aint"]

    def test_a_comma_inside_a_number_is_removed(self):
        assert m.words("10,000 Reasons, Live") == ["10000", "reasons", "live"]


class TestMissingWords:
    def test_a_full_match_is_missing_nothing(self):
        assert m.missing_words("amazing grace cece winans",
                               _song("Amazing Grace")) == []

    def test_a_wrong_artist_is_named(self):
        """The likeliest mistake: the right hymn by someone else."""
        assert m.missing_words("Amazing Grace Carrie Underwood",
                               _song("Amazing Grace")) == ["carrie", "underwood"]

    def test_the_album_counts(self):
        assert m.missing_words("Amazing Grace Hymns",
                               _song("Amazing Grace")) == []

    def test_filler_is_not_reported(self):
        assert m.missing_words("Amazing Grace by the CeCe Winans",
                               _song("Amazing Grace")) == []

    def test_live_is_never_filler(self):
        """It is what an admin types to get one recording over another."""
        assert m.missing_words("Amazing Grace live",
                               _song("Amazing Grace")) == ["live"]

    def test_a_query_of_only_filler_is_still_checked(self):
        assert m.missing_words(
            "With The", _song("Amazing Grace", album="")) == ["with", "the"]

    @pytest.mark.parametrize("typed,title", [
        ("waymaker", "Way Maker"), ("way maker", "Waymaker"),
        ("the waymaker song", "Way Maker Song"),
    ])
    def test_run_together_words_agree(self, typed, title):
        assert m.missing_words(typed, _song(title)) == []

    def test_a_part_of_a_word_does_not_agree(self):
        assert m.missing_words("art", _song("Heart")) == ["art"]

    def test_a_repeated_word_is_reported_once(self):
        assert m.missing_words("holy holy zz zz",
                               _song("Holy")) == ["zz"]


class TestCreditedWords:
    """Every typed word present, and the artist still wrong."""

    @pytest.mark.parametrize("credit", [
        "(feat. CeCe Winans)", "[feat. CeCe Winans]", "(ft. CeCe Winans)",
        "(featuring CeCe Winans)", "(with CeCe Winans)", "(With CeCe Winans)",
        "(Made Famous by CeCe Winans)", "(Originally by CeCe Winans)",
        "(In the Style of CeCe Winans)", "[A Tribute to CeCe Winans]",
        "(Live feat. CeCe Winans)",
    ])
    def test_a_credit_is_not_the_artist(self, credit):
        song = _song(f"Great Is Thy Faithfulness {credit}", "Carrie Underwood")
        query = "Great Is Thy Faithfulness CeCe Winans"
        assert m.missing_words(query, song) == []
        assert m.credited_words(query, song) == ["cece", "winans"]

    def test_the_real_artist_is_not_flagged(self):
        song = _song("Grace (feat. Someone)")
        assert m.credited_words("Grace CeCe Winans", song) == []

    def test_an_ordinary_bracket_is_not_a_credit(self):
        song = _song("Amazing Grace (My Chains Are Gone) [Live]")
        assert m.credited_words("amazing grace my chains live", song) == []

    @pytest.mark.parametrize("word", ["feat", "ft", "featuring", "with"])
    def test_typing_the_credit_asks_for_it(self, word):
        song = _song("Grace (feat. Someone)")
        assert m.credited_words(f"Grace {word} Someone", song) == []

    def test_an_absent_word_is_not_also_credited(self):
        song = _song("Grace (feat. Someone)")
        assert m.credited_words("Grace Someone Nobody", song) == ["someone"]


class TestSeveralArtists:
    A = _song("Amazing Grace", "CeCe Winans", n=1)
    B = _song("Amazing Grace", "Alan Jackson", n=2)
    OTHER = _song("Grace Amazing Love", "Third", n=3)

    def test_a_bare_title_with_two_artists(self):
        assert m.several_artists("amazing grace", self.A, [self.A, self.B])

    def test_one_artist_twice_is_not_several(self):
        again = _song("Amazing Grace", "CeCe  WINANS", n=9)
        assert not m.several_artists("amazing grace", self.A, [self.A, again])

    def test_a_different_song_does_not_count(self):
        other = _song("Goodness of God", "Other", n=4)
        assert not m.several_artists("amazing grace", self.A, [self.A, other])

    def test_not_when_the_artist_was_typed(self):
        assert not m.several_artists("amazing grace winans", self.A,
                                     [self.A, self.B])

    def test_not_when_part_of_the_artist_is_also_in_the_title(self):
        a = _song("Grace", "Grace", n=1)
        b = _song("Grace", "Other", n=2)
        assert not m.several_artists("grace", a, [a, b])

    def test_an_empty_query_is_not_several(self):
        assert not m.several_artists("!!", self.A, [self.A, self.B])


class TestLine:
    def test_the_top_result_is_taken_provisionally(self):
        a, b = _song("A", n=1), _song("B", n=2)
        line = _line("a", a, b)
        assert line.song is a and not line.confirmed and not line.missing

    def test_no_results_is_missing(self):
        line = _line("a")
        assert line.song is None and line.missing

    def test_an_unsearched_line_is_not_missing(self):
        assert not Line("a").missing

    def test_a_dropped_line_has_no_song_and_is_not_missing(self):
        line = _line("a", _song("A"), dropped=True)
        assert line.song is None and not line.missing
        assert not _line("a", dropped=True).missing

    def test_settling_replaces_everything(self):
        a, b = _song("A", n=1), _song("B", n=2)
        line = _line("typo", dropped=True)
        line.settle("right", [a, b], 1)
        assert (line.query, line.song, line.confirmed, line.dropped) == (
            "right", b, True, False)
        assert not line.missing and line.searched


class TestFlags:
    A = _song("Amazing Grace", "CeCe Winans", n=1)
    B = _song("Amazing Grace", "Alan Jackson", n=2)

    def test_a_good_match(self):
        assert m.flags([_line("amazing grace winans", self.A)]) == [("✓", "")]

    def test_not_found(self):
        assert m.flags([_line("zz")]) == [("⚠️", "not found")]

    def test_not_searched(self):
        assert m.flags([Line("zz")])[0][0] == "⏳"

    def test_left_out(self):
        assert m.flags([_line("a", self.A, dropped=True)]) == [
            ("🚫", "left out")]

    def test_doubtful_names_the_words(self):
        icon, note = m.flags([_line("amazing grace tomlin", self.A)])[0]
        assert icon == "❓" and note == "“tomlin” is not in this match"

    def test_doubtful_plural(self):
        assert m.flags([_line("amazing grace chris tomlin", self.A)])[0][1] == \
            "“chris”, “tomlin” are not in this match"

    def test_a_featured_artist_is_doubtful_and_names_the_real_one(self):
        song = _song("Grace (feat. CeCe Winans)", "Carrie Underwood")
        icon, note = m.flags([_line("grace cece winans", song)])[0]
        assert icon == "❓" and "credited" in note and "Carrie Underwood" in note

    def test_several_artists(self):
        icon, note = m.flags([_line("amazing grace", self.A, self.B)])[0]
        assert icon == "👥" and "several artists" in note

    def test_a_human_pick_settles_doubt_and_choice(self):
        one = _line("amazing grace tomlin", self.A, confirmed=True)
        two = _line("amazing grace", self.B, self.A, confirmed=True)
        assert m.flags([one, two]) == [("✓", ""), ("✓", "")]

    def test_a_human_pick_settles_a_credit(self):
        song = _song("Grace (feat. CeCe Winans)", "Carrie Underwood")
        assert m.flags([_line("grace cece winans", song, confirmed=True)]) == [
            ("✓", "")]

    def test_a_duplicate_points_at_the_first(self):
        lines = [_line("amazing grace winans", self.A) for _ in range(3)]
        assert m.flags(lines) == [
            ("✓", ""), ("🔁", "the same recording as number 1"),
            ("🔁", "the same recording as number 1")]

    def test_a_duplicate_of_a_song_already_chosen(self):
        assert m.flags([_line("amazing grace winans", self.A)], [self.A]) == [
            ("🔁", "already in your list")]

    def test_a_duplicate_survives_a_human_pick(self):
        """Unlike ❓, nobody has answered it."""
        assert m.flags([_line("x", self.A, confirmed=True)], [self.A])[0][0] == "🔁"

    def test_a_dropped_line_is_not_what_a_later_one_duplicates(self):
        lines = [_line("amazing grace winans", self.A, dropped=True),
                 _line("amazing grace winans", self.A)]
        assert m.flags(lines)[1] == ("✓", "")

    def test_doubt_outranks_duplication_and_keeps_both_notes(self):
        icon, note = m.flags([_line("amazing grace zz", self.A)], [self.A])[0]
        assert icon == "❓"
        assert "“zz”" in note and "already in your list" in note

    def test_duplication_outranks_choice_and_keeps_both_notes(self):
        icon, note = m.flags([_line("amazing grace", self.A, self.B)], [self.A])[0]
        assert icon == "🔁"
        assert "already" in note and "several artists" in note

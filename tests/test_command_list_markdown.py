"""The command lists must survive Telegram's legacy Markdown parser.

WHY THIS EXISTS

v0.28.5 added a `/sharecontact` line to ADMIN_COMMANDS_TEXT containing the
bare word `chat_id`. `reply_markdown` sends with ParseMode.MARKDOWN — legacy
Markdown v1 — in which a lone underscore opens an italic entity that is never
closed. Telegram answered 400 Bad Request, "can't find end of the entity
starting at byte offset 2253", for EVERY admin who saw the command list: on
/help, /adminhelp, /start and the reply after sharing a contact. It shipped in
two tagged releases and was found in production on 2026-11-06, when an admin
ran /sharecontact and the journal recorded the rejection fourteen seconds
later.

Nothing was lost — `_markdown_or_plain` caught the BadRequest and resent as
plain text, which is exactly what that fallback is for. The cost was silent:
three admins got unformatted help text for a day and nobody could tell why.

These constants are prose that people edit, inside a format where one
character changes the meaning of the rest. The module docstring of
tests/test_no_secrets_committed.py makes the same argument about credentials:
the only guard that works is one that runs on every commit.

WHAT THIS CHECKS, AND WHAT IT DOES NOT

It checks a NECESSARY condition — that `*`, `_` and `[`/`]` are balanced
outside code spans — not that Telegram will accept the message. Proving that
needs Telegram, and probing the live bot is not acceptable (a `setX` call with
no parameters has wiped a field here before). So this cannot certify a block
as valid; it catches the specific, repeatable mistake of an unpaired marker,
which is the one that has actually bitten.

The validator is therefore pinned by a positive control AND by the real
regression: `test_it_catches_the_v0_28_5_regression` rebuilds the exact text
that shipped and asserts this test would have failed on it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.user_basics as ub
from localization import AVAILABLE_LANGUAGES, t

#: Markers that open and close an entity in Telegram's Markdown v1.
PAIRED_MARKERS = ("*", "_")


def _strip_code(text: str) -> str:
    """Remove pre blocks then inline code, where markers are literal."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return re.sub(r"`[^`]*`", "", text)


#: An underscore with word characters on BOTH sides, outside code spans: an
#: identifier like chat_id or enable_appt_proxies. Parity alone does not catch
#: these. `/enable_appt_proxies` has TWO underscores, so it balances and is
#: accepted — and then renders as "enable<i>appt</i>proxies", mangling a
#: command the reader has to type. Found 2026-11-06 by this very test failing
#: on text added in the same change, which is the only reason the rule exists.
INTRA_WORD_UNDERSCORE = re.compile(r"(?<=\w)_(?=\w)")


def unbalanced_markers(text: str) -> dict[str, int]:
    """Markers Telegram's Markdown v1 would misread. Empty dict means clean.

    Two distinct faults, not one:

      * an ODD count of a paired marker — Telegram rejects the whole message
        with 400 and the caller resends it unformatted;
      * an INTRA-WORD underscore — accepted, then silently rendered with part
        of an identifier italicised and the underscores eaten.

    The second is worse in one respect: nothing fails, so nothing is logged.
    """
    stripped = _strip_code(text)
    found = {}
    for marker in PAIRED_MARKERS:
        count = stripped.count(marker)
        if count % 2:
            found[marker] = count
    backticks = text.count("`") - len(re.findall(r"```", text)) * 3
    if backticks % 2:
        found["`"] = backticks
    if stripped.count("[") != stripped.count("]"):
        found["[]"] = stripped.count("[") - stripped.count("]")
    intra = INTRA_WORD_UNDERSCORE.findall(stripped)
    if intra:
        found["identifier_"] = len(intra)
    return found


def _describe(text: str, bad: dict[str, int]) -> str:
    lines = [f"unbalanced Markdown: {bad}"]
    for marker in bad:
        if marker in PAIRED_MARKERS:
            for i, line in enumerate(text.splitlines(), 1):
                if marker in _strip_code(line):
                    lines.append(f"  line {i}: {line.strip()[:90]}")
    lines.append(
        "A lone marker opens an entity Telegram never closes, so the WHOLE "
        "message is rejected and resent unformatted. Put identifiers like "
        "`chat_id` in backticks."
    )
    return "\n".join(lines)


#: Every role combination, since the blocks are concatenated and an odd
#: marker in one is cancelled or exposed by the next.
ROLE_COMBINATIONS = [
    (a, p, b, o)
    for a in (False, True)
    for p in (False, True)
    for b in (False, True)
    for o in (False, True)
]


class TestTheValidatorWorks:
    """Guards everything below: a validator that never fires proves nothing."""

    @pytest.mark.parametrize("bad,marker", [
        ("a lone _underscore here", "_"),
        ("a lone *asterisk here", "*"),
        ("unclosed `code", "`"),
        ("a [link with no close", "[]"),
    ])
    def test_it_flags_an_unpaired_marker(self, bad, marker):
        assert marker in unbalanced_markers(bad)

    @pytest.mark.parametrize("good", [
        "_italic_ and *bold* and `code`",
        "an identifier in backticks: `chat_id`",
        "```pre block with _underscores_ inside```",
        "a [link](https://example.test) is balanced",
        "plain text with no markers at all",
    ])
    def test_it_passes_balanced_text(self, good):
        assert unbalanced_markers(good) == {}

    @pytest.mark.parametrize("bad", [
        "an identifier chat_id in prose",
        "two of them: enable_appt_proxies",
        "snake_case words render wrong",
    ])
    def test_it_flags_an_intra_word_underscore(self, bad):
        """Even parity is not enough: enable_appt_proxies balances and still
        renders as enable<i>appt</i>proxies."""
        assert "identifier_" in unbalanced_markers(bad)

    def test_an_even_number_of_intra_word_underscores_is_still_a_fault(self):
        """The case that parity misses entirely, stated on its own."""
        text = "run /enable_appt_proxies now"
        assert text.count("_") % 2 == 0, "fixture no longer has even parity"
        assert unbalanced_markers(text) == {"identifier_": 2}

    def test_italic_prose_is_not_mistaken_for_an_identifier(self):
        """The prayer and board blocks end with a _whole sentence_ in italics;
        those underscores sit at word boundaries and must stay legal."""
        assert unbalanced_markers("_Answering a request deletes it._") == {}
        assert unbalanced_markers("_Notices go out daily at 6am._") == {}

    def test_an_underscore_inside_backticks_is_literal(self):
        """The fix relies on this, so it is asserted rather than assumed.

        Bare, `chat_id` trips BOTH rules — odd parity and the intra-word
        rule — which is why the one-character fix cures both.
        """
        assert unbalanced_markers("`chat_id`") == {}
        assert unbalanced_markers("chat_id") == {"_": 1, "identifier_": 1}

    def test_it_catches_the_v0_28_5_regression(self):
        """The exact text that shipped, rebuilt. This test must fail on it.

        Without this, the validator could be subtly wrong in a way that
        happens to pass the current constants — and the one bug it exists to
        prevent would slip through again.
        """
        source = ub.OFFICIAL_COMMANDS_TEXT
        shipped = source.replace("`chat_id`", "chat_id")
        assert shipped != source, (
            "could not rebuild the pre-fix text — the fix was reworded or the "
            "line moved again, so this regression check is no longer checking "
            "anything. The bare word was `chat_id`; find where it lives now."
        )
        bad = unbalanced_markers(shipped)
        assert bad.get("_") == 1, f"parity fault not detected: {bad}"
        assert bad.get("identifier_") == 1, (
            f"intra-word underscore not detected: {bad}"
        )


class TestTheCommandBlocks:
    @pytest.mark.parametrize("name", [
        "ADMIN_COMMANDS_TEXT", "PRAYER_COMMANDS_TEXT", "BOARD_COMMANDS_TEXT",
    ])
    def test_each_block_is_balanced_on_its_own(self, name):
        text = getattr(ub, name)
        assert text.strip(), f"{name} is empty — this test would be vacuous"
        bad = unbalanced_markers(text)
        assert not bad, f"{name}: {_describe(text, bad)}"

    @pytest.mark.parametrize("lang", sorted(AVAILABLE_LANGUAGES))
    def test_the_localised_user_list_is_balanced(self, lang):
        text = t("user_commands", lang)
        assert text.strip(), f"user_commands is empty for {lang}"
        bad = unbalanced_markers(text)
        assert not bad, f"user_commands[{lang}]: {_describe(text, bad)}"


class TestTheAssembledMessage:
    """What _commands_text actually produces — the real function.

    The blocks are concatenated, so balance per block is not enough: an odd
    marker in one block pairs with a marker in the next, which is precisely
    how the v0.28.5 bug presented. The rejection named byte 2253, the last
    byte of the admin+board text, because the stray `_` in the admin block
    had consumed the opening half of the board block's italic and left its
    closing half unterminated.
    """

    @pytest.mark.parametrize("lang", sorted(AVAILABLE_LANGUAGES))
    @pytest.mark.parametrize("is_adm,is_prayer,is_board,is_official", ROLE_COMBINATIONS)
    def test_every_role_combination_in_every_language(
            self, lang, is_adm, is_prayer, is_board, is_official):
        text = ub._commands_text(lang, is_adm, is_prayer, is_board, is_official)
        bad = unbalanced_markers(text)
        assert not bad, (
            f"lang={lang} admin={is_adm} prayer={is_prayer} "
            f"board={is_board} official={is_official}:\n{_describe(text, bad)}"
        )

    def test_the_sweep_actually_assembled_something(self):
        """A `_commands_text` that returned "" would pass every case above."""
        biggest = ub._commands_text("en", True, True, True, True)
        smallest = ub._commands_text("en", False, False, False, False)
        assert len(biggest) > len(smallest) > 100
        for marker, block in [("/adminhelp", "admin"), ("/prayerrequests", "prayer"),
                              ("/boarditems", "board"), ("/sharecontact", "officials")]:
            assert marker in biggest, (
                f"the {block} block is not reaching the assembled text, so the "
                "combination sweep is not covering it"
            )

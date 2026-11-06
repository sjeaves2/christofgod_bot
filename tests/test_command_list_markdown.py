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


def unbalanced_markers(text: str) -> dict[str, int]:
    """Markers left unpaired outside code spans. Empty dict means balanced."""
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
    (False, False, False),
    (True, False, False),
    (True, True, False),
    (True, False, True),
    (True, True, True),
    (False, True, False),      # prayer without admin: not reachable today,
    (False, False, True),      # board without admin: IS reachable today
    (False, True, True),
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

    def test_an_underscore_inside_backticks_is_literal(self):
        """The fix relies on this, so it is asserted rather than assumed."""
        assert unbalanced_markers("`chat_id`") == {}
        assert unbalanced_markers("chat_id") == {"_": 1}

    def test_it_catches_the_v0_28_5_regression(self):
        """The exact text that shipped, rebuilt. This test must fail on it.

        Without this, the validator could be subtly wrong in a way that
        happens to pass the current constants — and the one bug it exists to
        prevent would slip through again.
        """
        shipped = ub.ADMIN_COMMANDS_TEXT.replace("`chat_id`", "chat_id")
        assert shipped != ub.ADMIN_COMMANDS_TEXT, (
            "could not rebuild the pre-fix text — the fix was reworded, so "
            "this regression check is no longer checking anything"
        )
        assert unbalanced_markers(shipped) == {"_": 1}


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
    @pytest.mark.parametrize("is_adm,is_prayer,is_board", ROLE_COMBINATIONS)
    def test_every_role_combination_in_every_language(
            self, lang, is_adm, is_prayer, is_board):
        text = ub._commands_text(lang, is_adm, is_prayer, is_board)
        bad = unbalanced_markers(text)
        assert not bad, (
            f"lang={lang} admin={is_adm} prayer={is_prayer} "
            f"board={is_board}:\n{_describe(text, bad)}"
        )

    def test_the_sweep_actually_assembled_something(self):
        """A `_commands_text` that returned "" would pass every case above."""
        biggest = ub._commands_text("en", True, True, True)
        smallest = ub._commands_text("en", False, False, False)
        assert len(biggest) > len(smallest) > 100
        assert "/sharecontact" in biggest, (
            "the admin block is not reaching the assembled text, so the "
            "combination sweep is not covering it"
        )

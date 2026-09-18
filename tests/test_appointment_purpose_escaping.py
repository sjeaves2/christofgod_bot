"""An appointment's purpose is typed by a congregant and must be escaped.

Every other name in these messages — the official, the requester, the
negotiator — is passed through md() where it is assigned. The purpose was not,
at the three places it is rendered: the confirmation to the official, and both
sides of a reschedule.

This is the one path in the Markdown-escaping family that an ordinary member
can trigger. An admin whose event name breaks /events sees an error and can
edit it. A member who writes "discuss my son_s baptism" sees their request
accepted, and the message to the official simply never arrives — Telegram
rejects the whole message over the unpaired underscore, and nobody learns why.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from common import md  # noqa: E402

SOURCE = (Path(__file__).resolve().parents[1] / "handlers" / "appointments.py").read_text()


class TestPurposeIsEscapedAtEverySite:
    """Pinned against the source: a fourth render site must escape too."""

    def test_no_unescaped_purpose_render_remains(self):
        assert "{appt.get('description', '')}" not in SOURCE, (
            "an appointment purpose is rendered into Markdown unescaped. It is "
            "typed by a congregant, so an underscore or asterisk in it makes "
            "Telegram reject the whole message and the notification is lost."
        )

    def test_every_purpose_render_is_escaped(self):
        assert SOURCE.count("{md(appt.get('description', ''))}") == 3


class TestEscapingBehaviour:
    @pytest.mark.parametrize("raw,expected", [
        ("discuss my son_s baptism", "discuss my son\\_s baptism"),
        ("*urgent* prayer", "\\*urgent\\* prayer"),
        ("re: the `budget`", "re: the \\`budget\\`"),
        # Legacy Markdown treats "[" as a marker; "]" alone is ordinary text.
        ("a [private] matter", "a \\[private] matter"),
    ])
    def test_markdown_characters_a_member_may_type(self, raw, expected):
        assert md(raw) == expected

    def test_ordinary_text_is_untouched(self):
        assert md("Baptism for my daughter") == "Baptism for my daughter"

    def test_a_missing_purpose_renders_empty_not_none(self):
        """appt.get('description', '') may be absent on older records."""
        assert md(None) == ""
        assert md("") == ""

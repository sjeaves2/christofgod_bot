"""The join link in a reminder is a tappable button, not a raw URL.

Reminders posted to the group are the one channel the congregation
demonstrably receives — of twelve registered members, one has ever run a
command, while every reminder lands. So the sensible place to improve things is
the message they already get, not a new one they would have to seek out.

A Zoom join URL runs to ~90 characters including the passcode. In the body of a
message that is both ugly and fiddly to hit on a phone; as a button it is one
tap. The URL therefore leaves the text when a button carries it.

The hazard this introduces: Telegram rejects the ENTIRE message if an inline
button's url is not acceptable to it. That would turn a cosmetic improvement
into an undelivered reminder — the exact failure the Markdown work spent a week
removing. Hence two defences: only a well-formed http(s) url becomes a button,
and a rejected button falls back to the old text form rather than failing.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz
from telegram.error import BadRequest

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.notifications as N  # noqa: E402

TZ = pytz.timezone("America/New_York")
# A fixture, never a real link. A Zoom URL embeds its passcode, so a realistic
# one IS a credential — tests/test_no_secrets_committed.py exists because this
# repository has leaked one before, and it caught this file using the live
# Sabbath Eve link on 2026-10-13. Nothing here needs a real host: what these
# tests exercise is the scheme and the length, not the domain.
JOIN_URL = "https://example.test/j/1234567890?pwd=NOTAREALPASSCODE.1"


def _event(url=JOIN_URL, **extra):
    ev = {
        "key": "ev1",
        "name": "God's Holy Convocation—Sabbath Eve",
        "service_time": datetime.now(TZ) + timedelta(hours=2),
        "notification_time": datetime.now(TZ),
        "url": url,
        "announcements": [],
        "target_chat_ids": [-1001178984510],
    }
    ev.update(extra)
    return ev


class TestWhichLinksBecomeButtons:
    @pytest.mark.parametrize("url", [
        JOIN_URL,
        "http://example.test/j/1",
        "HTTPS://EXAMPLE.TEST/J/1",          # scheme case is not significant
    ])
    def test_a_well_formed_link_becomes_a_button(self, url):
        assert N._join_markup(_event(url=url), "en") is not None

    @pytest.mark.parametrize("url", [
        "", None, "   ",
        "example.test/j/1",                   # no scheme
        "ftp://example.test/j/1",             # wrong scheme
        "https://example.test/j/1 extra",     # whitespace
        "Call Pastor for the link",           # free text an admin might type
    ])
    def test_anything_else_does_not(self, url):
        assert N._join_markup(_event(url=url), "en") is None

    def test_the_button_carries_the_url_unaltered(self):
        btn = N._join_markup(_event(), "en").inline_keyboard[0][0]
        assert btn.url == JOIN_URL, "the passcode must survive intact"

    def test_the_button_label_is_localized(self):
        from localization import t
        for lang in ("en", "es", "fr", "zu"):
            label = N._join_markup(_event(), lang).inline_keyboard[0][0].text
            assert label == t("notif_join_button", lang)
            assert label != "notif_join_button", f"missing {lang} string"


class TestTheTextAdaptsToTheButton:
    def test_the_url_leaves_the_text_when_a_button_carries_it(self):
        msg = N._render_notification(_event(), TZ, "en", inline_url=False)
        assert "example.test" not in msg, "the long URL in the body is what we removed"
        assert "Sabbath Eve" in msg, "the rest of the reminder must survive"

    def test_the_url_stays_in_the_text_when_there_is_no_button(self):
        msg = N._render_notification(_event(), TZ, "en", inline_url=True)
        assert "example.test" in msg

    def test_an_unbuttonable_link_is_still_shown(self):
        """A link the bot cannot make a button of must not simply vanish."""
        ev = _event(url="Call Pastor for the link")
        assert N._join_markup(ev, "en") is None
        assert "Call Pastor" in N._render_notification(ev, TZ, "en", inline_url=True)

    def test_a_link_with_an_underscore_is_still_escaped_in_the_text(self):
        """The 2026-09-17 defect: an unescaped '_' kills the whole message."""
        ev = _event(url="https://example.test/j/1?pwd=aB_cD")
        msg = N._render_notification(ev, TZ, "en", inline_url=True)
        assert "aB\\_cD" in msg


class TestDeliveryUsesTheButton:
    @staticmethod
    async def _deliver(event, button_rejected=False):
        sends = []

        async def send_message(chat_id, text, **kwargs):
            if button_rejected and kwargs.get("reply_markup") is not None:
                raise BadRequest("BUTTON_URL_INVALID")
            sends.append((text, kwargs.get("reply_markup")))

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=send_message)

        async def _load():
            return {}

        async def _save(_s):
            pass

        with patch("storage._load_notif_state", side_effect=_load), \
             patch("storage._save_notif_state", side_effect=_save):
            await N.deliver_event_notifications(bot, event)
        return sends

    async def test_the_reminder_goes_out_with_a_button(self):
        sends = await self._deliver(_event())
        assert len(sends) == 1
        text, markup = sends[0]
        assert markup is not None, "the join link should be a button"
        assert "example.test" not in text

    async def test_a_rejected_button_still_delivers_the_reminder(self):
        """Never let a link cost the whole message — that is the whole lesson
        of the Markdown work, applied to buttons."""
        sends = await self._deliver(_event(), button_rejected=True)
        assert sends, "the reminder must still arrive"
        text, markup = sends[-1]
        assert markup is None, "the retry drops the button"
        assert "example.test" in text, "and puts the link back in the text"

    async def test_an_event_with_no_link_sends_no_button(self):
        sends = await self._deliver(_event(url=""))
        assert len(sends) == 1 and sends[0][1] is None

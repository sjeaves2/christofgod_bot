"""Regression tests for Markdown-escaping of runtime text.

Telegram rejects an entire message whose Markdown entities don't balance
("Can't parse entities"), so any unescaped '_', '*', '`' or '[' in a display
name, appointment purpose, or event/announcement title silently prevents
delivery. This bit /userlist on 2026-07-12; these tests cover the other
surfaces that interpolate user- or admin-authored text.

Each test feeds text containing Markdown metacharacters and asserts the
rendered message escapes them.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.user_basics as hub

TZ = pytz.timezone("America/New_York")
NASTY = "John_Doe *VIP* [x] `code`"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _balanced(text: str) -> bool:
    """Every Markdown metacharacter in the message is either escaped or paired."""
    for ch in ("_", "*", "`"):
        unescaped = 0
        i = 0
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == ch:
                unescaped += 1
            i += 1
        if unescaped % 2:
            return False
    return True


class TestMdHelper:
    def test_escapes_all_metacharacters(self):
        from common import md
        out = md(NASTY)
        assert "\\_" in out and "\\*" in out and "\\[" in out and "\\`" in out

    def test_handles_none_and_non_strings(self):
        from common import md
        assert md(None) == ""
        assert md(42) == "42"


class TestAppointmentRequestToOfficial:
    """_notify_official_of_request embeds the requester's name and purpose."""

    def _notify(self, display_name, description):
        import handlers.appointments as ha
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.get_user_profile_photos = AsyncMock(
            return_value=MagicMock(photos=[]))
        ctx.bot.send_photo = AsyncMock()
        appt = {
            "id": "ABC123", "user_chat_id": 111, "user_display_name": display_name,
            "user_username": "jd_2026", "official_id": "off1",
            "official_name": "Pastor_Test", "description": description,
            "requested_datetime": (datetime.now(TZ) + timedelta(days=3)).isoformat(),
            "duration_minutes": 30, "status": "pending",
        }
        with patch("permissions.OFFICIALS",
                   [{"id": "off1", "name": "Pastor_Test", "chat_id": 999}]):
            _run(ha._notify_official_of_request(ctx, appt, MagicMock()))
        return ctx.bot.send_message.await_args[0][1]

    def test_display_name_escaped(self):
        msg = self._notify(NASTY, "prayer")
        assert "John\\_Doe" in msg
        assert _balanced(msg)

    def test_purpose_escaped(self):
        msg = self._notify("Jane", "discuss *baptism* and _membership_")
        assert "\\*baptism\\*" in msg
        assert _balanced(msg)

    def test_single_underscore_name_does_not_unbalance(self):
        # The exact shape of the /userlist failure: one stray underscore.
        msg = self._notify("John_Doe", "prayer")
        assert _balanced(msg), "unbalanced Markdown would be rejected by Telegram"

    def test_username_escaped(self):
        msg = self._notify("Jane", "prayer")
        assert "@jd\\_2026" in msg


class TestEventNotificationRendering:
    def _render(self, name, description="", announcements=()):
        import handlers.notifications as hn
        event = {
            "name": name, "description": description,
            "service_time": datetime.now(TZ) + timedelta(days=1),
            "announcements": list(announcements),
        }
        return hn._render_notification(event, TZ, "en")

    def test_event_name_escaped(self):
        msg = self._render("Youth_Night *Special*")
        assert "Youth\\_Night" in msg and "\\*Special\\*" in msg
        assert _balanced(msg)

    def test_description_escaped(self):
        msg = self._render("Service", description="bring a friend_or_two")
        assert "friend\\_or\\_two" in msg
        assert _balanced(msg)

    def test_announcements_escaped(self):
        msg = self._render("Service", announcements=["Roof *fund* update_1"])
        assert "\\*fund\\*" in msg
        assert _balanced(msg)


class TestEventsAdminEcho:
    """Admin-typed event names are echoed back in confirmation messages."""

    def _delete_confirm_prompt(self, name):
        import handlers.events_admin as ea
        ctx = MagicMock(); ctx.user_data = {}
        ctx.user_data["de_events"] = [{
            "key": "special_x", "name": name, "type": "special",
            "service_time": datetime.now(TZ) + timedelta(days=2)}]
        upd = MagicMock()
        upd.effective_user.id = 1
        upd.effective_user.username = "admin"
        upd.effective_user.full_name = "Admin"
        upd.message.text = "1"
        upd.message.reply_text = AsyncMock()
        _run(ea.de_select(upd, ctx))
        return upd.message.reply_text.call_args[0][0]

    def test_event_name_escaped_in_delete_prompt(self):
        msg = self._delete_confirm_prompt("Youth_Night")
        assert "Youth\\_Night" in msg
        assert _balanced(msg)

    def test_event_name_escaped_in_annotation_prompt(self):
        import handlers.events_admin as ea
        ctx = MagicMock(); ctx.user_data = {}
        ctx.user_data["de_events"] = [{
            "key": "sab_x", "name": "Sabbath_Eve *Holy*", "type": "convocation",
            "service_time": datetime.now(TZ) + timedelta(days=2)}]
        upd = MagicMock()
        upd.effective_user.id = 1
        upd.effective_user.username = "admin"
        upd.effective_user.full_name = "Admin"
        upd.message.text = "1"
        upd.message.reply_text = AsyncMock()
        _run(ea.de_select(upd, ctx))
        msg = upd.message.reply_text.call_args[0][0]
        assert "Sabbath\\_Eve" in msg and "\\*Holy\\*" in msg
        assert _balanced(msg)


class TestAnnouncementRendering:
    def test_title_and_body_escaped(self):
        import handlers.announcements as ha
        out = ha._render_announcement({"title": "Roof_Fund *update*",
                                       "body": "we raised $5_000 [total]"})
        assert "Roof\\_Fund" in out and "\\*update\\*" in out
        assert "5\\_000" in out and "\\[total" in out
        assert _balanced(out)


class TestUserListEscaping:
    def test_userlist_still_escapes(self):
        """The original incident — guarded here alongside the others."""
        upd = MagicMock()
        upd.effective_user.id = 1
        upd.effective_user.username = "admin"
        upd.effective_user.full_name = "Admin"
        upd.message.reply_text = AsyncMock()

        async def _users():
            return [{"chat_id": 1, "display_name": NASTY, "username": "jd_2026"}]

        with patch("storage.get_all_users", side_effect=_users), \
             patch("permissions.is_admin", return_value=True):
            _run(hub.cmd_userlist(upd, MagicMock()))
        msg = upd.message.reply_text.call_args[0][0]
        assert "John\\_Doe" in msg
        assert _balanced(msg)

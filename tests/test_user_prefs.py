"""Tests for per-user timezone/language preferences and the /settimezone,
/language commands."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

import common
import handlers.notifications as hn
import handlers.user_basics as hub
import localization
import settings
import telegram.ext as ext

TZ = pytz.timezone("America/New_York")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_update(text: str = "", chat_id: int = 111) -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = "user"
    upd.effective_user.first_name = "User"
    upd.effective_user.full_name = "Test User"
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    return upd


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    return ctx


# ---------------------------------------------------------------------------
# Preference helpers
# ---------------------------------------------------------------------------

class TestPrefHelpers:
    def test_user_tz_of_default(self):
        assert common.user_tz_of(None) == settings.TZ
        assert common.user_tz_of({}) == settings.TZ

    def test_user_tz_of_valid(self):
        tz = common.user_tz_of({"timezone": "America/Los_Angeles"})
        assert str(tz) == "America/Los_Angeles"

    def test_user_tz_of_invalid_falls_back(self):
        assert common.user_tz_of({"timezone": "Not/AZone"}) == settings.TZ

    def test_user_lang_of_default(self):
        assert common.user_lang_of(None) == localization.DEFAULT_LANG
        assert common.user_lang_of({"language": "zz"}) == localization.DEFAULT_LANG

    def test_user_lang_of_known(self):
        assert common.user_lang_of({"language": "en"}) == "en"

    def test_get_user_prefs(self):
        users = [{"chat_id": 111, "timezone": "America/Chicago", "language": "en"}]

        async def _fake_get():
            return users

        with patch("storage.get_all_users", side_effect=_fake_get):
            tz, lang = _run(common.get_user_prefs(111))
        assert str(tz) == "America/Chicago"
        assert lang == "en"

    def test_get_user_prefs_unknown_user_defaults(self):

        async def _fake_get():
            return []

        with patch("storage.get_all_users", side_effect=_fake_get):
            tz, lang = _run(common.get_user_prefs(999))
        assert tz == settings.TZ
        assert lang == localization.DEFAULT_LANG


class TestFormatDt:
    def test_default_tz(self):
        dt = pytz.utc.localize(datetime(2030, 7, 1, 14, 0))  # 10:00 EDT
        assert "10:00 AM" in common.format_dt(dt)

    def test_explicit_tz_changes_output(self):
        dt = pytz.utc.localize(datetime(2030, 7, 1, 14, 0))  # 07:00 PDT
        out = common.format_dt(dt, pytz.timezone("America/Los_Angeles"))
        assert "7:00 AM" in out


# ---------------------------------------------------------------------------
# /settimezone
# ---------------------------------------------------------------------------

class TestSetTimezone:
    def _run_select(self, text, users=None):
        ctx = _make_context()
        upd = _make_update(text=text)
        saved = {}

        async def _fake_get():
            return users if users is not None else [{"chat_id": 111}]

        async def _fake_save(u):
            saved["users"] = u

        with patch("storage.get_all_users", side_effect=_fake_get), \
             patch("storage.save_users", side_effect=_fake_save):
            result = _run(hub.tz_typed(upd, ctx))
        return result, upd, saved

    def test_type_number_from_menu(self):
        result, upd, saved = self._run_select("2")  # America/Chicago
        assert result == ext.ConversationHandler.END
        assert saved["users"][0]["timezone"] == "America/Chicago"

    def test_type_custom_iana(self):
        result, upd, saved = self._run_select("Europe/Paris")
        assert result == ext.ConversationHandler.END
        assert saved["users"][0]["timezone"] == "Europe/Paris"

    def test_invalid_zone_stays(self):
        result, upd, saved = self._run_select("Mars/Phobos")
        assert result == hub.TZ_SELECT
        assert "users" not in saved
        assert "recognised" in upd.message.reply_text.call_args[0][0].lower()

    def test_confirmation_shows_zone(self):
        result, upd, saved = self._run_select("America/Los_Angeles")
        assert "America/Los_Angeles" in upd.message.reply_text.call_args[0][0]


class TestSetTimezoneButtons:
    def _run_button(self, data, users=None):
        ctx = _make_context()
        upd = MagicMock()
        upd.effective_user.id = 111
        upd.effective_user.username = "u"
        upd.effective_user.full_name = "U"
        q = MagicMock()
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        q.data = data
        upd.callback_query = q
        saved = {}

        async def _fake_get():
            return users if users is not None else [{"chat_id": 111}]

        async def _fake_save(u):
            saved["users"] = u

        with patch("storage.get_all_users", side_effect=_fake_get), \
             patch("storage.save_users", side_effect=_fake_save):
            result = _run(hub.tz_button(upd, ctx))
        return result, q, saved

    def test_first_button_sets_new_york(self):
        result, q, saved = self._run_button("tz:0")
        assert result == ext.ConversationHandler.END
        assert saved["users"][0]["timezone"] == "America/New_York"

    def test_button_confirmation_shows_zone(self):
        _, q, _ = self._run_button("tz:3")  # America/Los_Angeles
        assert "America/Los_Angeles" in q.edit_message_text.call_args[0][0]

    def test_out_of_range_button_ends(self):
        result, q, saved = self._run_button("tz:99")
        assert result == ext.ConversationHandler.END
        assert "users" not in saved

    def test_cmd_settimezone_shows_buttons(self):
        ctx = _make_context()
        upd = _make_update()

        async def _fake_get():
            return [{"chat_id": 111}]

        with patch("storage.get_all_users", side_effect=_fake_get):
            result = _run(hub.cmd_settimezone(upd, ctx))
        assert result == hub.TZ_SELECT
        markup = upd.message.reply_text.call_args.kwargs["reply_markup"]
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "tz:0" in cbs and len(cbs) == len(hub.COMMON_TIMEZONES)


# ---------------------------------------------------------------------------
# /language
# ---------------------------------------------------------------------------

class TestLanguage:
    def _run_select(self, data):
        ctx = _make_context()
        upd = MagicMock()
        upd.effective_user.id = 111
        upd.effective_user.username = "u"
        upd.effective_user.full_name = "U"
        q = MagicMock()
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        q.data = data
        upd.callback_query = q
        saved = {}

        async def _fake_get():
            return [{"chat_id": 111}]

        async def _fake_save(u):
            saved["users"] = u

        with patch("storage.get_all_users", side_effect=_fake_get), \
             patch("storage.save_users", side_effect=_fake_save):
            result = _run(hub.lang_select(upd, ctx))
        return result, q, saved

    def test_select_english(self):
        result, q, saved = self._run_select("lang:en")
        assert result == ext.ConversationHandler.END
        assert saved["users"][0]["language"] == "en"

    def test_unknown_code_ends_without_saving(self):
        result, q, saved = self._run_select("lang:xx")
        assert result == ext.ConversationHandler.END
        assert "users" not in saved


# ---------------------------------------------------------------------------
# Per-user timezone applied to displayed events
# ---------------------------------------------------------------------------

class TestEventsRespectUserTz:
    def _run_events(self, user_record):
        ctx = _make_context()
        upd = _make_update(chat_id=111)

        event = {
            "name": "Test Service",
            "service_time": pytz.utc.localize(datetime(2030, 7, 1, 14, 0)),  # 10:00 EDT
            "announcements": [],
        }

        async def _fake_users():
            return [user_record]

        async def _fake_upcoming(days_ahead=30):
            return [event]

        with patch("storage.get_all_users", side_effect=_fake_users), \
             patch("handlers.user_basics.all_upcoming", side_effect=_fake_upcoming), \
             patch("permissions.is_admin", return_value=False):
            _run(hub.cmd_events(upd, ctx))
        return upd.message.reply_text.call_args[0][0]

    def test_eastern_user_sees_eastern_time(self):
        msg = self._run_events({"chat_id": 111, "timezone": "America/New_York"})
        assert "10:00 AM" in msg

    def test_pacific_user_sees_pacific_time(self):
        msg = self._run_events({"chat_id": 111, "timezone": "America/Los_Angeles"})
        assert "7:00 AM" in msg


# ---------------------------------------------------------------------------
# Notifications render per-recipient timezone
# ---------------------------------------------------------------------------

class TestNotificationPerUserTz:
    def test_render_uses_given_tz(self):
        event = {
            "name": "Test Service",
            "service_time": pytz.utc.localize(datetime(2030, 7, 1, 14, 0)),
            "announcements": [],
        }
        eastern = hn._render_notification(event, pytz.timezone("America/New_York"), "en")
        pacific = hn._render_notification(event, pytz.timezone("America/Los_Angeles"), "en")
        assert "10:00 AM" in eastern
        assert "7:00 AM" in pacific

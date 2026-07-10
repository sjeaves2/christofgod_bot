"""Tests for opt-in personal reminders (/notifications) and /help subcommands."""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

TZ = pytz.timezone("America/New_York")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    ctx.args = []
    return ctx


def _msg_update(chat_id: int = 111) -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = "friend"
    upd.effective_user.first_name = "Friend"
    upd.effective_user.full_name = "Church Friend"
    upd.message.reply_text = AsyncMock()
    return upd


def _cb_update(data, chat_id: int = 111) -> tuple[MagicMock, MagicMock]:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = "friend"
    upd.effective_user.full_name = "Church Friend"
    q = MagicMock()
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.data = data
    upd.callback_query = q
    return upd, q


# ---------------------------------------------------------------------------
# Category classification
# ---------------------------------------------------------------------------

class TestEventCategory:
    def test_convocation(self):
        import bot
        assert bot._event_category({"type": "convocation"}) == "convocations"

    def test_sunday_prayer(self):
        import bot
        assert bot._event_category(
            {"type": "special", "special_id": "sunday_morning_prayer"}) == "sunday_prayer"

    def test_other_special(self):
        import bot
        assert bot._event_category({"type": "special"}) == "special"


class TestUserNotifPrefs:
    def test_none_record(self):
        import bot
        assert bot.user_notif_prefs(None) == set()

    def test_filters_unknown_categories(self):
        import bot
        rec = {"notif_prefs": ["convocations", "bogus", "special"]}
        assert bot.user_notif_prefs(rec) == {"convocations", "special"}

    def test_empty_when_absent(self):
        import bot
        assert bot.user_notif_prefs({"chat_id": 1}) == set()


# ---------------------------------------------------------------------------
# Recipient assembly
# ---------------------------------------------------------------------------

class TestNotificationRecipients:
    def _recipients(self, event, users):
        import bot

        async def _fake_users():
            return users

        with patch("bot.get_all_users", side_effect=_fake_users):
            return _run(bot._notification_recipients(event))

    def test_groups_only_when_nobody_opted_in(self):
        import bot
        rec = self._recipients(
            {"type": "convocation", "target_chat_ids": [-100]}, [])
        assert rec == {-100: (bot.TZ, bot.DEFAULT_LANG)}

    def test_opted_in_user_added_with_own_prefs(self):
        rec = self._recipients(
            {"type": "convocation", "target_chat_ids": [-100]},
            [{"chat_id": 111, "timezone": "America/Los_Angeles",
              "language": "es", "notif_prefs": ["convocations"]}],
        )
        assert -100 in rec
        assert 111 in rec
        tz, lang = rec[111]
        assert str(tz) == "America/Los_Angeles"
        assert lang == "es"

    def test_user_not_added_for_uncategorised_event(self):
        rec = self._recipients(
            {"type": "special", "target_chat_ids": []},
            [{"chat_id": 111, "notif_prefs": ["convocations"]}],
        )
        assert 111 not in rec

    def test_empty_when_no_groups_and_no_optins(self):
        rec = self._recipients(
            {"type": "convocation", "target_chat_ids": []},
            [{"chat_id": 111, "notif_prefs": ["special"]}],
        )
        assert rec == {}


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

class TestSetGetPrefs:
    def test_creates_record_when_missing(self):
        import bot
        users = []
        saved = {}

        async def _get():
            return users

        async def _save(u):
            saved["users"] = u

        with patch("bot.get_all_users", side_effect=_get), \
             patch("bot.save_users", side_effect=_save):
            _run(bot._set_user_notif_prefs(111, "friend", "Church Friend",
                                           {"convocations"}))
        rec = saved["users"][0]
        assert rec["chat_id"] == 111
        assert rec["notif_prefs"] == ["convocations"]

    def test_updates_existing_record(self):
        import bot
        users = [{"chat_id": 111, "language": "en"}]
        saved = {}

        async def _get():
            return users

        async def _save(u):
            saved["users"] = u

        with patch("bot.get_all_users", side_effect=_get), \
             patch("bot.save_users", side_effect=_save):
            _run(bot._set_user_notif_prefs(111, "friend", "Church Friend",
                                           {"special", "convocations"}))
        assert saved["users"][0]["notif_prefs"] == ["convocations", "special"]

    def test_get_reads_prefs(self):
        import bot
        users = [{"chat_id": 111, "notif_prefs": ["sunday_prayer"]}]

        async def _get():
            return users

        with patch("bot.get_all_users", side_effect=_get):
            prefs = _run(bot._get_user_notif_prefs(111))
        assert prefs == {"sunday_prayer"}


# ---------------------------------------------------------------------------
# /notifications command + toggle callback
# ---------------------------------------------------------------------------

class TestCmdNotifications:
    def test_shows_keyboard(self):
        import bot
        ctx = _make_context()
        upd = _msg_update()

        async def _get():
            return [{"chat_id": 111}]

        with patch("bot.get_all_users", side_effect=_get):
            _run(bot.cmd_notifications(upd, ctx))
        markup = upd.message.reply_text.call_args.kwargs["reply_markup"]
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "np:toggle:convocations" in cbs
        assert "np:done" in cbs


class TestNotifCallback:
    def _run_cb(self, data, users):
        import bot
        ctx = _make_context()
        upd, q = _cb_update(data)
        saved = {}

        async def _get():
            return [dict(u) for u in users]

        async def _save(u):
            saved["users"] = u

        with patch("bot.get_all_users", side_effect=_get), \
             patch("bot.save_users", side_effect=_save):
            _run(bot.notif_prefs_callback(upd, ctx))
        return q, saved

    def test_toggle_on(self):
        q, saved = self._run_cb("np:toggle:convocations", [{"chat_id": 111}])
        assert saved["users"][0]["notif_prefs"] == ["convocations"]
        q.edit_message_reply_markup.assert_awaited()

    def test_toggle_off(self):
        q, saved = self._run_cb(
            "np:toggle:convocations",
            [{"chat_id": 111, "notif_prefs": ["convocations"]}])
        assert saved["users"][0]["notif_prefs"] == []

    def test_toggle_unknown_category_ignored(self):
        q, saved = self._run_cb("np:toggle:bogus", [{"chat_id": 111}])
        assert "users" not in saved

    def test_done_shows_summary(self):
        q, saved = self._run_cb(
            "np:done", [{"chat_id": 111, "notif_prefs": ["special"]}])
        q.edit_message_text.assert_awaited()
        assert "users" not in saved  # done doesn't save


# ---------------------------------------------------------------------------
# Delivery reaches opted-in users
# ---------------------------------------------------------------------------

class TestDeliveryToOptedIn:
    def test_opted_in_user_receives_dm(self):
        import bot
        service_time = bot.now_tz() + timedelta(hours=2)
        event = {
            "key": "conv_2099", "name": "Sabbath Eve",
            "type": "convocation", "target_chat_ids": [],
            "service_time": service_time,
            "announcements": [],
        }
        users = [{"chat_id": 111, "notif_prefs": ["convocations"],
                  "timezone": "America/New_York", "language": "en"}]

        async def _get_users():
            return users

        async def _load_state():
            return {}

        async def _save_state(s):
            pass

        sent = []

        async def _send_payload(bot_, chat_id, media, text, caches):
            sent.append(chat_id)
            return True

        with patch("bot.get_all_users", side_effect=_get_users), \
             patch("bot._load_notif_state", side_effect=_load_state), \
             patch("bot._save_notif_state", side_effect=_save_state), \
             patch("bot._send_notification_payload", side_effect=_send_payload):
            count = _run(bot.deliver_event_notifications(MagicMock(), event))
        assert count == 1
        assert sent == [111]


# ---------------------------------------------------------------------------
# /help subcommands
# ---------------------------------------------------------------------------

class TestHelpTopics:
    def _run_help(self, args):
        import bot
        ctx = _make_context()
        ctx.args = args
        upd = _msg_update()

        async def _get():
            return [{"chat_id": 111}]

        with patch("bot.get_all_users", side_effect=_get), \
             patch("bot.is_admin", return_value=False):
            _run(bot.cmd_help(upd, ctx))
        return upd.message.reply_text.call_args[0][0]

    def test_no_arg_lists_commands_with_hint(self):
        msg = self._run_help([])
        assert "/appointment" in msg
        assert "/help" in msg.lower()

    def test_known_topic(self):
        import bot
        msg = self._run_help(["appointment"])
        assert msg == bot.t("help_appointment", "en")

    def test_topic_with_leading_slash(self):
        import bot
        msg = self._run_help(["/notifications"])
        assert msg == bot.t("help_notifications", "en")

    def test_unknown_topic_lists_available(self):
        msg = self._run_help(["frobnicate"])
        assert "appointment" in msg

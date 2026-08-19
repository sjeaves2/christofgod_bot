"""Tests for the handlers that remain in bot.py itself:

  handle_contact  — identifies phone-only admins/officials from a shared contact
  error_handler   — logs unhandled exceptions to the activity log
  reschedule_job  — the weekly notification-rescheduling job
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    return ctx


def _contact_update(user_id=111, contact_user_id=None, phone="+1 (555) 000-1234",
                    username="friend") -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = user_id
    upd.effective_user.id = user_id
    upd.effective_user.username = username
    upd.effective_user.full_name = "Church Friend"
    upd.message.contact.user_id = user_id if contact_user_id is None else contact_user_id
    upd.message.contact.phone_number = phone
    upd.message.reply_text = AsyncMock()
    return upd


def _run_contact(upd, users=None, is_adm=False, known_official=False):
    import bot
    ctx = _ctx()
    saved = {}
    user_list = users if users is not None else [{"chat_id": 111}]

    async def _get_users():
        return user_list

    async def _save_users(u):
        saved["users"] = u

    async def _reg_phone(uid, phone):
        saved["registered_phone"] = (uid, phone)

    async def _reg_official(uid, uname, phone):
        saved["registered_official"] = (uid, uname, phone)

    with patch("storage.get_all_users", side_effect=_get_users), \
         patch("storage.save_users", side_effect=_save_users), \
         patch("permissions._register_admin_by_phone", side_effect=_reg_phone), \
         patch("bot._register_official_if_known", side_effect=_reg_official), \
         patch("permissions.is_admin", return_value=is_adm), \
         patch("permissions._is_known_official", return_value=known_official):
        _run(bot.handle_contact(upd, ctx))
    return saved, upd


class TestHandleContact:
    def test_rejects_someone_elses_contact(self):
        saved, upd = _run_contact(_contact_update(user_id=111, contact_user_id=222))
        msg = upd.message.reply_text.call_args[0][0]
        assert "your own contact" in msg.lower()
        assert "registered_phone" not in saved   # no identification attempted
        assert "users" not in saved              # nothing persisted

    def test_registers_phone_and_official(self):
        saved, _ = _run_contact(_contact_update())
        assert saved["registered_phone"][0] == 111
        assert saved["registered_official"][0] == 111

    def test_stores_normalised_phone_on_user_record(self):
        saved, _ = _run_contact(_contact_update(phone="+1 (555) 000-1234"))
        assert saved["users"][0]["phone"] == "15550001234"

    def test_does_not_overwrite_existing_phone(self):
        users = [{"chat_id": 111, "phone": "9999999999"}]
        saved, _ = _run_contact(_contact_update(), users=users)
        assert "users" not in saved                  # no save triggered
        assert users[0]["phone"] == "9999999999"     # left as-is

    def test_unknown_user_record_is_not_created(self):
        saved, _ = _run_contact(_contact_update(user_id=999), users=[{"chat_id": 111}])
        assert "users" not in saved

    def test_admin_gets_admin_acknowledgement(self):
        saved, upd = _run_contact(_contact_update(), is_adm=True)
        reply = upd.message.reply_text.call_args_list[0][0][0]
        assert "administrator" in reply.lower()

    def test_official_gets_official_acknowledgement(self):
        saved, upd = _run_contact(_contact_update(), known_official=True)
        reply = upd.message.reply_text.call_args_list[0][0][0]
        assert "official" in reply.lower()

    def test_plain_user_gets_generic_thanks(self):
        saved, upd = _run_contact(_contact_update())
        reply = upd.message.reply_text.call_args_list[0][0][0]
        assert "thank you" in reply.lower()

    def test_admin_sees_admin_commands_listed(self):
        saved, upd = _run_contact(_contact_update(), is_adm=True)
        cmd_text = upd.message.reply_text.call_args_list[1][0][0]
        assert "/broadcast" in cmd_text          # admin-only command present

    def test_regular_user_does_not_see_admin_commands(self):
        saved, upd = _run_contact(_contact_update())
        cmd_text = upd.message.reply_text.call_args_list[1][0][0]
        assert "/broadcast" not in cmd_text
        assert "/events" in cmd_text


class TestErrorHandler:
    def test_logs_error_to_activity_log(self):
        import bot
        ctx = _ctx()
        ctx.error = RuntimeError("boom")
        with patch.object(bot.activity, "log_error") as log_error:
            _run(bot.error_handler(MagicMock(), ctx))
        log_error.assert_called_once()
        assert "boom" in log_error.call_args[0][0]

    def test_survives_non_exception_error_value(self):
        import bot
        ctx = _ctx()
        ctx.error = "plain string failure"
        with patch.object(bot.activity, "log_error") as log_error:
            _run(bot.error_handler(MagicMock(), ctx))
        assert "plain string failure" in log_error.call_args[0][0]


class TestRescheduleJob:
    def test_reschedules_all_upcoming(self):
        import bot
        ctx = _ctx()
        ctx.application = MagicMock()
        sched = AsyncMock()
        with patch("bot.schedule_all_upcoming", sched):
            _run(bot.reschedule_job(ctx))
        sched.assert_awaited_once_with(ctx.application)


# ---------------------------------------------------------------------------
# Official username drift alerts
# ---------------------------------------------------------------------------

def _officials_with(chat_id=999, uname="pastorold", proxy_uname="janesec"):
    return [{
        "id": "off1", "name": "Pastor Test", "chat_id": chat_id,
        "telegram_username": uname, "proxies_enabled": True,
        "proxies": [{"name": "Jane Sec", "chat_id": 888,
                     "telegram_username": proxy_uname}],
    }]


class TestOfficialUsernameDriftDetection:
    def test_detects_changed_official_username(self):
        import permissions
        with patch.object(permissions, "OFFICIALS", _officials_with()):
            drift = permissions.official_username_drift(999, "pastornew")
        assert drift["kind"] == "official"
        assert drift["configured"] == "pastorold"
        assert drift["current"] == "pastornew"

    def test_no_drift_when_username_matches(self):
        import permissions
        with patch.object(permissions, "OFFICIALS", _officials_with()):
            assert permissions.official_username_drift(999, "PastorOld") is None

    def test_detects_removed_username(self):
        import permissions
        with patch.object(permissions, "OFFICIALS", _officials_with()):
            drift = permissions.official_username_drift(999, None)
        assert drift is not None and drift["current"] is None

    def test_detects_proxy_drift(self):
        import permissions
        with patch.object(permissions, "OFFICIALS", _officials_with()):
            drift = permissions.official_username_drift(888, "janenew")
        assert drift["kind"] == "proxy" and drift["name"] == "Jane Sec"

    def test_ignores_unknown_chat_id(self):
        import permissions
        with patch.object(permissions, "OFFICIALS", _officials_with()):
            assert permissions.official_username_drift(555, "stranger") is None

    def test_ignores_official_without_configured_username(self):
        import permissions
        offs = _officials_with()
        offs[0]["telegram_username"] = ""
        with patch.object(permissions, "OFFICIALS", offs):
            assert permissions.official_username_drift(999, "anything") is None


class TestOfficialUsernameDriftAlert:
    def _run_alert(self, uid=999, uname="pastornew", admin_users=None, officials=None):
        import bot
        bot._reported_username_drift.clear()
        ctx = _ctx()
        ctx.bot.send_message = AsyncMock()
        users = admin_users if admin_users is not None else [
            {"chat_id": 1, "username": "alice"},      # admin
            {"chat_id": 2, "username": "bob"},        # not an admin
        ]

        async def _get_users():
            return users

        with patch("storage.get_all_users", side_effect=_get_users), \
             patch.object(__import__("permissions"), "OFFICIALS",
                          officials if officials is not None else _officials_with()), \
             patch.object(__import__("permissions"), "ADMIN_USERNAMES", {"alice"}), \
             patch.object(__import__("permissions"), "_admin_chat_ids", set()):
            _run(bot._warn_official_username_drift(ctx, uid, uname))
        return ctx

    def test_alerts_admins(self):
        ctx = self._run_alert()
        sent_to = [c.args[0] for c in ctx.bot.send_message.await_args_list]
        assert sent_to == [1]                      # only the admin
        msg = ctx.bot.send_message.await_args[0][1]
        assert "Pastor Test" in msg and "pastorold" in msg and "pastornew" in msg

    def test_no_alert_when_no_drift(self):
        ctx = self._run_alert(uname="pastorold")
        ctx.bot.send_message.assert_not_awaited()

    def test_alert_is_sent_once_per_change(self):
        import bot
        ctx = self._run_alert()
        assert ctx.bot.send_message.await_count == 1
        # A second command from the same person must not re-alert.
        ctx2 = _ctx()
        ctx2.bot.send_message = AsyncMock()

        async def _get_users():
            return [{"chat_id": 1, "username": "alice"}]

        with patch("storage.get_all_users", side_effect=_get_users), \
             patch.object(__import__("permissions"), "OFFICIALS", _officials_with()), \
             patch.object(__import__("permissions"), "ADMIN_USERNAMES", {"alice"}), \
             patch.object(__import__("permissions"), "_admin_chat_ids", set()):
            _run(bot._warn_official_username_drift(ctx2, 999, "pastornew"))
        ctx2.bot.send_message.assert_not_awaited()

    def test_alert_repeats_for_a_different_new_username(self):
        import bot
        self._run_alert(uname="pastornew")
        ctx2 = _ctx()
        ctx2.bot.send_message = AsyncMock()

        async def _get_users():
            return [{"chat_id": 1, "username": "alice"}]

        with patch("storage.get_all_users", side_effect=_get_users), \
             patch.object(__import__("permissions"), "OFFICIALS", _officials_with()), \
             patch.object(__import__("permissions"), "ADMIN_USERNAMES", {"alice"}), \
             patch.object(__import__("permissions"), "_admin_chat_ids", set()):
            _run(bot._warn_official_username_drift(ctx2, 999, "pastorthird"))
        ctx2.bot.send_message.assert_awaited_once()

    def test_does_not_alert_the_person_who_changed_it(self):
        # The drifting official is also an admin — they should be skipped.
        users = [{"chat_id": 999, "username": "pastornew"},
                 {"chat_id": 1, "username": "alice"}]
        import bot
        bot._reported_username_drift.clear()
        ctx = _ctx()
        ctx.bot.send_message = AsyncMock()

        async def _get_users():
            return users

        with patch("storage.get_all_users", side_effect=_get_users), \
             patch.object(__import__("permissions"), "OFFICIALS", _officials_with()), \
             patch.object(__import__("permissions"), "ADMIN_USERNAMES", {"alice", "pastornew"}), \
             patch.object(__import__("permissions"), "_admin_chat_ids", set()):
            _run(bot._warn_official_username_drift(ctx, 999, "pastornew"))
        sent_to = [c.args[0] for c in ctx.bot.send_message.await_args_list]
        assert 999 not in sent_to and sent_to == [1]

    def test_markdown_in_names_is_escaped(self):
        offs = _officials_with(uname="old_name")
        offs[0]["name"] = "Pastor_Test *Snr*"
        ctx = self._run_alert(officials=offs)
        msg = ctx.bot.send_message.await_args[0][1]
        assert "Pastor\\_Test" in msg and "\\*Snr\\*" in msg

    def test_send_failure_does_not_raise(self):
        from telegram.error import TelegramError
        import bot
        bot._reported_username_drift.clear()
        ctx = _ctx()
        ctx.bot.send_message = AsyncMock(side_effect=TelegramError("blocked"))

        async def _get_users():
            return [{"chat_id": 1, "username": "alice"}]

        with patch("storage.get_all_users", side_effect=_get_users), \
             patch.object(__import__("permissions"), "OFFICIALS", _officials_with()), \
             patch.object(__import__("permissions"), "ADMIN_USERNAMES", {"alice"}), \
             patch.object(__import__("permissions"), "_admin_chat_ids", set()):
            _run(bot._warn_official_username_drift(ctx, 999, "pastornew"))  # must not raise


class TestCommandHookTriggersDriftCheck:
    def test_command_invocation_checks_for_drift(self):
        import bot
        upd = MagicMock()
        upd.effective_message.text = "/events"
        upd.effective_user.id = 999
        upd.effective_user.username = "pastornew"
        upd.effective_user.full_name = "Pastor Test"
        warn = AsyncMock()

        async def _noop(*a, **k):
            pass

        with patch("bot._refresh_user_identity", side_effect=_noop), \
             patch("bot._warn_official_username_drift", warn):
            _run(bot._log_command_invocation(upd, _ctx()))
        warn.assert_awaited_once()
        assert warn.await_args[0][1] == 999
        assert warn.await_args[0][2] == "pastornew"

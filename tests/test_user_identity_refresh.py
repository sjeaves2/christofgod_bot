"""Tests for keeping stored username/display_name in sync with Telegram.

A user may create/change/remove their @username (or rename themselves) after
registering; every private command refreshes the stored record via the
group -1 pre-handler.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _patched(users, saved):
    async def _get():
        return users

    async def _save(u):
        saved["users"] = u

    return patch("bot.get_all_users", side_effect=_get), \
        patch("bot.save_users", side_effect=_save)


class TestRefreshHelper:
    def _refresh(self, users, uid=111, uname="newname", dname="New Name"):
        import bot
        saved: dict = {}
        p_get, p_save = _patched(users, saved)
        with p_get, p_save:
            _run(bot._refresh_user_identity(uid, uname, dname))
        return saved

    def test_username_added_later_is_stored(self):
        users = [{"chat_id": 111, "username": None, "display_name": "New Name"}]
        saved = self._refresh(users)
        assert saved["users"][0]["username"] == "newname"

    def test_username_change_is_stored(self):
        users = [{"chat_id": 111, "username": "oldname", "display_name": "New Name"}]
        saved = self._refresh(users)
        assert saved["users"][0]["username"] == "newname"

    def test_username_removed_is_stored(self):
        users = [{"chat_id": 111, "username": "oldname", "display_name": "New Name"}]
        saved = self._refresh(users, uname=None)
        assert saved["users"][0]["username"] is None

    def test_display_name_change_is_stored(self):
        users = [{"chat_id": 111, "username": "newname", "display_name": "Old Name"}]
        saved = self._refresh(users)
        assert saved["users"][0]["display_name"] == "New Name"

    def test_no_change_does_not_save(self):
        users = [{"chat_id": 111, "username": "newname", "display_name": "New Name"}]
        saved = self._refresh(users)
        assert "users" not in saved

    def test_unregistered_user_not_created(self):
        saved = self._refresh([])
        assert "users" not in saved

    def test_other_fields_preserved(self):
        users = [{"chat_id": 111, "username": None, "display_name": "New Name",
                  "timezone": "Europe/Paris", "notif_prefs": ["special"]}]
        saved = self._refresh(users)
        rec = saved["users"][0]
        assert rec["timezone"] == "Europe/Paris"
        assert rec["notif_prefs"] == ["special"]


class TestCommandHookRefreshes:
    def test_any_command_triggers_refresh(self):
        import bot
        upd = MagicMock()
        upd.effective_message.text = "/events"
        upd.effective_user.id = 111
        upd.effective_user.username = "newname"
        upd.effective_user.full_name = "New Name"
        upd.effective_user.first_name = "New"
        ctx = MagicMock()

        users = [{"chat_id": 111, "username": None, "display_name": "New Name"}]
        saved: dict = {}
        p_get, p_save = _patched(users, saved)
        with p_get, p_save:
            _run(bot._log_command_invocation(upd, ctx))
        assert saved["users"][0]["username"] == "newname"

    def test_non_text_update_is_ignored(self):
        import bot
        upd = MagicMock()
        upd.effective_message = None
        refresh = AsyncMock()
        with patch("bot._refresh_user_identity", refresh):
            _run(bot._log_command_invocation(upd, MagicMock()))
        refresh.assert_not_called()

"""Tests for /userlist — user-supplied names must be Markdown-escaped so a name
containing '_', '*', '`', or '[' can't break the message (BadRequest: can't
parse entities). See the 2026-07-12 incident."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_update(chat_id: int = 1) -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = "admin"
    upd.effective_user.full_name = "Admin User"
    upd.message.reply_text = AsyncMock()
    return upd


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    return ctx


def _run_userlist(users):
    import bot
    upd = _make_update()
    ctx = _make_context()

    async def _fake_users():
        return users

    with patch("bot.get_all_users", side_effect=_fake_users), \
         patch("bot.is_admin", return_value=True):
        _run(bot.cmd_userlist(upd, ctx))
    return upd.message.reply_text.call_args[0][0]


class TestUserlistEscaping:
    def test_underscore_in_display_name_escaped(self):
        msg = _run_userlist([{"chat_id": 111, "display_name": "John_Doe",
                              "username": "jd"}])
        assert "John\\_Doe" in msg
        assert "John_Doe" not in msg.replace("John\\_Doe", "")

    def test_special_chars_in_username_escaped(self):
        msg = _run_userlist([{"chat_id": 111, "display_name": "Jane",
                              "username": "jane_2026"}])
        assert "@jane\\_2026" in msg

    def test_asterisk_and_backtick_escaped(self):
        msg = _run_userlist([{"chat_id": 111, "display_name": "*VIP* `boss`",
                              "username": "boss"}])
        assert "\\*VIP\\*" in msg
        assert "\\`boss\\`" in msg

    def test_missing_fields_render_dash(self):
        msg = _run_userlist([{"chat_id": 111}])
        assert "1. — (—)" in msg

    def test_empty_userlist(self):
        msg = _run_userlist([])
        assert "No registered users" in msg

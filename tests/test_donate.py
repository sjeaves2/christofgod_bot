"""Tests for /donate — invitation message with a configurable giving link."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_update(chat_id: int = 111) -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = "friend"
    upd.effective_user.full_name = "Church Friend"
    upd.message.reply_text = AsyncMock()
    return upd


def _run_donate(url, users=None):
    import bot
    upd = _make_update()
    ctx = MagicMock()
    ctx.bot = MagicMock()

    async def _fake_users():
        return users if users is not None else [{"chat_id": 111}]

    with patch("bot.DONATION_URL", url), \
         patch("bot.get_all_users", side_effect=_fake_users):
        _run(bot.cmd_donate(upd, ctx))
    return upd


class TestDonate:
    def test_configured_sends_message_with_link_button(self):
        upd = _run_donate("https://paypal.me/example")
        text = upd.message.reply_text.call_args[0][0]
        assert "Support the Congregation" in text
        markup = upd.message.reply_text.call_args.kwargs["reply_markup"]
        btn = markup.inline_keyboard[0][0]
        assert btn.url == "https://paypal.me/example"

    def test_unconfigured_sends_fallback(self):
        upd = _run_donate("")
        text = upd.message.reply_text.call_args[0][0]
        assert "isn't set up yet" in text
        assert "reply_markup" not in upd.message.reply_text.call_args.kwargs

    def test_localized_for_spanish_user(self):
        upd = _run_donate(
            "https://paypal.me/example",
            users=[{"chat_id": 111, "language": "es"}],
        )
        text = upd.message.reply_text.call_args[0][0]
        assert "Apoya a la Congregación" in text

    def test_help_topic_exists(self):
        import bot
        assert bot.HELP_TOPICS["donate"] == "help_donate"

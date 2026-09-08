"""Tests for the unknown-command reply.

The behaviour is small; the *wiring* is where this feature lives or dies. A
fallback registered in the wrong handler group answers "not a valid command" to
every valid command as well, so the registration tests below matter more than
the reply-text ones.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import CommandHandler, ConversationHandler, MessageHandler

sys.path.insert(0, str(Path(__file__).parent.parent))

from handlers.user_basics import cmd_unknown  # noqa: E402


def _upd(text: str = "/evnts", chat_id: int = 42) -> MagicMock:
    upd = MagicMock()
    upd.effective_user.id = chat_id
    upd.effective_user.username = "member"
    upd.effective_user.full_name = "A Member"
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    return upd


async def _call(upd):
    with patch("handlers.user_basics.get_user_prefs",
               AsyncMock(return_value=(None, "en"))):
        await cmd_unknown(upd, MagicMock())
    return upd.message.reply_text


class TestReply:
    @pytest.mark.asyncio
    async def test_names_the_command_and_points_at_help(self):
        reply = await _call(_upd("/evnts"))
        text = reply.call_args[0][0]
        assert "/evnts" in text
        assert "/help" in text
        assert "not a valid command" in text

    @pytest.mark.asyncio
    async def test_sent_as_plain_text(self):
        """No parse_mode: an unbalanced _ or * would make Telegram reject the
        whole message, restoring the very silence this fixes."""
        reply = await _call(_upd("/we_ird_*command*"))
        assert "parse_mode" not in reply.call_args.kwargs

    @pytest.mark.asyncio
    async def test_markdown_metacharacters_survive_intact(self):
        reply = await _call(_upd("/bad_*cmd"))
        assert "/bad_*cmd" in reply.call_args[0][0]

    @pytest.mark.asyncio
    async def test_strips_bot_username_suffix(self):
        reply = await _call(_upd("/evnts@ChristOfGodBot"))
        text = reply.call_args[0][0]
        assert "/evnts" in text
        assert "@ChristOfGodBot" not in text

    @pytest.mark.asyncio
    async def test_ignores_arguments(self):
        reply = await _call(_upd("/evnts tomorrow please"))
        assert "tomorrow" not in reply.call_args[0][0]

    @pytest.mark.asyncio
    async def test_truncates_absurdly_long_input(self):
        reply = await _call(_upd("/" + "x" * 500))
        assert len(reply.call_args[0][0]) < 200

    @pytest.mark.asyncio
    async def test_handles_empty_message_text(self):
        """update.message.text is None for a photo/sticker; must not crash."""
        reply = await _call(_upd(None))
        assert reply.called

    @pytest.mark.asyncio
    async def test_uses_the_users_language(self):
        upd = _upd("/evnts")
        with patch("handlers.user_basics.get_user_prefs",
                   AsyncMock(return_value=(None, "es"))):
            await cmd_unknown(upd, MagicMock())
        assert "no es un comando válido" in upd.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_logged_for_stats(self):
        upd = _upd("/evnts")
        with patch("handlers.user_basics.activity.log_command") as log:
            with patch("handlers.user_basics.get_user_prefs",
                       AsyncMock(return_value=(None, "en"))):
                await cmd_unknown(upd, MagicMock())
        assert log.called
        assert log.call_args[0][0] == "unknown"
        assert log.call_args.kwargs["details"] == "/evnts"


class TestRegistration:
    """The part that actually makes or breaks the feature."""

    @staticmethod
    def _handlers():
        import bot
        app = MagicMock()
        registered: dict[int, list] = {}

        def add_handler(handler, group=0):
            registered.setdefault(group, []).append(handler)

        app.add_handler.side_effect = add_handler
        app.add_error_handler = MagicMock()
        app.run_polling = MagicMock()
        app.job_queue = MagicMock()

        builder = MagicMock()
        builder.token.return_value = builder
        builder.post_init.return_value = builder
        builder.build.return_value = app
        with patch.object(bot, "Application") as App:
            App.builder.return_value = builder
            bot.main()
        return registered

    def test_fallback_is_the_last_handler_in_group_zero(self):
        """PTB runs only the first match per group, so this must come last —
        anything registered after it would be unreachable."""
        group0 = self._handlers()[0]
        last = group0[-1]
        assert isinstance(last, MessageHandler)
        assert last.callback is cmd_unknown

    def test_fallback_is_in_group_zero_not_a_later_group(self):
        """In a later group it would fire on EVERY command, valid ones too."""
        registered = self._handlers()
        for group, handlers in registered.items():
            for h in handlers:
                if getattr(h, "callback", None) is cmd_unknown:
                    assert group == 0, (
                        f"cmd_unknown registered in group {group}; in any group "
                        "other than 0 it answers valid commands as well"
                    )

    def test_real_commands_are_registered_before_the_fallback(self):
        """Proves the ordering claim rather than assuming it."""
        group0 = self._handlers()[0]
        fallback_index = next(
            i for i, h in enumerate(group0)
            if getattr(h, "callback", None) is cmd_unknown
        )
        command_names = set()
        for h in group0[:fallback_index]:
            if isinstance(h, CommandHandler):
                command_names |= set(h.commands)
            elif isinstance(h, ConversationHandler):
                for entry in h.entry_points:
                    if isinstance(entry, CommandHandler):
                        command_names |= set(entry.commands)
        for expected in ("help", "start", "events", "stats", "appointment"):
            assert expected in command_names, (
                f"/{expected} must be registered before the unknown-command "
                "fallback or it would never reach its own handler"
            )

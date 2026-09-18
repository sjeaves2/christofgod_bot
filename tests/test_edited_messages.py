"""An edited command must not crash the bot.

On 2026-09-18 an admin typed /private, was told it was not a valid command,
and corrected the typo by EDITING the message — the natural thing to do. PTB
fires CommandHandler on an edit too, but an edit arrives as
update.edited_message, so update.message is None:

    AttributeError: 'NoneType' object has no attribute 'reply_text'
      handlers/user_basics.py:293 in cmd_privacy

/privacy was merely where it landed. All 180 uses of update.message across the
handlers had the same exposure, so this is fixed once, in front of them all,
rather than 180 times.

The bot declines to act on edits rather than honouring them: honouring an edit
would also re-deliver edited text into an open conversation, letting a member
silently resubmit an old prayer request. But it says so — a silent refusal
leaves someone who has just typed staring at nothing.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import ApplicationHandlerStop

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.user_basics as UB  # noqa: E402


def _edited(text="/privacy", is_command=True):
    user = User(id=474108062, first_name="Sam", is_bot=False, username="sjeaves2")
    chat = Chat(id=474108062, type=Chat.PRIVATE)
    msg = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user,
        text=text,
        entities=[MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0,
                                length=len(text))] if is_command else [],
    )
    bot = MagicMock()
    bot.username = "christofgod_bot"
    bot.send_message = AsyncMock()
    msg.set_bot(bot)
    return Update(update_id=1, edited_message=msg)


async def _run(update):
    sent = []

    async def reply(text, **kwargs):
        sent.append(text)

    with patch.object(UB, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
         patch.object(type(update.edited_message), "reply_text",
                      AsyncMock(side_effect=reply), create=True):
        try:
            await UB.ignore_edited_messages(update, MagicMock())
        except ApplicationHandlerStop:
            return sent, True
    return sent, False


class TestEditedCommands:
    async def test_the_exact_production_case_does_not_crash(self):
        """/private edited to /privacy — update.message is None throughout."""
        upd = _edited("/privacy")
        assert upd.message is None, "the premise: an edit has no .message"
        sent, stopped = await _run(upd)
        assert stopped, "later handlers must never see the update"

    async def test_the_sender_is_told_why_nothing_happened(self):
        sent, _ = await _run(_edited("/privacy"))
        assert sent and "edited" in sent[0].lower()

    async def test_an_edited_non_command_is_dropped_silently(self):
        """Editing ordinary chat should not earn a lecture."""
        sent, stopped = await _run(_edited("just fixing a typo", is_command=False))
        assert stopped and sent == []

    @pytest.mark.parametrize("lang", ["en", "es", "fr", "zu"])
    async def test_the_notice_is_localized(self, lang):
        from localization import t
        assert t("edited_message_ignored", lang), f"missing {lang} string"
        assert t("edited_message_ignored", lang) != "edited_message_ignored"


class TestWiring:
    """Registered is not the same as reached."""

    @staticmethod
    def _handlers():
        import warnings
        import bot

        class FakeApp:
            def __init__(self):
                self.handlers = []
                self.job_queue = MagicMock()
                self.bot = MagicMock()
                self.bot.set_my_commands = AsyncMock()

            def add_handler(self, h, group=0):
                self.handlers.append((group, h))

            def add_error_handler(self, h):
                pass

            def run_polling(self, **kw):
                pass

        fake = FakeApp()

        class FakeBuilder:
            def token(self, t_):
                return self

            def post_init(self, f):
                return self

            def build(self):
                return fake

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with patch.object(bot.Application, "builder",
                              staticmethod(lambda: FakeBuilder())):
                bot.main()
        return fake.handlers

    def test_it_is_registered_ahead_of_every_real_handler(self):
        handlers = self._handlers()
        assert len(handlers) > 20, "the sweep must see the real wiring"
        groups = [g for g, h in handlers
                  if getattr(h, "callback", None) is UB.ignore_edited_messages]
        assert groups, "the edited-message gatekeeper is not registered at all"
        assert min(groups) < 0, (
            "it must sit in a group before the command handlers, or they act on "
            "the edit first and crash on update.message being None"
        )

    def test_it_actually_matches_an_edited_update(self):
        """The filter has to select edits — registration alone proves nothing."""
        handlers = self._handlers()
        h = next(h for g, h in handlers
                 if getattr(h, "callback", None) is UB.ignore_edited_messages)
        assert h.check_update(_edited("/privacy")), "filter does not match an edit"

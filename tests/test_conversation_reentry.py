"""Retyping a conversation's own command must restart it, not reject it.

On 2026-09-17, part-way through re-entering service links, /setservicelink
began answering "⛔ Unknown command." The admin check was fine and the command
was spelled correctly. The conversation was simply already running:

  * PTB does not re-fire an entry point for an active conversation unless
    allow_reentry=True, and it was set nowhere in bot.py;
  * every state handler is TEXT & ~COMMAND, so it ignores a command;
  * so the update fell through to the unknown-command catch-all.

The reply was worse than unhelpful — it says the command does not exist, when
in fact it exists and is already running. /cancel was the only way out and
nothing said so.

This affected all thirteen conversations, including /prayer, where the person
stuck would be an ordinary member rather than an admin.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import ConversationHandler

sys.path.insert(0, str(Path(__file__).parent.parent))


def _built_conversations():
    """Every ConversationHandler main() actually registers."""
    import bot

    class FakeApp:
        def __init__(self):
            self.handlers = []
            self.job_queue = MagicMock()
            self.bot = MagicMock()
            self.bot.set_my_commands = AsyncMock()

        def add_handler(self, h, group=0):
            self.handlers.append(h)

        def add_error_handler(self, h):
            pass

        def run_polling(self, **kw):
            pass

    fake = FakeApp()

    class FakeBuilder:
        def token(self, t):
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
    return [h for h in fake.handlers if isinstance(h, ConversationHandler)]


class TestEveryConversationAllowsReentry:
    def test_the_sweep_sees_the_wiring(self):
        """Guards the test below: a sweep over nothing passes vacuously."""
        assert len(_built_conversations()) >= 10

    def test_all_conversations_are_reentrant(self):
        bad = []
        for c in _built_conversations():
            if not c.allow_reentry:
                cmds = [x for h in c.entry_points
                        for x in (getattr(h, "commands", None) or [])]
                bad.append("/" + (cmds[0] if cmds else c.name))
        assert not bad, (
            "these answer their own command with '⛔ Unknown command.' once "
            f"running, and only /cancel escapes: {sorted(bad)}"
        )


class TestReentryDoesNotLetAnEntryPointHijackLiveTaps:
    """The price of allow_reentry=True, and a real production bug.

    Because allow_reentry is set everywhere (see above), PTB checks ENTRY
    POINTS BEFORE the state handlers on every update:

        if state is None or self.allow_reentry:
            for entry_point in self.entry_points:     # first
        ...
        for candidate in self.states.get(state, []):  # only after

    So a CallbackQueryHandler among the entry points intercepts LIVE button
    taps mid-conversation. /songlist shipped that way in v0.26.0: tapping a
    song answered "the bot restarted since those buttons were drawn", while a
    list was plainly in progress.

    It is not banned outright, because board_items_conversation uses one
    safely — its entry-point callback IS one of its state handlers, so
    whichever branch PTB takes, the same function runs. THAT IDENTITY is the
    load-bearing part, and it is what this test checks. A conversation with
    per-button state handlers cannot satisfy it and must register its
    dead-button handler outside the conversation instead.
    """

    @staticmethod
    def _callback_names(handlers):
        return {getattr(getattr(h, "callback", None), "__name__", "?")
                for h in handlers}

    def test_the_sweep_sees_the_wiring(self):
        """Guards the test below against passing vacuously."""
        assert len(_built_conversations()) >= 10

    def test_no_callback_entry_point_can_shadow_a_state_handler(self):
        from telegram.ext import CallbackQueryHandler

        offenders = []
        for conv in _built_conversations():
            entries = [h for h in conv.entry_points
                       if isinstance(h, CallbackQueryHandler)]
            if not entries or not conv.allow_reentry:
                continue
            entry_cbs = self._callback_names(entries)
            state_cbs = set()
            for handlers in conv.states.values():
                state_cbs |= self._callback_names(handlers)
            hijackable = entry_cbs - state_cbs
            if hijackable:
                label = ",".join(sorted(
                    x for h in conv.entry_points
                    for x in (getattr(h, "commands", None) or []))) or conv.name
                offenders.append(f"{label}: {sorted(hijackable)}")
        assert not offenders, (
            "these have a CallbackQueryHandler entry point whose callback is "
            "NOT also a state handler, so with allow_reentry=True it will "
            "intercept live button taps mid-conversation: "
            f"{sorted(offenders)}"
        )


class TestReentryDispatch:
    """Behavioural: an ACTIVE conversation still matches its entry command."""

    @staticmethod
    def _conv(name):
        for c in _built_conversations():
            if any(name in (getattr(h, "commands", None) or []) for h in c.entry_points):
                return c
        pytest.fail(f"no conversation with entry command /{name}")

    @staticmethod
    def _update(text):
        """A REAL Update: CommandHandler needs a genuine BOT_COMMAND entity,
        which a MagicMock cannot supply."""
        from datetime import datetime, timezone

        from telegram import Chat, Message, MessageEntity, Update, User

        user = User(id=99, first_name="An", is_bot=False, username="admin")
        chat = Chat(id=99, type=Chat.PRIVATE)
        msg = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text=text,
            entities=[MessageEntity(type=MessageEntity.BOT_COMMAND,
                                    offset=0, length=len(text))],
        )
        bot = MagicMock()
        bot.username = "christofgod_bot"       # CommandHandler checks @mentions
        msg.set_bot(bot)
        return Update(update_id=1, message=msg)

    @pytest.mark.parametrize("cmd", ["setservicelink", "prayer", "appointment"])
    def test_retyping_the_command_mid_conversation_is_handled(self, cmd):
        conv = self._conv(cmd)
        conv._conversations[(99, 99)] = sorted(conv.states)[0]
        try:
            assert conv.check_update(self._update(f"/{cmd}")), (
                f"/{cmd} typed during its own conversation is not handled, so "
                "it falls through to the unknown-command catch-all"
            )
        finally:
            conv._conversations.pop((99, 99), None)

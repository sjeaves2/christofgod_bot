"""A command typed in the group gets pointed at a private chat.

The bot serves private chats only, and before this every group message was
dropped in silence. The effect showed up in the usage log: of twelve registered
members, exactly one had ever run a command (a single /start on 2026-09-03).
Members sitting in the COGM group could not reach any command at all — they
would have had to leave the group, open a private chat and type. Reminders
arrived because those are POSTED to the group; nothing else was reachable.

The nudge is posted to the GROUP, not sent as a DM. Telegram forbids a bot from
opening a conversation with someone who has not pressed Start, so a private
reply would be silence for exactly the newcomer who most needs the answer.

It matters most for /prayer: that used to vanish, leaving a member about to
type something personal into a group of twelve.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.user_basics as UB  # noqa: E402

GROUP = -1001178984510


def _group_update(text: str, chat_id: int = GROUP, user_id: int = 99):
    upd = MagicMock()
    upd.effective_user.id = user_id
    upd.effective_user.username = "member"
    upd.effective_user.full_name = "A Member"
    msg = MagicMock()
    msg.text = text
    msg.chat.id = chat_id
    msg.reply_text = AsyncMock()
    upd.effective_message = msg
    return upd, msg


async def _nudge(upd):
    with patch.object(UB, "get_user_prefs", AsyncMock(return_value=(None, "en"))):
        with pytest.raises(ApplicationHandlerStop):
            await UB.nudge_group_to_private(upd, MagicMock())


@pytest.fixture(autouse=True)
def _clear_cooldown():
    UB._group_nudge_sent.clear()
    yield
    UB._group_nudge_sent.clear()


class TestWhatGetsANudge:
    async def test_a_command_is_answered(self):
        upd, msg = _group_update("/events")
        await _nudge(upd)
        msg.reply_text.assert_awaited_once()
        assert "privately" in msg.reply_text.await_args[0][0]

    async def test_ordinary_chatter_is_ignored(self):
        """A bot that replied to conversation would be the clutter this avoids."""
        upd, msg = _group_update("see you at the service tonight")
        await _nudge(upd)
        msg.reply_text.assert_not_called()

    async def test_a_command_addressed_to_the_bot_is_answered(self):
        """Telegram appends @botname when several bots are in a group."""
        upd, msg = _group_update("/events@christofgod_bot")
        await _nudge(upd)
        msg.reply_text.assert_awaited_once()

    async def test_a_command_with_arguments_is_answered(self):
        upd, msg = _group_update("/stats 30")
        await _nudge(upd)
        msg.reply_text.assert_awaited_once()

    async def test_prayer_is_redirected_before_anything_personal_is_typed(self):
        upd, msg = _group_update("/prayer")
        await _nudge(upd)
        msg.reply_text.assert_awaited_once()

    async def test_every_group_message_stops_the_handler_chain(self):
        """Nudged or not, no real handler may act on a group message."""
        for text in ("/events", "ordinary chatter"):
            upd, _ = _group_update(text)
            await _nudge(upd)   # asserts ApplicationHandlerStop


class TestTheButton:
    @staticmethod
    async def _markup(text):
        upd, msg = _group_update(text)
        await _nudge(upd)
        return msg.reply_text.await_args.kwargs["reply_markup"]

    async def test_it_deep_links_to_this_bot(self):
        btn = (await self._markup("/events")).inline_keyboard[0][0]
        assert btn.url.startswith("https://t.me/christofgod_bot?start=")

    async def test_the_payload_carries_the_command(self):
        """So the button finishes the request instead of dead-ending."""
        btn = (await self._markup("/events")).inline_keyboard[0][0]
        assert btn.url.endswith("start=events")

    async def test_an_unsupported_command_falls_back_to_plain_start(self):
        btn = (await self._markup("/setservicelink")).inline_keyboard[0][0]
        assert btn.url.endswith("start=start")

    async def test_the_button_has_a_label(self):
        btn = (await self._markup("/events")).inline_keyboard[0][0]
        assert btn.text and btn.text != "group_nudge_button"


class TestThrottle:
    async def test_one_nudge_per_command_per_window(self):
        posts = 0
        for _ in range(3):
            upd, msg = _group_update("/events")
            await _nudge(upd)
            posts += msg.reply_text.await_count
        assert posts == 1, f"three members asking produced {posts} posts in the group"

    async def test_a_different_command_is_not_throttled(self):
        sent = []
        for text in ("/events", "/donate"):
            upd, msg = _group_update(text)
            await _nudge(upd)
            sent.append(msg.reply_text.await_count)
        assert sent == [1, 1]

    async def test_a_different_group_is_not_throttled(self):
        upd, _ = _group_update("/events", chat_id=GROUP)
        await _nudge(upd)
        upd2, msg2 = _group_update("/events", chat_id=-100999)
        await _nudge(upd2)
        msg2.reply_text.assert_awaited_once()

    async def test_the_nudge_returns_after_the_window(self):
        upd, _ = _group_update("/events")
        await _nudge(upd)
        key = (GROUP, "events")
        UB._group_nudge_sent[key] -= UB.GROUP_NUDGE_COOLDOWN + timedelta(seconds=1)
        upd2, msg2 = _group_update("/events")
        await _nudge(upd2)
        msg2.reply_text.assert_awaited_once()

    def test_the_window_is_thirty_minutes(self):
        assert UB.GROUP_NUDGE_COOLDOWN == timedelta(minutes=30)


class TestFailureIsContained:
    async def test_a_failed_post_does_not_break_the_gate(self):
        """If the bot cannot post in the group, group messages must still be
        stopped rather than falling through to the real handlers."""
        upd, msg = _group_update("/events")
        msg.reply_text = AsyncMock(side_effect=TelegramError("no permission"))
        await _nudge(upd)   # still raises ApplicationHandlerStop


class TestDeepLinkFollowUp:
    def test_every_advertised_payload_is_one_start_can_act_on(self):
        """A button promising an answer must lead to one."""
        assert UB.DEEP_LINK_COMMANDS == set(UB.DEEP_LINK_FOLLOW_UPS)

    def test_no_follow_up_opens_a_conversation(self):
        """Someone who just pressed Start has not asked to be put into a
        multi-step flow; /cancel would be their first experience of the bot."""
        import handlers.prayer as prayer
        import handlers.appointments as appointments
        conversational = {prayer.cmd_prayer, appointments.cmd_appointment}
        assert not (set(UB.DEEP_LINK_FOLLOW_UPS.values()) & conversational)


class TestStartActsOnThePayload:
    """The button is only worth pressing if /start finishes the request."""

    @staticmethod
    async def _start(payload):
        ran = []

        async def fake_events(u, c):
            ran.append("events")

        upd = MagicMock()
        upd.effective_user.id = 7
        upd.effective_user.username = "member"
        upd.effective_user.full_name = "A Member"
        upd.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.args = [payload] if payload else []

        with patch.object(UB.storage, "get_all_users", AsyncMock(return_value=[])), \
             patch.object(UB.storage, "save_users", AsyncMock()), \
             patch.object(UB, "get_user_prefs", AsyncMock(return_value=(None, "en"))), \
             patch.object(UB, "_register_official_if_known", AsyncMock()), \
             patch.object(UB.permissions, "_register_admin_by_username", AsyncMock()), \
             patch.object(UB.permissions, "is_admin", lambda _u: False), \
             patch.object(UB.permissions, "is_prayer_admin", lambda _u: False), \
             patch.object(UB.permissions, "_is_known_official", lambda *a: True), \
             patch.object(UB.activity, "log_command", lambda *a, **k: None), \
             patch.object(UB.activity, "log_user_joined", lambda *a, **k: None), \
             patch.dict(UB.DEEP_LINK_FOLLOW_UPS, {"events": fake_events}):
            await UB.cmd_start(upd, ctx)
        return ran

    async def test_the_payload_runs_the_command(self):
        assert await self._start("events") == ["events"]

    async def test_a_plain_start_runs_nothing_extra(self):
        assert await self._start(None) == []

    async def test_an_unknown_payload_is_ignored(self):
        """A stale or hand-edited link must not raise."""
        assert await self._start("nonsense") == []


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

    @staticmethod
    def _real_group_update():
        """A genuine Update; a MagicMock cannot exercise a PTB filter."""
        from datetime import datetime, timezone

        from telegram import Chat, Message, MessageEntity, Update, User

        user = User(id=99, first_name="A", is_bot=False, username="member")
        chat = Chat(id=GROUP, type=Chat.SUPERGROUP)
        msg = Message(
            message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user,
            text="/events",
            entities=[MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=7)],
        )
        bot_ = MagicMock()
        bot_.username = "christofgod_bot"
        msg.set_bot(bot_)
        return Update(update_id=1, message=msg)

    def test_its_filter_actually_matches_a_group_message(self):
        """Registration proves nothing if the filter no longer selects groups."""
        handlers = self._handlers()
        h = next(h for _, h in handlers
                 if getattr(h, "callback", None) is UB.nudge_group_to_private)
        assert h.check_update(self._real_group_update()), (
            "the gate does not match a group message, so group commands would "
            "be handled for real instead of redirected"
        )

    def test_the_nudge_guards_group_messages_before_every_handler(self):
        handlers = self._handlers()
        assert len(handlers) > 20, "the sweep must see the real wiring"
        groups = [g for g, h in handlers
                  if getattr(h, "callback", None) is UB.nudge_group_to_private]
        assert groups, "the group gate is not registered at all"
        assert min(groups) < 0, (
            "it must run before the command handlers, or a group command is "
            "handled for real instead of being redirected"
        )

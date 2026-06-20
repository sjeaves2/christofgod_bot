"""Tests for the notification retry queue and command-execution logging."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytz
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

TZ = pytz.timezone("America/New_York")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeForbidden(Exception):
    pass


def _event(key="ev1", minutes_to_service=60):
    svc = datetime.now(TZ) + timedelta(minutes=minutes_to_service)
    return {
        "key": key,
        "name": "Test Service",
        "service_time": svc,
        "notification_time": svc - timedelta(minutes=90),
        "announcements": [],
    }


def _bot_with(send_side_effect=None):
    bot_obj = MagicMock()
    bot_obj.send_message = AsyncMock(side_effect=send_side_effect)
    return bot_obj


class TestDeliverEventNotifications:
    """deliver_event_notifications must be idempotent and retry only the pending."""

    def _setup(self, users, state=None, send_side_effect=None):
        import bot

        saved_state = {"states": dict(state or {})}
        saved_users = {"value": list(users)}

        async def _get_users():
            return list(saved_users["value"])

        async def _save_users(u):
            saved_users["value"] = list(u)

        async def _load_state():
            return dict(saved_state["states"])

        async def _save_state(s):
            saved_state["states"] = dict(s)

        bot_obj = _bot_with(send_side_effect)
        ctx_patches = [
            patch("bot.get_all_users", side_effect=_get_users),
            patch("bot.save_users", side_effect=_save_users),
            patch("bot._load_notif_state", side_effect=_load_state),
            patch("bot._save_notif_state", side_effect=_save_state),
        ]
        return bot, bot_obj, saved_state, saved_users, ctx_patches

    def _deliver(self, bot, bot_obj, ctx_patches, event):
        for p in ctx_patches:
            p.start()
        try:
            return _run(bot.deliver_event_notifications(bot_obj, event))
        finally:
            for p in ctx_patches:
                p.stop()

    def test_sends_to_all_subscribers(self):
        bot, bot_obj, state, users, p = self._setup([{"chat_id": 1}, {"chat_id": 2}])
        sent = self._deliver(bot, bot_obj, p, _event())
        assert sent == 2
        assert bot_obj.send_message.await_count == 2

    def test_records_notified_in_state(self):
        bot, bot_obj, state, users, p = self._setup([{"chat_id": 1}, {"chat_id": 2}])
        self._deliver(bot, bot_obj, p, _event(key="abc"))
        assert set(state["states"]["abc"]["notified"]) == {1, 2}

    def test_does_not_resend_to_already_notified(self):
        bot, bot_obj, state, users, p = self._setup(
            [{"chat_id": 1}, {"chat_id": 2}],
            state={"abc": {"name": "x", "service_time": "t", "notified": [1]}},
        )
        sent = self._deliver(bot, bot_obj, p, _event(key="abc"))
        assert sent == 1
        # Only chat_id 2 should have been messaged.
        assert bot_obj.send_message.await_args[0][0] == 2

    def test_fully_notified_is_noop(self):
        bot, bot_obj, state, users, p = self._setup(
            [{"chat_id": 1}],
            state={"abc": {"name": "x", "service_time": "t", "notified": [1]}},
        )
        sent = self._deliver(bot, bot_obj, p, _event(key="abc"))
        assert sent == 0
        bot_obj.send_message.assert_not_awaited()

    def test_past_service_time_sends_nothing(self):
        bot, bot_obj, state, users, p = self._setup([{"chat_id": 1}])
        sent = self._deliver(bot, bot_obj, p, _event(minutes_to_service=-5))
        assert sent == 0
        bot_obj.send_message.assert_not_awaited()

    def test_network_failure_leaves_user_pending(self):
        import bot as botmod
        from telegram.error import NetworkError

        bot, bot_obj, state, users, p = self._setup(
            [{"chat_id": 1}, {"chat_id": 2}],
            send_side_effect=NetworkError("down"),
        )
        sent = self._deliver(bot, bot_obj, p, _event(key="abc"))
        assert sent == 0
        # Nobody recorded as notified — they'll be retried next tick.
        assert state["states"]["abc"]["notified"] == []

    def test_partial_failure_then_retry_completes(self):
        import bot as botmod
        from telegram.error import NetworkError

        # First attempt: chat 1 ok, chat 2 fails.
        calls = {"n": 0}

        async def flaky(chat_id, text, **kwargs):
            calls["n"] += 1
            if chat_id == 2 and calls["n"] <= 2:
                raise NetworkError("temporary")

        bot, bot_obj, state, users, p = self._setup(
            [{"chat_id": 1}, {"chat_id": 2}], send_side_effect=flaky)

        first = self._deliver(bot, bot_obj, p, _event(key="abc"))
        assert first == 1
        assert state["states"]["abc"]["notified"] == [1]

        # Second attempt (retry): only chat 2 is pending and now succeeds.
        second = self._deliver(bot, bot_obj, p, _event(key="abc"))
        assert second == 1
        assert set(state["states"]["abc"]["notified"]) == {1, 2}

    def test_blocked_user_removed(self):
        from telegram.error import Forbidden

        async def block_two(chat_id, text, **kwargs):
            if chat_id == 2:
                raise Forbidden("blocked")

        bot, bot_obj, state, users, p = self._setup(
            [{"chat_id": 1}, {"chat_id": 2}], send_side_effect=block_two)
        sent = self._deliver(bot, bot_obj, p, _event(key="abc"))
        assert sent == 1
        assert all(u["chat_id"] != 2 for u in users["value"])


class TestCatchupJob:
    def test_delivers_for_in_window_event_and_prunes(self):
        import bot

        in_window = _event(key="live", minutes_to_service=30)       # notif passed, service future
        future = _event(key="future", minutes_to_service=60 * 24)   # notif not yet due
        future["notification_time"] = datetime.now(TZ) + timedelta(hours=1)
        past = _event(key="past", minutes_to_service=-10)           # already started

        delivered = []

        async def _fake_all_upcoming(days_ahead=3):
            return [in_window, future, past]

        async def _fake_deliver(bot_obj, ev):
            delivered.append(ev["key"])
            return 1

        saved_state = {"states": {"past": {"notified": [1]}, "live": {"notified": []}}}

        async def _load_state():
            return dict(saved_state["states"])

        async def _save_state(s):
            saved_state["states"] = dict(s)

        ctx = MagicMock()
        ctx.bot = MagicMock()

        with patch("bot.all_upcoming", side_effect=_fake_all_upcoming), \
             patch("bot.deliver_event_notifications", side_effect=_fake_deliver), \
             patch("bot._load_notif_state", side_effect=_load_state), \
             patch("bot._save_notif_state", side_effect=_save_state):
            _run(bot.notification_catchup_job(ctx))

        # Only the in-window event triggers delivery.
        assert delivered == ["live"]
        # State for the already-started event is pruned; live retained.
        assert "past" not in saved_state["states"]
        assert "live" in saved_state["states"]


class TestCommandLogging:
    def test_logs_command_at_info(self, caplog):
        import bot

        upd = MagicMock()
        upd.effective_message.text = "/events extra args"
        upd.effective_user.full_name = "Jane Doe"
        upd.effective_user.id = 42
        ctx = MagicMock()

        with caplog.at_level(logging.INFO, logger="bot"):
            _run(bot._log_command_invocation(upd, ctx))

        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "/events" in msgs
        assert "Jane Doe" in msgs
        assert "42" in msgs

    def test_ignores_non_text_message(self):
        import bot

        upd = MagicMock()
        upd.effective_message = None
        ctx = MagicMock()
        # Should not raise.
        _run(bot._log_command_invocation(upd, ctx))

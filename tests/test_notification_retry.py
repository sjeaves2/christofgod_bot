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


def _event(key="ev1", minutes_to_service=60, targets=(-100, -200)):
    svc = datetime.now(TZ) + timedelta(minutes=minutes_to_service)
    return {
        "key": key,
        "name": "Test Service",
        "service_time": svc,
        "notification_time": svc - timedelta(minutes=90),
        "target_chat_ids": list(targets),
        "announcements": [],
    }


def _bot_with(send_side_effect=None):
    bot_obj = MagicMock()
    bot_obj.send_message = AsyncMock(side_effect=send_side_effect)
    return bot_obj


class TestDeliverEventNotifications:
    """deliver_event_notifications posts once per target group and is idempotent."""

    def _setup(self, state=None, send_side_effect=None):
        import bot

        saved_state = {"states": dict(state or {})}

        async def _load_state():
            return dict(saved_state["states"])

        async def _save_state(s):
            saved_state["states"] = dict(s)

        bot_obj = _bot_with(send_side_effect)
        ctx_patches = [
            patch("bot._load_notif_state", side_effect=_load_state),
            patch("bot._save_notif_state", side_effect=_save_state),
        ]
        return bot, bot_obj, saved_state, ctx_patches

    def _deliver(self, bot, bot_obj, ctx_patches, event):
        for p in ctx_patches:
            p.start()
        try:
            return _run(bot.deliver_event_notifications(bot_obj, event))
        finally:
            for p in ctx_patches:
                p.stop()

    def test_posts_once_per_target_group(self):
        bot, bot_obj, state, p = self._setup()
        sent = self._deliver(bot, bot_obj, p, _event(targets=(-100, -200)))
        assert sent == 2
        assert bot_obj.send_message.await_count == 2

    def test_no_targets_sends_nothing(self):
        bot, bot_obj, state, p = self._setup()
        sent = self._deliver(bot, bot_obj, p, _event(targets=()))
        assert sent == 0
        bot_obj.send_message.assert_not_awaited()

    def test_records_notified_targets_in_state(self):
        bot, bot_obj, state, p = self._setup()
        self._deliver(bot, bot_obj, p, _event(key="abc", targets=(-100, -200)))
        assert set(state["states"]["abc"]["notified"]) == {-100, -200}

    def test_does_not_repost_to_already_notified(self):
        bot, bot_obj, state, p = self._setup(
            state={"abc": {"name": "x", "service_time": "t", "notified": [-100]}},
        )
        sent = self._deliver(bot, bot_obj, p, _event(key="abc", targets=(-100, -200)))
        assert sent == 1
        assert bot_obj.send_message.await_args[0][0] == -200

    def test_fully_notified_is_noop(self):
        bot, bot_obj, state, p = self._setup(
            state={"abc": {"name": "x", "service_time": "t", "notified": [-100]}},
        )
        sent = self._deliver(bot, bot_obj, p, _event(key="abc", targets=(-100,)))
        assert sent == 0
        bot_obj.send_message.assert_not_awaited()

    def test_past_service_time_sends_nothing(self):
        bot, bot_obj, state, p = self._setup()
        sent = self._deliver(bot, bot_obj, p, _event(minutes_to_service=-5))
        assert sent == 0
        bot_obj.send_message.assert_not_awaited()

    def test_network_failure_leaves_target_pending(self):
        from telegram.error import NetworkError

        bot, bot_obj, state, p = self._setup(send_side_effect=NetworkError("down"))
        sent = self._deliver(bot, bot_obj, p, _event(key="abc", targets=(-100, -200)))
        assert sent == 0
        # Nothing recorded as notified — retried next tick.
        assert state["states"]["abc"]["notified"] == []

    def test_forbidden_leaves_target_pending(self):
        """Bot removed from a group → Forbidden → keep pending (no user deletion)."""
        from telegram.error import Forbidden

        bot, bot_obj, state, p = self._setup(send_side_effect=Forbidden("not in group"))
        sent = self._deliver(bot, bot_obj, p, _event(key="abc", targets=(-100,)))
        assert sent == 0
        assert state["states"]["abc"]["notified"] == []

    def test_partial_failure_then_retry_completes(self):
        from telegram.error import NetworkError

        calls = {"n": 0}

        async def flaky(chat_id, text, **kwargs):
            calls["n"] += 1
            if chat_id == -200 and calls["n"] <= 2:
                raise NetworkError("temporary")

        bot, bot_obj, state, p = self._setup(send_side_effect=flaky)

        first = self._deliver(bot, bot_obj, p, _event(key="abc", targets=(-100, -200)))
        assert first == 1
        assert state["states"]["abc"]["notified"] == [-100]

        second = self._deliver(bot, bot_obj, p, _event(key="abc", targets=(-100, -200)))
        assert second == 1
        assert set(state["states"]["abc"]["notified"]) == {-100, -200}


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


class TestTargetResolution:
    def test_maps_registry_names_to_chat_ids(self):
        import bot
        registry = {"main": -1001, "ann": "@cogm"}
        assert bot._resolve_targets(["main", "ann"], registry) == [-1001, "@cogm"]

    def test_passes_through_raw_numeric_id(self):
        import bot
        assert bot._resolve_targets([-1002], {}) == [-1002]

    def test_passes_through_numeric_string_and_channel_handle(self):
        import bot
        assert bot._resolve_targets(["-1003", "@chan"], {}) == [-1003, "@chan"]

    def test_drops_unknown_names(self):
        import bot
        assert bot._resolve_targets(["nope"], {"main": -1001}) == []

    def test_deduplicates(self):
        import bot
        assert bot._resolve_targets(["main", "main", -1001], {"main": -1001}) == [-1001]

    def test_empty(self):
        import bot
        assert bot._resolve_targets([], {"main": -1001}) == []


class TestGroupGate:
    def test_group_message_raises_stop(self):
        import bot
        ctx = MagicMock()
        upd = MagicMock()
        with pytest.raises(bot.ApplicationHandlerStop):
            _run(bot._ignore_group_messages(upd, ctx))

    def test_my_chat_member_logs_chat_id(self, caplog):
        import bot, logging
        from telegram.constants import ChatType

        upd = MagicMock()
        upd.my_chat_member.chat.type = ChatType.SUPERGROUP
        upd.my_chat_member.chat.title = "COGM Members"
        upd.my_chat_member.chat.id = -1009999
        upd.my_chat_member.new_chat_member.status = "member"
        ctx = MagicMock()

        with caplog.at_level(logging.INFO, logger="bot"):
            _run(bot.on_my_chat_member(upd, ctx))

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "-1009999" in joined
        assert "COGM Members" in joined

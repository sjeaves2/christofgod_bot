"""Tests for appointment reminder DMs (24h and 2h before confirmed appointments)."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

TZ = pytz.timezone("America/New_York")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _officials():
    return [{"id": "off1", "name": "Pastor Test", "chat_id": 999}]


def _appt(hours_ahead=48, status="confirmed", appt_id="R1", reminders=None):
    dt = (datetime.now(TZ) + timedelta(hours=hours_ahead)).replace(microsecond=0)
    a = {
        "id": appt_id, "user_chat_id": 111, "user_username": "req",
        "user_display_name": "John Doe", "official_id": "off1",
        "official_name": "Pastor Test",
        "requested_datetime": dt.isoformat(),
        "confirmed_datetime": dt.isoformat() if status == "confirmed" else None,
        "description": "chat", "status": status, "duration_minutes": 30,
    }
    if reminders is not None:
        a["reminders_sent"] = reminders
    return a


def _run_job(appts):
    import bot
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    saved: dict = {}

    async def _get():
        return appts

    async def _save(a):
        saved["appts"] = a

    async def _prefs(chat_id):
        return bot.TZ, bot.DEFAULT_LANG

    with patch("bot.get_appointments", side_effect=_get), \
         patch("bot.save_appointments", side_effect=_save), \
         patch("bot.get_user_prefs", side_effect=_prefs), \
         patch("bot.OFFICIALS", _officials()):
        _run(bot.appointment_reminder_job(ctx))
    return ctx, saved


class TestReminderTiming:
    def test_nothing_due_far_out(self):
        ctx, saved = _run_job([_appt(hours_ahead=48)])
        ctx.bot.send_message.assert_not_awaited()
        assert "appts" not in saved

    def test_24h_stage_sends_to_both_parties(self):
        ctx, saved = _run_job([_appt(hours_ahead=20)])
        sent_to = {c.args[0] for c in ctx.bot.send_message.await_args_list}
        assert sent_to == {111, 999}
        assert sorted(saved["appts"][0]["reminders_sent"]["24h"]) == [111, 999]

    def test_2h_stage_sends(self):
        ctx, saved = _run_job([_appt(hours_ahead=1.5, reminders={"24h": [111, 999]})])
        sent_to = {c.args[0] for c in ctx.bot.send_message.await_args_list}
        assert sent_to == {111, 999}
        assert sorted(saved["appts"][0]["reminders_sent"]["2h"]) == [111, 999]

    def test_close_booking_sends_only_closest_stage(self):
        # Booked 1h ahead: both stages due at once → only the 2h reminder is
        # sent; the 24h stage is marked superseded without messages.
        ctx, saved = _run_job([_appt(hours_ahead=1)])
        assert len(ctx.bot.send_message.await_args_list) == 2  # one per party
        rs = saved["appts"][0]["reminders_sent"]
        assert sorted(rs["24h"]) == [111, 999]  # superseded, not sent
        assert sorted(rs["2h"]) == [111, 999]

    def test_past_appointment_ignored(self):
        ctx, saved = _run_job([_appt(hours_ahead=-1)])
        ctx.bot.send_message.assert_not_awaited()


class TestReminderIdempotency:
    def test_already_sent_stage_not_resent(self):
        ctx, saved = _run_job(
            [_appt(hours_ahead=20, reminders={"24h": [111, 999]})])
        ctx.bot.send_message.assert_not_awaited()
        assert "appts" not in saved

    def test_partial_failure_retries_only_missing(self):
        # Official (999) already got the 24h reminder; only the user is sent.
        ctx, saved = _run_job([_appt(hours_ahead=20, reminders={"24h": [999]})])
        sent_to = [c.args[0] for c in ctx.bot.send_message.await_args_list]
        assert sent_to == [111]
        assert sorted(saved["appts"][0]["reminders_sent"]["24h"]) == [111, 999]

    def test_send_failure_leaves_recipient_pending(self):
        import bot
        from telegram.error import TelegramError

        appts = [_appt(hours_ahead=20)]
        ctx = MagicMock()
        ctx.bot = MagicMock()

        async def _fail_for_user(chat_id, *a, **k):
            if chat_id == 111:
                raise TelegramError("boom")

        ctx.bot.send_message = AsyncMock(side_effect=_fail_for_user)
        saved: dict = {}

        async def _get():
            return appts

        async def _save(a):
            saved["appts"] = a

        async def _prefs(chat_id):
            return bot.TZ, bot.DEFAULT_LANG

        with patch("bot.get_appointments", side_effect=_get), \
             patch("bot.save_appointments", side_effect=_save), \
             patch("bot.get_user_prefs", side_effect=_prefs), \
             patch("bot.OFFICIALS", _officials()):
            _run(bot.appointment_reminder_job(ctx))
        # Official recorded; user left pending for the next tick.
        assert saved["appts"][0]["reminders_sent"]["24h"] == [999]


class TestReminderScope:
    def test_pending_appointment_ignored(self):
        ctx, saved = _run_job([_appt(hours_ahead=20, status="pending")])
        ctx.bot.send_message.assert_not_awaited()

    def test_cancelled_appointment_ignored(self):
        ctx, saved = _run_job([_appt(hours_ahead=20, status="cancelled")])
        ctx.bot.send_message.assert_not_awaited()

    def test_reminder_mentions_counterparty(self):
        ctx, saved = _run_job([_appt(hours_ahead=20)])
        by_chat = {c.args[0]: c.args[1] for c in ctx.bot.send_message.await_args_list}
        assert "Pastor Test" in by_chat[111]   # user sees the official
        assert "John Doe" in by_chat[999]      # official sees the requester


class TestRearmOnReconfirm:
    def test_finalize_clears_reminder_state(self):
        import bot
        appt = _appt(hours_ahead=20, reminders={"24h": [111, 999]})
        appts = [appt]
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.send_document = AsyncMock()
        saved: dict = {}

        async def _save(a):
            saved["appts"] = a

        async def _prefs(chat_id):
            return bot.TZ, bot.DEFAULT_LANG

        with patch("bot.save_appointments", side_effect=_save), \
             patch("bot.get_user_prefs", side_effect=_prefs), \
             patch("bot.OFFICIALS", _officials()):
            _run(bot._finalize_appointment(ctx, appt, appts))
        assert "reminders_sent" not in saved["appts"][0]

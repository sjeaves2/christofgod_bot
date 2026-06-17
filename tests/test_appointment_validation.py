"""Tests for appointment request validation:

  1. No appointments in the past or more than 6 months ahead.
  2. Only one active appointment per official at a time.
"""

from __future__ import annotations

import asyncio
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


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.user_data = {}
    return ctx


def _make_update(text: str = "", chat_id: int = 111, username: str = "requester") -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = username
    upd.effective_user.first_name = "Test"
    upd.effective_user.full_name = "Test User"
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    return upd


def _officials() -> list:
    return [{"id": "off1", "name": "Pastor Test", "chat_id": 999}]


def _make_appt(user_chat_id=111, official_id="off1", status="pending", appt_id="EXIST1") -> dict:
    dt = (datetime.now(TZ) + timedelta(days=5)).replace(microsecond=0)
    return {
        "id": appt_id,
        "user_chat_id": user_chat_id,
        "user_username": "requester",
        "user_display_name": "Test User",
        "official_id": official_id,
        "official_name": "Pastor Test",
        "requested_datetime": dt.isoformat(),
        "confirmed_datetime": dt.isoformat() if status == "confirmed" else None,
        "description": "x",
        "status": status,
        "duration_minutes": 30,
    }


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_max_request_datetime_is_about_six_months_out(self):
        from bot import _max_request_datetime, now_tz
        max_dt = _max_request_datetime()
        now = now_tz()
        delta_days = (max_dt - now).days
        # 6 calendar months ≈ 181–184 days depending on month lengths
        assert 175 <= delta_days <= 190

    def test_active_appt_with_official_found(self):
        from bot import _active_appt_with_official
        appts = [_make_appt(status="pending")]
        assert _active_appt_with_official(appts, 111, "off1") is not None

    def test_active_appt_with_official_ignores_cancelled(self):
        from bot import _active_appt_with_official
        appts = [_make_appt(status="cancelled")]
        assert _active_appt_with_official(appts, 111, "off1") is None

    def test_active_appt_with_official_ignores_other_official(self):
        from bot import _active_appt_with_official
        appts = [_make_appt(official_id="off2")]
        assert _active_appt_with_official(appts, 111, "off1") is None

    def test_active_appt_with_official_ignores_other_user(self):
        from bot import _active_appt_with_official
        appts = [_make_appt(user_chat_id=222)]
        assert _active_appt_with_official(appts, 111, "off1") is None

    def test_confirmed_counts_as_active(self):
        from bot import _active_appt_with_official
        appts = [_make_appt(status="confirmed")]
        assert _active_appt_with_official(appts, 111, "off1") is not None


class TestOverlapHelper:
    def _appt_at(self, when: datetime, duration=30, user_chat_id=111,
                 official_id="off1", status="confirmed", appt_id="A1") -> dict:
        return {
            "id": appt_id,
            "user_chat_id": user_chat_id,
            "official_id": official_id,
            "official_name": "Pastor Test",
            "requested_datetime": when.isoformat(),
            "confirmed_datetime": when.isoformat(),
            "status": status,
            "duration_minutes": duration,
        }

    def test_exact_same_time_overlaps(self):
        from bot import _overlapping_appt
        t = TZ.localize(datetime(2099, 6, 1, 10, 0))
        appts = [self._appt_at(t)]
        assert _overlapping_appt(appts, 111, t, 30) is not None

    def test_partial_overlap_detected(self):
        from bot import _overlapping_appt
        existing = TZ.localize(datetime(2099, 6, 1, 10, 0))  # 10:00–10:30
        new = TZ.localize(datetime(2099, 6, 1, 10, 15))       # 10:15–10:45
        assert _overlapping_appt([self._appt_at(existing)], 111, new, 30) is not None

    def test_adjacent_back_to_back_does_not_overlap(self):
        from bot import _overlapping_appt
        existing = TZ.localize(datetime(2099, 6, 1, 10, 0))   # 10:00–10:30
        new = TZ.localize(datetime(2099, 6, 1, 10, 30))        # 10:30–11:00
        assert _overlapping_appt([self._appt_at(existing)], 111, new, 30) is None

    def test_non_overlapping_times_ok(self):
        from bot import _overlapping_appt
        existing = TZ.localize(datetime(2099, 6, 1, 10, 0))
        new = TZ.localize(datetime(2099, 6, 1, 14, 0))
        assert _overlapping_appt([self._appt_at(existing)], 111, new, 30) is None

    def test_overlap_across_different_officials(self):
        from bot import _overlapping_appt
        t = TZ.localize(datetime(2099, 6, 1, 10, 0))
        appts = [self._appt_at(t, official_id="off2")]
        assert _overlapping_appt(appts, 111, t, 30) is not None

    def test_other_users_overlap_ignored(self):
        from bot import _overlapping_appt
        t = TZ.localize(datetime(2099, 6, 1, 10, 0))
        appts = [self._appt_at(t, user_chat_id=222)]
        assert _overlapping_appt(appts, 111, t, 30) is None

    def test_cancelled_overlap_ignored(self):
        from bot import _overlapping_appt
        t = TZ.localize(datetime(2099, 6, 1, 10, 0))
        appts = [self._appt_at(t, status="cancelled")]
        assert _overlapping_appt(appts, 111, t, 30) is None

    def test_exclude_id_skips_self(self):
        from bot import _overlapping_appt
        t = TZ.localize(datetime(2099, 6, 1, 10, 0))
        appts = [self._appt_at(t, appt_id="SELF")]
        assert _overlapping_appt(appts, 111, t, 30, exclude_id="SELF") is None

    def test_longer_existing_duration_overlaps(self):
        from bot import _overlapping_appt
        existing = TZ.localize(datetime(2099, 6, 1, 10, 0))    # 10:00–11:00 (60 min)
        new = TZ.localize(datetime(2099, 6, 1, 10, 45))         # 10:45–11:15
        assert _overlapping_appt([self._appt_at(existing, duration=60)], 111, new, 30) is not None


# ---------------------------------------------------------------------------
# Rule 1: date window (validated in ap_time)
# ---------------------------------------------------------------------------

class TestDateWindow:
    def _run_ap_time(self, date_str: str, time_str: str, existing_appts=None):
        from bot import ap_time
        ctx = _make_context()
        ctx.user_data["ap_date"] = date_str
        upd = _make_update(text=time_str)

        async def _fake_get():
            return list(existing_appts or [])

        with patch("bot.get_appointments", side_effect=_fake_get), \
             patch("bot.OFFICIALS", _officials()):
            result = _run(ap_time(upd, ctx))
        return result, upd, ctx

    def test_future_date_accepted(self):
        from bot import AP_DESC
        future = (datetime.now(TZ) + timedelta(days=30)).strftime("%Y-%m-%d")
        result, _, ctx = self._run_ap_time(future, "10:00")
        assert result == AP_DESC
        assert ctx.user_data.get("ap_time") == "10:00"

    def test_past_date_rejected(self):
        from bot import AP_DATE
        past = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
        result, upd, ctx = self._run_ap_time(past, "10:00")
        assert result == AP_DATE
        assert "ap_time" not in ctx.user_data
        assert "past" in upd.message.reply_text.call_args[0][0].lower()

    def test_too_far_future_rejected(self):
        from bot import AP_DATE
        far = (datetime.now(TZ) + timedelta(days=400)).strftime("%Y-%m-%d")
        result, upd, ctx = self._run_ap_time(far, "10:00")
        assert result == AP_DATE
        assert "ap_time" not in ctx.user_data
        msg = upd.message.reply_text.call_args[0][0].lower()
        assert "6 months" in msg or "months ahead" in msg

    def test_just_inside_window_accepted(self):
        from bot import AP_DESC
        near = (datetime.now(TZ) + timedelta(days=150)).strftime("%Y-%m-%d")
        result, _, _ = self._run_ap_time(near, "10:00")
        assert result == AP_DESC

    def test_invalid_calendar_date_rejected(self):
        from bot import AP_DATE
        result, upd, ctx = self._run_ap_time("2027-13-40", "10:00")
        assert result == AP_DATE
        assert "ap_time" not in ctx.user_data
        assert "valid" in upd.message.reply_text.call_args[0][0].lower()

    def test_bad_time_format_stays_in_time(self):
        from bot import AP_TIME
        future = (datetime.now(TZ) + timedelta(days=30)).strftime("%Y-%m-%d")
        result, _, _ = self._run_ap_time(future, "9am")
        assert result == AP_TIME

    def test_overlapping_time_rejected(self):
        from bot import AP_DATE
        day = (datetime.now(TZ) + timedelta(days=30)).replace(
            hour=10, minute=0, second=0, microsecond=0)
        existing = {
            "id": "CLASH1", "user_chat_id": 111, "official_id": "off2",
            "official_name": "Bishop X",
            "requested_datetime": day.isoformat(),
            "confirmed_datetime": day.isoformat(),
            "status": "confirmed", "duration_minutes": 30,
        }
        result, upd, ctx = self._run_ap_time(
            day.strftime("%Y-%m-%d"), "10:15", existing_appts=[existing])
        assert result == AP_DATE
        assert "ap_time" not in ctx.user_data
        assert "overlap" in upd.message.reply_text.call_args[0][0].lower()

    def test_non_overlapping_time_accepted(self):
        from bot import AP_DESC
        day = (datetime.now(TZ) + timedelta(days=30)).replace(
            hour=10, minute=0, second=0, microsecond=0)
        existing = {
            "id": "OK1", "user_chat_id": 111, "official_id": "off2",
            "official_name": "Bishop X",
            "requested_datetime": day.isoformat(),
            "confirmed_datetime": day.isoformat(),
            "status": "confirmed", "duration_minutes": 30,
        }
        result, _, ctx = self._run_ap_time(
            day.strftime("%Y-%m-%d"), "14:00", existing_appts=[existing])
        assert result == AP_DESC


# ---------------------------------------------------------------------------
# Rule 2: one active appointment per official (checked in ap_official)
# ---------------------------------------------------------------------------

class TestOnePerOfficial:
    def _run_ap_official(self, appts, selection="1"):
        from bot import ap_official
        ctx = _make_context()
        upd = _make_update(text=selection, chat_id=111, username="requester")

        async def _fake_get():
            return appts

        with patch("bot.get_appointments", side_effect=_fake_get), \
             patch("bot.OFFICIALS", _officials()):
            result = _run(ap_official(upd, ctx))
        return result, upd, ctx

    def test_no_existing_appointment_proceeds(self):
        from bot import AP_DATE
        result, _, ctx = self._run_ap_official([])
        assert result == AP_DATE
        assert ctx.user_data.get("ap_official", {}).get("id") == "off1"

    def test_existing_active_appointment_blocks(self):
        from bot import ConversationHandler
        result, upd, ctx = self._run_ap_official([_make_appt(status="pending")])
        assert result == ConversationHandler.END
        assert "ap_official" not in ctx.user_data

    def test_block_message_mentions_cancel(self):
        result, upd, _ = self._run_ap_official([_make_appt(status="confirmed")])
        msg = upd.message.reply_text.call_args[0][0].lower()
        assert "cancelappointment" in msg

    def test_cancelled_appointment_does_not_block(self):
        from bot import AP_DATE
        result, _, _ = self._run_ap_official([_make_appt(status="cancelled")])
        assert result == AP_DATE

    def test_appointment_with_other_official_does_not_block(self):
        from bot import AP_DATE
        result, _, _ = self._run_ap_official([_make_appt(official_id="off2")])
        assert result == AP_DATE

    def test_invalid_selection_stays(self):
        from bot import AP_OFFICIAL
        result, _, _ = self._run_ap_official([], selection="9")
        assert result == AP_OFFICIAL


# ---------------------------------------------------------------------------
# Rule 2 final guard in ap_confirm
# ---------------------------------------------------------------------------

class TestConfirmGuard:
    def _run_confirm(self, existing_appts):
        from bot import ap_confirm
        ctx = _make_context()
        future = datetime.now(TZ) + timedelta(days=10)
        ctx.user_data.update({
            "ap_official": {"id": "off1", "name": "Pastor Test"},
            "ap_date": future.strftime("%Y-%m-%d"),
            "ap_time": "10:00",
            "ap_desc": "Test",
        })
        upd = _make_update(text="yes", chat_id=111, username="requester")

        saved = []

        async def _fake_get():
            return list(existing_appts)

        async def _fake_save(appts):
            saved.extend(appts)

        async def _fake_notify(context, appt, update):
            pass

        with patch("bot.get_appointments", side_effect=_fake_get), \
             patch("bot.save_appointments", side_effect=_fake_save), \
             patch("bot.OFFICIALS", _officials()), \
             patch("bot._notify_official_of_request", side_effect=_fake_notify):
            result = _run(ap_confirm(upd, ctx))
        return result, upd, saved

    def test_submits_when_no_conflict(self):
        from bot import ConversationHandler
        result, upd, saved = self._run_confirm([])
        assert result == ConversationHandler.END
        assert any(a["status"] == "pending" for a in saved)

    def test_blocks_when_conflict_appears(self):
        from bot import ConversationHandler
        result, upd, saved = self._run_confirm([_make_appt(status="pending")])
        assert result == ConversationHandler.END
        # Nothing new saved (save_appointments not called)
        assert saved == []
        assert "already have" in upd.message.reply_text.call_args[0][0].lower()

    def test_blocks_when_overlap_with_other_official_appears(self):
        from bot import ConversationHandler
        # Same 10:00 slot as the confirm flow's date/time, but a different official.
        future = datetime.now(TZ) + timedelta(days=10)
        slot = future.replace(hour=10, minute=0, second=0, microsecond=0)
        clash = {
            "id": "CLASH2", "user_chat_id": 111, "official_id": "off2",
            "official_name": "Bishop X",
            "requested_datetime": slot.isoformat(),
            "confirmed_datetime": slot.isoformat(),
            "status": "confirmed", "duration_minutes": 30,
        }
        result, upd, saved = self._run_confirm([clash])
        assert result == ConversationHandler.END
        assert saved == []
        assert "overlap" in upd.message.reply_text.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# Overlap guard on confirmation callbacks (confirm / accept_counter /
# accept_user_counter)
# ---------------------------------------------------------------------------

class TestCallbackOverlapGuard:
    def _slot(self, hour=10):
        return (datetime.now(TZ) + timedelta(days=10)).replace(
            hour=hour, minute=0, second=0, microsecond=0)

    def _target(self, appt_id="TARGET", **extra):
        slot = self._slot()
        appt = {
            "id": appt_id,
            "user_chat_id": 111,
            "user_username": "requester",
            "user_display_name": "Test User",
            "official_id": "off1",
            "official_name": "Pastor Test",
            "requested_datetime": slot.isoformat(),
            "confirmed_datetime": None,
            "description": "x",
            "status": "pending",
            "duration_minutes": 30,
        }
        appt.update(extra)
        return appt

    def _clash(self):
        # Same 10:00 slot, a different official → overlaps the target.
        slot = self._slot()
        return {
            "id": "CLASH", "user_chat_id": 111, "official_id": "off2",
            "official_name": "Bishop X",
            "requested_datetime": slot.isoformat(),
            "confirmed_datetime": slot.isoformat(),
            "status": "confirmed", "duration_minutes": 30,
        }

    def _run_callback(self, action, appts, appt_id="TARGET"):
        from bot import appt_callback, CB_APPT_PREFIX

        ctx = _make_context()
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.data = f"{CB_APPT_PREFIX}{action}:{appt_id}"
        query.message.chat_id = 999
        upd = MagicMock()
        upd.callback_query = query

        saved = []
        finalize = AsyncMock()

        async def _fake_get():
            return [a.copy() for a in appts]

        async def _fake_save(a):
            saved.extend(a)

        with patch("bot.get_appointments", side_effect=_fake_get), \
             patch("bot.save_appointments", side_effect=_fake_save), \
             patch("bot.OFFICIALS", _officials()), \
             patch("bot._finalize_appointment", finalize):
            _run(appt_callback(upd, ctx))
        return query, finalize

    # --- confirm ---

    def test_confirm_overlap_blocks_finalize(self):
        target = self._target()
        query, finalize = self._run_callback("confirm", [target, self._clash()])
        finalize.assert_not_called()
        assert "overlap" in query.edit_message_text.call_args[0][0].lower()

    def test_confirm_no_overlap_finalizes(self):
        target = self._target()
        query, finalize = self._run_callback("confirm", [target])
        finalize.assert_called_once()

    # --- accept_counter (user accepts official's suggestion) ---

    def test_accept_counter_overlap_blocks_finalize(self):
        slot = self._slot()
        target = self._target(status="counter_proposed",
                              counter_datetime=slot.isoformat())
        query, finalize = self._run_callback("accept_counter", [target, self._clash()])
        finalize.assert_not_called()
        assert "overlap" in query.edit_message_text.call_args[0][0].lower()

    def test_accept_counter_no_overlap_finalizes(self):
        slot = self._slot()
        target = self._target(status="counter_proposed",
                              counter_datetime=slot.isoformat())
        query, finalize = self._run_callback("accept_counter", [target])
        finalize.assert_called_once()

    # --- accept_user_counter (official accepts user's suggestion) ---

    def test_accept_user_counter_overlap_blocks_finalize(self):
        slot = self._slot()
        target = self._target(status="counter_proposed",
                              user_counter_datetime=slot.isoformat())
        query, finalize = self._run_callback("accept_user_counter", [target, self._clash()])
        finalize.assert_not_called()
        assert "overlap" in query.edit_message_text.call_args[0][0].lower()

    def test_accept_user_counter_no_overlap_finalizes(self):
        slot = self._slot()
        target = self._target(status="counter_proposed",
                              user_counter_datetime=slot.isoformat())
        query, finalize = self._run_callback("accept_user_counter", [target])
        finalize.assert_called_once()

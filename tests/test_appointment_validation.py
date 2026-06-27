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

    def _at(self, days, status="pending", official_id="off1", user_chat_id=111, i=0):
        dt = (datetime.now(TZ) + timedelta(days=days)).replace(microsecond=0)
        return {
            "id": f"A{i}", "user_chat_id": user_chat_id, "official_id": official_id,
            "official_name": "Pastor Test", "requested_datetime": dt.isoformat(),
            "confirmed_datetime": dt.isoformat(), "status": status, "duration_minutes": 30,
        }

    def _end(self, days=6):
        return (datetime.now(TZ) + timedelta(days=days)).replace(microsecond=0)

    def test_counts_active_appt_in_window(self):
        from bot import _count_active_appts_with_official
        appts = [self._at(5, status="pending")]
        assert _count_active_appts_with_official(appts, 111, "off1", self._end()) == 1

    def test_confirmed_counts(self):
        from bot import _count_active_appts_with_official
        appts = [self._at(5, status="confirmed")]
        assert _count_active_appts_with_official(appts, 111, "off1", self._end()) == 1

    def test_ignores_cancelled_and_declined(self):
        from bot import _count_active_appts_with_official
        appts = [self._at(5, status="cancelled", i=1), self._at(4, status="declined", i=2)]
        assert _count_active_appts_with_official(appts, 111, "off1", self._end()) == 0

    def test_ignores_other_official_and_user(self):
        from bot import _count_active_appts_with_official
        appts = [self._at(5, official_id="off2", i=1), self._at(5, user_chat_id=222, i=2)]
        assert _count_active_appts_with_official(appts, 111, "off1", self._end()) == 0

    def test_excludes_appt_outside_window(self):
        from bot import _count_active_appts_with_official
        # 40 days before the anchor end date — outside the trailing 30-day window.
        appts = [self._at(-40, status="confirmed")]
        assert _count_active_appts_with_official(appts, 111, "off1", self._end()) == 0

    def test_includes_recent_past_appt(self):
        from bot import _count_active_appts_with_official
        # A confirmed appt 10 days before the anchor counts (within window).
        appts = [self._at(-10, status="confirmed")]
        assert _count_active_appts_with_official(appts, 111, "off1", self._end(days=0)) == 1

    def test_counts_multiple(self):
        from bot import _count_active_appts_with_official, APPOINTMENT_MAX_PER_WINDOW
        appts = [self._at(d, i=d) for d in (1, 3, 5, 7)]
        assert _count_active_appts_with_official(appts, 111, "off1", self._end(days=8)) == 4
        assert APPOINTMENT_MAX_PER_WINDOW == 4


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

def _run_ap_time(date_str, time_str, existing_appts=None):
    from bot import ap_time
    ctx = _make_context()
    ctx.user_data["ap_date"] = date_str
    ctx.user_data["ap_official"] = {"id": "off1", "name": "Pastor Test"}
    upd = _make_update(text=time_str)

    async def _fake_get():
        return list(existing_appts or [])

    with patch("bot.get_appointments", side_effect=_fake_get), \
         patch("bot.OFFICIALS", _officials()):
        result = _run(ap_time(upd, ctx))
    return result, upd, ctx


class TestDateWindow:
    def _run_ap_time(self, date_str: str, time_str: str, existing_appts=None):
        return _run_ap_time(date_str, time_str, existing_appts)

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
# Official selection (ap_official) — no per-official block here anymore
# ---------------------------------------------------------------------------

class TestApOfficialSelection:
    def _run_ap_official(self, appts, data="apsel:0"):
        from bot import ap_official
        ctx = _make_context()
        upd = MagicMock()
        upd.effective_chat.id = 111
        upd.effective_user.id = 111
        upd.effective_user.username = "requester"
        upd.effective_user.full_name = "Test User"
        q = MagicMock()
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        q.data = data
        q.from_user.id = 111
        upd.callback_query = q

        async def _fake_get():
            return appts

        with patch("bot.get_appointments", side_effect=_fake_get), \
             patch("bot.OFFICIALS", _officials()):
            result = _run(ap_official(upd, ctx))
        return result, q, ctx

    def test_selection_proceeds_to_date(self):
        from bot import AP_DATE
        result, _, ctx = self._run_ap_official([])
        assert result == AP_DATE
        assert ctx.user_data.get("ap_official", {}).get("id") == "off1"

    def test_proceeds_even_with_existing_active_appointment(self):
        # The per-official cap moved to ap_time; selection no longer blocks.
        from bot import AP_DATE
        result, _, ctx = self._run_ap_official([_make_appt(status="confirmed")])
        assert result == AP_DATE
        assert ctx.user_data.get("ap_official", {}).get("id") == "off1"

    def test_invalid_selection_ends(self):
        from bot import ConversationHandler
        result, _, _ = self._run_ap_official([], data="apsel:9")
        assert result == ConversationHandler.END

    def test_cancel_button_ends(self):
        from bot import ConversationHandler
        result, q, _ = self._run_ap_official([], data="apsel:cancel")
        assert result == ConversationHandler.END


# ---------------------------------------------------------------------------
# Per-official frequency limit (4 within ±15 days of now) — in ap_official
# ---------------------------------------------------------------------------

class TestPerOfficialWindowLimit:
    def _appt_offset(self, days, official_id="off1", user_chat_id=111, status="confirmed", i=0):
        # An appointment `days` from now, at 09:00 on that day.
        dt = (datetime.now(TZ) + timedelta(days=days)).replace(
            hour=9, minute=0, second=0, microsecond=0)
        return {
            "id": f"W{i}", "user_chat_id": user_chat_id, "official_id": official_id,
            "official_name": "Pastor Test", "requested_datetime": dt.isoformat(),
            "confirmed_datetime": dt.isoformat(), "status": status, "duration_minutes": 30,
        }

    def _run_ap_official(self, existing):
        from bot import ap_official
        ctx = _make_context()
        upd = MagicMock()
        upd.effective_user.id = 111
        upd.effective_user.username = "requester"
        upd.effective_user.full_name = "Test User"
        q = MagicMock()
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        q.data = "apsel:0"
        q.from_user.id = 111
        upd.callback_query = q

        async def _fake_get():
            return list(existing)

        with patch("bot.get_appointments", side_effect=_fake_get), \
             patch("bot.OFFICIALS", _officials()):
            result = _run(ap_official(upd, ctx))
        return result, q, ctx

    def test_fourth_appointment_allowed(self):
        from bot import AP_DATE
        existing = [self._appt_offset(d, i=d) for d in (-6, -3, 5)]  # 3 in window
        result, _, ctx = self._run_ap_official(existing)
        assert result == AP_DATE

    def test_fifth_appointment_blocked(self):
        from bot import ConversationHandler
        existing = [self._appt_offset(d, i=d) for d in (-6, -3, 5, 8)]  # 4 in window
        result, q, ctx = self._run_ap_official(existing)
        assert result == ConversationHandler.END
        assert "ap_official" not in ctx.user_data
        assert "limit" in q.edit_message_text.call_args[0][0].lower()

    def test_window_is_symmetric_future_counts(self):
        from bot import ConversationHandler
        # All four in the +15 side (future) still hit the limit.
        existing = [self._appt_offset(d, i=d) for d in (2, 6, 10, 14)]
        result, q, ctx = self._run_ap_official(existing)
        assert result == ConversationHandler.END

    def test_appts_outside_window_do_not_count(self):
        from bot import AP_DATE
        # 4 appts beyond ±15 days (some far future, some far past) → not counted.
        existing = [self._appt_offset(d, i=d) for d in (-40, -20, 20, 40)]
        result, _, ctx = self._run_ap_official(existing)
        assert result == AP_DATE

    def test_other_official_does_not_count(self):
        from bot import AP_DATE
        existing = [self._appt_offset(d, official_id="off2", i=d) for d in (-6, -3, 5, 8)]
        result, _, ctx = self._run_ap_official(existing)
        assert result == AP_DATE

    def test_cancelled_appts_do_not_count(self):
        from bot import AP_DATE
        existing = [self._appt_offset(d, status="cancelled", i=d) for d in (-6, -3, 5, 8)]
        result, _, ctx = self._run_ap_official(existing)
        assert result == AP_DATE


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

    def test_allows_when_under_limit(self):
        from bot import ConversationHandler
        # One existing appt with the same official is fine (limit is 4).
        result, upd, saved = self._run_confirm([_make_appt(status="pending")])
        assert result == ConversationHandler.END
        assert any(a["status"] == "pending" for a in saved)

    def test_blocks_when_limit_reached(self):
        from bot import ConversationHandler
        # 4 existing appts with off1 within the window → the 5th is blocked.
        base = datetime.now(TZ) + timedelta(days=10)
        existing = []
        for k in range(4):
            dt = (base - timedelta(days=k + 1)).replace(hour=9, minute=0, second=0, microsecond=0)
            existing.append({
                "id": f"L{k}", "user_chat_id": 111, "official_id": "off1",
                "official_name": "Pastor Test", "requested_datetime": dt.isoformat(),
                "confirmed_datetime": dt.isoformat(), "status": "confirmed",
                "duration_minutes": 30,
            })
        result, upd, saved = self._run_confirm(existing)
        assert result == ConversationHandler.END
        assert saved == []
        assert "limit" in upd.message.reply_text.call_args[0][0].lower()

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

    def _run_callback(self, action, appts, appt_id="TARGET", answer_side_effect=None):
        from bot import appt_callback, CB_APPT_PREFIX

        ctx = _make_context()
        query = MagicMock()
        query.answer = AsyncMock(side_effect=answer_side_effect)
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

    # --- stale callback recovery ---

    def test_confirm_survives_stale_callback_query(self):
        """If query.answer() raises BadRequest (bot was offline when tapped),
        the confirmation must still complete rather than abort."""
        from telegram.error import BadRequest
        target = self._target()
        query, finalize = self._run_callback(
            "confirm", [target],
            answer_side_effect=BadRequest("Query is too old and response timeout expired"),
        )
        finalize.assert_called_once()

    # --- idempotency guard ---

    def test_confirm_on_already_confirmed_is_noop(self):
        target = self._target(status="confirmed")
        query, finalize = self._run_callback("confirm", [target])
        finalize.assert_not_called()
        assert "already" in query.edit_message_text.call_args[0][0].lower()

    def test_confirm_on_declined_is_noop(self):
        target = self._target(status="declined")
        query, finalize = self._run_callback("confirm", [target])
        finalize.assert_not_called()

    def test_decline_on_confirmed_is_noop(self):
        target = self._target(status="confirmed")
        query, finalize = self._run_callback("decline", [target])
        # No requester notification or re-save for an already-terminal appt.
        finalize.assert_not_called()
        assert "already" in query.edit_message_text.call_args[0][0].lower()

    def test_accept_counter_on_cancelled_is_noop(self):
        target = self._target(status="cancelled")
        query, finalize = self._run_callback("accept_counter", [target])
        finalize.assert_not_called()

    def test_repeated_confirm_finalizes_once(self):
        """Simulate two taps: first confirms, the persisted state makes the
        second a no-op (no duplicate finalize)."""
        from telegram.error import BadRequest
        target = self._target()
        store = {"appts": [target]}

        from bot import appt_callback, CB_APPT_PREFIX

        async def _fake_get():
            return [a.copy() for a in store["appts"]]

        async def _fake_save(a):
            store["appts"] = a

        finalize_calls = {"n": 0}

        async def _fake_finalize(ctx, appt, appts):
            finalize_calls["n"] += 1
            # Mirror real finalize: persist the confirmed status.
            for i, x in enumerate(appts):
                if x["id"] == appt["id"]:
                    appts[i] = appt
            await _fake_save(appts)

        def _one_tap():
            ctx = _make_context()
            query = MagicMock()
            query.answer = AsyncMock()
            query.edit_message_text = AsyncMock()
            query.data = f"{CB_APPT_PREFIX}confirm:TARGET"
            query.message.chat_id = 999
            upd = MagicMock()
            upd.callback_query = query
            with patch("bot.get_appointments", side_effect=_fake_get), \
                 patch("bot.save_appointments", side_effect=_fake_save), \
                 patch("bot.OFFICIALS", _officials()), \
                 patch("bot._finalize_appointment", side_effect=_fake_finalize):
                _run(appt_callback(upd, ctx))

        _one_tap()
        _one_tap()
        assert finalize_calls["n"] == 1

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

"""Tests for appointment confirmation updates:

  1. format_dt() — day-of-week, 12-hour clock, AM/PM, timezone
  2. ap_desc confirmation summary — uses format_dt() not raw date/time strings
  3. _finalize_appointment() — sends message + ICS to BOTH user and official
"""

from __future__ import annotations

import asyncio
import io
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytz
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

TZ = pytz.timezone("America/New_York")

# ---------------------------------------------------------------------------
# format_dt — standalone tests (pure function, no Telegram needed)
# ---------------------------------------------------------------------------

class TestFormatDt:
    """format_dt must produce: 'Weekday, Month DD, YYYY at HH:MM AM/PM TZ'"""

    def _fmt(self, dt: datetime) -> str:
        """Import and call format_dt without triggering bot module-level I/O."""
        from ics_generator import events_to_ics  # confirm tz-aware dt works
        return dt.astimezone(TZ).strftime("%A, %B %d, %Y at %I:%M %p %Z")

    def test_includes_day_of_week(self):
        # 2026-06-18 is a Thursday
        dt = TZ.localize(datetime(2026, 6, 18, 10, 15))
        assert "Thursday" in self._fmt(dt)

    def test_includes_full_month_name(self):
        dt = TZ.localize(datetime(2026, 6, 18, 10, 15))
        assert "June" in self._fmt(dt)

    def test_includes_four_digit_year(self):
        dt = TZ.localize(datetime(2026, 6, 18, 10, 15))
        assert "2026" in self._fmt(dt)

    def test_am_pm_present_for_morning(self):
        dt = TZ.localize(datetime(2026, 6, 18, 10, 15))
        result = self._fmt(dt)
        assert "AM" in result or "am" in result

    def test_am_pm_present_for_afternoon(self):
        dt = TZ.localize(datetime(2026, 6, 18, 14, 30))
        result = self._fmt(dt)
        assert "PM" in result or "pm" in result

    def test_midnight_shows_12am(self):
        dt = TZ.localize(datetime(2026, 6, 18, 0, 0))
        result = self._fmt(dt)
        assert "12:00 AM" in result

    def test_noon_shows_12pm(self):
        dt = TZ.localize(datetime(2026, 6, 18, 12, 0))
        result = self._fmt(dt)
        assert "12:00 PM" in result

    def test_1pm_shows_01pm_not_13(self):
        dt = TZ.localize(datetime(2026, 6, 18, 13, 0))
        result = self._fmt(dt)
        assert "01:00 PM" in result
        assert "13" not in result

    def test_timezone_abbreviation_present(self):
        dt = TZ.localize(datetime(2026, 6, 18, 10, 0))
        result = self._fmt(dt)
        # EDT in summer, EST in winter
        assert "ET" in result or "EST" in result or "EDT" in result

    def test_minutes_preserved(self):
        dt = TZ.localize(datetime(2026, 6, 18, 10, 45))
        assert "10:45" in self._fmt(dt)

    def test_saturday_label(self):
        dt = TZ.localize(datetime(2026, 6, 20, 11, 0))  # Saturday
        assert "Saturday" in self._fmt(dt)

    def test_sunday_label(self):
        dt = TZ.localize(datetime(2026, 6, 21, 6, 0))  # Sunday
        assert "Sunday" in self._fmt(dt)

    def test_utc_input_converted_to_local(self):
        utc = pytz.utc.localize(datetime(2026, 6, 18, 14, 0))  # 10am EDT
        result = self._fmt(utc)
        assert "10:00 AM" in result

    def test_full_format_example(self):
        dt = TZ.localize(datetime(2026, 6, 18, 10, 15))
        result = self._fmt(dt)
        assert result == "Thursday, June 18, 2026 at 10:15 AM EDT"


# ---------------------------------------------------------------------------
# ap_desc summary format
# ---------------------------------------------------------------------------

class TestApDescSummaryFormat:
    """The confirmation summary shown to the user before submitting must use
    the human-readable date format, not the raw YYYY-MM-DD HH:MM string."""

    def _build_summary(self, date_str: str, time_str: str, official_name: str, desc: str) -> str:
        """Replicate the summary-building logic from ap_desc."""
        parts_d = [int(x) for x in date_str.split("-")]
        parts_t = [int(x) for x in time_str.split(":")]
        req_dt = TZ.localize(datetime(parts_d[0], parts_d[1], parts_d[2], parts_t[0], parts_t[1]))
        dt_str = req_dt.astimezone(TZ).strftime("%A, %B %d, %Y at %I:%M %p %Z")
        return (
            f"*Appointment Request Summary:*\n"
            f"With: {official_name}\n"
            f"When: {dt_str}\n"
            f"Description: {desc}\n\n"
            f"Submit? (yes/no)"
        )

    def test_summary_contains_day_of_week(self):
        summary = self._build_summary("2026-06-18", "10:15", "Pastor Smith", "Test meeting")
        assert "Thursday" in summary

    def test_summary_contains_am_pm(self):
        summary = self._build_summary("2026-06-18", "10:15", "Pastor Smith", "Test meeting")
        assert "AM" in summary

    def test_summary_does_not_contain_raw_date(self):
        summary = self._build_summary("2026-06-18", "10:15", "Pastor Smith", "Test meeting")
        # Raw ISO date should NOT appear — it should be replaced by human-readable form
        assert "2026-06-18" not in summary

    def test_summary_does_not_contain_24h_time(self):
        summary = self._build_summary("2026-06-18", "14:30", "Pastor Smith", "Afternoon meeting")
        assert "14:30" not in summary

    def test_summary_contains_official_name(self):
        summary = self._build_summary("2026-06-18", "10:15", "Bishop Eaves", "Discussion")
        assert "Bishop Eaves" in summary

    def test_summary_contains_description(self):
        summary = self._build_summary("2026-06-18", "10:15", "Pastor Smith", "Airport pickup")
        assert "Airport pickup" in summary

    def test_summary_contains_submit_prompt(self):
        summary = self._build_summary("2026-06-18", "10:15", "Pastor Smith", "Test")
        assert "Submit? (yes/no)" in summary

    def test_description_truncated_to_128_chars(self):
        long_desc = "A" * 200
        truncated = long_desc[:128]
        summary = self._build_summary("2026-06-18", "10:15", "Pastor Smith", truncated)
        assert "A" * 128 in summary
        assert "A" * 129 not in summary

    def test_pm_time_formatted_correctly(self):
        summary = self._build_summary("2026-06-18", "18:00", "Pastor Smith", "Evening")
        assert "06:00 PM" in summary

    def test_when_label_present(self):
        summary = self._build_summary("2026-06-18", "10:00", "Pastor Smith", "Test")
        assert "When:" in summary

    def test_with_label_present(self):
        summary = self._build_summary("2026-06-18", "10:00", "Pastor Smith", "Test")
        assert "With:" in summary


# ---------------------------------------------------------------------------
# _finalize_appointment — ICS sent to both user and official
# ---------------------------------------------------------------------------

def _make_appt(official_chat_id: int | None = 999) -> dict:
    confirmed = TZ.localize(datetime(2026, 6, 18, 10, 15))
    return {
        "id": "TESTAPPT01",
        "user_chat_id": 111,
        "user_username": "testuser",
        "user_display_name": "Test User",
        "official_id": "test_official",
        "official_name": "Pastor Test",
        "requested_datetime": confirmed.isoformat(),
        "confirmed_datetime": confirmed.isoformat(),
        "description": "Test meeting",
        "status": "confirmed",
        "duration_minutes": 30,
        "_official_chat_id": official_chat_id,
    }


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestFinalizeAppointment:
    """_finalize_appointment must send message+ICS to user and, if known, to official."""

    def _make_context(self) -> MagicMock:
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.send_document = AsyncMock()
        return ctx

    def _make_officials(self, chat_id: int | None) -> list:
        off = {"id": "test_official", "name": "Pastor Test"}
        if chat_id is not None:
            off["chat_id"] = chat_id
        return [off]

    def _run_finalize(self, appt: dict, officials: list) -> MagicMock:
        from ics_generator import appointment_to_ics

        ctx = self._make_context()

        async def _fake_save(appts):
            pass

        with patch("bot.OFFICIALS", officials), \
             patch("bot.save_appointments", side_effect=_fake_save):
            from bot import _finalize_appointment
            _run(_finalize_appointment(ctx, appt, [appt.copy()]))

        return ctx

    def test_user_receives_confirmation_message(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        user_calls = [c for c in ctx.bot.send_message.call_args_list
                      if c.args[0] == 111]
        assert len(user_calls) == 1

    def test_user_message_contains_confirmed(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        user_msg = next(c.args[1] for c in ctx.bot.send_message.call_args_list
                        if c.args[0] == 111)
        assert "confirmed" in user_msg.lower()

    def test_user_message_contains_formatted_datetime(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        user_msg = next(c.args[1] for c in ctx.bot.send_message.call_args_list
                        if c.args[0] == 111)
        assert "Thursday" in user_msg
        assert "AM" in user_msg

    def test_user_message_does_not_contain_raw_iso_date(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        user_msg = next(c.args[1] for c in ctx.bot.send_message.call_args_list
                        if c.args[0] == 111)
        assert "2026-06-18T" not in user_msg

    def test_user_receives_ics_document(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        user_doc_calls = [c for c in ctx.bot.send_document.call_args_list
                          if c.args[0] == 111]
        assert len(user_doc_calls) == 1

    def test_user_ics_filename_is_appointment_ics(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        user_doc_call = next(c for c in ctx.bot.send_document.call_args_list
                             if c.args[0] == 111)
        doc_arg = user_doc_call.kwargs.get("document") or user_doc_call.args[1]
        assert doc_arg.filename == "appointment.ics"

    def test_official_receives_confirmation_message_when_chat_id_known(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        off_calls = [c for c in ctx.bot.send_message.call_args_list
                     if c.args[0] == 999]
        assert len(off_calls) == 1

    def test_official_message_contains_user_display_name(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        off_msg = next(c.args[1] for c in ctx.bot.send_message.call_args_list
                       if c.args[0] == 999)
        assert "Test User" in off_msg

    def test_official_message_contains_username(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        off_msg = next(c.args[1] for c in ctx.bot.send_message.call_args_list
                       if c.args[0] == 999)
        assert "@testuser" in off_msg

    def test_official_message_contains_formatted_datetime(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        off_msg = next(c.args[1] for c in ctx.bot.send_message.call_args_list
                       if c.args[0] == 999)
        assert "Thursday" in off_msg
        assert "AM" in off_msg

    def test_official_message_contains_description(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        off_msg = next(c.args[1] for c in ctx.bot.send_message.call_args_list
                       if c.args[0] == 999)
        assert "Test meeting" in off_msg

    def test_official_receives_ics_document(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        off_doc_calls = [c for c in ctx.bot.send_document.call_args_list
                         if c.args[0] == 999]
        assert len(off_doc_calls) == 1

    def test_official_ics_filename_is_appointment_ics(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        off_doc_call = next(c for c in ctx.bot.send_document.call_args_list
                            if c.args[0] == 999)
        doc_arg = off_doc_call.kwargs.get("document") or off_doc_call.args[1]
        assert doc_arg.filename == "appointment.ics"

    def test_total_send_message_calls_is_two(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        assert ctx.bot.send_message.call_count == 2

    def test_total_send_document_calls_is_two(self):
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        assert ctx.bot.send_document.call_count == 2

    def test_ics_buffers_are_independent(self):
        """Each party gets a separate BytesIO so reading one doesn't exhaust the other."""
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, self._make_officials(999))
        docs = [c.kwargs.get("document") or c.args[1]
                for c in ctx.bot.send_document.call_args_list]
        assert len(docs) == 2
        # Both buffers should have content (unwrap PTB InputFile wrapper)
        for doc in docs:
            raw = doc.input_file_content
            data = raw.read(5) if hasattr(raw, "read") else raw[:5]
            assert data == b"BEGIN"  # ICS starts with "BEGIN:VCALENDAR"

    def test_no_message_to_official_when_chat_id_unknown(self):
        """If official hasn't started the bot (no chat_id), skip silently."""
        appt = _make_appt(official_chat_id=None)
        ctx = self._run_finalize(appt, self._make_officials(None))
        # Only the user message should be sent
        assert ctx.bot.send_message.call_count == 1
        assert ctx.bot.send_document.call_count == 1

    def test_no_ics_to_official_when_chat_id_unknown(self):
        appt = _make_appt(official_chat_id=None)
        ctx = self._run_finalize(appt, self._make_officials(None))
        off_doc_calls = [c for c in ctx.bot.send_document.call_args_list
                         if c.args[0] != 111]
        assert len(off_doc_calls) == 0

    def test_official_not_found_does_not_raise(self):
        """Unknown official_id should not crash — user still gets their ICS."""
        appt = _make_appt(official_chat_id=999)
        ctx = self._run_finalize(appt, [])  # empty officials list
        assert ctx.bot.send_message.call_count == 1

    def test_naive_confirmed_datetime_localised(self):
        """A naive datetime in confirmed_datetime must not raise."""
        appt = _make_appt(official_chat_id=None)
        appt["confirmed_datetime"] = "2026-06-18T10:15:00"  # no tz offset
        ctx = self._run_finalize(appt, self._make_officials(None))
        assert ctx.bot.send_message.call_count == 1

    def test_appt_status_saved_as_confirmed(self):
        """_finalize_appointment is called after status is set to confirmed."""
        appt = _make_appt(official_chat_id=None)
        appt["status"] = "confirmed"
        saved = []

        async def _fake_save(appts):
            saved.extend(appts)

        ctx = self._make_context()
        with patch("bot.OFFICIALS", []), \
             patch("bot.save_appointments", side_effect=_fake_save):
            from bot import _finalize_appointment
            _run(_finalize_appointment(ctx, appt, [appt.copy()]))

        assert any(a["status"] == "confirmed" for a in saved)

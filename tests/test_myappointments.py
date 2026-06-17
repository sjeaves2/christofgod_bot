"""Tests for /myappointments — each party sees the appointments they're part of."""

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


def _make_appt(
    appt_id: str = "APPT001",
    status: str = "confirmed",
    user_chat_id: int = 111,
    user_username: str = "requester",
    user_display_name: str = "Test Requester",
    official_id: str = "off1",
    official_name: str = "Pastor Test",
    when: datetime | None = None,
) -> dict:
    dt = when or (datetime.now(TZ) + timedelta(days=7)).replace(microsecond=0)
    return {
        "id": appt_id,
        "user_chat_id": user_chat_id,
        "user_username": user_username,
        "user_display_name": user_display_name,
        "official_id": official_id,
        "official_name": official_name,
        "requested_datetime": dt.isoformat(),
        "confirmed_datetime": dt.isoformat(),
        "description": "Test meeting",
        "status": status,
        "duration_minutes": 30,
    }


def _make_officials(chat_id: int | None = 999, username: str | None = None) -> list:
    off = {"id": "off1", "name": "Pastor Test"}
    if chat_id is not None:
        off["chat_id"] = chat_id
    if username is not None:
        off["telegram_username"] = username
    return [off]


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    return ctx


def _make_update(chat_id: int = 111, username: str = "requester") -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = username
    upd.effective_user.first_name = "Test"
    upd.effective_user.full_name = "Test User"
    upd.message.reply_text = AsyncMock()
    return upd


def _run_cmd(appts, officials, chat_id=111, username="requester"):
    from bot import cmd_myappointments

    ctx = _make_context()
    upd = _make_update(chat_id=chat_id, username=username)

    async def _fake_get():
        return appts

    with patch("bot.get_appointments", side_effect=_fake_get), \
         patch("bot.OFFICIALS", officials):
        _run(cmd_myappointments(upd, ctx))
    return upd.message.reply_text.call_args[0][0]


class TestMyAppointments:

    def test_no_appointments_message(self):
        text = _run_cmd([], _make_officials())
        assert "no appointments" in text.lower()

    def test_requester_sees_their_appointment(self):
        appt = _make_appt(user_chat_id=111)
        text = _run_cmd([appt], _make_officials(), chat_id=111)
        assert "APPT001" in text

    def test_requester_sees_official_as_counterparty(self):
        appt = _make_appt(user_chat_id=111, official_name="Pastor Test")
        text = _run_cmd([appt], _make_officials(), chat_id=111)
        assert "Pastor Test" in text

    def test_requester_does_not_see_others_appointments(self):
        mine = _make_appt(appt_id="MINE", user_chat_id=111)
        other = _make_appt(appt_id="OTHER", user_chat_id=222)
        text = _run_cmd([mine, other], _make_officials(), chat_id=111)
        assert "MINE" in text
        assert "OTHER" not in text

    def test_official_sees_assigned_appointment_by_chat_id(self):
        appt = _make_appt(user_chat_id=111, official_id="off1")
        text = _run_cmd([appt], _make_officials(chat_id=999), chat_id=999, username="pastor")
        assert "APPT001" in text

    def test_official_sees_requester_as_counterparty(self):
        appt = _make_appt(user_chat_id=111, user_display_name="Jane Doe",
                          user_username="janed", official_id="off1")
        text = _run_cmd([appt], _make_officials(chat_id=999), chat_id=999, username="pastor")
        assert "Jane Doe" in text
        assert "@janed" in text

    def test_official_matched_by_telegram_username(self):
        appt = _make_appt(user_chat_id=111, official_id="off1")
        officials = _make_officials(chat_id=None, username="OneMorah")
        text = _run_cmd([appt], officials, chat_id=555, username="OneMorah")
        assert "APPT001" in text

    def test_all_statuses_shown(self):
        confirmed = _make_appt(appt_id="C1", status="confirmed", user_chat_id=111)
        cancelled = _make_appt(appt_id="X1", status="cancelled", user_chat_id=111)
        declined = _make_appt(appt_id="D1", status="declined", user_chat_id=111)
        text = _run_cmd([confirmed, cancelled, declined], _make_officials(), chat_id=111)
        assert "C1" in text and "X1" in text and "D1" in text

    def test_status_label_present(self):
        appt = _make_appt(status="cancelled", user_chat_id=111)
        text = _run_cmd([appt], _make_officials(), chat_id=111)
        assert "cancelled" in text

    def test_upcoming_and_past_sections(self):
        future = _make_appt(appt_id="FUT", user_chat_id=111,
                            when=(datetime.now(TZ) + timedelta(days=10)).replace(microsecond=0))
        old = _make_appt(appt_id="OLD", user_chat_id=111,
                         when=(datetime.now(TZ) - timedelta(days=10)).replace(microsecond=0))
        text = _run_cmd([future, old], _make_officials(), chat_id=111)
        assert "*Upcoming:*" in text
        assert "*Past:*" in text

    def test_only_upcoming_section_when_no_past(self):
        future = _make_appt(appt_id="FUT", user_chat_id=111,
                            when=(datetime.now(TZ) + timedelta(days=10)).replace(microsecond=0))
        text = _run_cmd([future], _make_officials(), chat_id=111)
        assert "*Upcoming:*" in text
        assert "*Past:*" not in text

    def test_formatted_datetime_shown(self):
        appt = _make_appt(user_chat_id=111,
                          when=TZ.localize(datetime(2099, 6, 18, 10, 15)))
        text = _run_cmd([appt], _make_officials(), chat_id=111)
        # format_dt output, e.g. "Thursday, June 18, 2099 at 10:15 AM EDT"
        assert "June 18, 2099" in text
        assert "10:15" in text

    def test_user_who_is_both_party_sees_appointment_once(self):
        # Official chat_id == requester chat_id (edge case)
        appt = _make_appt(user_chat_id=999, official_id="off1")
        text = _run_cmd([appt], _make_officials(chat_id=999), chat_id=999, username="pastor")
        assert text.count("APPT001") == 1

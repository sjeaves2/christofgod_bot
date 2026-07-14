"""Tests for /appointment request-rate limiting (option E):

  - a 2-minute cooldown measured from the user's last appointment action
    (create / confirm / cancel / reschedule), and
  - a cap of 5 outstanding pending requests,

both enforced in ap_confirm and both exempting admins.
"""

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


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.user_data = {}
    return ctx


def _make_update(text: str = "yes", chat_id: int = 111,
                 username: str = "requester") -> MagicMock:
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


def _appt(user_chat_id=111, status="pending", appt_id="A1",
          last_action_at=None, days=5) -> dict:
    dt = (datetime.now(TZ) + timedelta(days=days)).replace(microsecond=0)
    a = {
        "id": appt_id, "user_chat_id": user_chat_id, "user_username": "requester",
        "user_display_name": "Test User", "official_id": "off1",
        "official_name": "Pastor Test", "requested_datetime": dt.isoformat(),
        "confirmed_datetime": dt.isoformat() if status == "confirmed" else None,
        "description": "x", "status": status, "duration_minutes": 30,
    }
    if last_action_at is not None:
        a["last_action_at"] = last_action_at
    return a


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestLastActionHelper:
    def test_none_when_no_stamp(self):
        import bot
        assert bot._user_last_action_at([_appt()], 111) is None

    def test_returns_most_recent(self):
        import bot
        older = (bot.now_tz() - timedelta(minutes=30)).isoformat()
        newer = (bot.now_tz() - timedelta(minutes=1)).isoformat()
        appts = [_appt(appt_id="A1", last_action_at=older),
                 _appt(appt_id="A2", last_action_at=newer)]
        latest = bot._user_last_action_at(appts, 111)
        assert latest is not None
        assert abs((bot.now_tz() - latest).total_seconds() - 60) < 5

    def test_ignores_other_users(self):
        import bot
        appts = [_appt(user_chat_id=222, appt_id="A1",
                       last_action_at=bot.now_tz().isoformat())]
        assert bot._user_last_action_at(appts, 111) is None

    def test_tolerates_bad_timestamp(self):
        import bot
        assert bot._user_last_action_at(
            [_appt(last_action_at="not-a-date")], 111) is None


class TestPendingCount:
    def test_counts_only_pending_for_user(self):
        import bot
        appts = [
            _appt(appt_id="A1", status="pending"),
            _appt(appt_id="A2", status="confirmed"),
            _appt(appt_id="A3", status="pending", user_chat_id=222),
        ]
        assert bot._count_pending_appts(appts, 111) == 1


class TestStamp:
    def test_stamp_sets_iso_timestamp(self):
        import bot
        a = _appt()
        bot._stamp_appt_action(a)
        parsed = datetime.fromisoformat(a["last_action_at"])
        assert abs((bot.now_tz() - parsed).total_seconds()) < 5


# ---------------------------------------------------------------------------
# ap_confirm enforcement
# ---------------------------------------------------------------------------

class TestConfirmRateLimit:
    def _run_confirm(self, existing, chat_id=111, is_admin=False):
        import bot
        ctx = _make_context()
        future = datetime.now(TZ) + timedelta(days=10)
        ctx.user_data.update({
            "ap_official": {"id": "off1", "name": "Pastor Test"},
            "ap_date": future.strftime("%Y-%m-%d"),
            "ap_time": "10:00",
            "ap_desc": "Test",
        })
        upd = _make_update(chat_id=chat_id)
        saved: list = []

        async def _fake_get():
            return list(existing)

        async def _fake_save(appts):
            saved.clear()
            saved.extend(appts)

        async def _fake_notify(context, appt, update):
            pass

        with patch("bot.get_appointments", side_effect=_fake_get), \
             patch("bot.save_appointments", side_effect=_fake_save), \
             patch("bot.OFFICIALS", _officials()), \
             patch("bot.is_admin", return_value=is_admin), \
             patch("bot._notify_official_of_request", side_effect=_fake_notify):
            result = _run(bot.ap_confirm(upd, ctx))
        return result, upd, saved

    # -- cooldown --------------------------------------------------------

    def test_cooldown_blocks_recent_action(self):
        import bot
        recent = (bot.now_tz() - timedelta(seconds=30)).isoformat()
        result, upd, saved = self._run_confirm(
            [_appt(appt_id="A1", status="cancelled", last_action_at=recent)])
        assert result == bot.ConversationHandler.END
        assert saved == []  # nothing new submitted
        assert "wait" in upd.message.reply_text.call_args[0][0].lower()

    def test_cooldown_allows_after_window(self):
        import bot
        old = (bot.now_tz() - timedelta(seconds=bot.APPOINTMENT_COOLDOWN_SECONDS + 5)).isoformat()
        result, upd, saved = self._run_confirm(
            [_appt(appt_id="A1", status="cancelled", last_action_at=old)])
        assert result == bot.ConversationHandler.END
        assert any(a["status"] == "pending" for a in saved)

    def test_no_stamp_means_no_cooldown(self):
        result, upd, saved = self._run_confirm([])
        assert any(a["status"] == "pending" for a in saved)

    # -- pending cap -----------------------------------------------------

    def test_pending_cap_blocks_at_five(self):
        import bot
        old = (bot.now_tz() - timedelta(hours=1)).isoformat()
        # Placed >15 days out so they count toward the pending cap but not the
        # separate per-official density window (±15 days).
        existing = [_appt(appt_id=f"P{k}", status="pending",
                          last_action_at=old, days=20 + k) for k in range(5)]
        result, upd, saved = self._run_confirm(existing)
        assert result == bot.ConversationHandler.END
        assert saved == []
        assert "pending" in upd.message.reply_text.call_args[0][0].lower()

    def test_pending_cap_allows_under_five(self):
        import bot
        old = (bot.now_tz() - timedelta(hours=1)).isoformat()
        existing = [_appt(appt_id=f"P{k}", status="pending",
                          last_action_at=old, days=20 + k) for k in range(4)]
        result, upd, saved = self._run_confirm(existing)
        assert any(a["status"] == "pending" for a in saved)

    # -- admin exemption -------------------------------------------------

    def test_admin_exempt_from_cooldown(self):
        import bot
        recent = (bot.now_tz() - timedelta(seconds=10)).isoformat()
        result, upd, saved = self._run_confirm(
            [_appt(appt_id="A1", status="cancelled", last_action_at=recent)],
            is_admin=True)
        assert any(a["status"] == "pending" for a in saved)

    def test_admin_exempt_from_pending_cap(self):
        import bot
        old = (bot.now_tz() - timedelta(hours=1)).isoformat()
        existing = [_appt(appt_id=f"P{k}", status="pending",
                          last_action_at=old, days=20 + k) for k in range(5)]
        result, upd, saved = self._run_confirm(existing, is_admin=True)
        assert any(a["status"] == "pending" for a in saved)

    # -- new request is stamped -----------------------------------------

    def test_new_request_is_stamped(self):
        result, upd, saved = self._run_confirm([])
        new = next(a for a in saved if a["status"] == "pending")
        assert "last_action_at" in new

"""Tests for appointment proxies (secretaries who negotiate on an official's behalf)."""

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


def _official(enabled=True, proxy_chat=222):
    return {
        "id": "off1", "name": "Pastor Test", "telegram_username": "pastor",
        "chat_id": 999, "proxies_enabled": enabled,
        "proxies": [{"name": "Jane Sec", "telegram_username": "janesec", "chat_id": proxy_chat}],
    }


def _appt(appt_id="A1", status="pending", **extra):
    dt = datetime.now(TZ) + timedelta(days=3)
    a = {
        "id": appt_id, "user_chat_id": 111, "user_username": "req",
        "user_display_name": "John Doe", "official_id": "off1",
        "official_name": "Pastor Test", "requested_datetime": dt.isoformat(),
        "confirmed_datetime": None, "description": "chat", "status": status,
        "duration_minutes": 30,
    }
    a.update(extra)
    return a


def _cb_update(data, from_id, from_username=None):
    upd = MagicMock()
    q = MagicMock()
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.data = data
    q.from_user.id = from_id
    q.from_user.username = from_username
    q.message.chat_id = from_id
    upd.callback_query = q
    return upd, q


def _ctx():
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_document = AsyncMock()
    ctx.bot.send_photo = AsyncMock()
    ctx.bot.get_user_profile_photos = AsyncMock(return_value=MagicMock(photos=[]))
    return ctx


# ---------------------------------------------------------------------------
# Identity / authorization helpers
# ---------------------------------------------------------------------------

class TestProxyIdentity:
    def test_official_can_act(self):
        import bot
        off = _official()
        assert bot._user_can_act_for_official(off, 999, "pastor")

    def test_enabled_proxy_can_act(self):
        import bot
        off = _official(enabled=True)
        assert bot._user_can_act_for_official(off, 222, "janesec")

    def test_disabled_proxy_cannot_act(self):
        import bot
        off = _official(enabled=False)
        assert not bot._user_can_act_for_official(off, 222, "janesec")

    def test_stranger_cannot_act(self):
        import bot
        assert not bot._user_can_act_for_official(_official(), 777, "nobody")

    def test_acting_identity_official(self):
        import bot
        name, is_proxy = bot._acting_identity(_official(), 999, "pastor")
        assert (name, is_proxy) == ("Pastor Test", False)

    def test_acting_identity_proxy(self):
        import bot
        name, is_proxy = bot._acting_identity(_official(), 222, "janesec")
        assert (name, is_proxy) == ("Jane Sec", True)

    def test_recipients_includes_enabled_proxy(self):
        import bot
        ids = [r["chat_id"] for r in bot._official_side_recipients(_official(enabled=True))]
        assert 999 in ids and 222 in ids

    def test_recipients_excludes_disabled_proxy(self):
        import bot
        ids = [r["chat_id"] for r in bot._official_side_recipients(_official(enabled=False))]
        assert ids == [999]


# ---------------------------------------------------------------------------
# /enable_appt_proxies command
# ---------------------------------------------------------------------------

class TestEnableCommand:
    def _run_cmd(self, arg, from_id=999, from_username="pastor", officials=None):
        import bot
        officials = officials if officials is not None else [_official(enabled=False)]
        ctx = _ctx()
        ctx.args = [arg] if arg is not None else []
        upd = MagicMock()
        upd.effective_user.id = from_id
        upd.effective_user.username = from_username
        upd.effective_user.full_name = "U"
        upd.message.reply_text = AsyncMock()
        saved = {"called": False}
        with patch("bot.OFFICIALS", officials), \
             patch("bot._save_officials", side_effect=lambda: saved.update(called=True)):
            _run(bot.cmd_enable_appt_proxies(upd, ctx))
        return officials, upd, saved

    def test_official_enables(self):
        officials, upd, saved = self._run_cmd("yes")
        assert officials[0]["proxies_enabled"] is True
        assert saved["called"]

    def test_official_disables(self):
        officials = [_official(enabled=True)]
        officials, upd, saved = self._run_cmd("no", officials=officials)
        assert officials[0]["proxies_enabled"] is False

    def test_non_official_rejected(self):
        officials, upd, saved = self._run_cmd("yes", from_id=777, from_username="stranger")
        assert not saved["called"]
        assert "official" in upd.message.reply_text.call_args[0][0].lower()

    def test_bad_arg_shows_usage(self):
        officials, upd, saved = self._run_cmd("maybe")
        assert not saved["called"]
        assert "usage" in upd.message.reply_text.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# Claim / lock behavior in appt_callback
# ---------------------------------------------------------------------------

class TestClaim:
    def _run(self, upd, appts, finalize=None):
        import bot
        finalize = finalize or AsyncMock()
        saved = {"appts": [a.copy() for a in appts]}

        async def _get():
            return [a.copy() for a in saved["appts"]]

        async def _save(a):
            saved["appts"] = a

        with patch("bot.get_appointments", side_effect=_get), \
             patch("bot.save_appointments", side_effect=_save), \
             patch("bot.OFFICIALS", [_official(enabled=True)]), \
             patch("bot._finalize_appointment", finalize), \
             patch("bot._notify_negotiation_started", new=AsyncMock()) as notif:
            ctx = _ctx()
            _run_cb = bot.appt_callback(upd, ctx)
            _run(_run_cb)
        return saved, finalize, notif, ctx

    def test_proxy_can_confirm(self):
        upd, q = _cb_update("appt:confirm:A1", from_id=222, from_username="janesec")
        saved, finalize, notif, ctx = self._run(upd, [_appt()])
        finalize.assert_called_once()

    def test_claim_records_negotiator(self):
        upd, q = _cb_update("appt:counter:A1", from_id=222, from_username="janesec")
        saved, finalize, notif, ctx = self._run(upd, [_appt()])
        appt = saved["appts"][0]
        assert appt["negotiator_chat_id"] == 222
        assert appt["negotiator_is_proxy"] is True
        notif.assert_awaited_once()

    def test_stale_actor_blocked(self):
        # Official taps after the proxy already claimed it.
        upd, q = _cb_update("appt:confirm:A1", from_id=999, from_username="pastor")
        claimed = _appt(negotiator_chat_id=222, negotiator_name="Jane Sec", negotiator_is_proxy=True)
        saved, finalize, notif, ctx = self._run(upd, [claimed])
        finalize.assert_not_called()
        assert "already being handled" in q.edit_message_text.call_args[0][0].lower()

    def test_same_negotiator_proceeds(self):
        upd, q = _cb_update("appt:confirm:A1", from_id=222, from_username="janesec")
        claimed = _appt(negotiator_chat_id=222, negotiator_name="Jane Sec", negotiator_is_proxy=True)
        saved, finalize, notif, ctx = self._run(upd, [claimed])
        finalize.assert_called_once()

    def test_unauthorized_actor_blocked(self):
        upd, q = _cb_update("appt:confirm:A1", from_id=777, from_username="stranger")
        saved, finalize, notif, ctx = self._run(upd, [_appt()])
        finalize.assert_not_called()
        assert "not authorized" in q.edit_message_text.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# Proxy confirmation note in _finalize_appointment
# ---------------------------------------------------------------------------

class TestFinalizeProxyNote:
    def _finalize(self, appt):
        import bot
        ctx = _ctx()

        async def _save(a):
            pass

        async def _prefs(cid):
            return TZ, "en"

        with patch("bot.save_appointments", side_effect=_save), \
             patch("bot.get_user_prefs", side_effect=_prefs), \
             patch("bot.OFFICIALS", [_official(enabled=True)]):
            _run(bot._finalize_appointment(ctx, appt, [appt]))
        return ctx

    def test_proxy_gets_confirmation_note(self):
        dt = (datetime.now(TZ) + timedelta(days=2)).isoformat()
        appt = _appt(status="confirmed", confirmed_datetime=dt,
                     negotiator_chat_id=222, negotiator_is_proxy=True, negotiator_name="Jane Sec")
        ctx = self._finalize(appt)
        # A message was sent to the proxy chat (222).
        targets = [c.args[0] for c in ctx.bot.send_message.call_args_list]
        assert 222 in targets

    def test_no_proxy_note_when_official_handled(self):
        dt = (datetime.now(TZ) + timedelta(days=2)).isoformat()
        appt = _appt(status="confirmed", confirmed_datetime=dt,
                     negotiator_chat_id=999, negotiator_is_proxy=False)
        ctx = self._finalize(appt)
        targets = [c.args[0] for c in ctx.bot.send_message.call_args_list]
        # Only requester (111) and official (999) — proxy 222 not messaged.
        assert 222 not in targets


# ---------------------------------------------------------------------------
# Request fan-out to proxies
# ---------------------------------------------------------------------------

class TestRequestFanout:
    def test_request_sent_to_official_and_proxy(self):
        import bot
        ctx = _ctx()
        appt = _appt()
        with patch("bot.OFFICIALS", [_official(enabled=True)]):
            _run(bot._notify_official_of_request(ctx, appt, MagicMock()))
        targets = [c.args[0] for c in ctx.bot.send_message.call_args_list]
        assert 999 in targets and 222 in targets

    def test_request_only_official_when_disabled(self):
        import bot
        ctx = _ctx()
        appt = _appt()
        with patch("bot.OFFICIALS", [_official(enabled=False)]):
            _run(bot._notify_official_of_request(ctx, appt, MagicMock()))
        targets = [c.args[0] for c in ctx.bot.send_message.call_args_list]
        assert targets == [999]

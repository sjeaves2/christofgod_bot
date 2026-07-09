"""Tests for /reschedule — propose a new time; original stays until accepted."""

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


def _officials(proxies_enabled=False, proxy=None):
    off = {"id": "off1", "name": "Pastor Test", "chat_id": 999}
    if proxies_enabled:
        off["proxies_enabled"] = True
        off["proxies"] = [proxy or {"name": "Jane Sec", "telegram_username": "janesec",
                                    "chat_id": 888}]
    return [off]


def _appt(status="confirmed", when=None, appt_id="APPT1"):
    dt = when or (datetime.now(TZ) + timedelta(days=5)).replace(microsecond=0)
    return {
        "id": appt_id, "user_chat_id": 111, "user_username": "req",
        "user_display_name": "John Doe", "official_id": "off1",
        "official_name": "Pastor Test",
        "requested_datetime": dt.isoformat(), "confirmed_datetime": dt.isoformat(),
        "description": "chat", "status": status, "duration_minutes": 30,
    }


def _ctx():
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_document = AsyncMock()
    return ctx


def _msg_update(text, chat_id=111, username="req", full_name="John Doe"):
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = username
    upd.effective_user.full_name = full_name
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    return upd


def _cb_update(data, chat_id=111, username="req"):
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = username
    q = MagicMock()
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.data = data
    q.from_user.id = chat_id
    q.from_user.username = username
    upd.callback_query = q
    return upd, q


def _patches(appts, officials, saved):
    async def _get():
        return [a.copy() for a in appts]

    async def _save(a):
        saved.clear()
        saved.extend(a)

    return [
        patch("bot.get_appointments", side_effect=_get),
        patch("bot.save_appointments", side_effect=_save),
        patch("bot.OFFICIALS", officials),
    ]


def _run_with(patches, coro_factory):
    for p in patches:
        p.start()
    try:
        return _run(coro_factory())
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# Command entry: list building
# ---------------------------------------------------------------------------

class TestCmdReschedule:
    def _run_cmd(self, appts, chat_id=111, username="req", officials=None):
        import bot
        officials = officials or _officials()
        ctx = _ctx()
        upd = _msg_update("", chat_id=chat_id, username=username)
        with patch("bot.get_appointments", side_effect=_g(appts)), \
             patch("bot.OFFICIALS", officials):
            result = _run(bot.cmd_reschedule(upd, ctx))
        return result, upd, ctx

    def test_none_when_no_appts(self):
        import bot
        result, upd, _ = self._run_cmd([])
        assert result == bot.ConversationHandler.END

    def test_lists_future_confirmed(self):
        import bot
        result, upd, ctx = self._run_cmd([_appt(status="confirmed")])
        assert result == bot.RS_SELECT
        assert len(ctx.user_data["rs_appts"]) == 1

    def test_past_appt_excluded(self):
        import bot
        past = _appt(when=(datetime.now(TZ) - timedelta(days=1)).replace(microsecond=0))
        result, _, _ = self._run_cmd([past])
        assert result == bot.ConversationHandler.END

    def test_official_sees_appt(self):
        import bot
        result, _, ctx = self._run_cmd([_appt()], chat_id=999, username="pastor")
        assert result == bot.RS_SELECT

    def test_proxy_sees_appt_when_enabled(self):
        import bot
        result, _, ctx = self._run_cmd(
            [_appt()], chat_id=888, username="janesec",
            officials=_officials(proxies_enabled=True))
        assert result == bot.RS_SELECT

    def test_proxy_excluded_when_disabled(self):
        import bot
        result, _, _ = self._run_cmd(
            [_appt()], chat_id=888, username="janesec",
            officials=_officials(proxies_enabled=False))
        assert result == bot.ConversationHandler.END

    def test_stranger_sees_nothing(self):
        import bot
        result, _, _ = self._run_cmd([_appt()], chat_id=555, username="nobody")
        assert result == bot.ConversationHandler.END


def _g(appts):
    async def _get():
        return [a.copy() for a in appts]
    return _get


# ---------------------------------------------------------------------------
# Proposing a new time (rs_newtime)
# ---------------------------------------------------------------------------

class TestRsNewtime:
    def _run(self, appt, text, role="user", chat_id=111, username="req",
             officials=None, extra_appts=None):
        import bot
        officials = officials or _officials()
        appts = [appt] + (extra_appts or [])
        ctx = _ctx()
        ctx.user_data["rs_appt_id"] = appt["id"]
        ctx.user_data["rs_role"] = role
        upd = _msg_update(text, chat_id=chat_id, username=username)
        saved = []
        result = _run_with(_patches(appts, officials, saved),
                           lambda: bot.rs_newtime(upd, ctx))
        return result, upd, ctx, saved

    def test_bad_format_stays(self):
        import bot
        result, upd, _, _ = self._run(_appt(), "not a date")
        assert result == bot.RS_NEWTIME

    def test_past_time_stays(self):
        import bot
        result, upd, _, _ = self._run(_appt(), "2000-01-01 10:00")
        assert result == bot.RS_NEWTIME
        assert "past" in upd.message.reply_text.call_args[0][0].lower()

    def test_valid_proposal_by_user_notifies_official(self):
        import bot
        future = (datetime.now(TZ) + timedelta(days=10)).strftime("%Y-%m-%d 10:00")
        result, upd, ctx, saved = self._run(_appt(), future, role="user")
        assert result == bot.ConversationHandler.END
        # Proposal recorded, status unchanged (still confirmed).
        a = saved[0]
        assert a["reschedule_proposed_datetime"]
        assert a["status"] == "confirmed"
        # Official (chat 999) was notified with Accept/Decline.
        assert ctx.bot.send_message.await_args[0][0] == 999

    def test_valid_proposal_by_official_notifies_requester(self):
        import bot
        future = (datetime.now(TZ) + timedelta(days=10)).strftime("%Y-%m-%d 10:00")
        result, upd, ctx, saved = self._run(
            _appt(), future, role="official", chat_id=999, username="pastor")
        assert result == bot.ConversationHandler.END
        assert ctx.bot.send_message.await_args[0][0] == 111  # requester notified

    def test_proxy_proposal_sets_negotiator(self):
        future = (datetime.now(TZ) + timedelta(days=10)).strftime("%Y-%m-%d 10:00")
        result, upd, ctx, saved = self._run(
            _appt(), future, role="official", chat_id=888, username="janesec",
            officials=_officials(proxies_enabled=True))
        a = saved[0]
        assert a.get("negotiator_is_proxy") is True
        assert a.get("negotiator_chat_id") == 888


# ---------------------------------------------------------------------------
# Accept / decline responses
# ---------------------------------------------------------------------------

class TestRescheduleResponse:
    def _pending(self, proposed_by="user", proxy=False):
        a = _appt(status="confirmed")
        new = (datetime.now(TZ) + timedelta(days=10)).replace(microsecond=0)
        a["reschedule_proposed_datetime"] = new.isoformat()
        a["reschedule_proposed_by"] = proposed_by
        if proxy:
            a["negotiator_is_proxy"] = True
            a["negotiator_chat_id"] = 888
            a["negotiator_name"] = "Jane Sec"
        return a, new

    def _run_cb(self, appt, action, chat_id, username, officials=None):
        import bot
        officials = officials or _officials()
        ctx = _ctx()
        upd, q = _cb_update(f"appt:{action}:{appt['id']}", chat_id=chat_id, username=username)
        saved = []
        finalize = AsyncMock()
        patches = _patches([appt], officials, saved) + [
            patch("bot._finalize_appointment", finalize)]
        _run_with(patches, lambda: bot.appt_callback(upd, ctx))
        return q, ctx, saved, finalize

    def test_requester_accepts_official_proposal(self):
        appt, new = self._pending(proposed_by="official")
        q, ctx, saved, finalize = self._run_cb(appt, "rs_accept", 111, "req")
        finalize.assert_called_once()
        # confirmed_datetime updated to the proposed time; proposal cleared.
        a = finalize.call_args[0][1]
        assert a["confirmed_datetime"] == new.isoformat()
        assert "reschedule_proposed_datetime" not in a

    def test_official_accepts_user_proposal(self):
        appt, new = self._pending(proposed_by="user")
        q, ctx, saved, finalize = self._run_cb(appt, "rs_accept", 999, "pastor")
        finalize.assert_called_once()

    def test_decline_keeps_original(self):
        appt, new = self._pending(proposed_by="official")
        original = appt["confirmed_datetime"]
        q, ctx, saved, finalize = self._run_cb(appt, "rs_decline", 111, "req")
        finalize.assert_not_called()
        a = saved[0]
        assert a["status"] == "confirmed"
        assert a["confirmed_datetime"] == original   # unchanged
        assert "reschedule_proposed_datetime" not in a
        # Proposer (official) notified of the decline.
        assert any(c.args[0] == 999 for c in ctx.bot.send_message.await_args_list)

    def test_unauthorized_responder_blocked(self):
        # Official proposed → only the requester may respond; a stranger can't.
        appt, new = self._pending(proposed_by="official")
        q, ctx, saved, finalize = self._run_cb(appt, "rs_accept", 555, "nobody")
        finalize.assert_not_called()
        assert "authoriz" in q.edit_message_text.call_args[0][0].lower()

    def test_proxy_can_respond_to_user_proposal(self):
        appt, new = self._pending(proposed_by="user")
        q, ctx, saved, finalize = self._run_cb(
            appt, "rs_accept", 888, "janesec",
            officials=_officials(proxies_enabled=True))
        finalize.assert_called_once()

    def test_already_handled(self):
        # No pending proposal → treated as already handled.
        appt = _appt(status="confirmed")
        q, ctx, saved, finalize = self._run_cb(appt, "rs_accept", 111, "req")
        finalize.assert_not_called()
        assert "already" in q.edit_message_text.call_args[0][0].lower()

    def test_accept_past_proposal_no_change(self):
        appt = _appt(status="confirmed")
        appt["reschedule_proposed_datetime"] = (
            datetime.now(TZ) - timedelta(days=1)).isoformat()
        appt["reschedule_proposed_by"] = "official"
        q, ctx, saved, finalize = self._run_cb(appt, "rs_accept", 111, "req")
        finalize.assert_not_called()
        assert "passed" in q.edit_message_text.call_args[0][0].lower()

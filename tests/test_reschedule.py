"""Tests for /reschedule — propose a new time; original stays until accepted."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.appointments as ha
import telegram.ext as ext

TZ = pytz.timezone("America/New_York")




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
        patch("storage.get_appointments", side_effect=_get),
        patch("storage.save_appointments", side_effect=_save),
        patch("permissions.OFFICIALS", officials),
    ]


async def _run_with(patches, coro_factory):
    for p in patches:
        p.start()
    try:
        return await coro_factory()
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# Command entry: list building
# ---------------------------------------------------------------------------

class TestCmdReschedule:
    async def _run_cmd(self, appts, chat_id=111, username="req", officials=None):
        officials = officials or _officials()
        ctx = _ctx()
        upd = _msg_update("", chat_id=chat_id, username=username)
        with patch("storage.get_appointments", side_effect=_g(appts)), \
             patch("permissions.OFFICIALS", officials):
            result = await ha.cmd_reschedule(upd, ctx)
        return result, upd, ctx

    async def test_none_when_no_appts(self):
        result, upd, _ = await self._run_cmd([])
        assert result == ext.ConversationHandler.END

    async def test_lists_future_confirmed(self):
        result, upd, ctx = await self._run_cmd([_appt(status="confirmed")])
        assert result == ha.RS_SELECT
        assert len(ctx.user_data["rs_appts"]) == 1

    async def test_past_appt_excluded(self):
        past = _appt(when=(datetime.now(TZ) - timedelta(days=1)).replace(microsecond=0))
        result, _, _ = await self._run_cmd([past])
        assert result == ext.ConversationHandler.END

    async def test_official_sees_appt(self):
        result, _, ctx = await self._run_cmd([_appt()], chat_id=999, username="pastor")
        assert result == ha.RS_SELECT

    async def test_proxy_sees_appt_when_enabled(self):
        result, _, ctx = await self._run_cmd(
            [_appt()], chat_id=888, username="janesec",
            officials=_officials(proxies_enabled=True))
        assert result == ha.RS_SELECT

    async def test_proxy_excluded_when_disabled(self):
        result, _, _ = await self._run_cmd(
            [_appt()], chat_id=888, username="janesec",
            officials=_officials(proxies_enabled=False))
        assert result == ext.ConversationHandler.END

    async def test_stranger_sees_nothing(self):
        result, _, _ = await self._run_cmd([_appt()], chat_id=555, username="nobody")
        assert result == ext.ConversationHandler.END


def _g(appts):
    async def _get():
        return [a.copy() for a in appts]
    return _get


# ---------------------------------------------------------------------------
# Proposing a new time — the picker (typed YYYY-MM-DD HH:MM until 2026-11-23)
# ---------------------------------------------------------------------------

def _free_slot(appts, appt) -> datetime:
    """The first slot the picker really offers for *appt*, relative to now."""
    import appointment_slots as slots
    rules = ha._slot_rules(appts, appt["official_id"], appt["user_chat_id"],
                           exclude_id=appt["id"])
    day = datetime.now(TZ).date()
    for _ in range(60):
        free = slots.free_slots_for_viewer_day(day, TZ, **rules)
        if free:
            return free[0]
        day += timedelta(days=1)
    raise AssertionError("no free slot in 60 days")


class TestRescheduleOpensThePicker:
    async def test_choosing_an_appointment_shows_the_calendar_and_ends(self):
        appt = _appt()
        ctx = _ctx()
        ctx.user_data["rs_appts"] = [appt]
        upd, q = _cb_update("rs:sel:0")
        result = await _run_with(_patches([appt], _officials(), []),
                                 lambda: ha.rs_select(upd, ctx))
        assert result == ext.ConversationHandler.END
        markup = q.edit_message_text.call_args.kwargs["reply_markup"]
        data = [k.callback_data for row in markup.inline_keyboard for k in row]
        assert any(d.startswith("appick:r:APPT1:d:") for d in data)

    async def test_an_appointment_cancelled_meanwhile_is_refused(self):
        listed = _appt()
        ctx = _ctx()
        ctx.user_data["rs_appts"] = [listed]
        upd, q = _cb_update("rs:sel:0")
        result = await _run_with(_patches([_appt(status="cancelled")], _officials(), []),
                                 lambda: ha.rs_select(upd, ctx))
        assert result == ext.ConversationHandler.END
        assert "no longer" in q.edit_message_text.call_args[0][0].lower()
        assert "reply_markup" not in q.edit_message_text.call_args.kwargs


class TestReschedulePickingATime:
    async def _tap(self, appt, slot, chat_id=111, username="req", officials=None,
                   extra=None):
        appts = [appt] + (extra or [])
        ctx = _ctx()
        upd, q = _cb_update(f"appick:r:{appt['id']}:t:{int(slot.timestamp())}",
                            chat_id=chat_id, username=username)
        q.message.reply_text = AsyncMock()
        saved = []
        await _run_with(_patches(appts, officials or _officials(), saved),
                        lambda: ha.appt_picker_callback(upd, ctx))
        return q, ctx, saved

    async def test_a_past_slot_is_refused(self):
        q, ctx, saved = await self._tap(_appt(), TZ.localize(datetime(2000, 1, 3, 10)))
        assert saved == []
        ctx.bot.send_message.assert_not_called()

    async def test_by_the_requester_notifies_the_official(self):
        appt = _appt()
        q, ctx, saved = await self._tap(appt, _free_slot([appt], appt))
        a = saved[0]
        assert a["reschedule_proposed_datetime"]
        assert a["reschedule_proposed_by"] == "user"
        assert a["status"] == "confirmed"              # original stands
        assert ctx.bot.send_message.await_args[0][0] == 999

    async def test_by_the_official_notifies_the_requester(self):
        appt = _appt()
        q, ctx, saved = await self._tap(appt, _free_slot([appt], appt),
                                        chat_id=999, username="pastor")
        assert saved[0]["reschedule_proposed_by"] == "official"
        assert ctx.bot.send_message.await_args[0][0] == 111

    async def test_by_a_proxy_sets_the_negotiator(self):
        appt = _appt()
        q, ctx, saved = await self._tap(appt, _free_slot([appt], appt),
                                        chat_id=888, username="janesec",
                                        officials=_officials(proxies_enabled=True))
        assert saved[0]["negotiator_is_proxy"] is True
        assert saved[0]["negotiator_chat_id"] == 888

    async def test_a_stranger_is_refused(self):
        appt = _appt()
        q, ctx, saved = await self._tap(appt, _free_slot([appt], appt),
                                        chat_id=555, username="nobody")
        assert saved == []
        assert "not authorized" in q.edit_message_text.call_args[0][0]

    async def test_the_appointments_own_slot_stays_on_offer(self):
        """Excluded from the busy times, so moving it by half an hour works."""
        import appointment_slots as slots
        probe = _appt()
        own = _free_slot([], probe)              # a real slot, on the half hour
        appt = _appt(when=own)
        excluded = ha._slot_rules([appt], "off1", 111, exclude_id=appt["id"])
        included = ha._slot_rules([appt], "off1", 111)
        day = own.astimezone(TZ).date()
        assert own in slots.free_slots_for_viewer_day(day, TZ, **excluded)
        assert own not in slots.free_slots_for_viewer_day(day, TZ, **included)

    async def test_the_officials_other_booking_is_not_offered(self):
        appt = _appt()
        slot = _free_slot([appt], appt)
        other = _appt(appt_id="OTHER", when=slot)
        other["user_chat_id"] = 222
        q, ctx, saved = await self._tap(appt, slot, extra=[other])
        assert saved == []


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

    async def _run_cb(self, appt, action, chat_id, username, officials=None):
        officials = officials or _officials()
        ctx = _ctx()
        upd, q = _cb_update(f"appt:{action}:{appt['id']}", chat_id=chat_id, username=username)
        saved = []
        finalize = AsyncMock()
        patches = _patches([appt], officials, saved) + [
            patch("handlers.appointments._finalize_appointment", finalize)]
        await _run_with(patches, lambda: ha.appt_callback(upd, ctx))
        return q, ctx, saved, finalize

    async def test_requester_accepts_official_proposal(self):
        appt, new = self._pending(proposed_by="official")
        q, ctx, saved, finalize = await self._run_cb(appt, "rs_accept", 111, "req")
        finalize.assert_called_once()
        # confirmed_datetime updated to the proposed time; proposal cleared.
        a = finalize.call_args[0][1]
        assert a["confirmed_datetime"] == new.isoformat()
        assert "reschedule_proposed_datetime" not in a

    async def test_official_accepts_user_proposal(self):
        appt, new = self._pending(proposed_by="user")
        q, ctx, saved, finalize = await self._run_cb(appt, "rs_accept", 999, "pastor")
        finalize.assert_called_once()

    async def test_decline_keeps_original(self):
        appt, new = self._pending(proposed_by="official")
        original = appt["confirmed_datetime"]
        q, ctx, saved, finalize = await self._run_cb(appt, "rs_decline", 111, "req")
        finalize.assert_not_called()
        a = saved[0]
        assert a["status"] == "confirmed"
        assert a["confirmed_datetime"] == original   # unchanged
        assert "reschedule_proposed_datetime" not in a
        # Proposer (official) notified of the decline.
        assert any(c.args[0] == 999 for c in ctx.bot.send_message.await_args_list)

    async def test_unauthorized_responder_blocked(self):
        # Official proposed → only the requester may respond; a stranger can't.
        appt, new = self._pending(proposed_by="official")
        q, ctx, saved, finalize = await self._run_cb(appt, "rs_accept", 555, "nobody")
        finalize.assert_not_called()
        assert "authoriz" in q.edit_message_text.call_args[0][0].lower()

    async def test_proxy_can_respond_to_user_proposal(self):
        appt, new = self._pending(proposed_by="user")
        q, ctx, saved, finalize = await self._run_cb(
            appt, "rs_accept", 888, "janesec",
            officials=_officials(proxies_enabled=True))
        finalize.assert_called_once()

    async def test_already_handled(self):
        # No pending proposal → treated as already handled.
        appt = _appt(status="confirmed")
        q, ctx, saved, finalize = await self._run_cb(appt, "rs_accept", 111, "req")
        finalize.assert_not_called()
        assert "already" in q.edit_message_text.call_args[0][0].lower()

    async def test_accept_past_proposal_no_change(self):
        appt = _appt(status="confirmed")
        appt["reschedule_proposed_datetime"] = (
            datetime.now(TZ) - timedelta(days=1)).isoformat()
        appt["reschedule_proposed_by"] = "official"
        q, ctx, saved, finalize = await self._run_cb(appt, "rs_accept", 111, "req")
        finalize.assert_not_called()
        assert "passed" in q.edit_message_text.call_args[0][0].lower()

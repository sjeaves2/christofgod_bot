"""Tests for /cancelappointment — inline-keyboard select + confirm, both parties notified."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
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
    official_id: str = "off1",
    official_name: str = "Pastor Test",
    official_chat_id: int | None = 999,
) -> dict:
    dt = TZ.localize(datetime(2026, 8, 1, 10, 0))
    return {
        "id": appt_id,
        "user_chat_id": user_chat_id,
        "user_username": user_username,
        "user_display_name": "Test Requester",
        "official_id": official_id,
        "official_name": official_name,
        "requested_datetime": dt.isoformat(),
        "confirmed_datetime": dt.isoformat(),
        "description": "Test meeting",
        "status": status,
        "duration_minutes": 30,
        "_official_chat_id": official_chat_id,
    }


def _make_officials(chat_id: int | None = 999) -> list:
    off = {"id": "off1", "name": "Pastor Test"}
    if chat_id is not None:
        off["chat_id"] = chat_id
    return [off]


def _make_context(user_chat_id: int = 111) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_document = AsyncMock()
    ctx.user_data = {}
    return ctx


def _make_cmd_update(chat_id: int = 111, username: str = "requester") -> MagicMock:
    """A plain command update (entry point uses update.message.reply_text)."""
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = username
    upd.effective_user.full_name = "Test User"
    upd.message.reply_text = AsyncMock()
    return upd


def _make_cb_update(data: str, chat_id: int = 111, username: str = "requester"):
    """A callback-query update (button press)."""
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = username
    upd.effective_user.full_name = "Test User"
    q = MagicMock()
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.data = data
    q.from_user.id = chat_id
    upd.callback_query = q
    return upd, q


def _button_rows(reply_markup):
    return [[(b.text, b.callback_data) for b in row] for row in reply_markup.inline_keyboard]


# ---------------------------------------------------------------------------
# cmd_cancelappointment — entry point (renders the selection keyboard)
# ---------------------------------------------------------------------------

class TestCmdCancelAppointment:

    def _run_cmd(self, appts, user_chat_id=111, username="requester"):
        from bot import cmd_cancelappointment
        ctx = _make_context(user_chat_id)
        upd = _make_cmd_update(chat_id=user_chat_id, username=username)

        async def _fake_get():
            return appts

        with patch("bot.get_appointments", side_effect=_fake_get), \
             patch("bot.OFFICIALS", _make_officials()):
            result = _run(cmd_cancelappointment(upd, ctx))
        return result, upd, ctx

    def test_no_active_appointments_ends_conversation(self):
        from bot import ConversationHandler
        result, _, _ = self._run_cmd([])
        assert result == ConversationHandler.END

    def test_active_appointment_prompts_selection(self):
        from bot import CA_SELECT
        result, _, _ = self._run_cmd([_make_appt(status="confirmed")])
        assert result == CA_SELECT

    def test_pending_appointments_included(self):
        from bot import CA_SELECT
        result, _, _ = self._run_cmd([_make_appt(status="pending")])
        assert result == CA_SELECT

    def test_counter_proposed_appointments_included(self):
        from bot import CA_SELECT
        result, _, _ = self._run_cmd([_make_appt(status="counter_proposed")])
        assert result == CA_SELECT

    def test_cancelled_appointments_excluded(self):
        from bot import ConversationHandler
        result, _, _ = self._run_cmd([_make_appt(status="cancelled")])
        assert result == ConversationHandler.END

    def test_declined_appointments_excluded(self):
        from bot import ConversationHandler
        result, _, _ = self._run_cmd([_make_appt(status="declined")])
        assert result == ConversationHandler.END

    def test_keyboard_lists_appointment(self):
        _, upd, _ = self._run_cmd([_make_appt(status="confirmed")])
        markup = upd.message.reply_text.call_args.kwargs["reply_markup"]
        rows = _button_rows(markup)
        labels = [text for row in rows for text, _ in row]
        assert any("Pastor Test" in lbl for lbl in labels)
        # The first appointment maps to callback "ca:sel:0".
        cbs = [cb for row in rows for _, cb in row]
        assert "ca:sel:0" in cbs

    def test_active_appts_stored_in_user_data(self):
        _, _, ctx = self._run_cmd([_make_appt(status="confirmed")])
        assert len(ctx.user_data["ca_appts"]) == 1

    def test_only_own_appointments_shown_to_requester(self):
        own = _make_appt(user_chat_id=111)
        other = _make_appt(appt_id="APPT002", user_chat_id=999)
        _, _, ctx = self._run_cmd([own, other], user_chat_id=111)
        ids = [a["id"] for a in ctx.user_data.get("ca_appts", [])]
        assert "APPT001" in ids
        assert "APPT002" not in ids


# ---------------------------------------------------------------------------
# ca_select — picking which appointment to cancel (callback)
# ---------------------------------------------------------------------------

class TestCaSelect:

    def _run_select(self, data, appts):
        from bot import ca_select
        ctx = _make_context()
        ctx.user_data["ca_appts"] = appts
        upd, q = _make_cb_update(data)
        result = _run(ca_select(upd, ctx))
        return result, q, ctx

    def test_valid_selection_advances_to_confirm(self):
        from bot import CA_CONFIRM
        result, _, _ = self._run_select("ca:sel:0", [_make_appt()])
        assert result == CA_CONFIRM

    def test_selected_appt_stored_in_user_data(self):
        _, _, ctx = self._run_select("ca:sel:0", [_make_appt()])
        assert ctx.user_data["ca_appt"]["id"] == "APPT001"

    def test_out_of_range_ends(self):
        from bot import ConversationHandler
        result, _, _ = self._run_select("ca:sel:5", [_make_appt()])
        assert result == ConversationHandler.END

    def test_abort_ends(self):
        from bot import ConversationHandler
        result, q, _ = self._run_select("ca:abort", [_make_appt()])
        assert result == ConversationHandler.END

    def test_confirm_prompt_contains_official_name(self):
        _, q, _ = self._run_select("ca:sel:0", [_make_appt()])
        assert "Pastor Test" in q.edit_message_text.call_args[0][0]

    def test_confirm_presents_yes_no_keyboard(self):
        _, q, _ = self._run_select("ca:sel:0", [_make_appt()])
        markup = q.edit_message_text.call_args.kwargs["reply_markup"]
        cbs = [cb for row in _button_rows(markup) for _, cb in row]
        assert "ca:yes" in cbs and "ca:no" in cbs


# ---------------------------------------------------------------------------
# ca_confirm — requester cancels (callback)
# ---------------------------------------------------------------------------

class TestCaConfirmRequester:

    def _run_confirm(self, data, appt, all_appts=None, user_chat_id=111,
                     username="requester", officials=None):
        from bot import ca_confirm
        if all_appts is None:
            all_appts = [appt.copy()]
        if officials is None:
            officials = _make_officials(chat_id=999)

        ctx = _make_context(user_chat_id)
        ctx.user_data["ca_appt"] = appt
        upd, q = _make_cb_update(data, chat_id=user_chat_id, username=username)

        saved = []

        async def _fake_get():
            return [a.copy() for a in all_appts]

        async def _fake_save(appts):
            saved.extend(appts)

        with patch("bot.get_appointments", side_effect=_fake_get), \
             patch("bot.save_appointments", side_effect=_fake_save), \
             patch("bot.OFFICIALS", officials), \
             patch("bot._is_known_official", return_value=False):
            result = _run(ca_confirm(upd, ctx))
        return result, q, ctx, saved

    def test_yes_ends_conversation(self):
        from bot import ConversationHandler
        result, _, _, _ = self._run_confirm("ca:yes", _make_appt())
        assert result == ConversationHandler.END

    def test_no_aborts_and_ends(self):
        from bot import ConversationHandler
        result, _, _, saved = self._run_confirm("ca:no", _make_appt())
        assert result == ConversationHandler.END
        assert not saved

    def test_no_sends_aborted_message(self):
        _, q, _, _ = self._run_confirm("ca:no", _make_appt())
        text = q.edit_message_text.call_args[0][0].lower()
        assert "abort" in text or "cancel" in text

    def test_appointment_saved_as_cancelled(self):
        _, _, _, saved = self._run_confirm("ca:yes", _make_appt())
        assert any(a["status"] == "cancelled" for a in saved)

    def test_official_notified_when_chat_id_known(self):
        _, _, ctx, _ = self._run_confirm("ca:yes", _make_appt(),
                                         officials=_make_officials(chat_id=999))
        assert ctx.bot.send_message.call_count == 1
        assert ctx.bot.send_message.call_args[0][0] == 999

    def test_official_notification_contains_appt_id(self):
        _, _, ctx, _ = self._run_confirm("ca:yes", _make_appt(),
                                         officials=_make_officials(chat_id=999))
        assert "APPT001" in ctx.bot.send_message.call_args[0][1]

    def test_official_notification_mentions_cancelled(self):
        _, _, ctx, _ = self._run_confirm("ca:yes", _make_appt(),
                                         officials=_make_officials(chat_id=999))
        assert "cancel" in ctx.bot.send_message.call_args[0][1].lower()

    def test_no_official_notification_when_chat_id_unknown(self):
        _, _, ctx, _ = self._run_confirm("ca:yes", _make_appt(),
                                         officials=_make_officials(chat_id=None))
        assert ctx.bot.send_message.call_count == 0

    def test_requester_receives_confirmation_reply(self):
        _, q, _, _ = self._run_confirm("ca:yes", _make_appt())
        q.edit_message_text.assert_called_once()
        assert "cancel" in q.edit_message_text.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# ca_confirm — official cancels (callback)
# ---------------------------------------------------------------------------

class TestCaConfirmOfficial:

    def _run_confirm_as_official(self, appt, officials=None):
        from bot import ca_confirm
        if officials is None:
            officials = _make_officials(chat_id=999)

        ctx = _make_context(user_chat_id=999)
        ctx.user_data["ca_appt"] = appt
        upd, q = _make_cb_update("ca:yes", chat_id=999, username="test_official")

        saved = []

        async def _fake_get():
            return [appt.copy()]

        async def _fake_save(appts):
            saved.extend(appts)

        with patch("bot.get_appointments", side_effect=_fake_get), \
             patch("bot.save_appointments", side_effect=_fake_save), \
             patch("bot.OFFICIALS", officials), \
             patch("bot._is_known_official", return_value=True):
            result = _run(ca_confirm(upd, ctx))
        return result, q, ctx, saved

    def test_appointment_saved_as_cancelled(self):
        _, _, _, saved = self._run_confirm_as_official(_make_appt())
        assert any(a["status"] == "cancelled" for a in saved)

    def test_requester_notified(self):
        _, _, ctx, _ = self._run_confirm_as_official(_make_appt(user_chat_id=111))
        assert ctx.bot.send_message.call_count == 1
        assert ctx.bot.send_message.call_args[0][0] == 111

    def test_requester_notification_mentions_cancelled_by_official(self):
        _, _, ctx, _ = self._run_confirm_as_official(_make_appt(user_chat_id=111))
        msg = ctx.bot.send_message.call_args[0][1].lower()
        assert "cancel" in msg and "official" in msg

    def test_requester_notification_contains_appt_id(self):
        _, _, ctx, _ = self._run_confirm_as_official(_make_appt())
        assert "APPT001" in ctx.bot.send_message.call_args[0][1]

    def test_official_receives_confirmation_reply(self):
        _, q, _, _ = self._run_confirm_as_official(_make_appt())
        q.edit_message_text.assert_called_once()

    def test_official_reply_mentions_requester_notified(self):
        _, q, _, _ = self._run_confirm_as_official(_make_appt(user_chat_id=111))
        text = q.edit_message_text.call_args[0][0].lower()
        assert "notified" in text or "requester" in text


# ---------------------------------------------------------------------------
# ca_confirm — cancellation ICS delivered to both parties
# ---------------------------------------------------------------------------

def _doc_bytes(call) -> bytes:
    doc = call.kwargs.get("document") or call.args[1]
    raw = doc.input_file_content
    return raw if isinstance(raw, (bytes, bytearray)) else raw.read()


def _doc_filename(call) -> str:
    doc = call.kwargs.get("document") or call.args[1]
    return doc.filename


class TestCancellationIcs:
    """A METHOD:CANCEL ICS must be sent to both parties when an appointment is cancelled."""

    def _run_as_requester(self, appt, officials):
        from bot import ca_confirm
        ctx = _make_context(user_chat_id=111)
        ctx.user_data["ca_appt"] = appt
        upd, q = _make_cb_update("ca:yes", chat_id=111, username="requester")

        async def _fake_get():
            return [appt.copy()]

        async def _fake_save(appts):
            pass

        with patch("bot.get_appointments", side_effect=_fake_get), \
             patch("bot.save_appointments", side_effect=_fake_save), \
             patch("bot.OFFICIALS", officials), \
             patch("bot._is_known_official", return_value=False):
            _run(ca_confirm(upd, ctx))
        return ctx

    def _run_as_official(self, appt, officials):
        from bot import ca_confirm
        ctx = _make_context(user_chat_id=999)
        ctx.user_data["ca_appt"] = appt
        upd, q = _make_cb_update("ca:yes", chat_id=999, username="test_official")

        async def _fake_get():
            return [appt.copy()]

        async def _fake_save(appts):
            pass

        with patch("bot.get_appointments", side_effect=_fake_get), \
             patch("bot.save_appointments", side_effect=_fake_save), \
             patch("bot.OFFICIALS", officials), \
             patch("bot._is_known_official", return_value=True):
            _run(ca_confirm(upd, ctx))
        return ctx

    def test_requester_cancel_sends_ics_to_official(self):
        ctx = self._run_as_requester(_make_appt(user_chat_id=111), _make_officials(chat_id=999))
        targets = [c.args[0] for c in ctx.bot.send_document.call_args_list]
        assert 999 in targets

    def test_requester_cancel_sends_ics_to_self(self):
        ctx = self._run_as_requester(_make_appt(user_chat_id=111), _make_officials(chat_id=999))
        targets = [c.args[0] for c in ctx.bot.send_document.call_args_list]
        assert 111 in targets

    def test_requester_cancel_two_ics_sent(self):
        ctx = self._run_as_requester(_make_appt(user_chat_id=111), _make_officials(chat_id=999))
        assert ctx.bot.send_document.call_count == 2

    def test_requester_cancel_only_self_ics_when_official_unknown(self):
        ctx = self._run_as_requester(_make_appt(user_chat_id=111), _make_officials(chat_id=None))
        targets = [c.args[0] for c in ctx.bot.send_document.call_args_list]
        assert targets == [111]

    def test_official_cancel_sends_ics_to_requester(self):
        ctx = self._run_as_official(_make_appt(user_chat_id=111), _make_officials(chat_id=999))
        targets = [c.args[0] for c in ctx.bot.send_document.call_args_list]
        assert 111 in targets

    def test_official_cancel_sends_ics_to_self(self):
        ctx = self._run_as_official(_make_appt(user_chat_id=111), _make_officials(chat_id=999))
        targets = [c.args[0] for c in ctx.bot.send_document.call_args_list]
        assert 999 in targets

    def test_official_cancel_two_ics_sent(self):
        ctx = self._run_as_official(_make_appt(user_chat_id=111), _make_officials(chat_id=999))
        assert ctx.bot.send_document.call_count == 2

    def test_ics_filename_is_cancellation(self):
        ctx = self._run_as_requester(_make_appt(user_chat_id=111), _make_officials(chat_id=999))
        for call in ctx.bot.send_document.call_args_list:
            assert _doc_filename(call) == "appointment-cancelled.ics"

    def test_ics_is_valid_calendar(self):
        ctx = self._run_as_requester(_make_appt(user_chat_id=111), _make_officials(chat_id=999))
        data = _doc_bytes(ctx.bot.send_document.call_args_list[0])
        assert data.startswith(b"BEGIN:VCALENDAR")

    def test_ics_has_cancel_method(self):
        ctx = self._run_as_requester(_make_appt(user_chat_id=111), _make_officials(chat_id=999))
        data = _doc_bytes(ctx.bot.send_document.call_args_list[0])
        assert b"METHOD:CANCEL" in data

    def test_ics_has_cancelled_status(self):
        ctx = self._run_as_requester(_make_appt(user_chat_id=111), _make_officials(chat_id=999))
        data = _doc_bytes(ctx.bot.send_document.call_args_list[0])
        assert b"STATUS:CANCELLED" in data

    def test_ics_uid_contains_appt_id(self):
        ctx = self._run_as_requester(_make_appt(appt_id="APPT001", user_chat_id=111),
                                     _make_officials(chat_id=999))
        data = _doc_bytes(ctx.bot.send_document.call_args_list[0])
        assert b"APPT001" in data

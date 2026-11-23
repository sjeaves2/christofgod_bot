"""The /myappointments buttons, and replies to a request that arrived as a photo.

Both added 2026-11-23 at the user's request.

* /myappointments numbers each upcoming appointment the viewer can still
  change and gives it a Reschedule and a Cancel button, so nobody has to know
  /reschedule or /cancelappointment exist. The buttons are stateless — the id
  rides in them — and every tap re-checks the stored record.
* An appointment request reaches the official as a PHOTO when the requester
  has a profile picture. Telegram refuses edit_message_text on a photo, so
  every reply to a tap on that request now edits the caption instead.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.appointments as ha

TZ = pytz.timezone("America/New_York")
OFFICIALS = [{"id": "off1", "name": "Pastor Test", "chat_id": 999}]


def _appt(appt_id="A1", status="confirmed", days=7, user=111, official="off1"):
    dt = TZ.localize((datetime.now(TZ).replace(tzinfo=None) + timedelta(days=days))
                     .replace(hour=10, minute=0, second=0, microsecond=0))
    return {
        "id": appt_id, "user_chat_id": user, "user_username": "requester",
        "user_display_name": "Test Requester", "official_id": official,
        "official_name": "Pastor Test", "requested_datetime": dt.isoformat(),
        "confirmed_datetime": dt.isoformat() if status == "confirmed" else None,
        "description": "Test meeting", "status": status, "duration_minutes": 30,
    }


def _keys(markup):
    return [k for row in markup.inline_keyboard for k in row] if markup else []


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

async def _list(appts, chat_id=111, username="requester"):
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = username
    upd.effective_user.full_name = "Test User"
    upd.message.reply_text = AsyncMock()

    async def fake_get():
        return appts

    with patch("storage.get_appointments", side_effect=fake_get), \
         patch("permissions.OFFICIALS", OFFICIALS):
        await ha.cmd_myappointments(upd, MagicMock())
    call = upd.message.reply_text.call_args
    return call[0][0], call.kwargs.get("reply_markup")


class TestTheList:
    async def test_an_upcoming_appointment_gets_both_buttons(self):
        text, markup = await _list([_appt()])
        assert [k.callback_data for k in _keys(markup)] == [
            "appick:r:A1:open", "apmy:c:A1"]
        assert [k.text for k in _keys(markup)] == ["📅 Reschedule 1", "❌ Cancel 1"]

    async def test_the_number_in_the_text_matches_the_buttons(self):
        text, _ = await _list([_appt("A1", days=3), _appt("A2", days=9)])
        assert "*1.* With:" in text and "*2.* With:" in text
        assert text.index("A1") < text.index("A2")
        assert "buttons below" in text

    async def test_past_and_finished_appointments_get_none(self):
        text, markup = await _list([
            _appt("PAST", days=-3), _appt("GONE", status="cancelled"),
            _appt("NO", status="declined")])
        assert markup is None
        assert "buttons below" not in text
        assert "*1.*" not in text

    async def test_pending_requests_are_changeable_too(self):
        _, markup = await _list([_appt("P", status="pending")])
        assert "apmy:c:P" in [k.callback_data for k in _keys(markup)]

    async def test_the_official_gets_buttons_for_their_appointments(self):
        _, markup = await _list([_appt()], chat_id=999, username="pastor")
        assert "apmy:c:A1" in [k.callback_data for k in _keys(markup)]

    async def test_buttons_are_capped_but_the_list_is_not(self):
        many = [_appt(f"A{i}", days=i + 1) for i in range(ha.MYAPPTS_MAX_BUTTONS + 3)]
        text, markup = await _list(many)
        assert len(markup.inline_keyboard) == ha.MYAPPTS_MAX_BUTTONS
        assert all(a["id"] in text for a in many)

    async def test_no_appointments_still_says_so(self):
        upd = MagicMock()
        upd.effective_user.id = 111
        upd.effective_user.username = "requester"
        upd.effective_user.full_name = "Test User"
        upd.message.reply_text = AsyncMock()

        async def fake_get():
            return []

        with patch("storage.get_appointments", side_effect=fake_get), \
             patch("permissions.OFFICIALS", OFFICIALS):
            await ha.cmd_myappointments(upd, MagicMock())
        assert "no appointments" in upd.message.reply_text.call_args[0][0].lower()

    @pytest.mark.parametrize("lang", ["en", "es", "fr", "zu"])
    def test_every_language_starts_appt_line_with_a_bullet(self, lang):
        """The numbering replaces that bullet; without one it is prepended."""
        from localization import CATALOG
        assert CATALOG[lang]["appt_line"].startswith("•")

    @pytest.mark.parametrize("lang", ["en", "es", "fr", "zu"])
    def test_every_language_has_the_button_strings(self, lang):
        from localization import CATALOG
        for key in ("myappts_btn_reschedule", "myappts_btn_cancel",
                    "myappts_actions_hint", "myappts_no_longer",
                    "myappts_yes_cancel", "myappts_no_keep"):
            assert key in CATALOG[lang], f"{lang} lacks {key}"
        assert "{n}" in CATALOG[lang]["myappts_btn_cancel"]
        assert "{n}" in CATALOG[lang]["myappts_btn_reschedule"]


# ---------------------------------------------------------------------------
# Tapping the buttons
# ---------------------------------------------------------------------------

async def _tap(data, appts, chat_id=111, username="requester"):
    q = MagicMock()
    q.data = data
    q.from_user.id = chat_id
    q.from_user.username = username
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message.reply_text = AsyncMock()
    upd = MagicMock()
    upd.callback_query = q
    upd.effective_user.id = chat_id
    upd.effective_user.username = username
    upd.effective_user.full_name = "Test User"
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_document = AsyncMock()
    saves = []

    async def fake_get():
        return appts

    async def fake_save(a):
        saves.append(a)

    handler = ha.appt_picker_callback if data.startswith("appick:") else ha.myappt_callback
    with patch("storage.get_appointments", side_effect=fake_get), \
         patch("storage.save_appointments", side_effect=fake_save), \
         patch("permissions.OFFICIALS", OFFICIALS):
        await handler(upd, ctx)
    return q, ctx, saves


class TestCancelButton:
    async def test_cancel_asks_first_in_a_new_message(self):
        q, ctx, saves = await _tap("apmy:c:A1", [_appt()])
        assert saves == []
        q.edit_message_text.assert_not_called()          # the list stays
        markup = q.message.reply_text.call_args.kwargs["reply_markup"]
        assert [k.callback_data for k in _keys(markup)] == ["apmy:y:A1", "apmy:k:A1"]

    async def test_yes_cancels_and_tells_the_official(self):
        appt = _appt()
        q, ctx, saves = await _tap("apmy:y:A1", [appt])
        assert appt["status"] == "cancelled" and len(saves) == 1
        assert ctx.bot.send_message.await_args_list[0][0][0] == 999
        sent_to = [c[0][0] for c in ctx.bot.send_document.await_args_list]
        assert sorted(sent_to) == [111, 999]             # both get the removal ICS
        assert "cancelled" in q.edit_message_text.call_args[0][0].lower()

    async def test_the_official_cancelling_tells_the_requester(self):
        appt = _appt()
        q, ctx, saves = await _tap("apmy:y:A1", [appt], chat_id=999, username="pastor")
        assert appt["status"] == "cancelled"
        assert ctx.bot.send_message.await_args_list[0][0][0] == 111

    async def test_keep_changes_nothing(self):
        appt = _appt()
        q, ctx, saves = await _tap("apmy:k:A1", [appt])
        assert appt["status"] == "confirmed" and saves == []
        assert q.edit_message_text.call_args[0][0] == "Cancellation aborted."

    @pytest.mark.parametrize("appt", [_appt(status="cancelled"), _appt(days=-2)])
    async def test_yes_on_an_appointment_no_longer_changeable(self, appt):
        q, ctx, saves = await _tap(f"apmy:y:{appt['id']}", [appt])
        assert saves == []
        assert "no longer be changed" in q.edit_message_text.call_args[0][0]
        ctx.bot.send_message.assert_not_called()

    async def test_a_stranger_cannot_cancel(self):
        appt = _appt()
        q, ctx, saves = await _tap("apmy:y:A1", [appt], chat_id=555, username="nobody")
        assert saves == [] and appt["status"] == "confirmed"

    async def test_a_stale_list_asking_to_cancel_says_why(self):
        q, _, saves = await _tap("apmy:c:A1", [_appt(status="declined")])
        assert saves == []
        assert "no longer be changed" in q.message.reply_text.call_args[0][0]

    async def test_a_missing_appointment(self):
        q, _, saves = await _tap("apmy:y:NOPE", [_appt()])
        assert saves == [] and "no longer works" in q.edit_message_text.call_args[0][0]


class TestRescheduleButton:
    async def test_it_opens_the_picker_in_a_new_message(self):
        q, _, saves = await _tap("appick:r:A1:open", [_appt()])
        assert saves == []
        q.edit_message_text.assert_not_called()
        markup = q.message.reply_text.call_args.kwargs["reply_markup"]
        data = [k.callback_data for k in _keys(markup)]
        assert any(d.startswith("appick:r:A1:d:") for d in data)

    async def test_it_is_refused_once_the_appointment_has_passed(self):
        q, _, _ = await _tap("appick:r:A1:open", [_appt(days=-2)])
        assert "no longer" in q.edit_message_text.call_args[0][0].lower()
        q.message.reply_text.assert_not_called()


# ---------------------------------------------------------------------------
# A request that arrived as a photo
# ---------------------------------------------------------------------------

def _photo_query(data, actor=999):
    q = MagicMock()
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock(
        side_effect=AssertionError("edit_message_text on a photo message"))
    q.edit_message_caption = AsyncMock()
    q.message.photo = (MagicMock(),)        # what PTB gives for a photo message
    q.message.reply_text = AsyncMock()
    q.message.chat_id = actor
    q.from_user.id = actor
    q.from_user.username = None
    upd = MagicMock()
    upd.callback_query = q
    return upd, q


class TestRepliesToAPhotoRequest:
    async def test_the_helper_edits_the_caption_of_a_photo(self):
        _, q = _photo_query("x")
        await ha._edit_tapped(q, "hello")
        q.edit_message_caption.assert_awaited_once_with(caption="hello")

    async def test_the_helper_edits_the_text_of_a_text_message(self):
        q = MagicMock()
        q.message.photo = ()
        q.edit_message_text = AsyncMock()
        q.edit_message_caption = AsyncMock()
        await ha._edit_tapped(q, "hello")
        q.edit_message_text.assert_awaited_once_with("hello")
        q.edit_message_caption.assert_not_called()

    @pytest.mark.parametrize("action,status", [("confirm", "confirmed"),
                                               ("decline", "declined")])
    async def test_confirm_and_decline_work_on_a_photo(self, action, status):
        appt = _appt(status="pending")
        upd, q = _photo_query(f"appt:{action}:A1")
        ctx = MagicMock()

        async def fake_get():
            return [appt]

        with patch("storage.get_appointments", side_effect=fake_get), \
             patch("storage.save_appointments", new=AsyncMock()), \
             patch("permissions.OFFICIALS", OFFICIALS), \
             patch.object(ha, "_finalize_appointment", new=AsyncMock()), \
             patch.object(ha, "_notify_negotiation_started", new=AsyncMock()), \
             patch.object(ha, "send_markdown", new=AsyncMock()):
            await ha.appt_callback(upd, ctx)
        assert appt["status"] == status
        q.edit_message_caption.assert_awaited_once()
        assert "A1" in q.edit_message_caption.call_args.kwargs["caption"]

    async def test_a_guard_refusal_on_a_photo_edits_the_caption(self):
        appt = _appt(status="cancelled")
        upd, q = _photo_query("appt:confirm:A1")

        async def fake_get():
            return [appt]

        with patch("storage.get_appointments", side_effect=fake_get), \
             patch("permissions.OFFICIALS", OFFICIALS):
            await ha.appt_callback(upd, MagicMock())
        assert "already been cancelled" in q.edit_message_caption.call_args.kwargs["caption"]

    async def test_suggest_time_on_a_photo_sends_the_picker_separately(self):
        appt = _appt(status="pending")
        upd, q = _photo_query("appt:counter:A1")

        async def fake_get():
            return [appt]

        with patch("storage.get_appointments", side_effect=fake_get), \
             patch("storage.save_appointments", new=AsyncMock()), \
             patch("permissions.OFFICIALS", OFFICIALS), \
             patch.object(ha, "_notify_negotiation_started", new=AsyncMock()):
            await ha.appt_callback(upd, MagicMock())
        q.message.reply_text.assert_awaited_once()
        q.edit_message_caption.assert_not_called()

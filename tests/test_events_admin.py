"""Tests for the admin event-management conversations:

  /addevent     — one-time and weekly special events
  /modifyevent  — field edits with type coercion
  /deleteevent  — delete a special event, or annotate a convocation

These flows write to events.yaml, so the assertions focus on the saved shape
as well as the conversation state transitions.
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


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.user_data = {}
    ctx.application = MagicMock()
    return ctx


def _upd(text: str = "", chat_id: int = 1) -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = "admin"
    upd.effective_user.full_name = "Admin User"
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    return upd


def _future_date(days=10) -> str:
    return (datetime.now(TZ) + timedelta(days=days)).strftime("%Y-%m-%d")


def _run_step(fn_name, text, user_data=None, evdata=None):
    """Drive one conversation step, capturing any events.yaml write."""
    import handlers.events_admin as ea
    ctx = _ctx()
    ctx.user_data.update(user_data or {})
    upd = _upd(text)
    saved = {}
    data = evdata if evdata is not None else {"special_events": []}

    async def _get():
        return data

    async def _save(d):
        saved["data"] = d

    with patch("storage.get_all_events_data", side_effect=_get), \
         patch("storage.save_events_data", side_effect=_save), \
         patch("handlers.events_admin.schedule_event_notification"):
        result = _run(getattr(ea, fn_name)(upd, ctx))
    return result, upd, ctx, saved


# ---------------------------------------------------------------------------
# /addevent
# ---------------------------------------------------------------------------

class TestAddEventFlow:
    def test_non_admin_blocked(self):
        import handlers.events_admin as ea
        from bot import ConversationHandler
        with patch("permissions.is_admin", return_value=False):
            result = _run(ea.cmd_addevent(_upd(), _ctx()))
        assert result == ConversationHandler.END

    def test_entry_asks_for_name(self):
        import handlers.events_admin as ea
        ctx, upd = _ctx(), _upd()
        with patch("permissions.is_admin", return_value=True):
            result = _run(ea.cmd_addevent(upd, ctx))
        assert result == ea.AE_NAME
        assert "name" in upd.message.reply_text.call_args[0][0].lower()

    def test_name_advances_to_date(self):
        import handlers.events_admin as ea
        result, _, ctx, _ = _run_step("ae_name", "  Youth Night  ")
        assert result == ea.AE_DATE
        assert ctx.user_data["ae_name"] == "Youth Night"

    def test_bad_time_format_stays(self):
        import handlers.events_admin as ea
        result, upd, ctx, _ = _run_step("ae_time", "7pm")
        assert result == ea.AE_TIME
        assert "ae_time" not in ctx.user_data

    def test_valid_time_advances(self):
        import handlers.events_admin as ea
        result, _, ctx, _ = _run_step("ae_time", "19:00")
        assert result == ea.AE_DURATION
        assert ctx.user_data["ae_time"] == "19:00"

    def test_duration_defaults_when_not_a_number(self):
        import handlers.events_admin as ea
        result, _, ctx, _ = _run_step("ae_duration", "skip")
        assert result == ea.AE_DESC
        assert ctx.user_data["ae_duration"] == 60

    def test_duration_uses_entered_number(self):
        import handlers.events_admin as ea
        result, _, ctx, _ = _run_step("ae_duration", "45")
        assert result == ea.AE_DESC
        assert ctx.user_data["ae_duration"] == 45

    def test_dash_clears_description_and_url(self):
        import handlers.events_admin as ea
        _, _, ctx1, _ = _run_step("ae_desc", "-")
        assert ctx1.user_data["ae_desc"] == ""
        result, _, ctx2, _ = _run_step("ae_url", "-")
        assert result == ea.AE_NOTIF
        assert ctx2.user_data["ae_url"] == ""

    def test_notif_defaults_and_shows_summary(self):
        import handlers.events_admin as ea
        base = {"ae_name": "Youth Night", "ae_date": _future_date(), "ae_time": "19:00",
                "ae_duration": 90, "ae_desc": "Bring a friend", "ae_url": ""}
        result, upd, ctx, _ = _run_step("ae_notif", "notanumber", base)
        assert result == ea.AE_CONFIRM
        assert ctx.user_data["ae_notif"] == ea.DEFAULT_NOTIF_MIN
        summary = upd.message.reply_text.call_args[0][0]
        assert "Youth Night" in summary and "90 min" in summary

    def _confirm_data(self, date_value):
        return {"ae_name": "Youth Night", "ae_date": date_value, "ae_time": "19:00",
                "ae_duration": 90, "ae_desc": "Bring a friend",
                "ae_url": "https://zoom.us/j/1", "ae_notif": 45}

    def test_confirm_no_discards(self):
        from bot import ConversationHandler
        result, upd, _, saved = _run_step("ae_confirm", "no", self._confirm_data(_future_date()))
        assert result == ConversationHandler.END
        assert "data" not in saved  # nothing written

    def test_confirm_saves_one_time_event(self):
        from bot import ConversationHandler
        date = _future_date()
        result, _, _, saved = _run_step("ae_confirm", "yes", self._confirm_data(date))
        assert result == ConversationHandler.END
        ev = saved["data"]["special_events"][0]
        assert ev["type"] == "once"
        assert ev["date"] == date
        assert ev["time"] == "19:00"
        assert ev["duration_minutes"] == 90
        assert ev["notification_minutes"] == 45
        assert ev["url"] == "https://zoom.us/j/1"
        assert ev["active"] is True
        assert ev["id"].startswith("special_")

    def test_confirm_saves_weekly_event(self):
        result, _, _, saved = _run_step("ae_confirm", "yes", self._confirm_data("weekly:2"))
        ev = saved["data"]["special_events"][0]
        assert ev["type"] == "weekly"
        assert ev["weekday"] == 2
        assert "date" not in ev

    def test_confirm_appends_without_dropping_existing(self):
        existing = {"special_events": [{"id": "keep_me", "name": "Existing"}]}
        _, _, _, saved = _run_step("ae_confirm", "yes", self._confirm_data(_future_date()),
                                   evdata=existing)
        ids = [e["id"] for e in saved["data"]["special_events"]]
        assert "keep_me" in ids and len(ids) == 2

    def test_confirm_schedules_notification(self):
        import handlers.events_admin as ea
        ctx = _ctx()
        ctx.user_data.update(self._confirm_data(_future_date(days=3)))
        sched = MagicMock()

        async def _get():
            return {"special_events": []}

        async def _save(d):
            pass

        with patch("storage.get_all_events_data", side_effect=_get), \
             patch("storage.save_events_data", side_effect=_save), \
             patch("handlers.events_admin.schedule_event_notification", sched):
            _run(ea.ae_confirm(_upd("yes"), ctx))
        sched.assert_called()


# ---------------------------------------------------------------------------
# /modifyevent
# ---------------------------------------------------------------------------

class TestModifyEventFlow:
    def _special(self):
        return {"id": "special_abc", "name": "Youth Night", "type": "once",
                "date": _future_date(), "time": "19:00", "duration_minutes": 60,
                "notification_minutes": 90, "description": "", "url": "", "active": True}

    def test_non_admin_blocked(self):
        import handlers.events_admin as ea
        from bot import ConversationHandler
        with patch("permissions.is_admin", return_value=False):
            result = _run(ea.cmd_modifyevent(_upd(), _ctx()))
        assert result == ConversationHandler.END

    def test_entry_lists_specials(self):
        import handlers.events_admin as ea
        ctx, upd = _ctx(), _upd()

        async def _get():
            return {"special_events": [self._special()]}

        with patch("permissions.is_admin", return_value=True), \
             patch("storage.get_all_events_data", side_effect=_get):
            result = _run(ea.cmd_modifyevent(upd, ctx))
        assert result == ea.ME_SELECT
        assert "Youth Night" in upd.message.reply_text.call_args[0][0]
        assert ctx.user_data["me_specials"]

    def test_select_by_number(self):
        import handlers.events_admin as ea
        result, _, ctx, _ = _run_step("me_select", "1", {"me_specials": [self._special()]})
        assert result == ea.ME_FIELD
        assert ctx.user_data["me_event"]["id"] == "special_abc"

    def test_select_by_id(self):
        import handlers.events_admin as ea
        result, _, ctx, _ = _run_step("me_select", "special_abc",
                                      {"me_specials": [self._special()]})
        assert result == ea.ME_FIELD

    def test_select_unknown_stays(self):
        import handlers.events_admin as ea
        result, _, ctx, _ = _run_step("me_select", "nope", {"me_specials": [self._special()]})
        assert result == ea.ME_SELECT
        assert "me_event" not in ctx.user_data

    def test_invalid_field_stays(self):
        import handlers.events_admin as ea
        result, upd, ctx, _ = _run_step("me_field", "colour")
        assert result == ea.ME_FIELD
        assert "me_field" not in ctx.user_data

    def test_valid_field_advances(self):
        import handlers.events_admin as ea
        result, _, ctx, _ = _run_step("me_field", "Duration")
        assert result == ea.ME_VALUE
        assert ctx.user_data["me_field"] == "duration"

    def _value_step(self, field, value, ev=None):
        ev = ev or self._special()
        data = {"special_events": [dict(ev)]}
        return _run_step("me_value", value,
                         {"me_field": field, "me_event": ev}, evdata=data)

    def test_value_updates_int_field(self):
        from bot import ConversationHandler
        result, _, _, saved = self._value_step("duration", "120")
        assert result == ConversationHandler.END
        assert saved["data"]["special_events"][0]["duration_minutes"] == 120

    def test_value_updates_notification_int(self):
        _, _, _, saved = self._value_step("notification", "30")
        assert saved["data"]["special_events"][0]["notification_minutes"] == 30

    def test_value_coerces_active_bool(self):
        _, _, _, saved = self._value_step("active", "no")
        assert saved["data"]["special_events"][0]["active"] is False
        _, _, _, saved2 = self._value_step("active", "yes")
        assert saved2["data"]["special_events"][0]["active"] is True

    def test_value_updates_string_field(self):
        _, _, _, saved = self._value_step("name", "Youth Gathering")
        assert saved["data"]["special_events"][0]["name"] == "Youth Gathering"

    def test_value_leaves_other_events_untouched(self):
        ev = self._special()
        other = {"id": "special_other", "name": "Other", "type": "once",
                 "date": _future_date(), "time": "08:00"}
        data = {"special_events": [dict(ev), other]}
        _, _, _, saved = _run_step("me_value", "Renamed",
                                   {"me_field": "name", "me_event": ev}, evdata=data)
        names = {e["id"]: e["name"] for e in saved["data"]["special_events"]}
        assert names["special_abc"] == "Renamed"
        assert names["special_other"] == "Other"


# ---------------------------------------------------------------------------
# /deleteevent
# ---------------------------------------------------------------------------

class TestDeleteEventFlow:
    def _special_ev(self):
        return {"key": "special_abc", "name": "Youth Night", "type": "special",
                "service_time": datetime.now(TZ) + timedelta(days=3)}

    def _convocation_ev(self):
        return {"key": "sabbath_eve_x", "name": "Sabbath Eve", "type": "convocation",
                "service_time": datetime.now(TZ) + timedelta(days=2)}

    def test_non_admin_blocked(self):
        import handlers.events_admin as ea
        from bot import ConversationHandler
        with patch("permissions.is_admin", return_value=False):
            result = _run(ea.cmd_deleteevent(_upd(), _ctx()))
        assert result == ConversationHandler.END

    def test_entry_lists_upcoming(self):
        import handlers.events_admin as ea
        ctx, upd = _ctx(), _upd()

        async def _upcoming(days_ahead=30):
            return [self._special_ev(), self._convocation_ev()]

        with patch("permissions.is_admin", return_value=True), \
             patch("handlers.events_admin.all_upcoming", side_effect=_upcoming):
            result = _run(ea.cmd_deleteevent(upd, ctx))
        assert result == ea.DE_SELECT
        assert len(ctx.user_data["de_events"]) == 2
        assert "Youth Night" in upd.message.reply_text.call_args[0][0]

    def test_invalid_selection_stays(self):
        import handlers.events_admin as ea
        result, _, ctx, _ = _run_step("de_select", "9",
                                      {"de_events": [self._special_ev()]})
        assert result == ea.DE_SELECT
        assert "de_ev" not in ctx.user_data

    def test_special_selection_goes_to_confirm(self):
        import handlers.events_admin as ea
        result, _, ctx, _ = _run_step("de_select", "1",
                                      {"de_events": [self._special_ev()]})
        assert result == ea.DE_CONFIRM
        assert ctx.user_data["de_ev"]["key"] == "special_abc"

    def test_convocation_selection_goes_to_annotation(self):
        import handlers.events_admin as ea
        result, _, ctx, _ = _run_step("de_select", "1",
                                      {"de_events": [self._convocation_ev()]})
        assert result == ea.DE_ANNOT

    def test_confirm_no_keeps_event(self):
        from bot import ConversationHandler
        data = {"special_events": [{"id": "special_abc", "name": "Youth Night"}]}
        result, _, _, saved = _run_step("de_confirm", "no",
                                        {"de_ev": self._special_ev()}, evdata=data)
        assert result == ConversationHandler.END
        assert "data" not in saved

    def test_confirm_yes_deletes_only_that_event(self):
        data = {"special_events": [{"id": "special_abc", "name": "Youth Night"},
                                   {"id": "special_keep", "name": "Other"}]}
        _, _, _, saved = _run_step("de_confirm", "yes",
                                   {"de_ev": self._special_ev()}, evdata=data)
        ids = [e["id"] for e in saved["data"]["special_events"]]
        assert ids == ["special_keep"]

    def test_annotation_dash_cancels(self):
        from bot import ConversationHandler
        result, _, _, saved = _run_step("de_annot", "-",
                                        {"de_ev": self._convocation_ev()})
        assert result == ConversationHandler.END
        assert "data" not in saved

    def test_annotation_appended_to_convocation(self):
        data = {"convocation_announcements": {}}
        _, _, _, saved = _run_step("de_annot", "Service cancelled — icy roads",
                                   {"de_ev": self._convocation_ev()}, evdata=data)
        anns = saved["data"]["convocation_announcements"]["sabbath_eve_x"]
        assert anns == ["Service cancelled — icy roads"]

    def test_annotation_appends_to_existing_list(self):
        data = {"convocation_announcements": {"sabbath_eve_x": ["First note"]}}
        _, _, _, saved = _run_step("de_annot", "Second note",
                                   {"de_ev": self._convocation_ev()}, evdata=data)
        assert saved["data"]["convocation_announcements"]["sabbath_eve_x"] == [
            "First note", "Second note"]

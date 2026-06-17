"""Tests for per-service (per-phase) join links:

  - URLs attached to convocation/Sabbath events from convocation_urls map
  - URL surfaced in notifications
  - /setservicelink admin command (set + clear)
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


def _make_update(text: str = "", chat_id: int = 1) -> MagicMock:
    upd = MagicMock()
    upd.effective_chat.id = chat_id
    upd.effective_user.id = chat_id
    upd.effective_user.username = "admin"
    upd.effective_user.first_name = "Admin"
    upd.effective_user.full_name = "Admin User"
    upd.message.text = text
    upd.message.reply_text = AsyncMock()
    return upd


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.application = MagicMock()
    ctx.user_data = {}
    return ctx


# ---------------------------------------------------------------------------
# all_upcoming attaches per-phase URLs
# ---------------------------------------------------------------------------

class TestUrlAttachment:
    def _run_all_upcoming(self, urls_map):
        import bot

        sabbath_eve = {
            "key": "sabbath_eve_2099-01-02",
            "phase_key": "sabbath::Eve",
            "name": "God's Holy Convocation--Sabbath Eve",
            "service_time": TZ.localize(datetime(2099, 1, 2, 18, 0)),
            "notification_time": TZ.localize(datetime(2099, 1, 2, 16, 30)),
            "duration_minutes": 90,
            "type": "convocation",
            "announcements": [],
        }
        sabbath_morning = {
            "key": "sabbath_morning_2099-01-03",
            "phase_key": "sabbath::Morning",
            "name": "God's Holy Convocation--Sabbath Morning",
            "service_time": TZ.localize(datetime(2099, 1, 3, 11, 0)),
            "notification_time": TZ.localize(datetime(2099, 1, 3, 9, 30)),
            "duration_minutes": 90,
            "type": "convocation",
            "announcements": [],
        }

        async def _fake_events_data():
            return {"convocation_urls": urls_map, "convocation_announcements": {}, "special_events": []}

        with patch("bot.get_all_events_data", side_effect=_fake_events_data), \
             patch("bot.all_upcoming_events", return_value=[sabbath_eve, sabbath_morning]):
            events = _run(bot.all_upcoming(days_ahead=30))
        return {e["phase_key"]: e for e in events}

    def test_distinct_link_per_phase(self):
        events = self._run_all_upcoming({
            "sabbath::Eve": "https://zoom.us/eve",
            "sabbath::Morning": "https://zoom.us/morning",
        })
        assert events["sabbath::Eve"]["url"] == "https://zoom.us/eve"
        assert events["sabbath::Morning"]["url"] == "https://zoom.us/morning"

    def test_phase_without_link_has_no_url(self):
        events = self._run_all_upcoming({"sabbath::Eve": "https://zoom.us/eve"})
        assert events["sabbath::Eve"]["url"] == "https://zoom.us/eve"
        assert not events["sabbath::Morning"].get("url")

    def test_no_links_configured(self):
        events = self._run_all_upcoming({})
        assert not events["sabbath::Eve"].get("url")
        assert not events["sabbath::Morning"].get("url")


class TestSundayPrayerLink:
    """Sunday Morning Prayer is a true special event driven by events.yaml;
    its link comes from the special_events def's url, via _merge_special_events."""

    def _sunday_event(self, url):
        import bot

        defn = {
            "id": "sunday_morning_prayer",
            "name": "Sunday Morning Prayer",
            "type": "weekly",
            "weekday": 6,
            "time": "06:00",
            "duration_minutes": 20,
            "notification_minutes": 720,
            "description": "Weekly Sunday Morning Prayer service — all are welcome.",
            "url": url,
            "active": True,
        }
        results = bot._merge_special_events([defn], {}, days_ahead=14)
        return results[0] if results else None

    def test_sunday_is_type_special(self):
        ev = self._sunday_event("https://zoom.us/sunday")
        assert ev is not None
        assert ev["type"] == "special"

    def test_sunday_link_attached_from_special_def(self):
        ev = self._sunday_event("https://zoom.us/sunday")
        assert ev["url"] == "https://zoom.us/sunday"

    def test_sunday_no_link_when_url_blank(self):
        ev = self._sunday_event("")
        assert not ev.get("url")

    def test_sunday_not_duplicated_in_all_upcoming(self):
        """Sunday must appear once (via _merge_special_events), not twice."""
        import bot

        async def _fake_events_data():
            return {
                "convocation_urls": {},
                "convocation_announcements": {},
                "special_events": [{
                    "id": "sunday_morning_prayer",
                    "name": "Sunday Morning Prayer",
                    "type": "weekly", "weekday": 6, "time": "06:00",
                    "duration_minutes": 20, "notification_minutes": 720,
                    "description": "x", "url": "https://zoom.us/sunday", "active": True,
                }],
            }

        with patch("bot.get_all_events_data", side_effect=_fake_events_data):
            events = _run(bot.all_upcoming(days_ahead=14))
        sundays = [e for e in events if "Sunday Morning Prayer" in e["name"]]
        assert len(sundays) >= 1
        assert all(s["url"] == "https://zoom.us/sunday" for s in sundays)
        # No two share the same service_time (would indicate a duplicate).
        times = [s["service_time"] for s in sundays]
        assert len(times) == len(set(times))


# ---------------------------------------------------------------------------
# Notification includes the join link
# ---------------------------------------------------------------------------

class TestNotificationLink:
    def _run_notify(self, event):
        import bot

        ctx = _make_context()
        ctx.job = MagicMock()
        ctx.job.data = event

        async def _fake_users():
            return [{"chat_id": 111}]

        async def _fake_save(u):
            pass

        with patch("bot.get_all_users", side_effect=_fake_users), \
             patch("bot.save_users", side_effect=_fake_save):
            _run(bot.send_notification(ctx))
        return ctx.bot.send_message.call_args[0][1]

    def _event(self, **extra):
        ev = {
            "name": "God's Holy Convocation--Sabbath Eve",
            "service_time": TZ.localize(datetime(2099, 1, 2, 18, 0)),
            "announcements": [],
        }
        ev.update(extra)
        return ev

    def test_link_included_when_present(self):
        msg = self._run_notify(self._event(url="https://zoom.us/eve"))
        assert "https://zoom.us/eve" in msg

    def test_no_link_line_when_absent(self):
        msg = self._run_notify(self._event())
        assert "Join" not in msg


# ---------------------------------------------------------------------------
# /setservicelink command
# ---------------------------------------------------------------------------

class TestSetServiceLink:
    def test_lists_phases_and_enters_select(self):
        import bot
        from bot import SL_SELECT

        ctx = _make_context()
        upd = _make_update()

        async def _fake_events_data():
            return {"convocation_urls": {"sabbath::Eve": "https://old"}}

        with patch("bot.is_admin", return_value=True), \
             patch("bot.get_all_events_data", side_effect=_fake_events_data):
            result = _run(bot.cmd_setservicelink(upd, ctx))

        assert result == SL_SELECT
        text = upd.message.reply_text.call_args[0][0]
        assert "Sabbath — Eve" in text
        assert "https://old" in text  # shows current link
        assert ctx.user_data["sl_phases"]

    def test_non_admin_blocked(self):
        import bot
        from bot import ConversationHandler

        ctx = _make_context()
        upd = _make_update()
        with patch("bot.is_admin", return_value=False):
            result = _run(bot.cmd_setservicelink(upd, ctx))
        assert result == ConversationHandler.END

    def test_select_valid_advances_to_url(self):
        import bot
        from bot import SL_URL, service_phases

        ctx = _make_context()
        ctx.user_data["sl_phases"] = service_phases()
        upd = _make_update(text="1")
        result = _run(bot.sl_select(upd, ctx))
        assert result == SL_URL
        assert ctx.user_data["sl_phase"]["phase_key"] == "sabbath::Eve"

    def test_select_invalid_stays(self):
        import bot
        from bot import SL_SELECT, service_phases

        ctx = _make_context()
        ctx.user_data["sl_phases"] = service_phases()
        upd = _make_update(text="999")
        result = _run(bot.sl_select(upd, ctx))
        assert result == SL_SELECT

    def _run_sl_url(self, text, phase_key="sabbath::Eve", existing=None):
        import bot
        from hebrew_calendar import service_phases

        ctx = _make_context()
        ph = next(p for p in service_phases() if p["phase_key"] == phase_key)
        ctx.user_data["sl_phase"] = ph
        upd = _make_update(text=text)

        saved = {}
        data = {"convocation_urls": dict(existing or {})}

        async def _fake_get():
            return data

        async def _fake_save(d):
            saved.update(d)

        async def _fake_schedule(app):
            pass

        with patch("bot.get_all_events_data", side_effect=_fake_get), \
             patch("bot.save_events_data", side_effect=_fake_save), \
             patch("bot.schedule_all_upcoming", side_effect=_fake_schedule):
            result = _run(bot.sl_url(upd, ctx))
        return result, upd, saved

    def test_set_link_saved(self):
        from bot import ConversationHandler
        result, upd, saved = self._run_sl_url("https://zoom.us/new")
        assert result == ConversationHandler.END
        assert saved["convocation_urls"]["sabbath::Eve"] == "https://zoom.us/new"

    def test_clear_link_removes_entry(self):
        result, upd, saved = self._run_sl_url("-", existing={"sabbath::Eve": "https://old"})
        assert "sabbath::Eve" not in saved["convocation_urls"]

    def test_set_does_not_disturb_other_phase(self):
        result, upd, saved = self._run_sl_url(
            "https://zoom.us/eve", existing={"sabbath::Morning": "https://morning"})
        assert saved["convocation_urls"]["sabbath::Morning"] == "https://morning"
        assert saved["convocation_urls"]["sabbath::Eve"] == "https://zoom.us/eve"

    def test_set_reschedules_notifications(self):
        import bot
        from hebrew_calendar import service_phases

        ctx = _make_context()
        ctx.user_data["sl_phase"] = next(
            p for p in service_phases() if p["phase_key"] == "sabbath::Eve")
        upd = _make_update(text="https://zoom.us/new")

        data = {"convocation_urls": {}}
        reschedule = AsyncMock()

        async def _fake_get():
            return data

        async def _fake_save(d):
            pass

        with patch("bot.get_all_events_data", side_effect=_fake_get), \
             patch("bot.save_events_data", side_effect=_fake_save), \
             patch("bot.schedule_all_upcoming", reschedule):
            _run(bot.sl_url(upd, ctx))

        reschedule.assert_called_once()

"""Tests for the 2026-09-11/12 double-notification bug and its two fixes.

Rosh Hashanah fell on the Sabbath. Two things went wrong:

1. Two reminders whose notification_time was the same instant raced on
   notification_state; the second write erased the first, and the catch-up job
   re-sent the lost one ~85 seconds later.
2. Even without the race, the weekend produced two separate announcements for
   what is one gathering.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

import handlers.notifications as N  # noqa: E402
import storage  # noqa: E402
from cache import FileCache  # noqa: E402
from hebrew_calendar import merge_sabbath_coincidences  # noqa: E402

TZ = pytz.timezone("America/New_York")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _sabbath(label="Eve", hour=18, date=(2026, 9, 11), notify_min=90):
    svc = TZ.localize(datetime(date[0], date[1], date[2], hour, 0))
    return {
        "key": f"sabbath_{label.lower()}_{date[0]}-{date[1]:02d}-{date[2]:02d}",
        "phase_key": f"sabbath::{label}",
        "name": f"God's Holy Convocation—Sabbath {label}",
        "convocation_key": "sabbath", "convocation_name": "Sabbath",
        "phase": None, "label": label, "service_time": svc,
        "notification_time": svc - timedelta(minutes=notify_min),
        "duration_minutes": 90, "type": "convocation", "announcements": [],
    }


def _festival(key="rosh_hashanah", name="Rosh Hashanah", label="Eve", hour=18,
              date=(2026, 9, 11), notify_min=90, phase=None):
    svc = TZ.localize(datetime(date[0], date[1], date[2], hour, 0))
    return {
        "key": f"{key}_{label.lower()}_{date[0]}-{date[1]:02d}-{date[2]:02d}",
        "phase_key": f"{key}::{label}",
        "name": f"God's Holy Convocation—{name} {label}",
        "convocation_key": key, "convocation_name": name,
        "phase": phase, "label": label, "service_time": svc,
        "notification_time": svc - timedelta(minutes=notify_min),
        "duration_minutes": 60, "type": "convocation", "announcements": [],
        "biblical_month": "Ethanim", "hebrew_year": 5787,
    }


# ---------------------------------------------------------------------------
# 1. The duplicate-send race
# ---------------------------------------------------------------------------

class TestNoDuplicateNotifications:
    """The actual bug the congregation saw."""

    @staticmethod
    def _isolate():
        tmp = Path(tempfile.mkdtemp())
        storage.notif_state_cache = FileCache(tmp / "notification_state.yaml")

    @staticmethod
    def _event(key, name):
        now = N.now_tz()
        return {"key": key, "name": name, "type": "convocation",
                "service_time": now + timedelta(minutes=90),
                "notification_time": now, "target_chat_ids": [-100123],
                "announcements": [], "url": "", "description": ""}

    def _deliver_both(self, sends):
        async def fake_send(bot, chat_id, media, text, caches):
            sends.append(chat_id)
            await asyncio.sleep(0.01)  # real network latency: forces interleaving

        async def go():
            with patch.object(N, "_send_notification_payload", fake_send), \
                 patch.object(storage, "get_all_users", AsyncMock(return_value=[])):
                await asyncio.gather(
                    N.deliver_event_notifications(AsyncMock(), self._event("sab", "Sabbath Eve")),
                    N.deliver_event_notifications(AsyncMock(), self._event("rh", "Rosh Eve")),
                )
        _run(go())

    def test_simultaneous_reminders_both_record_their_delivery(self):
        """The lost write: the second save used to erase the first."""
        self._isolate()
        self._deliver_both([])
        states = _run(storage._load_notif_state())
        assert sorted(states) == ["rh", "sab"], (
            "both simultaneous reminders must persist their delivery record; "
            "losing one makes the catch-up job re-send it"
        )

    def test_catchup_does_not_resend_after_simultaneous_delivery(self):
        """End-to-end reproduction of what the congregation experienced."""
        self._isolate()
        sends: list = []
        self._deliver_both(sends)
        first_round = len(sends)
        self._deliver_both(sends)  # the catch-up job, 85s later
        assert first_round == 2
        assert len(sends) == first_round, (
            f"catch-up re-sent {len(sends) - first_round} reminder(s) — "
            "this is the duplicate the congregation received"
        )

    def test_delivery_is_still_recorded_when_nothing_races(self):
        """Guard against 'fixing' the race by never writing state at all."""
        self._isolate()

        async def go():
            async def fake_send(bot, chat_id, media, text, caches):
                return None
            with patch.object(N, "_send_notification_payload", fake_send), \
                 patch.object(storage, "get_all_users", AsyncMock(return_value=[])):
                await N.deliver_event_notifications(AsyncMock(), self._event("solo", "Solo"))
            return await storage._load_notif_state()

        states = _run(go())
        assert states["solo"]["notified"] == [-100123]

    def test_a_concurrent_write_does_not_erase_an_existing_record(self):
        """Delivery must merge into current state, not overwrite a snapshot."""
        self._isolate()
        _run(storage._save_notif_state(
            {"other": {"name": "Other", "service_time": "x", "notified": [-999]}}))

        async def go():
            async def fake_send(bot, chat_id, media, text, caches):
                await asyncio.sleep(0.01)
            with patch.object(N, "_send_notification_payload", fake_send), \
                 patch.object(storage, "get_all_users", AsyncMock(return_value=[])):
                await N.deliver_event_notifications(AsyncMock(), self._event("new", "New"))
            return await storage._load_notif_state()

        states = _run(go())
        assert "other" in states, "an unrelated event's record was erased"
        assert states["other"]["notified"] == [-999]


# ---------------------------------------------------------------------------
# 2. Combining a convocation that falls on the Sabbath
# ---------------------------------------------------------------------------

class TestMergeSabbathCoincidence:
    def test_coincident_services_become_one_event(self):
        merged = merge_sabbath_coincidences([_sabbath(), _festival()])
        assert len(merged) == 1

    def test_combined_title_leads_with_sabbath(self):
        merged = merge_sabbath_coincidences([_sabbath(), _festival()])
        assert merged[0]["name"] == "God's Holy Convocation—Sabbath & Rosh Hashanah Eve"

    def test_non_coincident_services_stay_separate(self):
        """A festival at a different hour is a different gathering."""
        merged = merge_sabbath_coincidences([_sabbath(hour=18), _festival(hour=20)])
        assert len(merged) == 2

    def test_ordinary_sabbath_is_untouched(self):
        merged = merge_sabbath_coincidences([_sabbath()])
        assert len(merged) == 1
        assert merged[0]["name"] == "God's Holy Convocation—Sabbath Eve"
        assert merged[0].get("combined") is not True

    def test_festival_without_sabbath_is_untouched(self):
        merged = merge_sabbath_coincidences([_festival(hour=20)])
        assert len(merged) == 1
        assert merged[0].get("combined") is not True

    def test_earliest_notification_time_wins(self):
        """Nobody should be reminded later than they would have been."""
        merged = merge_sabbath_coincidences(
            [_sabbath(notify_min=90), _festival(notify_min=30)])
        assert merged[0]["notification_time"] == _sabbath(notify_min=90)["notification_time"]

    def test_longest_duration_wins(self):
        """The calendar entry must not end before the service does."""
        merged = merge_sabbath_coincidences([_sabbath(), _festival()])
        assert merged[0]["duration_minutes"] == 90

    def test_both_phase_keys_are_carried_sabbath_first(self):
        """Sabbath configuration takes precedence for a combined service."""
        merged = merge_sabbath_coincidences([_sabbath(), _festival()])
        assert merged[0]["phase_keys"] == ["sabbath::Eve", "rosh_hashanah::Eve"]

    def test_component_keys_are_carried_sabbath_first(self):
        """Reading order, so announcements match the order in the title."""
        merged = merge_sabbath_coincidences([_sabbath(), _festival()])
        assert merged[0]["component_keys"] == [_sabbath()["key"], _festival()["key"]]

    def test_key_is_deterministic(self):
        """State tracking and job ids depend on a stable key across restarts."""
        a = merge_sabbath_coincidences([_sabbath(), _festival()])[0]["key"]
        b = merge_sabbath_coincidences([_festival(), _sabbath()])[0]["key"]
        assert a == b

    def test_key_differs_from_either_component(self):
        """Otherwise the combined event inherits a component's delivery state."""
        merged = merge_sabbath_coincidences([_sabbath(), _festival()])
        assert merged[0]["key"] not in (_sabbath()["key"], _festival()["key"])

    def test_festival_metadata_survives(self):
        """Built from the festival, so its calendar context is not lost."""
        merged = merge_sabbath_coincidences([_sabbath(), _festival()])
        assert merged[0]["biblical_month"] == "Ethanim"
        assert merged[0]["hebrew_year"] == 5787

    def test_still_a_convocation_for_opt_in_purposes(self):
        """Category is derived from type; anyone opted into convocations gets it."""
        from common import _event_category
        merged = merge_sabbath_coincidences([_sabbath(), _festival()])
        assert _event_category(merged[0]) == "convocations"

    def test_morning_services_merge_too(self):
        merged = merge_sabbath_coincidences([
            _sabbath(label="Morning", hour=11),
            _festival(label="Morning", hour=11),
        ])
        assert len(merged) == 1
        assert merged[0]["name"] == "God's Holy Convocation—Sabbath & Rosh Hashanah Morning"

    def test_a_phased_festival_keeps_its_phase_in_the_title(self):
        merged = merge_sabbath_coincidences([
            _sabbath(),
            _festival(key="unleavened_bread_opening", name="Unleavened Bread",
                      phase="Opening"),
        ])
        assert merged[0]["name"] == (
            "God's Holy Convocation—Sabbath & Unleavened Bread Opening Eve")

    def test_output_is_sorted_by_service_time(self):
        merged = merge_sabbath_coincidences([
            _festival(hour=20), _sabbath(hour=18), _festival(hour=18)])
        times = [e["service_time"] for e in merged]
        assert times == sorted(times)

    def test_whole_weekend_produces_two_events_not_four(self):
        """The actual 2026-09-11/12 weekend: Eve and Morning, once each."""
        merged = merge_sabbath_coincidences([
            _sabbath(label="Eve", hour=18, date=(2026, 9, 11)),
            _festival(label="Eve", hour=18, date=(2026, 9, 11)),
            _sabbath(label="Morning", hour=11, date=(2026, 9, 12)),
            _festival(label="Morning", hour=11, date=(2026, 9, 12)),
        ])
        assert len(merged) == 2
        assert all(e["combined"] for e in merged)


class TestMergeIsActuallyWired:
    """Merging correctly is worthless if nothing calls it."""

    def test_all_upcoming_events_applies_the_merge(self):
        import hebrew_calendar as hc
        with patch.object(hc, "sabbath_events", lambda tz, d=90: [_sabbath()]), \
             patch.object(hc, "upcoming_convocation_events", lambda tz, d=90: [_festival()]):
            events = hc.all_upcoming_events(TZ, 30)
        assert len(events) == 1, "all_upcoming_events must merge coincident services"
        assert events[0]["name"] == "God's Holy Convocation—Sabbath & Rosh Hashanah Eve"

    def test_two_festivals_on_one_sabbath_have_a_stable_order(self):
        """Ordering must not depend on input order, or the key/title would drift."""
        a = _festival(key="succoth_closing", name="Succoth Closing")
        b = _festival(key="rosh_hashanah", name="Rosh Hashanah")
        first = merge_sabbath_coincidences([_sabbath(), a, b])[0]
        second = merge_sabbath_coincidences([_sabbath(), b, a])[0]
        assert first["name"] == second["name"]
        assert first["key"] == second["key"]
        assert first["phase_keys"] == second["phase_keys"]


class TestCombinedEventPlumbing:
    """events.all_upcoming must resolve config against BOTH identities."""

    @staticmethod
    def _all_upcoming(evdata):
        import events as E
        with patch.object(E, "all_upcoming_events",
                          lambda tz, d: merge_sabbath_coincidences([_sabbath(), _festival()])), \
             patch.object(E.storage, "get_all_events_data",
                          AsyncMock(return_value=evdata)):
            return _run(E.all_upcoming(30))

    def test_announcements_from_either_component_are_included(self):
        evs = self._all_upcoming({"convocation_announcements": {
            _sabbath()["key"]: ["Bring a dish"],
            _festival()["key"]: ["Shofar at sunset"],
        }})
        assert evs[0]["announcements"] == ["Bring a dish", "Shofar at sunset"]

    def test_announcements_are_not_duplicated(self):
        evs = self._all_upcoming({"convocation_announcements": {
            _sabbath()["key"]: ["Same notice"],
            _festival()["key"]: ["Same notice"],
        }})
        assert evs[0]["announcements"] == ["Same notice"]

    def test_sabbath_join_link_wins_even_when_the_festival_has_one(self):
        """The congregation gathers in the standing Sabbath room."""
        evs = self._all_upcoming({"convocation_urls": {
            "sabbath::Eve": "https://zoom.test/sabbath",
            "rosh_hashanah::Eve": "https://zoom.test/rosh",
        }})
        assert evs[0]["url"] == "https://zoom.test/sabbath"

    def test_sabbath_link_used_when_the_festival_has_none(self):
        evs = self._all_upcoming({"convocation_urls": {
            "sabbath::Eve": "https://zoom.test/sabbath"}})
        assert evs[0]["url"] == "https://zoom.test/sabbath"

    def test_festival_link_used_only_when_the_sabbath_has_none(self):
        """Fallback, not precedence: better some link than none."""
        evs = self._all_upcoming({"convocation_urls": {
            "rosh_hashanah::Eve": "https://zoom.test/rosh"}})
        assert evs[0]["url"] == "https://zoom.test/rosh"

    def test_sabbath_image_wins(self):
        """Sabbath-first applies to media as well as the join link."""
        evs = self._all_upcoming({"convocation_images": {
            "sabbath::Eve": "media/sabbath.jpg",
            "rosh_hashanah::Eve": "media/rosh.jpg",
        }})
        assert evs[0]["image"] == "media/sabbath.jpg"

    def test_sabbath_document_wins(self):
        evs = self._all_upcoming({"convocation_documents": {
            "sabbath::Eve": "media/sabbath-order.pdf",
            "rosh_hashanah::Eve": "media/rosh-order.pdf",
        }})
        assert evs[0]["document"] == "media/sabbath-order.pdf"

    def test_sabbath_notification_targets_win(self):
        """Who gets told follows the Sabbath's configuration too."""
        evs = self._all_upcoming({
            "notification_targets": {"sab_group": -100, "rh_group": -200},
            "convocation_targets": {
                "sabbath::Eve": ["sab_group"],
                "rosh_hashanah::Eve": ["rh_group"],
            },
        })
        assert evs[0]["target_chat_ids"] == [-100]

    def test_festival_media_used_only_as_a_fallback(self):
        evs = self._all_upcoming({"convocation_images": {
            "rosh_hashanah::Eve": "media/rosh.jpg"}})
        assert evs[0]["image"] == "media/rosh.jpg"


class TestStableCalendarUids:
    """Exporting twice must not duplicate every service in people's calendars.

    Calendar clients key on UID: same UID replaces the held entry, a different
    one adds a second copy. The export used to mint a fresh uuid4() per run.
    """

    @staticmethod
    def _uids(events):
        from ics_generator import events_to_ics
        return [ln.split(":", 1)[1] for ln in events_to_ics(events).decode().splitlines()
                if ln.startswith("UID:")]

    def test_two_exports_of_the_same_event_share_a_uid(self):
        """The bug: re-exporting duplicated everything."""
        assert self._uids([_sabbath()]) == self._uids([_sabbath()])

    def test_different_events_get_different_uids(self):
        """Guard against 'fixing' it with one constant UID for everything."""
        uids = self._uids([_sabbath(), _festival(hour=20)])
        assert len(uids) == 2 and uids[0] != uids[1]

    def test_successive_sabbaths_are_distinct(self):
        """Weekly services must not collapse into a single calendar entry."""
        a = self._uids([_sabbath(date=(2026, 9, 11))])
        b = self._uids([_sabbath(date=(2026, 9, 18))])
        assert a != b

    def test_uid_is_derived_from_the_event_key(self):
        uid = self._uids([_sabbath()])[0]
        assert uid.startswith(_sabbath()["key"] + "@")

    def test_combined_event_has_one_stable_uid(self):
        merged = merge_sabbath_coincidences([_sabbath(), _festival()])
        assert len(self._uids(merged)) == 1
        assert self._uids(merged) == self._uids(merged)

    def test_uid_survives_a_changed_time_or_link(self):
        """A rescheduled service should UPDATE the entry, not add a new one."""
        moved = dict(_sabbath())
        moved["service_time"] = moved["service_time"] + timedelta(minutes=30)
        moved["url"] = "https://zoom.test/changed"
        assert self._uids([moved]) == self._uids([_sabbath()])

    def test_export_carries_a_sequence_for_updates(self):
        from ics_generator import events_to_ics
        assert "SEQUENCE:" in events_to_ics([_sabbath()]).decode()

    def test_event_without_a_key_still_gets_a_stable_uid(self):
        """Fallback path: deterministic, not random."""
        keyless = {k: v for k, v in _sabbath().items() if k != "key"}
        assert self._uids([keyless]) == self._uids([keyless])


class TestIcsSequenceRevisions:
    """SEQUENCE must track significant revisions (RFC 5545 3.8.7.4).

    Significant = the gathering moved in time, so anyone holding it has to
    re-decide. A rename or reworded description is not.
    """

    @staticmethod
    def _modify(field, value, start=None):
        """Drive the real /modifyevent value step and return the stored event."""
        import handlers.events_admin as EA
        ev = {"id": "sunday_morning_prayer", "name": "Sunday Morning Prayer",
              "type": "weekly", "weekday": 6, "time": "09:00", "date": "2026-10-04",
              "duration_minutes": 60, "notification_minutes": 90,
              "description": "Prayer", "active": True, "url": ""}
        if start is not None:
            ev["sequence"] = start
        upd = MagicMock()
        upd.message.text = value
        upd.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.user_data = {"me_field": field, "me_event": ev}
        ctx.application = MagicMock()
        with patch.object(EA.storage, "get_all_events_data",
                          AsyncMock(return_value={"special_events": [ev]})), \
             patch.object(EA.storage, "save_events_data", AsyncMock()), \
             patch.object(EA, "schedule_event_notification", lambda a, e: None), \
             patch.object(EA.activity, "log_command", lambda *a, **k: None):
            _run(EA.me_value(upd, ctx))
        return ev

    def test_changing_the_time_bumps_sequence(self):
        assert self._modify("time", "10:30").get("sequence") == 1

    def test_changing_the_date_bumps_sequence(self):
        assert self._modify("date", "2026-10-11").get("sequence") == 1

    def test_changing_the_duration_bumps_sequence(self):
        assert self._modify("duration", "90").get("sequence") == 1

    def test_renaming_does_not_bump_sequence(self):
        """A rename is not something attendees must re-decide about."""
        assert self._modify("name", "Morning Prayer").get("sequence", 0) == 0

    def test_description_edit_does_not_bump_sequence(self):
        assert self._modify("description", "New wording").get("sequence", 0) == 0

    def test_url_change_does_not_bump_sequence(self):
        """URL is not a significant revision under RFC 5545."""
        assert self._modify("url", "https://zoom.test/new").get("sequence", 0) == 0

    def test_setting_the_same_value_does_not_bump_sequence(self):
        """Re-saving an unchanged value is not a revision."""
        assert self._modify("time", "09:00").get("sequence", 0) == 0

    def test_sequence_increments_from_its_previous_value(self):
        """Monotonic: clients treat a lower SEQUENCE as stale."""
        assert self._modify("time", "11:00", start=4).get("sequence") == 5

    def test_sequence_reaches_the_exported_ics(self):
        """The counter is worthless if the export never reads it."""
        from ics_generator import events_to_ics
        ev = dict(_sabbath())
        ev["sequence"] = 3
        assert "SEQUENCE:3" in events_to_ics([ev]).decode()

    def test_special_event_sequence_survives_expansion(self):
        """_merge_special_events must carry it through to the export."""
        from events import _merge_special_events
        defn = {"id": "x", "name": "X", "type": "once", "date": "2099-01-01",
                "time": "10:00", "active": True, "sequence": 7}
        out = _merge_special_events([defn], {}, days_ahead=40000)
        assert out and out[0]["sequence"] == 7

    def test_weekly_event_sequence_survives_expansion(self):
        """The weekly branch expands separately from 'once' — cover both."""
        from events import _merge_special_events
        defn = {"id": "sunday_morning_prayer", "name": "Sunday Morning Prayer",
                "type": "weekly", "weekday": 6, "time": "09:00",
                "active": True, "sequence": 5}
        out = _merge_special_events([defn], {}, days_ahead=14)
        assert out, "expected at least one weekly occurrence in the next 14 days"
        assert all(e["sequence"] == 5 for e in out)

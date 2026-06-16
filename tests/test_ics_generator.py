"""Tests for ics_generator.py — ICS calendar file generation."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import pytest
from icalendar import Calendar

sys.path.insert(0, str(Path(__file__).parent.parent))
from ics_generator import appointment_to_ics, events_to_ics

TZ = pytz.timezone("America/New_York")


def _parse(ics_bytes: bytes) -> Calendar:
    return Calendar.from_ical(ics_bytes)


def _events_from_cal(cal: Calendar) -> list:
    return [c for c in cal.walk() if c.name == "VEVENT"]


def _make_event(name: str, offset_days: int = 1, duration: int = 60, desc: str = "") -> dict:
    svc = TZ.localize(datetime.now().replace(microsecond=0) + timedelta(days=offset_days))
    return {
        "name": name,
        "service_time": svc,
        "duration_minutes": duration,
        "description": desc,
        "announcements": [],
    }


# ---------------------------------------------------------------------------
# events_to_ics
# ---------------------------------------------------------------------------

class TestEventsToIcs:
    def test_returns_bytes(self):
        result = events_to_ics([])
        assert isinstance(result, bytes)

    def test_empty_list_produces_valid_calendar(self):
        cal = _parse(events_to_ics([]))
        assert cal is not None

    def test_single_event_appears_in_calendar(self):
        ev = _make_event("Sabbath Eve")
        vevents = _events_from_cal(_parse(events_to_ics([ev])))
        assert len(vevents) == 1

    def test_event_summary_matches_name(self):
        ev = _make_event("God's Holy Convocation--Sabbath Eve")
        vevents = _events_from_cal(_parse(events_to_ics([ev])))
        assert str(vevents[0]["SUMMARY"]) == "God's Holy Convocation--Sabbath Eve"

    def test_multiple_events_all_included(self):
        evs = [_make_event(f"Event {i}", offset_days=i + 1) for i in range(5)]
        vevents = _events_from_cal(_parse(events_to_ics(evs)))
        assert len(vevents) == 5

    def test_event_dtstart_matches_service_time(self):
        svc = TZ.localize(datetime(2025, 6, 20, 18, 0, 0))
        ev = {
            "name": "Test",
            "service_time": svc,
            "duration_minutes": 90,
            "description": "",
            "announcements": [],
        }
        vevents = _events_from_cal(_parse(events_to_ics([ev])))
        dtstart = vevents[0].decoded("dtstart")
        assert dtstart == svc

    def test_event_dtend_is_dtstart_plus_duration(self):
        svc = TZ.localize(datetime(2025, 6, 20, 18, 0, 0))
        ev = {
            "name": "Test",
            "service_time": svc,
            "duration_minutes": 90,
            "description": "",
            "announcements": [],
        }
        vevents = _events_from_cal(_parse(events_to_ics([ev])))
        dtstart = vevents[0].decoded("dtstart")
        dtend = vevents[0].decoded("dtend")
        assert dtend - dtstart == timedelta(minutes=90)

    def test_description_included(self):
        ev = _make_event("Test", desc="Weekly prayer service")
        vevents = _events_from_cal(_parse(events_to_ics([ev])))
        assert "Weekly prayer service" in str(vevents[0].get("DESCRIPTION", ""))

    def test_announcements_appended_to_description(self):
        ev = _make_event("Test")
        ev["announcements"] = ["Venue change: Main Hall"]
        vevents = _events_from_cal(_parse(events_to_ics([ev])))
        desc = str(vevents[0].get("DESCRIPTION", ""))
        assert "Venue change" in desc

    def test_each_event_has_unique_uid(self):
        evs = [_make_event(f"Event {i}", offset_days=i + 1) for i in range(10)]
        vevents = _events_from_cal(_parse(events_to_ics(evs)))
        uids = [str(e["UID"]) for e in vevents]
        assert len(uids) == len(set(uids))

    def test_calendar_name_set(self):
        ics = events_to_ics([], calendar_name="My Church")
        cal = _parse(ics)
        assert "My Church" in str(cal.get("X-WR-CALNAME", ""))

    def test_prodid_present(self):
        ics = events_to_ics([])
        cal = _parse(ics)
        assert cal.get("PRODID") is not None


# ---------------------------------------------------------------------------
# appointment_to_ics
# ---------------------------------------------------------------------------

class TestAppointmentToIcs:
    def _make_appt(self, dt: datetime | None = None) -> dict:
        confirmed = dt or TZ.localize(datetime(2025, 7, 15, 10, 0, 0))
        return {
            "id": "ABC123",
            "official_name": "Pastor Crowdy",
            "confirmed_datetime": confirmed,
            "description": "Discuss upcoming event",
            "duration_minutes": 30,
        }

    def test_returns_bytes(self):
        assert isinstance(appointment_to_ics(self._make_appt(), TZ), bytes)

    def test_produces_valid_calendar(self):
        cal = _parse(appointment_to_ics(self._make_appt(), TZ))
        assert cal is not None

    def test_single_vevent(self):
        vevents = _events_from_cal(_parse(appointment_to_ics(self._make_appt(), TZ)))
        assert len(vevents) == 1

    def test_summary_includes_official_name(self):
        vevents = _events_from_cal(_parse(appointment_to_ics(self._make_appt(), TZ)))
        assert "Pastor Crowdy" in str(vevents[0]["SUMMARY"])

    def test_description_matches(self):
        vevents = _events_from_cal(_parse(appointment_to_ics(self._make_appt(), TZ)))
        assert "Discuss upcoming event" in str(vevents[0].get("DESCRIPTION", ""))

    def test_uid_contains_appt_id(self):
        vevents = _events_from_cal(_parse(appointment_to_ics(self._make_appt(), TZ)))
        assert "ABC123" in str(vevents[0]["UID"])

    def test_dtstart_matches_confirmed_datetime(self):
        confirmed = TZ.localize(datetime(2025, 8, 1, 14, 30, 0))
        vevents = _events_from_cal(_parse(appointment_to_ics(self._make_appt(confirmed), TZ)))
        assert vevents[0].decoded("dtstart") == confirmed

    def test_naive_datetime_gets_localised(self):
        appt = self._make_appt()
        appt["confirmed_datetime"] = datetime(2025, 9, 1, 9, 0, 0)  # naive
        # Should not raise
        result = appointment_to_ics(appt, TZ)
        assert isinstance(result, bytes)

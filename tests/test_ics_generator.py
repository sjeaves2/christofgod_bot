"""Tests for ics_generator.py — ICS calendar file generation."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import pytest
from icalendar import Calendar

sys.path.insert(0, str(Path(__file__).parent.parent))
from ics_generator import (
    appointment_cancellation_to_ics,
    appointment_to_ics,
    events_to_ics,
    _ALARM_OFFSETS,
)

TZ = pytz.timezone("America/New_York")


def _parse(ics_bytes: bytes) -> Calendar:
    return Calendar.from_ical(ics_bytes)


def _events_from_cal(cal: Calendar) -> list:
    return [c for c in cal.walk() if c.name == "VEVENT"]


def _alarms_from_vevent(vevent) -> list:
    return [c for c in vevent.walk() if c.name == "VALARM"]


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


# ---------------------------------------------------------------------------
# VALARM reminders
# ---------------------------------------------------------------------------

class TestVAlarms:
    """Both events_to_ics and appointment_to_ics must embed VALARM components."""

    def _event_vevent(self) -> object:
        ev = {
            "name": "Test Event",
            "service_time": TZ.localize(datetime(2025, 10, 1, 18, 0, 0)),
            "duration_minutes": 60,
            "description": "",
            "announcements": [],
        }
        return _events_from_cal(_parse(events_to_ics([ev])))[0]

    def _appt_vevent(self) -> object:
        appt = {
            "id": "X1",
            "official_name": "Pastor Test",
            "confirmed_datetime": TZ.localize(datetime(2025, 10, 1, 10, 0, 0)),
            "description": "",
            "duration_minutes": 30,
        }
        return _events_from_cal(_parse(appointment_to_ics(appt, TZ)))[0]

    # --- event alarms ---

    def test_event_has_alarms(self):
        assert len(_alarms_from_vevent(self._event_vevent())) > 0

    def test_event_alarm_count_matches_offsets(self):
        assert len(_alarms_from_vevent(self._event_vevent())) == len(_ALARM_OFFSETS)

    def test_event_has_two_hour_alarm(self):
        alarms = _alarms_from_vevent(self._event_vevent())
        triggers = [a.decoded("trigger") for a in alarms]
        assert timedelta(hours=-2) in triggers

    def test_event_has_one_hour_alarm(self):
        alarms = _alarms_from_vevent(self._event_vevent())
        triggers = [a.decoded("trigger") for a in alarms]
        assert timedelta(hours=-1) in triggers

    def test_event_alarm_action_is_display(self):
        for alarm in _alarms_from_vevent(self._event_vevent()):
            assert str(alarm["ACTION"]).upper() == "DISPLAY"

    def test_event_alarm_description_mentions_reminder(self):
        for alarm in _alarms_from_vevent(self._event_vevent()):
            assert "Reminder" in str(alarm.get("DESCRIPTION", ""))

    def test_event_alarm_two_hour_description_mentions_2_hours(self):
        alarms = _alarms_from_vevent(self._event_vevent())
        two_hour = next(a for a in alarms if a.decoded("trigger") == timedelta(hours=-2))
        assert "2 hour" in str(two_hour.get("DESCRIPTION", ""))

    def test_event_alarm_one_hour_description_mentions_1_hour(self):
        alarms = _alarms_from_vevent(self._event_vevent())
        one_hour = next(a for a in alarms if a.decoded("trigger") == timedelta(hours=-1))
        assert "1 hour" in str(one_hour.get("DESCRIPTION", ""))

    # --- appointment alarms ---

    def test_appointment_has_alarms(self):
        assert len(_alarms_from_vevent(self._appt_vevent())) > 0

    def test_appointment_alarm_count_matches_offsets(self):
        assert len(_alarms_from_vevent(self._appt_vevent())) == len(_ALARM_OFFSETS)

    def test_appointment_has_two_hour_alarm(self):
        alarms = _alarms_from_vevent(self._appt_vevent())
        triggers = [a.decoded("trigger") for a in alarms]
        assert timedelta(hours=-2) in triggers

    def test_appointment_has_one_hour_alarm(self):
        alarms = _alarms_from_vevent(self._appt_vevent())
        triggers = [a.decoded("trigger") for a in alarms]
        assert timedelta(hours=-1) in triggers

    def test_appointment_alarm_action_is_display(self):
        for alarm in _alarms_from_vevent(self._appt_vevent()):
            assert str(alarm["ACTION"]).upper() == "DISPLAY"

    # --- url in ics ---

    def test_event_with_url_includes_url_property(self):
        ev = {
            "name": "Zoom Service",
            "service_time": TZ.localize(datetime(2025, 10, 1, 18, 0, 0)),
            "duration_minutes": 60,
            "description": "",
            "announcements": [],
            "url": "https://zoom.us/j/999888777",
        }
        vevents = _events_from_cal(_parse(events_to_ics([ev])))
        assert "zoom.us" in str(vevents[0].get("URL", ""))

    def test_event_without_url_has_no_url_property(self):
        ev = {
            "name": "In-Person Service",
            "service_time": TZ.localize(datetime(2025, 10, 1, 18, 0, 0)),
            "duration_minutes": 60,
            "description": "",
            "announcements": [],
        }
        vevents = _events_from_cal(_parse(events_to_ics([ev])))
        assert vevents[0].get("URL") is None

    def test_event_with_empty_url_has_no_url_property(self):
        ev = {
            "name": "In-Person Service",
            "service_time": TZ.localize(datetime(2025, 10, 1, 18, 0, 0)),
            "duration_minutes": 60,
            "description": "",
            "announcements": [],
            "url": "",
        }
        vevents = _events_from_cal(_parse(events_to_ics([ev])))
        assert vevents[0].get("URL") is None


# ---------------------------------------------------------------------------
# appointment_cancellation_to_ics
# ---------------------------------------------------------------------------

class TestAppointmentCancellationToIcs:
    def _make_appt(self, dt: datetime | None = None, seq: int | None = None) -> dict:
        confirmed = dt or TZ.localize(datetime(2025, 7, 15, 10, 0, 0))
        appt = {
            "id": "ABC123",
            "official_name": "Pastor Crowdy",
            "confirmed_datetime": confirmed,
            "description": "Discuss upcoming event",
            "duration_minutes": 30,
        }
        if seq is not None:
            appt["sequence"] = seq
        return appt

    def test_returns_bytes(self):
        assert isinstance(appointment_cancellation_to_ics(self._make_appt(), TZ), bytes)

    def test_produces_valid_calendar(self):
        cal = _parse(appointment_cancellation_to_ics(self._make_appt(), TZ))
        assert cal is not None

    def test_calendar_method_is_cancel(self):
        cal = _parse(appointment_cancellation_to_ics(self._make_appt(), TZ))
        assert str(cal.get("METHOD")).upper() == "CANCEL"

    def test_vevent_status_is_cancelled(self):
        vevents = _events_from_cal(_parse(appointment_cancellation_to_ics(self._make_appt(), TZ)))
        assert str(vevents[0]["STATUS"]).upper() == "CANCELLED"

    def test_uid_matches_original_appointment(self):
        appt = self._make_appt()
        orig_uid = str(_events_from_cal(_parse(appointment_to_ics(appt, TZ)))[0]["UID"])
        cancel_uid = str(
            _events_from_cal(_parse(appointment_cancellation_to_ics(appt, TZ)))[0]["UID"]
        )
        assert orig_uid == cancel_uid

    def test_uid_contains_appt_id(self):
        vevents = _events_from_cal(_parse(appointment_cancellation_to_ics(self._make_appt(), TZ)))
        assert "ABC123" in str(vevents[0]["UID"])

    def test_sequence_incremented_from_zero(self):
        vevents = _events_from_cal(_parse(appointment_cancellation_to_ics(self._make_appt(), TZ)))
        assert int(vevents[0]["SEQUENCE"]) == 1

    def test_sequence_incremented_from_existing(self):
        appt = self._make_appt(seq=3)
        vevents = _events_from_cal(_parse(appointment_cancellation_to_ics(appt, TZ)))
        assert int(vevents[0]["SEQUENCE"]) == 4

    def test_cancellation_sequence_higher_than_original(self):
        appt = self._make_appt()
        orig_seq = int(_events_from_cal(_parse(appointment_to_ics(appt, TZ)))[0]["SEQUENCE"])
        cancel_seq = int(
            _events_from_cal(_parse(appointment_cancellation_to_ics(appt, TZ)))[0]["SEQUENCE"]
        )
        assert cancel_seq > orig_seq

    def test_dtstart_matches_confirmed_datetime(self):
        confirmed = TZ.localize(datetime(2025, 8, 1, 14, 30, 0))
        vevents = _events_from_cal(
            _parse(appointment_cancellation_to_ics(self._make_appt(confirmed), TZ))
        )
        assert vevents[0].decoded("dtstart") == confirmed

    def test_summary_includes_official_name(self):
        vevents = _events_from_cal(_parse(appointment_cancellation_to_ics(self._make_appt(), TZ)))
        assert "Pastor Crowdy" in str(vevents[0]["SUMMARY"])

    def test_no_alarms_in_cancellation(self):
        vevents = _events_from_cal(_parse(appointment_cancellation_to_ics(self._make_appt(), TZ)))
        assert len(_alarms_from_vevent(vevents[0])) == 0

    def test_accepts_iso_string_datetime(self):
        appt = self._make_appt()
        appt["confirmed_datetime"] = "2025-09-01T09:00:00-04:00"
        result = appointment_cancellation_to_ics(appt, TZ)
        assert isinstance(result, bytes)

    def test_naive_datetime_gets_localised(self):
        appt = self._make_appt()
        appt["confirmed_datetime"] = datetime(2025, 9, 1, 9, 0, 0)  # naive
        result = appointment_cancellation_to_ics(appt, TZ)
        assert isinstance(result, bytes)

"""Tests for hebrew_calendar.py — convocation date computation."""

import sys
from datetime import datetime
from pathlib import Path

import pytz
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from hebrew_calendar import (
    BIBLICAL_MONTH,
    CONVOCATION_DEFS,
    _full_name,
    _phase_key,
    all_upcoming_events,
    convocations_for_hebrew_year,
    sabbath_events,
    service_phases,
    upcoming_convocation_events,
)

TZ = pytz.timezone("America/New_York")


# ---------------------------------------------------------------------------
# Biblical month names
# ---------------------------------------------------------------------------

class TestBiblicalMonthNames:
    def test_nisan_is_abib(self):
        assert BIBLICAL_MONTH[1] == "Abib"

    def test_tishri_is_ethanim(self):
        assert BIBLICAL_MONTH[7] == "Ethanim"

    def test_sivan_unchanged(self):
        assert BIBLICAL_MONTH[3] == "Sivan"

    def test_all_twelve_months_present(self):
        assert len(BIBLICAL_MONTH) == 12


# ---------------------------------------------------------------------------
# Event full-name builder
# ---------------------------------------------------------------------------

class TestFullName:
    def test_sabbath_eve(self):
        assert _full_name("Sabbath", None, "Eve") == "God's Holy Convocation--Sabbath Eve"

    def test_sabbath_morning(self):
        assert _full_name("Sabbath", None, "Morning") == "God's Holy Convocation--Sabbath Morning"

    def test_passover_at_even(self):
        assert _full_name("Passover", None, "at Even") == "God's Holy Convocation--Passover at Even"

    def test_unleavened_bread_opening_day(self):
        result = _full_name("Feast of Unleavened Bread", "Opening", "Day")
        assert result == "God's Holy Convocation--Feast of Unleavened Bread Opening Day"

    def test_unleavened_bread_closing_eve(self):
        result = _full_name("Feast of Unleavened Bread", "Closing", "Eve")
        assert result == "God's Holy Convocation--Feast of Unleavened Bread Closing Eve"

    def test_succoth_opening_eve(self):
        result = _full_name("Succoth", "Opening", "Eve")
        assert result == "God's Holy Convocation--Succoth Opening Eve"

    def test_succoth_closing_day(self):
        result = _full_name("Succoth", "Closing", "Day")
        assert result == "God's Holy Convocation--Succoth Closing Day"

    def test_rosh_hashanah_morning(self):
        result = _full_name("Rosh Hashanah", None, "Morning")
        assert result == "God's Holy Convocation--Rosh Hashanah Morning"


# ---------------------------------------------------------------------------
# Convocation definitions structure
# ---------------------------------------------------------------------------

class TestConvocationDefs:
    EXPECTED_KEYS = {
        "passover",
        "unleavened_bread_opening",
        "unleavened_bread_closing",
        "shavuot",
        "rosh_hashanah",
        "yom_kippur",
        "succoth_opening",
        "succoth_closing",
    }

    def test_all_expected_convocations_defined(self):
        keys = {d["key"] for d in CONVOCATION_DEFS}
        assert keys == self.EXPECTED_KEYS

    def test_passover_is_nisan_14(self):
        p = next(d for d in CONVOCATION_DEFS if d["key"] == "passover")
        assert p["hebrew_month"] == 1
        assert p["hebrew_day"] == 14

    def test_passover_service_at_3pm(self):
        p = next(d for d in CONVOCATION_DEFS if d["key"] == "passover")
        svc = p["services"][0]
        assert svc["hour"] == 15
        assert svc["minute"] == 0

    def test_passover_no_day_offset(self):
        p = next(d for d in CONVOCATION_DEFS if d["key"] == "passover")
        assert p["services"][0]["day_offset"] == 0

    def test_passover_has_no_phase(self):
        p = next(d for d in CONVOCATION_DEFS if d["key"] == "passover")
        assert p["phase"] is None

    def test_eve_services_at_6pm(self):
        for defn in CONVOCATION_DEFS:
            for svc in defn["services"]:
                if svc["label"] == "Eve":
                    assert svc["hour"] == 18, f"{defn['key']} Eve not at 18:00"
                    assert svc["day_offset"] == -1, f"{defn['key']} Eve day_offset not -1"

    def test_morning_services_at_11am(self):
        for defn in CONVOCATION_DEFS:
            for svc in defn["services"]:
                if svc["label"] in ("Morning", "Day"):
                    assert svc["hour"] == 11, f"{defn['key']} Morning/Day not at 11:00"
                    assert svc["day_offset"] == 0, f"{defn['key']} Morning/Day day_offset not 0"

    def test_yom_kippur_is_tishri_10(self):
        yk = next(d for d in CONVOCATION_DEFS if d["key"] == "yom_kippur")
        assert yk["hebrew_month"] == 7
        assert yk["hebrew_day"] == 10

    def test_rosh_hashanah_is_tishri_1(self):
        rh = next(d for d in CONVOCATION_DEFS if d["key"] == "rosh_hashanah")
        assert rh["hebrew_month"] == 7
        assert rh["hebrew_day"] == 1

    def test_shavuot_is_sivan_6(self):
        sh = next(d for d in CONVOCATION_DEFS if d["key"] == "shavuot")
        assert sh["hebrew_month"] == 3
        assert sh["hebrew_day"] == 6

    def test_succoth_opening_is_tishri_15(self):
        s = next(d for d in CONVOCATION_DEFS if d["key"] == "succoth_opening")
        assert s["hebrew_month"] == 7
        assert s["hebrew_day"] == 15

    def test_succoth_closing_is_tishri_22(self):
        s = next(d for d in CONVOCATION_DEFS if d["key"] == "succoth_closing")
        assert s["hebrew_month"] == 7
        assert s["hebrew_day"] == 22

    def test_feast_of_unleavened_bread_opening_is_nisan_15(self):
        f = next(d for d in CONVOCATION_DEFS if d["key"] == "unleavened_bread_opening")
        assert f["hebrew_month"] == 1
        assert f["hebrew_day"] == 15

    def test_feast_of_unleavened_bread_closing_is_nisan_21(self):
        f = next(d for d in CONVOCATION_DEFS if d["key"] == "unleavened_bread_closing")
        assert f["hebrew_month"] == 1
        assert f["hebrew_day"] == 21

    def test_default_notification_minutes(self):
        for defn in CONVOCATION_DEFS:
            for svc in defn["services"]:
                assert svc["notification_minutes"] == 90


# ---------------------------------------------------------------------------
# convocations_for_hebrew_year
# ---------------------------------------------------------------------------

class TestConvocationsForHebrewYear:
    HEBREW_YEAR = 5785  # 2024-2025

    def setup_method(self):
        self.events = convocations_for_hebrew_year(self.HEBREW_YEAR, TZ)

    def test_returns_list(self):
        assert isinstance(self.events, list)

    def test_produces_events(self):
        assert len(self.events) > 0

    def test_all_events_have_required_keys(self):
        required = {"key", "name", "service_time", "notification_time", "type", "biblical_month"}
        for ev in self.events:
            assert required <= ev.keys(), f"Missing keys in {ev.get('name')}"

    def test_all_events_have_timezone(self):
        for ev in self.events:
            assert ev["service_time"].tzinfo is not None

    def test_notification_is_90_minutes_before_service(self):
        for ev in self.events:
            delta = ev["service_time"] - ev["notification_time"]
            assert delta.total_seconds() == 90 * 60, f"{ev['name']} notification gap wrong"

    def test_passover_service_time_is_3pm(self):
        passover = [e for e in self.events if "Passover" in e["name"]]
        assert passover, "No Passover event found"
        for p in passover:
            local = p["service_time"].astimezone(TZ)
            assert local.hour == 15
            assert local.minute == 0

    def test_passover_biblical_month_is_abib(self):
        passover = next(e for e in self.events if "Passover" in e["name"])
        assert passover["biblical_month"] == "Abib"

    def test_rosh_hashanah_biblical_month_is_ethanim(self):
        rh = next(e for e in self.events if "Rosh Hashanah" in e["name"])
        assert rh["biblical_month"] == "Ethanim"

    def test_eve_services_at_6pm(self):
        # "at Even" is Passover's special 3pm service — exclude it from this check
        for ev in self.events:
            if ev["label"] == "Eve":
                local = ev["service_time"].astimezone(TZ)
                assert local.hour == 18, f"{ev['name']} Eve not at 18:00"

    def test_morning_day_services_at_11am(self):
        for ev in self.events:
            if ev["name"].endswith("Morning") or ev["name"].endswith("Day"):
                local = ev["service_time"].astimezone(TZ)
                assert local.hour == 11, f"{ev['name']} not at 11:00"

    def test_event_type_is_convocation(self):
        for ev in self.events:
            assert ev["type"] == "convocation"

    def test_announcements_list_starts_empty(self):
        for ev in self.events:
            assert ev["announcements"] == []

    def test_event_names_start_with_gods_holy_convocation(self):
        for ev in self.events:
            assert ev["name"].startswith("God's Holy Convocation--"), \
                f"Unexpected name: {ev['name']}"

    def test_unleavened_bread_has_opening_and_closing(self):
        ub_names = [e["name"] for e in self.events if "Unleavened Bread" in e["name"]]
        assert any("Opening" in n for n in ub_names)
        assert any("Closing" in n for n in ub_names)

    def test_succoth_has_opening_and_closing(self):
        s_names = [e["name"] for e in self.events if "Succoth" in e["name"]]
        assert any("Opening" in n for n in s_names)
        assert any("Closing" in n for n in s_names)


# ---------------------------------------------------------------------------
# sabbath_events
# ---------------------------------------------------------------------------

class TestSabbathEvents:
    def setup_method(self):
        self.events = sabbath_events(TZ, days_ahead=14)

    def test_returns_list(self):
        assert isinstance(self.events, list)

    def test_produces_both_eve_and_morning(self):
        names = [e["name"] for e in self.events]
        assert any("Sabbath Eve" in n for n in names)
        assert any("Sabbath Morning" in n for n in names)

    def test_eve_is_on_friday(self):
        for ev in self.events:
            if "Sabbath Eve" in ev["name"]:
                assert ev["service_time"].astimezone(TZ).weekday() == 4

    def test_morning_is_on_saturday(self):
        for ev in self.events:
            if "Sabbath Morning" in ev["name"]:
                assert ev["service_time"].astimezone(TZ).weekday() == 5

    def test_eve_service_at_6pm(self):
        for ev in self.events:
            if "Sabbath Eve" in ev["name"]:
                assert ev["service_time"].astimezone(TZ).hour == 18

    def test_morning_service_at_11am(self):
        for ev in self.events:
            if "Sabbath Morning" in ev["name"]:
                assert ev["service_time"].astimezone(TZ).hour == 11

    def test_notification_90_minutes_before(self):
        for ev in self.events:
            delta = (ev["service_time"] - ev["notification_time"]).total_seconds()
            assert delta == 90 * 60

    def test_all_events_in_future(self):
        now = datetime.now(TZ)
        for ev in self.events:
            assert ev["service_time"] > now

    def test_event_type_is_convocation(self):
        for ev in self.events:
            assert ev["type"] == "convocation"

    def test_days_ahead_respected(self):
        events_7 = sabbath_events(TZ, days_ahead=7)
        events_14 = sabbath_events(TZ, days_ahead=14)
        assert len(events_14) >= len(events_7)

    def test_keys_are_unique(self):
        keys = [e["key"] for e in self.events]
        assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# all_upcoming_events — integration
# ---------------------------------------------------------------------------

class TestAllUpcomingEvents:
    def test_returns_sorted_by_service_time(self):
        events = all_upcoming_events(TZ, days_ahead=60)
        times = [e["service_time"] for e in events]
        assert times == sorted(times)

    def test_contains_sabbath_events(self):
        events = all_upcoming_events(TZ, days_ahead=14)
        names = [e["name"] for e in events]
        assert any("Sabbath" in n for n in names)

    def test_excludes_sunday_prayer(self):
        # Sunday Morning Prayer is a special event (events.yaml), not a convocation.
        events = all_upcoming_events(TZ, days_ahead=30)
        names = [e["name"] for e in events]
        assert not any("Sunday Morning Prayer" in n for n in names)

    def test_all_events_are_future(self):
        now = datetime.now(TZ)
        events = all_upcoming_events(TZ, days_ahead=30)
        for ev in events:
            assert ev["service_time"] > now

    def test_days_ahead_limits_results(self):
        events_7 = all_upcoming_events(TZ, days_ahead=7)
        events_60 = all_upcoming_events(TZ, days_ahead=60)
        assert len(events_60) > len(events_7)


# ---------------------------------------------------------------------------
# Per-service (per-phase) links
# ---------------------------------------------------------------------------

class TestPhaseKeys:
    def test_phase_key_format(self):
        assert _phase_key("sabbath", "Eve") == "sabbath::Eve"

    def test_sabbath_events_carry_phase_key(self):
        evs = sabbath_events(TZ, days_ahead=21)
        keys = {e["phase_key"] for e in evs}
        assert keys <= {"sabbath::Eve", "sabbath::Morning"}
        assert keys  # at least one within three weeks

    def test_eve_and_morning_have_distinct_phase_keys(self):
        evs = sabbath_events(TZ, days_ahead=21)
        eve = next((e for e in evs if e["label"] == "Eve"), None)
        morning = next((e for e in evs if e["label"] == "Morning"), None)
        if eve and morning:
            assert eve["phase_key"] != morning["phase_key"]

    def test_convocation_events_carry_phase_key(self):
        # Use a Hebrew year far enough to always produce events.
        evs = convocations_for_hebrew_year(5786, TZ)
        assert all("phase_key" in e for e in evs)

    def test_convocation_phase_key_matches_convocation_and_label(self):
        evs = convocations_for_hebrew_year(5786, TZ)
        for e in evs:
            assert e["phase_key"] == f"{e['convocation_key']}::{e['label']}"


class TestServicePhases:
    def test_includes_sabbath_eve_and_morning(self):
        keys = {p["phase_key"] for p in service_phases()}
        assert "sabbath::Eve" in keys
        assert "sabbath::Morning" in keys

    def test_includes_every_convocation_service(self):
        keys = {p["phase_key"] for p in service_phases()}
        for defn in CONVOCATION_DEFS:
            for svc in defn["services"]:
                assert f"{defn['key']}::{svc['label']}" in keys

    def test_all_phase_keys_unique(self):
        keys = [p["phase_key"] for p in service_phases()]
        assert len(keys) == len(set(keys))

    def test_every_phase_has_display(self):
        assert all(p.get("display") for p in service_phases())

    def test_sabbath_listed_first(self):
        phases = service_phases()
        assert phases[0]["phase_key"] == "sabbath::Eve"
        assert phases[1]["phase_key"] == "sabbath::Morning"

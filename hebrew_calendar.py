"""Hebrew calendar utilities — compute convocation service datetimes."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytz
from pyluach import dates as heb_dates

# Biblical / historic month names used in announcements
BIBLICAL_MONTH = {
    1: "Abib",     # Nisan
    2: "Zif",      # Iyyar
    3: "Sivan",
    4: "Thamuz",   # Tammuz
    5: "Av",
    6: "Elul",
    7: "Ethanim",  # Tishri
    8: "Bul",      # Cheshvan
    9: "Chisleu",  # Kislev
    10: "Tebeth",  # Tevet
    11: "Sebat",   # Shevat
    12: "Adar",
}

# ---------------------------------------------------------------------------
# Convocation definitions
# day_offset: 0 = the Gregorian date that pyluach returns for the Hebrew date;
#             -1 = the Gregorian day BEFORE (i.e. the "Eve" evening that opens
#                  the Hebrew day at sunset).
# ---------------------------------------------------------------------------
CONVOCATION_DEFS: list[dict[str, Any]] = [
    {
        "key": "passover",
        "name": "Passover",
        "phase": None,
        "hebrew_month": 1,
        "hebrew_day": 14,
        "services": [
            {"label": "at Even", "day_offset": 0, "hour": 15, "minute": 0, "notification_minutes": 90},
        ],
    },
    {
        "key": "unleavened_bread_opening",
        "name": "Feast of Unleavened Bread",
        "phase": "Opening",
        "hebrew_month": 1,
        "hebrew_day": 15,
        "services": [
            {"label": "Day", "day_offset": 0, "hour": 11, "minute": 0, "notification_minutes": 90},
        ],
    },
    {
        "key": "unleavened_bread_closing",
        "name": "Feast of Unleavened Bread",
        "phase": "Closing",
        "hebrew_month": 1,
        "hebrew_day": 21,
        "services": [
            {"label": "Eve", "day_offset": -1, "hour": 18, "minute": 0, "notification_minutes": 90},
            {"label": "Day", "day_offset": 0, "hour": 11, "minute": 0, "notification_minutes": 90},
        ],
    },
    {
        "key": "shavuot",
        "name": "Shavuot",
        "phase": None,
        "hebrew_month": 3,
        "hebrew_day": 6,
        "services": [
            {"label": "Eve", "day_offset": -1, "hour": 18, "minute": 0, "notification_minutes": 90},
            {"label": "Morning", "day_offset": 0, "hour": 11, "minute": 0, "notification_minutes": 90},
        ],
    },
    {
        "key": "rosh_hashanah",
        "name": "Rosh Hashanah",
        "phase": None,
        "hebrew_month": 7,
        "hebrew_day": 1,
        "services": [
            {"label": "Eve", "day_offset": -1, "hour": 18, "minute": 0, "notification_minutes": 90},
            {"label": "Morning", "day_offset": 0, "hour": 11, "minute": 0, "notification_minutes": 90},
        ],
    },
    {
        "key": "yom_kippur",
        "name": "Yom Kippur",
        "phase": None,
        "hebrew_month": 7,
        "hebrew_day": 10,
        "services": [
            {"label": "Eve", "day_offset": -1, "hour": 18, "minute": 0, "notification_minutes": 90},
            {"label": "Morning", "day_offset": 0, "hour": 11, "minute": 0, "notification_minutes": 90},
        ],
    },
    {
        "key": "succoth_opening",
        "name": "Succoth",
        "phase": "Opening",
        "hebrew_month": 7,
        "hebrew_day": 15,
        "services": [
            {"label": "Eve", "day_offset": -1, "hour": 18, "minute": 0, "notification_minutes": 90},
            {"label": "Day", "day_offset": 0, "hour": 11, "minute": 0, "notification_minutes": 90},
        ],
    },
    {
        "key": "succoth_closing",
        "name": "Succoth",
        "phase": "Closing",
        "hebrew_month": 7,
        "hebrew_day": 22,
        "services": [
            {"label": "Eve", "day_offset": -1, "hour": 18, "minute": 0, "notification_minutes": 90},
            {"label": "Day", "day_offset": 0, "hour": 11, "minute": 0, "notification_minutes": 90},
        ],
    },
]


def _full_name(name: str, phase: str | None, label: str) -> str:
    phase_str = f" {phase}" if phase else ""
    return f"God's Holy Convocation--{name}{phase_str} {label}"


def convocations_for_hebrew_year(
    hebrew_year: int, tz: pytz.BaseTzInfo
) -> list[dict[str, Any]]:
    """Return all convocation service events for a single Hebrew year."""
    events: list[dict[str, Any]] = []
    for defn in CONVOCATION_DEFS:
        try:
            base_gdate: date = heb_dates.HebrewDate(
                hebrew_year, defn["hebrew_month"], defn["hebrew_day"]
            ).to_pydate()
        except Exception:
            continue

        for svc in defn["services"]:
            svc_date = base_gdate + timedelta(days=svc["day_offset"])
            svc_dt = tz.localize(
                datetime(svc_date.year, svc_date.month, svc_date.day, svc["hour"], svc["minute"])
            )
            notif_dt = svc_dt - timedelta(minutes=svc["notification_minutes"])
            full = _full_name(defn["name"], defn.get("phase"), svc["label"])
            month_name = BIBLICAL_MONTH.get(defn["hebrew_month"], "")
            events.append(
                {
                    "key": f"{defn['key']}_{svc['label'].lower().replace(' ', '_')}_{svc_date.isoformat()}",
                    "name": full,
                    "convocation_key": defn["key"],
                    "convocation_name": defn["name"],
                    "phase": defn.get("phase"),
                    "label": svc["label"],
                    "hebrew_month": defn["hebrew_month"],
                    "hebrew_day": defn["hebrew_day"],
                    "biblical_month": month_name,
                    "service_time": svc_dt,
                    "notification_time": notif_dt,
                    "duration_minutes": 60,
                    "type": "convocation",
                    "hebrew_year": hebrew_year,
                    "announcements": [],
                }
            )
    return events


def upcoming_convocation_events(tz: pytz.BaseTzInfo, days_ahead: int = 400) -> list[dict[str, Any]]:
    """All convocation events (excluding Sabbath) within the next *days_ahead* days."""
    now = datetime.now(tz)
    cutoff = now + timedelta(days=days_ahead)
    today_heb = heb_dates.HebrewDate.today()
    all_events: list[dict[str, Any]] = []
    for hy in [today_heb.year, today_heb.year + 1]:
        all_events.extend(convocations_for_hebrew_year(hy, tz))
    return [e for e in all_events if now <= e["service_time"] <= cutoff]


def sabbath_events(tz: pytz.BaseTzInfo, days_ahead: int = 90) -> list[dict[str, Any]]:
    """Generate weekly Sabbath Eve (Fri 6pm) and Morning (Sat 11am) events."""
    now = datetime.now(tz)
    events: list[dict[str, Any]] = []
    current = now.date()
    end = current + timedelta(days=days_ahead)
    while current <= end:
        wd = current.weekday()
        if wd == 4:  # Friday → Sabbath Eve
            svc_dt = tz.localize(datetime(current.year, current.month, current.day, 18, 0))
            notif_dt = svc_dt - timedelta(minutes=90)
            if svc_dt > now:
                events.append({
                    "key": f"sabbath_eve_{current.isoformat()}",
                    "name": "God's Holy Convocation--Sabbath Eve",
                    "convocation_key": "sabbath",
                    "convocation_name": "Sabbath",
                    "phase": None,
                    "label": "Eve",
                    "service_time": svc_dt,
                    "notification_time": notif_dt,
                    "duration_minutes": 90,
                    "type": "convocation",
                    "announcements": [],
                })
        elif wd == 5:  # Saturday → Sabbath Morning
            svc_dt = tz.localize(datetime(current.year, current.month, current.day, 11, 0))
            notif_dt = svc_dt - timedelta(minutes=90)
            if svc_dt > now:
                events.append({
                    "key": f"sabbath_morning_{current.isoformat()}",
                    "name": "God's Holy Convocation--Sabbath Morning",
                    "convocation_key": "sabbath",
                    "convocation_name": "Sabbath",
                    "phase": None,
                    "label": "Morning",
                    "service_time": svc_dt,
                    "notification_time": notif_dt,
                    "duration_minutes": 90,
                    "type": "convocation",
                    "announcements": [],
                })
        current += timedelta(days=1)
    return events


def sunday_prayer_events(tz: pytz.BaseTzInfo, days_ahead: int = 90) -> list[dict[str, Any]]:
    """Generate weekly Sunday Morning Prayer events (Sat 6pm notification)."""
    now = datetime.now(tz)
    events: list[dict[str, Any]] = []
    current = now.date()
    end = current + timedelta(days=days_ahead)
    while current <= end:
        if current.weekday() == 6:  # Sunday
            svc_dt = tz.localize(datetime(current.year, current.month, current.day, 6, 0))
            notif_dt = svc_dt - timedelta(minutes=720)
            if svc_dt > now:
                events.append({
                    "key": f"sunday_prayer_{current.isoformat()}",
                    "name": "Sunday Morning Prayer",
                    "type": "special",
                    "service_time": svc_dt,
                    "notification_time": notif_dt,
                    "duration_minutes": 20,
                    "description": "Weekly Sunday Morning Prayer service — all are welcome.",
                    "announcements": [],
                })
        current += timedelta(days=1)
    return events


def all_upcoming_events(tz: pytz.BaseTzInfo, days_ahead: int = 90) -> list[dict[str, Any]]:
    """Merge and sort all event types."""
    events: list[dict[str, Any]] = []
    events.extend(sabbath_events(tz, days_ahead))
    events.extend(upcoming_convocation_events(tz, days_ahead))
    events.extend(sunday_prayer_events(tz, days_ahead))
    events.sort(key=lambda e: e["service_time"])
    return events

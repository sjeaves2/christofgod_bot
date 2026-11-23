"""Which appointment times can be offered: office hours, feast days, clashes.

Pure functions, no Telegram and no storage, so the rules can be tested
directly. handlers/appointments.py builds the keyboards from these and
supplies the busy intervals, because it is the module that knows what each
appointment status means.

THE RULES (agreed with the user 2026-11-20)
-------------------------------------------
* Office hours, in the CHURCH timezone: Monday–Thursday 09:00–17:00, Friday
  09:00–13:00, closed at the weekend. Fridays end at 1 PM because the Sabbath
  begins on Friday evening. The church uses 6:00 PM as a FIXED reference time
  for that, not sunset, so nothing here calculates sunset — do not add it.
* Slots start every 30 minutes and must END by closing time, so the last
  Monday slot is 16:30 and the last Friday slot 12:30.
* A weekday that is an annual feast day is closed, from the convocation dates
  the bot already computes for its reminders. "Eve" services are skipped: they
  are at 18:00 the evening before, after hours, and the day they fall on is an
  ordinary working day.
* US federal holidays are closed too (added at the user's request 2026-11-23,
  Juneteenth included), on the day they are OBSERVED: one falling on a
  Saturday is taken the Friday before, on a Sunday the Monday after — the
  federal rule, and the day an office actually shuts.
* Past slots, and slots beyond the booking horizon, are never offered.
* A slot that overlaps a busy interval is not offered. Which appointments are
  busy is the caller's decision.

`appointments.hours` in config.yaml can replace the default hours; see
parse_hours() for the format. When it is given it REPLACES the default
outright, so a day it leaves out is closed.

DATES ARE THE VIEWER'S, HOURS ARE THE CHURCH'S
----------------------------------------------
A member abroad sees the calendar in their own dates and times. Office hours
are church hours, so 09:00 New York is 16:00 in Johannesburg, and for a member
far enough away a church day's slots can straddle two of their dates. So the
slots for one of the VIEWER's dates are gathered from the church dates either
side and kept when they land on that date in the viewer's timezone.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Iterable

import pytz

logger = logging.getLogger(__name__)

SLOT_MINUTES = 30

# weekday (Monday = 0) -> (opening, closing) in minutes after midnight.
Hours = dict[int, tuple[int, int]]

DEFAULT_HOURS: Hours = {
    0: (9 * 60, 17 * 60),
    1: (9 * 60, 17 * 60),
    2: (9 * 60, 17 * 60),
    3: (9 * 60, 17 * 60),
    4: (9 * 60, 13 * 60),   # Friday: ends at 1 PM, before the Sabbath
}

_DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday",
              "saturday", "sunday")
_RANGE_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_hours(raw) -> Hours:
    """Office hours from config, e.g. ``{"monday": "09:00-17:00", ...}``.

    Raises ValueError on anything it cannot read, naming the problem. A day
    that is absent is closed. Callers that must keep running (the bot) use
    configured_hours(), which falls back to DEFAULT_HOURS.
    """
    if not isinstance(raw, dict) or not raw:
        raise ValueError("appointments.hours must be a mapping of day names "
                         "to \"HH:MM-HH:MM\"")
    hours: Hours = {}
    for name, value in raw.items():
        day = str(name).strip().lower()
        if day not in _DAY_NAMES:
            raise ValueError(f"appointments.hours: unknown day {name!r}")
        m = _RANGE_RE.match(str(value))
        if not m:
            raise ValueError(f"appointments.hours.{day}: {value!r} is not "
                             "\"HH:MM-HH:MM\"")
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59 or (h2 == 24 and m2):
            raise ValueError(f"appointments.hours.{day}: {value!r} is not a "
                             "valid time range")
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if end - start < SLOT_MINUTES:
            raise ValueError(f"appointments.hours.{day}: {value!r} is shorter "
                             f"than one {SLOT_MINUTES}-minute slot")
        hours[_DAY_NAMES.index(day)] = (start, end)
    return hours


def configured_hours(raw) -> Hours:
    """parse_hours(raw), or DEFAULT_HOURS when *raw* is absent or unreadable.

    A typo in config must not take booking down for everyone, so a bad value
    is logged as an ERROR — loudly enough to find — and the defaults apply.
    """
    if raw is None:
        return dict(DEFAULT_HOURS)
    try:
        return parse_hours(raw)
    except ValueError as exc:
        logger.error("%s — using the default office hours instead.", exc)
        return dict(DEFAULT_HOURS)


def feast_dates(tz: pytz.BaseTzInfo) -> set[date]:
    """Church dates of the annual feast days in the coming year.

    The date of every convocation service except an "Eve" (see the module
    docstring). Never raises: if the calendar cannot be computed, booking
    carries on without the feast-day rule and the failure is logged.
    """
    try:
        import hebrew_calendar
        events = hebrew_calendar.upcoming_convocation_events(tz)
    except Exception:
        logger.exception("Could not compute feast days; not hiding any.")
        return set()
    return {e["service_time"].astimezone(tz).date() for e in events
            if e.get("label") != "Eve"}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The *n*th *weekday* (Monday = 0) of a month; n = -1 is the last."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """Saturday holidays are observed on Friday, Sunday ones on Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def us_federal_holidays(year: int) -> dict[date, str]:
    """The eleven US federal holidays of *year*, keyed by OBSERVED date.

    New Year's Day of the FOLLOWING year is included when it is observed on
    31 December of this one (1 January on a Saturday), since that closure
    falls in this year.
    """
    MON, THU = 0, 3
    days = {
        _observed(date(year, 1, 1)): "New Year's Day",
        _nth_weekday(year, 1, MON, 3): "Martin Luther King Jr. Day",
        _nth_weekday(year, 2, MON, 3): "Washington's Birthday",
        _nth_weekday(year, 5, MON, -1): "Memorial Day",
        _observed(date(year, 6, 19)): "Juneteenth",
        _observed(date(year, 7, 4)): "Independence Day",
        _nth_weekday(year, 9, MON, 1): "Labor Day",
        _nth_weekday(year, 10, MON, 2): "Columbus Day",
        _observed(date(year, 11, 11)): "Veterans Day",
        _nth_weekday(year, 11, THU, 4): "Thanksgiving Day",
        _observed(date(year, 12, 25)): "Christmas Day",
    }
    next_new_year = _observed(date(year + 1, 1, 1))
    if next_new_year.year == year:
        days[next_new_year] = "New Year's Day"
    return {d: name for d, name in days.items() if d.year == year}


def closed_dates(tz: pytz.BaseTzInfo, today: date) -> set[date]:
    """Every church date that is closed for a reason other than the weekday:
    feast days and US federal holidays, from this year through next (the
    booking horizon is six months, so that always covers it)."""
    closed = set(feast_dates(tz))
    for year in (today.year, today.year + 1):
        closed.update(us_federal_holidays(year))
    return closed


def day_slots(day: date, hours: Hours, tz: pytz.BaseTzInfo) -> list[datetime]:
    """Every slot start on church date *day*, ignoring all other rules."""
    span = hours.get(day.weekday())
    if not span:
        return []
    start, end = span
    out = []
    minute = start
    while minute + SLOT_MINUTES <= end:
        naive = datetime(day.year, day.month, day.day) + timedelta(minutes=minute)
        out.append(tz.localize(naive))
        minute += SLOT_MINUTES
    return out


def _overlaps(start: datetime, busy: Iterable[tuple[datetime, datetime]]) -> bool:
    end = start + timedelta(minutes=SLOT_MINUTES)
    return any(start < b_end and b_start < end for b_start, b_end in busy)


def free_slots_for_viewer_day(
    viewer_day: date,
    viewer_tz: pytz.BaseTzInfo,
    *,
    church_tz: pytz.BaseTzInfo,
    hours: Hours,
    closed: set[date],
    busy: list[tuple[datetime, datetime]],
    now: datetime,
    latest: datetime,
) -> list[datetime]:
    """Bookable slot starts that fall on *viewer_day* in the viewer's timezone.

    Returned in the church timezone, sorted. A slot is bookable when it is in
    office hours on a church date that is not in *closed*, starts after *now*,
    starts no later than *latest*, and overlaps nothing in *busy*.
    """
    out = []
    for offset in (-1, 0, 1):
        church_day = viewer_day + timedelta(days=offset)
        if church_day in closed:
            continue
        for slot in day_slots(church_day, hours, church_tz):
            if slot.astimezone(viewer_tz).date() != viewer_day:
                continue
            if slot <= now or slot > latest or _overlaps(slot, busy):
                continue
            out.append(slot)
    return sorted(out)


def bookable_viewer_days(year: int, month: int, viewer_tz: pytz.BaseTzInfo,
                         **rules) -> set[date]:
    """The viewer's dates in *year*/*month* that have at least one free slot."""
    first = date(year, month, 1)
    days = set()
    d = first
    while d.month == month:
        if free_slots_for_viewer_day(d, viewer_tz, **rules):
            days.add(d)
        d += timedelta(days=1)
    return days

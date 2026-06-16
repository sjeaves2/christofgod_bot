"""ICS (iCalendar) file generation for events and appointments."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from io import BytesIO
from typing import Any

import pytz
from icalendar import Calendar, Event, vText


def _make_calendar(name: str) -> Calendar:
    cal = Calendar()
    cal.add("prodid", f"-//Christ of God Ministries Bot//{name}//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", vText(name))
    return cal


def events_to_ics(events: list[dict[str, Any]], calendar_name: str = "Christ of God Ministries Events") -> bytes:
    """Convert a list of event dicts to ICS bytes."""
    cal = _make_calendar(calendar_name)
    for evt in events:
        vevent = Event()
        vevent.add("summary", evt.get("name", "Event"))
        svc_time: datetime = evt["service_time"]
        duration = timedelta(minutes=int(evt.get("duration_minutes", 60)))
        vevent.add("dtstart", svc_time)
        vevent.add("dtend", svc_time + duration)
        vevent.add("uid", str(uuid.uuid4()))
        if evt.get("description"):
            vevent.add("description", evt["description"])
        if evt.get("announcements"):
            note = "\n".join(evt["announcements"])
            existing = vevent.get("description", "")
            vevent["description"] = (str(existing) + "\n\n" + note).strip() if existing else note
        cal.add_component(vevent)
    return cal.to_ical()


def appointment_to_ics(
    appt: dict[str, Any],
    tz: pytz.BaseTzInfo,
    organizer_name: str = "Christ of God Ministries Bot",
) -> bytes:
    """Generate a single-event ICS for a confirmed appointment."""
    cal = _make_calendar("Appointment")
    vevent = Event()
    vevent.add("summary", f"Meeting with {appt['official_name']}")

    appt_dt: datetime = appt["confirmed_datetime"]
    if appt_dt.tzinfo is None:
        appt_dt = tz.localize(appt_dt)
    duration = timedelta(minutes=int(appt.get("duration_minutes", 30)))
    vevent.add("dtstart", appt_dt)
    vevent.add("dtend", appt_dt + duration)
    vevent.add("uid", f"appt-{appt['id']}@christofgodbot")
    vevent.add("description", appt.get("description", ""))
    cal.add_component(vevent)
    return cal.to_ical()
